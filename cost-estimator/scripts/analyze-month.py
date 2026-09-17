"""Retrospective Claude Code session cost and active-time analyzer.

This cohesive CLI stays in one file because parsing, aggregation, and CSV output share one schema.

Walks all session JSONLs (parents + subagents) under one or more
.claude/projects roots, dedupes assistant turns by message.id, and prices
each turn per the canonical formula (Opus / Sonnet / Haiku rates, flat
across the 1M-context tier, cache read 0.1x, cache write 1.25x).

Backs the retrospective half of the cost-estimator skill. Answers "what
did I spend?" and "how much active time did I wait?" with per-session,
daily, slash-command, and waste-pattern detail.

Usage:
    python analyze-month.py <projects_root> [<projects_root> ...]
        (--month 2026-04 | --start 2026-04-01 --end 2026-04-30)
        [--label hostA --label hostB]
        [--out <directory>]
        [--workers N]

Outputs CSVs in --out (default: ~/.agents/cost-estimator/reports/):
    sessions.csv  - one row per logical session (parent + its subagents)
    daily.csv     - daily totals
    commands.csv  - measured slash-command time per logical session
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pricing import (  # noqa: E402  -- after sys.path manipulation
    cost_for_turn,
    parse_timestamp,
    unpriced_usage,
    _loads,
)
from roots import (  # noqa: E402  -- after sys.path manipulation
    _resolve_roots,
    date_bounds,
    month_bounds,
    reports_directory,
)
import stats_cache  # noqa: E402  -- after sys.path manipulation


COMMAND_NAME_PATTERN = re.compile(
    r"<command-name>\s*([^<]+?)\s*</command-name>",
    re.IGNORECASE,
)
COMMAND_MESSAGE_PATTERN = re.compile(
    r"<command-message>\s*([^<]+?)\s*</command-message>",
    re.IGNORECASE,
)


@dataclass
class FileTotals:
    path: str
    is_subagent: bool
    parent_session: str
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    by_model: dict = field(default_factory=dict)
    unpriced_models: dict = field(default_factory=dict)  # model_id -> [turns, tokens]
    tool_calls: Counter = field(default_factory=Counter)
    assistant_turns: int = 0
    user_turns: int = 0
    had_compact: bool = False
    first_turn_input_tokens: int = 0  # initial system-prompt size proxy
    active_time_ms: int = 0
    timed_turns: int = 0
    command_time_ms: Counter = field(default_factory=Counter)
    command_invocations: Counter = field(default_factory=Counter)


def _content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


def _normalize_command(command):
    command = command.strip().split(maxsplit=1)[0] if command.strip() else ""
    if not command:
        return None
    if not command.startswith("/"):
        command = f"/{command}"
    return command.lower()


def command_name_from(content):
    text = _content_text(content)
    for pattern in (COMMAND_NAME_PATTERN, COMMAND_MESSAGE_PATTERN):
        match = pattern.search(text)
        if match:
            return _normalize_command(match.group(1))
    if text.strip().startswith("/"):
        return _normalize_command(text)
    return None


def _duration_ms(value):
    if isinstance(value, bool):
        return None
    try:
        duration = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return duration if duration >= 0 else None


def process_file(path, parent_session, is_subagent):
    totals = FileTotals(
        path=str(path), is_subagent=is_subagent, parent_session=parent_session
    )
    seen_ids = set()
    saw_any = False
    first_turn_recorded = False
    seen_duration_ids = set()
    pending_command = None

    try:
        with open(path, "rb") as handle:
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
                saw_any = True

                timestamp = parse_timestamp(entry.get("timestamp"))
                if timestamp:
                    if (
                        totals.first_timestamp is None
                        or timestamp < totals.first_timestamp
                    ):
                        totals.first_timestamp = timestamp
                    if (
                        totals.last_timestamp is None
                        or timestamp > totals.last_timestamp
                    ):
                        totals.last_timestamp = timestamp

                entry_type = entry.get("type")
                if entry_type == "system" and entry.get("subtype") == "local_command":
                    pending_command = command_name_from(entry.get("content"))
                    continue

                if entry_type == "system" and entry.get("subtype") == "turn_duration":
                    command_for_duration = pending_command
                    pending_command = None
                    duration_identifier = entry.get("uuid")
                    if duration_identifier:
                        if duration_identifier in seen_duration_ids:
                            continue
                        seen_duration_ids.add(duration_identifier)
                    duration = _duration_ms(entry.get("durationMs"))
                    if duration is None:
                        continue
                    totals.active_time_ms += duration
                    totals.timed_turns += 1
                    if command_for_duration:
                        totals.command_time_ms[command_for_duration] += duration
                        totals.command_invocations[command_for_duration] += 1
                    continue

                if entry_type == "user":
                    totals.user_turns += 1
                    if entry.get("isCompactSummary"):
                        totals.had_compact = True
                    message = entry.get("message") or {}
                    content = message.get("content")
                    command = command_name_from(content)
                    has_user_text = isinstance(content, str) or bool(
                        _content_text(content).strip()
                    )
                    if command or has_user_text or entry.get("isCompactSummary"):
                        pending_command = command
                    continue

                if entry_type != "assistant":
                    continue

                message = entry.get("message") or {}
                if message.get("role") != "assistant":
                    continue

                message_id = message.get("id")
                if message_id:
                    if message_id in seen_ids:
                        continue
                    seen_ids.add(message_id)

                totals.assistant_turns += 1

                usage = message.get("usage") or {}
                input_tokens = int(usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or 0)
                cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
                cache_write_tokens = int(usage.get("cache_creation_input_tokens") or 0)
                cache_creation = usage.get("cache_creation") or {}
                if not isinstance(cache_creation, dict):
                    cache_creation = {}
                cache_write_5m = int(
                    cache_creation.get("ephemeral_5m_input_tokens") or 0
                )
                cache_write_1h = int(
                    cache_creation.get("ephemeral_1h_input_tokens") or 0
                )

                totals.input_tokens += input_tokens
                totals.output_tokens += output_tokens
                totals.cache_read_tokens += cache_read_tokens
                totals.cache_write_tokens += cache_write_tokens

                model_identifier = message.get("model") or ""
                totals.cost_usd += cost_for_turn(
                    model_identifier,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                    cache_write_5m,
                    cache_write_1h,
                )

                gap = unpriced_usage(
                    model_identifier,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                )
                if gap is not None:
                    turns, tokens = gap
                    gap_entry = totals.unpriced_models.setdefault(
                        model_identifier, [0, 0]
                    )
                    gap_entry[0] += turns
                    gap_entry[1] += tokens

                model_breakdown = totals.by_model.setdefault(
                    model_identifier, [0, 0, 0, 0]
                )
                model_breakdown[0] += input_tokens
                model_breakdown[1] += output_tokens
                model_breakdown[2] += cache_read_tokens
                model_breakdown[3] += cache_write_tokens

                if not first_turn_recorded:
                    # First assistant turn input includes system prompt + tool
                    # schemas; high values flag MCP/skill loader bloat.
                    totals.first_turn_input_tokens = (
                        input_tokens + cache_read_tokens + cache_write_tokens
                    )
                    first_turn_recorded = True

                content = message.get("content") or []
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tool_name = block.get("name") or "?"
                            totals.tool_calls[tool_name] += 1
    except (OSError, IOError):
        return None

    if not saw_any:
        return None
    return totals


def discover_files(projects_root):
    found = []
    if not projects_root.exists():
        return found
    for slug_directory in projects_root.iterdir():
        if not slug_directory.is_dir():
            continue
        for entry in slug_directory.iterdir():
            if entry.is_file() and entry.suffix == ".jsonl":
                session_identifier = entry.stem
                found.append((entry, session_identifier, False))
            elif entry.is_dir():
                subagent_directory = entry / "subagents"
                if subagent_directory.is_dir():
                    session_identifier = entry.name
                    for subagent in subagent_directory.glob("agent-*.jsonl"):
                        found.append((subagent, session_identifier, True))
    return found


def _worker(payload):
    path, parent, is_subagent = payload
    return process_file(Path(path), parent, is_subagent)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "roots", nargs="*", help="One or more .claude/projects directories"
    )
    range_group = parser.add_mutually_exclusive_group(required=True)
    range_group.add_argument("--month", help="YYYY-MM (e.g. 2026-04)")
    range_group.add_argument(
        "--start", help="YYYY-MM-DD inclusive start (paired with --end)"
    )
    parser.add_argument("--end", help="YYYY-MM-DD inclusive end (paired with --start)")
    parser.add_argument(
        "--label",
        action="append",
        help="Label paired with each root (repeat to label multiple)",
    )
    parser.add_argument(
        "--out",
        default=str(reports_directory()),
        help="Output directory for CSVs "
        "(default: ~/.agents/cost-estimator/reports/, "
        "override dir via AGENTS_COST_REPORTS_DIR)",
    )
    parser.add_argument(
        "--workers", type=int, default=max(1, (os.cpu_count() or 4) // 2)
    )
    arguments = parser.parse_args()

    if arguments.month and arguments.end:
        parser.error("--end requires --start, not --month")

    if arguments.month:
        try:
            range_start, range_end = month_bounds(arguments.month)
        except ValueError:
            parser.error(f"--month must be YYYY-MM, got {arguments.month!r}")
        range_label = arguments.month
    else:
        if not arguments.end:
            parser.error("--start requires --end")
        range_start, range_end = date_bounds(arguments.start, arguments.end)
        range_label = f"{arguments.start}..{arguments.end}"

    resolved_roots = _resolve_roots(
        cli_roots=arguments.roots,
        cli_labels=arguments.label,
        env_value=os.environ.get("AGENTS_COST_ROOTS"),
    )
    # Validate paths exist before kicking off workers
    for label, path in resolved_roots:
        if not path.exists():
            sys.exit(f"error: root not found: {path} (label={label})")

    # Discover all files
    all_files = []
    for label, root in resolved_roots:
        files = discover_files(root)
        print(f"[{label}] {root}: {len(files)} jsonl files", file=sys.stderr)
        for path, parent, is_subagent in files:
            all_files.append((path, parent, is_subagent, label))
    print(f"Total files: {len(all_files)}", file=sys.stderr)

    file_results = []
    with ProcessPoolExecutor(max_workers=arguments.workers) as executor:
        futures = {
            executor.submit(_worker, (str(path), parent, is_subagent)): label
            for path, parent, is_subagent, label in all_files
        }
        completed = 0
        for future in as_completed(futures):
            label = futures[future]
            try:
                totals = future.result()
            except Exception as error:
                print(f"WORKER ERROR: {error}", file=sys.stderr)
                continue
            if totals is not None:
                file_results.append((totals, label))
            completed += 1
            if completed % 200 == 0:
                print(f"  processed {completed}/{len(all_files)}", file=sys.stderr)
    print(f"Got totals for {len(file_results)} files", file=sys.stderr)

    by_session = {}
    for totals, label in file_results:
        key = (label, totals.parent_session)
        session = by_session.setdefault(
            key,
            {
                "label": label,
                "session_id": totals.parent_session,
                "parent_path": "",
                "first_timestamp": None,
                "last_timestamp": None,
                "cost_usd": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "by_model": defaultdict(lambda: [0, 0, 0, 0]),
                "unpriced_models": defaultdict(lambda: [0, 0]),
                "tool_calls": Counter(),
                "assistant_turns": 0,
                "user_turns": 0,
                "subagent_count": 0,
                "subagent_cost": 0.0,
                "had_compact": False,
                "first_turn_input_tokens": 0,
                "active_time_ms": 0,
                "subagent_time_ms": 0,
                "timed_turns": 0,
                "subagent_timed_turns": 0,
                "command_time_ms": Counter(),
                "command_invocations": Counter(),
            },
        )
        if not totals.is_subagent:
            session["parent_path"] = totals.path
            session["first_turn_input_tokens"] = totals.first_turn_input_tokens
            session["active_time_ms"] += totals.active_time_ms
            session["timed_turns"] += totals.timed_turns
            session["command_time_ms"].update(totals.command_time_ms)
            session["command_invocations"].update(totals.command_invocations)
        else:
            session["subagent_count"] += 1
            session["subagent_cost"] += totals.cost_usd
            session["subagent_time_ms"] += totals.active_time_ms
            session["subagent_timed_turns"] += totals.timed_turns

        session["cost_usd"] += totals.cost_usd
        session["input_tokens"] += totals.input_tokens
        session["output_tokens"] += totals.output_tokens
        session["cache_read_tokens"] += totals.cache_read_tokens
        session["cache_write_tokens"] += totals.cache_write_tokens
        session["assistant_turns"] += totals.assistant_turns
        session["user_turns"] += totals.user_turns
        session["had_compact"] = session["had_compact"] or totals.had_compact

        for model_identifier, vals in totals.by_model.items():
            target = session["by_model"][model_identifier]
            for index in range(4):
                target[index] += vals[index]
        for model_identifier, vals in totals.unpriced_models.items():
            gap_target = session["unpriced_models"][model_identifier]
            gap_target[0] += vals[0]
            gap_target[1] += vals[1]
        for tool_name, count in totals.tool_calls.items():
            session["tool_calls"][tool_name] += count

        if totals.first_timestamp and (
            session["first_timestamp"] is None
            or totals.first_timestamp < session["first_timestamp"]
        ):
            session["first_timestamp"] = totals.first_timestamp
        if totals.last_timestamp and (
            session["last_timestamp"] is None
            or totals.last_timestamp > session["last_timestamp"]
        ):
            session["last_timestamp"] = totals.last_timestamp

    selected_sessions = []
    for session in by_session.values():
        reference = session["first_timestamp"] or session["last_timestamp"]
        if reference is None and session["parent_path"]:
            try:
                reference = datetime.fromtimestamp(
                    os.path.getmtime(session["parent_path"]), tz=timezone.utc
                )
            except OSError:
                reference = None
        if reference and range_start <= reference < range_end:
            session["session_date"] = (
                reference.astimezone(timezone.utc).date().isoformat()
            )
            selected_sessions.append(session)
    print(f"Sessions in {range_label}: {len(selected_sessions)}", file=sys.stderr)

    output_directory = Path(arguments.out)
    output_directory.mkdir(parents=True, exist_ok=True)

    fields = [
        "label",
        "session_date",
        "session_id",
        "first_timestamp",
        "last_timestamp",
        "cost_usd",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cache_hit_pct",
        "first_turn_input_tokens",
        "assistant_turns",
        "user_turns",
        "subagent_count",
        "subagent_cost",
        "active_time_seconds",
        "subagent_time_seconds",
        "timed_turns",
        "subagent_timed_turns",
        "had_compact",
        "models",
        "top_tools",
        "parent_path",
    ]
    rows = []
    selected_sessions.sort(key=lambda session: -session["cost_usd"])
    for session in selected_sessions:
        cache_total = (
            session["cache_read_tokens"]
            + session["cache_write_tokens"]
            + session["input_tokens"]
        )
        hit_percent = (
            session["cache_read_tokens"] / cache_total * 100.0
            if cache_total > 0
            else 0.0
        )
        models_label = ",".join(
            f"{name}:{sum(values)}"
            for name, values in session["by_model"].items()
            if any(values)
        )
        top_tools = session["tool_calls"].most_common(5)
        top_tools_label = ",".join(f"{name}:{count}" for name, count in top_tools)
        rows.append(
            {
                "label": session["label"],
                "session_date": session.get("session_date", ""),
                "session_id": session["session_id"],
                "first_timestamp": session["first_timestamp"].isoformat()
                if session["first_timestamp"]
                else "",
                "last_timestamp": session["last_timestamp"].isoformat()
                if session["last_timestamp"]
                else "",
                "cost_usd": round(session["cost_usd"], 4),
                "input_tokens": session["input_tokens"],
                "output_tokens": session["output_tokens"],
                "cache_read_tokens": session["cache_read_tokens"],
                "cache_write_tokens": session["cache_write_tokens"],
                "cache_hit_pct": round(hit_percent, 2),
                "first_turn_input_tokens": session["first_turn_input_tokens"],
                "assistant_turns": session["assistant_turns"],
                "user_turns": session["user_turns"],
                "subagent_count": session["subagent_count"],
                "subagent_cost": round(session["subagent_cost"], 4),
                "active_time_seconds": round(session["active_time_ms"] / 1000, 3),
                "subagent_time_seconds": round(session["subagent_time_ms"] / 1000, 3),
                "timed_turns": session["timed_turns"],
                "subagent_timed_turns": session["subagent_timed_turns"],
                "had_compact": session["had_compact"],
                "models": models_label,
                "top_tools": top_tools_label,
                "parent_path": session["parent_path"],
            }
        )

    sessions_csv = output_directory / "sessions.csv"
    with open(sessions_csv, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {sessions_csv} ({len(rows)} rows)", file=sys.stderr)

    commands_csv = output_directory / "commands.csv"
    command_fields = [
        "label",
        "session_date",
        "session_id",
        "command",
        "invocations",
        "active_seconds",
        "parent_path",
    ]
    command_rows = []
    for session in selected_sessions:
        for command in sorted(session["command_invocations"]):
            command_rows.append(
                {
                    "label": session["label"],
                    "session_date": session.get("session_date", ""),
                    "session_id": session["session_id"],
                    "command": command,
                    "invocations": session["command_invocations"][command],
                    "active_seconds": round(
                        session["command_time_ms"][command] / 1000, 3
                    ),
                    "parent_path": session["parent_path"],
                }
            )
    with open(commands_csv, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=command_fields)
        writer.writeheader()
        writer.writerows(command_rows)
    print(f"Wrote {commands_csv} ({len(command_rows)} rows)", file=sys.stderr)

    by_day = defaultdict(
        lambda: {
            "sessions": 0,
            "cost": 0.0,
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_write": 0,
            "subagents": 0,
            "active_seconds": 0.0,
        }
    )
    for row in rows:
        bucket_key = row["session_date"] or "unknown"
        bucket = by_day[bucket_key]
        bucket["sessions"] += 1
        bucket["cost"] += row["cost_usd"]
        bucket["input"] += row["input_tokens"]
        bucket["output"] += row["output_tokens"]
        bucket["cache_read"] += row["cache_read_tokens"]
        bucket["cache_write"] += row["cache_write_tokens"]
        bucket["subagents"] += row["subagent_count"]
        bucket["active_seconds"] += row["active_time_seconds"]
    daily_csv = output_directory / "daily.csv"
    with open(daily_csv, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "date",
                "sessions",
                "cost_usd",
                "input",
                "output",
                "cache_read",
                "cache_write",
                "subagents",
                "active_seconds",
            ]
        )
        for date_string in sorted(by_day):
            bucket = by_day[date_string]
            writer.writerow(
                [
                    date_string,
                    bucket["sessions"],
                    round(bucket["cost"], 4),
                    bucket["input"],
                    bucket["output"],
                    bucket["cache_read"],
                    bucket["cache_write"],
                    bucket["subagents"],
                    round(bucket["active_seconds"], 3),
                ]
            )
    print(f"Wrote {daily_csv}", file=sys.stderr)

    total_cost = sum(row["cost_usd"] for row in rows)
    total_input = sum(row["input_tokens"] for row in rows)
    total_output = sum(row["output_tokens"] for row in rows)
    total_cache_read = sum(row["cache_read_tokens"] for row in rows)
    total_cache_write = sum(row["cache_write_tokens"] for row in rows)
    cache_total = total_cache_read + total_cache_write + total_input
    overall_hit = total_cache_read / cache_total * 100.0 if cache_total else 0.0

    print(f"\n=== {range_label.upper()} SUMMARY ===")
    print(f"Sessions: {len(rows)}")
    print(f"Total raw cost: ${total_cost:,.2f}")
    total_active_seconds = sum(row["active_time_seconds"] for row in rows)
    print(f"Active turn time: {total_active_seconds:,.1f} seconds")
    print(
        f"Tokens: input {total_input:,}  output {total_output:,}  "
        f"cache_read {total_cache_read:,}  cache_write {total_cache_write:,}"
    )
    print(f"Overall cache hit: {overall_hit:.1f}%")

    by_label = defaultdict(lambda: [0.0, 0])
    for row in rows:
        by_label[row["label"]][0] += row["cost_usd"]
        by_label[row["label"]][1] += 1
    print("\nBy machine:")
    for label, (cost, count) in by_label.items():
        print(f"  {label}: ${cost:,.2f}  ({count} sessions)")

    print("\nTop 20 sessions by raw cost:")
    for row in rows[:20]:
        print(
            f"  ${row['cost_usd']:>7.2f}  {row['session_date']}  "
            f"{row['label']:9}  hit={row['cache_hit_pct']:>5.1f}%  "
            f"sub={row['subagent_count']:>3}  turns={row['assistant_turns']:>3}  "
            f"id={row['session_id'][:8]}  models={row['models'][:60]}"
        )

    print("\nLeast cache-friendly (>=$5 cost, hit < 70%):")
    bad = [
        row for row in rows if row["cost_usd"] >= 5.0 and row["cache_hit_pct"] < 70.0
    ]
    for row in bad[:15]:
        cw_to_in_ratio = row["cache_write_tokens"] / max(row["input_tokens"], 1)
        print(
            f"  ${row['cost_usd']:>7.2f}  hit={row['cache_hit_pct']:>5.1f}%  "
            f"cw/in={cw_to_in_ratio:>6.1f}  "
            f"id={row['session_id'][:8]}  tools={row['top_tools'][:60]}"
        )

    # Unpriced-model guardrail. cost_for_turn() silently returns $0.00 for
    # any model id pricing.py's model_family() doesn't recognize -- a new
    # model family would otherwise undercount every turn it appears in
    # with no signal anywhere in the pipeline (the COVERAGE check below
    # reconciles token counts, not price coverage, so it would stay quiet).
    unpriced_totals = defaultdict(lambda: [0, 0])
    for session in selected_sessions:
        for model_identifier, vals in session["unpriced_models"].items():
            gap_target = unpriced_totals[model_identifier]
            gap_target[0] += vals[0]
            gap_target[1] += vals[1]

    print("\n=== UNPRICED MODELS ===")
    if unpriced_totals:
        print(
            f"warning: {len(unpriced_totals)} model id(s) below have no "
            f"entry in pricing.py's model_family() -- cost_for_turn() "
            f"returned $0.00 for every one of these turns, so the totals "
            f"above UNDERCOUNT this range."
        )
        for model_identifier, (turns, tokens) in sorted(
            unpriced_totals.items(), key=lambda item: -item[1][1]
        ):
            print(
                f"  UNPRICED MODEL: {model_identifier!r} "
                f"({turns} turns, {tokens:,} tokens) -- update pricing.py"
            )
    else:
        print(
            "No unrecognized model ids in this range -- every turn priced "
            "against a known family."
        )

    # Coverage guardrail. The cost above prices only SURVIVING transcripts;
    # if transcripts were GC'd (old 30-day retention), the total undercounts.
    # Compare raw transcript in+out against /stats dailyModelTokens for the
    # same range and warn when coverage is low. Best-effort: a missing or
    # malformed stats-cache.json must not sink the main report, so we note
    # the skip reason rather than failing.
    print("\n=== COVERAGE vs /stats ===")
    try:
        combined_coverage, _ = stats_cache.coverage_for_roots(
            resolved_roots, range_start, range_end
        )
        warning = stats_cache.format_warning(combined_coverage)
        if warning:
            print(warning)
            print("Run stats_cache.py for the per-day cleared/match breakdown.")
        elif combined_coverage.coverage_pct is not None:
            print(
                f"{100 * combined_coverage.coverage_pct:.0f}% of the /stats "
                f"in+out aggregate is present in transcripts for this range "
                f"-- no material undercount."
            )
        else:
            print("No /stats aggregate recorded for this range; nothing to reconcile.")
    except Exception as error:
        print(f"note: coverage check skipped ({error})", file=sys.stderr)


if __name__ == "__main__":
    main()
