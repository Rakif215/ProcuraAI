import os
import sys
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timezone

# Insert path to override local package shadowing
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../backend')))

from app.db.client import supabase

# IMAP Configuration
IMAP_HOST = "mail.spacemail.com"
IMAP_PORT = 993
EMAIL_ADDRESS = "connect@mafaz.me"
EMAIL_PASSWORD = "Space@1987"

def clean_text(text: bytes, encoding: str) -> str:
    if not encoding:
        encoding = "utf-8"
    try:
        return text.decode(encoding, errors="ignore")
    except Exception:
        return str(text)

def ingest_live_emails(tenant_id: str):
    print(f"Connecting to IMAP server {IMAP_HOST}:{IMAP_PORT}...")
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
    mail.select("INBOX")
    
    # Search for our generated RFQ subjects
    status, messages = mail.search(None, '(SUBJECT "RFQ-2026-")')
    if status != "OK":
        print("No RFQ-2026 emails found.")
        return
        
    mail_ids = messages[0].split()
    print(f"Found {len(mail_ids)} live RFQ emails in inbox.")
    
    # Also search for the URGENT Safety email
    status_urg, messages_urg = mail.search(None, '(SUBJECT "URGENT: Safety PPE")')
    if status_urg == "OK" and messages_urg[0]:
        mail_ids.extend(messages_urg[0].split())
        # Remove duplicates
        mail_ids = list(set(mail_ids))
        
    print(f"Total target emails to process: {len(mail_ids)}")
    
    # Fetch existing message IDs to do incremental sync
    existing_message_ids = set()
    try:
        exist_res = supabase.table("apex_emails").select("message_id").execute()
        if exist_res.data:
            existing_message_ids = {e["message_id"] for e in exist_res.data if e.get("message_id")}
        print(f"Loaded {len(existing_message_ids)} existing email Message-IDs from database.")
    except Exception as e:
        print("Warning reading existing emails:", e)

    count = 0
    for mail_id in mail_ids:
        # Fetch only header block first to check if already exists
        res_h, data_h = mail.fetch(mail_id, "(BODY[HEADER])")
        if res_h != "OK" or not data_h or not data_h[0]:
            continue
            
        header_msg = email.message_from_bytes(data_h[0][1])
        msg_uid = header_msg["Message-ID"] or f"<msg-live-{mail_id.decode()}@apex-auto.com>"
        
        if msg_uid in existing_message_ids:
            print(f"Skipping already ingested email: {msg_uid}")
            continue
            
        # Fetch full email bytes since it is new
        res, data = mail.fetch(mail_id, "(RFC822)")
        if res != "OK":
            continue
            
        raw_email = data[0][1]
        msg = email.message_from_bytes(raw_email)
        
        # Decode subject
        subject, encoding = decode_header(msg["Subject"])[0]
        if isinstance(subject, bytes):
            subject = clean_text(subject, encoding)
            
        # Decode sender
        sender, encoding = decode_header(msg["From"])[0]
        if isinstance(sender, bytes):
            sender = clean_text(sender, encoding)
            
        print(f"Ingesting email: {subject} from {sender}")
        
        # Extract body text (check both plain and html parts)
        body = ""
        html_body = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))
                if "attachment" not in content_disposition:
                    if content_type == "text/plain":
                        body = clean_text(part.get_payload(decode=True), part.get_content_charset())
                    elif content_type == "text/html":
                        html_body = clean_text(part.get_payload(decode=True), part.get_content_charset())
        else:
            content_type = msg.get_content_type()
            if content_type == "text/html":
                html_body = clean_text(msg.get_payload(decode=True), msg.get_content_charset())
            else:
                body = clean_text(msg.get_payload(decode=True), msg.get_content_charset())
                
        final_body = body if body.strip() else html_body
        
        # Parse sender details
        buyer_name = "Unknown"
        buyer_email = sender
        if " <" in sender:
            buyer_name = sender.split(" <")[0].strip('" ')
            buyer_email = sender.split(" <")[1].strip("> ")
            
        # 1. Insert Conversation
        conv_res = supabase.table("apex_conversations").insert({
            "subject": subject,
            "buyer_email": buyer_email,
            "buyer_name": buyer_name,
            "current_status": "pending_review",
            "tenant_id": tenant_id
        }).execute()
        
        conv_id = conv_res.data[0]["id"]
        
        # 2. Insert Email Message
        msg_uid = msg["Message-ID"] or f"<msg-live-{mail_id.decode()}@apex-auto.com>"
        supabase.table("apex_emails").insert({
            "conversation_id": conv_id,
            "message_id": msg_uid,
            "sender": sender,
            "body_text": final_body,
            "received_at": datetime.now(timezone.utc).isoformat()
        }).execute()
        
        count += 1
        
    print(f"Live Ingestion complete! Successfully processed {count} emails into database.")
    mail.close()
    mail.logout()

if __name__ == "__main__":
    ingest_live_emails()
