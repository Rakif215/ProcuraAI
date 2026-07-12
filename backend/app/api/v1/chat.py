"""
app/api/v1/chat.py
------------------
WebSocket endpoint for real-time streaming chat with the Orion agent.

Connection flow:
  1. Client opens:  ws://localhost:8000/api/v1/chat/ws/{conversation_id}?token=JWT
  2. Server validates JWT, loads agent config, compiles LangGraph graph
  3. Client sends JSON: {"content": "Show me my urgent emails"}
  4. Server streams tokens back: {"type": "token", "content": "Here"}...
  5. Tool calls are broadcast as:  {"type": "tool_start", "tool_name": "get_urgent_emails"}
  6. Final message ends with:     {"type": "done"}
"""
import json
import logging
import uuid
from datetime import datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from langchain_core.messages import HumanMessage, AIMessageChunk

from app.agents.engine import get_compiled_graph, DEFAULT_SYSTEM_PROMPT
from app.core.security import decode_token, extract_user_id
from app.db.client import supabase
from app.services.email_service import fetch_and_triage
from app.services.memory_service import get_user_context, extract_memories

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])

EMAIL_TOOL_ROUTING_PROMPT = """\

Email tool selection:
- If the user asks to summarize "all emails", "emails", "recent emails", or their inbox generally, use get_recent_emails first.
- Use get_urgent_emails only when they explicitly ask for urgent, important, priority, or what needs attention most.
- Use get_unread_emails only when they explicitly ask for unread, unresponded, pending, or needs-reply emails."""


async def _get_agent_config(tenant_id: str) -> tuple[str, str]:
    """
    Look up the default agent for this tenant and return
    (agent_id, system_prompt).
    Falls back to the default system prompt if no agent is configured.
    """
    try:
        result = (
            supabase.table("agents")
            .select("id, system_prompt")
            .eq("tenant_id", tenant_id)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        if result.data:
            agent = result.data[0]
            return agent["id"], agent.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
    except Exception as exc:
        logger.warning("Could not fetch agent config: %s", exc)

    return str(uuid.uuid4()), DEFAULT_SYSTEM_PROMPT


async def _ensure_conversation(conversation_id: str, tenant_id: str, user_id: str, agent_id: str):
    """Ensure a conversation record exists (upsert)."""
    try:
        # Check if it exists first
        existing = (
            supabase.table("conversations")
            .select("id")
            .eq("id", conversation_id)
            .execute()
        )
        if not existing.data:
            supabase.table("conversations").insert({
                "id": conversation_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "agent_id": agent_id,
                "title": "New Conversation",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
            logger.info("Created conversation: %s", conversation_id)
    except Exception as exc:
        logger.warning("Could not ensure conversation: %s", exc)


async def _save_message(conversation_id: str, role: str, content: str, tool_calls: Optional[list] = None):
    """Persist a message to the messages table."""
    try:
        supabase.table("messages").insert({
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "tool_calls": tool_calls,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as exc:
        logger.warning("Failed to save message: %s", exc)


def _format_email_summary(emails: list[dict], label: str) -> str:
    if not emails:
        return f"I checked your inbox.\n\nNo {label} emails need attention right now."

    urgent = sum(1 for email in emails if email.get("priority") == "urgent")
    needs_reply = sum(1 for email in emails if email.get("needs_response") and not email.get("responded"))
    low_priority = sum(1 for email in emails if email.get("priority") == "low")
    important = [
        email for email in emails
        if email.get("priority") == "urgent" or (email.get("needs_response") and not email.get("responded"))
    ]
    if not important:
        important = [
            email for email in emails
            if (email.get("category") or "") in {"po", "rfq", "quotation", "invoice", "finance", "logistics"}
        ]
    now = datetime.now().strftime("%I:%M %p").lstrip("0")
    lines = [
        f"Today, {now}",
        f"I checked your {label} inbox.",
    ]

    if urgent or needs_reply:
        lines.append(f"{urgent} urgent. {needs_reply} need replies.")
    else:
        lines.append("Nothing needs action right now.")

    top_items = important[:3]
    if top_items:
        lines.append("Top items:")
    for idx, email in enumerate(top_items, 1):
        priority = (email.get("priority") or "normal").title()
        subject = email.get("subject") or "(no subject)"
        sender = (email.get("sender") or "Unknown").split("<")[0].strip()
        reply_marker = "needs reply" if email.get("needs_response") and not email.get("responded") else "review"
        lines.append(f"{idx}. {sender}: {subject} ({priority}, {reply_marker})")

    if low_priority:
        lines.append(f"I batched {low_priority} low-priority emails into the digest.")

    lines.append("Next: open Inbox to review one item at a time.")

    return "\n\n".join(lines).strip()


def _qatar_today_cutoff() -> str:
    today_qatar = datetime.now(ZoneInfo("Asia/Qatar")).date()
    start_qatar = datetime.combine(today_qatar, time.min, tzinfo=ZoneInfo("Asia/Qatar"))
    return start_qatar.astimezone(timezone.utc).isoformat()


def _sync_active_accounts_once(tenant_id: str) -> None:
    """Best-effort controlled sync before answering a fresh inbox question."""
    try:
        accounts = (
            supabase.table("email_accounts")
            .select("*")
            .eq("tenant_id", tenant_id)
            .eq("is_active", True)
            .execute()
        ).data or []
        for account in accounts:
            fetch_and_triage(account)
    except Exception as exc:
        logger.warning("Pre-summary sync failed for tenant %s: %s", tenant_id, exc)


def _run_email_query(tenant_id: str, lowered: str):
    query = (
        supabase.table("emails")
        .select("id, sender, subject, summary, body_text, priority, category, needs_response, responded, received_at, created_at")
        .eq("tenant_id", tenant_id)
    )

    label = "recent"
    if "today" in lowered:
        query = query.gte("received_at", _qatar_today_cutoff())
        label = "today's"
    if "urgent" in lowered or "important" in lowered:
        query = query.eq("priority", "urgent")
        label = "urgent"
    if any(term in lowered for term in ["needs reply", "need reply", "pending", "unresponded", "unread"]):
        query = query.eq("responded", False).eq("needs_response", True)
        label = "emails needing reply"

    return query.order("received_at", desc=True).limit(25).execute(), label


def _try_email_fast_response(user_message: str, tenant_id: str) -> Optional[str]:
    """Answer common inbox-summary questions without spending LLM tokens."""
    lowered = user_message.lower()
    asks_email = any(word in lowered for word in ["email", "emails", "inbox", "urgent", "reply"])
    asks_summary = any(word in lowered for word in ["summarize", "summary", "what", "show", "list", "today"])
    if not asks_email or not asks_summary:
        return None

    try:
        result, label = _run_email_query(tenant_id, lowered)
        if not result.data and any(term in lowered for term in ["today", "recent", "summary", "summarize", "inbox", "email"]):
            _sync_active_accounts_once(tenant_id)
            result, label = _run_email_query(tenant_id, lowered)
        return _format_email_summary(result.data or [], label)
    except Exception as exc:
        logger.warning("Fast email summary failed: %s", exc)
        return None


@router.websocket("/ws/{conversation_id}")
async def chat_websocket(
    websocket: WebSocket,
    conversation_id: str,
    token: str = Query(..., description="Supabase JWT"),
):
    """
    WebSocket endpoint for real-time AI chat.

    Authentication is via query param ?token=JWT because WebSocket
    connections don't support custom headers in most clients.
    """
    # ── Auth ──────────────────────────────────────────────────────────────────
    payload = decode_token(token)
    if not payload:
        await websocket.close(code=4001, reason="Invalid token")
        return

    user_id = extract_user_id(payload)
    if not user_id:
        await websocket.close(code=4001, reason="Invalid token subject")
        return

    # Fetch tenant_id from profile
    try:
        profile = supabase.table("profiles").select("tenant_id").eq("id", user_id).single().execute()
        tenant_id = profile.data["tenant_id"]
    except Exception:
        await websocket.close(code=4003, reason="User profile not found")
        return

    await websocket.accept()
    logger.info("WebSocket connected: user=%s tenant=%s conv=%s", user_id, tenant_id, conversation_id)

    # ── Load agent config + memory injection ─────────────────────────────────
    agent_id, base_system_prompt = await _get_agent_config(tenant_id)
    active_icon = "🤖"

    # Retrieve user's memory context and inject into the agent's system prompt.
    # Only 'active' memories are included (max 8 — avoids prompt noise).
    user_memory = get_user_context(user_id, scope="general")
    if user_memory:
        system_prompt = f"{base_system_prompt}{EMAIL_TOOL_ROUTING_PROMPT}\n\n{user_memory}"
        logger.debug("Injected memory context for user %s (%d chars)", user_id, len(user_memory))
    else:
        system_prompt = f"{base_system_prompt}{EMAIL_TOOL_ROUTING_PROMPT}"

    graph = get_compiled_graph(system_prompt)

    # Ensure the conversation record exists (prevents FK violations on messages)
    await _ensure_conversation(conversation_id, tenant_id, user_id, agent_id)

    # Track turns for post-conversation memory extraction
    session_turns: list[dict] = []

    # ── Message loop ──────────────────────────────────────────────────────────
    try:
        while True:
            # Receive message from client
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
                # Support both "content" (frontend sends this) and "message" (legacy)
                user_message = (data.get("content") or data.get("message", "")).strip()
            except json.JSONDecodeError:
                user_message = raw.strip()

            if not user_message:
                continue

            # Check for BMad agent switch command, e.g. /pm or /dev
            if user_message.startswith("/"):
                parts = user_message.split(maxsplit=1)
                cmd = parts[0][1:].lower()
                
                bmad_prompt, bmad_icon = get_bmad_agent_prompt(cmd)
                if bmad_prompt:
                    base_system_prompt = bmad_prompt
                    active_icon = bmad_icon
                    
                    user_memory = get_user_context(user_id, scope="general")
                    if user_memory:
                        system_prompt = f"{base_system_prompt}{EMAIL_TOOL_ROUTING_PROMPT}\n\n{user_memory}"
                    else:
                        system_prompt = f"{base_system_prompt}{EMAIL_TOOL_ROUTING_PROMPT}"
                    
                    graph = get_compiled_graph(system_prompt)
                    
                    agent_titles = {
                        "pm": "John (Product Manager)", "john": "John (Product Manager)",
                        "arch": "Winston (Solutions Architect)", "winston": "Winston (Solutions Architect)",
                        "dev": "Amelia (Lead Developer)", "amelia": "Amelia (Lead Developer)",
                        "analyst": "Mary (Business Analyst)", "mary": "Mary (Business Analyst)",
                        "ux": "Sally (UX Designer)", "sally": "Sally (UX Designer)",
                        "writer": "Paige (Technical Writer)", "paige": "Paige (Technical Writer)",
                        "tea": "Murat (Test Architect)", "murat": "Murat (Test Architect)",
                    }
                    agent_title = agent_titles.get(cmd, f"{cmd.title()} Specialist")
                    
                    switch_notice = f"🔄 *Switched conversation persona to {active_icon} {agent_title}*\n\n"
                    await websocket.send_json({
                        "type": "token",
                        "content": switch_notice
                    })
                    
                    # Save switcher command as user message, notice as assistant response
                    await _save_message(conversation_id, "user", parts[0])
                    await _save_message(conversation_id, "assistant", switch_notice)
                    session_turns.append({"role": "user", "content": parts[0]})
                    session_turns.append({"role": "assistant", "content": switch_notice})
                    
                    if len(parts) > 1:
                        user_message = parts[1]
                    else:
                        await websocket.send_json({"type": "done"})
                        continue

            # Track for memory extraction
            session_turns.append({"role": "user", "content": user_message})

            # Save user message to DB
            await _save_message(conversation_id, "user", user_message)

            fast_response = _try_email_fast_response(user_message, tenant_id)
            if fast_response:
                await websocket.send_json({"type": "token", "content": fast_response})
                await websocket.send_json({"type": "done"})
                await _save_message(conversation_id, "assistant", fast_response)
                session_turns.append({"role": "assistant", "content": fast_response})
                continue

            # Config for LangGraph checkpointer (thread = conversation)
            config = {
                "configurable": {
                    "thread_id": conversation_id,
                }
            }

            state_input = {
                "messages": [HumanMessage(content=user_message)],
                "tenant_id": tenant_id,
                "user_id": user_id,
                "agent_id": agent_id,
                "conversation_id": conversation_id,
            }

            # Stream the agent response
            full_response = []
            try:
                async for event in graph.astream(state_input, config, stream_mode="messages"):
                    if isinstance(event, tuple):
                        chunk, metadata = event
                    else:
                        chunk = event

                    # Stream AI tokens
                    if isinstance(chunk, AIMessageChunk) and chunk.content:
                        full_response.append(chunk.content)
                        await websocket.send_json({
                            "type": "token",
                            "content": chunk.content,
                        })

                    # Notify client when agent calls a tool
                    if hasattr(chunk, "tool_calls") and chunk.tool_calls:
                        for tc in chunk.tool_calls:
                            tool_name = tc.get("name", "unknown") if isinstance(tc, dict) else getattr(tc, "name", "unknown")
                            await websocket.send_json({
                                "type": "tool_start",
                                "tool_name": tool_name,
                            })

            except Exception as stream_err:
                logger.error("Streaming error: %s", stream_err, exc_info=True)
                await websocket.send_json({
                    "type": "error",
                    "message": f"Agent error: {str(stream_err)[:200]}",
                })

            # Signal completion
            await websocket.send_json({"type": "done"})

            # Save assistant response to DB and track turn
            if full_response:
                assistant_text = "".join(full_response)
                await _save_message(conversation_id, "assistant", assistant_text)
                session_turns.append({"role": "assistant", "content": assistant_text})

                # Run memory extraction after every 3 turns (non-blocking best-effort)
                # Explicit preferences are captured immediately; implicit ones accumulate
                if len(session_turns) % 6 == 0:  # every 3 user+assistant pairs
                    try:
                        extracted = extract_memories(
                            user_id=user_id,
                            tenant_id=tenant_id,
                            conversation_turns=session_turns,
                            source_id=conversation_id,
                        )
                        if extracted:
                            logger.info("Extracted %d memories from conversation %s",
                                        len(extracted), conversation_id)
                    except Exception as mem_err:
                        logger.debug("Memory extraction skipped: %s", mem_err)

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: user=%s", user_id)
    except Exception as exc:
        logger.error("WebSocket error: %s", exc, exc_info=True)
        try:
            await websocket.send_json({"type": "error", "message": "An error occurred. Please try again."})
        except Exception:
            pass


# ── REST endpoint for conversation history ────────────────────────────────────

@router.get("/conversations")
async def list_conversations(token: str = Query(...)):
    """List all conversations for the current user."""
    payload = decode_token(token)
    if not payload:
        return []

    user_id = extract_user_id(payload)
    try:
        profile = supabase.table("profiles").select("tenant_id").eq("id", user_id).single().execute()
        tenant_id = profile.data["tenant_id"]

        result = (
            supabase.table("conversations")
            .select("id, title, created_at")
            .eq("tenant_id", tenant_id)
            .order("created_at", desc=True)
            .limit(50)
            .execute()
        )
        return result.data or []
    except Exception as exc:
        logger.warning("Failed to list conversations: %s", exc)
        return []


@router.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: str, token: str = Query(...)):
    """Get all messages for a conversation."""
    payload = decode_token(token)
    if not payload:
        return []

    try:
        result = (
            supabase.table("messages")
            .select("id, role, content, tool_calls, created_at")
            .eq("conversation_id", conversation_id)
            .order("created_at")
            .limit(200)
            .execute()
        )
        return result.data or []
    except Exception as exc:
        logger.warning("Failed to get messages: %s", exc)
        return []


import tomllib
from pathlib import Path

def get_bmad_agent_prompt(agent_key: str) -> tuple[Optional[str], Optional[str]]:
    mapping = {
        "pm": "bmad-agent-pm",
        "john": "bmad-agent-pm",
        "arch": "bmad-agent-architect",
        "winston": "bmad-agent-architect",
        "dev": "bmad-agent-dev",
        "amelia": "bmad-agent-dev",
        "analyst": "bmad-agent-analyst",
        "mary": "bmad-agent-analyst",
        "ux": "bmad-agent-ux-designer",
        "sally": "bmad-agent-ux-designer",
        "writer": "bmad-agent-tech-writer",
        "paige": "bmad-agent-tech-writer",
        "tea": "bmad-tea",
        "murat": "bmad-tea",
    }
    skill_dir = mapping.get(agent_key.lower())
    if not skill_dir:
        return None, None
        
    # Find project root dynamically
    current_dir = Path(__file__).resolve().parent
    project_root = None
    for p in [current_dir, *current_dir.parents]:
        if (p / ".agents").exists():
            project_root = p
            break
    if not project_root:
        project_root = current_dir.parent.parent.parent.parent
        
    toml_path = project_root / ".agents" / "skills" / skill_dir / "customize.toml"
    if not toml_path.exists():
        return None, None
        
    try:
        with open(toml_path, "rb") as f:
            config = tomllib.load(f)
        
        agent_data = config.get("agent", {})
        name = agent_data.get("name", "Orion Specialist")
        title = agent_data.get("title", "Agent")
        icon = agent_data.get("icon", "🤖")
        role = agent_data.get("role", "")
        identity = agent_data.get("identity", "")
        principles = agent_data.get("principles", [])
        
        prompt = f"You are {name}, the {title} ({icon}).\n"
        if role:
            prompt += f"Your role is to: {role}\n"
        if identity:
            prompt += f"Your identity is: {identity}\n"
        if principles:
            prompt += "Your core principles are:\n"
            for p in principles:
                prompt += f"- {p}\n"
                
        prompt += "\nYou have access to database and email tools. Stay strictly in character.\n"
        return prompt, icon
    except Exception as e:
        logger.warning(f"Failed to parse BMad agent TOML: {e}")
        return None, None
