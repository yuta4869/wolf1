from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union


_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _looks_like_windows_drive(raw: str) -> bool:
    return bool(_WINDOWS_DRIVE_RE.match(raw))


def is_relative_to_path(
    child: Path,
    parent: Path,
    *,
    case_sensitive: bool = True,
) -> bool:
    c_parts = child.parts
    p_parts = parent.parts
    if len(c_parts) < len(p_parts):
        return False
    head = c_parts[: len(p_parts)]
    if case_sensitive:
        return head == p_parts
    return tuple(s.lower() for s in head) == tuple(s.lower() for s in p_parts)


@dataclass(frozen=True)
class ProjectBoundaryConfig:
    case_sensitive: bool = True
    require_project_root_exists: bool = True


@dataclass(frozen=True)
class ProjectBoundaryDecision:
    allowed: bool
    reason: str
    normalized_path: str
    project_root: str
    escaped: bool
    used_realpath: bool


PathLike = Union[Path, str, None]


class ProjectBoundaryGuard:
    def __init__(
        self,
        project_root: Union[Path, str],
        config: Optional[ProjectBoundaryConfig] = None,
    ) -> None:
        self.config: ProjectBoundaryConfig = (
            config if config is not None else ProjectBoundaryConfig()
        )
        if project_root is None:
            raise ValueError("project_root must not be None")
        root_str = str(project_root)
        if not root_str.strip():
            raise ValueError("project_root must not be empty")

        candidate = Path(root_str)
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            raise ValueError(
                f"project_root could not be resolved: {exc}"
            ) from exc

        if (
            self.config.require_project_root_exists
            and not resolved.exists()
        ):
            raise ValueError(
                f"project_root does not exist: {resolved}"
            )

        self.project_root: Path = resolved

    def check(self, path: PathLike) -> ProjectBoundaryDecision:
        if path is None:
            return self._invalid("path is None")
        raw = str(path)
        if not raw.strip():
            return self._invalid("empty or whitespace-only path")

        if _looks_like_windows_drive(raw) and os.sep == "/":
            return self._invalid(
                f"windows-style drive path rejected on POSIX: {raw!r}"
            )

        try:
            expanded = os.path.expanduser(raw)
        except Exception as exc:
            return self._invalid(f"expanduser failed: {exc}")

        if not os.path.isabs(expanded):
            expanded = os.path.join(str(self.project_root), expanded)

        try:
            lexical = Path(os.path.normpath(expanded))
        except Exception as exc:
            return self._invalid(f"normalization failed: {exc}")

        try:
            real = Path(os.path.realpath(expanded))
        except OSError as exc:
            return ProjectBoundaryDecision(
                allowed=False,
                reason=f"realpath failed: {exc} (fail-closed)",
                normalized_path=str(lexical),
                project_root=str(self.project_root),
                escaped=False,
                used_realpath=False,
            )

        lexical_inside = self._is_inside(lexical)
        real_inside = self._is_inside(real)
        used_realpath = real != lexical

        if lexical_inside and real_inside:
            return ProjectBoundaryDecision(
                allowed=True,
                reason="path is inside project_root",
                normalized_path=str(real if used_realpath else lexical),
                project_root=str(self.project_root),
                escaped=False,
                used_realpath=used_realpath,
            )

        if not lexical_inside and not real_inside:
            return ProjectBoundaryDecision(
                allowed=False,
                reason="normalized path is outside project_root",
                normalized_path=str(lexical),
                project_root=str(self.project_root),
                escaped=True,
                used_realpath=False,
            )

        if real_inside and not lexical_inside:
            return ProjectBoundaryDecision(
                allowed=True,
                reason=(
                    "path is inside project_root after realpath resolution"
                ),
                normalized_path=str(real),
                project_root=str(self.project_root),
                escaped=False,
                used_realpath=True,
            )

        return ProjectBoundaryDecision(
            allowed=False,
            reason="symlink resolves outside project_root",
            normalized_path=str(real),
            project_root=str(self.project_root),
            escaped=True,
            used_realpath=True,
        )

    def _is_inside(self, candidate: Path) -> bool:
        if candidate == self.project_root:
            return True
        return is_relative_to_path(
            candidate,
            self.project_root,
            case_sensitive=self.config.case_sensitive,
        )

    def _invalid(self, reason: str) -> ProjectBoundaryDecision:
        return ProjectBoundaryDecision(
            allowed=False,
            reason=f"{reason} (fail-closed)",
            normalized_path="",
            project_root=str(self.project_root),
            escaped=False,
            used_realpath=False,
        )
