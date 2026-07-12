import unittest
from unittest.mock import patch, MagicMock
from app.agents.tools.po_tools import list_documents_needing_review

class TestPoTools(unittest.TestCase):
    """[P0] Unit tests for PO database tool functions"""

    @patch('app.agents.tools.po_tools.supabase')
    def test_list_documents_needing_review_applies_type_filter(self, mock_supabase):
        # Create a mock chain for Supabase query builder
        mock_table = MagicMock()
        mock_select = MagicMock()
        mock_eq1 = MagicMock()
        mock_eq2 = MagicMock()
        mock_order = MagicMock()
        mock_limit = MagicMock()
        mock_execute = MagicMock()

        mock_supabase.table.return_value = mock_table
        mock_table.select.return_value = mock_select
        mock_select.eq.return_value = mock_eq1
        mock_eq1.eq.return_value = mock_eq2
        mock_eq2.order.return_value = mock_order
        mock_order.limit.return_value = mock_limit
        mock_limit.execute.return_value = mock_execute

        mock_execute.data = [
            {"id": "test-uuid", "document_type": "purchase_order", "po_number": "PO-123", "vendor_name": "Test Vendor", "total_amount": 100.0, "currency": "USD"}
        ]

        # Call underlying function .func directly
        result = list_documents_needing_review.func(document_type="purchase_order")
        self.assertIn("PO-123", result)
        self.assertIn("Test Vendor", result)
        
        # Verify filtering calls were made correctly
        mock_table.select.assert_called_once_with("id, document_type, po_number, vendor_name, total_amount, currency, created_at")
        mock_select.eq.assert_called_once_with("needs_review", True)
        mock_eq1.eq.assert_called_once_with("document_type", "purchase_order")
