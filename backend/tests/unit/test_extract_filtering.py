import unittest

def clean_vendor_name(name: str) -> str:
    """Strips email addresses and brackets from vendor names"""
    if " <" in name:
        return name.split(" <")[0].strip()
    return name.strip()

def filter_empty_drafts(items: list[dict]) -> list[dict]:
    """Filters out items with 0.00 total amount"""
    return [item for item in items if item.get("total_amount", 0) > 0]

class TestExtractFiltering(unittest.TestCase):
    """[P0] Unit tests for metadata normalization and statistical cleansing"""

    def test_clean_vendor_name_strips_emails(self):
        self.assertEqual(clean_vendor_name("Qatar Shipyard <steel@qatarshipyard.qa>"), "Qatar Shipyard")

    def test_filter_empty_drafts_filters_zero_amount(self):
        items = [{"total_amount": 0.0}, {"total_amount": 45000.0}]
        filtered = filter_empty_drafts(items)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["total_amount"], 45000.0)
