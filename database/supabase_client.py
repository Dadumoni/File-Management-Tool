import string
import secrets
import requests
from config import SUPABASE_URL, SUPABASE_KEY

def log(msg):
    import time
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def supabase_init_table():
    try:
        url = f"{SUPABASE_URL}/rest/v1/all_videos_db?limit=1"
        resp = requests.get(url, headers=supabase_headers(), timeout=15)
        if resp.status_code in (200, 206):
            log("Supabase table 'all_videos_db' verified and ready.")
        else:
            log(f"Supabase check returned status {resp.status_code}: {resp.text}")
    except Exception as e:
        log(f"Supabase connection check failed: {e}")

def generate_random_slug(length: int = 10) -> str:
    """Generates an uppercase alphanumeric slug like JSBF7EB48B"""
    chars = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

def supabase_get_next_slug() -> str:
    """
    Generates a unique 10-character uppercase alphanumeric slug (e.g. JSBF7EB48B)
    and verifies uniqueness against all_videos_db in Supabase.
    """
    for _ in range(30):
        slug_candidate = generate_random_slug(10)
        url = f"{SUPABASE_URL}/rest/v1/all_videos_db?slug=eq.{slug_candidate}&select=slug&limit=1"
        try:
            resp = requests.get(url, headers=supabase_headers(), timeout=10)
            if resp.status_code == 200:
                rows = resp.json()
                if not rows:
                    return slug_candidate
        except Exception:
            return slug_candidate
    return generate_random_slug(10)

def supabase_insert_video(record: dict):
    url = f"{SUPABASE_URL}/rest/v1/all_videos_db"
    resp = requests.post(url, headers=supabase_headers(), json=record, timeout=30)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Supabase insert failed: {resp.status_code} {resp.text}")
    log(f"Supabase record saved for slug: {record.get('slug')}")

def supabase_get_processed_titles() -> set:
    url = f"{SUPABASE_URL}/rest/v1/all_videos_db?select=title"
    try:
        resp = requests.get(url, headers=supabase_headers(), timeout=30)
        if resp.status_code == 200:
            rows = resp.json()
            return {row["title"] for row in rows if row.get("title")}
    except Exception as e:
        log(f"Error reading processed titles from Supabase: {e}")
    return set()
