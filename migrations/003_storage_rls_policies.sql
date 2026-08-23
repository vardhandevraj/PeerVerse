-- Storage bucket policies for CampusConnectAI (Supabase).
--
-- The Flask server uploads with the publishable key, which acts as the
-- `anon` role against Storage. Without these policies every upload fails
-- with: 403 "new row violates row-level security policy".
--
-- Run ONCE in the Supabase SQL editor, or through the direct database URL:
--   python -c "..."  (see README) — applied by the project maintainer.
--
-- Buckets must already exist and be marked public (dashboard → Storage):
--   profile-images, post-files, resources, chat-files

-- Allow the server key to upload into the four application buckets.
DROP POLICY IF EXISTS "app_buckets_insert" ON storage.objects;
CREATE POLICY "app_buckets_insert"
    ON storage.objects FOR INSERT TO anon, authenticated
    WITH CHECK (
        bucket_id IN ('profile-images', 'post-files', 'resources', 'chat-files')
    );

-- Allow replacing an object at a path you can see (overwrite semantics).
DROP POLICY IF EXISTS "app_buckets_update" ON storage.objects;
CREATE POLICY "app_buckets_update"
    ON storage.objects FOR UPDATE TO anon, authenticated
    USING (
        bucket_id IN ('profile-images', 'post-files', 'resources', 'chat-files')
    );

-- Public read access so media_url() links work without signed URLs.
DROP POLICY IF EXISTS "app_buckets_select" ON storage.objects;
CREATE POLICY "app_buckets_select"
    ON storage.objects FOR SELECT TO anon, authenticated
    USING (
        bucket_id IN ('profile-images', 'post-files', 'resources', 'chat-files')
    );

-- Allow server-side cleanup through supabase.storage.from_(bucket).remove().
DROP POLICY IF EXISTS "app_buckets_delete" ON storage.objects;
CREATE POLICY "app_buckets_delete"
    ON storage.objects FOR DELETE TO anon, authenticated
    USING (
        bucket_id IN ('profile-images', 'post-files', 'resources', 'chat-files')
    );
