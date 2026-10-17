import os
import time
import threading
import requests
from config import APP_URL

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def _auto_ping_worker():
    # Wait 60 seconds after server start before initial ping
    time.sleep(60)
    while True:
        port = int(os.getenv("PORT", 8080))
        target_url = (APP_URL or "").strip()
        if not target_url:
            target_url = f"http://127.0.0.1:{port}/health"
        elif not target_url.endswith("/health") and not target_url.endswith("/ping"):
            target_url = target_url.rstrip("/") + "/health"

        try:
            resp = requests.get(target_url, timeout=10)
            log(f"[Auto-Ping] Pinged {target_url} | Status: {resp.status_code}")
        except Exception as e:
            log(f"[Auto-Ping] Ping attempt warning: {e}")

        # Sleep for 10 minutes (600 seconds)
        time.sleep(10 * 60)

def start_auto_ping():
    """Starts background daemon thread that periodically pings the server to keep it alive."""
    t = threading.Thread(target=_auto_ping_worker, daemon=True, name="AutoPingWorker")
    t.start()
    log("✔ Auto-ping keep-alive background worker initialized (Interval: 10m).")

