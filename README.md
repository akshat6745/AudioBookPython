# Novel Reader Backend

This is a Python backend service that provides APIs to:
1. Fetch novel names from a public Google Document
2. Fetch chapters for a specific novel from novelfire.net
3. Fetch content of a specific chapter
4. Convert text to speech using Edge TTS

## Local Development Setup

1. Setup virtual environment:
```bash
python3 -m venv .venv
source .venv/bin/activate  # On macOS/Linux
# or
.venv\Scripts\activate  # On Windows
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Configure environment variables:
Create a `.env` file in the root directory (you can copy `.env.example` as a starting point):
```
SHEET_ID=your_google_sheet_id
SUPABASE_URL=your_supabase_project_url
SUPABASE_KEY=your_supabase_anon_key
ENVIRONMENT=development
DEBUG=True
LOG_LEVEL=DEBUG
```

4. Start the development server:
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

## API Endpoints

1. `GET /novels` - Fetch all novel names from the Google Document
2. `GET /chapters-with-pages/{novel_name}` - Fetch chapters for a specific novel with pagination
3. `GET /chapter` - Fetch content of a specific chapter (requires `chapterNumber` and `novelName` query parameters)
4. `POST /tts` - Convert text to speech using Edge TTS
5. `GET /health` - Health check endpoint (returns 200 OK if service is healthy)

## Deployment

For detailed deployment instructions, please refer to [DEPLOYMENT.md](DEPLOYMENT.md).

### Render Deployment (Recommended)

This project includes a `render.yaml` Blueprint for easy deployment on Render.

1. Push your code to a GitHub repository.
2. Log in to [Render](https://render.com) and create a new **Blueprint Instance**.
3. Connect your repository.
4. Provide the required environment variables when prompted:
   - `SHEET_ID`
   - `SUPABASE_URL`
   - `SUPABASE_KEY`

Render will automatically build and deploy the application using the provided Dockerfile.

### Configuration Options

For all deployment methods, you can configure the application using environment variables:

- `SHEET_ID`: (Required) Google Document ID containing novel names
- `SUPABASE_URL`: (Required) Supabase Project URL
- `SUPABASE_KEY`: (Required) Supabase API Key (service role or anon key)
- `ENVIRONMENT`: `development` or `production` (default: `production`)
- `DEBUG`: `True` or `False` (default: `False`)
- `LOG_LEVEL`: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` (default: `INFO`)
- `PORT`: Port to run the server on (default: `8080`)
- `HOST`: Host to bind the server to (default: `0.0.0.0`)
- `DEFAULT_VOICE`: Default voice for TTS (default: `en-US-ChristopherNeural`)

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
- This API fetches data from novelfire.net.
- The text-to-speech functionality uses Edge TTS, which provides high-quality voice synthesis.