# Novel Reader API Documentation

## Version
Current API Version: 2.0

## Base URL
```
http://localhost:8080
```

---

## Endpoints

### 1. Health Check

#### GET `/health`
Get the health status of the API service.

**Response:**
```json
{
  "status": "healthy",
  "env": "development"
}
```

---

### 2. Novel Management

#### GET `/novels`
Returns a list of all available novels from Supabase database and uploaded EPUBs.

**Response:**
```json
[
  {
    "id": "23",
    "slug": "mother-of-learning",
    "title": "Mother of Learning",
    "author": "nobody103",
    "chapterCount": 112,
    "source": "supabase",
    "status": "Completed",
    "genres": ["Fantasy", "Adventure", "Action"],
    "description": "Zorian is a teenage mage of humble birth...",
    "hasImages": false,
    "imageCount": 0
  },
  {
    "id": "firebase-doc-id",
    "slug": null,
    "title": "My Uploaded Novel",
    "author": "Author Name",
    "chapterCount": 85,
    "source": "epub_upload",
    "status": null,
    "genres": null,
    "description": null,
    "hasImages": true,
    "imageCount": 5
  }
]
```

**Fields:**
| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique identifier |
| `slug` | string\|null | URL-friendly identifier (use this for API calls) |
| `title` | string | Novel title |
| `author` | string\|null | Author name |
| `chapterCount` | int\|null | Total number of chapters |
| `source` | string | `"supabase"` or `"epub_upload"` |
| `status` | string\|null | e.g., "Completed", "Ongoing", "Unknown" |
| `genres` | string[]\|null | Array of genre tags |
| `description` | string\|null | Novel synopsis |
| `hasImages` | bool | Whether novel contains images (EPUB only) |
| `imageCount` | int | Number of images (EPUB only) |

> **Important:** For Supabase novels, use the `slug` field for all subsequent API calls. For EPUB novels, use the `title` field.

---

#### POST `/upload-epub`
Upload and parse an EPUB file for storage.

**Request:** `multipart/form-data` with key `file` containing the EPUB file.

**Response:**
```json
{
  "title": "Novel Title",
  "author": "Author Name",
  "chapterCount": 85,
  "message": "Novel successfully uploaded and stored"
}
```

---

### 3. Chapter Management

#### GET `/chapters-with-pages/{novel_identifier}`
Get a paginated list of chapters for a novel.

**Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `novel_identifier` | path | Novel `slug` (Supabase) or `title` (EPUB) |
| `page` | query | Page number (default: 1, 100 chapters per page) |

**Example:** `GET /chapters-with-pages/mother-of-learning?page=1`

**Response:**
```json
{
  "chapters": [
    {
      "chapterNumber": 112,
      "chapterTitle": "Chapter 112 - The AU Chapter - Grand Whistler",
      "id": "15293",
      "wordCount": 8094
    },
    {
      "chapterNumber": 111,
      "chapterTitle": "Chapter 111 - The AU Chapter - The Fourth Looper",
      "id": "15397",
      "wordCount": 8512
    }
  ],
  "total_pages": 2,
  "current_page": 1
}
```

> **Note:** Chapters are returned in **ascending order** (chapter 1 first). `wordCount` is only available for Supabase novels.

---

#### GET `/chapter`
Get the content of a specific chapter.

**Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `novelName` | query | Novel `slug` (Supabase) or `title` (EPUB) |
| `chapterNumber` | query | Chapter number (1-indexed) |

**Example:** `GET /chapter?novelName=mother-of-learning&chapterNumber=1`

**Response:**
```json
{
  "chapterNumber": 1,
  "chapterTitle": "Chapter 1 - Good Morning Brother",
  "content": [
    "Zorian's eyes abruptly shot open as a sharp pain erupted from his stomach.",
    "\"Good morning, brother!\" an annoyingly cheerful voice sounded right on top of him.",
    "Zorian glared at his little sister, but she just smiled back at him cheekily..."
  ]
}
```

> **Note:** The `content` array contains paragraphs ready for display or TTS processing. No HTML parsing required.

---

### 4. Text-to-Speech

#### POST `/tts`
Convert text to speech using Edge TTS.

**Request:**
```json
{
  "text": "Text to convert to speech",
  "voice": "en-US-ChristopherNeural"
}
```

**Response:** Audio file (`audio/mp3`)

---

#### POST `/tts-dual-voice`
Convert text to speech with separate voices for narration and dialogue.

**Request:**
```json
{
  "text": "He said \"Hello there!\" and walked away.",
  "paragraphVoice": "en-US-ChristopherNeural",
  "dialogueVoice": "en-US-AriaNeural"
}
```

**Response:** Audio file (`audio/mp3`)

---

#### GET `/novel-with-tts`
Fetch a chapter and convert it to speech with dual voices. Streams the audio.

**Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `novelName` | query | Novel `slug` or `title` |
| `chapterNumber` | query | Chapter number |
| `voice` | query | Voice for narration |
| `dialogueVoice` | query | Voice for dialogue |

**Example:** 
```
GET /novel-with-tts?novelName=mother-of-learning&chapterNumber=1&voice=en-US-AvaMultilingualNeural&dialogueVoice=en-GB-RyanNeural
```

**Response:** Streaming audio file (`audio/mp3`)

---

### 5. Chapter Download

#### GET `/download-chapter/{novel_name}/{chapter_number}`
Download a chapter as a ZIP file containing content and audio files.

**Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `novel_name` | path | Novel `slug` or `title` |
| `chapter_number` | path | Chapter number |
| `voice` | query | Voice for narration |
| `dialogue_voice` | query | Voice for dialogue |
| `progress_id` | query | Optional ID to track download progress |

**Response:** ZIP file containing:
- `content.json` - Chapter metadata and paragraphs
- `audio/title.mp3` - Chapter title audio
- `audio/0.mp3`, `audio/1.mp3`, ... - Paragraph audio files

---

#### GET `/download/progress/{progress_id}`
Get the progress of an ongoing download.

**Response:**
```json
{
  "status": "processing",
  "total": 50,
  "current": 25,
  "percent": 50.0
}
```

---

### 6. User Management

#### POST `/userLogin`
Login with username and password.

**Request:**
```json
{
  "username": "user",
  "password": "pass"
}
```

**Response:**
```json
{
  "status": "success",
  "message": "Login successful"
}
```

---

#### POST `/register`
Register a new user.

**Request:**
```json
{
  "username": "user",
  "password": "pass"
}
```

**Response:**
```json
{
  "status": "success",
  "message": "User registered successfully"
}
```

---

#### POST `/user/progress`
Save user's reading progress for a novel.

**Request:**
```json
{
  "username": "user",
  "novelName": "mother-of-learning",
  "lastChapterRead": 10
}
```

**Response:**
```json
{
  "status": "success",
  "message": "Progress saved"
}
```

---

#### GET `/user/progress`
Get all reading progress for a user.

**Parameters:** `username` (query)

**Response:**
```json
{
  "progress": [
    {
      "novelName": "mother-of-learning",
      "lastChapterRead": 10
    }
  ]
}
```

---

#### GET `/user/progress/{novelName}`
Get user's reading progress for a specific novel.

**Parameters:** 
- `novelName` (path)
- `username` (query)

**Response:**
```json
{
  "novelName": "mother-of-learning",
  "lastChapterRead": 10
}
```

---

## Error Responses

The API uses standard HTTP status codes:

```json
{
  "detail": "Error message describing what went wrong"
}
```

| Code | Meaning |
|------|---------|
| 400 | Bad Request (invalid input) |
| 401 | Unauthorized (invalid login) |
| 404 | Not Found (novel/chapter/user not found) |
| 500 | Internal Server Error |
| 504 | Gateway Timeout (TTS generation timeout) |

---

## Data Sources

| Source | Identifier | Description |
|--------|------------|-------------|
| **Supabase** | `slug` | Primary novel database with pre-parsed content |
| **EPUB Upload** | `title` | User-uploaded EPUB files stored in Firebase |

### Database Structure

**Supabase (novels & chapters):**
```
novels: id, slug, title, author, genres, status, description
chapters: id, novel_id, chapter_number, chapter_title, content (TEXT[]), word_count
```

**Firebase (users & EPUB uploads):**
```
users/{user_id}: username, password, progress[]
novels/{novel_id}: title, author, chapterCount, chapters/{chapter_number}
```

---

## Frontend Integration Notes

1. **Novel List:** 
   - Store the `slug` field for Supabase novels to use in subsequent API calls
   - For EPUB novels (`source: "epub_upload"`), use the `title` field instead

2. **Chapter Content:**
   - Content is returned as an array of paragraphs - no HTML parsing needed
   - Each paragraph can be rendered directly or sent for TTS

3. **Progress Tracking:**
   - Use the novel's `slug` (or `title` for EPUB) as `novelName` when saving progress

4. **Available Voices:**
   - Use `en-US-AvaMultilingualNeural`, `en-US-ChristopherNeural`, `en-GB-RyanNeural`, etc.
   - Full list available via Edge TTS documentation
