from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_oracle_execute.py"


def load_module():
    name = "chatgpt_oracle_execute_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def executor():
    return load_module()


def fake_preflight(_command, _profile, port):
    return {
        "ok": True,
        "pid": 1234,
        "port": port,
        "target_id": "B" * 32,
        "conversation_url": "https://chatgpt.com/?temporary-chat=true",
        "browser_ws": f"ws://127.0.0.1:{port}/devtools/browser/test",
    }


def fake_cleanup(*_args, **_kwargs):
    return {"status": "closed"}


def browser_port(argv):
    return int(argv[argv.index("--remote-chrome") + 1].rsplit(":", 1)[1])


def test_process_probe_does_not_signal_on_windows(executor, monkeypatch):
    import os

    if os.name == "nt":
        monkeypatch.setattr(os, "kill", lambda *args: pytest.fail("Windows probe must never signal"))
    assert executor._pid_alive(os.getpid()) is True
    assert executor._pid_alive(-1) is False


def test_reconnect_lock_rejects_overlap_and_releases(executor, tmp_path):
    with executor._exact_run_lock(tmp_path):
        with pytest.raises(executor.ExecutionError, match="another reconnect"):
            with executor._exact_run_lock(tmp_path):
                pytest.fail("overlapping reconnect acquired the lock")
    # A persistent lock file is harmless after its OS lock has been released.
    with executor._exact_run_lock(tmp_path):
        pass


@pytest.fixture
def execution_paths(tmp_path: Path, monkeypatch, executor):
    root = tmp_path / "project"
    root.mkdir()
    mission = root / "mission.md"
    mission.write_text("Implement the requested change.\n", encoding="utf-8")
    run_root = tmp_path / "host-state" / "runs"
    session_root = tmp_path / "oracle-sessions"
    profile = tmp_path / "signed-in-profile"
    profile.mkdir()
    monkeypatch.setenv("ORACLE_SESSION_ROOT", str(session_root))
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(profile))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    return root, mission, run_root, session_root


def test_config_binds_ambient_task_owner(executor, execution_paths, monkeypatch):
    root, mission, run_root, _ = execution_paths
    owner = "01234567-89ab-cdef-0123-456789abcdef"
    monkeypatch.setenv("CODEX_THREAD_ID", owner)
    config = executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    assert config.source_thread_id == owner
    assert executor.manifest_payload(config)["source_thread_id"] == owner


def test_slug_survives_oracle_normalization(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    slug = executor._slug(config)
    assert 3 <= len(slug.split("-")) <= 5
    assert all(len(word) <= 10 for word in slug.split("-"))


def test_child_does_not_request_a_runtime_personalization_patch(executor, monkeypatch):
    monkeypatch.setenv("CODEX_ORACLE_TEMPORARY_PERSONALIZATION", "disabled")
    environment = executor._child_environment()
    assert "CODEX_ORACLE_TEMPORARY_PERSONALIZATION" not in environment
    assert "CODEX_ORACLE_TEMPORARY_PERSONALIZATION_HELPER" not in environment


def test_personalization_preflight_is_bound_before_remote_oracle(executor, tmp_path):
    node = tmp_path / "node.exe"
    entry = tmp_path / "oracle" / "dist" / "bin" / "oracle-cli.js"
    profile = tmp_path / "profile"
    entry.parent.mkdir(parents=True)
    profile.mkdir()
    node.write_bytes(b"node")
    entry.write_bytes(b"oracle")
    observed = []

    def run(argv, **kwargs):
        observed.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "ok": True, "pid": 1234, "port": 49152, "target_id": "B" * 32,
            "conversation_url": "https://chatgpt.com/?temporary-chat=true",
            "browser_ws": "ws://127.0.0.1:49152/devtools/browser/test",
        }))

    result = executor._start_personalized_browser([str(node), str(entry)], profile, 49152, run_factory=run)
    assert result["target_id"] == "B" * 32
    assert observed[0][0][-2:] == ["49152", "https://chatgpt.com/?temporary-chat=true"]


def test_cleanup_delegates_exact_browser_and_tab_identity_once(executor):
    observed = []

    def run(argv, **kwargs):
        observed.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stderr="", stdout='{"ok":true,"closed":true}')

    preflight = fake_preflight([], Path(), 49152)
    expected_url = "https://chatgpt.com/c/owned-temporary-run"
    result = executor._cleanup_personalized_browser(
        preflight, ["node", "oracle"], expected_url=expected_url, run_factory=run
    )
    assert result == {"status": "closed", "target_id": "B" * 32}
    assert observed[0][0][-4:] == [
        "49152", preflight["browser_ws"], preflight["target_id"], expected_url
    ]


def test_pre_submit_archive_retries_transient_windows_profile_release(executor, tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "state.json").write_text(
        json.dumps({"failure_stage": None, "submission": "not_observed"}), encoding="utf-8"
    )
    (run_dir / "stdout.log").write_text("evidence\n", encoding="utf-8")
    (run_dir / "stderr.log").write_bytes(b"")
    profile = run_dir / "browser-profile"
    profile.mkdir()
    (profile / "Preferences").write_text("{}", encoding="utf-8")

    real_replace = Path.replace
    blocked_once = False

    def replace(path, target):
        nonlocal blocked_once
        target = Path(target)
        if path.name == "browser-profile" and target.parent.name == "attempt-001" and not blocked_once:
            blocked_once = True
            raise PermissionError(5, "Access is denied")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", replace)

    assert executor._archive_pre_submit_attempt(run_dir, {"failure_stage": None, "submission": "not_observed"}) == 1
    archived = run_dir / "pre-submit-attempts" / "attempt-001"
    assert blocked_once is True
    assert (archived / "browser-profile" / "Preferences").read_text(encoding="utf-8") == "{}"
    index = json.loads((run_dir / "pre-submit-attempts" / "index.json").read_text(encoding="utf-8"))
    assert index["attempts"][0]["status"] == "archived"


def test_pre_submit_archive_failure_restores_history_index(executor, tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state_bytes = json.dumps({"failure_stage": None, "submission": "not_observed"}).encode()
    (run_dir / "state.json").write_bytes(state_bytes)
    (run_dir / "stdout.log").write_text("evidence\n", encoding="utf-8")
    (run_dir / "stderr.log").write_bytes(b"")
    profile = run_dir / "browser-profile"
    profile.mkdir()
    (profile / "Preferences").write_text("{}", encoding="utf-8")

    real_replace = Path.replace

    def replace(path, target):
        target = Path(target)
        if path.name == "browser-profile" and target.parent.name == "attempt-001":
            raise PermissionError(5, "Access is denied")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", replace)

    with pytest.raises(PermissionError, match="Access is denied"):
        executor._archive_pre_submit_attempt(run_dir, {"failure_stage": None, "submission": "not_observed"})

    assert (run_dir / "state.json").read_bytes() == state_bytes
    assert (run_dir / "stdout.log").read_text(encoding="utf-8") == "evidence\n"
    assert (run_dir / "stderr.log").read_bytes() == b""
    assert (run_dir / "browser-profile" / "Preferences").read_text(encoding="utf-8") == "{}"
    history = run_dir / "pre-submit-attempts"
    index = json.loads((history / "index.json").read_text(encoding="utf-8"))
    assert index["attempts"] == []
    assert not (history / "attempt-001").exists()


def test_changed_mission_is_rejected_before_launch(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    mission.write_text("changed after preparation", encoding="utf-8")
    with pytest.raises(executor.ExecutionError) as exc:
        executor.execute_config(config, command_resolver=lambda: pytest.fail("must not launch"))
    assert exc.value.code == "MISSION_CHANGED"
    assert not (run_root / config.run_id).exists()


def test_retained_profile_copy_preserves_seed_and_normalizes_only_copy(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    seed = config.copy_profile / "Default"
    seed.mkdir()
    original = json.dumps({"profile": {"exit_type": "Crashed"}, "session": {"restore_on_startup": 1}, "custom": 42}).encode()
    (seed / "Preferences").write_bytes(original)
    (seed / "Cookies").write_bytes(b"fixture-cookie-data")
    (seed / "Cache").mkdir()
    (seed / "Cache" / "cached").write_bytes(b"discardable cache")
    run_dir = run_root / config.run_id
    run_dir.mkdir(parents=True)
    copied = executor._prepare_run_profile(config, run_dir)
    assert (seed / "Preferences").read_bytes() == original
    assert (copied / "Default" / "Cookies").read_bytes() == b"fixture-cookie-data"
    assert not (copied / "Default" / "Cache").exists()
    preferences = json.loads((copied / "Default" / "Preferences").read_text())
    assert preferences["profile"]["exit_type"] == "Normal"
    assert preferences["profile"]["exited_cleanly"] is True
    assert preferences["session"] == {"restore_on_startup": 5, "startup_urls": []}
    assert preferences["custom"] == 42


def test_profile_is_required_only_for_live_execution(executor, execution_paths, monkeypatch, tmp_path):
    root, mission, run_root, _ = execution_paths
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(tmp_path / "missing-profile"))
    config = executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    assert executor.execute_config(config, dry_run=True)["writes_performed"] is False
    assert not run_root.exists()
    with pytest.raises(executor.ExecutionError) as exc:
        executor.execute_config(config, command_resolver=lambda: pytest.fail("must not launch"))
    assert exc.value.code == "SIGNED_IN_PROFILE_UNAVAILABLE"


def test_profile_copy_failure_is_definitely_before_submission(executor, execution_paths, monkeypatch):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    def failed_copy(*args):
        raise OSError("profile copy failed")
    monkeypatch.setattr(executor, "_prepare_run_profile", failed_copy)
    result = executor.execute_config(config, command_resolver=lambda: ["oracle"],
        version_resolver=lambda command: "oracle 0.20.0", compat_factory=lambda version: {},
        popen_factory=lambda *args, **kwargs: pytest.fail("must not start Oracle"))
    assert result["result"]["submission"] == "not_observed"
    assert result["result"]["failure_stage"] == "profile-preparation"


def test_profile_copy_handles_long_windows_destination(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    source = config.copy_profile / "Default" / "nested-profile-assets"
    source.mkdir(parents=True)
    name = "asset-" + "x" * 100 + ".txt"
    (source / name).write_bytes(b"fixture")
    run_dir = run_root / ("long-run-" + "x" * 60)
    copied = executor._prepare_run_profile(config, run_dir)
    target = copied / "Default" / "nested-profile-assets" / name
    if executor.os.name == "nt":
        target = Path("\\\\?\\" + str(target.absolute()))
    try:
        assert target.read_bytes() == b"fixture"
    finally:
        # tempfile cleanup does not use the Windows extended-length prefix.
        target.unlink()


def test_ordinary_execution_does_not_require_legacy_devspace_config(executor, execution_paths, monkeypatch):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    assert not hasattr(executor, "DEVSPACE_PREFLIGHT")
    def reached_command_resolution():
        raise RuntimeError("resolved without legacy setup")
    with pytest.raises(RuntimeError, match="resolved without legacy setup"):
        executor.execute_config(config, command_resolver=reached_command_resolution)
    assert not (run_root / config.run_id).exists()


def test_filesystem_root_is_not_an_approved_project(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    with pytest.raises(executor.ExecutionError) as exc:
        executor.make_config(project_root=Path(root.anchor), mission_path=mission, run_root=run_root)
    assert exc.value.code == "PROJECT_ROOT_TOO_BROAD"


def test_selected_node_is_forwarded_to_compatibility(executor, execution_paths, tmp_path, monkeypatch):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(project_root=root, mission_path=mission, run_root=run_root)
    node = tmp_path / "bundled node" / "node.exe"
    node.parent.mkdir()
    node.write_bytes(b"fixture")
    command = [str(node), str(tmp_path / "oracle-cli.js")]
    observed = []
    def verify(version, **kwargs):
        observed.append((version, kwargs))
        raise RuntimeError("stop before browser or profile copy")
    with pytest.raises(RuntimeError, match="stop before browser"):
        executor.execute_config(config, command_resolver=lambda: command,
            version_resolver=lambda value: "oracle 0.20.0", compat_factory=verify)
    assert observed == [("oracle 0.20.0", {"node_executable": str(node)})]
    assert not (run_root / config.run_id).exists()


def picker_proof(effort: str = "pro") -> dict:
    ordinal = 5 if effort == "pro" else 4
    return {
        "schema": "codex.oracle.picker-dom-proof/v1",
        "latestClicked": True,
        "stableReads": 2,
        "modelRows": [
            {
                "text": "Latest",
                "ariaLabel": None,
                "role": "menuitemradio",
                "checked": "true",
                "expanded": None,
                "visible": True,
            }
        ],
        "composer": {
            "text": "6 Pro" if effort == "pro" else "Thinking effort",
            "ariaLabel": None,
            "role": None,
            "checked": None,
            "expanded": "true",
            "visible": True,
        },
        "modelSignals": [],
        "slider": {
            "minimum": 0,
            "maximum": 4,
            "current": ordinal - 1,
            "ordinal": ordinal,
            "total": 5,
            "displayOrdinal": ordinal,
            "displayTotal": 5,
            "atMaximum": ordinal == 5,
            "text": f"{ordinal} of 5",
            "visible": True,
        },
    }


def native_latest_evidence(
    effort: str = "pro", model_label: str = "Latest", effort_label: str | None = None
) -> str:
    label = effort_label or ("Pro" if effort == "pro" else "Extra High")
    return (
        f"[browser] Model selection evidence: requestedKey=latest; target=latest; resolvedLabel={model_label}; "
        "status=already-selected; strategy=select; verified=yes; source=chatgpt-model-picker; capturedAt=now\n"
        f"[browser] Thinking effort evidence: requestedLevel={effort}; status=already-selected; "
        f"resolvedLabel={label}; verified=yes; failClosed={'yes' if effort == 'pro' else 'no'}; "
        "targetModelKind=(none); observedModelKind=(none); source=chatgpt-thinking-picker; capturedAt=now\n"
    )


def native_sol_plus_high_evidence() -> str:
    return (
        "[browser] Model selection evidence: requestedKey=gpt-5.6-sol; target=GPT-5.6 Sol; "
        "resolvedLabel=GPT-5.6 Sol; status=already-selected; strategy=select; verified=yes; "
        "source=chatgpt-model-picker; capturedAt=now\n"
        "[browser] Thinking effort evidence: requestedLevel=extended; status=already-selected; "
        "resolvedLabel=High; maximum=2; verified=yes; source=chatgpt-thinking-picker; capturedAt=now\n"
    )


def native_sol_plus_high_menu_evidence() -> str:
    return (
        "[browser] Model selection evidence: requestedKey=gpt-5.6-sol; target=GPT-5.6 Sol; "
        "resolvedLabel=GPT-5.6 Sol; status=already-selected; strategy=select; verified=yes; "
        "source=chatgpt-model-picker; capturedAt=now\n"
        "[browser] Thinking effort evidence: requestedLevel=extended; status=already-selected; "
        "resolvedLabel=High; maximum=(unknown); verified=yes; "
        "source=chatgpt-thinking-picker-plus-high-menu; capturedAt=now\n"
    )


def write_session_meta(
    session_root: Path,
    slug: str,
    *,
    status: str,
    submitted: bool = True,
    port: int = 43123,
    target_id: str = "B" * 32,
    url: str = "https://chatgpt.com/c/owned-temporary-run",
    harvested: bool = False,
) -> dict:
    directory = session_root / slug
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "browser": {
            "runtime": {
                "chromeHost": "127.0.0.1",
                "chromePort": port,
                "chromeTargetId": target_id,
                "tabUrl": url,
                "promptSubmitted": submitted,
            },
            **(
                {
                    "harvest": {
                        "targetId": target_id,
                        "url": url,
                        "state": "completed",
                        "assistantCount": 1,
                        "stopExists": False,
                        "integrity": {
                            "status": "matched",
                            "observedConversationId": "owned-temporary-run",
                        },
                    }
                }
                if harvested
                else {}
            ),
        },
    }
    (directory / "meta.json").write_text(json.dumps(payload), encoding="utf-8")
    return {"target_id": target_id, "conversation_url": url}


def create_uncertain_parent_run(executor, root: Path, run_root: Path, owner: str):
    old_mission = root / "old-mission.md"
    old_mission.write_text("Original authorized read.\n", encoding="utf-8")
    parent_config = executor.make_config(
        project_root=root,
        mission_path=old_mission,
        run_root=run_root,
        run_id="ordinary-run-uncertain-01",
        model="gpt-5.6-sol",
        effort="extended",
        app_name="codex",
    )
    parent_dir = run_root / parent_config.run_id
    parent_dir.mkdir(parents=True)
    parent_output = parent_dir / "output.md"
    parent_output.write_bytes(b"")
    parent_stdout = parent_dir / "stdout.log"
    parent_stdout.write_text(
        'Prompt commit check failed: {"userMatched":false,"hasNewTurn":false,"turnsCount":0}\n'
        "ERROR: Prompt did not appear in conversation before timeout (send may have failed)\n",
        encoding="utf-8",
    )
    (parent_dir / "reconnect-stdout.log").write_text("Prompt-free recovery completed.\n", encoding="utf-8")
    (parent_dir / "reconnect-stderr.log").write_text(
        "Recovered ChatGPT conversation did not become ready in time.\n", encoding="utf-8"
    )
    parent_state = executor._initial_state(
        parent_config, parent_dir, "old-session", parent_output, ["oracle"], cdp_port=43123
    )
    parent_state.update(status="attention_required", submission="observed")
    parent_state["oracle"]["binding"] = {
        "prompt_submitted": True,
        "session_status": "error",
        "conversation_url": "https://chatgpt.com/c/old?temporary-chat=true",
    }
    parent_state["artifacts"].update(output_bytes=0)
    parent_state_path = parent_dir / "state.json"
    parent_state_bytes = (json.dumps(parent_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    parent_state_path.write_bytes(parent_state_bytes)
    return parent_config, parent_dir, parent_state_path, parent_state_bytes


class Process:
    pid = 4242

    def __init__(self, code: int = 0):
        self.code = code

    def wait(self):
        return self.code


def test_manifest_is_minimal_and_rejects_retired_fields(executor, execution_paths, tmp_path: Path):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="ordinary-run-0001",
    )
    manifest = executor.manifest_payload(config)
    assert set(manifest) == {
        "schema", "project_root", "mission_path", "run_root", "run_id", "model", "effort", "app_name"
    }

    manifest["mode"] = "review"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(executor.ExecutionError) as exc:
        executor.load_manifest(path)
    assert exc.value.code == "MANIFEST_FIELDS_INVALID"


def test_dry_run_has_temp_personalized_route_and_no_writes(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="ordinary-run-0002",
        app_name="git-app",
    )
    result = executor.execute_config(config, dry_run=True)
    argv = result["argv"]

    assert result["writes_performed"] is False
    assert not run_root.exists()
    assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"
    assert argv[argv.index("--browser-model-strategy") + 1] == "select"
    assert argv[argv.index("--browser-thinking-time") + 1] == "extended"
    assert argv[argv.index("--chatgpt-url") + 1] == "https://chatgpt.com/?temporary-chat=true"
    assert argv[argv.index("--browser-archive") + 1] == "never"
    assert "--remote-chrome" in argv
    assert argv[argv.index("--remote-chrome") + 1].startswith("127.0.0.1:")
    assert "--browser-keep-browser" not in argv
    assert "--browser-hide-window" not in argv
    assert "--copy-profile" not in argv
    assert "--browser-manual-login-profile-dir" not in argv
    assert "--browser-tab" not in argv
    assert argv[argv.index("--prompt") + 1] == "<mission-handoff>"
    assert "TASK_OUTCOME" not in " ".join(argv)


@pytest.mark.parametrize("effort", ["pro", "extra-high"])
def test_one_latest_model_check_covers_model_and_effort(executor, tmp_path: Path, effort: str):
    stdout = tmp_path / "stdout.log"
    stdout.write_text(
        f"[browser] Picker DOM proof: {json.dumps(picker_proof(effort))}\n",
        encoding="utf-8",
    )
    check = executor.observed_model_check(stdout, model="latest", effort=effort)
    assert check == {
        "verified": True,
        "model": "latest",
        "actual_model": "6 Pro" if effort == "pro" else None,
        "effort": effort,
        "source": "oracle-picker-dom-log",
    }


@pytest.mark.parametrize(
    "effort,model_label,effort_label",
    [("pro", "Latest", "Pro"), ("extra-high", "최신", "매우 높음")],
)
def test_latest_model_check_prefers_native_020_evidence(
    executor, tmp_path: Path, effort: str, model_label: str, effort_label: str
):
    stdout = tmp_path / "stdout.log"
    stdout.write_text(native_latest_evidence(effort, model_label, effort_label), encoding="utf-8")
    result = executor.observed_model_check(stdout, model="latest", effort=effort)
    assert result["verified"] is True
    assert result["source"] == "oracle-native-selection-log"


def test_latest_native_evidence_fails_closed_when_thinking_is_unverified(executor, tmp_path: Path):
    stdout = tmp_path / "stdout.log"
    stdout.write_text(
        native_latest_evidence("extra-high").replace(
            "status=already-selected; resolvedLabel=Extra High; verified=yes",
            "status=unverified; resolvedLabel=(none); verified=no",
        ),
        encoding="utf-8",
    )
    assert executor.observed_model_check(stdout, model="latest", effort="extra-high")["verified"] is False


@pytest.mark.parametrize("key,verified", [("gpt-6-astra", True), ("gpt-5.6-sol", False)])
def test_native_latest_alias_uses_oracle_normalized_request_key(executor, tmp_path: Path, key: str, verified: bool):
    stdout = tmp_path / "stdout.log"
    stdout.write_text(native_latest_evidence("extra-high").replace("requestedKey=latest;", f"requestedKey={key};"), encoding="utf-8")
    assert executor.observed_model_check(stdout, model="latest", effort="extra-high")["verified"] is verified


@pytest.mark.parametrize("label, verified", [("Thinking effort", True), ("추론 수준", True), ("5.6 Pro", False)])
def test_latest_pro_accepts_actual_menu_signal(executor, tmp_path, label, verified):
    proof = picker_proof()
    proof["composer"]["text"] = label
    proof["modelSignals"] = [{"text": "6Pro", "visible": True}]
    stdout = tmp_path / "stdout.log"
    stdout.write_text(executor.PICKER_PROOF_PREFIX + json.dumps(proof), encoding="utf-8")
    result = executor.observed_model_check(stdout, model="latest", effort="pro")
    assert result["verified"] is verified


def test_explicit_model_uses_observed_select_logs(executor, tmp_path: Path):
    stdout = tmp_path / "stdout.log"
    stdout.write_text(
        "[browser] Model selection evidence: requestedKey=gpt-5.6-sol; target=GPT-5.6 Sol; "
        "resolvedLabel=GPT-5.6 Sol; status=switched; strategy=select; verified=yes; source=chatgpt-model-picker\n"
        "[browser] Thinking time: Pro, 5 of 5\n",
        encoding="utf-8",
    )
    assert executor.observed_model_check(stdout, model="gpt-5.6-sol", effort="pro")["verified"] is True


@pytest.mark.parametrize("effort", ["pro", "extra-high"])
def test_explicit_model_accepts_native_already_selected_effort(executor, tmp_path: Path, effort: str):
    stdout = tmp_path / "stdout.log"
    evidence = native_latest_evidence(effort, "GPT-5.6 Sol").replace(
        "requestedKey=latest; target=latest;", "requestedKey=gpt-5.6-sol; target=GPT-5.6 Sol;"
    )
    stdout.write_text(evidence, encoding="utf-8")
    result = executor.observed_model_check(stdout, model="gpt-5.6-sol", effort=effort)
    assert result["verified"] is True
    assert result["actual_model"] is None
    assert result["source"] == "oracle-native-selection-log"
    assert executor.observed_model_check(stdout, model="latest", effort=effort)["verified"] is False


def test_explicit_sol_high_accepts_exact_current_plus_high_menu_proof(executor, tmp_path: Path):
    stdout = tmp_path / "stdout.log"
    stdout.write_text(native_sol_plus_high_menu_evidence(), encoding="utf-8")
    result = executor.observed_model_check(stdout, model="gpt-5.6-sol", effort="extended")
    assert result["verified"] is True
    assert result["source"] == "oracle-native-selection-log"


def test_explicit_sol_high_accepts_legacy_020_meta_plus_exact_high_log(executor, tmp_path: Path):
    stdout = tmp_path / "stdout.log"
    stdout.write_text(
        "Model picker: GPT-5.6 Sol\n"
        "[browser] Thinking time: High (already selected)\n",
        encoding="utf-8",
    )
    session_meta = {
        "browser": {
            "modelSelection": {
                "requestedModel": "GPT-5.6 Sol",
                "resolvedLabel": "GPT-5.6 Sol",
                "strategy": "select",
                "status": "already-selected",
                "verified": True,
                "source": "chatgpt-model-picker",
            }
        },
        "options": {
            "model": "gpt-5.6-sol",
            "effectiveModelId": "gpt-5.6-sol",
            "browserConfig": {
                "desiredModel": "GPT-5.6 Sol",
                "modelStrategy": "select",
                "thinkingTime": "extended",
            },
        },
    }
    result = executor.observed_model_check(
        stdout,
        model="gpt-5.6-sol",
        effort="extended",
        session_meta=session_meta,
    )
    assert result["verified"] is True
    assert result["source"] == "oracle-020-session-meta-plus-selection-log"


def test_explicit_sol_high_does_not_accept_unknown_maximum_without_menu_proof(executor, tmp_path: Path):
    stdout = tmp_path / "stdout.log"
    stdout.write_text(
        native_sol_plus_high_menu_evidence().replace(
            "source=chatgpt-thinking-picker-plus-high-menu",
            "source=chatgpt-thinking-picker",
        ),
        encoding="utf-8",
    )
    assert executor.observed_model_check(
        stdout, model="gpt-5.6-sol", effort="extended"
    )["verified"] is False


def test_close_owned_tab_targets_only_exact_recorded_target(executor):
    urls: list[str] = []
    target_id = "B" * 32

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps(self.payload).encode()

    calls = 0

    def opener(request, timeout):
        nonlocal calls
        calls += 1
        urls.append(request.full_url)
        if calls == 1:
            return Response([
                {"id": target_id, "url": "https://chatgpt.com/c/owned", "type": "page"},
                {"id": "C" * 32, "url": "https://chatgpt.com/c/foreign", "type": "page"},
            ])
        if calls == 2:
            return Response({})
        return Response([{"id": "C" * 32, "url": "https://chatgpt.com/c/foreign", "type": "page"}])

    result = executor.close_owned_tab(
        {
            "host": "127.0.0.1",
            "port": 43123,
            "target_id": target_id,
            "conversation_url": "https://chatgpt.com/c/owned",
        },
        opener=opener,
    )

    assert result == {"status": "closed", "target_id": target_id}
    assert urls[1].endswith(f"/json/close/{target_id}")
    assert all(("C" * 32) not in url for url in urls)


def test_capture_state_is_durable_before_owned_tab_close(executor, execution_paths):
    root, mission, run_root, session_root = execution_paths
    config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="ordinary-run-0003",
    )
    state_path = run_root / config.run_id / "state.json"

    def popen(argv, **kwargs):
        assert argv[argv.index("--browser-tab") + 1] == "B" * 32
        output = Path(argv[argv.index("--write-output") + 1])
        output.write_text("Complete captured answer.\n", encoding="utf-8")
        kwargs["stdout"].write(native_sol_plus_high_evidence().encode())
        kwargs["stdout"].flush()
        write_session_meta(
            session_root,
            argv[argv.index("--slug") + 1],
            status="completed",
            port=browser_port(argv),
        )
        return Process(0)

    cleanup_calls = []

    def cleanup(preflight, command, *, expected_url):
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted["status"] == "captured"
        assert persisted["capture"] == "durable"
        assert persisted["artifacts"]["output_sha256"]
        cleanup_calls.append((preflight["target_id"], command, expected_url))
        return {"status": "closed", "target_id": preflight["target_id"]}

    result = executor.execute_config(
        config,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda command: "oracle 0.20.0",
        compat_factory=lambda version: {"ok": True},
        popen_factory=popen,
        tab_closer=lambda binding: pytest.fail("preflight run must not close a tab separately"),
        browser_preflight=fake_preflight,
        browser_cleanup=cleanup,
    )

    assert result["ok"] is True
    assert result["status"] == "captured"
    assert result["result"]["semantic_outcome"] == "unknown"
    assert result["result"]["tab_close"]["status"] == "not_attempted"
    assert result["result"]["browser_cleanup"]["status"] == "closed"
    assert cleanup_calls == [("B" * 32, ["oracle"], "https://chatgpt.com/c/owned-temporary-run")]


def test_preflight_target_mismatch_blocks_capture_and_preserves_browser(executor, execution_paths):
    root, mission, run_root, session_root = execution_paths
    config = executor.make_config(
        project_root=root, mission_path=mission, run_root=run_root, run_id="ordinary-run-mismatch"
    )

    def popen(argv, **kwargs):
        Path(argv[argv.index("--write-output") + 1]).write_text("Answer from wrong tab.\n", encoding="utf-8")
        kwargs["stdout"].write(native_sol_plus_high_evidence().encode())
        kwargs["stdout"].flush()
        write_session_meta(
            session_root,
            argv[argv.index("--slug") + 1],
            status="completed",
            port=browser_port(argv),
            target_id="A" * 32,
        )
        return Process(0)

    result = executor.execute_config(
        config,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda command: "oracle 0.20.0",
        compat_factory=lambda version: {"ok": True},
        popen_factory=popen,
        tab_closer=lambda binding: pytest.fail("mismatched target must be preserved"),
        browser_preflight=fake_preflight,
        browser_cleanup=lambda *_args: pytest.fail("uncertain target must preserve browser"),
    )

    assert result["status"] == "attention_required"
    assert result["result"]["capture"] == "durable"
    assert result["result"]["submission"] == "unknown"
    assert result["result"]["oracle"]["binding"] is None


def test_popen_failure_cleans_owned_preflight_before_submission(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    config = executor.make_config(
        project_root=root, mission_path=mission, run_root=run_root, run_id="ordinary-run-popen-fail"
    )
    cleanup_calls = []

    def cleanup(preflight, command):
        cleanup_calls.append((preflight["target_id"], command))
        return {"status": "closed", "target_id": preflight["target_id"]}

    result = executor.execute_config(
        config,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda command: "oracle 0.20.0",
        compat_factory=lambda version: {"ok": True},
        popen_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("popen failed")),
        tab_closer=lambda binding: pytest.fail("Popen failure must use browser cleanup only"),
        browser_preflight=fake_preflight,
        browser_cleanup=cleanup,
    )

    assert result["result"]["submission"] == "not_observed"
    assert result["result"]["browser_cleanup"]["status"] == "closed"
    assert cleanup_calls == [("B" * 32, ["oracle"])]


def test_historical_run_without_preflight_keeps_exact_tab_close(executor, execution_paths):
    root, mission, run_root, session_root = execution_paths
    config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="historical-run-0001",
        model="latest",
        effort="pro",
    )
    run_dir = run_root / config.run_id
    run_dir.mkdir(parents=True)
    output = run_dir / "output.md"
    stdout = run_dir / "stdout.log"
    output.write_text("Historical captured answer.\n", encoding="utf-8")
    stdout.write_text(f"{executor.PICKER_PROOF_PREFIX} {json.dumps(picker_proof())}\n", encoding="utf-8")
    state = executor._initial_state(config, run_dir, executor._slug(config), output, ["oracle"], cdp_port=43123)
    state_path = run_dir / "state.json"
    write_session_meta(
        session_root, state["oracle"]["slug"], status="completed", port=43123, target_id="A" * 32
    )
    closed = []

    result = executor._finalize_capture(
        state_path,
        state,
        stdout_path=stdout,
        output_path=output,
        tab_closer=lambda binding: closed.append(binding["target_id"]) or {"status": "closed"},
    )

    assert result["status"] == "captured"
    assert result["tab_close"]["status"] == "closed"
    assert closed == ["A" * 32]


def test_error_session_with_exact_completed_harvest_is_terminal(executor, execution_paths):
    root, mission, run_root, session_root = execution_paths
    config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="ordinary-run-harvested-error",
    )
    run_dir = run_root / config.run_id
    run_dir.mkdir(parents=True)
    output = run_dir / "output.md"
    stdout = run_dir / "stdout.log"
    output.write_text("Recovered complete answer.\n", encoding="utf-8")
    stdout.write_text(native_sol_plus_high_evidence(), encoding="utf-8")
    state = executor._initial_state(config, run_dir, executor._slug(config), output, ["oracle"], cdp_port=43123)
    state_path = run_dir / "state.json"
    write_session_meta(
        session_root,
        state["oracle"]["slug"],
        status="error",
        port=43123,
        target_id="A" * 32,
        harvested=True,
    )

    result = executor._finalize_capture(
        state_path,
        state,
        stdout_path=stdout,
        output_path=output,
        tab_closer=lambda binding: {"status": "closed", "target_id": binding["target_id"]},
    )

    assert result["status"] == "captured"
    assert result["capture"] == "durable"
    assert result["submission"] == "observed"


def test_running_session_with_exact_completed_harvest_is_terminal(executor, execution_paths):
    root, mission, run_root, session_root = execution_paths
    config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="ordinary-run-harvested-running",
    )
    run_dir = run_root / config.run_id
    run_dir.mkdir(parents=True)
    output = run_dir / "output.md"
    stdout = run_dir / "stdout.log"
    output.write_text("Recovered complete answer.\n", encoding="utf-8")
    stdout.write_text(native_sol_plus_high_evidence(), encoding="utf-8")
    state = executor._initial_state(config, run_dir, executor._slug(config), output, ["oracle"], cdp_port=43123)
    state_path = run_dir / "state.json"
    write_session_meta(
        session_root,
        state["oracle"]["slug"],
        status="running",
        port=43123,
        target_id="A" * 32,
        harvested=True,
    )

    result = executor._finalize_capture(
        state_path,
        state,
        stdout_path=stdout,
        output_path=output,
        tab_closer=lambda binding: {"status": "closed", "target_id": binding["target_id"]},
    )

    assert result["status"] == "captured"
    assert result["capture"] == "durable"
    assert result["submission"] == "observed"


def test_timeout_keeps_same_tab_and_never_resubmits(executor, execution_paths):
    root, mission, run_root, session_root = execution_paths
    config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="ordinary-run-0004",
    )
    launches: list[list[str]] = []

    def popen(argv, **kwargs):
        launches.append(list(argv))
        write_session_meta(
            session_root,
            argv[argv.index("--slug") + 1],
            status="running",
            port=browser_port(argv),
        )
        return Process(1)

    result = executor.execute_config(
        config,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda command: "oracle 0.20.0",
        compat_factory=lambda version: {"ok": True},
        popen_factory=popen,
        tab_closer=lambda binding: pytest.fail("incomplete run must not close its tab"),
        browser_preflight=fake_preflight,
        browser_cleanup=fake_cleanup,
    )

    assert result["ok"] is False
    assert result["status"] == "attention_required"
    assert result["result"]["submission"] == "observed"
    assert result["result"]["tab_close"]["status"] == "not_attempted"
    assert len(launches) == 1
    assert launches[0].count("--prompt") == 1


def test_reconnect_is_prompt_free_and_uses_original_slug(executor, execution_paths):
    root, mission, run_root, session_root = execution_paths
    config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="ordinary-run-0005",
    )

    def initial_popen(argv, **kwargs):
        kwargs["stdout"].write(native_sol_plus_high_evidence().encode())
        kwargs["stdout"].flush()
        write_session_meta(
            session_root,
            argv[argv.index("--slug") + 1],
            status="running",
            port=browser_port(argv),
        )
        return Process(1)

    first = executor.execute_config(
        config,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda command: "oracle 0.20.0",
        compat_factory=lambda version: {"ok": True},
        popen_factory=initial_popen,
        tab_closer=lambda binding: pytest.fail("initial incomplete run must retain tab"),
        browser_preflight=fake_preflight,
        browser_cleanup=fake_cleanup,
    )
    run_dir = Path(first["run_dir"])
    original = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    slug = original["oracle"]["slug"]
    before_preview = {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()}
    preview = executor.reconnect_run(run_dir, dry_run=True)
    assert preview["resubmit"] is False
    assert before_preview == {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()}
    recovery_argv: list[str] = []

    def reconnect_popen(argv, **kwargs):
        recovery_argv.extend(argv)
        Path(original["artifacts"]["output"]).write_text("Recovered complete answer.\n", encoding="utf-8")
        write_session_meta(
            session_root,
            slug,
            status="completed",
            port=int(original["oracle"]["expected_cdp_port"]),
        )
        return Process(0)

    recovered = executor.reconnect_run(
        run_dir,
        popen_factory=reconnect_popen,
        tab_closer=lambda binding: {"status": "closed", "target_id": binding["target_id"]},
        browser_cleanup=fake_cleanup,
    )

    assert recovered["ok"] is True
    assert recovery_argv[:3] == ["oracle", "session", slug]
    assert "--live" in recovery_argv
    assert "--prompt" not in recovery_argv
    assert "-p" not in recovery_argv


def test_reconnect_uses_stdout_confirmed_collision_slug(executor, execution_paths):
    root, mission, run_root, session_root = execution_paths
    config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="ordinary-run-collision-slug",
    )
    actual_slug = ""
    conversation_url = "https://chatgpt.com/c/collision-run?temporary-chat=true"

    def initial_popen(argv, **kwargs):
        nonlocal actual_slug
        requested_slug = argv[argv.index("--slug") + 1]
        actual_slug = f"{requested_slug}-2"
        kwargs["stdout"].write(
            (
                native_sol_plus_high_evidence()
                + f"Session: {actual_slug}\n"
                + f"Reattach: oracle session {actual_slug}\n"
                + f"[browser] conversation url (post-submit) = {conversation_url}\n"
            ).encode()
        )
        kwargs["stdout"].flush()
        write_session_meta(
            session_root,
            requested_slug,
            status="error",
            port=browser_port(argv) + 1,
            target_id="C" * 32,
            url="https://chatgpt.com/c/stale-session",
        )
        write_session_meta(
            session_root,
            actual_slug,
            status="error",
            port=browser_port(argv),
            url=conversation_url,
        )
        return Process(1)

    first = executor.execute_config(
        config,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda command: "oracle 0.20.0",
        compat_factory=lambda version: {"ok": True},
        popen_factory=initial_popen,
        tab_closer=lambda binding: pytest.fail("initial incomplete run must retain tab"),
        browser_preflight=fake_preflight,
        browser_cleanup=fake_cleanup,
    )
    run_dir = Path(first["run_dir"])
    original = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    requested_slug = original["oracle"]["slug"]
    assert actual_slug == f"{requested_slug}-2"
    assert original["oracle"]["effective_slug"] == actual_slug

    preview = executor.reconnect_run(run_dir, dry_run=True)
    assert preview["argv"][:3] == ["oracle", "session", actual_slug]
    assert requested_slug not in preview["argv"][:3]
    assert "--prompt" not in preview["argv"]
    assert "-p" not in preview["argv"]


def test_reconnect_rejects_ambiguous_stdout_session_identity(executor, execution_paths):
    root, mission, run_root, session_root = execution_paths
    config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="ordinary-run-ambiguous-slug",
    )

    def initial_popen(argv, **kwargs):
        kwargs["stdout"].write(native_sol_plus_high_evidence().encode())
        kwargs["stdout"].flush()
        write_session_meta(
            session_root,
            argv[argv.index("--slug") + 1],
            status="error",
            port=browser_port(argv),
        )
        return Process(1)

    first = executor.execute_config(
        config,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda command: "oracle 0.20.0",
        compat_factory=lambda version: {"ok": True},
        popen_factory=initial_popen,
        tab_closer=lambda binding: pytest.fail("initial incomplete run must retain tab"),
        browser_preflight=fake_preflight,
        browser_cleanup=fake_cleanup,
    )
    run_dir = Path(first["run_dir"])
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    requested_slug = state["oracle"]["slug"]
    (run_dir / "stdout.log").write_text(
        native_sol_plus_high_evidence()
        + f"Session: {requested_slug}-2\n"
        + f"Session: {requested_slug}-3\n"
        + f"Reattach: oracle session {requested_slug}-2\n",
        encoding="utf-8",
    )

    with pytest.raises(executor.ExecutionError) as exc:
        executor.reconnect_run(run_dir, dry_run=True)
    assert exc.value.code == "RUN_RECOVERY_SESSION_AMBIGUOUS"


def test_unresolved_observed_run_blocks_duplicate_submission(executor, execution_paths):
    root, mission, run_root, session_root = execution_paths

    def popen(argv, **kwargs):
        write_session_meta(
            session_root,
            argv[argv.index("--slug") + 1],
            status="running",
            port=browser_port(argv),
        )
        return Process(1)

    first_config = executor.make_config(
        project_root=root, mission_path=mission, run_root=run_root, run_id="ordinary-run-0006"
    )
    executor.execute_config(
        first_config,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda command: "oracle 0.20.0",
        compat_factory=lambda version: {"ok": True},
        popen_factory=popen,
        browser_preflight=fake_preflight,
        browser_cleanup=fake_cleanup,
    )
    mission.write_text("A changed mission must not bypass an uncertain submission.\n", encoding="utf-8")
    second_config = executor.make_config(
        project_root=root, mission_path=mission, run_root=run_root, run_id="ordinary-run-0007"
    )
    with pytest.raises(executor.ExecutionError) as exc:
        executor.execute_config(
            second_config,
            command_resolver=lambda: pytest.fail("duplicate must fail before resolving Oracle"),
        )
    assert exc.value.code == "RUN_RECONNECT_REQUIRED"


def test_hash_bound_user_authorized_successor_replaces_parent_as_unresolved_owner(
    executor, execution_paths, monkeypatch
):
    root, mission, run_root, _ = execution_paths
    owner = "01234567-89ab-cdef-0123-456789abcdef"
    monkeypatch.setenv("CODEX_THREAD_ID", owner)
    old_mission = root / "old-mission.md"
    old_mission.write_text("Original authorized read.\n", encoding="utf-8")
    parent_config = executor.make_config(
        project_root=root,
        mission_path=old_mission,
        run_root=run_root,
        run_id="ordinary-run-uncertain-01",
        model="gpt-5.6-sol",
        effort="extended",
    )
    parent_dir = run_root / parent_config.run_id
    parent_dir.mkdir(parents=True)
    parent_output = parent_dir / "output.md"
    parent_output.write_bytes(b"")
    parent_stdout = parent_dir / "stdout.log"
    parent_stdout.write_text(
        'Prompt commit check failed: {"userMatched":false,"hasNewTurn":false,"turnsCount":0}\n'
        "ERROR: Prompt did not appear in conversation before timeout (send may have failed)\n",
        encoding="utf-8",
    )
    parent_reconnect_stdout = parent_dir / "reconnect-stdout.log"
    parent_reconnect_stdout.write_text("Prompt-free recovery completed.\n", encoding="utf-8")
    parent_reconnect_stderr = parent_dir / "reconnect-stderr.log"
    parent_reconnect_stderr.write_text(
        "Recovered ChatGPT conversation did not become ready in time.\n", encoding="utf-8"
    )
    parent_state = executor._initial_state(
        parent_config,
        parent_dir,
        "old-session",
        parent_output,
        ["oracle"],
        cdp_port=43123,
    )
    parent_state.update(status="attention_required", submission="observed")
    parent_state["oracle"]["binding"] = {
        "prompt_submitted": True,
        "session_status": "error",
        "conversation_url": "https://chatgpt.com/c/old?temporary-chat=true",
    }
    parent_state["artifacts"].update(output_bytes=0)
    parent_state_path = parent_dir / "state.json"
    parent_state_bytes = (json.dumps(parent_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    parent_state_path.write_bytes(parent_state_bytes)

    successor_id = "zz-successor-retry-01"
    ticket = {
        "schema": "codex.chatgpt.oracle-authorized-retry/v1",
        "status": "authorized",
        "parent_run_id": parent_config.run_id,
        "parent_state_sha256": hashlib.sha256(parent_state_bytes).hexdigest(),
        "parent_stdout_sha256": hashlib.sha256(parent_stdout.read_bytes()).hexdigest(),
        "parent_output_sha256": hashlib.sha256(parent_output.read_bytes()).hexdigest(),
        "parent_reconnect_stdout_sha256": hashlib.sha256(parent_reconnect_stdout.read_bytes()).hexdigest(),
        "parent_reconnect_stderr_sha256": hashlib.sha256(parent_reconnect_stderr.read_bytes()).hexdigest(),
        "source_thread_id": owner,
        "project_root": str(root.resolve()),
        "successor_run_id": successor_id,
        "successor_mission_path": str(mission.resolve()),
        "successor_mission_sha256": hashlib.sha256(mission.read_bytes()).hexdigest(),
        "selection": {"model": "gpt-5.6-sol", "effort": "extended", "app_name": "codex"},
        "authorization": "explicit-user-authorized-retry-despite-uncertain-delivery",
        "possible_duplicate_delivery": True,
    }
    ticket_path = parent_dir / "authorized-retry.json"
    ticket_bytes = (json.dumps(ticket, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    ticket_path.write_bytes(ticket_bytes)
    ticket_sha256 = hashlib.sha256(ticket_bytes).hexdigest()

    successor_config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id=successor_id,
        model="gpt-5.6-sol",
        effort="extended",
    )
    successor_dir = run_root / successor_id
    successor_dir.mkdir()
    successor_state = executor._initial_state(
        successor_config,
        successor_dir,
        "successor-session",
        successor_dir / "output.md",
        ["oracle"],
        cdp_port=43124,
    )
    successor_state.update(status="attention_required", submission="observed")
    successor_state["authorized_retry"] = {
        "parent_run_id": parent_config.run_id,
        "authorization_sha256": ticket_sha256,
        "possible_duplicate_delivery": True,
    }
    (successor_dir / "state.json").write_text(
        json.dumps(successor_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    later_config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="ordinary-run-next-0001",
        model="gpt-5.6-sol",
        effort="extended",
    )
    duplicate = executor._unresolved_duplicate(later_config)

    assert parent_state_path.read_bytes() == parent_state_bytes
    assert duplicate == {
        "run_dir": str(successor_dir),
        "status": "attention_required",
        "submission": "observed",
    }

    # A matching ticket digest alone must not let a malformed retry ticket
    # supersede the parent with a Pro or otherwise unauthorized selection.
    ticket["selection"]["model"] = "gpt-5.6-pro"
    ticket_bytes = (json.dumps(ticket, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    ticket_path.write_bytes(ticket_bytes)
    successor_state["selection"]["model"] = "gpt-5.6-pro"
    successor_state["authorized_retry"]["authorization_sha256"] = hashlib.sha256(ticket_bytes).hexdigest()
    (successor_dir / "state.json").write_text(
        json.dumps(successor_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert executor._authorized_retry_successor(parent_state_path, parent_state) is None
    assert executor._unresolved_duplicate(later_config) == {
        "run_dir": str(parent_dir),
        "status": "attention_required",
        "submission": "observed",
    }


def test_execute_records_user_authorized_retry_and_submits_one_bound_successor(
    executor, execution_paths, monkeypatch
):
    root, mission, run_root, session_root = execution_paths
    owner = "01234567-89ab-cdef-0123-456789abcdef"
    monkeypatch.setenv("CODEX_THREAD_ID", owner)
    monkeypatch.setattr(executor, "_pid_alive", lambda _pid: False)
    _parent_config, parent_dir, parent_state_path, parent_state_bytes = create_uncertain_parent_run(
        executor, root, run_root, owner
    )
    successor_config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="authorized-retry-run-0001",
        model="gpt-5.6-sol",
        effort="extended",
        app_name="codex",
    )
    launches: list[list[str]] = []

    def popen(argv, **_kwargs):
        launches.append(list(argv))
        write_session_meta(
            session_root,
            argv[argv.index("--slug") + 1],
            status="running",
            port=browser_port(argv),
        )
        return Process(1)

    result = executor.execute_config(
        successor_config,
        retry_from_run=parent_dir,
        confirm_uncertain_retry=True,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda _command: "oracle 0.20.0",
        compat_factory=lambda _version: {"ok": True},
        popen_factory=popen,
        browser_preflight=fake_preflight,
        browser_cleanup=fake_cleanup,
    )

    assert len(launches) == 1
    assert launches[0].count("--prompt") == 1
    assert parent_state_path.read_bytes() == parent_state_bytes
    ticket_path = parent_dir / "authorized-retry.json"
    ticket = json.loads(ticket_path.read_text(encoding="utf-8"))
    assert ticket["authorization"] == "explicit-user-authorized-retry-despite-uncertain-delivery"
    assert ticket["possible_duplicate_delivery"] is True
    assert ticket["successor_run_id"] == successor_config.run_id
    successor_state = json.loads((Path(result["run_dir"]) / "state.json").read_text(encoding="utf-8"))
    assert successor_state["authorized_retry"]["parent_run_id"] == parent_dir.name
    assert successor_state["authorized_retry"]["possible_duplicate_delivery"] is True
    assert result["result"]["submission"] == "observed"
    with pytest.raises(executor.ExecutionError) as exc:
        executor.reconnect_run(parent_dir, dry_run=True)
    assert exc.value.code == "RUN_SUPERSEDED_BY_AUTHORIZED_RETRY"


def test_authorized_retry_accepts_terminal_assistant_capture_and_hydration_failure(
    executor, execution_paths, monkeypatch
):
    root, mission, run_root, _session_root = execution_paths
    owner = "01234567-89ab-cdef-0123-456789abcdef"
    monkeypatch.setenv("CODEX_THREAD_ID", owner)
    monkeypatch.setattr(executor, "_pid_alive", lambda _pid: False)
    _parent_config, parent_dir, _parent_state_path, _parent_state_bytes = create_uncertain_parent_run(
        executor, root, run_root, owner
    )
    (parent_dir / "stdout.log").write_text(
        "Activated send button\n"
        "[browser] conversation url (post-submit) = https://chatgpt.com/c/example?temporary-chat=true\n"
        "[browser] ChatGPT thinking - 30s elapsed; status=response streaming; source=inline\n"
        "Browser automation failure (assistant-response-unconfirmed); capturing DOM snapshot for debugging...\n"
        "Conversation snapshot: [{\"role\":null,\"text\":\"You said: @codex read the file\"}]\n"
        "Saved ChatGPT conversation did not load stable prior turns; refusing to submit follow-up as a fresh chat.\n",
        encoding="utf-8",
    )
    (parent_dir / "reconnect-stdout.log").write_text(
        'No live ChatGPT tab matched session "old-session". Attempting recovery by reopening the saved conversation URL.\n',
        encoding="utf-8",
    )
    (parent_dir / "reconnect-stderr.log").write_text(
        "Recovered ChatGPT conversation did not become ready in time.\n",
        encoding="utf-8",
    )
    successor_config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="authorized-retry-run-assistant-capture-0001",
        model="gpt-5.6-sol",
        effort="extended",
        app_name="codex",
    )

    ticket = executor._authorize_uncertain_retry(
        successor_config,
        parent_dir,
        confirmed=True,
    )

    assert ticket["authorization"] == "explicit-user-authorized-retry-despite-uncertain-delivery"
    assert ticket["possible_duplicate_delivery"] is True
    assert ticket["successor_run_id"] == successor_config.run_id


def test_authorized_retry_accepts_lost_observer_after_streaming_and_failed_reconnect(
    executor, execution_paths, monkeypatch
):
    root, mission, run_root, _session_root = execution_paths
    owner = "01234567-89ab-cdef-0123-456789abcdef"
    monkeypatch.setenv("CODEX_THREAD_ID", owner)
    monkeypatch.setattr(executor, "_pid_alive", lambda _pid: False)
    _parent_config, parent_dir, parent_state_path, _parent_state_bytes = create_uncertain_parent_run(
        executor, root, run_root, owner
    )
    conversation_url = "https://chatgpt.com/c/lost-observer?temporary-chat=true"
    (parent_dir / "stdout.log").write_text(
        "Session: old-session\n"
        "Reattach: oracle session old-session\n"
        "Activated send button\n"
        f"[browser] conversation url (post-submit) = {conversation_url}\n"
        f"[browser] conversation url (assistant-wait) = {conversation_url}\n"
        "Waiting for ChatGPT response\n"
        "Confirming the capture is terminal (not a mid-stream/preamble capture)\n"
        "[browser] ChatGPT thinking - 30s elapsed; status=response streaming; source=inline\n"
        "[browser] Waiting for ChatGPT response - 14m 0s elapsed; no thinking status detected yet.\n",
        encoding="utf-8",
    )
    (parent_dir / "reconnect-stdout.log").write_text(
        'No live ChatGPT tab matched session "old-session". '
        "Attempting recovery by reopening the saved conversation URL.\n",
        encoding="utf-8",
    )
    (parent_dir / "reconnect-stderr.log").write_text(
        "Recovered ChatGPT conversation did not become ready in time.\n",
        encoding="utf-8",
    )
    parent_state = json.loads(parent_state_path.read_text(encoding="utf-8"))
    parent_state["oracle_process_pid"] = 41001
    parent_state["reconnect_process_pid"] = 41002
    parent_state["reconnect_exit_code"] = 1
    parent_state["oracle"]["binding"] = {
        "prompt_submitted": True,
        "session_status": "running",
        "conversation_url": conversation_url,
        "host": "127.0.0.1",
        "port": 43123,
        "target_id": "B" * 32,
    }
    parent_state_path.write_text(
        json.dumps(parent_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    successor_config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="authorized-retry-run-lost-observer-0001",
        model="gpt-5.6-sol",
        effort="extended",
        app_name="codex",
    )

    ticket = executor._authorize_uncertain_retry(
        successor_config,
        parent_dir,
        confirmed=True,
    )

    assert ticket["authorization"] == "explicit-user-authorized-retry-despite-uncertain-delivery"
    assert ticket["possible_duplicate_delivery"] is True
    assert ticket["successor_run_id"] == successor_config.run_id


@pytest.mark.parametrize(
    "preflight_error",
    [
        "[WinError 32] Cookies database is locked by Chrome",
        "temporary-chat personalization could not be confirmed before submission",
    ],
)
def test_authorized_retry_resumes_same_run_after_proven_pre_submit_failure(
    executor, execution_paths, monkeypatch, preflight_error
):
    root, mission, run_root, session_root = execution_paths
    owner = "01234567-89ab-cdef-0123-456789abcdef"
    monkeypatch.setenv("CODEX_THREAD_ID", owner)
    monkeypatch.setattr(executor, "_pid_alive", lambda _pid: False)
    _parent_config, parent_dir, parent_state_path, parent_state_bytes = create_uncertain_parent_run(
        executor, root, run_root, owner
    )
    config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="authorized-retry-run-resume-0001",
        model="gpt-5.6-sol",
        effort="extended",
        app_name="codex",
    )
    prepare_calls: list[Path] = []

    def prepare_profile(_config, run_dir):
        profile = run_dir / "browser-profile"
        profile.mkdir()
        (profile / "partial-copy.bin").write_bytes(b"preserve this failed preparation")
        prepare_calls.append(profile)
        return profile

    monkeypatch.setattr(executor, "_prepare_run_profile", prepare_profile)
    preflight_calls = 0

    def preflight(command, profile, port):
        nonlocal preflight_calls
        preflight_calls += 1
        if preflight_calls == 1:
            raise RuntimeError(preflight_error)
        return fake_preflight(command, profile, port)

    launches: list[list[str]] = []

    def popen(argv, **kwargs):
        launches.append(list(argv))
        Path(argv[argv.index("--write-output") + 1]).write_text(
            "Authorized one-shot response.\n", encoding="utf-8"
        )
        kwargs["stdout"].write(native_sol_plus_high_evidence().encode())
        kwargs["stdout"].flush()
        write_session_meta(
            session_root,
            argv[argv.index("--slug") + 1],
            status="completed",
            port=browser_port(argv),
        )
        return Process(0)

    first = executor.execute_config(
        config,
        retry_from_run=parent_dir,
        confirm_uncertain_retry=True,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda _command: "oracle 0.20.0",
        compat_factory=lambda _version: {"ok": True},
        browser_preflight=preflight,
        popen_factory=lambda *_args, **_kwargs: pytest.fail("pre-submit profile failure must not launch Oracle"),
    )
    assert first["result"]["failure_stage"] == "profile-preparation"
    assert first["result"]["submission"] == "not_observed"
    with pytest.raises(executor.ExecutionError) as reconnect_error:
        executor.reconnect_run(Path(first["run_dir"]), dry_run=True)
    assert reconnect_error.value.code == "RUN_NOT_SUBMITTED"

    resumed = executor.execute_config(
        config,
        retry_from_run=parent_dir,
        confirm_uncertain_retry=True,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda _command: "oracle 0.20.0",
        compat_factory=lambda _version: {"ok": True},
        browser_preflight=preflight,
        popen_factory=popen,
        browser_cleanup=fake_cleanup,
    )

    run_dir = Path(resumed["run_dir"])
    archived_attempt = run_dir / "pre-submit-attempts" / "attempt-001"
    archive_index = json.loads((run_dir / "pre-submit-attempts" / "index.json").read_text(encoding="utf-8"))
    final_state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    ticket = json.loads((parent_dir / "authorized-retry.json").read_text(encoding="utf-8"))

    assert resumed["status"] == "captured"
    assert len(launches) == 1 and launches[0].count("--prompt") == 1
    assert prepare_calls == [run_dir / "browser-profile", run_dir / "browser-profile"]
    assert (archived_attempt / "state.json").is_file()
    assert (archived_attempt / "browser-profile" / "partial-copy.bin").read_bytes() == b"preserve this failed preparation"
    assert archive_index["schema"] == "codex.chatgpt.oracle-pre-submit-attempt-history/v1"
    assert archive_index["attempts"][0]["submission"] == "not_observed"
    assert archive_index["attempts"][0]["state_sha256"] == hashlib.sha256((archived_attempt / "state.json").read_bytes()).hexdigest()
    assert final_state["authorized_retry"]["authorization_sha256"] == hashlib.sha256(
        (parent_dir / "authorized-retry.json").read_bytes()
    ).hexdigest()
    assert ticket["successor_run_id"] == config.run_id
    assert parent_state_path.read_bytes() == parent_state_bytes
    assert resumed["result"]["artifacts"]["output_bytes"] > 0


def test_authorized_retry_resumes_same_run_after_exact_no_send_high_gate_failure(
    executor, execution_paths, monkeypatch
):
    root, mission, run_root, session_root = execution_paths
    owner = "01234567-89ab-cdef-0123-456789abcdef"
    monkeypatch.setenv("CODEX_THREAD_ID", owner)
    _parent_config, parent_dir, parent_state_path, parent_state_bytes = create_uncertain_parent_run(
        executor, root, run_root, owner
    )
    config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="authorized-retry-run-high-gate-0001",
        model="gpt-5.6-sol",
        effort="extended",
        app_name="codex",
    )
    browser_pid = 7777
    oracle_pid = Process.pid
    monkeypatch.setattr(
        executor,
        "_pid_alive",
        lambda pid: int(pid or 0) == browser_pid,
    )

    prepare_calls: list[Path] = []

    def prepare_profile(_config, run_dir):
        profile = run_dir / "browser-profile"
        profile.mkdir()
        (profile / "owned-profile.bin").write_bytes(b"owned failed browser profile")
        prepare_calls.append(profile)
        return profile

    monkeypatch.setattr(executor, "_prepare_run_profile", prepare_profile)
    preflight_calls = 0
    temporary_url = "https://chatgpt.com/?temporary-chat=true"

    def preflight(_command, _profile, port):
        nonlocal preflight_calls
        preflight_calls += 1
        return {
            "ok": True,
            "pid": browser_pid if preflight_calls == 1 else browser_pid + 1,
            "port": port,
            "target_id": "B" * 32,
            "conversation_url": temporary_url,
            "browser_ws": f"ws://127.0.0.1:{port}/devtools/browser/owned",
            "startup_blank_tabs": 0,
            "page_count": 1,
            "personalization": "enabled",
            "logs": ["[browser] Temporary chat personalization: enabled"],
        }

    launches = 0

    def popen(argv, **kwargs):
        nonlocal launches
        launches += 1
        slug = argv[argv.index("--slug") + 1]
        port = browser_port(argv)
        if launches == 1:
            kwargs["stdout"].write(
                (
                    "Model picker: GPT-5.6 Sol\n"
                    "Prompt textarea ready (after model switch, 574 chars queued)\n"
                    "[browser] Model picker diagnostic: "
                    '{\"targetLevel\":\"extended\",\"menus\":[{\"items\":['
                    '{\"role\":\"menuitem\",\"ariaLabel\":\"Select model\",\"text\":\"High\"},'
                    '{\"role\":\"menuitem\",\"ariaLabel\":\"Power\",\"text\":\"\"},'
                    '{\"role\":\"menuitemradio\",\"ariaChecked\":\"true\",\"text\":\"GPT-5.6 Sol\"},'
                    '{\"role\":\"menuitemradio\",\"ariaChecked\":\"false\",\"text\":\"GPT-5.5 Leaving on October 14\"}'
                    "]}]}\n"
                    "[retry] Thinking time (extended) attempt 3: Thinking time: menu not found "
                    "(requested Extended); refusing to submit without confirmed High.\n"
                    "ERROR: Thinking time: menu not found (requested Extended); refusing to submit "
                    "without confirmed High.\n"
                    "User error (browser-automation): Thinking time: menu not found "
                    "(requested Extended); refusing to submit without confirmed High.\n"
                ).encode()
            )
            kwargs["stdout"].flush()
            write_session_meta(
                session_root,
                slug,
                status="error",
                submitted=False,
                port=port,
                url=temporary_url,
            )
            return Process(1)
        Path(argv[argv.index("--write-output") + 1]).write_text(
            "Authorized one-shot response.\n", encoding="utf-8"
        )
        kwargs["stdout"].write(native_sol_plus_high_menu_evidence().encode())
        kwargs["stdout"].flush()
        write_session_meta(
            session_root,
            slug,
            status="completed",
            submitted=True,
            port=port,
        )
        return Process(0)

    cleanup_calls: list[tuple[dict, str | None]] = []

    def cleanup(preflight_evidence, _command, *, expected_url=None):
        cleanup_calls.append((dict(preflight_evidence), expected_url))
        return {"status": "closed", "target_id": preflight_evidence["target_id"]}

    first = executor.execute_config(
        config,
        retry_from_run=parent_dir,
        confirm_uncertain_retry=True,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda _command: "oracle 0.20.0",
        compat_factory=lambda _version: {"ok": True},
        browser_preflight=preflight,
        popen_factory=popen,
        browser_cleanup=cleanup,
    )
    assert first["status"] == "attention_required"
    assert first["result"]["submission"] == "not_observed"
    assert first["result"]["oracle"]["binding"]["prompt_submitted"] is False
    assert first["result"]["artifacts"]["output_bytes"] == 0
    assert cleanup_calls == []

    resumed = executor.execute_config(
        config,
        retry_from_run=parent_dir,
        confirm_uncertain_retry=True,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda _command: "oracle 0.20.0",
        compat_factory=lambda _version: {"ok": True},
        browser_preflight=preflight,
        popen_factory=popen,
        browser_cleanup=cleanup,
    )

    run_dir = Path(resumed["run_dir"])
    archived_attempt = run_dir / "pre-submit-attempts" / "attempt-001"
    archived_state = json.loads((archived_attempt / "state.json").read_text(encoding="utf-8"))
    assert resumed["status"] == "captured"
    assert launches == 2
    assert prepare_calls == [run_dir / "browser-profile", run_dir / "browser-profile"]
    assert archived_state["oracle"]["binding"]["prompt_submitted"] is False
    assert archived_state["browser_cleanup"]["status"] == "closed"
    assert (archived_attempt / "stdout.log").stat().st_size > 0
    assert (archived_attempt / "browser-profile" / "owned-profile.bin").is_file()
    assert len(cleanup_calls) == 2
    assert cleanup_calls[0][0]["pid"] == browser_pid
    assert cleanup_calls[0][1] == temporary_url
    assert parent_state_path.read_bytes() == parent_state_bytes


def test_authorized_retry_refuses_to_resume_unproven_pre_submit_failure(
    executor, execution_paths, monkeypatch
):
    root, mission, run_root, _ = execution_paths
    owner = "01234567-89ab-cdef-0123-456789abcdef"
    monkeypatch.setenv("CODEX_THREAD_ID", owner)
    monkeypatch.setattr(executor, "_pid_alive", lambda _pid: False)
    _parent_config, parent_dir, _parent_state_path, _parent_state_bytes = create_uncertain_parent_run(
        executor, root, run_root, owner
    )
    config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="authorized-retry-run-unproven-0001",
        model="gpt-5.6-sol",
        effort="extended",
        app_name="codex",
    )

    def prepare_profile(_config, run_dir):
        profile = run_dir / "browser-profile"
        profile.mkdir()
        return profile

    monkeypatch.setattr(executor, "_prepare_run_profile", prepare_profile)
    first = executor.execute_config(
        config,
        retry_from_run=parent_dir,
        confirm_uncertain_retry=True,
        command_resolver=lambda: ["oracle"],
        version_resolver=lambda _command: "oracle 0.20.0",
        compat_factory=lambda _version: {"ok": True},
        browser_preflight=lambda *_args: (_ for _ in ()).throw(RuntimeError("unclassified browser startup failure")),
    )
    assert first["result"]["submission"] == "not_observed"

    with pytest.raises(executor.ExecutionError) as exc:
        executor.execute_config(
            config,
            retry_from_run=parent_dir,
            confirm_uncertain_retry=True,
            command_resolver=lambda: pytest.fail("unproven failures must be rejected before Oracle resolution"),
        )

    assert exc.value.code == "PRE_SUBMIT_RESTART_NOT_SAFE"
    assert not (Path(first["run_dir"]) / "pre-submit-attempts").exists()


def test_uncertain_retry_requires_user_confirmation_before_creating_ticket(
    executor, execution_paths, monkeypatch
):
    root, mission, run_root, _ = execution_paths
    owner = "01234567-89ab-cdef-0123-456789abcdef"
    monkeypatch.setenv("CODEX_THREAD_ID", owner)
    _parent_config, parent_dir, _parent_state_path, _parent_state_bytes = create_uncertain_parent_run(
        executor, root, run_root, owner
    )
    successor_config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="authorized-retry-run-0002",
        model="gpt-5.6-sol",
        effort="extended",
        app_name="codex",
    )

    with pytest.raises(executor.ExecutionError) as exc:
        executor.execute_config(
            successor_config,
            retry_from_run=parent_dir,
            command_resolver=lambda: pytest.fail("confirmation must be checked before resolving Oracle"),
        )

    assert exc.value.code == "RETRY_CONFIRMATION_REQUIRED"
    assert not (parent_dir / "authorized-retry.json").exists()
    assert not (run_root / successor_config.run_id).exists()


def test_unreadable_prior_run_state_blocks_replacement_submission(executor, execution_paths):
    root, mission, run_root, _ = execution_paths
    corrupt_run = run_root / "ordinary-run-corrupt"
    corrupt_run.mkdir(parents=True)
    (corrupt_run / "state.json").write_text('{"schema":', encoding="utf-8")
    config = executor.make_config(
        project_root=root,
        mission_path=mission,
        run_root=run_root,
        run_id="ordinary-run-0008",
    )

    with pytest.raises(executor.ExecutionError) as exc:
        executor.execute_config(
            config,
            command_resolver=lambda: pytest.fail("corrupt prior state must block before Oracle resolution"),
        )

    assert exc.value.code == "RUN_RECONNECT_REQUIRED"
    assert exc.value.evidence == {
        "run_dir": str(corrupt_run),
        "status": "state_unreadable",
        "submission": "unknown",
        "state_error_code": "RUN_STATE_INVALID",
    }
    assert not (run_root / config.run_id).exists()
