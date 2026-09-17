"""Coverage for transcript pricing and Chart.js runtime helpers."""

from __future__ import annotations

import json
from pathlib import Path

import chart_runtime
import pricing


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
    # claude-sonnet-5 prices at its own $2/$10 MODEL_PRICES row (not the
    # $3/$15 "sonnet" family fallback used above), with the lump cache-write
    # fallback (no TTL split passed) at 2.0x: 2 + 10 + 1*(2*0.1) + 1*(2*2.0)
    # == 2 + 10 + 0.2 + 4 == 16.2.
    assert (
        pricing.cost_for_turn(
            "claude-sonnet-5",
            1_000_000,
            1_000_000,
            1_000_000,
            1_000_000,
        )
        == 16.2
    )


def test_rates_for_model_uses_per_model_row_not_stale_family_rate():
    # Regression guard: Claude Sonnet 5 bills $2/$10, not the $3/$15 the
    # family-flat "sonnet" row would apply -- that row encoded a Sept 1
    # 2026 increase that was announced and then cancelled.
    assert pricing.rates_for_model("claude-sonnet-5") == (2.0, 10.0)
    assert pricing.rates_for_model("claude-sonnet-4-6") == (3.0, 15.0)


def test_rates_for_model_strips_context_tier_suffix():
    assert pricing.rates_for_model("claude-opus-5[1m]") == (5.0, 25.0)


def test_rates_for_model_falls_back_to_family_for_unlisted_version():
    # A version not yet added to MODEL_PRICES still prices at the family
    # rate instead of silently returning $0.00.
    assert pricing.rates_for_model("claude-sonnet-9-9") == (3.0, 15.0)


def test_rates_for_model_returns_none_for_non_claude_id():
    assert pricing.rates_for_model("gpt-5") is None
    assert pricing.cost_for_turn("gpt-5", 1_000_000, 1_000_000, 0, 0) == 0.0


def test_cache_read_multiplier_for_fable_and_mythos_5_1_overrides():
    assert pricing.cache_read_multiplier_for("claude-fable-5-1") == 0.025
    assert pricing.cache_read_multiplier_for("claude-mythos-5-1") == 0.025


def test_cache_read_multiplier_for_default_still_point_one():
    # Fable 5 / Mythos 5 (not 5.1) stay at the plain 0.1x default.
    for model_id in (
        "claude-fable-5",
        "claude-mythos-5",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    ):
        assert pricing.cache_read_multiplier_for(model_id) == 0.1


def test_cost_for_turn_prices_cache_writes_from_the_real_ttl_split():
    # Isolate the cache-write component: zero input/output/cache-read.
    assert (
        pricing.cost_for_turn(
            "claude-opus-5", 0, 0, 0, 1_000_000, cache_write_5m=1_000_000
        )
        == 6.25
    )
    assert (
        pricing.cost_for_turn(
            "claude-opus-5", 0, 0, 0, 1_000_000, cache_write_1h=1_000_000
        )
        == 10.00
    )
    # No TTL split reported -- fall back to the lump total at 2.0x.
    assert pricing.cost_for_turn("claude-opus-5", 0, 0, 0, 1_000_000) == 10.00
    # A mixed split prices each half at its own multiplier.
    assert (
        pricing.cost_for_turn(
            "claude-opus-5",
            0,
            0,
            0,
            1_000_000,
            cache_write_5m=500_000,
            cache_write_1h=500_000,
        )
        == 8.125
    )


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
                        "cache_creation": "not a mapping",
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
    # A non-mapping usage.cache_creation (malformed transcript) must not
    # raise -- it falls back to an empty split, so cost_for_turn() prices
    # the lump cache_write_tokens total instead of crashing.
    assert turns[0]["cache_write_5m"] == 0
    assert turns[0]["cache_write_1h"] == 0
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
