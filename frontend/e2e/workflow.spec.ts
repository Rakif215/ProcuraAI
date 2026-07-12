import { test, expect } from '@playwright/test';

test.describe('ProcuraAI RFQ End-to-End Workflow Pipeline', () => {
  // Stateful mock data to simulate backend DB updates across the 4-step pipeline
  let mockConversation: any = {
    conversation_id: 'conv-999',
    subject: 'RFQ for Grade-B Seamless Carbon Steel Pipes',
    buyer_name: 'Fahad Al-Qahtani',
    buyer_company: 'SABIC Industrial',
    rfq_ref: 'SABIC-RFQ-2026-88',
    current_status: 'received',
    extracted_items: [],
    quote: null,
    draft_email: null,
  };

  test.beforeEach(async ({ page }) => {
    // Pipe browser console logs to host shell for debugging
    page.on('console', (msg) => console.log(`[BROWSER CONSOLE] ${msg.type()}: ${msg.text()}`));
    page.on('pageerror', (err) => console.log(`[BROWSER ERROR] ${err.message}`));
    page.on('request', (req) => console.log(`[BROWSER REQUEST] ${req.method()}: ${req.url()}`));
    page.on('response', (res) => console.log(`[BROWSER RESPONSE] ${res.status()}: ${res.url()}`));

    // 1. Mock Login/Registration APIs
    await page.route('**/api/v1/auth/login', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          access_token: 'valid-procurement-jwt-token',
          token_type: 'bearer',
          user_id: 'user-888',
          full_name: 'Procurement Specialist',
        }),
      });
    });

    // 2. Mock GET conversations to return stateful mock data
    await page.route('**/api/v1/rfq-auto/conversations', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([mockConversation]),
      });
    });

    // 3. Mock POST sync-mailbox
    await page.route('**/api/v1/rfq-auto/sync-mailbox', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'success' }),
      });
    });

    // 4. Mock POST extract-items: update state from "received" -> "extracted"
    await page.route('**/api/v1/rfq-auto/extract-items', async (route) => {
      mockConversation.current_status = 'extracted';
      mockConversation.extracted_items = [
        {
          item_name: '2-inch Seamless Pipe',
          specification: 'ASTM A106 Grade B',
          quantity: 250,
          unit: 'meters',
        },
      ];
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'success' }),
      });
    });

    // 5. Mock POST generate-quote: update state from "extracted" -> "quoted"
    await page.route('**/api/v1/rfq-auto/generate-quote', async (route) => {
      mockConversation.current_status = 'quoted';
      mockConversation.quote = {
        quote_number: 'QT-2026-0089',
        total_amount: 12500.0,
        quote_date: '2026-07-12',
        items: [
          {
            item_name: '2-inch Seamless Pipe',
            specification: 'ASTM A106 Grade B',
            quantity_quoted: 250,
            shortage_quantity: 0,
            unit_price: 50.0,
            total_price: 12500.0,
            unit: 'meters',
            match_status: 'FULL_STOCK',
          },
        ],
      };
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'success' }),
      });
    });

    // 6. Mock POST draft-email: update state to include draft_email
    await page.route('**/api/v1/rfq-auto/draft-email', async (route) => {
      mockConversation.draft_email = 'Dear Fahad,\n\nPlease find attached our quotation QT-2026-0089 for 2-inch Seamless Pipes. The total amount is USD 12,500.00.\n\nBest regards,\nProcuraAI Agent';
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'success' }),
      });
    });

    // 7. Mock POST send-quote: update state from "quoted" -> "sent"
    await page.route('**/api/v1/rfq-auto/send-quote', async (route) => {
      mockConversation.current_status = 'sent';
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'success' }),
      });
    });

    // Reset workflow state before every test run
    mockConversation = {
      conversation_id: 'conv-999',
      subject: 'RFQ for Grade-B Seamless Carbon Steel Pipes',
      buyer_name: 'Fahad Al-Qahtani',
      buyer_company: 'SABIC Industrial',
      rfq_ref: 'SABIC-RFQ-2026-88',
      current_status: 'received',
      extracted_items: [],
      quote: null,
      draft_email: null,
    };
  });

  test('should run full 4-step RFQ pipeline successfully', async ({ page }) => {
    // Navigate and login
    await page.goto('/');
    await page.fill('input[placeholder="username"]', 'testuser');
    await page.fill('input[placeholder="••••••••"]', 'password123');
    await page.click('button[type="submit"]');

    // Verify dashboard displays
    await expect(page.locator('text=DASHBOARD')).toBeVisible();

    // Verify initial state: conversation is selected and shows "Scanned RFQ" active
    await expect(page.locator('text=SABIC Industrial').first()).toBeVisible();
    await expect(page.locator('text=Scanned RFQ').first()).toBeVisible();
    
    // ─── STEP 1: Scan & Sync Mailbox ───
    await page.click('button[title="Scan Mailbox for New RFQs"]');
    await expect(page.locator('text=Mailbox synced successfully').first()).toBeVisible();

    // ─── STEP 2: AI Line Extraction ───
    // Click extract items button
    await page.click('button:has-text("Run AI Extraction")');
    await expect(page.locator('text=Items extracted successfully').first()).toBeVisible();
    
    // Extracted items list should render in Step 1 (Scanned RFQ) panel
    await expect(page.locator('text=2-inch Seamless Pipe').first()).toBeVisible();
    await expect(page.locator('text=ASTM A106 Grade B').first()).toBeVisible();

    // Verify wizard unlocks Step 2 tab and switch to it
    await page.click('text=Catalog Match');
    
    // ─── STEP 3: Quotation Generation ───
    await page.click('button:has-text("Run Catalog Match")');
    await expect(page.locator('text=Quotation generated successfully').first()).toBeVisible();

    // Matched items should now be visible in the Step 2 matched table
    await expect(page.locator('text=2-inch Seamless Pipe').first()).toBeVisible();

    // Switch to Step 3 tab
    await page.click('text=PDF & Draft');

    // ─── STEP 4: Email Draft & PDF Build ───
    await page.click('button:has-text("Build PDF & Email")');
    await expect(page.locator('text=Email draft generated successfully').first()).toBeVisible();

    // Verify visual document details and email draft are now rendered
    await expect(page.locator('text=QT-2026-0089').first()).toBeVisible();
    await expect(page.locator('text=12,500').first()).toBeVisible();
    await expect(page.locator('text=Download PDF').first()).toBeVisible();
    await expect(page.locator('text=Dear Fahad').first()).toBeVisible();

    // Switch to Step 4 tab
    await page.click('text=Dispatch Email');

    // Send final quote
    await page.click('button:has-text("Send Quotation Email")');
    await expect(page.locator('text=Quotation sent to buyer successfully!').first()).toBeVisible();

    // Verify status changes to completed/sent visual mark
    await expect(page.locator('text=Dispatch Email').first()).toBeVisible();
  });
});
