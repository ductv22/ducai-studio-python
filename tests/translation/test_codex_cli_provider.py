from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ducai_studio.providers.translation.codex_cli import CodexCliProvider
from ducai_studio.providers.translation.contracts import (
    TranslationProviderFailure,
    TranslationPurpose,
    TranslationRequest,
)


class FakeProcess:
    def __init__(self, command: list[str], **_kwargs) -> None:
        self.command = command
        self.returncode = 0

    def communicate(self, prompt: str, timeout: float):
        assert prompt == "DỊCH CHUNK"
        assert timeout == 15.0
        output = Path(self.command[self.command.index("--output-last-message") + 1])
        output.write_text(
            '{"translations":[{"id":1,"text":"Bản dịch."}],"glossary_updates":[]}',
            encoding="utf-8",
        )
        return "", ""

    def poll(self):
        return self.returncode


def test_codex_cli_uses_ephemeral_luna_and_reads_only_last_message(
    monkeypatch,
) -> None:
    commands: list[list[str]] = []

    def fake_popen(command: list[str], **kwargs):
        commands.append(command)
        return FakeProcess(command, **kwargs)

    monkeypatch.setattr("shutil.which", lambda _value: "C:/bin/codex.exe")
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    provider = CodexCliProvider(
        model_id="gpt-5.6-luna",
        timeout_seconds=15,
    )

    response = provider.generate(
        TranslationRequest("chunk-1", TranslationPurpose.TRANSLATE, "DỊCH CHUNK", (1,))
    )

    assert response.text.startswith('{"translations"')
    command = commands[0]
    assert command[:4] == ["C:/bin/codex.exe", "exec", "--model", "gpt-5.6-luna"]
    assert "--ephemeral" in command
    assert command[-1] == "-"
    assert subprocess.CREATE_NO_WINDOW >= 0


def test_codex_cli_runs_windows_cmd_shim_through_command_interpreter(
    monkeypatch,
) -> None:
    commands: list[list[str]] = []

    def fake_popen(command: list[str], **kwargs):
        commands.append(command)
        return FakeProcess(command, **kwargs)

    monkeypatch.setattr("os.name", "nt")
    # Pin the simulated Windows architecture: uncached platform.machine()
    # may itself launch cmd, outside this test's provider-process fake.
    monkeypatch.setattr("platform.machine", lambda: "AMD64")
    monkeypatch.setenv("COMSPEC", "C:/Windows/System32/cmd.exe")
    monkeypatch.setattr("shutil.which", lambda _value: "C:/Users/test/codex.CMD")
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    provider = CodexCliProvider(timeout_seconds=15)

    provider.generate(
        TranslationRequest("chunk-1", TranslationPurpose.TRANSLATE, "DỊCH CHUNK", (1,))
    )

    assert commands[0][:6] == [
        "C:/Windows/System32/cmd.exe",
        "/d",
        "/s",
        "/c",
        str(Path("C:/Users/test/codex.CMD")),
        "exec",
    ]


def test_codex_cli_prefers_native_binary_inside_npm_package(
    tmp_path, monkeypatch
) -> None:
    npm_root = tmp_path / "npm"
    shim = npm_root / "codex.CMD"
    native = (
        npm_root
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    native.parent.mkdir(parents=True)
    shim.write_text("@echo off", encoding="utf-8")
    native.write_bytes(b"MZ")
    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setattr("platform.machine", lambda: "AMD64")
    monkeypatch.setattr("shutil.which", lambda _value: str(shim))

    provider = CodexCliProvider()

    assert provider.executable == str(native)


def test_codex_cli_maps_launch_permission_error_to_resumable_failure(
    monkeypatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _value: "C:/bin/codex.exe")

    def denied(*_args, **_kwargs):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr("subprocess.Popen", denied)
    provider = CodexCliProvider(timeout_seconds=15)

    with pytest.raises(TranslationProviderFailure, match="Không khởi chạy được"):
        provider.generate(
            TranslationRequest("chunk-1", TranslationPurpose.TRANSLATE, "DỊCH CHUNK", (1,))
        )
