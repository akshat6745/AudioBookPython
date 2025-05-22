# Novel Reader Backend

This is a Python backend service that provides APIs to:
1. Fetch novel names from a public Google Document
2. Fetch chapters for a specific novel from novelfire.net
3. Fetch content of a specific chapter
4. Convert text to speech using Edge TTS

## Local Development Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Create a `.env` file with:
```
SHEET_ID=1VRLgR_6cCJeXVh6N3IAiwrlxEmeDbc03CqqSZ_o57so
ENVIRONMENT=development
DEBUG=True
LOG_LEVEL=DEBUG
```

3. Run the server:
```bash
uvicorn main:app --reload
```

## API Endpoints

1. `GET /novels` - Fetch all novel names from the Google Document
2. `GET /chapters-with-pages/{novel_name}` - Fetch chapters for a specific novel with pagination
3. `GET /chapter` - Fetch content of a specific chapter (requires `chapterNumber` and `novelName` query parameters)
4. `POST /tts` - Convert text to speech using Edge TTS
5. `GET /health` - Health check endpoint (returns 200 OK if service is healthy)

## Deployment Options

### Docker Deployment

1. Make sure you have Docker and Docker Compose installed.

2. Create a `.env` file with your environment variables:
```
SHEET_ID=your_google_document_id_here
ENVIRONMENT=production
DEBUG=False
LOG_LEVEL=INFO
```

3. Build and start the Docker container:
```bash
docker-compose up -d
```

This will start the API service on port 8000.

### Heroku Deployment

This repository includes Heroku configuration files (Procfile and runtime.txt).

1. Install the Heroku CLI and log in:
```bash
heroku login
```

2. Create a new Heroku app:
```bash
heroku create your-app-name
```

3. Set required environment variables:
```bash
heroku config:set SHEET_ID=your_google_document_id_here
```

4. Deploy to Heroku:
```bash
git push heroku main
```

### Configuration Options

For all deployment methods, you can configure the application using environment variables:

- `SHEET_ID`: (Required) Google Document ID containing novel names
- `ENVIRONMENT`: `development` or `production` (default: `production`)
- `DEBUG`: `True` or `False` (default: `False`)
- `LOG_LEVEL`: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` (default: `INFO`)
- `PORT`: Port to run the server on (default: `8000`)
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