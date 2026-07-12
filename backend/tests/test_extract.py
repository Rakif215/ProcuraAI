import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
import sys
from pathlib import Path

# Add backend directory to path
backend_dir = str(Path(__file__).parent.parent)
if backend_dir not in sys.path:
    sys.path.append(backend_dir)

from app.main import app

class TestExtractRouter(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch('app.api.v1.extract.run_extractor_pipeline')
    def test_run_pipeline_trigger(self, mock_pipeline):
        mock_pipeline.return_value = {"ok": True}
        response = self.client.post("/api/v1/extract/run?company_short_name=mafaz")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "accepted")

    @patch('app.api.v1.extract.supabase')
    def test_list_pending_reviews(self, mock_supabase):
        # Mock Supabase table select execution
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_select = MagicMock()
        mock_table.select.return_value = mock_select
        mock_eq = MagicMock()
        mock_select.eq.return_value = mock_eq
        mock_order = MagicMock()
        mock_eq.order.return_value = mock_order
        
        mock_response = MagicMock()
        mock_response.data = [{"id": "po-123", "po_number": None, "total_amount": 100.0, "needs_review": True}]
        mock_order.execute.return_value = mock_response

        response = self.client.get("/api/v1/extract/review")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]["id"], "po-123")

    @patch('app.api.v1.extract.supabase')
    def test_approve_purchase_order(self, mock_supabase):
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_update = MagicMock()
        mock_table.update.return_value = mock_update
        mock_eq = MagicMock()
        mock_update.eq.return_value = mock_eq
        
        mock_response = MagicMock()
        mock_response.data = [{"id": "po-123", "po_number": "PO-APPROVED", "needs_review": False}]
        mock_eq.execute.return_value = mock_response

        payload = {"po_number": "PO-APPROVED", "total_amount": 150.0}
        response = self.client.post("/api/v1/extract/review/po-123/approve", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "approved")
        self.assertEqual(response.json()["record"]["po_number"], "PO-APPROVED")

    @patch('app.api.v1.extract.supabase')
    def test_get_dashboard_stats(self, mock_supabase):
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_select = MagicMock()
        mock_table.select.return_value = mock_select
        
        mock_response = MagicMock()
        mock_response.data = [
            {"id": "po-1", "po_number": "PO-78901", "vendor_name": "TechCorp", "total_amount": 45200.0, "needs_review": False},
            {"id": "po-2", "po_number": "PO-78900", "vendor_name": "SwiftLog", "total_amount": 22800.0, "needs_review": True}
        ]
        mock_select.execute.return_value = mock_response

        response = self.client.get("/api/v1/extract/dashboard-stats")
        self.assertEqual(response.status_code, 200)
        res_data = response.json()
        self.assertEqual(res_data["total_pos"], 2)
        self.assertEqual(res_data["total_revenue"], 68000.0)
        self.assertEqual(res_data["avg_po_value"], 34000.0)
        self.assertEqual(res_data["active_vendors"], 2)

if __name__ == '__main__':
    unittest.main()
