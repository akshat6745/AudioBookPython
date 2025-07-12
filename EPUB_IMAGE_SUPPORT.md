# EPUB Image Support Implementation

## Overview

The codebase has been updated to support extracting, storing, and serving images from EPUB files. Previously, only text content was processed, but now images are fully handled.

## What Was Added

### 1. Image Extraction (`app/services/epub_parser.py`)

- **New Function**: `extract_images_from_epub(book)` 
  - Extracts all images using `ebooklib.ITEM_IMAGE`
  - Converts images to base64 for storage
  - Generates unique UUIDs for each image
  - Captures metadata (filename, content type, size)

- **Enhanced Function**: `extract_chapter_content_with_images(soup, epub_images)`
  - Processes both text paragraphs and image references
  - Creates image placeholders in chapter content: `[IMAGE: Alt Text - ID: uuid]`
  - Returns both content and list of image IDs per chapter

- **Updated Function**: `parse_epub_content(epub_content)`
  - Now returns `(Novel, chapters, images)` instead of `(Novel, chapters)`
  - Integrates image extraction with chapter processing

### 2. Database Storage (`app/api/epub.py`)

- **Novel Document Enhancement**:
  - Added `hasImages: boolean` field
  - Added `imageCount: number` field

- **Images Subcollection**:
  - Each novel gets an `images` subcollection
  - Each image stored with metadata and base64 data

- **Chapter Enhancement**:
  - Added `images: string[]` field containing image IDs used in that chapter

### 3. API Endpoints (`app/api/epub.py`)

- **`GET /novel/{novel_id}/image/{image_id}`**
  - Serves individual images with proper content types
  - Includes caching headers for performance
  - Supports all common image formats (JPEG, PNG, GIF, SVG)

- **`GET /novel/{novel_id}/images`**
  - Lists all images in a novel with metadata
  - Returns image URLs, sizes, content types
  - Useful for building image galleries

### 4. Schema Updates (`app/models/schemas.py`)

- **Enhanced `NovelInfo`**:
  - Added `hasImages: Optional[bool]`
  - Added `imageCount: Optional[int]`

- **New `ImageInfo`**:
  - Schema for image metadata
  - Includes URL for accessing the image

- **New `NovelImagesResponse`**:
  - Response format for images list endpoint

## Database Structure

```
novels/
  {novel_id}/
    title: string
    author: string
    chapterCount: number
    hasImages: boolean          // NEW
    imageCount: number          // NEW
    source: "epub_upload"
    id: string
    
    chapters/
      {chapter_number}/
        chapterNumber: number
        chapterTitle: string
        content: string[]
        images: string[]        // NEW - array of image IDs
        id: string
    
    images/                     // NEW subcollection
      {image_id}/
        id: string (UUID)
        originalPath: string
        contentType: string
        size: number
        data: string (base64)
```

## How Images Work

### 1. Upload Process
1. EPUB file is uploaded via `/upload-epub`
2. `ebooklib.ITEM_IMAGE` items are extracted
3. Images converted to base64 and stored in Firestore
4. Chapters are processed, and image references are tracked
5. Image placeholders are inserted in chapter content

### 2. Chapter Display
- Chapter content includes image placeholders: `[IMAGE: Description - ID: uuid]`
- Frontend can parse these placeholders and replace with actual images
- Image URLs: `/novel/{novel_id}/image/{image_id}`

### 3. Image Serving
- Images are served with proper MIME types
- Cached for 1 hour for performance
- Base64 data is decoded on-the-fly

## Frontend Integration Examples

### Displaying Chapter with Images

```javascript
async function displayChapter(novelId, chapterNumber) {
    // Fetch chapter content
    const chapter = await fetch(`/chapter?novelName=${novelId}&chapterNumber=${chapterNumber}`)
        .then(r => r.json());
    
    // Process content and replace image placeholders
    const processedContent = chapter.content.map(paragraph => {
        // Check if paragraph is an image placeholder
        const imageMatch = paragraph.match(/\[IMAGE: (.*?) - ID: ([a-f0-9-]+)\]/);
        if (imageMatch) {
            const [, altText, imageId] = imageMatch;
            return `<img src="/novel/${novelId}/image/${imageId}" alt="${altText}" />`;
        }
        return `<p>${paragraph}</p>`;
    });
    
    document.getElementById('chapter-content').innerHTML = processedContent.join('');
}
```

### Image Gallery

```javascript
async function showNovelImages(novelId) {
    const response = await fetch(`/novel/${novelId}/images`);
    const data = await response.json();
    
    const gallery = data.images.map(img => 
        `<img src="${img.url}" alt="${img.originalPath}" 
              title="Size: ${img.size} bytes, Type: ${img.contentType}" />`
    ).join('');
    
    document.getElementById('image-gallery').innerHTML = gallery;
}
```

## Performance Considerations

1. **Storage**: Images are stored as base64 in Firestore
   - Pro: Simple implementation, no external storage needed
   - Con: Larger storage size (33% increase from base64 encoding)

2. **Caching**: Images include cache headers for 1 hour
   - Reduces server load for repeated requests
   - Consider longer cache times for production

3. **Future Improvements**:
   - Consider using Google Cloud Storage for large images
   - Implement image resizing/optimization
   - Add lazy loading for chapter images

## Migration Notes

- Existing EPUB novels without images will continue to work
- New `hasImages` and `imageCount` fields default to `false` and `0`
- No breaking changes to existing API contracts

## Testing

To test the image functionality:

1. Upload an EPUB file with images using `/upload-epub`
2. Check the response includes image count
3. Fetch images list: `GET /novel/{novel_id}/images`
4. Retrieve specific image: `GET /novel/{novel_id}/image/{image_id}`
5. Verify chapter content includes image placeholders

## Security Considerations

- Images are served with `inline` content disposition (display in browser)
- Content types are validated and stored
- No user-uploaded paths are directly served
- All images go through the API endpoint with proper authentication context
