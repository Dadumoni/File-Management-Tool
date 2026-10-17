import json
import subprocess
import time
from pathlib import Path
from PIL import Image

def log(msg, log_cb=None):
    ts = time.strftime("%H:%M:%S")
    formatted = f"[{ts}] [VideoMerge] {msg}"
    if log_cb:
        log_cb(formatted)
    else:
        print(formatted, flush=True)

def extract_video_frame(video_path: Path, output_path: Path, timestamp: float = 0.5) -> bool:
    """
    Extracts a single frame from video at given timestamp using ffmpeg.
    """
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{timestamp:.2f}",
        "-i", str(video_path),
        "-vframes", "1",
        "-q:v", "2",
        str(output_path)
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if not output_path.exists() and timestamp > 0.0:
        # Retry at 0.0s if 0.5s failed
        cmd[2] = "0.0"
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return output_path.exists()

def create_merged_thumbnail(frame_paths: list, output_path: Path) -> Path:
    """
    Creates a single composite 1280x720 thumbnail from 1, 2, 3, 4+ selected frames.
    """
    canvas_w, canvas_h = 1280, 720
    canvas = Image.new("RGB", (canvas_w, canvas_h), color=(10, 10, 12))

    valid_images = []
    for fp in frame_paths:
        p = Path(fp)
        if p.exists():
            try:
                img = Image.open(p).convert("RGB")
                valid_images.append(img)
            except Exception as e:
                log(f"Failed to load frame {p}: {e}")

    count = len(valid_images)
    if count == 0:
        # Fallback empty canvas with minimal text or placeholder
        canvas.save(str(output_path), format="WEBP", quality=88, method=6)
        return output_path

    if count == 1:
        # 1 Frame: Cover fill canvas
        img = valid_images[0]
        # Aspect fill
        img_ratio = img.width / img.height
        target_ratio = canvas_w / canvas_h
        if img_ratio > target_ratio:
            new_h = canvas_h
            new_w = int(new_h * img_ratio)
        else:
            new_w = canvas_w
            new_h = int(new_w / img_ratio)
        resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        # Center crop
        left = (new_w - canvas_w) // 2
        top = (new_h - canvas_h) // 2
        cropped = resized.crop((left, top, left + canvas_w, top + canvas_h))
        canvas.paste(cropped, (0, 0))

    elif count == 2:
        # 2 Frames: Side-by-side split
        gap = 8
        panel_w = (canvas_w - gap) // 2
        panel_h = canvas_h
        for idx, img in enumerate(valid_images[:2]):
            # Center crop to panel
            ir = img.width / img.height
            pr = panel_w / panel_h
            if ir > pr:
                nh = panel_h
                nw = int(nh * ir)
            else:
                nw = panel_w
                nh = int(nw / ir)
            res = img.resize((nw, nh), Image.Resampling.LANCZOS)
            cl = (nw - panel_w) // 2
            ct = (nh - panel_h) // 2
            cropped = res.crop((cl, ct, cl + panel_w, ct + panel_h))
            x = idx * (panel_w + gap)
            canvas.paste(cropped, (x, 0))

    elif count == 3:
        # 3 Frames: 3 vertical side-by-side panels
        gap = 8
        panel_w = (canvas_w - 2 * gap) // 3
        panel_h = canvas_h
        for idx, img in enumerate(valid_images[:3]):
            ir = img.width / img.height
            pr = panel_w / panel_h
            if ir > pr:
                nh = panel_h
                nw = int(nh * ir)
            else:
                nw = panel_w
                nh = int(nw / ir)
            res = img.resize((nw, nh), Image.Resampling.LANCZOS)
            cl = (nw - panel_w) // 2
            ct = (nh - panel_h) // 2
            cropped = res.crop((cl, ct, cl + panel_w, ct + panel_h))
            x = idx * (panel_w + gap)
            canvas.paste(cropped, (x, 0))

    else:
        # 4 or more Frames: 2x2 Grid
        gap = 8
        panel_w = (canvas_w - gap) // 2
        panel_h = (canvas_h - gap) // 2
        positions = [
            (0, 0),
            (panel_w + gap, 0),
            (0, panel_h + gap),
            (panel_w + gap, panel_h + gap)
        ]
        for idx, img in enumerate(valid_images[:4]):
            ir = img.width / img.height
            pr = panel_w / panel_h
            if ir > pr:
                nh = panel_h
                nw = int(nh * ir)
            else:
                nw = panel_w
                nh = int(nw / ir)
            res = img.resize((nw, nh), Image.Resampling.LANCZOS)
            cl = (nw - panel_w) // 2
            ct = (nh - panel_h) // 2
            cropped = res.crop((cl, ct, cl + panel_w, ct + panel_h))
            canvas.paste(cropped, positions[idx])

    canvas.save(str(output_path), format="WEBP", quality=88, method=6)
    return output_path

def inspect_video_streams(video_path: Path):
    """
    Returns (has_video, has_audio, width, height, video_codec, audio_codec)
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type,codec_name,width,height",
        "-of", "json",
        str(video_path)
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            streams = data.get("streams", [])
            has_video = any(s.get("codec_type") == "video" for s in streams)
            has_audio = any(s.get("codec_type") == "audio" for s in streams)
            w = h = 0
            v_codec = a_codec = ""
            for s in streams:
                if s.get("codec_type") == "video" and w == 0:
                    w = int(s.get("width", 0))
                    h = int(s.get("height", 0))
                    v_codec = s.get("codec_name", "")
                elif s.get("codec_type") == "audio" and not a_codec:
                    a_codec = s.get("codec_name", "")
            return has_video, has_audio, w, h, v_codec, a_codec
    except Exception:
        pass
    return True, True, 1280, 720, "h264", "aac"

def merge_videos_in_sequence(video_paths: list, output_path: Path, log_cb=None) -> bool:
    """
    Merges multiple video files in exact specified sequence using FFmpeg.
    First tries fast stream-copy concat demuxer if parameters match.
    Falls back to robust re-encoding filter_complex concat ensuring audio/video sync.
    """
    if not video_paths:
        log("No videos provided for merging.", log_cb)
        return False

    if len(video_paths) == 1:
        # Just copy/rename single video
        import shutil
        shutil.copy2(str(video_paths[0]), str(output_path))
        return output_path.exists()

    log(f"Merging {len(video_paths)} videos in order: {[p.name for p in video_paths]}", log_cb)

    temp_dir = output_path.parent
    concat_list_file = temp_dir / f"concat_list_{int(time.time())}.txt"

    # Step A: Check if all streams are identical for fast stream copy
    all_same = True
    base_info = None
    video_infos = []

    for vp in video_paths:
        info = inspect_video_streams(vp)
        video_infos.append(info)
        if base_info is None:
            base_info = info
        else:
            # Check width, height, codecs, has_audio
            if info[1:] != base_info[1:]:
                all_same = False

    if all_same and base_info[1]:  # identical and has audio
        log("Videos have identical stream attributes. Attempting fast stream-copy concat...", log_cb)
        try:
            with open(concat_list_file, "w", encoding="utf-8") as f:
                for vp in video_paths:
                    safe_path = str(vp.resolve()).replace("'", "'\\''")
                    f.write(f"file '{safe_path}'\n")

            cmd_copy = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_list_file),
                "-c", "copy",
                "-movflags", "+faststart",
                str(output_path)
            ]
            res = subprocess.run(cmd_copy, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if res.returncode == 0 and output_path.exists() and output_path.stat().st_size > 1024:
                log(f"✔ Fast stream-copy merge succeeded! Output size: {output_path.stat().st_size / 1024 / 1024:.2f} MB", log_cb)
                if concat_list_file.exists():
                    concat_list_file.unlink()
                return True
            else:
                log("Stream-copy returned non-zero code. Falling back to filter_complex...", log_cb)
        except Exception as e:
            log(f"Stream-copy failed with exception: {e}. Falling back to filter_complex...", log_cb)

    if concat_list_file.exists():
        concat_list_file.unlink()

    # Step B: Robust re-encode concat using filter_complex
    log("Running universal filter_complex merge with resolution & audio normalization...", log_cb)

    # Determine target resolution: default 1280x720, or max among inputs (capped at 1920x1080)
    target_w = 1280
    target_h = 720
    max_w = max((info[2] for info in video_infos if info[2] > 0), default=1280)
    max_h = max((info[3] for info in video_infos if info[3] > 0), default=720)
    if max_w >= 1920 or max_h >= 1080:
        target_w, target_h = 1920, 1080

    cmd = ["ffmpeg", "-y"]

    # Add all inputs
    for vp in video_paths:
        cmd.extend(["-i", str(vp)])

    # Build filter complex
    filter_parts = []
    concat_inputs = []

    for i, (has_v, has_a, w, h, vc, ac) in enumerate(video_infos):
        # Scale and pad to target_w x target_h with aspect ratio preserved
        v_filter = f"[{i}:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v{i}]"
        filter_parts.append(v_filter)

        if has_a:
            a_filter = f"[{i}:a]aformat=sample_rates=44100:channel_layouts=stereo[a{i}]"
            filter_parts.append(a_filter)
        else:
            # Generate silent audio matching video length
            filter_parts.append(f"anullsrc=channel_layout=stereo:sample_rate=44100[a_null_{i}];[{i}:v]trim[v_dummy_{i}];[a_null_{i}]atrim[a{i}]")

        concat_inputs.append(f"[v{i}][a{i}]")

    concat_cmd = f"{''.join(concat_inputs)}concat=n={len(video_paths)}:v=1:a=1[outv][outa]"
    filter_parts.append(concat_cmd)
    full_filter = ";".join(filter_parts)

    cmd.extend([
        "-filter_complex", full_filter,
        "-map", "[outv]",
        "-map", "[outa]",
        "-c:v", "libx264",
        "-preset", "faster",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output_path)
    ])

    log("Starting FFmpeg concatenation process...", log_cb)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    if proc.returncode != 0:
        err_msg = proc.stderr.decode("utf-8", errors="replace")[-600:]
        log(f"✖ FFmpeg merge failed: {err_msg}", log_cb)
        return False

    if output_path.exists() and output_path.stat().st_size > 1024:
        log(f"✔ FFmpeg merge completed successfully! File size: {output_path.stat().st_size / 1024 / 1024:.2f} MB", log_cb)
        return True

    return False
