"""
app/agents/tools/__init__.py
----------------------------
Central registry of all available agent tools.
Each tool is a Python function decorated with @tool from LangChain.
The engine calls get_all_tools() to bind them to the LLM.
"""
from app.agents.tools.search_tools import web_search, get_current_time
from app.agents.tools.email_tools import (
    get_recent_emails,
    get_unread_emails,
    get_urgent_emails,
    search_emails,
    get_email_detail,
    draft_reply,
    send_reply,
)
from app.agents.tools.po_tools import (
    list_documents_needing_review,
    get_document_details,
    update_document_fields,
    approve_and_verify_document,
)


def get_all_tools() -> list:
    """
    Returns all tools available to the agent.
    Add new tools here as they are implemented in future phases.
    """
    return [
        # Search & utility
        web_search,
        get_current_time,
        # Email
        get_recent_emails,
        get_unread_emails,
        get_urgent_emails,
        search_emails,
        get_email_detail,
        draft_reply,
        send_reply,
        # PO / RFQ Database
        list_documents_needing_review,
        get_document_details,
        update_document_fields,
        approve_and_verify_document,
    ]
