from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .adapters.llm import LLMAdapter
from .core.audit import AuditLogger
from .core.errors import AdapterError
from .core.types import RiskLevel
from .fakes.llm import FakeLLM
from .fakes.robot import FakeRobotTransport
from .files.chunking import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_MAX_CHUNKS,
    split_text,
)
from .files.index import (
    DEFAULT_INCLUDE_EXTS as INDEX_DEFAULT_EXTS,
    DEFAULT_MAX_BYTES_PER_FILE as INDEX_DEFAULT_MAX_BYTES,
    DEFAULT_MAX_FILES as INDEX_DEFAULT_MAX_FILES,
    build_index,
    default_index_path,
    load_index_json,
    save_index_json,
)
from .files.read_text import (
    DEFAULT_MAX_BYTES as FILE_DEFAULT_MAX_BYTES,
    FileReadError,
    read_text_file,
)
from .files.search import (
    DEFAULT_MAX_HITS as SEARCH_DEFAULT_MAX_HITS,
    SearchHit,
    search as search_index,
)
from .mail.draft import build_draft_prompt, default_subject_for_reply
from .mail.read_local import (
    DEFAULT_MAX_BODY_BYTES as MAIL_DEFAULT_MAX_BODY,
    DEFAULT_MBOX_LIMIT as MAIL_DEFAULT_MBOX_LIMIT,
    DateFilter,
    MailReadError,
    ParsedMail,
    _parse_filter_date,
    read_mail_any,
    read_mbox,
)
from .mail.search import (
    DEFAULT_MAX_HITS as MAIL_SEARCH_DEFAULT_MAX_HITS,
    MailHit,
    search_mail,
)
from .mail.thread import Thread, ThreadMessage, build_threads
from .gmail import (
    FakeGmailClient,
    GmailClient,
    GmailClientError,
    GmailCredentials,
    GmailDraft,
    GmailMessage,
    GmailSearchHit,
    GmailThread,
    GmailThreadMessage,
    build_threads as build_gmail_threads,
)
from .gmail.draft import build_reply_draft_raw  # noqa: F401
from .files.semantic_search import (
    DEFAULT_MAX_HITS as SEMANTIC_DEFAULT_MAX_HITS,
    SemanticHit,
    search_semantic,
)
from .files.vector_index import (
    VectorEntry,
    VectorIndex,
    default_vector_index_path,
    load_vector_index_json,
    save_vector_index_json,
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


def _build_embedder_from_args(args: argparse.Namespace):
    """Build an EmbeddingAdapter. Mirrors _build_llm_from_args.

    For 'fake' backend, returns a FakeEmbeddingAdapter; for 'ollama',
    returns an OllamaEmbeddingAdapter. Raises ValueError on misconfig.
    """
    backend = getattr(args, "embedding_backend", "ollama") or "ollama"
    model = getattr(args, "embedding_model", None)
    if backend == "fake":
        from .fakes.embedding import FakeEmbeddingAdapter

        return FakeEmbeddingAdapter(model=model or "fake-embed")
    if backend == "ollama":
        if not model:
            raise ValueError(
                "--embedding-backend ollama requires --embedding-model "
                "(e.g. --embedding-model nomic-embed-text)"
            )
        from .adapters.ollama import DEFAULT_BASE_URL as OLLAMA_DEFAULT_URL
        from .adapters.ollama_embeddings import OllamaEmbeddingAdapter

        base_url = (
            getattr(args, "embedding_ollama_url", None)
            or getattr(args, "ollama_url", None)
            or OLLAMA_DEFAULT_URL
        )
        allow_non_localhost = bool(
            getattr(args, "allow_non_localhost_ollama", False)
        )
        return OllamaEmbeddingAdapter(
            model=model,
            base_url=base_url,
            allow_non_localhost=allow_non_localhost,
        )
    raise ValueError(f"unsupported embedding backend: {backend!r}")


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

    # Step 4: optionally split the body into chunks and summarize each,
    # then summarize the joined chunk summaries.
    decision = _summarize_text_via_router(
        router=router,
        text=read_result.text,
        source_path=target_path,
        encoding=read_result.encoding,
        byte_size=read_result.byte_size,
        chunk_size=int(getattr(args, "chunk_size", DEFAULT_CHUNK_SIZE)),
        max_chunks=int(getattr(args, "max_chunks", DEFAULT_MAX_CHUNKS)),
        no_chunk=bool(getattr(args, "no_chunk", False)),
    )
    return _emit_decision(
        decision,
        output_mode=output_mode,
        include_result=True,
    )


def _summarize_text_via_router(
    *,
    router: Router,
    text: str,
    source_path: Path,
    encoding: str,
    byte_size: int,
    chunk_size: int,
    max_chunks: int,
    no_chunk: bool,
) -> RouterDecision:
    """Route text through the Router as one or more LLM_SUMMARIZE actions.

    For text <= chunk_size or when no_chunk is set, sends the whole body
    in a single call. Otherwise, splits the body, summarizes each chunk,
    then summarizes the joined chunk summaries. Returns the final
    RouterDecision with any chunk warnings folded in. If any chunk
    summarize step is denied, that decision is returned as-is.
    """
    chunk_warnings: List[str] = []
    if no_chunk or byte_size <= chunk_size:
        text_for_summary = text
    else:
        split = split_text(text, chunk_size=chunk_size, max_chunks=max_chunks)
        if split.truncated:
            chunk_warnings.append(
                f"chunking: truncated after {len(split.chunks)} chunks "
                f"(input > chunk_size * max_chunks)"
            )
        per_chunk_summaries: List[str] = []
        for idx, chunk in enumerate(split.chunks):
            decision = _summarize_chunk_via_router(
                router=router,
                text=chunk,
                source_ref=f"{source_path}:chunk-{idx + 1}",
                byte_size=len(chunk.encode("utf-8")),
                encoding=encoding,
            )
            if not decision.allowed or not isinstance(decision.result, str):
                # Propagate denial to caller as-is (with any chunk-warning
                # context); the caller decides what to do.
                if chunk_warnings:
                    decision = _decision_with_extra_warnings(
                        decision, chunk_warnings
                    )
                return decision
            per_chunk_summaries.append(decision.result)
            chunk_warnings.extend(decision.warnings)
        text_for_summary = "\n\n".join(
            f"--- chunk {i + 1} of {len(per_chunk_summaries)} ---\n{s}"
            for i, s in enumerate(per_chunk_summaries)
        )

    body = wrap_untrusted(
        text_for_summary,
        SourceKind.LOCAL_DOCUMENT,
        source_ref=str(source_path),
        metadata={
            "byte_size": str(byte_size),
            "encoding": encoding,
        },
    )
    final = router.route(
        RouterAction(
            kind=ActionKind.LLM_SUMMARIZE,
            risk_level=RiskLevel.LOW,
            body=body,
        )
    )
    if chunk_warnings:
        final = _decision_with_extra_warnings(final, chunk_warnings)
    return final


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


def cmd_index_files(args: argparse.Namespace) -> int:
    from .safety.project_boundary import ProjectBoundaryGuard
    from .safety.sensitive_paths import SensitivePathGuard

    project_root = Path(args.project_root).resolve()
    output_mode = getattr(args, "output", "json")

    # Build the Router only to run the directory's path through
    # boundary + sensitive gates and to write an audit event for the
    # index build. We do not use the LLM at all here.
    try:
        llm = _build_llm_from_args(args)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED
    router = _build_router(project_root, llm=llm)

    gate = router.route(
        RouterAction(
            kind=ActionKind.FILE_READ,
            risk_level=RiskLevel.LOW,
            target_path=args.path,
        )
    )
    if not gate.allowed:
        return _emit_decision(gate, output_mode=output_mode, include_result=False)

    target_dir = Path(args.path)
    if not target_dir.is_absolute():
        target_dir = (project_root / target_dir).resolve()
    if not target_dir.is_dir():
        if output_mode == "text":
            sys.stderr.write("wolf cli: index target is not a directory\n")
        else:
            _print_json(
                {
                    "allowed": False,
                    "executed": False,
                    "requires_confirmation": False,
                    "stage": "file_read",
                    "reason": "index target is not a directory",
                    "provider_called": False,
                    "audit_event_id": gate.audit_event_id,
                    "failed_checks": ["index: not a directory"],
                    "warnings": [],
                }
            )
        return EXIT_DENIED

    boundary = ProjectBoundaryGuard(project_root)
    sensitive = SensitivePathGuard(project_root=project_root)
    include = tuple(getattr(args, "include", None) or [])
    exclude = tuple(getattr(args, "exclude", None) or [])
    recursive = not bool(getattr(args, "no_recursive", False))

    result = build_index(
        project_root=project_root,
        target_dir=target_dir,
        boundary=boundary,
        sensitive=sensitive,
        recursive=recursive,
        include=include or None,
        exclude=exclude or None,
        max_files=int(getattr(args, "max_files", INDEX_DEFAULT_MAX_FILES)),
        max_bytes_per_file=int(
            getattr(args, "max_bytes", INDEX_DEFAULT_MAX_BYTES)
        ),
    )

    output_arg = getattr(args, "index_output", None)
    out_path = (
        Path(output_arg).resolve()
        if output_arg
        else default_index_path(project_root)
    )
    # Confine the index output to project_root.
    if not str(out_path).startswith(str(project_root)):
        sys.stderr.write(
            "wolf cli: --index-output must be inside --project-root\n"
        )
        return EXIT_DENIED
    save_index_json(result.index, out_path)

    # Optional embedding index built alongside the metadata index.
    embed_result_summary = None
    embed_warnings: List[str] = []
    if bool(getattr(args, "embed", False)):
        embed_out_arg = getattr(args, "embedding_index_path", None)
        embed_out_path = (
            Path(embed_out_arg).resolve()
            if embed_out_arg
            else default_vector_index_path(project_root)
        )
        if not str(embed_out_path).startswith(str(project_root)):
            sys.stderr.write(
                "wolf cli: --embedding-index-path must be inside --project-root\n"
            )
            return EXIT_DENIED
        try:
            embedder = _build_embedder_from_args(args)
        except ValueError as exc:
            sys.stderr.write(f"wolf cli: {exc}\n")
            return EXIT_DENIED

        max_embed_bytes = int(
            getattr(args, "embedding_input_bytes", 4096)
        )
        vec_entries: List[VectorEntry] = []
        for entry in result.index.entries:
            full = (project_root / entry.path).resolve()
            try:
                blob = full.read_bytes()[:max_embed_bytes]
                text_for_embedding = blob.decode(entry.encoding, errors="ignore")
            except OSError as exc:
                embed_warnings.append(
                    f"embed: skipped {entry.path} (read: {type(exc).__name__})"
                )
                continue
            try:
                vec = embedder.embed(text_for_embedding)
            except AdapterError as exc:
                embed_warnings.append(
                    f"embed: skipped {entry.path} (embedder: {exc.label})"
                )
                continue
            vec_entries.append(
                VectorEntry(
                    path=entry.path,
                    size=entry.size,
                    mtime=entry.mtime,
                    extension=entry.extension,
                    snippet=entry.snippet,
                    encoding=entry.encoding,
                    embedding=tuple(vec),
                )
            )

        if not vec_entries:
            sys.stderr.write(
                "wolf cli: embedding index would be empty; skipping save\n"
            )
            embed_result_summary = {
                "indexed_embeddings": 0,
                "embedding_index_path": None,
                "embedding_warnings": embed_warnings,
            }
        else:
            model_name = getattr(args, "embedding_model", None) or "fake-embed"
            vector_index = VectorIndex(
                project_root=str(project_root),
                created_at=result.index.created_at,
                embedding_model=model_name,
                dim=len(vec_entries[0].embedding),
                entries=tuple(vec_entries),
                skipped=tuple(embed_warnings),
            )
            save_vector_index_json(vector_index, embed_out_path)
            embed_result_summary = {
                "indexed_embeddings": len(vec_entries),
                "embedding_index_path": str(
                    embed_out_path.relative_to(project_root)
                ),
                "embedding_model": model_name,
                "embedding_dim": vector_index.dim,
            }

    if output_mode == "text":
        sys.stdout.write(
            f"indexed {result.accepted_count} files "
            f"(skipped {result.skipped_count}) -> "
            f"{out_path.relative_to(project_root)}\n"
        )
        if embed_result_summary:
            ep = embed_result_summary.get("embedding_index_path")
            n = embed_result_summary.get("indexed_embeddings", 0)
            if ep:
                sys.stdout.write(
                    f"embedded {n} files -> {ep}\n"
                )
            else:
                sys.stdout.write("embedding index was empty (no files saved)\n")
        return EXIT_SUCCESS

    payload_result = {
        "indexed": result.accepted_count,
        "skipped": result.skipped_count,
        "index_path": str(out_path.relative_to(project_root)),
    }
    if embed_result_summary:
        payload_result.update(embed_result_summary)
    _print_json(
        {
            "allowed": True,
            "executed": True,
            "requires_confirmation": False,
            "stage": "complete",
            "reason": "index built",
            "provider_called": False,
            "audit_event_id": gate.audit_event_id,
            "failed_checks": [],
            "warnings": list(result.index.skipped) + embed_warnings,
            "result": payload_result,
        }
    )
    return EXIT_SUCCESS


def cmd_search_files(args: argparse.Namespace) -> int:
    from .safety.project_boundary import ProjectBoundaryGuard
    from .safety.sensitive_paths import SensitivePathGuard

    project_root = Path(args.project_root).resolve()
    output_mode = getattr(args, "output", "json")
    query = getattr(args, "query", None)
    if not query:
        sys.stderr.write("wolf cli: --query is required\n")
        return EXIT_DENIED

    boundary = ProjectBoundaryGuard(project_root)
    sensitive = SensitivePathGuard(project_root=project_root)
    max_hits = int(getattr(args, "max_hits", SEARCH_DEFAULT_MAX_HITS))
    use_semantic = bool(getattr(args, "semantic", False))

    if use_semantic:
        # Semantic mode: load vector index and embed the query.
        vec_path_arg = getattr(args, "embedding_index_path", None)
        vec_path = (
            Path(vec_path_arg).resolve()
            if vec_path_arg
            else default_vector_index_path(project_root)
        )
        if not str(vec_path).startswith(str(project_root)):
            sys.stderr.write(
                "wolf cli: --embedding-index-path must be inside --project-root\n"
            )
            return EXIT_DENIED
        try:
            vec_index = load_vector_index_json(vec_path)
        except (FileNotFoundError, ValueError) as exc:
            if output_mode == "text":
                sys.stderr.write(f"wolf cli: semantic index load failed: {exc}\n")
            else:
                _print_json(
                    {
                        "allowed": False,
                        "executed": False,
                        "requires_confirmation": False,
                        "stage": "file_read",
                        "reason": f"semantic index load failed: {exc}",
                        "provider_called": False,
                        "audit_event_id": None,
                        "failed_checks": ["semantic_index: load_failed"],
                        "warnings": [],
                    }
                )
            return EXIT_DENIED
        try:
            embedder = _build_embedder_from_args(args)
        except ValueError as exc:
            sys.stderr.write(f"wolf cli: {exc}\n")
            return EXIT_DENIED
        try:
            sem_hits = search_semantic(
                vec_index,
                query,
                embedder=embedder,
                project_root=project_root,
                boundary=boundary,
                sensitive=sensitive,
                max_hits=max_hits,
            )
        except AdapterError as exc:
            if output_mode == "text":
                sys.stderr.write(
                    f"wolf cli: embedder failed: {exc.label}\n"
                )
            else:
                _print_json(
                    {
                        "allowed": False,
                        "executed": False,
                        "requires_confirmation": False,
                        "stage": "provider",
                        "reason": f"embedder failed: {exc.label}",
                        "provider_called": True,
                        "audit_event_id": None,
                        "failed_checks": [f"embedder: {exc.label}"],
                        "warnings": [],
                    }
                )
            return EXIT_DENIED

        if not sem_hits:
            if output_mode == "text":
                sys.stderr.write(
                    f"wolf cli: no semantic hits for {query!r}\n"
                )
            else:
                _print_json(
                    {
                        "allowed": False,
                        "executed": False,
                        "requires_confirmation": False,
                        "stage": "search",
                        "reason": "no hits",
                        "provider_called": False,
                        "audit_event_id": None,
                        "failed_checks": [],
                        "warnings": [],
                        "result": {
                            "hits": [],
                            "query": query,
                            "mode": "semantic",
                        },
                    }
                )
            return EXIT_DENIED

        if output_mode == "text":
            for h in sem_hits:
                snippet_one_line = h.snippet.replace("\n", " ")
                sys.stdout.write(
                    f"{h.path} (score={h.score:.4f}): "
                    f"{snippet_one_line}\n"
                )
            return EXIT_SUCCESS

        _print_json(
            {
                "allowed": True,
                "executed": True,
                "requires_confirmation": False,
                "stage": "complete",
                "reason": f"{len(sem_hits)} semantic hit(s)",
                "provider_called": True,
                "audit_event_id": None,
                "failed_checks": [],
                "warnings": [],
                "result": {
                    "query": query,
                    "mode": "semantic",
                    "hits": [
                        {
                            "path": h.path,
                            "score": h.score,
                            "snippet": h.snippet,
                        }
                        for h in sem_hits
                    ],
                },
            }
        )
        return EXIT_SUCCESS

    # Substring (default) path.
    index_path_arg = getattr(args, "index_path", None)
    index_path = (
        Path(index_path_arg).resolve()
        if index_path_arg
        else default_index_path(project_root)
    )
    if not str(index_path).startswith(str(project_root)):
        sys.stderr.write(
            "wolf cli: --index-path must be inside --project-root\n"
        )
        return EXIT_DENIED
    try:
        index = load_index_json(index_path)
    except (FileNotFoundError, ValueError) as exc:
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: index load failed: {exc}\n")
        else:
            _print_json(
                {
                    "allowed": False,
                    "executed": False,
                    "requires_confirmation": False,
                    "stage": "file_read",
                    "reason": f"index load failed: {exc}",
                    "provider_called": False,
                    "audit_event_id": None,
                    "failed_checks": [f"index: load_failed"],
                    "warnings": [],
                }
            )
        return EXIT_DENIED

    hits = search_index(
        index,
        query,
        project_root=project_root,
        boundary=boundary,
        sensitive=sensitive,
        max_hits=max_hits,
    )

    if not hits:
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: no hits for {query!r}\n")
        else:
            _print_json(
                {
                    "allowed": False,
                    "executed": False,
                    "requires_confirmation": False,
                    "stage": "search",
                    "reason": "no hits",
                    "provider_called": False,
                    "audit_event_id": None,
                    "failed_checks": [],
                    "warnings": [],
                    "result": {"hits": [], "query": query},
                }
            )
        return EXIT_DENIED

    if output_mode == "text":
        for h in hits:
            line_label = f":{h.line_number}" if h.line_number else ""
            snippet_one_line = h.snippet.replace("\n", " ")
            sys.stdout.write(
                f"{h.path}{line_label} ({h.match_count} match"
                f"{'es' if h.match_count != 1 else ''}): "
                f"{snippet_one_line}\n"
            )
        return EXIT_SUCCESS

    _print_json(
        {
            "allowed": True,
            "executed": True,
            "requires_confirmation": False,
            "stage": "complete",
            "reason": f"{len(hits)} hit(s)",
            "provider_called": False,
            "audit_event_id": None,
            "failed_checks": [],
            "warnings": [],
            "result": {
                "query": query,
                "hits": [
                    {
                        "path": h.path,
                        "line_number": h.line_number,
                        "snippet": h.snippet,
                        "match_count": h.match_count,
                    }
                    for h in hits
                ],
            },
        }
    )
    return EXIT_SUCCESS


def _cmd_search_summarize_semantic(
    *,
    args: argparse.Namespace,
    project_root: Path,
    output_mode: str,
    query: str,
    router: Router,
    boundary,
    sensitive,
    build_warnings: List[str],
) -> int:
    vec_path_arg = getattr(args, "embedding_index_path", None)
    vec_path = (
        Path(vec_path_arg).resolve()
        if vec_path_arg
        else default_vector_index_path(project_root)
    )
    if not str(vec_path).startswith(str(project_root)):
        sys.stderr.write(
            "wolf cli: --embedding-index-path must be inside --project-root\n"
        )
        return EXIT_DENIED
    try:
        vec_index = load_vector_index_json(vec_path)
    except (FileNotFoundError, ValueError) as exc:
        if output_mode == "text":
            sys.stderr.write(
                f"wolf cli: semantic index load failed: {exc}\n"
            )
        else:
            _print_json(
                {
                    "allowed": False,
                    "executed": False,
                    "requires_confirmation": False,
                    "stage": "file_read",
                    "reason": f"semantic index load failed: {exc}",
                    "provider_called": False,
                    "audit_event_id": None,
                    "failed_checks": ["semantic_index: load_failed"],
                    "warnings": build_warnings,
                }
            )
        return EXIT_DENIED

    try:
        embedder = _build_embedder_from_args(args)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED

    limit = int(getattr(args, "limit", 5))
    max_files = int(getattr(args, "max_files", 5))
    include_per_file_summary = bool(
        getattr(args, "include_per_file_summary", False)
    )
    try:
        sem_hits = search_semantic(
            vec_index,
            query,
            embedder=embedder,
            project_root=project_root,
            boundary=boundary,
            sensitive=sensitive,
            max_hits=limit,
        )
    except AdapterError as exc:
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: embedder failed: {exc.label}\n")
        else:
            _print_json(
                {
                    "allowed": False,
                    "executed": False,
                    "requires_confirmation": False,
                    "stage": "provider",
                    "reason": f"embedder failed: {exc.label}",
                    "provider_called": True,
                    "audit_event_id": None,
                    "failed_checks": [f"embedder: {exc.label}"],
                    "warnings": build_warnings,
                }
            )
        return EXIT_DENIED

    if not sem_hits:
        payload = {
            "allowed": False,
            "executed": False,
            "requires_confirmation": False,
            "stage": "search",
            "reason": "no hits",
            "provider_called": True,
            "audit_event_id": None,
            "failed_checks": [],
            "warnings": build_warnings,
            "result": {"query": query, "hit_count": 0, "mode": "semantic"},
        }
        if output_mode == "text":
            sys.stderr.write(
                f"wolf cli: no semantic hits for {query!r}\n"
            )
        else:
            _print_json(payload)
        return EXIT_DENIED

    chunk_size = int(getattr(args, "chunk_size", DEFAULT_CHUNK_SIZE))
    max_chunks = int(getattr(args, "max_chunks", DEFAULT_MAX_CHUNKS))
    no_chunk = bool(getattr(args, "no_chunk", False))
    per_file_summaries: List[str] = []
    file_records: List[dict] = []
    warnings: List[str] = list(build_warnings)
    accepted = 0
    for hit in sem_hits[:max_files]:
        full_path = (project_root / hit.path).resolve()
        bd = boundary.check(full_path)
        sd = sensitive.check(full_path)
        if not bd.allowed or not sd.allowed:
            warnings.append(
                f"search-summarize: skipped {hit.path} (gate denied)"
            )
            continue
        try:
            read_result = read_text_file(
                full_path,
                max_bytes=int(
                    getattr(
                        args, "max_bytes_per_file", FILE_DEFAULT_MAX_BYTES
                    )
                ),
            )
        except FileReadError as exc:
            warnings.append(
                f"search-summarize: skipped {hit.path} "
                f"(file_read: {exc.label})"
            )
            continue
        decision = _summarize_text_via_router(
            router=router,
            text=read_result.text,
            source_path=full_path,
            encoding=read_result.encoding,
            byte_size=read_result.byte_size,
            chunk_size=chunk_size,
            max_chunks=max_chunks,
            no_chunk=no_chunk,
        )
        if not decision.allowed or not isinstance(decision.result, str):
            warnings.append(
                f"search-summarize: skipped {hit.path} "
                f"(stage={decision.stage})"
            )
            continue
        per_file_summaries.append(f"[{hit.path}]\n{decision.result}")
        warnings.extend(decision.warnings)
        record = {
            "path": hit.path,
            "score": hit.score,
            "summary_length": len(decision.result),
        }
        if include_per_file_summary:
            record["summary"] = decision.result
        file_records.append(record)
        accepted += 1

    if accepted == 0:
        payload = {
            "allowed": False,
            "executed": False,
            "requires_confirmation": False,
            "stage": "search",
            "reason": "no files could be summarized",
            "provider_called": True,
            "audit_event_id": None,
            "failed_checks": ["search-summarize: no eligible hits"],
            "warnings": warnings,
            "result": {
                "query": query,
                "mode": "semantic",
                "hit_count": len(sem_hits),
                "summarized_count": 0,
                "skipped_count": len(sem_hits),
                "files": [],
            },
        }
        if output_mode == "text":
            sys.stderr.write(
                f"wolf cli: {len(sem_hits)} semantic hit(s) but none "
                "could be summarized\n"
            )
        else:
            _print_json(payload)
        return EXIT_DENIED

    aggregated_text = "\n\n".join(per_file_summaries)
    final_body = wrap_untrusted(
        aggregated_text,
        SourceKind.LOCAL_DOCUMENT,
        source_ref=f"search:semantic:{query}",
        metadata={
            "hit_count": str(len(sem_hits)),
            "summarized_count": str(accepted),
        },
    )
    final = router.route(
        RouterAction(
            kind=ActionKind.LLM_SUMMARIZE,
            risk_level=RiskLevel.LOW,
            body=final_body,
        )
    )
    if warnings:
        final = _decision_with_extra_warnings(final, warnings)

    if output_mode == "text":
        if final.allowed and isinstance(final.result, str):
            sys.stdout.write(final.result)
            if not final.result.endswith("\n"):
                sys.stdout.write("\n")
            if final.warnings:
                sys.stderr.write(
                    f"wolf cli: {len(final.warnings)} warning(s) during "
                    f"search-summarize\n"
                )
            return EXIT_SUCCESS
        sys.stderr.write(
            f"wolf cli: stage={final.stage} reason={final.reason}\n"
        )
        return _exit_code_for(final)

    payload = _decision_to_safe_dict(final, include_result=False)
    payload["result"] = {
        "query": query,
        "mode": "semantic",
        "hit_count": len(sem_hits),
        "summarized_count": accepted,
        "skipped_count": len(sem_hits) - accepted,
        "files": file_records,
    }
    if final.allowed and isinstance(final.result, str):
        payload["result"]["summary"] = final.result
    _print_json(payload)
    return _exit_code_for(final)


def cmd_search_summarize(args: argparse.Namespace) -> int:
    """Search the index and summarize matching files.

    Pipeline:
      1. Build LLM adapter, build Router (with warning-allow default unless
         --strict-prompt-injection).
      2. Load .wolf/index/files.json (or build it if --build-index).
      3. Run the keyword search; if zero hits, exit 2.
      4. For each hit (up to --limit / --max-files), read the file and
         summarize it via _summarize_text_via_router. Track per-file
         results and skip reasons.
      5. If at least one file was successfully summarized, concatenate
         the per-file summaries and route them through one final
         LLM_SUMMARIZE for an aggregate result.
    """
    from .safety.project_boundary import ProjectBoundaryGuard
    from .safety.sensitive_paths import SensitivePathGuard

    project_root = Path(args.project_root).resolve()
    output_mode = getattr(args, "output", "json")
    strict = bool(getattr(args, "strict_prompt_injection", False))
    query = getattr(args, "query", None)
    if not query:
        sys.stderr.write("wolf cli: --query is required\n")
        return EXIT_DENIED

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

    # Step 1: locate / build the index.
    index_path_arg = getattr(args, "index_path", None)
    index_path = (
        Path(index_path_arg).resolve()
        if index_path_arg
        else default_index_path(project_root)
    )
    if not str(index_path).startswith(str(project_root)):
        sys.stderr.write(
            "wolf cli: --index-path must be inside --project-root\n"
        )
        return EXIT_DENIED

    boundary = ProjectBoundaryGuard(project_root)
    sensitive = SensitivePathGuard(project_root=project_root)
    build_index_first = bool(getattr(args, "build_index", False))
    build_warnings: List[str] = []
    use_semantic = bool(getattr(args, "semantic", False))

    # Semantic path: load vector index, embed query, get hits, then jump
    # straight to the per-hit summarize loop using a normalized hit list.
    if use_semantic:
        return _cmd_search_summarize_semantic(
            args=args,
            project_root=project_root,
            output_mode=output_mode,
            query=query,
            router=router,
            boundary=boundary,
            sensitive=sensitive,
            build_warnings=build_warnings,
        )

    if build_index_first or not index_path.exists():
        if not build_index_first and not index_path.exists():
            if output_mode == "text":
                sys.stderr.write(
                    f"wolf cli: index not found at {index_path}; "
                    "rerun with --build-index or run index-files first\n"
                )
            else:
                _print_json(
                    {
                        "allowed": False,
                        "executed": False,
                        "requires_confirmation": False,
                        "stage": "file_read",
                        "reason": "index not found",
                        "provider_called": False,
                        "audit_event_id": None,
                        "failed_checks": ["index: not found"],
                        "warnings": [],
                    }
                )
            return EXIT_DENIED

        target_dir = Path(getattr(args, "path", None) or project_root)
        if not target_dir.is_absolute():
            target_dir = (project_root / target_dir).resolve()
        # Gate the directory via the Router so the audit log records
        # the implicit index build.
        gate = router.route(
            RouterAction(
                kind=ActionKind.FILE_READ,
                risk_level=RiskLevel.LOW,
                target_path=str(target_dir),
            )
        )
        if not gate.allowed:
            return _emit_decision(
                gate, output_mode=output_mode, include_result=False
            )
        result = build_index(
            project_root=project_root,
            target_dir=target_dir,
            boundary=boundary,
            sensitive=sensitive,
            recursive=not bool(getattr(args, "no_recursive", False)),
            include=tuple(getattr(args, "include", None) or []) or None,
            exclude=tuple(getattr(args, "exclude", None) or []) or None,
            max_files=int(
                getattr(args, "max_files", INDEX_DEFAULT_MAX_FILES)
            ),
            max_bytes_per_file=int(
                getattr(args, "max_bytes", INDEX_DEFAULT_MAX_BYTES)
            ),
        )
        save_index_json(result.index, index_path)
        build_warnings.append(
            f"index: built {result.accepted_count} entries "
            f"(skipped {result.skipped_count}) at {index_path.relative_to(project_root)}"
        )
        index = result.index
    else:
        try:
            index = load_index_json(index_path)
        except (FileNotFoundError, ValueError) as exc:
            if output_mode == "text":
                sys.stderr.write(f"wolf cli: index load failed: {exc}\n")
            else:
                _print_json(
                    {
                        "allowed": False,
                        "executed": False,
                        "requires_confirmation": False,
                        "stage": "file_read",
                        "reason": f"index load failed: {exc}",
                        "provider_called": False,
                        "audit_event_id": None,
                        "failed_checks": ["index: load_failed"],
                        "warnings": build_warnings,
                    }
                )
            return EXIT_DENIED

    # Step 2: search.
    limit = int(getattr(args, "limit", 5))
    max_files = int(getattr(args, "max_files", 5))
    include_per_file_summary = bool(
        getattr(args, "include_per_file_summary", False)
    )
    hits = search_index(
        index,
        query,
        project_root=project_root,
        boundary=boundary,
        sensitive=sensitive,
        max_hits=limit,
    )
    if not hits:
        payload = {
            "allowed": False,
            "executed": False,
            "requires_confirmation": False,
            "stage": "search",
            "reason": "no hits",
            "provider_called": False,
            "audit_event_id": None,
            "failed_checks": [],
            "warnings": build_warnings,
            "result": {"query": query, "hit_count": 0},
        }
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: no hits for {query!r}\n")
        else:
            _print_json(payload)
        return EXIT_DENIED

    # Step 3: per-hit summarize.
    chunk_size = int(getattr(args, "chunk_size", DEFAULT_CHUNK_SIZE))
    max_chunks = int(getattr(args, "max_chunks", DEFAULT_MAX_CHUNKS))
    no_chunk = bool(getattr(args, "no_chunk", False))
    per_file_summaries: List[str] = []
    file_records: List[dict] = []
    warnings: List[str] = list(build_warnings)
    accepted = 0
    for hit in hits[:max_files]:
        full_path = (project_root / hit.path).resolve()
        # Re-validate boundary + sensitive (search() already did, but be
        # explicit at the call site).
        bd = boundary.check(full_path)
        sd = sensitive.check(full_path)
        if not bd.allowed or not sd.allowed:
            warnings.append(
                f"search-summarize: skipped {hit.path} (gate denied)"
            )
            continue
        try:
            read_result = read_text_file(
                full_path,
                max_bytes=int(
                    getattr(args, "max_bytes_per_file", FILE_DEFAULT_MAX_BYTES)
                ),
            )
        except FileReadError as exc:
            warnings.append(
                f"search-summarize: skipped {hit.path} "
                f"(file_read: {exc.label})"
            )
            continue
        decision = _summarize_text_via_router(
            router=router,
            text=read_result.text,
            source_path=full_path,
            encoding=read_result.encoding,
            byte_size=read_result.byte_size,
            chunk_size=chunk_size,
            max_chunks=max_chunks,
            no_chunk=no_chunk,
        )
        if not decision.allowed or not isinstance(decision.result, str):
            warnings.append(
                f"search-summarize: skipped {hit.path} "
                f"(stage={decision.stage})"
            )
            continue
        per_file_summaries.append(f"[{hit.path}]\n{decision.result}")
        warnings.extend(decision.warnings)
        record = {
            "path": hit.path,
            "match_count": hit.match_count,
            "line_number": hit.line_number,
            "summary_length": len(decision.result),
        }
        if include_per_file_summary:
            record["summary"] = decision.result
        file_records.append(record)
        accepted += 1

    if accepted == 0:
        payload = {
            "allowed": False,
            "executed": False,
            "requires_confirmation": False,
            "stage": "search",
            "reason": "no files could be summarized",
            "provider_called": False,
            "audit_event_id": None,
            "failed_checks": ["search-summarize: no eligible hits"],
            "warnings": warnings,
            "result": {
                "query": query,
                "hit_count": len(hits),
                "summarized_count": 0,
                "skipped_count": len(hits),
                "files": [],
            },
        }
        if output_mode == "text":
            sys.stderr.write(
                f"wolf cli: {len(hits)} hit(s) but none could be summarized\n"
            )
        else:
            _print_json(payload)
        return EXIT_DENIED

    # Step 4: final aggregate summary.
    aggregated_text = "\n\n".join(per_file_summaries)
    final_body = wrap_untrusted(
        aggregated_text,
        SourceKind.LOCAL_DOCUMENT,
        source_ref=f"search:{query}",
        metadata={
            "hit_count": str(len(hits)),
            "summarized_count": str(accepted),
        },
    )
    final = router.route(
        RouterAction(
            kind=ActionKind.LLM_SUMMARIZE,
            risk_level=RiskLevel.LOW,
            body=final_body,
        )
    )
    if warnings:
        final = _decision_with_extra_warnings(final, warnings)

    if output_mode == "text":
        if final.allowed and isinstance(final.result, str):
            sys.stdout.write(final.result)
            if not final.result.endswith("\n"):
                sys.stdout.write("\n")
            if final.warnings:
                sys.stderr.write(
                    f"wolf cli: {len(final.warnings)} warning(s) during "
                    f"search-summarize\n"
                )
            return EXIT_SUCCESS
        sys.stderr.write(
            f"wolf cli: stage={final.stage} reason={final.reason}\n"
        )
        return _exit_code_for(final)

    payload = _decision_to_safe_dict(final, include_result=False)
    payload["result"] = {
        "query": query,
        "hit_count": len(hits),
        "summarized_count": accepted,
        "skipped_count": len(hits) - accepted,
        "files": file_records,
    }
    if final.allowed and isinstance(final.result, str):
        payload["result"]["summary"] = final.result
    _print_json(payload)
    return _exit_code_for(final)


def _gate_mail_path(
    *,
    router: Router,
    path_str: str,
    output_mode: str,
) -> Optional[RouterDecision]:
    """Route the .eml/.mbox path through ProjectBoundary + SensitivePath.

    Returns None on allow; returns the deny RouterDecision on failure
    so the caller can pass it to _emit_decision.
    """
    gate = router.route(
        RouterAction(
            kind=ActionKind.FILE_READ,
            risk_level=RiskLevel.LOW,
            target_path=path_str,
        )
    )
    if not gate.allowed:
        return gate
    return None


def _emit_text_warning_count(warnings_count: int) -> None:
    if warnings_count > 0:
        sys.stderr.write(
            f"wolf cli: {warnings_count} warning(s) during operation\n"
        )


def _build_date_filter_from_args(
    args: argparse.Namespace,
) -> Optional[DateFilter]:
    """Build a DateFilter from --filter-since / --filter-until args.

    Returns None when both are absent so callers can pass an empty
    filter through unchanged. Raises ValueError on a malformed CLI
    date.
    """
    since_str = getattr(args, "filter_since", None)
    until_str = getattr(args, "filter_until", None)
    if not since_str and not until_str:
        return None
    since = _parse_filter_date(since_str) if since_str else None
    until = _parse_filter_date(until_str) if until_str else None
    return DateFilter(since=since, until=until)


def _read_mail_with_args(
    *,
    args: argparse.Namespace,
    path: Path,
) -> "MboxReadResult":
    """Centralized read_mail_any call site that pulls every filter the
    mail subcommands accept off the argparse Namespace."""
    return read_mail_any(
        path,
        limit=int(getattr(args, "limit", MAIL_DEFAULT_MBOX_LIMIT)),
        max_bytes=int(getattr(args, "max_bytes", MAIL_DEFAULT_MAX_BODY)),
        filter_subject=getattr(args, "filter_subject", None) or None,
        filter_from=getattr(args, "filter_from", None) or None,
        filter_body_contains=getattr(args, "filter_body_contains", None) or None,
        date_filter=_build_date_filter_from_args(args),
    )


def cmd_mail_summarize(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output_mode = getattr(args, "output", "json")
    try:
        llm = _build_llm_from_args(args)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED
    # Mail is more strict by default: warning-level injection markers
    # block too. Override via --strict-prompt-injection toggle is not
    # offered for mail in v0.2 (out of scope).
    router = _build_router(
        project_root,
        llm=llm,
        config=RouterConfig(allow_warning_injection_findings=False),
    )

    gate = _gate_mail_path(
        router=router, path_str=args.path, output_mode=output_mode
    )
    if gate is not None:
        return _emit_decision(
            gate, output_mode=output_mode, include_result=False
        )

    path = Path(args.path)
    if not path.is_absolute():
        path = (project_root / path).resolve()
    try:
        result = _read_mail_with_args(args=args, path=path)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED
    except MailReadError as exc:
        payload = {
            "allowed": False,
            "executed": False,
            "requires_confirmation": False,
            "stage": "mail_read",
            "reason": f"mail_read failed: {exc.label}",
            "provider_called": False,
            "audit_event_id": None,
            "failed_checks": [f"mail_read: {exc.label}"],
            "warnings": [],
        }
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: mail_read failed: {exc.label}\n")
        else:
            _print_json(payload)
        return EXIT_DENIED

    if not result.messages:
        payload = {
            "allowed": False,
            "executed": False,
            "requires_confirmation": False,
            "stage": "mail_read",
            "reason": "no messages",
            "provider_called": False,
            "audit_event_id": None,
            "failed_checks": [],
            "warnings": list(result.skipped),
        }
        if output_mode == "text":
            sys.stderr.write("wolf cli: no messages found\n")
        else:
            _print_json(payload)
        return EXIT_DENIED

    summaries: List[dict] = []
    warnings: List[str] = list(result.skipped)
    for pm in result.messages:
        body = wrap_untrusted(
            pm.body,
            SourceKind.EMAIL,
            source_ref=pm.message_id or str(path),
            metadata={
                "subject": pm.subject,
                "from": pm.from_,
                "byte_size": str(pm.byte_size),
            },
        )
        decision = router.route(
            RouterAction(
                kind=ActionKind.LLM_SUMMARIZE_EMAIL,
                risk_level=RiskLevel.LOW,
                body=body,
            )
        )
        if not decision.allowed or not isinstance(decision.result, str):
            warnings.append(
                f"mail-summarize: skipped {pm.message_id or pm.subject!r} "
                f"(stage={decision.stage})"
            )
            continue
        summaries.append(
            {
                "message_id": pm.message_id,
                "subject": pm.subject,
                "from": pm.from_,
                "date": pm.date,
                "summary": decision.result,
                "summary_length": len(decision.result),
                "has_attachments": pm.has_attachments,
                "attachments_count": len(pm.attachments),
                "attachments": [
                    {
                        "filename": a.filename,
                        "content_type": a.content_type,
                        "size_bytes": a.size_bytes,
                    }
                    for a in pm.attachments
                ],
            }
        )
        warnings.extend(decision.warnings)

    if not summaries:
        payload = {
            "allowed": False,
            "executed": False,
            "requires_confirmation": False,
            "stage": "prompt_injection",
            "reason": "no messages could be summarized",
            "provider_called": True,
            "audit_event_id": None,
            "failed_checks": ["mail-summarize: no eligible messages"],
            "warnings": warnings,
            "result": {
                "message_count": len(result.messages),
                "summarized_count": 0,
                "summaries": [],
            },
        }
        if output_mode == "text":
            sys.stderr.write(
                f"wolf cli: {len(result.messages)} message(s) read but none "
                "could be summarized\n"
            )
        else:
            _print_json(payload)
        return EXIT_DENIED

    if output_mode == "text":
        for s in summaries:
            sys.stdout.write(
                f"[{s['subject']}] {s['summary']}\n"
            )
        _emit_text_warning_count(len(warnings))
        return EXIT_SUCCESS

    _print_json(
        {
            "allowed": True,
            "executed": True,
            "requires_confirmation": False,
            "stage": "complete",
            "reason": f"{len(summaries)} message(s) summarized",
            "provider_called": True,
            "audit_event_id": None,
            "failed_checks": [],
            "warnings": warnings,
            "result": {
                "message_count": len(result.messages),
                "summarized_count": len(summaries),
                "summaries": summaries,
            },
        }
    )
    return EXIT_SUCCESS


def cmd_mail_search(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output_mode = getattr(args, "output", "json")
    query = getattr(args, "query", None)
    if not query:
        sys.stderr.write("wolf cli: --query is required\n")
        return EXIT_DENIED

    # Build router only so the path gate + audit log run.
    try:
        llm = _build_llm_from_args(args)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED
    router = _build_router(project_root, llm=llm)
    gate = _gate_mail_path(
        router=router, path_str=args.path, output_mode=output_mode
    )
    if gate is not None:
        return _emit_decision(
            gate, output_mode=output_mode, include_result=False
        )

    path = Path(args.path)
    if not path.is_absolute():
        path = (project_root / path).resolve()
    try:
        result = _read_mail_with_args(args=args, path=path)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED
    except MailReadError as exc:
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: mail_read failed: {exc.label}\n")
        else:
            _print_json(
                {
                    "allowed": False,
                    "executed": False,
                    "requires_confirmation": False,
                    "stage": "mail_read",
                    "reason": f"mail_read failed: {exc.label}",
                    "provider_called": False,
                    "audit_event_id": None,
                    "failed_checks": [f"mail_read: {exc.label}"],
                    "warnings": [],
                }
            )
        return EXIT_DENIED

    max_hits = int(
        getattr(args, "max_hits", MAIL_SEARCH_DEFAULT_MAX_HITS)
    )
    hits = search_mail(result.messages, query, max_hits=max_hits)

    if not hits:
        payload = {
            "allowed": False,
            "executed": False,
            "requires_confirmation": False,
            "stage": "search",
            "reason": "no hits",
            "provider_called": False,
            "audit_event_id": None,
            "failed_checks": [],
            "warnings": list(result.skipped),
            "result": {"query": query, "hits": [], "message_count": len(result.messages)},
        }
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: no mail hits for {query!r}\n")
        else:
            _print_json(payload)
        return EXIT_DENIED

    if output_mode == "text":
        for h in hits:
            sys.stdout.write(
                f"[{h.match_field}] {h.subject} <{h.from_}>: "
                f"{h.snippet.replace(chr(10), ' ')}\n"
            )
        _emit_text_warning_count(len(result.skipped))
        return EXIT_SUCCESS

    _print_json(
        {
            "allowed": True,
            "executed": True,
            "requires_confirmation": False,
            "stage": "complete",
            "reason": f"{len(hits)} mail hit(s)",
            "provider_called": False,
            "audit_event_id": None,
            "failed_checks": [],
            "warnings": list(result.skipped),
            "result": {
                "query": query,
                "message_count": len(result.messages),
                "hits": [
                    {
                        "subject": h.subject,
                        "from": h.from_,
                        "date": h.date,
                        "message_id": h.message_id,
                        "snippet": h.snippet,
                        "match_field": h.match_field,
                        "match_count": h.match_count,
                        "has_attachments": h.has_attachments,
                        "attachments_count": h.attachments_count,
                    }
                    for h in hits
                ],
            },
        }
    )
    return EXIT_SUCCESS


def cmd_mail_draft(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output_mode = getattr(args, "output", "json")
    instruction = getattr(args, "instruction", None)
    if not instruction or not instruction.strip():
        sys.stderr.write("wolf cli: --instruction is required\n")
        return EXIT_DENIED

    try:
        llm = _build_llm_from_args(args)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED
    # Mail draft: strict (warning markers block). The mail body is
    # untrusted; the user instruction is trusted but routed through the
    # same prompt-injection scan via UntrustedText wrap of the mail.
    router = _build_router(
        project_root,
        llm=llm,
        config=RouterConfig(allow_warning_injection_findings=False),
    )

    gate = _gate_mail_path(
        router=router, path_str=args.path, output_mode=output_mode
    )
    if gate is not None:
        return _emit_decision(
            gate, output_mode=output_mode, include_result=False
        )

    path = Path(args.path)
    if not path.is_absolute():
        path = (project_root / path).resolve()
    try:
        result = _read_mail_with_args(args=args, path=path)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED
    except MailReadError as exc:
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: mail_read failed: {exc.label}\n")
        else:
            _print_json(
                {
                    "allowed": False,
                    "executed": False,
                    "requires_confirmation": False,
                    "stage": "mail_read",
                    "reason": f"mail_read failed: {exc.label}",
                    "provider_called": False,
                    "audit_event_id": None,
                    "failed_checks": [f"mail_read: {exc.label}"],
                    "warnings": [],
                }
            )
        return EXIT_DENIED

    msg_index = int(getattr(args, "message_index", 0))
    if not result.messages:
        if output_mode == "text":
            sys.stderr.write(
                "wolf cli: no messages match the filters\n"
            )
        else:
            _print_json(
                {
                    "allowed": False,
                    "executed": False,
                    "requires_confirmation": False,
                    "stage": "mail_read",
                    "reason": "no messages",
                    "provider_called": False,
                    "audit_event_id": None,
                    "failed_checks": ["mail-draft: no messages after filters"],
                    "warnings": list(result.skipped),
                }
            )
        return EXIT_DENIED
    if msg_index < 0 or msg_index >= len(result.messages):
        if output_mode == "text":
            sys.stderr.write(
                f"wolf cli: --message-index {msg_index} out of range "
                f"(0..{len(result.messages) - 1})\n"
            )
        else:
            _print_json(
                {
                    "allowed": False,
                    "executed": False,
                    "requires_confirmation": False,
                    "stage": "mail_read",
                    "reason": "message_index out of range",
                    "provider_called": False,
                    "audit_event_id": None,
                    "failed_checks": [
                        f"mail-draft: index {msg_index} of {len(result.messages)}"
                    ],
                    "warnings": list(result.skipped),
                }
            )
        return EXIT_DENIED

    pm = result.messages[msg_index]
    parts = build_draft_prompt(pm, instruction)
    # The Router sees the composed prompt (instruction + mail body
    # boundary text) wrapped as UntrustedText so the prompt-injection
    # scan applies to the mail content.
    composed = parts.composed + parts.mail_body
    body = wrap_untrusted(
        composed,
        SourceKind.EMAIL,
        source_ref=pm.message_id or str(path),
        metadata={
            "subject": pm.subject,
            "from": pm.from_,
            "byte_size": str(pm.byte_size),
        },
    )
    decision = router.route(
        RouterAction(
            kind=ActionKind.LLM_SUMMARIZE_EMAIL,
            risk_level=RiskLevel.LOW,
            body=body,
        )
    )

    if not decision.allowed or not isinstance(decision.result, str):
        return _emit_decision(
            decision, output_mode=output_mode, include_result=False
        )

    subject_suggestion = default_subject_for_reply(pm.subject)
    if output_mode == "text":
        sys.stdout.write(decision.result)
        if not decision.result.endswith("\n"):
            sys.stdout.write("\n")
        _emit_text_warning_count(len(decision.warnings))
        return EXIT_SUCCESS

    _print_json(
        {
            "allowed": True,
            "executed": True,
            "requires_confirmation": False,
            "stage": "complete",
            "reason": "draft generated",
            "provider_called": True,
            "audit_event_id": None,
            "failed_checks": [],
            "warnings": list(decision.warnings),
            "result": {
                "source_subject": pm.subject,
                "source_from": pm.from_,
                "source_message_id": pm.message_id,
                "source_has_attachments": pm.has_attachments,
                "source_attachments_count": len(pm.attachments),
                "subject_suggestion": subject_suggestion,
                "body": decision.result,
                "body_length": len(decision.result),
            },
        }
    )
    return EXIT_SUCCESS


def _thread_to_dict(t: Thread) -> dict:
    return {
        "thread_id": t.thread_id,
        "subject": t.subject,
        "message_count": t.message_count,
        "participants": list(t.participants),
        "first_date": t.first_date,
        "last_date": t.last_date,
        "messages": [
            {
                "index": m.index,
                "subject": m.subject,
                "from": m.from_,
                "date": m.date,
                "message_id": m.message_id,
            }
            for m in t.messages
        ],
    }


def cmd_mail_thread(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output_mode = getattr(args, "output", "json")

    try:
        llm = _build_llm_from_args(args)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED
    router = _build_router(project_root, llm=llm)

    gate = _gate_mail_path(
        router=router, path_str=args.path, output_mode=output_mode
    )
    if gate is not None:
        return _emit_decision(
            gate, output_mode=output_mode, include_result=False
        )

    path = Path(args.path)
    if not path.is_absolute():
        path = (project_root / path).resolve()
    try:
        result = _read_mail_with_args(args=args, path=path)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED
    except MailReadError as exc:
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: mail_read failed: {exc.label}\n")
        else:
            _print_json(
                {
                    "allowed": False,
                    "executed": False,
                    "requires_confirmation": False,
                    "stage": "mail_read",
                    "reason": f"mail_read failed: {exc.label}",
                    "provider_called": False,
                    "audit_event_id": None,
                    "failed_checks": [f"mail_read: {exc.label}"],
                    "warnings": [],
                }
            )
        return EXIT_DENIED

    threads = build_threads(result.messages)
    if not threads:
        if output_mode == "text":
            sys.stderr.write("wolf cli: no threads to report\n")
        else:
            _print_json(
                {
                    "allowed": True,
                    "executed": True,
                    "requires_confirmation": False,
                    "stage": "complete",
                    "reason": "no threads",
                    "provider_called": False,
                    "audit_event_id": None,
                    "failed_checks": [],
                    "warnings": list(result.skipped),
                    "result": {"thread_count": 0, "threads": []},
                }
            )
        return EXIT_DENIED

    if output_mode == "text":
        for t in threads:
            sys.stderr.write("")  # noop placeholder
            sys.stdout.write(
                f"[{t.message_count}] {t.subject} "
                f"({t.first_date} → {t.last_date}; "
                f"{len(t.participants)} participant(s))\n"
            )
        _emit_text_warning_count(len(result.skipped))
        return EXIT_SUCCESS

    _print_json(
        {
            "allowed": True,
            "executed": True,
            "requires_confirmation": False,
            "stage": "complete",
            "reason": f"{len(threads)} thread(s)",
            "provider_called": False,
            "audit_event_id": None,
            "failed_checks": [],
            "warnings": list(result.skipped),
            "result": {
                "thread_count": len(threads),
                "threads": [_thread_to_dict(t) for t in threads],
            },
        }
    )
    return EXIT_SUCCESS


def cmd_mail_search_summarize(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output_mode = getattr(args, "output", "json")
    query = getattr(args, "query", None)
    if not query:
        sys.stderr.write("wolf cli: --query is required\n")
        return EXIT_DENIED

    try:
        llm = _build_llm_from_args(args)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED
    # Mail is strict by default (warning markers block).
    router = _build_router(
        project_root,
        llm=llm,
        config=RouterConfig(allow_warning_injection_findings=False),
    )

    gate = _gate_mail_path(
        router=router, path_str=args.path, output_mode=output_mode
    )
    if gate is not None:
        return _emit_decision(
            gate, output_mode=output_mode, include_result=False
        )

    path = Path(args.path)
    if not path.is_absolute():
        path = (project_root / path).resolve()
    try:
        result = _read_mail_with_args(args=args, path=path)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED
    except MailReadError as exc:
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: mail_read failed: {exc.label}\n")
        else:
            _print_json(
                {
                    "allowed": False,
                    "executed": False,
                    "requires_confirmation": False,
                    "stage": "mail_read",
                    "reason": f"mail_read failed: {exc.label}",
                    "provider_called": False,
                    "audit_event_id": None,
                    "failed_checks": [f"mail_read: {exc.label}"],
                    "warnings": [],
                }
            )
        return EXIT_DENIED

    max_hits = int(getattr(args, "max_hits", MAIL_SEARCH_DEFAULT_MAX_HITS))
    hits = search_mail(result.messages, query, max_hits=max_hits)
    if not hits:
        payload = {
            "allowed": False,
            "executed": False,
            "requires_confirmation": False,
            "stage": "search",
            "reason": "no hits",
            "provider_called": False,
            "audit_event_id": None,
            "failed_checks": [],
            "warnings": list(result.skipped),
            "result": {
                "query": query,
                "hit_count": 0,
                "message_count": len(result.messages),
            },
        }
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: no mail hits for {query!r}\n")
        else:
            _print_json(payload)
        return EXIT_DENIED

    # Build a set of hit message_ids so we can pick the matching
    # ParsedMail objects (the search returns MailHit metadata only;
    # we need full bodies to summarize).
    threaded = bool(getattr(args, "threaded", False))
    include_per_message = bool(
        getattr(args, "include_per_message_summary", False)
    )

    if threaded:
        threads = build_threads(result.messages)
        # Pick threads that contain at least one hit.
        hit_msg_ids = {h.message_id for h in hits if h.message_id}
        hit_subjects = {h.subject.lower() for h in hits if h.subject}
        relevant_threads: List[Thread] = []
        for t in threads:
            in_thread = any(
                (m.message_id and m.message_id in hit_msg_ids)
                or (m.subject and m.subject.lower() in hit_subjects)
                for m in t.messages
            )
            if in_thread:
                relevant_threads.append(t)
        if not relevant_threads:
            if output_mode == "text":
                sys.stderr.write("wolf cli: no threads matched the hits\n")
            else:
                _print_json(
                    {
                        "allowed": False,
                        "executed": False,
                        "requires_confirmation": False,
                        "stage": "search",
                        "reason": "no threads matched",
                        "provider_called": False,
                        "audit_event_id": None,
                        "failed_checks": [],
                        "warnings": list(result.skipped),
                        "result": {
                            "query": query,
                            "hit_count": len(hits),
                            "summarized_count": 0,
                        },
                    }
                )
            return EXIT_DENIED

        # Summarize each thread by concatenating its messages' bodies.
        thread_records: List[dict] = []
        per_thread_summaries: List[str] = []
        warnings: List[str] = list(result.skipped)
        accepted = 0
        for t in relevant_threads:
            joined = "\n\n---\n\n".join(
                f"From: {tm.from_}\nSubject: {tm.subject}\nDate: {tm.date}\n\n"
                f"{result.messages[tm.index].body}"
                for tm in t.messages
            )
            body = wrap_untrusted(
                joined,
                SourceKind.EMAIL,
                source_ref=t.thread_id,
                metadata={
                    "subject": t.subject,
                    "message_count": str(t.message_count),
                },
            )
            decision = router.route(
                RouterAction(
                    kind=ActionKind.LLM_SUMMARIZE_EMAIL,
                    risk_level=RiskLevel.LOW,
                    body=body,
                )
            )
            if not decision.allowed or not isinstance(decision.result, str):
                warnings.append(
                    f"mail-search-summarize: skipped thread "
                    f"{t.thread_id!r} (stage={decision.stage})"
                )
                continue
            per_thread_summaries.append(decision.result)
            warnings.extend(decision.warnings)
            record = {
                "thread_id": t.thread_id,
                "subject": t.subject,
                "message_count": t.message_count,
                "participants": list(t.participants),
                "summary_length": len(decision.result),
            }
            if include_per_message:
                record["summary"] = decision.result
            thread_records.append(record)
            accepted += 1

        if accepted == 0:
            payload = {
                "allowed": False,
                "executed": False,
                "requires_confirmation": False,
                "stage": "search",
                "reason": "no threads could be summarized",
                "provider_called": True,
                "audit_event_id": None,
                "failed_checks": [
                    "mail-search-summarize: no eligible threads"
                ],
                "warnings": warnings,
                "result": {
                    "query": query,
                    "hit_count": len(hits),
                    "summarized_count": 0,
                    "skipped_count": len(relevant_threads),
                    "threads": [],
                },
            }
            if output_mode == "text":
                sys.stderr.write(
                    f"wolf cli: {len(relevant_threads)} thread(s) but none "
                    "could be summarized\n"
                )
            else:
                _print_json(payload)
            return EXIT_DENIED

        aggregated_text = "\n\n".join(per_thread_summaries)
        agg_body = wrap_untrusted(
            aggregated_text,
            SourceKind.EMAIL,
            source_ref=f"search:thread:{query}",
            metadata={
                "thread_count": str(len(relevant_threads)),
                "summarized_count": str(accepted),
            },
        )
        # Aggregate input is our own LLM output; critical markers still
        # block, but warning-level markers (e.g., "command", "tool call"
        # in the model's own preamble) should not derail the final
        # summary.
        agg_router = _build_router(
            project_root,
            llm=llm,
            config=RouterConfig(allow_warning_injection_findings=True),
        )
        final = agg_router.route(
            RouterAction(
                kind=ActionKind.LLM_SUMMARIZE_EMAIL,
                risk_level=RiskLevel.LOW,
                body=agg_body,
            )
        )
        if warnings:
            final = _decision_with_extra_warnings(final, warnings)

        if output_mode == "text":
            if final.allowed and isinstance(final.result, str):
                sys.stdout.write(final.result)
                if not final.result.endswith("\n"):
                    sys.stdout.write("\n")
                _emit_text_warning_count(len(final.warnings))
                return EXIT_SUCCESS
            sys.stderr.write(
                f"wolf cli: stage={final.stage} reason={final.reason}\n"
            )
            return _exit_code_for(final)

        payload = _decision_to_safe_dict(final, include_result=False)
        payload["result"] = {
            "query": query,
            "mode": "threaded",
            "hit_count": len(hits),
            "summarized_count": accepted,
            "skipped_count": len(relevant_threads) - accepted,
            "threads": thread_records,
        }
        if final.allowed and isinstance(final.result, str):
            payload["result"]["summary"] = final.result
        _print_json(payload)
        return _exit_code_for(final)

    # Default (non-threaded) path: per-message summary, then aggregate.
    hit_msg_ids = {h.message_id: h for h in hits if h.message_id}
    msg_records: List[dict] = []
    per_msg_summaries: List[str] = []
    warnings = list(result.skipped)
    accepted = 0
    for pm in result.messages:
        if pm.message_id not in hit_msg_ids:
            # Skip non-hit messages.
            continue
        hit = hit_msg_ids[pm.message_id]
        body = wrap_untrusted(
            pm.body,
            SourceKind.EMAIL,
            source_ref=pm.message_id or "",
            metadata={
                "subject": pm.subject,
                "from": pm.from_,
            },
        )
        decision = router.route(
            RouterAction(
                kind=ActionKind.LLM_SUMMARIZE_EMAIL,
                risk_level=RiskLevel.LOW,
                body=body,
            )
        )
        if not decision.allowed or not isinstance(decision.result, str):
            warnings.append(
                f"mail-search-summarize: skipped "
                f"{pm.message_id or pm.subject!r} (stage={decision.stage})"
            )
            continue
        per_msg_summaries.append(decision.result)
        warnings.extend(decision.warnings)
        record = {
            "message_id": pm.message_id,
            "subject": pm.subject,
            "from": pm.from_,
            "date": pm.date,
            "match_field": hit.match_field,
            "match_count": hit.match_count,
            "summary_length": len(decision.result),
        }
        if include_per_message:
            record["summary"] = decision.result
        msg_records.append(record)
        accepted += 1

    if accepted == 0:
        payload = {
            "allowed": False,
            "executed": False,
            "requires_confirmation": False,
            "stage": "search",
            "reason": "no messages could be summarized",
            "provider_called": True,
            "audit_event_id": None,
            "failed_checks": ["mail-search-summarize: no eligible messages"],
            "warnings": warnings,
            "result": {
                "query": query,
                "hit_count": len(hits),
                "summarized_count": 0,
                "skipped_count": len(hits),
                "messages": [],
            },
        }
        if output_mode == "text":
            sys.stderr.write(
                f"wolf cli: {len(hits)} hit(s) but none could be summarized\n"
            )
        else:
            _print_json(payload)
        return EXIT_DENIED

    aggregated_text = "\n\n".join(per_msg_summaries)
    agg_body = wrap_untrusted(
        aggregated_text,
        SourceKind.EMAIL,
        source_ref=f"search:mail:{query}",
        metadata={
            "hit_count": str(len(hits)),
            "summarized_count": str(accepted),
        },
    )
    # Aggregate over LLM-generated per-message summaries: warning-level
    # markers from the model's own preamble must not block.
    agg_router = _build_router(
        project_root,
        llm=llm,
        config=RouterConfig(allow_warning_injection_findings=True),
    )
    final = agg_router.route(
        RouterAction(
            kind=ActionKind.LLM_SUMMARIZE_EMAIL,
            risk_level=RiskLevel.LOW,
            body=agg_body,
        )
    )
    if warnings:
        final = _decision_with_extra_warnings(final, warnings)

    if output_mode == "text":
        if final.allowed and isinstance(final.result, str):
            sys.stdout.write(final.result)
            if not final.result.endswith("\n"):
                sys.stdout.write("\n")
            _emit_text_warning_count(len(final.warnings))
            return EXIT_SUCCESS
        sys.stderr.write(
            f"wolf cli: stage={final.stage} reason={final.reason}\n"
        )
        return _exit_code_for(final)

    payload = _decision_to_safe_dict(final, include_result=False)
    payload["result"] = {
        "query": query,
        "mode": "message",
        "hit_count": len(hits),
        "summarized_count": accepted,
        "skipped_count": len(hits) - accepted,
        "messages": msg_records,
    }
    if final.allowed and isinstance(final.result, str):
        payload["result"]["summary"] = final.result
    _print_json(payload)
    return _exit_code_for(final)


GMAIL_DEFAULT_LIMIT = 10
GMAIL_DEFAULT_BODY_PREVIEW_BYTES = 500


def _build_gmail_client_from_args(
    args: argparse.Namespace,
):
    """Build a Gmail client (real or fake) from CLI args.

    Raises GmailClientError for invalid configuration so the caller
    can convert it into a CLI exit-2 with a stage label.
    """
    backend = getattr(args, "gmail_backend", "fake")
    if backend == "fake":
        return FakeGmailClient()
    if backend == "gmail":
        creds_path = getattr(args, "credentials_path", None)
        if not creds_path:
            raise GmailClientError(
                "--gmail-backend gmail requires --credentials-path"
            )
        path = Path(creds_path)
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        creds = GmailCredentials.from_path(path)
        base_url = getattr(args, "gmail_base_url", None) or None
        kwargs = {}
        if base_url:
            kwargs["base_url"] = base_url
            if getattr(args, "allow_non_https_gmail", False):
                kwargs["allow_non_https"] = True
        return GmailClient(creds, **kwargs)
    raise GmailClientError(f"unsupported gmail backend: {backend!r}")


def _build_llm_for_gmail(args: argparse.Namespace) -> LLMAdapter:
    """Mirror of _build_llm_from_args but reads --llm-backend / --model
    from gmail subcommand args. The model flag is shared (--model)."""
    backend = getattr(args, "llm_backend", "fake")
    if backend == "fake":
        return FakeLLM()
    if backend == "ollama":
        from .adapters.ollama import (
            DEFAULT_BASE_URL as OLLAMA_DEFAULT_URL,
            OllamaLLMAdapter,
        )

        model = getattr(args, "model", None)
        if not model:
            raise ValueError(
                "--llm-backend ollama requires --model (e.g. --model llama3.1)"
            )
        base_url = getattr(args, "ollama_url", None) or OLLAMA_DEFAULT_URL
        allow_non_localhost = bool(
            getattr(args, "allow_non_localhost_ollama", False)
        )
        return OllamaLLMAdapter(
            model=model,
            base_url=base_url,
            allow_non_localhost=allow_non_localhost,
        )
    raise ValueError(f"unsupported llm backend: {backend!r}")


def _gmail_error_decision(label: str, *, stage: str = "gmail") -> Mapping[str, Any]:
    return {
        "allowed": False,
        "executed": False,
        "requires_confirmation": False,
        "stage": stage,
        "reason": label,
        "provider_called": False,
        "audit_event_id": None,
        "failed_checks": [label],
        "warnings": [],
    }


def _truncate_for_preview(text: str, *, max_bytes: int) -> str:
    """Return a UTF-8 bounded preview of `text` (best effort)."""
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    cut = encoded[:max_bytes]
    # Trim a possible split UTF-8 sequence by progressively shrinking.
    for back in range(0, 4):
        try:
            return cut[: len(cut) - back].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return cut.decode("utf-8", errors="replace")


def _search_hit_to_dict(h: GmailSearchHit) -> dict:
    return {"message_id": h.message_id, "thread_id": h.thread_id}


def _gmail_message_summary_record(m: GmailMessage) -> dict:
    """Body-less metadata record used in gmail-search output."""
    return {
        "message_id": m.message_id,
        "thread_id": m.thread_id,
        "subject": m.subject,
        "from": m.from_,
        "date": m.date,
        "snippet": m.snippet,
        "has_attachments": m.has_attachments,
        "attachments_count": len(m.attachments),
    }


def cmd_gmail_search(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output_mode = getattr(args, "output", "json")
    provider = getattr(args, "gmail_backend", "fake")
    try:
        client = _build_gmail_client_from_args(args)
    except GmailClientError as exc:
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: gmail config error: {exc.label}\n")
        else:
            _print_json(_gmail_error_decision(exc.label, stage="gmail_config"))
        return EXIT_DENIED

    try:
        hits = client.search(
            query=args.query,
            max_results=int(args.limit),
        )
    except GmailClientError as exc:
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: {exc.label}\n")
        else:
            _print_json(_gmail_error_decision(exc.label, stage="gmail_search"))
        return EXIT_DENIED

    # If --enrich-headers, read each hit to get subject/from/date/snippet.
    # Without it, the JSON only carries ids (which mirrors the raw Gmail
    # list endpoint). Enrichment is on by default because the bare list
    # is not usable.
    enrich = not getattr(args, "no_enrich", False)
    enriched_messages: List[dict] = []
    skip: List[str] = []
    if enrich:
        for h in hits:
            try:
                m = client.read(message_id=h.message_id)
            except GmailClientError as exc:
                skip.append(
                    f"gmail-search: skipped {h.message_id} ({exc.label})"
                )
                continue
            enriched_messages.append(_gmail_message_summary_record(m))

    # Audit the search (metadata only — query content never recorded).
    try:
        _audit_gmail_api_event(
            project_root=project_root,
            actor="cli:gmail-search",
            action_kind="gmail.search",
            target="gmail:search",
            outcome="search_complete",
            decision="allow",
            detail={
                "provider": provider,
                "query_length": len(args.query or ""),
                "query_fingerprint": _query_fingerprint(args.query),
                "max_results": int(args.limit),
                "hit_count": len(hits),
                "enriched_count": len(enriched_messages),
                "skipped_count": len(skip),
            },
        )
    except OSError as exc:
        return _emit_audit_fail_closed(
            output_mode=output_mode,
            action_kind="gmail.search",
            exc=exc,
        )

    if output_mode == "text":
        if not hits:
            sys.stdout.write("(no hits)\n")
            return EXIT_SUCCESS
        for rec in enriched_messages if enrich else [_search_hit_to_dict(h) for h in hits]:
            line_parts = [
                rec.get("message_id", ""),
                rec.get("subject", ""),
                rec.get("from", ""),
            ]
            sys.stdout.write("\t".join(line_parts) + "\n")
        return EXIT_SUCCESS

    payload = {
        "allowed": True,
        "executed": True,
        "requires_confirmation": False,
        "stage": "complete",
        "reason": "gmail search complete",
        "provider_called": True,
        "audit_event_id": None,
        "failed_checks": [],
        "warnings": skip,
        "result": {
            "query": args.query,
            "hit_count": len(hits),
            "hits": [_search_hit_to_dict(h) for h in hits],
            "messages": enriched_messages,
        },
    }
    _print_json(payload)
    return EXIT_SUCCESS


def cmd_gmail_read(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output_mode = getattr(args, "output", "json")
    provider = getattr(args, "gmail_backend", "fake")
    try:
        client = _build_gmail_client_from_args(args)
    except GmailClientError as exc:
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: gmail config error: {exc.label}\n")
        else:
            _print_json(_gmail_error_decision(exc.label, stage="gmail_config"))
        return EXIT_DENIED

    try:
        msg = client.read(message_id=args.message_id)
    except GmailClientError as exc:
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: {exc.label}\n")
        else:
            _print_json(_gmail_error_decision(exc.label, stage="gmail_read"))
        return EXIT_DENIED

    # Audit the read (metadata only).
    try:
        _audit_gmail_api_event(
            project_root=project_root,
            actor="cli:gmail-read",
            action_kind="gmail.read",
            target=f"gmail:{msg.message_id}",
            outcome="read_complete",
            decision="allow",
            detail={
                "provider": provider,
                "message_id": msg.message_id,
                "thread_id": msg.thread_id,
                "subject": msg.subject,
                "has_attachments": msg.has_attachments,
                "attachments_count": len(msg.attachments),
                "body_total_bytes": len(msg.body_text.encode("utf-8")),
            },
        )
    except OSError as exc:
        return _emit_audit_fail_closed(
            output_mode=output_mode,
            action_kind="gmail.read",
            exc=exc,
        )

    preview_bytes = int(getattr(args, "body_preview_bytes", GMAIL_DEFAULT_BODY_PREVIEW_BYTES))
    preview = _truncate_for_preview(msg.body_text, max_bytes=preview_bytes)
    truncated = len(msg.body_text.encode("utf-8")) > preview_bytes

    if output_mode == "text":
        sys.stdout.write(f"Subject: {msg.subject}\n")
        sys.stdout.write(f"From: {msg.from_}\n")
        sys.stdout.write(f"Date: {msg.date}\n")
        sys.stdout.write("\n")
        sys.stdout.write(preview)
        if not preview.endswith("\n"):
            sys.stdout.write("\n")
        if truncated:
            sys.stdout.write("[... body truncated ...]\n")
        return EXIT_SUCCESS

    payload = {
        "allowed": True,
        "executed": True,
        "requires_confirmation": False,
        "stage": "complete",
        "reason": "gmail read complete",
        "provider_called": True,
        "audit_event_id": None,
        "failed_checks": [],
        "warnings": [],
        "result": {
            "message_id": msg.message_id,
            "thread_id": msg.thread_id,
            "subject": msg.subject,
            "from": msg.from_,
            "to": msg.to,
            "cc": msg.cc,
            "date": msg.date,
            "rfc822_message_id": msg.rfc822_message_id,
            "snippet": msg.snippet,
            "label_ids": list(msg.label_ids),
            "has_attachments": msg.has_attachments,
            "attachments_count": len(msg.attachments),
            "attachments": [
                {
                    "filename": a.filename,
                    "mime_type": a.mime_type,
                    "size_bytes": a.size_bytes,
                }
                for a in msg.attachments
            ],
            "body_preview": preview,
            "body_preview_bytes": len(preview.encode("utf-8")),
            "body_total_bytes": len(msg.body_text.encode("utf-8")),
            "body_truncated": truncated,
        },
    }
    _print_json(payload)
    return EXIT_SUCCESS


def cmd_gmail_summarize(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output_mode = getattr(args, "output", "json")

    try:
        client = _build_gmail_client_from_args(args)
    except GmailClientError as exc:
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: gmail config error: {exc.label}\n")
        else:
            _print_json(_gmail_error_decision(exc.label, stage="gmail_config"))
        return EXIT_DENIED

    try:
        llm = _build_llm_for_gmail(args)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED

    # Mail-strict Router (warning markers block).
    router = _build_router(
        project_root,
        llm=llm,
        config=RouterConfig(allow_warning_injection_findings=False),
    )

    query = getattr(args, "query", None)
    message_id = getattr(args, "message_id", None)
    if not query and not message_id:
        sys.stderr.write(
            "wolf cli: gmail-summarize requires --query or --message-id\n"
        )
        return EXIT_DENIED

    # Resolve target message ids.
    target_ids: List[str] = []
    if message_id:
        target_ids = [message_id]
    else:
        try:
            hits = client.search(
                query=query, max_results=int(args.limit)
            )
        except GmailClientError as exc:
            if output_mode == "text":
                sys.stderr.write(f"wolf cli: {exc.label}\n")
            else:
                _print_json(
                    _gmail_error_decision(exc.label, stage="gmail_search")
                )
            return EXIT_DENIED
        target_ids = [h.message_id for h in hits]

    if not target_ids:
        if output_mode == "text":
            sys.stderr.write("wolf cli: no gmail messages match\n")
        else:
            _print_json(
                {
                    "allowed": False,
                    "executed": False,
                    "requires_confirmation": False,
                    "stage": "gmail_search",
                    "reason": "no messages",
                    "provider_called": False,
                    "audit_event_id": None,
                    "failed_checks": [],
                    "warnings": [],
                }
            )
        return EXIT_DENIED

    summaries: List[dict] = []
    warnings: List[str] = []
    for mid in target_ids:
        try:
            m = client.read(message_id=mid)
        except GmailClientError as exc:
            warnings.append(f"gmail-summarize: skipped {mid} ({exc.label})")
            continue
        body = wrap_untrusted(
            m.body_text,
            SourceKind.EMAIL,
            source_ref=m.rfc822_message_id or m.message_id,
            metadata={
                "subject": m.subject,
                "from": m.from_,
                "gmail_message_id": m.message_id,
            },
        )
        decision = router.route(
            RouterAction(
                kind=ActionKind.LLM_SUMMARIZE_EMAIL,
                risk_level=RiskLevel.LOW,
                body=body,
            )
        )
        if not decision.allowed or not isinstance(decision.result, str):
            warnings.append(
                f"gmail-summarize: skipped {m.message_id} "
                f"(stage={decision.stage})"
            )
            continue
        summaries.append(
            {
                "message_id": m.message_id,
                "thread_id": m.thread_id,
                "subject": m.subject,
                "from": m.from_,
                "date": m.date,
                "summary": decision.result,
                "summary_length": len(decision.result),
            }
        )

    provider = getattr(args, "gmail_backend", "fake")
    llm_backend = getattr(args, "llm_backend", "fake")
    searched_count = 0 if message_id else len(target_ids)
    input_mode = "message_id" if message_id else "query"

    # Audit the summarize stage (metadata only; query text is
    # fingerprinted, not stored).
    try:
        _audit_gmail_api_event(
            project_root=project_root,
            actor="cli:gmail-summarize",
            action_kind="gmail.summarize",
            target=(
                f"gmail-summarize:{message_id}"
                if message_id else "gmail-summarize:search"
            ),
            outcome=(
                "summarize_complete"
                if summaries else "summarize_empty"
            ),
            decision="allow" if summaries else "deny",
            detail={
                "provider": provider,
                "llm_backend": llm_backend,
                "input_mode": input_mode,
                "query_length": len(query or ""),
                "query_fingerprint": _query_fingerprint(query),
                "searched_count": searched_count,
                "read_count": len(target_ids),
                "summarized_count": len(summaries),
            },
        )
    except OSError as exc:
        return _emit_audit_fail_closed(
            output_mode=output_mode,
            action_kind="gmail.summarize",
            exc=exc,
        )

    if output_mode == "text":
        if not summaries:
            sys.stderr.write("wolf cli: no summaries produced\n")
            return EXIT_DENIED
        for s in summaries:
            sys.stdout.write(f"# {s['subject']} ({s['from']})\n")
            sys.stdout.write(s["summary"])
            if not s["summary"].endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.write("\n")
        return EXIT_SUCCESS

    _print_json(
        {
            "allowed": bool(summaries),
            "executed": bool(summaries),
            "requires_confirmation": False,
            "stage": "complete" if summaries else "complete",
            "reason": (
                "gmail summarize complete"
                if summaries
                else "no summaries produced"
            ),
            "provider_called": True,
            "audit_event_id": None,
            "failed_checks": [] if summaries else ["gmail-summarize: all messages skipped"],
            "warnings": warnings,
            "result": {
                "message_count": len(target_ids),
                "summarized_count": len(summaries),
                "summaries": summaries,
            },
        }
    )
    return EXIT_SUCCESS if summaries else EXIT_DENIED


def cmd_gmail_draft(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output_mode = getattr(args, "output", "json")
    instruction = getattr(args, "instruction", None)
    if not instruction or not instruction.strip():
        sys.stderr.write("wolf cli: --instruction is required\n")
        return EXIT_DENIED

    try:
        client = _build_gmail_client_from_args(args)
    except GmailClientError as exc:
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: gmail config error: {exc.label}\n")
        else:
            _print_json(_gmail_error_decision(exc.label, stage="gmail_config"))
        return EXIT_DENIED

    try:
        llm = _build_llm_for_gmail(args)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED

    router = _build_router(
        project_root,
        llm=llm,
        config=RouterConfig(allow_warning_injection_findings=False),
    )

    try:
        msg = client.read(message_id=args.message_id)
    except GmailClientError as exc:
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: {exc.label}\n")
        else:
            _print_json(_gmail_error_decision(exc.label, stage="gmail_read"))
        return EXIT_DENIED

    # Re-use the local mail draft prompt composer: it builds the same
    # bilingual instruction-vs-untrusted-body boundary. We synthesize a
    # minimal ParsedMail-shaped object on the fly because the composer
    # only consumes .subject / .from_ / .body / .message_id.
    composer_view = ParsedMail(
        subject=msg.subject,
        from_=msg.from_,
        to=msg.to,
        cc=msg.cc,
        date=msg.date,
        message_id=msg.rfc822_message_id or msg.message_id,
        body=msg.body_text,
        has_attachments=msg.has_attachments,
        content_type="text/plain",
        byte_size=len(msg.body_text.encode("utf-8")),
    )
    parts = build_draft_prompt(composer_view, instruction)
    composed = parts.composed + parts.mail_body
    body = wrap_untrusted(
        composed,
        SourceKind.EMAIL,
        source_ref=msg.rfc822_message_id or msg.message_id,
        metadata={
            "subject": msg.subject,
            "from": msg.from_,
            "gmail_message_id": msg.message_id,
        },
    )
    decision = router.route(
        RouterAction(
            kind=ActionKind.LLM_SUMMARIZE_EMAIL,
            risk_level=RiskLevel.LOW,
            body=body,
        )
    )
    if not decision.allowed or not isinstance(decision.result, str):
        return _emit_decision(
            decision, output_mode=output_mode, include_result=False
        )

    draft_body = decision.result
    subject_suggestion = default_subject_for_reply(msg.subject)

    provider = getattr(args, "gmail_backend", "fake")

    # Create the draft on Gmail (fake or real). Sending is never called.
    try:
        draft = client.create_draft(
            to=msg.from_,
            source_subject=msg.subject,
            body=draft_body,
            in_reply_to=msg.rfc822_message_id,
            references=msg.rfc822_message_id,
            thread_id=msg.thread_id,
        )
    except GmailClientError as exc:
        # Audit the failed attempt so we can correlate stage transitions.
        try:
            _audit_gmail_draft_event(
                project_root=project_root,
                provider=provider,
                source_message=msg,
                draft=None,
                subject_suggestion=subject_suggestion,
                draft_body_length=len(draft_body.encode("utf-8")),
                outcome="draft_failed",
                detail_extra={"error_label": exc.label},
            )
        except OSError:
            pass
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: {exc.label}\n")
        else:
            _print_json(
                _gmail_error_decision(exc.label, stage="gmail_create_draft")
            )
        return EXIT_DENIED

    # Audit the successful create_draft. Fail-closed: if the audit
    # write fails (disk full, permission, etc.) we treat the action as
    # unrecorded and report an audit_log stage error. The draft has
    # already been created on the Gmail side; we surface that fact
    # in the error label so the user knows to clean up.
    try:
        _audit_gmail_draft_event(
            project_root=project_root,
            provider=provider,
            source_message=msg,
            draft=draft,
            subject_suggestion=subject_suggestion,
            draft_body_length=len(draft_body.encode("utf-8")),
            outcome="draft_created",
        )
    except OSError as exc:
        if output_mode == "text":
            sys.stderr.write(
                "wolf cli: audit_log failed after create_draft; "
                f"draft_id={draft.draft_id} ({type(exc).__name__})\n"
            )
        else:
            _print_json(
                {
                    "allowed": False,
                    "executed": True,
                    "requires_confirmation": False,
                    "stage": "audit_log",
                    "reason": (
                        f"audit_log failed after create_draft "
                        f"(draft_id={draft.draft_id})"
                    ),
                    "provider_called": True,
                    "audit_event_id": None,
                    "failed_checks": [
                        f"audit_log: {type(exc).__name__}"
                    ],
                    "warnings": [],
                }
            )
        return EXIT_DENIED

    preview_bytes = int(
        getattr(args, "body_preview_bytes", GMAIL_DEFAULT_BODY_PREVIEW_BYTES)
    )
    body_preview = _truncate_for_preview(draft_body, max_bytes=preview_bytes)

    if output_mode == "text":
        sys.stdout.write(draft_body)
        if not draft_body.endswith("\n"):
            sys.stdout.write("\n")
        return EXIT_SUCCESS

    _print_json(
        {
            "allowed": True,
            "executed": True,
            "requires_confirmation": False,
            "stage": "complete",
            "reason": "gmail draft created (not sent)",
            "provider_called": True,
            "audit_event_id": None,
            "failed_checks": [],
            "warnings": list(decision.warnings),
            "result": {
                "draft_id": draft.draft_id,
                "message_id": draft.message_id,
                "thread_id": draft.thread_id,
                "source_message_id": msg.message_id,
                "source_subject": msg.subject,
                "source_from": msg.from_,
                "subject_suggestion": subject_suggestion,
                "body_preview": body_preview,
                "body_preview_bytes": len(body_preview.encode("utf-8")),
                "body_total_bytes": len(draft_body.encode("utf-8")),
                "body_truncated": (
                    len(draft_body.encode("utf-8")) > preview_bytes
                ),
                "provider": provider,
            },
        }
    )
    return EXIT_SUCCESS


def _audit_gmail_draft_event(
    *,
    project_root: Path,
    provider: str,
    source_message: GmailMessage,
    draft: Optional[GmailDraft],
    subject_suggestion: str,
    draft_body_length: int,
    outcome: str,
    detail_extra: Optional[Mapping[str, Any]] = None,
) -> None:
    """Write one AuditEvent for a gmail-draft create_draft attempt.

    NEVER includes the draft body, the source mail body, or the
    Gmail access token. AuditLogger's _mask() additionally redacts
    any key matching the 'token' / 'credential' / 'secret' family
    so an accidental field would be redacted on write.
    """
    from .core.audit import utc_now_iso
    from .core.types import AuditEvent

    audit = AuditLogger(_default_audit_path(project_root.resolve()))
    detail: Dict[str, Any] = {
        "provider": provider,
        "source_message_id": source_message.message_id,
        "source_thread_id": source_message.thread_id,
        "source_subject": source_message.subject,
        "source_from": source_message.from_,
        "source_rfc822_message_id": source_message.rfc822_message_id,
        "subject_suggestion": subject_suggestion,
        "draft_body_length": int(draft_body_length),
    }
    if draft is not None:
        detail["draft_id"] = draft.draft_id
        detail["draft_message_id"] = draft.message_id
        detail["draft_thread_id"] = draft.thread_id
    if detail_extra:
        detail.update(detail_extra)
    event = AuditEvent(
        ts=utc_now_iso(),
        actor="cli:gmail-draft",
        action_kind="gmail.create_draft",
        decision="allow" if outcome == "draft_created" else "deny",
        target=f"gmail:{source_message.message_id}",
        outcome=outcome,
        detail=detail,
    )
    audit.log(event)


def _audit_gmail_api_event(
    *,
    project_root: Path,
    actor: str,
    action_kind: str,
    target: str,
    outcome: str,
    decision: str,
    detail: Mapping[str, Any],
) -> None:
    """Write one AuditEvent for a non-draft Gmail API call.

    NEVER includes raw mail bodies, query *content*, or the access
    token. Callers should pass only metadata (counts, lengths,
    ids, backend name, stage). AuditLogger._mask() additionally
    redacts any key matching the token / credential / secret /
    auth family.
    """
    from .core.audit import utc_now_iso
    from .core.types import AuditEvent

    audit = AuditLogger(_default_audit_path(project_root.resolve()))
    event = AuditEvent(
        ts=utc_now_iso(),
        actor=actor,
        action_kind=action_kind,
        decision=decision,
        target=target,
        outcome=outcome,
        detail=dict(detail),
    )
    audit.log(event)


def _query_fingerprint(query: Optional[str]) -> Optional[str]:
    """Stable 12-hex fingerprint of a search query string.

    Used in Gmail audit events to correlate runs WITHOUT recording
    the raw query content. Returns None for None / empty / pure
    whitespace queries (so an audit consumer can distinguish "no
    query" from a real fingerprint).

    This is a correlation key, NOT a privacy primitive — the
    fingerprint is reversible for short / well-known query strings
    via a rainbow table. Treat audit consumers as trusted and the
    fingerprint as "good enough to dedupe runs", nothing stronger.
    """
    import hashlib

    if query is None:
        return None
    stripped = query.strip()
    if not stripped:
        return None
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:12]


def _emit_audit_fail_closed(
    *,
    output_mode: str,
    action_kind: str,
    exc: BaseException,
) -> int:
    """Render the fail-closed JSON for a gmail-* audit OSError."""
    label = f"audit_log failed: {action_kind} ({type(exc).__name__})"
    if output_mode == "text":
        sys.stderr.write(f"wolf cli: {label}\n")
    else:
        _print_json(
            {
                "allowed": False,
                "executed": False,
                "requires_confirmation": False,
                "stage": "audit_log",
                "reason": label,
                "provider_called": True,
                "audit_event_id": None,
                "failed_checks": [label],
                "warnings": [],
            }
        )
    return EXIT_DENIED


def _gmail_thread_to_dict(t: GmailThread) -> dict:
    return {
        "thread_id": t.thread_id,
        "subject": t.subject,
        "message_count": t.message_count,
        "participants": list(t.participants),
        "first_date": t.first_date,
        "last_date": t.last_date,
        "messages": [
            {
                "index": m.index,
                "gmail_message_id": m.gmail_message_id,
                "rfc822_message_id": m.rfc822_message_id,
                "subject": m.subject,
                "from": m.from_,
                "date": m.date,
            }
            for m in t.messages
        ],
    }


def _resolve_gmail_targets(
    *,
    client,
    args: argparse.Namespace,
) -> "tuple[List[str], List[str]]":
    """Resolve the set of message ids to operate on.

    Returns (ids, warnings). Either --message-id (single) or --query
    (search → ids) is required. The query path honors --limit.
    """
    message_id = getattr(args, "message_id", None)
    query = getattr(args, "query", None)
    if message_id:
        return [message_id], []
    if not query:
        return [], []
    hits = client.search(query=query, max_results=int(args.limit))
    return [h.message_id for h in hits], []


def _read_gmail_messages(
    *,
    client,
    ids: Sequence[str],
) -> "tuple[List[GmailMessage], List[str]]":
    """Read each id; collect successes and per-id skip warnings."""
    out: List[GmailMessage] = []
    warnings: List[str] = []
    for mid in ids:
        try:
            out.append(client.read(message_id=mid))
        except GmailClientError as exc:
            warnings.append(f"gmail: skipped {mid} ({exc.label})")
    return out, warnings


def cmd_gmail_thread(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output_mode = getattr(args, "output", "json")
    provider = getattr(args, "gmail_backend", "fake")
    try:
        client = _build_gmail_client_from_args(args)
    except GmailClientError as exc:
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: gmail config error: {exc.label}\n")
        else:
            _print_json(_gmail_error_decision(exc.label, stage="gmail_config"))
        return EXIT_DENIED

    thread_id = getattr(args, "thread_id", None)
    query = getattr(args, "query", None)
    message_id = getattr(args, "message_id", None)
    inputs_set = sum(1 for v in (thread_id, query, message_id) if v)
    if inputs_set == 0:
        sys.stderr.write(
            "wolf cli: gmail-thread requires --query, --message-id, or --thread-id\n"
        )
        return EXIT_DENIED
    if inputs_set > 1:
        sys.stderr.write(
            "wolf cli: gmail-thread accepts only one of "
            "--query / --message-id / --thread-id\n"
        )
        return EXIT_DENIED

    # Direct thread-id path: skip search/read and go straight to
    # client.get_thread.
    if thread_id:
        try:
            members = client.get_thread(thread_id=thread_id)
        except GmailClientError as exc:
            if output_mode == "text":
                sys.stderr.write(f"wolf cli: {exc.label}\n")
            else:
                _print_json(
                    _gmail_error_decision(exc.label, stage="gmail_get_thread")
                )
            return EXIT_DENIED
        messages = list(members)
        skip: List[str] = []
    else:
        try:
            ids, _ = _resolve_gmail_targets(client=client, args=args)
        except GmailClientError as exc:
            if output_mode == "text":
                sys.stderr.write(f"wolf cli: {exc.label}\n")
            else:
                _print_json(_gmail_error_decision(exc.label, stage="gmail_search"))
            return EXIT_DENIED

        messages, skip = _read_gmail_messages(client=client, ids=ids)

    if not messages:
        if output_mode == "text":
            sys.stderr.write("wolf cli: no gmail messages found\n")
        else:
            _print_json(
                {
                    "allowed": False,
                    "executed": False,
                    "requires_confirmation": False,
                    "stage": "gmail_read",
                    "reason": "no messages",
                    "provider_called": False,
                    "audit_event_id": None,
                    "failed_checks": [],
                    "warnings": skip,
                }
            )
        return EXIT_DENIED

    threads = build_gmail_threads(messages)
    if not threads:
        if output_mode == "text":
            sys.stderr.write("wolf cli: no threads built\n")
        else:
            _print_json(
                {
                    "allowed": False,
                    "executed": False,
                    "requires_confirmation": False,
                    "stage": "gmail_thread",
                    "reason": "no threads",
                    "provider_called": False,
                    "audit_event_id": None,
                    "failed_checks": [],
                    "warnings": skip,
                }
            )
        return EXIT_DENIED

    if thread_id:
        input_mode = "thread_id"
    elif message_id:
        input_mode = "message_id"
    else:
        input_mode = "query"

    try:
        _audit_gmail_api_event(
            project_root=project_root,
            actor="cli:gmail-thread",
            action_kind="gmail.thread",
            target=(
                f"gmail-thread:{thread_id}"
                if thread_id else "gmail-thread:search"
            ),
            outcome="thread_complete",
            decision="allow",
            detail={
                "provider": provider,
                "input_mode": input_mode,
                "query_length": len(query or ""),
                "query_fingerprint": _query_fingerprint(query),
                "message_count": len(messages),
                "thread_count": len(threads),
                "skipped_count": len(skip),
            },
        )
    except OSError as exc:
        return _emit_audit_fail_closed(
            output_mode=output_mode,
            action_kind="gmail.thread",
            exc=exc,
        )

    if output_mode == "text":
        for t in threads:
            sys.stdout.write(
                f"{t.thread_id}\t{t.subject}\tcount={t.message_count}\t"
                f"participants={len(t.participants)}\n"
            )
        return EXIT_SUCCESS

    _print_json(
        {
            "allowed": True,
            "executed": True,
            "requires_confirmation": False,
            "stage": "complete",
            "reason": "gmail thread build complete",
            "provider_called": True,
            "audit_event_id": None,
            "failed_checks": [],
            "warnings": skip,
            "result": {
                "message_count": len(messages),
                "thread_count": len(threads),
                "threads": [_gmail_thread_to_dict(t) for t in threads],
            },
        }
    )
    return EXIT_SUCCESS


def cmd_gmail_search_summarize(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output_mode = getattr(args, "output", "json")
    provider = getattr(args, "gmail_backend", "fake")
    llm_backend = getattr(args, "llm_backend", "fake")

    try:
        client = _build_gmail_client_from_args(args)
    except GmailClientError as exc:
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: gmail config error: {exc.label}\n")
        else:
            _print_json(_gmail_error_decision(exc.label, stage="gmail_config"))
        return EXIT_DENIED

    try:
        llm = _build_llm_for_gmail(args)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED

    thread_id = getattr(args, "thread_id", None)
    query = getattr(args, "query", None)
    message_id = getattr(args, "message_id", None)
    inputs_set = sum(1 for v in (thread_id, query, message_id) if v)
    if inputs_set == 0:
        sys.stderr.write(
            "wolf cli: gmail-search-summarize requires "
            "--query, --message-id, or --thread-id\n"
        )
        return EXIT_DENIED
    if inputs_set > 1:
        sys.stderr.write(
            "wolf cli: gmail-search-summarize accepts only one of "
            "--query / --message-id / --thread-id\n"
        )
        return EXIT_DENIED

    if thread_id:
        input_mode = "thread_id"
    elif message_id:
        input_mode = "message_id"
    else:
        input_mode = "query"

    # Mail-strict Router (warning markers block) for per-message summary.
    router = _build_router(
        project_root,
        llm=llm,
        config=RouterConfig(allow_warning_injection_findings=False),
    )

    # Resolve targets. The --thread-id path skips search entirely.
    if thread_id:
        try:
            members = client.get_thread(thread_id=thread_id)
        except GmailClientError as exc:
            if output_mode == "text":
                sys.stderr.write(f"wolf cli: {exc.label}\n")
            else:
                _print_json(
                    _gmail_error_decision(exc.label, stage="gmail_get_thread")
                )
            return EXIT_DENIED
        messages = list(members)
        skip: List[str] = []
        searched_count = 0
    else:
        try:
            ids, _ = _resolve_gmail_targets(client=client, args=args)
        except GmailClientError as exc:
            if output_mode == "text":
                sys.stderr.write(f"wolf cli: {exc.label}\n")
            else:
                _print_json(_gmail_error_decision(exc.label, stage="gmail_search"))
            return EXIT_DENIED
        searched_count = len(ids)
        messages, skip = _read_gmail_messages(client=client, ids=ids)

    if not messages:
        if output_mode == "text":
            sys.stderr.write("wolf cli: no gmail messages found\n")
        else:
            _print_json(
                {
                    "allowed": False,
                    "executed": False,
                    "requires_confirmation": False,
                    "stage": "gmail_search",
                    "reason": "no messages",
                    "provider_called": False,
                    "audit_event_id": None,
                    "failed_checks": [],
                    "warnings": skip,
                }
            )
        return EXIT_DENIED

    threaded = bool(getattr(args, "threaded", False)) or bool(thread_id)
    include_per_msg = bool(getattr(args, "include_per_message_summary", False))
    include_per_thread = bool(getattr(args, "include_per_thread_summary", False))

    per_message_summaries: List[dict] = []
    per_message_summary_texts: List[str] = []
    summarized_count = 0
    warnings: List[str] = list(skip)

    for m in messages:
        body = wrap_untrusted(
            m.body_text,
            SourceKind.EMAIL,
            source_ref=m.rfc822_message_id or m.message_id,
            metadata={
                "subject": m.subject,
                "from": m.from_,
                "gmail_message_id": m.message_id,
            },
        )
        decision = router.route(
            RouterAction(
                kind=ActionKind.LLM_SUMMARIZE_EMAIL,
                risk_level=RiskLevel.LOW,
                body=body,
            )
        )
        if not decision.allowed or not isinstance(decision.result, str):
            warnings.append(
                f"gmail-search-summarize: skipped {m.message_id} "
                f"(stage={decision.stage})"
            )
            continue
        rec = {
            "message_id": m.message_id,
            "thread_id": m.thread_id,
            "subject": m.subject,
            "from": m.from_,
            "date": m.date,
            "summary_length": len(decision.result),
        }
        if include_per_msg:
            rec["summary"] = decision.result
        per_message_summaries.append(rec)
        per_message_summary_texts.append(decision.result)
        summarized_count += 1

    if summarized_count == 0:
        if output_mode == "text":
            sys.stderr.write("wolf cli: no summaries produced\n")
        else:
            _print_json(
                {
                    "allowed": False,
                    "executed": False,
                    "requires_confirmation": False,
                    "stage": "gmail_summarize",
                    "reason": "no summaries produced",
                    "provider_called": True,
                    "audit_event_id": None,
                    "failed_checks": [
                        "gmail-search-summarize: all messages skipped"
                    ],
                    "warnings": warnings,
                }
            )
        return EXIT_DENIED

    # Build thread-level summaries if requested.
    thread_summaries: List[dict] = []
    per_thread_summary_texts: List[str] = []
    if threaded:
        # Re-summarize per-thread by concatenating member bodies.
        threads = build_gmail_threads(messages)
        by_id = {m.message_id: m for m in messages}
        for t in threads:
            concatenated_parts: List[str] = []
            for tm in t.messages:
                gm = by_id.get(tm.gmail_message_id)
                if gm is None:
                    continue
                header = (
                    f"--- from: {gm.from_} date: {gm.date} subject: {gm.subject} ---\n"
                )
                concatenated_parts.append(header + (gm.body_text or ""))
            if not concatenated_parts:
                continue
            concatenated = "\n\n".join(concatenated_parts)
            body = wrap_untrusted(
                concatenated,
                SourceKind.EMAIL,
                source_ref=t.thread_id,
                metadata={
                    "subject": t.subject,
                    "message_count": str(t.message_count),
                },
            )
            decision = router.route(
                RouterAction(
                    kind=ActionKind.LLM_SUMMARIZE_EMAIL,
                    risk_level=RiskLevel.LOW,
                    body=body,
                )
            )
            if not decision.allowed or not isinstance(decision.result, str):
                warnings.append(
                    f"gmail-search-summarize: skipped thread {t.thread_id} "
                    f"(stage={decision.stage})"
                )
                continue
            tr = {
                "thread_id": t.thread_id,
                "subject": t.subject,
                "message_count": t.message_count,
                "participants": list(t.participants),
                "summary_length": len(decision.result),
            }
            if include_per_thread:
                tr["summary"] = decision.result
            thread_summaries.append(tr)
            per_thread_summary_texts.append(decision.result)

    # Aggregate step: re-feed the per-message (or per-thread) summary
    # texts into the Router for a one-paragraph aggregate. The input
    # here is LLM-generated text and may contain benign warning
    # markers (e.g. the literal word "command"); allow warnings.
    aggregate_router = _build_router(
        project_root,
        llm=llm,
        config=RouterConfig(allow_warning_injection_findings=True),
    )
    aggregate_inputs: List[str] = (
        per_thread_summary_texts if threaded else per_message_summary_texts
    )

    aggregate_summary = ""
    if aggregate_inputs:
        joined = "\n\n".join(aggregate_inputs)
        body = wrap_untrusted(
            joined,
            SourceKind.EMAIL,
            source_ref="aggregate",
            metadata={"role": "per-summary-aggregate"},
        )
        decision = aggregate_router.route(
            RouterAction(
                kind=ActionKind.LLM_SUMMARIZE_EMAIL,
                risk_level=RiskLevel.LOW,
                body=body,
            )
        )
        if decision.allowed and isinstance(decision.result, str):
            aggregate_summary = decision.result
        else:
            warnings.append(
                f"gmail-search-summarize: aggregate skipped (stage={decision.stage})"
            )

    # Audit the search-summarize stage (metadata only).
    try:
        _audit_gmail_api_event(
            project_root=project_root,
            actor="cli:gmail-search-summarize",
            action_kind="gmail.search_summarize",
            target=(
                f"gmail-thread:{thread_id}"
                if thread_id
                else "gmail-search-summarize"
            ),
            outcome=(
                "summarize_complete"
                if summarized_count > 0
                else "summarize_empty"
            ),
            decision="allow" if summarized_count > 0 else "deny",
            detail={
                "provider": provider,
                "llm_backend": llm_backend,
                "input_mode": input_mode,
                "query_length": len(query or ""),
                "query_fingerprint": _query_fingerprint(query),
                "searched_count": searched_count,
                "read_count": len(messages),
                "summarized_count": summarized_count,
                "threaded": bool(threaded),
                "thread_count": (
                    len(thread_summaries) if threaded else 0
                ),
                "aggregate_summary_length": len(aggregate_summary or ""),
            },
        )
    except OSError as exc:
        return _emit_audit_fail_closed(
            output_mode=output_mode,
            action_kind="gmail.search_summarize",
            exc=exc,
        )

    if output_mode == "text":
        if aggregate_summary:
            sys.stdout.write(aggregate_summary)
            if not aggregate_summary.endswith("\n"):
                sys.stdout.write("\n")
        else:
            sys.stderr.write("wolf cli: aggregate summary not produced\n")
            return EXIT_DENIED
        return EXIT_SUCCESS

    trace = {
        "input_mode": input_mode,
        "gmail_backend": provider,
        "llm_backend": llm_backend,
        "searched_count": searched_count,
        "read_count": len(messages),
        "summarized_count": summarized_count,
        "audit_event_count": 1,
    }

    result: Dict[str, Any] = {
        "mode": "threaded" if threaded else "message",
        "query": getattr(args, "query", None),
        "thread_id": thread_id,
        "message_count": len(messages),
        "summarized_count": summarized_count,
        "messages": per_message_summaries,
        "trace": trace,
    }
    if threaded:
        result["threads"] = thread_summaries
        result["thread_count"] = len(thread_summaries)
    if aggregate_summary:
        result["summary"] = aggregate_summary
        result["summary_length"] = len(aggregate_summary)

    _print_json(
        {
            "allowed": True,
            "executed": True,
            "requires_confirmation": False,
            "stage": "complete",
            "reason": "gmail search-summarize complete",
            "provider_called": True,
            "audit_event_id": None,
            "failed_checks": [],
            "warnings": warnings,
            "result": result,
        }
    )
    return EXIT_SUCCESS


def _audit_task_event(
    *,
    project_root: Path,
    actor: str,
    action_kind: str,
    target: str,
    outcome: str,
    decision: str,
    detail: Mapping[str, Any],
) -> None:
    """Write one AuditEvent for a task / calendar extraction run."""
    from .core.audit import utc_now_iso
    from .core.types import AuditEvent

    audit = AuditLogger(_default_audit_path(project_root.resolve()))
    event = AuditEvent(
        ts=utc_now_iso(),
        actor=actor,
        action_kind=action_kind,
        decision=decision,
        target=target,
        outcome=outcome,
        detail=dict(detail),
    )
    audit.log(event)


def _run_extraction_for_messages(
    *,
    llm: LLMAdapter,
    router: Router,
    messages: Sequence[Any],
    source_kind: str,
    body_attr: str = "body_text",
    id_attr: str = "message_id",
) -> "tuple[List[dict], List[dict], List[str]]":
    """Extract tasks + events from each message via Router → LLM."""
    from .tasks import extract_candidates_from_text, EXTRACTION_INSTRUCTION

    tasks_out: List[dict] = []
    events_out: List[dict] = []
    warnings: List[str] = []
    for m in messages:
        body = getattr(m, body_attr, "") or ""
        if not body.strip():
            warnings.append(
                f"{source_kind}: skipped empty body for "
                f"{getattr(m, id_attr, '')}"
            )
            continue
        source_id = getattr(m, id_attr, "") or ""
        subject = getattr(m, "subject", "") or ""
        from_ = getattr(m, "from_", "") or ""
        prompt = (
            f"{EXTRACTION_INSTRUCTION}\n\n<MAIL_BODY>\n{body}\n</MAIL_BODY>\n"
        )
        wrapped = wrap_untrusted(
            prompt,
            SourceKind.EMAIL,
            source_ref=source_id,
            metadata={"subject": subject, "from": from_},
        )
        decision = router.route(
            RouterAction(
                kind=ActionKind.LLM_SUMMARIZE_EMAIL,
                risk_level=RiskLevel.LOW,
                body=wrapped,
            )
        )
        if not decision.allowed or not isinstance(decision.result, str):
            warnings.append(
                f"{source_kind}: extraction skipped {source_id} "
                f"(stage={decision.stage})"
            )
            continue
        extraction = extract_candidates_from_text(
            llm_output=decision.result,
            body=body,
            source_kind=source_kind,
            source_id=source_id,
            source_subject=subject,
            source_from=from_,
        )
        tasks_out.extend(t.to_dict() for t in extraction.tasks)
        events_out.extend(e.to_dict() for e in extraction.events)
        if extraction.warnings:
            warnings.extend(extraction.warnings)
    return tasks_out, events_out, warnings


def _events_from_dicts(records: Sequence[Mapping[str, Any]]):
    from .tasks.types import CalendarEventCandidate

    out = []
    for r in records:
        out.append(
            CalendarEventCandidate(
                title=r.get("title", ""),
                description=r.get("description", ""),
                start_date=r.get("start_date", ""),
                start_time=r.get("start_time", ""),
                end_date=r.get("end_date", ""),
                end_time=r.get("end_time", ""),
                timezone=r.get("timezone", ""),
                location=r.get("location", ""),
                attendees=tuple(r.get("attendees", []) or ()),
                source_kind=r.get("source_kind", ""),
                source_id=r.get("source_id", ""),
                source_subject=r.get("source_subject", ""),
                confidence=float(r.get("confidence", 0.0)),
                evidence_snippet=r.get("evidence_snippet", ""),
            )
        )
    return out


def _maybe_write_ics_file(
    *,
    ics_text: str,
    project_root: Path,
    out_path_arg: Optional[str],
) -> Optional[Path]:
    if not out_path_arg:
        return None
    target = Path(out_path_arg)
    if not target.is_absolute():
        target = (project_root / target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(ics_text, encoding="utf-8")
    return target


def cmd_task_extract_mail(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output_mode = getattr(args, "output", "json")

    try:
        llm = _build_llm_from_args(args)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED

    router = _build_router(
        project_root, llm=llm,
        config=RouterConfig(allow_warning_injection_findings=False),
    )

    gate = _gate_mail_path(
        router=router, path_str=args.path, output_mode=output_mode
    )
    if gate is not None:
        return _emit_decision(gate, output_mode=output_mode, include_result=False)

    path = Path(args.path)
    if not path.is_absolute():
        path = (project_root / path).resolve()
    try:
        result = _read_mail_with_args(args=args, path=path)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED
    except MailReadError as exc:
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: mail_read failed: {exc.label}\n")
        else:
            _print_json(_gmail_error_decision(exc.label, stage="mail_read"))
        return EXIT_DENIED

    if not result.messages:
        if output_mode == "text":
            sys.stderr.write("wolf cli: no mail messages found\n")
        else:
            _print_json(
                {
                    "allowed": False, "executed": False,
                    "requires_confirmation": False,
                    "stage": "mail_read", "reason": "no messages",
                    "provider_called": False, "audit_event_id": None,
                    "failed_checks": [], "warnings": list(result.skipped),
                }
            )
        return EXIT_DENIED

    tasks_out, events_out, warnings = _run_extraction_for_messages(
        llm=llm, router=router, messages=result.messages,
        source_kind="local_mail", body_attr="body", id_attr="message_id",
    )

    try:
        _audit_task_event(
            project_root=project_root,
            actor="cli:task-extract-mail",
            action_kind="task.extract_mail",
            target=f"local_mail:{path}",
            outcome=(
                "extract_complete" if (tasks_out or events_out) else "extract_empty"
            ),
            decision="allow",
            detail={
                "source_kind": "local_mail",
                "provider": getattr(args, "backend", "fake"),
                "output_mode": output_mode,
                "message_count": len(result.messages),
                "task_count": len(tasks_out),
                "event_count": len(events_out),
            },
        )
    except OSError as exc:
        return _emit_audit_fail_closed(
            output_mode=output_mode,
            action_kind="task.extract_mail",
            exc=exc,
        )

    if output_mode == "text":
        if not tasks_out and not events_out:
            sys.stdout.write("(no tasks or events extracted)\n")
            return EXIT_SUCCESS
        for t in tasks_out:
            due = t.get("due_date") or "no-date"
            sys.stdout.write(f"TASK\t{due}\t{t['title']}\n")
        for e in events_out:
            d = e.get("start_date") or "no-date"
            t_ = e.get("start_time") or "all-day"
            sys.stdout.write(f"EVENT\t{d} {t_}\t{e['title']}\n")
        return EXIT_SUCCESS

    _print_json(
        {
            "allowed": True, "executed": True,
            "requires_confirmation": False,
            "stage": "complete",
            "reason": "task extraction complete",
            "provider_called": True, "audit_event_id": None,
            "failed_checks": [],
            "warnings": warnings + list(result.skipped),
            "result": {
                "source_kind": "local_mail",
                "message_count": len(result.messages),
                "task_count": len(tasks_out),
                "event_count": len(events_out),
                "tasks": tasks_out,
                "events": events_out,
            },
        }
    )
    return EXIT_SUCCESS


def cmd_task_extract_gmail(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output_mode = getattr(args, "output", "json")
    provider = getattr(args, "gmail_backend", "fake")
    llm_backend = getattr(args, "llm_backend", "fake")

    try:
        client = _build_gmail_client_from_args(args)
    except GmailClientError as exc:
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: gmail config error: {exc.label}\n")
        else:
            _print_json(_gmail_error_decision(exc.label, stage="gmail_config"))
        return EXIT_DENIED

    try:
        llm = _build_llm_for_gmail(args)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED

    router = _build_router(
        project_root, llm=llm,
        config=RouterConfig(allow_warning_injection_findings=False),
    )

    query = getattr(args, "query", None)
    message_id = getattr(args, "message_id", None)
    if not query and not message_id:
        sys.stderr.write(
            "wolf cli: task-extract-gmail requires --query or --message-id\n"
        )
        return EXIT_DENIED

    if message_id:
        ids = [message_id]
    else:
        try:
            hits = client.search(query=query, max_results=int(args.limit))
        except GmailClientError as exc:
            if output_mode == "text":
                sys.stderr.write(f"wolf cli: {exc.label}\n")
            else:
                _print_json(_gmail_error_decision(exc.label, stage="gmail_search"))
            return EXIT_DENIED
        ids = [h.message_id for h in hits]

    messages, skip = _read_gmail_messages(client=client, ids=ids)
    if not messages:
        if output_mode == "text":
            sys.stderr.write("wolf cli: no gmail messages found\n")
        else:
            _print_json(
                {
                    "allowed": False, "executed": False,
                    "requires_confirmation": False,
                    "stage": "gmail_search", "reason": "no messages",
                    "provider_called": False, "audit_event_id": None,
                    "failed_checks": [], "warnings": skip,
                }
            )
        return EXIT_DENIED

    tasks_out, events_out, warnings = _run_extraction_for_messages(
        llm=llm, router=router, messages=messages,
        source_kind="gmail", body_attr="body_text", id_attr="message_id",
    )
    warnings = list(skip) + warnings

    try:
        _audit_task_event(
            project_root=project_root,
            actor="cli:task-extract-gmail",
            action_kind="task.extract_gmail",
            target=f"gmail:{message_id or 'search'}",
            outcome=(
                "extract_complete" if (tasks_out or events_out) else "extract_empty"
            ),
            decision="allow",
            detail={
                "source_kind": "gmail",
                "provider": provider,
                "llm_backend": llm_backend,
                "output_mode": output_mode,
                "query_length": len(query or ""),
                "query_fingerprint": _query_fingerprint(query),
                "message_count": len(messages),
                "task_count": len(tasks_out),
                "event_count": len(events_out),
            },
        )
    except OSError as exc:
        return _emit_audit_fail_closed(
            output_mode=output_mode,
            action_kind="task.extract_gmail",
            exc=exc,
        )

    if output_mode == "text":
        if not tasks_out and not events_out:
            sys.stdout.write("(no tasks or events extracted)\n")
            return EXIT_SUCCESS
        for t in tasks_out:
            due = t.get("due_date") or "no-date"
            sys.stdout.write(f"TASK\t{due}\t{t['title']}\n")
        for e in events_out:
            d = e.get("start_date") or "no-date"
            t_ = e.get("start_time") or "all-day"
            sys.stdout.write(f"EVENT\t{d} {t_}\t{e['title']}\n")
        return EXIT_SUCCESS

    _print_json(
        {
            "allowed": True, "executed": True,
            "requires_confirmation": False,
            "stage": "complete",
            "reason": "task extraction complete",
            "provider_called": True, "audit_event_id": None,
            "failed_checks": [],
            "warnings": warnings,
            "result": {
                "source_kind": "gmail",
                "message_count": len(messages),
                "task_count": len(tasks_out),
                "event_count": len(events_out),
                "tasks": tasks_out,
                "events": events_out,
            },
        }
    )
    return EXIT_SUCCESS


def _emit_calendar_draft(
    *,
    project_root: Path,
    events_out: List[dict],
    output_mode: str,
    out_file_arg: Optional[str],
    extra_warnings: List[str],
    audit_kwargs: Mapping[str, Any],
) -> int:
    from .tasks import build_ics

    candidates = _events_from_dicts(events_out)
    ics_text = build_ics(candidates) if candidates else ""
    written_path: Optional[Path] = None
    try:
        written_path = _maybe_write_ics_file(
            ics_text=ics_text,
            project_root=project_root,
            out_path_arg=out_file_arg,
        )
    except OSError as exc:
        if output_mode == "text":
            sys.stderr.write(
                f"wolf cli: calendar-draft: write failed ({type(exc).__name__})\n"
            )
        else:
            _print_json(
                _gmail_error_decision(
                    f"calendar-draft: write failed ({type(exc).__name__})",
                    stage="calendar_write",
                )
            )
        return EXIT_DENIED

    try:
        merged_detail = dict(audit_kwargs.get("detail", {}))
        merged_detail.update({
            "event_count": len(events_out),
            "output_mode": output_mode,
            "wrote_file": bool(written_path),
        })
        _audit_task_event(
            project_root=project_root,
            actor=audit_kwargs["actor"],
            action_kind=audit_kwargs["action_kind"],
            target=audit_kwargs["target"],
            outcome=audit_kwargs["outcome"],
            decision=audit_kwargs["decision"],
            detail=merged_detail,
        )
    except OSError as exc:
        return _emit_audit_fail_closed(
            output_mode=output_mode,
            action_kind=audit_kwargs.get("action_kind", "calendar.draft"),
            exc=exc,
        )

    if output_mode == "ics":
        sys.stdout.write(ics_text)
        return EXIT_SUCCESS
    if output_mode == "text":
        if not events_out:
            sys.stdout.write("(no events extracted)\n")
            return EXIT_SUCCESS
        for e in events_out:
            d = e.get("start_date") or "no-date"
            t_ = e.get("start_time") or "all-day"
            sys.stdout.write(f"EVENT\t{d} {t_}\t{e['title']}\n")
        if written_path:
            sys.stdout.write(f"WROTE\t{written_path}\n")
        return EXIT_SUCCESS

    _print_json(
        {
            "allowed": True, "executed": True,
            "requires_confirmation": False,
            "stage": "complete",
            "reason": "calendar draft complete",
            "provider_called": True, "audit_event_id": None,
            "failed_checks": [],
            "warnings": extra_warnings,
            "result": {
                "event_count": len(events_out),
                "events": events_out,
                "ics": ics_text,
                "wrote_file": str(written_path) if written_path else None,
            },
        }
    )
    return EXIT_SUCCESS


def cmd_calendar_draft_mail(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output_mode = getattr(args, "output", "json")

    try:
        llm = _build_llm_from_args(args)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED
    router = _build_router(
        project_root, llm=llm,
        config=RouterConfig(allow_warning_injection_findings=False),
    )

    gate = _gate_mail_path(
        router=router, path_str=args.path, output_mode=output_mode
    )
    if gate is not None:
        return _emit_decision(gate, output_mode=output_mode, include_result=False)

    path = Path(args.path)
    if not path.is_absolute():
        path = (project_root / path).resolve()
    try:
        result = _read_mail_with_args(args=args, path=path)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED
    except MailReadError as exc:
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: mail_read failed: {exc.label}\n")
        else:
            _print_json(_gmail_error_decision(exc.label, stage="mail_read"))
        return EXIT_DENIED

    if not result.messages:
        if output_mode == "text":
            sys.stderr.write("wolf cli: no mail messages found\n")
        else:
            _print_json(
                {
                    "allowed": False, "executed": False,
                    "requires_confirmation": False,
                    "stage": "mail_read", "reason": "no messages",
                    "provider_called": False, "audit_event_id": None,
                    "failed_checks": [], "warnings": list(result.skipped),
                }
            )
        return EXIT_DENIED

    _tasks_out, events_out, warnings = _run_extraction_for_messages(
        llm=llm, router=router, messages=result.messages,
        source_kind="local_mail", body_attr="body", id_attr="message_id",
    )
    extra_warnings = list(result.skipped) + warnings

    return _emit_calendar_draft(
        project_root=project_root,
        events_out=events_out,
        output_mode=output_mode,
        out_file_arg=getattr(args, "output_file", None),
        extra_warnings=extra_warnings,
        audit_kwargs={
            "actor": "cli:calendar-draft-mail",
            "action_kind": "calendar.draft_mail",
            "target": f"local_mail:{path}",
            "outcome": ("draft_complete" if events_out else "draft_empty"),
            "decision": "allow" if events_out else "deny",
            "detail": {
                "source_kind": "local_mail",
                "provider": getattr(args, "backend", "fake"),
                "message_count": len(result.messages),
                "task_count": len(_tasks_out),
            },
        },
    )


def cmd_calendar_draft_gmail(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output_mode = getattr(args, "output", "json")
    provider = getattr(args, "gmail_backend", "fake")
    llm_backend = getattr(args, "llm_backend", "fake")

    try:
        client = _build_gmail_client_from_args(args)
    except GmailClientError as exc:
        if output_mode == "text":
            sys.stderr.write(f"wolf cli: gmail config error: {exc.label}\n")
        else:
            _print_json(_gmail_error_decision(exc.label, stage="gmail_config"))
        return EXIT_DENIED

    try:
        llm = _build_llm_for_gmail(args)
    except ValueError as exc:
        sys.stderr.write(f"wolf cli: {exc}\n")
        return EXIT_DENIED

    router = _build_router(
        project_root, llm=llm,
        config=RouterConfig(allow_warning_injection_findings=False),
    )

    query = getattr(args, "query", None)
    message_id = getattr(args, "message_id", None)
    if not query and not message_id:
        sys.stderr.write(
            "wolf cli: calendar-draft-gmail requires --query or --message-id\n"
        )
        return EXIT_DENIED

    if message_id:
        ids = [message_id]
    else:
        try:
            hits = client.search(query=query, max_results=int(args.limit))
        except GmailClientError as exc:
            if output_mode == "text":
                sys.stderr.write(f"wolf cli: {exc.label}\n")
            else:
                _print_json(_gmail_error_decision(exc.label, stage="gmail_search"))
            return EXIT_DENIED
        ids = [h.message_id for h in hits]

    messages, skip = _read_gmail_messages(client=client, ids=ids)
    if not messages:
        if output_mode == "text":
            sys.stderr.write("wolf cli: no gmail messages found\n")
        else:
            _print_json(
                {
                    "allowed": False, "executed": False,
                    "requires_confirmation": False,
                    "stage": "gmail_search", "reason": "no messages",
                    "provider_called": False, "audit_event_id": None,
                    "failed_checks": [], "warnings": skip,
                }
            )
        return EXIT_DENIED

    _tasks_out, events_out, warnings = _run_extraction_for_messages(
        llm=llm, router=router, messages=messages,
        source_kind="gmail", body_attr="body_text", id_attr="message_id",
    )
    extra_warnings = list(skip) + warnings

    return _emit_calendar_draft(
        project_root=project_root,
        events_out=events_out,
        output_mode=output_mode,
        out_file_arg=getattr(args, "output_file", None),
        extra_warnings=extra_warnings,
        audit_kwargs={
            "actor": "cli:calendar-draft-gmail",
            "action_kind": "calendar.draft_gmail",
            "target": f"gmail:{message_id or 'search'}",
            "outcome": ("draft_complete" if events_out else "draft_empty"),
            "decision": "allow" if events_out else "deny",
            "detail": {
                "source_kind": "gmail",
                "provider": provider,
                "llm_backend": llm_backend,
                "query_length": len(query or ""),
                "query_fingerprint": _query_fingerprint(query),
                "message_count": len(messages),
                "task_count": len(_tasks_out),
            },
        },
    )


def cmd_audit_tail(args: argparse.Namespace) -> int:
    """Print the last N AuditLogger events from var/audit/audit.jsonl.

    Missing audit file is treated as empty (exit 0). Malformed JSONL
    lines are skipped with a stderr warning. Body / token content
    is never re-injected by this command — we only re-print what is
    already in the file, and the audit writer never recorded those
    fields to begin with.
    """
    project_root = Path(args.project_root).resolve()
    audit_path = _default_audit_path(project_root)
    output_mode = getattr(args, "output", "json")
    limit = int(getattr(args, "limit", 20))
    action_kind_filter = getattr(args, "action_kind", None)

    events: List[Dict[str, Any]] = []
    malformed = 0
    if audit_path.exists():
        try:
            with audit_path.open("r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        rec = json.loads(s)
                    except json.JSONDecodeError:
                        malformed += 1
                        continue
                    if not isinstance(rec, dict):
                        malformed += 1
                        continue
                    if (
                        action_kind_filter
                        and rec.get("action_kind") != action_kind_filter
                    ):
                        continue
                    events.append(rec)
        except OSError as exc:
            sys.stderr.write(f"wolf cli: audit-tail: read failed ({type(exc).__name__})\n")
            return EXIT_DENIED

    if malformed:
        sys.stderr.write(
            f"wolf cli: audit-tail: skipped {malformed} malformed line(s)\n"
        )

    tail = events[-limit:] if limit > 0 else events

    if output_mode == "text":
        if not tail:
            sys.stdout.write("(no audit events)\n")
            return EXIT_SUCCESS
        for ev in tail:
            ts = ev.get("ts", "")
            kind = ev.get("action_kind", "")
            outcome = ev.get("outcome", "")
            decision = ev.get("decision", "")
            detail = ev.get("detail") or {}
            compact_keys = (
                "provider",
                "input_mode",
                "hit_count",
                "thread_count",
                "summarized_count",
                "draft_id",
                "source_message_id",
            )
            compact = " ".join(
                f"{k}={detail[k]}" for k in compact_keys if k in detail
            )
            sys.stdout.write(
                f"{ts}\t{kind}\t{decision}/{outcome}\t{compact}\n"
            )
        return EXIT_SUCCESS

    payload = {
        "allowed": True,
        "executed": True,
        "requires_confirmation": False,
        "stage": "complete",
        "reason": (
            "audit-tail empty"
            if not audit_path.exists()
            else "audit-tail complete"
        ),
        "provider_called": False,
        "audit_event_id": None,
        "failed_checks": [],
        "warnings": (
            [f"audit-tail: skipped {malformed} malformed line(s)"]
            if malformed else []
        ),
        "result": {
            "audit_path": str(audit_path),
            "audit_exists": audit_path.exists(),
            "limit": limit,
            "action_kind": action_kind_filter,
            "event_count": len(tail),
            "total_matched": len(events),
            "events": tail,
        },
    }
    _print_json(payload)
    return EXIT_SUCCESS


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

    ifp = sub.add_parser(
        "index-files",
        help=(
            "Walk a project-local directory and write a JSON file index "
            "(metadata + short snippet per file; no full bodies stored)"
        ),
    )
    ifp.add_argument(
        "--path",
        required=True,
        help="Directory to index (relative paths resolve against --project-root)",
    )
    ifp.add_argument(
        "--no-recursive",
        action="store_true",
        help="Index only files directly under --path",
    )
    ifp.add_argument(
        "--include",
        action="append",
        default=None,
        help=(
            "fnmatch pattern to include; may be repeated. Default: "
            "*.txt, *.md, *.rst, *.py"
        ),
    )
    ifp.add_argument(
        "--exclude",
        action="append",
        default=None,
        help="fnmatch pattern to exclude; may be repeated",
    )
    ifp.add_argument(
        "--max-files",
        type=int,
        default=INDEX_DEFAULT_MAX_FILES,
        help=f"Maximum files to index (default: {INDEX_DEFAULT_MAX_FILES})",
    )
    ifp.add_argument(
        "--max-bytes",
        type=int,
        default=INDEX_DEFAULT_MAX_BYTES,
        help="Per-file size limit (default: 1 MiB)",
    )
    ifp.add_argument(
        "--index-output",
        default=None,
        help=(
            "Where to write the JSON index "
            "(default: <project_root>/.wolf/index/files.json)"
        ),
    )
    ifp.add_argument(
        "--backend",
        choices=("fake", "ollama"),
        default="fake",
        help="LLM backend for audit / pipeline coherence (default: fake)",
    )
    ifp.add_argument("--model", default=None)
    ifp.add_argument("--ollama-url", default=None)
    ifp.add_argument("--allow-non-localhost-ollama", action="store_true")
    ifp.add_argument(
        "--embed",
        action="store_true",
        help="Also build the embedding index (requires --embedding-model)",
    )
    ifp.add_argument(
        "--embedding-backend",
        choices=("fake", "ollama"),
        default="ollama",
        help="Embedding backend (default: ollama)",
    )
    ifp.add_argument(
        "--embedding-model",
        default=None,
        help=(
            "Embedding model name (required when --embedding-backend ollama)"
        ),
    )
    ifp.add_argument(
        "--embedding-ollama-url",
        default=None,
        help="Ollama server URL for embeddings (default: --ollama-url or localhost)",
    )
    ifp.add_argument(
        "--embedding-index-path",
        default=None,
        help=(
            "Where to write the embedding JSON index "
            "(default: <project_root>/.wolf/index/embeddings.json)"
        ),
    )
    ifp.add_argument(
        "--embedding-input-bytes",
        type=int,
        default=4096,
        help=(
            "Bytes per file to send to the embedder (default: 4096). "
            "Larger values may exceed the model's context."
        ),
    )
    ifp.add_argument(
        "--output",
        choices=("json", "text"),
        default="json",
        help="Output format (default: json)",
    )
    ifp.set_defaults(func=cmd_index_files)

    sr = sub.add_parser(
        "search-files",
        help=(
            "Keyword search over a previously built JSON file index "
            "(case-insensitive substring; snippet around match)"
        ),
    )
    sr.add_argument("--query", required=True, help="Substring to search for")
    sr.add_argument(
        "--index-path",
        default=None,
        help=(
            "Path to the JSON index file "
            "(default: <project_root>/.wolf/index/files.json)"
        ),
    )
    sr.add_argument(
        "--max-hits",
        type=int,
        default=SEARCH_DEFAULT_MAX_HITS,
        help=f"Maximum hits to return (default: {SEARCH_DEFAULT_MAX_HITS})",
    )
    sr.add_argument(
        "--semantic",
        action="store_true",
        help=(
            "Use the embedding index instead of substring match. "
            "Requires that index-files was run with --embed and that "
            "--embedding-model matches the index's embedding model."
        ),
    )
    sr.add_argument(
        "--embedding-index-path",
        default=None,
        help=(
            "Path to the embedding JSON index "
            "(default: <project_root>/.wolf/index/embeddings.json)"
        ),
    )
    sr.add_argument(
        "--embedding-backend",
        choices=("fake", "ollama"),
        default="ollama",
        help="Embedding backend (default: ollama)",
    )
    sr.add_argument(
        "--embedding-model",
        default=None,
        help="Embedding model name (required for ollama backend)",
    )
    sr.add_argument(
        "--embedding-ollama-url",
        default=None,
        help="Ollama server URL for embeddings",
    )
    sr.add_argument(
        "--allow-non-localhost-ollama",
        action="store_true",
        help="Permit a non-localhost embedding URL",
    )
    sr.add_argument(
        "--output",
        choices=("json", "text"),
        default="json",
        help="Output format (default: json)",
    )
    sr.set_defaults(func=cmd_search_files)

    ss = sub.add_parser(
        "search-summarize",
        help=(
            "Search the file index for a query, summarize each matching "
            "file, and produce one aggregate summary"
        ),
    )
    ss.add_argument("--query", required=True, help="Substring to search for")
    ss.add_argument(
        "--index-path",
        default=None,
        help=(
            "JSON index path "
            "(default: <project_root>/.wolf/index/files.json)"
        ),
    )
    ss.add_argument(
        "--build-index",
        action="store_true",
        help=(
            "Build the index before searching. Implies index-files on "
            "--path (default: --project-root)."
        ),
    )
    ss.add_argument(
        "--path",
        default=None,
        help=(
            "Directory to index when --build-index is used "
            "(default: --project-root)"
        ),
    )
    ss.add_argument(
        "--no-recursive",
        action="store_true",
        help="Pass-through to the index builder when --build-index is set",
    )
    ss.add_argument(
        "--include",
        action="append",
        default=None,
        help="fnmatch pattern (repeatable); used by --build-index",
    )
    ss.add_argument(
        "--exclude",
        action="append",
        default=None,
        help="fnmatch pattern (repeatable); used by --build-index",
    )
    ss.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum search hits to consider (default: 5)",
    )
    ss.add_argument(
        "--max-files",
        type=int,
        default=5,
        help="Maximum hits to summarize (default: 5)",
    )
    ss.add_argument(
        "--max-bytes",
        type=int,
        default=INDEX_DEFAULT_MAX_BYTES,
        help="Per-file size limit for the index builder (default: 1 MiB)",
    )
    ss.add_argument(
        "--max-bytes-per-file",
        type=int,
        default=FILE_DEFAULT_MAX_BYTES,
        help="Per-hit summarize read limit (default: 1 MiB)",
    )
    ss.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"Chunk size in bytes (default: {DEFAULT_CHUNK_SIZE})",
    )
    ss.add_argument(
        "--max-chunks",
        type=int,
        default=DEFAULT_MAX_CHUNKS,
        help=f"Maximum chunks per file (default: {DEFAULT_MAX_CHUNKS})",
    )
    ss.add_argument(
        "--no-chunk",
        action="store_true",
        help="Disable per-file chunking",
    )
    ss.add_argument(
        "--strict-prompt-injection",
        action="store_true",
        help="Block on warning markers in addition to critical markers",
    )
    ss.add_argument(
        "--backend",
        choices=("fake", "ollama"),
        default="fake",
        help="LLM backend (default: fake)",
    )
    ss.add_argument("--model", default=None, help="Ollama model name")
    ss.add_argument("--ollama-url", default=None, help="Ollama server URL")
    ss.add_argument(
        "--allow-non-localhost-ollama",
        action="store_true",
        help="Permit a non-localhost --ollama-url",
    )
    ss.add_argument(
        "--output",
        choices=("json", "text"),
        default="json",
        help="Output format (default: json)",
    )
    ss.add_argument(
        "--include-per-file-summary",
        action="store_true",
        help=(
            "JSON-only: attach each hit's summary text under "
            "result.files[].summary. Default omits per-file summaries "
            "to keep the payload small. Text output ignores this flag."
        ),
    )
    ss.add_argument(
        "--semantic",
        action="store_true",
        help=(
            "Use the embedding vector index for retrieval. Requires that "
            "index-files was run with --embed and that --embedding-model "
            "matches the index's model."
        ),
    )
    ss.add_argument(
        "--embedding-index-path",
        default=None,
        help="Path to the embedding JSON index",
    )
    ss.add_argument(
        "--embedding-backend",
        choices=("fake", "ollama"),
        default="ollama",
        help="Embedding backend (default: ollama)",
    )
    ss.add_argument(
        "--embedding-model",
        default=None,
        help="Embedding model name (required for ollama embedding backend)",
    )
    ss.add_argument(
        "--embedding-ollama-url",
        default=None,
        help="Ollama server URL for embeddings",
    )
    ss.set_defaults(func=cmd_search_summarize)

    ms = sub.add_parser(
        "mail-summarize",
        help=(
            "Read a local .eml or .mbox and summarize each message via "
            "the selected LLM backend"
        ),
    )
    ms.add_argument("--path", required=True, help=".eml or .mbox file")
    ms.add_argument(
        "--limit",
        type=int,
        default=MAIL_DEFAULT_MBOX_LIMIT,
        help=f"Max messages to read from .mbox (default: {MAIL_DEFAULT_MBOX_LIMIT})",
    )
    ms.add_argument(
        "--max-bytes",
        type=int,
        default=MAIL_DEFAULT_MAX_BODY,
        help="Per-message body size cap (default: 1 MiB)",
    )
    ms.add_argument(
        "--backend",
        choices=("fake", "ollama"),
        default="fake",
        help="LLM backend (default: fake)",
    )
    ms.add_argument("--model", default=None, help="Ollama model name")
    ms.add_argument("--ollama-url", default=None, help="Ollama server URL")
    ms.add_argument(
        "--allow-non-localhost-ollama",
        action="store_true",
        help="Permit a non-localhost --ollama-url",
    )
    ms.add_argument(
        "--filter-since",
        default=None,
        help=(
            "Drop messages whose Date header is before this YYYY-MM-DD "
            "(inclusive, UTC; naive timestamps treated as UTC)."
        ),
    )
    ms.add_argument(
        "--filter-until",
        default=None,
        help=(
            "Drop messages whose Date header is after this YYYY-MM-DD "
            "(inclusive end-of-day UTC)."
        ),
    )
    ms.add_argument(
        "--filter-subject",
        action="append",
        default=None,
        help=(
            "Case-insensitive substring filter on the Subject header. "
            "Repeat to OR-combine: --filter-subject Lunch "
            "--filter-subject Meeting keeps subjects containing either. "
            "Different filter kinds are AND-combined."
        ),
    )
    ms.add_argument(
        "--filter-from",
        action="append",
        default=None,
        help=(
            "Case-insensitive substring filter on the From header. "
            "Repeatable (OR within --filter-from)."
        ),
    )
    ms.add_argument(
        "--filter-body-contains",
        action="append",
        default=None,
        help=(
            "Case-insensitive substring filter on the message body. "
            "Repeatable (OR within --filter-body-contains)."
        ),
    )
    ms.add_argument(
        "--output",
        choices=("json", "text"),
        default="json",
        help="Output format (default: json)",
    )
    ms.set_defaults(func=cmd_mail_summarize)

    msr = sub.add_parser(
        "mail-search",
        help="Substring search across subject / from / body of local mail",
    )
    msr.add_argument("--path", required=True, help=".eml or .mbox file")
    msr.add_argument("--query", required=True, help="Substring to search for")
    msr.add_argument(
        "--limit",
        type=int,
        default=MAIL_DEFAULT_MBOX_LIMIT,
        help="Max messages to scan from .mbox (default: 10)",
    )
    msr.add_argument(
        "--max-hits",
        type=int,
        default=MAIL_SEARCH_DEFAULT_MAX_HITS,
        help="Maximum hits to return (default: 10)",
    )
    msr.add_argument(
        "--max-bytes",
        type=int,
        default=MAIL_DEFAULT_MAX_BODY,
        help="Per-message body size cap (default: 1 MiB)",
    )
    msr.add_argument(
        "--filter-since",
        default=None,
        help="Drop messages dated before YYYY-MM-DD (UTC inclusive).",
    )
    msr.add_argument(
        "--filter-until",
        default=None,
        help="Drop messages dated after YYYY-MM-DD (UTC end-of-day inclusive).",
    )
    msr.add_argument(
        "--filter-subject",
        action="append",
        default=None,
        help=(
            "Pre-filter mailbox messages by Subject substring before "
            "running the --query search. Repeatable (OR)."
        ),
    )
    msr.add_argument(
        "--filter-from",
        action="append",
        default=None,
        help="Pre-filter by From substring. Repeatable (OR).",
    )
    msr.add_argument(
        "--filter-body-contains",
        action="append",
        default=None,
        help="Pre-filter by body substring. Repeatable (OR).",
    )
    msr.add_argument(
        "--output",
        choices=("json", "text"),
        default="json",
        help="Output format (default: json)",
    )
    msr.set_defaults(func=cmd_mail_search)

    md = sub.add_parser(
        "mail-draft",
        help=(
            "Generate a reply draft for a local mail message. The user "
            "instruction is trusted; the mail body is treated as "
            "UntrustedText. No mail is sent."
        ),
    )
    md.add_argument("--path", required=True, help=".eml or .mbox file")
    md.add_argument(
        "--instruction",
        required=True,
        help="Trusted instruction describing the reply to compose",
    )
    md.add_argument(
        "--message-index",
        type=int,
        default=0,
        help="0-based index into an .mbox (default: 0; for .eml ignored)",
    )
    md.add_argument(
        "--limit",
        type=int,
        default=MAIL_DEFAULT_MBOX_LIMIT,
        help="Max messages to scan from .mbox before indexing (default: 10)",
    )
    md.add_argument(
        "--max-bytes",
        type=int,
        default=MAIL_DEFAULT_MAX_BODY,
        help="Per-message body size cap (default: 1 MiB)",
    )
    md.add_argument(
        "--backend",
        choices=("fake", "ollama"),
        default="fake",
        help="LLM backend (default: fake)",
    )
    md.add_argument("--model", default=None, help="Ollama model name")
    md.add_argument("--ollama-url", default=None, help="Ollama server URL")
    md.add_argument(
        "--allow-non-localhost-ollama",
        action="store_true",
        help="Permit a non-localhost --ollama-url",
    )
    md.add_argument(
        "--filter-since",
        default=None,
        help="Drop messages dated before YYYY-MM-DD (UTC inclusive).",
    )
    md.add_argument(
        "--filter-until",
        default=None,
        help="Drop messages dated after YYYY-MM-DD (UTC end-of-day inclusive).",
    )
    md.add_argument(
        "--filter-subject",
        action="append",
        default=None,
        help=(
            "Narrow .mbox / Maildir messages by Subject substring before "
            "--message-index is applied. Repeatable (OR). For .eml a "
            "mismatch exits 2."
        ),
    )
    md.add_argument(
        "--filter-from",
        action="append",
        default=None,
        help=(
            "Narrow by From substring before --message-index. "
            "Repeatable (OR)."
        ),
    )
    md.add_argument(
        "--filter-body-contains",
        action="append",
        default=None,
        help=(
            "Narrow by body substring before --message-index. "
            "Repeatable (OR)."
        ),
    )
    md.add_argument(
        "--output",
        choices=("json", "text"),
        default="json",
        help="Output format (default: json)",
    )
    md.set_defaults(func=cmd_mail_draft)

    mt = sub.add_parser(
        "mail-thread",
        help=(
            "Group local mail (.eml / .mbox / Maildir) into conversation "
            "threads via Message-ID / In-Reply-To / References, with a "
            "normalized-subject fallback."
        ),
    )
    mt.add_argument(
        "--path", required=True, help=".eml, .mbox, or Maildir directory"
    )
    mt.add_argument(
        "--limit",
        type=int,
        default=MAIL_DEFAULT_MBOX_LIMIT,
        help="Max messages to read from .mbox / Maildir (default: 10)",
    )
    mt.add_argument(
        "--max-bytes",
        type=int,
        default=MAIL_DEFAULT_MAX_BODY,
        help="Per-message body size cap (default: 1 MiB)",
    )
    mt.add_argument(
        "--filter-since", default=None, help="Drop messages dated before YYYY-MM-DD."
    )
    mt.add_argument(
        "--filter-until", default=None, help="Drop messages dated after YYYY-MM-DD."
    )
    mt.add_argument(
        "--filter-subject", action="append", default=None,
        help="Subject substring filter (repeatable: OR).",
    )
    mt.add_argument(
        "--filter-from", action="append", default=None,
        help="From substring filter (repeatable: OR).",
    )
    mt.add_argument(
        "--filter-body-contains", action="append", default=None,
        help="Body substring filter (repeatable: OR).",
    )
    mt.add_argument(
        "--backend",
        choices=("fake", "ollama"),
        default="fake",
        help="LLM backend for the audit gate (default: fake)",
    )
    mt.add_argument("--model", default=None, help="Ollama model name")
    mt.add_argument("--ollama-url", default=None, help="Ollama server URL")
    mt.add_argument(
        "--allow-non-localhost-ollama",
        action="store_true",
        help="Permit a non-localhost --ollama-url",
    )
    mt.add_argument(
        "--output", choices=("json", "text"), default="json",
        help="Output format (default: json)",
    )
    mt.set_defaults(func=cmd_mail_thread)

    mss = sub.add_parser(
        "mail-search-summarize",
        help=(
            "Search local mail and summarize each matching message (or "
            "thread with --threaded), then summarize the per-message / "
            "per-thread summaries into one aggregate."
        ),
    )
    mss.add_argument(
        "--path", required=True, help=".eml, .mbox, or Maildir directory"
    )
    mss.add_argument("--query", required=True, help="Substring to search for")
    mss.add_argument(
        "--limit",
        type=int,
        default=MAIL_DEFAULT_MBOX_LIMIT,
        help="Max messages to scan (default: 10)",
    )
    mss.add_argument(
        "--max-hits",
        type=int,
        default=MAIL_SEARCH_DEFAULT_MAX_HITS,
        help="Maximum hits to summarize (default: 10)",
    )
    mss.add_argument(
        "--max-bytes",
        type=int,
        default=MAIL_DEFAULT_MAX_BODY,
        help="Per-message body size cap (default: 1 MiB)",
    )
    mss.add_argument(
        "--threaded",
        action="store_true",
        help=(
            "Summarize the threads containing the hits, not just the "
            "matching messages. Threads are built with build_threads."
        ),
    )
    mss.add_argument(
        "--include-per-message-summary",
        action="store_true",
        help=(
            "JSON-only: attach each per-message (or per-thread) summary "
            "under result.messages[].summary / result.threads[].summary."
        ),
    )
    mss.add_argument(
        "--filter-since", default=None, help="Drop messages dated before YYYY-MM-DD."
    )
    mss.add_argument(
        "--filter-until", default=None, help="Drop messages dated after YYYY-MM-DD."
    )
    mss.add_argument(
        "--filter-subject", action="append", default=None,
        help="Subject filter (repeatable: OR).",
    )
    mss.add_argument(
        "--filter-from", action="append", default=None,
        help="From filter (repeatable: OR).",
    )
    mss.add_argument(
        "--filter-body-contains", action="append", default=None,
        help="Body filter (repeatable: OR).",
    )
    mss.add_argument(
        "--backend",
        choices=("fake", "ollama"),
        default="fake",
        help="LLM backend (default: fake)",
    )
    mss.add_argument("--model", default=None, help="Ollama model name")
    mss.add_argument("--ollama-url", default=None, help="Ollama server URL")
    mss.add_argument(
        "--allow-non-localhost-ollama",
        action="store_true",
        help="Permit a non-localhost --ollama-url",
    )
    mss.add_argument(
        "--output", choices=("json", "text"), default="json",
        help="Output format (default: json)",
    )
    mss.set_defaults(func=cmd_mail_search_summarize)

    # gmail-search
    gs = sub.add_parser(
        "gmail-search",
        help=(
            "Search Gmail and return message ids + headers. "
            "Default backend is fake (no network)."
        ),
    )
    gs.add_argument("--query", required=True, help="Gmail search query (`q=`)")
    gs.add_argument(
        "--limit",
        type=int,
        default=GMAIL_DEFAULT_LIMIT,
        help="Max results (default: 10)",
    )
    gs.add_argument(
        "--gmail-backend",
        choices=("fake", "gmail"),
        default="fake",
        help="fake (in-memory) or gmail (real API)",
    )
    gs.add_argument(
        "--credentials-path",
        default=None,
        help="Path to a JSON file with {access_token: ...} (required for --gmail-backend gmail)",
    )
    gs.add_argument(
        "--gmail-base-url",
        default=None,
        help="Override Gmail API base URL (https://gmail.googleapis.com)",
    )
    gs.add_argument(
        "--allow-non-https-gmail",
        action="store_true",
        help="Permit a non-https --gmail-base-url for localhost test stubs",
    )
    gs.add_argument(
        "--no-enrich",
        action="store_true",
        help="Return raw search hits (ids only) without per-message header lookup",
    )
    gs.add_argument(
        "--output", choices=("json", "text"), default="json",
        help="Output format (default: json)",
    )
    gs.set_defaults(func=cmd_gmail_search)

    # gmail-read
    gr = sub.add_parser(
        "gmail-read",
        help="Read one Gmail message and return metadata + bounded body preview.",
    )
    gr.add_argument("--message-id", required=True, help="Gmail message id")
    gr.add_argument(
        "--gmail-backend",
        choices=("fake", "gmail"),
        default="fake",
    )
    gr.add_argument("--credentials-path", default=None)
    gr.add_argument("--gmail-base-url", default=None)
    gr.add_argument(
        "--allow-non-https-gmail",
        action="store_true",
    )
    gr.add_argument(
        "--body-preview-bytes",
        type=int,
        default=GMAIL_DEFAULT_BODY_PREVIEW_BYTES,
        help="Cap on body_preview output (default: 500 bytes)",
    )
    gr.add_argument(
        "--output", choices=("json", "text"), default="json",
    )
    gr.set_defaults(func=cmd_gmail_read)

    # gmail-summarize
    gsm = sub.add_parser(
        "gmail-summarize",
        help=(
            "Search/read Gmail messages and summarize each via the "
            "Router pipeline. No send. No store."
        ),
    )
    gsm.add_argument(
        "--query", default=None,
        help="Gmail search query (alternative to --message-id)",
    )
    gsm.add_argument(
        "--message-id", default=None,
        help="Summarize this exact id (alternative to --query)",
    )
    gsm.add_argument(
        "--limit",
        type=int,
        default=GMAIL_DEFAULT_LIMIT,
        help="Max search results to summarize (default: 10)",
    )
    gsm.add_argument(
        "--gmail-backend",
        choices=("fake", "gmail"),
        default="fake",
    )
    gsm.add_argument("--credentials-path", default=None)
    gsm.add_argument("--gmail-base-url", default=None)
    gsm.add_argument("--allow-non-https-gmail", action="store_true")
    gsm.add_argument(
        "--llm-backend",
        choices=("fake", "ollama"),
        default="fake",
        help="LLM backend (default: fake)",
    )
    gsm.add_argument("--model", default=None, help="Ollama model name")
    gsm.add_argument("--ollama-url", default=None, help="Ollama server URL")
    gsm.add_argument(
        "--allow-non-localhost-ollama",
        action="store_true",
        help="Permit a non-localhost --ollama-url",
    )
    gsm.add_argument(
        "--output", choices=("json", "text"), default="json",
    )
    gsm.set_defaults(func=cmd_gmail_summarize)

    # gmail-draft
    gd = sub.add_parser(
        "gmail-draft",
        help=(
            "Generate a reply draft via the LLM and create it as a Gmail "
            "draft. NEVER sends."
        ),
    )
    gd.add_argument("--message-id", required=True, help="Gmail message id to reply to")
    gd.add_argument(
        "--instruction", required=True,
        help="User instruction for the reply (trusted; mail body is untrusted)",
    )
    gd.add_argument(
        "--gmail-backend",
        choices=("fake", "gmail"),
        default="fake",
    )
    gd.add_argument("--credentials-path", default=None)
    gd.add_argument("--gmail-base-url", default=None)
    gd.add_argument("--allow-non-https-gmail", action="store_true")
    gd.add_argument(
        "--llm-backend",
        choices=("fake", "ollama"),
        default="fake",
    )
    gd.add_argument("--model", default=None)
    gd.add_argument("--ollama-url", default=None)
    gd.add_argument("--allow-non-localhost-ollama", action="store_true")
    gd.add_argument(
        "--body-preview-bytes",
        type=int,
        default=GMAIL_DEFAULT_BODY_PREVIEW_BYTES,
        help="Cap on body_preview output (default: 500 bytes)",
    )
    gd.add_argument(
        "--output", choices=("json", "text"), default="json",
    )
    gd.set_defaults(func=cmd_gmail_draft)

    # gmail-thread
    gt = sub.add_parser(
        "gmail-thread",
        help=(
            "Group Gmail messages into threads. Prefers Gmail's "
            "threadId; falls back to rfc822 id / normalized subject."
        ),
    )
    gt.add_argument(
        "--query", default=None,
        help="Gmail search query (alternative to --message-id)",
    )
    gt.add_argument(
        "--message-id", default=None,
        help="Read only this exact message and try to thread it",
    )
    gt.add_argument(
        "--thread-id", default=None,
        help=(
            "Fetch the messages of this Gmail thread directly "
            "(GET /threads/{id}). Mutually exclusive with --query / "
            "--message-id."
        ),
    )
    gt.add_argument(
        "--limit",
        type=int,
        default=GMAIL_DEFAULT_LIMIT,
        help="Max search results to read (default: 10)",
    )
    gt.add_argument(
        "--gmail-backend",
        choices=("fake", "gmail"),
        default="fake",
    )
    gt.add_argument("--credentials-path", default=None)
    gt.add_argument("--gmail-base-url", default=None)
    gt.add_argument(
        "--allow-non-https-gmail",
        action="store_true",
    )
    gt.add_argument(
        "--output", choices=("json", "text"), default="json",
    )
    gt.set_defaults(func=cmd_gmail_thread)

    # gmail-search-summarize
    gss = sub.add_parser(
        "gmail-search-summarize",
        help=(
            "Search/read Gmail and summarize per-message (default) or "
            "per-thread (--threaded), then summarize the per-* summaries "
            "into one aggregate."
        ),
    )
    gss.add_argument("--query", default=None)
    gss.add_argument("--message-id", default=None)
    gss.add_argument(
        "--thread-id", default=None,
        help=(
            "Summarize one Gmail thread directly (GET /threads/{id}). "
            "Mutually exclusive with --query / --message-id. With "
            "--thread-id the command implicitly enters threaded mode."
        ),
    )
    gss.add_argument(
        "--limit",
        type=int,
        default=GMAIL_DEFAULT_LIMIT,
        help="Max search results to summarize (default: 10)",
    )
    gss.add_argument(
        "--gmail-backend",
        choices=("fake", "gmail"),
        default="fake",
    )
    gss.add_argument("--credentials-path", default=None)
    gss.add_argument("--gmail-base-url", default=None)
    gss.add_argument("--allow-non-https-gmail", action="store_true")
    gss.add_argument(
        "--threaded",
        action="store_true",
        help="Summarize per-thread (uses Gmail threadId) instead of per-message.",
    )
    gss.add_argument(
        "--include-per-message-summary",
        action="store_true",
        help="JSON-only: include each per-message summary text under result.messages[].summary",
    )
    gss.add_argument(
        "--include-per-thread-summary",
        action="store_true",
        help="JSON-only: include each per-thread summary text under result.threads[].summary",
    )
    gss.add_argument(
        "--llm-backend",
        choices=("fake", "ollama"),
        default="fake",
    )
    gss.add_argument("--model", default=None)
    gss.add_argument("--ollama-url", default=None)
    gss.add_argument("--allow-non-localhost-ollama", action="store_true")
    gss.add_argument(
        "--output", choices=("json", "text"), default="json",
    )
    gss.set_defaults(func=cmd_gmail_search_summarize)

    # audit-tail
    # task-extract-mail (PR #30)
    tem = sub.add_parser(
        "task-extract-mail",
        help=(
            "Extract task / calendar candidates from a local .eml / "
            ".mbox / Maildir. LLM-mediated with deterministic regex "
            "fallback. No calendar registration, no send."
        ),
    )
    tem.add_argument(
        "--path", required=True, help=".eml, .mbox, or Maildir directory"
    )
    tem.add_argument(
        "--limit", type=int, default=MAIL_DEFAULT_MBOX_LIMIT,
        help="Max messages to scan (default: 10)",
    )
    tem.add_argument(
        "--max-bytes", type=int, default=MAIL_DEFAULT_MAX_BODY,
    )
    tem.add_argument("--filter-since", default=None)
    tem.add_argument("--filter-until", default=None)
    tem.add_argument(
        "--filter-subject", action="append", default=None,
    )
    tem.add_argument(
        "--filter-from", action="append", default=None,
    )
    tem.add_argument(
        "--filter-body-contains", action="append", default=None,
    )
    tem.add_argument(
        "--backend", choices=("fake", "ollama"), default="fake",
    )
    tem.add_argument("--model", default=None)
    tem.add_argument("--ollama-url", default=None)
    tem.add_argument("--allow-non-localhost-ollama", action="store_true")
    tem.add_argument(
        "--output", choices=("json", "text"), default="json",
    )
    tem.set_defaults(func=cmd_task_extract_mail)

    # task-extract-gmail (PR #30)
    teg = sub.add_parser(
        "task-extract-gmail",
        help=(
            "Extract task / calendar candidates from one or more "
            "Gmail messages. No send, no calendar registration."
        ),
    )
    teg.add_argument("--query", default=None)
    teg.add_argument("--message-id", default=None)
    teg.add_argument(
        "--limit", type=int, default=GMAIL_DEFAULT_LIMIT,
    )
    teg.add_argument(
        "--gmail-backend", choices=("fake", "gmail"), default="fake",
    )
    teg.add_argument("--credentials-path", default=None)
    teg.add_argument("--gmail-base-url", default=None)
    teg.add_argument("--allow-non-https-gmail", action="store_true")
    teg.add_argument(
        "--llm-backend", choices=("fake", "ollama"), default="fake",
    )
    teg.add_argument("--model", default=None)
    teg.add_argument("--ollama-url", default=None)
    teg.add_argument("--allow-non-localhost-ollama", action="store_true")
    teg.add_argument(
        "--output", choices=("json", "text"), default="json",
    )
    teg.set_defaults(func=cmd_task_extract_gmail)

    # calendar-draft-mail (PR #30)
    cdm = sub.add_parser(
        "calendar-draft-mail",
        help=(
            "Build a draft iCalendar (.ics) from local mail. The "
            "output is text only; nothing is sent or registered."
        ),
    )
    cdm.add_argument("--path", required=True)
    cdm.add_argument(
        "--limit", type=int, default=MAIL_DEFAULT_MBOX_LIMIT,
    )
    cdm.add_argument(
        "--max-bytes", type=int, default=MAIL_DEFAULT_MAX_BODY,
    )
    cdm.add_argument("--filter-since", default=None)
    cdm.add_argument("--filter-until", default=None)
    cdm.add_argument(
        "--filter-subject", action="append", default=None,
    )
    cdm.add_argument(
        "--filter-from", action="append", default=None,
    )
    cdm.add_argument(
        "--filter-body-contains", action="append", default=None,
    )
    cdm.add_argument(
        "--backend", choices=("fake", "ollama"), default="fake",
    )
    cdm.add_argument("--model", default=None)
    cdm.add_argument("--ollama-url", default=None)
    cdm.add_argument("--allow-non-localhost-ollama", action="store_true")
    cdm.add_argument(
        "--output-file", default=None,
        help="Write the .ics to this path (resolved under project-root if relative)",
    )
    cdm.add_argument(
        "--output", choices=("json", "text", "ics"), default="json",
    )
    cdm.set_defaults(func=cmd_calendar_draft_mail)

    # calendar-draft-gmail (PR #30)
    cdg = sub.add_parser(
        "calendar-draft-gmail",
        help=(
            "Build a draft iCalendar (.ics) from Gmail messages. "
            "No send, no calendar registration."
        ),
    )
    cdg.add_argument("--query", default=None)
    cdg.add_argument("--message-id", default=None)
    cdg.add_argument(
        "--limit", type=int, default=GMAIL_DEFAULT_LIMIT,
    )
    cdg.add_argument(
        "--gmail-backend", choices=("fake", "gmail"), default="fake",
    )
    cdg.add_argument("--credentials-path", default=None)
    cdg.add_argument("--gmail-base-url", default=None)
    cdg.add_argument("--allow-non-https-gmail", action="store_true")
    cdg.add_argument(
        "--llm-backend", choices=("fake", "ollama"), default="fake",
    )
    cdg.add_argument("--model", default=None)
    cdg.add_argument("--ollama-url", default=None)
    cdg.add_argument("--allow-non-localhost-ollama", action="store_true")
    cdg.add_argument(
        "--output-file", default=None,
    )
    cdg.add_argument(
        "--output", choices=("json", "text", "ics"), default="json",
    )
    cdg.set_defaults(func=cmd_calendar_draft_gmail)

    at = sub.add_parser(
        "audit-tail",
        help=(
            "Print the last N events from var/audit/audit.jsonl. "
            "Metadata only — no raw bodies or tokens are ever recorded "
            "by the audit writer."
        ),
    )
    at.add_argument(
        "--limit", type=int, default=20,
        help="Max events to print (default: 20)",
    )
    at.add_argument(
        "--action-kind", default=None,
        help="Filter events by action_kind exact match",
    )
    at.add_argument(
        "--output", choices=("json", "text"), default="json",
    )
    at.set_defaults(func=cmd_audit_tail)

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
