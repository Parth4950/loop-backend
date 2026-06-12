"""Gemini agent: a single tool-calling conversation, streamed step by step.

Rate-limit discipline (free tier ~15 RPM): exactly ONE model conversation per
/agent/run — a manual function-calling loop, a handful of model turns. 429s are
retried with exponential backoff + jitter. /agent/simulate makes ZERO model calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter
from google import genai
from google.genai import errors, types
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from . import tools
from .llm import http_options as _http_options

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_MODEL_LITE = os.environ.get("GEMINI_MODEL_LITE", "gemini-2.5-flash-lite")

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "planner_system.txt").read_text(
    encoding="utf-8"
)

MAX_TURNS = 8

# Friendly timeline label per tool call.
STEP_LABELS = {
    "segment_customers": "Analyzing customers",
    "get_campaign_history": "Reviewing past campaigns",
    "compare_channels": "Comparing channels",
    "create_campaign": "Building the plan",
}

# ---------------------------------------------------------------------------
# Tool declarations (model picks the arguments)
# ---------------------------------------------------------------------------
_OBJ = types.Type.OBJECT
_STR = types.Type.STRING
_INT = types.Type.INTEGER
_NUM = types.Type.NUMBER

_FILTERS_SCHEMA = types.Schema(
    type=_OBJ,
    description="Only include the keys you actually want to filter on.",
    properties={
        "min_total_spend": types.Schema(type=_NUM, description="Minimum lifetime spend in ₹."),
        "max_total_spend": types.Schema(type=_NUM, description="Maximum lifetime spend in ₹."),
        "days_inactive": types.Schema(type=_INT, description="No order within the last N days."),
        "city": types.Schema(type=_STR, description="Restrict to a single city."),
        "min_orders": types.Schema(type=_INT, description="Minimum number of past orders."),
    },
)

_FUNCTION_DECLARATIONS = [
    types.FunctionDeclaration(
        name="segment_customers",
        description="Count and sample the customers matching a set of segment filters.",
        parameters=types.Schema(type=_OBJ, properties={"filters": _FILTERS_SCHEMA}, required=["filters"]),
    ),
    types.FunctionDeclaration(
        name="get_campaign_history",
        description="Return the last 5 campaigns with audience size and engagement rates.",
        parameters=types.Schema(type=_OBJ, properties={}),
    ),
    types.FunctionDeclaration(
        name="compare_channels",
        description=(
            "Project reach/opens/clicks for ALL channels (whatsapp, rcs, sms, email) at once, "
            "from the same rates for every channel. Call this to choose the channel by data."
        ),
        parameters=types.Schema(
            type=_OBJ,
            properties={"segment_size": types.Schema(type=_INT)},
            required=["segment_size"],
        ),
    ),
    types.FunctionDeclaration(
        name="create_campaign",
        description="Finalize: persist the campaign (pending_approval) and queue one message per matched customer.",
        parameters=types.Schema(
            type=_OBJ,
            properties={
                "segment_filters": _FILTERS_SCHEMA,
                "message": types.Schema(type=_STR, description="The short, on-brand campaign message."),
                "channel": types.Schema(type=_STR, description="whatsapp | sms | email | rcs"),
                "confidence": types.Schema(type=_INT, description="0-100 confidence in the plan."),
                "reason": types.Schema(type=_STR, description="One paragraph: why this audience/channel and exclusions."),
            },
            required=["segment_filters", "message", "channel", "confidence", "reason"],
        ),
    ),
]

_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    tools=[types.Tool(function_declarations=_FUNCTION_DECLARATIONS)],
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    temperature=0.7,
)


# ---------------------------------------------------------------------------
# Model call with backoff
# ---------------------------------------------------------------------------
async def _generate_with_backoff(client: genai.Client, contents: list, max_attempts: int = 6):
    for attempt in range(max_attempts):
        try:
            return await client.aio.models.generate_content(
                model=GEMINI_MODEL, contents=contents, config=_CONFIG
            )
        except errors.APIError as exc:
            retryable = getattr(exc, "code", None) in (429, 503)
            if not retryable or attempt == max_attempts - 1:
                raise
            wait = min(2.0 * (2 ** attempt) + random.uniform(0, 1.5), 60.0)
            await asyncio.sleep(wait)


async def _dispatch(name: str, args: dict[str, Any]) -> Any:
    if name == "segment_customers":
        return await tools.segment_customers(args.get("filters") or {})
    if name == "get_campaign_history":
        return await tools.get_campaign_history()
    if name == "compare_channels":
        return tools.compare_channels(int(args["segment_size"]))
    if name == "create_campaign":
        return await tools.create_campaign(
            segment_filters=args.get("segment_filters") or {},
            message=str(args["message"]),
            channel=str(args["channel"]),
            confidence=int(args.get("confidence", 0)),
            reason=str(args.get("reason", "")),
        )
    raise ValueError(f"unknown tool: {name}")


def _explainability(plan: dict[str, Any]) -> dict[str, str]:
    f = plan.get("segment_filters") or {}
    included = f"{plan['audience_size']} customers matching: {plan['compiled_sql']}."
    bits: list[str] = []
    if f.get("min_total_spend") is not None:
        bits.append(f"customers with total spend ≤ ₹{f['min_total_spend']:g} (not high-value)")
    if f.get("days_inactive") is not None:
        bits.append(f"customers who ordered within the last {int(f['days_inactive'])} days (still active)")
    if f.get("max_total_spend") is not None:
        bits.append(f"customers spending ≥ ₹{f['max_total_spend']:g}")
    if f.get("min_orders") is not None:
        bits.append(f"customers with fewer than {int(f['min_orders'])} orders")
    if f.get("city"):
        bits.append(f"customers outside {f['city']}")
    excluded = "; ".join(bits) if bits else "no one — the entire base qualifies."
    return {"included": included, "excluded": excluded}


def _memory_note(history: list[dict[str, Any]] | None) -> str:
    if not history:
        return (
            "No prior campaigns to learn from yet; defaulting to WhatsApp, which "
            "typically leads engagement for Brew & Co."
        )
    best = max(history, key=lambda c: c.get("click_rate", 0))
    return (
        f"Past data: '{best['name']}' on {best['channel']} had the best click-rate "
        f"({best['click_rate']:.0%}) across the last {len(history)} campaigns; "
        "favoring channels with proven engagement."
    )


def _line(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


async def run_agent(prompt: str) -> AsyncIterator[str]:
    """Drive the tool-calling loop, streaming a timeline then the final plan."""
    if not GEMINI_API_KEY:
        yield _line({"type": "error", "message": "GEMINI_API_KEY is not set"})
        return

    client = genai.Client(api_key=GEMINI_API_KEY, http_options=_http_options())
    contents: list = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]

    history_result: list[dict[str, Any]] | None = None
    plan_result: dict[str, Any] | None = None

    try:
        for _ in range(MAX_TURNS):
            response = await _generate_with_backoff(client, contents)
            calls = response.function_calls or []
            if not calls:
                break

            contents.append(response.candidates[0].content)
            response_parts = []
            for call in calls:
                name = call.name
                args = dict(call.args or {})

                if name == "create_campaign":
                    yield _line({"type": "timeline", "step": "Writing the message"})
                yield _line({"type": "timeline", "step": STEP_LABELS.get(name, name)})

                result = await _dispatch(name, args)
                if name == "get_campaign_history":
                    history_result = result
                if name == "create_campaign":
                    plan_result = result

                response_parts.append(
                    types.Part.from_function_response(name=name, response={"result": result})
                )

            contents.append(types.Content(role="user", parts=response_parts))
            if plan_result is not None:
                break
    except errors.APIError as exc:
        yield _line({"type": "error", "message": f"Gemini API error: {exc}"})
        return

    if plan_result is None:
        yield _line({"type": "error", "message": "agent did not produce a campaign plan"})
        return

    # Chosen channel, plan projections, and the what-if comparison all derive from
    # ONE compare_channels call on the authoritative audience size, so they can
    # never disagree with each other.
    comparison = tools.compare_channels(plan_result["audience_size"])["channels"]
    chosen = plan_result["channel"]

    plan = {
        **plan_result,
        "projected": comparison.get(chosen, plan_result.get("projected")),
        "channel_comparison": comparison,
        "explainability": _explainability(plan_result),
        "memory_note": _memory_note(history_result),
    }
    yield _line({"type": "plan", "plan": plan})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
router = APIRouter()


class RunRequest(BaseModel):
    prompt: str


class SimulateRequest(BaseModel):
    segment_size: int
    channel: str


@router.post("/agent/run")
async def agent_run(req: RunRequest):
    return StreamingResponse(run_agent(req.prompt), media_type="application/x-ndjson")


@router.post("/agent/simulate")
async def agent_simulate(req: SimulateRequest):
    # ZERO model calls — pure deterministic projection.
    return tools.estimate_metrics(req.segment_size, req.channel)
