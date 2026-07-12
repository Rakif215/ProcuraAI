import os
import sys
import json
import asyncio
from datetime import datetime
from sqlalchemy import text

# Insert the parent backend directory at index 0 to override local package shadowing
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../backend')))

from app.db.client import engine

async def run_migrations(conn):
    print("Ensuring buyer_company and rfq_ref columns exist in apex_conversations...")
    await conn.execute(text("ALTER TABLE apex_conversations ADD COLUMN IF NOT EXISTS buyer_company TEXT;"))
    await conn.execute(text("ALTER TABLE apex_conversations ADD COLUMN IF NOT EXISTS rfq_ref TEXT;"))
    print("Database columns verified.")

async def clear_data(conn):
    print("Clearing previous database records...")
    # Delete from leaves to roots
    await conn.execute(text("DELETE FROM apex_rfq_line_items WHERE id IS NOT NULL;"))
    await conn.execute(text("DELETE FROM apex_emails WHERE id IS NOT NULL;"))
    await conn.execute(text("DELETE FROM apex_conversations WHERE id IS NOT NULL;"))
    print("Previous records cleared.")

async def persist_data():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    conversations_path = os.path.join(base_dir, 'parsed_conversations.json')
    extracted_items_path = os.path.join(base_dir, 'extracted_items.json')
    
    with open(conversations_path) as f:
        conversations = json.load(f)
    with open(extracted_items_path) as f:
        extracted = json.load(f)

    extracted_by_conv = {item["conversation_id"]: item for item in extracted}
    
    async with engine.begin() as conn:
        # Run migrations and clear previous records
        await run_migrations(conn)
        await clear_data(conn)

        # 1. Insert conversations
        print(f"Inserting {len(conversations)} conversations...")
        for conv in conversations:
            conv_id = conv["conversation_id"]
            buyer_company = conv.get("buyer_company")
            buyer_name = conv.get("buyer_contact_person")
            rfq_ref = conv.get("rfq_ref")
            subject = conv.get("original_subject")
            
            await conn.execute(
                text("INSERT INTO apex_conversations (id, subject, buyer_name, buyer_company, rfq_ref, current_status) VALUES (:id, :subject, :buyer_name, :buyer_company, :rfq_ref, :current_status);"),
                {
                    "id": conv_id,
                    "subject": subject,
                    "buyer_name": buyer_name,
                    "buyer_company": buyer_company,
                    "rfq_ref": rfq_ref,
                    "current_status": "pending_review"
                }
            )
            
            # 2. Insert email
            await conn.execute(
                text("INSERT INTO apex_emails (conversation_id, message_id, in_reply_to, sender, body_text, received_at) VALUES (:conversation_id, :message_id, :in_reply_to, :sender, :body_text, :received_at);"),
                {
                    "conversation_id": conv_id,
                    "message_id": f"msg-{conv_id}",
                    "in_reply_to": None,
                    "sender": f"sales@{buyer_company.lower().replace(' ', '')}.com" if buyer_company else "unknown@buyer.com",
                    "body_text": conv.get("summary", ""),
                    "received_at": datetime.fromisoformat("2026-07-01T12:00:00+00:00")
                }
            )
            
            # 3. Insert Line Items
            matching_extracted = extracted_by_conv.get(conv_id)
            if matching_extracted:
                for item in matching_extracted.get("line_items", []):
                    await conn.execute(
                        text("INSERT INTO apex_rfq_line_items (conversation_id, item_name, specification, quantity_requested, unit) VALUES (:conversation_id, :item_name, :specification, :quantity_requested, :unit);"),
                        {
                            "conversation_id": conv_id,
                            "item_name": item.get("item_name"),
                            "specification": item.get("specification"),
                            "quantity_requested": item.get("quantity"),
                            "unit": item.get("unit")
                        }
                    )
                    
    print("Database persistence complete!")

async def main():
    await persist_data()

if __name__ == "__main__":
    asyncio.run(main())
