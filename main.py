import os
import time
import tempfile
import threading
from pathlib import Path
from datetime import datetime
from flask import Flask, jsonify, request, render_template_string
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
    make_rename
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
  </style>
</head>
<body class="bg-zinc-950 text-zinc-100 min-h-screen flex flex-col antialiased">

  <!-- TOP HEADER -->
  <header class="sticky top-0 z-40 bg-zinc-900/95 backdrop-blur border-b border-zinc-800 px-4 lg:px-8 py-3.5 transition-all">
    <div class="max-w-7xl mx-auto flex items-center justify-between gap-4">
      <div class="flex items-center gap-3">
        <div class="h-9 w-9 rounded-lg bg-zinc-800 border border-zinc-700 grid place-items-center text-white">
          <i data-lucide="cloud-download" class="w-5 h-5"></i>
        </div>
        <div>
          <h1 class="text-sm font-semibold tracking-tight text-white flex items-center gap-2">
            R2 File Explorer
            <span class="text-[10px] font-mono font-normal bg-zinc-800 text-zinc-400 px-2 py-0.5 rounded border border-zinc-700">Source Bucket: {{ bucket_name }}</span>
          </h1>
          <p class="text-xs text-zinc-400">Manual video selector & processor</p>
        </div>
      </div>

      <div class="flex items-center gap-2">
        <button onclick="openTaskModal()" class="px-3 py-1.5 rounded-lg border border-zinc-700 bg-zinc-800 hover:bg-zinc-700 text-xs font-medium text-zinc-200 flex items-center gap-1.5 transition-colors">
          <i data-lucide="activity" class="w-3.5 h-3.5 text-emerald-400"></i>
          <span>Live Task Status</span>
          <span id="taskStatusDot" class="w-2 h-2 rounded-full bg-zinc-500 inline-block"></span>
        </button>

        <button onclick="loadDirectory(currentPrefix)" class="p-2 rounded-lg border border-zinc-700 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 transition-colors" title="Refresh Directory">
          <i data-lucide="rotate-cw" class="w-4 h-4" id="refreshIcon"></i>
        </button>
      </div>
    </div>
  </header>

  <!-- SELECTION ACTION BAR (Floating Header when items are selected) -->
  <div id="selectionHeader" class="hidden sticky top-14 z-30 bg-blue-950/90 border-b border-blue-800/80 px-4 lg:px-8 py-2.5 shadow-lg backdrop-blur">
    <div class="max-w-7xl mx-auto flex flex-wrap items-center justify-between gap-3">
      <div class="flex items-center gap-2.5">
        <span class="h-2 w-2 rounded-full bg-blue-400 animate-pulse"></span>
        <span id="selectedCountText" class="text-xs font-semibold text-blue-200 font-mono">0 item(s) selected</span>
        <button onclick="clearSelection()" class="text-[11px] text-blue-400 hover:text-white underline cursor-pointer ml-1">Clear</button>
      </div>

      <div class="flex items-center gap-2">
        <!-- DOWNLOAD & PROCESS BUTTON -->
        <button onclick="triggerProcessSelected()" class="px-3.5 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold flex items-center gap-1.5 shadow transition-all cursor-pointer">
          <i data-lucide="play" class="w-3.5 h-3.5 fill-white"></i>
          <span>Download & Process</span>
        </button>

        <!-- DELETE BUTTON -->
        <button onclick="openDeleteModal()" class="px-3 py-1.5 rounded-lg bg-red-600/90 hover:bg-red-500 text-white text-xs font-semibold flex items-center gap-1.5 shadow transition-all cursor-pointer">
          <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
          <span>Delete from R2</span>
        </button>
      </div>
    </div>
  </div>

  <!-- MAIN BODY -->
  <main class="flex-1 max-w-7xl w-full mx-auto p-4 lg:p-8 space-y-4">

    <!-- BREADCRUMBS & SEARCH BAR -->
    <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-3 flex flex-col md:flex-row items-stretch md:items-center justify-between gap-3 shadow-sm">
      <!-- Breadcrumb navigation -->
      <nav id="breadcrumbs" class="flex items-center gap-1 text-xs text-zinc-400 overflow-x-auto whitespace-nowrap py-1">
        <!-- populated dynamically -->
      </nav>

      <!-- Search input -->
      <div class="relative min-w-[240px]">
        <i data-lucide="search" class="w-3.5 h-3.5 text-zinc-400 absolute left-3 top-1/2 -translate-y-1/2"></i>
        <input 
          type="text" 
          id="searchFilter" 
          placeholder="Filter current view..." 
          oninput="renderView()" 
          class="w-full bg-zinc-950 border border-zinc-800 rounded-lg pl-8 pr-3 py-1.5 text-xs text-white placeholder-zinc-500 focus:outline-none focus:border-zinc-600"
        >
      </div>
    </div>

    <!-- DIRECTORY EXPLORER CARD -->
    <div class="bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden shadow">
      <!-- Header row -->
      <div class="bg-zinc-950/60 border-b border-zinc-800 px-4 py-3 flex items-center justify-between text-xs font-medium text-zinc-400 uppercase tracking-wider text-[11px]">
        <div class="flex items-center gap-3">
          <input type="checkbox" id="selectAllCheckbox" onchange="toggleSelectAll(this.checked)" class="rounded bg-zinc-800 border-zinc-700 text-blue-500 focus:ring-0 cursor-pointer">
          <span>Name</span>
        </div>
        <div class="flex items-center gap-8">
          <span class="hidden sm:inline w-20 text-right">Size</span>
          <span class="hidden md:inline w-32 text-right">Last Modified</span>
          <span class="w-24 text-right">Actions</span>
        </div>
      </div>

      <!-- File List -->
      <div id="fileListContainer" class="divide-y divide-zinc-800/60 text-xs">
        <!-- Dynamic file and folder rows -->
        <div class="p-8 text-center text-zinc-500">Loading directory...</div>
      </div>
    </div>

  </main>

  <!-- TASK STATUS MODAL -->
  <div id="taskModal" class="hidden fixed inset-0 z-50 overflow-hidden flex items-center justify-center p-4 bg-black/70 backdrop-blur-xs">
    <div class="bg-zinc-900 border border-zinc-800 rounded-2xl max-w-2xl w-full p-5 shadow-2xl flex flex-col max-h-[85vh]">
      <div class="flex items-center justify-between pb-3 border-b border-zinc-800">
        <div class="flex items-center gap-2">
          <i data-lucide="cpu" class="w-4 h-4 text-emerald-400"></i>
          <h3 class="text-sm font-semibold text-white">Video Processing Status</h3>
        </div>
        <button onclick="closeTaskModal()" class="text-zinc-400 hover:text-white p-1">
          <i data-lucide="x" class="w-4 h-4"></i>
        </button>
      </div>

      <div class="py-4 space-y-4 flex-1 overflow-y-auto">
        <!-- Status Banner -->
        <div id="taskActiveBanner" class="p-3.5 rounded-xl border border-zinc-800 bg-zinc-950 flex items-center justify-between">
          <div>
            <div id="taskStageText" class="text-xs font-semibold text-white">Idle</div>
            <div id="taskKeyText" class="text-[11px] text-zinc-400 font-mono truncate max-w-md mt-0.5">No active task</div>
          </div>
          <div id="taskProgressBadge" class="text-xs font-mono font-semibold px-2 py-1 rounded bg-zinc-800 text-zinc-300">0 / 0</div>
        </div>

        <!-- Terminal Logs Box -->
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

  <!-- DELETE CONFIRMATION MODAL -->
  <div id="deleteModal" class="hidden fixed inset-0 z-50 overflow-hidden flex items-center justify-center p-4 bg-black/70 backdrop-blur-xs">
    <div class="bg-zinc-900 border border-zinc-800 rounded-2xl max-w-md w-full p-5 shadow-2xl space-y-4">
      <div class="flex items-center gap-3 text-red-400">
        <div class="p-2 rounded-lg bg-red-950/60 border border-red-800/80">
          <i data-lucide="alert-triangle" class="w-5 h-5 text-red-400"></i>
        </div>
        <div>
          <h3 class="text-sm font-semibold text-white">Confirm Permanent Deletion</h3>
          <p class="text-xs text-zinc-400 mt-0.5">Objects will be deleted directly from Source R2.</p>
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

  <!-- JAVASCRIPT LOGIC -->
  <script>
    let currentPrefix = "";
    let directoryData = { folders: [], files: [] };
    let selectedFolders = new Set();
    let selectedFiles = new Set();
    let taskPollTimer = null;

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
          renderView();
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
        <button onclick="loadDirectory('')" class="hover:text-white font-medium flex items-center gap-1 transition-colors">
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
          html += `<button onclick="loadDirectory('${targetPrefix}')" class="hover:text-white transition-colors">${part}</button>`;
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

    function renderView() {
      const container = document.getElementById("fileListContainer");
      const filter = (document.getElementById("searchFilter")?.value || "").toLowerCase().trim();

      const filteredFolders = directoryData.folders.filter(f => f.name.toLowerCase().includes(filter));
      const filteredFiles = directoryData.files.filter(f => f.name.toLowerCase().includes(filter));

      if (filteredFolders.length === 0 && filteredFiles.length === 0) {
        container.innerHTML = `<div class="p-12 text-center text-zinc-500 text-xs">No files or folders found in this directory.</div>`;
        return;
      }

      let html = "";

      // Render Folders
      filteredFolders.forEach(folder => {
        const isSelected = selectedFolders.has(folder.prefix);
        html += `
          <div class="px-4 py-2.5 flex items-center justify-between hover:bg-zinc-800/40 transition-colors ${isSelected ? 'bg-blue-950/20' : ''}">
            <div class="flex items-center gap-3 min-w-0">
              <input type="checkbox" ${isSelected ? 'checked' : ''} onchange="toggleSelectFolder('${folder.prefix}', this.checked)" class="rounded bg-zinc-800 border-zinc-700 text-blue-500 focus:ring-0 cursor-pointer">
              <div onclick="loadDirectory('${folder.prefix}')" class="flex items-center gap-2 cursor-pointer group">
                <i data-lucide="folder" class="w-4 h-4 text-amber-400 fill-amber-400/20 group-hover:scale-110 transition-transform"></i>
                <span class="font-medium text-white group-hover:underline truncate">${folder.name}</span>
                <span class="text-[9.5px] font-mono px-1.5 py-0.2 rounded bg-zinc-800 text-zinc-400 border border-zinc-700">FOLDER</span>
              </div>
            </div>
            <div class="flex items-center gap-8">
              <span class="hidden sm:inline w-20 text-right text-zinc-500 font-mono">-</span>
              <span class="hidden md:inline w-32 text-right text-zinc-500 font-mono">-</span>
              <div class="w-24 text-right">
                <button onclick="triggerProcessFolder('${folder.prefix}')" class="px-2 py-1 rounded bg-zinc-800 hover:bg-emerald-600 hover:text-white text-[11px] text-zinc-300 transition-colors" title="Process all videos in folder">
                  Process
                </button>
              </div>
            </div>
          </div>
        `;
      });

      // Render Files
      filteredFiles.forEach(file => {
        const isSelected = selectedFiles.has(file.key);
        let iconName = "file";
        let iconColor = "text-zinc-400";
        let badgeBg = "bg-zinc-800 text-zinc-400 border-zinc-700";

        if (file.is_video) {
          iconName = "film";
          iconColor = "text-rose-400";
          badgeBg = "bg-rose-950/40 text-rose-300 border-rose-800/60";
        } else if (file.is_image) {
          iconName = "image";
          iconColor = "text-blue-400";
          badgeBg = "bg-blue-950/40 text-blue-300 border-blue-800/60";
        }

        html += `
          <div class="px-4 py-2.5 flex items-center justify-between hover:bg-zinc-800/40 transition-colors ${isSelected ? 'bg-blue-950/20' : ''}">
            <div class="flex items-center gap-3 min-w-0">
              <input type="checkbox" ${isSelected ? 'checked' : ''} onchange="toggleSelectFile('${file.key}', this.checked)" class="rounded bg-zinc-800 border-zinc-700 text-blue-500 focus:ring-0 cursor-pointer">
              <div class="flex items-center gap-2 truncate">
                <i data-lucide="${iconName}" class="w-4 h-4 ${iconColor}"></i>
                <span class="text-zinc-200 truncate font-mono">${file.name}</span>
                <span class="text-[9.5px] font-mono px-1.5 py-0.2 rounded border ${badgeBg}">${file.ext.toUpperCase()}</span>
              </div>
            </div>
            <div class="flex items-center gap-8 font-mono">
              <span class="hidden sm:inline w-20 text-right text-zinc-400 text-[11px]">${formatBytes(file.size)}</span>
              <span class="hidden md:inline w-32 text-right text-zinc-500 text-[11px]">${file.last_modified}</span>
              <div class="w-24 text-right">
                ${file.is_video ? `
                  <button onclick="triggerProcessSingle('${file.key}')" class="px-2 py-1 rounded bg-emerald-950/50 hover:bg-emerald-600 text-emerald-300 hover:text-white border border-emerald-800 text-[11px] transition-colors" title="Download & Upload to 3 hosts">
                    Process
                  </button>
                ` : `
                  <button onclick="triggerDeleteSingle('${file.key}')" class="px-2 py-1 rounded hover:bg-red-950 text-zinc-500 hover:text-red-400 text-[11px] transition-colors" title="Delete file">
                    Delete
                  </button>
                `}
              </div>
            </div>
          </div>
        `;
      });

      container.innerHTML = html;
      lucide.createIcons();
    }

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
      renderView();
      updateSelectionUI();
    }

    function clearSelection() {
      selectedFolders.clear();
      selectedFiles.clear();
      const chk = document.getElementById("selectAllCheckbox");
      if (chk) chk.checked = false;
      renderView();
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

    // TRIGGER PROCESS
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

    // TRIGGER DELETE
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
        } else {
          alert(json.error || "Failed to delete items.");
        }
      } catch (err) {
        alert("Deletion failed: " + err);
      }
    }

    // TASK MONITORING
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

    setInterval(() => {
      pollTaskStatus();
    }, 2500);

    // Initial load
    window.addEventListener("DOMContentLoaded", () => {
      loadDirectory("");
      lucide.createIcons();
    });
  </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(INDEX_HTML, bucket_name=SOURCE_R2_BUCKET or "Source Bucket")

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
