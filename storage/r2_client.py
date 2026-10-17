from pathlib import Path
from datetime import datetime
import boto3
from botocore.config import Config
from config import (
    SOURCE_R2_ENDPOINT,
    SOURCE_R2_ACCESS_KEY,
    SOURCE_R2_SECRET_KEY,
    SOURCE_R2_BUCKET,
    SOURCE_R2_WATCH_FOLDER,
    DEST_R2_ENDPOINT,
    DEST_R2_ACCESS_KEY,
    DEST_R2_SECRET_KEY,
    DEST_R2_BUCKET,
    VIDEO_EXTENSIONS,
    validate_config
)

def log(msg):
    import time
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def create_r2_clients():
    validate_config()
    source_s3 = boto3.client(
        "s3",
        endpoint_url=SOURCE_R2_ENDPOINT,
        aws_access_key_id=SOURCE_R2_ACCESS_KEY,
        aws_secret_access_key=SOURCE_R2_SECRET_KEY,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "adaptive"}),
    )
    dest_s3 = boto3.client(
        "s3",
        endpoint_url=DEST_R2_ENDPOINT,
        aws_access_key_id=DEST_R2_ACCESS_KEY,
        aws_secret_access_key=DEST_R2_SECRET_KEY,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "adaptive"}),
    )
    return source_s3, dest_s3

def list_videos(s3_source):
    prefix = SOURCE_R2_WATCH_FOLDER.strip()
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    videos = []
    paginator = s3_source.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=SOURCE_R2_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            ext = Path(key).suffix.lower()
            if ext in VIDEO_EXTENSIONS:
                videos.append({"key": key, "size": obj["Size"]})
    return videos

def delete_from_r2(s3_source, key):
    log(f"Deleting original video from Source R2: {key}")
    s3_source.delete_object(Bucket=SOURCE_R2_BUCKET, Key=key)

def download_from_r2(s3_source, key, destination: Path):
    log(f"Downloading from Source R2: {key}")
    s3_source.download_file(SOURCE_R2_BUCKET, key, str(destination))
    if not destination.exists():
        raise RuntimeError("Download completed but file missing.")
    log(f"Downloaded: {destination.stat().st_size / 1024 / 1024:.2f} MB")

def make_rename(ext):
    ts = datetime.now().strftime("%d%m%y%H%M%S")
    return f"TG-@atoz_links-VID-{ts}{ext}"

def upload_thumb_to_r2(s3_dest, thumb_path: Path, slug: str) -> str:
    """
    Uploads thumbnail to Destination R2 inside 'new_thumbnails' folder.
    Returns the relative thumbnail path (NOT the URL link).
    e.g. new_thumbnails/JSBF7EB48B_thumb.jpg
    """
    r2_key = f"new_thumbnails/{slug}_thumb.jpg"
    log(f"Uploading thumbnail to Destination R2: {r2_key}")
    s3_dest.upload_file(
        str(thumb_path),
        DEST_R2_BUCKET,
        r2_key,
        ExtraArgs={"ContentType": "image/jpeg"},
    )
    log(f"Thumbnail uploaded successfully to path: {r2_key}")
    return r2_key
