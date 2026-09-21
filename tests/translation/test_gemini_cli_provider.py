from __future__ import annotations

import json

import pytest

from ducai_studio.providers.translation.contracts import (
    AuthenticationRequired,
    TranslationPurpose,
    TranslationRequest,
)
from ducai_studio.providers.translation.gemini_cli import GeminiCliProvider


class FakeProcess:
    def __init__(self, command, **_kwargs) -> None:
        self.command = command
        self.returncode = 0
        self.input = ""

    def communicate(self, value=None, timeout=None):
        self.input = value or ""
        return json.dumps({"response": "{\"translations\":[]}", "stats": {}}), ""

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -1

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def _request() -> TranslationRequest:
    return TranslationRequest(
        "cli-1", TranslationPurpose.TRANSLATE, "Một prompt rất dài", (1,)
    )


def test_cli_uses_stdin_headless_json_instead_of_command_line_prompt(monkeypatch) -> None:
    processes: list[FakeProcess] = []

    def create(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("shutil.which", lambda _value: "C:/bin/gemini.cmd")
    monkeypatch.setattr("subprocess.Popen", create)
    provider = GeminiCliProvider(model_id="gemini-test")

    response = provider.generate(_request())

    assert response.text == '{"translations":[]}'
    assert "Một prompt rất dài" not in processes[0].command
    assert processes[0].input == "Một prompt rất dài"
    assert "--output-format" in processes[0].command


def test_cli_classifies_oauth_failure(monkeypatch) -> None:
    class FailedProcess(FakeProcess):
        def communicate(self, value=None, timeout=None):
            self.returncode = 1
            return "", "Please login with OAuth"

    monkeypatch.setattr("shutil.which", lambda _value: "C:/bin/gemini.cmd")
    monkeypatch.setattr("subprocess.Popen", FailedProcess)
    provider = GeminiCliProvider(model_id="gemini-test")

    with pytest.raises(AuthenticationRequired):
        provider.generate(_request())
