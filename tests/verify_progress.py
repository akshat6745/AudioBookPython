import requests
import time
import threading
import uuid

BASE_URL = "http://localhost:8080"
NOVEL = "shadow-slave"
CHAPTER = 4
VOICE = "en-US-AvaMultilingualNeural"
DIALOGUE_VOICE = "en-GB-RyanNeural"
PROGRESS_ID = f"test_progress_{uuid.uuid4().hex}"

def poll_progress():
    print(f"Starting progress polling for ID: {PROGRESS_ID}")
    last_percent = -1
    while True:
        try:
            response = requests.get(f"{BASE_URL}/download/progress/{PROGRESS_ID}")
            if response.status_code == 200:
                data = response.json()
                percent = data.get("percent", 0)
                status = data.get("status")
                
                if percent != last_percent:
                    print(f"Progress Update: {percent}% (Status: {status})")
                    last_percent = percent
                
                if status in ["completed", "error"]:
                    print(f"Final Status: {status}")
                    break
            elif response.status_code == 404:
                # Might not be initialized yet
                pass
        except Exception as e:
            print(f"Polling error: {e}")
        
        time.sleep(0.5)

def start_download():
    print("Starting download...")
    url = f"{BASE_URL}/download-chapter/{NOVEL}/{CHAPTER}"
    params = {
        "voice": VOICE,
        "dialogue_voice": DIALOGUE_VOICE,
        "progress_id": PROGRESS_ID
    }
    try:
        with requests.get(url, params=params, stream=True) as r:
            r.raise_for_status()
            total_size = 0
            for chunk in r.iter_content(chunk_size=8192):
                total_size += len(chunk)
            print(f"Download complete. Total size: {total_size} bytes")
    except Exception as e:
        print(f"Download failed: {e}")

if __name__ == "__main__":
    # Start polling in a separate thread
    poll_thread = threading.Thread(target=poll_progress)
    poll_thread.start()
    
    # Start download in main thread
    start_download()
    
    # Wait for polling to finish
    poll_thread.join()
