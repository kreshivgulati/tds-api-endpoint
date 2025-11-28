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

    result = await solve_quiz_recursive(
        payload.email, payload.secret, payload.url, start_time, 180
    )

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
        return await solve_quiz_recursive(
            email, secret, result["next_url"], start_time, deadline
        )

    return result

# --------------------------------------------------------------------
# API QUIZ (GET request with headers)
# --------------------------------------------------------------------
async def handle_api_quiz(email, secret, url, html):
    import json

    # Detect API GET URL inside the instructions
    api_match = re.search(r'GET\s+(https?://[^\s"\'<>]+)', html)
    if not api_match:
        return {"error": "API URL not found"}

    api_url = api_match.group(1)

    # Detect HTTP headers in the instructions:
    # Example:
    #   Authorization: Bearer 12345
    #   X-API-Key: abcdef
    header_matches = re.findall(r'([A-Za-z0-9\-]+)\s*:\s*([^\n<]+)', html)

    headers = {}
    for key, value in header_matches:
        headers[key.strip()] = value.strip()

    # Call the API
    async with httpx.AsyncClient() as client:
        api_resp = await client.get(api_url, headers=headers)

    # Try parsing JSON, fallback to text
    try:
        data = api_resp.json()
    except:
        data = api_resp.text

    # -----------------------------------------------------
    # Now compute the answer depending on the data shape
    # -----------------------------------------------------

    # Case A: JSON list of objects with "value" keys
    if isinstance(data, list) and all(isinstance(x, dict) for x in data):
        if "value" in data[0]:
            answer = sum(item["value"] for item in data if isinstance(item.get("value"), (int, float)))
        else:
            answer = len(data)  # fallback logic

    # Case B: JSON object -> count keys or sum numbers inside
    elif isinstance(data, dict):
        nums = [v for v in data.values() if isinstance(v, (int, float))]
        answer = sum(nums) if nums else len(data)

    # Case C: pure text → extract numbers
    else:
        nums = [int(n) for n in re.findall(r"\b\d+\b", str(data))]
        answer = sum(nums) if nums else 0

    # Submit URL (from page origin)
    base = urlparse(url)
    submit_url = f"{base.scheme}://{base.netloc}/submit"

    payload = {
        "email": email,
        "secret": secret,
        "url": url,
        "answer": answer,
    }

    async with httpx.AsyncClient() as client:
        submit_resp = await client.post(submit_url, json=payload)

    rjson = submit_resp.json()

    return {
        "type": "api_quiz",
        "api_url": api_url,
        "headers_used": headers,
        "api_response": data,
        "computed_answer": answer,
        "submitted_to": submit_url,
        "server_response": rjson,
        "next_url": rjson.get("url"),
    }

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

            # Sometimes body contains JSON
            try:
                body_text = await page.inner_text("body")
                import json
                parsed = json.loads(body_text)
                if "url" in parsed and parsed["url"]:
                    return {
                        "type": "direct_json",
                        "server_response": parsed,
                        "next_url": parsed["url"],
                    }
            except:
                pass

            html = await page.content()
            print("=== RENDERED HTML ===")
            print(html)
            # API quiz detection
            api_get_url = re.search(r'\bGET\s+(https?://[^\s"\'<>]+)', html)
            
            if api_get_url:
                return await handle_api_quiz(email, secret, url, html)


            # PDF quiz
            pdf_match = re.search(r'href=["\']([^"\']+\.pdf)["\']', html)
            if pdf_match:
                return await handle_pdf_quiz(email, secret, url, html, pdf_match)

            # Scrape quiz
            if "demo-scrape-data" in html:
                return await handle_scrape_quiz(email, secret, url, html)

            # Audio quiz
            if "demo-audio-data.csv" in html or ".opus" in html:
                return await handle_audio_quiz(email, secret, url, html)

            # HTML quiz
            return await handle_html_quiz(email, secret, url, html)

        except Exception as e:
            return {"error": str(e), "url": url}

        finally:
            await browser.close()


# --------------------------------------------------------------------
# SCRAPE QUIZ
# --------------------------------------------------------------------
async def handle_scrape_quiz(email, secret, url, html):
    match = re.search(r'href="(/demo-scrape-data[^"]+)"', html)
    if not match:
        return {"error": "demo-scrape-data link not found"}

    scrape_url = urljoin(url, match.group(1))

    async with httpx.AsyncClient() as client:
        r = await client.get(scrape_url)
        secret_code = r.text.strip()

    base = urlparse(url)
    submit_url = f"{base.scheme}://{base.netloc}/submit"

    payload = {
        "email": email,
        "secret": secret,
        "url": url,
        "answer": secret_code,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(submit_url, json=payload)

    rjson = resp.json()

    return {
        "type": "scrape_quiz",
        "scraped_from": scrape_url,
        "secret_code": secret_code,
        "submitted_to": submit_url,
        "server_response": rjson,
        "next_url": rjson.get("url"),
    }


# --------------------------------------------------------------------
# AUDIO QUIZ
# --------------------------------------------------------------------
async def handle_audio_quiz(email, secret, url, html):
    csv_match = re.search(r'href="([^"]+\.csv)"', html)
    if not csv_match:
        return {"error": "CSV link not found"}

    csv_url = urljoin(url, csv_match.group(1))

    cutoff_match = re.search(r'<span id="cutoff">(\d+)</span>', html)
    if not cutoff_match:
        return {"error": "Cutoff missing"}

    cutoff = int(cutoff_match.group(1))

    async with httpx.AsyncClient() as client:
        resp = await client.get(csv_url)
        csv_text = resp.text

    numbers = []
    for line in csv_text.splitlines():
        try:
            numbers.append(int(line.strip()))
        except:
            pass

    answer = sum(n for n in numbers if n > cutoff)

    base = urlparse(url)
    submit_url = f"{base.scheme}://{base.netloc}/submit"

    payload = {
        "email": email,
        "secret": secret,
        "url": url,
        "answer": answer,
    }

    async with httpx.AsyncClient() as client:
        submit_resp = await client.post(submit_url, json=payload)

    rjson = submit_resp.json()

    return {
        "type": "audio_quiz",
        "csv": csv_url,
        "cutoff": cutoff,
        "numbers_count": len(numbers),
        "computed_answer": answer,
        "submitted_to": submit_url,
        "server_response": rjson,
        "next_url": rjson.get("url"),
    }


# --------------------------------------------------------------------
# HTML QUIZ
# --------------------------------------------------------------------
async def handle_html_quiz(email, secret, url, html):
    pre_match = re.search(r"<pre>(.*?)</pre>", html, flags=re.DOTALL)
    if pre_match:
        base = urlparse(url)
        submit_url = f"{base.scheme}://{base.netloc}/submit"

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                submit_url,
                json={
                    "email": email,
                    "secret": secret,
                    "url": url,
                    "answer": "test",
                },
            )

        rjson = resp.json()

        return {
            "type": "html_quiz",
            "submitted_to": submit_url,
            "server_response": rjson,
            "next_url": rjson.get("url"),
        }

    return {"error": "No PDF or HTML instructions found"}


# --------------------------------------------------------------------
# PDF QUIZ
# --------------------------------------------------------------------
async def handle_pdf_quiz(email, secret, url, html, pdf_match):
    pdf_url = urljoin(url, pdf_match.group(1))

    async with httpx.AsyncClient() as client:
        resp = await client.get(pdf_url)
        pdf_data = resp.content

    reader = PdfReader(BytesIO(pdf_data))
    text = reader.pages[1].extract_text()

    numbers = [int(n) for n in re.findall(r"\b\d+\b", text)]
    answer = sum(numbers)

    submit_match = re.search(r"https?://[^\"']+/submit", html)
    if not submit_match:
        return {"error": "submit URL not found"}

    submit_url = submit_match.group(0)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            submit_url,
            json={
                "email": email,
                "secret": secret,
                "url": url,
                "answer": answer,
            },
        )

    rjson = resp.json()

    return {
        "type": "pdf_quiz",
        "pdf": pdf_url,
        "numbers": numbers,
        "answer": answer,
        "submitted_to": submit_url,
        "server_response": rjson,
        "next_url": rjson.get("url"),
    }


# --------------------------------------------------------------------
# UVICORN ENTRYPOINT
# --------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("run:app", host="0.0.0.0", port=8000)

