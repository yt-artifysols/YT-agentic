"""Eval CLI.

    python -m ytcore.evals run --agent script            # compare with the stored baseline
    python -m ytcore.evals run --agent all
    python -m ytcore.evals run --agent script --update-baseline

Baselines live in ytcore/evals/baselines/<agent>.<model>.json. A run FAILS when any score
drops below the baseline (tolerance 0.01), when cost rises more than 25%, or when the prompt
files / model differ from the ones the baseline was recorded with (so a prompt or model change
must come with a re-run and a committed baseline). --update-baseline refuses to record lower
scores unless --accept-regression is given.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from ytcore.evals.harness import AGENTS, evaluate

BASELINES = Path(__file__).with_name("baselines")
TOLERANCE = 0.01
COST_SLACK = 1.25


def baseline_path(agent: str, model: str) -> Path:
    return BASELINES / f"{agent}.{re.sub(r'[^a-z0-9_.-]', '-', model.lower())}.json"


def compare(result: dict[str, Any], baseline: dict[str, Any] | None) -> list[str]:
    if baseline is None:
        return ["no baseline recorded for this agent and model (run with --update-baseline)"]
    problems = []
    for metric, value in baseline["scores"].items():
        got = result["scores"].get(metric)
        if got is None or got < value - TOLERANCE:
            problems.append(f"{metric}: {got} < baseline {value}")
    if result["cost_cents_avg"] > baseline["cost_cents_avg"] * COST_SLACK + 0.5:
        problems.append(
            f"cost {result['cost_cents_avg']} > baseline "
            f"{baseline['cost_cents_avg']} x {COST_SLACK}"
        )
    if result["prompts"] != baseline["prompts"]:
        problems.append(
            "prompt files changed since the baseline was recorded; re-run the evals and commit "
            "the updated baseline (--update-baseline)"
        )
    return problems


def main(argv: list[str] | None = None, *, llm: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ytcore.evals")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--agent", choices=[*AGENTS, "all"], required=True)
    run.add_argument("--update-baseline", action="store_true")
    run.add_argument("--accept-regression", action="store_true")
    args = parser.parse_args(argv)

    if llm is None:
        from ytcore.agents.factory import build_llm
        from ytcore.config import build_secrets_provider, load_settings

        settings = load_settings()
        llm = build_llm(settings, build_secrets_provider(settings))

    failed = False
    for agent in AGENTS if args.agent == "all" else (args.agent,):
        result = evaluate(agent, llm)
        path = baseline_path(agent, result["model"])
        baseline = json.loads(path.read_text()) if path.exists() else None
        problems = compare(result, baseline)
        if args.update_baseline:
            regressions = [p for p in problems if "<" in p or "cost" in p]
            if baseline is not None and regressions and not args.accept_regression:
                print(f"{agent}: refusing to record a worse baseline: {regressions}")
                failed = True
                continue
            BASELINES.mkdir(exist_ok=True)
            path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
            print(f"{agent}: baseline written to {path.name}: {result['scores']}")
            continue
        status = "FAIL" if problems else "ok"
        print(f"{status:4} {agent}: {result['scores']} cost={result['cost_cents_avg']}c "
              f"({result['samples']} samples, model {result['model']})")  # fmt: skip
        for p in problems:
            print(f"     - {p}")
        failed |= bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
