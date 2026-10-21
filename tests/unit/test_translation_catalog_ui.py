from copy import deepcopy

import pytest
from PySide6.QtCore import QCoreApplication

from ducai_studio.ui.viewmodels import application_view_model as module


class Settings:
    def __init__(self, *_):
        self.values = {}

    def value(self, key, default=None, *, type=None):
        value = self.values.get(key, default)
        return type(value) if type else value

    def setValue(self, key, value):
        self.values[key] = value


@pytest.fixture
def editor(monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr(module, "QSettings", Settings)
    model = module.ApplicationViewModel()
    model._set_backend_ready(True)
    requests = []
    monkeypatch.setattr(model.runtime, "request", lambda action, params=None, context=None:
                        requests.append((action, params, deepcopy(context))))
    return model, requests


def test_labeled_credential_keeps_secret_out_of_ui_settings(editor):
    model, requests = editor
    model.saveTranslationApiCredential("qwen-api", "Key chính", "test-secret")
    assert requests[-1] == ("store_credentials", {
        "credentials": [{"reference": "Key chính", "secret": "test-secret"}],
    }, {"credential_provider_id": "qwen-api", "credential_references": ["Key chính"],
        "start_translation": False})
    model._action_succeeded("store_credentials", {"references": ["Key chính"]}, requests[-1][2])
    assert "test-secret" not in str(model.settings.values)
    assert "Key chính" in model.qwenApiCredentialReferences


def test_catalog_ignores_stale_results_and_never_changes_manual_model(editor):
    model, requests = editor
    model.settings.setValue("ui/translationModel", "manual-model")
    model.refreshTranslationModels("gemini-api", "key", "")
    stale_context = requests[-1][2]
    assert model.translationModelCatalogBusy
    model.clearTranslationModelCatalog()
    model.refreshTranslationModels("qwen-api", "key", "")
    current_context = requests[-1][2]
    model._action_succeeded("list_translation_models", {"models": ["stale"]}, stale_context)
    assert model.translationAvailableModels == [] and model.translationModelCatalogBusy
    model._action_succeeded("list_translation_models", {"models": ["qwen-model"]}, current_context)
    assert model.translationAvailableModels == ["qwen-model"]
    assert not model.translationModelCatalogBusy
    assert model.settings.values["ui/translationModel"] == "manual-model"


def test_catalog_failure_is_sanitized_and_can_retry(editor):
    model, requests = editor
    model.refreshTranslationModels("gemini-api", "key", "")
    model._action_failed("list_translation_models", "error with secret", requests[-1][2])
    assert not model.translationModelCatalogBusy
    assert "secret" not in model.translationModelCatalogStatus
    model.refreshTranslationModels("gemini-api", "key", "")
    assert model.translationModelCatalogBusy


def test_scene_ocr_switch_keeps_existing_cues_and_freezes_single_and_multi_settings(
    editor, monkeypatch,
):
    model, requests = editor
    model._selected_voice_id = "voice-ready"
    monkeypatch.setattr(model.voice_catalog, "find", lambda _voice_id: {
        "id": "voice-ready", "provider_id": "piper", "installed": True,
    })
    project = {"id": "p", "name": "P", "revision": 1, "source_cues": [{
        "id": "scene", "index": 1, "text": "太南", "language": "zh", "role": "scene_text",
        "range": {"start_us": 0, "end_us": 1_000_000},
    }]}
    model.project_state.replace(project)
    assert model.ocrSceneTextEnabled
    model.setOcrSceneTextEnabled(False)
    assert model.project_state.subtitle_cues.find("scene")
    assert model.settings.values["ocr/sceneTextEnabled"] is False
    assert model._multi_settings_snapshot()["ocr"]["scene_text_enabled"] is False
    model.startOcr(1.25)
    assert requests[-1][0] == "create_ocr_job"
    assert requests[-1][1]["scene_text_enabled"] is False


def prepare_paused_translation(model, *, provider="gemini-api", base_url=""):
    model.project_state.replace({"id": "p", "name": "P", "revision": 1, "source_cues": [{
        "id": "cue", "index": 1, "text": "你好", "language": "zh",
        "range": {"start_us": 0, "end_us": 1_000_000},
    }]})
    model._translation_job_id = "old"
    model._translation_provider_id = provider
    model._translation_model_id = "text-model"
    model._translation_base_url = base_url
    model._translation_job_credential_references = ["key-a"]
    model._translation_job_state = "paused"
    model._translation_busy = True
    model.settings.setValue(module._credential_setting_key(provider), ["key-a", "key-b"])
    model.setUiStringValue("translation/selectedCredential/" + provider, "key-a")


@pytest.mark.parametrize("change", ["key", "endpoint", "model"])
def test_paused_selection_change_waits_for_terminal_and_starts_exactly_once(editor, change):
    model, requests = editor
    provider = "openai-compatible" if change == "endpoint" else "gemini-api"
    prepare_paused_translation(model, provider=provider, base_url="https://old.example/v1")
    base_url = "https://new.example/v1" if change == "endpoint" else "https://old.example/v1"
    chosen_model = "new-text-model" if change == "model" else "text-model"
    if change == "key":
        model.setUiStringValue("translation/selectedCredential/" + provider, "key-b")
    model.toggleTranslation(provider, chosen_model, base_url=base_url)
    assert requests[0][0] == "cancel_job"
    model._complete_translation_switch([{"id": "old", "state": "cancel_requested"}])
    assert len(requests) == 1
    model._action_succeeded("cancel_job", {"id": "old", "state": "cancelled"}, requests[0][2])
    model._complete_translation_switch([{"id": "old", "state": "cancelled"}])
    created = [params for action, params, _ in requests if action == "create_translation_job"]
    assert len(created) == 1
    assert created[0]["provider_id"] == provider
    assert created[0]["model_id"] == chosen_model
    assert created[0]["provider_options"]["credential_references"] == [
        "key-b" if change == "key" else "key-a"]
    if change == "endpoint":
        assert created[0]["provider_options"]["base_url"] == base_url
    assert not any(action == "resume_job" for action, _, _ in requests)
    assert model._translation_switch_pending is None


def test_unchanged_paused_selection_resumes_existing_worker(editor):
    model, requests = editor
    prepare_paused_translation(model)
    model.toggleTranslation("gemini-api", "text-model")
    assert [action for action, _, _ in requests] == ["resume_job"]
    assert requests[0][1] == {"job_id": "old"}


def test_pending_switch_never_starts_in_a_different_project(editor):
    model, requests = editor
    prepare_paused_translation(model)
    model.toggleTranslation("gemini-web", "selected-on-web")
    model.project_state.replace({"id": "other", "name": "Other", "revision": 1})
    model._complete_translation_switch([{"id": "old", "state": "cancelled"}])
    assert model._translation_switch_pending is None
    assert not any(action == "create_translation_job" for action, _, _ in requests)


def test_failed_switch_control_preserves_cache_and_refreshes_durable_state(editor):
    model, requests = editor
    prepare_paused_translation(model)
    model._selected_translation_cache_id = "old"
    model.toggleTranslation("gemini-web", "selected-on-web")
    model._action_failed("cancel_job", {"message": "disconnected"}, requests[0][2])
    assert model._translation_switch_pending is None
    assert model._selected_translation_cache_id == "old"
    assert requests[-1][0] == "list_jobs"
    assert not any(action == "create_translation_job" for action, _, _ in requests)
