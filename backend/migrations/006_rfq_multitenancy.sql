-- Migration SQL: Add Multi-Tenancy & Tenant RLS Isolation to RFQ Automation Tables

-- 1. Add tenant_id columns
ALTER TABLE apex_inventory ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE apex_conversations ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;
ALTER TABLE apex_quotations ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE;

-- 2. Enable Row Level Security (RLS)
ALTER TABLE apex_inventory ENABLE ROW LEVEL SECURITY;
ALTER TABLE apex_conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE apex_emails ENABLE ROW LEVEL SECURITY;
ALTER TABLE apex_rfq_line_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE apex_quotations ENABLE ROW LEVEL SECURITY;
ALTER TABLE apex_quotation_line_items ENABLE ROW LEVEL SECURITY;

-- 3. Drop existing policies if they exist to avoid duplication errors on re-run
DROP POLICY IF EXISTS apex_inventory_tenant_isolation ON apex_inventory;
DROP POLICY IF EXISTS apex_conversations_tenant_isolation ON apex_conversations;
DROP POLICY IF EXISTS apex_emails_tenant_isolation ON apex_emails;
DROP POLICY IF EXISTS apex_rfq_line_items_tenant_isolation ON apex_rfq_line_items;
DROP POLICY IF EXISTS apex_quotations_tenant_isolation ON apex_quotations;
DROP POLICY IF EXISTS apex_quotation_line_items_tenant_isolation ON apex_quotation_line_items;

-- 4. Create RLS Policies for Tenant-Level Isolation
CREATE POLICY apex_inventory_tenant_isolation ON apex_inventory
    FOR ALL USING (tenant_id = public.get_my_tenant_id());

CREATE POLICY apex_conversations_tenant_isolation ON apex_conversations
    FOR ALL USING (tenant_id = public.get_my_tenant_id());

CREATE POLICY apex_emails_tenant_isolation ON apex_emails
    FOR ALL USING (conversation_id IN (
        SELECT id FROM apex_conversations WHERE tenant_id = public.get_my_tenant_id()
    ));

CREATE POLICY apex_rfq_line_items_tenant_isolation ON apex_rfq_line_items
    FOR ALL USING (conversation_id IN (
        SELECT id FROM apex_conversations WHERE tenant_id = public.get_my_tenant_id()
    ));

CREATE POLICY apex_quotations_tenant_isolation ON apex_quotations
    FOR ALL USING (tenant_id = public.get_my_tenant_id());

CREATE POLICY apex_quotation_line_items_tenant_isolation ON apex_quotation_line_items
    FOR ALL USING (quotation_id IN (
        SELECT id FROM apex_quotations WHERE tenant_id = public.get_my_tenant_id()
    ));
