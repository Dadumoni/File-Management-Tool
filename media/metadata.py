import json
import subprocess
from pathlib import Path

def log(msg):
    import time
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

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
