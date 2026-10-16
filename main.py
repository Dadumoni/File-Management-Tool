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
from requests_toolbelt.multipart.encoder import MultipartEncoder
from PIL import Image


# ============================================================
# LOAD ENVIRONMENT
# ============================================================

load_dotenv()

# Source R2 (Video Download & Watch)
SOURCE_R2_ENDPOINT = os.getenv("SOURCE_R2_ENDPOINT") or os.getenv("R2_ENDPOINT")
SOURCE_R2_ACCESS_KEY = os.getenv("SOURCE_R2_ACCESS_KEY") or os.getenv("R2_ACCESS_KEY")
SOURCE_R2_SECRET_KEY = os.getenv("SOURCE_R2_SECRET_KEY") or os.getenv("R2_SECRET_KEY")
SOURCE_R2_BUCKET = os.getenv("SOURCE_R2_BUCKET") or os.getenv("R2_BUCKET")
SOURCE_R2_WATCH_FOLDER = os.getenv("SOURCE_R2_WATCH_FOLDER") or os.getenv("R2_WATCH_FOLDER", "uploads/")

# Destination R2 (Thumbnail Upload)
DEST_R2_ENDPOINT = os.getenv("DEST_R2_ENDPOINT") or SOURCE_R2_ENDPOINT
DEST_R2_ACCESS_KEY = os.getenv("DEST_R2_ACCESS_KEY") or SOURCE_R2_ACCESS_KEY
DEST_R2_SECRET_KEY = os.getenv("DEST_R2_SECRET_KEY") or SOURCE_R2_SECRET_KEY
DEST_R2_BUCKET = os.getenv("DEST_R2_BUCKET") or SOURCE_R2_BUCKET

# Supabase (Replaces Cloudflare D1)
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY")

# MongoDB (Video Metadata & Duplicate Prevention)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "video_bot_db")

# Uploader APIs
ZEROSTORAGE_API_KEY = os.getenv("ZEROSTORAGE_API_KEY")

APP_URL = os.getenv("APP_URL", "")

ANONMP4_URL = "https://anonmp4.to"
ABYSS_UPLOAD_URL = "http://up.hydrax.net/9e805aa56a78e2abd1be605e7af7b3cb"

MAX_RETRIES = 3
UPLOAD_TIMEOUT = 30 * 60   # 30 min
WATCH_INTERVAL = 30        # seconds between polls when idle

UPLOAD_CHUNK_SIZE = 2 * 1024 * 1024   # 2 MB for ZeroStorage

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm",
    ".flv", ".wmv", ".mpeg", ".mpg", ".m4v",
    ".ts", ".3gp", ".ogv", ".vob",
}


# ============================================================
# LOGGING
# ============================================================

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ============================================================
# HEALTH CHECK SERVER & AUTO-PING
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
    while True:
        time.sleep(15 * 60)
        ping_url = APP_URL if APP_URL else "http://127.0.0.1:8080/health"
        try:
            resp = requests.get(ping_url, timeout=10)
            log(f"[Auto-Ping] Pinged {ping_url} | Status: {resp.status_code}")
        except Exception as e:
            log(f"[Auto-Ping] Ping failed: {e}")


# ============================================================
# CONFIG VALIDATION & R2 CLIENTS
# ============================================================

def validate_config():
    required = {
        "SOURCE_R2_ENDPOINT": SOURCE_R2_ENDPOINT,
        "SOURCE_R2_ACCESS_KEY": SOURCE_R2_ACCESS_KEY,
        "SOURCE_R2_SECRET_KEY": SOURCE_R2_SECRET_KEY,
        "SOURCE_R2_BUCKET": SOURCE_R2_BUCKET,
        "DEST_R2_ENDPOINT": DEST_R2_ENDPOINT,
        "DEST_R2_ACCESS_KEY": DEST_R2_ACCESS_KEY,
        "DEST_R2_SECRET_KEY": DEST_R2_SECRET_KEY,
        "DEST_R2_BUCKET": DEST_R2_BUCKET,
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_KEY": SUPABASE_KEY,
        "ZEROSTORAGE_API_KEY": ZEROSTORAGE_API_KEY,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(
            "Missing .env variables:\n" + "\n".join(f"  - {x}" for x in missing)
        )

def create_r2_clients():
    validate_config()
    source_s3 = boto3.client(
        "s3",
        endpoint_url=SOURCE_R2_ENDPOINT,
        aws_access_key_id=SOURCE_R2_ACCESS_KEY,
        aws_secret_access_key=SOURCE_R2_SECRET_KEY,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "adaptive"}),
    )
    dest_s3 = boto3.client(
        "s3",
        endpoint_url=DEST_R2_ENDPOINT,
        aws_access_key_id=DEST_R2_ACCESS_KEY,
        aws_secret_access_key=DEST_R2_SECRET_KEY,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "adaptive"}),
    )
    return source_s3, dest_s3


# ============================================================
# MONGODB HELPERS (VIDEO METADATA & DUPLICATE PREVENTION)
# ============================================================

mongo_client = MongoClient(MONGO_URI)
mongo_db = mongo_client[MONGO_DB_NAME]
metadata_collection = mongo_db["video_metadata"]
hashes_collection = mongo_db["video_hashes"]

def calculate_file_hash(file_path: Path):
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            sha256.update(chunk)
    return sha256.hexdigest()

def is_duplicate_video(file_hash: str, metadata: dict):
    # 1. Exact SHA-256 match in hashes or metadata collections
    if hashes_collection.find_one({"file_hash": file_hash}):
        return True, "Exact file hash (SHA-256) match in hashes"
    if metadata_collection.find_one({"file_hash": file_hash}):
        return True, "Exact file hash (SHA-256) match in metadata"

    # 2. Match by video technical metadata (duration, resolution, file size)
    duration = metadata.get("duration", 0.0)
    width = metadata.get("width", 0)
    height = metadata.get("height", 0)
    file_size = metadata.get("file_size", 0)

    if duration > 1.0 and width > 0 and height > 0 and file_size > 0:
        meta_query = {
            "width": width,
            "height": height,
            "duration": {"$gte": round(duration - 0.2, 2), "$lte": round(duration + 0.2, 2)},
            "file_size": {"$gte": int(file_size * 0.99), "$lte": int(file_size * 1.01)}
        }
        dup = metadata_collection.find_one(meta_query)
        if dup:
            return True, f"Video metadata match (Duration: {duration:.2f}s, Res: {width}x{height}, Size: {file_size} bytes)"

    return False, None

def save_video_metadata(metadata_doc: dict):
    metadata_collection.insert_one(metadata_doc)
    # Also maintain video_hashes collection for backward compatibility
    hashes_collection.insert_one({
        "file_hash": metadata_doc["file_hash"],
        "slug": metadata_doc["slug"],
        "file_name": metadata_doc["title"],
        "created_at": metadata_doc.get("created_at", datetime.utcnow())
    })


# ============================================================
# SUPABASE HELPERS (REPLACES CLOUDFLARE D1)
# ============================================================

def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def supabase_init_table():
    try:
        url = f"{SUPABASE_URL}/rest/v1/all_videos_db?limit=1"
        resp = requests.get(url, headers=supabase_headers(), timeout=15)
        if resp.status_code in (200, 206):
            log("Supabase table 'all_videos_db' verified and ready.")
        else:
            log(f"Supabase check returned status {resp.status_code}: {resp.text}")
    except Exception as e:
        log(f"Supabase connection check failed: {e}")

def supabase_get_next_slug():
    url = f"{SUPABASE_URL}/rest/v1/all_videos_db?select=slug&order=id.desc&limit=1"
    try:
        resp = requests.get(url, headers=supabase_headers(), timeout=30)
        if resp.status_code == 200:
            rows = resp.json()
            if rows and isinstance(rows, list) and rows[0].get("slug"):
                last = rows[0]["slug"]
                digits = re.sub(r"[^\d]", "", last)
                num = int(digits) + 1 if digits else 1
                return f"vid{num:012d}"
    except Exception as e:
        log(f"Error fetching next slug from Supabase: {e}")
    return "vid000000000001"

def supabase_insert_video(record: dict):
    url = f"{SUPABASE_URL}/rest/v1/all_videos_db"
    resp = requests.post(url, headers=supabase_headers(), json=record, timeout=30)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Supabase insert failed: {resp.status_code} {resp.text}")
    log(f"Supabase record saved for slug: {record.get('slug')}")

def supabase_get_processed_titles():
    url = f"{SUPABASE_URL}/rest/v1/all_videos_db?select=title"
    try:
        resp = requests.get(url, headers=supabase_headers(), timeout=30)
        if resp.status_code == 200:
            rows = resp.json()
            return {row["title"] for row in rows if row.get("title")}
    except Exception as e:
        log(f"Error reading processed titles from Supabase: {e}")
    return set()


# ============================================================
# R2 FILE MANAGEMENT
# ============================================================

def list_videos(s3_source):
    prefix = SOURCE_R2_WATCH_FOLDER.strip()
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    videos = []
    paginator = s3_source.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=SOURCE_R2_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            ext = Path(key).suffix.lower()
            if ext in VIDEO_EXTENSIONS:
                videos.append({"key": key, "size": obj["Size"]})
    return videos

def delete_from_r2(s3_source, key):
    log(f"Deleting original video from Source R2: {key}")
    s3_source.delete_object(Bucket=SOURCE_R2_BUCKET, Key=key)

def download_from_r2(s3_source, key, destination: Path):
    log(f"Downloading from Source R2: {key}")
    s3_source.download_file(SOURCE_R2_BUCKET, key, str(destination))
    if not destination.exists():
        raise RuntimeError("Download completed but file missing.")
    log(f"Downloaded: {destination.stat().st_size / 1024 / 1024:.2f} MB")

def make_rename(ext):
    ts = datetime.now().strftime("%d%m%y%H%M%S")
    return f"TG-@atoz_links-VID-{ts}{ext}"


# ============================================================
# VIDEO METADATA & COMPOSITE THUMBNAIL GENERATION
# ============================================================

def get_video_metadata(video_path: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration,size,format_name:stream=width,height,codec_name,codec_type,r_frame_rate,bit_rate",
        "-of", "json",
        str(video_path)
    ]
    meta = {
        "duration": 0.0,
        "width": 0,
        "height": 0,
        "file_size": video_path.stat().st_size if video_path.exists() else 0,
        "video_codec": "",
        "audio_codec": "",
        "format": "",
        "aspect_ratio": 1.0,
        "aspect_ratio_str": "unknown"
    }
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            fmt = data.get("format", {})
            meta["duration"] = float(fmt.get("duration", 0.0))
            meta["file_size"] = int(fmt.get("size", meta["file_size"]))
            meta["format"] = fmt.get("format_name", "")

            streams = data.get("streams", [])
            for s in streams:
                if s.get("codec_type") == "video" and meta["width"] == 0:
                    meta["width"] = int(s.get("width", 0))
                    meta["height"] = int(s.get("height", 0))
                    meta["video_codec"] = s.get("codec_name", "")
                elif s.get("codec_type") == "audio" and not meta["audio_codec"]:
                    meta["audio_codec"] = s.get("codec_name", "")

            if meta["height"] > 0:
                meta["aspect_ratio"] = meta["width"] / meta["height"]
                meta["aspect_ratio_str"] = f"{meta['width']}:{meta['height']}"
    except Exception as e:
        log(f"Failed to inspect video with ffprobe: {e}")

    # Fallback duration if format duration was 0
    if meta["duration"] <= 0:
        meta["duration"] = 1.0

    return meta

def extract_frame(video_path: Path, timestamp: float, output_path: Path) -> bool:
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{timestamp:.3f}",
        "-i", str(video_path),
        "-vframes", "1",
        "-q:v", "2",
        str(output_path)
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return output_path.exists()

def generate_single_composite_thumbnail(video_path: Path, slug: str, temp_dir: Path, metadata: dict) -> Path:
    """
    Generates a single composite thumbnail based on the video's aspect ratio:
    - 9:16 (vertical): splits video into 3 parts, takes the first frame of each part,
      arranges 3 vertical panels side-by-side on a 16:9 canvas (Reference Image 1).
    - 16:9 (horizontal): splits video into 4 parts, takes the first frame of each part,
      arranges 4 frames in a 2x2 grid on a 16:9 canvas (Reference Image 2).
    - Other aspect ratios (e.g. 3:4, 4:3): arranges frames according to the image ratio.
    """
    duration = metadata.get("duration", 1.0)
    w = metadata.get("width", 0)
    h = metadata.get("height", 0)
    ratio = metadata.get("aspect_ratio", (w / h) if h > 0 else 1.0)

    log(f"Video resolution: {w}x{h} | Aspect Ratio: {ratio:.3f} | Duration: {duration:.2f}s")

    final_thumb_path = temp_dir / f"{slug}_thumb.jpg"

    # Identify layout type
    is_9_16 = 0.50 <= ratio <= 0.65
    is_16_9 = 1.60 <= ratio <= 1.95

    if is_9_16:
        # --- 9:16 Aspect Ratio -> 3 parts, 3 frames side-by-side on a 16:9 canvas ---
        log("Layout: 9:16 vertical video detected -> 3 intervals, 16:9 composite thumbnail")
        part_duration = duration / 3.0
        frame_times = [0.0, part_duration, 2.0 * part_duration]

        frame_imgs = []
        for idx, t in enumerate(frame_times):
            f_path = temp_dir / f"{slug}_part_{idx}.jpg"
            if not extract_frame(video_path, t, f_path) and idx > 0:
                extract_frame(video_path, 0.0, f_path)
            if f_path.exists():
                try:
                    frame_imgs.append(Image.open(f_path).convert("RGB"))
                except Exception:
                    pass

        # Target 16:9 canvas (1280 x 720) with white borders (Reference Image 1)
        canvas_w, canvas_h = 1280, 720
        canvas = Image.new("RGB", (canvas_w, canvas_h), color=(255, 255, 255))

        gap = 12
        panel_h = canvas_h - 2 * gap
        panel_w = int(panel_h * (9 / 16))

        total_content_w = 3 * panel_w + 2 * gap
        start_x = max(gap, (canvas_w - total_content_w) // 2)

        for i, img in enumerate(frame_imgs[:3]):
            resized = img.resize((panel_w, panel_h), Image.Resampling.LANCZOS)
            x = start_x + i * (panel_w + gap)
            y = gap
            canvas.paste(resized, (x, y))

        canvas.save(str(final_thumb_path), format="JPEG", quality=95)

        # Cleanup extracted temp frames
        for idx in range(len(frame_times)):
            p = temp_dir / f"{slug}_part_{idx}.jpg"
            if p.exists():
                p.unlink()

    elif is_16_9:
        # --- 16:9 Aspect Ratio -> 4 parts, 2x2 grid on a 16:9 canvas ---
        log("Layout: 16:9 horizontal video detected -> 4 intervals, 2x2 grid composite thumbnail")
        part_duration = duration / 4.0
        frame_times = [0.0, part_duration, 2.0 * part_duration, 3.0 * part_duration]

        frame_imgs = []
        for idx, t in enumerate(frame_times):
            f_path = temp_dir / f"{slug}_part_{idx}.jpg"
            if not extract_frame(video_path, t, f_path) and idx > 0:
                extract_frame(video_path, 0.0, f_path)
            if f_path.exists():
                try:
                    frame_imgs.append(Image.open(f_path).convert("RGB"))
                except Exception:
                    pass

        # Target 16:9 canvas (1280 x 720) with black borders (Reference Image 2)
        canvas_w, canvas_h = 1280, 720
        canvas = Image.new("RGB", (canvas_w, canvas_h), color=(0, 0, 0))

        gap = 8
        cell_w = (canvas_w - 3 * gap) // 2
        cell_h = (canvas_h - 3 * gap) // 2

        positions = [
            (gap, gap),
            (2 * gap + cell_w, gap),
            (gap, 2 * gap + cell_h),
            (2 * gap + cell_w, 2 * gap + cell_h)
        ]

        for i, img in enumerate(frame_imgs[:4]):
            resized = img.resize((cell_w, cell_h), Image.Resampling.LANCZOS)
            canvas.paste(resized, positions[i])

        canvas.save(str(final_thumb_path), format="JPEG", quality=95)

        # Cleanup extracted temp frames
        for idx in range(len(frame_times)):
            p = temp_dir / f"{slug}_part_{idx}.jpg"
            if p.exists():
                p.unlink()

    else:
        # --- Other Aspect Ratio (e.g. 3:4, 4:3, 1:1) ---
        log(f"Layout: Custom aspect ratio ({ratio:.3f}) -> arranging according to frames")
        if ratio < 1.0:
            # Portrait custom (e.g. 3:4) -> 3 panels side-by-side according to frame ratio
            part_duration = duration / 3.0
            frame_times = [0.0, part_duration, 2.0 * part_duration]
            frame_imgs = []
            for idx, t in enumerate(frame_times):
                f_path = temp_dir / f"{slug}_part_{idx}.jpg"
                if not extract_frame(video_path, t, f_path) and idx > 0:
                    extract_frame(video_path, 0.0, f_path)
                if f_path.exists():
                    try:
                        frame_imgs.append(Image.open(f_path).convert("RGB"))
                    except Exception:
                        pass

            panel_h = 720
            panel_w = max(100, int(panel_h * ratio))
            gap = 10
            canvas_w = 3 * panel_w + 4 * gap
            canvas_h = panel_h + 2 * gap
            canvas = Image.new("RGB", (canvas_w, canvas_h), color=(255, 255, 255))

            for i, img in enumerate(frame_imgs[:3]):
                resized = img.resize((panel_w, panel_h), Image.Resampling.LANCZOS)
                canvas.paste(resized, (gap + i * (panel_w + gap), gap))

            canvas.save(str(final_thumb_path), format="JPEG", quality=95)
        else:
            # Landscape / Square custom (e.g. 4:3, 1:1) -> 2x2 grid according to frame ratio
            part_duration = duration / 4.0
            frame_times = [0.0, part_duration, 2.0 * part_duration, 3.0 * part_duration]
            frame_imgs = []
            for idx, t in enumerate(frame_times):
                f_path = temp_dir / f"{slug}_part_{idx}.jpg"
                if not extract_frame(video_path, t, f_path) and idx > 0:
                    extract_frame(video_path, 0.0, f_path)
                if f_path.exists():
                    try:
                        frame_imgs.append(Image.open(f_path).convert("RGB"))
                    except Exception:
                        pass

            cell_h = 360
            cell_w = max(100, int(cell_h * ratio))
            gap = 8
            canvas_w = 2 * cell_w + 3 * gap
            canvas_h = 2 * cell_h + 3 * gap
            canvas = Image.new("RGB", (canvas_w, canvas_h), color=(0, 0, 0))

            positions = [
                (gap, gap),
                (2 * gap + cell_w, gap),
                (gap, 2 * gap + cell_h),
                (2 * gap + cell_w, 2 * gap + cell_h)
            ]

            for i, img in enumerate(frame_imgs[:4]):
                resized = img.resize((cell_w, cell_h), Image.Resampling.LANCZOS)
                canvas.paste(resized, positions[i])

            canvas.save(str(final_thumb_path), format="JPEG", quality=95)

        # Cleanup extracted temp frames
        for idx in range(len(frame_times)):
            p = temp_dir / f"{slug}_part_{idx}.jpg"
            if p.exists():
                p.unlink()

    if not final_thumb_path.exists():
        # Fallback single frame if composite failed
        extract_frame(video_path, 0.0, final_thumb_path)

    log(f"Single composite thumbnail generated: {final_thumb_path.name}")
    return final_thumb_path

def upload_thumb_to_r2(s3_dest, thumb_path: Path, slug: str) -> str:
    """
    Uploads thumbnail to Destination R2 inside 'new_thumbnails' folder.
    Returns the relative thumbnail path (NOT the URL link).
    """
    r2_key = f"new_thumbnails/{slug}_thumb.jpg"
    log(f"Uploading thumbnail to Destination R2: {r2_key}")
    s3_dest.upload_file(
        str(thumb_path),
        DEST_R2_BUCKET,
        r2_key,
        ExtraArgs={"ContentType": "image/jpeg"},
    )
    log(f"Thumbnail uploaded successfully to path: {r2_key}")
    return r2_key


# ============================================================
# UPLOADER ENGINES (UNCHANGED)
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
        except Exception as e:
            last_err = e
            log(f"[AnonMP4] Attempt {attempt} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 5)
    raise last_err

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

def upload_zerostorage(video_path: Path):
    file_path = Path(video_path)
    file_size = file_path.stat().st_size
    file_name = file_path.name
    upload_id = str(uuid.uuid4())
    total_chunks = (file_size + UPLOAD_CHUNK_SIZE - 1) // UPLOAD_CHUNK_SIZE

    log(f"[ZeroStorage] Uploading {file_name} ({file_size / 1024 / 1024:.2f} MB)")
    upload_url = "https://zerostorage.net/api/upload/chunked"
    uploaded = 0
    file_id = None

    with open(file_path, "rb") as f:
        for idx in range(total_chunks):
            chunk = f.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            metadata = {
                "uploadId": upload_id,
                "chunkIndex": idx,
                "totalChunks": total_chunks,
                "fileName": file_name,
                "fileSize": file_size,
                "chunkSize": len(chunk),
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
                    if attempt == MAX_RETRIES:
                        raise
                    time.sleep(attempt * 3)

            if not resp.ok:
                raise RuntimeError(f"[ZeroStorage] Chunk {idx+1} HTTP {resp.status_code}")

            uploaded += len(chunk)
            try:
                result = resp.json()
                if result.get("fileId"):
                    file_id = result["fileId"]
            except Exception:
                pass

    if not file_id:
        raise RuntimeError("[ZeroStorage] fileId missing.")

    return f"https://zerostorage.net/embed/{file_id}", f"https://zerostorage.net/api/files/download/{file_id}?track=true"


# ============================================================
# MAIN PROCESS LOGIC
# ============================================================

def process_video(s3_source, s3_dest, page, video: dict, temp_dir: Path, processed_titles: set):
    key       = video["key"]
    orig_ext  = Path(key).suffix.lower()
    orig_name = Path(key).name

    log("")
    log("=" * 65)
    log(f"PROCESSING : {orig_name}")
    log(f"R2 KEY     : {key}")
    log("=" * 65)

    slug = supabase_get_next_slug()
    new_name = make_rename(orig_ext)  # Original extension intact!
    local_file = temp_dir / new_name

    try:
        # Step 1: Download video from Source R2
        download_from_r2(s3_source, key, local_file)

        # Step 2: Extract metadata & check for duplicates in MongoDB
        file_hash = calculate_file_hash(local_file)
        metadata = get_video_metadata(local_file)

        is_dup, reason = is_duplicate_video(file_hash, metadata)
        if is_dup:
            log(f"[MongoDB] Duplicate video detected ({reason}). Skipping upload.")
            if local_file.exists():
                local_file.unlink()
            delete_from_r2(s3_source, key)
            return True

        # Step 3: Generate single composite thumbnail according to aspect ratio
        thumb_file = generate_single_composite_thumbnail(local_file, slug, temp_dir, metadata)

        # Step 4: Upload thumbnail to Destination R2 inside new_thumbnails folder
        thumb_path_rel = upload_thumb_to_r2(s3_dest, thumb_file, slug)
        if thumb_file.exists():
            thumb_file.unlink()

        # Step 5: Upload to video hosts (logic unchanged)
        log("[AnonMP4] Starting upload...")
        anon_url = upload_anonmp4_with_retry(page, local_file)
        log(f"✔ AnonMP4: {anon_url}")

        log("[Abyss] Starting upload...")
        abyss_url = upload_abyss(local_file)
        log(f"✔ Abyss: {abyss_url}")

        log("[ZeroStorage] Starting upload...")
        zs_embed, zs_video = upload_zerostorage(local_file)
        log(f"✔ ZS Video: {zs_video}")

        # Step 6: Save record to Supabase (saving thumbnail path, not link)
        record = {
            "slug":        slug,
            "title":       new_name,
            "thumbnail_1": thumb_path_rel,
            "thumbnail":   thumb_path_rel,
            "thumbnail_2": None,
            "thumbnail_3": None,
            "video_link":  zs_video,
            "zerostorage": zs_embed,
            "abyss":       abyss_url,
            "anonmp4":     anon_url,
        }
        supabase_insert_video(record)

        # Step 7: Save complete video metadata into MongoDB to prevent future duplicates
        metadata_doc = {
            "file_hash":        file_hash,
            "slug":             slug,
            "original_name":    orig_name,
            "title":            new_name,
            "file_size":        metadata.get("file_size", local_file.stat().st_size),
            "duration":         metadata.get("duration", 0.0),
            "width":            metadata.get("width", 0),
            "height":           metadata.get("height", 0),
            "aspect_ratio":     metadata.get("aspect_ratio_str", ""),
            "video_codec":      metadata.get("video_codec", ""),
            "audio_codec":      metadata.get("audio_codec", ""),
            "format":           metadata.get("format", ""),
            "thumbnail_path":   thumb_path_rel,
            "created_at":       datetime.utcnow()
        }
        save_video_metadata(metadata_doc)

        # Step 8: Clean up local file and delete original from Source R2
        if local_file.exists():
            local_file.unlink()

        delete_from_r2(s3_source, key)
        log("✔ COMPLETED SUCCESSFULLY")
        return True

    except Exception as e:
        log(f"✖ FAILED: {e}")
        if local_file.exists():
            local_file.unlink()
        return False


def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=auto_ping, daemon=True).start()

    validate_config()
    s3_source, s3_dest = create_r2_clients()
    supabase_init_table()

    temp_dir = Path(tempfile.mkdtemp(prefix="r2_backup_"))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page    = context.new_page()

        try:
            while True:
                videos = list_videos(s3_source)
                if not videos:
                    time.sleep(WATCH_INTERVAL)
                    continue

                processed_titles = supabase_get_processed_titles()
                already_done = getattr(main, "_processed_keys", set())
                new_videos = [v for v in videos if v["key"] not in already_done]

                if not new_videos:
                    time.sleep(WATCH_INTERVAL)
                    continue

                for i, video in enumerate(new_videos, start=1):
                    ok = process_video(s3_source, s3_dest, page, video, temp_dir, processed_titles)
                    if ok:
                        already_done.add(video["key"])
                main._processed_keys = already_done
                time.sleep(WATCH_INTERVAL)

        finally:
            context.close()
            browser.close()

if __name__ == "__main__":
    main()
