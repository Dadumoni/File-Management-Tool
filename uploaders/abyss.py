import time
import mimetypes
import requests
from pathlib import Path
from requests_toolbelt.multipart.encoder import MultipartEncoder
from config import ABYSS_UPLOAD_URL, MAX_RETRIES

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def upload_abyss(video_path: Path):
    log("[Abyss] Uploading (Streamed mode)...")
    file_type = mimetypes.guess_type(str(video_path))[0] or "video/mp4"

    for attempt in range(1, MAX_RETRIES + 1):
        log(f"[Abyss] Attempt {attempt}/{MAX_RETRIES}")
        try:
            with open(video_path, "rb") as vf:
                m = MultipartEncoder(
                    fields={'file': (video_path.name, vf, file_type)}
                )
                resp = requests.post(
                    ABYSS_UPLOAD_URL,
                    data=m,
                    headers={'Content-Type': m.content_type},
                    timeout=3600
                )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    iframe = data.get("response", {}).get("urlIframe") or data.get("urlIframe")
                    if iframe:
                        return iframe
                except Exception:
                    pass
                return None
            else:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            log(f"[Abyss] Attempt {attempt} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 5)
    raise RuntimeError("Abyss upload failed after max retries.")
