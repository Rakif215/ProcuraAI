---
status: 'active'
project: ProcuraAI
version: 1.0
date: 2026-07-12
---

# ProcuraAI — Epic Breakdown

## Overview

This document provides the complete epic and story breakdown for **ProcuraAI**, a B2B SaaS RFQ Automation platform. Epics 1–3 from the original Orion plan are complete. This is the new roadmap picking up from the current consolidated codebase state.

## Current State (Baseline)

The following have already been implemented:
- Single-root ProcuraAI workspace (frontend + backend)
- Aura Glass UI (React 19 + Tailwind v4 + Framer Motion)
- 4-step RFQ wizard (Scan → Extract → Quote → Dispatch)
- FastAPI backend with 55+ endpoints (auth, email, agents, memory, RFQ)
- Supabase multi-tenant database (18 tables, RLS on all apex_ tables via migration 006)
- JWT auth tokens flowing from frontend to all backend RFQ endpoints
- AI pipeline: Groq → OpenRouter → Gemini fallback chain
- BMad agents: /john (PM), /amelia (Dev), /arch, /analyst, /ux-designer

---

## Epic List

### Epic 4: Production Deployment & DevOps
- **Goal:** Get ProcuraAI deployed on live cloud infrastructure, accessible via a real domain, with CI/CD so every code push is tested and deployed automatically.
- **FRs:** GitHub repo setup, Vercel (frontend), Render (backend FastAPI), GitHub Actions CI/CD pipeline, staging environment, domain + SSL.

### Epic 5: Frontend API Hardening & Environment Config
- **Goal:** Remove all hardcoded `localhost:8000` URLs from the frontend and wire up a proper environment-based API layer so the app works identically in dev, staging, and production.
- **FRs:** Vite env variable config, API base URL abstraction, error handling, loading state polish.

### Epic 6: RFQ Pipeline Multi-Tenant Hardening
- **Goal:** Ensure the RFQ pipeline is fully scoped to the authenticated tenant — inventory matching, email sending credentials, and quotation generation must all respect tenant boundaries without sharing data.
- **FRs:** Tenant-scoped inventory catalog, per-tenant SMTP credentials, inventory seeding UI.

### Epic 7: User Onboarding & Account Management
- **Goal:** New customers must be able to self-register, connect their procurement email account, seed their product catalog, and start processing RFQs — without any manual setup from the ProcuraAI team.
- **FRs:** Registration flow, IMAP/SMTP connection wizard, CSV inventory upload, first-sync trigger.

### Epic 8: Quotation PDF & Email Delivery (Production)
- **Goal:** Replace the local disk PDF storage and SMTP simulation with real cloud-native PDF generation stored in Supabase Storage, and real authenticated email delivery per tenant.
- **FRs:** Supabase Storage for PDFs, real SMTP dispatch using tenant credentials, email delivery receipts.

### Epic 9: Dashboard Analytics & Reporting
- **Goal:** Give procurement managers actionable business intelligence — how many RFQs processed, conversion rate, average quote turnaround, top buyers, inventory gaps.
- **FRs:** Dashboard KPI cards, RFQ activity chart (recharts), quotation funnel, CSV export.

### Epic 10: SaaS Billing & Subscription Tiers
- **Goal:** Accept money. Implement Stripe subscription plans with feature gating so free/trial users have limits and paid users unlock full capacity.
- **FRs:** Stripe Checkout, webhook handler, subscription table enforcement, customer portal.

---

## Epic 4: Production Deployment & DevOps

### Story 4.1: GitHub Repository Initialization
As a developer,
I want the ProcuraAI codebase pushed to a clean GitHub repository with proper `.gitignore` and branch protection on `main`,
So that the team can collaborate and CI/CD pipelines can be triggered on push.

**Acceptance Criteria:**
- Given the ProcuraAI root folder exists locally,
- When running `git init && git remote add origin <repo>`,
- Then all source files (excluding `venv/`, `node_modules/`, `.env`, `dist/`) are committed and pushed to `main`.
- And a `.gitignore` covers Python, Node, macOS, and environment files.

### Story 4.2: Vercel + Render Cloud Deployment
As a DevOps engineer,
I want the React frontend deployed to **Vercel** (free tier) and the FastAPI backend deployed to **Render** (free tier),
So that ProcuraAI is accessible from a real cloud domain at zero cost.

**Deployment Architecture:**
- **Frontend (React/Vite):** Vercel — automatic deploys on push to `main`, CDN edge, free SSL
- **Backend (FastAPI):** Render Web Service — always-on container, supports WebSockets, free tier (spins down after 15 min inactivity)

**Acceptance Criteria:**
- Given the GitHub repo is connected to both Vercel and Render,
- When a push to `main` is made,
- Then Vercel auto-deploys the `frontend/` folder and serves the React SPA at `https://procura-ai.vercel.app`.
- And Render auto-deploys the `backend/` folder and serves FastAPI at `https://procuraai-api.onrender.com`.
- And `VITE_API_URL` is set in Vercel's environment variables to point to the Render backend URL.
- And all secret env vars (Supabase keys, AI keys, Fernet key) are configured in Render's environment dashboard.

### Story 4.3: GitHub Actions CI/CD Pipeline
As a developer,
I want a GitHub Actions workflow that runs backend syntax checks, frontend builds, and deploys on merge to `main`,
So that broken code is never deployed to production.

**Acceptance Criteria:**
- Given a pull request is opened against `main`,
- When the CI workflow runs,
- Then `python -m compileall app/` runs and passes for the backend.
- And `npm run build` runs and passes for the frontend.
- On merge to `main`, the DigitalOcean deployment is triggered automatically.

### Story 4.4: Staging Environment Setup
As a developer,
I want a separate staging environment with its own Supabase project and domain (`staging.procura.ai`),
So that changes can be tested in production-like conditions before going live.

**Acceptance Criteria:**
- Given a `staging` branch exists,
- When a push to `staging` is made,
- Then a separate DigitalOcean app (or DO preview) is deployed pointing to a staging Supabase project.
- And the staging domain resolves correctly with SSL.

---

## Epic 5: Frontend API Hardening & Environment Config

### Story 5.1: Vite Environment Variable API Base URL
As a frontend developer,
I want all API calls to use `VITE_API_URL` environment variable instead of the hardcoded `http://localhost:8000`,
So that the same frontend build works in development, staging, and production without code changes.

**Acceptance Criteria:**
- Given `VITE_API_URL` is set to `/api` in the `.do/app.yaml` build env,
- When any API fetch is made,
- Then the request targets `${VITE_API_URL}/v1/rfq-auto/conversations` (not `localhost:8000`).
- And in local dev, `VITE_API_URL=http://localhost:8000/api` still works via `.env.local`.

### Story 5.2: API Abstraction Layer
As a frontend developer,
I want all 8 RFQ API calls centralized in a single `src/lib/api.ts` module,
So that any URL or auth header change is made in one place, not scattered across 1,500 lines of App.tsx.

**Acceptance Criteria:**
- Given the `src/lib/api.ts` file is created,
- When any component calls `api.fetchConversations(token)`,
- Then the function constructs the correct URL, attaches the Bearer token, and returns parsed JSON or throws a typed error.
- And all raw `fetch()` calls are removed from `App.tsx`.

### Story 5.3: Global Error Boundary & Toast Notifications
As a user,
I want meaningful error messages when the backend is unreachable or returns an error,
So that I understand what went wrong instead of seeing a silent failure.

**Acceptance Criteria:**
- Given any API call fails (network error, 4xx, 5xx),
- When the error is caught,
- Then a dismissable toast notification appears at the top-right of the screen with the error message.
- And the UI does not crash or show a blank screen.

---

## Epic 6: RFQ Pipeline Multi-Tenant Hardening

### Story 6.1: Tenant-Scoped Inventory Catalog
As a procurement manager,
I want my company's inventory catalog to be private to my account,
So that competitors or other ProcuraAI customers cannot see our pricing or stock levels.

**Acceptance Criteria:**
- Given `apex_inventory` has a `tenant_id` column (from migration 006),
- When the inventory matching pipeline runs,
- Then `SELECT` queries on `apex_inventory` filter by `WHERE tenant_id = :current_tenant_id`.
- And a new tenant with no inventory sees an empty catalog, not another tenant's products.

### Story 6.2: Per-Tenant SMTP Credentials for Quote Dispatch
As a procurement manager,
I want quotation emails sent from my company's email address (e.g. `sales@mycompany.com`), not a shared ProcuraAI address,
So that buyers receive the email from a familiar sender.

**Acceptance Criteria:**
- Given the tenant has an email account stored in `email_accounts` table,
- When the "Send Quote" action is triggered,
- Then the backend fetches the tenant's SMTP credentials from `email_accounts` (decrypted via Fernet),
- And sends the quotation email via the tenant's own SMTP server.
- And falls back to a default ProcuraAI relay SMTP if no tenant credentials are found.

### Story 6.3: Inventory Seeding via CSV Upload
As a procurement manager,
I want to upload a CSV file of my product catalog (name, specification, stock, price),
So that the AI can match incoming RFQ line items against my actual inventory.

**Acceptance Criteria:**
- Given the user navigates to the Inventory section of the dashboard,
- When they upload a valid CSV file with columns: `item_name, specification, quantity_on_hand, unit, selling_price`,
- Then the backend parses the CSV and upserts rows into `apex_inventory` scoped to their `tenant_id`.
- And a success count is returned: "143 items imported successfully."

---

## Epic 7: User Onboarding & Account Management

### Story 7.1: Self-Service Registration & Email Verification
As a new customer,
I want to register for ProcuraAI with my work email and receive a verification email,
So that I can activate my account without needing manual approval from the ProcuraAI team.

**Acceptance Criteria:**
- Given a user submits the registration form with email + password,
- When the backend creates the Supabase auth user,
- Then a verification email is sent to the registered address.
- And the user cannot log in until the email is verified (Supabase `email_confirm: True` in production).
- And a new tenant row is created for their company workspace.

### Story 7.2: IMAP Email Account Connection Wizard
As a new customer,
I want a step-by-step wizard to connect my procurement inbox (Gmail or SMTP/IMAP),
So that ProcuraAI can monitor it for incoming RFQs.

**Acceptance Criteria:**
- Given the user is in the onboarding wizard step 2,
- When they enter their email + password + IMAP host,
- Then the backend tests the IMAP connection and returns success/failure.
- And on success, the credentials are encrypted via Fernet and stored in `email_accounts`.
- And the first mailbox sync is triggered automatically.

### Story 7.3: Product Catalog Onboarding Step
As a new customer,
I want the onboarding wizard to include an optional step to upload my product catalog,
So that the AI is ready to match RFQ line items from the very first sync.

**Acceptance Criteria:**
- Given the user is in onboarding step 3,
- When they either upload a CSV or skip,
- Then on upload: catalog items are upserted to `apex_inventory` with their `tenant_id`.
- And on skip: a placeholder is saved so they can upload later from the settings page.

---

## Epic 8: Quotation PDF & Cloud Storage

### Story 8.1: Supabase Storage for Generated PDFs
As a system,
I want generated quotation PDFs stored in Supabase Storage instead of the local server filesystem,
So that PDFs persist across deployments and are accessible from any server instance.

**Acceptance Criteria:**
- Given a quotation PDF is generated by ReportLab,
- When `generate_quote_pdf()` completes,
- Then the PDF bytes are uploaded to Supabase Storage bucket `quotation-pdfs/{tenant_id}/{quote_number}.pdf`.
- And the download URL is stored in `apex_quotations.pdf_url`.
- And the `/download-pdf/{quote_number}` route returns a redirect to the Supabase Storage signed URL.

### Story 8.2: Real Email Delivery with Attachment
As a procurement manager,
I want the "Send Quote" action to actually send the PDF via email to the buyer,
So that the quotation dispatch step is fully automated end-to-end.

**Acceptance Criteria:**
- Given a quotation PDF exists in Supabase Storage and a buyer email is stored in `apex_conversations.buyer_email`,
- When the user clicks "Dispatch Email",
- Then the backend downloads the PDF from Supabase Storage, attaches it to an SMTP email, and sends it to `buyer_email`.
- And the `apex_conversations.current_status` is set to `sent`.
- And a delivery timestamp is recorded in `apex_quotations.sent_at`.

---

## Epic 9: Dashboard Analytics & Reporting

### Story 9.1: RFQ KPI Summary Cards
As a procurement manager,
I want the main dashboard to show key metrics at a glance (total RFQs, quotes sent, avg turnaround, conversion rate),
So that I can assess team performance without digging into individual records.

**Acceptance Criteria:**
- Given the user is on the Dashboard view,
- When the page loads,
- Then 4 KPI cards render: Total RFQs This Month, Quotes Sent, Avg Turnaround (hours), Conversion Rate (%).
- And the data is fetched from a new `GET /api/v1/rfq-auto/dashboard-stats` endpoint scoped by `tenant_id`.

### Story 9.2: RFQ Activity Chart
As a procurement manager,
I want a line or bar chart showing RFQ volume over the last 30 days,
So that I can spot trends and peak demand periods.

**Acceptance Criteria:**
- Given the dashboard stats endpoint returns daily counts,
- When rendered in the Dashboard view,
- Then a Recharts `BarChart` displays daily RFQ counts for the past 30 days.
- And hovering a bar shows a tooltip with the exact date and count.

### Story 9.3: CSV Export of Quotation History
As a procurement manager,
I want to download a CSV of all quotations sent this month (buyer, items, total, date),
So that I can import the data into Excel or our ERP system.

**Acceptance Criteria:**
- Given quotation records exist for the tenant,
- When the user clicks "Export CSV" on the Dashboard,
- Then a `GET /api/v1/rfq-auto/export-csv` endpoint streams a CSV file.
- And the file downloads automatically in the browser with filename `ProcuraAI_Quotations_{YYYY-MM}.csv`.

---

## Epic 10: SaaS Billing & Subscription Tiers

### Story 10.1: Stripe Product & Pricing Configuration
As a ProcuraAI admin,
I want Stripe products and prices created for the Starter ($49/mo) and Pro ($149/mo) plans,
So that customers can subscribe via Stripe Checkout.

**Acceptance Criteria:**
- Given Stripe is configured with products and prices,
- When the pricing page renders,
- Then Starter and Pro plan cards display with the correct pricing and feature lists.
- And each card has a "Get Started" CTA that initiates a Stripe Checkout session.

### Story 10.2: Stripe Checkout & Webhook Integration
As a customer,
I want to subscribe to ProcuraAI via Stripe Checkout and have my account automatically upgraded,
So that I don't need to wait for manual provisioning.

**Acceptance Criteria:**
- Given a user clicks "Get Started" on the pricing page,
- When they complete the Stripe Checkout flow,
- Then a `checkout.session.completed` webhook event updates the `subscriptions` table with their plan and Stripe customer ID.
- And the user is redirected back to the dashboard with a success message.

### Story 10.3: Plan-Based Feature Gating
As a system,
I want API endpoints to enforce subscription tier limits (e.g., Starter = 50 RFQs/month, Pro = unlimited),
So that free/trial users cannot exceed their plan limits without upgrading.

**Acceptance Criteria:**
- Given a Starter plan user has processed 50 RFQs this month,
- When they trigger another AI extraction,
- Then the backend returns `403 Forbidden` with message "Monthly RFQ limit reached. Upgrade to Pro."
- And the frontend displays an upgrade prompt modal.
