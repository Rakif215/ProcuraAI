"""
app/agents/state.py
-------------------
LangGraph agent state schema.
Every node in the graph reads from and writes to this state.

Uses TypedDict (LangGraph standard) with Annotated + operator.add
so that each node appends messages rather than replacing them.
"""
from typing import Annotated, TypedDict
import operator
from langchain_core.messages import BaseMessage


class AgentState(TypedDict):
    """
    The full state that flows through the LangGraph agent.

    - messages:         The conversation history (LangChain message objects).
                        Using operator.add means each node appends rather than replaces.
    - tenant_id:        The company workspace this agent belongs to.
    - user_id:          The user who sent the message.
    - agent_id:         Which configured agent is running.
    - conversation_id:  The thread ID — used by LangGraph's checkpointer
                        to persist state between turns.
    """
    messages: Annotated[list[BaseMessage], operator.add]
    tenant_id: str
    user_id: str
    agent_id: str
    conversation_id: str
