# Novel Database Integration Guide

This document describes how to query the Supabase database to retrieve novel data for the backend API.

---

## Database Overview

| Table | Purpose |
|-------|---------|
| `novels` | Stores novel metadata (title, author, genres, etc.) |
| `chapters` | Stores chapter content as an array of paragraphs |
| `pipeline_runs` | Scraping pipeline run logs (internal use) |

---

## Table Schemas

### `novels`

| Column | Type | Description |
|--------|------|-------------|
| `id` | `BIGSERIAL` | Primary key |
| `slug` | `VARCHAR(255)` | **Unique** URL-friendly identifier (e.g., `mother-of-learning`) |
| `title` | `VARCHAR(500)` | Novel title |
| `author` | `VARCHAR(255)` | Author name |
| `genres` | `TEXT[]` | Array of genre strings |
| `status` | `VARCHAR(50)` | e.g., `Completed`, `Ongoing`, `Unknown` |
| `description` | `TEXT` | Novel synopsis |
| `alternative_names` | `TEXT` | Alternate titles |
| `source_url` | `TEXT` | Original scrape source URL |
| `created_at` | `TIMESTAMPTZ` | Record creation time |
| `updated_at` | `TIMESTAMPTZ` | Last update time |

### `chapters`

| Column | Type | Description |
|--------|------|-------------|
| `id` | `BIGSERIAL` | Primary key |
| `novel_id` | `BIGINT` | Foreign key → `novels.id` |
| `chapter_number` | `INTEGER` | Chapter number (1-indexed) |
| `chapter_title` | `VARCHAR(500)` | Chapter title |
| `content` | `TEXT[]` | **Array of paragraphs** (each element is one paragraph) |
| `word_count` | `INTEGER` | Total word count |
| `source_url` | `TEXT` | Original chapter URL |
| `created_at` | `TIMESTAMPTZ` | Record creation time |
| `updated_at` | `TIMESTAMPTZ` | Last update time |

> [!IMPORTANT]
> The `content` column is a **PostgreSQL array** (`TEXT[]`), not a single text blob. Each element represents one paragraph, ready for frontend rendering or TTS processing.

---

## Common Queries

### Get All Novels

```sql
SELECT id, slug, title, author, genres, status, description
FROM novels
ORDER BY updated_at DESC;
```

### Get Novel by Slug

```sql
SELECT * FROM novels WHERE slug = 'mother-of-learning';
```

### Get Novel with Chapter Count

```sql
SELECT 
    n.*,
    COUNT(c.id) as total_chapters,
    MAX(c.chapter_number) as latest_chapter
FROM novels n
LEFT JOIN chapters c ON n.id = c.novel_id
WHERE n.slug = 'mother-of-learning'
GROUP BY n.id;
```

### Get Chapters for a Novel (Paginated)

```sql
SELECT id, chapter_number, chapter_title, word_count
FROM chapters
WHERE novel_id = (SELECT id FROM novels WHERE slug = 'mother-of-learning')
ORDER BY chapter_number ASC
LIMIT 50 OFFSET 0;
```

### Get Single Chapter with Content

```sql
SELECT chapter_number, chapter_title, content, word_count
FROM chapters
WHERE novel_id = (SELECT id FROM novels WHERE slug = 'mother-of-learning')
  AND chapter_number = 1;
```

The `content` field returns a PostgreSQL array. In your backend:

```python
# Python (psycopg2/asyncpg)
paragraphs = row['content']  # Already a list: ["para1", "para2", ...]

# Node.js (pg)
const paragraphs = row.content;  // Array: ["para1", "para2", ...]
```

---

## Supabase JS/Python Client Examples

### JavaScript

```javascript
import { createClient } from '@supabase/supabase-js'

const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY)

// Get all novels
const { data: novels } = await supabase
  .from('novels')
  .select('id, slug, title, author, genres, status')
  .order('updated_at', { ascending: false })

// Get chapters for a novel
const { data: chapters } = await supabase
  .from('chapters')
  .select('chapter_number, chapter_title, content, word_count')
  .eq('novel_id', novelId)
  .order('chapter_number', { ascending: true })
  .range(0, 49)  // Paginate: first 50
```

### Python

```python
from supabase import create_client

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Get all novels
novels = supabase.table('novels').select('*').order('updated_at', desc=True).execute()

# Get chapter content
chapter = supabase.table('chapters') \
    .select('chapter_number, chapter_title, content') \
    .eq('novel_id', novel_id) \
    .eq('chapter_number', 1) \
    .single() \
    .execute()

paragraphs = chapter.data['content']  # List of paragraph strings
```

---

## Indexes

The following indexes exist for optimized queries:

| Index | Columns |
|-------|---------|
| `idx_novels_slug` | `novels(slug)` |
| `idx_chapters_novel_id` | `chapters(novel_id)` |
| `idx_chapters_number` | `chapters(novel_id, chapter_number)` |

---

## Connection Details

Use these environment variables to connect:

```
SUPABASE_URL=https://ffkvptusuytvufhpxbrd.supabase.co
SUPABASE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZma3ZwdHVzdXl0dnVmaHB4YnJkIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2NzkzODU3NCwiZXhwIjoyMDgzNTE0NTc0fQ.uIcXrY4wMQr4plXf5qTUzuW_RiAz1LZ3pYMXRYDX60k

```

> [!NOTE]
> Use the **service role key** for backend operations. The anon key has RLS restrictions.

---

## Data Characteristics

- **Paragraph format**: Each paragraph in `content[]` is already cleaned (no HTML, ads removed)
- **Chapter ordering**: Always use `chapter_number` for sorting (1-indexed, sequential)
- **Unique constraint**: `(novel_id, chapter_number)` is unique per novel
