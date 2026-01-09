import asyncio
import aiohttp
import re
import json

async def verify():
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    novel_name = "shadow-slave"
    
    async with aiohttp.ClientSession() as session:
        # 1. Get post_id
        print(f"Fetching main page for {novel_name}...")
        async with session.get(f"https://novelfire.net/book/{novel_name}/chapters", headers=headers) as resp:
            html = await resp.text()
            match = re.search(r'post_id=(\d+)', html)
            if not match:
                print("Could not find post_id")
                return
            post_id = match.group(1)
            print(f"Found post_id: {post_id}")

        # 2. Test current behavior (no sort)
        print("\n--- Current Behavior (Page 1, Length 50) ---")
        url_current = f"https://novelfire.net/listChapterDataAjax?post_id={post_id}&draw=1&start=0&length=50"
        async with session.get(url_current, headers=headers) as resp:
            data = await resp.json()
            chapters = data.get('data', [])
            print(f"Total Records: {data.get('recordsTotal')}")
            print("First 5 chapters:")
            for ch in chapters[:5]:
                print(f"  {ch.get('n_sort')}: {ch.get('title')}")

        # 3. Test with Sort - Attempt 2 (With column defs)
        print("\n--- With Sort (Page 1, Length 100, Sort by Col n_sort DESC) ---")
        # constructing params properly
        params = {
            "post_id": post_id,
            "draw": "1",
            "start": "0",
            "length": "100",
            "order[0][column]": "0",
            "order[0][dir]": "desc",
            "columns[0][data]": "n_sort",
            "columns[0][name]": "n_sort",
            "columns[0][searchable]": "true",
            "columns[0][orderable]": "true"
        }
        
        try:
            async with session.get("https://novelfire.net/listChapterDataAjax", params=params, headers=headers) as resp:
                print(f"Status: {resp.status}")
                if resp.status == 200:
                    data = await resp.json()
                    chapters = data.get('data', [])
                    print(f"Got {len(chapters)} chapters.")
                    print("First 5 chapters:")
                    for ch in chapters[:5]:
                        print(f"  {ch.get('n_sort')}: {ch.get('title')}")
                    
                    # Verify first is greater than second
                    if len(chapters) > 1:
                        n1 = int(chapters[0].get('n_sort'))
                        n2 = int(chapters[1].get('n_sort'))
                        print(f"Sort check: {n1} > {n2} is {n1 > n2}")

                else:
                    text = await resp.text()
                    print(f"Error: {text[:200]}")
        except Exception as e:
            print(f"Exception during sort test: {e}")

        # 4. Test Page Size - Large (Check if we can get ALL)
        print("\n--- Large Page Size (Length 3000) ---")
        try:
            params_large = {
                "post_id": post_id,
                "draw": "1",
                "start": "0",
                "length": "3000"
            }
            async with session.get("https://novelfire.net/listChapterDataAjax", params=params_large, headers=headers) as resp:
                data = await resp.json()
                chapters = data.get('data', [])
                print(f"Returned {len(chapters)} chapters")
                if len(chapters) > 0:
                     print(f"  First: {chapters[0].get('n_sort')}")
                     print(f"  Last: {chapters[-1].get('n_sort')}")
                
                # Check if they are sorted?
                is_sorted = True
                last_n = -1
                for ch in chapters:
                    n = int(ch.get('n_sort', -1) or -1)
                    if n < last_n:
                         is_sorted = False
                         # print(f"Unsorted at {n} after {last_n}")
                         # break
                    last_n = n
                print(f"Is Sorted natively? {is_sorted}")

        except Exception as e:
            print(f"Exception during large fetch: {e}")

if __name__ == "__main__":
    asyncio.run(verify())
