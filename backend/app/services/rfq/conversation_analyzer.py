import os
import sys
import json
import time
import logging
from typing import Optional, List
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

# Insert path to override local package shadowing
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../backend')))

from app.db.client import supabase
from app.core.config import settings

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def build_analyzer_llm():
    """
    Build the LLM engine with:
    Main: Nvidia DeepSeek V4 Flash
    Fallback: Gemini 2.0 Flash
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
        ))
        
    # 2. Fallback: Gemini 2.0 Flash
    if settings.gemini_api_key:
        logger.info("Initializing Gemini 2.0 Flash as backup LLM...")
        llms.append(ChatOpenAI(
            api_key=settings.gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            model="gemini-2.0-flash",
            temperature=0.1,
            max_retries=0,
        ))
        
    if not llms:
        raise RuntimeError("Neither NVIDIA_API_KEY nor GEMINI_API_KEY is configured in .env")
        
    primary = llms[0]
    if len(llms) > 1:
        primary = primary.with_fallbacks(llms[1:])
    return primary

# Strict JSON Instruction Prompt
ANALYZER_SYSTEM_PROMPT = """You are an AI Sales Operations Assistant at Apex Industrial Supplies.
Your job is to read an incoming email from a construction or procurement buyer, analyze the conversation, and return a clean JSON object containing the classified intent and key metadata.

You must return EXACTLY a JSON object with the following fields and structure. Do not output any conversational introduction, markdown wrapper (like ```json), or trailing notes.

JSON Schema:
{
  "intent": "NEW_RFQ" | "NEGOTIATION_AMENDMENT" | "NEGOTIATION_PRICE" | "GENERAL_INQUIRY" | "REGRET" | "OTHER",
  "confidence": float (0.0 to 1.0),
  "summary": "1-sentence summary of the email",
  "buyer_company": "Name of the buyer's company, or null if unknown",
  "buyer_contact_person": "Name of the person who sent the email, or null if unknown",
  "rfq_ref": "Extracted RFQ reference code (e.g., RFQ-2026-ELECT-01), or null if not present",
  "priority": "HIGH" | "MEDIUM" | "LOW",
  "action_items": ["List of next actions required"]
}

Guidelines for 'intent':
- NEW_RFQ: Email contains a request for a quote, pricing list, or delivery lead times for specific line items.
- NEGOTIATION_AMENDMENT: Buyer is negotiating specifications, variations, quantities, or delivery times for an existing quote.
- NEGOTIATION_PRICE: Buyer is asking for discounts or lower prices.
- GENERAL_INQUIRY: Asking for catalogs, company details, or general info.
- REGRET: Rejection of our offer or announcement that the bid was lost.
- OTHER: Out-of-office replies, spam, etc.
"""

def clean_json_string(text: str) -> str:
    """Helper to remove markdown wrappers if the LLM returned them."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()

def analyze_conversations():
    llm = build_analyzer_llm()
    
    print("Loading emails from apex_emails...")
    emails_res = supabase.table("apex_emails").select("id, conversation_id, sender, body_text").execute()
    emails = emails_res.data
    print(f"Loaded {len(emails)} emails.")
    
    # Load associated conversations to match subjects
    convs_res = supabase.table("apex_conversations").select("id, subject").execute()
    conv_map = {c["id"]: c["subject"] for c in convs_res.data}
    
    parsed_results = []
    
    for idx, email in enumerate(emails):
        subject = conv_map.get(email["conversation_id"], "No Subject")
        print(f"\n[{idx+1}/{len(emails)}] Analyzing: {subject}")
        
        prompt = f"Subject: {subject}\nSender: {email['sender']}\nBody:\n{email['body_text']}"
        
        # Exponential backoff retry loop (max 5 retries)
        retries = 5
        delay = 4
        success = False
        parsed_data = None
        
        for attempt in range(retries):
            try:
                messages = [
                    SystemMessage(content=ANALYZER_SYSTEM_PROMPT),
                    HumanMessage(content=prompt)
                ]
                response = llm.invoke(messages)
                raw_content = clean_json_string(response.content)
                parsed_data = json.loads(raw_content)
                success = True
                break
            except Exception as e:
                print(f"  Attempt {attempt+1} failed with error: {e}")
                if attempt < retries - 1:
                    print(f"  Waiting {delay} seconds before retrying...")
                    time.sleep(delay)
                    delay *= 2  # Exponential backoff
                    
        if success and parsed_data:
            # Add database identifier references
            parsed_data["email_id"] = email["id"]
            parsed_data["conversation_id"] = email["conversation_id"]
            parsed_data["original_subject"] = subject
            
            print(f"  Parsed Intent: {parsed_data.get('intent')} | Company: {parsed_data.get('buyer_company')} | Ref: {parsed_data.get('rfq_ref')}")
            parsed_results.append(parsed_data)
        else:
            print(f"  Failed to analyze email {email['id']} after {retries} attempts.")
            
        # Standard sleep to prevent aggressive API rate-limiting
        time.sleep(3)
            
    # Save the parsed results in a local JSON file
    output_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../parsed_conversations.json'))
    with open(output_path, "w") as f:
        json.dump(parsed_results, f, indent=2)
        
    print(f"\nSuccessfully stored {len(parsed_results)} parsed results in {output_path}")
    print("[JSON_START]")
    print(json.dumps(parsed_results, indent=2))
    print("[JSON_END]")

if __name__ == "__main__":
    analyze_conversations()
