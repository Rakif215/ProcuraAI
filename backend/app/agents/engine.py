"""
app/agents/engine.py
--------------------
The LangGraph agent engine — the heart of Orion.

Architecture (ReAct loop):
  User message
       ↓
  [call_model] → LLM decides: respond OR call a tool
       ↓ (if tool call)
  [tool_node]  → executes the tool, returns result
       ↓
  [call_model] → LLM reads tool result, decides next step
       ↓ (repeat until LLM responds directly)
  Stream tokens back to the WebSocket

The LLM provider chain is: Groq → OpenRouter → Gemini (automatic fallback).
If Groq rate-limits or errors, it transparently retries with the next provider.
"""
import logging
from functools import lru_cache
from typing import Literal

from langchain_core.messages import SystemMessage
from langchain_core.language_models import BaseChatModel
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

from app.agents.state import AgentState
from app.agents.tools import get_all_tools
from app.core.config import settings

logger = logging.getLogger(__name__)


def _build_llm() -> BaseChatModel:
    """
    Build the LLM with automatic fallback chain:
    Gemini 2.0 Flash → Gemini 1.5 Flash → Gemini 1.5 Pro → Groq.
    Uses LangChain's .with_fallbacks() with max_retries=0 for instant failover when quota is exceeded.
    """
    llms: list[BaseChatModel] = []

    if settings.gemini_api_key:
        logger.info("Adding Gemini 2.0 Flash to LLM chain")
        llms.append(ChatOpenAI(
            api_key=settings.gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            model="gemini-2.0-flash",
            temperature=0.2,
            streaming=True,
            max_retries=0,
        ))

    if settings.nvidia_api_key:
        logger.info("Adding Nvidia DeepSeek V4 Flash to LLM chain as Backup 1")
        llms.append(ChatOpenAI(
            api_key=settings.nvidia_api_key,
            base_url=settings.nvidia_base_url,
            model=settings.nvidia_model,
            temperature=0.2,
            streaming=True,
            max_retries=0,
        ))
        
    if settings.gemini_api_key:
        logger.info("Adding Gemini 1.5 Flash to LLM chain as backup")
        llms.append(ChatOpenAI(
            api_key=settings.gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            model="gemini-1.5-flash",
            temperature=0.2,
            streaming=True,
            max_retries=0,
        ))

        logger.info("Adding Gemini 1.5 Pro to LLM chain as backup")
        llms.append(ChatOpenAI(
            api_key=settings.gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            model="gemini-1.5-pro",
            temperature=0.2,
            streaming=True,
            max_retries=0,
        ))

    if settings.groq_api_key:
        logger.info("Adding Groq to LLM chain (%s)", settings.groq_model)
        llms.append(ChatGroq(
            api_key=settings.groq_api_key,
            model=settings.groq_model,
            temperature=0.2,
            streaming=True,
            max_retries=0,
        ))

    if settings.openrouter_api_key:
        logger.info("Adding OpenRouter to LLM chain (%s)", settings.openrouter_model)
        llms.append(ChatOpenAI(
            api_key=settings.openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
            model=settings.openrouter_model,
            temperature=0.2,
            streaming=True,
            max_retries=0,
        ))

    if not llms:
        raise RuntimeError(
            "No LLM provider configured. "
            "Set at least one of: GROQ_API_KEY, OPENROUTER_API_KEY, GEMINI_API_KEY"
        )

    # Primary = first available, fallbacks = rest
    primary = llms[0]
    if len(llms) > 1:
        primary = primary.with_fallbacks(llms[1:])
        logger.info("LLM fallback chain: %d providers configured", len(llms))

    return primary


DEFAULT_SYSTEM_PROMPT = """You are Orion, an intelligent AI assistant for business operations.

You have access to tools that can read emails, search databases, draft replies, and more.
When a user asks you something, use the appropriate tool to get real data before responding.
Always be concise, professional, and actionable.

When presenting emails or data, format them clearly.
If you cannot complete a task, explain why and suggest an alternative.

Current capabilities:
- Read and search emails (all recent, urgent, unread, by keyword)
- View full email details
- Draft reply suggestions for emails
- Search the web for current information
- Get the current date and time

Email tool selection:
- If the user asks to summarize "all emails", "emails", "recent emails", or their inbox generally, use get_recent_emails first.
- Use get_urgent_emails only when they explicitly ask for urgent, important, priority, or what needs attention most.
- Use get_unread_emails only when they explicitly ask for unread, unresponded, or pending emails."""


def build_graph(system_prompt: str = DEFAULT_SYSTEM_PROMPT):
    """
    Build and compile the LangGraph agent graph.

    Args:
        system_prompt: The tenant-specific system prompt for this agent.

    Returns:
        A compiled LangGraph graph ready to stream.
    """
    tools = get_all_tools()
    llm = _build_llm()
    llm_with_tools = llm.bind_tools(tools)

    def call_model(state: AgentState) -> dict:
        """LLM node: prepend the system prompt and invoke the LLM."""
        messages = [SystemMessage(content=system_prompt)] + list(state["messages"])
        response = llm_with_tools.invoke(messages)
        return {"messages": [response]}

    def should_continue(state: AgentState) -> Literal["tools", "__end__"]:
        """Router: if the last message has tool calls, execute them. Otherwise end."""
        last_message = state["messages"][-1]
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "tools"
        return END

    # Build the graph
    graph = StateGraph(AgentState)
    graph.add_node("call_model", call_model)
    graph.add_node("tools", ToolNode(tools))

    graph.set_entry_point("call_model")
    graph.add_conditional_edges("call_model", should_continue)
    graph.add_edge("tools", "call_model")

    return graph.compile()


# Cache compiled graphs per system_prompt to avoid rebuilding on every request
@lru_cache(maxsize=128)
def get_compiled_graph(system_prompt: str = DEFAULT_SYSTEM_PROMPT):
    """Get or build a cached compiled graph for the given system prompt."""
    logger.info("Compiling new LangGraph agent (prompt hash: %s...)", hash(system_prompt))
    return build_graph(system_prompt)
