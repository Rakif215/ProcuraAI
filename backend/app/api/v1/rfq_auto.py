import sys
import os
import logging
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends
from sqlalchemy import text
from pydantic import BaseModel

# Import pipeline modules from local rfq service
from app.services.rfq.pdf_generator import generate_quote_pdf
from app.services.rfq.email_drafter import draft_quote_reply

from app.db.client import engine
from app.core.deps import AuthUser

router = APIRouter(prefix="/rfq-auto", tags=["rfq-auto"])

class QuoteActionRequest(BaseModel):
    quote_number: str

class ConvActionRequest(BaseModel):
    conversation_id: str

class GenerateQuoteRequest(BaseModel):
    conversation_id: Optional[str] = None

@router.get("/conversations")
async def get_conversations(current_user: AuthUser = Depends()):
    """
    Returns all RFQ conversations with their current status, extracted items, quote headers, and draft replies.
    """
    async with engine.begin() as conn:
        # Fetch conversations
        convs_res = await conn.execute(
            text("""
                SELECT id, subject, buyer_name, buyer_company, rfq_ref, current_status
                FROM apex_conversations
                WHERE tenant_id = :tenant_id
                ORDER BY created_at DESC;
            """),
            {"tenant_id": current_user.tenant_id}
        )
        conversations = []
        for row in convs_res.all():
            conv_id = str(row[0])
            subject = row[1]
            buyer_name = row[2]
            buyer_company = row[3]
            rfq_ref = row[4]
            current_status = row[5]

            # Fetch incoming email body
            email_res = await conn.execute(
                text("SELECT body_text, sender, received_at FROM apex_emails WHERE conversation_id = :conv_id AND sender != 'sales@apexsuppliesqa.com' LIMIT 1;"),
                {"conv_id": conv_id}
            )
            email_row = email_res.first()
            email_body = email_row[0] if email_row else ""
            sender = email_row[1] if email_row else ""
            received_at = email_row[2].isoformat() if email_row and email_row[2] else ""

            # Fetch extracted RFQ line items
            items_res = await conn.execute(
                text("SELECT item_name, specification, quantity_requested, unit FROM apex_rfq_line_items WHERE conversation_id = :conv_id;"),
                {"conv_id": conv_id}
            )
            extracted_items = [
                {
                    "item_name": r[0],
                    "specification": r[1],
                    "quantity": float(r[2]) if r[2] else 0.0,
                    "unit": r[3] or "pcs"
                } for r in items_res.all()
            ]

            # Fetch Quote if exists
            quote_res = await conn.execute(
                text("SELECT id, quote_number, total_amount, status FROM apex_quotations WHERE conversation_id = :conv_id LIMIT 1;"),
                {"conv_id": conv_id}
            )
            quote_row = quote_res.first()
            quote_data = None
            draft_email = None

            if quote_row:
                quote_id, quote_number, total_amount, quote_status = quote_row
                
                # Fetch quote line items
                q_items_res = await conn.execute(
                    text("""
                        SELECT item_name, specification, quantity_quoted, unit_price, total_price, match_status, shortage_quantity 
                        FROM apex_quotation_line_items 
                        WHERE quotation_id = :quote_id;
                    """),
                    {"quote_id": quote_id}
                )
                quote_items = [
                    {
                        "item_name": qr[0],
                        "specification": qr[1],
                        "quantity_quoted": float(qr[2]) if qr[2] else 0.0,
                        "unit_price": float(qr[3]) if qr[3] else 0.0,
                        "total_price": float(qr[4]) if qr[4] else 0.0,
                        "match_status": qr[5],
                        "shortage_quantity": float(qr[6]) if qr[6] else 0.0
                    } for qr in q_items_res.all()
                ]

                quote_data = {
                    "quote_number": quote_number,
                    "total_amount": float(total_amount),
                    "status": quote_status,
                    "items": quote_items
                }

                # Fetch AI drafted reply email if exists
                draft_res = await conn.execute(
                    text("SELECT body_text FROM apex_emails WHERE conversation_id = :conv_id AND sender = 'sales@apexsuppliesqa.com' LIMIT 1;"),
                    {"conv_id": conv_id}
                )
                draft_row = draft_res.first()
                if draft_row:
                    draft_email = draft_row[0]

            conversations.append({
                "conversation_id": conv_id,
                "subject": subject,
                "buyer_name": buyer_name,
                "buyer_company": buyer_company,
                "rfq_ref": rfq_ref,
                "current_status": current_status,
                "received_at": received_at,
                "sender": sender,
                "email_body": email_body,
                "extracted_items": extracted_items,
                "quote": quote_data,
                "draft_email": draft_email
            })

        return conversations

@router.post("/sync-mailbox")
async def sync_mailbox(current_user: AuthUser = Depends()):
    """
    Syncs the mailbox and persists pipeline data to database.
    Runs the exact sync & persistence logic.
    """
    from app.services.rfq.ingest_live import ingest_live_emails
    try:
        # Run live IMAP email extraction
        ingest_live_emails(current_user.tenant_id)
        return {"status": "success", "message": "Live mailbox synced successfully."}
    except Exception as e:
        logging.error("Live email sync failed", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/extract-items")
async def extract_items(req: ConvActionRequest, current_user: AuthUser = Depends()):
    """
    Runs AI line item extraction specifically for a target conversation.
    """
    from app.services.rfq.extractor import extract_single_conversation
    try:
        extract_single_conversation(req.conversation_id)
        return {"status": "success", "message": "Items extracted successfully."}
    except Exception as e:
        logging.error("AI Extraction failed", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate-quote")
async def generate_quote(req: GenerateQuoteRequest = GenerateQuoteRequest(), current_user: AuthUser = Depends()):
    """
    Triggers inventory matching and quotation draft generation.
    """
    from app.services.rfq.inventory_matcher import match_inventory
    from app.services.rfq.quotation_generator import generate_quotations
    try:
        # Run live matching against inventory catalog
        match_inventory(req.conversation_id)
        # Generate quotations and insert into database
        await generate_quotations(req.conversation_id)
        return {"status": "success", "message": "Quotation drafts successfully generated."}
    except Exception as e:
        logging.error("Quotation generation failed", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/draft-email")
async def draft_email(req: QuoteActionRequest, current_user: AuthUser = Depends()):
    """
    Generates the premium PDF and drafts the reply email.
    """
    try:
        # Compile PDF
        await generate_quote_pdf(req.quote_number)
        # Draft email
        eml_path = await draft_quote_reply(req.quote_number)
        return {"status": "success", "message": f"PDF generated and reply drafted.", "eml_path": eml_path}
    except Exception as e:
        logging.error("Draft email failed with exception", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/send-quote")
async def send_quote(req: QuoteActionRequest, current_user: AuthUser = Depends()):
    """
    Simulates sending the quotation reply email.
    """
    import asyncio
    await asyncio.sleep(1.0)
    async with engine.begin() as conn:
        # Update conversation status to 'sent' if a quote exists for it
        conv_res = await conn.execute(
            text("SELECT conversation_id FROM apex_quotations WHERE quote_number = :quote_num;"),
            {"quote_num": req.quote_number}
        )
        row = conv_res.first()
        if row:
            await conn.execute(
                text("UPDATE apex_conversations SET current_status = 'sent' WHERE id = :conv_id;"),
                {"conv_id": row[0]}
            )
        await conn.execute(
            text("UPDATE apex_quotations SET status = 'sent' WHERE quote_number = :quote_num;"),
            {"quote_num": req.quote_number}
        )
    return {"status": "success", "message": f"Quotation email for {req.quote_number} sent successfully!"}

from fastapi.responses import FileResponse

@router.get("/download-pdf/{quote_number}")
async def download_pdf(quote_number: str):
    backend_app_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) # backend/app
    pdf_path = os.path.join(backend_app_dir, "services", "generated_quotes", f"{quote_number}.pdf")
    if not os.path.exists(pdf_path):
        raise HTTPException(status_code=404, detail="PDF not found")
    return FileResponse(pdf_path, media_type="application/pdf", filename=f"{quote_number}.pdf")
