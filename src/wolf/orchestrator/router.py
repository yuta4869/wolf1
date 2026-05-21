from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, FrozenSet, List, Mapping, Optional, Tuple, Union

from ..adapters.llm import LLMAdapter
from ..adapters.robot_transport import RobotState, RobotTransport
from ..core.audit import AuditLogger, utc_now_iso
from ..core.errors import AdapterError
from ..core.policy import PolicyEngine
from ..core.types import Action as PolicyAction
from ..core.types import AuditEvent, Decision, RiskLevel
from ..safety.project_boundary import ProjectBoundaryGuard
from ..safety.prompt_injection import (
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    TrustedInstruction,
    UntrustedText,
    quote_untrusted_for_prompt,
    scan_for_injection_markers,
)
from ..safety.robot_preflight import PreflightInput, RobotPreflight
from ..safety.sensitive_paths import SensitivePathGuard


class ActionKind(str, Enum):
    LLM_SUMMARIZE = "llm.summarize"
    LLM_SUMMARIZE_EMAIL = "llm.summarize_email"
    FILE_READ = "file.read"
    FILE_WRITE = "file.write"
    ROBOT_PREFLIGHT = "robot.preflight"
    ROBOT_MOTION_DRY_RUN = "robot.motion_dry_run"


STAGE_PROJECT_BOUNDARY = "project_boundary"
STAGE_SENSITIVE_PATH = "sensitive_path"
STAGE_POLICY = "policy"
STAGE_ROBOT_PREFLIGHT = "robot_preflight"
STAGE_PROMPT_INJECTION = "prompt_injection"
STAGE_PROVIDER = "provider"
STAGE_AUDIT = "audit"
STAGE_COMPLETE = "complete"


@dataclass(frozen=True)
class RouterConfig:
    allow_warning_injection_findings: bool = False
    approved_high_risk_actions: FrozenSet[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class RouterAction:
    kind: Union[ActionKind, str]
    risk_level: RiskLevel
    target_path: Optional[Union[Path, str]] = None
    body: Optional[Union[UntrustedText, TrustedInstruction, str]] = None
    robot_state: Optional[Union[RobotState, PreflightInput]] = None
    context: Mapping[str, Any] = field(default_factory=dict)
    confirmation_token: Optional[str] = None


@dataclass(frozen=True)
class RouterDecision:
    allowed: bool
    executed: bool
    requires_confirmation: bool
    reason: str
    stage: str
    audit_event_id: Optional[str]
    provider_called: bool
    result: Optional[Any]
    failed_checks: Tuple[str, ...]
    warnings: Tuple[str, ...]

    def __repr__(self) -> str:
        return (
            f"RouterDecision(allowed={self.allowed}, "
            f"executed={self.executed}, "
            f"requires_confirmation={self.requires_confirmation}, "
            f"stage={self.stage!r}, "
            f"provider_called={self.provider_called}, "
            f"audit_event_id={self.audit_event_id!r}, "
            f"failed_checks={self.failed_checks!r})"
        )


def _kind_str(kind: Union[ActionKind, str]) -> str:
    if isinstance(kind, ActionKind):
        return kind.value
    return str(kind)


class Router:
    def __init__(
        self,
        *,
        project_root: Union[Path, str],
        llm: LLMAdapter,
        robot_transport: RobotTransport,
        audit: AuditLogger,
        boundary: Optional[ProjectBoundaryGuard] = None,
        sensitive: Optional[SensitivePathGuard] = None,
        policy: Optional[PolicyEngine] = None,
        preflight: Optional[RobotPreflight] = None,
        config: Optional[RouterConfig] = None,
    ) -> None:
        if llm is None:
            raise ValueError("llm is required (no implicit default provider)")
        if robot_transport is None:
            raise ValueError(
                "robot_transport is required (no implicit default provider)"
            )
        if audit is None:
            raise ValueError("audit is required (no implicit default)")

        self.config: RouterConfig = (
            config if config is not None else RouterConfig()
        )
        self.project_root: Path = Path(project_root).resolve()
        self.llm = llm
        self.robot_transport = robot_transport
        self.audit = audit
        self.boundary: ProjectBoundaryGuard = (
            boundary
            if boundary is not None
            else ProjectBoundaryGuard(self.project_root)
        )
        self.sensitive: SensitivePathGuard = (
            sensitive
            if sensitive is not None
            else SensitivePathGuard(project_root=self.project_root)
        )
        self.policy: PolicyEngine = (
            policy
            if policy is not None
            else PolicyEngine(
                known_actions=frozenset(k.value for k in ActionKind),
                approved_high_risk=self.config.approved_high_risk_actions,
            )
        )
        self.preflight: RobotPreflight = (
            preflight if preflight is not None else RobotPreflight()
        )

    def route(self, action: RouterAction) -> RouterDecision:
        warnings: List[str] = []
        kind_str = _kind_str(action.kind)

        if action.target_path is not None:
            boundary_decision = self.boundary.check(action.target_path)
            if not boundary_decision.allowed:
                return self._finalize(
                    action,
                    allowed=False,
                    executed=False,
                    requires_confirmation=False,
                    reason=boundary_decision.reason,
                    stage=STAGE_PROJECT_BOUNDARY,
                    provider_called=False,
                    result=None,
                    failed_checks=[
                        f"project_boundary: {boundary_decision.reason}"
                    ],
                    warnings=warnings,
                )

            sensitive_decision = self.sensitive.check(action.target_path)
            if not sensitive_decision.allowed:
                return self._finalize(
                    action,
                    allowed=False,
                    executed=False,
                    requires_confirmation=False,
                    reason=sensitive_decision.reason,
                    stage=STAGE_SENSITIVE_PATH,
                    provider_called=False,
                    result=None,
                    failed_checks=[
                        f"sensitive_path: {sensitive_decision.reason}"
                    ],
                    warnings=warnings,
                )

        policy_action = PolicyAction(
            kind=kind_str,
            risk=action.risk_level,
            target=str(action.target_path)
            if action.target_path is not None
            else "",
            context=dict(action.context),
        )
        policy_decision = self.policy.evaluate(policy_action)
        if policy_decision is Decision.DENY:
            return self._finalize(
                action,
                allowed=False,
                executed=False,
                requires_confirmation=False,
                reason="policy: DENY",
                stage=STAGE_POLICY,
                provider_called=False,
                result=None,
                failed_checks=["policy: DENY"],
                warnings=warnings,
            )
        if policy_decision is Decision.REQUIRE_CONFIRMATION:
            return self._finalize(
                action,
                allowed=False,
                executed=False,
                requires_confirmation=True,
                reason="policy: REQUIRE_CONFIRMATION",
                stage=STAGE_POLICY,
                provider_called=False,
                result=None,
                failed_checks=["policy: REQUIRE_CONFIRMATION"],
                warnings=warnings,
            )

        if kind_str.startswith("robot."):
            preflight_result = self._evaluate_robot_preflight(action, warnings)
            if preflight_result is not None:
                return preflight_result

        quoted_body: Optional[str] = None
        if isinstance(action.body, UntrustedText):
            scan = scan_for_injection_markers(action.body)
            criticals = [
                f for f in scan.findings if f.severity == SEVERITY_CRITICAL
            ]
            warning_findings = [
                f for f in scan.findings if f.severity == SEVERITY_WARNING
            ]
            if criticals:
                return self._finalize(
                    action,
                    allowed=False,
                    executed=False,
                    requires_confirmation=False,
                    reason=(
                        f"{len(criticals)} critical injection marker(s) "
                        f"detected"
                    ),
                    stage=STAGE_PROMPT_INJECTION,
                    provider_called=False,
                    result=None,
                    failed_checks=[
                        f"injection_critical: {f.marker}" for f in criticals
                    ],
                    warnings=warnings,
                )
            if warning_findings:
                if self.config.allow_warning_injection_findings:
                    warnings.extend(
                        f"injection_warning: {f.marker}"
                        for f in warning_findings
                    )
                else:
                    return self._finalize(
                        action,
                        allowed=False,
                        executed=False,
                        requires_confirmation=False,
                        reason=(
                            f"{len(warning_findings)} warning injection "
                            f"marker(s) detected"
                        ),
                        stage=STAGE_PROMPT_INJECTION,
                        provider_called=False,
                        result=None,
                        failed_checks=[
                            f"injection_warning: {f.marker}"
                            for f in warning_findings
                        ],
                        warnings=warnings,
                    )
            quoted_body = quote_untrusted_for_prompt(action.body)

        provider_called = False
        executed = False
        result: Optional[Any] = None

        if kind_str in (
            ActionKind.LLM_SUMMARIZE.value,
            ActionKind.LLM_SUMMARIZE_EMAIL.value,
        ):
            text_for_llm: Optional[str] = None
            if quoted_body is not None:
                text_for_llm = quoted_body
            elif isinstance(action.body, TrustedInstruction):
                text_for_llm = action.body.as_instruction()
            elif isinstance(action.body, str):
                text_for_llm = action.body
            if text_for_llm is None:
                return self._finalize(
                    action,
                    allowed=False,
                    executed=False,
                    requires_confirmation=False,
                    reason="llm action missing body",
                    stage=STAGE_PROVIDER,
                    provider_called=False,
                    result=None,
                    failed_checks=["provider: missing body"],
                    warnings=warnings,
                )
            try:
                result = self.llm.summarize(text_for_llm)
            except AdapterError as exc:
                return self._finalize(
                    action,
                    allowed=False,
                    executed=False,
                    requires_confirmation=False,
                    reason=f"provider failed: {exc.label}",
                    stage=STAGE_PROVIDER,
                    provider_called=True,
                    result=None,
                    failed_checks=[f"provider: {exc.label}"],
                    warnings=warnings,
                )
            provider_called = True
            executed = True
        elif kind_str in (
            ActionKind.FILE_READ.value,
            ActionKind.FILE_WRITE.value,
        ):
            result = {
                "dry_run": True,
                "kind": kind_str,
                "target": str(action.target_path),
            }
            provider_called = False
            executed = False
        elif kind_str in (
            ActionKind.ROBOT_PREFLIGHT.value,
            ActionKind.ROBOT_MOTION_DRY_RUN.value,
        ):
            result = {
                "dry_run": True,
                "kind": kind_str,
                "note": "execute_motion not invoked in this PR",
            }
            provider_called = False
            executed = False
        else:
            return self._finalize(
                action,
                allowed=False,
                executed=False,
                requires_confirmation=False,
                reason=f"unsupported action kind {kind_str!r}",
                stage=STAGE_PROVIDER,
                provider_called=False,
                result=None,
                failed_checks=[
                    f"provider: unsupported kind {kind_str!r}"
                ],
                warnings=warnings,
            )

        return self._finalize(
            action,
            allowed=True,
            executed=executed,
            requires_confirmation=False,
            reason=(
                "action completed" if executed else "action allowed (dry-run)"
            ),
            stage=STAGE_COMPLETE,
            provider_called=provider_called,
            result=result,
            failed_checks=[],
            warnings=warnings,
        )

    def _evaluate_robot_preflight(
        self, action: RouterAction, warnings: List[str]
    ) -> Optional[RouterDecision]:
        if action.robot_state is None:
            return self._finalize(
                action,
                allowed=False,
                executed=False,
                requires_confirmation=False,
                reason="robot action missing robot_state",
                stage=STAGE_ROBOT_PREFLIGHT,
                provider_called=False,
                result=None,
                failed_checks=["robot_preflight: missing input"],
                warnings=warnings,
            )

        if isinstance(action.robot_state, PreflightInput):
            preflight_input = action.robot_state
        elif isinstance(action.robot_state, RobotState):
            env = action.context.get("environment_risk")
            env_map = env if isinstance(env, dict) else None
            preflight_input = PreflightInput.from_robot_state(
                action.robot_state,
                action_classification=action.risk_level,
                environment_risk=env_map,
            )
        else:
            return self._finalize(
                action,
                allowed=False,
                executed=False,
                requires_confirmation=False,
                reason=(
                    "unsupported robot_state type: "
                    f"{type(action.robot_state).__name__}"
                ),
                stage=STAGE_ROBOT_PREFLIGHT,
                provider_called=False,
                result=None,
                failed_checks=["robot_preflight: bad input type"],
                warnings=warnings,
            )

        pf = self.preflight.evaluate(preflight_input)
        if not pf.allowed:
            return self._finalize(
                action,
                allowed=False,
                executed=False,
                requires_confirmation=False,
                reason=pf.reason,
                stage=STAGE_ROBOT_PREFLIGHT,
                provider_called=False,
                result=None,
                failed_checks=list(pf.failed_checks),
                warnings=warnings,
            )
        if pf.requires_confirmation:
            return self._finalize(
                action,
                allowed=False,
                executed=False,
                requires_confirmation=True,
                reason=pf.reason or "robot_preflight: REQUIRE_CONFIRMATION",
                stage=STAGE_ROBOT_PREFLIGHT,
                provider_called=False,
                result=None,
                failed_checks=[],
                warnings=warnings,
            )
        return None

    def _finalize(
        self,
        action: RouterAction,
        *,
        allowed: bool,
        executed: bool,
        requires_confirmation: bool,
        reason: str,
        stage: str,
        provider_called: bool,
        result: Optional[Any],
        failed_checks: List[str],
        warnings: List[str],
    ) -> RouterDecision:
        event_id = str(uuid.uuid4())
        if allowed and executed:
            outcome = "success"
            audit_decision = "allow"
        elif allowed and not executed:
            outcome = "dry_run"
            audit_decision = "allow"
        elif requires_confirmation:
            outcome = "requires_confirmation"
            audit_decision = "require_confirmation"
        else:
            outcome = "denied"
            audit_decision = "deny"

        body_kind: Optional[str] = None
        body_len: Optional[int] = None
        if isinstance(action.body, UntrustedText):
            body_kind = action.body.source_kind.value
            body_len = len(action.body)
        elif isinstance(action.body, TrustedInstruction):
            body_kind = "trusted_instruction"
            body_len = len(action.body.text)
        elif isinstance(action.body, str):
            body_kind = "raw_str"
            body_len = len(action.body)

        event = AuditEvent(
            ts=utc_now_iso(),
            actor="router",
            action_kind=_kind_str(action.kind),
            decision=audit_decision,
            target=str(action.target_path)
            if action.target_path is not None
            else "",
            outcome=outcome,
            detail={
                "stage": stage,
                "audit_event_id": event_id,
                "reason": reason,
                "failed_checks": list(failed_checks),
                "warnings": list(warnings),
                "body_source": body_kind,
                "body_length": body_len,
                "confirmation_token_present": bool(action.confirmation_token),
                "provider_called": provider_called,
                "executed": executed,
                "result_type": type(result).__name__
                if result is not None
                else None,
            },
        )
        try:
            self.audit.log(event)
        except Exception as exc:
            return RouterDecision(
                allowed=False,
                executed=executed,
                requires_confirmation=False,
                reason=f"audit log write failed: {exc} (fail-closed)",
                stage=STAGE_AUDIT,
                audit_event_id=None,
                provider_called=provider_called,
                result=None,
                failed_checks=tuple(
                    list(failed_checks) + ["audit: write failed"]
                ),
                warnings=tuple(warnings),
            )

        return RouterDecision(
            allowed=allowed,
            executed=executed,
            requires_confirmation=requires_confirmation,
            reason=reason,
            stage=stage,
            audit_event_id=event_id,
            provider_called=provider_called,
            result=result,
            failed_checks=tuple(failed_checks),
            warnings=tuple(warnings),
        )
