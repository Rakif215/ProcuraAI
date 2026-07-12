import unittest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

# Mock Supabase before importing the app to prevent connection errors
import sys
sys.modules['app.db.client'] = MagicMock()

# Setup Supabase mock data
mock_supabase = MagicMock()
sys.modules['app.db.client'].supabase = mock_supabase

from app.main import app
from app.core.deps import get_current_user

# Mock authentication dependency
mock_user = MagicMock()
mock_user.user_id = "test-user-id"
mock_user.tenant_id = "test-tenant-id"
app.dependency_overrides[get_current_user] = lambda: mock_user

class TestFullSuite(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        mock_supabase.reset_mock()

    @patch("app.api.v1.auth.get_supabase_anon")
    @patch("app.api.v1.auth.supabase")
    def test_auth_login_mock(self, auth_supabase_mock, get_anon_mock):
        """Test auth login endpoint validation with proper string pydantic fields."""
        # Create a mock session that returns valid strings
        session_mock = MagicMock()
        session_mock.access_token = "mock-access-token"
        
        user_mock = MagicMock()
        user_mock.id = "test-user-id"
        
        auth_response_mock = MagicMock()
        auth_response_mock.session = session_mock
        auth_response_mock.user = user_mock
        
        # Configure get_supabase_anon() mock
        get_anon_mock.return_value.auth.sign_in_with_password.return_value = auth_response_mock
        
        # Configure profiles query mock
        profile_mock = MagicMock()
        profile_mock.data = {"tenant_id": "test-tenant-id", "role": "founder"}
        auth_supabase_mock.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value = profile_mock

        response = self.client.post("/api/v1/auth/login", json={
            "username": "testuser",
            "password": "testpassword"
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("access_token", response.json())

    def test_onboarding_providers(self):
        """Test listing available email providers."""
        response = self.client.get("/api/v1/onboarding/providers")
        self.assertEqual(response.status_code, 200)
        self.assertIn("providers", response.json())
        self.assertTrue(len(response.json()["providers"]) > 0)

    def test_onboarding_status(self):
        """Test onboarding status check returns state."""
        # Mock email accounts fetch
        mock_accounts = MagicMock()
        mock_accounts.data = [{"id": "acc-1", "email": "test@mafaz.me", "is_active": True}]
        
        # Mock user profile fetch
        mock_profile = MagicMock()
        mock_profile.data = {"role": "founder", "tone": "professional"}
        
        mock_supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = mock_accounts
        mock_supabase.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value = mock_profile

        response = self.client.get("/api/v1/onboarding/status")
        self.assertEqual(response.status_code, 200)
        self.assertIn("is_complete", response.json())

    @patch("app.api.v1.onboarding.supabase")
    def test_onboarding_profile_background_task(self, onboarding_supabase_mock):
        """Test profile setup offloads sync to BackgroundTasks instantly."""
        # Mock profile upsert
        onboarding_supabase_mock.table.return_value.upsert.return_value.execute.return_value = MagicMock(
            data=[{"user_id": "test-user-id", "role": "founder"}]
        )
        # Mock email accounts check (empty so sync finishes immediately)
        onboarding_supabase_mock.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

        response = self.client.post("/api/v1/onboarding/profile", json={
            "role": "founder",
            "tone": "professional",
            "email_length": "medium",
            "initial_sync_window_days": 1
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "saved")

    def test_email_stats_fallback(self):
        """Test email statistics endpoint returns schema structure."""
        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
        response = self.client.get("/api/v1/emails/stats")
        self.assertEqual(response.status_code, 200)
        self.assertIn("unread", response.json())

    def test_memory_items(self):
        """Test memory profile endpoint with full method chain mocked."""
        # Mock the chained table -> select -> eq -> not_ -> in_ -> order -> order -> execute chain
        mock_execute = MagicMock()
        mock_execute.execute.return_value = MagicMock(data=[])
        
        # In memory.py: supabase.table("memory_items").select(...).eq(...).not_.in_(...).order(...).order(...)
        # .not_ is accessed as attribute, then .in_ is called, then .order, then .order
        mock_supabase.table.return_value.select.return_value.eq.return_value.not_.in_.return_value.order.return_value.order.return_value = mock_execute
        
        response = self.client.get("/api/v1/memory/items")
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json(), list)

if __name__ == "__main__":
    unittest.main()
