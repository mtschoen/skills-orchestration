"""Canonical pricing helpers shared across cost-estimator scripts.

Single source of truth for the per-MTok rates and cache multipliers.
Both analyze-month.py (retrospective bulk analysis) and plot-session.py
(single-session trajectory) import from here so the formula does not
drift.

The PRICES table below is the source of truth for these scripts;
SKILL.md's pricing table mirrors it. Keep the two in sync when rates
change.
"""

from __future__ import annotations

from datetime import datetime


# Per-MTok rates. Verified 2026-09-17 against
# https://platform.claude.com/docs/en/about-claude/pricing
#
# Still no time-windowed pricing -- one rate per model for all time, so
# historical reports drift when a price changes. These are relative
# quantities, not an invoice.
#
# But the "one flat rate per FAMILY" rule had to go: Claude Sonnet 5
# bills at $2/$10 while Sonnet 4.6 bills at $3/$15, so a family-flat
# sonnet row overcharged every Sonnet 5 token by 50%. (The $3/$15 row
# encoded a Sept 1 2026 increase that was announced and then cancelled.)
# MODEL_PRICES holds per-version rows; PRICES stays as the fallback for
# an unrecognized version within a known family.
MODEL_PRICES = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-mythos-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
PRICES = {
    "fable": (10.0, 50.0),
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}

# Cache-write multipliers, relative to base input rate. Per the docs, a
# 5-minute write bills at 1.25x and a 1-HOUR write at 2.0x.
#
# RESOLVED 2026-09-17. The long-standing 1.25x-for-both override was
# measured in 2026-04 and has since gone stale. Solving for the implied
# multiplier against `costUSD` in ~/.claude.json's lastModelUsage, over
# 40 project/model records, splits perfectly by generation:
#   exactly 1.250 -> claude-opus-4-7, claude-sonnet-4-6   (16/16 records)
#   exactly 2.000 -> claude-opus-5, claude-opus-4-8,
#                    claude-sonnet-5, claude-fable-5,
#                    claude-fable-5-1                     (20/20 records)
# i.e. the 2026-04 finding was true for the models of its day, and every
# current model bills 1h writes at the documented 2.0x.
#
# Rather than pin a per-model constant, price from the actual TTL split:
# transcripts carry usage.cache_creation.ephemeral_{5m,1h}_input_tokens
# per turn. CACHE_WRITE_MULTIPLIER remains the fallback for turns (and
# older transcripts) that report only a lump cache_creation_input_tokens.
CACHE_WRITE_MULTIPLIER_5M = 1.25
CACHE_WRITE_MULTIPLIER_1H = 2.0
CACHE_WRITE_MULTIPLIER = 2.0

CACHE_READ_MULTIPLIER = 0.10
# Fable 5.1 / Mythos 5.1 read cache at 0.025x base input, not 0.1x --
# a 4x difference on the dominant token class in long cached sessions.
CACHE_READ_MULTIPLIER_OVERRIDES = {
    "claude-fable-5-1": 0.025,
    "claude-mythos-5-1": 0.025,
}


def rates_for(family):
    """Per-MTok (input, output) rates for a model family (fallback)."""
    return PRICES[family]


def _normalize(model_identifier):
    """Strip the Claude Code context-tier suffix, e.g. '...-5[1m]'."""
    return (model_identifier or "").lower().split("[")[0].strip()


def rates_for_model(model_identifier):
    """Per-MTok (input, output) rates for a specific model id.

    Falls back to the family rate when the exact version isn't listed,
    so a newly released version still prices instead of silently
    returning $0.00.
    """
    name = _normalize(model_identifier)
    if name in MODEL_PRICES:
        return MODEL_PRICES[name]
    family = model_family(model_identifier)
    return PRICES[family] if family else None


def cache_read_multiplier_for(model_identifier):
    """Cache-read multiplier for a specific model id."""
    return CACHE_READ_MULTIPLIER_OVERRIDES.get(
        _normalize(model_identifier), CACHE_READ_MULTIPLIER
    )


def model_family(model_identifier):
    if not model_identifier:
        return None
    name = model_identifier.lower()
    if "fable" in name or "mythos" in name:
        return "fable"
    if "opus" in name:
        return "opus"
    # Any "sonnet-N" raw model id (sonnet-5, sonnet-4-6, ...) classifies
    # as the single "sonnet" family -- versions don't get their own
    # PRICES row, they all bill at the same family rate.
    if "sonnet" in name:
        return "sonnet"
    if "haiku" in name:
        return "haiku"
    return None


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
    cache_write_5m=None,
    cache_write_1h=None,
):
    rates = rates_for_model(model_identifier)
    if rates is None:
        return 0.0
    input_rate, output_rate = rates
    cache_read_rate = input_rate * cache_read_multiplier_for(model_identifier)
    # Price cache writes from the real TTL split when the turn reports it;
    # fall back to the lump total at the 1h rate otherwise.
    if cache_write_5m is not None or cache_write_1h is not None:
        split_5m = cache_write_5m or 0
        split_1h = cache_write_1h or 0
        if split_5m + split_1h > 0:
            cache_write_cost = input_rate * (
                split_5m * CACHE_WRITE_MULTIPLIER_5M
                + split_1h * CACHE_WRITE_MULTIPLIER_1H
            )
        else:
            cache_write_cost = cache_write_tokens * input_rate * CACHE_WRITE_MULTIPLIER
    else:
        cache_write_cost = cache_write_tokens * input_rate * CACHE_WRITE_MULTIPLIER
    return (
        input_tokens * input_rate
        + output_tokens * output_rate
        + cache_read_tokens * cache_read_rate
        + cache_write_cost
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
            cache_creation = usage.get("cache_creation") or {}
            if not isinstance(cache_creation, dict):
                cache_creation = {}
            cache_write_5m = int(cache_creation.get("ephemeral_5m_input_tokens") or 0)
            cache_write_1h = int(cache_creation.get("ephemeral_1h_input_tokens") or 0)
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
                "cache_write_5m": cache_write_5m,
                "cache_write_1h": cache_write_1h,
                "cost_usd": cost_for_turn(
                    model_identifier,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                    cache_write_5m,
                    cache_write_1h,
                ),
                "top_tools": tools_seen,
            }
