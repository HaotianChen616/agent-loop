"""命令行触发器，以及面向教学的事件轨迹渲染器。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .agent import ScriptedAgent
from .application import ApplyPreview, apply_run
from .config import load_run_spec
from .engine import LoopEngine
from .maas_agent import MaaSAgent
from .providers import PROVIDER_NAMES, create_provider
from .storage import StateStore
from .types import AgentLoopError, LoopEvent, RunState, RunStatus


class ConsoleTrace:
    """把 LoopEvent 渲染成适合课堂演示的单行轨迹。"""

    def __init__(self, step: bool = False) -> None:
        """`step=True` 时每条事件后等待回车，便于逐步讲解。"""

        self.step = step

    def __call__(self, event: LoopEvent) -> None:
        """作为 StateStore 事件监听器，实时打印刚持久化的事件。"""

        label = event.event_type.upper().replace("_", " ")
        print(f"[{label:<24}] {event.summary}")
        if self.step:
            input("Press Enter for the next event...")

    @staticmethod
    def render_saved(event: dict) -> None:
        """渲染从 events.jsonl 读取的普通字典，用于 inspect。"""

        label = str(event["event_type"]).upper().replace("_", " ")
        print(f"[{label:<24}] {event['summary']}")


def _parser() -> argparse.ArgumentParser:
    """声明 run、resume、inspect 和 apply 四个稳定 CLI 入口。"""

    parser = argparse.ArgumentParser(prog="agent-loop", description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    run = subcommands.add_parser("run", help="start a scenario")
    run.add_argument("scenario", type=Path)
    run.add_argument("--run-id")
    run.add_argument("--runs-dir", type=Path, default=Path(".agent-loop/runs"))
    run.add_argument("--agent", choices=("scripted", "llm"))
    run.add_argument("--provider", choices=PROVIDER_NAMES)
    run.add_argument("--model")
    run.add_argument("--step", action="store_true")

    resume = subcommands.add_parser("resume", help="resume a persisted run")
    resume.add_argument("run_id")
    resume.add_argument("--scenario", type=Path)
    resume.add_argument("--runs-dir", type=Path, default=Path(".agent-loop/runs"))
    resume.add_argument("--agent", choices=("scripted", "llm"))
    resume.add_argument("--provider", choices=PROVIDER_NAMES)
    resume.add_argument("--model")
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


def _agent(
    spec,
    kind: str | None = None,
    model: str | None = None,
    provider: str | None = None,
):
    """根据 Scenario 与命令行覆盖项创建唯一的 Agent 适配器。"""

    selected = kind or spec.agent.kind
    if selected == "scripted":
        if model:
            raise ValueError("--model can only be used with --agent llm")
        if provider:
            raise ValueError("--provider can only be used with --agent llm")
        if not spec.agent.script:
            raise ValueError("scripted scenario requires agent.script")
        return ScriptedAgent.from_file(spec.agent.script)
    if selected == "llm":
        selected_provider = provider or spec.agent.provider or "openai"
        return MaaSAgent(
            create_provider(
                selected_provider,
                model or spec.agent.model or "",
                spec.agent.request_timeout_seconds,
                spec.agent.max_output_tokens,
            )
        )
    raise ValueError(f"unknown agent kind: {selected}")


def _exit_code(state: RunState) -> int:
    """把 Run 终态映射为 CLI 退出码：成功 0、待复核 2、其他 1。"""

    if state.status is RunStatus.COMPLETED:
        return 0
    if state.status is RunStatus.NEEDS_REVIEW:
        return 2
    return 1


def _print_result(state: RunState) -> None:
    """打印便于脚本和人阅读的最终运行摘要。"""

    print(
        f"\nrun={state.run_id} status={state.status.value} "
        f"iterations={state.iteration} reason={state.stop_reason or '-'}"
    )


def _confirm_apply(preview: ApplyPreview, assume_yes: bool) -> bool:
    """先展示逐文件 Apply 预览，再取得显式确认；EOF 按拒绝处理。"""

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
    """分发 CLI 子命令，并把预期错误收敛为稳定退出码。

    `inspect/apply` 操作既有 Run；`run/resume` 才加载 Scenario、构造 Agent 和
    LoopEngine。这里只负责组装，不包含状态迁移或工具执行规则。
    """

    args = _parser().parse_args(argv)
    try:
        # inspect 和 apply 只读取既有 Run，不需要重新创建 Agent。
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

        # run/resume 共享同一事件监听器，因此现场输出与持久化事件保持一致。
        trace = ConsoleTrace(args.step)
        store = StateStore(args.runs_dir, trace)
        if args.command == "run":
            spec = load_run_spec(args.scenario)
            state = LoopEngine(
                spec, _agent(spec, args.agent, args.model, args.provider), store
            ).start(args.run_id)
        else:
            manifest = store.load_manifest(args.run_id)
            saved_scenario = manifest["scenario"]
            scenario = args.scenario or Path(
                saved_scenario.get("scenario_file")
                or Path(saved_scenario["scenario_root"]) / "scenario.toml"
            )
            spec = load_run_spec(scenario)
            approval = True if args.approve else False if args.reject else None
            runtime = manifest.get("runtime", {})
            state = LoopEngine(
                spec,
                _agent(
                    spec,
                    args.agent or runtime.get("agent"),
                    args.model or runtime.get("model"),
                    args.provider or runtime.get("provider"),
                ),
                store,
            ).resume(
                args.run_id, approval
            )
        _print_result(state)
        return _exit_code(state)
    except (AgentLoopError, OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
