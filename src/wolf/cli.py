from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence

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


def _build_router(project_root: Path, audit_path: Optional[Path] = None) -> Router:
    root = project_root.resolve()
    audit_target = (
        audit_path
        if audit_path is not None
        else _default_audit_path(root)
    )
    audit = AuditLogger(audit_target)
    llm = FakeLLM()
    robot = FakeRobotTransport()
    return Router(
        project_root=root,
        llm=llm,
        robot_transport=robot,
        audit=audit,
    )


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
    router = _build_router(project_root)
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
        help="Wrap text as UntrustedText and route through FakeLLM",
    )
    se.add_argument("--text", required=True, help="Email body text")
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
