from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Optional, Tuple, Union


PROJECT_ANCHOR = "project"
HOME_ANCHOR = "home"

KIND_GLOB_AT_ROOT = "glob_at_root"
KIND_SUBTREE = "subtree"


@dataclass(frozen=True)
class PathRule:
    pattern: str
    kind: str
    anchor: str

    def describe(self) -> str:
        prefix = "~/" if self.anchor == HOME_ANCHOR else "./"
        return f"{prefix}{self.pattern}"


DEFAULT_RULES: Tuple[PathRule, ...] = (
    PathRule(pattern=".env", kind=KIND_GLOB_AT_ROOT, anchor=PROJECT_ANCHOR),
    PathRule(pattern=".env.*", kind=KIND_GLOB_AT_ROOT, anchor=PROJECT_ANCHOR),
    PathRule(pattern="secrets", kind=KIND_SUBTREE, anchor=PROJECT_ANCHOR),
    PathRule(pattern="credentials", kind=KIND_SUBTREE, anchor=PROJECT_ANCHOR),
    PathRule(pattern="tokens", kind=KIND_SUBTREE, anchor=PROJECT_ANCHOR),
    PathRule(pattern="private", kind=KIND_SUBTREE, anchor=PROJECT_ANCHOR),
    PathRule(pattern=".ssh", kind=KIND_SUBTREE, anchor=HOME_ANCHOR),
    PathRule(pattern=".aws", kind=KIND_SUBTREE, anchor=HOME_ANCHOR),
    PathRule(pattern=".config/gcloud", kind=KIND_SUBTREE, anchor=HOME_ANCHOR),
)


@dataclass(frozen=True)
class PathDecision:
    allowed: bool
    reason: str
    matched_rule: Optional[str]
    normalized_path: Optional[str]


PathLike = Union[str, Path, None]


class SensitivePathGuard:
    def __init__(
        self,
        *,
        project_root: Path,
        home: Optional[Path] = None,
        rules: Optional[Tuple[PathRule, ...]] = None,
        case_sensitive: bool = False,
    ) -> None:
        self.project_root: Path = Path(project_root).resolve()
        self.home: Path = (
            Path(home).resolve() if home is not None else Path.home().resolve()
        )
        self.rules: Tuple[PathRule, ...] = (
            rules if rules is not None else DEFAULT_RULES
        )
        self.case_sensitive: bool = case_sensitive

    def check(self, path: PathLike) -> PathDecision:
        if path is None:
            return self._fail_closed("path is None")
        raw = str(path)
        if not raw.strip():
            return self._fail_closed("empty path")

        try:
            expanded = self._expand_user(raw)
        except Exception as exc:
            return self._fail_closed(f"expanduser failed: {exc}")

        if not os.path.isabs(expanded):
            expanded = os.path.join(str(self.project_root), expanded)

        try:
            pure = Path(os.path.normpath(expanded))
        except Exception as exc:
            return self._fail_closed(f"normpath failed: {exc}")

        matched_pure = self._first_match(pure)
        if matched_pure is not None:
            return PathDecision(
                allowed=False,
                reason=f"matched {matched_pure.describe()} (pure path)",
                matched_rule=matched_pure.describe(),
                normalized_path=str(pure),
            )

        try:
            real = Path(os.path.realpath(expanded))
        except OSError as exc:
            return PathDecision(
                allowed=False,
                reason=f"realpath failed: {exc} (fail-closed)",
                matched_rule=None,
                normalized_path=str(pure),
            )

        if real != pure:
            matched_real = self._first_match(real)
            if matched_real is not None:
                return PathDecision(
                    allowed=False,
                    reason=f"matched {matched_real.describe()} via symlink",
                    matched_rule=matched_real.describe(),
                    normalized_path=str(real),
                )

        return PathDecision(
            allowed=True,
            reason="no rule matched",
            matched_rule=None,
            normalized_path=str(pure),
        )

    def _expand_user(self, raw: str) -> str:
        if raw == "~":
            return str(self.home)
        if raw.startswith("~/"):
            return str(self.home / raw[2:])
        return raw

    def _fail_closed(self, reason: str) -> PathDecision:
        return PathDecision(
            allowed=False,
            reason=f"{reason} (fail-closed)",
            matched_rule=None,
            normalized_path=None,
        )

    def _first_match(self, candidate: Path) -> Optional[PathRule]:
        for rule in self.rules:
            anchor = (
                self.project_root if rule.anchor == PROJECT_ANCHOR else self.home
            )
            try:
                rel = candidate.relative_to(anchor)
            except ValueError:
                continue
            if self._matches(rule, rel):
                return rule
        return None

    def _matches(self, rule: PathRule, rel: PurePath) -> bool:
        rel_parts = rel.parts
        if rule.kind == KIND_GLOB_AT_ROOT:
            if len(rel_parts) != 1:
                return False
            name = rel_parts[0]
            if self.case_sensitive:
                return fnmatch.fnmatchcase(name, rule.pattern)
            return fnmatch.fnmatchcase(name.lower(), rule.pattern.lower())
        if rule.kind == KIND_SUBTREE:
            pat_parts = PurePath(rule.pattern).parts
            if len(rel_parts) < len(pat_parts):
                return False
            head = rel_parts[: len(pat_parts)]
            if self.case_sensitive:
                return head == pat_parts
            return tuple(p.lower() for p in head) == tuple(
                p.lower() for p in pat_parts
            )
        return False
