import os
import sqlite3
import time
import threading
from pathlib import Path
from urllib.parse import quote
from config import (
    SOURCE_R2_ENDPOINT,
    SOURCE_R2_BUCKET,
    SOURCE_R2_PUBLIC_URL,
    DEST_R2_ENDPOINT,
    DEST_R2_BUCKET,
    DEST_R2_PUBLIC_URL,
    VIDEO_EXTENSIONS,
    IMAGE_EXTENSIONS
)

DB_PATH = Path(__file__).resolve().parent.parent / "r2_media.db"
_db_lock = threading.Lock()

sync_state = {
    "is_syncing": False,
    "last_synced": "",
    "total_videos": 0,
    "total_images": 0,
    "total_thumbnails": 0,
    "status_message": "Idle"
}

def get_connection():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db():
    with _db_lock:
        conn = get_connection()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS media_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bucket_type TEXT NOT NULL,
                    bucket_name TEXT NOT NULL,
                    key TEXT NOT NULL UNIQUE,
                    filename TEXT NOT NULL,
                    folder TEXT NOT NULL,
                    ext TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    size INTEGER NOT NULL DEFAULT 0,
                    last_modified TEXT NOT NULL,
                    direct_url TEXT NOT NULL,
                    stream_url TEXT NOT NULL,
                    synced_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_media_bucket_type ON media_items(bucket_type);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_media_file_type ON media_items(file_type);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_media_folder ON media_items(folder);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_media_key ON media_items(key);")
            conn.commit()
        finally:
            conn.close()

def make_direct_url(bucket_type: str, bucket_name: str, key: str) -> str:
    """
    Constructs a direct clean URL (NOT a signed URL).
    Uses public custom/r2.dev URL if set, otherwise standard direct bucket endpoint.
    """
    clean_key = quote(key.lstrip("/"), safe="/:@")
    if bucket_type == "source":
        if SOURCE_R2_PUBLIC_URL:
            return f"{SOURCE_R2_PUBLIC_URL.rstrip('/')}/{clean_key}"
        endpoint = (SOURCE_R2_ENDPOINT or "").rstrip("/")
        return f"{endpoint}/{bucket_name}/{clean_key}"
    else:
        if DEST_R2_PUBLIC_URL:
            return f"{DEST_R2_PUBLIC_URL.rstrip('/')}/{clean_key}"
        endpoint = (DEST_R2_ENDPOINT or "").rstrip("/")
        return f"{endpoint}/{bucket_name}/{clean_key}"

def make_stream_url(bucket_type: str, key: str) -> str:
    return f"/api/media/stream?target={bucket_type}&key={quote(key)}"

# ============================================================
# SYNC WORKER: SCANS BOTH R2 BUCKETS & POPULATES SQLITE
# ============================================================
def sync_r2_buckets(s3_source, s3_dest, log_cb=None):
    def _log(msg):
        if log_cb:
            log_cb(msg)
        else:
            print(f"[{time.strftime('%H:%M:%S')}] [R2 SQLite Sync] {msg}", flush=True)

    global sync_state
    if sync_state["is_syncing"]:
        _log("Sync already in progress. Skipping duplicate run.")
        return

    sync_state["is_syncing"] = True
    sync_state["status_message"] = "Scanning R2 buckets..."
    _log(f"⚡ Starting full R2 buckets scan (Source: {SOURCE_R2_BUCKET}, Dest: {DEST_R2_BUCKET})...")

    init_db()
    now_ts = time.strftime("%Y-%m-%d %H:%M:%S")
    source_keys = set()
    dest_keys = set()

    batch = []
    video_count = 0
    image_count = 0
    thumb_count = 0

    try:
        # 1. SCAN SOURCE R2 BUCKET
        if s3_source and SOURCE_R2_BUCKET:
            _log(f"Scanning Source R2 bucket '{SOURCE_R2_BUCKET}'...")
            paginator = s3_source.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=SOURCE_R2_BUCKET):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if key.endswith("/"):
                        continue
                    source_keys.add(key)
                    ext = Path(key).suffix.lower()
                    filename = Path(key).name
                    
                    folder = str(Path(key).parent).replace("\\", "/")
                    if folder == ".":
                        folder = ""
                    elif not folder.endswith("/"):
                        folder += "/"

                    if ext in VIDEO_EXTENSIONS:
                        file_type = "video"
                        video_count += 1
                    elif ext in IMAGE_EXTENSIONS:
                        file_type = "image"
                        image_count += 1
                    else:
                        file_type = "other"

                    last_mod = obj["LastModified"].strftime("%Y-%m-%d %H:%M") if obj.get("LastModified") else ""
                    d_url = make_direct_url("source", SOURCE_R2_BUCKET, key)
                    s_url = make_stream_url("source", key)

                    batch.append((
                        "source", SOURCE_R2_BUCKET, key, filename, folder, ext, file_type,
                        obj.get("Size", 0), last_mod, d_url, s_url, now_ts
                    ))

        # 2. SCAN DESTINATION R2 BUCKET (Thumbnails)
        if s3_dest and DEST_R2_BUCKET:
            _log(f"Scanning Destination R2 bucket '{DEST_R2_BUCKET}'...")
            paginator = s3_dest.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=DEST_R2_BUCKET):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if key.endswith("/"):
                        continue
                    dest_keys.add(key)
                    ext = Path(key).suffix.lower()
                    filename = Path(key).name

                    folder = str(Path(key).parent).replace("\\", "/")
                    if folder == ".":
                        folder = ""
                    elif not folder.endswith("/"):
                        folder += "/"

                    file_type = "thumbnail" if (ext in IMAGE_EXTENSIONS or "thumbnails" in key.lower()) else "other"
                    if file_type == "thumbnail":
                        thumb_count += 1

                    last_mod = obj["LastModified"].strftime("%Y-%m-%d %H:%M") if obj.get("LastModified") else ""
                    d_url = make_direct_url("dest", DEST_R2_BUCKET, key)
                    s_url = make_stream_url("dest", key)

                    batch.append((
                        "dest", DEST_R2_BUCKET, key, filename, folder, ext, file_type,
                        obj.get("Size", 0), last_mod, d_url, s_url, now_ts
                    ))

        # 3. SAVE TO SQLITE DB IN TRANSACTION
        _log(f"Writing {len(batch)} scanned items into local SQLite database...")
        with _db_lock:
            conn = get_connection()
            try:
                conn.executemany("""
                    INSERT INTO media_items (
                        bucket_type, bucket_name, key, filename, folder, ext, file_type,
                        size, last_modified, direct_url, stream_url, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        filename=excluded.filename,
                        folder=excluded.folder,
                        ext=excluded.ext,
                        file_type=excluded.file_type,
                        size=excluded.size,
                        last_modified=excluded.last_modified,
                        direct_url=excluded.direct_url,
                        stream_url=excluded.stream_url,
                        synced_at=excluded.synced_at
                """, batch)

                # Remove stale items that were deleted from R2
                if source_keys:
                    conn.execute("DELETE FROM media_items WHERE bucket_type='source' AND synced_at != ?", (now_ts,))
                if dest_keys:
                    conn.execute("DELETE FROM media_items WHERE bucket_type='dest' AND synced_at != ?", (now_ts,))

                conn.commit()
            finally:
                conn.close()

        sync_state["total_videos"] = video_count
        sync_state["total_images"] = image_count
        sync_state["total_thumbnails"] = thumb_count
        sync_state["last_synced"] = now_ts
        sync_state["status_message"] = "Sync Complete"
        _log(f"✔ R2 SQLite Sync finished! Found {video_count} videos, {image_count} images, {thumb_count} thumbnails.")

    except Exception as err:
        sync_state["status_message"] = f"Sync Error: {err}"
        _log(f"✖ R2 SQLite Sync failed: {err}")
    finally:
        sync_state["is_syncing"] = False

def start_background_sync(s3_source, s3_dest, log_cb=None):
    t = threading.Thread(target=sync_r2_buckets, args=(s3_source, s3_dest, log_cb), daemon=True, name="R2SqliteSyncWorker")
    t.start()
    return t

# ============================================================
# QUERY METHODS: INSTANT READS FROM SQLITE
# ============================================================
def get_counts():
    conn = get_connection()
    try:
        row_v = conn.execute("SELECT COUNT(*) AS c FROM media_items WHERE bucket_type='source' AND file_type='video'").fetchone()
        row_i = conn.execute("SELECT COUNT(*) AS c FROM media_items WHERE bucket_type='source' AND file_type='image'").fetchone()
        row_t = conn.execute("SELECT COUNT(*) AS c FROM media_items WHERE bucket_type='dest' AND (file_type='thumbnail' OR file_type='image')").fetchone()
        
        return {
            "videos": row_v["c"] if row_v else 0,
            "images": row_i["c"] if row_i else 0,
            "thumbnails": row_t["c"] if row_t else 0,
            "source_bucket": SOURCE_R2_BUCKET,
            "dest_bucket": DEST_R2_BUCKET,
            "is_syncing": sync_state["is_syncing"],
            "last_synced": sync_state["last_synced"]
        }
    finally:
        conn.close()

def list_directory_from_db(prefix=""):
    """
    Returns files and immediate subfolders under prefix directly from SQLite.
    """
    prefix = (prefix or "").strip()
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    conn = get_connection()
    try:
        # Fetch all items in this folder or deeper
        rows = conn.execute(
            "SELECT key, filename, folder, ext, file_type, size, last_modified, direct_url, stream_url FROM media_items WHERE bucket_type='source' AND key LIKE ? ORDER BY filename ASC",
            (prefix + "%",)
        ).fetchall()

        folders_set = set()
        files = []

        for r in rows:
            key = r["key"]
            if key == prefix:
                continue

            rel_path = key[len(prefix):]
            if "/" in rel_path:
                # Subfolder
                subfolder_name = rel_path.split("/")[0]
                folders_set.add(prefix + subfolder_name + "/")
            else:
                # Direct file in this folder
                files.append({
                    "name": r["filename"],
                    "key": r["key"],
                    "size": r["size"],
                    "last_modified": r["last_modified"],
                    "ext": r["ext"].lstrip("."),
                    "is_video": r["file_type"] == "video",
                    "is_image": r["file_type"] == "image",
                    "direct_url": r["direct_url"],
                    "stream_url": r["stream_url"]
                })

        folders = []
        for f_prefix in sorted(list(folders_set)):
            f_name = f_prefix[len(prefix):].rstrip("/")
            folders.append({"name": f_name, "prefix": f_prefix})

        return {
            "prefix": prefix,
            "folders": folders,
            "files": files,
            "bucket": SOURCE_R2_BUCKET
        }
    finally:
        conn.close()

def list_all_images_from_db(search=""):
    """
    Returns ALL images across all folders from Source R2 (flat view).
    """
    conn = get_connection()
    try:
        query = "SELECT key, filename, folder, ext, size, last_modified, direct_url, stream_url FROM media_items WHERE bucket_type='source' AND file_type='image'"
        params = []
        if search:
            query += " AND (filename LIKE ? OR key LIKE ?)"
            s_param = f"%{search}%"
            params.extend([s_param, s_param])
        query += " ORDER BY last_modified DESC, id DESC"

        rows = conn.execute(query, params).fetchall()
        images = []
        for r in rows:
            images.append({
                "key": r["key"],
                "name": r["filename"],
                "folder": r["folder"],
                "size": r["size"],
                "last_modified": r["last_modified"],
                "ext": r["ext"].lstrip("."),
                "direct_url": r["direct_url"],
                "stream_url": r["stream_url"]
            })
        return images
    finally:
        conn.close()

def list_all_videos_from_db(search=""):
    """
    Returns ALL videos across all folders from Source R2 (flat view).
    """
    conn = get_connection()
    try:
        query = "SELECT key, filename, folder, ext, size, last_modified, direct_url, stream_url FROM media_items WHERE bucket_type='source' AND file_type='video'"
        params = []
        if search:
            query += " AND (filename LIKE ? OR key LIKE ?)"
            s_param = f"%{search}%"
            params.extend([s_param, s_param])
        query += " ORDER BY last_modified DESC, id DESC"

        rows = conn.execute(query, params).fetchall()
        videos = []
        for r in rows:
            videos.append({
                "key": r["key"],
                "name": r["filename"],
                "folder": r["folder"],
                "size": r["size"],
                "last_modified": r["last_modified"],
                "ext": r["ext"].lstrip("."),
                "direct_url": r["direct_url"],
                "stream_url": r["stream_url"]
            })
        return videos
    finally:
        conn.close()

def list_thumbnails_from_db(search=""):
    """
    Returns ALL thumbnails from Destination R2.
    """
    conn = get_connection()
    try:
        query = "SELECT key, filename, folder, ext, size, last_modified, direct_url, stream_url FROM media_items WHERE bucket_type='dest' AND (file_type='thumbnail' OR file_type='image')"
        params = []
        if search:
            query += " AND (filename LIKE ? OR key LIKE ?)"
            s_param = f"%{search}%"
            params.extend([s_param, s_param])
        query += " ORDER BY last_modified DESC, id DESC"

        rows = conn.execute(query, params).fetchall()
        thumbs = []
        for r in rows:
            thumbs.append({
                "key": r["key"],
                "name": r["filename"],
                "size": r["size"],
                "last_modified": r["last_modified"],
                "ext": r["ext"].lstrip("."),
                "direct_url": r["direct_url"],
                "stream_url": r["stream_url"]
            })
        return thumbs
    finally:
        conn.close()

def delete_from_db(keys=None, prefixes=None, bucket_type="source"):
    """
    Removes deleted keys or prefixes from SQLite immediately.
    """
    with _db_lock:
        conn = get_connection()
        try:
            if keys:
                conn.executemany("DELETE FROM media_items WHERE bucket_type=? AND key=?", [(bucket_type, k) for k in keys])
            if prefixes:
                for p in prefixes:
                    conn.execute("DELETE FROM media_items WHERE bucket_type=? AND key LIKE ?", (bucket_type, p + "%"))
            conn.commit()
        finally:
            conn.close()
