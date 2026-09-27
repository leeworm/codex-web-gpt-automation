from __future__ import annotations

"""Small ordinary Oracle executor.

This is intentionally separate from the historical workflow/state machinery in
``chatgpt_oracle_run.py`` and ``chatgpt_oracle_state.py``.  New executions have
one mission, one owned browser tab, one model/effort check, and one durable
capture.  Historical commands remain available only through their explicit
recovery entry points.
"""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


BIN = Path(__file__).resolve().parent
MANIFEST_SCHEMA = "codex.chatgpt.oracle-execution/v1"
STATE_SCHEMA = "codex.chatgpt.oracle-execution-state/v1"
RETRY_AUTHORIZATION_SCHEMA = "codex.chatgpt.oracle-authorized-retry/v1"
RETRY_AUTHORIZATION_NAME = "authorized-retry.json"
RETRY_AUTHORIZATION_VALUE = "explicit-user-authorized-retry-despite-uncertain-delivery"
PRE_SUBMIT_HISTORY_SCHEMA = "codex.chatgpt.oracle-pre-submit-attempt-history/v1"
CHATGPT_URL = "https://chatgpt.com/?temporary-chat=true"
DEFAULT_APP_NAME = "codex"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_EFFORT = "extended"
SUPPORTED_MODELS = ("latest", "gpt-5.6-sol")
SUPPORTED_EFFORTS = ("pro", "extra-high", "extended")
ORACLE_EXPLICIT_STRATEGY = "select"
TERMINAL_ORACLE_STATES = frozenset({"complete", "completed", "done", "finished"})
UNRESOLVED_STATUSES = frozenset({"prepared", "running", "attention_required"})
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,95}$")
THREAD_ID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I)
APP_NAME_RE = re.compile(r"^[^\r\n@][^\r\n]*$")
TARGET_ID_RE = re.compile(r"^[A-Fa-f0-9]{8,64}$")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
PICKER_PROOF_PREFIX = "[browser] Picker DOM proof:"
MODEL_EVIDENCE_PREFIX = "[browser] Model selection evidence:"
THINKING_PREFIX = "[browser] Thinking time:"
THINKING_EVIDENCE_PREFIX = "[browser] Thinking effort evidence:"
ALLOWED_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "project_root",
        "mission_path",
        "run_root",
        "run_id",
        "source_thread_id",
        "model",
        "effort",
        "app_name",
    }
)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"module unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUNTIME = _load("chatgpt_oracle_execute_runtime", BIN / "chatgpt_oracle_runtime.py")
COMPAT = _load("chatgpt_oracle_execute_compat", BIN / "chatgpt_oracle_compat.py")


class ExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = dict(evidence or {})

    def envelope(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": str(self), "evidence": self.evidence}}


@dataclass(frozen=True)
class ExecutionConfig:
    project_root: Path
    mission_path: Path
    mission_sha256: str
    run_root: Path
    run_id: str
    source_thread_id: str | None
    model: str
    effort: str
    app_name: str
    copy_profile: Path


def _absolute_path(value: str | Path | None, *, label: str, must_exist: bool) -> Path:
    raw = Path(str(value or "")).expanduser()
    if not raw.is_absolute():
        raise ExecutionError(f"{label.upper()}_ABSOLUTE_REQUIRED", f"{label} must be absolute", {"path": str(raw)})
    try:
        return raw.resolve(strict=must_exist)
    except OSError as exc:
        raise ExecutionError(f"{label.upper()}_INVALID", f"{label} could not be resolved", {"path": str(raw)}) from exc


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalize_model(value: str | None) -> str:
    model = str(value or DEFAULT_MODEL).strip().casefold()
    if model not in SUPPORTED_MODELS:
        raise ExecutionError("MODEL_UNSUPPORTED", "model is not supported by the lean Oracle route", {"supported": list(SUPPORTED_MODELS)})
    return model


def _normalize_effort(value: str | None) -> str:
    effort = str(value or DEFAULT_EFFORT).strip().casefold().replace("_", "-")
    if effort not in SUPPORTED_EFFORTS:
        raise ExecutionError("EFFORT_UNSUPPORTED", "effort is not supported by the lean Oracle route", {"supported": list(SUPPORTED_EFFORTS)})
    return effort


def _normalize_app_name(value: str | None) -> str:
    app_name = str(value or DEFAULT_APP_NAME).strip().lstrip("@").strip()
    if not app_name or APP_NAME_RE.fullmatch(app_name) is None:
        raise ExecutionError("APP_NAME_INVALID", "app_name must be one nonempty line without a leading @")
    return app_name


def _default_run_root(project_root: Path) -> Path:
    base = Path(os.environ.get("CODEX_ORACLE_STATE_ROOT") or (Path.home() / ".codex" / "state" / "chatgpt-oracle")).expanduser().resolve()
    key = hashlib.sha256(str(project_root).casefold().encode("utf-8")).hexdigest()[:24]
    return base / "ordinary" / "projects" / key / "runs"


def make_config(
    *,
    project_root: str | Path,
    mission_path: str | Path,
    run_root: str | Path | None = None,
    run_id: str | None = None,
    source_thread_id: str | None = None,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    app_name: str = DEFAULT_APP_NAME,
) -> ExecutionConfig:
    root = _absolute_path(project_root, label="project_root", must_exist=True)
    if not root.is_dir():
        raise ExecutionError("PROJECT_ROOT_NOT_DIRECTORY", "project_root must identify a directory")
    if root.parent == root:
        raise ExecutionError("PROJECT_ROOT_TOO_BROAD", "project_root must not be a filesystem or drive root")
    raw_mission = Path(str(mission_path)).expanduser()
    if raw_mission.is_symlink():
        raise ExecutionError("MISSION_FILE_INVALID", "mission_path must not be a symlink", {"path": str(raw_mission)})
    mission = _absolute_path(raw_mission, label="mission_path", must_exist=True)
    if not mission.is_file() or not _is_within(root, mission):
        raise ExecutionError("MISSION_OUTSIDE_APPROVED_ROOT", "mission_path must be a regular file inside project_root")
    try:
        mission.read_bytes().decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ExecutionError("MISSION_UTF8_REQUIRED", "mission_path must contain valid UTF-8", {"offset": exc.start}) from exc
    actual_run_root = _absolute_path(run_root, label="run_root", must_exist=False) if run_root else _default_run_root(root)
    if _is_within(root, actual_run_root) or _is_within(actual_run_root, root):
        raise ExecutionError("RUN_ROOT_OVERLAPS_PROJECT", "run_root must be disjoint from the approved project root")
    actual_run_id = str(run_id or f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}").strip()
    if RUN_ID_RE.fullmatch(actual_run_id) is None:
        raise ExecutionError("RUN_ID_INVALID", "run_id must be a safe 8-96 character identifier")
    explicit_thread = str(source_thread_id or "").strip().casefold()
    environment_thread = str(os.environ.get("CODEX_THREAD_ID") or "").strip().casefold()
    if explicit_thread and THREAD_ID_RE.fullmatch(explicit_thread) is None:
        raise ExecutionError("SOURCE_THREAD_ID_INVALID", "source_thread_id must be a Codex task UUID")
    if environment_thread and THREAD_ID_RE.fullmatch(environment_thread) is None:
        raise ExecutionError("SOURCE_THREAD_ID_INVALID", "CODEX_THREAD_ID must be a Codex task UUID when set")
    if explicit_thread and environment_thread and explicit_thread != environment_thread:
        raise ExecutionError("SOURCE_THREAD_ID_MISMATCH", "manifest task owner does not match the current Codex task")
    profile_override = str(os.environ.get("ORACLE_BROWSER_PROFILE_DIR") or "").strip()
    copy_profile = (
        Path(profile_override).expanduser().absolute()
        if profile_override
        else (Path.home() / ".oracle" / "browser-profile").absolute()
    )
    if copy_profile.is_symlink():
        raise ExecutionError(
            "SIGNED_IN_PROFILE_UNAVAILABLE",
            "the signed-in Oracle profile seed is unavailable or unsafe",
            {"copy_profile": str(copy_profile)},
        )
    if _is_within(root, copy_profile) or _is_within(copy_profile, root):
        raise ExecutionError("COPY_PROFILE_OVERLAPS_PROJECT", "profile seed must be outside project_root")
    return ExecutionConfig(
        root,
        mission,
        _sha256(mission),
        actual_run_root,
        actual_run_id,
        explicit_thread or environment_thread or None,
        _normalize_model(model),
        _normalize_effort(effort),
        _normalize_app_name(app_name),
        copy_profile,
    )


def manifest_payload(config: ExecutionConfig) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "project_root": str(config.project_root),
        "mission_path": str(config.mission_path),
        "model": config.model,
        "effort": config.effort,
        "app_name": config.app_name,
    }
    if config.run_root != _default_run_root(config.project_root):
        payload["run_root"] = str(config.run_root)
    if config.run_id:
        payload["run_id"] = config.run_id
    if config.source_thread_id:
        payload["source_thread_id"] = config.source_thread_id
    return payload


def load_manifest(path: Path) -> ExecutionConfig:
    manifest_path = _absolute_path(path, label="manifest_path", must_exist=True)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionError("MANIFEST_INVALID", "manifest must be one valid UTF-8 JSON object") from exc
    if not isinstance(payload, dict) or payload.get("schema") != MANIFEST_SCHEMA:
        raise ExecutionError("MANIFEST_SCHEMA_INVALID", f"manifest schema must be {MANIFEST_SCHEMA}")
    unknown = sorted(set(payload) - ALLOWED_MANIFEST_FIELDS)
    if unknown:
        raise ExecutionError("MANIFEST_FIELDS_INVALID", "ordinary manifests contain retired or unknown fields", {"fields": unknown})
    return make_config(
        project_root=payload.get("project_root"),
        mission_path=payload.get("mission_path"),
        run_root=payload.get("run_root"),
        run_id=payload.get("run_id"),
        source_thread_id=payload.get("source_thread_id"),
        model=payload.get("model", DEFAULT_MODEL),
        effort=payload.get("effort", DEFAULT_EFFORT),
        app_name=payload.get("app_name", DEFAULT_APP_NAME),
    )


def public_contract(config: ExecutionConfig) -> dict[str, Any]:
    return {
        "schema": MANIFEST_SCHEMA,
        "project_root": str(config.project_root),
        "mission_path": str(config.mission_path),
        "model": config.model,
        "effort": config.effort,
        "app_name": config.app_name,
        "oracle_request": {"model": config.model, "model_strategy": _model_strategy(config.model)},
        "chatgpt_url": CHATGPT_URL,
        "archive": "never",
        "temporary_chat": True,
        "personalization": "enabled-before-submit",
    }


def _model_strategy(model: str) -> str:
    return ORACLE_EXPLICIT_STRATEGY


def _composer_prompt(config: ExecutionConfig) -> str:
    return (
        f"@{config.app_name} Open exactly this approved project root in checkout mode: {config.project_root}. "
        f"Read and execute the mission file: {config.mission_path}. "
        "The mission defines the task intent and action authority; read it and applicable AGENTS.md fully before acting. "
        "Do not substitute another root or connector, and do not change ChatGPT account, privacy, app, or permission settings. Temporary-chat personalization is enabled by the runner before submission."
    )


def _slug(config: ExecutionConfig) -> str:
    words = (re.findall(r"[a-z0-9]+", config.project_root.name.casefold()) or ["project"])[:3]
    identity = hashlib.sha256(
        (str(config.project_root).casefold() + "\0" + config.run_id + "\0" + str(config.source_thread_id or "cli")).encode("utf-8")
    ).hexdigest()[:16]
    # Oracle truncates each slug word to ten characters. Split the identity so
    # our persisted name is exactly the session directory Oracle creates.
    return f"oracle-{words[0][:10]}-{identity[:8]}-{identity[8:]}"


def build_oracle_argv(
    config: ExecutionConfig,
    command: Sequence[str],
    output_path: Path,
    slug: str,
    *,
    cdp_port: int | None = None,
    browser_tab: str | None = None,
) -> list[str]:
    if browser_tab is not None and (
        cdp_port is None or TARGET_ID_RE.fullmatch(browser_tab) is None
    ):
        raise ExecutionError("ORACLE_BROWSER_TAB_INVALID", "browser_tab requires an exact remote Chrome target")
    browser_args = (
        [
            "--remote-chrome", f"127.0.0.1:{cdp_port}",
            *(["--browser-tab", browser_tab] if browser_tab is not None else []),
        ]
        if cdp_port is not None
        else [
            "--browser-manual-login",
            "--browser-keep-browser",
            "--browser-hide-window",
            "--browser-manual-login-profile-dir", str(output_path.parent / "browser-profile"),
        ]
    )
    return [
        *command,
        "--engine", "browser",
        "--model", config.model,
        "--browser-model-strategy", _model_strategy(config.model),
        "--browser-thinking-time", config.effort,
        "--chatgpt-url", CHATGPT_URL,
        "--browser-archive", "never",
        *browser_args,
        "--browser-timeout", "100m",
        "--verbose",
        "--slug", slug,
        "--prompt", _composer_prompt(config),
        "--write-output", str(output_path),
    ]


def _redacted_argv(argv: Sequence[str]) -> list[str]:
    value = list(argv)
    if "--prompt" in value:
        value[value.index("--prompt") + 1] = "<mission-handoff>"
    return value


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionError("RUN_STATE_INVALID", "run state is unavailable or invalid", {"path": str(path)}) from exc
    if not isinstance(payload, dict) or payload.get("schema") != STATE_SCHEMA:
        raise ExecutionError("RUN_STATE_SCHEMA_INVALID", f"run state schema must be {STATE_SCHEMA}")
    return payload


def _authorized_retry_successor(state_path: Path, state: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a proven one-shot successor without rewriting the parent run."""
    ticket_path = state_path.parent / RETRY_AUTHORIZATION_NAME
    try:
        if ticket_path.is_symlink() or not ticket_path.is_file() or state_path.is_symlink():
            return None
        ticket = json.loads(ticket_path.read_text(encoding="utf-8", errors="strict"))
        if not isinstance(ticket, dict):
            return None
        if (
            ticket.get("schema") != RETRY_AUTHORIZATION_SCHEMA
            or ticket.get("status") != "authorized"
            or ticket.get("authorization") != RETRY_AUTHORIZATION_VALUE
            or ticket.get("possible_duplicate_delivery") is not True
            or ticket.get("parent_run_id") != state.get("run_id")
            or ticket.get("parent_state_sha256") != _sha256(state_path)
            or ticket.get("project_root") != state.get("project_root")
            or ticket.get("source_thread_id") != state.get("source_thread_id")
            or state.get("status") != "attention_required"
            or state.get("submission") != "observed"
        ):
            return None
        ticket_selection = ticket.get("selection") if isinstance(ticket.get("selection"), dict) else {}
        if ticket_selection != {"model": "gpt-5.6-sol", "effort": "extended", "app_name": "codex"}:
            return None
        parent_artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
        expected_parent_files = {
            "parent_stdout_sha256": state_path.parent / "stdout.log",
            "parent_output_sha256": state_path.parent / "output.md",
            "parent_reconnect_stdout_sha256": state_path.parent / "reconnect-stdout.log",
            "parent_reconnect_stderr_sha256": state_path.parent / "reconnect-stderr.log",
        }
        if parent_artifacts.get("output") != str(state_path.parent / "output.md"):
            return None
        if parent_artifacts.get("stdout") != str(state_path.parent / "stdout.log"):
            return None
        for field, path in expected_parent_files.items():
            if path.is_symlink() or not path.is_file() or ticket.get(field) != _sha256(path):
                return None
        if (
            state.get("capture") != "absent"
            or parent_artifacts.get("output_bytes") != 0
            or expected_parent_files["parent_output_sha256"].stat().st_size != 0
        ):
            return None
        successor_id = str(ticket.get("successor_run_id") or "")
        if RUN_ID_RE.fullmatch(successor_id) is None:
            return None
        successor_dir = state_path.parent.parent / successor_id
        successor_state_path = successor_dir / "state.json"
        if successor_dir.is_symlink() or successor_state_path.is_symlink() or not successor_state_path.is_file():
            return None
        successor = _load_state(successor_state_path)
        successor_mission = successor.get("mission") if isinstance(successor.get("mission"), dict) else {}
        selection = successor.get("selection") if isinstance(successor.get("selection"), dict) else {}
        authorized_retry = successor.get("authorized_retry") if isinstance(successor.get("authorized_retry"), dict) else {}
        mission_path = str(ticket.get("successor_mission_path") or "")
        if (
            successor.get("run_id") != successor_id
            or successor.get("project_root") != state.get("project_root")
            or successor.get("source_thread_id") != state.get("source_thread_id")
            or successor_mission.get("path") != mission_path
            or successor_mission.get("sha256") != ticket.get("successor_mission_sha256")
            or any(selection.get(field) != ticket_selection.get(field) for field in ("model", "effort", "app_name"))
            or authorized_retry.get("parent_run_id") != state.get("run_id")
            or authorized_retry.get("authorization_sha256") != _sha256(ticket_path)
            or authorized_retry.get("possible_duplicate_delivery") is not True
        ):
            return None
        if successor.get("status") == "captured":
            model_check = successor.get("model_check") if isinstance(successor.get("model_check"), dict) else {}
            return successor if successor.get("capture") == "durable" and model_check.get("verified") is True else None
        if (
            successor.get("status") in UNRESOLVED_STATUSES
            and successor.get("submission") in {"unknown", "observed"}
        ):
            return successor
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ExecutionError, TypeError, ValueError):
        return None


def _initial_state(
    config: ExecutionConfig,
    run_dir: Path,
    slug: str,
    output_path: Path,
    command: Sequence[str],
    *,
    cdp_port: int,
) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "run_id": config.run_id,
        "source_thread_id": config.source_thread_id,
        "project_root": str(config.project_root),
        "approved_roots": [str(config.project_root)],
        "mission": {"path": str(config.mission_path), "sha256": config.mission_sha256},
        "selection": {
            "model": config.model,
            "effort": config.effort,
            "app_name": config.app_name,
            "requested_model": config.model,
            "model_strategy": _model_strategy(config.model),
        },
        "status": "prepared",
        "submission": "not_observed",
        "capture": "absent",
        "semantic_outcome": "unknown",
        "model_check": {"verified": False, "source": None},
        "oracle": {
            "version": RUNTIME.SUPPORTED_VERSION,
            "command": list(command),
            "slug": slug,
            "copy_profile": str(config.copy_profile),
            "manual_login_profile": str(run_dir / "browser-profile"),
            "expected_cdp_port": cdp_port,
            "binding": None,
        },
        "artifacts": {
            "output": str(output_path),
            "stdout": str(run_dir / "stdout.log"),
            "stderr": str(run_dir / "stderr.log"),
            "output_sha256": None,
            "output_bytes": 0,
        },
        "tab_close": {"status": "not_attempted"},
        "recovery": {"kind": "same-session-only", "resubmit": False},
    }


def _unresolved_duplicate(config: ExecutionConfig) -> dict[str, Any] | None:
    if not config.run_root.is_dir():
        return None
    for state_path in sorted(config.run_root.glob("*/state.json")):
        try:
            state = _load_state(state_path)
        except ExecutionError as exc:
            # This run root is the submission-ownership boundary.  An
            # unreadable state cannot prove that an earlier submission ended,
            # so fail closed and require attention to that exact run instead
            # of silently permitting a replacement send.
            return {
                "run_dir": str(state_path.parent),
                "status": "state_unreadable",
                "submission": "unknown",
                "state_error_code": exc.code,
            }
        if (
            state.get("project_root") == str(config.project_root)
            and state.get("source_thread_id") == config.source_thread_id
            and state.get("status") in UNRESOLVED_STATUSES
            and state.get("submission") in {"unknown", "observed"}
        ):
            if _authorized_retry_successor(state_path, state) is not None:
                continue
            return {"run_dir": str(state_path.parent), "status": state.get("status"), "submission": state.get("submission")}
    return None


def _authorize_uncertain_retry(
    config: ExecutionConfig,
    parent_run_dir: Path,
    *,
    confirmed: bool,
) -> dict[str, Any]:
    if not confirmed:
        raise ExecutionError(
            "RETRY_CONFIRMATION_REQUIRED",
            "retrying an uncertain submission requires explicit user authorization",
        )
    raw_parent_dir = Path(parent_run_dir).expanduser()
    if raw_parent_dir.is_symlink():
        raise ExecutionError("RETRY_PARENT_SCOPE_INVALID", "retry parent must not be a symlink")
    parent_dir = _absolute_path(raw_parent_dir, label="retry_parent_run_dir", must_exist=True)
    if parent_dir.parent.resolve() != config.run_root:
        raise ExecutionError("RETRY_PARENT_SCOPE_INVALID", "retry parent must be one exact run under the same run root")
    if config.model != "gpt-5.6-sol" or config.effort != "extended" or config.app_name != "codex":
        raise ExecutionError(
            "RETRY_SELECTION_NOT_AUTHORIZED",
            "this authorized retry is bound to GPT-5.6 Sol, Plus High, and the codex app",
        )
    owner = str(os.environ.get("CODEX_THREAD_ID") or "").strip().casefold()
    if not config.source_thread_id or owner != config.source_thread_id.casefold():
        raise ExecutionError("FOREIGN_TASK_RUN", "only the current owning Codex task may authorize this retry")
    state_path = parent_dir / "state.json"
    ticket_path = parent_dir / RETRY_AUTHORIZATION_NAME
    with _exact_run_lock(parent_dir):
        state = _load_state(state_path)
        if (
            state.get("status") != "attention_required"
            or state.get("submission") != "observed"
            or state.get("project_root") != str(config.project_root)
            or state.get("source_thread_id") != config.source_thread_id
            or state.get("capture") != "absent"
        ):
            raise ExecutionError("RETRY_PARENT_NOT_ELIGIBLE", "retry parent is not this task's exact uncertain execution")
        old_mission = state.get("mission") if isinstance(state.get("mission"), dict) else {}
        old_mission_path = _absolute_path(old_mission.get("path"), label="retry_parent_mission", must_exist=True)
        if (
            old_mission_path.is_symlink()
            or not _is_within(config.project_root, old_mission_path)
            or _sha256(old_mission_path) != old_mission.get("sha256")
        ):
            raise ExecutionError("RETRY_PARENT_MISSION_INVALID", "retry parent mission no longer matches its recorded hash")
        oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
        binding = oracle.get("binding") if isinstance(oracle.get("binding"), dict) else {}
        if binding.get("prompt_submitted") is not True:
            raise ExecutionError("RETRY_PARENT_NOT_ELIGIBLE", "retry parent lacks the recorded uncertain-send state")
        session_status = str(binding.get("session_status") or "").strip().casefold()
        artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
        expected_files = {
            "parent_stdout_sha256": parent_dir / "stdout.log",
            "parent_output_sha256": parent_dir / "output.md",
            "parent_reconnect_stdout_sha256": parent_dir / "reconnect-stdout.log",
            "parent_reconnect_stderr_sha256": parent_dir / "reconnect-stderr.log",
        }
        if artifacts.get("stdout") != str(expected_files["parent_stdout_sha256"]):
            raise ExecutionError("RETRY_PARENT_ARTIFACT_INVALID", "retry parent stdout is not bound to its exact run")
        if artifacts.get("output") != str(expected_files["parent_output_sha256"]):
            raise ExecutionError("RETRY_PARENT_ARTIFACT_INVALID", "retry parent output is not bound to its exact run")
        for path in expected_files.values():
            if path.is_symlink() or not path.is_file():
                raise ExecutionError("RETRY_PARENT_ARTIFACT_INVALID", "retry parent evidence is unavailable or unsafe")
        if artifacts.get("output_bytes") != 0 or expected_files["parent_output_sha256"].stat().st_size != 0:
            raise ExecutionError("RETRY_PARENT_HAS_OUTPUT", "retry parent already has an output artifact")
        stdout_text = expected_files["parent_stdout_sha256"].read_text(encoding="utf-8", errors="replace")
        reconnect_stdout_text = expected_files["parent_reconnect_stdout_sha256"].read_text(
            encoding="utf-8", errors="replace"
        )
        reconnect_stderr_text = expected_files["parent_reconnect_stderr_sha256"].read_text(
            encoding="utf-8", errors="replace"
        )
        prompt_commit_failure = (
            session_status == "error"
            and
            "Prompt did not appear in conversation before timeout" in stdout_text
            and '"userMatched":false' in stdout_text
            and '"turnsCount":0' in stdout_text
            and "Recovered ChatGPT conversation did not become ready in time" in reconnect_stderr_text
        )
        assistant_capture_failure = (
            session_status == "error"
            and
            "Activated send button" in stdout_text
            and "[browser] conversation url (post-submit) = " in stdout_text
            and "status=response streaming" in stdout_text
            and "Browser automation failure (assistant-response-unconfirmed)" in stdout_text
            and "Saved ChatGPT conversation did not load stable prior turns; refusing to submit follow-up as a fresh chat."
            in stdout_text
            and "Conversation snapshot:" in stdout_text
            and "No live ChatGPT tab matched session" in reconnect_stdout_text
            and "Attempting recovery by reopening the saved conversation URL" in reconnect_stdout_text
            and "Recovered ChatGPT conversation did not become ready in time" in reconnect_stderr_text
        )
        conversation_url = str(binding.get("conversation_url") or "").strip()
        try:
            binding_port = int(binding.get("port") or 0)
            expected_port = int(oracle.get("expected_cdp_port") or 0)
            reconnect_exit_code = int(state.get("reconnect_exit_code"))
        except (TypeError, ValueError):
            binding_port = 0
            expected_port = 0
            reconnect_exit_code = 0
        observer_lost_after_streaming = bool(
            session_status in {"running", "error"}
            and conversation_url
            and str(binding.get("host") or "").strip() in {"127.0.0.1", "localhost", "::1"}
            and binding_port > 0
            and binding_port == expected_port
            and TARGET_ID_RE.fullmatch(str(binding.get("target_id") or "")) is not None
            and reconnect_exit_code != 0
            and "Activated send button" in stdout_text
            and f"[browser] conversation url (post-submit) = {conversation_url}" in stdout_text
            and f"[browser] conversation url (assistant-wait) = {conversation_url}" in stdout_text
            and "Waiting for ChatGPT response" in stdout_text
            and "Confirming the capture is terminal (not a mid-stream/preamble capture)" in stdout_text
            and "status=response streaming" in stdout_text
            and "[browser] Waiting for ChatGPT response - " in stdout_text
            and "No live ChatGPT tab matched session" in reconnect_stdout_text
            and "Attempting recovery by reopening the saved conversation URL" in reconnect_stdout_text
            and "Recovered ChatGPT conversation did not become ready in time" in reconnect_stderr_text
        )
        if not (prompt_commit_failure or assistant_capture_failure or observer_lost_after_streaming):
            raise ExecutionError("RETRY_EVIDENCE_INSUFFICIENT", "run logs do not prove the exact failed prompt-commit and recovery path")
        active_pids = [
            int(pid)
            for pid in (state.get("oracle_process_pid"), state.get("reconnect_process_pid"))
            if pid and _pid_alive(pid)
        ]
        if active_pids:
            raise ExecutionError(
                "RETRY_PARENT_ACTIVE",
                "an Oracle or reconnect process still owns the uncertain parent run",
                {"active_pids": active_pids},
            )
        mission_path = _absolute_path(config.mission_path, label="successor_mission", must_exist=True)
        if mission_path.is_symlink() or not _is_within(config.project_root, mission_path):
            raise ExecutionError("MISSION_OUTSIDE_APPROVED_ROOT", "retry mission escaped the approved project root")
        if _sha256(mission_path) != config.mission_sha256:
            raise ExecutionError("MISSION_CHANGED", "retry mission changed after configuration")
        parent_state_sha256 = _sha256(state_path)
        evidence_hashes = {field: _sha256(path) for field, path in expected_files.items()}
        ticket = {
            "schema": RETRY_AUTHORIZATION_SCHEMA,
            "status": "authorized",
            "authorization": RETRY_AUTHORIZATION_VALUE,
            "possible_duplicate_delivery": True,
            "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "parent_run_id": state.get("run_id"),
            "parent_state_sha256": parent_state_sha256,
            **evidence_hashes,
            "source_thread_id": config.source_thread_id,
            "project_root": str(config.project_root),
            "successor_run_id": config.run_id,
            "successor_mission_path": str(mission_path),
            "successor_mission_sha256": config.mission_sha256,
            "selection": {"model": config.model, "effort": config.effort, "app_name": config.app_name},
        }
        if ticket_path.exists() or ticket_path.is_symlink():
            if ticket_path.is_symlink() or not ticket_path.is_file():
                raise ExecutionError("RETRY_AUTHORIZATION_INVALID", "existing retry authorization is unsafe")
            try:
                existing = json.loads(ticket_path.read_text(encoding="utf-8", errors="strict"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ExecutionError("RETRY_AUTHORIZATION_INVALID", "existing retry authorization is unreadable") from exc
            comparable = dict(existing) if isinstance(existing, dict) else {}
            comparable.pop("created_at_utc", None)
            requested = dict(ticket)
            requested.pop("created_at_utc", None)
            if comparable != requested:
                raise ExecutionError("RETRY_ALREADY_AUTHORIZED", "a different one-shot retry is already bound to this parent run")
            ticket = existing
        else:
            _write_json_atomic(ticket_path, ticket)
        return ticket


def _validate_pre_submit_restart(
    config: ExecutionConfig,
    run_dir: Path,
    parent_run_dir: Path,
    ticket: Mapping[str, Any],
) -> dict[str, Any]:
    """Allow the same authorized run to resume only after a narrowly proven no-send failure."""
    if run_dir.is_symlink() or run_dir.resolve() != config.run_root / config.run_id:
        raise ExecutionError("PRE_SUBMIT_RESTART_SCOPE_INVALID", "pre-submit restart must stay in the exact authorized run")
    state_path = run_dir / "state.json"
    if state_path.is_symlink() or not state_path.is_file():
        raise ExecutionError("PRE_SUBMIT_RESTART_NOT_SAFE", "existing run has no safe state to prove that it did not submit")
    state = _load_state(state_path)
    parent_ticket_path = parent_run_dir / RETRY_AUTHORIZATION_NAME
    if parent_ticket_path.is_symlink() or not parent_ticket_path.is_file():
        raise ExecutionError("RETRY_AUTHORIZATION_INVALID", "the exact parent retry authorization is unavailable")
    oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    mission = state.get("mission") if isinstance(state.get("mission"), dict) else {}
    selection = state.get("selection") if isinstance(state.get("selection"), dict) else {}
    authorization = state.get("authorized_retry") if isinstance(state.get("authorized_retry"), dict) else {}
    preflight = (
        state.get("personalization_preflight")
        if isinstance(state.get("personalization_preflight"), dict)
        else None
    )
    binding = oracle.get("binding") if isinstance(oracle.get("binding"), dict) else None
    expected_paths = {
        "output": run_dir / "output.md",
        "stdout": run_dir / "stdout.log",
        "stderr": run_dir / "stderr.log",
    }
    if (
        state.get("run_id") != config.run_id
        or state.get("source_thread_id") != config.source_thread_id
        or state.get("project_root") != str(config.project_root)
        or state.get("status") != "attention_required"
        or state.get("submission") != "not_observed"
        or state.get("capture") != "absent"
        or mission.get("path") != str(config.mission_path)
        or mission.get("sha256") != config.mission_sha256
        or selection.get("model") != "gpt-5.6-sol"
        or selection.get("effort") != "extended"
        or selection.get("app_name") != "codex"
        or ticket.get("successor_run_id") != config.run_id
        or authorization.get("parent_run_id") != parent_run_dir.name
        or authorization.get("authorization_sha256") != _sha256(parent_ticket_path)
        or authorization.get("possible_duplicate_delivery") is not True
        or artifacts.get("output") != str(expected_paths["output"])
        or artifacts.get("stdout") != str(expected_paths["stdout"])
        or artifacts.get("stderr") != str(expected_paths["stderr"])
        or artifacts.get("output_bytes") != 0
    ):
        raise ExecutionError(
            "PRE_SUBMIT_RESTART_NOT_SAFE",
            "existing run is not the exact authorized attempt with a proven no-submit failure",
        )
    if _pid_alive(state.get("oracle_process_pid")) or _pid_alive(state.get("reconnect_process_pid")):
        raise ExecutionError("PRE_SUBMIT_RESTART_ACTIVE", "a process still owns this pre-submit run")
    error = str(state.get("error") or "")
    normalized_error = error.strip().casefold()
    profile_lock = "winerror 32" in normalized_error and "cookies" in normalized_error
    personalization_unconfirmed = (
        normalized_error == "temporary-chat personalization could not be confirmed before submission"
    )
    profile_preparation_failure = bool(
        state.get("failure_stage") == "profile-preparation"
        and preflight is None
        and state.get("oracle_process_pid") is None
        and state.get("reconnect_process_pid") is None
        and binding is None
        and (profile_lock or personalization_unconfirmed)
    )

    exact_high_gate_error = (
        "Thinking time: menu not found (requested Extended); "
        "refusing to submit without confirmed High."
    )
    browser_high_gate_failure = False
    if (
        state.get("failure_stage") is None
        and preflight is not None
        and binding is not None
        and state.get("exit_code") == 1
        and state.get("reconnect_process_pid") is None
        and binding.get("prompt_submitted") is False
        and binding.get("session_status") == "error"
        and preflight.get("ok") is True
        and preflight.get("personalization") == "enabled"
        and preflight.get("startup_blank_tabs") == 0
        and preflight.get("page_count") == 1
        and isinstance(preflight.get("pid"), int)
        and preflight.get("pid") > 0
        and preflight.get("port") == oracle.get("expected_cdp_port")
        and preflight.get("port") == binding.get("port")
        and preflight.get("target_id") == binding.get("target_id")
        and preflight.get("conversation_url") == binding.get("conversation_url")
        and isinstance(preflight.get("browser_ws"), str)
        and preflight.get("browser_ws")
        and isinstance(binding.get("session_meta_path"), str)
        and binding.get("session_meta_path")
        and isinstance(state.get("model_check"), dict)
        and state["model_check"].get("verified") is False
    ):
        session = _session_meta(str(oracle.get("slug") or ""))
        live_binding = _binding_from_meta(*session) if session else None
        stdout_lines = _clean_lines(expected_paths["stdout"])
        diagnostic = None
        for line in reversed(stdout_lines):
            if "[browser] Model picker diagnostic:" not in line:
                continue
            try:
                diagnostic = json.loads(
                    line.split("[browser] Model picker diagnostic:", 1)[1].strip()
                )
            except json.JSONDecodeError:
                diagnostic = None
            break
        menu_shape_ok = False
        if isinstance(diagnostic, dict) and diagnostic.get("targetLevel") == "extended":
            menus = diagnostic.get("menus")
            if isinstance(menus, list):
                for menu in menus:
                    items = menu.get("items") if isinstance(menu, dict) else None
                    if not isinstance(items, list) or len(items) != 4:
                        continue
                    high = [
                        item for item in items
                        if isinstance(item, dict)
                        and item.get("role") == "menuitem"
                        and item.get("ariaLabel") == "Select model"
                        and item.get("text") == "High"
                    ]
                    power = [
                        item for item in items
                        if isinstance(item, dict)
                        and item.get("role") == "menuitem"
                        and item.get("ariaLabel") == "Power"
                        and item.get("text") in {"", None}
                    ]
                    checked = [
                        item for item in items
                        if isinstance(item, dict)
                        and item.get("role") == "menuitemradio"
                        and item.get("ariaChecked") == "true"
                    ]
                    radios = [
                        item for item in items
                        if isinstance(item, dict) and item.get("role") == "menuitemradio"
                    ]
                    if (
                        len(high) == 1
                        and len(power) == 1
                        and len(radios) == 2
                        and len(checked) == 1
                        and re.sub(r"\s+", "", str(checked[0].get("text") or "")).casefold()
                        == "gpt-5.6sol"
                    ):
                        menu_shape_ok = True
                        break
        browser_high_gate_failure = bool(
            live_binding == binding
            and menu_shape_ok
            and f"ERROR: {exact_high_gate_error}" in stdout_lines
            and f"User error (browser-automation): {exact_high_gate_error}" in stdout_lines
        )

    if not (profile_preparation_failure or browser_high_gate_failure):
        raise ExecutionError(
            "PRE_SUBMIT_RESTART_NOT_SAFE",
            "only a recognized, proven no-submit failure can reuse this authorization",
        )
    output_path = expected_paths["output"]
    if output_path.is_symlink() or (output_path.exists() and (not output_path.is_file() or output_path.stat().st_size != 0)):
        raise ExecutionError("PRE_SUBMIT_RESTART_NOT_SAFE", "pre-submit run has output that must be preserved")
    for label in ("stdout", "stderr"):
        path = expected_paths[label]
        if path.is_symlink() or not path.is_file():
            raise ExecutionError("PRE_SUBMIT_RESTART_NOT_SAFE", "pre-submit run has unsafe Oracle logs")
        if profile_preparation_failure and path.stat().st_size != 0:
            raise ExecutionError("PRE_SUBMIT_RESTART_NOT_SAFE", "profile-preparation failure has unexpected Oracle logs")
        if browser_high_gate_failure and label == "stdout" and path.stat().st_size == 0:
            raise ExecutionError("PRE_SUBMIT_RESTART_NOT_SAFE", "browser no-send failure is missing its Oracle evidence")
        if browser_high_gate_failure and label == "stderr" and path.stat().st_size != 0:
            raise ExecutionError("PRE_SUBMIT_RESTART_NOT_SAFE", "browser no-send failure has unexpected stderr")
    return state


def _replace_archive_path(source: Path, destination: Path) -> None:
    """Allow a just-closed Windows browser a short window to release profile handles."""
    attempts = 50 if os.name == "nt" else 1
    for attempt in range(attempts):
        try:
            source.replace(destination)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.1)


def _archive_pre_submit_attempt(run_dir: Path, state: Mapping[str, Any]) -> int:
    history_dir = run_dir / "pre-submit-attempts"
    if history_dir.is_symlink() or (history_dir.exists() and not history_dir.is_dir()):
        raise ExecutionError("PRE_SUBMIT_HISTORY_INVALID", "pre-submit history path is unsafe")
    index_path = history_dir / "index.json"
    if index_path.is_symlink():
        raise ExecutionError("PRE_SUBMIT_HISTORY_INVALID", "pre-submit history index must not be a symlink")
    if index_path.is_file():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8", errors="strict"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExecutionError("PRE_SUBMIT_HISTORY_INVALID", "pre-submit history index is unreadable") from exc
        if (
            not isinstance(index, dict)
            or index.get("schema") != PRE_SUBMIT_HISTORY_SCHEMA
            or index.get("run_id") != run_dir.name
            or not isinstance(index.get("attempts"), list)
            or any(not isinstance(item, dict) or item.get("status") != "archived" for item in index["attempts"])
        ):
            raise ExecutionError("PRE_SUBMIT_HISTORY_INVALID", "pre-submit history does not contain only complete archived attempts")
    elif history_dir.exists() and any(history_dir.iterdir()):
        raise ExecutionError("PRE_SUBMIT_HISTORY_INVALID", "pre-submit history has files without a valid index")
    else:
        index = {"schema": PRE_SUBMIT_HISTORY_SCHEMA, "run_id": run_dir.name, "attempts": []}

    allowed = {"state.json", "stdout.log", "stderr.log", "output.md", "browser-profile"}
    unexpected = [item.name for item in run_dir.iterdir() if item.name not in allowed | {"pre-submit-attempts", ".reconnect.lock"}]
    if unexpected:
        raise ExecutionError(
            "PRE_SUBMIT_RESTART_NOT_SAFE",
            "run directory contains unrecognized files that will not be moved",
            {"entries": sorted(unexpected)},
        )
    archive_number = len(index["attempts"]) + 1
    archive_dir = history_dir / f"attempt-{archive_number:03d}"
    if archive_dir.exists() or archive_dir.is_symlink():
        raise ExecutionError("PRE_SUBMIT_HISTORY_INVALID", "next pre-submit archive path already exists")
    entries: list[dict[str, Any]] = []
    for name in ("state.json", "stdout.log", "stderr.log", "output.md", "browser-profile"):
        path = run_dir / name
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink():
            raise ExecutionError("PRE_SUBMIT_RESTART_NOT_SAFE", "pre-submit artifacts must not be symlinks")
        if not path.is_file() and not path.is_dir():
            raise ExecutionError("PRE_SUBMIT_RESTART_NOT_SAFE", "pre-submit artifacts must be regular files or directories")
        entries.append({
            "name": name,
            "kind": "directory" if path.is_dir() else "file",
            "sha256": _sha256(path) if path.is_file() else None,
        })
    if not any(item["name"] == "state.json" for item in entries):
        raise ExecutionError("PRE_SUBMIT_RESTART_NOT_SAFE", "pre-submit state disappeared before archival")

    history_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir()
    record = {
        "attempt": archive_number,
        "status": "archiving",
        "failure_stage": state.get("failure_stage"),
        "submission": state.get("submission"),
        "state_sha256": _sha256(run_dir / "state.json"),
        "artifacts": entries,
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    index["attempts"].append(record)
    _write_json_atomic(index_path, index)
    moved: list[tuple[Path, Path]] = []
    try:
        for item in entries:
            source = run_dir / item["name"]
            destination = archive_dir / item["name"]
            _replace_archive_path(source, destination)
            moved.append((destination, source))
        if _sha256(archive_dir / "state.json") != record["state_sha256"]:
            raise ExecutionError("PRE_SUBMIT_ARCHIVE_VERIFY_FAILED", "archived state hash did not match its pre-move value")
        record["status"] = "archived"
        record["archive_dir"] = str(archive_dir)
        index["attempts"][-1] = record
        _write_json_atomic(index_path, index)
    except Exception as exc:
        rollback_error: Exception | None = None
        for source, destination in reversed(moved):
            if source.exists() and not destination.exists():
                try:
                    _replace_archive_path(source, destination)
                except Exception as rollback_exc:
                    rollback_error = rollback_exc
                    break
        if rollback_error is None:
            try:
                if archive_dir.exists() and not any(archive_dir.iterdir()):
                    archive_dir.rmdir()
                index["attempts"].pop()
                _write_json_atomic(index_path, index)
            except Exception as rollback_exc:
                rollback_error = rollback_exc
        if rollback_error is not None:
            raise ExecutionError(
                "PRE_SUBMIT_ARCHIVE_ROLLBACK_FAILED",
                "failed pre-submit archive could not be restored atomically",
                {"archive_dir": str(archive_dir), "original_error": str(exc), "rollback_error": str(rollback_error)},
            ) from rollback_error
        raise
    return archive_number


@contextmanager
def _submit_lock(config: ExecutionConfig, timeout_seconds: float = 30.0) -> Iterator[None]:
    lock_key = hashlib.sha256(
        (str(config.project_root).casefold() + "\0" + str(config.source_thread_id or "local")).encode("utf-8")
    ).hexdigest()
    lock_path = config.run_root / ".locks" / f"{lock_key}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.monotonic() + timeout_seconds
    acquired = False
    try:
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise ExecutionError("SUBMIT_LOCK_TIMEOUT", "another execution owns this task/root scope")
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def _exact_run_lock(run_dir: Path) -> Iterator[None]:
    lock_path = run_dir / ".reconnect.lock"
    handle = lock_path.open("a+b")
    acquired = False
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            raise ExecutionError("RUN_RECONNECT_ACTIVE", "another reconnect already owns this exact run") from exc
        yield
    finally:
        try:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _pid_alive(value: Any) -> bool:
    try:
        pid = int(value)
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetExitCodeProcess.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() != 87  # Unknown/access-denied is not proof of exit.
            try:
                code = wintypes.DWORD()
                return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
            finally:
                kernel.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def _subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt" or not hasattr(subprocess, "STARTUPINFO"):
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    return {"creationflags": 0x08000000, "startupinfo": startup}


def _reserve_cdp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _resolve_version(command: Sequence[str], run_factory: Callable[..., Any] = subprocess.run) -> str:
    completed = run_factory(
        [*command, "--version"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
        **_subprocess_kwargs(),
    )
    text = f"{getattr(completed, 'stdout', '') or ''}\n{getattr(completed, 'stderr', '') or ''}"
    if int(getattr(completed, "returncode", 1)) != 0 or RUNTIME.SUPPORTED_VERSION not in text:
        raise ExecutionError("ORACLE_VERSION_INVALID", "installed Oracle does not match the supported version")
    return f"oracle {RUNTIME.SUPPORTED_VERSION}"


def _session_meta(slug: str) -> tuple[Path, dict[str, Any]] | None:
    session_root = Path(os.environ.get("ORACLE_SESSION_ROOT") or (Path.home() / ".oracle" / "sessions")).expanduser().resolve()
    candidate = session_root / slug / "meta.json"
    if candidate.is_symlink() or candidate.parent.is_symlink():
        return None
    path = candidate.resolve()
    if not _is_within(session_root, path) or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return (path, payload) if isinstance(payload, dict) else None


def _binding_from_meta(meta_path: Path, meta: Mapping[str, Any]) -> dict[str, Any] | None:
    browser = meta.get("browser") if isinstance(meta.get("browser"), dict) else {}
    runtime = browser.get("runtime") if isinstance(browser.get("runtime"), dict) else {}
    host = str(runtime.get("chromeHost") or "").strip()
    target_id = str(runtime.get("chromeTargetId") or "").strip()
    try:
        port = int(runtime.get("chromePort"))
    except (TypeError, ValueError):
        port = 0
    if host not in {"127.0.0.1", "localhost", "::1"} or not 1 <= port <= 65535 or TARGET_ID_RE.fullmatch(target_id) is None:
        return None
    return {
        "session_meta_path": str(meta_path),
        "session_status": str(meta.get("status") or "").strip().casefold(),
        "host": host,
        "port": port,
        "target_id": target_id,
        "conversation_url": str(runtime.get("tabUrl") or "").strip() or None,
        "prompt_submitted": runtime.get("promptSubmitted") is True,
    }


def _oracle_session_terminal(meta: Mapping[str, Any], binding: Mapping[str, Any]) -> bool:
    status = str(meta.get("status") or "").strip().casefold()
    if status in TERMINAL_ORACLE_STATES:
        return True
    if binding.get("prompt_submitted") is not True:
        return False
    browser = meta.get("browser") if isinstance(meta.get("browser"), dict) else {}
    harvest = browser.get("harvest") if isinstance(browser.get("harvest"), dict) else {}
    integrity = harvest.get("integrity") if isinstance(harvest.get("integrity"), dict) else {}
    try:
        assistant_count = int(harvest.get("assistantCount") or 0)
    except (TypeError, ValueError):
        assistant_count = 0
    conversation_url = str(binding.get("conversation_url") or "")
    match = re.search(r"/c/([^/?#]+)", conversation_url)
    expected_conversation_id = match.group(1) if match else ""
    observed_conversation_id = str(integrity.get("observedConversationId") or "")
    return bool(
        str(harvest.get("state") or "").strip().casefold() in TERMINAL_ORACLE_STATES
        and assistant_count >= 1
        and harvest.get("stopExists") is False
        and integrity.get("status") == "matched"
        and str(harvest.get("targetId") or "") == str(binding.get("target_id") or "")
        and str(harvest.get("url") or "") == conversation_url
        and expected_conversation_id
        and observed_conversation_id == expected_conversation_id
    )


def _resolve_effective_oracle_slug(state: Mapping[str, Any], stdout_path: Path) -> str:
    oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
    requested = str(oracle.get("slug") or "").strip()
    persisted = str(oracle.get("effective_slug") or "").strip()
    if not requested:
        raise ExecutionError("RUN_RECOVERY_BINDING_INVALID", "run has no requested Oracle session slug")

    session_values: set[str] = set()
    reattach_values: set[str] = set()
    conversation_urls: set[str] = set()
    for line in _clean_lines(stdout_path):
        if line.startswith("Session:"):
            value = line.split(":", 1)[1].strip()
            if value:
                session_values.add(value)
        elif line.startswith("Reattach: oracle session "):
            value = line.removeprefix("Reattach: oracle session ").split()[0].strip()
            if value:
                reattach_values.add(value)
        elif line.startswith("[browser] conversation url (post-submit) = "):
            value = line.removeprefix("[browser] conversation url (post-submit) = ").strip()
            if value:
                conversation_urls.add(value)

    has_stdout_identity = bool(session_values or reattach_values)
    if has_stdout_identity:
        if len(session_values) != 1 or len(reattach_values) != 1 or session_values != reattach_values:
            raise ExecutionError(
                "RUN_RECOVERY_SESSION_AMBIGUOUS",
                "Oracle stdout does not identify one exact recoverable session",
            )
        candidate = next(iter(session_values))
    else:
        candidate = persisted or requested

    if candidate != requested and re.fullmatch(re.escape(requested) + r"-[0-9]+", candidate) is None:
        raise ExecutionError(
            "RUN_RECOVERY_SESSION_MISMATCH",
            "Oracle session identity is not derived from this run's requested slug",
        )
    if persisted and persisted != candidate:
        raise ExecutionError(
            "RUN_RECOVERY_SESSION_MISMATCH",
            "persisted Oracle session identity conflicts with stdout",
        )

    session = _session_meta(candidate)
    if session is None:
        if has_stdout_identity or persisted or candidate != requested:
            raise ExecutionError(
                "RUN_RECOVERY_SESSION_UNAVAILABLE",
                "the exact Oracle session identified by this run is unavailable",
            )
        return requested
    binding = _binding_from_meta(*session)
    if binding is None and candidate != requested:
        raise ExecutionError(
            "RUN_RECOVERY_BINDING_INVALID",
            "the exact Oracle session has no usable browser binding",
        )
    if candidate == requested:
        return requested

    try:
        expected_port = int(oracle.get("expected_cdp_port") or 0)
    except (TypeError, ValueError):
        expected_port = 0
    if binding.get("port") != expected_port:
        raise ExecutionError(
            "RUN_RECOVERY_BINDING_MISMATCH",
            "Oracle session CDP port does not match this run",
        )
    preflight = state.get("personalization_preflight")
    if isinstance(preflight, dict):
        try:
            preflight_port = int(preflight.get("port") or 0)
        except (TypeError, ValueError):
            preflight_port = 0
        if binding.get("port") != preflight_port or binding.get("target_id") != str(preflight.get("target_id") or ""):
            raise ExecutionError(
                "RUN_RECOVERY_BINDING_MISMATCH",
                "Oracle session browser target does not match this run",
            )
    if candidate != requested:
        if len(conversation_urls) != 1 or binding.get("conversation_url") != next(iter(conversation_urls)):
            raise ExecutionError(
                "RUN_RECOVERY_BINDING_MISMATCH",
                "collision session conversation URL is not exactly bound to this run",
            )
    elif len(conversation_urls) == 1 and binding.get("conversation_url") != next(iter(conversation_urls)):
        raise ExecutionError(
            "RUN_RECOVERY_BINDING_MISMATCH",
            "Oracle session conversation URL does not match this run",
        )
    return candidate


def _clean_lines(path: Path) -> list[str]:
    try:
        return [ANSI_RE.sub("", line).strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines()]
    except OSError:
        return []


def _selected_latest_row(rows: Any) -> bool:
    if not isinstance(rows, list):
        return False
    selected = [
        row for row in rows
        if isinstance(row, dict)
        and row.get("visible") is True
        and str(row.get("checked") or "").casefold() == "true"
    ]
    return len(selected) == 1 and re.sub(r"\s+", "", str(selected[0].get("text") or "")).casefold() in {"latest", "최신"}


def observed_model_check(
    stdout_path: Path,
    *,
    model: str,
    effort: str,
    session_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    lines = _clean_lines(stdout_path)
    expected_ordinal = {"pro": 5, "extra-high": 4, "extended": 3}.get(effort, 4)
    expected_total = 3 if effort == "extended" else 5
    if model in SUPPORTED_MODELS:
        model_line = next((line for line in reversed(lines) if MODEL_EVIDENCE_PREFIX in line), "")
        thinking_line = next((line for line in reversed(lines) if THINKING_EVIDENCE_PREFIX in line), "")

        def fields(line: str, prefix: str) -> dict[str, str]:
            payload = line.split(prefix, 1)[1].strip() if prefix in line else ""
            return {
                key.strip(): value.strip()
                for part in payload.split(";")
                if "=" in part
                for key, value in [part.split("=", 1)]
            }

        model_evidence = fields(model_line, MODEL_EVIDENCE_PREFIX)
        thinking_evidence = fields(thinking_line, THINKING_EVIDENCE_PREFIX)
        thinking_source = thinking_evidence.get("source")
        extended_ui_verified = (
            effort != "extended"
            or (
                thinking_source == "chatgpt-thinking-picker"
                and thinking_evidence.get("maximum") == "2"
            )
            or thinking_source == "chatgpt-thinking-picker-plus-high-menu"
        )
        expected_effort_labels = {
            "pro": {"pro"},
            "extra-high": {"extrahigh", "sehrhoch", "非常に高い", "極高", "极高", "매우높음"},
            "extended": {"high", "높음"},
        }.get(effort, set())
        native_verified = bool(
            # Oracle normalizes the public 'latest' alias before formatting logs.
            model_evidence.get("requestedKey", "").casefold() in (
                {"latest", "gpt-6-astra"} if model == "latest" else {"gpt-5.6-sol"}
            )
            and re.sub(r"\s+", "", model_evidence.get("target", "")).casefold() == (
                "latest" if model == "latest" else "gpt-5.6sol"
            )
            and model_evidence.get("resolvedLabel", "").strip() in (
                {"Latest", "最新", "최신"} if model == "latest" else {"GPT-5.6 Sol"}
            )
            and model_evidence.get("status") in {"already-selected", "switched"}
            and model_evidence.get("strategy") == "select"
            and model_evidence.get("verified") == "yes"
            and model_evidence.get("source") == "chatgpt-model-picker"
            and thinking_evidence.get("requestedLevel") == effort
            and thinking_evidence.get("status") in {"already-selected", "switched"}
            and re.sub(r"\s+", "", thinking_evidence.get("resolvedLabel", "")).casefold() in expected_effort_labels
            and extended_ui_verified
            and thinking_evidence.get("verified") == "yes"
            and thinking_source in {
                "chatgpt-thinking-picker",
                "chatgpt-thinking-picker-plus-high-menu",
            }
        )
        if native_verified:
            return {
                "verified": True,
                "model": model,
                "actual_model": "6 Pro" if model == "latest" and effort == "pro" else None,
                "effort": effort,
                "source": "oracle-native-selection-log",
            }

    if model == "gpt-5.6-sol" and effort == "extended" and isinstance(session_meta, Mapping):
        browser = session_meta.get("browser") if isinstance(session_meta.get("browser"), dict) else {}
        selection = browser.get("modelSelection") if isinstance(browser.get("modelSelection"), dict) else {}
        options = session_meta.get("options") if isinstance(session_meta.get("options"), dict) else {}
        browser_options = options.get("browserConfig") if isinstance(options.get("browserConfig"), dict) else {}
        legacy_verified = bool(
            "Model picker: GPT-5.6 Sol" in lines
            and "[browser] Thinking time: High (already selected)" in lines
            and selection.get("requestedModel") == "GPT-5.6 Sol"
            and selection.get("resolvedLabel") == "GPT-5.6 Sol"
            and selection.get("strategy") == "select"
            and selection.get("status") in {"already-selected", "switched"}
            and selection.get("verified") is True
            and selection.get("source") == "chatgpt-model-picker"
            and options.get("model") == "gpt-5.6-sol"
            and options.get("effectiveModelId") == "gpt-5.6-sol"
            and browser_options.get("desiredModel") == "GPT-5.6 Sol"
            and browser_options.get("modelStrategy") == "select"
            and browser_options.get("thinkingTime") == "extended"
        )
        if legacy_verified:
            return {
                "verified": True,
                "model": model,
                "actual_model": None,
                "effort": effort,
                "source": "oracle-020-session-meta-plus-selection-log",
            }

    if model == "latest":
        # Historical patched Oracle releases emitted one combined DOM proof.
        # Keep accepting it for recovery runs, after preferring 0.20's native evidence.
        for line in reversed(lines):
            if PICKER_PROOF_PREFIX not in line:
                continue
            try:
                proof = json.loads(line.split(PICKER_PROOF_PREFIX, 1)[1].strip())
            except json.JSONDecodeError:
                continue
            if not isinstance(proof, dict):
                continue
            slider = proof.get("slider") if isinstance(proof, dict) and isinstance(proof.get("slider"), dict) else {}
            composer = proof.get("composer") if isinstance(proof, dict) and isinstance(proof.get("composer"), dict) else {}
            signals = proof.get("modelSignals") if isinstance(proof.get("modelSignals"), list) else []
            composer_label = re.sub(r"\s+", "", str(composer.get("text") or "")).casefold()
            pro_visible = composer.get("visible") is True and (
                composer_label == "6pro" or (
                    composer_label in {"thinkingeffort", "추론수준", "사고수준", "성능", "pro"}
                    and any(isinstance(signal, dict) and signal.get("visible") is True
                            and re.sub(r"\s+", "", str(signal.get("text") or "")).casefold() == "6pro"
                            for signal in signals)
                )
            )
            verified = bool(
                proof.get("schema") == "codex.oracle.picker-dom-proof/v1"
                and proof.get("latestClicked") is True
                and isinstance(proof.get("stableReads"), int) and proof["stableReads"] >= 2
                and _selected_latest_row(proof.get("modelRows"))
                and slider.get("visible") is True
                and slider.get("ordinal") == expected_ordinal
                and slider.get("total") == expected_total
                and slider.get("displayOrdinal") == expected_ordinal
                and slider.get("displayTotal") == expected_total
                and (
                    effort != "pro"
                    or pro_visible
                )
            )
            return {"verified": verified, "model": model, "actual_model": "6 Pro" if verified and effort == "pro" else None,
                    "effort": effort, "source": "oracle-picker-dom-log"}
        return {"verified": False, "model": model, "effort": effort, "source": None}

    evidence_line = next((line for line in reversed(lines) if MODEL_EVIDENCE_PREFIX in line), "")
    thinking_line = next((line for line in reversed(lines) if THINKING_PREFIX in line), "")
    compact_evidence = re.sub(r"\s+", "", evidence_line).casefold()
    compact_thinking = re.sub(r"\s+", "", thinking_line).casefold()
    model_verified = all(
        token in compact_evidence
        for token in ("resolvedlabel=gpt-5.6sol", "strategy=select", "verified=yes")
    )
    effort_verified = (
        (effort == "pro" and ("pro,5of5" in compact_thinking or compact_thinking.endswith(":pro")))
        or (effort == "extra-high" and ("extrahigh" in compact_thinking or "4of5" in compact_thinking))
        or (effort == "extended" and ("high,3of3" in compact_thinking or "높음,3of3" in compact_thinking))
    )
    return {
        "verified": bool(model_verified and effort_verified),
        "model": model,
        "effort": effort,
        "source": "oracle-observed-selection-log" if model_verified and effort_verified else None,
    }


def _capture(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        return {"status": "absent", "sha256": None, "bytes": 0}
    with path.open("r+b") as handle:
        data = handle.read()
        os.fsync(handle.fileno())
    if not data.strip():
        return {"status": "absent", "sha256": None, "bytes": len(data)}
    return {"status": "durable", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _urlopen_json(url: str, opener: Callable[..., Any]) -> Any:
    with opener(urllib.request.Request(url, method="GET"), timeout=5) as response:
        return json.loads(response.read().decode("utf-8", errors="strict"))


def close_owned_tab(binding: Mapping[str, Any], *, opener: Callable[..., Any] = urllib.request.urlopen) -> dict[str, Any]:
    host = str(binding.get("host") or "")
    port = int(binding.get("port") or 0)
    target_id = str(binding.get("target_id") or "")
    if host not in {"127.0.0.1", "localhost", "::1"} or not 1 <= port <= 65535 or TARGET_ID_RE.fullmatch(target_id) is None:
        return {"status": "invalid-binding"}
    authority = f"http://{'[::1]' if host == '::1' else host}:{port}"
    try:
        before = _urlopen_json(f"{authority}/json/list", opener)
        targets = [
            item
            for item in before
            if isinstance(item, dict)
            and (item.get("id") == target_id or item.get("targetId") == target_id)
            and item.get("type") == "page"
        ]
        if not targets:
            return {"status": "already-closed", "target_id": target_id}
        expected_url = str(binding.get("conversation_url") or "")
        actual_url = str(targets[0].get("url") or "")
        if not expected_url or actual_url != expected_url:
            return {"status": "binding-mismatch", "target_id": target_id}
        with opener(
            urllib.request.Request(f"{authority}/json/close/{urllib.parse.quote(target_id, safe='')}", method="GET"),
            timeout=5,
        ) as response:
            response.read()
        after = _urlopen_json(f"{authority}/json/list", opener)
        if any(
            isinstance(item, dict)
            and (item.get("id") == target_id or item.get("targetId") == target_id)
            and item.get("type") == "page"
            for item in after
        ):
            return {"status": "close-unconfirmed", "target_id": target_id}
        return {"status": "closed", "target_id": target_id}
    except Exception as exc:
        return {"status": "close-failed", "target_id": target_id, "error": str(exc)}


def _finalize_capture(
    state_path: Path,
    state: dict[str, Any],
    *,
    stdout_path: Path,
    output_path: Path,
    tab_closer: Callable[[Mapping[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    effective_slug = _resolve_effective_oracle_slug(state, stdout_path)
    state["oracle"]["effective_slug"] = effective_slug
    session = _session_meta(effective_slug)
    binding = _binding_from_meta(*session) if session else None
    expected_port = int((state.get("oracle") or {}).get("expected_cdp_port") or 0)
    preflight = state.get("personalization_preflight")
    if isinstance(preflight, dict):
        try:
            preflight_port = int(preflight.get("port") or 0)
        except (TypeError, ValueError):
            preflight_port = 0
        preflight_target = str(preflight.get("target_id") or "")
        if binding and (
            binding.get("port") != expected_port
            or binding.get("port") != preflight_port
            or binding.get("target_id") != preflight_target
        ):
            binding = None
    elif binding and binding.get("port") != expected_port:
        binding = None
    capture = _capture(output_path)
    selection = state["selection"]
    model_check = observed_model_check(
        stdout_path,
        model=selection["model"],
        effort=selection["effort"],
        session_meta=session[1] if session else None,
    )
    if binding and binding.get("prompt_submitted"):
        submission = "observed"
    elif binding:
        submission = "not_observed"
    else:
        submission = str(state.get("submission") or "unknown")
    oracle_terminal = bool(binding and session and _oracle_session_terminal(session[1], binding))
    captured = capture["status"] == "durable" and model_check["verified"] and oracle_terminal
    state.update(
        {
            "status": "captured" if captured else "attention_required",
            "submission": submission,
            "capture": capture["status"],
            "semantic_outcome": "unknown",
            "model_check": model_check,
        }
    )
    state["oracle"]["binding"] = binding
    state["artifacts"].update(
        {"output_sha256": capture["sha256"], "output_bytes": capture["bytes"]}
    )
    # Persist the complete capture evidence before touching the browser target.
    _write_json_atomic(state_path, state)
    if not captured or binding is None or isinstance(preflight, dict):
        return state
    close_result = tab_closer(binding)
    state["tab_close"] = close_result
    if close_result.get("status") not in {"closed", "already-closed"}:
        state["status"] = "attention_required"
    _write_json_atomic(state_path, state)
    return state


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("CODEX_ORACLE_TEMPORARY_PERSONALIZATION", None)
    environment.pop("CODEX_ORACLE_TEMPORARY_PERSONALIZATION_HELPER", None)
    for key in (
        "ORACLE_TASK_OUTCOME_TERMINAL_CONTRACT",
        "ORACLE_TERMINAL_MARKER_CONFIRM_CYCLES",
        "ORACLE_TERMINAL_MARKER_MIN_STABLE_MS",
    ):
        environment.pop(key, None)
    return environment


def _start_personalized_browser(
    command: Sequence[str],
    profile_path: Path,
    cdp_port: int,
    *,
    run_factory: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    if len(command) < 2:
        raise ExecutionError("ORACLE_COMMAND_INVALID", "the resolved Oracle command has no package entry point")
    node = Path(command[0]).expanduser().resolve()
    entry = Path(command[1]).expanduser().resolve()
    package_root = entry.parents[2]
    helper = Path(__file__).with_name("oracle_temporary_personalization_preflight.mjs").resolve()
    completed = run_factory(
        [str(node), str(helper), str(package_root), str(Path(__file__).with_name("oracle_temporary_personalization.mjs").resolve()),
         str(profile_path.resolve()), str(cdp_port), CHATGPT_URL],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=120,
        **_subprocess_kwargs(),
    )
    if completed.returncode != 0:
        raise ExecutionError(
            "TEMPORARY_PERSONALIZATION_UNCONFIRMED",
            "temporary-chat personalization could not be confirmed before submission",
            {"detail": (completed.stderr or completed.stdout).strip()[-1200:]},
        )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ExecutionError("TEMPORARY_PERSONALIZATION_UNCONFIRMED", "personalization preflight returned invalid evidence") from exc
    if (
        not isinstance(result, dict)
        or result.get("ok") is not True
        or int(result.get("port") or 0) != cdp_port
        or int(result.get("pid") or 0) <= 0
        or not TARGET_ID_RE.fullmatch(str(result.get("target_id") or ""))
        or not str(result.get("conversation_url") or "").startswith("https://chatgpt.com/")
        or not str(result.get("browser_ws") or "").startswith(f"ws://127.0.0.1:{cdp_port}/")
    ):
        raise ExecutionError("TEMPORARY_PERSONALIZATION_UNCONFIRMED", "personalization preflight evidence is incomplete")
    return result


def _cleanup_personalized_browser(
    preflight: Mapping[str, Any],
    command: Sequence[str],
    *,
    expected_url: str | None = None,
    run_factory: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    binding = {
        "port": int(preflight.get("port") or 0),
        "target_id": str(preflight.get("target_id") or ""),
        "conversation_url": str(expected_url or preflight.get("conversation_url") or ""),
    }
    helper = Path(__file__).with_name("oracle_temporary_personalization_preflight.mjs").resolve()
    completed = run_factory(
        [str(Path(command[0]).expanduser().resolve()), str(helper), "--close", str(binding["port"]),
         str(preflight.get("browser_ws") or ""), binding["target_id"], binding["conversation_url"]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=15,
        **_subprocess_kwargs(),
    )
    if completed.returncode != 0:
        return {"status": "browser-close-unconfirmed",
                "detail": (completed.stderr or completed.stdout).strip()[-800:]}
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"status": "browser-close-unconfirmed", "detail": "cleanup helper returned invalid evidence"}
    if not isinstance(result, dict) or result.get("ok") is not True or result.get("closed") is not True:
        return {"status": "browser-close-unconfirmed", "detail": "cleanup helper did not confirm browser closure"}
    return {"status": "closed", "target_id": binding["target_id"]}


def _prepare_run_profile(config: ExecutionConfig, run_dir: Path) -> Path:
    """Copy the seed into an owned, retained profile; never pass Oracle copy-profile."""
    destination = run_dir / "browser-profile"
    excluded = {"Cache", "Code Cache", "GPUCache", "ShaderCache", "Crashpad", "Sessions",
                "SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort", "LOCK"}

    def ignore(directory: str, names: list[str]) -> list[str]:
        ignored = []
        for name in names:
            candidate = Path(directory) / name
            attributes = getattr(candidate.lstat(), "st_file_attributes", 0)
            if name in excluded or candidate.is_symlink() or attributes & 0x400:
                ignored.append(name)
        return ignored

    def native_path(path: Path) -> Path:
        value = str(path.absolute())
        if os.name != "nt" or value.startswith("\\\\?\\"):
            return path
        return Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)

    copy_destination = native_path(destination)
    shutil.copytree(native_path(config.copy_profile), copy_destination, ignore=ignore)
    for preferences in copy_destination.glob("*/Preferences"):
        value = json.loads(preferences.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or any(
            key in value and not isinstance(value[key], dict) for key in ("profile", "session")
        ):
            raise ExecutionError("PROFILE_PREFERENCES_INVALID", "copied startup preferences are invalid")
        value.setdefault("profile", {}).update(exit_type="Normal", exited_cleanly=True)
        value.setdefault("session", {}).update(restore_on_startup=5, startup_urls=[])
        _write_json_atomic(preferences, value)
    return destination


def execute_config(
    config: ExecutionConfig,
    *,
    dry_run: bool = False,
    command_resolver: Callable[[], list[str]] = RUNTIME.resolve_default_oracle_command,
    version_resolver: Callable[[Sequence[str]], str] = _resolve_version,
    compat_factory: Callable[..., Mapping[str, Any]] = COMPAT.ensure_oracle_compatibility,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    tab_closer: Callable[[Mapping[str, Any]], dict[str, Any]] = close_owned_tab,
    browser_preflight: Callable[[Sequence[str], Path, int], dict[str, Any]] = _start_personalized_browser,
    browser_cleanup: Callable[..., dict[str, Any]] = _cleanup_personalized_browser,
    retry_from_run: Path | None = None,
    confirm_uncertain_retry: bool = False,
) -> dict[str, Any]:
    logical_command = ["npx", "-y", f"@steipete/oracle@{RUNTIME.SUPPORTED_VERSION}"]
    run_dir = config.run_root / config.run_id
    output_path = run_dir / "output.md"
    slug = _slug(config)
    cdp_port = _reserve_cdp_port()
    if dry_run:
        argv = build_oracle_argv(config, logical_command, output_path, slug, cdp_port=cdp_port)
        return {
            "ok": True,
            "status": "dry-run",
            "run_dir": str(run_dir),
            "contract": public_contract(config),
            "argv": _redacted_argv(argv),
            "writes_performed": False,
        }
    with _submit_lock(config):
        if not config.copy_profile.is_dir() or config.copy_profile.is_symlink():
            raise ExecutionError("SIGNED_IN_PROFILE_UNAVAILABLE", "the signed-in Oracle profile seed is unavailable or unsafe")
        # The manifest binds the user-authorized root; the chosen app enforces
        # its own access. Do not require an unrelated legacy DevSpace config or
        # write a qualification receipt for every ordinary mission.
        if not config.project_root.is_dir() or config.project_root.resolve() != config.project_root:
            raise ExecutionError("PROJECT_ROOT_UNAVAILABLE", "the exact project root changed before launch")
        if (
            config.mission_path.is_symlink()
            or config.mission_path.resolve() != config.mission_path
            or not _is_within(config.project_root, config.mission_path)
        ):
            raise ExecutionError("MISSION_OUTSIDE_APPROVED_ROOT", "mission_path changed or escaped project_root before launch")
        if _sha256(config.mission_path) != config.mission_sha256:
            raise ExecutionError("MISSION_CHANGED", "mission changed after configuration; prepare it again before submitting")
        if run_dir.is_symlink():
            raise ExecutionError("RUN_ID_UNSAFE", "run_id directory must not be a symlink", {"run_dir": str(run_dir)})
        existing_run_dir = run_dir.exists()
        duplicate = _unresolved_duplicate(config)
        retry_ticket: dict[str, Any] | None = None
        requested_parent: Path | None = None
        if duplicate:
            if retry_from_run is None:
                raise ExecutionError(
                    "RUN_RECONNECT_REQUIRED",
                    "an unresolved execution already owns this task/root; reconnect it or explicitly authorize one exact retry",
                    duplicate,
                )
            requested_parent = _absolute_path(retry_from_run, label="retry_parent_run_dir", must_exist=True)
            if requested_parent != Path(str(duplicate.get("run_dir"))).resolve():
                raise ExecutionError(
                    "RUN_RECONNECT_REQUIRED",
                    "authorized retry must bind the exact unresolved parent run",
                    duplicate,
                )
            retry_ticket = _authorize_uncertain_retry(
                config,
                requested_parent,
                confirmed=confirm_uncertain_retry,
            )
        elif retry_from_run is not None or confirm_uncertain_retry:
            raise ExecutionError(
                "RETRY_PARENT_NOT_UNRESOLVED",
                "uncertain retry authorization is valid only while its exact parent remains unresolved",
            )
        state_path = run_dir / "state.json"
        stdout_path = run_dir / "stdout.log"
        stderr_path = run_dir / "stderr.log"
        if existing_run_dir and (retry_ticket is None or requested_parent is None):
            raise ExecutionError("RUN_ID_EXISTS", "run_id already exists", {"run_dir": str(run_dir)})
        with ExitStack() as run_locks:
            previous_state: dict[str, Any] | None = None
            if existing_run_dir:
                run_locks.enter_context(_exact_run_lock(run_dir))
                previous_state = _validate_pre_submit_restart(config, run_dir, requested_parent, retry_ticket)
            command = command_resolver()
            version = version_resolver(command)
            compat_factory(version, **COMPAT.node_runtime_kwargs(command))
            archived_attempts = 0
            if previous_state is not None:
                previous_preflight = (
                    previous_state.get("personalization_preflight")
                    if isinstance(previous_state.get("personalization_preflight"), dict)
                    else None
                )
                if previous_preflight is not None:
                    previous_binding = (
                        (previous_state.get("oracle") or {}).get("binding")
                        if isinstance((previous_state.get("oracle") or {}).get("binding"), dict)
                        else {}
                    )
                    if _pid_alive(previous_preflight.get("pid")):
                        cleanup = browser_cleanup(
                            previous_preflight,
                            command,
                            expected_url=str(previous_binding.get("conversation_url") or ""),
                        )
                        if cleanup.get("status") != "closed":
                            raise ExecutionError(
                                "PRE_SUBMIT_RESTART_BROWSER_ACTIVE",
                                "the exact owned no-send browser could not be closed before archival",
                                cleanup,
                            )
                    else:
                        cleanup = {
                            "status": "already-closed",
                            "target_id": previous_preflight.get("target_id"),
                        }
                    previous_state["browser_cleanup"] = cleanup
                    _write_json_atomic(state_path, previous_state)
                archived_attempts = _archive_pre_submit_attempt(run_dir, previous_state)
            else:
                run_dir.mkdir(parents=True, exist_ok=False)
            state = _initial_state(config, run_dir, slug, output_path, command, cdp_port=cdp_port)
            if archived_attempts:
                state["pre_submit_attempts_archived"] = archived_attempts
            if retry_ticket is not None:
                ticket_path = requested_parent / RETRY_AUTHORIZATION_NAME
                state["authorized_retry"] = {
                    "parent_run_id": retry_ticket["parent_run_id"],
                    "authorization_sha256": _sha256(ticket_path),
                    "possible_duplicate_delivery": True,
                }
            _write_json_atomic(state_path, state)
            stdout_path.touch()
            stderr_path.touch()
        launch_attempted = False
        preflight: dict[str, Any] | None = None
        try:
            profile_path = _prepare_run_profile(config, run_dir)
            preflight = browser_preflight(command, profile_path, cdp_port)
            state["personalization_preflight"] = preflight
            _write_json_atomic(state_path, state)
            argv = build_oracle_argv(
                config,
                command,
                output_path,
                slug,
                cdp_port=cdp_port,
                browser_tab=str(preflight["target_id"]),
            )
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                process = popen_factory(
                    argv,
                    cwd=str(config.project_root),
                    env=_child_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    shell=False,
                    **_subprocess_kwargs(),
                )
                launch_attempted = True
                state.update({"status": "running", "submission": "unknown", "oracle_process_pid": getattr(process, "pid", None)})
                _write_json_atomic(state_path, state)
                state["exit_code"] = int(process.wait())
        except Exception as exc:
            if preflight is not None and not launch_attempted:
                state["browser_cleanup"] = browser_cleanup(preflight, command)
            state.update({"status": "attention_required",
                          "submission": "unknown" if launch_attempted else "not_observed",
                          "failure_stage": "oracle-launch-or-observation" if launch_attempted else "profile-preparation",
                          "error": str(exc)})
            _write_json_atomic(state_path, state)
            return {"ok": False, "status": state["status"], "run_dir": str(run_dir), "result": state}
        state = _finalize_capture(
            state_path,
            state,
            stdout_path=stdout_path,
            output_path=output_path,
            tab_closer=tab_closer,
        )
        if state["status"] == "captured" and preflight is not None:
            cleanup = browser_cleanup(
                preflight,
                command,
                expected_url=str(state["oracle"]["binding"]["conversation_url"]),
            )
            state["browser_cleanup"] = cleanup
            if cleanup.get("status") != "closed":
                state["status"] = "attention_required"
            _write_json_atomic(state_path, state)
        return {"ok": state["status"] == "captured", "status": state["status"], "run_dir": str(run_dir), "result": state}


def execute_manifest(path: Path, **kwargs: Any) -> dict[str, Any]:
    return execute_config(load_manifest(path), **kwargs)


def reconnect_run(
    run_dir: Path,
    *,
    dry_run: bool = False,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    tab_closer: Callable[[Mapping[str, Any]], dict[str, Any]] = close_owned_tab,
    browser_cleanup: Callable[..., dict[str, Any]] = _cleanup_personalized_browser,
) -> dict[str, Any]:
    directory = _absolute_path(run_dir, label="run_dir", must_exist=True)
    state_path = directory / "state.json"
    if dry_run:
        return _reconnect_locked(
            directory, state_path, _load_state(state_path), dry_run=True,
            popen_factory=popen_factory, tab_closer=tab_closer,
            browser_cleanup=browser_cleanup,
        )
    with _exact_run_lock(directory):
        state = _load_state(state_path)
        return _reconnect_locked(
            directory,
            state_path,
            state,
            dry_run=dry_run,
            popen_factory=popen_factory,
            tab_closer=tab_closer,
            browser_cleanup=browser_cleanup,
        )


def _reconnect_locked(
    directory: Path,
    state_path: Path,
    state: dict[str, Any],
    *,
    dry_run: bool,
    popen_factory: Callable[..., Any],
    tab_closer: Callable[[Mapping[str, Any]], dict[str, Any]],
    browser_cleanup: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    if (directory / RETRY_AUTHORIZATION_NAME).exists() or (directory / RETRY_AUTHORIZATION_NAME).is_symlink():
        raise ExecutionError(
            "RUN_SUPERSEDED_BY_AUTHORIZED_RETRY",
            "this exact run has an explicitly authorized successor; reconnect the successor run instead",
        )
    owner = str(state.get("source_thread_id") or "").strip().casefold()
    current = str(os.environ.get("CODEX_THREAD_ID") or "").strip().casefold()
    if owner and owner != current:
        raise ExecutionError("FOREIGN_TASK_RUN", "only the owning Codex task may reconnect this execution")
    if state.get("status") == "captured":
        raise ExecutionError("RUN_ALREADY_CAPTURED", "captured executions do not need reconnect")
    if state.get("submission") == "not_observed":
        raise ExecutionError(
            "RUN_NOT_SUBMITTED",
            "this run has no observed prompt submission; only its exact safe pre-submit execute retry may resume it",
        )
    if state.get("status") == "running" and _pid_alive(state.get("oracle_process_pid")):
        raise ExecutionError("RUN_STILL_ACTIVE", "the original Oracle process is still active; do not start another observer")
    oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
    command = oracle.get("command")
    requested_slug = str(oracle.get("slug") or "")
    output_path = Path(str((state.get("artifacts") or {}).get("output") or ""))
    if output_path != directory / "output.md" or output_path.is_symlink():
        raise ExecutionError("RUN_RECOVERY_BINDING_INVALID", "output must belong to the exact run directory")
    stdout_path = Path(str((state.get("artifacts") or {}).get("stdout") or ""))
    if stdout_path != directory / "stdout.log" or stdout_path.is_symlink():
        raise ExecutionError("RUN_RECOVERY_BINDING_INVALID", "model observation must belong to the exact run directory")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command) or not requested_slug:
        raise ExecutionError("RUN_RECOVERY_BINDING_INVALID", "run has no exact Oracle command/slug binding")
    slug = _resolve_effective_oracle_slug(state, stdout_path)
    argv = [*command, "session", slug, "--live", "--write-output", str(output_path)]
    if "--prompt" in argv or "-p" in argv:
        raise ExecutionError("RECOVERY_PROMPT_FORBIDDEN", "reconnect must never contain a prompt")
    if dry_run:
        return {"ok": True, "status": "dry-run", "run_dir": str(directory), "argv": argv, "resubmit": False}
    state["oracle"]["effective_slug"] = slug
    _write_json_atomic(state_path, state)
    output_path.touch(exist_ok=True)
    reconnect_stdout = directory / "reconnect-stdout.log"
    reconnect_stderr = directory / "reconnect-stderr.log"
    try:
        with reconnect_stdout.open("ab") as stdout, reconnect_stderr.open("ab") as stderr:
            process = popen_factory(
                argv,
                cwd=str(state["project_root"]),
                env=_child_environment(),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                shell=False,
                **_subprocess_kwargs(),
            )
            state.update({"status": "running", "reconnect_process_pid": getattr(process, "pid", None)})
            _write_json_atomic(state_path, state)
            state["reconnect_exit_code"] = int(process.wait())
    except Exception as exc:
        state.update({"status": "attention_required", "error": str(exc)})
        _write_json_atomic(state_path, state)
        return {"ok": False, "status": state["status"], "run_dir": str(directory), "result": state}
    # The original stdout contains the one model/effort proof; reconnect never
    # requests or repeats that check.
    state = _finalize_capture(
        state_path,
        state,
        stdout_path=Path(str(state["artifacts"]["stdout"])),
        output_path=output_path,
        tab_closer=tab_closer,
    )
    preflight = state.get("personalization_preflight")
    if state["status"] == "captured" and isinstance(preflight, dict):
        cleanup = browser_cleanup(
            preflight,
            command,
            expected_url=str(state["oracle"]["binding"]["conversation_url"]),
        )
        state["browser_cleanup"] = cleanup
        if cleanup.get("status") != "closed":
            state["status"] = "attention_required"
        _write_json_atomic(state_path, state)
    return {"ok": state["status"] == "captured", "status": state["status"], "run_dir": str(directory), "result": state}
