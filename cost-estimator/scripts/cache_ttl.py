"""Diagnostic: what TTL do this account's cache writes use, and does cache
survive across inter-turn gaps?

Two readouts over parent session transcripts:
  1. TTL of cache WRITES -- the split of ephemeral_5m vs ephemeral_1h tokens
     Claude Code requested (from each turn's usage.cache_creation breakdown).
  2. Behavioral test -- for each consecutive assistant-turn pair, bucket the
     wall-clock gap and look at the later turn's cache behavior. A high "miss%"
     (later turn re-wrote far more than it read back) in the 5-60m buckets
     would indicate a 5m TTL letting the prefix expire; low miss% across long
     gaps indicates 1h-TTL warmth.

On the account this probe was built against, 98.7% of cache-write tokens were
1h-TTL; re-run the probe to measure the current account rather than assuming
that split. Paths are parameterized via roots.py
(CLI roots or AGENTS_COST_ROOTS) -- no hard-coded machine paths.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pricing import parse_timestamp  # noqa: E402  -- after sys.path manipulation
from roots import _resolve_roots  # noqa: E402  -- after sys.path manipulation

# Inter-turn gap buckets, half-open [lo, hi) in seconds.
BUCKETS = [
    ("0-1m", 0, 60),
    ("1-5m", 60, 300),
    ("5-10m", 300, 600),
    ("10-20m", 600, 1200),
    ("20-60m", 1200, 3600),
    (">60m", 3600, 10**9),
]


def bucket_for(seconds):
    """Return the bucket label for an inter-turn gap, or None if out of range."""
    for name, low, high in BUCKETS:
        if low <= seconds < high:
            return name
    return None


def turns_of(path):
    """Deduped assistant turns from one parent transcript, sorted by time."""
    seen = set()
    out = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            # Transcripts are not uniformly objects: Claude Code writes bare
            # strings and other scalars as JSONL lines. Guard before .get().
            if not isinstance(entry, dict):
                continue
            if entry.get("type") != "assistant":
                continue
            message = entry.get("message") or {}
            if not isinstance(message, dict):
                continue
            message_id = message.get("id")
            if not message_id or message_id in seen:
                continue
            seen.add(message_id)
            usage = message.get("usage") or {}
            cache_creation = usage.get("cache_creation") or {}
            timestamp = entry.get("timestamp")
            if not timestamp:
                continue
            out.append(
                {
                    "ts": parse_timestamp(timestamp),
                    "read": usage.get("cache_read_input_tokens", 0) or 0,
                    "create": usage.get("cache_creation_input_tokens", 0) or 0,
                    "ephemeral_5m": cache_creation.get("ephemeral_5m_input_tokens", 0)
                    or 0,
                    "ephemeral_1h": cache_creation.get("ephemeral_1h_input_tokens", 0)
                    or 0,
                }
            )
    out.sort(key=lambda turn: turn["ts"])
    return out


def parent_transcripts(resolved_roots):
    """All parent (non-subagent) session transcripts under the resolved roots."""
    paths = []
    for _, root in resolved_roots:
        paths += glob.glob(str(Path(root) / "*" / "*.jsonl"))
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "roots", nargs="*", help="One or more .claude/projects directories"
    )
    parser.add_argument(
        "--label", action="append", help="Label paired with each root (repeatable)"
    )
    arguments = parser.parse_args()

    resolved_roots = _resolve_roots(
        cli_roots=arguments.roots,
        cli_labels=arguments.label,
        env_value=os.environ.get("AGENTS_COST_ROOTS"),
    )

    aggregate = {
        name: {"n": 0, "read": 0, "create": 0, "miss": 0} for name, _, _ in BUCKETS
    }
    total_5m = total_1h = total_create = 0

    for path in parent_transcripts(resolved_roots):
        try:
            turns = turns_of(path)
        except OSError as error:
            print(f"skip {os.path.basename(path)} ({error})", file=sys.stderr)
            continue
        for turn in turns:
            total_5m += turn["ephemeral_5m"]
            total_1h += turn["ephemeral_1h"]
            total_create += turn["create"]
        for earlier, later in zip(turns, turns[1:]):
            gap = (later["ts"] - earlier["ts"]).total_seconds()
            if gap < 0:
                continue
            name = bucket_for(gap)
            if not name:
                continue
            bucket = aggregate[name]
            bucket["n"] += 1
            bucket["read"] += later["read"]
            bucket["create"] += later["create"]
            # "miss": later turn re-wrote far more than it read back.
            if later["create"] > 5000 and later["read"] < 0.25 * later["create"]:
                bucket["miss"] += 1

    print("=" * 78)
    print("TTL of cache WRITES (what Claude Code requests on every write):")
    print("-" * 78)
    print(f"  ephemeral_5m total: {total_5m:>15,} tokens")
    print(f"  ephemeral_1h total: {total_1h:>15,} tokens")
    print(f"  (sum)               {total_create:>15,} cache_creation_input_tokens")
    if total_5m + total_1h > 0:
        percent_5m = 100 * total_5m / (total_5m + total_1h)
        print(
            f"  => {percent_5m:.1f}% of cache-write tokens are 5m-TTL, "
            f"{100 - percent_5m:.1f}% are 1h-TTL"
        )

    print("\n" + "=" * 78)
    print("BEHAVIORAL TEST: cache behavior of the LATER turn, by inter-turn gap")
    print("-" * 78)
    print(
        f"{'gap bucket':<10} {'pairs':>7} {'avg_read':>12} "
        f"{'avg_create':>12} {'miss%':>7}"
    )
    print("-" * 78)
    for name, _, _ in BUCKETS:
        bucket = aggregate[name]
        if bucket["n"] == 0:
            print(f"{name:<10} {0:>7}")
            continue
        average_read = bucket["read"] / bucket["n"]
        average_create = bucket["create"] / bucket["n"]
        miss_percent = 100 * bucket["miss"] / bucket["n"]
        print(
            f"{name:<10} {bucket['n']:>7} {average_read:>12,.0f} "
            f"{average_create:>12,.0f} {miss_percent:>6.1f}%"
        )
    print("-" * 78)
    print("miss% = share of later-turns that re-wrote >>4x what they read back")
    print("(prefix expired). High miss% in 5-60m buckets => 5m TTL in effect.")


if __name__ == "__main__":
    main()
