import os
import re
import time
import hashlib
import subprocess
import tempfile
import mimetypes
import json
import uuid
import threading
import requests
import boto3

from pathlib import Path
from datetime import datetime
from botocore.config import Config
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from pymongo import MongoClient
from flask import Flask, jsonify


# ============================================================
# LOAD ENVIRONMENT
# ============================================================

load_dotenv()

R2_ENDPOINT    = os.getenv("R2_ENDPOINT")
R2_ACCESS_KEY  = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY  = os.getenv("R2_SECRET_KEY")
R2_BUCKET      = os.getenv("R2_BUCKET")
R2_PUBLIC_URL  = os.getenv("R2_PUBLIC_URL", "").rstrip("/")
R2_WATCH_FOLDER = os.getenv("R2_WATCH_FOLDER", "uploads/")

ZEROSTORAGE_API_KEY = os.getenv("ZEROSTORAGE_API_KEY")

CF_ACCOUNT_ID  = os.getenv("CF_ACCOUNT_ID")
CF_API_TOKEN   = os.getenv("CF_API_TOKEN")
D1_DATABASE_ID = os.getenv("D1_DATABASE_ID")

MONGO_URI      = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME  = os.getenv("MONGO_DB_NAME", "video_bot_db")

# Koyeb App URL for Auto Ping (Optional, env se pick karega)
APP_URL        = os.getenv("APP_URL", "")

ANONMP4_URL      = "https://anonmp4.to"
ABYSS_UPLOAD_URL = "http://up.hydrax.net/9e805aa56a78e2abd1be605e7af7b3cb"

MAX_RETRIES       = 3
UPLOAD_TIMEOUT    = 30 * 60   # 30 min
WATCH_INTERVAL    = 30        # seconds between polls when idle

UPLOAD_CHUNK_SIZE = 2 * 1024 * 1024   # 2 MB for ZeroStorage

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm",
    ".flv", ".wmv", ".mpeg", ".mpg", ".m4v",
    ".ts", ".3gp", ".ogv", ".vob",
}

# 1st frame, 2 sec frame, 5 sec frame
THUMB_TIMESTAMPS = ["00:00:00.000", "00:00:02.000", "00:00:05.000"]


# ============================================================
# LOGGING
# ============================================================

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ============================================================
# HEALTH CHECK SERVER & AUTO-PING (FOR KOYEB)
# ============================================================

app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health_check():
    return jsonify({"status": "healthy", "service": "R2 Video Processor"}), 200

def run_health_server():
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def auto_ping():
    """Keep service alive by pinging every 15 minutes."""
    while True:
        time.sleep(15 * 60) # 15 min delay
        ping_url = APP_URL if APP_URL else "http://127.0.0.1:8080/health"
        try:
            resp = requests.get(ping_url, timeout=10)
            log(f"[Auto-Ping] Pinged {ping_url} | Status: {resp.status_code}")
        except Exception as e:
            log(f"[Auto-Ping] Ping failed: {e}")


# ============================================================
# CONFIG VALIDATION
# ============================================================

def validate_config():
    required = {
        "R2_ENDPOINT":        R2_ENDPOINT,
        "R2_ACCESS_KEY":      R2_ACCESS_KEY,
        "R2_SECRET_KEY":      R2_SECRET_KEY,
        "R2_BUCKET":          R2_BUCKET,
        "ZEROSTORAGE_API_KEY": ZEROSTORAGE_API_KEY,
        "CF_ACCOUNT_ID":      CF_ACCOUNT_ID,
        "CF_API_TOKEN":       CF_API_TOKEN,
        "D1_DATABASE_ID":     D1_DATABASE_ID,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(
            "Missing .env variables:\n" + "\n".join(f"  - {x}" for x in missing)
        )


# ============================================================
# R2 CLIENT
# ============================================================

def create_r2_client():
    validate_config()
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "adaptive"}),
    )


# ============================================================
# MONGODB CLIENT & HELPERS
# ============================================================

mongo_client = MongoClient(MONGO_URI)
mongo_db = mongo_client[MONGO_DB_NAME]
hashes_collection = mongo_db["video_hashes"]

def calculate_file_hash(file_path: Path):
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            sha256.update(chunk)
    return sha256.hexdigest()

def is_duplicate_hash(file_hash: str):
    return hashes_collection.find_one({"file_hash": file_hash}) is not None

def save_file_hash(file_hash: str, slug: str, file_name: str):
    hashes_collection.insert_one({
        "file_hash": file_hash,
        "slug": slug,
        "file_name": file_name,
        "created_at": datetime.utcnow()
    })


# ============================================================
# D1 DATABASE HELPERS
# ============================================================

D1_API_BASE = "https://api.cloudflare.com/client/v4"

def d1_query(sql, params=None):
    url = f"{D1_API_BASE}/accounts/{CF_ACCOUNT_ID}/d1/database/{D1_DATABASE_ID}/query"
    payload = {"sql": sql}
    if params:
        payload["params"] = params

    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {CF_API_TOKEN}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"D1 query failed: {data}")
    return data


def d1_init_table():
    sql = """
    CREATE TABLE IF NOT EXISTS all_videos_db (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        slug         TEXT NOT NULL UNIQUE,
        title        TEXT,
        thumbnail_1  TEXT,
        thumbnail_2  TEXT,
        thumbnail_3  TEXT,
        video_link   TEXT,
        zerostorage  TEXT,
        abyss        TEXT,
        anonmp4      TEXT,
        created_at   TEXT DEFAULT (datetime('now'))
    );
    """
    d1_query(sql)
    log("D1 table 'all_videos_db' ready.")


def d1_get_next_slug():
    result = d1_query("SELECT slug FROM all_videos_db ORDER BY id DESC LIMIT 1;")
    rows = result["result"][0].get("results", [])
    if not rows:
        return "vid000000000001"
    last = rows[0]["slug"]
    num = int(last.replace("vid", "")) + 1
    return f"vid{num:012d}"


def d1_insert_video(record: dict):
    sql = """
    INSERT OR IGNORE INTO all_videos_db
        (slug, title, thumbnail_1, thumbnail_2, thumbnail_3,
         video_link, zerostorage, abyss, anonmp4)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
    """
    d1_query(sql, [
        record.get("slug"),
        record.get("title"),
        record.get("thumbnail_1"),
        record.get("thumbnail_2"),
        record.get("thumbnail_3"),
        record.get("video_link"),
        record.get("zerostorage"),
        record.get("abyss"),
        record.get("anonmp4"),
    ])
    log(f"D1 record saved in all_videos_db for slug: {record['slug']}")


def d1_get_processed_titles():
    result = d1_query("SELECT title FROM all_videos_db;")
    rows = result["result"][0].get("results", [])
    return {row["title"] for row in rows if row.get("title")}


# ============================================================
# LIST & DELETE VIDEOS FROM R2
# ============================================================

def list_videos(s3):
    prefix = R2_WATCH_FOLDER.strip()
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    videos = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            ext = Path(key).suffix.lower()
            if ext in VIDEO_EXTENSIONS:
                videos.append({"key": key, "size": obj["Size"]})
    return videos


def delete_from_r2(s3, key):
    log(f"Deleting original video from R2: {key}")
    s3.delete_object(Bucket=R2_BUCKET, Key=key)
    log("Deleted from R2 successfully.")


def download_from_r2(s3, key, destination: Path):
    log(f"Downloading: {key}")
    s3.download_file(R2_BUCKET, key, str(destination))
    if not destination.exists():
        raise RuntimeError("Download completed but file missing.")
    log(f"Downloaded: {destination.stat().st_size / 1024 / 1024:.2f} MB")


# ============================================================
# SLUG & RENAME HELPERS
# ============================================================

def make_rename(ext):
    ts = datetime.now().strftime("%d%m%y%H%M%S")
    return f"TG-@atoz_links-VID-{ts}{ext}"


# ============================================================
# MP4 CONVERSION
# ============================================================

def convert_to_mp4(src: Path, dest: Path):
    log(f"Converting to MP4: {src.name}")
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-c:v", "copy",
        "-c:a", "aac",
        str(dest),
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg conversion failed:\n{res.stderr.decode()[-500:]}")
    log(f"Converted: {dest.name}")


# ============================================================
# THUMBNAIL GENERATION
# ============================================================

def generate_thumbnails(video_path: Path, slug: str, temp_dir: Path):
    thumbs = []
    for i, ts in enumerate(THUMB_TIMESTAMPS, start=1):
        thumb_path = temp_dir / f"{slug}_thumb_{i}.jpg"
        cmd = [
            "ffmpeg", "-y",
            "-ss", ts,
            "-i", str(video_path),
            "-vframes", "1",
            "-q:v", "2",
            str(thumb_path),
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode != 0 or not thumb_path.exists():
            cmd[3] = "00:00:00.000"
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        if thumb_path.exists():
            log(f"Thumbnail {i} generated at timestamp {ts}: {thumb_path.name}")
            thumbs.append(thumb_path)
        else:
            log(f"Thumbnail {i} FAILED at {ts}")
            thumbs.append(None)

    return thumbs


# ============================================================
# UPLOAD THUMBNAILS TO R2
# ============================================================

def upload_thumb_to_r2(s3, thumb_path: Path, slug: str, index: int):
    r2_key = f"thumbnails/{slug}_thumb_{index}.jpg"
    log(f"Uploading thumbnail {index} to R2: {r2_key}")
    s3.upload_file(
        str(thumb_path),
        R2_BUCKET,
        r2_key,
        ExtraArgs={"ContentType": "image/jpeg"},
    )
    
    if R2_PUBLIC_URL:
        r2_public_url = f"{R2_PUBLIC_URL}/{r2_key}"
    else:
        clean_endpoint = R2_ENDPOINT.replace("https://", "").replace("http://", "").rstrip("/")
        r2_public_url = f"https://{R2_BUCKET}.{clean_endpoint}/{r2_key}"

    return r2_public_url


# ============================================================
# ANONMP4 UPLOAD (PLAYWRIGHT)
# ============================================================

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
            raise RuntimeError("No URL returned.")
        except Exception as e:
            last_err = e
            log(f"[AnonMP4] Attempt {attempt} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 5)
                try:
                    page.goto(ANONMP4_URL, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
    raise last_err


# ============================================================
# ABYSS (HYDRAX) UPLOAD
# ============================================================

def upload_abyss(video_path: Path):
    log("[Abyss] Uploading...")
    file_type = mimetypes.guess_type(str(video_path))[0] or "video/mp4"
    for attempt in range(1, MAX_RETRIES + 1):
        log(f"[Abyss] Attempt {attempt}/{MAX_RETRIES}")
        try:
            with open(video_path, "rb") as vf:
                resp = requests.post(
                    ABYSS_UPLOAD_URL,
                    files={"file": (video_path.name, vf, file_type)},
                    timeout=3600,
                )
            if resp.status_code == 200:
                log(f"[Abyss] Raw: {resp.text.strip()[:200]}")
                try:
                    data = resp.json()
                    iframe = (
                        data.get("response", {}).get("urlIframe")
                        or data.get("urlIframe")
                    )
                    if iframe:
                        return iframe
                    log(f"[Abyss] urlIframe not found in: {data}")
                except Exception:
                    log(f"[Abyss] JSON parse failed, raw: {resp.text[:200]}")
                return None
            else:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            log(f"[Abyss] Attempt {attempt} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 5)
    raise RuntimeError("Abyss upload failed after max retries.")


# ============================================================
# ZEROSTORAGE CHUNKED UPLOAD
# ============================================================

def upload_zerostorage(video_path: Path):
    file_path = Path(video_path)
    file_size = file_path.stat().st_size
    file_name = file_path.name
    upload_id = str(uuid.uuid4())
    total_chunks = (file_size + UPLOAD_CHUNK_SIZE - 1) // UPLOAD_CHUNK_SIZE

    log(f"[ZeroStorage] Uploading {file_name} ({file_size / 1024 / 1024:.2f} MB) in {total_chunks} chunks")

    upload_url = "https://zerostorage.net/api/upload/chunked"
    uploaded = 0
    start_time = time.time()
    file_id = None

    with open(file_path, "rb") as f:
        for idx in range(total_chunks):
            chunk = f.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break

            metadata = {
                "uploadId":    upload_id,
                "chunkIndex":  idx,
                "totalChunks": total_chunks,
                "fileName":    file_name,
                "fileSize":    file_size,
                "chunkSize":   len(chunk),
            }

            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    resp = requests.post(
                        upload_url,
                        headers={"x-api-key": ZEROSTORAGE_API_KEY},
                        files={"chunk": (f"chunk-{idx}", chunk, "application/octet-stream")},
                        data={"metadata": json.dumps(metadata)},
                        timeout=300,
                    )
                    break
                except requests.exceptions.RequestException as e:
                    log(f"[ZeroStorage] Chunk {idx+1} attempt {attempt} error: {e}")
                    if attempt == MAX_RETRIES:
                        raise
                    time.sleep(attempt * 3)

            if not resp.ok:
                raise RuntimeError(f"[ZeroStorage] Chunk {idx+1} HTTP {resp.status_code}: {resp.text[:200]}")

            uploaded += len(chunk)
            elapsed = max(time.time() - start_time, 0.001)
            speed_mb = (uploaded / elapsed) / 1024 / 1024
            pct = (uploaded / file_size) * 100
            log(f"[ZeroStorage] Chunk {idx+1}/{total_chunks} ({pct:.1f}%) @ {speed_mb:.2f} MB/s")

            try:
                result = resp.json()
                if result.get("fileId"):
                    file_id = result["fileId"]
            except Exception:
                pass

    if not file_id:
        raise RuntimeError("[ZeroStorage] fileId not received in any chunk response.")

    embed_link  = f"https://zerostorage.net/embed/{file_id}"
    video_link  = f"https://zerostorage.net/api/files/download/{file_id}?track=true"
    log(f"[ZeroStorage] Done. fileId={file_id}")
    return embed_link, video_link


# ============================================================
# PROCESS SINGLE VIDEO
# ============================================================

def process_video(s3, page, video: dict, temp_dir: Path, processed_titles: set):
    key      = video["key"]
    orig_ext = Path(key).suffix.lower()
    orig_name = Path(key).name

    log("")
    log("=" * 65)
    log(f"PROCESSING : {orig_name}")
    log(f"R2 KEY     : {key}")
    log("=" * 65)

    local_orig  = temp_dir / orig_name

    try:
        download_from_r2(s3, key, local_orig)

        file_hash = calculate_file_hash(local_orig)
        if is_duplicate_hash(file_hash):
            log(f"[MongoDB] Duplicate video detected (Hash: {file_hash}). Skipping upload & deleting from R2.")
            if local_orig.exists():
                local_orig.unlink()
            delete_from_r2(s3, key)
            return True

        slug = d1_get_next_slug()
        log(f"Slug       : {slug}")

        new_ext   = ".mp4"
        new_name  = make_rename(new_ext)
        log(f"New name   : {new_name}")

        local_mp4   = temp_dir / new_name

        if orig_ext == ".mp4":
            log("Already MP4, copying as renamed file.")
            import shutil
            shutil.copy2(local_orig, local_mp4)
        else:
            convert_to_mp4(local_orig, local_mp4)

        if local_orig != local_mp4 and local_orig.exists():
            local_orig.unlink()

        thumbs = generate_thumbnails(local_mp4, slug, temp_dir)

        thumb_urls = []
        for i, thumb in enumerate(thumbs, start=1):
            if thumb and thumb.exists():
                full_url = upload_thumb_to_r2(s3, thumb, slug, i)
                thumb_urls.append(full_url)
                thumb.unlink()
            else:
                thumb_urls.append(None)

        while len(thumb_urls) < 3:
            thumb_urls.append(None)

        log("[AnonMP4] Starting upload...")
        anon_url = upload_anonmp4_with_retry(page, local_mp4)
        log(f"✔ AnonMP4: {anon_url}")

        log("[Abyss] Starting upload...")
        abyss_url = upload_abyss(local_mp4)
        log(f"✔ Abyss: {abyss_url}")

        log("[ZeroStorage] Starting upload...")
        zs_embed, zs_video = upload_zerostorage(local_mp4)
        log(f"✔ ZeroStorage embed : {zs_embed}")
        log(f"✔ ZeroStorage video : {zs_video}")

        record = {
            "slug":        slug,
            "title":       new_name,
            "thumbnail_1": thumb_urls[0],
            "thumbnail_2": thumb_urls[1],
            "thumbnail_3": thumb_urls[2],
            "video_link":  zs_video,
            "zerostorage": zs_embed,
            "abyss":       abyss_url,
            "anonmp4":     anon_url,
        }
        d1_insert_video(record)

        save_file_hash(file_hash, slug, new_name)
        log(f"[MongoDB] Video hash saved.")

        if local_mp4.exists():
            local_mp4.unlink()

        delete_from_r2(s3, key)

        log("")
        log("✔ COMPLETED SUCCESSFULLY")
        log(f"  Slug        : {slug}")
        log(f"  Title       : {new_name}")
        log(f"  Thumbnails  : {thumb_urls}")
        log(f"  AnonMP4     : {anon_url}")
        log(f"  Abyss       : {abyss_url}")
        log(f"  ZS Embed    : {zs_embed}")
        log(f"  ZS Video    : {zs_video}")

        return True

    except Exception as e:
        log(f"✖ FAILED: {e}")
        for f in [local_orig, local_mp4 if 'local_mp4' in locals() else None]:
            try:
                if f and f.exists():
                    f.unlink()
            except Exception:
                pass
        return False


# ============================================================
# MAIN WATCH LOOP
# ============================================================

def main():
    print()
    print("=" * 65)
    print("  R2 VIDEO BACKUP TOOL — AnonMP4 + Abyss + ZeroStorage + D1")
    print("=" * 65)
    print()

    # Start Health Check Flask Server in Background Thread
    threading.Thread(target=run_health_server, daemon=True).start()
    log("Health Check HTTP server started on port 8080.")

    # Start Auto-Ping Worker in Background Thread
    threading.Thread(target=auto_ping, daemon=True).start()
    log("Auto-ping background thread started.")

    validate_config()
    s3 = create_r2_client()
    d1_init_table()

    log(f"Bucket : {R2_BUCKET}")
    log(f"Folder : {R2_WATCH_FOLDER}")
    log(f"Polling every {WATCH_INTERVAL}s when idle...")
    print()

    temp_dir = Path(tempfile.mkdtemp(prefix="r2_backup_"))
    log(f"Temp dir: {temp_dir}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page    = context.new_page()
        log("Chromium ready.")

        try:
            while True:
                log("Scanning R2 folder...")
                videos = list_videos(s3)

                if not videos:
                    log(f"No videos found. Waiting {WATCH_INTERVAL}s...")
                    time.sleep(WATCH_INTERVAL)
                    continue

                processed_titles = d1_get_processed_titles()

                already_done = getattr(main, "_processed_keys", set())
                new_videos = [v for v in videos if v["key"] not in already_done]

                if not new_videos:
                    log(f"All {len(videos)} video(s) already processed. Waiting {WATCH_INTERVAL}s...")
                    time.sleep(WATCH_INTERVAL)
                    continue

                log(f"Found {len(new_videos)} new video(s) to process.")

                for i, video in enumerate(new_videos, start=1):
                    log(f"\nBATCH {i}/{len(new_videos)}: {Path(video['key']).name}")
                    ok = process_video(s3, page, video, temp_dir, processed_titles)
                    if ok:
                        already_done.add(video["key"])
                main._processed_keys = already_done

                log(f"Batch done. Waiting {WATCH_INTERVAL}s before next scan...")
                time.sleep(WATCH_INTERVAL)

        except KeyboardInterrupt:
            log("Stopped by user.")
        finally:
            try:
                context.close()
                browser.close()
            except Exception:
                pass

    try:
        remaining = list(temp_dir.iterdir())
        if not remaining:
            temp_dir.rmdir()
            log("Temp dir cleaned.")
        else:
            log(f"Temp dir retained (has files): {temp_dir}")
    except Exception as e:
        log(f"Cleanup: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted.")
    except Exception as e:
        log(f"Fatal error: {e}")
        raise
