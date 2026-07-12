import os
import sys
import json
import asyncio
from sqlalchemy import text

# Insert the parent backend directory at index 0 to override local package shadowing
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../backend')))

from app.db.client import engine

async def generate_quotations(target_conv_id: str = None):
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    matches_path = os.path.join(base_dir, 'parsed_matches.json')
    
    if not os.path.exists(matches_path):
        print(f"Error: {matches_path} does not exist. Run inventory_matcher.py first.")
        sys.exit(1)
        
    with open(matches_path) as f:
        matches_data = json.load(f)

    async with engine.begin() as conn:
        if target_conv_id:
            print(f"Clearing previous quotation drafts for conversation {target_conv_id}...")
            await conn.execute(
                text("DELETE FROM apex_quotation_line_items WHERE quotation_id IN (SELECT id FROM apex_quotations WHERE conversation_id = :conv_id);"),
                {"conv_id": target_conv_id}
            )
            await conn.execute(
                text("DELETE FROM apex_quotations WHERE conversation_id = :conv_id;"),
                {"conv_id": target_conv_id}
            )
        else:
            print("Clearing all previous quotation drafts...")
            await conn.execute(text("DELETE FROM apex_quotation_line_items WHERE id IS NOT NULL;"))
            await conn.execute(text("DELETE FROM apex_quotations WHERE id IS NOT NULL;"))
        print("Previous quotation drafts cleared.")

    async with engine.begin() as conn:
        # Loop and find the matching conversation
        for idx, match_record in enumerate(matches_data):
            conv_id = match_record["conversation_id"]
            if target_conv_id and conv_id != target_conv_id:
                continue
                
            rfq_ref = match_record["rfq_ref"]
            
            # Generate next quote sequence number
            count_res = await conn.execute(
                text("SELECT COUNT(*) FROM apex_quotations WHERE conversation_id != :conv_id;"),
                {"conv_id": conv_id}
            )
            next_seq = count_res.scalar() + 1
            quote_number = f"QT-2026-{next_seq:03d}"
            
            # Fetch tenant_id from conversation
            tenant_res = await conn.execute(
                text("SELECT tenant_id FROM apex_conversations WHERE id = :conv_id;"),
                {"conv_id": conv_id}
            )
            tenant_row = tenant_res.first()
            tenant_id = tenant_row[0] if tenant_row else None
            
            # 1. Fetch matching RFQ line items from DB to map the IDs back correctly
            lines_res = await conn.execute(
                text("SELECT id, item_name FROM apex_rfq_line_items WHERE conversation_id = :conv_id;"),
                {"conv_id": conv_id}
            )
            db_rfq_lines = {row[1].lower().strip(): row[0] for row in lines_res.all()}
            
            # 2. Insert Quotation Header
            print(f"Creating quotation draft {quote_number} for conversation {conv_id}...")
            quote_res = await conn.execute(
                text("INSERT INTO apex_quotations (conversation_id, quote_number, total_amount, status, tenant_id) VALUES (:conv_id, :quote_number, 0.00, 'draft', :tenant_id) RETURNING id;"),
                {
                    "conv_id": conv_id,
                    "quote_number": quote_number,
                    "tenant_id": tenant_id
                }
            )
            quote_id = quote_res.scalar()
            
            total_quote_amount = 0.00
            
            # 3. Process matched items and insert line items
            for item in match_record.get("matched_items", []):
                req_item_name = item.get("item_name", "")
                # Find matching rfq line item id
                rfq_line_id = db_rfq_lines.get(req_item_name.lower().strip())
                
                # Fetch price, quantity, status
                status = item.get("match_status", "OUT_OF_STOCK")
                qty_quoted = float(item.get("quantity_quoted", 0))
                unit_price = float(item.get("matched_selling_price")) if item.get("matched_selling_price") is not None else 0.00
                total_price = qty_quoted * unit_price
                shortage = float(item.get("shortage_quantity", 0))
                
                # Determine display name and spec
                display_name = item.get("matched_inventory_name") or req_item_name
                display_spec = item.get("specification") # Keep requested spec
                
                # Insert Quotation Line Item
                await conn.execute(
                    text("""
                        INSERT INTO apex_quotation_line_items 
                        (quotation_id, rfq_line_item_id, matched_inventory_id, item_name, specification, quantity_quoted, unit_price, total_price, match_status, shortage_quantity) 
                        VALUES (:quote_id, :rfq_line_id, :matched_inv_id, :item_name, :spec, :qty_quoted, :unit_price, :total_price, :status, :shortage);
                    """),
                    {
                        "quote_id": quote_id,
                        "rfq_line_id": rfq_line_id,
                        "matched_inv_id": item.get("matched_inventory_id"),
                        "item_name": display_name,
                        "spec": display_spec,
                        "qty_quoted": qty_quoted,
                        "unit_price": unit_price,
                        "total_price": total_price,
                        "status": status,
                        "shortage": shortage
                    }
                )
                
                total_quote_amount += total_price
                
            # 4. Update Quotation Header Total Amount
            print(f"Updating total amount for {quote_number} to {total_quote_amount:.2f} QAR...")
            await conn.execute(
                text("UPDATE apex_quotations SET total_amount = :total_amount WHERE id = :quote_id;"),
                {
                    "total_amount": total_quote_amount,
                    "quote_id": quote_id
                }
            )
            
            # 5. Update Conversation Status to 'quoted'
            await conn.execute(
                text("UPDATE apex_conversations SET current_status = 'quoted' WHERE id = :conv_id;"),
                {"conv_id": conv_id}
            )
            
    print("Quotation drafting complete!")

async def main():
    await generate_quotations()

if __name__ == "__main__":
    asyncio.run(main())
