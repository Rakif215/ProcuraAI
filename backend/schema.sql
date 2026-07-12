-- ============================================================================
-- Orion Platform — Full Database Schema
-- Run this in Supabase → SQL Editor
-- ============================================================================

-- Enable pgvector for Knowledge Base embeddings
CREATE EXTENSION IF NOT EXISTS vector;

-- ── Tenants (one row per company) ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tenants (
    id            UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    name          TEXT        NOT NULL,
    slug          TEXT        NOT NULL UNIQUE,
    plan          TEXT        NOT NULL DEFAULT 'starter',
    system_prompt TEXT,
    tools_enabled TEXT[]      DEFAULT '{}',
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- ── Profiles (extends Supabase auth.users) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS profiles (
    id         UUID REFERENCES auth.users(id) ON DELETE CASCADE PRIMARY KEY,
    tenant_id  UUID REFERENCES tenants(id) ON DELETE CASCADE NOT NULL,
    full_name  TEXT,
    role       TEXT NOT NULL DEFAULT 'member'
                   CHECK (role IN ('admin', 'member')),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── Agents (AI agent configurations) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agents (
    id            UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    tenant_id     UUID        REFERENCES tenants(id) ON DELETE CASCADE NOT NULL,
    name          TEXT        NOT NULL,
    description   TEXT,
    system_prompt TEXT,
    tools_enabled TEXT[]      DEFAULT '{}',
    is_active     BOOLEAN     DEFAULT TRUE,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- ── Agent Templates (pre-built configs for industries) ────────────────────────
CREATE TABLE IF NOT EXISTS agent_templates (
    id            UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    name          TEXT        NOT NULL,
    description   TEXT,
    industry      TEXT,        -- e.g. 'trading', 'logistics', 'healthcare'
    system_prompt TEXT        NOT NULL,
    tools_enabled TEXT[]      DEFAULT '{}',
    is_public     BOOLEAN     DEFAULT TRUE,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- ── Conversations ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS conversations (
    id         UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    tenant_id  UUID        REFERENCES tenants(id)  ON DELETE CASCADE NOT NULL,
    user_id    UUID        REFERENCES profiles(id)  ON DELETE SET NULL,
    agent_id   UUID        REFERENCES agents(id)    ON DELETE SET NULL,
    title      TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── Messages ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS messages (
    id              UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    conversation_id UUID        REFERENCES conversations(id) ON DELETE CASCADE NOT NULL,
    role            TEXT        NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'system')),
    content         TEXT,
    tool_calls      JSONB,
    metadata        JSONB       DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Email Accounts ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS email_accounts (
    id                 UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    tenant_id          UUID        REFERENCES tenants(id) ON DELETE CASCADE NOT NULL,
    email              TEXT        NOT NULL,
    username           TEXT,
    provider           TEXT        DEFAULT 'imap',
    imap_host          TEXT,
    imap_port          INTEGER     DEFAULT 993,
    smtp_host          TEXT,
    smtp_port          INTEGER     DEFAULT 465,
    password_encrypted TEXT,
    is_active          BOOLEAN     DEFAULT TRUE,
    last_synced_at     TIMESTAMPTZ,
    created_at         TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(tenant_id, email)
);

-- ── Emails ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS emails (
    id              UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    tenant_id       UUID        REFERENCES tenants(id)        ON DELETE CASCADE NOT NULL,
    account_id      UUID        REFERENCES email_accounts(id) ON DELETE CASCADE,
    imap_uid        TEXT,
    sender          TEXT,
    subject         TEXT,
    body_text       TEXT,
    summary         TEXT,
    priority        TEXT        CHECK (priority IN ('urgent', 'normal', 'low')),
    category        TEXT,
    sentiment       TEXT        CHECK (sentiment IN ('positive', 'neutral', 'negative')),
    action_items    JSONB       DEFAULT '[]',
    suggested_reply TEXT,
    needs_response  BOOLEAN     DEFAULT FALSE,
    responded       BOOLEAN     DEFAULT FALSE,
    responded_at    TIMESTAMPTZ,
    notified        BOOLEAN     DEFAULT FALSE,
    received_at     TIMESTAMPTZ,
    processed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(tenant_id, imap_uid)
);

-- ── Knowledge Bases ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS knowledge_bases (
    id          UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    tenant_id   UUID        REFERENCES tenants(id) ON DELETE CASCADE NOT NULL,
    name        TEXT        NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ── Documents (with vector embeddings) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS documents (
    id         UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    kb_id      UUID        REFERENCES knowledge_bases(id) ON DELETE CASCADE NOT NULL,
    name       TEXT        NOT NULL,
    content    TEXT,
    embedding  vector(1536),   -- OpenAI/Groq compatible embedding size
    metadata   JSONB       DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── Triggers ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS triggers (
    id         UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    agent_id   UUID        REFERENCES agents(id) ON DELETE CASCADE NOT NULL,
    type       TEXT        NOT NULL CHECK (type IN ('email', 'schedule', 'webhook', 'chat')),
    config     JSONB       DEFAULT '{}',
    is_active  BOOLEAN     DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── Task Runs (agent execution history) ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS task_runs (
    id           UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    tenant_id    UUID        REFERENCES tenants(id)  ON DELETE CASCADE NOT NULL,
    agent_id     UUID        REFERENCES agents(id)   ON DELETE SET NULL,
    trigger_id   UUID        REFERENCES triggers(id) ON DELETE SET NULL,
    status       TEXT        NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    input        JSONB,
    output       JSONB,
    error        TEXT,
    started_at   TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- ── Assistant Queue Items (Orion Lite calm review queue) ────────────────────
CREATE TABLE IF NOT EXISTS assistant_queue_items (
    id             UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    tenant_id      UUID        REFERENCES tenants(id) ON DELETE CASCADE NOT NULL,
    user_id        UUID        REFERENCES profiles(id) ON DELETE CASCADE NOT NULL,
    email_id       UUID        REFERENCES emails(id) ON DELETE CASCADE NOT NULL,
    status         TEXT        NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending', 'later', 'ignored', 'drafted', 'sent')),
    reason         TEXT,
    priority_score INTEGER     DEFAULT 0,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(tenant_id, email_id)
);

-- ── Telemetry Events (pilot instrumentation) ────────────────────────────────
CREATE TABLE IF NOT EXISTS telemetry_events (
    id          UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    tenant_id   UUID        REFERENCES tenants(id) ON DELETE CASCADE,
    user_id     UUID        REFERENCES profiles(id) ON DELETE SET NULL,
    event_name  TEXT        NOT NULL,
    properties  JSONB       DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ── Subscriptions (Stripe billing) ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS subscriptions (
    id                     UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    tenant_id              UUID        REFERENCES tenants(id) ON DELETE CASCADE NOT NULL UNIQUE,
    stripe_subscription_id TEXT,
    plan                   TEXT        NOT NULL DEFAULT 'starter',
    credits_used           INTEGER     DEFAULT 0,
    credits_limit          INTEGER     DEFAULT 1000,
    period_end             TIMESTAMPTZ,
    created_at             TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- Indexes (for performance)
-- ============================================================================

-- Vector similarity search
CREATE INDEX IF NOT EXISTS documents_embedding_idx
    ON documents USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Email queries
CREATE INDEX IF NOT EXISTS emails_tenant_priority   ON emails(tenant_id, priority);
CREATE INDEX IF NOT EXISTS emails_tenant_responded  ON emails(tenant_id, responded);
CREATE INDEX IF NOT EXISTS emails_tenant_received   ON emails(tenant_id, received_at DESC);

-- Message history
CREATE INDEX IF NOT EXISTS messages_conversation_time
    ON messages(conversation_id, created_at);

-- Agent lookup
CREATE INDEX IF NOT EXISTS agents_tenant_active ON agents(tenant_id, is_active);

-- Task run history
CREATE INDEX IF NOT EXISTS task_runs_tenant_time ON task_runs(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS assistant_queue_tenant_status
    ON assistant_queue_items(tenant_id, status, priority_score DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS telemetry_events_name_time
    ON telemetry_events(event_name, created_at DESC);
CREATE INDEX IF NOT EXISTS telemetry_events_tenant_time
    ON telemetry_events(tenant_id, created_at DESC);

-- ============================================================================
-- Row Level Security (RLS) — tenant isolation
-- ============================================================================

ALTER TABLE tenants         ENABLE ROW LEVEL SECURITY;
ALTER TABLE profiles        ENABLE ROW LEVEL SECURITY;
ALTER TABLE agents          ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations   ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages        ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_accounts  ENABLE ROW LEVEL SECURITY;
ALTER TABLE emails          ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_bases ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents       ENABLE ROW LEVEL SECURITY;
ALTER TABLE triggers        ENABLE ROW LEVEL SECURITY;
ALTER TABLE task_runs       ENABLE ROW LEVEL SECURITY;
ALTER TABLE assistant_queue_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE telemetry_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscriptions   ENABLE ROW LEVEL SECURITY;

-- ── RLS Policies ─────────────────────────────────────────────────────────────
-- Uses a SECURITY DEFINER function to look up the current user's tenant_id
-- without triggering RLS on the profiles table (avoids infinite recursion).
-- The backend uses the service role key which bypasses RLS entirely.

CREATE OR REPLACE FUNCTION public.get_my_tenant_id()
RETURNS UUID
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT tenant_id FROM profiles WHERE id = auth.uid();
$$;

-- Profiles: users can see their own row
CREATE POLICY "profiles_own_row" ON profiles
    FOR ALL USING (id = auth.uid());

-- Tenants: users can see their own tenant
CREATE POLICY "tenants_own" ON tenants
    FOR ALL USING (id = public.get_my_tenant_id());

-- Agents: tenant isolation
CREATE POLICY "agents_tenant_isolation" ON agents
    FOR ALL USING (tenant_id = public.get_my_tenant_id());

-- Emails: tenant isolation
CREATE POLICY "emails_tenant_isolation" ON emails
    FOR ALL USING (tenant_id = public.get_my_tenant_id());

-- Conversations: tenant isolation
CREATE POLICY "conversations_tenant_isolation" ON conversations
    FOR ALL USING (tenant_id = public.get_my_tenant_id());

-- Messages: via conversation's tenant
CREATE POLICY "messages_tenant_isolation" ON messages
    FOR ALL USING (conversation_id IN (
        SELECT id FROM conversations WHERE tenant_id = public.get_my_tenant_id()
    ));

-- Email accounts: tenant isolation
CREATE POLICY "email_accounts_tenant_isolation" ON email_accounts
    FOR ALL USING (tenant_id = public.get_my_tenant_id());

-- Knowledge bases: tenant isolation
CREATE POLICY "knowledge_bases_tenant_isolation" ON knowledge_bases
    FOR ALL USING (tenant_id = public.get_my_tenant_id());

-- Documents: via knowledge base's tenant
CREATE POLICY "documents_tenant_isolation" ON documents
    FOR ALL USING (kb_id IN (
        SELECT id FROM knowledge_bases WHERE tenant_id = public.get_my_tenant_id()
    ));

-- Triggers: via agent's tenant
CREATE POLICY "triggers_tenant_isolation" ON triggers
    FOR ALL USING (agent_id IN (
        SELECT id FROM agents WHERE tenant_id = public.get_my_tenant_id()
    ));

-- Task runs: tenant isolation
CREATE POLICY "task_runs_tenant_isolation" ON task_runs
    FOR ALL USING (tenant_id = public.get_my_tenant_id());

CREATE POLICY "assistant_queue_tenant_isolation" ON assistant_queue_items
    FOR ALL USING (tenant_id = public.get_my_tenant_id());

CREATE POLICY "telemetry_events_tenant_isolation" ON telemetry_events
    FOR ALL USING (tenant_id = public.get_my_tenant_id());

-- Subscriptions: tenant isolation
CREATE POLICY "subscriptions_tenant_isolation" ON subscriptions
    FOR ALL USING (tenant_id = public.get_my_tenant_id());

-- ============================================================================
-- Seed: Default agent templates
-- ============================================================================

INSERT INTO agent_templates (name, description, industry, system_prompt, tools_enabled) VALUES
(
    'Gulf Trading Assistant',
    'Specialized for trading companies in the Gulf region. Handles RFQs, POs, and supplier communications.',
    'trading',
    'You are a professional AI assistant for a Gulf-based trading company. You are fluent in business English and understand trading terminology (RFQ, PO, LC, freight, customs). When reviewing emails, identify: purchase orders, quotes, supplier responses, and payment requests. Always be formal and concise. Flag urgent payment or delivery issues immediately.',
    '{"get_urgent_emails", "get_unread_emails", "search_emails", "get_email_detail"}'
),
(
    'Logistics Coordinator',
    'Manages shipment tracking, customs documentation, and freight coordination.',
    'logistics',
    'You are an AI logistics coordinator assistant. You monitor shipping schedules, customs clearances, and freight communications. When emails arrive about shipments, extract: tracking numbers, ETA, port of entry, and any delays. Escalate immediately if a shipment is delayed more than 24 hours.',
    '{"get_urgent_emails", "get_unread_emails", "search_emails", "get_email_detail"}'
),
(
    'General Business Assistant',
    'A versatile AI assistant for general business email management and task coordination.',
    'general',
    'You are Orion, a professional AI business assistant. You help manage emails, track tasks, and coordinate communications efficiently. Be concise, professional, and action-oriented. Prioritize urgent matters and provide clear summaries.',
    '{"get_urgent_emails", "get_unread_emails", "search_emails", "get_email_detail", "web_search"}'
)
ON CONFLICT DO NOTHING;
