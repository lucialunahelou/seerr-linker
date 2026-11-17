import os
import sys
import logging
import re
import time
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

# --- Configuration Mapping ---
# Radarr/Sonarr download locations (Invisible to Jellyfin)
ALL_MOVIES = os.environ.get("ALL_MOVIES", "/data/media/all_movies")
ALL_SERIES = os.environ.get("ALL_SERIES", "/data/media/all_series")

# User locations (Mounted to Jellyfin)
MAIN_MOVIES = os.environ.get("MAIN_MOVIES", "/data/media/movies")
MAIN_SERIES = os.environ.get("MAIN_SERIES", "/data/media/series")
GUEST_MOVIES = os.environ.get("GUEST_MOVIES", "/data/media/guest_movies")
GUEST_SERIES = os.environ.get("GUEST_SERIES", "/data/media/guest_series")

# Define which Seerr username triggers the guest folders
GUEST_USERNAMES = [u.strip().lower() for u in os.environ.get("GUEST_USERNAMES", "guest").split(",")]

# --- Helper Functions ---
def is_strict_match(title: str, folder_name: str) -> bool:
    t_clean = title.lower().strip()
    f_clean = folder_name.lower().strip()
    if t_clean == f_clean: 
        return True
    pattern = r"^" + re.escape(t_clean) + r"( \(\d{4}\))?$"
    return bool(re.match(pattern, f_clean))

def wait_for_media(source_pool, subject, timeout=600):
    """Polls until the media file/folder exists in the ALL pool."""
    start_time = time.time()
    logging.info(f"Waiting up to 10m for '{subject}' to appear in {source_pool}...")
    
    while time.time() - start_time < timeout:
        if not os.path.exists(source_pool):
            time.sleep(5)
            continue
            
        for entry in os.listdir(source_pool):
            if subject and is_strict_match(subject, entry):
                logging.info(f"Media '{entry}' found. Proceeding with hardlink.")
                return entry
                
        time.sleep(5)
        
    logging.error(f"Timeout: '{subject}' never appeared in {source_pool}.")
    return None

def create_hardlinks_recursive(src: str, dst: str):
    if os.path.isfile(src):
        if not os.path.exists(dst):
            os.link(src, dst)
            logging.info(f"Hardlinked: {src} -> {dst}")
        return

    if os.path.isdir(src):
        os.makedirs(dst, exist_ok=True)
        for item in os.listdir(src):
            create_hardlinks_recursive(os.path.join(src, item), os.path.join(dst, item))

# --- Async Workers ---
def process_seerr_async(subject, media_type, request_user):
    is_guest = request_user.lower() in GUEST_USERNAMES
    
    if media_type == "tv":
        source_pool = ALL_SERIES
        target_dir = GUEST_SERIES if is_guest else MAIN_SERIES
    else:
        source_pool = ALL_MOVIES
        target_dir = GUEST_MOVIES if is_guest else MAIN_MOVIES

    matched_entry = wait_for_media(source_pool, subject)
    if not matched_entry: return

    src_path = os.path.join(source_pool, matched_entry)
    dst_path = os.path.join(target_dir, matched_entry)

    try:
        if os.path.isdir(src_path):
            os.makedirs(dst_path, exist_ok=True)
        create_hardlinks_recursive(src_path, dst_path)
    except OSError as e:
        logging.error(f"Error linking {matched_entry}: {e}")

def process_sonarr_async(series_title):
    source_pool = ALL_SERIES
    matched_entry = wait_for_media(source_pool, series_title)
    if not matched_entry: return

    src_path = os.path.join(source_pool, matched_entry)
    
    # Iterate through users and hardlink the new episode if they already have the series folder
    for target_base in [MAIN_SERIES, GUEST_SERIES]:
        target_series_dir = os.path.join(target_base, matched_entry)
        if os.path.exists(target_series_dir):
            try:
                create_hardlinks_recursive(src_path, target_series_dir)
                logging.info(f"Appended new episodes to user folder {target_base}")
            except OSError as e:
                logging.error(f"Error updating {target_series_dir}: {e}")

# --- Garbage Collector ---
def garbage_collector():
    """Runs once a week to purge files with only 1 hardlink from the ALL folders."""
    while True:
        time.sleep(604800)  # 7 days
        logging.info("Running weekly garbage collection...")
        
        for pool in [ALL_MOVIES, ALL_SERIES]:
            if not os.path.exists(pool): continue
            
            for root, dirs, files in os.walk(pool, topdown=False):
                for name in files:
                    filepath = os.path.join(root, name)
                    try:
                        stat = os.stat(filepath)
                        # st_nlink == 1 means no hardlinks exist in Main or Guest folders
                        if stat.st_nlink == 1:
                            os.remove(filepath)
                            logging.info(f"Purged orphaned file: {filepath}")
                    except OSError:
                        pass
                        
                for name in dirs:
                    dirpath = os.path.join(root, name)
                    if not os.listdir(dirpath):
                        os.rmdir(dirpath)
                        logging.info(f"Purged empty folder: {dirpath}")

# --- Endpoints ---
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"}), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    if not data: return jsonify({"error": "Invalid JSON"}), 400

    if data.get("notification_type") != "MEDIA_AVAILABLE" and data.get("event") != "media_available":
        return jsonify({"message": "Event ignored"}), 200

    request_user = data.get("request", {}).get("requestedBy_username") or data.get("requestedBy_username") or data.get("user", {}).get("username")
    if not request_user: return jsonify({"error": "Missing user"}), 400

    media_type = data.get("media", {}).get("media_type", "movie").lower()
    subject = data.get("subject") or data.get("media", {}).get("title")

    # Pass to background thread so Seerr doesn't timeout
    threading.Thread(target=process_seerr_async, args=(subject, media_type, request_user), daemon=True).start()
    return jsonify({"status": "processing", "message": "Request accepted, checking for file..."}), 202

@app.route("/sonarr", methods=["POST"])
def sonarr_webhook():
    data = request.get_json(silent=True)
    if not data or data.get("eventType") != "Download":
        return jsonify({"message": "Ignored"}), 200

    series_title = data.get("series", {}).get("title")
    if not series_title: return jsonify({"error": "Missing series title"}), 400

    threading.Thread(target=process_sonarr_async, args=(series_title,), daemon=True).start()
    return jsonify({"status": "processing", "message": "Sonarr update accepted..."}), 202

if __name__ == "__main__":
    threading.Thread(target=garbage_collector, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
