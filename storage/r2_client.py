from pathlib import Path
from datetime import datetime
import boto3
from botocore.config import Config
from config import (
    SOURCE_R2_ENDPOINT,
    SOURCE_R2_ACCESS_KEY,
    SOURCE_R2_SECRET_KEY,
    SOURCE_R2_BUCKET,
    DEST_R2_ENDPOINT,
    DEST_R2_ACCESS_KEY,
    DEST_R2_SECRET_KEY,
    DEST_R2_BUCKET,
    VIDEO_EXTENSIONS,
    IMAGE_EXTENSIONS,
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

def list_directory(s3_source, prefix=""):
    """
    Lists folders and files under a specific prefix in Source R2 bucket (filesystem-like navigation).
    Uses Delimiter='/' to split subdirectories and items.
    """
    prefix = (prefix or "").strip()
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    folders = []
    files = []

    paginator = s3_source.get_paginator("list_objects_v2")
    paginate_args = {"Bucket": SOURCE_R2_BUCKET, "Delimiter": "/"}
    if prefix:
        paginate_args["Prefix"] = prefix

    for page in paginator.paginate(**paginate_args):
        # Subfolders
        for cp in page.get("CommonPrefixes", []):
            folder_prefix = cp["Prefix"]
            folder_name = folder_prefix[len(prefix):].rstrip("/")
            folders.append({
                "name": folder_name,
                "prefix": folder_prefix,
            })

        # Files
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key == prefix or key.endswith("/"):
                continue

            file_name = key[len(prefix):]
            ext = Path(key).suffix.lower()
            is_video = ext in VIDEO_EXTENSIONS
            is_image = ext in IMAGE_EXTENSIONS

            files.append({
                "name": file_name,
                "key": key,
                "size": obj["Size"],
                "last_modified": obj["LastModified"].strftime("%Y-%m-%d %H:%M") if obj.get("LastModified") else "",
                "ext": ext.lstrip("."),
                "is_video": is_video,
                "is_image": is_image,
            })

    return {
        "prefix": prefix,
        "folders": sorted(folders, key=lambda x: x["name"].lower()),
        "files": sorted(files, key=lambda x: x["name"].lower()),
        "bucket": SOURCE_R2_BUCKET,
    }

def get_all_videos_under_targets(s3_source, keys=None, folders=None):
    """
    Gathers all video keys from selected individual files and recursive folders.
    """
    video_keys = []
    keys = keys or []
    folders = folders or []

    for k in keys:
        if Path(k).suffix.lower() in VIDEO_EXTENSIONS:
            if k not in video_keys:
                video_keys.append(k)

    paginator = s3_source.get_paginator("list_objects_v2")
    for folder in folders:
        f_prefix = folder.strip()
        if f_prefix and not f_prefix.endswith("/"):
            f_prefix += "/"
        for page in paginator.paginate(Bucket=SOURCE_R2_BUCKET, Prefix=f_prefix):
            for obj in page.get("Contents", []):
                k = obj["Key"]
                if k.endswith("/"):
                    continue
                if Path(k).suffix.lower() in VIDEO_EXTENSIONS:
                    if k not in video_keys:
                        video_keys.append(k)

    return video_keys

def delete_targets_from_r2(s3_source, keys=None, folders=None):
    """
    Recursively deletes selected files and folder contents from Source R2 bucket.
    """
    to_delete = list(keys or [])
    folders = folders or []

    paginator = s3_source.get_paginator("list_objects_v2")
    for folder in folders:
        f_prefix = folder.strip()
        if f_prefix and not f_prefix.endswith("/"):
            f_prefix += "/"
        for page in paginator.paginate(Bucket=SOURCE_R2_BUCKET, Prefix=f_prefix):
            for obj in page.get("Contents", []):
                to_delete.append(obj["Key"])

    unique_keys = list(set(to_delete))
    if not unique_keys:
        return 0

    log(f"Deleting {len(unique_keys)} object(s) from Source R2...")
    deleted_count = 0
    for i in range(0, len(unique_keys), 1000):
        chunk = unique_keys[i:i + 1000]
        delete_payload = {"Objects": [{"Key": k} for k in chunk], "Quiet": True}
        s3_source.delete_objects(Bucket=SOURCE_R2_BUCKET, Delete=delete_payload)
        deleted_count += len(chunk)

    log(f"Successfully deleted {deleted_count} object(s) from Source R2.")
    return deleted_count

def list_destination_thumbnails(s3_dest, prefix="new_thumbnails/"):
    """
    Lists all thumbnail images from Destination R2 bucket under the given prefix.
    """
    if not s3_dest:
        return []
    prefix = (prefix or "").strip()
    thumbnails = []
    paginator = s3_dest.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=DEST_R2_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                ext = Path(key).suffix.lower()
                if ext in IMAGE_EXTENSIONS:
                    thumbnails.append({
                        "key": key,
                        "name": Path(key).name,
                        "size": obj["Size"],
                        "last_modified": obj["LastModified"].strftime("%Y-%m-%d %H:%M") if obj.get("LastModified") else "",
                    })
    except Exception as e:
        log(f"Warning listing destination thumbnails: {e}")
    return sorted(thumbnails, key=lambda x: x["last_modified"], reverse=True)

def delete_from_dest_r2(s3_dest, key):
    """
    Deletes a thumbnail from Destination R2.
    """
    log(f"Deleting thumbnail from Destination R2: {key}")
    s3_dest.delete_object(Bucket=DEST_R2_BUCKET, Key=key)

_counts_cache = {"timestamp": 0, "data": None}

def get_bucket_counts(s3_source, s3_dest):
    """
    Returns quick counts of videos and images in Source R2, and thumbnails in Destination R2.
    Uses 60s cache to keep UI responsive.
    """
    import time
    now = time.time()
    if _counts_cache["data"] and (now - _counts_cache["timestamp"] < 60):
        return _counts_cache["data"]

    videos_count = 0
    images_count = 0
    thumbs_count = 0
    try:
        if s3_source:
            paginator = s3_source.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=SOURCE_R2_BUCKET):
                for obj in page.get("Contents", []):
                    k = obj["Key"]
                    if k.endswith("/"):
                        continue
                    ext = Path(k).suffix.lower()
                    if ext in VIDEO_EXTENSIONS:
                        videos_count += 1
                    elif ext in IMAGE_EXTENSIONS:
                        images_count += 1
        if s3_dest:
            paginator_dest = s3_dest.get_paginator("list_objects_v2")
            for page in paginator_dest.paginate(Bucket=DEST_R2_BUCKET, Prefix="new_thumbnails/"):
                for obj in page.get("Contents", []):
                    if not obj["Key"].endswith("/"):
                        thumbs_count += 1
    except Exception as e:
        log(f"Warning counting bucket objects: {e}")

    result = {
        "videos": videos_count,
        "images": images_count,
        "thumbnails": thumbs_count,
        "source_bucket": SOURCE_R2_BUCKET,
        "dest_bucket": DEST_R2_BUCKET
    }
    _counts_cache["timestamp"] = now
    _counts_cache["data"] = result
    return result

