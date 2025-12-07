import requests
import zipfile
import io
import json
import sys
import argparse

def verify_chapter(novel, chapter, port=8080, voice="en-US-ChristopherNeural", dialogue_voice="en-US-JennyNeural"):
    url = f"http://localhost:{port}/download-chapter/{novel}/{chapter}"
    params = {
        "voice": voice,
        "dialogue_voice": dialogue_voice,
        "progress_id": f"verify_{chapter}"
    }
    
    print(f"Downloading {novel} chapter {chapter} from port {port}...")
    try:
        response = requests.get(url, params=params, stream=True)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"FAIL: Request failed for chapter {chapter}: {e}")
        return False

    try:
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            namelist = z.namelist()
            
            # 1. Check content.json
            if "content.json" not in namelist:
                print(f"FAIL: content.json missing in chapter {chapter}")
                return False
            
            with z.open("content.json") as f:
                content = json.load(f)
            
            paragraphs = content.get("paragraphs", [])
            num_paragraphs = len(paragraphs)
            print(f"  - Found {num_paragraphs} paragraphs in content.json")
            
            # 2. Check title audio
            if "audio/title.mp3" not in namelist:
                 print(f"FAIL: audio/title.mp3 missing in chapter {chapter}")
                 return False

            # 3. Check paragraph audios
            missing_audio = []
            for i in range(num_paragraphs):
                audio_file = f"audio/{i}.mp3"
                if audio_file not in namelist:
                    missing_audio.append(audio_file)
            
            if missing_audio:
                print(f"FAIL: Missing {len(missing_audio)} audio files in chapter {chapter}: {missing_audio[:5]}...")
                return False
            
            print(f"PASS: Chapter {chapter} verified ({num_paragraphs} paragraphs, all audio present).")
            return True

    except zipfile.BadZipFile:
        print(f"FAIL: Invalid ZIP received for chapter {chapter}")
        check_progress_error(chapter, port)
        return False
    except Exception as e:
        print(f"FAIL: Error verifying chapter {chapter}: {e}")
        check_progress_error(chapter, port)
        return False

def check_progress_error(chapter, port):
    try:
        url = f"http://localhost:{port}/download/progress/verify_{chapter}"
        resp = requests.get(url)
        if resp.status_code == 200:
            data = resp.json()
            print(f"DEBUG: Progress status for chapter {chapter}: {json.dumps(data, indent=2)}")
        else:
             print(f"DEBUG: Could not fetch progress (Status {resp.status_code})")
    except Exception as e:
        print(f"DEBUG: Error fetching progress: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify chapter downloads")
    parser.add_argument("--novel", default="shadow-slave", help="Novel name")
    parser.add_argument("--start", type=int, default=2720, help="Start chapter")
    parser.add_argument("--end", type=int, help="End chapter (inclusive), defaults to start if not provided")
    parser.add_argument("--port", type=int, default=8080, help="Server port")
    
    args = parser.parse_args()
    end_chapter = args.end if args.end else args.start
    
    success_count = 0
    total_count = 0
    
    for ch in range(args.start, end_chapter + 1):
        total_count += 1
        if verify_chapter(args.novel, ch, port=args.port):
            success_count += 1
            
    print(f"\nSummary: {success_count}/{total_count} chapters verified successfully.")
