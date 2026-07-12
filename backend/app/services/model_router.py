import json
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from app.core.config import settings
from app.services.telemetry import log_event
from app.services.usage_limits import can_use_ai, estimate_tokens

logger = logging.getLogger(__name__)


@dataclass
class ModelResult:
    content: str
    provider: str
    model: str


def _configured_providers() -> list[dict]:
    providers: list[dict] = []
    if settings.gemini_api_key:
        providers.append({
            "provider": "gemini",
            "api_key": settings.gemini_api_key,
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            "model": settings.gemini_model,
        })
    if settings.groq_api_key:
        providers.append({
            "provider": "groq",
            "api_key": settings.groq_api_key,
            "base_url": "https://api.groq.com/openai/v1/chat/completions",
            "model": settings.groq_model,
        })
    if settings.openrouter_api_key:
        providers.append({
            "provider": "openrouter",
            "api_key": settings.openrouter_api_key,
            "base_url": "https://openrouter.ai/api/v1/chat/completions",
            "model": settings.openrouter_model,
        })
    return providers


def chat_completion(
    *,
    messages: list[dict],
    purpose: str,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    temperature: float = 0.2,
    max_tokens: int = 1024,
    json_mode: bool = False,
) -> Optional[ModelResult]:
    prompt_text = json.dumps(messages, ensure_ascii=False)
    token_estimate = estimate_tokens(prompt_text) + max_tokens
    if not can_use_ai(
        tenant_id=tenant_id,
        user_id=user_id,
        purpose=purpose,
        estimated_tokens=token_estimate,
    ):
        return None

    providers = _configured_providers()
    if not providers:
        log_event(
            "model_call_skipped_unconfigured",
            tenant_id=tenant_id,
            user_id=user_id,
            properties={"purpose": purpose},
        )
        return None

    last_error = ""
    for index, provider in enumerate(providers):
        payload = {
            "model": provider["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            response = httpx.post(
                provider["base_url"],
                headers={
                    "Authorization": f"Bearer {provider['api_key']}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30.0,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"].strip()
            log_event(
                "model_call_completed",
                tenant_id=tenant_id,
                user_id=user_id,
                properties={
                    "purpose": purpose,
                    "provider": provider["provider"],
                    "model": provider["model"],
                    "fallback_index": index,
                    "estimated_tokens": token_estimate,
                },
            )
            return ModelResult(
                content=content,
                provider=provider["provider"],
                model=provider["model"],
            )
        except Exception as exc:
            last_error = str(exc)[:240]
            logger.warning(
                "Model provider %s failed for %s: %s",
                provider["provider"],
                purpose,
                last_error,
            )
            log_event(
                "model_call_failed",
                tenant_id=tenant_id,
                user_id=user_id,
                properties={
                    "purpose": purpose,
                    "provider": provider["provider"],
                    "model": provider["model"],
                    "fallback_index": index,
                    "error": last_error,
                },
            )

    log_event(
        "model_call_exhausted",
        tenant_id=tenant_id,
        user_id=user_id,
        properties={"purpose": purpose, "last_error": last_error},
    )
    return None
