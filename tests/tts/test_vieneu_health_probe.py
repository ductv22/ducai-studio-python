import json
import subprocess

import pytest

from ducai_studio.providers.tts import vieneu


def _probe(label):
    return vieneu._probe_runtime_and_profile(label, "", (0, 0), (0, 0), (0, 0), ())


def test_probe_allows_realistic_torch_cold_start(monkeypatch):
    def run(*args, **kwargs):
        if kwargs["timeout"] < 24:
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        return subprocess.CompletedProcess(args[0], 0, '{"version":"3.8.1"}', "")

    monkeypatch.setattr(vieneu.subprocess, "run", run)
    assert _probe("cold-start")["version"] == "3.8.1"


@pytest.mark.parametrize("failure", ["timeout", "exit", "json", "shape"])
def test_transient_probe_failure_is_not_cached(monkeypatch, failure):
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
            if failure == "exit":
                raise subprocess.CalledProcessError(1, args[0], stderr="private details")
            return subprocess.CompletedProcess(
                args[0], 0, "not-json" if failure == "json" else "[]", ""
            )
        return subprocess.CompletedProcess(args[0], 0, '{"version":"3.8.1"}', "")

    monkeypatch.setattr(vieneu.subprocess, "run", run)
    first = _probe("retry-" + failure)
    assert "private details" not in json.dumps(first)
    assert _probe("retry-" + failure)["version"] == "3.8.1"
    assert _probe("retry-" + failure)["version"] == "3.8.1"
    assert len(calls) == 2
