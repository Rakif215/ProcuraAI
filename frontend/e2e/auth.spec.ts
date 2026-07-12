import { test, expect } from '@playwright/test';

test.describe('ProcuraAI Authentication E2E Tests', () => {
  test.beforeEach(async ({ page }) => {
    // Intercept backend auth APIs and mock responses for reliable offline execution
    await page.route('**/api/v1/auth/login', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          access_token: 'mocked-jwt-token-xyz',
          token_type: 'bearer',
          user_id: 'tenant-123',
          full_name: 'Test Procurement User',
        }),
      });
    });

    await page.route('**/api/v1/auth/register', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          access_token: 'mocked-jwt-token-xyz',
          token_type: 'bearer',
          user_id: 'tenant-123',
          full_name: 'Test Procurement User',
        }),
      });
    });

    // Mock initial conversations endpoint to load dashboard without error
    await page.route('**/api/v1/rfq-auto/conversations', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([]),
      });
    });

    // Go to home page
    await page.goto('/');
  });

  test('should display login page and perform sign in successfully', async ({ page }) => {
    // Check heading
    await expect(page.locator('h1')).toHaveText('ProcuraAI');

    // Enter username & password
    await page.fill('input[placeholder="username"]', 'testuser');
    await page.fill('input[placeholder="••••••••"]', 'password123');

    // Click Sign In button
    await page.click('button[type="submit"]');

    // Verify redirection/state change (e.g. check for dashboard view elements)
    // The Dashboard contains elements like a greeting or a logout button or the dashboard title
    // Let's assert that the heading shifts to the logged-in state or App title
    await expect(page.locator('text=DASHBOARD')).toBeVisible({ timeout: 5000 });
  });

  test('should allow toggling tabs and signing up', async ({ page }) => {
    // Click Create Account tab
    await page.click('button:has-text("Create Account")');

    // Fill registration form
    await page.fill('input[placeholder="Ganesh Kumar"]', 'John Doe');
    await page.fill('input[placeholder="username"]', 'newuser');
    await page.fill('input[placeholder="••••••••"]', 'securepass123');

    // Submit
    await page.click('button[type="submit"]');

    // Dashboard should load
    await expect(page.locator('text=DASHBOARD')).toBeVisible({ timeout: 5000 });
  });
});
