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
    DEST_R2_BUCKET
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
        log("✔ S3 R2 Clients and Supabase table initialized successfully.")
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
    try:
        data = list_directory(s3_source, prefix)
        return jsonify({"success": True, "data": data})
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

@app.route("/api/media/stream")
def media_stream():
    """
    Streams media (videos and images) from Source or Destination R2.
    Supports HTTP 206 Partial Content (Range requests) so modern browsers can
    instantly load the video's first frame or seek without downloading the whole file.
    """
    key = request.args.get("key", "")
    target = request.args.get("target", "source")
    if not key:
        return "Missing key parameter", 400

    client = s3_dest if target == "dest" else s3_source
    bucket = DEST_R2_BUCKET if target == "dest" else SOURCE_R2_BUCKET

    if not client or not bucket:
        return "Storage client not initialized", 500

    ext = Path(key).suffix.lower()
    content_type = "video/mp4"
    if ext in [".jpg", ".jpeg"]:
        content_type = "image/jpeg"
    elif ext == ".png":
        content_type = "image/png"
    elif ext == ".webp":
        content_type = "image/webp"
    elif ext == ".webm":
        content_type = "video/webm"
    elif ext in [".mkv", ".ts"]:
        content_type = "video/mp4"
    elif ext == ".mov":
        content_type = "video/quicktime"
    elif ext == ".3gp":
        content_type = "video/3gpp"

    try:
        presigned_url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=3600 * 24
        )
        return redirect(presigned_url, code=302)
    except Exception as e:
        log(f"Presigned error for {key}, attempting direct stream: {e}")
        try:
            s3_res = client.get_object(Bucket=bucket, Key=key)
            return Response(s3_res["Body"].read(), status=200, content_type=content_type)
        except Exception as inner_err:
            log(f"Media stream failed: {inner_err}")
            return f"Streaming error: {inner_err}", 500

@app.route("/api/thumbnails")
def api_thumbnails():
    try:
        data = list_destination_thumbnails(s3_dest)
        return jsonify({"success": True, "thumbnails": data, "bucket": DEST_R2_BUCKET})
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
        return jsonify({"success": True, "key": key})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/stats")
def api_stats():
    try:
        stats = get_bucket_counts(s3_source, s3_dest)
        return jsonify({"success": True, "stats": stats})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ============================================================
# EMBEDDED SINGLE-PAGE WEB EXPLORER (INDEX PAGE)
# ============================================================
INDEX_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>R2 Video Manager & Processor</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://unpkg.com/lucide@latest"></script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
    body { font-family: 'Inter', system-ui, sans-serif; }
    .font-mono { font-family: 'JetBrains Mono', monospace; }
    /* Toast transition */
    #copyToast { transition: opacity 0.3s ease, transform 0.3s ease; }
  </style>
</head>
<body class="bg-zinc-950 text-zinc-100 min-h-screen flex flex-col antialiased">

  <!-- TOP HEADER -->
  <header class="sticky top-0 z-40 bg-zinc-900/95 backdrop-blur border-b border-zinc-800 px-4 lg:px-8 py-3 transition-all shadow-sm">
    <div class="max-w-7xl mx-auto flex flex-wrap items-center justify-between gap-3">
      
      <!-- Brand & Main Navigation -->
      <div class="flex items-center gap-4">
        <div class="flex items-center gap-2.5">
          <div class="h-9 w-9 rounded-lg bg-blue-600/20 border border-blue-500/30 grid place-items-center text-blue-400">
            <i data-lucide="clapperboard" class="w-5 h-5"></i>
          </div>
          <div>
            <h1 class="text-sm font-semibold tracking-tight text-white flex items-center gap-2">
              R2 Video Manager
              <span class="text-[10px] font-mono font-normal bg-zinc-800 text-zinc-400 px-2 py-0.5 rounded border border-zinc-700 hidden sm:inline-block">Koyeb Engine</span>
            </h1>
            <p class="text-[11px] text-zinc-400 font-mono">Source: {{ source_bucket }} | Dest: {{ dest_bucket }}</p>
          </div>
        </div>

        <!-- Navigation Tabs Menu -->
        <div class="flex items-center bg-zinc-950 p-1 rounded-xl border border-zinc-800 ml-2">
          <button 
            id="tabSourceBtn" 
            onclick="switchMainTab('source')" 
            class="px-3 py-1.5 rounded-lg text-xs font-medium transition-all flex items-center gap-1.5 bg-zinc-800 text-white shadow-xs"
          >
            <i data-lucide="folder" class="w-3.5 h-3.5 text-blue-400"></i>
            <span>Source Files</span>
          </button>
          
          <button 
            id="tabDestBtn" 
            onclick="switchMainTab('dest')" 
            class="px-3 py-1.5 rounded-lg text-xs font-medium transition-all flex items-center gap-1.5 text-zinc-400 hover:text-white"
          >
            <i data-lucide="sparkles" class="w-3.5 h-3.5 text-amber-400"></i>
            <span>Destination Thumbnails</span>
            <span id="tabDestCountBadge" class="ml-1 text-[10px] font-mono px-1.5 py-0.2 rounded-full bg-amber-950/80 text-amber-300 border border-amber-800/60">0</span>
          </button>
        </div>
      </div>

      <!-- Header Action Buttons -->
      <div class="flex items-center gap-2">
        <button onclick="openTaskModal()" class="px-3 py-1.5 rounded-lg border border-zinc-700 bg-zinc-800 hover:bg-zinc-700 text-xs font-medium text-zinc-200 flex items-center gap-1.5 transition-colors cursor-pointer">
          <i data-lucide="activity" class="w-3.5 h-3.5 text-emerald-400"></i>
          <span>Tasks</span>
          <span id="taskStatusDot" class="w-2 h-2 rounded-full bg-zinc-500 inline-block"></span>
        </button>

        <button onclick="refreshActiveTab()" class="p-2 rounded-lg border border-zinc-700 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 transition-colors cursor-pointer" title="Refresh Current View">
          <i data-lucide="rotate-cw" class="w-4 h-4" id="refreshIcon"></i>
        </button>
      </div>

    </div>
  </header>

  <!-- FLOATING SELECTION BAR (For Batch Operations) -->
  <div id="selectionHeader" class="hidden sticky top-14 z-30 bg-blue-950/95 border-b border-blue-800/80 px-4 lg:px-8 py-2.5 shadow-lg backdrop-blur">
    <div class="max-w-7xl mx-auto flex flex-wrap items-center justify-between gap-3">
      <div class="flex items-center gap-2.5">
        <span class="h-2 w-2 rounded-full bg-blue-400 animate-pulse"></span>
        <span id="selectedCountText" class="text-xs font-semibold text-blue-200 font-mono">0 item(s) selected</span>
        <button onclick="clearSelection()" class="text-[11px] text-blue-400 hover:text-white underline cursor-pointer ml-1">Clear</button>
      </div>

      <div class="flex items-center gap-2">
        <!-- DOWNLOAD & PROCESS BUTTON -->
        <button onclick="triggerProcessSelected()" class="px-3.5 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold flex items-center gap-1.5 shadow transition-all cursor-pointer">
          <i data-lucide="download-cloud" class="w-3.5 h-3.5"></i>
          <span>Download & Process</span>
        </button>

        <!-- DELETE BUTTON -->
        <button onclick="openDeleteModal()" class="px-3 py-1.5 rounded-lg bg-red-600 hover:bg-red-500 text-white text-xs font-semibold flex items-center gap-1.5 shadow transition-all cursor-pointer">
          <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
          <span>Delete from R2</span>
        </button>
      </div>
    </div>
  </div>

  <!-- MAIN BODY CONTAINER -->
  <main class="flex-1 max-w-7xl w-full mx-auto p-4 lg:p-8 space-y-5">

    <!-- SUMMARY STATS: CHOTE CHOTE CARDS -->
    <div class="grid grid-cols-1 sm:grid-cols-3 gap-3.5">
      
      <!-- Card 1: R2 Videos -->
      <div 
        onclick="applyTypeFilter('video')" 
        class="bg-zinc-900 border border-zinc-800 hover:border-blue-500/50 rounded-xl p-3.5 flex items-center justify-between shadow-xs cursor-pointer transition-all hover:bg-zinc-900/80 group"
      >
        <div class="flex items-center gap-3">
          <div class="h-10 w-10 rounded-xl bg-blue-950/60 border border-blue-800/60 text-blue-400 grid place-items-center group-hover:scale-105 transition-transform">
            <i data-lucide="film" class="w-5 h-5"></i>
          </div>
          <div>
            <div class="text-[11px] font-medium text-zinc-400 uppercase tracking-wider">R2 Videos</div>
            <div id="statVideosCount" class="text-lg font-bold text-white font-mono leading-tight">--</div>
            <div class="text-[10px] text-zinc-500">Source Bucket Videos</div>
          </div>
        </div>
        <div class="text-right">
          <span class="text-[10px] font-mono px-2 py-0.5 rounded bg-blue-950/80 text-blue-300 border border-blue-800/60">Filter</span>
        </div>
      </div>

      <!-- Card 2: R2 Images -->
      <div 
        onclick="applyTypeFilter('image')" 
        class="bg-zinc-900 border border-zinc-800 hover:border-emerald-500/50 rounded-xl p-3.5 flex items-center justify-between shadow-xs cursor-pointer transition-all hover:bg-zinc-900/80 group"
      >
        <div class="flex items-center gap-3">
          <div class="h-10 w-10 rounded-xl bg-emerald-950/60 border border-emerald-800/60 text-emerald-400 grid place-items-center group-hover:scale-105 transition-transform">
            <i data-lucide="image" class="w-5 h-5"></i>
          </div>
          <div>
            <div class="text-[11px] font-medium text-zinc-400 uppercase tracking-wider">R2 Images</div>
            <div id="statImagesCount" class="text-lg font-bold text-white font-mono leading-tight">--</div>
            <div class="text-[10px] text-zinc-500">Source Bucket Images</div>
          </div>
        </div>
        <div class="text-right">
          <span class="text-[10px] font-mono px-2 py-0.5 rounded bg-emerald-950/80 text-emerald-300 border border-emerald-800/60">Filter</span>
        </div>
      </div>

      <!-- Card 3: Thumbnails (Destination) -->
      <div 
        onclick="switchMainTab('dest')" 
        class="bg-zinc-900 border border-zinc-800 hover:border-amber-500/50 rounded-xl p-3.5 flex items-center justify-between shadow-xs cursor-pointer transition-all hover:bg-zinc-900/80 group"
      >
        <div class="flex items-center gap-3">
          <div class="h-10 w-10 rounded-xl bg-amber-950/60 border border-amber-800/60 text-amber-400 grid place-items-center group-hover:scale-105 transition-transform">
            <i data-lucide="sparkles" class="w-5 h-5"></i>
          </div>
          <div>
            <div class="text-[11px] font-medium text-zinc-400 uppercase tracking-wider">Thumbnails</div>
            <div id="statThumbsCount" class="text-lg font-bold text-white font-mono leading-tight">--</div>
            <div class="text-[10px] text-zinc-500">new_thumbnails/ Folder</div>
          </div>
        </div>
        <div class="text-right">
          <span class="text-[10px] font-mono px-2 py-0.5 rounded bg-amber-950/80 text-amber-300 border border-amber-800/60">Gallery &rarr;</span>
        </div>
      </div>

    </div>

    <!-- ============================================================ -->
    <!-- SECTION 1: SOURCE R2 FILES EXPLORER -->
    <!-- ============================================================ -->
    <div id="sectionSource" class="space-y-4">
      
      <!-- TOOLBAR: BREADCRUMBS, SEARCH, TYPE FILTERS & VIEW MODE -->
      <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-3 flex flex-col md:flex-row items-stretch md:items-center justify-between gap-3 shadow-xs">
        
        <!-- Breadcrumb navigation -->
        <nav id="breadcrumbs" class="flex items-center gap-1 text-xs text-zinc-400 overflow-x-auto whitespace-nowrap py-1">
          <!-- Populated dynamically -->
        </nav>

        <!-- Right Controls: Type Filter, Search & Layout View Switcher -->
        <div class="flex flex-wrap items-center gap-2">
          
          <!-- Filter buttons: All, Videos, Images -->
          <div class="flex items-center bg-zinc-950 p-1 rounded-lg border border-zinc-800 text-[11px]">
            <button onclick="applyTypeFilter('all')" id="filterBtnAll" class="px-2.5 py-1 rounded-md font-medium text-white bg-zinc-800">All</button>
            <button onclick="applyTypeFilter('video')" id="filterBtnVideo" class="px-2.5 py-1 rounded-md font-medium text-zinc-400 hover:text-white">Videos</button>
            <button onclick="applyTypeFilter('image')" id="filterBtnImage" class="px-2.5 py-1 rounded-md font-medium text-zinc-400 hover:text-white">Images</button>
          </div>

          <!-- Search input -->
          <div class="relative min-w-[200px] flex-1 sm:flex-initial">
            <i data-lucide="search" class="w-3.5 h-3.5 text-zinc-400 absolute left-3 top-1/2 -translate-y-1/2"></i>
            <input 
              type="text" 
              id="searchFilter" 
              placeholder="Search current folder..." 
              oninput="renderSourceView()" 
              class="w-full bg-zinc-950 border border-zinc-800 rounded-lg pl-8 pr-3 py-1.5 text-xs text-white placeholder-zinc-500 focus:outline-none focus:border-zinc-600"
            >
          </div>

          <!-- View Switcher (Grid Cards vs Table) -->
          <div class="flex items-center bg-zinc-950 p-1 rounded-lg border border-zinc-800">
            <button onclick="setViewMode('cards')" id="viewModeCardsBtn" class="p-1.5 rounded-md text-white bg-zinc-800" title="Grid / Small Cards View">
              <i data-lucide="layout-grid" class="w-3.5 h-3.5"></i>
            </button>
            <button onclick="setViewMode('table')" id="viewModeTableBtn" class="p-1.5 rounded-md text-zinc-400 hover:text-white" title="Table View">
              <i data-lucide="list" class="w-3.5 h-3.5"></i>
            </button>
          </div>

        </div>
      </div>

      <!-- CONTAINER: DYNAMIC SOURCE FILES VIEW -->
      <div id="sourceViewContainer">
        <!-- Renders Cards or Table via JavaScript -->
        <div class="p-12 text-center text-zinc-500 text-xs bg-zinc-900 border border-zinc-800 rounded-xl">Loading directory...</div>
      </div>

    </div>

    <!-- ============================================================ -->
    <!-- SECTION 2: DESTINATION THUMBNAILS GALLERY (NEW MENU) -->
    <!-- ============================================================ -->
    <div id="sectionDest" class="hidden space-y-4">
      
      <!-- Thumbnails Bar: Search & Info -->
      <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-3 flex flex-col md:flex-row items-stretch md:items-center justify-between gap-3 shadow-xs">
        <div class="flex items-center gap-2 text-xs">
          <i data-lucide="sparkles" class="w-4 h-4 text-amber-400"></i>
          <span class="font-semibold text-white">Destination R2 Thumbnails</span>
          <span class="text-zinc-500 font-mono text-[11px]">(Prefix: new_thumbnails/)</span>
        </div>

        <div class="flex items-center gap-2">
          <!-- Search input for thumbnails -->
          <div class="relative min-w-[240px]">
            <i data-lucide="search" class="w-3.5 h-3.5 text-zinc-400 absolute left-3 top-1/2 -translate-y-1/2"></i>
            <input 
              type="text" 
              id="thumbSearchFilter" 
              placeholder="Search by slug or filename..." 
              oninput="renderThumbnailsView()" 
              class="w-full bg-zinc-950 border border-zinc-800 rounded-lg pl-8 pr-3 py-1.5 text-xs text-white placeholder-zinc-500 focus:outline-none focus:border-zinc-600"
            >
          </div>

          <button onclick="loadThumbnails()" class="px-3 py-1.5 rounded-lg border border-zinc-700 bg-zinc-800 hover:bg-zinc-700 text-xs font-medium text-zinc-200 flex items-center gap-1.5 transition-colors cursor-pointer">
            <i data-lucide="rotate-cw" class="w-3.5 h-3.5"></i>
            <span>Refresh</span>
          </button>
        </div>
      </div>

      <!-- Thumbnail Cards Grid -->
      <div id="thumbnailsContainer" class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
        <div class="col-span-full p-12 text-center text-zinc-500 text-xs bg-zinc-900 border border-zinc-800 rounded-xl">Loading thumbnails...</div>
      </div>

    </div>

  </main>

  <!-- ============================================================ -->
  <!-- VIDEO PLAYER MODAL (PLAY ACTION) -->
  <!-- ============================================================ -->
  <div id="videoModal" class="hidden fixed inset-0 z-50 overflow-hidden flex items-center justify-center p-4 bg-black/85 backdrop-blur-md">
    <div class="bg-zinc-900 border border-zinc-800 rounded-2xl max-w-4xl w-full p-4 shadow-2xl flex flex-col space-y-3">
      
      <!-- Modal Header -->
      <div class="flex items-center justify-between pb-2 border-b border-zinc-800">
        <div class="flex items-center gap-2 truncate">
          <div class="p-1.5 rounded-lg bg-blue-950/80 border border-blue-800/60 text-blue-400">
            <i data-lucide="play" class="w-4 h-4 fill-blue-400"></i>
          </div>
          <div>
            <h3 id="videoModalTitle" class="text-xs font-semibold text-white font-mono truncate max-w-lg">Video Preview</h3>
            <p id="videoModalSubtitle" class="text-[10px] text-zinc-400 font-mono">Source R2 Stream</p>
          </div>
        </div>
        <button onclick="closeVideoPlayer()" class="text-zinc-400 hover:text-white p-1 rounded-lg hover:bg-zinc-800 transition-colors">
          <i data-lucide="x" class="w-5 h-5"></i>
        </button>
      </div>

      <!-- HTML5 Video Player Container -->
      <div class="relative bg-black rounded-xl overflow-hidden aspect-video flex items-center justify-center border border-zinc-800">
        <video 
          id="mainVideoPlayer" 
          controls 
          autoplay 
          playsinline 
          class="w-full h-full object-contain"
        ></video>
      </div>

      <!-- Modal Footer Actions -->
      <div class="flex items-center justify-between pt-2">
        <div class="text-[11px] font-mono text-zinc-400 truncate max-w-md" id="videoModalKeyText"></div>
        <div class="flex items-center gap-2">
          <button id="videoModalProcessBtn" onclick="processCurrentPlayingVideo()" class="px-3.5 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold flex items-center gap-1.5 shadow transition-all cursor-pointer">
            <i data-lucide="download-cloud" class="w-3.5 h-3.5"></i>
            <span>Process This Video</span>
          </button>
          <button onclick="closeVideoPlayer()" class="px-3.5 py-1.5 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-xs font-medium text-zinc-300 transition-colors cursor-pointer">
            Close
          </button>
        </div>
      </div>

    </div>
  </div>

  <!-- ============================================================ -->
  <!-- IMAGE / THUMBNAIL LIGHTBOX MODAL -->
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

  <!-- ============================================================ -->
  <!-- LIVE TASK STATUS MODAL -->
  <!-- ============================================================ -->
  <div id="taskModal" class="hidden fixed inset-0 z-50 overflow-hidden flex items-center justify-center p-4 bg-black/75 backdrop-blur-xs">
    <div class="bg-zinc-900 border border-zinc-800 rounded-2xl max-w-2xl w-full p-5 shadow-2xl flex flex-col max-h-[85vh]">
      <div class="flex items-center justify-between pb-3 border-b border-zinc-800">
        <div class="flex items-center gap-2">
          <i data-lucide="cpu" class="w-4 h-4 text-emerald-400"></i>
          <h3 class="text-sm font-semibold text-white">Video Processing Console</h3>
        </div>
        <button onclick="closeTaskModal()" class="text-zinc-400 hover:text-white p-1">
          <i data-lucide="x" class="w-4 h-4"></i>
        </button>
      </div>

      <div class="py-4 space-y-4 flex-1 overflow-y-auto">
        <div id="taskActiveBanner" class="p-3.5 rounded-xl border border-zinc-800 bg-zinc-950 flex items-center justify-between">
          <div>
            <div id="taskStageText" class="text-xs font-semibold text-white">Idle</div>
            <div id="taskKeyText" class="text-[11px] text-zinc-400 font-mono truncate max-w-md mt-0.5">No active task</div>
          </div>
          <div id="taskProgressBadge" class="text-xs font-mono font-semibold px-2 py-1 rounded bg-zinc-800 text-zinc-300">0 / 0</div>
        </div>

        <div>
          <span class="text-[10px] uppercase font-semibold text-zinc-500 tracking-wider">Live Processing Console</span>
          <div id="terminalLogs" class="mt-1.5 p-3 rounded-xl bg-black border border-zinc-800 text-[11px] font-mono text-zinc-300 h-64 overflow-y-auto whitespace-pre-wrap leading-relaxed">
            Ready.
          </div>
        </div>
      </div>

      <div class="pt-3 border-t border-zinc-800 flex justify-end">
        <button onclick="closeTaskModal()" class="px-4 py-2 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-xs font-medium text-white transition-colors">
          Close
        </button>
      </div>
    </div>
  </div>

  <!-- ============================================================ -->
  <!-- DELETE CONFIRMATION MODAL -->
  <!-- ============================================================ -->
  <div id="deleteModal" class="hidden fixed inset-0 z-50 overflow-hidden flex items-center justify-center p-4 bg-black/75 backdrop-blur-xs">
    <div class="bg-zinc-900 border border-zinc-800 rounded-2xl max-w-md w-full p-5 shadow-2xl space-y-4">
      <div class="flex items-center gap-3 text-red-400">
        <div class="p-2 rounded-lg bg-red-950/60 border border-red-800/80">
          <i data-lucide="alert-triangle" class="w-5 h-5 text-red-400"></i>
        </div>
        <div>
          <h3 class="text-sm font-semibold text-white">Confirm Permanent Deletion</h3>
          <p class="text-xs text-zinc-400 mt-0.5">Items will be deleted permanently from R2 storage.</p>
        </div>
      </div>

      <div class="p-3 rounded-xl bg-zinc-950 border border-zinc-800 text-xs text-zinc-300 max-h-40 overflow-y-auto space-y-1 font-mono">
        <div id="deleteSummaryText">Selected items will be deleted permanently.</div>
      </div>

      <div class="flex items-center justify-end gap-2 pt-2">
        <button onclick="closeDeleteModal()" class="px-3.5 py-1.5 rounded-lg border border-zinc-700 text-xs font-medium text-zinc-300 hover:bg-zinc-800">
          Cancel
        </button>
        <button onclick="executeDelete()" class="px-3.5 py-1.5 rounded-lg bg-red-600 hover:bg-red-500 text-xs font-semibold text-white shadow">
          Delete Permanently
        </button>
      </div>
    </div>
  </div>

  <!-- TOAST NOTIFICATION -->
  <div id="copyToast" class="fixed bottom-6 right-6 z-50 bg-emerald-600 text-white text-xs font-mono px-4 py-2 rounded-xl shadow-2xl opacity-0 translate-y-3 pointer-events-none flex items-center gap-2">
    <i data-lucide="check" class="w-4 h-4"></i>
    <span id="copyToastText">Copied to clipboard!</span>
  </div>

  <!-- ============================================================ -->
  <!-- FRONTEND JAVASCRIPT APPLICATION LOGIC -->
  <!-- ============================================================ -->
  <script>
    let activeMainTab = "source"; // 'source' or 'dest'
    let viewMode = "cards";        // 'cards' or 'table'
    let typeFilter = "all";        // 'all', 'video', 'image'
    let currentPrefix = "";
    let directoryData = { folders: [], files: [] };
    let thumbnailsData = [];
    let selectedFolders = new Set();
    let selectedFiles = new Set();
    let currentlyPlayingKey = "";

    // ------------------------------------------------------------
    // TABS SWITCHING
    // ------------------------------------------------------------
    function switchMainTab(tab) {
      activeMainTab = tab;
      const secSource = document.getElementById("sectionSource");
      const secDest = document.getElementById("sectionDest");
      const btnSource = document.getElementById("tabSourceBtn");
      const btnDest = document.getElementById("tabDestBtn");

      if (tab === "source") {
        secSource.classList.remove("hidden");
        secDest.classList.add("hidden");
        btnSource.className = "px-3 py-1.5 rounded-lg text-xs font-medium transition-all flex items-center gap-1.5 bg-zinc-800 text-white shadow-xs";
        btnDest.className = "px-3 py-1.5 rounded-lg text-xs font-medium transition-all flex items-center gap-1.5 text-zinc-400 hover:text-white";
        renderSourceView();
      } else {
        secSource.classList.add("hidden");
        secDest.classList.remove("hidden");
        btnSource.className = "px-3 py-1.5 rounded-lg text-xs font-medium transition-all flex items-center gap-1.5 text-zinc-400 hover:text-white";
        btnDest.className = "px-3 py-1.5 rounded-lg text-xs font-medium transition-all flex items-center gap-1.5 bg-zinc-800 text-white shadow-xs";
        loadThumbnails();
      }
      lucide.createIcons();
    }

    function refreshActiveTab() {
      if (activeMainTab === "source") {
        loadDirectory(currentPrefix);
      } else {
        loadThumbnails();
      }
      loadBucketStats();
    }

    // ------------------------------------------------------------
    // STATS & COUNTERS
    // ------------------------------------------------------------
    async function loadBucketStats() {
      try {
        const res = await fetch("/api/stats");
        const json = await res.json();
        if (json.success && json.stats) {
          document.getElementById("statVideosCount").innerText = json.stats.videos || 0;
          document.getElementById("statImagesCount").innerText = json.stats.images || 0;
          document.getElementById("statThumbsCount").innerText = json.stats.thumbnails || 0;
          document.getElementById("tabDestCountBadge").innerText = json.stats.thumbnails || 0;
        }
      } catch (e) {
        // silent
      }
    }

    // ------------------------------------------------------------
    // VIEW MODE & TYPE FILTERS
    // ------------------------------------------------------------
    function setViewMode(mode) {
      viewMode = mode;
      const cardsBtn = document.getElementById("viewModeCardsBtn");
      const tableBtn = document.getElementById("viewModeTableBtn");

      if (mode === "cards") {
        cardsBtn.className = "p-1.5 rounded-md text-white bg-zinc-800";
        tableBtn.className = "p-1.5 rounded-md text-zinc-400 hover:text-white";
      } else {
        cardsBtn.className = "p-1.5 rounded-md text-zinc-400 hover:text-white";
        tableBtn.className = "p-1.5 rounded-md text-white bg-zinc-800";
      }
      renderSourceView();
    }

    function applyTypeFilter(type) {
      typeFilter = type;
      if (activeMainTab !== "source") {
        switchMainTab("source");
      }
      const btnAll = document.getElementById("filterBtnAll");
      const btnVideo = document.getElementById("filterBtnVideo");
      const btnImage = document.getElementById("filterBtnImage");

      [btnAll, btnVideo, btnImage].forEach(b => {
        b.className = "px-2.5 py-1 rounded-md font-medium text-zinc-400 hover:text-white";
      });

      if (type === "all") btnAll.className = "px-2.5 py-1 rounded-md font-medium text-white bg-zinc-800";
      else if (type === "video") btnVideo.className = "px-2.5 py-1 rounded-md font-medium text-white bg-blue-900/60 text-blue-300";
      else if (type === "image") btnImage.className = "px-2.5 py-1 rounded-md font-medium text-white bg-emerald-900/60 text-emerald-300";

      renderSourceView();
    }

    // ------------------------------------------------------------
    // SOURCE R2 DIRECTORY LOADING
    // ------------------------------------------------------------
    async function loadDirectory(prefix = "") {
      const icon = document.getElementById("refreshIcon");
      if (icon) icon.classList.add("animate-spin");

      currentPrefix = prefix;
      selectedFolders.clear();
      selectedFiles.clear();
      updateSelectionUI();

      try {
        const res = await fetch(`/api/files?prefix=${encodeURIComponent(prefix)}`);
        const json = await res.json();
        if (json.success) {
          directoryData = json.data;
          renderBreadcrumbs();
          renderSourceView();
        } else {
          alert("Error loading directory: " + (json.error || "Unknown"));
        }
      } catch (err) {
        alert("Failed to connect to backend: " + err);
      } finally {
        if (icon) icon.classList.remove("animate-spin");
      }
    }

    function renderBreadcrumbs() {
      const container = document.getElementById("breadcrumbs");
      if (!container) return;

      const parts = currentPrefix.split("/").filter(Boolean);
      let html = `
        <button onclick="loadDirectory('')" class="hover:text-white font-medium flex items-center gap-1 transition-colors cursor-pointer">
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
          html += `<button onclick="loadDirectory('${targetPrefix}')" class="hover:text-white transition-colors cursor-pointer">${part}</button>`;
        }
      });

      container.innerHTML = html;
      lucide.createIcons();
    }

    function formatBytes(bytes) {
      if (!bytes || bytes === 0) return "0 B";
      const k = 1024;
      const sizes = ["B", "KB", "MB", "GB", "TB"];
      const i = Math.floor(Math.log(bytes) / Math.log(k));
      return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + " " + sizes[i];
    }

    // ------------------------------------------------------------
    // RENDER SOURCE FILES (CARDS OR TABLE VIEW WITH FIRST FRAME)
    // ------------------------------------------------------------
    function renderSourceView() {
      const container = document.getElementById("sourceViewContainer");
      const search = (document.getElementById("searchFilter")?.value || "").toLowerCase().trim();

      let folders = directoryData.folders || [];
      let files = directoryData.files || [];

      // Update top counter cards immediately from directory files
      const allFiles = directoryData.files || [];
      const currentVideos = allFiles.filter(f => f.is_video).length;
      const currentImages = allFiles.filter(f => f.is_image).length;
      const statVid = document.getElementById("statVideosCount");
      const statImg = document.getElementById("statImagesCount");
      if (statVid) statVid.innerText = currentVideos;
      if (statImg) statImg.innerText = currentImages;

      // Apply search filter
      if (search) {
        folders = folders.filter(f => f.name.toLowerCase().includes(search));
        files = files.filter(f => f.name.toLowerCase().includes(search));
      }

      // Apply type filter
      if (typeFilter === "video") {
        folders = [];
        files = files.filter(f => f.is_video);
      } else if (typeFilter === "image") {
        folders = [];
        files = files.filter(f => f.is_image);
      }

      if (folders.length === 0 && files.length === 0) {
        container.innerHTML = `
          <div class="p-14 text-center text-zinc-500 text-xs bg-zinc-900 border border-zinc-800 rounded-xl space-y-2">
            <i data-lucide="inbox" class="w-8 h-8 mx-auto text-zinc-600"></i>
            <div>No matching files or folders found in this directory.</div>
          </div>
        `;
        lucide.createIcons();
        return;
      }

      if (viewMode === "cards") {
        renderCardsView(container, folders, files);
      } else {
        renderTableView(container, folders, files);
      }

      lucide.createIcons();
    }

    // ------------------------------------------------------------
    // 1. CARDS / GRID VIEW (CHOTE CHOTE CARDS WITH FIRST FRAME)
    // ------------------------------------------------------------
    function renderCardsView(container, folders, files) {
      let html = `<div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">`;

      // Folder Cards
      folders.forEach(folder => {
        const isSelected = selectedFolders.has(folder.prefix);
        html += `
          <div class="bg-zinc-900 border ${isSelected ? 'border-blue-500 bg-blue-950/20' : 'border-zinc-800'} rounded-xl p-3 flex flex-col justify-between hover:border-zinc-700 transition-all shadow-xs group">
            <div class="flex items-start justify-between gap-2">
              <input type="checkbox" ${isSelected ? 'checked' : ''} onchange="toggleSelectFolder('${folder.prefix}', this.checked)" class="rounded bg-zinc-800 border-zinc-700 text-blue-500 focus:ring-0 cursor-pointer mt-1">
              <div onclick="loadDirectory('${folder.prefix}')" class="flex-1 cursor-pointer">
                <div class="h-20 rounded-lg bg-zinc-950 border border-zinc-800/80 flex flex-col items-center justify-center text-amber-400 group-hover:scale-102 transition-transform">
                  <i data-lucide="folder" class="w-8 h-8 fill-amber-400/20"></i>
                  <span class="text-[10px] text-zinc-500 mt-1 uppercase font-mono font-medium tracking-wider">Subfolder</span>
                </div>
                <div class="mt-2.5">
                  <h4 class="text-xs font-semibold text-white group-hover:underline truncate" title="${folder.name}">${folder.name}</h4>
                </div>
              </div>
            </div>

            <div class="mt-3 pt-2.5 border-t border-zinc-800/80 flex items-center justify-between">
              <span class="text-[10px] font-mono text-zinc-500">Folder</span>
              <button onclick="triggerProcessFolder('${folder.prefix}')" class="px-2 py-1 rounded bg-zinc-800 hover:bg-emerald-600 hover:text-white text-[11px] text-zinc-300 font-medium transition-colors cursor-pointer" title="Process all videos in folder">
                Process
              </button>
            </div>
          </div>
        `;
      });

      // File Cards (Videos & Images)
      files.forEach(file => {
        const isSelected = selectedFiles.has(file.key);
        const streamUrl = `/api/media/stream?target=source&key=${encodeURIComponent(file.key)}`;
        const directUrl = file.url || streamUrl;
        const safeName = file.name.replace(/'/g, "\\\\'");
        const safeKey = file.key.replace(/'/g, "\\\\'");
        const safeUrl = directUrl.replace(/'/g, "\\\\'");

        html += `
          <div class="bg-zinc-900 border ${isSelected ? 'border-blue-500 bg-blue-950/20' : 'border-zinc-800'} rounded-xl overflow-hidden flex flex-col justify-between hover:border-zinc-700 transition-all shadow-xs group">
            
            <!-- Thumbnail / First Frame Preview Box -->
            <div class="relative w-full aspect-video bg-black overflow-hidden border-b border-zinc-800/80">
              
              ${file.is_video ? `
                <!-- Video First Frame using direct presigned URL with onloadedmetadata -->
                <video 
                  src="${directUrl}#t=0.5" 
                  preload="metadata" 
                  muted 
                  playsinline 
                  onloadedmetadata="try{if(this.currentTime===0)this.currentTime=0.5;}catch(e){}"
                  onerror="if(!this.dataset.retry){this.dataset.retry='1';this.src='${streamUrl}#t=0.5';}"
                  class="w-full h-full object-cover"
                ></video>

                <!-- Hover Play Overlay Icon -->
                <div 
                  onclick="openVideoPlayer('${safeKey}', '${safeName}', '${safeUrl}')" 
                  class="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 flex items-center justify-center transition-opacity cursor-pointer"
                  title="Play video"
                >
                  <div class="w-10 h-10 rounded-full bg-blue-600/90 text-white flex items-center justify-center shadow-lg hover:scale-110 transition-transform">
                    <i data-lucide="play" class="w-4 h-4 fill-white ml-0.5"></i>
                  </div>
                </div>

                <!-- Video Badge -->
                <span class="absolute top-2 right-2 text-[9.5px] font-mono font-semibold px-1.5 py-0.5 rounded bg-rose-950/90 text-rose-300 border border-rose-800/60 shadow">
                  ${file.ext.toUpperCase()}
                </span>
              ` : file.is_image ? `
                <!-- Image Preview with direct presigned URL and fallback -->
                <img 
                  src="${directUrl}" 
                  loading="lazy" 
                  alt="${file.name}" 
                  onclick="openImageModal('${safeKey}', '${safeName}', 'source', '${safeUrl}')"
                  onerror="if(!this.dataset.retry){this.dataset.retry='1';this.src='${streamUrl}';}"
                  class="w-full h-full object-cover cursor-pointer hover:scale-105 transition-transform"
                >
                <span class="absolute top-2 right-2 text-[9.5px] font-mono font-semibold px-1.5 py-0.5 rounded bg-blue-950/90 text-blue-300 border border-blue-800/60 shadow">
                  ${file.ext.toUpperCase()}
                </span>
              ` : `
                <!-- Generic File -->
                <div class="w-full h-full flex flex-col items-center justify-center text-zinc-500">
                  <i data-lucide="file" class="w-8 h-8"></i>
                  <span class="text-[10px] font-mono mt-1">${file.ext.toUpperCase()}</span>
                </div>
              `}

              <!-- Checkbox (Top Left) -->
              <div class="absolute top-2 left-2 z-10 bg-zinc-950/70 p-1 rounded backdrop-blur">
                <input 
                  type="checkbox" 
                  ${isSelected ? 'checked' : ''} 
                  onchange="toggleSelectFile('${file.key}', this.checked)" 
                  class="rounded bg-zinc-800 border-zinc-700 text-blue-500 focus:ring-0 cursor-pointer block"
                >
              </div>

            </div>

            <!-- Card Body Details -->
            <div class="p-3 space-y-2">
              <h4 class="text-xs font-semibold text-white font-mono truncate" title="${file.name}">${file.name}</h4>
              <div class="flex items-center justify-between text-[10px] font-mono text-zinc-400">
                <span>${formatBytes(file.size)}</span>
                <span>${file.last_modified}</span>
              </div>
            </div>

            <!-- Card Actions -->
            <div class="px-3 pb-3 pt-1 border-t border-zinc-800/80 flex items-center justify-between gap-1.5">
              ${file.is_video ? `
                <button 
                  onclick="openVideoPlayer('${safeKey}', '${safeName}', '${safeUrl}')" 
                  class="flex-1 py-1 px-2 rounded-lg bg-blue-950/60 hover:bg-blue-600 text-blue-300 hover:text-white border border-blue-800/60 text-[11px] font-semibold flex items-center justify-center gap-1 transition-all cursor-pointer"
                  title="Play Video"
                >
                  <i data-lucide="play" class="w-3 h-3 fill-current"></i>
                  <span>Play</span>
                </button>

                <button 
                  onclick="triggerProcessSingle('${safeKey}')" 
                  class="flex-1 py-1 px-2 rounded-lg bg-emerald-950/60 hover:bg-emerald-600 text-emerald-300 hover:text-white border border-emerald-800/60 text-[11px] font-semibold flex items-center justify-center gap-1 transition-all cursor-pointer"
                  title="Download, Make Composite Thumbnail & Upload"
                >
                  <i data-lucide="download-cloud" class="w-3 h-3"></i>
                  <span>Process</span>
                </button>
              ` : file.is_image ? `
                <button 
                  onclick="openImageModal('${safeKey}', '${safeName}', 'source', '${safeUrl}')" 
                  class="flex-1 py-1 px-2 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-zinc-300 hover:text-white text-[11px] font-medium flex items-center justify-center gap-1 transition-all cursor-pointer"
                >
                  <i data-lucide="maximize-2" class="w-3 h-3"></i>
                  <span>View</span>
                </button>
              ` : ''}

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

      html += `</div>`;
      container.innerHTML = html;
    }

    // ------------------------------------------------------------
    // 2. TABLE VIEW (WITH VIDEO FIRST FRAME IN COLUMN)
    // ------------------------------------------------------------
    function renderTableView(container, folders, files) {
      let html = `
        <div class="bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden shadow-xs">
          <!-- Table Header -->
          <div class="bg-zinc-950/80 border-b border-zinc-800 px-4 py-3 flex items-center justify-between text-xs font-medium text-zinc-400 uppercase tracking-wider text-[11px]">
            <div class="flex items-center gap-3">
              <input type="checkbox" id="selectAllCheckbox" onchange="toggleSelectAll(this.checked)" class="rounded bg-zinc-800 border-zinc-700 text-blue-500 focus:ring-0 cursor-pointer">
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

      // Render Folders
      folders.forEach(folder => {
        const isSelected = selectedFolders.has(folder.prefix);
        html += `
          <div class="px-4 py-2.5 flex items-center justify-between hover:bg-zinc-800/40 transition-colors ${isSelected ? 'bg-blue-950/20' : ''}">
            <div class="flex items-center gap-3 min-w-0">
              <input type="checkbox" ${isSelected ? 'checked' : ''} onchange="toggleSelectFolder('${folder.prefix}', this.checked)" class="rounded bg-zinc-800 border-zinc-700 text-blue-500 focus:ring-0 cursor-pointer">
              <div onclick="loadDirectory('${folder.prefix}')" class="flex items-center gap-3 cursor-pointer group truncate">
                <div class="w-12 h-8 rounded bg-zinc-950 border border-zinc-800 flex items-center justify-center text-amber-400 shrink-0">
                  <i data-lucide="folder" class="w-4 h-4 fill-amber-400/20"></i>
                </div>
                <span class="font-medium text-white group-hover:underline truncate">${folder.name}</span>
                <span class="text-[9.5px] font-mono px-1.5 py-0.2 rounded bg-zinc-800 text-zinc-400 border border-zinc-700">DIR</span>
              </div>
            </div>
            <div class="flex items-center gap-8 font-mono">
              <span class="hidden sm:inline w-20 text-right text-zinc-500">-</span>
              <span class="hidden md:inline w-32 text-right text-zinc-500">-</span>
              <div class="w-36 text-right">
                <button onclick="triggerProcessFolder('${folder.prefix}')" class="px-2 py-1 rounded bg-zinc-800 hover:bg-emerald-600 hover:text-white text-[11px] text-zinc-300 font-medium transition-colors cursor-pointer" title="Process folder">
                  Process Folder
                </button>
              </div>
            </div>
          </div>
        `;
      });

      // Render Files
      files.forEach(file => {
        const isSelected = selectedFiles.has(file.key);
        const streamUrl = `/api/media/stream?target=source&key=${encodeURIComponent(file.key)}`;
        const directUrl = file.url || streamUrl;
        const safeName = file.name.replace(/'/g, "\\\\'");
        const safeKey = file.key.replace(/'/g, "\\\\'");
        const safeUrl = directUrl.replace(/'/g, "\\\\'");

        html += `
          <div class="px-4 py-2 flex items-center justify-between hover:bg-zinc-800/40 transition-colors ${isSelected ? 'bg-blue-950/20' : ''}">
            <div class="flex items-center gap-3 min-w-0">
              <input type="checkbox" ${isSelected ? 'checked' : ''} onchange="toggleSelectFile('${file.key}', this.checked)" class="rounded bg-zinc-800 border-zinc-700 text-blue-500 focus:ring-0 cursor-pointer">
              
              <!-- First Frame Preview Thumbnail in Table -->
              <div class="w-12 h-8 rounded bg-black border border-zinc-800 overflow-hidden relative shrink-0">
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
                  <div onclick="openVideoPlayer('${safeKey}', '${safeName}', '${safeUrl}')" class="absolute inset-0 bg-black/30 hover:bg-black/0 flex items-center justify-center cursor-pointer">
                    <i data-lucide="play" class="w-2.5 h-2.5 fill-white text-white"></i>
                  </div>
                ` : file.is_image ? `
                  <img 
                    src="${directUrl}" 
                    loading="lazy" 
                    onclick="openImageModal('${safeKey}', '${safeName}', 'source', '${safeUrl}')" 
                    onerror="if(!this.dataset.retry){this.dataset.retry='1';this.src='${streamUrl}';}"
                    class="w-full h-full object-cover cursor-pointer"
                  >
                ` : `
                  <div class="w-full h-full flex items-center justify-center text-zinc-500"><i data-lucide="file" class="w-3.5 h-3.5"></i></div>
                `}
              </div>

              <!-- Name & Badge -->
              <div class="truncate flex items-center gap-2">
                <span class="text-zinc-200 truncate font-mono text-xs">${file.name}</span>
                <span class="text-[9px] font-mono px-1 py-0.2 rounded border ${file.is_video ? 'bg-rose-950/40 text-rose-300 border-rose-800/60' : 'bg-blue-950/40 text-blue-300 border-blue-800/60'}">${file.ext.toUpperCase()}</span>
              </div>
            </div>

            <!-- Table Columns: Size, Date, Actions -->
            <div class="flex items-center gap-8 font-mono">
              <span class="hidden sm:inline w-20 text-right text-zinc-400 text-[11px]">${formatBytes(file.size)}</span>
              <span class="hidden md:inline w-32 text-right text-zinc-500 text-[11px]">${file.last_modified}</span>
              <div class="w-36 text-right flex items-center justify-end gap-1.5">
                ${file.is_video ? `
                  <button 
                    onclick="openVideoPlayer('${safeKey}', '${safeName}', '${safeUrl}')" 
                    class="px-2 py-1 rounded bg-blue-950/60 hover:bg-blue-600 text-blue-300 hover:text-white border border-blue-800/60 text-[11px] font-semibold flex items-center gap-1 transition-all cursor-pointer"
                    title="Play Video"
                  >
                    <i data-lucide="play" class="w-2.5 h-2.5 fill-current"></i>
                    <span>Play</span>
                  </button>

                  <button 
                    onclick="triggerProcessSingle('${safeKey}')" 
                    class="px-2 py-1 rounded bg-emerald-950/60 hover:bg-emerald-600 text-emerald-300 hover:text-white border border-emerald-800/60 text-[11px] font-semibold transition-all cursor-pointer"
                    title="Process"
                  >
                    Process
                  </button>
                ` : file.is_image ? `
                  <button 
                    onclick="openImageModal('${safeKey}', '${safeName}', 'source', '${safeUrl}')" 
                    class="px-2 py-1 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-300 text-[11px] transition-colors cursor-pointer"
                  >
                    View
                  </button>
                ` : ''}

                <button 
                  onclick="triggerDeleteSingle('${safeKey}')" 
                  class="p-1 rounded hover:bg-red-950/60 text-zinc-500 hover:text-red-400 transition-colors cursor-pointer"
                  title="Delete"
                >
                  <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
                </button>
              </div>
            </div>
          </div>
        `;
      });

      html += `</div></div>`;
      container.innerHTML = html;
    }

    // ------------------------------------------------------------
    // 3. DESTINATION THUMBNAILS GALLERY LOGIC (NEW MENU)
    // ------------------------------------------------------------
    async function loadThumbnails() {
      const container = document.getElementById("thumbnailsContainer");
      container.innerHTML = `<div class="col-span-full p-12 text-center text-zinc-500 text-xs">Loading thumbnails from Destination R2...</div>`;

      try {
        const res = await fetch("/api/thumbnails");
        const json = await res.json();
        if (json.success) {
          thumbnailsData = json.thumbnails || [];
          document.getElementById("tabDestCountBadge").innerText = thumbnailsData.length;
          document.getElementById("statThumbsCount").innerText = thumbnailsData.length;
          renderThumbnailsView();
        } else {
          container.innerHTML = `<div class="col-span-full p-8 text-center text-red-400 text-xs">Failed to load thumbnails: ${json.error}</div>`;
        }
      } catch (err) {
        container.innerHTML = `<div class="col-span-full p-8 text-center text-red-400 text-xs">Error connecting to server: ${err}</div>`;
      }
    }

    function renderThumbnailsView() {
      const container = document.getElementById("thumbnailsContainer");
      const search = (document.getElementById("thumbSearchFilter")?.value || "").toLowerCase().trim();

      let filtered = thumbnailsData;
      if (search) {
        filtered = filtered.filter(t => t.name.toLowerCase().includes(search) || t.key.toLowerCase().includes(search));
      }

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
        const streamUrl = `/api/media/stream?target=dest&key=${encodeURIComponent(thumb.key)}`;
        const directUrl = thumb.url || streamUrl;
        const safeKey = thumb.key.replace(/'/g, "\\\\'");
        const safeName = thumb.name.replace(/'/g, "\\\\'");
        const safeUrl = directUrl.replace(/'/g, "\\\\'");

        html += `
          <div class="bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden flex flex-col justify-between hover:border-amber-500/50 transition-all shadow-xs group">
            
            <!-- Thumbnail Image Container -->
            <div class="relative w-full aspect-video bg-black overflow-hidden border-b border-zinc-800/80">
              <img 
                src="${directUrl}" 
                loading="lazy" 
                alt="${thumb.name}" 
                onclick="openImageModal('${safeKey}', '${safeName}', 'dest', '${safeUrl}')"
                onerror="if(!this.dataset.retry){this.dataset.retry='1';this.src='${streamUrl}';}"
                class="w-full h-full object-cover cursor-pointer group-hover:scale-104 transition-transform"
              >
              <span class="absolute top-2 right-2 text-[9.5px] font-mono px-1.5 py-0.5 rounded bg-black/80 text-amber-300 border border-amber-800/60 backdrop-blur">
                COMPOSITE
              </span>
            </div>

            <!-- Details -->
            <div class="p-3 space-y-1.5">
              <div class="flex items-center justify-between">
                <span class="text-xs font-semibold text-white font-mono truncate" title="${thumb.name}">${thumb.name}</span>
              </div>
              <div class="flex items-center justify-between text-[10px] font-mono text-zinc-400">
                <span>${formatBytes(thumb.size)}</span>
                <span>${thumb.last_modified}</span>
              </div>
            </div>

            <!-- Actions -->
            <div class="px-3 pb-3 pt-1 border-t border-zinc-800/80 flex items-center justify-between gap-1.5">
              <button 
                onclick="openImageModal('${safeKey}', '${safeName}', 'dest', '${safeUrl}')" 
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
                <span>Copy Path</span>
              </button>

              <button 
                onclick="deleteThumbnail('${safeKey}')" 
                class="p-1 rounded-lg text-zinc-500 hover:text-red-400 hover:bg-red-950/50 transition-colors cursor-pointer" 
                title="Delete thumbnail from Destination R2"
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
      if (!confirm(`Are you sure you want to permanently delete thumbnail: ${key}?`)) return;
      try {
        const res = await fetch("/api/thumbnails/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ key })
        });
        const json = await res.json();
        if (json.success) {
          loadThumbnails();
          loadBucketStats();
        } else {
          alert("Error: " + (json.error || "Failed to delete thumbnail"));
        }
      } catch (e) {
        alert("Failed to delete thumbnail: " + e);
      }
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

    // ------------------------------------------------------------
    // 4. VIDEO PLAYER MODAL (PLAY ACTION)
    // ------------------------------------------------------------
    function openVideoPlayer(key, name, url = "") {
      currentlyPlayingKey = key;
      const modal = document.getElementById("videoModal");
      const player = document.getElementById("mainVideoPlayer");
      const title = document.getElementById("videoModalTitle");
      const keyText = document.getElementById("videoModalKeyText");

      title.innerText = name || key;
      keyText.innerText = `R2 Key: ${key}`;

      player.src = url || `/api/media/stream?target=source&key=${encodeURIComponent(key)}`;
      modal.classList.remove("hidden");
      player.load();
      player.play().catch(e => console.log("Autoplay notice:", e));
      lucide.createIcons();
    }

    function closeVideoPlayer() {
      const modal = document.getElementById("videoModal");
      const player = document.getElementById("mainVideoPlayer");
      modal.classList.add("hidden");
      player.pause();
      player.src = "";
      currentlyPlayingKey = "";
    }

    function processCurrentPlayingVideo() {
      if (!currentlyPlayingKey) return;
      const key = currentlyPlayingKey;
      closeVideoPlayer();
      triggerProcessSingle(key);
    }

    // ------------------------------------------------------------
    // 5. IMAGE LIGHTBOX MODAL
    // ------------------------------------------------------------
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

    // ------------------------------------------------------------
    // 6. SELECTION & PROCESS ACTIONS
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

    function toggleSelectAll(isChecked) {
      if (isChecked) {
        directoryData.folders.forEach(f => selectedFolders.add(f.prefix));
        directoryData.files.forEach(f => selectedFiles.add(f.key));
      } else {
        selectedFolders.clear();
        selectedFiles.clear();
      }
      renderSourceView();
      updateSelectionUI();
    }

    function clearSelection() {
      selectedFolders.clear();
      selectedFiles.clear();
      const chk = document.getElementById("selectAllCheckbox");
      if (chk) chk.checked = false;
      renderSourceView();
      updateSelectionUI();
    }

    function updateSelectionUI() {
      const header = document.getElementById("selectionHeader");
      const text = document.getElementById("selectedCountText");
      const total = selectedFolders.size + selectedFiles.size;

      if (total > 0) {
        header.classList.remove("hidden");
        text.innerText = `${total} item(s) selected (${selectedFolders.size} folder${selectedFolders.size === 1 ? '' : 's'}, ${selectedFiles.size} file${selectedFiles.size === 1 ? '' : 's'})`;
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
    // 7. DELETE OPERATIONS
    // ------------------------------------------------------------
    function openDeleteModal() {
      const modal = document.getElementById("deleteModal");
      const summary = document.getElementById("deleteSummaryText");
      const total = selectedFolders.size + selectedFiles.size;
      summary.innerText = `Are you sure you want to permanently delete ${total} selected item(s) from Source R2? This action cannot be undone.`;
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
          loadDirectory(currentPrefix);
          loadBucketStats();
        } else {
          alert(json.error || "Failed to delete items.");
        }
      } catch (err) {
        alert("Deletion failed: " + err);
      }
    }

    // ------------------------------------------------------------
    // 8. TASK MONITORING
    // ------------------------------------------------------------
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
      } catch (e) {
        // silent
      }
    }

    // Keyboard shortcuts: ESC closes modals
    window.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        closeVideoPlayer();
        closeImageModal();
        closeTaskModal();
        closeDeleteModal();
      }
    });

    // Auto poll task status every 3 seconds
    setInterval(() => {
      pollTaskStatus();
    }, 3000);

    // Initial load
    window.addEventListener("DOMContentLoaded", () => {
      loadDirectory("");
      loadBucketStats();
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
