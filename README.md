# Novel Reader Backend

This is a Python backend service that provides APIs to:
1. Fetch novel names from a public Google Document
2. Fetch chapters for a specific novel from pandanovel.org
3. Fetch content of a specific chapter from pandanovel.org
4. Convert text to speech using Edge TTS

## Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Create a `.env` file with:
```
SHEET_ID=1VRLgR_6cCJeXVh6N3IAiwrlxEmeDbc03CqqSZ_o57so
```

3. Run the server:
```bash
uvicorn main:app --reload
```

## API Endpoints

1. `GET /novels` - Fetch all novel names from the Google Document
2. `GET /chapters/{novel_name}` - Fetch chapters for a specific novel
3. `GET /chapter` - Fetch content of a specific chapter (requires `link` query parameter)
4. `POST /tts` - Convert text to speech using Edge TTS

## Testing Edge TTS

You can test the Edge TTS functionality directly without running the full server:

```bash
python test_edge_tts.py
```

This will generate a test audio file named `test_direct.mp3` in the current directory.

You can also test the TTS API endpoint by running the server and then:

```bash
python test_tts.py
```

Or open the `tts_test.html` file in a browser while the server is running to test the TTS functionality through a web interface.

## Notes
- The Google Document must be publicly accessible (anyone with the link can view).
- This API fetches data from pandanovel.org. Previously it was using novelbin.me, but has been updated to use pandanovel.org.
- The API endpoints remain the same, but the underlying implementation has changed.
- The text-to-speech functionality uses Edge TTS, which provides high-quality voice synthesis.