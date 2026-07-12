CREATE TABLE IF NOT EXISTS telemetry_events (
    id          UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    tenant_id   UUID        REFERENCES tenants(id) ON DELETE CASCADE,
    user_id     UUID        REFERENCES profiles(id) ON DELETE SET NULL,
    event_name  TEXT        NOT NULL,
    properties  JSONB       DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS telemetry_events_name_time
    ON telemetry_events(event_name, created_at DESC);

CREATE INDEX IF NOT EXISTS telemetry_events_tenant_time
    ON telemetry_events(tenant_id, created_at DESC);

ALTER TABLE telemetry_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY "telemetry_events_tenant_isolation" ON telemetry_events
    FOR ALL USING (tenant_id = public.get_my_tenant_id());
