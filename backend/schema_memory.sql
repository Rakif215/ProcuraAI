-- ============================================================================
-- Phase 2.5 — User Memory Layer
-- Run this in Supabase → SQL Editor AFTER schema.sql
-- ============================================================================

-- ── User Profiles (stable, structured traits) ─────────────────────────────────
-- One row per user. Stores high-confidence, stable properties.
-- These are injected into the system prompt on every conversation.
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id        UUID        PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    tenant_id      UUID        REFERENCES tenants(id) ON DELETE CASCADE NOT NULL,
    -- Identity & role
    role           TEXT,                       -- "founder", "EA", "recruiter", "sales_rep", "logistics"
    industry       TEXT,                       -- "trading", "logistics", "healthcare"
    timezone       TEXT        DEFAULT 'UTC',
    language       TEXT        DEFAULT 'en',
    -- Communication style
    tone           TEXT        DEFAULT 'professional'
                               CHECK (tone IN ('formal', 'professional', 'casual', 'concise', 'warm')),
    email_length   TEXT        DEFAULT 'medium'
                               CHECK (email_length IN ('short', 'medium', 'detailed')),
    -- Freeform structured preferences (JSON for extensibility)
    -- Example: {"no_meetings_before": "11:00", "always_bcc": "legal@co.com", "avoid_cc": true}
    preferences    JSONB       DEFAULT '{}',
    -- Metadata
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at     TIMESTAMPTZ DEFAULT NOW()
);

-- ── Memory Items (dynamic, lifecycle-managed observations) ───────────────────
-- Each row is a single learned fact about a user.
-- Evidence-backed: every memory links to at least one memory_evidence row.
CREATE TABLE IF NOT EXISTS memory_items (
    id               UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id          UUID        REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
    tenant_id        UUID        REFERENCES tenants(id) ON DELETE CASCADE NOT NULL,

    -- Classification
    category         TEXT        NOT NULL
                                 CHECK (category IN ('style','preference','role','outcome','fact')),

    -- Human-readable (shown in UI and injected into prompt)
    content          TEXT        NOT NULL,   -- "User prefers concise email replies under 3 sentences"

    -- Machine-friendly (used for retrieval, deduplication, and override logic)
    memory_key       TEXT,                   -- e.g. "email_tone"
    memory_value     TEXT,                   -- e.g. "concise"
    scope            TEXT,                   -- e.g. "email_drafting", "scheduling", "general"

    -- Provenance
    source           TEXT        NOT NULL DEFAULT 'explicit'
                                 CHECK (source IN ('explicit', 'implicit', 'inferred', 'admin')),
    evidence_count   INTEGER     DEFAULT 1,  -- how many times observed
    confidence       FLOAT       DEFAULT 0.8 CHECK (confidence >= 0.0 AND confidence <= 1.0),

    -- Lifecycle
    status           TEXT        DEFAULT 'candidate'
                                 CHECK (status IN ('active', 'candidate', 'contradicted', 'expired', 'deleted')),
    -- Only 'active' memories are injected. 'candidate' requires repeated confirmation.
    -- 'contradicted' is replaced by a newer memory.

    contradicted_by  UUID        REFERENCES memory_items(id) ON DELETE SET NULL,
    expires_at       TIMESTAMPTZ,            -- NULL = permanent
    last_seen_at     TIMESTAMPTZ DEFAULT NOW(),
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ── Memory Evidence (audit trail — links memories to source events) ───────────
-- Every memory write must create at least one evidence row.
-- This enables auditing, safer updates, and explaining why a memory exists.
CREATE TABLE IF NOT EXISTS memory_evidence (
    id               UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    memory_id        UUID        REFERENCES memory_items(id) ON DELETE CASCADE NOT NULL,
    -- Source of this observation
    source_type      TEXT        NOT NULL
                                 CHECK (source_type IN ('message', 'email_edit', 'draft_feedback', 'explicit_command', 'admin')),
    source_id        TEXT,                   -- conversation_id or message_id or email_id
    -- What was observed
    extraction_type  TEXT,                   -- "explicit_statement", "repeated_signal", "correction"
    span             TEXT,                   -- The exact text that triggered this memory
    extractor_version TEXT       DEFAULT 'v1',
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ── Memory Feedback (learn from accepted/edited/rejected outputs) ─────────────
-- Captures user behavior on agent-generated outputs.
-- Used to promote candidate memories, decay stale ones, and update confidence.
CREATE TABLE IF NOT EXISTS memory_feedback (
    id              UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id         UUID        REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
    memory_id       UUID        REFERENCES memory_items(id) ON DELETE CASCADE,
    -- What the user did
    feedback_type   TEXT        NOT NULL
                                CHECK (feedback_type IN ('accepted', 'edited', 'rejected', 'corrected')),
    -- If edited or corrected — the actual correction text
    correction      TEXT,
    -- Which output this came from (for tracing)
    source_type     TEXT,                   -- "email_draft", "chat_response", "suggested_reply"
    source_id       TEXT,                   -- email_id or conversation_id
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- Indexes
-- ============================================================================

CREATE INDEX IF NOT EXISTS memory_items_user_active
    ON memory_items(user_id, status)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS memory_items_user_category
    ON memory_items(user_id, category, status);

CREATE INDEX IF NOT EXISTS memory_items_scope
    ON memory_items(user_id, scope, confidence DESC);

CREATE INDEX IF NOT EXISTS memory_evidence_memory_id
    ON memory_evidence(memory_id);

CREATE INDEX IF NOT EXISTS memory_feedback_user_memory
    ON memory_feedback(user_id, memory_id);

CREATE INDEX IF NOT EXISTS user_profiles_tenant
    ON user_profiles(tenant_id);

-- ============================================================================
-- Auto-update updated_at
-- ============================================================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER user_profiles_updated_at
    BEFORE UPDATE ON user_profiles
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER memory_items_updated_at
    BEFORE UPDATE ON memory_items
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- RLS — Memory is user-scoped (not just tenant-scoped)
-- ============================================================================

ALTER TABLE user_profiles    ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_items     ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_evidence  ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_feedback  ENABLE ROW LEVEL SECURITY;

CREATE POLICY "user_profiles_own" ON user_profiles
    FOR ALL USING (user_id = auth.uid());

CREATE POLICY "memory_items_own" ON memory_items
    FOR ALL USING (user_id = auth.uid());

CREATE POLICY "memory_evidence_own" ON memory_evidence
    FOR ALL USING (memory_id IN (
        SELECT id FROM memory_items WHERE user_id = auth.uid()
    ));

CREATE POLICY "memory_feedback_own" ON memory_feedback
    FOR ALL USING (user_id = auth.uid());
