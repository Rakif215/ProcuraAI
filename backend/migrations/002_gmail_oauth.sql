-- ============================================================================
-- Orion — Gmail OAuth2 Support Migration
-- Run this in Supabase → SQL Editor (after schema.sql)
-- ============================================================================

-- Add OAuth refresh token column to email_accounts
ALTER TABLE email_accounts
ADD COLUMN IF NOT EXISTS oauth_refresh_token TEXT;

-- Add column comment
COMMENT ON COLUMN email_accounts.oauth_refresh_token IS
  'Encrypted Google OAuth2 refresh token for Gmail IMAP/SMTP access';
