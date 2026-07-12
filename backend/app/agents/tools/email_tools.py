"""
app/agents/tools/email_tools.py
--------------------------------
Email tools for the Orion agent.
These query the Supabase emails table. The email poller (Celery) populates
this table from IMAP every 5 minutes.
"""
from langchain_core.tools import tool
from app.db.client import supabase


@tool
def get_recent_emails(limit: int = 20) -> str:
    """
    Get the most recent emails from the inbox, regardless of priority or
    responded status. Use this when the user asks to summarize all emails,
    see recent emails, review their inbox, or asks generally about emails
    without saying urgent/unread only.

    Args:
        limit: Maximum number of emails to return (default 20, max 50).

    Returns:
        A formatted list of recent emails with sender, subject, priority,
        category, summary, and response status.
    """
    try:
        limit = min(limit, 50)
        result = (
            supabase.table("emails")
            .select("id, sender, subject, summary, priority, category, received_at, needs_response, responded")
            .order("received_at", desc=True)
            .limit(limit)
            .execute()
        )

        emails = result.data
        if not emails:
            return "No emails found in your inbox."

        lines = [f"Found {len(emails)} recent email(s):\n"]
        for i, email in enumerate(emails, 1):
            priority = (email.get("priority") or "normal").upper()
            status = "responded" if email.get("responded") else "needs review"
            lines.append(f"{i}. [{priority}] **{email.get('subject') or '(no subject)'}**")
            lines.append(f"   From: {email.get('sender') or 'Unknown'}")
            if email.get("category"):
                lines.append(f"   Category: {email['category']}")
            if email.get("summary"):
                lines.append(f"   Summary: {email['summary']}")
            lines.append(f"   Status: {status}")
            lines.append(f"   ID: {email['id']}\n")

        return "\n".join(lines)

    except Exception as exc:
        return f"Failed to retrieve recent emails: {str(exc)}"


@tool
def get_unread_emails(limit: int = 10) -> str:
    """
    Get a list of unread/unresponded emails from the inbox.
    Use this when the user asks to see their emails, check their inbox,
    or wants to know what messages they have.

    Args:
        limit: Maximum number of emails to return (default 10, max 50).

    Returns:
        A formatted list of emails with sender, subject, priority, and date.
    """
    try:
        limit = min(limit, 50)
        result = (
            supabase.table("emails")
            .select("id, sender, subject, priority, category, received_at, needs_response")
            .eq("responded", False)
            .order("received_at", desc=True)
            .limit(limit)
            .execute()
        )

        emails = result.data
        if not emails:
            return "Your inbox is empty — no unread emails found."

        lines = [f"Found {len(emails)} unread email(s):\n"]
        for i, email in enumerate(emails, 1):
            priority = email.get("priority", "normal").upper()
            lines.append(
                f"{i}. [{priority}] **{email['subject']}**\n"
                f"   From: {email['sender']}\n"
                f"   ID: {email['id']}\n"
            )

        return "\n".join(lines)

    except Exception as exc:
        return f"Failed to retrieve emails: {str(exc)}"


@tool
def get_urgent_emails() -> str:
    """
    Get only urgent/high-priority emails that require immediate attention.
    Use this when the user asks about urgent emails, important messages,
    or what needs their attention most.

    Returns:
        A formatted list of urgent emails.
    """
    try:
        result = (
            supabase.table("emails")
            .select("id, sender, subject, summary, received_at, action_items")
            .eq("priority", "urgent")
            .eq("responded", False)
            .order("received_at", desc=True)
            .limit(20)
            .execute()
        )

        emails = result.data
        if not emails:
            return "No urgent emails found. You're all caught up! ✅"

        lines = [f"🚨 {len(emails)} urgent email(s) need your attention:\n"]
        for i, email in enumerate(emails, 1):
            lines.append(f"{i}. **{email['subject']}**")
            lines.append(f"   From: {email['sender']}")
            if email.get("summary"):
                lines.append(f"   Summary: {email['summary']}")
            lines.append(f"   ID: {email['id']}\n")

        return "\n".join(lines)

    except Exception as exc:
        return f"Failed to retrieve urgent emails: {str(exc)}"


@tool
def search_emails(query: str) -> str:
    """
    Search through emails by keyword — matches against sender, subject, or body.
    Use this when the user asks to find a specific email, conversation,
    or reference to a company/person/topic.

    Args:
        query: Search keyword or phrase.

    Returns:
        A list of matching emails.
    """
    try:
        result = (
            supabase.table("emails")
            .select("id, sender, subject, priority, received_at, summary")
            .or_(f"subject.ilike.%{query}%,sender.ilike.%{query}%,body_text.ilike.%{query}%")
            .order("received_at", desc=True)
            .limit(10)
            .execute()
        )

        emails = result.data
        if not emails:
            return f"No emails found matching '{query}'."

        lines = [f"Found {len(emails)} email(s) matching '{query}':\n"]
        for i, email in enumerate(emails, 1):
            priority = email.get("priority", "normal").upper()
            lines.append(f"{i}. [{priority}] **{email['subject']}**")
            lines.append(f"   From: {email['sender']}")
            if email.get("summary"):
                lines.append(f"   {email['summary']}")
            lines.append(f"   ID: {email['id']}\n")

        return "\n".join(lines)

    except Exception as exc:
        return f"Search failed: {str(exc)}"


@tool
def get_email_detail(email_id: str) -> str:
    """
    Get the full details of a specific email by its ID.
    Use this when the user wants to read an email, see its full content,
    or get the suggested reply for a specific email.

    Args:
        email_id: The UUID of the email from get_unread_emails or search_emails.

    Returns:
        Full email details including body, summary, and suggested reply.
    """
    try:
        result = (
            supabase.table("emails")
            .select("*")
            .eq("id", email_id)
            .single()
            .execute()
        )

        email = result.data
        if not email:
            return f"Email with ID {email_id} not found."

        lines = [
            f"**Subject:** {email['subject']}",
            f"**From:** {email['sender']}",
            f"**Priority:** {email.get('priority', 'normal').upper()}",
            f"**Category:** {email.get('category', 'general')}",
            f"**Received:** {email.get('received_at', 'Unknown')}",
            "",
        ]

        if email.get("summary"):
            lines += [f"**Summary:**\n{email['summary']}", ""]

        if email.get("body_text"):
            body_preview = email["body_text"][:800]
            if len(email["body_text"]) > 800:
                body_preview += "\n... [truncated]"
            lines += [f"**Full Message:**\n{body_preview}", ""]

        if email.get("action_items"):
            lines += ["**Action Items:**"]
            items = email["action_items"] if isinstance(email["action_items"], list) else []
            for item in items:
                lines.append(f"  • {item}")
            lines.append("")

        if email.get("suggested_reply"):
            lines += [f"**Suggested Reply:**\n{email['suggested_reply']}", ""]

        return "\n".join(lines)

    except Exception as exc:
        return f"Failed to retrieve email: {str(exc)}"


@tool
def draft_reply(email_id: str, instructions: str = "") -> str:
    """
    Fetch an email's details to draft a reply. Returns the original email
    context so the AI can compose an appropriate response.
    Use this when the user asks to draft a reply, respond to an email,
    or write back to someone.

    Args:
        email_id: The UUID of the email to reply to.
        instructions: Optional special instructions for the reply
                      (e.g., "be apologetic", "confirm the delivery").

    Returns:
        The original email context formatted for reply drafting.
    """
    try:
        result = (
            supabase.table("emails")
            .select("sender, subject, body_text, summary, suggested_reply")
            .eq("id", email_id)
            .single()
            .execute()
        )

        email = result.data
        if not email:
            return f"Email with ID {email_id} not found."

        lines = [
            "**Original Email (for reply drafting):**",
            f"**To:** {email['sender']}",
            f"**Subject:** Re: {email['subject']}",
            "",
        ]

        if email.get("body_text"):
            body = email["body_text"][:600]
            lines.append(f"**Original Message:**\n{body}")
            lines.append("")

        if email.get("suggested_reply"):
            lines.append(f"**Pre-generated Suggestion:**\n{email['suggested_reply']}")
            lines.append("")

        if instructions:
            lines.append(f"**User Instructions:** {instructions}")

        lines.append(
            "\nPlease draft a professional reply based on the above context. "
            "Format it as a complete email body ready to send."
        )

        return "\n".join(lines)

    except Exception as exc:
        return f"Failed to prepare reply draft: {str(exc)}"


@tool
def send_reply(email_id: str, body: str) -> str:
    """
    Send a reply to an email via SMTP and mark it as responded.
    Use this when the user says "send the reply", "send it", or confirms they
    want to send an email response.

    IMPORTANT: Only call this after the user has reviewed and confirmed the reply body.
    Never send an email without the user's explicit approval.

    Args:
        email_id: The UUID of the email to reply to.
        body: The complete reply body text to send.

    Returns:
        Confirmation that the email was sent, or an error message.
    """
    try:
        # Get the email and its account
        email_result = (
            supabase.table("emails")
            .select("id, sender, subject, account_id, tenant_id")
            .eq("id", email_id)
            .single()
            .execute()
        )
        original = email_result.data
        if not original:
            return f"Email {email_id} not found."

        account_id = original.get("account_id")
        if not account_id:
            return ("Cannot send — no email account linked to this email. "
                    "Please configure an email account in Settings first.")

        account_result = (
            supabase.table("email_accounts")
            .select("*")
            .eq("id", account_id)
            .single()
            .execute()
        )
        account = account_result.data
        if not account or not account.get("password_encrypted"):
            return "Email account credentials not found. Please reconnect your email in Settings."

        from app.services.email_service import send_email
        success = send_email(
            account=account,
            to_address=original["sender"],
            subject=original["subject"],
            body=body,
        )

        if success:
            # Mark as responded
            from datetime import datetime, timezone
            supabase.table("emails").update({
                "responded": True,
                "responded_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", email_id).execute()

            return f"✅ Reply sent to **{original['sender']}** successfully. The email has been marked as responded."
        else:
            return "❌ Failed to send the reply. Please check your email account settings in Settings → Integrations."

    except Exception as exc:
        return f"Error sending reply: {str(exc)}"
