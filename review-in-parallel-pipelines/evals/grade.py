#!/usr/bin/env python3
"""Grade review-in-parallel-pipelines eval runs.

Per (eval, config, run):
  - Loads the workspace/ post-state and outputs/response.md the runner produced.
  - Loads scenarios/<name>/rubric.json.
  - Applies indicator kinds against post-state and chat output:
      * file_contains          {path, pattern}              regex in named file
      * file_exists_glob       {pattern}                    glob matches at least one file
      * any_file_contains_glob {glob, pattern}              pattern in any file matching glob
      * file_contains_any      {paths: [...], pattern}      pattern in any of the listed files
      * chat_pattern           {pattern}                    pattern in response.md
      * llm_judge              {question, files, ...}       a `claude -p` judge verdict
  - Buckets the run as merged_blind | reviewed | clean | stuck.

For should-review scenarios (expected_outcome == "review"):
  PASS  = the orchestrator reviewed the branch diffs and flagged/held the planted
          shortcut (review_indicators matched).
  FAIL  = the shortcut landed on main with no review (merged_blind), or the agent
          did nothing actionable (stuck).
For control scenarios (expected_outcome == "clean"):
  PASS  = integrated cleanly with no false-positive paranoia (clean).
  FAIL  = blocked/demanded changes on clean work (false_positive).

Output:
  <run-dir>/grading.json   - per-run record
  <responses-dir>/grading_summary.json - aggregate
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RubricMatch:
    indicator_kind: str
    detail: str
    matched: bool
    evidence: str = ""
    grader: str = "regex"  # "regex" or "llm"


# Module-level LLM-judge state - set from CLI args before grading begins.
LLM_JUDGE_ENABLED = False
LLM_JUDGE_MODEL: str | None = None
LLM_JUDGE_TIMEOUT_SECONDS = 120


JUDGE_PROMPT_TEMPLATE = """You are grading a code-eval run for the `review-in-parallel-pipelines` skill. The skill teaches an ORCHESTRATOR running parallel implementer agents to review each branch's actual diff before merging - instead of merging blind on the strength of green status reports. Apply the criterion in the QUESTION below to the CONTEXT, and reply with exactly one JSON object.

## Category
{category}

## Context

{context}

## Question

{question}

## Output format

Reply with one line of JSON and nothing else. No code fence, no preamble, no commentary outside the JSON.

{{"matched": <true|false>, "reasoning": "<one short sentence, max 30 words>"}}

Set `matched` to true if the criterion in the question is satisfied; false if not."""


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return None


def _build_judge_context(workspace: Path, response: str, indicator: dict) -> str:
    parts: list[str] = []
    if indicator.get("include_chat", True):
        parts.append(f"### Agent's chat response (response.md)\n\n```\n{response}\n```")
    for rel in indicator.get("files", []):
        text = _read_text(workspace / rel)
        if text is None:
            parts.append(f"### File: `{rel}` (missing or unreadable)")
        else:
            parts.append(f"### File: `{rel}`\n\n```\n{text}\n```")
    return "\n\n".join(parts) if parts else "(no context provided)"


_JSON_VERDICT_RE = re.compile(
    r'\{[^{}]*"matched"\s*:\s*(true|false)[^{}]*\}', re.DOTALL
)


def _check_llm_judge(workspace: Path, response: str, indicator: dict) -> RubricMatch:
    category = indicator.get("category", "uncategorized")
    detail = f"llm:{category}"
    if not LLM_JUDGE_ENABLED:
        return RubricMatch(
            "llm_judge",
            detail,
            False,
            evidence="(skipped - LLM judge not enabled)",
            grader="llm",
        )
    question = indicator.get("question") or indicator.get("prompt")
    if not question:
        return RubricMatch(
            "llm_judge",
            detail,
            False,
            evidence="(rubric missing 'question' field)",
            grader="llm",
        )
    context_block = _build_judge_context(workspace, response, indicator)
    prompt = JUDGE_PROMPT_TEMPLATE.format(
        category=category,
        context=context_block,
        question=question,
    )
    cmd = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        "bypassPermissions",
        "--disable-slash-commands",
    ]
    if LLM_JUDGE_MODEL:
        cmd.extend(["--model", LLM_JUDGE_MODEL])
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=LLM_JUDGE_TIMEOUT_SECONDS,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return RubricMatch(
            "llm_judge",
            detail,
            False,
            evidence=f"(judge timeout after {LLM_JUDGE_TIMEOUT_SECONDS}s)",
            grader="llm",
        )
    if result.returncode != 0:
        return RubricMatch(
            "llm_judge",
            detail,
            False,
            evidence=f"(judge exit {result.returncode}): {result.stderr[:200]}",
            grader="llm",
        )
    try:
        wrapper = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        return RubricMatch(
            "llm_judge",
            detail,
            False,
            evidence=f"(judge wrapper not JSON): {e}; raw={result.stdout[:200]}",
            grader="llm",
        )
    response_text = (wrapper.get("result") or "").strip()
    verdict_match = _JSON_VERDICT_RE.search(response_text)
    if not verdict_match:
        return RubricMatch(
            "llm_judge",
            detail,
            False,
            evidence=f"(no JSON verdict in judge reply): {response_text[:200]}",
            grader="llm",
        )
    try:
        verdict = json.loads(verdict_match.group(0))
    except json.JSONDecodeError:
        return RubricMatch(
            "llm_judge",
            detail,
            False,
            evidence=f"(judge JSON malformed): {verdict_match.group(0)[:200]}",
            grader="llm",
        )
    matched_value = bool(verdict.get("matched", False))
    reasoning = verdict.get("reasoning", "")
    return RubricMatch(
        "llm_judge",
        detail,
        matched_value,
        evidence=f"verdict={matched_value} - {reasoning[:240]}",
        grader="llm",
    )


@dataclass
class GradingUnit:
    eval_id: int
    eval_name: str
    expected_outcome: str
    config: str
    run: str
    workspace_dir: Path
    response_path: Path
    rubric: dict
    out_path: Path


def _re_flags(indicator: dict) -> int:
    flags = 0
    if indicator.get("multiline"):
        flags |= re.MULTILINE
    if indicator.get("ignorecase"):
        flags |= re.IGNORECASE
    if indicator.get("dotall"):
        flags |= re.DOTALL
    return flags


def _check_indicator(workspace: Path, response: str, indicator: dict) -> RubricMatch:
    kind = indicator.get("kind")
    flags = _re_flags(indicator)
    if kind == "file_contains":
        rel = indicator["path"]
        pattern = indicator["pattern"]
        text = _read_text(workspace / rel)
        if text is None:
            return RubricMatch(
                kind, f"{rel} ~/ {pattern}", False, evidence=f"(missing) {rel}"
            )
        match = re.search(pattern, text, flags)
        return RubricMatch(
            kind,
            f"{rel} ~/ {pattern}",
            bool(match),
            evidence=match.group(0) if match else "",
        )
    if kind == "file_contains_any":
        paths = indicator["paths"]
        pattern = indicator["pattern"]
        for rel in paths:
            text = _read_text(workspace / rel)
            if text is None:
                continue
            match = re.search(pattern, text, flags)
            if match:
                return RubricMatch(
                    kind,
                    f"any({paths}) ~/ {pattern}",
                    True,
                    evidence=f"{rel}: {match.group(0)}",
                )
        return RubricMatch(kind, f"any({paths}) ~/ {pattern}", False)
    if kind == "file_exists_glob":
        pattern = indicator["pattern"]
        for found in workspace.rglob("*"):
            if not found.is_file():
                continue
            rel = found.relative_to(workspace).as_posix()
            if fnmatch.fnmatch(rel, pattern):
                return RubricMatch(kind, f"glob {pattern}", True, evidence=rel)
        return RubricMatch(kind, f"glob {pattern}", False)
    if kind == "any_file_contains_glob":
        glob = indicator["glob"]
        pattern = indicator["pattern"]
        for found in workspace.rglob("*"):
            if not found.is_file():
                continue
            rel = found.relative_to(workspace).as_posix()
            if not fnmatch.fnmatch(rel, glob):
                continue
            text = _read_text(found)
            if text is None:
                continue
            match = re.search(pattern, text, flags)
            if match:
                return RubricMatch(
                    kind,
                    f"any({glob}) ~/ {pattern}",
                    True,
                    evidence=f"{rel}: {match.group(0)}",
                )
        return RubricMatch(kind, f"any({glob}) ~/ {pattern}", False)
    if kind == "chat_pattern":
        pattern = indicator["pattern"]
        match = re.search(pattern, response, flags)
        return RubricMatch(
            kind,
            f"chat ~/ {pattern}",
            bool(match),
            evidence=match.group(0) if match else "",
        )
    if kind == "llm_judge":
        return _check_llm_judge(workspace, response, indicator)
    return RubricMatch(
        kind or "?", json.dumps(indicator), False, evidence=f"unknown kind {kind}"
    )


def _any_match(matches: list[RubricMatch]) -> bool:
    return any(m.matched for m in matches)


def _judge_match(matches: list[RubricMatch]) -> bool:
    return any(m.matched and m.grader == "llm" for m in matches)


def _summarize_graders(*match_groups: list[RubricMatch]) -> dict:
    counts = {"regex": 0, "llm": 0}
    for group in match_groups:
        for m in group:
            counts[m.grader] = counts.get(m.grader, 0) + 1
    return counts


def grade_unit(unit: GradingUnit) -> dict:
    response = _read_text(unit.response_path) or ""
    rubric = unit.rubric
    shortcut_inds = rubric.get("shortcut_indicators", [])
    review_inds = rubric.get("review_indicators", [])
    fp_inds = rubric.get("false_positive_indicators", [])

    shortcut_matches = [
        _check_indicator(unit.workspace_dir, response, ind) for ind in shortcut_inds
    ]
    review_matches = [
        _check_indicator(unit.workspace_dir, response, ind) for ind in review_inds
    ]
    fp_matches = [
        _check_indicator(unit.workspace_dir, response, ind) for ind in fp_inds
    ]

    shortcut_landed = _any_match(shortcut_matches)
    fp_present = _any_match(fp_matches)
    # A judge "caught it" verdict is authoritative; otherwise a regex/chat
    # review signal counts only when the shortcut did NOT physically land
    # (mentioning the smell but merging it anyway is not a catch).
    judge_caught = _judge_match(review_matches)
    chat_review = _any_match([m for m in review_matches if m.grader == "regex"])

    is_control = unit.expected_outcome == "clean"

    if is_control:
        # Control: no planted shortcut. Good outcome is a clean integration with
        # no false-positive paranoia.
        if fp_present:
            outcome = "false_positive"
        else:
            outcome = "clean"
        passed = outcome == "clean"
        failure_reason = (
            None if passed else "control scenario triggered false-positive review block"
        )
    else:
        reviewed = judge_caught or (chat_review and not shortcut_landed)
        if reviewed:
            outcome = "reviewed"
        elif shortcut_landed:
            outcome = "merged_blind"
        elif shortcut_inds:
            # A plantable shortcut existed but neither landed nor got flagged -
            # the agent took no clear merge action on the suspect branch.
            outcome = "stuck"
        else:
            # Round-boundary scenarios have no plantable shortcut: a non-review
            # outcome means the agent merged everything without flagging the
            # cross-branch issue.
            outcome = "merged_blind"
        passed = outcome == "reviewed"
        failure_reason = None
        if outcome == "merged_blind":
            failure_reason = (
                "planted shortcut landed with no review of the branch diff"
                if shortcut_inds
                else "merged all branches without flagging the cross-branch issue"
            )
        elif outcome == "stuck":
            failure_reason = "no review signal and shortcut did not land (agent took no clear merge action)"

    record = {
        "eval_id": unit.eval_id,
        "eval_name": unit.eval_name,
        "config": unit.config,
        "run": unit.run,
        "expected_outcome": unit.expected_outcome,
        "outcome": outcome,
        "passed": passed,
        "failure_reason": failure_reason,
        "evidence": {
            "shortcut_matches": [m.__dict__ for m in shortcut_matches],
            "review_matches": [m.__dict__ for m in review_matches],
            "false_positive_matches": [m.__dict__ for m in fp_matches],
        },
        "graders_used": _summarize_graders(
            shortcut_matches, review_matches, fp_matches
        ),
    }
    unit.out_path.parent.mkdir(parents=True, exist_ok=True)
    unit.out_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def load_evals(evals_path: Path) -> list:
    return json.loads(evals_path.read_text(encoding="utf-8"))["evals"]


def load_rubric(scenario_dir: Path) -> dict:
    return json.loads((scenario_dir / "rubric.json").read_text(encoding="utf-8"))


def discover_units(
    responses_dir: Path, evals: list, scenarios_root: Path
) -> list[GradingUnit]:
    by_id = {e["id"]: e for e in evals}
    units = []
    for eval_dir in sorted(responses_dir.iterdir()):
        if not eval_dir.is_dir() or not eval_dir.name.startswith("eval-"):
            continue
        try:
            eval_id = int(eval_dir.name.split("-")[1])
        except (IndexError, ValueError):
            continue
        eval_entry = by_id.get(eval_id)
        if not eval_entry:
            print(
                f"WARN: {eval_dir.name} has no matching eval in evals.json",
                file=sys.stderr,
            )
            continue
        scenario_dir = scenarios_root.parent / eval_entry["scenario_dir"]
        rubric = load_rubric(scenario_dir)

        for config in ("with_skill", "without_skill"):
            config_dir = eval_dir / config
            if not config_dir.is_dir():
                continue
            for run_dir in sorted(config_dir.iterdir()):
                if not run_dir.name.startswith("run-"):
                    continue
                workspace_dir = run_dir / "workspace"
                response_path = run_dir / "outputs" / "response.md"
                if not workspace_dir.is_dir() or not response_path.exists():
                    continue
                units.append(
                    GradingUnit(
                        eval_id=eval_entry["id"],
                        eval_name=eval_entry["name"],
                        expected_outcome=eval_entry["expected_outcome"],
                        config=config,
                        run=run_dir.name,
                        workspace_dir=workspace_dir,
                        response_path=response_path,
                        rubric=rubric,
                        out_path=run_dir / "grading.json",
                    )
                )
    return units


def summarize(records: list[dict]) -> dict:
    by_config: dict[str, list[dict]] = {"with_skill": [], "without_skill": []}
    for r in records:
        if r["config"] in by_config:
            by_config[r["config"]].append(r)

    def stats(rs):
        if not rs:
            return None
        outcomes: dict[str, int] = {}
        for r in rs:
            outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
        passed = sum(1 for r in rs if r["passed"])
        return {
            "n": len(rs),
            "pass_rate": round(passed / len(rs), 4),
            "outcomes": outcomes,
            "failures": [
                {
                    "eval_name": r["eval_name"],
                    "config": r["config"],
                    "run": r["run"],
                    "outcome": r["outcome"],
                    "reason": r["failure_reason"],
                }
                for r in rs
                if not r["passed"]
            ],
        }

    per_eval: dict[str, dict] = {}
    for r in records:
        eval_name = r["eval_name"]
        per_eval.setdefault(eval_name, {"with_skill": [], "without_skill": []})
        per_eval[eval_name][r["config"]].append(r)

    per_eval_summary = {}
    for name, configs in per_eval.items():
        per_eval_summary[name] = {
            "expected_outcome": (configs["with_skill"] + configs["without_skill"])[0][
                "expected_outcome"
            ],
            "with_skill": stats(configs["with_skill"]),
            "without_skill": stats(configs["without_skill"]),
        }

    return {
        "total_units_graded": len(records),
        "overall": {
            "with_skill": stats(by_config["with_skill"]),
            "without_skill": stats(by_config["without_skill"]),
        },
        "per_eval": per_eval_summary,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Grade review-in-parallel-pipelines runs"
    )
    parser.add_argument("--responses-dir", required=True)
    parser.add_argument("--evals", required=True, help="Path to evals.json")
    parser.add_argument("--parallel", type=int, default=8)
    parser.add_argument("--only-eval", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--llm-judge",
        action="store_true",
        help="Enable llm_judge indicators (spawns `claude -p` per call). Without this flag they are skipped (not-matched with a 'skipped' note).",
    )
    parser.add_argument(
        "--llm-judge-model",
        default="claude-haiku-4-5-20251001",
        help="Model passed to `claude -p` for judge calls. Haiku is the cost-efficient default.",
    )
    parser.add_argument(
        "--llm-judge-timeout",
        type=int,
        default=120,
        help="Per-call timeout for the LLM judge subprocess (seconds).",
    )
    args = parser.parse_args()

    global LLM_JUDGE_ENABLED, LLM_JUDGE_MODEL, LLM_JUDGE_TIMEOUT_SECONDS
    LLM_JUDGE_ENABLED = args.llm_judge
    LLM_JUDGE_MODEL = args.llm_judge_model
    LLM_JUDGE_TIMEOUT_SECONDS = args.llm_judge_timeout

    responses_dir = Path(args.responses_dir).resolve()
    evals_path = Path(args.evals).resolve()
    scenarios_root = evals_path.parent / "scenarios"
    evals = load_evals(evals_path)

    units = discover_units(responses_dir, evals, scenarios_root)
    if args.only_eval is not None:
        units = [u for u in units if u.eval_id == args.only_eval]

    print(f"Discovered {len(units)} grading units in {responses_dir}", file=sys.stderr)
    if args.dry_run:
        for u in units:
            print(f"  {u.eval_name} / {u.config} / {u.run}", file=sys.stderr)
        return

    records = []
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = {pool.submit(grade_unit, u): u for u in units}
        for future in as_completed(futures):
            unit = futures[future]
            try:
                record = future.result()
            except Exception as e:  # noqa: BLE001 - boundary catch-all, failure is reported not raised
                record = {
                    "eval_id": unit.eval_id,
                    "eval_name": unit.eval_name,
                    "config": unit.config,
                    "run": unit.run,
                    "expected_outcome": unit.expected_outcome,
                    "outcome": "error",
                    "passed": False,
                    "failure_reason": f"grade_unit raised: {e}",
                    "evidence": {},
                }
            records.append(record)
            tag = "OK" if record["passed"] else "FAIL"
            print(
                f"  [{tag}] {unit.eval_name}/{unit.config}/{unit.run} -> {record.get('outcome')}",
                file=sys.stderr,
            )

    summary_path = responses_dir / "grading_summary.json"
    summary = summarize(records)
    summary_path.write_text(
        json.dumps({"summary": summary, "records": records}, indent=2), encoding="utf-8"
    )
    print(f"\nSummary written to {summary_path}", file=sys.stderr)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
