"""Real IPC/QML activation smoke in disposable data roots, with owner-issued token."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QCoreApplication, QUrl
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication

from ducai_studio.infrastructure.machine_identity import current_device_id, request_code
from ducai_studio.infrastructure.offline_license import ActivationRequired
from ducai_studio.ui.app import _register_bundled_fonts
from ducai_studio.ui.qt_runtime import configure_qt_plugin_dll_search
from ducai_studio.ui.viewmodels.application_view_model import ApplicationViewModel


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    root = arguments.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    owner_file = Path(__file__).resolve().parents[2] / "scripts" / "license-issuer.py"
    device = current_device_id()
    owner_state = root / "issuer-state"
    legacy_file = root / "source-smoke-legacy.ducai-license"
    compact_file = root / "source-smoke-compact.ducai-license"
    compact_import_file = root / "compact-import.ducai-license"
    try:
        for token_file, extra in ((legacy_file, ["--legacy"]), (compact_file, [])):
            issued = subprocess.run(
                [sys.executable, str(owner_file), "--state-root", str(owner_state), "issue",
                 "--request-code", request_code(device), "--output", str(token_file), *extra],
                capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            assert issued.returncode == 0, "Owner issuer signing failed"
        legacy_token = legacy_file.read_text(encoding="utf-8")
        compact_token = compact_file.read_text(encoding="utf-8")
        assert legacy_token.startswith("DUCAI-L1.")
        assert compact_token.startswith("DUCAI-L2.") and len(compact_token) == 128
    finally:
        legacy_file.unlink(missing_ok=True)
        compact_file.unlink(missing_ok=True)
        for filename in ("issued-licenses.json", "key-info.json", "issuer.lock"):
            (owner_state / filename).unlink(missing_ok=True)
    os.environ["LOCALAPPDATA"] = str(root / "isolated-localappdata")
    os.environ["DUCAI_APP_DATA_ROOT"] = str(root / "isolated-projectdata")
    QCoreApplication.setOrganizationName("DUCAI licensing smoke")
    QCoreApplication.setApplicationName("Activation smoke")
    QQuickStyle.setStyle("Basic")
    configure_qt_plugin_dll_search()
    app = QApplication([])
    _register_bundled_fonts()
    model = ApplicationViewModel(app)
    qml = Path(__file__).resolve().parents[2] / "src/ducai_studio/ui/qml"
    engine = QQmlApplicationEngine(model)
    errors: list[str] = []
    engine.warnings.connect(lambda values: errors.extend(value.toString() for value in values))
    engine.addImportPath(str(qml))
    engine.setInitialProperties({"appModel": model})
    engine.load(QUrl.fromLocalFile(str(qml / "Main.qml")))
    assert engine.rootObjects(), errors
    window = engine.rootObjects()[0]
    window.setWidth(1180)
    window.setHeight(720)

    def wait_for(predicate, timeout=30) -> None:
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert predicate(), model.statusMessage

    results: dict = {}
    try:
        model.start()
        wait_for(lambda: model.backendReady and not model.activation.busy)
        assert not model.activation.active
        assert model.currentWorkspace == "settings"
        results["unlicensed_settings"] = True
        model.navigate("timeline")
        assert model.currentWorkspace == "settings"
        denied: list[dict] = []
        model.runtime.actionFailed.connect(
            lambda action, error, _context:
            denied.append(error) if action == "create_project" else None)
        # Direct IPC cannot be bypassed by stale or edited QML.
        model.runtime._request("create_project", {"project_path": str(root / "denied.ducai"),
                                                 "name": "Denied"})
        wait_for(lambda: bool(denied))
        assert denied[-1]["code"] == ActivationRequired.__name__
        assert not (root / "denied.ducai").exists()
        results["ipc_denied"] = True
        window.grabWindow().save(str(root / "activation-unlicensed.png"))
        model.activation.activate(legacy_token)
        wait_for(lambda: model.activation.active and not model.activation.busy)
        results["real_owner_legacy_signature_activation"] = True
        saved = root / "isolated-localappdata/DUCAI STUDIO/Licensing/license-v1.txt"
        before = saved.read_bytes()
        assert before == legacy_token.encode("utf-8")
        model.activation.activate(compact_token[:-1])
        wait_for(lambda: not model.activation.busy)
        assert model.activation.active
        assert saved.read_bytes() == before
        results["invalid_l2_replacement_preserves_legacy_key"] = True
        from ducai_studio.infrastructure.licensing_state import LicensingState
        assert LicensingState().status()["active"]
        results["fresh_verifier_legacy_restart_persistence"] = True
        compact_import_file.write_text(compact_token, encoding="utf-8-sig")
        model.activation.importLicense(QUrl.fromLocalFile(str(compact_import_file)))
        wait_for(lambda: model.activation.active and not model.activation.busy)
        compact_bytes = saved.read_bytes()
        assert compact_bytes == compact_token.encode("utf-8") and compact_bytes != before
        model.activation.activate("DUCAI-L1.not-a-valid-key")
        wait_for(lambda: not model.activation.busy)
        assert model.activation.active
        assert saved.read_bytes() == compact_bytes
        results["real_owner_compact_file_import_and_invalid_l1_preservation"] = True
        model.activation.refresh()
        wait_for(lambda: not model.activation.busy)
        assert model.activation.active
        model.navigate("settings")
        app.processEvents()
        window.grabWindow().save(str(root / "activation-licensed.png"))
        created: list[dict] = []
        model.runtime.actionSucceeded.connect(
            lambda action, result, _context: created.append(result)
            if action == "create_project" else None)
        project_file = root / f"smoke-{uuid4().hex}.ducai"
        model.runtime.request("create_project", {"project_path": str(project_file),
                                                "name": "License smoke"})
        wait_for(lambda: bool(created))
        results["licensed_project_create"] = True
        assert LicensingState().status()["active"]
        results["fresh_verifier_compact_restart_persistence"] = True
        results["qml_errors"] = errors
        assert not errors, errors
        (root / "result.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    finally:
        complete: list[bool] = []
        model.quitReady.connect(lambda: complete.append(True))
        model.requestQuit()
        try:
            wait_for(lambda: bool(complete), timeout=10)
        finally:
            model.forceShutdown()
            # Do not leave a machine-bound token in test reports or repository.
            license_file = root / "isolated-localappdata/DUCAI STUDIO/Licensing/license-v1.txt"
            license_file.unlink(missing_ok=True)
            compact_import_file.unlink(missing_ok=True)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
