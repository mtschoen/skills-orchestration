"""Unit tests for scripts/pricing.py's unpriced_usage() guardrail.

model_family() returns None for any model id it doesn't recognize, and
cost_for_turn() then silently returns $0.00 for that turn -- a new model
family would otherwise undercount every retrospective report with no
warning anywhere in the pipeline. unpriced_usage() is the pure helper
analyze-month.py uses to detect and tally that gap.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import pricing  # noqa: E402


def test_unpriced_usage_none_for_known_family():
    assert pricing.unpriced_usage("claude-opus-4-6", 100, 50, 10, 5) is None


def test_unpriced_usage_none_for_sonnet5_variant():
    # sonnet-5 is a version of the generic "sonnet" family with its own
    # VERSION_PRICES row -- model_family() must still recognize it.
    assert pricing.unpriced_usage("claude-sonnet-5-20260101", 1, 1, 0, 0) is None


def test_unpriced_usage_flags_unrecognized_model():
    result = pricing.unpriced_usage("claude-nova-7", 1000, 200, 300, 100)
    assert result == (1, 1600)


def test_unpriced_usage_tokens_sum_all_four_buckets():
    _, tokens = pricing.unpriced_usage("mystery-model", 10, 20, 30, 40)
    assert tokens == 100


def test_unpriced_usage_ignores_missing_model_id():
    """A missing/empty model id is a different bug (malformed entry), not
    an unrecognized-family gap -- don't flag it here."""
    assert pricing.unpriced_usage("", 100, 50, 0, 0) is None
    assert pricing.unpriced_usage(None, 100, 50, 0, 0) is None


def test_unpriced_usage_ignores_zero_usage_synthetic_entry():
    """Real Claude Code transcripts carry "<synthetic>" entries with an
    unrecognized model id but all-zero token usage. Without the
    zero-volume exclusion, every real report would falsely raise the
    UNPRICED MODELS warning even though nothing was actually
    undercounted -- flagging a turn that moved zero tokens is noise,
    not a gap."""
    assert pricing.unpriced_usage("<synthetic>", 0, 0, 0, 0) is None


def test_unpriced_usage_still_flags_nonzero_unknown_model():
    """The zero-volume exclusion must not swallow a real gap: a genuine
    unrecognized model id with nonzero usage still flags."""
    result = pricing.unpriced_usage("<synthetic>", 5, 0, 0, 0)
    assert result == (1, 5)


def test_unpriced_usage_matches_cost_for_turn_zero_dollar_case():
    """Whenever cost_for_turn() would silently return 0.0 for a real model
    id, unpriced_usage() must flag it -- that's the exact gap it exists
    to surface."""
    model_id = "claude-nova-7"
    assert pricing.cost_for_turn(model_id, 100, 50, 10, 5) == 0.0
    assert pricing.unpriced_usage(model_id, 100, 50, 10, 5) is not None


if __name__ == "__main__":
    test_unpriced_usage_none_for_known_family()
    test_unpriced_usage_none_for_sonnet5_variant()
    test_unpriced_usage_flags_unrecognized_model()
    test_unpriced_usage_tokens_sum_all_four_buckets()
    test_unpriced_usage_ignores_missing_model_id()
    test_unpriced_usage_ignores_zero_usage_synthetic_entry()
    test_unpriced_usage_still_flags_nonzero_unknown_model()
    test_unpriced_usage_matches_cost_for_turn_zero_dollar_case()
    print("OK")
