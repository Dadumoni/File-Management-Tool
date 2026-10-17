import os
import time
import requests
from flask import Flask, jsonify
from config import APP_URL

app = Flask(__name__)

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

@app.route("/")
@app.route("/health")
def health_check():
    return jsonify({"status": "healthy", "service": "R2 Video Processor"}), 200

def run_health_server():
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def auto_ping():
    while True:
        time.sleep(15 * 60)
        ping_url = APP_URL if APP_URL else "http://127.0.0.1:8080/health"
        try:
            resp = requests.get(ping_url, timeout=10)
            log(f"[Auto-Ping] Pinged {ping_url} | Status: {resp.status_code}")
        except Exception as e:
            log(f"[Auto-Ping] Ping failed: {e}")
