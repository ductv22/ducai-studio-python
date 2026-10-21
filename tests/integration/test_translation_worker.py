from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread

import pytest

import ducai_studio.workers.translation_worker as translation_worker_module
from ducai_studio.application.media_commands import parse_srt
from ducai_studio.application.translation_pipeline import (
    ChunkState,
    TranslationQaIssue,
    TranslationQaSeverity,
    TranslationValue,
    ledger_to_dict,
)
from ducai_studio.providers.contracts import (
    HealthState,
    ProviderCapabilities,
    ProviderHealth,
    ProviderKind,
)
from ducai_studio.providers.translation.contracts import (
    AuthenticationRequired,
    ProviderResponse,
    TransientProviderFailure,
    TranslationPurpose,
    TranslationRequest,
)
from ducai_studio.providers.translation.gemini_api import HttpResponse
from ducai_studio.providers.translation.openai_compatible import OpenAICompatibleProvider
from ducai_studio.workers.protocol import PROTOCOL_VERSION, WorkerMessage
from ducai_studio.workers.translation_worker import (
    TranslationWorker,
    _drop_resolved_qa_issues,
    _repair_values_from_qa_suggestions,
)


def test_provider_request_event_thaws_nested_usage_for_worker_protocol() -> None:
    worker = TranslationWorker("nested-usage-job")
    events: list[tuple[str, dict]] = []
    worker.send = lambda kind, payload: events.append((kind, payload))  # type: ignore[method-assign]
    request = TranslationRequest(
        "diagnostic",
        TranslationPurpose.TRANSLATE,
        "Return JSON",
    )
    response = ProviderResponse(
        "diagnostic",
        '{"ok":true}',
        "gemini-api",
        "gemini-3.5-flash-lite",
        {
            "promptTokenCount": 7,
            "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 7}],
        },
    )

    worker._emit_provider_request(request, response)

    kind, payload = events[0]
    message = WorkerMessage(PROTOCOL_VERSION, "nested-usage-job", "event-1", kind, payload)
    decoded = json.loads(message.to_json_line())
    assert decoded["payload"]["transport"]["promptTokensDetails"] == [
        {"modality": "TEXT", "tokenCount": 7}
    ]


def test_worker_factory_accepts_openai_compatible_provider(monkeypatch) -> None:
    class FakeCredentialStore:
        def read(self, reference: str) -> str:
            return f"secret-{reference}"

    monkeypatch.setattr(translation_worker_module, "WindowsCredentialStore", FakeCredentialStore)
    worker = TranslationWorker("openai-compatible-job")
    worker.spec = {"provider_id": "deepseek-api", "model_id": "deepseek-v4-flash"}

    provider = worker._provider_from_config(
        {
            "credential_references": ["deepseek-key"],
            "base_url": "https://api.deepseek.com",
            "json_mode": True,
            "extra_body": {"thinking": {"type": "disabled"}},
            "provider_timeout_seconds": 30,
        }
    )

    assert provider.provider_id == "deepseek-api"
    assert provider.model_id == "deepseek-v4-flash"
    provider.close()


def test_qa_suggestion_fallback_accepts_dialogue_but_rejects_editing_advice() -> None:
    values = (
        TranslationValue("cue-1", 1, "Lục Cảnh đã đến."),
        TranslationValue("cue-2", 2, "Tên cũ."),
        TranslationValue("cue-3", 3, "Câu cũ."),
    )
    issues = (
        TranslationQaIssue(
            1,
            "NAME_INCONSISTENCY",
            TranslationQaSeverity.BLOCKER,
            "Sai tên nhân vật.",
            "Lục Tĩnh đã đến.",
        ),
        TranslationQaIssue(
            2,
            "NAME_INCONSISTENCY",
            TranslationQaSeverity.BLOCKER,
            "Sai tên nhân vật.",
            "Nên dùng tên nhất quán theo Film Bible: Lục Tĩnh.",
        ),
        TranslationQaIssue(
            3,
            "TRANSLATION_ERROR",
            TranslationQaSeverity.BLOCKER,
            "Câu bị cụt.",
            "Sửa câu thành: Dám khiêu khích ta sao?",
        ),
    )

    repaired = _repair_values_from_qa_suggestions(values, issues)

    assert [value.text for value in repaired] == [
        "Lục Tĩnh đã đến.",
        "Tên cũ.",
        "Dám khiêu khích ta sao?",
    ]
    remaining = _drop_resolved_qa_issues(
        repaired,
        issues,
        previous_values=values,
    )
    assert [issue.index for issue in remaining] == [2]


def test_qa_suggestion_fallback_rejects_every_cjk_replacement() -> None:
    values = (TranslationValue("cue-1", 1, "Tên cũ."),)
    issues = (
        TranslationQaIssue(
            1,
            "NAME_INCONSISTENCY",
            TranslationQaSeverity.BLOCKER,
            "Tên nhân vật chưa đúng.",
            "Tôn Nh晔 đã đến.",
        ),
    )

    assert _repair_values_from_qa_suggestions(values, issues) == values


def test_qa_blocker_is_not_dropped_when_suggestion_was_already_current_text() -> None:
    values = (TranslationValue("cue-1", 1, "Lục Tĩnh đã đến."),)
    issues = (
        TranslationQaIssue(
            1,
            "NAME_INCONSISTENCY",
            TranslationQaSeverity.BLOCKER,
            "Tên nhân vật chưa được chứng minh đã sửa.",
            "Lục Tĩnh đã đến.",
        ),
    )

    assert (
        _drop_resolved_qa_issues(
            values,
            issues,
            previous_values=values,
        )
        == issues
    )


class DeterministicProvider:
    provider_id = "deterministic-translation"
    model_id = "fixture-v1"
    kind = ProviderKind.TRANSLATION

    def __init__(self, *, transient_first: bool = False, qa_blocker: bool = False) -> None:
        self.transient_first = transient_first
        self.qa_blocker = qa_blocker
        self.calls = []
        self.closed_count = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities({"deterministic": True})

    def health(self) -> ProviderHealth:
        return ProviderHealth(self.provider_id, HealthState.READY, "now", "ready")

    def close(self) -> None:
        self.closed_count += 1

    def generate(self, request) -> ProviderResponse:
        self.calls.append(request)
        if self.transient_first and request.purpose is TranslationPurpose.TRANSLATE:
            self.transient_first = False
            raise TransientProviderFailure("1095")
        if request.purpose is TranslationPurpose.FILM_BIBLE:
            text = json.dumps(
                {
                    "profile": "xianxia",
                    "synopsis": "Một câu chuyện tu luyện.",
                    "characters": [],
                    "terms": [],
                },
                ensure_ascii=False,
            )
        elif request.purpose is TranslationPurpose.QA:
            issues = (
                [
                    {
                        "id": request.expected_ids[0],
                        "severity": "blocker",
                        "code": "context",
                        "message": "Cần kiểm tra ngữ cảnh.",
                        "suggestion": "Sửa lại câu.",
                    }
                ]
                if self.qa_blocker
                else []
            )
            text = json.dumps({"issues": issues}, ensure_ascii=False)
        else:
            text = json.dumps(
                {
                    "translations": [
                        {"id": index, "text": f"Đây là bản dịch tự nhiên số {index}."}
                        for index in request.expected_ids
                    ],
                    "glossary_updates": [],
                },
                ensure_ascii=False,
            )
        return ProviderResponse(request.request_id, text, self.provider_id, self.model_id)


class SplitThenAuthProvider(DeterministicProvider):
    def generate(self, request) -> ProviderResponse:
        if request.purpose is not TranslationPurpose.TRANSLATE:
            return super().generate(request)
        self.calls.append(request)
        if len(request.expected_ids) > 1:
            rows = [
                {"id": index, "text": f"Bản dịch {index}."} for index in request.expected_ids[:-1]
            ]
            return ProviderResponse(
                request.request_id,
                json.dumps(
                    {"translations": rows, "glossary_updates": []},
                    ensure_ascii=False,
                ),
                self.provider_id,
                self.model_id,
            )
        if request.expected_ids == (3,):
            raise AuthenticationRequired("login required")
        return ProviderResponse(
            request.request_id,
            json.dumps(
                {
                    "translations": [
                        {
                            "id": request.expected_ids[0],
                            "text": f"Bản dịch {request.expected_ids[0]}.",
                        }
                    ],
                    "glossary_updates": [],
                },
                ensure_ascii=False,
            ),
            self.provider_id,
            self.model_id,
        )


class SplitOnPersistentTransientProvider(DeterministicProvider):
    def generate(self, request) -> ProviderResponse:
        if request.purpose is TranslationPurpose.TRANSLATE and len(request.expected_ids) > 1:
            self.calls.append(request)
            raise TransientProviderFailure("web response stalled")
        return super().generate(request)


class InvalidQaFormatProvider(DeterministicProvider):
    def generate(self, request) -> ProviderResponse:
        if request.purpose is TranslationPurpose.QA:
            self.calls.append(request)
            return ProviderResponse(
                request.request_id,
                '{"issues":[{"id":999,"severity":"blocker",'
                '"code":"context","message":"Sai","suggestion":"Sửa"}]}',
                self.provider_id,
                self.model_id,
            )
        return super().generate(request)


class IntegratedNameRepairProvider(DeterministicProvider):
    def generate(self, request) -> ProviderResponse:
        self.calls.append(request)
        if request.purpose is TranslationPurpose.FILM_BIBLE:
            payload = {
                "profile": "xianxia",
                "synopsis": "Một câu chuyện tu luyện.",
                "characters": ["Nhân vật Hai"],
                "terms": [{"source": "第2句话", "vi": "Nhân vật Hai", "kind": "name"}],
            }
        elif request.request_id.startswith("repair-"):
            payload = {
                "translations": [{"id": 2, "text": "Nhân vật Hai đã xuất hiện."}],
                "glossary_updates": [],
            }
        else:
            payload = {
                "translations": [
                    {
                        "id": index,
                        "text": (
                            "Tên sai đã xuất hiện."
                            if index == 2
                            else f"Đây là bản dịch tự nhiên số {index}."
                        ),
                    }
                    for index in request.expected_ids
                ],
                "glossary_updates": [],
            }
        return ProviderResponse(
            request.request_id,
            json.dumps(payload, ensure_ascii=False),
            self.provider_id,
            self.model_id,
        )


class SameReplyIntegratedQaProvider(DeterministicProvider):
    def generate(self, request) -> ProviderResponse:
        self.calls.append(request)
        if request.purpose is TranslationPurpose.FILM_BIBLE:
            payload = {
                "profile": "xianxia",
                "synopsis": "Một câu chuyện tu luyện.",
                "characters": [],
                "terms": [],
            }
        elif request.request_id.startswith("repair-"):
            payload = {
                "translations": [{"id": 2, "text": "Câu số hai đã được sửa đúng ngữ cảnh."}],
                "glossary_updates": [],
                "issues": [],
            }
        else:
            payload = {
                "translations": [
                    {"id": index, "text": f"Đây là bản dịch tự nhiên số {index}."}
                    for index in request.expected_ids
                ],
                "glossary_updates": [],
                "issues": [
                    {
                        "id": 2,
                        "severity": "blocker",
                        "code": "context",
                        "message": "Câu số hai còn sai ngữ cảnh.",
                        "suggestion": "",
                    }
                ]
                if 2 in request.expected_ids
                else [],
            }
        return ProviderResponse(
            request.request_id,
            json.dumps(payload, ensure_ascii=False),
            self.provider_id,
            self.model_id,
        )


class ChangedTargetRepairProvider(DeterministicProvider):
    def generate(self, request) -> ProviderResponse:
        if request.request_id.startswith("repair-"):
            self.calls.append(request)
            return ProviderResponse(
                request.request_id,
                json.dumps(
                    {
                        "translations": [
                            {"id": index, "text": f"Câu {index} đã được sửa theo ngữ cảnh."}
                            for index in request.expected_ids
                        ],
                        "glossary_updates": [],
                    },
                    ensure_ascii=False,
                ),
                self.provider_id,
                self.model_id,
            )
        return super().generate(request)


class CjkThenValidRepairProvider(SameReplyIntegratedQaProvider):
    def __init__(self) -> None:
        super().__init__()
        self.repair_attempts = 0

    def generate(self, request) -> ProviderResponse:
        if request.request_id.startswith("repair-"):
            self.calls.append(request)
            self.repair_attempts += 1
            text = (
                "Câu số hai vẫn còn chữ 漢."
                if self.repair_attempts == 1
                else "Câu số hai đã được sửa hoàn toàn."
            )
            return ProviderResponse(
                request.request_id,
                json.dumps(
                    {
                        "translations": [{"id": 2, "text": text}],
                        "glossary_updates": [],
                        "issues": [],
                    },
                    ensure_ascii=False,
                ),
                self.provider_id,
                self.model_id,
            )
        return super().generate(request)


class PersistentCjkRepairProvider(DeterministicProvider):
    def generate(self, request) -> ProviderResponse:
        self.calls.append(request)
        if request.purpose is TranslationPurpose.FILM_BIBLE:
            payload = {
                "profile": "xianxia",
                "synopsis": "Một câu chuyện tu luyện.",
                "characters": [],
                "terms": [],
            }
        elif request.request_id.startswith("repair-"):
            payload = {
                "translations": [
                    {"id": index, "text": f"Câu {index} vẫn còn chữ 漢."}
                    for index in request.expected_ids
                ],
                "glossary_updates": [],
                "issues": [],
            }
        else:
            payload = {
                "translations": [
                    {
                        "id": index,
                        "text": (
                            "Bản dịch vẫn còn chữ 漢."
                            if index == 2
                            else f"Đây là bản dịch tự nhiên số {index}."
                        ),
                    }
                    for index in request.expected_ids
                ],
                "glossary_updates": [],
                "issues": [],
            }
        return ProviderResponse(
            request.request_id,
            json.dumps(payload, ensure_ascii=False),
            self.provider_id,
            self.model_id,
        )


class PersistentOverlongRepairProvider(DeterministicProvider):
    def generate(self, request) -> ProviderResponse:
        self.calls.append(request)
        if request.purpose is TranslationPurpose.FILM_BIBLE:
            payload = {
                "profile": "xianxia",
                "synopsis": "Một câu chuyện tu luyện.",
                "characters": [],
                "terms": [],
            }
        else:
            long_text = " ".join(["Đây là một câu dịch rất dài để kiểm tra rút gọn."] * 8)
            payload = {
                "translations": [
                    {"id": index, "text": long_text} for index in request.expected_ids
                ],
                "glossary_updates": [],
                "issues": [],
            }
        return ProviderResponse(
            request.request_id,
            json.dumps(payload, ensure_ascii=False),
            self.provider_id,
            self.model_id,
        )


class PersistentAncientStyleProvider(DeterministicProvider):
    def generate(self, request) -> ProviderResponse:
        self.calls.append(request)
        if request.purpose is TranslationPurpose.FILM_BIBLE:
            payload = {
                "profile": "xianxia",
                "synopsis": "Một câu chuyện tu luyện.",
                "characters": ["Hồng Ly"],
                "terms": [],
            }
        else:
            payload = {
                "translations": [
                    {"id": index, "text": "Tỷ tỷ Hồng Ly, anh đến rồi."}
                    for index in request.expected_ids
                ],
                "glossary_updates": [],
                "issues": [],
            }
        return ProviderResponse(
            request.request_id,
            json.dumps(payload, ensure_ascii=False),
            self.provider_id,
            self.model_id,
        )


class MalformedIntegratedQaProvider(DeterministicProvider):
    def generate(self, request) -> ProviderResponse:
        if request.purpose is TranslationPurpose.FILM_BIBLE:
            return super().generate(request)
        self.calls.append(request)
        payload = {
            "translations": [
                {"id": index, "text": f"Đây là bản dịch tự nhiên số {index}."}
                for index in request.expected_ids
            ],
            "glossary_updates": [],
            "issues": [
                {
                    "severity": "unexpected-severity",
                    "code": "context",
                    "message": "QA sai schema dù phần dịch đã đủ.",
                    "suggestion": "",
                }
            ],
        }
        return ProviderResponse(
            request.request_id,
            json.dumps(payload, ensure_ascii=False),
            self.provider_id,
            self.model_id,
        )


def _config(output_path: Path, count: int) -> dict:
    return {
        "output_path": str(output_path),
        "title": "Phim thử nghiệm",
        "max_chunk_cues": 24,
        "max_chunk_source_chars": 1_800,
        "source_cues": [
            {
                "id": f"cue-{index}",
                "index": index,
                "start_us": (index - 1) * 700_000,
                "end_us": index * 700_000,
                "text": f"第{index}句话。",
                "language": "zh",
            }
            for index in range(1, count + 1)
        ],
    }


def _configure_worker(
    tmp_path: Path,
    monkeypatch,
    provider: DeterministicProvider,
    *,
    count: int,
    qa_mode: str = "integrated",
) -> tuple[TranslationWorker, list[tuple[str, dict]], Path]:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    output = tmp_path / "film_ZH_FINAL_codex_vi.srt"
    worker = TranslationWorker(
        "translation-job",
        provider_factory=lambda _config: provider,
    )
    events: list[tuple[str, dict]] = []
    worker.send = lambda kind, payload: events.append((kind, payload))  # type: ignore[method-assign]
    worker._interruptible_wait = lambda _seconds: None  # type: ignore[method-assign]
    config = _config(output, count)
    config["qa_mode"] = qa_mode
    worker._configure(
        {
            "job": {
                "kind": "translate",
                "provider_id": provider.provider_id,
                "model_id": provider.model_id,
                "provider_config": config,
            }
        }
    )
    return worker, events, output


def test_transient_1095_retries_exact_chunk_and_publishes_pass(tmp_path, monkeypatch) -> None:
    provider = DeterministicProvider(transient_first=True)
    worker, events, output = _configure_worker(tmp_path, monkeypatch, provider, count=3)

    worker._execute()

    translation_calls = [
        call for call in provider.calls if call.purpose is TranslationPurpose.TRANSLATE
    ]
    assert len(translation_calls) == 2
    assert translation_calls[0].prompt == translation_calls[1].prompt
    assert provider.closed_count >= 1
    assert output.is_file()
    assert events[-1][0] == "succeeded"
    assert events[-1][1]["status"] == "PASS"


@pytest.mark.parametrize(
    ("manual_source", "manual_role", "manual_track"),
    (
        ("manual_scene_text", "scene_text", "S2"),
        ("manual_dialogue", "dialogue", "S1"),
    ),
)
def test_manual_cue_is_preserved_without_provider_translation(
    tmp_path: Path,
    monkeypatch,
    manual_source: str,
    manual_role: str,
    manual_track: str,
) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    provider = DeterministicProvider()
    output = tmp_path / "film_ZH_FINAL_codex_vi.srt"
    config = _config(output, 3)
    config["source_cues"][1].update(
        {
            "text": "Bí cảnh",
            "language": "vi",
            "source": manual_source,
            "role": manual_role,
            "subtitle_track_id": manual_track,
            "text_font_size": 22,
        }
    )
    config["preserved_manual_translations"] = [
        {"index": 2, "text": "Bí cảnh thủ công"}
    ]
    worker = TranslationWorker(
        "manual-cue-job",
        provider_factory=lambda _config: provider,
    )
    events: list[tuple[str, dict]] = []
    worker.send = lambda kind, payload: events.append((kind, payload))  # type: ignore[method-assign]
    worker._interruptible_wait = lambda _seconds: None  # type: ignore[method-assign]
    worker._configure(
        {
            "job": {
                "kind": "translate",
                "provider_id": provider.provider_id,
                "model_id": provider.model_id,
                "provider_config": config,
            }
        }
    )

    worker._execute()

    translation_calls = [
        call for call in provider.calls if call.purpose is TranslationPurpose.TRANSLATE
    ]
    assert [call.expected_ids for call in translation_calls] == [(1,), (3,)]
    cues = parse_srt(
        output.read_text(encoding="utf-8-sig"),
        language="vi",
        id_factory=lambda: "parsed-cue",
    )
    assert cues[1].text == "Bí cảnh thủ công"
    assert "số 1" in cues[0].text
    assert "số 3" in cues[2].text
    assert events[-1][0] == "succeeded"


def test_persistent_transport_failure_never_splits_the_chunk(tmp_path, monkeypatch) -> None:
    provider = SplitOnPersistentTransientProvider()
    worker, events, output = _configure_worker(tmp_path, monkeypatch, provider, count=4)

    with pytest.raises(TransientProviderFailure):
        worker._execute()

    translation_calls = [
        call for call in provider.calls if call.purpose is TranslationPurpose.TRANSLATE
    ]
    assert [call.expected_ids for call in translation_calls] == [(1, 2, 3, 4)] * 2
    assert not output.exists()


def test_web_transient_activates_codex_fallback_for_remaining_job(tmp_path, monkeypatch) -> None:
    primary = SplitOnPersistentTransientProvider()
    fallback = DeterministicProvider()
    worker, events, output = _configure_worker(tmp_path, monkeypatch, primary, count=30)
    worker.fallback_provider = fallback

    worker._execute()

    primary_translation_calls = [
        call for call in primary.calls if call.purpose is TranslationPurpose.TRANSLATE
    ]
    fallback_translation_calls = [
        call for call in fallback.calls if call.purpose is TranslationPurpose.TRANSLATE
    ]
    assert len(primary_translation_calls) == 1
    assert [call.expected_ids for call in fallback_translation_calls] == [
        tuple(range(1, 25)),
        tuple(range(25, 31)),
    ]
    assert any(
        kind == "warning" and payload["code"] == "codex_fallback_activated"
        for kind, payload in events
    )
    assert output.is_file()
    assert events[-1][0] == "succeeded"


def test_guest_150_profile_never_constructs_codex_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    provider = DeterministicProvider()
    output = tmp_path / "film_ZH_FINAL_codex_vi.srt"
    config = _config(output, 320)
    config.update(
        {
            "max_chunk_cues": 150,
            "max_chunk_source_chars": 8_000,
            "codex_fallback_enabled": False,
        }
    )
    worker = TranslationWorker(
        "guest-150-no-codex",
        provider_factory=lambda _config: provider,
    )
    worker.send = lambda _kind, _payload: None  # type: ignore[method-assign]
    worker._configure(
        {
            "job": {
                "kind": "translate",
                "provider_id": "gemini-web",
                "model_id": provider.model_id,
                "provider_config": config,
            }
        }
    )

    assert worker.fallback_provider is None
    assert [len(chunk.cues) for chunk in worker.chunks] == [150, 150, 20]


def test_legacy_web_job_without_explicit_opt_in_never_constructs_codex_fallback(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    provider = DeterministicProvider()
    output = tmp_path / "film_ZH_FINAL_codex_vi.srt"
    config = _config(output, 2)
    worker = TranslationWorker(
        "legacy-web-no-codex-opt-in",
        provider_factory=lambda _config: provider,
    )
    worker.send = lambda _kind, _payload: None  # type: ignore[method-assign]
    worker._configure(
        {
            "job": {
                "kind": "translate",
                "provider_id": "gemini-web",
                "model_id": provider.model_id,
                "provider_config": config,
            }
        }
    )

    assert worker.fallback_provider is None


def test_successful_translation_is_checkpointed_before_live_delta(tmp_path, monkeypatch) -> None:
    provider = DeterministicProvider()
    worker, events, _output = _configure_worker(tmp_path, monkeypatch, provider, count=3)

    worker._execute()

    delta_index = next(
        index for index, (kind, _payload) in enumerate(events) if kind == "translation_delta"
    )
    checkpoint_index = max(
        index
        for index, (kind, payload) in enumerate(events[:delta_index])
        if kind == "checkpoint" and payload["checkpoint_key"] == "latest"
    )
    delta = events[delta_index][1]
    assert checkpoint_index < delta_index
    assert delta["completed_cues"] == 3
    assert [value["index"] for value in delta["values"]] == [1, 2, 3]
    assert all(value["text"].strip() for value in delta["values"])


def test_persistent_qa_blocker_creates_review_not_false_final(tmp_path, monkeypatch) -> None:
    provider = DeterministicProvider(qa_blocker=True)
    worker, events, output = _configure_worker(
        tmp_path, monkeypatch, provider, count=2, qa_mode="independent"
    )

    worker._execute()

    review = output.with_name(output.stem + "_REVIEW.srt")
    assert not output.exists()
    assert review.is_file()
    assert events[-1] == (
        "failed",
        {
            "code": "translation_review_required",
            "status": "REVIEW",
            "output_path": str(review),
            "qa_path": str(output.with_name(output.stem + "_QA.json")),
        },
    )


def test_integrated_qa_does_not_send_a_second_gemini_qa_request(tmp_path, monkeypatch) -> None:
    provider = DeterministicProvider()
    worker, events, output = _configure_worker(tmp_path, monkeypatch, provider, count=2)

    worker._execute()

    purposes = [call.purpose for call in provider.calls]
    assert purposes == [TranslationPurpose.FILM_BIBLE, TranslationPurpose.TRANSLATE]
    assert any(
        kind == "progress" and payload.get("phase") == "integrated_qa" for kind, payload in events
    )
    progress = [payload for kind, payload in events if kind == "progress"]
    assert progress
    assert all(payload["total"] == 2 for payload in progress)
    assert all(payload["current"] <= payload["total"] for payload in progress)
    integrated_gate = next(payload for payload in progress if payload["phase"] == "integrated_qa")
    assert integrated_gate["translated_cues"] == 2
    assert integrated_gate["current"] == 0
    assert progress[-1]["current"] == 2
    assert output.is_file()


def test_malformed_integrated_qa_keeps_translations_and_continues_later_chunks(
    tmp_path, monkeypatch
) -> None:
    provider = MalformedIntegratedQaProvider()
    worker, events, output = _configure_worker(tmp_path, monkeypatch, provider, count=30)

    worker._execute()

    translation_calls = [
        call for call in provider.calls if call.purpose is TranslationPurpose.TRANSLATE
    ]
    assert len(translation_calls) == 2
    assert worker.ledger is not None
    assert len(worker.ledger.translations) == 30
    assert all(
        issue.code == "malformed_integrated_qa"
        for chunk in worker.ledger.chunks
        for issue in chunk.qa_issues
    )
    assert not any(call.request_id.startswith("repair-") for call in provider.calls)
    assert not output.exists()
    assert events[-1][0] == "failed"
    assert events[-1][1]["code"] == "translation_review_required"


def test_resume_eta_uses_only_new_session_progress(tmp_path, monkeypatch) -> None:
    provider = DeterministicProvider()
    worker, events, _output = _configure_worker(
        tmp_path, monkeypatch, provider, count=3, qa_mode="independent"
    )
    assert worker.ledger is not None
    worker.ledger = replace(worker.ledger, translations={1: "Câu một."})
    clock = [100.0]
    monkeypatch.setattr(translation_worker_module.time, "monotonic", lambda: clock[0])
    worker._job_started = clock[0]
    worker._progress_baseline_completed = worker._completed_progress_units()

    clock[0] += 10.0
    worker._send_progress("translation")
    assert events[-1][1]["eta_ms"] is None

    worker.ledger = replace(
        worker.ledger,
        translations={1: "Câu một.", 2: "Câu hai."},
    )
    clock[0] += 10.0
    worker._send_progress("translation")

    assert events[-1][1]["current"] == 2
    assert events[-1][1]["total"] == 6
    assert events[-1][1]["eta_ms"] == 80_000


def test_legacy_job_without_qa_mode_keeps_independent_qa_semantics(tmp_path, monkeypatch) -> None:
    provider = DeterministicProvider()
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    worker = TranslationWorker(
        "translation-legacy-job",
        provider_factory=lambda _config: provider,
    )
    output = tmp_path / "legacy_codex_vi.srt"

    worker._configure(
        {
            "job": {
                "kind": "translate",
                "provider_id": provider.provider_id,
                "model_id": provider.model_id,
                "provider_config": _config(output, 2),
            }
        }
    )

    assert worker.qa_mode == "independent"


def test_integrated_qa_repairs_only_the_failing_cue(tmp_path, monkeypatch) -> None:
    provider = IntegratedNameRepairProvider()
    worker, events, output = _configure_worker(tmp_path, monkeypatch, provider, count=3)

    worker._execute()

    repair_calls = [call for call in provider.calls if call.request_id.startswith("repair-")]
    assert len(repair_calls) == 1
    assert repair_calls[0].expected_ids == (2,)
    assert not any(call.purpose is TranslationPurpose.QA for call in provider.calls)
    assert output.is_file()
    assert events[-1][0] == "succeeded"


def test_integrated_qa_uses_same_translation_reply_then_repairs_only_blocker(
    tmp_path, monkeypatch
) -> None:
    provider = SameReplyIntegratedQaProvider()
    worker, events, output = _configure_worker(tmp_path, monkeypatch, provider, count=3)

    worker._execute()

    repair_calls = [call for call in provider.calls if call.request_id.startswith("repair-")]
    assert len(repair_calls) == 1
    assert repair_calls[0].expected_ids == (2,)
    assert not any(call.purpose is TranslationPurpose.QA for call in provider.calls)
    assert output.is_file()
    assert events[-1][0] == "succeeded"


def test_targeted_repair_retries_immediately_when_response_still_contains_cjk(
    tmp_path, monkeypatch
) -> None:
    provider = CjkThenValidRepairProvider()
    worker, events, output = _configure_worker(tmp_path, monkeypatch, provider, count=3)

    worker._execute()

    repair_calls = [call for call in provider.calls if call.request_id.startswith("repair-")]
    assert len(repair_calls) == 2
    assert all(call.expected_ids == (2,) for call in repair_calls)
    assert not any(
        "漢" in str(payload)
        for kind, payload in events
        if kind in {"checkpoint", "translation_delta"}
    )
    assert output.is_file()
    assert "漢" not in output.read_text(encoding="utf-8-sig")
    assert events[-1][0] == "succeeded"


def test_persistent_cjk_repair_is_deferred_without_stopping_remaining_translation(
    tmp_path, monkeypatch
) -> None:
    provider = PersistentCjkRepairProvider()
    worker, events, output = _configure_worker(tmp_path, monkeypatch, provider, count=30)

    worker._execute()

    repair_calls = [call for call in provider.calls if call.request_id.startswith("repair-")]
    assert len(repair_calls) == 4
    assert all(call.expected_ids == (2,) for call in repair_calls)
    assert any(
        kind == "warning" and payload["code"] == "targeted_repair_deferred"
        for kind, payload in events
    )
    # The next semantic chunk was still translated and checkpointed.
    assert any(
        call.purpose is TranslationPurpose.TRANSLATE and 30 in call.expected_ids
        for call in provider.calls
    )
    assert not output.exists()
    assert output.with_name(output.stem + "_REVIEW.srt").is_file()
    assert events[-1][0] == "failed"
    assert events[-1][1]["code"] == "translation_review_required"


def test_multi_policy_keeps_cjk_blocker_and_publishes_after_one_overlong_pass(
    tmp_path, monkeypatch
) -> None:
    provider = PersistentOverlongRepairProvider()
    worker, events, output = _configure_worker(tmp_path, monkeypatch, provider, count=2)
    assert worker.ledger is not None
    worker.config.update(
        {
            "qa_policy": "multi_auto",
            "cjk_is_blocker": True,
            "auto_compact_overlong_once": True,
            "max_overlong_compaction_passes": 1,
            "qa_non_blocking": True,
        }
    )
    worker.qa_policy = "multi_auto"
    worker.cjk_is_blocker = True
    worker.auto_compact_overlong_once = True
    worker.max_overlong_compaction_passes = 1

    worker._execute()

    repair_calls = [call for call in provider.calls if call.request_id.startswith("repair-")]
    assert len(repair_calls) == 1
    assert output.is_file()
    assert events[-1][0] == "succeeded"
    assert events[-1][1]["status"] == "PASS"


def test_multi_policy_reports_cjk_as_the_only_semantic_blocker(tmp_path, monkeypatch) -> None:
    provider = PersistentCjkRepairProvider()
    worker, events, output = _configure_worker(tmp_path, monkeypatch, provider, count=2)
    worker.config.update(
        {
            "qa_policy": "multi_auto",
            "cjk_is_blocker": True,
            "auto_compact_overlong_once": True,
            "max_overlong_compaction_passes": 1,
            "qa_non_blocking": True,
        }
    )
    worker.qa_policy = "multi_auto"
    worker.cjk_is_blocker = True
    worker.auto_compact_overlong_once = True
    worker.max_overlong_compaction_passes = 1

    worker._execute()

    assert not output.exists()
    assert events[-1][0] == "failed"
    assert events[-1][1]["code"] == "translation_review_required"
    assert events[-1][1]["cjk_cue_ids"] == [2]
    assert events[-1][1]["cjk_blocker_count"] == 1
    assert events[-1][1]["warnings_only"] is False


def test_ancient_style_repair_runs_once_then_becomes_warning(tmp_path, monkeypatch) -> None:
    provider = PersistentAncientStyleProvider()
    monkeypatch.setenv("DUCAI_ARTIFACT_STAGING_ROOT", str(tmp_path / "staging"))
    output = tmp_path / "film_ZH_FINAL_codex_vi.srt"
    config = _config(output, 2)
    config.update(
        {
            "qa_mode": "integrated",
            "qa_non_blocking": True,
            "translation_style_preset": "xianxia",
            "translation_custom_rules": "Giữ danh xưng theo vai vế.",
        }
    )
    worker = TranslationWorker(
        "translation-style-job",
        provider_factory=lambda _config: provider,
    )
    events: list[tuple[str, dict]] = []
    worker.send = lambda kind, payload: events.append((kind, payload))  # type: ignore[method-assign]
    worker._interruptible_wait = lambda _seconds: None  # type: ignore[method-assign]
    worker._configure(
        {
            "job": {
                "kind": "translate",
                "provider_id": provider.provider_id,
                "model_id": provider.model_id,
                "provider_config": config,
            }
        }
    )

    worker._execute()

    repairs = [call for call in provider.calls if call.request_id.startswith("repair-")]
    assert len(repairs) == 1
    assert "Giữ danh xưng theo vai vế" in repairs[0].prompt
    assert output.is_file()
    assert events[-1][0] == "succeeded"
    assert events[-1][1]["warning_count"] >= 1


def test_4333_cues_publish_with_identical_timeline_and_no_empty_cue(tmp_path, monkeypatch) -> None:
    provider = DeterministicProvider()
    worker, events, output = _configure_worker(tmp_path, monkeypatch, provider, count=4_333)

    worker._execute()

    result = parse_srt(
        output.read_text(encoding="utf-8-sig"),
        language="vi",
        id_factory=lambda: "result",
    )
    assert len(result) == 4_333
    assert [cue.index for cue in result] == list(range(1, 4_334))
    assert all(cue.text.strip() for cue in result)
    assert result[0].range.start_us == 0
    assert result[-1].range.end_us == 4_333 * 700_000
    assert events[-1][1]["cue_count"] == 4_333


def test_completed_chunk_cache_resumes_without_resending_any_provider_call(
    tmp_path, monkeypatch
) -> None:
    first_provider = DeterministicProvider()
    first, events, output = _configure_worker(tmp_path, monkeypatch, first_provider, count=30)
    first._execute()
    latest_checkpoint = next(
        payload
        for kind, payload in reversed(events)
        if kind == "checkpoint" and payload["checkpoint_key"] == "latest"
    )
    output.unlink()
    second_provider = DeterministicProvider()
    second = TranslationWorker(
        "translation-job-resume",
        provider_factory=lambda _config: second_provider,
    )
    resumed_events: list[tuple[str, dict]] = []
    second.send = (  # type: ignore[method-assign]
        lambda kind, payload: resumed_events.append((kind, payload))
    )
    second._configure(
        {
            "job": {
                "kind": "translate",
                "provider_id": second_provider.provider_id,
                "model_id": second_provider.model_id,
                "provider_config": _config(output, 30),
            },
            "checkpoint": {"artifact": {"uri": latest_checkpoint["artifact_path"]}},
        }
    )

    second._execute()

    assert second_provider.calls == []
    assert output.is_file()
    assert resumed_events[-1][0] == "succeeded"


def test_multi_policy_resume_keeps_unreviewed_and_uncached_chunks_pending(
    tmp_path, monkeypatch
) -> None:
    first_provider = DeterministicProvider()
    first, _events, output = _configure_worker(
        tmp_path,
        monkeypatch,
        first_provider,
        count=50,
    )
    assert first.ledger is not None
    assert len(first.ledger.chunks) == 3
    completed_chunk = first.ledger.chunks[0]
    unreviewed_chunk = first.ledger.chunks[1]
    uncached_chunk = first.ledger.chunks[2]
    cached = {
        cue_id: f"Đây là bản dịch đã lưu số {cue_id}."
        for cue_id in (*completed_chunk.cue_ids, *unreviewed_chunk.cue_ids)
    }
    restored_ledger = replace(
        first.ledger,
        chunks=(
            replace(completed_chunk, state=ChunkState.QA_PASS),
            replace(unreviewed_chunk, state=ChunkState.PENDING),
            replace(uncached_chunk, state=ChunkState.PENDING),
        ),
        translations=cached,
    )
    checkpoint_path = tmp_path / "partial-ledger.json"
    checkpoint_path.write_text(
        json.dumps(ledger_to_dict(restored_ledger), ensure_ascii=False),
        encoding="utf-8",
    )

    resumed_provider = DeterministicProvider()
    resumed = TranslationWorker(
        "translation-job-partial-resume",
        provider_factory=lambda _config: resumed_provider,
    )
    resumed_events: list[tuple[str, dict]] = []
    resumed.send = (  # type: ignore[method-assign]
        lambda kind, payload: resumed_events.append((kind, payload))
    )
    resumed._interruptible_wait = lambda _seconds: None  # type: ignore[method-assign]
    resumed._configure(
        {
            "job": {
                "kind": "translate",
                "provider_id": resumed_provider.provider_id,
                "model_id": resumed_provider.model_id,
                "provider_config": {
                    **_config(output, 50),
                    "qa_mode": "integrated",
                    "qa_non_blocking": True,
                    "qa_policy": "multi_auto",
                    "cjk_is_blocker": True,
                    "auto_compact_overlong_once": True,
                    "max_overlong_compaction_passes": 1,
                    "headed": False,
                },
            },
            "checkpoint": {"artifact": {"uri": str(checkpoint_path)}},
        }
    )

    assert resumed.ledger is not None
    assert resumed.ledger.chunks[0].state is ChunkState.QA_PASS
    assert resumed.ledger.chunks[1].state is ChunkState.PENDING
    assert resumed.ledger.chunks[2].state is ChunkState.PENDING
    resumed._execute()

    translated_ids = {
        cue_id
        for call in resumed_provider.calls
        if call.purpose is TranslationPurpose.TRANSLATE
        for cue_id in call.expected_ids
    }
    assert translated_ids == set(uncached_chunk.cue_ids)
    assert resumed_events[-1][0] == "succeeded"


def test_resume_rechecks_remote_qa_format_review_without_retranslating_cache(
    tmp_path, monkeypatch
) -> None:
    first_provider = InvalidQaFormatProvider()
    first, events, output = _configure_worker(
        tmp_path, monkeypatch, first_provider, count=2, qa_mode="independent"
    )
    first._execute()
    assert events[-1][0] == "failed"
    latest_checkpoint = next(
        payload
        for kind, payload in reversed(events)
        if kind == "checkpoint" and payload["checkpoint_key"] == "latest"
    )

    second_provider = DeterministicProvider()
    second = TranslationWorker(
        "translation-job-review-resume",
        provider_factory=lambda _config: second_provider,
    )
    resumed_events: list[tuple[str, dict]] = []
    second.send = (  # type: ignore[method-assign]
        lambda kind, payload: resumed_events.append((kind, payload))
    )
    second._configure(
        {
            "job": {
                "kind": "translate",
                "provider_id": second_provider.provider_id,
                "model_id": second_provider.model_id,
                "provider_config": {
                    **_config(output, 2),
                    "qa_mode": "independent",
                },
            },
            "checkpoint": {"artifact": {"uri": latest_checkpoint["artifact_path"]}},
        }
    )

    second._execute()

    purposes = [call.purpose for call in second_provider.calls]
    assert TranslationPurpose.TRANSLATE not in purposes
    assert purposes == [TranslationPurpose.QA]
    assert output.is_file()
    assert resumed_events[-1][0] == "succeeded"


def test_integrated_resume_repairs_preserved_review_blocker_without_remote_qa(
    tmp_path, monkeypatch
) -> None:
    first_provider = DeterministicProvider(qa_blocker=True)
    first, events, output = _configure_worker(
        tmp_path, monkeypatch, first_provider, count=3, qa_mode="independent"
    )
    first._execute()
    assert events[-1][0] == "failed"
    latest_checkpoint = next(
        payload
        for kind, payload in reversed(events)
        if kind == "checkpoint" and payload["checkpoint_key"] == "latest"
    )

    second_provider = ChangedTargetRepairProvider()
    second = TranslationWorker(
        "translation-job-integrated-review-resume",
        provider_factory=lambda _config: second_provider,
    )
    resumed_events: list[tuple[str, dict]] = []
    second.send = (  # type: ignore[method-assign]
        lambda kind, payload: resumed_events.append((kind, payload))
    )
    second._configure(
        {
            "job": {
                "kind": "translate",
                "provider_id": second_provider.provider_id,
                "model_id": second_provider.model_id,
                "provider_config": {
                    **_config(output, 3),
                    "qa_mode": "integrated",
                },
            },
            "checkpoint": {"artifact": {"uri": latest_checkpoint["artifact_path"]}},
        }
    )

    second._execute()

    assert not any(call.purpose is TranslationPurpose.QA for call in second_provider.calls)
    repair_calls = [call for call in second_provider.calls if call.request_id.startswith("repair-")]
    assert len(repair_calls) == 1
    assert repair_calls[0].expected_ids == (1,)
    assert resumed_events[-1][0] == "succeeded"


def test_integrated_resume_keeps_review_blocker_when_targeted_repair_is_unchanged(
    tmp_path, monkeypatch
) -> None:
    first_provider = DeterministicProvider(qa_blocker=True)
    first, events, output = _configure_worker(
        tmp_path, monkeypatch, first_provider, count=2, qa_mode="independent"
    )
    first._execute()
    latest_checkpoint = next(
        payload
        for kind, payload in reversed(events)
        if kind == "checkpoint" and payload["checkpoint_key"] == "latest"
    )
    second_provider = DeterministicProvider()
    second = TranslationWorker(
        "translation-job-unchanged-review-resume",
        provider_factory=lambda _config: second_provider,
    )
    resumed_events: list[tuple[str, dict]] = []
    second.send = (  # type: ignore[method-assign]
        lambda kind, payload: resumed_events.append((kind, payload))
    )
    second._configure(
        {
            "job": {
                "kind": "translate",
                "provider_id": second_provider.provider_id,
                "model_id": second_provider.model_id,
                "provider_config": {
                    **_config(output, 2),
                    "qa_mode": "integrated",
                },
            },
            "checkpoint": {"artifact": {"uri": latest_checkpoint["artifact_path"]}},
        }
    )

    second._execute()

    assert resumed_events[-1][0] == "failed"
    assert resumed_events[-1][1]["code"] == "translation_review_required"


def test_pause_and_resume_are_explicit_and_do_not_lose_the_job(tmp_path, monkeypatch) -> None:
    provider = DeterministicProvider()
    worker, events, output = _configure_worker(tmp_path, monkeypatch, provider, count=2)

    def capture(kind: str, payload: dict) -> WorkerMessage:
        events.append((kind, payload))
        return WorkerMessage(
            PROTOCOL_VERSION,
            worker.job_id,
            f"event-{len(events)}",
            kind,
            payload,
        )

    worker.send = capture  # type: ignore[method-assign]
    worker.commands.put(
        WorkerMessage(
            PROTOCOL_VERSION,
            worker.job_id,
            "pause-event",
            "control",
            {"action": "pause", "command_id": "pause-command"},
        )
    )
    worker.commands.put(
        WorkerMessage(
            PROTOCOL_VERSION,
            worker.job_id,
            "resume-event",
            "control",
            {"action": "resume", "command_id": "resume-command"},
        )
    )

    worker._execute()

    kinds = [kind for kind, _payload in events]
    assert "paused" in kinds
    assert "resumed" in kinds
    assert kinds.index("checkpoint") < kinds.index("paused")
    pause_checkpoint = next(
        payload
        for kind, payload in events
        if kind == "checkpoint" and payload["checkpoint_key"] == "latest"
    )
    assert Path(pause_checkpoint["artifact_path"]).is_file()
    assert kinds[-1] == "succeeded"
    assert output.is_file()


def test_cancel_checkpoints_cached_translations_before_stopping(tmp_path, monkeypatch) -> None:
    provider = DeterministicProvider()
    worker, events, _output = _configure_worker(
        tmp_path,
        monkeypatch,
        provider,
        count=2,
    )
    assert worker.ledger is not None
    worker.ledger = replace(worker.ledger, translations={1: "Bản dịch đã lưu."})
    worker.commands.put(
        WorkerMessage(
            PROTOCOL_VERSION,
            worker.job_id,
            "cancel-event",
            "control",
            {"action": "cancel", "command_id": "cancel-command"},
        )
    )

    with pytest.raises(translation_worker_module.WorkerStopped):
        worker._control_point()

    checkpoint = next(
        payload
        for kind, payload in events
        if kind == "checkpoint" and payload["checkpoint_key"] == "latest"
    )
    cached = json.loads(Path(checkpoint["artifact_path"]).read_text(encoding="utf-8"))
    assert cached["translations"] == {"1": "Bản dịch đã lưu."}
    assert events[-1] == (
        "stopped",
        {"status": "cancel", "command_id": "cancel-command"},
    )


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_api_control_is_handled_while_http_response_is_pending(tmp_path, monkeypatch, action):
    entered, release, paused = Event(), Event(), Event()
    observations = []

    class BlockingTransport:
        def post(self, *_args, **_kwargs):
            entered.set()
            assert release.wait(3), "Test transport was not released"
            return HttpResponse(200, json.dumps({"choices": [{"message": {
                "content": '{"translations":[]}'}, "finish_reason": "stop"}]}).encode(), {})

    provider = OpenAICompatibleProvider(
        provider_id="openai-compatible", model_id="test-model", base_url="http://localhost/v1",
        credential_references=["test-key"], credential_resolver=lambda _: "test-secret",
        transport=BlockingTransport(),
    )
    worker, events, _ = _configure_worker(tmp_path, monkeypatch, provider, count=2)
    worker.ledger = replace(worker.ledger, translations={1: "Bản dịch đã lưu."})

    def capture(kind, payload):
        events.append((kind, payload))
        if kind == "paused":
            paused.set()
        return WorkerMessage(PROTOCOL_VERSION, worker.job_id, f"event-{len(events)}",
                             kind, payload)

    worker.send = capture

    def command(name):
        worker.commands.put(WorkerMessage(PROTOCOL_VERSION, worker.job_id, name,
                                         "control", {"action": name, "command_id": name}))

    def control():
        assert entered.wait(1)
        command(action)
        if action == "pause":
            observations.append(paused.wait(1))
            observations.append(not release.is_set())
            release.set()
            # A finished HTTP response must not be published during pause.
            observations.append(not any(k == "provider_request" for k, _ in events))
            command("resume")
        else:
            # Keep HTTP blocked until the worker has processed cancel.
            if not paused.wait(1):
                release.set()

    controller = Thread(target=control)
    controller.start()
    try:
        request = TranslationRequest("pending", TranslationPurpose.TRANSLATE, "Return JSON")
        if action == "cancel":
            with pytest.raises(translation_worker_module.WorkerStopped):
                worker._generate(request)
            assert not release.is_set()
            assert not any(k == "provider_request" for k, _ in events)
        else:
            worker._generate(request)
            assert observations == [True, True, True]
            assert [k for k, _ in events].count("provider_request") == 1
            assert any(k == "resumed" for k, _ in events)
        checkpoint = next(p for k, p in events if k == "checkpoint")
        assert json.loads(Path(checkpoint["artifact_path"]).read_text("utf-8"))[
            "translations"] == {"1": "Bản dịch đã lưu."}
    finally:
        paused.set()
        release.set()
        controller.join(3)
    assert not controller.is_alive()


def test_complete_ordered_partial_response_is_salvaged_without_retranslation(
    tmp_path, monkeypatch
) -> None:
    first_provider = SplitThenAuthProvider()
    first, events, output = _configure_worker(tmp_path, monkeypatch, first_provider, count=4)

    first._execute()

    assert first.ledger is not None
    assert output.is_file()
    assert first.ledger is not None
    assert dict(first.ledger.translations) == {index: f"Bản dịch {index}." for index in range(1, 5)}
    translated_ids = [
        call.expected_ids
        for call in first_provider.calls
        if call.purpose is TranslationPurpose.TRANSLATE
    ]
    assert translated_ids[:2] == [(1, 2, 3, 4), (4,)]
