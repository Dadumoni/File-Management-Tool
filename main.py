import time
import tempfile
import threading
from pathlib import Path
from datetime import datetime
from playwright.sync_api import sync_playwright

from config import (
    validate_config,
    WATCH_INTERVAL
)
from server.health import run_health_server, auto_ping
from storage.r2_client import (
    create_r2_clients,
    list_videos,
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

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def process_video(s3_source, s3_dest, page, video: dict, temp_dir: Path, processed_titles: set):
    key       = video["key"]
    orig_ext  = Path(key).suffix.lower()
    orig_name = Path(key).name

    log("")
    log("=" * 65)
    log(f"PROCESSING : {orig_name}")
    log(f"R2 KEY     : {key}")
    log("=" * 65)

    # Generate 10-char alphanumeric slug e.g. JSBF7EB48B
    slug = supabase_get_next_slug()
    new_name = make_rename(orig_ext)
    local_file = temp_dir / new_name

    try:
        # Step 1: Download video from Source R2
        download_from_r2(s3_source, key, local_file)

        # Step 2: Extract technical metadata & check for duplicates in MongoDB
        file_hash = calculate_file_hash(local_file)
        metadata = get_video_metadata(local_file)

        is_dup, reason = is_duplicate_video(file_hash, metadata)
        if is_dup:
            log(f"[MongoDB] Duplicate video detected ({reason}). Skipping upload.")
            if local_file.exists():
                local_file.unlink()
            delete_from_r2(s3_source, key)
            return True

        # Step 3: Generate single composite thumbnail (9:16 -> 3 panels, 16:9 -> 2x2 grid, etc.)
        thumb_file = generate_single_composite_thumbnail(local_file, slug, temp_dir, metadata)

        # Step 4: Upload thumbnail to Destination R2 inside 'new_thumbnails' folder
        # Key will be e.g. new_thumbnails/JSBF7EB48B_thumb.jpg
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

        # Step 6: Save record to Supabase (saving thumbnail path, not full URL)
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

        # Step 7: Save rich metadata in MongoDB to prevent future duplicates
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
