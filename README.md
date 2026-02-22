# Novel Reader Backend

This is a Python FastAPI backend service that provides APIs to:
1. Fetch novel metadata (from Cloudflare D1)
2. Fetch paginated chapter lists (from Cloudflare D1)
3. Fetch chapter content (text streamed from Cloudflare R2)
4. Convert text to speech using Edge TTS
5. Upload and parse EPUBs (metadata to D1/R2, images to Supabase)
6. Manage user authentication and reading progress (using Cloudflare D1)

## Architecture

The backend previously relied entirely on Supabase/PostgreSQL. It has now been migrated to a modern edge-ready architecture for massive scalability:
* **Cloudflare D1 (SQLite at the Edge)**: Stores `novels`, `chapters` metadata, `users`, and `user_progress`.
* **Cloudflare R2 (S3-compatible Object Storage)**: Stores chapter content as compressed `.txt.gz` files for massive cost reduction and speed.
* **Supabase**: Only kept for storing binary EPUB images (`epub_images` table).

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
```env
# Cloudflare D1 (Database)
CF_ACCOUNT_ID="your_cloudflare_account_id"
CF_API_TOKEN="your_cloudflare_api_token"
D1_DATABASE_ID="your_d1_database_uuid"

# Cloudflare R2 (Chapter Content Storage)
R2_ACCESS_KEY_ID="your_r2_access_key"
R2_SECRET_ACCESS_KEY="your_r2_secret_key"
R2_BUCKET_NAME="your_r2_bucket_name"
R2_ENDPOINT_URL="https://<CF_ACCOUNT_ID>.r2.cloudflarestorage.com"

# Supabase (EPUB Images)
SUPABASE_URL="your_supabase_project_url"
SUPABASE_KEY="your_supabase_anon_key"

# App
ENVIRONMENT="development"
DEBUG=True
```

4. Start the development server:
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

## API Endpoints

### Novels & Chapters
* `GET /novels` - Fetch all novels from D1
* `GET /chapters-with-pages/{novel_name}` - Fetch paginated chapter metadata
* `GET /chapter?chapterNumber=1&novelName=shadow-slave` - Fetch specific chapter content (from R2)

### Users & Progress
* `POST /userLogin` - Login user
* `POST /register` - Register a new user
* `POST /user/progress` - Save reading progress
* `GET /user/progress` - Get all reading progress
* `GET /user/progress/{novelName}` - Get progress for a specific novel

### EPUB & TTS
* `POST /upload-epub` - Upload and parse an EPUB file
* `GET /novel/{novel_id}/image/{image_id}` - Fetch images embedded in the EPUB
* `POST /tts` - Convert text to speech using Edge TTS
* `GET /health` - Health check

## Deployment (Render)

For deployment to Render:
1. Push your code to a GitHub repository.
2. Log in to [Render](https://render.com) and create a new **Web Service**.
3. Connect your repository.
4. Set the Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Provide the required environment variables:
   - `CF_ACCOUNT_ID`
   - `CF_API_TOKEN`
   - `D1_DATABASE_ID`
   - `R2_ACCESS_KEY_ID`
   - `R2_SECRET_ACCESS_KEY`
   - `R2_BUCKET_NAME`
   - `R2_ENDPOINT_URL`
   - `SUPABASE_URL`
   - `SUPABASE_KEY`

Render will automatically build and deploy the application using the configuration.

## Migration Tools

If you are migrating from the old Supabase Postgres architecture to Cloudflare, use the included one-off scripts:
* `python scripts/migrate_to_cloudflare.py` - Migrates novels and chapters from Supabase to D1 and R2
* `python scripts/migrate_users_to_d1.py` - Migrates `users_data` table from Supabase to D1 `users`/`user_progress` tables.