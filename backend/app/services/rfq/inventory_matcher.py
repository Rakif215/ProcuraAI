import os
import sys
import json
import time
import logging
from typing import List, Dict, Any
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

# Insert path to override local package shadowing
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../backend')))

from app.db.client import supabase
from app.core.config import settings

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def build_matcher_llm():
    """
    Build the LLM engine with:
    Main: Nvidia DeepSeek V4 Flash
    Fallback 1: Groq Llama 3.3
    Fallback 2: Gemini 2.0 Flash
    """
    llms = []
    
    # 1. Main: DeepSeek via Nvidia
    if settings.nvidia_api_key:
        logger.info("Initializing Nvidia DeepSeek V4 Flash as primary LLM...")
        llms.append(ChatOpenAI(
            api_key=settings.nvidia_api_key,
            base_url=settings.nvidia_base_url,
            model=settings.nvidia_model,
            temperature=0.1,
            max_retries=0,
            timeout=5.0,
        ))
        
    # 2. Fallback 1: Groq Llama 3.3
    if settings.groq_api_key:
        logger.info("Initializing Groq Llama 3.3 as fallback 1...")
        llms.append(ChatOpenAI(
            api_key=settings.groq_api_key,
            base_url="https://api.groq.com/openai/v1",
            model=settings.groq_model,
            temperature=0.1,
            max_retries=0,
        ))
        
    # 3. Fallback 2: Gemini 2.0 Flash
    if settings.gemini_api_key:
        logger.info("Initializing Gemini 2.0 Flash as fallback 2...")
        llms.append(ChatOpenAI(
            api_key=settings.gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            model="gemini-2.0-flash",
            temperature=0.1,
            max_retries=0,
        ))
        
    if not llms:
        raise RuntimeError("No LLM keys configured in .env")
        
    primary = llms[0]
    if len(llms) > 1:
        primary = primary.with_fallbacks(llms[1:])
    return primary

MATCHER_SYSTEM_PROMPT = """You are an AI Inventory matching expert at Apex Industrial Supplies.
Your job is to compare a requested product line item from an RFQ against our active inventory catalog. You must determine if we sell a matching product and output a structured JSON match record.

Strict Matching Rules:
1. Material & Type Fit: The requested item must match the inventory item's core category and material (e.g. Copper wire does not match PVC pipes).
2. Sizing/Dimensional Alignment: Physical dimensions must align (e.g. 70mm2 grounding cable matches 70mm2 copper earth cable; but 70mm2 grounding cable DOES NOT match 25mm2 cable; 3-inch pipes DO NOT match 4-inch pipes).
3. If no catalog item matches core materials and sizes, you MUST mark matched_inventory_id as null and match_status as OUT_OF_STOCK.
4. Confidence Guardrail: Output confidence >= 0.85 only if materials and sizing align. Otherwise, reject the match (set matched_inventory_id to null).

JSON Schema to return:
{
  "matched_inventory_id": "UUID of the matched item, or null if no fit",
  "matched_inventory_name": "Exact item name from catalog, or null if no fit",
  "match_status": "FULL_STOCK" | "PARTIAL_STOCK" | "OUT_OF_STOCK",
  "shortage_quantity": float (requested quantity minus quantity available in stock, or 0 if enough. If unmatched or quantity available is 0, this is the entire requested quantity),
  "confidence": float (0.0 to 1.0),
  "reasoning": "1-sentence technical explanation of why this catalog item was matched or rejected"
}
"""

def clean_json_string(text: str) -> str:
    """Helper to extract JSON substrings from raw text response."""
    text = text.strip()
    first_brace = text.find("{")
    first_bracket = text.find("[")
    
    start_idx = -1
    if first_brace != -1 and first_bracket != -1:
        start_idx = min(first_brace, first_bracket)
    elif first_brace != -1:
        start_idx = first_brace
    elif first_bracket != -1:
        start_idx = first_bracket
        
    last_brace = text.rfind("}")
    last_bracket = text.rfind("]")
    
    end_idx = -1
    if last_brace != -1 and last_bracket != -1:
        end_idx = max(last_brace, last_bracket)
    elif last_brace != -1:
        end_idx = last_brace
    elif last_bracket != -1:
        end_idx = last_bracket
        
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        return text[start_idx:end_idx+1].strip()
    return text.strip()

def match_items_fallback(item_name: str, spec: str, qty: float, catalog: list) -> dict:
    """
    Finds the best catalog item match using deterministic keyword analysis.
    """
    best_match = None
    best_score = 0
    
    name_lower = item_name.lower()
    spec_lower = spec.lower() if spec else ""
    
    import re
    for cat_item in catalog:
        cat_name_lower = cat_item["item_name"].lower()
        cat_spec_lower = (cat_item.get("specification") or "").lower()
        
        score = 0
        # Check size numbers match (e.g. 70, 16, 25, 2.5)
        req_numbers = set(re.findall(r'\b\d+(?:\.\d+)?\b', name_lower + " " + spec_lower))
        cat_numbers = set(re.findall(r'\b\d+(?:\.\d+)?\b', cat_name_lower + " " + cat_spec_lower))
        
        number_match = len(req_numbers.intersection(cat_numbers))
        score += number_match * 10
        
        # Word token overlap
        req_words = set(re.findall(r'\b[a-z]{3,}\b', name_lower))
        cat_words = set(re.findall(r'\b[a-z]{3,}\b', cat_name_lower))
        word_overlap = len(req_words.intersection(cat_words))
        score += word_overlap
        
        # Boost for matching core categories
        categories = ["cable", "conduit", "mcb", "wire", "gland", "switch", "box", "driver", "pipe"]
        for cat in categories:
            if cat in name_lower and cat in cat_name_lower:
                score += 15
                
        if score > best_score and score > 5:
            best_score = score
            best_match = cat_item
            
    if best_match:
        qty_avail = float(best_match["quantity_on_hand"])
        status = "FULL_STOCK" if qty_avail >= qty else ("PARTIAL_STOCK" if qty_avail > 0 else "OUT_OF_STOCK")
        shortage = max(0.0, qty - qty_avail)
        
        return {
            "matched_inventory_id": best_match["id"],
            "matched_inventory_name": best_match["item_name"],
            "match_status": status,
            "shortage_quantity": shortage,
            "confidence": 0.90,
            "reasoning": f"Deterministic fallback match with {best_match['item_name']} (score: {best_score})"
        }
        
    return {
        "matched_inventory_id": None,
        "matched_inventory_name": None,
        "match_status": "OUT_OF_STOCK",
        "shortage_quantity": qty,
        "confidence": 0.0,
        "reasoning": "No matching inventory item found in catalog via fallback search"
    }

def match_inventory(target_conv_id: str = None):
    llm = build_matcher_llm()
    
    # Load extracted line items
    extracted_items_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../extracted_items.json'))
    if not os.path.exists(extracted_items_path):
        print(f"Error: {extracted_items_path} does not exist. Run extractor.py first.")
        sys.exit(1)
        
    with open(extracted_items_path, 'r') as f:
        extracted_conversations = json.load(f)
        
    print(f"Loaded {len(extracted_conversations)} conversations from extracted_items.json.")
    
    if target_conv_id:
        extracted_conversations = [c for c in extracted_conversations if c["conversation_id"] == target_conv_id]
        print(f"Filtered matching to target conversation: {target_conv_id} (Found: {len(extracted_conversations)})")
    else:
        extracted_conversations = extracted_conversations[:1]
        print(f"No target conversation specified. Matching first conversation only: {extracted_conversations[0]['conversation_id']}")
    
    # Load inventory from Supabase
    print("Loading inventory catalog from Supabase...")
    inv_res = supabase.table("apex_inventory").select("id, item_name, specification, quantity_on_hand, unit, selling_price").execute()
    catalog = inv_res.data
    print(f"Loaded {len(catalog)} catalog items.")
    
    # Build a simplified catalog string to feed to LLM context
    catalog_str = json.dumps([
        {
            "id": item["id"],
            "item_name": item["item_name"],
            "specification": item["specification"],
            "quantity_on_hand": float(item["quantity_on_hand"]),
            "unit": item["unit"]
        }
        for item in catalog
    ], indent=2)
    
    matched_results = []
    
    for idx, conv in enumerate(extracted_conversations):
        rfq_ref = conv["rfq_ref"]
        subject = conv["original_subject"]
        line_items = conv["line_items"]
        
        print(f"\n[{idx+1}/{len(extracted_conversations)}] Matching items for: {subject} (Ref: {rfq_ref})")
        matched_items_list = []
        
        for item_idx, item in enumerate(line_items):
            item_name = item["item_name"]
            spec = item["specification"]
            qty = float(item["quantity"])
            unit = item["unit"]
            
            print(f"  Extracted Line: {item_name} (Spec: {spec}) | Requested: {qty} {unit}")
            
            # Construct comparison prompt
            prompt = (
                f"Requested Item Details:\n"
                f"- Name: {item_name}\n"
                f"- Spec: {spec}\n"
                f"- Requested Qty: {qty}\n"
                f"- Unit: {unit}\n\n"
                f"Apex Inventory Catalog:\n"
                f"{catalog_str}"
            )
            
            # Retry loop for LLM matcher
            retries = 1
            delay = 4
            success = False
            match_data = None
            
            for attempt in range(retries):
                try:
                    messages = [
                        SystemMessage(content=MATCHER_SYSTEM_PROMPT),
                        HumanMessage(content=prompt)
                    ]
                    response = llm.invoke(messages)
                    raw_content = clean_json_string(response.content)
                    match_data = json.loads(raw_content)
                    success = True
                    break
                except Exception as e:
                    print(f"    Attempt {attempt+1} failed: {e}")
                    if attempt < retries - 1:
                        time.sleep(delay)
            if not success or not match_data:
                print(f"    WARNING: LLM matching failed for {item_name}. Falling back to deterministic keyword matching...")
                match_data = match_items_fallback(item_name, spec, qty, catalog)
                success = True
                
            if success and match_data:
                # Add price details from catalog if matched
                matched_id = match_data.get("matched_inventory_id")
                selling_price = None
                qty_quoted = 0.0
                
                if matched_id:
                    # Find price in catalog
                    inv_item = next((x for x in catalog if x["id"] == matched_id), None)
                    if inv_item:
                        selling_price = float(inv_item["selling_price"])
                        qty_on_hand = float(inv_item["quantity_on_hand"])
                        
                        # Calculate quantity quoted based on stock status
                        if match_data["match_status"] == "FULL_STOCK":
                            qty_quoted = qty
                        elif match_data["match_status"] == "PARTIAL_STOCK":
                            qty_quoted = qty_on_hand
                        else:
                            qty_quoted = 0.0
                            
                item_match_record = {
                    "item_no": item.get("item_no", item_idx + 1),
                    "item_name": item_name,
                    "specification": spec,
                    "quantity_requested": qty,
                    "unit": unit,
                    "matched_inventory_id": matched_id,
                    "matched_inventory_name": match_data.get("matched_inventory_name"),
                    "matched_selling_price": selling_price,
                    "quantity_quoted": qty_quoted,
                    "match_status": match_data.get("match_status"),
                    "shortage_quantity": match_data.get("shortage_quantity"),
                    "confidence": match_data.get("confidence"),
                    "reasoning": match_data.get("reasoning")
                }
                
                print(f"    Match Result: {item_match_record['matched_inventory_name']} | Status: {item_match_record['match_status']} | Shortage: {item_match_record['shortage_quantity']} (Reason: {item_match_record['reasoning']})")
                matched_items_list.append(item_match_record)
            else:
                print(f"    Failed to match line item: {item_name}")
                
            # No sleep
            
        matched_results.append({
            "email_id": conv["email_id"],
            "conversation_id": conv["conversation_id"],
            "rfq_ref": rfq_ref,
            "original_subject": subject,
            "matched_items": matched_items_list
        })
        # No sleep
        
    output_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../parsed_matches.json'))
    with open(output_path, "w") as f:
        json.dump(matched_results, f, indent=2)
        
    print(f"\nSuccessfully stored all matching results in {output_path}")
    print("[JSON_START]")
    print(json.dumps(matched_results, indent=2))
    print("[JSON_END]")

if __name__ == "__main__":
    match_inventory()
