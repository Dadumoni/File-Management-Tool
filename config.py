import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CLOUDFLARE R2 CONFIGURATION
# ============================================================
# Source R2 (Video Download & Watch)
SOURCE_R2_ENDPOINT = os.getenv("SOURCE_R2_ENDPOINT") or os.getenv("R2_ENDPOINT")
SOURCE_R2_ACCESS_KEY = os.getenv("SOURCE_R2_ACCESS_KEY") or os.getenv("R2_ACCESS_KEY")
SOURCE_R2_SECRET_KEY = os.getenv("SOURCE_R2_SECRET_KEY") or os.getenv("R2_SECRET_KEY")
SOURCE_R2_BUCKET = os.getenv("SOURCE_R2_BUCKET") or os.getenv("R2_BUCKET")
SOURCE_R2_WATCH_FOLDER = os.getenv("SOURCE_R2_WATCH_FOLDER") or os.getenv("R2_WATCH_FOLDER", "uploads/")

# Destination R2 (Thumbnail Upload to new_thumbnails)
DEST_R2_ENDPOINT = os.getenv("DEST_R2_ENDPOINT") or SOURCE_R2_ENDPOINT
DEST_R2_ACCESS_KEY = os.getenv("DEST_R2_ACCESS_KEY") or SOURCE_R2_ACCESS_KEY
DEST_R2_SECRET_KEY = os.getenv("DEST_R2_SECRET_KEY") or SOURCE_R2_SECRET_KEY
DEST_R2_BUCKET = os.getenv("DEST_R2_BUCKET") or SOURCE_R2_BUCKET

# ============================================================
# SUPABASE CONFIGURATION
# ============================================================
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY")

# ============================================================
# MONGODB CONFIGURATION
# ============================================================
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "video_bot_db")

# ============================================================
# UPLOADER APIS & SERVICE URLS
# ============================================================
ZEROSTORAGE_API_KEY = os.getenv("ZEROSTORAGE_API_KEY")
APP_URL = os.getenv("APP_URL", "")

ANONMP4_URL = "https://anonmp4.to"
ABYSS_UPLOAD_URL = "http://up.hydrax.net/9e805aa56a78e2abd1be605e7af7b3cb"

# ============================================================
# CONSTANTS & TIMEOUTS
# ============================================================
MAX_RETRIES = 3
UPLOAD_TIMEOUT = 30 * 60   # 30 min
WATCH_INTERVAL = 30        # seconds between polls when idle
UPLOAD_CHUNK_SIZE = 2 * 1024 * 1024   # 2 MB for ZeroStorage

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm",
    ".flv", ".wmv", ".mpeg", ".mpg", ".m4v",
    ".ts", ".3gp", ".ogv", ".vob",
}

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
