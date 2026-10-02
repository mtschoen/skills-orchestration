"""Canonical pricing helpers shared across cost-estimator scripts.

Single source of truth for the per-MTok rates and cache multipliers.
Both analyze-month.py (retrospective bulk analysis) and plot-session.py
(single-session trajectory) import from here so the formula does not
drift.

The PRICES and VERSION_PRICES tables below are the source of truth for
these scripts; SKILL.md's pricing table mirrors them. Keep the two in
sync when rates change.
"""

from __future__ import annotations

import re
from datetime import datetime


# Per-MTok rates, verified 2026-10-02 against the Anthropic pricing page
# (https://platform.claude.com/docs/en/about-claude/pricing). The family
# rows below are the default for every version of a family; the rows in
# VERSION_PRICES override them for versions whose published price
# differs. Accept that historical data will drift as prices change--these
# are relative quantities we're tracking, not an invoice of real money
# spend. Long-context requests (up to 1M tokens) bill at the same
# per-token rates, so there is no long-context tier.
PRICES = {
    "fable": (10.0, 50.0),
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}
CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute cache write
CACHE_WRITE_1H_MULTIPLIER = 2.0  # 1-hour cache write
CACHE_READ_MULTIPLIER = 0.10

# Version-specific rows: (pattern, input, output, cache read multiplier).
# First match wins. Each row mirrors a published price that differs from
# the family default above.
VERSION_PRICES = (
    # Fable 5.1 and Mythos 5.1: cache reads at 0.025x base input.
    (re.compile(r"(fable|mythos)-5-1"), 10.0, 50.0, 0.025),
    # Opus 5.5: $4 / $20, cache reads at 0.05x base input.
    (re.compile(r"opus-5-5"), 4.0, 20.0, 0.05),
    # Opus 4 and Opus 4.1 (retired): $15 / $75.
    (
        re.compile(r"opus-4(?:-1)?(?:[-@]\d{8})?(?:-v1:0)?(?:\[|$)"),
        15.0,
        75.0,
        CACHE_READ_MULTIPLIER,
    ),
    # Sonnet 5 and Sonnet 5.5: $2 / $10 (the planned $3 / $15 step on
    # 2026-09-01 never happened). "sonnet-5" does not match "sonnet-4-5".
    (re.compile(r"sonnet-5(?:-|\[|$)"), 2.0, 10.0, CACHE_READ_MULTIPLIER),
    # Haiku 3.5 (retired): $0.80 / $4.
    (re.compile(r"haiku-3-5|3-5-haiku"), 0.8, 4.0, CACHE_READ_MULTIPLIER),
)


def rates_for(family):
    """Per-MTok (input, output) rates for a model family."""
    return PRICES[family]


def model_family(model_identifier):
    if not model_identifier:
        return None
    name = model_identifier.lower()
    if "fable" in name or "mythos" in name:
        return "fable"
    if "opus" in name:
        return "opus"
    if "sonnet" in name:
        return "sonnet"
    if "haiku" in name:
        return "haiku"
    return None


def price_for_model(model_identifier):
    """(input, output, cache read multiplier) for a raw model id, or None.

    A matching VERSION_PRICES row wins; otherwise the family default
    applies. None means the id is not a recognized family.
    """
    family = model_family(model_identifier)
    if family is None:
        return None
    name = model_identifier.lower()
    for pattern, input_rate, output_rate, read_multiplier in VERSION_PRICES:
        if pattern.search(name):
            return input_rate, output_rate, read_multiplier
    input_rate, output_rate = rates_for(family)
    return input_rate, output_rate, CACHE_READ_MULTIPLIER


def parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def cost_for_turn(
    model_identifier,
    input_tokens,
    output_tokens,
    cache_read_tokens,
    cache_write_tokens,
    cache_write_1h_tokens=0,
):
    """Dollar cost of one turn.

    `cache_write_tokens` is the total cache write volume;
    `cache_write_1h_tokens` is the portion of it written with the
    1-hour TTL (usage.cache_creation.ephemeral_1h_input_tokens), billed
    at 2x base input. The rest bills at the 5-minute 1.25x rate.
    """
    price = price_for_model(model_identifier)
    if price is None:
        return 0.0
    input_rate, output_rate, read_multiplier = price
    one_hour = min(cache_write_1h_tokens, cache_write_tokens)
    five_minute = cache_write_tokens - one_hour
    return (
        input_tokens * input_rate
        + output_tokens * output_rate
        + cache_read_tokens * input_rate * read_multiplier
        + five_minute * input_rate * CACHE_WRITE_MULTIPLIER
        + one_hour * input_rate * CACHE_WRITE_1H_MULTIPLIER
    ) / 1_000_000


def unpriced_usage(
    model_identifier, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
):
    """Flag a turn that cost_for_turn() silently priced at $0.00.

    Returns (turns=1, tokens) when `model_identifier` is a real (non-empty)
    id that model_family() doesn't recognize -- the exact condition that
    makes cost_for_turn() return 0.0 above. Returns None for a known
    family, a missing/empty model id (a different, unrelated gap), or a
    turn with zero total token volume. The zero-volume exclusion covers
    "<synthetic>" transcript entries generically: Claude Code emits them
    with an unrecognized model id but all-zero usage, so without this
    check every real report would falsely raise the UNPRICED MODELS
    warning even though nothing was actually undercounted.
    `tokens` is this turn's full volume (input + output + cache read +
    cache write), so callers can judge how much a silent gap matters.

    Callers accumulate this per model id into a running {turns, tokens}
    tally and surface it loudly -- a new model family priced at $0.00
    would otherwise undercount every retrospective report without any
    warning.
    """
    if not model_identifier or model_family(model_identifier) is not None:
        return None
    tokens = input_tokens + output_tokens + cache_read_tokens + cache_write_tokens
    if tokens == 0:
        return None
    return 1, tokens


try:
    from orjson import loads as _loads
except ImportError:
    from json import loads as _loads


def iter_assistant_turns(jsonl_path):
    """Yield one record per priced assistant turn in a Claude Code JSONL.

    Dedupes on `message.id` (turns recur in JSONL snapshots; naive
    iteration double-counts). Turns without a `message.id` are kept.
    Lines that fail JSON parsing are silently skipped.

    Yielded record shape:
        {
          "index": 1-based turn number within this JSONL,
          "timestamp": ISO 8601 string (or "" if missing),
          "model": str (e.g. "claude-opus-4-7" or "...-7[1m]"),
          "input_tokens": int,
          "output_tokens": int,
          "cache_read_tokens": int,
          "cache_write_tokens": int,
          "cost_usd": float,
          "top_tools": list[str] of tool names from this turn's tool_use
              blocks, deduped, in first-appearance order,
        }
    """
    seen_ids = set()
    index = 0
    with open(jsonl_path, "rb") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = _loads(line)
            except Exception:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("type") != "assistant":
                continue
            message = entry.get("message") or {}
            if message.get("role") != "assistant":
                continue
            message_id = message.get("id")
            if message_id:
                if message_id in seen_ids:
                    continue
                seen_ids.add(message_id)

            usage = message.get("usage") or {}
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
            cache_write_tokens = int(usage.get("cache_creation_input_tokens") or 0)
            cache_write_1h_tokens = int(
                (usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens")
                or 0
            )
            model_identifier = message.get("model") or ""

            tools_seen = []
            content = message.get("content") or []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name") or "?"
                        if name not in tools_seen:
                            tools_seen.append(name)

            index += 1
            yield {
                "index": index,
                "timestamp": entry.get("timestamp") or "",
                "model": model_identifier,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_write_tokens": cache_write_tokens,
                "cost_usd": cost_for_turn(
                    model_identifier,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                    cache_write_1h_tokens,
                ),
                "top_tools": tools_seen,
            }
