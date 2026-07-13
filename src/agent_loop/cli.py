"""Command-line trigger and teaching trace renderer."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .agent import ScriptedAgent
from .application import ApplyPreview, apply_run
from .config import load_run_spec
from .engine import LoopEngine
from .storage import StateStore
from .types import AgentLoopError, LoopEvent, RunState, RunStatus


class ConsoleTrace:
    def __init__(self, step: bool = False) -> None:
        self.step = step

    def __call__(self, event: LoopEvent) -> None:
        label = event.event_type.upper().replace("_", " ")
        print(f"[{label:<24}] {event.summary}")
        if self.step:
            input("Press Enter for the next event...")

    @staticmethod
    def render_saved(event: dict) -> None:
        label = str(event["event_type"]).upper().replace("_", " ")
        print(f"[{label:<24}] {event['summary']}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-loop", description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    run = subcommands.add_parser("run", help="start a scenario")
    run.add_argument("scenario", type=Path)
    run.add_argument("--run-id")
    run.add_argument("--runs-dir", type=Path, default=Path(".agent-loop/runs"))
    run.add_argument("--step", action="store_true")

    resume = subcommands.add_parser("resume", help="resume a persisted run")
    resume.add_argument("run_id")
    resume.add_argument("--scenario", type=Path)
    resume.add_argument("--runs-dir", type=Path, default=Path(".agent-loop/runs"))
    decision = resume.add_mutually_exclusive_group()
    decision.add_argument("--approve", action="store_true")
    decision.add_argument("--reject", action="store_true")
    resume.add_argument("--step", action="store_true")

    inspect = subcommands.add_parser("inspect", help="render a saved event timeline")
    inspect.add_argument("run_id")
    inspect.add_argument("--runs-dir", type=Path, default=Path(".agent-loop/runs"))

    apply = subcommands.add_parser("apply", help="apply a completed workspace")
    apply.add_argument("run_id")
    apply.add_argument("target_dir", type=Path)
    apply.add_argument("--runs-dir", type=Path, default=Path(".agent-loop/runs"))
    apply.add_argument("--yes", action="store_true", help="confirm the displayed preview")
    return parser


def _scripted_agent(spec):
    if not spec.agent.script:
        raise ValueError("scripted scenario requires agent.script")
    return ScriptedAgent.from_file(spec.agent.script)


def _exit_code(state: RunState) -> int:
    if state.status is RunStatus.COMPLETED:
        return 0
    if state.status is RunStatus.NEEDS_REVIEW:
        return 2
    return 1


def _print_result(state: RunState) -> None:
    print(
        f"\nrun={state.run_id} status={state.status.value} "
        f"iterations={state.iteration} reason={state.stop_reason or '-'}"
    )


def _confirm_apply(preview: ApplyPreview, assume_yes: bool) -> bool:
    print(f"Apply {len(preview.changes)} file(s) to {preview.target_dir}:")
    for change in preview.changes:
        print(f"  {change.operation:<6} {change.path} ({change.size_bytes} bytes)")
    if assume_yes:
        return True
    try:
        return input("Continue? [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inspect":
            store = StateStore(args.runs_dir)
            state = store.load(args.run_id)
            for event in store.read_events(args.run_id):
                ConsoleTrace.render_saved(event)
            _print_result(state)
            return _exit_code(state)

        if args.command == "apply":
            store = StateStore(args.runs_dir)
            record = apply_run(
                store,
                args.run_id,
                args.target_dir,
                lambda preview: _confirm_apply(preview, args.yes),
            )
            print(f"application={record['application_id']} status={record['status']}")
            return 0 if record["status"] == "applied" else 1

        trace = ConsoleTrace(args.step)
        store = StateStore(args.runs_dir, trace)
        if args.command == "run":
            spec = load_run_spec(args.scenario)
            state = LoopEngine(spec, _scripted_agent(spec), store).start(args.run_id)
        else:
            manifest = store.load_manifest(args.run_id)
            scenario = args.scenario or Path(manifest["scenario"]["scenario_root"]) / "scenario.toml"
            spec = load_run_spec(scenario)
            approval = True if args.approve else False if args.reject else None
            state = LoopEngine(spec, _scripted_agent(spec), store).resume(
                args.run_id, approval
            )
        _print_result(state)
        return _exit_code(state)
    except (AgentLoopError, OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
