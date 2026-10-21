from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from uuid import uuid4

import pytest
from PySide6.QtCore import QCoreApplication

from ducai_studio.infrastructure.ipc.messages import (
    IPC_PROTOCOL_VERSION,
    FrameDecoder,
    IpcMessage,
)
from ducai_studio.infrastructure.licensing_state import (
    UNLICENSED_ACTIONS,
    LicensingState,
)
from ducai_studio.infrastructure.offline_license import ActivationRequired
from ducai_studio.job_manager.server import JobManagerServer
from ducai_studio.job_manager.service import JobManagerService

EXPECTED_UNLICENSED_ACTIONS = frozenset(
    {
        "ping",
        "get_license_status",
        "activate_license",
        "runtime_diagnostics",
        "shutdown",
        "get_update_install_eligibility",
        "reserve_update_install",
        "release_update_install",
        "pause_job",
        "cancel_job",
        "multi_pause",
        "cancel_database_optimization",
    }
)


def _registered_actions() -> set[str]:
    source = textwrap.dedent(inspect.getsource(JobManagerServer._dispatch))
    actions: set[str] = {"list_translation_models"}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name):
            continue
        if node.left.id != "action":
            continue
        for comparator in node.comparators:
            values = (
                comparator.elts
                if isinstance(comparator, (ast.Set, ast.Tuple, ast.List))
                else [comparator]
            )
            actions.update(
                value.value
                for value in values
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            )
    return actions


@pytest.fixture
def unlicensed_server(tmp_path: Path, license_fixture):
    QCoreApplication.instance() or QCoreApplication([])
    service = JobManagerService(
        database_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )
    server = JobManagerServer(name=f"unlicensed-{uuid4()}", service=service)
    server.licensing = LicensingState(
        path=tmp_path / "missing-license.txt",
        identity=lambda: license_fixture.device_id,
        public_keys=license_fixture.public_keys,
    )
    try:
        yield server
    finally:
        server.close()


def test_every_registered_action_has_narrow_unlicensed_policy(license_fixture, tmp_path) -> None:
    assert UNLICENSED_ACTIONS == EXPECTED_UNLICENSED_ACTIONS
    registered = _registered_actions()
    assert registered >= EXPECTED_UNLICENSED_ACTIONS
    state = LicensingState(
        path=tmp_path / "missing.txt",
        identity=lambda: license_fixture.device_id,
        public_keys=license_fixture.public_keys,
    )

    for action in sorted(registered):
        if action in EXPECTED_UNLICENSED_ACTIONS:
            state.require_action(action)
        else:
            with pytest.raises(ActivationRequired, match="Chưa kích hoạt"):
                state.require_action(action)


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("_start_job", ({"job_id": "missing"},)),
        ("_retry_job", ("missing",)),
        ("_resume_job", ("missing",)),
    ],
)
def test_deep_job_admission_rejects_before_service_lookup(
    unlicensed_server: JobManagerServer, method: str, arguments: tuple[object, ...]
) -> None:
    with pytest.raises(ActivationRequired, match="Chưa kích hoạt"):
        getattr(unlicensed_server, method)(*arguments)


def test_dispatch_blocks_reads_edits_and_multi_before_service(
    unlicensed_server: JobManagerServer,
) -> None:
    for action in (
        "list_projects",
        "list_jobs",
        "list_credentials",
        "save_project_edits",
        "timeline_command",
        "create_job",
        "multi_start",
        "multi_resume",
    ):
        with pytest.raises(ActivationRequired, match="Chưa kích hoạt"):
            unlicensed_server._dispatch(action, {})


def test_async_model_catalog_is_denied_before_thread_start(
    unlicensed_server: JobManagerServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RecordingSocket:
        def __init__(self) -> None:
            self.frames: list[bytes] = []

        def write(self, frame: bytes) -> int:
            self.frames.append(bytes(frame))
            return len(frame)

        def flush(self) -> bool:
            return True

    started: list[object] = []
    monkeypatch.setattr(
        unlicensed_server,
        "_start_model_catalog",
        lambda *args: started.append(args),
    )
    socket = RecordingSocket()
    request = IpcMessage(
        IPC_PROTOCOL_VERSION,
        "catalog-request",
        "request",
        {
            "action": "list_translation_models",
            "params": {"provider_id": "openai", "credential_reference": "ref"},
        },
    )

    unlicensed_server._handle_message(socket, request)  # type: ignore[arg-type]

    response = FrameDecoder().feed(b"".join(socket.frames))[0]
    assert started == []
    assert response.payload["ok"] is False
    assert response.payload["error"]["code"] == "ActivationRequired"
    assert "Chưa kích hoạt" in response.payload["error"]["message"]


def test_manager_activation_unlocks_normal_dispatch(
    unlicensed_server: JobManagerServer, license_fixture
) -> None:
    result = unlicensed_server._dispatch(
        "activate_license",
        {"token": license_fixture.token()},
    )

    assert result["active"] is True
    assert unlicensed_server._dispatch("list_projects", {}) == []


@pytest.mark.parametrize(
    "module",
    [
        "ducai_studio.workers.ocr_worker",
        "ducai_studio.workers.translation_worker",
        "ducai_studio.workers.tts_worker",
        "ducai_studio.workers.export_worker",
    ],
)
def test_direct_worker_cli_is_blocked_without_provider_side_effect(
    tmp_path: Path, module: str
) -> None:
    env = os.environ.copy()
    env["DUCAI_TEST_LICENSE_PATH"] = str(tmp_path / "missing-license.txt")
    staging = tmp_path / "staging"
    env["DUCAI_ARTIFACT_STAGING_ROOT"] = str(staging)

    result = subprocess.run(
        [sys.executable, "-m", module, "unlicensed-probe"],
        input="",
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )

    assert result.returncode != 0
    assert result.stderr == "activation_required\n"
    assert not staging.exists() or not any(staging.iterdir())


def test_frozen_style_worker_route_cannot_bypass_activation(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["DUCAI_TEST_LICENSE_PATH"] = str(tmp_path / "missing-license.txt")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ducai_studio",
            "-m",
            "ducai_studio.workers.export_worker",
            "unlicensed-frozen-probe",
        ],
        input="",
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )

    assert result.returncode != 0
    assert result.stderr == "activation_required\n"


def test_test_bootstrap_hook_is_absent_from_product_source() -> None:
    root = Path(__file__).parents[2] / "src"
    for path in root.rglob("*.py"):
        assert "DUCAI_TEST_LICENSE_" not in path.read_text(encoding="utf-8"), path
