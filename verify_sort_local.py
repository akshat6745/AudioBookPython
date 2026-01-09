import asyncio
import aiohttp
import json

async def verify_local_sort():
    url = "http://localhost:8080/chapters-with-pages/shadow-slave?page=1"
    print(f"Checking {url}")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    print(f"Error: {resp.status}")
                    text = await resp.text()
                    print(text)
                    return

                data = await resp.json()
                chapters = data.get("chapters", [])
                print(f"Received {len(chapters)} chapters")
                
                if not chapters:
                    print("No chapters returned")
                    return

                # Check sorting structure: Latest + Ascending
                # Expected: chapters[0] is the latest (MAX)
                #           chapters[1:] are sorted ASCENDING
                
                nums = [c['chapterNumber'] for c in chapters]
                
                print(f"First 10 numbers: {nums[:10]}")
                print(f"Last 10 numbers: {nums[-10:]}")
                
                if len(nums) < 2:
                    print("Not enough chapters to verify structure")
                    return

                # 1. Check Latest Chapter Pin
                latest_num = nums[0]
                first_page_num = nums[1]
                
                if latest_num < first_page_num:
                    print(f"FAILURE: Latest chapter pin check failed. {latest_num} is not > {first_page_num}")
                else:
                     print(f"SUCCESS: Latest chapter pinned correctly ({latest_num}).")

                # 2. Check Ascending Order for the rest
                rest_nums = nums[1:]
                is_sorted_asc = True
                fail_idx = -1
                
                for i in range(len(rest_nums) - 1):
                    if rest_nums[i] > rest_nums[i+1]:
                        is_sorted_asc = False
                        fail_idx = i
                        print(f"Sort violation at rest_index {i} (absolute {i+2}): {rest_nums[i]} > {rest_nums[i+1]}")
                        break
                
                if is_sorted_asc:
                    print("SUCCESS: Remaining chapters are sorted ASCENDING.")
                else:
                    print("FAILURE: Remaining chapters are NOT sorted ascending.")

    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    asyncio.run(verify_local_sort())
