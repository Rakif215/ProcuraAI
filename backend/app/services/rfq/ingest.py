import os
import sys
from datetime import datetime, timezone

# Insert path to override local package shadowing
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../backend')))

from app.db.client import supabase

def parse_email_text(content: str) -> dict:
    lines = content.strip().split("\n")
    email_data = {"sender": "", "subject": "", "body": ""}
    body_lines = []
    in_body = False
    
    for line in lines:
        if line.startswith("Sender:"):
            email_data["sender"] = line.split("Sender:", 1)[1].strip()
        elif line.startswith("Subject:"):
            email_data["subject"] = line.split("Subject:", 1)[1].strip()
        elif line.startswith("Body:"):
            email_data["body"] = line.split("Body:", 1)[1].strip()
            in_body = True
        elif in_body:
            body_lines.append(line)
            
    if body_lines:
        email_data["body"] += "\n" + "\n".join(body_lines)
    return email_data

def ingest_samples():
    emails_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../samples/emails'))
    attachments_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../samples/attachments'))

    print("Starting Ingestion of 10 Mock Conversations...")
    
    for i in range(1, 11):
        num_str = f"{i:02d}"
        email_file = f"email_{num_str}.txt"
        att_file = f"rfq_{num_str}.txt"
        
        email_path = os.path.join(emails_dir, email_file)
        att_path = os.path.join(attachments_dir, att_file)
        
        if not os.path.exists(email_path) or not os.path.exists(att_path):
            print(f"Error: Missing mock files for index {num_str}")
            continue
            
        with open(email_path, "r") as f:
            email_content = f.read()
        with open(att_path, "r") as f:
            att_content = f.read()
            
        parsed = parse_email_text(email_content)
        
        # Parse sender name and email
        sender_raw = parsed["sender"]
        buyer_name = "Unknown"
        buyer_email = sender_raw
        
        if " <" in sender_raw:
            buyer_name = sender_raw.split(" <")[0].strip('" ')
            buyer_email = sender_raw.split(" <")[1].strip("> ")
        elif "|" in sender_raw:
            buyer_name = sender_raw.split("|")[1].strip()
            buyer_email = sender_raw.split("|")[0].strip()
            
        print(f"Ingesting: {parsed['subject']} from {buyer_email}")
        
        # 1. Insert Conversation
        conv_res = supabase.table("apex_conversations").insert({
            "subject": parsed["subject"],
            "buyer_email": buyer_email,
            "buyer_name": buyer_name,
            "current_status": "pending_review"
        }).execute()
        
        conv_id = conv_res.data[0]["id"]
        
        # 2. Insert Email Message (store attachment path in body/meta)
        msg_id = f"<msg-2026-rfq-{num_str}@apex-auto.com>"
        supabase.table("apex_emails").insert({
            "conversation_id": conv_id,
            "message_id": msg_id,
            "sender": sender_raw,
            "body_text": f"{parsed['body']}\n\n[Attachment: {att_file} - Local Path: {att_path}]",
            "received_at": datetime.now(timezone.utc).isoformat()
        }).execute()
        
    print("Ingestion completed successfully!")

if __name__ == "__main__":
    ingest_samples()
