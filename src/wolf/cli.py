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
from .files.chunking import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_MAX_CHUNKS,
    split_text,
)
from .files.read_text import (
    DEFAULT_MAX_BYTES as FILE_DEFAULT_MAX_BYTES,
    FileReadError,
    read_text_file,
)
from .orchestrator.router import (
    ActionKind,
    Router,
    RouterAction,
    RouterConfig,
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
    config: Optional[RouterConfig] = None,
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
        config=config,
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


def _emit_file_read_failure(
    *,
    label: str,
    gate_decision: RouterDecision,
    output_mode: str,
) -> int:
    payload = {
        "allowed": False,
        "executed": False,
        "requires_confirmation": False,
        "stage": "file_read",
        "reason": f"file_read failed: {label}",
        "provider_called": False,
        "audit_event_id": gate_decision.audit_event_id,
        "failed_checks": [f"file_read: {label}"],
        "warnings": [],
    }
    if output_mode == "text":
        sys.stderr.write(f"wolf cli: file_read failed: {label}\n")
    else:
        _print_json(payload)
    return EXIT_DENIED


def _emit_decision(
    decision: RouterDecision,
    *,
    output_mode: str,
    include_result: bool,
) -> int:
    if output_mode == "text":
        if decision.allowed and decision.executed and isinstance(
            decision.result, str
        ):
            sys.stdout.write(decision.result)
            if not decision.result.endswith("\n"):
                sys.stdout.write("\n")
            if decision.warnings:
                sys.stderr.write(
                    f"wolf cli: {len(decision.warnings)} warning(s) "
                    f"during summarize (see --output json for details)\n"
                )
            return EXIT_SUCCESS
        sys.stderr.write(
            f"wolf cli: stage={decision.stage} "
            f"reason={decision.reason}\n"
        )
        return _exit_code_for(decision)
    # Default: json
    payload = _decision_to_safe_dict(decision, include_result=include_result)
    _print_json(payload)
    return _exit_code_for(decision)


def cmd_summarize_file(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output_mode = getattr(args, "output", "json")
    strict = bool(getattr(args, "strict_prompt_injection", False))

    # Step 1: build the LLM backend up front so a misconfigured Ollama
    # backend (missing --model, external URL without --allow-non-localhost)
    # fails fast before any filesystem activity.
    try:
        llm = _build_llm_from_args(args)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED

    # For local files, the default policy is to surface warning-level
    # injection markers as warnings rather than blocking — most project
    # documents mention words like "robot" or "send email" without being
    # actual prompt injections. Critical markers ("ignore previous
    # instructions" etc.) still block. --strict-prompt-injection flips
    # this back to PR #14 behavior (warnings block too).
    router_config = RouterConfig(
        allow_warning_injection_findings=not strict
    )
    router = _build_router(project_root, llm=llm, config=router_config)

    # Step 2: gate the path through ProjectBoundary + SensitivePath via a
    # FILE_READ action. This emits an audit event for the path-check
    # outcome regardless of whether we go on to read the file.
    gate_action = RouterAction(
        kind=ActionKind.FILE_READ,
        risk_level=RiskLevel.LOW,
        target_path=args.path,
    )
    gate_decision = router.route(gate_action)
    if not gate_decision.allowed:
        return _emit_decision(
            gate_decision,
            output_mode=output_mode,
            include_result=False,
        )

    # Step 3: read the file safely. Failures are reported as a JSON
    # RouterDecision-shaped payload so the schema stays stable; we do not
    # echo the raw file content anywhere.
    target_path = Path(args.path)
    if not target_path.is_absolute():
        target_path = (project_root / target_path).resolve()
    max_bytes = int(getattr(args, "max_bytes", FILE_DEFAULT_MAX_BYTES))
    try:
        read_result = read_text_file(target_path, max_bytes=max_bytes)
    except FileReadError as exc:
        return _emit_file_read_failure(
            label=exc.label,
            gate_decision=gate_decision,
            output_mode=output_mode,
        )

    # Step 4: wrap the body as UntrustedText (source = local_document) and
    # route through the LLM_SUMMARIZE pipeline. The Router runs the
    # prompt-injection scan and quote-for-prompt step before the adapter.
    # Step 4: optionally split the body into chunks and summarize each,
    # then summarize the joined chunk summaries. The Router still runs
    # the prompt-injection scan and quote step on every UntrustedText.
    no_chunk = bool(getattr(args, "no_chunk", False))
    chunk_size = int(getattr(args, "chunk_size", DEFAULT_CHUNK_SIZE))
    max_chunks = int(getattr(args, "max_chunks", DEFAULT_MAX_CHUNKS))

    text_for_summary: Optional[str] = None
    chunk_warnings: List[str] = []

    if no_chunk or read_result.byte_size <= chunk_size:
        text_for_summary = read_result.text
    else:
        split = split_text(
            read_result.text,
            chunk_size=chunk_size,
            max_chunks=max_chunks,
        )
        if split.truncated:
            chunk_warnings.append(
                f"chunking: truncated after {len(split.chunks)} chunks "
                f"(file > chunk_size * max_chunks)"
            )
        per_chunk_summaries: List[str] = []
        for idx, chunk in enumerate(split.chunks):
            decision = _summarize_chunk_via_router(
                router=router,
                text=chunk,
                source_ref=f"{target_path}:chunk-{idx + 1}",
                byte_size=len(chunk.encode("utf-8")),
                encoding=read_result.encoding,
            )
            if not decision.allowed or not isinstance(decision.result, str):
                return _emit_decision(
                    decision,
                    output_mode=output_mode,
                    include_result=False,
                )
            per_chunk_summaries.append(decision.result)
            chunk_warnings.extend(decision.warnings)
        text_for_summary = "\n\n".join(
            f"--- chunk {i + 1} of {len(per_chunk_summaries)} ---\n{s}"
            for i, s in enumerate(per_chunk_summaries)
        )

    body = wrap_untrusted(
        text_for_summary,
        SourceKind.LOCAL_DOCUMENT,
        source_ref=str(target_path),
        metadata={
            "byte_size": str(read_result.byte_size),
            "encoding": read_result.encoding,
        },
    )
    summarize_action = RouterAction(
        kind=ActionKind.LLM_SUMMARIZE,
        risk_level=RiskLevel.LOW,
        body=body,
    )
    decision = router.route(summarize_action)
    if chunk_warnings:
        decision = _decision_with_extra_warnings(decision, chunk_warnings)
    return _emit_decision(
        decision,
        output_mode=output_mode,
        include_result=True,
    )


def _summarize_chunk_via_router(
    *,
    router: Router,
    text: str,
    source_ref: str,
    byte_size: int,
    encoding: str,
) -> RouterDecision:
    body = wrap_untrusted(
        text,
        SourceKind.LOCAL_DOCUMENT,
        source_ref=source_ref,
        metadata={"byte_size": str(byte_size), "encoding": encoding},
    )
    action = RouterAction(
        kind=ActionKind.LLM_SUMMARIZE,
        risk_level=RiskLevel.LOW,
        body=body,
    )
    return router.route(action)


def _decision_with_extra_warnings(
    decision: RouterDecision, extra: Sequence[str]
) -> RouterDecision:
    if not extra:
        return decision
    return RouterDecision(
        allowed=decision.allowed,
        executed=decision.executed,
        requires_confirmation=decision.requires_confirmation,
        reason=decision.reason,
        stage=decision.stage,
        audit_event_id=decision.audit_event_id,
        provider_called=decision.provider_called,
        result=decision.result,
        failed_checks=decision.failed_checks,
        warnings=tuple(list(decision.warnings) + list(extra)),
    )


DEFAULT_DIR_INCLUDE_EXTS = (".txt", ".md", ".rst", ".py")
DEFAULT_DIR_MAX_FILES = 50
DEFAULT_DIR_MAX_TOTAL_BYTES = 2 * 1024 * 1024  # 2 MiB


def _fnmatch_any(name: str, patterns: Sequence[str]) -> bool:
    import fnmatch

    return any(fnmatch.fnmatch(name, p) for p in patterns)


def _iter_candidate_files(
    *,
    root: Path,
    recursive: bool,
    include: Sequence[str],
    exclude: Sequence[str],
) -> List[Path]:
    out: List[Path] = []
    if not root.exists() or not root.is_dir():
        return out
    if recursive:
        walker = root.rglob("*")
    else:
        walker = root.glob("*")
    for p in walker:
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        if include and not _fnmatch_any(rel, include) and not _fnmatch_any(
            p.name, include
        ):
            continue
        if exclude and (
            _fnmatch_any(rel, exclude) or _fnmatch_any(p.name, exclude)
        ):
            continue
        out.append(p)
    out.sort()
    return out


def cmd_summarize_dir(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output_mode = getattr(args, "output", "json")
    strict = bool(getattr(args, "strict_prompt_injection", False))

    try:
        llm = _build_llm_from_args(args)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED

    router = _build_router(
        project_root,
        llm=llm,
        config=RouterConfig(allow_warning_injection_findings=not strict),
    )

    # Step 1: gate the directory path. We route a FILE_READ action so
    # ProjectBoundary + SensitivePath run; if the directory itself is
    # outside or sensitive, deny immediately.
    gate = router.route(
        RouterAction(
            kind=ActionKind.FILE_READ,
            risk_level=RiskLevel.LOW,
            target_path=args.path,
        )
    )
    if not gate.allowed:
        return _emit_decision(gate, output_mode=output_mode, include_result=False)

    # Step 2: resolve and walk the directory.
    target_dir = Path(args.path)
    if not target_dir.is_absolute():
        target_dir = (project_root / target_dir).resolve()
    if not target_dir.is_dir():
        payload = {
            "allowed": False,
            "executed": False,
            "requires_confirmation": False,
            "stage": "file_read",
            "reason": f"file_read failed: not a directory",
            "provider_called": False,
            "audit_event_id": gate.audit_event_id,
            "failed_checks": ["file_read: not a directory"],
            "warnings": [],
        }
        if output_mode == "text":
            sys.stderr.write("wolf cli: file_read failed: not a directory\n")
        else:
            _print_json(payload)
        return EXIT_DENIED

    recursive = not bool(getattr(args, "no_recursive", False))
    include = tuple(getattr(args, "include", None) or [])
    if not include:
        include = tuple(f"*{ext}" for ext in DEFAULT_DIR_INCLUDE_EXTS)
    exclude = tuple(getattr(args, "exclude", None) or [])
    max_files = int(getattr(args, "max_files", DEFAULT_DIR_MAX_FILES))
    max_total_bytes = int(
        getattr(args, "max_total_bytes", DEFAULT_DIR_MAX_TOTAL_BYTES)
    )
    max_bytes_per_file = int(
        getattr(args, "max_bytes", FILE_DEFAULT_MAX_BYTES)
    )

    candidates = _iter_candidate_files(
        root=target_dir,
        recursive=recursive,
        include=include,
        exclude=exclude,
    )

    # Step 3: per-file summarize. Each file goes through the Router's
    # full pipeline (boundary + sensitive + injection scan + provider).
    per_file_summaries: List[str] = []
    warnings: List[str] = []
    accepted_count = 0
    bytes_seen = 0

    for candidate in candidates:
        if accepted_count >= max_files:
            warnings.append(
                f"dir: stopped at max_files={max_files}; remaining files skipped"
            )
            break
        rel = candidate.relative_to(project_root)
        # Per-file boundary + sensitive check via Router.
        file_gate = router.route(
            RouterAction(
                kind=ActionKind.FILE_READ,
                risk_level=RiskLevel.LOW,
                target_path=str(candidate),
            )
        )
        if not file_gate.allowed:
            warnings.append(
                f"dir: skipped {rel} (stage={file_gate.stage})"
            )
            continue
        try:
            read_result = read_text_file(
                candidate, max_bytes=max_bytes_per_file
            )
        except FileReadError as exc:
            warnings.append(f"dir: skipped {rel} (file_read: {exc.label})")
            continue
        if bytes_seen + read_result.byte_size > max_total_bytes:
            warnings.append(
                f"dir: skipped {rel} (max_total_bytes={max_total_bytes} reached)"
            )
            continue
        # Summarize the file content (no chunking inside the dir walker;
        # callers pick smaller files or use summarize-file for per-file
        # chunking).
        decision = _summarize_chunk_via_router(
            router=router,
            text=read_result.text,
            source_ref=str(candidate),
            byte_size=read_result.byte_size,
            encoding=read_result.encoding,
        )
        if not decision.allowed or not isinstance(decision.result, str):
            warnings.append(
                f"dir: skipped {rel} (stage={decision.stage})"
            )
            continue
        per_file_summaries.append(f"[{rel}]\n{decision.result}")
        warnings.extend(decision.warnings)
        bytes_seen += read_result.byte_size
        accepted_count += 1

    if accepted_count == 0:
        payload = {
            "allowed": False,
            "executed": False,
            "requires_confirmation": False,
            "stage": "file_read",
            "reason": "no eligible files found",
            "provider_called": False,
            "audit_event_id": gate.audit_event_id,
            "failed_checks": ["dir: no eligible files"],
            "warnings": warnings,
        }
        if output_mode == "text":
            sys.stderr.write("wolf cli: no eligible files found\n")
        else:
            _print_json(payload)
        return EXIT_DENIED

    # Step 4: aggregate per-file summaries into a final summary via the
    # Router so the Ollama/Fake backend produces a single consolidated
    # output and the audit log records one final action.
    aggregated_text = "\n\n".join(per_file_summaries)
    body = wrap_untrusted(
        aggregated_text,
        SourceKind.LOCAL_DOCUMENT,
        source_ref=f"dir:{target_dir}",
        metadata={
            "files": str(accepted_count),
            "bytes_total": str(bytes_seen),
        },
    )
    final = router.route(
        RouterAction(
            kind=ActionKind.LLM_SUMMARIZE,
            risk_level=RiskLevel.LOW,
            body=body,
        )
    )
    if warnings:
        final = _decision_with_extra_warnings(final, warnings)
    return _emit_decision(final, output_mode=output_mode, include_result=True)


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

    sf = sub.add_parser(
        "summarize-file",
        help=(
            "Read a project-local text file (boundary + sensitive checked) "
            "and summarize via the selected LLM backend"
        ),
    )
    sf.add_argument(
        "--path",
        required=True,
        help="File path to read (relative paths resolve against --project-root)",
    )
    sf.add_argument(
        "--backend",
        choices=("fake", "ollama"),
        default="fake",
        help="LLM backend to use (default: fake)",
    )
    sf.add_argument(
        "--model",
        default=None,
        help="Ollama model name (required when --backend ollama)",
    )
    sf.add_argument(
        "--ollama-url",
        default=None,
        help="Ollama server URL (default: http://127.0.0.1:11434)",
    )
    sf.add_argument(
        "--allow-non-localhost-ollama",
        action="store_true",
        help="Explicitly permit a non-localhost --ollama-url",
    )
    sf.add_argument(
        "--max-bytes",
        type=int,
        default=FILE_DEFAULT_MAX_BYTES,
        help=(
            "Maximum file size in bytes (default: 1 MiB). Larger files "
            "are rejected without reading."
        ),
    )
    sf.add_argument(
        "--strict-prompt-injection",
        action="store_true",
        help=(
            "Treat warning-level prompt-injection markers as denials "
            "(default: warnings are surfaced but do not block). "
            "Critical markers always block regardless of this flag."
        ),
    )
    sf.add_argument(
        "--output",
        choices=("json", "text"),
        default="json",
        help=(
            "Output format. 'json' (default) emits the safe "
            "RouterDecision schema to stdout. 'text' emits the summary "
            "only on success; failures go to stderr with a short, "
            "content-free reason."
        ),
    )
    sf.add_argument(
        "--no-chunk",
        action="store_true",
        help=(
            "Disable automatic chunking; pass the full file body to the "
            "LLM in a single call (default: chunking is enabled when the "
            "file exceeds --chunk-size)."
        ),
    )
    sf.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=(
            "Chunk size in bytes for chunked summarize "
            f"(default: {DEFAULT_CHUNK_SIZE})."
        ),
    )
    sf.add_argument(
        "--max-chunks",
        type=int,
        default=DEFAULT_MAX_CHUNKS,
        help=(
            "Maximum number of chunks to summarize; remaining chunks are "
            f"dropped with a warning (default: {DEFAULT_MAX_CHUNKS})."
        ),
    )
    sf.set_defaults(func=cmd_summarize_file)

    sd = sub.add_parser(
        "summarize-dir",
        help=(
            "Walk a project-local directory, summarize each eligible "
            "text file, then summarize the aggregated per-file summaries"
        ),
    )
    sd.add_argument(
        "--path",
        required=True,
        help="Directory path (relative paths resolve against --project-root)",
    )
    sd.add_argument(
        "--backend",
        choices=("fake", "ollama"),
        default="fake",
        help="LLM backend to use (default: fake)",
    )
    sd.add_argument("--model", default=None, help="Ollama model name")
    sd.add_argument("--ollama-url", default=None, help="Ollama server URL")
    sd.add_argument(
        "--allow-non-localhost-ollama",
        action="store_true",
        help="Permit a non-localhost --ollama-url",
    )
    sd.add_argument(
        "--no-recursive",
        action="store_true",
        help="Only consider files directly under --path",
    )
    sd.add_argument(
        "--include",
        action="append",
        default=None,
        help=(
            "fnmatch pattern to include (relative to --path); may be "
            "repeated. Default: *.txt, *.md, *.rst, *.py"
        ),
    )
    sd.add_argument(
        "--exclude",
        action="append",
        default=None,
        help=(
            "fnmatch pattern to exclude (relative to --path); may be repeated"
        ),
    )
    sd.add_argument(
        "--max-files",
        type=int,
        default=DEFAULT_DIR_MAX_FILES,
        help=f"Maximum files to summarize (default: {DEFAULT_DIR_MAX_FILES})",
    )
    sd.add_argument(
        "--max-total-bytes",
        type=int,
        default=DEFAULT_DIR_MAX_TOTAL_BYTES,
        help=(
            "Cumulative read budget in bytes; further files are skipped "
            f"(default: {DEFAULT_DIR_MAX_TOTAL_BYTES})"
        ),
    )
    sd.add_argument(
        "--max-bytes",
        type=int,
        default=FILE_DEFAULT_MAX_BYTES,
        help="Per-file size limit (default: 1 MiB)",
    )
    sd.add_argument(
        "--strict-prompt-injection",
        action="store_true",
        help="Block on warning markers in addition to critical markers",
    )
    sd.add_argument(
        "--output",
        choices=("json", "text"),
        default="json",
        help="Output format (default: json)",
    )
    sd.set_defaults(func=cmd_summarize_dir)

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
