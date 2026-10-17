import re
import time
from pathlib import Path
from config import ANONMP4_URL, UPLOAD_TIMEOUT, MAX_RETRIES

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def find_anonmp4_url(page):
    try:
        for link in page.locator("a[href]").all():
            href = link.get_attribute("href") or ""
            if "/v/" in href:
                return ("https://anonmp4.to" + href) if href.startswith("/") else href
    except Exception:
        pass
    try:
        text = page.locator("body").inner_text(timeout=3000)
        for pattern in [
            r"https?://(?:www\.)?anonmp4\.to/v/[A-Za-z0-9_-]+",
            r"https?://anonmp4\.to/[A-Za-z0-9_-]+",
        ]:
            matches = re.findall(pattern, text, re.IGNORECASE)
            if matches:
                return matches[0]
    except Exception:
        pass
    return None

def upload_anonmp4_file(page, file_path: Path):
    log(f"[AnonMP4] Opening {ANONMP4_URL}")
    page.goto(ANONMP4_URL, wait_until="domcontentloaded", timeout=60000)
    fi = page.locator('input[type="file"]').first
    fi.wait_for(state="attached", timeout=30000)
    log("[AnonMP4] Submitting file...")
    fi.set_input_files(str(file_path))
    deadline = time.time() + UPLOAD_TIMEOUT
    while time.time() < deadline:
        url = find_anonmp4_url(page)
        if url:
            return url
        time.sleep(2)
    raise TimeoutError("Timed out waiting for AnonMP4 link.")

def upload_anonmp4_with_retry(page, file_path: Path):
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        log(f"[AnonMP4] Attempt {attempt}/{MAX_RETRIES}")
        try:
            url = upload_anonmp4_file(page, file_path)
            if url:
                return url
        except Exception as e:
            last_err = e
            log(f"[AnonMP4] Attempt {attempt} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 5)
    raise last_err
