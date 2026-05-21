from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence

from .adapters.llm import LLMAdapter
from .core.audit import AuditLogger
from .core.types import RiskLevel
from .fakes.llm import FakeLLM
from .fakes.robot import FakeRobotTransport
from .orchestrator.router import (
    ActionKind,
    Router,
    RouterAction,
    RouterDecision,
)
from .safety.prompt_injection import SourceKind, wrap_untrusted


DEFAULT_AUDIT_FILE = "audit.jsonl"

EXIT_SUCCESS = 0
EXIT_DENIED = 2
EXIT_INTERNAL_ERROR = 1


def _default_audit_path(project_root: Path) -> Path:
    return project_root / "var" / "audit" / DEFAULT_AUDIT_FILE


def _build_router(
    project_root: Path,
    *,
    audit_path: Optional[Path] = None,
    llm: Optional[LLMAdapter] = None,
) -> Router:
    root = project_root.resolve()
    audit_target = (
        audit_path
        if audit_path is not None
        else _default_audit_path(root)
    )
    audit = AuditLogger(audit_target)
    llm_adapter: LLMAdapter = llm if llm is not None else FakeLLM()
    robot = FakeRobotTransport()
    return Router(
        project_root=root,
        llm=llm_adapter,
        robot_transport=robot,
        audit=audit,
    )


def _build_llm_from_args(args: argparse.Namespace) -> LLMAdapter:
    backend = getattr(args, "backend", "fake")
    if backend == "fake":
        return FakeLLM()
    if backend == "ollama":
        # Lazy import so importing the CLI does not pull urllib bindings
        # unless an Ollama backend is actually requested. Also keeps
        # `python -m wolf.cli summarize-email --backend fake ...` working
        # even if a future ollama.py refactor breaks something.
        from .adapters.ollama import (
            DEFAULT_BASE_URL,
            OllamaLLMAdapter,
        )

        model = getattr(args, "model", None)
        if not model:
            raise ValueError(
                "--backend ollama requires --model (e.g. --model llama3.1)"
            )
        base_url = getattr(args, "ollama_url", None) or DEFAULT_BASE_URL
        allow_non_localhost = bool(
            getattr(args, "allow_non_localhost_ollama", False)
        )
        return OllamaLLMAdapter(
            model=model,
            base_url=base_url,
            allow_non_localhost=allow_non_localhost,
        )
    raise ValueError(f"unsupported backend: {backend!r}")


def _decision_to_safe_dict(
    decision: RouterDecision,
    *,
    include_result: bool = False,
) -> Mapping[str, Any]:
    safe: dict = {
        "allowed": decision.allowed,
        "executed": decision.executed,
        "requires_confirmation": decision.requires_confirmation,
        "stage": decision.stage,
        "reason": decision.reason,
        "provider_called": decision.provider_called,
        "audit_event_id": decision.audit_event_id,
        "failed_checks": list(decision.failed_checks),
        "warnings": list(decision.warnings),
    }
    if include_result and decision.result is not None:
        if isinstance(decision.result, str):
            safe["result"] = decision.result
        elif isinstance(decision.result, (dict, list, int, float, bool)):
            safe["result"] = decision.result
        else:
            safe["result"] = {"type": type(decision.result).__name__}
    return safe


def _exit_code_for(decision: RouterDecision) -> int:
    if decision.allowed and not decision.requires_confirmation:
        return EXIT_SUCCESS
    return EXIT_DENIED


def _print_json(payload: Mapping[str, Any], stream=None) -> None:
    if stream is None:
        stream = sys.stdout
    stream.write(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    )
    stream.write("\n")


def _healthy_environment() -> Mapping[str, bool]:
    return {
        "near_people": False,
        "near_animals": False,
        "near_fragile": False,
        "near_water": False,
        "near_fire": False,
        "near_stairs": False,
        "near_chemicals": False,
        "unstable_floor": False,
        "unknown_obstacle": False,
    }


def cmd_summarize_email(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    try:
        llm = _build_llm_from_args(args)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED
    router = _build_router(project_root, llm=llm)
    body = wrap_untrusted(args.text, SourceKind.EMAIL)
    action = RouterAction(
        kind=ActionKind.LLM_SUMMARIZE_EMAIL,
        risk_level=RiskLevel.LOW,
        body=body,
    )
    decision = router.route(action)
    payload = _decision_to_safe_dict(decision, include_result=True)
    _print_json(payload)
    return _exit_code_for(decision)


def cmd_check_path(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    router = _build_router(project_root)
    action = RouterAction(
        kind=ActionKind.FILE_READ,
        risk_level=RiskLevel.LOW,
        target_path=args.path,
    )
    decision = router.route(action)
    payload = _decision_to_safe_dict(decision, include_result=False)
    _print_json(payload)
    return _exit_code_for(decision)


def cmd_robot_preflight(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    router = _build_router(project_root)
    robot = FakeRobotTransport()
    action = RouterAction(
        kind=ActionKind.ROBOT_PREFLIGHT,
        risk_level=RiskLevel.LOW,
        robot_state=robot.get_state(),
        context={"environment_risk": _healthy_environment()},
    )
    decision = router.route(action)
    payload = _decision_to_safe_dict(decision, include_result=True)
    _print_json(payload)
    return _exit_code_for(decision)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wolf",
        description=(
            "Multi-task Partner AI CLI smoke. Fake providers only. "
            "No external network calls. Robot execute_motion is never called."
        ),
    )
    parser.add_argument(
        "--project-root",
        default=str(Path.cwd()),
        help="Project root for boundary / sensitivity / audit (default: cwd)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    se = sub.add_parser(
        "summarize-email",
        help=(
            "Wrap text as UntrustedText and route through the selected "
            "LLM backend (default: FakeLLM)"
        ),
    )
    se.add_argument("--text", required=True, help="Email body text")
    se.add_argument(
        "--backend",
        choices=("fake", "ollama"),
        default="fake",
        help=(
            "LLM backend to use. 'fake' uses the in-process FakeLLM. "
            "'ollama' connects to a locally-running Ollama server."
        ),
    )
    se.add_argument(
        "--model",
        default=None,
        help="Ollama model name (required when --backend ollama)",
    )
    se.add_argument(
        "--ollama-url",
        default=None,
        help=(
            "Ollama server URL (default: http://127.0.0.1:11434). "
            "Must be localhost unless --allow-non-localhost-ollama is set."
        ),
    )
    se.add_argument(
        "--allow-non-localhost-ollama",
        action="store_true",
        help=(
            "Explicitly permit a non-localhost --ollama-url. Default is "
            "to refuse external URLs to prevent accidental cloud calls."
        ),
    )
    se.set_defaults(func=cmd_summarize_email)

    cp = sub.add_parser(
        "check-path",
        help="Run ProjectBoundary + SensitivePath checks on a path",
    )
    cp.add_argument(
        "--path", required=True, help="Path to evaluate (relative or absolute)"
    )
    cp.set_defaults(func=cmd_check_path)

    rp = sub.add_parser(
        "robot-preflight",
        help=(
            "Run RobotPreflight against FakeRobotTransport healthy state "
            "(dry-run, never invokes execute_motion)"
        ),
    )
    rp.set_defaults(func=cmd_robot_preflight)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception as exc:
        sys.stderr.write(
            f"wolf cli: internal error ({type(exc).__name__}): {exc}\n"
        )
        return EXIT_INTERNAL_ERROR


if __name__ == "__main__":
    sys.exit(main())
