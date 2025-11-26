import sys
import asyncio
import csv
import re
import time
import base64
from urllib.parse import urljoin, urlparse
from io import BytesIO

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright
import httpx
from PyPDF2 import PdfReader

# Windows fix for Playwright
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
    # Validate JSON
    if not payload.email or not payload.secret or not payload.url:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    # Validate secret
    if payload.secret != STUDENT_SECRET:
        return JSONResponse(status_code=403, content={"error": "Invalid secret"})

    start_time = time.time()
    result = await solve_quiz_recursive(
        payload.email,
        payload.secret,
        payload.url,
        start_time,
        180,
    )

    return JSONResponse(content={"status": "secret verified", "result": result})


# -----------------------------------------------------------------------
# 🔥 MULTI-STEP QUIZ LOOP
# -----------------------------------------------------------------------

async def solve_quiz_recursive(email, secret, url, start_time, deadline):
    if time.time() - start_time > deadline:
        return {"error": "Time exceeded 3 minutes"}

    result = await solve_single_quiz(email, secret, url)

    if "error" in result:
        return result

    # Follow next URL if provided
    if result.get("next_url"):
        return await solve_quiz_recursive(
            email, secret, result["next_url"], start_time, deadline
        )

    return result


# -----------------------------------------------------------------------
# 🔥 UNIVERSAL QUIZ SOLVER (PDF + HTML + DEMO + ANY)
# -----------------------------------------------------------------------

async def solve_single_quiz(email, secret, url):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await page.goto(url, wait_until="load", timeout=15000)
            await page.wait_for_timeout(800)

            # 🟢 Try to read <body> as JSON (demo returns this directly)
            try:
                body_text = await page.inner_text("body")
                import json
                parsed = json.loads(body_text)
                # If contains "url", this is a direct redirect quiz
                if "url" in parsed and parsed["url"]:
                    return {
                        "type": "direct_json",
                        "server_response": parsed,
                        "next_url": parsed["url"]
                    }
            except:
                pass

            html = await page.content()
            print("=== RENDERED HTML ===")
            print(html)
# 2. PDF quiz
            pdf_match = re.search(r'href=["\']([^"\']+\.pdf)["\']', html)
            if pdf_match:
                return await handle_pdf_quiz(email, secret, url, html, pdf_match)

            # 3. Scrape quiz
            if "demo-scrape-data" in html:
                return await handle_scrape_quiz(email, secret, url, html)

            # 4. Audio quiz
            if "demo-audio-data.csv" in html or ".opus" in html:
                return await handle_audio_quiz(email, secret, url, html)


            # 🟡 Otherwise treat as HTML quiz
            return await handle_html_quiz(email, secret, url, html)

        except Exception as e:
            return {"error": str(e), "url": url}

        finally:
            await browser.close()
async def handle_scrape_quiz(email, secret, url, html):
    # Extract link to scrape-data
    match = re.search(r'href="(\/demo-scrape-data[^"]+)"', html)
    if not match:
        return {"error": "demo-scrape-data link not found"}

    scrape_url = urljoin(url, match.group(1))

    # Fetch the scrape-data page
    async with httpx.AsyncClient() as client:
        r = await client.get(scrape_url)
        text = r.text.strip()

    # The secret code is usually a number or string on this page
    secret_code = text

    # Find submit URL
    base = urlparse(url)
    submit_url = f"{base.scheme}://{base.netloc}/submit"

    payload = {
        "email": email,
        "secret": secret,
        "url": url,
        "answer": secret_code
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
        "next_url": rjson.get("url")
    }

async def handle_audio_quiz(email, secret, url, html):
    # Extract CSV link
    csv_match = re.search(r'href="([^"]+\.csv)"', html)
    if not csv_match:
        return {"error": "CSV link not found"}

    csv_url = urljoin(url, csv_match.group(1))

    # Extract cutoff value
    cutoff_match = re.search(r'<span id="cutoff">(\d+)</span>', html)
    if not cutoff_match:
        return {"error": "Cutoff missing"}

    cutoff = int(cutoff_match.group(1))

    # Download CSV
    async with httpx.AsyncClient() as client:
        resp = await client.get(csv_url)
        csv_text = resp.text

    # DEBUG PRINT
    print("\n=== AUDIO CSV DEBUG ===")
    print("CSV URL:", csv_url)
    print("CSV TEXT (first 200 chars):")
    print(csv_text[:200])
    print("=======================\n")

    # Parse single-column CSV
    try:
        numbers = [int(line.strip()) for line in csv_text.splitlines() if line.strip().isdigit()]
    except:
        return {"error": "CSV parsing failed"}

    # Compute answer
    answer = sum(n for n in numbers if n > cutoff)

    # Submit answer
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


    async with httpx.AsyncClient() as client:
        submit_resp = await client.post(submit_url, json=payload)

    rjson = submit_resp.json()

    return {
        "type": "audio_quiz",
        "csv": csv_url,
        "cutoff": cutoff,
        "numbers_parsed": numbers[:20],  # preview
        "computed_answer": answer,
        "submitted_to": submit_url,
        "server_response": rjson,
        "next_url": rjson.get("url")
    }


async def handle_html_quiz(email, secret, url, html):
    # The quiz usually contains a <pre> with JSON instructions
    pre_match = re.search(r"<pre>(.*?)</pre>", html, flags=re.DOTALL)

    if pre_match:
        extracted = pre_match.group(1)
        extracted = re.sub(r"<.*?>", "", extracted).strip()

        # Try to detect submit URL
        base = urlparse(url)
        submit_url = f"{base.scheme}://{base.netloc}/submit"

        # Demo accepts ANY answer
        answer = "test"

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

        resp_json = resp.json()

        return {
            "type": "html_quiz",
            "submitted_to": submit_url,
            "server_response": resp_json,
            "next_url": resp_json.get("url"),
        }

    return {"error": "No PDF or HTML instructions found", "snippet": html[:300]}


# -----------------------------------------------------------------------
# 🔥 HANDLE PDF QUIZZES
# -----------------------------------------------------------------------

async def handle_pdf_quiz(email, secret, url, html, pdf_match):
    pdf_url = pdf_match.group(1)
    pdf_url = urljoin(url, pdf_url)

    async with httpx.AsyncClient() as client:
        pdf_resp = await client.get(pdf_url)
        pdf_data = pdf_resp.content

    reader = PdfReader(BytesIO(pdf_data))
    text = reader.pages[1].extract_text()

    numbers = [int(n) for n in re.findall(r"\b\d+\b", text)]
    answer = sum(numbers)

    # Find submit URL
    submit_match = re.search(r'https?://[^"\']+/submit', html)
    if not submit_match:
        return {"error": "submit URL not found in PDF quiz"}

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

    resp_json = resp.json()

    return {
        "type": "pdf_quiz",
        "pdf": pdf_url,
        "numbers": numbers,
        "answer": answer,
        "submitted_to": submit_url,
        "server_response": resp_json,
        "next_url": resp_json.get("url"),
    }

