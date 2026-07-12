"""
app/api/v1/agents.py
--------------------
Agent CRUD endpoints.
"""
import logging
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from typing import Optional, List

from app.db.client import supabase
from app.core.deps import AuthUser

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agents", tags=["agents"])


class AgentCreate(BaseModel):
    name: str
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    tools_enabled: List[str] = []


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    tools_enabled: Optional[List[str]] = None
    is_active: Optional[bool] = None


@router.get("")
async def list_agents(current_user: AuthUser):
    """List all agents for the current tenant."""
    result = (
        supabase.table("agents")
        .select("*")
        .eq("tenant_id", current_user.tenant_id)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data


@router.get("/{agent_id}")
async def get_agent(agent_id: str, current_user: AuthUser):
    """Get a specific agent."""
    result = (
        supabase.table("agents")
        .select("*")
        .eq("id", agent_id)
        .eq("tenant_id", current_user.tenant_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Agent not found")
    return result.data


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_agent(body: AgentCreate, current_user: AuthUser):
    """Create a new agent."""
    result = (
        supabase.table("agents")
        .insert({
            "tenant_id": current_user.tenant_id,
            "name": body.name,
            "description": body.description,
            "system_prompt": body.system_prompt,
            "tools_enabled": body.tools_enabled,
        })
        .execute()
    )
    return result.data[0]


@router.patch("/{agent_id}")
async def update_agent(agent_id: str, body: AgentUpdate, current_user: AuthUser):
    """Update an agent."""
    update_data = body.model_dump(exclude_none=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    result = (
        supabase.table("agents")
        .update(update_data)
        .eq("id", agent_id)
        .eq("tenant_id", current_user.tenant_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Agent not found")
    return result.data[0]


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(agent_id: str, current_user: AuthUser):
    """Delete an agent."""
    supabase.table("agents").delete().eq("id", agent_id).eq(
        "tenant_id", current_user.tenant_id
    ).execute()
