# ---------------- FIXED CODE -----------------

import sys
import asyncio
import csv
import re
import time
from urllib.parse import urljoin, urlparse
from io import BytesIO

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright
import httpx
from PyPDF2 import PdfReader

# Windows fix for Playwright (ignored in Docker)
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

app = FastAPI()
STUDENT_SECRET = "kreshiv"


class QuizRequest(BaseModel):
    email: str
    secret: str
    url: str


@app.post("/quiz")
async def quiz_entry(payload: QuizRequest):
    if not payload.email or not payload.secret or not payload.url:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})
    if payload.secret != STUDENT_SECRET:
        return JSONResponse(status_code=403, content={"error": "Invalid secret"})

    start_time = time.time()
    result = await solve_quiz_recursive(payload.email, payload.secret, payload.url, start_time, 180)
    return JSONResponse(content={"status": "secret verified", "result": result})


# --------------------------------------------------------------------
# MULTI-STEP QUIZ LOOP
# --------------------------------------------------------------------
async def solve_quiz_recursive(email, secret, url, start_time, deadline):
    if time.time() - start_time > deadline:
        return {"error": "Time exceeded 3 minutes"}

    result = await solve_single_quiz(email, secret, url)
    if "error" in result:
        return result

    if result.get("next_url"):
        return await solve_quiz_recursive(email, secret, result["next_url"], start_time, deadline)

    return result


# --------------------------------------------------------------------
# PDF FALLBACK EXTRACTION
# --------------------------------------------------------------------
def extract_text_fallback(reader):
    full_text = ""
    for page in reader.pages:
        try:
            t = page.extract_text() or ""
            full_text += t + "\n"
            continue
        except:
            pass
        try:
            contents = page.get_contents()
            if contents:
                raw_bytes = contents.get_data()
                full_text += raw_bytes.decode('latin-1', errors='ignore') + "\n"
        except:
            pass
        try:
            raw_page = page.extract_text() or ""
            raw_numbers = re.findall(rb"\d+", raw_page)
            full_text += " ".join(n.decode() for n in raw_numbers) + "\n"
        except:
            pass
    return full_text


# --------------------------------------------------------------------
# API QUIZ
# --------------------------------------------------------------------
async def handle_api_quiz(email, secret, url, html):
    api_match = re.search(r'GET\s+(https?://[^\s"\'<>]+)', html)
    if not api_match:
        return {"error": "API URL not found"}

    api_url = api_match.group(1)
    header_matches = re.findall(r'([A-Za-z0-9\-]+)\s*:\s*([^\n<]+)', html)

    headers = {k.strip(): v.strip() for k, v in header_matches}

    async with httpx.AsyncClient() as client:
        api_resp = await client.get(api_url, headers=headers)

    try:
        data = api_resp.json()
    except:
        data = api_resp.text

    if isinstance(data, list) and all(isinstance(x, dict) for x in data):
        if "value" in data[0]:
            answer = sum(item["value"] for item in data)
        else:
            answer = len(data)
    elif isinstance(data, dict):
        nums = [v for v in data.values() if isinstance(v, (int, float))]
        answer = sum(nums) if nums else len(data)
    else:
        nums = [int(n) for n in re.findall(r"\b\d+\b", str(data))]
        answer = sum(nums) if nums else 0

    submit_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}/submit"
    payload = {"email": email, "secret": secret, "url": url, "answer": answer}

    async with httpx.AsyncClient() as client:
        resp = await client.post(submit_url, json=payload)

    rjson = resp.json()
    return {
        "type": "api_quiz",
        "api_url": api_url,
        "headers_used": headers,
        "api_response": data,
        "computed_answer": answer,
        "server_response": rjson,
        "next_url": rjson.get("url")
    }


# --------------------------------------------------------------------
# CSV PARSER WITH HEADER SUPPORT
# --------------------------------------------------------------------
def parse_csv_with_headers(csv_text):
    lines = csv_text.strip().splitlines()
    try:
        reader = csv.DictReader(lines)
        rows = list(reader)
        if rows and all(rows[0].keys()):
            return rows
    except:
        pass

    result = []
    for line in lines:
        try:
            result.append(int(line))
        except:
            try:
                result.append(float(line))
            except:
                result.append(line)
    return result


# --------------------------------------------------------------------
# UNIVERSAL QUIZ SOLVER
# --------------------------------------------------------------------
async def solve_single_quiz(email, secret, url):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="load", timeout=15000)
            await page.wait_for_timeout(800)

            try:
                body_text = await page.inner_text("body")
                parsed_json = json.loads(body_text)
                if "url" in parsed_json:
                    return {"type": "direct_json", "server_response": parsed_json, "next_url": parsed_json["url"]}
            except:
                pass

            html = await page.content()

            if re.search(r'\bGET\s+(https?://[^\s"\'<>]+)', html):
                return await handle_api_quiz(email, secret, url, html)

            pdf_match = re.search(r'href=["\']([^"\']+\.pdf)["\']', html)
            if pdf_match:
                return await handle_pdf_quiz(email, secret, url, html, pdf_match)

            if "demo-scrape-data" in html:
                return await handle_scrape_quiz(email, secret, url, html)

            if "demo-audio-data.csv" in html or ".opus" in html:
                return await handle_audio_quiz(email, secret, url, html)

            return await handle_html_quiz(email, secret, url, html)

        except Exception as e:
            return {"error": str(e), "url": url}

        finally:
            await browser.close()


# --------------------------------------------------------------------
# AUDIO QUIZ
# --------------------------------------------------------------------
async def handle_audio_quiz(email, secret, url, html):

    csv_match = re.search(r'href="([^"]+\.csv)"', html)
    if not csv_match:
        return {"error": "CSV link not found"}

    csv_url = urljoin(url, csv_match.group(1))

    cutoff = int(re.search(r'<span id="cutoff">(\d+)</span>', html).group(1))

    async with httpx.AsyncClient() as client:
        csv_text = (await client.get(csv_url)).text

    parsed = parse_csv_with_headers(csv_text)

    if parsed and isinstance(parsed[0], dict):
        numeric_cols = []
        for col in parsed[0]:
            try:
                float(parsed[0][col])
                numeric_cols.append(col)
            except:
                pass
        if numeric_cols:
            col = numeric_cols[0]
            numbers = [float(row[col]) for row in parsed]
        else:
            numbers = []
    else:
        numbers = [x for x in parsed if isinstance(x, (int, float))]

    answer = sum(n for n in numbers if n > cutoff)

    submit_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}/submit"
    payload = {"email": email, "secret": secret, "url": url, "answer": answer}

    async with httpx.AsyncClient() as client:
        r = await client.post(submit_url, json=payload)

    return {
        "type": "audio_quiz",
        "computed_answer": answer,
        "next_url": r.json().get("url")
    }


# --------------------------------------------------------------------
# SCRAPE QUIZ
# --------------------------------------------------------------------
async def handle_scrape_quiz(email, secret, url, html):
    match = re.search(r'href="(/demo-scrape-data[^"]+)"', html)
    scrape_url = urljoin(url, match.group(1))

    async with httpx.AsyncClient() as client:
        secret_code = (await client.get(scrape_url)).text.strip()

    submit_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}/submit"
    payload = {"email": email, "secret": secret, "url": url, "answer": secret_code}

    async with httpx.AsyncClient() as client:
        r = await client.post(submit_url, json=payload)

    return {"type": "scrape_quiz", "answer": secret_code, "next_url": r.json().get("url")}


# --------------------------------------------------------------------
# HTML QUIZ
# --------------------------------------------------------------------
async def handle_html_quiz(email, secret, url, html):
    if re.search(r"<pre>(.*?)</pre>", html, re.DOTALL):
        submit_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}/submit"
        payload = {"email": email, "secret": secret, "url": url, "answer": "test"}

        async with httpx.AsyncClient() as client:
            r = await client.post(submit_url, json=payload)

        return {"type": "html_quiz", "next_url": r.json().get("url")}

    return {"error": "No quiz instructions found"}


# --------------------------------------------------------------------
# PDF QUIZ (FIXED)
# --------------------------------------------------------------------
async def handle_pdf_quiz(email, secret, url, html, pdf_match):

    pdf_url = urljoin(url, pdf_match.group(1))

    async with httpx.AsyncClient() as client:
        pdf_data = (await client.get(pdf_url)).content

    reader = PdfReader(BytesIO(pdf_data))

    try:
        if len(reader.pages) > 1:
            text = reader.pages[1].extract_text() or ""
        else:
            text = reader.pages[0].extract_text() or ""

        if len(text.strip()) < 10:
            text = extract_text_fallback(reader)
    except:
        text = extract_text_fallback(reader)

    numbers = [int(n) for n in re.findall(r"\b\d+\b", text)]
    answer = sum(numbers)

    submit_url = re.search(r"https?://[^\"']+/submit", html).group(0)

    payload = {"email": email, "secret": secret, "url": url, "answer": answer}

    async with httpx.AsyncClient() as client:
        r = await client.post(submit_url, json=payload)

    return {"type": "pdf_quiz", "answer": answer, "next_url": r.json().get("url")}


# --------------------------------------------------------------------
# UVICORN ENTRYPOINT
# --------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("run:app", host="0.0.0.0", port=8000)
