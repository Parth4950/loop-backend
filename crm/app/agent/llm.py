"""Shared Gemini helpers for one-shot (non tool-loop) calls.

Used by the mid-campaign monitor (Flash-Lite) and the post-campaign analysis
(Flash). Keeps a single place for the OS-trust-store TLS fix and 429/503 backoff.
"""

from __future__ import annotations

import asyncio
import os
import random
import ssl
from typing import Optional

from google import genai
from google.genai import errors, types

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_MODEL_LITE = os.environ.get("GEMINI_MODEL_LITE", "gemini-2.5-flash-lite")


def http_options() -> Optional[types.HttpOptions]:
    """Use the OS trust store for TLS (works behind an intercepting CA that
    isn't in certifi's bundle). No-op if truststore is unavailable."""
    try:
        import truststore

        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        return types.HttpOptions(async_client_args={"verify": ctx})
    except Exception:
        return None


def build_client() -> genai.Client:
    return genai.Client(api_key=GEMINI_API_KEY, http_options=http_options())


async def generate(
    model: str,
    prompt: str,
    *,
    system_instruction: Optional[str] = None,
    temperature: float = 0.4,
    response_schema: Optional[types.Schema] = None,
    max_attempts: int = 6,
) -> str:
    """One Gemini call with exponential backoff + jitter on 429/503."""
    client = build_client()
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=temperature,
        response_mime_type="application/json" if response_schema is not None else None,
        response_schema=response_schema,
    )
    for attempt in range(max_attempts):
        try:
            response = await client.aio.models.generate_content(
                model=model, contents=prompt, config=config
            )
            return response.text or ""
        except errors.APIError as exc:
            retryable = getattr(exc, "code", None) in (429, 503)
            if not retryable or attempt == max_attempts - 1:
                raise
            wait = min(2.0 * (2**attempt) + random.uniform(0, 1.5), 60.0)
            await asyncio.sleep(wait)
    return ""  # unreachable; loop either returns or raises
