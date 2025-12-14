import sys
import os
import argparse
import time

# Add tests directory to path to import verify_chapter
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from verify_download import verify_chapter

def run_suite(port=8080):
    test_cases = [
        {"novel": "shadow-slave", "chapter": 1},
        {"novel": "shadow-slave", "chapter": 2720},
        {"novel": "lord-of-the-mysteries", "chapter": 1},
        {"novel": "the-beginning-after-the-end", "chapter": 100}
    ]

    results = []
    print(f"Starting Integration Test Suite on port {port}...")
    print("=" * 60)

    for case in test_cases:
        novel = case["novel"]
        chapter = case["chapter"]
        print(f"\n[TEST] {novel} - Chapter {chapter}")
        
        start_time = time.time()
        # Use verify_chapter from verify_download.py
        # It handles the downloading and verification logic
        success = verify_chapter(novel, chapter, port=port)
        duration = time.time() - start_time
        
        status = "PASS" if success else "FAIL"
        results.append({
            "case": f"{novel} #{chapter}",
            "status": status,
            "duration": duration
        })
        
        if success:
            print(f"Result: {status} ({duration:.2f}s)")
        else:
            print(f"Result: {status} ({duration:.2f}s) - CHECK LOGS ABOVE")
        
        # Small delay to be nice to the server/external APIs
        time.sleep(1)

    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    all_passed = True
    for res in results:
        print(f"{res['status']:<4} | {res['case']:<40} | {res['duration']:.2f}s")
        if res['status'] != "PASS":
            all_passed = False
    
    print("-" * 60)
    if all_passed:
        print("SUITE PASSED")
        sys.exit(0)
    else:
        print("SUITE FAILED")
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run integration tests")
    parser.add_argument("--port", type=int, default=8080, help="Server port")
    args = parser.parse_args()
    
    run_suite(port=args.port)
