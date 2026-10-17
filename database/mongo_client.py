import hashlib
from pathlib import Path
from datetime import datetime
from pymongo import MongoClient
from config import MONGO_URI, MONGO_DB_NAME

mongo_client = MongoClient(MONGO_URI)
mongo_db = mongo_client[MONGO_DB_NAME]
metadata_collection = mongo_db["video_metadata"]
hashes_collection = mongo_db["video_hashes"]

def calculate_file_hash(file_path: Path) -> str:
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            sha256.update(chunk)
    return sha256.hexdigest()

def is_duplicate_video(file_hash: str, metadata: dict):
    # 1. Exact SHA-256 match in hashes or metadata collections
    if hashes_collection.find_one({"file_hash": file_hash}):
        return True, "Exact file hash (SHA-256) match in hashes"
    if metadata_collection.find_one({"file_hash": file_hash}):
        return True, "Exact file hash (SHA-256) match in metadata"

    # 2. Match by video technical metadata (duration, resolution, file size)
    duration = metadata.get("duration", 0.0)
    width = metadata.get("width", 0)
    height = metadata.get("height", 0)
    file_size = metadata.get("file_size", 0)

    if duration > 1.0 and width > 0 and height > 0 and file_size > 0:
        meta_query = {
            "width": width,
            "height": height,
            "duration": {"$gte": round(duration - 0.2, 2), "$lte": round(duration + 0.2, 2)},
            "file_size": {"$gte": int(file_size * 0.99), "$lte": int(file_size * 1.01)}
        }
        dup = metadata_collection.find_one(meta_query)
        if dup:
            return True, f"Video metadata match (Duration: {duration:.2f}s, Res: {width}x{height}, Size: {file_size} bytes)"

    return False, None

def save_video_metadata(metadata_doc: dict):
    metadata_collection.insert_one(metadata_doc)
    # Maintain video_hashes collection for backward compatibility
    hashes_collection.insert_one({
        "file_hash": metadata_doc["file_hash"],
        "slug": metadata_doc["slug"],
        "file_name": metadata_doc["title"],
        "created_at": metadata_doc.get("created_at", datetime.utcnow())
    })
