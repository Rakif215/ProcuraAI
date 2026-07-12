"""
app/agents/tools/search_tools.py
---------------------------------
General-purpose search tools available to all agents.
"""
from datetime import datetime, timezone
from langchain_core.tools import tool
import httpx


@tool
def web_search(query: str) -> str:
    """
    Search the web for current information.
    Use this when the user asks about something you don't know or that requires
    up-to-date information (news, prices, company info, etc.).

    Args:
        query: The search query string.

    Returns:
        A string summary of the top search results.
    """
    try:
        # DuckDuckGo Instant Answer API — no API key required
        response = httpx.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
            timeout=10.0,
        )
        data = response.json()

        results = []

        if data.get("AbstractText"):
            results.append(f"**Summary:** {data['AbstractText']}")

        if data.get("RelatedTopics"):
            topics = data["RelatedTopics"][:5]
            for topic in topics:
                if isinstance(topic, dict) and topic.get("Text"):
                    results.append(f"- {topic['Text']}")

        if results:
            return "\n".join(results)

        return f"No direct results found for: {query}. Try rephrasing or asking more specifically."

    except Exception as exc:
        return f"Web search failed: {str(exc)}"


@tool
def get_current_time() -> str:
    """
    Get the current date and time in UTC.
    Use this when the user asks what time it is, today's date, or anything
    related to the current date/time.

    Returns:
        The current date and time formatted as a human-readable string.
    """
    now = datetime.now(timezone.utc)
    return (
        f"Current UTC time: {now.strftime('%A, %B %d, %Y at %I:%M %p UTC')}\n"
        f"ISO format: {now.isoformat()}"
    )
