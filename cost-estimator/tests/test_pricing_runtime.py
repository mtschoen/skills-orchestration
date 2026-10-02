"""Coverage for transcript pricing and Chart.js runtime helpers."""

from __future__ import annotations

import json
from pathlib import Path

import chart_runtime
import pricing
import pytest


def test_pricing_helpers_cover_model_families_and_flat_rates():
    assert pricing.model_family(None) is None
    assert pricing.model_family("CLAUDE-MYTHOS") == "fable"
    assert pricing.model_family("claude-opus-4-7[1m]") == "opus"
    assert pricing.model_family("claude-sonnet-5") == "sonnet"
    assert pricing.model_family("claude-sonnet-4-6") == "sonnet"
    assert pricing.model_family("claude-haiku-4-5") == "haiku"
    assert pricing.model_family("unknown") is None

    assert pricing.rates_for("sonnet") == (3.0, 15.0)
    assert pricing.parse_timestamp(None) is None
    assert pricing.parse_timestamp("not-a-date") is None
    assert pricing.parse_timestamp("2026-03-01T10:00:00Z").year == 2026

    assert pricing.cost_for_turn("unknown", 1, 2, 3, 4) == 0.0
    assert pricing.cost_for_turn("claude-opus-4-7", 1_000_000, 0, 0, 0) == 5.0
    # Sonnet 5: $2 in + $10 out + 0.1x read ($0.20) + 1.25x write ($2.50).
    assert (
        pricing.cost_for_turn(
            "claude-sonnet-5",
            1_000_000,
            1_000_000,
            1_000_000,
            1_000_000,
        )
        == 14.7
    )


@pytest.mark.parametrize(
    ("model_identifier", "expected"),
    [
        ("claude-fable-5", (10.0, 50.0, 0.1)),
        ("claude-fable-5-1", (10.0, 50.0, 0.025)),
        ("claude-fable-5-1[1m]", (10.0, 50.0, 0.025)),
        ("claude-mythos-5-1", (10.0, 50.0, 0.025)),
        ("claude-opus-5-5", (4.0, 20.0, 0.05)),
        ("claude-opus-5", (5.0, 25.0, 0.1)),
        ("claude-opus-4-8", (5.0, 25.0, 0.1)),
        ("claude-opus-4-7", (5.0, 25.0, 0.1)),
        ("claude-opus-4-6", (5.0, 25.0, 0.1)),
        ("claude-opus-4-5-20251101", (5.0, 25.0, 0.1)),
        ("claude-opus-4-5@20251101", (5.0, 25.0, 0.1)),
        ("us.anthropic.claude-opus-4-1-20250805-v1:0", (15.0, 75.0, 0.1)),
        ("claude-opus-4-1@20250805", (15.0, 75.0, 0.1)),
        ("claude-opus-4-1-20250805", (15.0, 75.0, 0.1)),
        ("claude-opus-4@20250514", (15.0, 75.0, 0.1)),
        ("claude-opus-4-20250514", (15.0, 75.0, 0.1)),
        ("claude-sonnet-5", (2.0, 10.0, 0.1)),
        ("claude-sonnet-5-5", (2.0, 10.0, 0.1)),
        ("claude-sonnet-4-6", (3.0, 15.0, 0.1)),
        ("claude-sonnet-4-5", (3.0, 15.0, 0.1)),
        ("claude-haiku-4-5-20251001", (1.0, 5.0, 0.1)),
        ("claude-3-5-haiku-20241022", (0.8, 4.0, 0.1)),
        ("opus", (5.0, 25.0, 0.1)),
    ],
)
def test_price_for_model_resolves_real_model_ids(model_identifier, expected):
    assert pricing.price_for_model(model_identifier) == expected


def test_price_for_model_unknown_is_none():
    assert pricing.price_for_model("claude-nova-7") is None


def test_cost_for_turn_applies_cache_ttl_and_read_multipliers():
    million = 1_000_000
    # Opus 5.5: 0.05x cache read of $4 = $0.20.
    assert pricing.cost_for_turn("claude-opus-5-5", 0, 0, million, 0) == pytest.approx(
        0.2
    )
    # Fable 5.1: 0.025x cache read of $10 = $0.25.
    assert pricing.cost_for_turn("claude-fable-5-1", 0, 0, million, 0) == pytest.approx(
        0.25
    )
    # Opus 5: 5m write 1.25x ($6.25), 1h write 2x ($10).
    assert pricing.cost_for_turn("claude-opus-5", 0, 0, 0, million) == pytest.approx(
        6.25
    )
    assert pricing.cost_for_turn(
        "claude-opus-5", 0, 0, 0, million, cache_write_1h_tokens=million
    ) == pytest.approx(10.0)
    assert pricing.cost_for_turn(
        "claude-opus-5", 0, 0, 0, million, cache_write_1h_tokens=million // 2
    ) == pytest.approx(8.125)


def test_iter_assistant_turns_filters_duplicates_and_collects_tools(
    workspace_directory: Path,
):
    transcript = workspace_directory / "session.jsonl"
    entries = [
        "",
        "not json",
        json.dumps(["not", "a", "mapping"]),
        json.dumps({"type": "user", "message": {"role": "user"}}),
        json.dumps({"type": "assistant", "message": {"role": "user"}}),
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-03-01T10:00:00Z",
                "message": {
                    "id": "turn-1",
                    "role": "assistant",
                    "model": "claude-opus-4-7",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 30,
                        "cache_creation_input_tokens": 40,
                    },
                    "content": [
                        {"type": "text", "text": "hello"},
                        {"type": "tool_use", "name": "Read"},
                        {"type": "tool_use", "name": "Read"},
                        {"type": "tool_use"},
                        "not a block",
                    ],
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {"id": "turn-1", "role": "assistant"},
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "claude-haiku-4-5",
                    "content": "not a list",
                },
            }
        ),
    ]
    transcript.write_text("\n".join(entries) + "\n", encoding="utf-8")

    turns = list(pricing.iter_assistant_turns(transcript))

    assert [turn["index"] for turn in turns] == [1, 2]
    assert turns[0]["top_tools"] == ["Read", "?"]
    assert turns[0]["cache_write_tokens"] == 40
    assert turns[0]["cost_usd"] > 0
    assert turns[1]["timestamp"] == ""


def test_chartjs_script_tags_support_cdn_and_inline(monkeypatch):
    chart_tag, adapter_tag = chart_runtime.chartjs_script_tags(
        inline=False,
        want_time_adapter=True,
    )
    assert chart_runtime.CHARTJS_CDN_URL in chart_tag
    assert chart_runtime.TIME_ADAPTER_CDN_URL in adapter_tag

    chart_tag, adapter_tag = chart_runtime.chartjs_script_tags(
        inline=False,
        want_time_adapter=False,
    )
    assert chart_runtime.CHARTJS_CDN_URL in chart_tag
    assert adapter_tag == ""

    monkeypatch.setattr(chart_runtime, "cached_download", lambda url, filename: b"js")
    chart_tag, adapter_tag = chart_runtime.chartjs_script_tags(
        inline=True,
        want_time_adapter=True,
    )
    assert chart_tag == "<script>js</script>"
    assert adapter_tag == "<script>js</script>"


def test_cached_download_writes_and_reuses_cache(
    monkeypatch, workspace_directory: Path
):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exception_information):
            return False

        def read(self):
            return b"downloaded"

    calls = []

    def urlopen(url):
        calls.append(url)
        return Response()

    monkeypatch.setattr(
        Path, "home", classmethod(lambda path_class: workspace_directory)
    )
    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    assert (
        chart_runtime.cached_download("https://example.test/chart.js", "chart.js")
        == b"downloaded"
    )
    assert (
        chart_runtime.cached_download("https://example.test/chart.js", "chart.js")
        == b"downloaded"
    )
    assert calls == ["https://example.test/chart.js"]
