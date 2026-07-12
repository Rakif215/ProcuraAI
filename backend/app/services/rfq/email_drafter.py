import os
import sys
import json
import asyncio
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from sqlalchemy import text

# Insert the parent backend directory at index 0 to override local package shadowing
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../backend')))

from app.db.client import engine
from app.services.model_router import chat_completion

async def draft_quote_reply(quote_number: str) -> str:
    # 1. Fetch details from database
    async with engine.begin() as conn:
        quote_res = await conn.execute(
            text("""
                SELECT 
                    q.id,
                    q.total_amount,
                    c.id as conv_id,
                    c.buyer_name,
                    c.buyer_company,
                    c.rfq_ref,
                    c.subject
                FROM apex_quotations q
                JOIN apex_conversations c ON q.conversation_id = c.id
                WHERE q.quote_number = :quote_number;
            """),
            {"quote_number": quote_number}
        )
        quote = quote_res.first()
        if not quote:
            raise ValueError(f"Quotation {quote_number} not found.")
            
        quote_id, total_amount, conv_id, buyer_name, buyer_company, rfq_ref, subject = quote
        
        # Fetch items to list in the prompt
        items_res = await conn.execute(
            text("""
                SELECT 
                    qli.item_name,
                    qli.quantity_quoted,
                    qli.unit_price,
                    qli.total_price,
                    qli.match_status,
                    rli.unit
                FROM apex_quotation_line_items qli
                LEFT JOIN apex_rfq_line_items rli ON qli.rfq_line_item_id = rli.id
                WHERE qli.quotation_id = :quote_id;
            """),
            {"quote_id": quote_id}
        )
        items = items_res.all()

    # Format items description for LLM
    items_desc_list = []
    for item in items:
        unit = item[5] or "pcs"
        qty = float(item[1]) if item[1] is not None else 0.0
        price = float(item[2]) if item[2] is not None else 0.0
        items_desc_list.append(
            f"- {item[0]}: Quoted Qty: {qty:g} {unit} | Unit Price: {price:,.2f} QAR | Status: {item[4] or 'UNMATCHED'}"
        )
    items_summary = "\n".join(items_desc_list)

    # 2. Call LLM to draft the reply
    system_prompt = (
        "You are a professional Sales Account Manager at Apex Industrial Supplies WLL in Doha, Qatar.\n"
        "Your job is to write a highly professional, polite business email response replying to a customer's RFQ.\n"
        "We are attaching a detailed quotation PDF.\n\n"
        "In the email body:\n"
        "1. Address the customer by name (e.g. Ganesh Kumar) and thank them for their RFQ.\n"
        "2. Reference the quotation number (e.g. QT-2026-003) and state that the detailed quote is attached as a PDF.\n"
        "3. Highlight the quotation details:\n"
        "   - Total Quoted Amount clearly in QAR (Qatari Riyal).\n"
        "   - Give them a summary of item availability. If any items have PARTIAL_STOCK or OUT_OF_STOCK, politely explain that those specific items require factory lead times (typically 7-14 days).\n"
        "   - State standard delivery for in-stock items is 3-5 days.\n"
        "4. Keep the tone premium, formal, and helpful. Sign off as 'Sales Department | Apex Industrial Supplies'.\n\n"
        "Do NOT include the subject line or any headers in your output. Output ONLY the email body text.\n"
        "Do NOT use placeholders like [Buyer Name] or [Quotation Number] - use the actual values provided below."
    )

    user_content = (
        f"Buyer Name: {buyer_name or 'Procurement Officer'}\n"
        f"Buyer Company: {buyer_company or 'Customer'}\n"
        f"RFQ Ref: {rfq_ref or 'N/A'}\n"
        f"Quotation Ref: {quote_number}\n"
        f"Total Quote Value: {float(total_amount):,.2f} QAR\n"
        f"Items Quoted:\n{items_summary}"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content}
    ]

    print("Generating reply email using AI pipeline...")
    try:
        response = chat_completion(
            messages=messages,
            purpose="draft_quote_reply_email",
            temperature=0.3
        )
        if not response or not response.content:
            raise RuntimeError("AI model returned empty response.")
        email_body = response.content.strip()
    except Exception as e:
        print(f"WARNING: Targeted LLM email drafting failed ({e}). Falling back to deterministic mock email drafter...")
        buyer_display_name = buyer_name or "Procurement Team"
        
        email_body = (
            f"Dear {buyer_display_name},\n\n"
            f"Thank you for contacting Apex Industrial Supplies WLL with your Request for Quotation (RFQ Ref: {rfq_ref or 'N/A'}).\n\n"
            f"We are pleased to submit our formal quotation {quote_number} for your review. "
            f"The complete commercial document detailing pricing, specifications, and terms is attached to this email as a PDF.\n\n"
            f"Quotation Summary:\n"
            f"- Total Quote Value: {float(total_amount):,.2f} QAR\n"
            f"- Quotation Ref: {quote_number}\n\n"
            f"Line Items Summary:\n"
        )
        for item in items:
            unit = item[5] or "pcs"
            status_tag = item[4] or "IN_STOCK"
            lead_time_info = " (Factory Lead Time: 7-14 Days)" if status_tag in ["PARTIAL_STOCK", "OUT_OF_STOCK"] else ""
            email_body += f"- {item[0]}: Quoted Qty: {float(item[1]):g} {unit} | Unit Price: {float(item[2]):,.2f} QAR | Status: {status_tag}{lead_time_info}\n"
            
        email_body += (
            f"\nStandard delivery terms for in-stock items are 3 to 5 business days.\n"
            f"Please let us know if you require any adjustments or have technical questions.\n\n"
            f"Sincerely,\n\n"
            f"Sales Department\n"
            f"Apex Industrial Supplies WLL\n"
            f"Doha, Qatar"
        )

    # 3. Create MIME Message
    from_email = "sales@apexsuppliesqa.com"
    to_email = f"sales@{buyer_company.lower().replace(' ', '')}.com" if buyer_company else "procurement@client.com"
    reply_subject = f"Re: {subject}" if subject and not subject.lower().startswith("re:") else (subject or "Quotation Draft")
    
    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = reply_subject
    msg["Message-ID"] = f"<reply-{quote_number}@apexsuppliesqa.com>"
    
    # Attach body
    msg.attach(MIMEText(email_body, "plain", "utf-8"))
    
    # Attach PDF
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pdf_path = os.path.join(base_dir, 'generated_quotes', f"{quote_number}.pdf")
    
    if os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f"attachment; filename={quote_number}.pdf"
            )
            msg.attach(part)
        print(f"PDF {quote_number}.pdf attached successfully.")
    else:
        print(f"Warning: PDF file {pdf_path} not found. Sending email without attachment.")

    # Save EML file to disk
    eml_dir = os.path.join(base_dir, 'generated_quotes')
    os.makedirs(eml_dir, exist_ok=True)
    eml_path = os.path.join(eml_dir, f"{quote_number}_reply.eml")
    with open(eml_path, "w") as f:
        f.write(msg.as_string())
    print(f"MIME email draft saved as EML at: {eml_path}")

    # 4. Save outgoing email to Supabase database (apex_emails)
    print("Persisting drafted reply email to Supabase apex_emails table...")
    async with engine.begin() as conn:
        # Check if already exists to prevent duplicate runs
        dup_res = await conn.execute(
            text("SELECT id FROM apex_emails WHERE message_id = :msg_id;"),
            {"msg_id": f"msg-reply-{quote_number}"}
        )
        if not dup_res.first():
            await conn.execute(
                text("""
                    INSERT INTO apex_emails 
                    (conversation_id, message_id, in_reply_to, sender, body_text, received_at) 
                    VALUES (:conv_id, :msg_id, :in_reply, :sender, :body, :received);
                """),
                {
                    "conv_id": conv_id,
                    "msg_id": f"msg-reply-{quote_number}",
                    "in_reply": f"msg-{conv_id}",
                    "sender": from_email,
                    "body": email_body,
                    "received": None # Outgoing mail has no received_at
                }
            )
            
            # Update quotation status in apex_quotations to 'sent' (or keep 'draft' as needed)
            await conn.execute(
                text("UPDATE apex_quotations SET status = 'sent' WHERE id = :quote_id;"),
                {"quote_id": quote_id}
            )
            print("Database record successfully saved.")
        else:
            print("Database record already exists. Skipped insertion.")

    return eml_path

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python email_drafter.py <quote_number>")
        sys.exit(1)
    asyncio.run(draft_quote_reply(sys.argv[1]))
