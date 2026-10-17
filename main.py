import os
import time
import tempfile
import threading
from pathlib import Path
from datetime import datetime
from flask import Flask, jsonify, request, render_template_string, Response, redirect
from playwright.sync_api import sync_playwright

from config import (
    validate_config,
    SOURCE_R2_BUCKET,
    SOURCE_R2_PUBLIC_URL,
    DEST_R2_BUCKET,
    DEST_R2_PUBLIC_URL
)
from storage.r2_client import (
    create_r2_clients,
    list_directory,
    get_all_videos_under_targets,
    delete_targets_from_r2,
    download_from_r2,
    delete_from_r2,
    upload_thumb_to_r2,
    make_rename,
    list_destination_thumbnails,
    delete_from_dest_r2,
    get_bucket_counts
)
from database.sqlite_store import (
    init_db,
    start_background_sync,
    get_counts,
    list_directory_from_db,
    list_all_images_from_db,
    list_all_videos_from_db,
    list_thumbnails_from_db,
    delete_from_db,
    sync_state
)
from database.supabase_client import (
    supabase_init_table,
    supabase_get_next_slug,
    supabase_insert_video,
    supabase_get_processed_titles
)
from database.mongo_client import (
    calculate_file_hash,
    is_duplicate_video,
    save_video_metadata
)
from media.metadata import get_video_metadata
from media.thumbnail import generate_single_composite_thumbnail
from uploaders.anonmp4 import upload_anonmp4_with_retry
from uploaders.abyss import upload_abyss
from uploaders.zerostorage import upload_zerostorage
from server.health import start_auto_ping

# ============================================================
# INITIALIZATION & LOGGING
# ============================================================
app = Flask(__name__)
s3_source = None
s3_dest = None

task_lock = threading.Lock()
current_task = {
    "is_running": False,
    "total": 0,
    "current_index": 0,
    "current_key": "",
    "current_stage": "",
    "completed": [],
    "failed": [],
    "logs": []
}

def log(msg):
    ts = time.strftime('%H:%M:%S')
    formatted = f"[{ts}] {msg}"
    print(formatted, flush=True)
    with task_lock:
        current_task["logs"].append(formatted)
        if len(current_task["logs"]) > 250:
            current_task["logs"].pop(0)

def init_services():
    global s3_source, s3_dest
    try:
        validate_config()
        s3_source, s3_dest = create_r2_clients()
        supabase_init_table()
        init_db()
        # Scan both buckets automatically as soon as app starts/deploys
        start_background_sync(s3_source, s3_dest, log)
        log("✔ S3 R2 Clients, Supabase table, and SQLite background scanner started.")
    except Exception as e:
        log(f"✖ Service initialization warning: {e}")

# ============================================================
# VIDEO PROCESSING WORKFLOW (Single video)
# ============================================================
def process_single_video(page, key: str, temp_dir: Path, processed_titles: set):
    orig_ext  = Path(key).suffix.lower()
    orig_name = Path(key).name

    with task_lock:
        current_task["current_key"] = orig_name
        current_task["current_stage"] = "Initializing..."

    log("")
    log("=" * 65)
    log(f"PROCESSING : {orig_name}")
    log(f"R2 KEY     : {key}")
    log("=" * 65)

    slug = supabase_get_next_slug()
    new_name = make_rename(orig_ext)
    local_file = temp_dir / new_name

    try:
        # Step 1: Download video from Source R2
        with task_lock:
            current_task["current_stage"] = "Downloading from Source R2..."
        download_from_r2(s3_source, key, local_file)

        # Step 2: Extract technical metadata & check for duplicates in MongoDB
        with task_lock:
            current_task["current_stage"] = "Checking duplicate in MongoDB..."
        file_hash = calculate_file_hash(local_file)
        metadata = get_video_metadata(local_file)

        is_dup, reason = is_duplicate_video(file_hash, metadata)
        if is_dup:
            log(f"[MongoDB] Duplicate video detected ({reason}). Skipping upload.")
            if local_file.exists():
                local_file.unlink()
            delete_from_r2(s3_source, key)
            with task_lock:
                current_task["completed"].append({"key": key, "status": "duplicate_skipped", "reason": reason})
            return True

        # Step 3: Generate single composite thumbnail based on aspect ratio
        with task_lock:
            current_task["current_stage"] = "Generating composite thumbnail..."
        thumb_file = generate_single_composite_thumbnail(local_file, slug, temp_dir, metadata)

        # Step 4: Upload thumbnail to Destination R2 inside 'new_thumbnails' folder
        with task_lock:
            current_task["current_stage"] = "Uploading thumbnail to Destination R2..."
        thumb_path_rel = upload_thumb_to_r2(s3_dest, thumb_file, slug)
        if thumb_file.exists():
            thumb_file.unlink()

        # Step 5: Upload to video hosts
        with task_lock:
            current_task["current_stage"] = "Uploading to AnonMP4..."
        log("[AnonMP4] Starting upload...")
        anon_url = upload_anonmp4_with_retry(page, local_file)
        log(f"✔ AnonMP4: {anon_url}")

        with task_lock:
            current_task["current_stage"] = "Uploading to Abyss..."
        log("[Abyss] Starting upload...")
        abyss_url = upload_abyss(local_file)
        log(f"✔ Abyss: {abyss_url}")

        with task_lock:
            current_task["current_stage"] = "Uploading to ZeroStorage..."
        log("[ZeroStorage] Starting upload...")
        zs_embed, zs_video = upload_zerostorage(local_file)
        log(f"✔ ZS Video: {zs_video}")

        # Step 6: Save record to Supabase
        with task_lock:
            current_task["current_stage"] = "Saving record to Supabase & MongoDB..."
        record = {
            "slug":        slug,
            "title":       new_name,
            "thumbnail":   thumb_path_rel,
            "video_link":  zs_video,
            "zerostorage": zs_embed,
            "abyss":       abyss_url,
            "anonmp4":     anon_url,
        }
        supabase_insert_video(record)

        # Step 7: Save rich metadata in MongoDB
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
        delete_from_db(keys=[key], bucket_type="source")
        log(f"✔ COMPLETED SUCCESSFULLY: {orig_name}")

        with task_lock:
            current_task["completed"].append({"key": key, "slug": slug, "status": "success"})
        return True

    except Exception as e:
        log(f"✖ FAILED: {orig_name} - {e}")
        if local_file.exists():
            local_file.unlink()
        with task_lock:
            current_task["failed"].append({"key": key, "error": str(e)})
        return False

def background_process_worker(video_keys: list):
    temp_dir = Path(tempfile.mkdtemp(prefix="r2_manual_"))
    processed_titles = supabase_get_processed_titles()

    with task_lock:
        current_task["is_running"] = True
        current_task["total"] = len(video_keys)
        current_task["current_index"] = 0
        current_task["completed"] = []
        current_task["failed"] = []
        current_task["logs"].append(f"Starting processing queue for {len(video_keys)} video(s)...")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()

            try:
                for idx, key in enumerate(video_keys, start=1):
                    with task_lock:
                        current_task["current_index"] = idx
                    process_single_video(page, key, temp_dir, processed_titles)
            finally:
                context.close()
                browser.close()
    except Exception as err:
        log(f"Worker execution error: {err}")
    finally:
        with task_lock:
            current_task["is_running"] = False
            current_task["current_stage"] = "All Selected Tasks Completed"
            current_task["logs"].append("Processing finished.")

# ============================================================
# FLASK WEB ENDPOINTS
# ============================================================
@app.route("/health")
@app.route("/ping")
def health():
    return jsonify({"status": "ok", "service": "File-Management-Tool", "timestamp": time.time()}), 200

@app.route("/api/files")
def api_files():
    prefix = request.args.get("prefix", "")
    page = request.args.get("page", 1, type=int)
    limit = request.args.get("limit", 36, type=int)
    search = request.args.get("search", "")
    try:
        data = list_directory_from_db(prefix, page=page, limit=limit, search=search)
        return jsonify({"success": True, "data": data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/images")
def api_images():
    search = request.args.get("search", "")
    page = request.args.get("page", 1, type=int)
    limit = request.args.get("limit", 36, type=int)
    try:
        res = list_all_images_from_db(search, page=page, limit=limit)
        return jsonify({
            "success": True, 
            "images": res["items"], 
            "total": res["total"],
            "page": res["page"],
            "limit": res["limit"],
            "total_pages": res["total_pages"]
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/videos")
def api_videos():
    search = request.args.get("search", "")
    page = request.args.get("page", 1, type=int)
    limit = request.args.get("limit", 24, type=int)
    try:
        res = list_all_videos_from_db(search, page=page, limit=limit)
        return jsonify({
            "success": True, 
            "videos": res["items"], 
            "total": res["total"],
            "page": res["page"],
            "limit": res["limit"],
            "total_pages": res["total_pages"]
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/thumbnails")
def api_thumbnails():
    search = request.args.get("search", "")
    page = request.args.get("page", 1, type=int)
    limit = request.args.get("limit", 36, type=int)
    try:
        res = list_thumbnails_from_db(search, page=page, limit=limit)
        return jsonify({
            "success": True, 
            "thumbnails": res["items"], 
            "total": res["total"],
            "page": res["page"],
            "limit": res["limit"],
            "total_pages": res["total_pages"],
            "bucket": DEST_R2_BUCKET
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/thumbnails/delete", methods=["POST"])
def api_thumbnails_delete():
    payload = request.get_json() or {}
    key = payload.get("key", "")
    if not key:
        return jsonify({"success": False, "error": "No thumbnail key provided."}), 400
    try:
        delete_from_dest_r2(s3_dest, key)
        delete_from_db(keys=[key], bucket_type="dest")
        return jsonify({"success": True, "key": key})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/stats")
def api_stats():
    try:
        stats = get_counts()
        return jsonify({"success": True, "stats": stats})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/sync", methods=["POST"])
def api_sync():
    try:
        start_background_sync(s3_source, s3_dest, log)
        return jsonify({"success": True, "message": "R2 buckets scan started in background."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/process", methods=["POST"])
def api_process():
    with task_lock:
        if current_task["is_running"]:
            return jsonify({"success": False, "error": "A processing task is already in progress. Please wait for it to finish."}), 400

    payload = request.get_json() or {}
    keys = payload.get("keys", [])
    folders = payload.get("folders", [])

    if not keys and not folders:
        return jsonify({"success": False, "error": "No files or folders selected."}), 400

    video_keys = get_all_videos_under_targets(s3_source, keys, folders)
    if not video_keys:
        return jsonify({"success": False, "error": "No valid video files found in the selection."}), 400

    thread = threading.Thread(target=background_process_worker, args=(video_keys,), daemon=True)
    thread.start()

    return jsonify({"success": True, "count": len(video_keys), "message": f"Started processing {len(video_keys)} video(s)."})

@app.route("/api/delete", methods=["POST"])
def api_delete():
    payload = request.get_json() or {}
    keys = payload.get("keys", [])
    folders = payload.get("folders", [])

    if not keys and not folders:
        return jsonify({"success": False, "error": "No files or folders selected."}), 400

    try:
        deleted_count = delete_targets_from_r2(s3_source, keys, folders)
        delete_from_db(keys=keys, prefixes=folders, bucket_type="source")
        return jsonify({"success": True, "deleted_count": deleted_count})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/task/status")
def api_task_status():
    with task_lock:
        return jsonify({
            "is_running": current_task["is_running"],
            "total": current_task["total"],
            "current_index": current_task["current_index"],
            "current_key": current_task["current_key"],
            "current_stage": current_task["current_stage"],
            "completed_count": len(current_task["completed"]),
            "failed_count": len(current_task["failed"]),
            "logs": current_task["logs"][-60:]
        })

@app.route("/api/media/stream", methods=["GET", "HEAD"])
def media_stream():
    """
    High-performance media streaming supporting RFC 7233 HTTP Range requests.
    Enables instant video playback and smooth seeking directly from Cloudflare R2.
    """
    key = request.args.get("key", "")
    target = request.args.get("target", "source")
    if not key:
        return "Missing key parameter", 400

    client = s3_dest if target == "dest" else s3_source
    bucket = DEST_R2_BUCKET if target == "dest" else SOURCE_R2_BUCKET

    if not client or not bucket:
        return "Storage client not initialized", 500

    public_url = SOURCE_R2_PUBLIC_URL if target == "source" else DEST_R2_PUBLIC_URL
    if public_url:
        clean_key = quote(key.lstrip("/"), safe="/:@")
        return redirect(f"{public_url.rstrip('/')}/{clean_key}", code=302)

    ext = Path(key).suffix.lower()
    content_type = "video/mp4"
    if ext in [".jpg", ".jpeg"]:
        content_type = "image/jpeg"
    elif ext == ".png":
        content_type = "image/png"
    elif ext == ".webp":
        content_type = "image/webp"
    elif ext == ".gif":
        content_type = "image/gif"
    elif ext == ".webm":
        content_type = "video/webm"
    elif ext in [".mkv", ".ts"]:
        content_type = "video/mp4"
    elif ext == ".mov":
        content_type = "video/quicktime"
    elif ext == ".3gp":
        content_type = "video/3gpp"

    range_header = request.headers.get("Range", None)

    try:
        # Browser HEAD request: probe for range support and total file size
        if request.method == "HEAD":
            s3_meta = client.head_object(Bucket=bucket, Key=key)
            resp = Response(status=200, content_type=content_type)
            resp.headers["Accept-Ranges"] = "bytes"
            resp.headers["Content-Length"] = str(s3_meta.get("ContentLength", 0))
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp

        # HTTP Range request: stream only the requested byte chunk for instant playback & seeking
        if range_header:
            s3_res = client.get_object(Bucket=bucket, Key=key, Range=range_header)
            content_range = s3_res.get("ContentRange")
            content_length = s3_res.get("ContentLength")

            def generate_range():
                for chunk in s3_res["Body"].iter_chunks(chunk_size=64 * 1024):
                    yield chunk

            resp = Response(generate_range(), status=206, content_type=content_type)
            resp.headers["Accept-Ranges"] = "bytes"
            if content_range:
                resp.headers["Content-Range"] = content_range
            if content_length:
                resp.headers["Content-Length"] = str(content_length)
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Cache-Control"] = "no-cache"
            return resp

        # Full GET request
        s3_res = client.get_object(Bucket=bucket, Key=key)
        content_length = s3_res.get("ContentLength", 0)

        def generate_full():
            for chunk in s3_res["Body"].iter_chunks(chunk_size=64 * 1024):
                yield chunk

        resp = Response(generate_full(), status=200, content_type=content_type)
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Content-Length"] = str(content_length)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    except Exception as e:
        log(f"Stream error for key '{key}': {e}")
        return f"Streaming error: {e}", 500

# ============================================================
# EMBEDDED SINGLE-PAGE WEB EXPLORER (INDEX PAGE)
# ============================================================
INDEX_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>R2 Media & File Manager</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://unpkg.com/lucide@latest"></script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
    body { font-family: 'Inter', system-ui, sans-serif; }
    .font-mono { font-family: 'JetBrains Mono', monospace; }
    #copyToast { transition: opacity 0.3s ease, transform 0.3s ease; }
  </style>
</head>
<body class="bg-zinc-950 text-zinc-100 min-h-screen flex flex-col antialiased">

  <!-- TOP HEADER -->
  <header class="sticky top-0 z-40 bg-zinc-900/95 backdrop-blur border-b border-zinc-800 px-4 lg:px-8 py-3 transition-all shadow-sm">
    <div class="max-w-7xl mx-auto flex flex-wrap items-center justify-between gap-3">
      
      <!-- Brand & Navigation Menu Tabs -->
      <div class="flex items-center gap-4 flex-wrap">
        <div class="flex items-center gap-2.5">
          <div class="h-9 w-9 rounded-lg bg-blue-600/20 border border-blue-500/30 grid place-items-center text-blue-400">
            <i data-lucide="database" class="w-5 h-5"></i>
          </div>
          <div>
            <h1 class="text-sm font-semibold tracking-tight text-white flex items-center gap-2">
              R2 Media Manager
              <span class="text-[10px] font-mono font-normal bg-emerald-950 text-emerald-300 px-2 py-0.5 rounded border border-emerald-800">SQLite Engine</span>
            </h1>
            <p class="text-[11px] text-zinc-400 font-mono">Source: {{ source_bucket }} | Dest: {{ dest_bucket }}</p>
          </div>
        </div>

        <!-- Navigation Tabs: Explorer, All Images, All Videos, Thumbnails -->
        <div class="flex items-center bg-zinc-950 p-1 rounded-xl border border-zinc-800 text-xs">
          <button 
            id="tabBtnExplorer" 
            onclick="switchTab('explorer')" 
            class="px-3 py-1.5 rounded-lg font-medium transition-all flex items-center gap-1.5 bg-zinc-800 text-white shadow-xs cursor-pointer"
          >
            <i data-lucide="folder" class="w-3.5 h-3.5 text-blue-400"></i>
            <span>Explorer</span>
          </button>
          
          <button 
            id="tabBtnImages" 
            onclick="switchTab('images')" 
            class="px-3 py-1.5 rounded-lg font-medium transition-all flex items-center gap-1.5 text-zinc-400 hover:text-white cursor-pointer"
          >
            <i data-lucide="image" class="w-3.5 h-3.5 text-emerald-400"></i>
            <span>All Images</span>
            <span id="tabImagesCountBadge" class="ml-1 text-[10px] font-mono px-1.5 py-0.2 rounded-full bg-emerald-950/80 text-emerald-300 border border-emerald-800/60">0</span>
          </button>

          <button 
            id="tabBtnVideos" 
            onclick="switchTab('videos')" 
            class="px-3 py-1.5 rounded-lg font-medium transition-all flex items-center gap-1.5 text-zinc-400 hover:text-white cursor-pointer"
          >
            <i data-lucide="film" class="w-3.5 h-3.5 text-rose-400"></i>
            <span>All Videos</span>
            <span id="tabVideosCountBadge" class="ml-1 text-[10px] font-mono px-1.5 py-0.2 rounded-full bg-rose-950/80 text-rose-300 border border-rose-800/60">0</span>
          </button>

          <button 
            id="tabBtnThumbnails" 
            onclick="switchTab('thumbnails')" 
            class="px-3 py-1.5 rounded-lg font-medium transition-all flex items-center gap-1.5 text-zinc-400 hover:text-white cursor-pointer"
          >
            <i data-lucide="sparkles" class="w-3.5 h-3.5 text-amber-400"></i>
            <span>Thumbnails</span>
            <span id="tabThumbsCountBadge" class="ml-1 text-[10px] font-mono px-1.5 py-0.2 rounded-full bg-amber-950/80 text-amber-300 border border-amber-800/60">0</span>
          </button>
        </div>
      </div>

      <!-- Action Buttons (Sync, Tasks) -->
      <div class="flex items-center gap-2">
        <!-- Manual Rescan R2 Button -->
        <button 
          id="btnRescan" 
          onclick="triggerRescan()" 
          class="px-3 py-1.5 rounded-lg border border-zinc-700 bg-zinc-800 hover:bg-zinc-700 text-xs font-medium text-zinc-200 flex items-center gap-1.5 transition-colors cursor-pointer"
          title="Rescan R2 buckets and update SQLite database"
        >
          <i data-lucide="refresh-cw" class="w-3.5 h-3.5 text-blue-400" id="syncSpinIcon"></i>
          <span id="syncBtnText">Rescan R2</span>
        </button>

        <button onclick="openTaskModal()" class="px-3 py-1.5 rounded-lg border border-zinc-700 bg-zinc-800 hover:bg-zinc-700 text-xs font-medium text-zinc-200 flex items-center gap-1.5 transition-colors cursor-pointer">
          <i data-lucide="activity" class="w-3.5 h-3.5 text-emerald-400"></i>
          <span>Tasks</span>
          <span id="taskStatusDot" class="w-2 h-2 rounded-full bg-zinc-500 inline-block"></span>
        </button>
      </div>

    </div>
  </header>

  <!-- BATCH SELECTION BAR -->
  <div id="selectionHeader" class="hidden sticky top-14 z-30 bg-blue-950/95 border-b border-blue-800/80 px-4 lg:px-8 py-2.5 shadow-lg backdrop-blur">
    <div class="max-w-7xl mx-auto flex flex-wrap items-center justify-between gap-3">
      <div class="flex items-center gap-2.5">
        <span class="h-2 w-2 rounded-full bg-blue-400 animate-pulse"></span>
        <span id="selectedCountText" class="text-xs font-semibold text-blue-200 font-mono">0 item(s) selected</span>
        <button onclick="clearSelection()" class="text-[11px] text-blue-400 hover:text-white underline cursor-pointer ml-1">Clear</button>
      </div>

      <div class="flex items-center gap-2">
        <button id="batchProcessBtn" onclick="triggerProcessSelected()" class="px-3.5 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold flex items-center gap-1.5 shadow transition-all cursor-pointer">
          <i data-lucide="download-cloud" class="w-3.5 h-3.5"></i>
          <span>Download & Process</span>
        </button>

        <button onclick="openDeleteModal()" class="px-3.5 py-1.5 rounded-lg bg-red-600 hover:bg-red-500 text-white text-xs font-semibold flex items-center gap-1.5 shadow transition-all cursor-pointer">
          <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
          <span>Delete from R2</span>
        </button>
      </div>
    </div>
  </div>

  <!-- MAIN BODY -->
  <main class="flex-1 max-w-7xl w-full mx-auto p-4 lg:p-8 space-y-5">

    <!-- SUMMARY STATS: CHOTE CHOTE CARDS -->
    <div class="grid grid-cols-1 sm:grid-cols-3 gap-3.5">
      
      <!-- Card 1: R2 Videos (Click shows all videos) -->
      <div 
        onclick="switchTab('videos')" 
        class="bg-zinc-900 border border-zinc-800 hover:border-rose-500/50 rounded-xl p-3.5 flex items-center justify-between shadow-xs cursor-pointer transition-all hover:bg-zinc-900/80 group"
      >
        <div class="flex items-center gap-3">
          <div class="h-10 w-10 rounded-xl bg-rose-950/60 border border-rose-800/60 text-rose-400 grid place-items-center group-hover:scale-105 transition-transform">
            <i data-lucide="film" class="w-5 h-5"></i>
          </div>
          <div>
            <div class="text-[11px] font-medium text-zinc-400 uppercase tracking-wider">R2 Videos</div>
            <div id="statVideosCount" class="text-lg font-bold text-white font-mono leading-tight">--</div>
            <div class="text-[10px] text-zinc-500">Click to view all videos</div>
          </div>
        </div>
        <div class="text-right">
          <span class="text-[10px] font-mono px-2 py-0.5 rounded bg-rose-950/80 text-rose-300 border border-rose-800/60">View All &rarr;</span>
        </div>
      </div>

      <!-- Card 2: R2 Images (Click shows ALL IMAGES across ALL folders flat!) -->
      <div 
        onclick="switchTab('images')" 
        class="bg-zinc-900 border border-zinc-800 hover:border-emerald-500/50 rounded-xl p-3.5 flex items-center justify-between shadow-xs cursor-pointer transition-all hover:bg-zinc-900/80 group"
      >
        <div class="flex items-center gap-3">
          <div class="h-10 w-10 rounded-xl bg-emerald-950/60 border border-emerald-800/60 text-emerald-400 grid place-items-center group-hover:scale-105 transition-transform">
            <i data-lucide="image" class="w-5 h-5"></i>
          </div>
          <div>
            <div class="text-[11px] font-medium text-zinc-400 uppercase tracking-wider">R2 Images</div>
            <div id="statImagesCount" class="text-lg font-bold text-white font-mono leading-tight">--</div>
            <div class="text-[10px] text-zinc-500">Click to view all images (Flat)</div>
          </div>
        </div>
        <div class="text-right">
          <span class="text-[10px] font-mono px-2 py-0.5 rounded bg-emerald-950/80 text-emerald-300 border border-emerald-800/60">View All &rarr;</span>
        </div>
      </div>

      <!-- Card 3: Thumbnails (Destination) -->
      <div 
        onclick="switchTab('thumbnails')" 
        class="bg-zinc-900 border border-zinc-800 hover:border-amber-500/50 rounded-xl p-3.5 flex items-center justify-between shadow-xs cursor-pointer transition-all hover:bg-zinc-900/80 group"
      >
        <div class="flex items-center gap-3">
          <div class="h-10 w-10 rounded-xl bg-amber-950/60 border border-amber-800/60 text-amber-400 grid place-items-center group-hover:scale-105 transition-transform">
            <i data-lucide="sparkles" class="w-5 h-5"></i>
          </div>
          <div>
            <div class="text-[11px] font-medium text-zinc-400 uppercase tracking-wider">Thumbnails</div>
            <div id="statThumbsCount" class="text-lg font-bold text-white font-mono leading-tight">--</div>
            <div class="text-[10px] text-zinc-500">Destination R2 new_thumbnails/</div>
          </div>
        </div>
        <div class="text-right">
          <span class="text-[10px] font-mono px-2 py-0.5 rounded bg-amber-950/80 text-amber-300 border border-amber-800/60">Gallery &rarr;</span>
        </div>
      </div>

    </div>

    <!-- ============================================================ -->
    <!-- VIEW 1: ALL IMAGES (FLAT ACROSS ALL FOLDERS) -->
    <!-- ============================================================ -->
    <div id="viewImages" class="hidden space-y-4">
      <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-3 flex flex-col md:flex-row items-stretch md:items-center justify-between gap-3 shadow-xs">
        <div class="flex items-center gap-2 text-xs">
          <i data-lucide="image" class="w-4 h-4 text-emerald-400"></i>
          <span class="font-semibold text-white">All Source Images</span>
          <span class="text-zinc-500 font-mono text-[11px]" id="imagesHeaderSubtext">(Flat view across all folders)</span>
        </div>

        <div class="flex items-center gap-2">
          <!-- Select All Images Checkbox -->
          <button onclick="toggleSelectAllImages()" class="px-2.5 py-1.5 rounded-lg border border-zinc-700 bg-zinc-950 hover:bg-zinc-800 text-[11px] text-zinc-300 cursor-pointer">
            Select / Deselect Page
          </button>

          <div class="relative min-w-[240px]">
            <i data-lucide="search" class="w-3.5 h-3.5 text-zinc-400 absolute left-3 top-1/2 -translate-y-1/2"></i>
            <input 
              type="text" 
              id="allImagesSearch" 
              placeholder="Search images by name..." 
              oninput="onImagesSearchInput()" 
              class="w-full bg-zinc-950 border border-zinc-800 rounded-lg pl-8 pr-3 py-1.5 text-xs text-white placeholder-zinc-500 focus:outline-none focus:border-zinc-600"
            >
          </div>
        </div>
      </div>

      <!-- Images Grid Container -->
      <div id="allImagesGrid" class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
        <!-- Rendered dynamically -->
      </div>
      <div id="imagesPaginationContainer"></div>
    </div>

    <!-- ============================================================ -->
    <!-- VIEW 2: ALL VIDEOS (FLAT ACROSS ALL FOLDERS) -->
    <!-- ============================================================ -->
    <div id="viewVideos" class="hidden space-y-4">
      <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-3 flex flex-col md:flex-row items-stretch md:items-center justify-between gap-3 shadow-xs">
        <div class="flex items-center gap-2 text-xs">
          <i data-lucide="film" class="w-4 h-4 text-rose-400"></i>
          <span class="font-semibold text-white">All Source Videos</span>
          <span class="text-zinc-500 font-mono text-[11px]" id="videosHeaderSubtext">(All folders combined)</span>
        </div>

        <div class="flex items-center gap-2">
          <button onclick="toggleSelectAllVideos()" class="px-2.5 py-1.5 rounded-lg border border-zinc-700 bg-zinc-950 hover:bg-zinc-800 text-[11px] text-zinc-300 cursor-pointer">
            Select / Deselect Page
          </button>

          <div class="relative min-w-[240px]">
            <i data-lucide="search" class="w-3.5 h-3.5 text-zinc-400 absolute left-3 top-1/2 -translate-y-1/2"></i>
            <input 
              type="text" 
              id="allVideosSearch" 
              placeholder="Search videos by name..." 
              oninput="onVideosSearchInput()" 
              class="w-full bg-zinc-950 border border-zinc-800 rounded-lg pl-8 pr-3 py-1.5 text-xs text-white placeholder-zinc-500 focus:outline-none focus:border-zinc-600"
            >
          </div>
        </div>
      </div>

      <!-- Videos Grid Container -->
      <div id="allVideosGrid" class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
        <!-- Rendered dynamically -->
      </div>
      <div id="videosPaginationContainer"></div>
    </div>

    <!-- ============================================================ -->
    <!-- VIEW 3: EXPLORER VIEW (HIERARCHY SUBFOLDERS & FILES) -->
    <!-- ============================================================ -->
    <div id="viewExplorer" class="space-y-4">
      
      <!-- Toolbar: Breadcrumbs, Search & Layout Switcher -->
      <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-3 flex flex-col md:flex-row items-stretch md:items-center justify-between gap-3 shadow-xs">
        <nav id="breadcrumbs" class="flex items-center gap-1 text-xs text-zinc-400 overflow-x-auto whitespace-nowrap py-1">
          <!-- Populated dynamically -->
        </nav>

        <div class="flex items-center gap-2">
          <div class="relative min-w-[200px] flex-1 sm:flex-initial">
            <i data-lucide="search" class="w-3.5 h-3.5 text-zinc-400 absolute left-3 top-1/2 -translate-y-1/2"></i>
            <input 
              type="text" 
              id="explorerSearch" 
              placeholder="Search current folder..." 
              oninput="onExplorerSearchInput()" 
              class="w-full bg-zinc-950 border border-zinc-800 rounded-lg pl-8 pr-3 py-1.5 text-xs text-white placeholder-zinc-500 focus:outline-none focus:border-zinc-600"
            >
          </div>

          <div class="flex items-center bg-zinc-950 p-1 rounded-lg border border-zinc-800">
            <button onclick="setExplorerLayout('cards')" id="layoutCardsBtn" class="p-1.5 rounded-md text-white bg-zinc-800 cursor-pointer" title="Cards View">
              <i data-lucide="layout-grid" class="w-3.5 h-3.5"></i>
            </button>
            <button onclick="setExplorerLayout('table')" id="layoutTableBtn" class="p-1.5 rounded-md text-zinc-400 hover:text-white cursor-pointer" title="Table View">
              <i data-lucide="list" class="w-3.5 h-3.5"></i>
            </button>
          </div>
        </div>
      </div>

      <div id="explorerContainer">
        <!-- Dynamic folders and files -->
      </div>
      <div id="explorerPaginationContainer"></div>

    </div>

    <!-- ============================================================ -->
    <!-- VIEW 4: DESTINATION THUMBNAILS GALLERY -->
    <!-- ============================================================ -->
    <div id="viewThumbnails" class="hidden space-y-4">
      <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-3 flex flex-col md:flex-row items-stretch md:items-center justify-between gap-3 shadow-xs">
        <div class="flex items-center gap-2 text-xs">
          <i data-lucide="sparkles" class="w-4 h-4 text-amber-400"></i>
          <span class="font-semibold text-white">Destination R2 Thumbnails</span>
          <span class="text-zinc-500 font-mono text-[11px]" id="thumbsHeaderSubtext">(new_thumbnails/ folder)</span>
        </div>

        <div class="flex items-center gap-2">
          <div class="relative min-w-[240px]">
            <i data-lucide="search" class="w-3.5 h-3.5 text-zinc-400 absolute left-3 top-1/2 -translate-y-1/2"></i>
            <input 
              type="text" 
              id="thumbsSearch" 
              placeholder="Search thumbnails..." 
              oninput="onThumbsSearchInput()" 
              class="w-full bg-zinc-950 border border-zinc-800 rounded-lg pl-8 pr-3 py-1.5 text-xs text-white placeholder-zinc-500 focus:outline-none focus:border-zinc-600"
            >
          </div>
        </div>
      </div>

      <div id="thumbnailsGrid" class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
        <!-- Rendered dynamically -->
      </div>
      <div id="thumbnailsPaginationContainer"></div>
    </div>

  </main>

  <!-- ============================================================ -->
  <!-- VIDEO PLAYER MODAL -->
  <!-- ============================================================ -->
  <div id="videoModal" class="hidden fixed inset-0 z-50 overflow-hidden flex items-center justify-center p-4 bg-black/85 backdrop-blur-md">
    <div class="bg-zinc-900 border border-zinc-800 rounded-2xl max-w-4xl w-full p-4 shadow-2xl flex flex-col space-y-3">
      <div class="flex items-center justify-between pb-2 border-b border-zinc-800">
        <div class="flex items-center gap-2 truncate">
          <div class="p-1.5 rounded-lg bg-blue-950/80 border border-blue-800/60 text-blue-400">
            <i data-lucide="play" class="w-4 h-4 fill-blue-400"></i>
          </div>
          <div>
            <h3 id="videoModalTitle" class="text-xs font-semibold text-white font-mono truncate max-w-lg">Video Preview</h3>
            <p id="videoModalSubtitle" class="text-[10px] text-zinc-400 font-mono">Direct Streaming</p>
          </div>
        </div>
        <button onclick="closeVideoPlayer()" class="text-zinc-400 hover:text-white p-1 rounded-lg hover:bg-zinc-800 transition-colors">
          <i data-lucide="x" class="w-5 h-5"></i>
        </button>
      </div>

      <div class="relative bg-black rounded-xl overflow-hidden aspect-video flex items-center justify-center border border-zinc-800">
        <video id="mainVideoPlayer" controls autoplay playsinline class="w-full h-full object-contain"></video>
      </div>

      <div class="flex items-center justify-between pt-2">
        <div class="text-[11px] font-mono text-zinc-400 truncate max-w-md" id="videoModalKeyText"></div>
        <div class="flex items-center gap-2">
          <button id="videoModalProcessBtn" onclick="processCurrentPlayingVideo()" class="px-3.5 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold flex items-center gap-1.5 shadow transition-all cursor-pointer">
            <i data-lucide="download-cloud" class="w-3.5 h-3.5"></i>
            <span>Process Video</span>
          </button>
          <button onclick="closeVideoPlayer()" class="px-3.5 py-1.5 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-xs font-medium text-zinc-300 transition-colors cursor-pointer">
            Close
          </button>
        </div>
      </div>
    </div>
  </div>

  <!-- ============================================================ -->
  <!-- IMAGE LIGHTBOX MODAL -->
  <!-- ============================================================ -->
  <div id="imageModal" class="hidden fixed inset-0 z-50 overflow-hidden flex items-center justify-center p-4 bg-black/85 backdrop-blur-md" onclick="closeImageModal()">
    <div class="bg-zinc-900 border border-zinc-800 rounded-2xl max-w-4xl w-full p-4 shadow-2xl flex flex-col space-y-3" onclick="event.stopPropagation()">
      <div class="flex items-center justify-between pb-2 border-b border-zinc-800">
        <div class="flex items-center gap-2 truncate">
          <i data-lucide="image" class="w-4 h-4 text-emerald-400"></i>
          <h3 id="imageModalTitle" class="text-xs font-semibold text-white font-mono truncate">Image Preview</h3>
        </div>
        <button onclick="closeImageModal()" class="text-zinc-400 hover:text-white p-1 rounded-lg hover:bg-zinc-800 transition-colors">
          <i data-lucide="x" class="w-5 h-5"></i>
        </button>
      </div>
      <div class="flex items-center justify-center bg-black rounded-xl overflow-hidden p-2 border border-zinc-800 max-h-[75vh]">
        <img id="imageModalEl" src="" alt="Preview" class="max-h-[70vh] max-w-full object-contain rounded-lg">
      </div>
    </div>
  </div>

  <!-- LIVE TASK STATUS MODAL -->
  <div id="taskModal" class="hidden fixed inset-0 z-50 overflow-hidden flex items-center justify-center p-4 bg-black/75 backdrop-blur-xs">
    <div class="bg-zinc-900 border border-zinc-800 rounded-2xl max-w-2xl w-full p-5 shadow-2xl flex flex-col max-h-[85vh]">
      <div class="flex items-center justify-between pb-3 border-b border-zinc-800">
        <div class="flex items-center gap-2">
          <i data-lucide="cpu" class="w-4 h-4 text-emerald-400"></i>
          <h3 class="text-sm font-semibold text-white">Video Processing Console</h3>
        </div>
        <button onclick="closeTaskModal()" class="text-zinc-400 hover:text-white p-1"><i data-lucide="x" class="w-4 h-4"></i></button>
      </div>
      <div class="py-4 space-y-4 flex-1 overflow-y-auto">
        <div class="p-3.5 rounded-xl border border-zinc-800 bg-zinc-950 flex items-center justify-between">
          <div>
            <div id="taskStageText" class="text-xs font-semibold text-white">Idle</div>
            <div id="taskKeyText" class="text-[11px] text-zinc-400 font-mono truncate max-w-md mt-0.5">No active task</div>
          </div>
          <div id="taskProgressBadge" class="text-xs font-mono font-semibold px-2 py-1 rounded bg-zinc-800 text-zinc-300">0 / 0</div>
        </div>
        <div>
          <span class="text-[10px] uppercase font-semibold text-zinc-500 tracking-wider">Live Processing Console</span>
          <div id="terminalLogs" class="mt-1.5 p-3 rounded-xl bg-black border border-zinc-800 text-[11px] font-mono text-zinc-300 h-64 overflow-y-auto whitespace-pre-wrap leading-relaxed">Ready.</div>
        </div>
      </div>
      <div class="pt-3 border-t border-zinc-800 flex justify-end">
        <button onclick="closeTaskModal()" class="px-4 py-2 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-xs font-medium text-white transition-colors cursor-pointer">Close</button>
      </div>
    </div>
  </div>

  <!-- DELETE CONFIRMATION MODAL -->
  <div id="deleteModal" class="hidden fixed inset-0 z-50 overflow-hidden flex items-center justify-center p-4 bg-black/75 backdrop-blur-xs">
    <div class="bg-zinc-900 border border-zinc-800 rounded-2xl max-w-md w-full p-5 shadow-2xl space-y-4">
      <div class="flex items-center gap-3 text-red-400">
        <div class="p-2 rounded-lg bg-red-950/60 border border-red-800/80"><i data-lucide="alert-triangle" class="w-5 h-5 text-red-400"></i></div>
        <div>
          <h3 class="text-sm font-semibold text-white">Confirm Permanent Deletion</h3>
          <p class="text-xs text-zinc-400 mt-0.5">Items will be deleted from R2 and SQLite.</p>
        </div>
      </div>
      <div class="p-3 rounded-xl bg-zinc-950 border border-zinc-800 text-xs text-zinc-300 max-h-40 overflow-y-auto space-y-1 font-mono">
        <div id="deleteSummaryText">Selected items will be deleted permanently.</div>
      </div>
      <div class="flex items-center justify-end gap-2 pt-2">
        <button onclick="closeDeleteModal()" class="px-3.5 py-1.5 rounded-lg border border-zinc-700 text-xs font-medium text-zinc-300 hover:bg-zinc-800 cursor-pointer">Cancel</button>
        <button onclick="executeDelete()" class="px-3.5 py-1.5 rounded-lg bg-red-600 hover:bg-red-500 text-xs font-semibold text-white shadow cursor-pointer">Delete Permanently</button>
      </div>
    </div>
  </div>

  <!-- TOAST -->
  <div id="copyToast" class="fixed bottom-6 right-6 z-50 bg-emerald-600 text-white text-xs font-mono px-4 py-2 rounded-xl shadow-2xl opacity-0 translate-y-3 pointer-events-none flex items-center gap-2">
    <i data-lucide="check" class="w-4 h-4"></i>
    <span id="copyToastText">Notification</span>
  </div>

  <!-- JAVASCRIPT LOGIC -->
  <script>
    let activeTab = "explorer"; // 'explorer', 'images', 'videos', 'thumbnails'
    let explorerLayout = "cards"; // 'cards' or 'table'
    let currentPrefix = "";

    // Data stores from SQLite
    let explorerData = { folders: [], files: [] };
    let allImagesData = [];
    let allVideosData = [];
    let thumbnailsData = [];

    // Pagination states
    const imagesPaging = { page: 1, limit: 36, total: 0, totalPages: 1 };
    const videosPaging = { page: 1, limit: 24, total: 0, totalPages: 1 };
    const thumbsPaging = { page: 1, limit: 36, total: 0, totalPages: 1 };
    const explorerPaging = { page: 1, limit: 36, total: 0, totalPages: 1 };

    function renderPaginationHTML({ targetView, page, totalPages, totalItems, limit, onPageFn, onLimitFn }) {
      if (totalItems <= 0) return "";
      const startItem = (page - 1) * limit + 1;
      const endItem = Math.min(page * limit, totalItems);

      return `
        <div class="flex flex-col sm:flex-row items-center justify-between gap-3 pt-4 border-t border-zinc-800 text-xs text-zinc-400">
          <div class="flex flex-wrap items-center gap-3 font-mono">
            <span>Showing <strong class="text-white">${startItem}</strong> - <strong class="text-white">${endItem}</strong> of <strong class="text-white">${totalItems.toLocaleString()}</strong> items</span>
            <span class="text-zinc-600 hidden sm:inline">|</span>
            <label class="flex items-center gap-1.5">
              <span>Per page:</span>
              <select onchange="${onLimitFn}(Number(this.value))" class="bg-zinc-950 border border-zinc-800 rounded px-2 py-1 text-white text-xs focus:outline-none focus:border-zinc-700 cursor-pointer">
                <option value="24" ${limit === 24 ? 'selected' : ''}>24</option>
                <option value="36" ${limit === 36 ? 'selected' : ''}>36</option>
                <option value="48" ${limit === 48 ? 'selected' : ''}>48</option>
                <option value="96" ${limit === 96 ? 'selected' : ''}>96</option>
              </select>
            </label>
          </div>

          <div class="flex items-center gap-1.5 font-mono">
            <button 
              onclick="${onPageFn}(1)" 
              ${page <= 1 ? 'disabled class="px-2.5 py-1 rounded bg-zinc-900 border border-zinc-800/60 text-zinc-600 cursor-not-allowed"' : 'class="px-2.5 py-1 rounded bg-zinc-900 border border-zinc-800 text-zinc-300 hover:text-white hover:bg-zinc-800 cursor-pointer transition-colors"'} 
              title="First Page"
            >
              &laquo; First
            </button>
            <button 
              onclick="${onPageFn}(${page - 1})" 
              ${page <= 1 ? 'disabled class="px-2.5 py-1 rounded bg-zinc-900 border border-zinc-800/60 text-zinc-600 cursor-not-allowed"' : 'class="px-2.5 py-1 rounded bg-zinc-900 border border-zinc-800 text-zinc-300 hover:text-white hover:bg-zinc-800 cursor-pointer transition-colors"'} 
              title="Previous Page"
            >
              &lsaquo; Prev
            </button>

            <span class="px-3 py-1 rounded bg-zinc-950 border border-zinc-800 text-zinc-400">
              Page <strong class="text-white">${page}</strong> of <strong class="text-white">${totalPages}</strong>
            </span>

            <button 
              onclick="${onPageFn}(${page + 1})" 
              ${page >= totalPages ? 'disabled class="px-2.5 py-1 rounded bg-zinc-900 border border-zinc-800/60 text-zinc-600 cursor-not-allowed"' : 'class="px-2.5 py-1 rounded bg-zinc-900 border border-zinc-800 text-zinc-300 hover:text-white hover:bg-zinc-800 cursor-pointer transition-colors"'} 
              title="Next Page"
            >
              Next &rsaquo;
            </button>
            <button 
              onclick="${onPageFn}(${totalPages})" 
              ${page >= totalPages ? 'disabled class="px-2.5 py-1 rounded bg-zinc-900 border border-zinc-800/60 text-zinc-600 cursor-not-allowed"' : 'class="px-2.5 py-1 rounded bg-zinc-900 border border-zinc-800 text-zinc-300 hover:text-white hover:bg-zinc-800 cursor-pointer transition-colors"'} 
              title="Last Page"
            >
              Last &raquo;
            </button>
          </div>
        </div>
      `;
    }

    // Selected items (Set of keys or folder prefixes)
    let selectedFiles = new Set();
    let selectedFolders = new Set();
    let currentlyPlayingKey = "";

    function formatBytes(bytes) {
      if (!bytes || bytes === 0) return "0 B";
      const k = 1024;
      const sizes = ["B", "KB", "MB", "GB", "TB"];
      const i = Math.floor(Math.log(bytes) / Math.log(k));
      return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + " " + sizes[i];
    }

    // ------------------------------------------------------------
    // TAB NAVIGATION
    // ------------------------------------------------------------
    function switchTab(tab) {
      activeTab = tab;
      selectedFiles.clear();
      selectedFolders.clear();
      updateSelectionUI();

      const tabs = ["explorer", "images", "videos", "thumbnails"];
      tabs.forEach(t => {
        const viewEl = document.getElementById(`view${t.charAt(0).toUpperCase() + t.slice(1)}`);
        const btnEl = document.getElementById(`tabBtn${t.charAt(0).toUpperCase() + t.slice(1)}`);
        if (t === tab) {
          viewEl?.classList.remove("hidden");
          btnEl.className = "px-3 py-1.5 rounded-lg font-medium transition-all flex items-center gap-1.5 bg-zinc-800 text-white shadow-xs cursor-pointer";
        } else {
          viewEl?.classList.add("hidden");
          btnEl.className = "px-3 py-1.5 rounded-lg font-medium transition-all flex items-center gap-1.5 text-zinc-400 hover:text-white cursor-pointer";
        }
      });

      if (tab === "explorer") loadExplorer(currentPrefix);
      else if (tab === "images") loadAllImages();
      else if (tab === "videos") loadAllVideos();
      else if (tab === "thumbnails") loadThumbnails();

      lucide.createIcons();
    }

    // ------------------------------------------------------------
    // STATS & BACKGROUND RESCAN
    // ------------------------------------------------------------
    async function loadStats() {
      try {
        const res = await fetch("/api/stats");
        const json = await res.json();
        if (json.success && json.stats) {
          const s = json.stats;
          document.getElementById("statVideosCount").innerText = s.videos;
          document.getElementById("statImagesCount").innerText = s.images;
          document.getElementById("statThumbsCount").innerText = s.thumbnails;
          document.getElementById("tabVideosCountBadge").innerText = s.videos;
          document.getElementById("tabImagesCountBadge").innerText = s.images;
          document.getElementById("tabThumbsCountBadge").innerText = s.thumbnails;

          const spinIcon = document.getElementById("syncSpinIcon");
          const syncText = document.getElementById("syncBtnText");
          if (s.is_syncing) {
            spinIcon.classList.add("animate-spin");
            syncText.innerText = "Syncing R2...";
          } else {
            spinIcon.classList.remove("animate-spin");
            syncText.innerText = "Rescan R2";
          }
        }
      } catch (e) {}
    }

    async function triggerRescan() {
      try {
        const btn = document.getElementById("btnRescan");
        document.getElementById("syncSpinIcon").classList.add("animate-spin");
        document.getElementById("syncBtnText").innerText = "Scanning...";
        const res = await fetch("/api/sync", { method: "POST" });
        const json = await res.json();
        showToast("R2 Scan started in background!");
        
        let pollCount = 0;
        const interval = setInterval(async () => {
          await loadStats();
          pollCount++;
          if (pollCount > 20) clearInterval(interval);
        }, 1500);
      } catch (e) {
        alert("Rescan error: " + e);
      }
    }

    // ------------------------------------------------------------
    // 1. ALL IMAGES (FLAT ACROSS ALL FOLDERS - PAGINATED)
    // ------------------------------------------------------------
    let imagesSearchTimeout = null;
    function onImagesSearchInput() {
      clearTimeout(imagesSearchTimeout);
      imagesSearchTimeout = setTimeout(() => {
        imagesPaging.page = 1;
        loadAllImages();
      }, 300);
    }

    function filterAllImages() {
      onImagesSearchInput();
    }

    function setImagesPage(p) {
      if (p < 1 || p > imagesPaging.totalPages || p === imagesPaging.page) return;
      imagesPaging.page = p;
      loadAllImages();
      document.getElementById("viewImages")?.scrollIntoView({ behavior: 'smooth' });
    }

    function setImagesLimit(l) {
      imagesPaging.limit = l;
      imagesPaging.page = 1;
      loadAllImages();
    }

    async function loadAllImages() {
      const container = document.getElementById("allImagesGrid");
      const pagingContainer = document.getElementById("imagesPaginationContainer");
      const search = (document.getElementById("allImagesSearch")?.value || "").trim();
      container.innerHTML = `<div class="col-span-full p-12 text-center text-zinc-500 text-xs">Loading images from SQLite...</div>`;

      try {
        const res = await fetch(`/api/images?page=${imagesPaging.page}&limit=${imagesPaging.limit}&search=${encodeURIComponent(search)}`);
        const json = await res.json();
        if (json.success) {
          allImagesData = json.images || [];
          imagesPaging.total = json.total || 0;
          imagesPaging.totalPages = json.total_pages || 1;
          imagesPaging.page = json.page || 1;

          const subtext = document.getElementById("imagesHeaderSubtext");
          if (subtext) {
            subtext.innerText = `(${imagesPaging.total.toLocaleString()} images • Page ${imagesPaging.page} of ${imagesPaging.totalPages})`;
          }

          renderAllImagesView();
          if (pagingContainer) {
            pagingContainer.innerHTML = renderPaginationHTML({
              targetView: 'images',
              page: imagesPaging.page,
              totalPages: imagesPaging.totalPages,
              totalItems: imagesPaging.total,
              limit: imagesPaging.limit,
              onPageFn: 'setImagesPage',
              onLimitFn: 'setImagesLimit'
            });
          }
        } else {
          container.innerHTML = `<div class="col-span-full p-8 text-center text-red-400 text-xs">${json.error}</div>`;
        }
      } catch (e) {
        container.innerHTML = `<div class="col-span-full p-8 text-center text-red-400 text-xs">Error: ${e}</div>`;
      }
    }

    function renderAllImagesView() {
      const container = document.getElementById("allImagesGrid");
      let filtered = allImagesData;

      if (filtered.length === 0) {
        container.innerHTML = `
          <div class="col-span-full p-14 text-center text-zinc-500 text-xs bg-zinc-900 border border-zinc-800 rounded-xl space-y-2">
            <i data-lucide="image" class="w-8 h-8 mx-auto text-zinc-600"></i>
            <div>No images found. Try a different search or click 'Rescan R2' to index items!</div>
          </div>
        `;
        lucide.createIcons();
        return;
      }

      let html = "";
      filtered.forEach(img => {
        const isSelected = selectedFiles.has(img.key);
        const safeKey = img.key.replace(/'/g, "\\\\'");
        const safeName = img.name.replace(/'/g, "\\\\'");
        const directUrl = img.direct_url;
        const streamUrl = img.stream_url;

        html += `
          <div class="bg-zinc-900 border ${isSelected ? 'border-emerald-500 bg-emerald-950/20' : 'border-zinc-800'} rounded-xl overflow-hidden flex flex-col justify-between hover:border-zinc-700 transition-all shadow-xs group">
            
            <div class="relative w-full aspect-video bg-black overflow-hidden border-b border-zinc-800/80">
              <img 
                src="${directUrl}" 
                loading="lazy" 
                alt="${img.name}" 
                onclick="openImageModal('${safeKey}', '${safeName}', 'source', '${directUrl}')"
                onerror="if(!this.dataset.retry){this.dataset.retry='1';this.src='${streamUrl}';}"
                class="w-full h-full object-cover cursor-pointer hover:scale-105 transition-transform"
              >

              <div class="absolute top-2 left-2 z-10 bg-zinc-950/70 p-1 rounded backdrop-blur">
                <input 
                  type="checkbox" 
                  ${isSelected ? 'checked' : ''} 
                  onchange="toggleSelectFile('${img.key}', this.checked)" 
                  class="rounded bg-zinc-800 border-zinc-700 text-emerald-500 focus:ring-0 cursor-pointer block"
                >
              </div>

              ${img.folder ? `
                <span class="absolute bottom-2 left-2 text-[9px] font-mono px-1.5 py-0.5 rounded bg-black/80 text-zinc-400 border border-zinc-800 truncate max-w-[85%] backdrop-blur" title="${img.folder}">
                  📁 ${img.folder}
                </span>
              ` : `
                <span class="absolute bottom-2 left-2 text-[9px] font-mono px-1.5 py-0.5 rounded bg-black/80 text-zinc-500 border border-zinc-800 backdrop-blur">Root</span>
              `}
            </div>

            <div class="p-3 space-y-1">
              <h4 class="text-xs font-semibold text-white font-mono truncate" title="${img.name}">${img.name}</h4>
              <div class="flex items-center justify-between text-[10px] font-mono text-zinc-400">
                <span>${formatBytes(img.size)}</span>
                <span>${img.last_modified}</span>
              </div>
            </div>

            <div class="px-3 pb-3 pt-1 border-t border-zinc-800/80 flex items-center justify-between gap-1.5">
              <button 
                onclick="openImageModal('${safeKey}', '${safeName}', 'source', '${directUrl}')" 
                class="flex-1 py-1 px-2 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-zinc-300 hover:text-white text-[11px] font-medium flex items-center justify-center gap-1 transition-all cursor-pointer"
              >
                <i data-lucide="maximize-2" class="w-3 h-3"></i>
                <span>View</span>
              </button>

              <button 
                onclick="triggerDeleteSingle('${safeKey}')" 
                class="p-1 rounded-lg text-zinc-500 hover:text-red-400 hover:bg-red-950/50 transition-colors cursor-pointer" 
                title="Delete from R2"
              >
                <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
              </button>
            </div>

          </div>
        `;
      });

      container.innerHTML = html;
      lucide.createIcons();
    }

    function toggleSelectAllImages() {
      const allSelected = allImagesData.length > 0 && allImagesData.every(img => selectedFiles.has(img.key));
      if (allSelected) {
        allImagesData.forEach(img => selectedFiles.delete(img.key));
      } else {
        allImagesData.forEach(img => selectedFiles.add(img.key));
      }
      renderAllImagesView();
      updateSelectionUI();
    }

    // ------------------------------------------------------------
    // 2. ALL VIDEOS (FLAT ACROSS ALL FOLDERS - PAGINATED)
    // ------------------------------------------------------------
    let videosSearchTimeout = null;
    function onVideosSearchInput() {
      clearTimeout(videosSearchTimeout);
      videosSearchTimeout = setTimeout(() => {
        videosPaging.page = 1;
        loadAllVideos();
      }, 300);
    }

    function filterAllVideos() {
      onVideosSearchInput();
    }

    function setVideosPage(p) {
      if (p < 1 || p > videosPaging.totalPages || p === videosPaging.page) return;
      videosPaging.page = p;
      loadAllVideos();
      document.getElementById("viewVideos")?.scrollIntoView({ behavior: 'smooth' });
    }

    function setVideosLimit(l) {
      videosPaging.limit = l;
      videosPaging.page = 1;
      loadAllVideos();
    }

    async function loadAllVideos() {
      const container = document.getElementById("allVideosGrid");
      const pagingContainer = document.getElementById("videosPaginationContainer");
      const search = (document.getElementById("allVideosSearch")?.value || "").trim();
      container.innerHTML = `<div class="col-span-full p-12 text-center text-zinc-500 text-xs">Loading videos from SQLite...</div>`;

      try {
        const res = await fetch(`/api/videos?page=${videosPaging.page}&limit=${videosPaging.limit}&search=${encodeURIComponent(search)}`);
        const json = await res.json();
        if (json.success) {
          allVideosData = json.videos || [];
          videosPaging.total = json.total || 0;
          videosPaging.totalPages = json.total_pages || 1;
          videosPaging.page = json.page || 1;

          const subtext = document.getElementById("videosHeaderSubtext");
          if (subtext) {
            subtext.innerText = `(${videosPaging.total.toLocaleString()} videos • Page ${videosPaging.page} of ${videosPaging.totalPages})`;
          }

          renderAllVideosView();
          if (pagingContainer) {
            pagingContainer.innerHTML = renderPaginationHTML({
              targetView: 'videos',
              page: videosPaging.page,
              totalPages: videosPaging.totalPages,
              totalItems: videosPaging.total,
              limit: videosPaging.limit,
              onPageFn: 'setVideosPage',
              onLimitFn: 'setVideosLimit'
            });
          }
        } else {
          container.innerHTML = `<div class="col-span-full p-8 text-center text-red-400 text-xs">${json.error}</div>`;
        }
      } catch (e) {
        container.innerHTML = `<div class="col-span-full p-8 text-center text-red-400 text-xs">Error: ${e}</div>`;
      }
    }

    function renderAllVideosView() {
      const container = document.getElementById("allVideosGrid");
      let filtered = allVideosData;

      if (filtered.length === 0) {
        container.innerHTML = `
          <div class="col-span-full p-14 text-center text-zinc-500 text-xs bg-zinc-900 border border-zinc-800 rounded-xl space-y-2">
            <i data-lucide="film" class="w-8 h-8 mx-auto text-zinc-600"></i>
            <div>No videos found. Try a different search!</div>
          </div>
        `;
        lucide.createIcons();
        return;
      }

      let html = "";
      filtered.forEach(vid => {
        const isSelected = selectedFiles.has(vid.key);
        const safeKey = vid.key.replace(/'/g, "\\\\'");
        const safeName = vid.name.replace(/'/g, "\\\\'");
        const directUrl = vid.direct_url;
        const streamUrl = vid.stream_url;

        html += `
          <div class="bg-zinc-900 border ${isSelected ? 'border-rose-500 bg-rose-950/20' : 'border-zinc-800'} rounded-xl overflow-hidden flex flex-col justify-between hover:border-zinc-700 transition-all shadow-xs group">
            
            <div class="relative w-full aspect-video bg-black overflow-hidden border-b border-zinc-800/80">
              <video 
                src="${directUrl}#t=0.5" 
                preload="metadata" 
                muted 
                playsinline 
                onloadedmetadata="try{if(this.currentTime===0)this.currentTime=0.5;}catch(e){}"
                onerror="if(!this.dataset.retry){this.dataset.retry='1';this.src='${streamUrl}#t=0.5';}"
                class="w-full h-full object-cover"
              ></video>

              <div 
                onclick="openVideoPlayer('${safeKey}', '${safeName}', '${directUrl}')" 
                class="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 flex items-center justify-center transition-opacity cursor-pointer"
                title="Play video"
              >
                <div class="w-10 h-10 rounded-full bg-blue-600/90 text-white flex items-center justify-center shadow-lg hover:scale-110 transition-transform">
                  <i data-lucide="play" class="w-4 h-4 fill-white ml-0.5"></i>
                </div>
              </div>

              <div class="absolute top-2 left-2 z-10 bg-zinc-950/70 p-1 rounded backdrop-blur">
                <input 
                  type="checkbox" 
                  ${isSelected ? 'checked' : ''} 
                  onchange="toggleSelectFile('${vid.key}', this.checked)" 
                  class="rounded bg-zinc-800 border-zinc-700 text-rose-500 focus:ring-0 cursor-pointer block"
                >
              </div>

              <span class="absolute top-2 right-2 text-[9.5px] font-mono font-semibold px-1.5 py-0.5 rounded bg-rose-950/90 text-rose-300 border border-rose-800/60 shadow">
                ${vid.ext.toUpperCase()}
              </span>

              ${vid.folder ? `
                <span class="absolute bottom-2 left-2 text-[9px] font-mono px-1.5 py-0.5 rounded bg-black/80 text-zinc-400 border border-zinc-800 truncate max-w-[85%] backdrop-blur">
                  📁 ${vid.folder}
                </span>
              ` : `
                <span class="absolute bottom-2 left-2 text-[9px] font-mono px-1.5 py-0.5 rounded bg-black/80 text-zinc-500 border border-zinc-800 backdrop-blur">Root</span>
              `}
            </div>

            <div class="p-3 space-y-1">
              <h4 class="text-xs font-semibold text-white font-mono truncate" title="${vid.name}">${vid.name}</h4>
              <div class="flex items-center justify-between text-[10px] font-mono text-zinc-400">
                <span>${formatBytes(vid.size)}</span>
                <span>${vid.last_modified}</span>
              </div>
            </div>

            <div class="px-3 pb-3 pt-1 border-t border-zinc-800/80 flex items-center justify-between gap-1.5">
              <button 
                onclick="openVideoPlayer('${safeKey}', '${safeName}', '${directUrl}')" 
                class="flex-1 py-1 px-2 rounded-lg bg-blue-950/60 hover:bg-blue-600 text-blue-300 hover:text-white border border-blue-800/60 text-[11px] font-semibold flex items-center justify-center gap-1 transition-all cursor-pointer"
              >
                <i data-lucide="play" class="w-3 h-3 fill-current"></i>
                <span>Play</span>
              </button>

              <button 
                onclick="triggerProcessSingle('${safeKey}')" 
                class="flex-1 py-1 px-2 rounded-lg bg-emerald-950/60 hover:bg-emerald-600 text-emerald-300 hover:text-white border border-emerald-800/60 text-[11px] font-semibold flex items-center justify-center gap-1 transition-all cursor-pointer"
              >
                <i data-lucide="download-cloud" class="w-3 h-3"></i>
                <span>Process</span>
              </button>

              <button 
                onclick="triggerDeleteSingle('${safeKey}')" 
                class="p-1 rounded-lg text-zinc-500 hover:text-red-400 hover:bg-red-950/50 transition-colors cursor-pointer" 
                title="Delete from R2"
              >
                <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
              </button>
            </div>

          </div>
        `;
      });

      container.innerHTML = html;
      lucide.createIcons();
    }

    function toggleSelectAllVideos() {
      const allSelected = allVideosData.length > 0 && allVideosData.every(v => selectedFiles.has(v.key));
      if (allSelected) {
        allVideosData.forEach(v => selectedFiles.delete(v.key));
      } else {
        allVideosData.forEach(v => selectedFiles.add(v.key));
      }
      renderAllVideosView();
      updateSelectionUI();
    }

    // ------------------------------------------------------------
    // 3. EXPLORER VIEW (HIERARCHY SUBFOLDERS & FILES - PAGINATED)
    // ------------------------------------------------------------
    let explorerSearchTimeout = null;
    function onExplorerSearchInput() {
      clearTimeout(explorerSearchTimeout);
      explorerSearchTimeout = setTimeout(() => {
        explorerPaging.page = 1;
        loadExplorer(currentPrefix);
      }, 300);
    }

    function setExplorerPage(p) {
      if (p < 1 || p > explorerPaging.totalPages || p === explorerPaging.page) return;
      explorerPaging.page = p;
      loadExplorer(currentPrefix);
      document.getElementById("viewExplorer")?.scrollIntoView({ behavior: 'smooth' });
    }

    function setExplorerLimit(l) {
      explorerPaging.limit = l;
      explorerPaging.page = 1;
      loadExplorer(currentPrefix);
    }

    async function loadExplorer(prefix = "") {
      if (prefix !== currentPrefix) {
        explorerPaging.page = 1;
      }
      currentPrefix = prefix;
      selectedFiles.clear();
      selectedFolders.clear();
      updateSelectionUI();

      const search = (document.getElementById("explorerSearch")?.value || "").trim();
      const pagingContainer = document.getElementById("explorerPaginationContainer");

      try {
        const res = await fetch(`/api/files?prefix=${encodeURIComponent(prefix)}&page=${explorerPaging.page}&limit=${explorerPaging.limit}&search=${encodeURIComponent(search)}`);
        const json = await res.json();
        if (json.success && json.data) {
          explorerData = json.data;
          explorerPaging.total = json.data.total_files || 0;
          explorerPaging.totalPages = json.data.total_pages || 1;
          explorerPaging.page = json.data.page || 1;

          renderBreadcrumbs();
          renderExplorerView();

          if (pagingContainer) {
            pagingContainer.innerHTML = renderPaginationHTML({
              targetView: 'explorer',
              page: explorerPaging.page,
              totalPages: explorerPaging.totalPages,
              totalItems: explorerPaging.total,
              limit: explorerPaging.limit,
              onPageFn: 'setExplorerPage',
              onLimitFn: 'setExplorerLimit'
            });
          }
        }
      } catch (e) {
        console.error("loadExplorer error:", e);
      }
    }

    function renderBreadcrumbs() {
      const container = document.getElementById("breadcrumbs");
      if (!container) return;

      const parts = currentPrefix.split("/").filter(Boolean);
      let html = `
        <button onclick="loadExplorer('')" class="hover:text-white font-medium flex items-center gap-1 transition-colors cursor-pointer">
          <i data-lucide="home" class="w-3.5 h-3.5"></i>
          <span>Root</span>
        </button>
      `;

      let accumulated = "";
      parts.forEach((part, index) => {
        accumulated += part + "/";
        const targetPrefix = accumulated;
        const isLast = index === parts.length - 1;
        html += `<span class="text-zinc-600">/</span>`;
        if (isLast) {
          html += `<span class="text-white font-semibold">${part}</span>`;
        } else {
          html += `<button onclick="loadExplorer('${targetPrefix}')" class="hover:text-white transition-colors cursor-pointer">${part}</button>`;
        }
      });

      container.innerHTML = html;
      lucide.createIcons();
    }

    function setExplorerLayout(layout) {
      explorerLayout = layout;
      const cBtn = document.getElementById("layoutCardsBtn");
      const tBtn = document.getElementById("layoutTableBtn");
      if (layout === "cards") {
        cBtn.className = "p-1.5 rounded-md text-white bg-zinc-800 cursor-pointer";
        tBtn.className = "p-1.5 rounded-md text-zinc-400 hover:text-white cursor-pointer";
      } else {
        cBtn.className = "p-1.5 rounded-md text-zinc-400 hover:text-white cursor-pointer";
        tBtn.className = "p-1.5 rounded-md text-white bg-zinc-800 cursor-pointer";
      }
      renderExplorerView();
    }

    function renderExplorerView() {
      const container = document.getElementById("explorerContainer");
      const search = (document.getElementById("explorerSearch")?.value || "").toLowerCase().trim();

      let folders = explorerData.folders || [];
      let files = explorerData.files || [];

      if (search) {
        folders = folders.filter(f => f.name.toLowerCase().includes(search));
        files = files.filter(f => f.name.toLowerCase().includes(search));
      }

      if (folders.length === 0 && files.length === 0) {
        container.innerHTML = `
          <div class="p-14 text-center text-zinc-500 text-xs bg-zinc-900 border border-zinc-800 rounded-xl space-y-2">
            <i data-lucide="inbox" class="w-8 h-8 mx-auto text-zinc-600"></i>
            <div>No items in this directory.</div>
          </div>
        `;
        lucide.createIcons();
        return;
      }

      if (explorerLayout === "cards") {
        let html = `<div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">`;

        // Folder Cards
        folders.forEach(f => {
          const isSelected = selectedFolders.has(f.prefix);
          html += `
            <div class="bg-zinc-900 border ${isSelected ? 'border-blue-500 bg-blue-950/20' : 'border-zinc-800'} rounded-xl p-3 flex flex-col justify-between hover:border-zinc-700 transition-all shadow-xs group">
              <div class="flex items-start justify-between gap-2">
                <input type="checkbox" ${isSelected ? 'checked' : ''} onchange="toggleSelectFolder('${f.prefix}', this.checked)" class="rounded bg-zinc-800 border-zinc-700 text-blue-500 focus:ring-0 cursor-pointer mt-1">
                <div onclick="loadExplorer('${f.prefix}')" class="flex-1 cursor-pointer">
                  <div class="h-20 rounded-lg bg-zinc-950 border border-zinc-800/80 flex flex-col items-center justify-center text-amber-400 group-hover:scale-102 transition-transform">
                    <i data-lucide="folder" class="w-8 h-8 fill-amber-400/20"></i>
                    <span class="text-[10px] text-zinc-500 mt-1 uppercase font-mono font-medium tracking-wider">Subfolder</span>
                  </div>
                  <div class="mt-2.5">
                    <h4 class="text-xs font-semibold text-white group-hover:underline truncate">${f.name}</h4>
                  </div>
                </div>
              </div>
              <div class="mt-3 pt-2.5 border-t border-zinc-800/80 flex items-center justify-between">
                <span class="text-[10px] font-mono text-zinc-500">Folder</span>
                <button onclick="triggerProcessFolder('${f.prefix}')" class="px-2 py-1 rounded bg-zinc-800 hover:bg-emerald-600 hover:text-white text-[11px] text-zinc-300 font-medium transition-colors cursor-pointer">
                  Process Folder
                </button>
              </div>
            </div>
          `;
        });

        // File Cards
        files.forEach(file => {
          const isSelected = selectedFiles.has(file.key);
          const safeKey = file.key.replace(/'/g, "\\\\'");
          const safeName = file.name.replace(/'/g, "\\\\'");
          const directUrl = file.direct_url;
          const streamUrl = file.stream_url;

          html += `
            <div class="bg-zinc-900 border ${isSelected ? 'border-blue-500 bg-blue-950/20' : 'border-zinc-800'} rounded-xl overflow-hidden flex flex-col justify-between hover:border-zinc-700 transition-all shadow-xs group">
              <div class="relative w-full aspect-video bg-black overflow-hidden border-b border-zinc-800/80">
                ${file.is_video ? `
                  <video 
                    src="${directUrl}#t=0.5" 
                    preload="metadata" 
                    muted 
                    playsinline 
                    onloadedmetadata="try{if(this.currentTime===0)this.currentTime=0.5;}catch(e){}"
                    onerror="if(!this.dataset.retry){this.dataset.retry='1';this.src='${streamUrl}#t=0.5';}"
                    class="w-full h-full object-cover"
                  ></video>
                  <div onclick="openVideoPlayer('${safeKey}', '${safeName}', '${directUrl}')" class="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 flex items-center justify-center transition-opacity cursor-pointer">
                    <div class="w-10 h-10 rounded-full bg-blue-600/90 text-white flex items-center justify-center shadow-lg hover:scale-110 transition-transform">
                      <i data-lucide="play" class="w-4 h-4 fill-white ml-0.5"></i>
                    </div>
                  </div>
                  <span class="absolute top-2 right-2 text-[9.5px] font-mono font-semibold px-1.5 py-0.5 rounded bg-rose-950/90 text-rose-300 border border-rose-800/60 shadow">${file.ext.toUpperCase()}</span>
                ` : file.is_image ? `
                  <img 
                    src="${directUrl}" 
                    loading="lazy" 
                    alt="${file.name}" 
                    onclick="openImageModal('${safeKey}', '${safeName}', 'source', '${directUrl}')"
                    onerror="if(!this.dataset.retry){this.dataset.retry='1';this.src='${streamUrl}';}"
                    class="w-full h-full object-cover cursor-pointer hover:scale-105 transition-transform"
                  >
                  <span class="absolute top-2 right-2 text-[9.5px] font-mono font-semibold px-1.5 py-0.5 rounded bg-blue-950/90 text-blue-300 border border-blue-800/60 shadow">${file.ext.toUpperCase()}</span>
                ` : `
                  <div class="w-full h-full flex flex-col items-center justify-center text-zinc-500">
                    <i data-lucide="file" class="w-8 h-8"></i>
                    <span class="text-[10px] font-mono mt-1">${file.ext.toUpperCase()}</span>
                  </div>
                `}

                <div class="absolute top-2 left-2 z-10 bg-zinc-950/70 p-1 rounded backdrop-blur">
                  <input type="checkbox" ${isSelected ? 'checked' : ''} onchange="toggleSelectFile('${file.key}', this.checked)" class="rounded bg-zinc-800 border-zinc-700 text-blue-500 focus:ring-0 cursor-pointer block">
                </div>
              </div>

              <div class="p-3 space-y-1">
                <h4 class="text-xs font-semibold text-white font-mono truncate" title="${file.name}">${file.name}</h4>
                <div class="flex items-center justify-between text-[10px] font-mono text-zinc-400">
                  <span>${formatBytes(file.size)}</span>
                  <span>${file.last_modified}</span>
                </div>
              </div>

              <div class="px-3 pb-3 pt-1 border-t border-zinc-800/80 flex items-center justify-between gap-1.5">
                ${file.is_video ? `
                  <button onclick="openVideoPlayer('${safeKey}', '${safeName}', '${directUrl}')" class="flex-1 py-1 px-2 rounded-lg bg-blue-950/60 hover:bg-blue-600 text-blue-300 hover:text-white border border-blue-800/60 text-[11px] font-semibold flex items-center justify-center gap-1 transition-all cursor-pointer">
                    <i data-lucide="play" class="w-3 h-3 fill-current"></i>
                    <span>Play</span>
                  </button>
                  <button onclick="triggerProcessSingle('${safeKey}')" class="flex-1 py-1 px-2 rounded-lg bg-emerald-950/60 hover:bg-emerald-600 text-emerald-300 hover:text-white border border-emerald-800/60 text-[11px] font-semibold flex items-center justify-center gap-1 transition-all cursor-pointer">
                    <i data-lucide="download-cloud" class="w-3 h-3"></i>
                    <span>Process</span>
                  </button>
                ` : file.is_image ? `
                  <button onclick="openImageModal('${safeKey}', '${safeName}', 'source', '${directUrl}')" class="flex-1 py-1 px-2 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-zinc-300 hover:text-white text-[11px] font-medium flex items-center justify-center gap-1 transition-all cursor-pointer">
                    <i data-lucide="maximize-2" class="w-3 h-3"></i>
                    <span>View</span>
                  </button>
                ` : ''}

                <button onclick="triggerDeleteSingle('${safeKey}')" class="p-1 rounded-lg text-zinc-500 hover:text-red-400 hover:bg-red-950/50 transition-colors cursor-pointer" title="Delete">
                  <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
                </button>
              </div>
            </div>
          `;
        });

        html += `</div>`;
        container.innerHTML = html;
      } else {
        // Table layout
        let html = `
          <div class="bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden shadow-xs">
            <div class="bg-zinc-950/80 border-b border-zinc-800 px-4 py-3 flex items-center justify-between text-xs font-medium text-zinc-400 uppercase tracking-wider text-[11px]">
              <div class="flex items-center gap-3">
                <input type="checkbox" id="selectAllCheckbox" onchange="toggleSelectAllExplorer(this.checked)" class="rounded bg-zinc-800 border-zinc-700 text-blue-500 focus:ring-0 cursor-pointer">
                <span>Item</span>
              </div>
              <div class="flex items-center gap-8">
                <span class="hidden sm:inline w-20 text-right">Size</span>
                <span class="hidden md:inline w-32 text-right">Last Modified</span>
                <span class="w-36 text-right">Actions</span>
              </div>
            </div>
            <div class="divide-y divide-zinc-800/60 text-xs">
        `;

        folders.forEach(f => {
          const isSelected = selectedFolders.has(f.prefix);
          html += `
            <div class="px-4 py-2.5 flex items-center justify-between hover:bg-zinc-800/40 transition-colors ${isSelected ? 'bg-blue-950/20' : ''}">
              <div class="flex items-center gap-3 min-w-0">
                <input type="checkbox" ${isSelected ? 'checked' : ''} onchange="toggleSelectFolder('${f.prefix}', this.checked)" class="rounded bg-zinc-800 border-zinc-700 text-blue-500 focus:ring-0 cursor-pointer">
                <div onclick="loadExplorer('${f.prefix}')" class="flex items-center gap-3 cursor-pointer group truncate">
                  <div class="w-12 h-8 rounded bg-zinc-950 border border-zinc-800 flex items-center justify-center text-amber-400 shrink-0">
                    <i data-lucide="folder" class="w-4 h-4 fill-amber-400/20"></i>
                  </div>
                  <span class="font-medium text-white group-hover:underline truncate">${f.name}</span>
                  <span class="text-[9.5px] font-mono px-1.5 py-0.2 rounded bg-zinc-800 text-zinc-400 border border-zinc-700">DIR</span>
                </div>
              </div>
              <div class="flex items-center gap-8 font-mono">
                <span class="hidden sm:inline w-20 text-right text-zinc-500">-</span>
                <span class="hidden md:inline w-32 text-right text-zinc-500">-</span>
                <div class="w-36 text-right">
                  <button onclick="triggerProcessFolder('${f.prefix}')" class="px-2 py-1 rounded bg-zinc-800 hover:bg-emerald-600 hover:text-white text-[11px] text-zinc-300 font-medium transition-colors cursor-pointer">
                    Process Folder
                  </button>
                </div>
              </div>
            </div>
          `;
        });

        files.forEach(file => {
          const isSelected = selectedFiles.has(file.key);
          const safeKey = file.key.replace(/'/g, "\\\\'");
          const safeName = file.name.replace(/'/g, "\\\\'");
          const directUrl = file.direct_url;
          const streamUrl = file.stream_url;

          html += `
            <div class="px-4 py-2 flex items-center justify-between hover:bg-zinc-800/40 transition-colors ${isSelected ? 'bg-blue-950/20' : ''}">
              <div class="flex items-center gap-3 min-w-0">
                <input type="checkbox" ${isSelected ? 'checked' : ''} onchange="toggleSelectFile('${file.key}', this.checked)" class="rounded bg-zinc-800 border-zinc-700 text-blue-500 focus:ring-0 cursor-pointer">
                
                <div class="w-12 h-8 rounded bg-black border border-zinc-800 overflow-hidden relative shrink-0">
                  ${file.is_video ? `
                    <video src="${directUrl}#t=0.5" preload="metadata" muted playsinline onloadedmetadata="try{if(this.currentTime===0)this.currentTime=0.5;}catch(e){}" class="w-full h-full object-cover"></video>
                    <div onclick="openVideoPlayer('${safeKey}', '${safeName}', '${directUrl}')" class="absolute inset-0 bg-black/30 hover:bg-black/0 flex items-center justify-center cursor-pointer">
                      <i data-lucide="play" class="w-2.5 h-2.5 fill-white text-white"></i>
                    </div>
                  ` : file.is_image ? `
                    <img src="${directUrl}" loading="lazy" onclick="openImageModal('${safeKey}', '${safeName}', 'source', '${directUrl}')" class="w-full h-full object-cover cursor-pointer" onerror="this.src='${streamUrl}';">
                  ` : `
                    <div class="w-full h-full flex items-center justify-center text-zinc-500"><i data-lucide="file" class="w-3.5 h-3.5"></i></div>
                  `}
                </div>

                <div class="truncate flex items-center gap-2">
                  <span class="text-zinc-200 truncate font-mono text-xs">${file.name}</span>
                  <span class="text-[9px] font-mono px-1 py-0.2 rounded border ${file.is_video ? 'bg-rose-950/40 text-rose-300 border-rose-800/60' : 'bg-blue-950/40 text-blue-300 border-blue-800/60'}">${file.ext.toUpperCase()}</span>
                </div>
              </div>

              <div class="flex items-center gap-8 font-mono">
                <span class="hidden sm:inline w-20 text-right text-zinc-400 text-[11px]">${formatBytes(file.size)}</span>
                <span class="hidden md:inline w-32 text-right text-zinc-500 text-[11px]">${file.last_modified}</span>
                <div class="w-36 text-right flex items-center justify-end gap-1.5">
                  ${file.is_video ? `
                    <button onclick="openVideoPlayer('${safeKey}', '${safeName}', '${directUrl}')" class="px-2 py-1 rounded bg-blue-950/60 hover:bg-blue-600 text-blue-300 hover:text-white border border-blue-800/60 text-[11px] font-semibold flex items-center gap-1 transition-all cursor-pointer">
                      <i data-lucide="play" class="w-2.5 h-2.5 fill-current"></i>
                      <span>Play</span>
                    </button>
                    <button onclick="triggerProcessSingle('${safeKey}')" class="px-2 py-1 rounded bg-emerald-950/60 hover:bg-emerald-600 text-emerald-300 hover:text-white border border-emerald-800/60 text-[11px] font-semibold transition-all cursor-pointer">Process</button>
                  ` : file.is_image ? `
                    <button onclick="openImageModal('${safeKey}', '${safeName}', 'source', '${directUrl}')" class="px-2 py-1 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-300 text-[11px] transition-colors cursor-pointer">View</button>
                  ` : ''}
                  <button onclick="triggerDeleteSingle('${safeKey}')" class="p-1 rounded hover:bg-red-950/60 text-zinc-500 hover:text-red-400 transition-colors cursor-pointer" title="Delete"><i data-lucide="trash-2" class="w-3.5 h-3.5"></i></button>
                </div>
              </div>
            </div>
          `;
        });

        html += `</div></div>`;
        container.innerHTML = html;
      }

      lucide.createIcons();
    }

    function toggleSelectAllExplorer(isChecked) {
      if (isChecked) {
        explorerData.folders.forEach(f => selectedFolders.add(f.prefix));
        explorerData.files.forEach(f => selectedFiles.add(f.key));
      } else {
        selectedFolders.clear();
        selectedFiles.clear();
      }
      renderExplorerView();
      updateSelectionUI();
    }

    // ------------------------------------------------------------
    // 4. DESTINATION THUMBNAILS (PAGINATED)
    // ------------------------------------------------------------
    let thumbsSearchTimeout = null;
    function onThumbsSearchInput() {
      clearTimeout(thumbsSearchTimeout);
      thumbsSearchTimeout = setTimeout(() => {
        thumbsPaging.page = 1;
        loadThumbnails();
      }, 300);
    }

    function filterThumbnails() {
      onThumbsSearchInput();
    }

    function setThumbsPage(p) {
      if (p < 1 || p > thumbsPaging.totalPages || p === thumbsPaging.page) return;
      thumbsPaging.page = p;
      loadThumbnails();
      document.getElementById("viewThumbnails")?.scrollIntoView({ behavior: 'smooth' });
    }

    function setThumbsLimit(l) {
      thumbsPaging.limit = l;
      thumbsPaging.page = 1;
      loadThumbnails();
    }

    async function loadThumbnails() {
      const container = document.getElementById("thumbnailsGrid");
      const pagingContainer = document.getElementById("thumbnailsPaginationContainer");
      const search = (document.getElementById("thumbsSearch")?.value || "").trim();
      container.innerHTML = `<div class="col-span-full p-12 text-center text-zinc-500 text-xs">Loading thumbnails...</div>`;

      try {
        const res = await fetch(`/api/thumbnails?page=${thumbsPaging.page}&limit=${thumbsPaging.limit}&search=${encodeURIComponent(search)}`);
        const json = await res.json();
        if (json.success) {
          thumbnailsData = json.thumbnails || [];
          thumbsPaging.total = json.total || 0;
          thumbsPaging.totalPages = json.total_pages || 1;
          thumbsPaging.page = json.page || 1;

          const subtext = document.getElementById("thumbsHeaderSubtext");
          if (subtext) {
            subtext.innerText = `(${thumbsPaging.total.toLocaleString()} thumbnails • Page ${thumbsPaging.page} of ${thumbsPaging.totalPages})`;
          }

          renderThumbnailsView();
          if (pagingContainer) {
            pagingContainer.innerHTML = renderPaginationHTML({
              targetView: 'thumbnails',
              page: thumbsPaging.page,
              totalPages: thumbsPaging.totalPages,
              totalItems: thumbsPaging.total,
              limit: thumbsPaging.limit,
              onPageFn: 'setThumbsPage',
              onLimitFn: 'setThumbsLimit'
            });
          }
        } else {
          container.innerHTML = `<div class="col-span-full p-8 text-center text-red-400 text-xs">${json.error}</div>`;
        }
      } catch (e) {
        container.innerHTML = `<div class="col-span-full p-8 text-center text-red-400 text-xs">Error: ${e}</div>`;
      }
    }

    function renderThumbnailsView() {
      const container = document.getElementById("thumbnailsGrid");
      let filtered = thumbnailsData;

      if (filtered.length === 0) {
        container.innerHTML = `
          <div class="col-span-full p-14 text-center text-zinc-500 text-xs bg-zinc-900 border border-zinc-800 rounded-xl space-y-2">
            <i data-lucide="sparkles" class="w-8 h-8 mx-auto text-zinc-600"></i>
            <div>No destination thumbnails found. Process videos to generate composite thumbnails!</div>
          </div>
        `;
        lucide.createIcons();
        return;
      }

      let html = "";
      filtered.forEach(thumb => {
        const safeKey = thumb.key.replace(/'/g, "\\\\'");
        const safeName = thumb.name.replace(/'/g, "\\\\'");
        const directUrl = thumb.direct_url;
        const streamUrl = thumb.stream_url;

        html += `
          <div class="bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden flex flex-col justify-between hover:border-amber-500/50 transition-all shadow-xs group">
            <div class="relative w-full aspect-video bg-black overflow-hidden border-b border-zinc-800/80">
              <img 
                src="${directUrl}" 
                loading="lazy" 
                alt="${thumb.name}" 
                onclick="openImageModal('${safeKey}', '${safeName}', 'dest', '${directUrl}')"
                onerror="if(!this.dataset.retry){this.dataset.retry='1';this.src='${streamUrl}';}"
                class="w-full h-full object-cover cursor-pointer group-hover:scale-104 transition-transform"
              >
              <span class="absolute top-2 right-2 text-[9.5px] font-mono px-1.5 py-0.5 rounded bg-black/80 text-amber-300 border border-amber-800/60 backdrop-blur">
                COMPOSITE
              </span>
            </div>

            <div class="p-3 space-y-1">
              <h4 class="text-xs font-semibold text-white font-mono truncate" title="${thumb.name}">${thumb.name}</h4>
              <div class="flex items-center justify-between text-[10px] font-mono text-zinc-400">
                <span>${formatBytes(thumb.size)}</span>
                <span>${thumb.last_modified}</span>
              </div>
            </div>

            <div class="px-3 pb-3 pt-1 border-t border-zinc-800/80 flex items-center justify-between gap-1.5">
              <button 
                onclick="openImageModal('${safeKey}', '${safeName}', 'dest', '${directUrl}')" 
                class="flex-1 py-1 px-2 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-zinc-300 hover:text-white text-[11px] font-medium flex items-center justify-center gap-1 transition-all cursor-pointer"
              >
                <i data-lucide="maximize-2" class="w-3 h-3"></i>
                <span>View</span>
              </button>

              <button 
                onclick="copyPathToClipboard('${thumb.key}')" 
                class="flex-1 py-1 px-2 rounded-lg bg-amber-950/60 hover:bg-amber-600 text-amber-300 hover:text-white border border-amber-800/60 text-[11px] font-semibold flex items-center justify-center gap-1 transition-all cursor-pointer"
                title="Copy relative thumbnail path"
              >
                <i data-lucide="copy" class="w-3 h-3"></i>
                <span>Copy</span>
              </button>

              <button 
                onclick="deleteThumbnail('${safeKey}')" 
                class="p-1 rounded-lg text-zinc-500 hover:text-red-400 hover:bg-red-950/50 transition-colors cursor-pointer" 
                title="Delete from Destination R2"
              >
                <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
              </button>
            </div>
          </div>
        `;
      });

      container.innerHTML = html;
      lucide.createIcons();
    }

    async function deleteThumbnail(key) {
      if (!confirm(`Delete thumbnail: ${key}?`)) return;
      try {
        const res = await fetch("/api/thumbnails/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ key })
        });
        const json = await res.json();
        if (json.success) {
          loadThumbnails();
          loadStats();
        } else {
          alert("Error: " + (json.error || "Failed to delete thumbnail"));
        }
      } catch (e) {
        alert("Failed to delete thumbnail: " + e);
      }
    }

    // ------------------------------------------------------------
    // SELECTION & PROCESS ACTIONS
    // ------------------------------------------------------------
    function toggleSelectFolder(prefix, isChecked) {
      if (isChecked) selectedFolders.add(prefix);
      else selectedFolders.delete(prefix);
      updateSelectionUI();
    }

    function toggleSelectFile(key, isChecked) {
      if (isChecked) selectedFiles.add(key);
      else selectedFiles.delete(key);
      updateSelectionUI();
    }

    function clearSelection() {
      selectedFolders.clear();
      selectedFiles.clear();
      updateSelectionUI();
      if (activeTab === "explorer") renderExplorerView();
      else if (activeTab === "images") renderAllImagesView();
      else if (activeTab === "videos") renderAllVideosView();
    }

    function updateSelectionUI() {
      const header = document.getElementById("selectionHeader");
      const text = document.getElementById("selectedCountText");
      const processBtn = document.getElementById("batchProcessBtn");
      const total = selectedFolders.size + selectedFiles.size;

      if (total > 0) {
        header.classList.remove("hidden");
        text.innerText = `${total} item(s) selected (${selectedFolders.size} folder${selectedFolders.size === 1 ? '' : 's'}, ${selectedFiles.size} file${selectedFiles.size === 1 ? '' : 's'})`;
        
        // Hide Process button if on Images view
        if (activeTab === "images") {
          processBtn?.classList.add("hidden");
        } else {
          processBtn?.classList.remove("hidden");
        }
      } else {
        header.classList.add("hidden");
      }
    }

    async function triggerProcessSelected() {
      if (selectedFolders.size === 0 && selectedFiles.size === 0) return;
      const payload = {
        folders: Array.from(selectedFolders),
        keys: Array.from(selectedFiles)
      };

      try {
        const res = await fetch("/api/process", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const json = await res.json();
        if (json.success) {
          clearSelection();
          openTaskModal();
        } else {
          alert(json.error || "Failed to start processing.");
        }
      } catch (err) {
        alert("Request error: " + err);
      }
    }

    function triggerProcessFolder(folderPrefix) {
      selectedFolders.clear();
      selectedFiles.clear();
      selectedFolders.add(folderPrefix);
      triggerProcessSelected();
    }

    function triggerProcessSingle(fileKey) {
      selectedFolders.clear();
      selectedFiles.clear();
      selectedFiles.add(fileKey);
      triggerProcessSelected();
    }

    // ------------------------------------------------------------
    // DELETE OPERATIONS
    // ------------------------------------------------------------
    function openDeleteModal() {
      const modal = document.getElementById("deleteModal");
      const summary = document.getElementById("deleteSummaryText");
      const total = selectedFolders.size + selectedFiles.size;
      summary.innerText = `Are you sure you want to permanently delete ${total} selected item(s) from Source R2?`;
      modal.classList.remove("hidden");
    }

    function closeDeleteModal() {
      document.getElementById("deleteModal").classList.add("hidden");
    }

    function triggerDeleteSingle(fileKey) {
      selectedFolders.clear();
      selectedFiles.clear();
      selectedFiles.add(fileKey);
      openDeleteModal();
    }

    async function executeDelete() {
      closeDeleteModal();
      const payload = {
        folders: Array.from(selectedFolders),
        keys: Array.from(selectedFiles)
      };

      try {
        const res = await fetch("/api/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const json = await res.json();
        if (json.success) {
          clearSelection();
          if (activeTab === "explorer") loadExplorer(currentPrefix);
          else if (activeTab === "images") loadAllImages();
          else if (activeTab === "videos") loadAllVideos();
          loadStats();
          showToast(`Deleted ${json.deleted_count} item(s)!`);
        } else {
          alert(json.error || "Failed to delete items.");
        }
      } catch (err) {
        alert("Deletion failed: " + err);
      }
    }

    // ------------------------------------------------------------
    // MODALS: VIDEO PLAYER, IMAGE LIGHTBOX, TASKS
    // ------------------------------------------------------------
    function openVideoPlayer(key, name, url = "") {
      currentlyPlayingKey = key;
      const modal = document.getElementById("videoModal");
      const player = document.getElementById("mainVideoPlayer");
      const title = document.getElementById("videoModalTitle");
      const keyText = document.getElementById("videoModalKeyText");
      const subtitle = document.getElementById("videoModalSubtitle");

      title.innerText = name || key;
      keyText.innerText = `R2 Key: ${key}`;
      if (subtitle) subtitle.innerText = "Connecting stream...";

      // Always stream directly through the range-supported endpoint for instant playback
      const streamUrl = `/api/media/stream?target=source&key=${encodeURIComponent(key)}`;
      player.preload = "auto";
      player.src = streamUrl;

      modal.classList.remove("hidden");
      player.load();

      player.oncanplay = () => {
        if (subtitle) subtitle.innerText = "Streaming (Instant Play)";
      };

      player.onerror = () => {
        console.error("Video player error:", player.error);
        if (subtitle) subtitle.innerText = "Error: Video format not supported natively or stream failed";
      };

      const playPromise = player.play();
      if (playPromise !== undefined) {
        playPromise.catch(e => {
          console.log("Autoplay notice:", e);
        });
      }
      lucide.createIcons();
    }

    function closeVideoPlayer() {
      const modal = document.getElementById("videoModal");
      const player = document.getElementById("mainVideoPlayer");
      modal.classList.add("hidden");
      player.pause();
      player.removeAttribute("src");
      player.load();
      currentlyPlayingKey = "";
    }

    function processCurrentPlayingVideo() {
      if (!currentlyPlayingKey) return;
      const key = currentlyPlayingKey;
      closeVideoPlayer();
      triggerProcessSingle(key);
    }

    function openImageModal(key, name, target = "source", url = "") {
      const modal = document.getElementById("imageModal");
      const img = document.getElementById("imageModalEl");
      const title = document.getElementById("imageModalTitle");

      title.innerText = name || key;
      img.src = url || `/api/media/stream?target=${target}&key=${encodeURIComponent(key)}`;
      modal.classList.remove("hidden");
      lucide.createIcons();
    }

    function closeImageModal() {
      const modal = document.getElementById("imageModal");
      const img = document.getElementById("imageModalEl");
      modal.classList.add("hidden");
      img.src = "";
    }

    function openTaskModal() {
      document.getElementById("taskModal").classList.remove("hidden");
      pollTaskStatus();
    }

    function closeTaskModal() {
      document.getElementById("taskModal").classList.add("hidden");
    }

    async function pollTaskStatus() {
      try {
        const res = await fetch("/api/task/status");
        const data = await res.json();

        const dot = document.getElementById("taskStatusDot");
        const stage = document.getElementById("taskStageText");
        const keyText = document.getElementById("taskKeyText");
        const badge = document.getElementById("taskProgressBadge");
        const logsBox = document.getElementById("terminalLogs");

        if (data.is_running) {
          dot.className = "w-2 h-2 rounded-full bg-emerald-500 animate-ping inline-block";
          stage.innerText = data.current_stage || "Processing...";
          keyText.innerText = data.current_key ? "Current: " + data.current_key : "Working...";
          badge.innerText = `${data.current_index} / ${data.total}`;
          badge.className = "text-xs font-mono font-semibold px-2 py-1 rounded bg-emerald-950 text-emerald-300 border border-emerald-800";
        } else {
          dot.className = "w-2 h-2 rounded-full bg-zinc-600 inline-block";
          stage.innerText = data.total > 0 ? "Completed" : "Idle";
          keyText.innerText = data.total > 0 ? `Finished: ${data.completed_count} success, ${data.failed_count} failed` : "No active task running.";
          badge.innerText = `${data.completed_count} / ${data.total}`;
          badge.className = "text-xs font-mono font-semibold px-2 py-1 rounded bg-zinc-800 text-zinc-400";
        }

        if (Array.isArray(data.logs)) {
          logsBox.innerText = data.logs.join("\\n");
          logsBox.scrollTop = logsBox.scrollHeight;
        }
      } catch (e) {}
    }

    function copyPathToClipboard(text) {
      navigator.clipboard.writeText(text).then(() => {
        showToast(`Copied path: ${text}`);
      }).catch(err => {
        prompt("Copy path:", text);
      });
    }

    function showToast(msg) {
      const toast = document.getElementById("copyToast");
      const text = document.getElementById("copyToastText");
      text.innerText = msg;
      toast.classList.remove("opacity-0", "translate-y-3", "pointer-events-none");
      setTimeout(() => {
        toast.classList.add("opacity-0", "translate-y-3", "pointer-events-none");
      }, 2500);
    }

    window.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        closeVideoPlayer();
        closeImageModal();
        closeTaskModal();
        closeDeleteModal();
      }
    });

    setInterval(() => {
      pollTaskStatus();
      loadStats();
    }, 3000);

    window.addEventListener("DOMContentLoaded", () => {
      loadStats();
      loadExplorer("");
      lucide.createIcons();
    });
  </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(
        INDEX_HTML, 
        source_bucket=SOURCE_R2_BUCKET or "Source Bucket",
        dest_bucket=DEST_R2_BUCKET or "Destination Bucket"
    )


# ============================================================
# MAIN ENTRYPOINT (KOYEB COMPLIANT)
# ============================================================
def main():
    init_services()
    start_auto_ping()
    port = int(os.environ.get("PORT", 8080))
    log(f"🚀 Starting R2 File Manager Web Server on port {port} for Koyeb...")
    app.run(host="0.0.0.0", port=port, debug=False)

if __name__ == "__main__":
    main()
