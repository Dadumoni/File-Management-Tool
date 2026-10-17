import time
import json
import uuid
import requests
from pathlib import Path
from config import ZEROSTORAGE_API_KEY, UPLOAD_CHUNK_SIZE, MAX_RETRIES

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

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
