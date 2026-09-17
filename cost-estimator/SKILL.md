---
name: cost-estimator
description: Use when the user asks for retrospective Claude Code cost or active-time analysis over a date range, including spend, top sessions, cache discipline, subscription leverage, time spent waiting, or time spent on slash commands such as /wrap.
---

# cost-estimator (retrospective)

This skill answers "what did I spend on Claude Code over [some date range]?"
and "how much active time did Claude Code spend over that range?"
It walks the local session transcripts and produces defensible cost and active
time breakdowns. The cost analysis goes beyond `/cost` (current session only)
and `ccusage` (no per-machine grouping, no waste-pattern flags).

For timing questions, active time means measured `turn_duration` wall clock
from the transcript. It is not inferred from gaps between timestamps, so idle
time between prompts is excluded.

## When to invoke

The user's intent is retrospective spend analysis when they say things
like "what did I spend last month", "/cost estimate April", "audit my
Claude usage", "where did my budget go", "show me my top sessions",
"break down my spending". If they instead ask "how much will THIS cost"
about something they have not yet run, that is the predictive case which
is not yet built - say so.

## Inputs to gather from the user

Most invocations need three things; ask only when not obvious:

1. **Date range.** Accept any of: a YYYY-MM month ("April 2026", "2026-04",
   "last month"), or an explicit YYYY-MM-DD start/end pair. Convert
   relative phrases to absolute dates before calling the script.
2. **Projects roots.** Default to `~/.claude/projects` on the current
   host. If the user works across multiple machines, ask whether to
   include other roots - typically a network-mounted path to another
   host's `~/.claude/projects`. The user's AGENTS.md often documents
   the cross-machine convention; consult it before guessing.

   Multi-machine setups can set the `AGENTS_COST_ROOTS` env var once
   (format: `"label1:path1,label2:path2"`) - analyze-month.py picks it
   up automatically when no positional roots are given. CLI args
   always win when both are present.
3. **Actually paid (optional).** If the user wants prorated cost
   alongside raw, ask what they paid that period (Max plan tier + any
   extra usage). Without it, the script reports raw only - that is
   fine when the user only cares about API-equivalent value.

## Steps

1. Resolve the date range. For a whole month, pass `--month YYYY-MM`.
   For arbitrary ranges, pass `--start YYYY-MM-DD --end YYYY-MM-DD` (end
   is inclusive).
2. Run the analyzer:

   ```bash
   python <skill-root>/scripts/analyze-month.py \
       <root-1> [<root-2> ...] \
       --month <YYYY-MM>           # OR --start ... --end ...
       --label <name-1> [--label <name-2> ...]
   ```

   This writes `sessions.csv`, `daily.csv`, and `commands.csv` to
   `~/.agents/cost-estimator/reports/` - outside the installed skill tree,
   so a skill reinstall never deletes generated data (override the location
   with `AGENTS_COST_REPORTS_DIR`) - and prints
   a brief stderr summary. `sessions.csv` includes parent active time and
   separately reported subagent time. `commands.csv` attributes measured turns
   to slash commands such as `/wrap`. It also prints two guardrail sections:

   - **"UNPRICED MODELS"** - flags model ids `pricing.py` doesn't
     recognize. Those turns silently price at $0.00, so the dollar total
     undercounts by however many tokens are listed. Treat a non-empty
     list as a **floor** and add the family to `pricing.py`'s
     `model_family()` before trusting the total.
   - **"COVERAGE vs /stats"** - flags when surviving transcripts
     undercount the range because old days were cache-cleared
     (transcripts garbage-collected under the old 30-day retention).

   When either warns, the dollar total is a **floor** - carry that into
   the report (step 4), and drill in with `stats_cache.py` (step 8) for
   the per-day coverage breakdown.
3. Run the deeper summary:

   ```bash
   python <skill-root>/scripts/summarize.py [--paid <usd>] [--command /wrap]
   ```

   Pass `--paid` only if the user supplied an actually-paid amount. Use
   `--command` when the user asks about one slash command. The summary prints
   parent active time, separately reported subagent time, slash-command totals
   and detail, subagent-vs-parent cost reconciliation, top tool calls,
   first-turn input bloat, top-N sessions, daily totals, and (when
   `--paid` is set) leverage and prorated columns.
4. **Synthesize a markdown report** for the user. Follow
   `REPORT_TEMPLATE.md` - it specifies every section to include
   (headline, coverage/confidence, leverage, top sessions, top tool
   calls, first-turn bloat, daily totals, things-to-avoid walkthrough,
   methodology). Don't drop sections to save space; the value of the
   cost report is precisely that it surfaces patterns the user wouldn't see
   in a one-line summary. A time-only report may omit unrelated cost sections.
   Pull each included section from the corresponding part
   of summarize.py's stdout - except the coverage section, which comes
   from analyze-month.py's "UNPRICED MODELS" and "COVERAGE vs /stats"
   output. Always label raw vs prorated explicitly, and if either
   guardrail warned, label the total a floor - never leave it ambiguous.
   For a time-only question, lead with active time and the requested
   slash-command detail; retain cost sections only when they help answer
   the request.
5. **Plot top spike sessions on demand.** When the report flags a
   top-N session that the user wants to investigate, render its
   per-turn trajectory:

   ```bash
   python <skill-root>/scripts/plot-session.py <session-id-prefix> --open
   ```

   This produces an HTML chart (per-turn bars + cumulative line +
   hover tooltips) at `~/.agents/cost-estimator/reports/session-<prefix>.html`,
   helping the user see *where* in the session the spike happened.
   Pass `--inline-js` for an offline-viewable file. Pass `--x time`
   to render the x-axis as wall-clock time instead of turn number -
   useful for sessions with long idle gaps. Currently plots the
   parent JSONL only - subagent cost appears in the page caption but
   not as overlay curves.
6. **Plot the aggregate trend across the range.** Run after
   analyze-month.py so the CSV exists:

   ```bash
   python <skill-root>/scripts/plot-trend.py \
       (--month YYYY-MM | --start YYYY-MM-DD --end YYYY-MM-DD) \
       [--bucket {day,week,month}] [--inline-js] [--open]
   ```

   Produces an HTML chart at `~/.agents/cost-estimator/reports/trend-<range>.html`.
   Bars stack per-machine cost in each bucket; right-axis line shows
   the cumulative total. Bucket size auto-picks from range length
   (≤14d→day, ≤90d→week, >90d→month) or override with `--bucket`.
7. **Compare two windows side-by-side.** When the user asks "is my
   spend trending up/down?" or "how does this week compare to last?",
   render the period-over-period overlay:

   ```bash
   python <skill-root>/scripts/plot-compare.py \
       (--month YYYY-MM | --start YYYY-MM-DD --end YYYY-MM-DD | --last <Nh|Nd>) \
       [--bucket {day,week,month}] [--inline-js] [--open]
   ```

   The prior window is auto-derived as the same-length window
   immediately before the current one. Bucket-index (Day/Week/Month N)
   makes paired bars apples-to-apples wall-clock-relative slices, not
   calendar-aligned. Output lands in `~/.agents/cost-estimator/reports/compare-<range>.html`.
8. **Reconcile against /stats on demand.** When the coverage warning
   fires (or the user asks "why doesn't this match /stats?"), run the
   full per-day reconciliation:

   ```bash
   python <skill-root>/scripts/stats_cache.py \
       <root-1> [<root-2> ...] \
       (--month YYYY-MM | --start YYYY-MM-DD --end YYYY-MM-DD) \
       [--label <name-1> ...] [--threshold 0.90]
   ```

   Prints, per machine: the `stats-cache.json` `modelUsage` inventory, a
   per-day `/stats`-vs-transcript table (match / partial / cleared), and
   the coverage %. `cleared` days are ones whose transcripts were
   garbage-collected - `/stats` `dailyModelTokens` is their only
   surviving record (in+out only, no cache, no dollars). The comparison
   is raw-vs-raw (both non-deduped, verified equal to the token on intact
   days), so a fully-present range reads ~100%. Use this to *explain* the
   gap, not to "fix" the dollar total - cleared days genuinely cannot be
   priced. Roots/stats files are resolved exactly like analyze-month.py
   (`AGENTS_COST_ROOTS` or positional roots; the stats file is the
   sibling `stats-cache.json` of each projects root).
9. **Probe cache TTL on demand.** If the user asks whether their cache is
   1h- or 5m-TTL, or why cache-write cost looks high:

   ```bash
   python <skill-root>/scripts/cache_ttl.py <root-1> [<root-2> ...]
   ```

   Reports the `ephemeral_5m` vs `ephemeral_1h` split of cache-write
   tokens and a behavioral inter-turn-gap table (a high "miss%" in the
   5–60m buckets would betray a 5m TTL letting prefixes expire).
   Subscription accounts write 1h-TTL by default.
10. **Offer to save.** If the analysis was substantive, save the report
   to `~/.agents/cost-estimator/reports/<range>.md`. That directory lives
   outside the installed skill and survives reinstalls. Capture the
   summary.txt alongside via shell redirect:

   ```bash
   python <skill-root>/scripts/summarize.py [--paid <usd>] \
       > ~/.agents/cost-estimator/reports/summary.txt
   ```

## Interpreting time totals

- **Parent active time is user wait time.** Sum `turn_duration` only from the
  parent transcript. This excludes idle gaps between prompts.
- **Subagent time is separate.** Subagents can run concurrently with the parent
  and each other, so never add subagent time to parent time and call the result
  elapsed time.
- **Slash-command time is measured turn time.** `commands.csv` associates a
  `system/local_command` record (or the older `<command-name>` wrapper) with
  the following `turn_duration`. Invocations without a duration record are not
  estimated.
- **Older transcripts may lack timing.** Report the number of timed turns and
  describe the total as coverage of surviving `turn_duration` records, not a
  complete estimate when records are absent.

## What "things to avoid" looks like in this skill's output

The data lets you flag four classes of waste. Walk through each in the
report and either point at culprits or explicitly state "no problem
here":

- **Cache discipline.** From `sessions.csv` look at sessions with
  `cost_usd >= 5` and `cache_hit_pct < 70`. If the list is empty, say
  so - that is itself a useful finding.
- **Skill / MCP loader bloat.** `first_turn_input_tokens` proxies the
  system-prompt + tool-schema payload paid on every fresh session. The
  summary script ranks the worst offenders. Above ~50K is suspicious
  and worth flagging the slug for the user to investigate.
- **Subagent fan-out.** `subagent_count` and `subagent_cost` per
  session. High counts are healthy when matched by high turn counts;
  high subagent cost in short sessions indicates unproductive fan-out.
- **Repeat tool patterns.** The summary's "tools called in >50% of
  sessions and >1000 calls" section lists candidates that would benefit
  from skill or memory wrapping. An empty list means existing
  skills/memory are doing their job.

## Cross-validation discipline

Always show the user that the number is defensible. The summary script
already prints subagent-vs-parent breakdowns. When the user asks about
a single specific number, also report what `ccusage monthly` says for
the same range - they may diverge by a few percent (from subagent
handling and the $0.01/web-search charge, not from 1M-tier pricing:
both price the 1M tier at the flat rate). Bracket the truth between the
two values rather than asserting one.

## Pricing table (canonical, inlined)

The analyzer applies these rates per million tokens. This table and
`scripts/pricing.py` are the source of truth - keep the two in sync
when rates change.

| Model | Input | Output | Cache read |
|---|---|---|---|
| Fable / Mythos 5.1 | $10 | $50 | **0.025x** |
| Fable / Mythos 5   | $10 | $50 | 0.1x |
| Opus (4.5 - 5)     | $5  | $25 | 0.1x |
| Sonnet 5           | **$2**  | **$10** | 0.1x |
| Sonnet 4.5 / 4.6   | $3  | $15 | 0.1x |
| Haiku 4.5          | $1  | $5  | 0.1x |

One flat rate per model for all time -- no time-windowed pricing.
Historical reports drift when a price changes; these are relative
quantities, not an invoice.

Rates are **per model version**, not per family. That rule changed on
2026-09-17: Sonnet 5 bills at $2/$10 while Sonnet 4.6 bills at $3/$15,
so the old family-flat `sonnet` row overcharged every Sonnet 5 token by
50%. (It encoded a Sept 1 2026 increase that was announced and then
cancelled.) `pricing.py`'s `MODEL_PRICES` is the source of truth;
`PRICES` remains a per-family fallback so an unrecognized new version
still prices instead of silently costing $0.00.

Cache multipliers, relative to base input rate. Cache **read** is
**0.1x**, except **0.025x on Fable 5.1 / Mythos 5.1** -- a 4x
difference on the dominant token class in long cached sessions.

Cache **write** is priced from the turn's real TTL split:
**1.25x** for `ephemeral_5m` tokens, **2.0x** for `ephemeral_1h`,
read per-turn from `usage.cache_creation`. Turns that report only a
lump `cache_creation_input_tokens` fall back to 2.0x.

**The old 1.25x-for-both override is retired (resolved 2026-09-17).**
It was measured in 2026-04 and went stale as models turned over.
Solving for the implied multiplier against `costUSD` in
`~/.claude.json`'s `lastModelUsage`, across 40 project/model records,
splits perfectly by generation:

| Implied multiplier | Models | Records |
|---|---|---|
| exactly 1.250 | `opus-4-7`, `sonnet-4-6` | 16/16 |
| exactly 2.000 | `opus-5`, `opus-4-8`, `sonnet-5`, `fable-5`, `fable-5-1` | 20/20 |

So the 2026-04 finding was correct for the models of its day, and every
current model bills 1h writes at the documented 2.0x. Four `opus-4-6`
and `sonnet-5` records solved to 3.3-6.5 and remain unexplained --
likely web-search charges folded into `costUSD`, or a historical base
rate this table no longer carries. They are a small minority and do not
affect current-model pricing.

Re-run that solve (it needs only `~/.claude.json`) after any model
generation turns over; it is the cheapest available ground truth.

The 1M-context tier (when `model.id` contains `[1m]`) bills at the SAME
flat per-token rate - no surcharge above 200K (verified 2026-06 against
`~/.claude.json` billing across 28 Opus[1m] sessions and current
Anthropic docs). Earlier docs and this skill modeled a 2x doubling; it
was never present in the billed aggregate.

## What this skill does not do (yet)

- Predict cost or duration for a future task. A predictive cost companion is in design
  (uses `count_tokens` API + heuristics + the historical
  `sessions.csv` as a reference dataset).
- Subagent timeline overlays. `plot-session.py` plots the parent
  JSONL only; subagent costs are summarized in the chart caption but
  not plotted as their own anchored sub-trajectories. Adding overlay
  curves anchored to spawn/finish timestamps is a planned Phase 2.
- Per-project breakdown. The analyzer groups by machine label, not by
  project slug. Easy extension: bucket `parent_path` by its containing
  directory in a follow-up summary.
- Dollar backfill of cache-cleared days. `stats_cache.py` *detects* and
  *quantifies* cleared days (coverage %, cleared in+out tokens) but does
  not estimate their dollars. `/stats` `dailyModelTokens` is raw
  (non-deduped, ~3.2× hot), carries no input/output split (only a
  per-model in+out sum), and excludes cache entirely - so any backfilled
  dollar figure would be a coarse guess stacked on three approximations.
  Deferred until that estimation can be designed deliberately; for now
  the floor + the cleared-token count is the honest answer.

## Files in this skill

An installed copy ships `SKILL.md`, `REPORT_TEMPLATE.md` (the report
template step 4 follows), and `scripts/`. Every script the steps above
reference is invoked directly as `python <skill-root>/scripts/<name>.py`.

Four modules in `scripts/` are never invoked directly - they are shared
helpers the others import so the logic cannot drift between them:

- `pricing.py` - the canonical pricing formula (rates, cache multipliers,
  flat 1M-tier pricing) plus the JSONL turn-iterator. Keep the inlined
  pricing table above in sync with it.
- `roots.py` - root resolution (`AGENTS_COST_ROOTS` / positional roots),
  date-bound parsers, `stats_file_for()`, and `reports_directory()`, which
  is why generated data lands outside the installed skill tree.
- `trend_data.py` - bucket math, CSV reading, and range parsing shared by
  both trend charts.
- `chart_runtime.py` - Chart.js version constants and download cache.

See `README.md` in the source repo for dev-only files (screenshot regen
tooling, test fixtures) that don't ship.
