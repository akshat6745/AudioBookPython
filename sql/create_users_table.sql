-- SQL to create the users_data table in Supabase

CREATE TABLE IF NOT EXISTS public.users_data (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    progress JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Enable RLS
ALTER TABLE public.users_data ENABLE ROW LEVEL SECURITY;

-- Create policy to allow all access for now (or refine as needed)
-- Note: Since we use the service_role key, it bypasses RLS if configured correctly,
-- but having a policy for service_role is good practice if using anon key.
CREATE POLICY "Enable all access for service role" ON public.users_data
    USING (true)
    WITH CHECK (true);

-- Indexes
CREATE INDEX IF NOT EXISTS users_data_username_idx ON public.users_data (username);
