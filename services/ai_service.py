"""
AI service — client-facing AI for the Lash Master Telegram Bot.

Activated via AI_ENABLED=true in .env.
Provider is selected via AI_PROVIDER=openai|gigachat in .env.

Settings per provider:
  OpenAI:   OPENAI_API_KEY, model gpt-4o-mini
  GigaChat: GIGACHAT_CREDENTIALS (base64 from Sber developer portal), model GigaChat

Temperature: 0.3, max_tokens: 300 (both providers).

Functions:
  - detect_intent(text)   -> Optional[str]             — FSM entry point from free text
  - parse_datetime(text)  -> Optional[tuple[str, str]] — date + time_range from free text
  - answer_faq(question)  -> Optional[str]             — answer from static knowledge base
  - parse_schedule(text)  -> list[dict]                — batch slot parsing (master panel)

Usage in handlers (typing indicator + AI call):

    from aiogram.enums import ChatAction
    from services import ai_service

    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    intent = await ai_service.detect_intent(message.text)
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Optional

import config
from services.prompts import (
    SYSTEM_DETECT_INTENT,
    SYSTEM_PARSE_DATETIME,
    SYSTEM_ANSWER_FAQ,
    SYSTEM_PARSE_SCHEDULE,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider backends — lazy-initialised
# ---------------------------------------------------------------------------

_openai_client = None


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import AsyncOpenAI
        _openai_client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    return _openai_client


async def _chat_openai(system: str, user_text: str) -> Optional[str]:
    try:
        response = await _get_openai_client().chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.3,
            max_tokens=300,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
        )
        return response.choices[0].message.content.strip()
    except Exception as exc:
        logger.error("OpenAI error: %s", exc)
        return None


async def _chat_gigachat(system: str, user_text: str) -> Optional[str]:
    try:
        from gigachat import GigaChat
        from gigachat.models import Chat, Messages, MessagesRole

        async with GigaChat(
            credentials=config.GIGACHAT_CREDENTIALS,
            verify_ssl_certs=False,
        ) as client:
            response = await client.achat(
                Chat(
                    model="GigaChat",
                    temperature=0.3,
                    max_tokens=300,
                    messages=[
                        Messages(role=MessagesRole.SYSTEM, content=system),
                        Messages(role=MessagesRole.USER, content=user_text),
                    ],
                )
            )
        return response.choices[0].message.content.strip()
    except Exception as exc:
        logger.error("GigaChat error: %s", exc)
        return None


async def _chat(system: str, user_text: str) -> Optional[str]:
    """Dispatch to the configured AI provider."""
    if config.AI_PROVIDER == "gigachat":
        return await _chat_gigachat(system, user_text)
    return await _chat_openai(system, user_text)


# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    return config.AI_ENABLED


# ---------------------------------------------------------------------------
# Valid intent values — must match entry-point callback_data in handlers
# ---------------------------------------------------------------------------

_VALID_INTENTS = {"start_booking", "my_bookings", "faq"}


async def detect_intent(text: str) -> Optional[str]:
    """
    Detect client intent from free text.
    Returns one of: 'start_booking', 'my_bookings', 'faq' — or None.
    """
    if not is_enabled():
        return None

    raw = await _chat(SYSTEM_DETECT_INTENT, text)
    if not raw:
        return None

    intent = raw.strip().lower().strip('"\'')
    if intent == "null" or intent not in _VALID_INTENTS:
        if intent not in _VALID_INTENTS and intent != "null":
            logger.warning("detect_intent: unexpected value %r", raw)
        return None
    return intent


async def parse_datetime(text: str) -> Optional[tuple[str, str]]:
    """
    Parse date and time range from free text.
    Returns (YYYY-MM-DD, time_range) e.g. ("2026-04-04", "17:00-21:00") or None.
    Either element can be None — caller checks individually.
    """
    if not is_enabled():
        return None

    import pytz
    today = datetime.now(pytz.timezone(config.TIMEZONE)).date().isoformat()
    system = SYSTEM_PARSE_DATETIME.format(today=today)
    raw = await _chat(system, text)
    if not raw:
        return None

    raw = raw.strip()
    if raw.lower() == "null":
        return None

    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        data = json.loads(raw)
        dt = data.get("date")
        tr = data.get("time_range")
        if not dt and not tr:
            return None
        return (dt, tr)
    except Exception:
        logger.warning("parse_datetime: bad JSON %r — trying regex fallback", raw)

    # Regex fallback: extract date and time from free-form AI response
    import re
    dt = None
    tr = None
    date_m = re.search(r"\d{4}-\d{2}-\d{2}", raw)
    time_m = re.search(r"\b(\d{2}:\d{2})(?:-(\d{2}:\d{2}))?", raw)
    if date_m:
        dt = date_m.group(0)
    if time_m:
        start = time_m.group(1)
        end = time_m.group(2) or start
        tr = f"{start}-{end}"
    if dt or tr:
        logger.info("parse_datetime: regex extracted date=%r time=%r", dt, tr)
        return (dt, tr)

    return None


async def answer_faq(question: str) -> Optional[str]:
    """
    Answer a client question from the static knowledge base.
    Returns None if AI is disabled.
    """
    if not is_enabled():
        return None

    from services.knowledge import FAQ_CONTEXT
    system = SYSTEM_ANSWER_FAQ.format(context=FAQ_CONTEXT)
    return await _chat(system, question)


async def parse_schedule(text: str) -> list[dict]:
    """
    Parse a batch of working slots from master's free text.
    Returns list of {slot_date: str, slot_time: str} dicts.
    """
    if not is_enabled():
        return []

    import pytz
    today = datetime.now(pytz.timezone(config.TIMEZONE)).date().isoformat()
    system = SYSTEM_PARSE_SCHEDULE.format(today=today)
    raw = await _chat(system, text)
    if not raw:
        return []

    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        slots = json.loads(raw)
        if not isinstance(slots, list):
            return []
        return [
            s for s in slots
            if isinstance(s, dict)
            and isinstance(s.get("slot_date"), str)
            and isinstance(s.get("slot_time"), str)
        ]
    except Exception:
        logger.warning("parse_schedule: bad JSON %r", raw)
        return []
