-- Assistant queue for Orion Lite calm inbox workflow.
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

CREATE INDEX IF NOT EXISTS assistant_queue_tenant_status
    ON assistant_queue_items(tenant_id, status, priority_score DESC, created_at DESC);

ALTER TABLE assistant_queue_items ENABLE ROW LEVEL SECURITY;

CREATE POLICY "assistant_queue_tenant_isolation" ON assistant_queue_items
    FOR ALL USING (tenant_id = public.get_my_tenant_id());
