import subprocess
from pathlib import Path
from PIL import Image

def log(msg):
    import time
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

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
      arranges 3 vertical panels side-by-side on a 16:9 canvas (white background/borders).
    - 16:9 (horizontal): splits video into 4 parts, takes the first frame of each part,
      arranges 4 frames in a 2x2 grid on a 16:9 canvas (black background/borders).
    - Other aspect ratios (e.g. 3:4, 4:3): arranges frames according to the image ratio.
    """
    duration = metadata.get("duration", 1.0)
    w = metadata.get("width", 0)
    h = metadata.get("height", 0)
    ratio = metadata.get("aspect_ratio", (w / h) if h > 0 else 1.0)

    log(f"Video resolution: {w}x{h} | Aspect Ratio: {ratio:.3f} | Duration: {duration:.2f}s")

    final_thumb_path = temp_dir / f"{slug}_thumb.jpg"

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

        for idx in range(len(frame_times)):
            p = temp_dir / f"{slug}_part_{idx}.jpg"
            if p.exists():
                p.unlink()

    else:
        # --- Other Aspect Ratio (e.g. 3:4, 4:3, 1:1) ---
        log(f"Layout: Custom aspect ratio ({ratio:.3f}) -> arranging according to frames")
        if ratio < 1.0:
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

        for idx in range(len(frame_times)):
            p = temp_dir / f"{slug}_part_{idx}.jpg"
            if p.exists():
                p.unlink()

    if not final_thumb_path.exists():
        extract_frame(video_path, 0.0, final_thumb_path)

    log(f"Single composite thumbnail generated: {final_thumb_path.name}")
    return final_thumb_path
