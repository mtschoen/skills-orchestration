"""Unit tests for scripts/cache_ttl.py's gap-bucketing and transcript parsing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import cache_ttl  # noqa: E402


def test_bucket_for_boundaries():
    # buckets are half-open [lo, hi): 0-1m, 1-5m, 5-10m, 10-20m, 20-60m, >60m
    assert cache_ttl.bucket_for(0) == "0-1m"
    assert cache_ttl.bucket_for(59) == "0-1m"
    assert cache_ttl.bucket_for(60) == "1-5m"
    assert cache_ttl.bucket_for(299) == "1-5m"
    assert cache_ttl.bucket_for(300) == "5-10m"
    assert cache_ttl.bucket_for(3599) == "20-60m"
    assert cache_ttl.bucket_for(3600) == ">60m"
    assert cache_ttl.bucket_for(10**8) == ">60m"


def test_turns_of_skips_non_dict_lines_without_raising(workspace_directory: Path):
    # Real transcripts are not uniformly JSON objects -- Claude Code writes
    # bare strings and other scalars as their own JSONL lines. Before the
    # isinstance guard, entry.get("type") on a bare string raised
    # AttributeError and crashed the whole probe.
    transcript = workspace_directory / "session.jsonl"
    lines = [
        json.dumps("just a bare string"),
        json.dumps(["not", "a", "mapping"]),
        json.dumps(123),
        json.dumps({"type": "assistant", "message": "also not a mapping"}),
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-03-01T10:00:00Z",
                "message": {
                    "id": "turn-1",
                    "usage": {
                        "cache_read_input_tokens": 10,
                        "cache_creation_input_tokens": 20,
                    },
                },
            }
        ),
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

    turns = cache_ttl.turns_of(transcript)

    assert len(turns) == 1
    assert turns[0]["read"] == 10
    assert turns[0]["create"] == 20


if __name__ == "__main__":
    test_bucket_for_boundaries()
    print("OK")
