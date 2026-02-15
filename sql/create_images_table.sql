-- SQL to create the epub_images table in Supabase

CREATE TABLE IF NOT EXISTS public.epub_images (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    novel_id UUID REFERENCES public.novels(id) ON DELETE CASCADE,
    image_id TEXT NOT NULL,
    original_path TEXT,
    content_type TEXT,
    size INTEGER,
    data TEXT, -- Base64 encoded image data
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(novel_id, image_id)
);

-- Enable RLS
ALTER TABLE public.epub_images ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Enable all access for service role" ON public.epub_images
    USING (true)
    WITH CHECK (true);

-- Index for faster lookups
CREATE INDEX IF NOT EXISTS epub_images_novel_id_idx ON public.epub_images (novel_id);
CREATE INDEX IF NOT EXISTS epub_images_image_id_idx ON public.epub_images (image_id);
