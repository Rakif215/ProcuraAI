from langchain_core.tools import tool
from app.db.client import supabase
import logging

logger = logging.getLogger(__name__)

@tool
def list_documents_needing_review(document_type: str = None, limit: int = 10) -> str:
    """
    List extracted purchase orders (POs) and requests for quotes (RFQs) that currently need review or verification.
    Optionally filter by document_type (e.g., 'purchase_order', 'rfq', 'invoice', etc.) to query specific types of files.
    """
    try:
        query = supabase.table("purchase_orders").select("id, document_type, po_number, vendor_name, total_amount, currency, created_at").eq("needs_review", True)
        if document_type:
            query = query.eq("document_type", document_type.lower())
        res = query.order("created_at", desc=True).limit(limit).execute()
        if not res.data:
            type_str = f" of type '{document_type}'" if document_type else ""
            return f"No documents{type_str} currently need review."
        
        output = "Documents Needing Review:\n"
        for doc in res.data:
            doc_type = (doc.get("document_type") or "PO").upper()
            ref = doc.get("po_number") or "N/A"
            vendor = doc.get("vendor_name") or "Unknown Vendor"
            amt = f"{doc.get('currency') or '$'} {doc.get('total_amount') or 0:.2f}" if doc.get("total_amount") else "No pricing (RFQ)"
            output += f"- [{doc_type}] ID: {doc['id']} | Ref: {ref} | Vendor: {vendor} | Amount: {amt}\n"
        return output
    except Exception as e:
        return f"Error listing documents: {e}"

@tool
def get_document_details(document_id: str) -> str:
    """
    Retrieve full details for a specific purchase order or RFQ by its UUID.
    """
    try:
        res = supabase.table("purchase_orders").select("*").eq("id", document_id).single().execute()
        if not res.data:
            return f"No document found with ID: {document_id}"
        
        doc = res.data
        doc_type = (doc.get("document_type") or "PO").upper()
        output = f"=== {doc_type} Details ===\n"
        output += f"ID: {doc['id']}\n"
        output += f"Reference/PO Number: {doc.get('po_number') or 'N/A'}\n"
        output += f"Vendor Name: {doc.get('vendor_name') or 'N/A'}\n"
        if doc.get("document_type") == "rfq":
            output += "Amount: Request for Quote (No price required)\n"
        else:
            output += f"Amount: {doc.get('currency') or '$'} {doc.get('total_amount') or 0:.2f}\n"
        output += f"Issue Date: {doc.get('issue_date') or 'N/A'}\n"
        output += f"Source PDF: {doc.get('source_pdf_filename') or 'N/A'}\n"
        output += f"Source Email Subject: {doc.get('source_email_subject') or 'N/A'}\n"
        output += f"Needs Review: {doc.get('needs_review')}\n"
        output += f"Confidence Notes: {doc.get('confidence_notes') or 'N/A'}\n"
        return output
    except Exception as e:
        return f"Error fetching details: {e}"

@tool
def update_document_fields(document_id: str, po_number: str = None, vendor_name: str = None, total_amount: float = None) -> str:
    """
    Update specific extracted fields on a purchase order or RFQ (e.g. correcting a mis-extracted vendor name, PO number, or total amount).
    """
    update_data = {}
    if po_number is not None:
        update_data["po_number"] = po_number
    if vendor_name is not None:
        update_data["vendor_name"] = vendor_name
    if total_amount is not None:
        update_data["total_amount"] = total_amount
        
    if not update_data:
        return "No fields provided to update."
        
    try:
        res = supabase.table("purchase_orders").update(update_data).eq("id", document_id).execute()
        if not res.data:
            return f"Could not find or update document with ID: {document_id}"
        return f"Successfully updated document {document_id} fields: {list(update_data.keys())}."
    except Exception as e:
        return f"Error updating fields: {e}"

@tool
def approve_and_verify_document(document_id: str) -> str:
    """
    Approve and verify a purchase order or RFQ, marking it as reviewed (needs_review = False) and saving it.
    """
    try:
        res = supabase.table("purchase_orders").update({"needs_review": False}).eq("id", document_id).execute()
        if not res.data:
            return f"Could not verify document with ID: {document_id}"
        return f"Document {document_id} successfully verified and approved."
    except Exception as e:
        return f"Error verifying document: {e}"
