from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QObject

from ducai_studio.infrastructure import runtime_program
from ducai_studio.ui.viewmodels import update_view_model as module


class _Settings:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def value(self, key: str, default=None, *, type=None):
        value = self.values.get(key, default)
        return type(value) if type else value

    def setValue(self, key: str, value: object) -> None:
        self.values[key] = value


class _Runtime(QObject):
    def __init__(self, root: Path) -> None:
        super().__init__()
        self.database_path = root / "state.db"
        self.requests: list[tuple[str, object, object]] = []

    def request(self, action: str, params=None, *, context=None) -> str:
        self.requests.append((action, params, context))
        return str(len(self.requests))


def _release() -> SimpleNamespace:
    manifest = SimpleNamespace(
        version="2.0.0a118",
        digest="a" * 64,
        archive=SimpleNamespace(name="DUCAI-STUDIO-2.0.0a118-win64-update.zip", size=10),
    )
    return SimpleNamespace(
        manifest=manifest,
        manifest_bytes=b"signed-manifest",
        signature_bytes=b"s" * 64,
        release=SimpleNamespace(archive=SimpleNamespace(url="https://example.invalid/update")),
    )


def test_manual_check_is_immediate_and_channel_is_persisted(tmp_path, monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    settings = _Settings()
    vm = module.UpdateViewModel(_Runtime(tmp_path), settings)
    tasks = []
    monkeypatch.setattr(vm._pool, "start", tasks.append)

    vm.setChannel("stable")
    assert settings.values["updates/channel"] == "stable"
    vm.check()
    assert vm.state == "checking"
    assert len(tasks) == 1

    vm._checked(None, "offline")
    assert vm.state == "error"
    assert "offline" in vm.message
    vm.check()
    vm._checked(None, "")
    assert vm.state == "up_to_date"


def test_portable_update_staging_is_outside_install_tree(tmp_path, monkeypatch) -> None:
    install = tmp_path / "Portable App"
    local = tmp_path / "Local App Data"
    runtime = _Runtime(install / "DUCAI_STUDIO_DATA")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(module, "current_install_root", lambda: install.resolve())

    assert module._update_work_root(runtime) == (
        local / "DUCAI STUDIO" / "updates"
    ).resolve()


def test_download_reports_real_bytes_and_keeps_verified_stage(tmp_path, monkeypatch) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    vm = module.UpdateViewModel(_Runtime(tmp_path), _Settings())
    tasks = []
    monkeypatch.setattr(vm._pool, "start", tasks.append)
    vm.check()
    vm._checked(_release(), "")
    assert vm.state == "available"

    def download(_url, destination, _manifest, *, progress, cancelled):
        assert not cancelled()
        progress(4, 10)
        assert vm.receivedBytes == 4
        assert vm.percent == 40
        destination.write_bytes(b"archive")
        return destination

    def extract(_archive, stage, _manifest, *, cancelled):
        assert not cancelled()
        stage.mkdir()
        return stage

    monkeypatch.setattr(module, "download_verified_archive", download)
    monkeypatch.setattr(module, "extract_verified_archive", extract)
    vm.download()
    assert vm.state == "downloading"
    tasks[-1].run()
    assert vm.state == "ready"
    assert vm._stage.is_dir()
    assert vm._manifest_path.read_bytes() == b"signed-manifest"
    assert vm._signature_path.read_bytes() == b"s" * 64


def test_install_reserves_globally_and_never_auto_installs_after_jobs(
    tmp_path, monkeypatch
) -> None:
    QCoreApplication.instance() or QCoreApplication([])
    runtime = _Runtime(tmp_path)
    vm = module.UpdateViewModel(runtime, _Settings())
    tasks = []
    monkeypatch.setattr(vm._pool, "start", tasks.append)
    vm._release = _release()
    vm._stage = tmp_path
    vm._manifest_path = tmp_path / "manifest.json"
    vm._signature_path = tmp_path / "manifest.sig"
    vm._set_state("ready", "ready")

    confirmations = []
    vm.installConfirmationRequested.connect(lambda: confirmations.append(True))
    vm.requestInstallConfirmation()
    assert confirmations == [True]
    assert runtime.requests == []

    vm.installConfirmed()
    assert vm.state == "installing"
    assert runtime.requests[-1][0] == "reserve_update_install"
    vm.action_succeeded(
        "reserve_update_install",
        {"eligible": False, "active_job_count": 1, "multi_state": "running"},
    )
    assert vm.state == "waiting_jobs"
    assert tasks == []

    vm.setInstallAfterJobs(True)
    vm.action_succeeded("get_update_install_eligibility", {"eligible": True})
    assert vm.state == "ready"
    assert runtime.requests[-1][0] == "reserve_update_install"
    assert tasks == []

    vm.installConfirmed()
    vm.action_succeeded("reserve_update_install", {"eligible": True, "token": "lease"})
    assert len(tasks) == 1
    vm._launched("launch failed")
    assert vm.state == "ready"
    assert runtime.requests[-1][0] == "release_update_install"
    assert runtime.requests[-1][1] == {"token": "lease"}


def test_install_task_uses_actual_frozen_root_without_explicit_override(tmp_path, monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    install = tmp_path / "Portable App"
    install.mkdir()
    executable = install / "DucaiStudio2.exe"
    executable.touch()
    monkeypatch.setattr(runtime_program.sys, "executable", str(install / "python.exe"))
    monkeypatch.setattr(runtime_program.sys, "argv", [str(executable)])
    prepared = []
    launched = []
    errors = []
    handoff = object()

    def prepare(**kwargs):
        prepared.append(kwargs)
        return handoff

    monkeypatch.setattr(module, "prepare_update_handoff", prepare)
    monkeypatch.setattr(
        module, "launch_update_helper", lambda value, **kwargs: launched.append(value)
    )
    signals = module._UpdateSignals()
    signals.launched.connect(errors.append)
    task = module._InstallTask(
        signals, tmp_path / "payload", tmp_path / "manifest", tmp_path / "signature",
        tmp_path / "updates", "2.0.0a151", "a" * 64,
    )

    task.run()

    assert prepared[0]["install_root"] == install.resolve()
    assert launched == [handoff]
    assert errors == [""]


def test_portable_staging_resolves_synthetic_frozen_path(tmp_path, monkeypatch):
    install = tmp_path / "Portable App"
    install.mkdir()
    executable = install / "DucaiStudio2.exe"
    executable.touch()
    monkeypatch.setattr(runtime_program.sys, "executable", str(install / "python.exe"))
    monkeypatch.setattr(runtime_program.sys, "argv", [str(executable)])
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local App Data"))
    runtime = _Runtime(install / "DUCAI_STUDIO_DATA")

    assert module._update_work_root(runtime) == (
        tmp_path / "Local App Data" / "DUCAI STUDIO" / "updates"
    ).resolve()
