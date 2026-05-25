from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence

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
