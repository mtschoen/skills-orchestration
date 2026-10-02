# Cost and Active-Time Analysis: {RANGE_LABEL}

Generated {YYYY-MM-DD} from local JSONLs on {MACHINE_LIST}.

All dollar figures are **raw API-equivalent token cost** unless explicitly
labelled prorated. {IF --paid SUPPLIED: "Prorated cost = raw × {DISCOUNT_FACTOR} (the implied subscription discount factor)."}

## Headline numbers

| | Raw cost | Sessions | Cache hit |
|---|---|---|---|
| {LABEL_1} | ${COST} | {N} | {HIT}% |
| {LABEL_2} | ${COST} | {N} | {HIT}% |
| **Total** | **${TOTAL}** | **{N}** | **{HIT}%** |

| | Parent active time | Timed turns | Subagent processing time |
|---|---|---|---|
| **Total** | **{DURATION}** | **{N}** | **{DURATION}** |

Parent active time is measured user wait time from `turn_duration`. Subagent
processing time is reported separately because concurrent durations overlap.

## Slash command time

Source: `commands.csv` (or "SLASH COMMAND TIME" from summarize.py).

| Command | Active time | Timed invocations | Sessions |
|---|---|---|---|
| `/wrap` | 1h 23m | 7 | 6 |
| ... | | | |

When the user asks about one command, run `summarize.py --command /name` and
include its per-session detail. State that invocations without a surviving
`turn_duration` record are not estimated.

## Coverage / confidence

Source: the "COVERAGE vs /stats" section of `analyze-month.py` output (or
run `scripts/stats_cache.py` for the per-day breakdown).

State plainly whether this report covers the full range or is a **floor**:

- **If coverage ≥ 90%** (or no `/stats` aggregate exists): say the total is
  complete to within normal staleness. One line is enough.
- **If coverage < 90%**: the dollar total is a **FLOOR**. Some days were
  cache-cleared - their transcripts were garbage-collected before they could
  be priced (the old 30-day retention; now 365 days, so the gap is historical
  and stops growing). Report:

  | | |
  |---|---|
  | Coverage | **{PCT}%** of the /stats in+out aggregate present in transcripts |
  | Cleared days | **{N}** ({CLEARED_TOKENS} in+out tokens with no surviving transcript) |
  | Implication | reported spend is a **lower bound** for this range |

  Comparison is raw-transcript-in+out vs `/stats` `dailyModelTokens` (both
  non-deduped, so apples-to-apples - verified equal to the token on intact
  days). `/stats` carries no dollars and no cache, so the cleared days cannot
  be priced; name them and move on. Do **not** present the floor as the
  definitive total.

Also check the "UNPRICED MODELS" section of `analyze-month.py` output - a
different confidence gap than coverage. It flags model ids `pricing.py`
doesn't recognize; those turns priced at **$0.00**, so the total
undercounts by however many tokens are listed. If it fired, name the
model id(s) and turn/token counts, and say the total is a floor for that
reason too (on top of any coverage floor). If it's empty, one line
("every model id in this range priced against a known family") is enough.

## Subscription leverage

*Include this section only when the user supplied --paid.*

| | |
|---|---|
| Raw token cost (analyzer) | **${RAW}** |
| Actually paid | **${PAID}** |
| Leverage | **{X}×** |
| Implied prorate factor | **{F}** |

Subagent spend was ${SUB} ({PCT}% of total).

## Cross-validation against ccusage

*Include this section only when ccusage was actually run for the same range.*

`ccusage monthly --since YYYYMMDD --until YYYYMMDD` reports ${CCUSAGE_TOTAL}
across {LABELS}. Analyzer reports ${ANALYZER_TOTAL} - delta {DELTA}%.
Where they disagree, treat **${LOW}–${HIGH}** as the honest bracketed
range; residual gaps come from subagent handling and the $0.01/web-search
charge, not 1M-tier pricing (both price `[1m]` at the flat rate).

## Top sessions

Source: top-N rows from `sessions.csv`.

| Date | Session | Raw | Prorated | Hit | Sub | Turns | Top tools |
|---|---|---|---|---|---|---|---|
| YYYY-MM-DD | {ID} ({label}) | $X | $Y | Z% | N | T | {Tool:N, ...} |
| ... | | | | | | | |

If the user asked about a specific session, include the absolute path:

- Parent: `<roots>/<slug>/<session-id>.jsonl`
- Subagents: `<roots>/<slug>/<session-id>/subagents/agent-*.jsonl`

## Top tool calls (across all sessions)

Source: "TOP TOOL CALLS" section of summarize.py output.

| Tool | Calls | Sessions | Coverage |
|---|---|---|---|
| Bash | 11,417 | 279/739 | 37.8% |
| ... | | | |

Highlight any MCP tools concentrated in a small number of expensive
sessions - those are typical "specialized work" patterns (e.g. Unity
sessions, web automation), not waste.

## First-turn input bloat (skill / MCP loader proxy)

Source: "FIRST-TURN INPUT TOKENS" section.

- Sum across all sessions: {X} tokens
- Mean: {Y}    Median: {Z}

Top 10 sessions by opening payload size (system prompt + tool schemas
paid on the first turn of every fresh session):

| Tokens | Cost | Hit % | Session | Machine |
|---|---|---|---|---|
| ... | | | | |

Anything over ~50K is worth investigating - that fresh-session toll
gets paid every time the conversation restarts. Shaving auto-loaded
plugins/MCPs/skills compounds across sessions.

## Daily totals

Source: `daily.csv` (or "DAILY TOTALS" section of summarize.py output).

| Date | Sessions | Raw $ | Prorated $ | Subagent $ | Subagents | Machines |
|---|---|---|---|---|---|---|
| YYYY-MM-DD | N | $X | $Y | $Z | M | hostA+hostB |

Drop the prorated column when --paid was not supplied.

## Things to avoid - what the data shows

Walk through the four classes below. Either point at culprits or
explicitly state "no problem here" - both are useful findings.

### 1. Cache discipline

Sessions with `cost_usd >= 5` and `cache_hit_pct < 70` (from
`sessions.csv`). If empty, say so explicitly: that means cache is
working everywhere it should.

### 2. Skill / MCP loader bloat

Reference the first-turn input table above. Above ~50K is suspicious;
flag the slugs for the user to investigate which plugins/MCPs/skills
are loading.

### 3. Subagent fan-out

High `subagent_count` matched by high `assistant_turns` is healthy
(genuine parallel work). High `subagent_cost` in short-turn sessions
suggests unproductive fan-out. Pull from the top sessions list.

### 4. Repeat tool patterns

The "SKILL/MEMORY CANDIDATES" section of summarize.py output. Tools
called in >50% of sessions and >1000 calls are skill/memory candidates
the user has not yet captured. Empty list = existing skills/memory are
absorbing what they should.

## Notable specific findings

Anything outlier-shaped that doesn't fit the above categories. Single
mega-sessions, fleet-sweep days, unusual model mixes, etc. Quote
specific dollar figures and session IDs.

## Methodology

- Walks `<projects>/<slug>/<sessionId>.jsonl` (parents) and
  `<projects>/<slug>/<sessionId>/subagents/agent-*.jsonl` (subagents).
- Dedupes assistant turns by `message.id` to avoid the snapshot
  double-count gotcha.
- Prices each turn per the canonical Anthropic rate table (per-model
  rows, for example Opus 5.5 $4/$20, Sonnet 5.5 $2/$10, Haiku 4.5 $1/$5;
  cache read 0.1x input rate except 0.05x Opus 5.5 and 0.025x Fable 5.1;
  cache write 1.25x for 5m and 2x for 1h; the 1M tier bills at the flat
  rate, no surcharge).
- Filters by first-entry timestamp into [start, end+1) UTC.
- Sums `system/turn_duration.durationMs` for parent active time and reports
  subagent time separately to avoid double-counting concurrent work.
- Attributes slash commands from `system/local_command` or `<command-name>` to
  the following measured turn and writes `commands.csv`.
- Re-runnable: `python scripts/analyze-month.py <projects> --month YYYY-MM`
  then `python scripts/summarize.py [--paid <usd>] [--command /name]`.
