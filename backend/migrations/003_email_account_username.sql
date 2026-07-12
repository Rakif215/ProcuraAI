-- ============================================================================
-- Orion — Email Account Login Username Support
-- Run this in Supabase → SQL Editor (after schema.sql)
-- ============================================================================

ALTER TABLE email_accounts
ADD COLUMN IF NOT EXISTS username TEXT;
