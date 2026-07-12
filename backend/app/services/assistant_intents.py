"""
Context-aware action intents for Orion Lite.

The intent system is hierarchical:
1. Universal action family: acknowledge, request_information, confirm,
   schedule, follow_up, negotiate, escalate, save_for_later, digest.
2. Role/domain adapter: logistics, finance, sales, founder, engineering.
3. Safe fallback: useful generic suggestion if no specific mapping is confident.

This stays deterministic and cheap for the pilot; LLM refinement can be layered
on later for ambiguous high-value emails.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional


AMOUNT_RE = re.compile(
    r"(?P<currency>QAR|AED|SAR|USD|EUR|GBP|QR|\$|€|£)\s?(?P<amount>\d[\d,]*(?:\.\d{1,2})?)|"
    r"(?P<amount_alt>\d[\d,]*(?:\.\d{1,2})?)\s?(?P<currency_alt>QAR|AED|SAR|USD|EUR|GBP|QR)",
    re.IGNORECASE,
)
PO_RE = re.compile(r"\b(?:PO|P\.O\.|purchase order)[\s_:#-]*([A-Z0-9][A-Z0-9\-_/]{2,})\b", re.IGNORECASE)
INVOICE_RE = re.compile(
    r"\b(?:invoice|inv)\s*(?:#|number|no\.?)\s*([A-Z0-9][A-Z0-9\-_/]{2,})\b",
    re.IGNORECASE,
)
DATE_RE = re.compile(
    r"\b(?:by|before|on|due|eta|delivery date|payment date)?\s*"
    r"(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|"
    r"(?:today|tomorrow|eod|end of day|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
    re.IGNORECASE,
)


@dataclass
class CommercialExtraction:
    document_type: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    due_date: Optional[str] = None
    po_number: Optional[str] = None
    invoice_number: Optional[str] = None
    negotiation_opportunity: bool = False
    missing_document: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, "", False)}


@dataclass
class AssistantIntent:
    intent_family: str
    domain_intent: str
    confidence: float
    situation: str
    action: str
    label: str
    prompt: str
    commercial: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["confidence"] = round(float(self.confidence), 2)
        return payload


def classify_email_intent(email: dict, signals: Optional[dict] = None) -> dict:
    signals = signals or {}
    role = (signals.get("role") or "generic").lower()
    category = (email.get("category") or "").lower()
    needs_reply = bool(email.get("needs_response")) and not bool(email.get("responded"))
    subject = email.get("subject") or ""
    summary = email.get("summary") or ""
    body = email.get("body_text") or ""
    text = _compact_text(subject, summary, body)
    text_l = text.lower()

    commercial = extract_commercial_fields(text, category)

    base = _classify_universal(text_l, category, needs_reply, commercial)
    refined = _adapt_for_role(base, role, category, text_l, commercial)
    return refined.to_dict()


def extract_commercial_fields(text: str, category: str = "") -> CommercialExtraction:
    lowered = text.lower()
    extraction = CommercialExtraction()

    if any(term in lowered for term in ["quote", "quotation", "proposal", "offer", "pricing"]):
        extraction.document_type = "quote"
    if "invoice" in lowered or category in {"invoice", "finance"}:
        extraction.document_type = extraction.document_type or "invoice"
    if "payment" in lowered or "overdue" in lowered:
        extraction.document_type = extraction.document_type or "payment"
    if "rfq" in lowered or "request for quote" in lowered:
        extraction.document_type = "rfq"
    if "purchase order" in lowered or re.search(r"\bpo\b", lowered):
        extraction.document_type = extraction.document_type or "po"

    amount_match = AMOUNT_RE.search(text)
    if amount_match:
        extraction.amount = (amount_match.group("amount") or amount_match.group("amount_alt") or "").replace(",", "")
        extraction.currency = (amount_match.group("currency") or amount_match.group("currency_alt") or "").upper()

    for po_match in PO_RE.finditer(text):
        candidate = po_match.group(1).strip("_-/")
        if any(char.isdigit() for char in candidate) and candidate.lower() not in {"and", "the", "status"}:
            leading_number = re.match(r"(\d{4,})", candidate)
            if leading_number:
                candidate = leading_number.group(1)
            extraction.po_number = candidate
            break

    invoice_match = INVOICE_RE.search(text)
    if invoice_match:
        extraction.invoice_number = invoice_match.group(1)

    date_match = DATE_RE.search(text)
    if date_match:
        extraction.due_date = date_match.group(1)

    if extraction.amount and any(term in lowered for term in ["discount", "counter", "negotiate", "best price", "too high", "revise", "revised"]):
        extraction.negotiation_opportunity = True
    elif extraction.document_type in {"quote", "proposal"} and extraction.amount:
        extraction.negotiation_opportunity = True

    missing_terms = {
        "invoice": ["missing invoice", "send invoice", "invoice copy", "attach invoice"],
        "packing list": ["packing list"],
        "certificate": ["certificate", "coo", "certificate of origin"],
        "tracking": ["tracking", "awb", "waybill"],
        "po": ["missing po", "purchase order copy", "po copy"],
    }
    for doc, terms in missing_terms.items():
        if any(term in lowered for term in terms):
            extraction.missing_document = doc
            break

    return extraction


def _classify_universal(
    text_l: str,
    category: str,
    needs_reply: bool,
    commercial: CommercialExtraction,
) -> AssistantIntent:
    if _has_any(text_l, ["urgent", "blocked", "escalat", "critical", "outage", "security incident", "failed", "breach"]):
        return _intent("escalate", "escalate_for_review", 0.78, "This may need quick review.", "Escalate", "This looks time-sensitive. Shall I mark it for review and draft a short acknowledgement?", commercial)

    if commercial.negotiation_opportunity:
        amount = _amount_phrase(commercial)
        return _intent("negotiate", "counter_offer", 0.82, "This looks like a quote or proposal discussion.", "Draft counter", f"This looks like a quote discussion{amount}. Should I draft a professional counter-offer?", commercial)

    if commercial.missing_document:
        return _intent("request_information", f"request_{_slug(commercial.missing_document)}", 0.86, f"They need a {commercial.missing_document}.", "Request doc", f"Want me to request the missing {commercial.missing_document}?", commercial)

    if category in {"invoice", "finance"} or commercial.document_type in {"invoice", "payment"}:
        if _has_any(text_l, ["overdue", "pending", "unpaid", "payment status", "due"]):
            return _intent("follow_up", "request_payment_status", 0.82, "This is about payment status.", "Draft follow-up", "Shall I ask for the payment status and expected date?", commercial)
        return _intent("save_for_later", "finance_review", 0.68, "This is finance-related.", "Save", "This looks finance-related. Should I keep it saved for review?", commercial)

    if _has_any(text_l, ["delivery date", "delivered", "delivery confirmation", "dispatch", "deliver today", "ddp"]) or commercial.document_type == "po":
        return _intent("confirm", "confirm_delivery_date", 0.86, "They are waiting on delivery confirmation.", "Ask date", "Shall I ask for the updated delivery date and status?", commercial)

    if _has_any(text_l, ["eta", "tracking", "awb", "waybill", "shipment status", "container", "customs", "port", "incoterm"]):
        return _intent("request_information", "ask_eta", 0.86, "This is about shipment status.", "Ask ETA", "Want me to request the latest ETA and tracking update?", commercial)

    if _has_any(text_l, ["meeting", "call", "schedule", "availability", "available", "appointment", "demo"]):
        return _intent("schedule", "suggest_times", 0.84, "They are trying to schedule time.", "Draft times", "Shall I suggest two meeting times?", commercial)

    if category in {"rfq", "quotation"} or commercial.document_type == "rfq":
        return _intent("acknowledge", "acknowledge_rfq", 0.8, "This looks like a commercial request.", "Acknowledge", "Shall I acknowledge the RFQ and say we are reviewing it?", commercial)

    if needs_reply and _has_any(text_l, ["please confirm", "confirm receipt", "received?"]):
        return _intent("confirm", "confirm_receipt", 0.78, "They want confirmation.", "Confirm", "Shall I confirm receipt and say we will follow up shortly?", commercial)

    if needs_reply:
        return _intent("request_information", "clarify_next_step", 0.58, "This needs a reply, but the next step is not fully clear.", "Clarify", "Shall I draft a short clarification reply?", commercial)

    return _intent("digest", "save_for_later", 0.52, "No immediate action is obvious.", "Save", "Should I save this for later review?", commercial)


def _adapt_for_role(
    intent: AssistantIntent,
    role: str,
    category: str,
    text_l: str,
    commercial: CommercialExtraction,
) -> AssistantIntent:
    if role in {"logistics", "operations", "manager"}:
        if intent.intent_family == "request_information" and _has_any(text_l, ["shipment", "delivery", "container", "customs", "tracking"]):
            return _replace(intent, domain_intent="ask_eta", situation="A logistics update needs timing clarity.", label="Ask ETA", prompt="Want me to ask for the latest ETA, tracking reference, and delivery status?", confidence=max(intent.confidence, 0.88))
        if intent.intent_family == "confirm" and _has_any(text_l, ["po", "purchase order", "delivery"]):
            po = f" for PO {commercial.po_number}" if commercial.po_number else ""
            return _replace(intent, domain_intent="confirm_po_status", situation="They are waiting on PO or delivery status.", label="Confirm PO", prompt=f"Shall I ask for the updated delivery date and PO status{po}?", confidence=max(intent.confidence, 0.86))

    if role in {"finance", "admin"}:
        if commercial.document_type in {"invoice", "payment"}:
            invoice = f" for invoice {commercial.invoice_number}" if commercial.invoice_number else ""
            return _replace(intent, domain_intent="request_payment_or_invoice_status", situation="This is a finance follow-up.", label="Draft finance reply", prompt=f"Shall I ask for the payment status or missing invoice details{invoice}?", confidence=max(intent.confidence, 0.84))

    if role in {"sales", "founder"}:
        if intent.intent_family == "negotiate":
            amount = _amount_phrase(commercial)
            return _replace(intent, domain_intent="commercial_counter_offer", situation="This may affect pricing or margin.", label="Draft counter", prompt=f"This looks commercially important{amount}. Should I draft a calm counter-offer?", confidence=max(intent.confidence, 0.86))
        if category in {"rfq", "quotation"}:
            return _replace(intent, domain_intent="acknowledge_and_follow_up", situation="This looks like a customer or supplier commercial request.", label="Acknowledge", prompt="Shall I acknowledge it and ask for the missing commercial details?", confidence=max(intent.confidence, 0.82))

    if role in {"it", "engineering", "engineer", "computer_engineer", "developer"}:
        if _has_any(text_l, ["bug", "error", "issue", "crash", "stack trace", "logs", "production", "deploy", "server"]):
            if _has_any(text_l, ["security", "breach", "incident", "outage", "production down"]):
                return _replace(intent, intent_family="escalate", domain_intent="escalate_security_or_outage", situation="This may be a production or security issue.", label="Escalate", prompt="This may be urgent. Shall I acknowledge it and ask for logs, impact, and current status?", confidence=max(intent.confidence, 0.88))
            return _replace(intent, intent_family="request_information", domain_intent="ask_for_logs", situation="This looks like a technical issue report.", label="Ask logs", prompt="Shall I ask for logs, screenshots, environment details, and steps to reproduce?", confidence=max(intent.confidence, 0.86))
        if _has_any(text_l, ["deployment", "maintenance window", "release"]):
            return _replace(intent, intent_family="confirm", domain_intent="confirm_deployment_window", situation="This is about a deployment window.", label="Confirm window", prompt="Shall I confirm the deployment window and ask for rollback details?", confidence=max(intent.confidence, 0.82))

    return intent


def _intent(
    family: str,
    domain: str,
    confidence: float,
    situation: str,
    label: str,
    prompt: str,
    commercial: CommercialExtraction,
) -> AssistantIntent:
    action = {
        "schedule": "suggest_meeting_times",
        "negotiate": "counter_offer",
        "save_for_later": "save_for_later",
        "digest": "save_for_later",
        "escalate": "escalate_for_review",
    }.get(family, "draft_reply")
    return AssistantIntent(
        intent_family=family,
        domain_intent=domain,
        confidence=confidence,
        situation=situation,
        action=action,
        label=label,
        prompt=prompt,
        commercial=commercial.to_dict(),
    )


def _replace(intent: AssistantIntent, **updates) -> AssistantIntent:
    data = asdict(intent)
    data.update(updates)
    return AssistantIntent(**data)


def _compact_text(subject: str, summary: str, body: str) -> str:
    return " ".join(part.strip() for part in [subject, summary, body[:1500]] if part and part.strip())


def _has_any(text: str, terms: list[str]) -> bool:
    return any(term in text for term in terms)


def _amount_phrase(commercial: CommercialExtraction) -> str:
    if commercial.amount and commercial.currency:
        return f" around {commercial.currency} {commercial.amount}"
    if commercial.amount:
        return f" around {commercial.amount}"
    return ""


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_") or "item"
