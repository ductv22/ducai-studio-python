from __future__ import annotations

import copy

import pytest
from dialogue_casting.analysis import Settings
from dialogue_casting.ecapa import verify_model
from dialogue_casting.refined_demo import carry_exact_reviews, verify_predictions
from dialogue_casting.srt_demo import classify_srt_cue, validate_review


def cue():
    return {
        "id": "a",
        "film_id": "film",
        "clip_sha256": "clip",
        "start_us": 0,
        "end_us": 2_000_000,
        "target_start_us": 0,
        "target_end_us": 2_000_000,
        "machine": {"label": "unknown"},
        "excerpt_id": "excerpt",
    }


def test_exact_human_state_can_survive_a_model_change_without_rewriting_history():
    before = cue()
    old = {"demo_id": "old", "experiment_id": "old-exp", "datasets": [{"cues": [before]}]}
    new = {**before, "machine": {"label": "male"}}
    reviews = {"a": {"override": "female", "reviewed": True, "notes": "giữ nguyên"}}
    baseline = copy.deepcopy((old, reviews))
    carried, origins = carry_exact_reviews(old, [new], reviews)
    assert carried == reviews
    assert origins["a"] == {
        "demo_id": "old",
        "experiment_id": "old-exp",
        "machine_label": "unknown",
        "clip_sha256": "clip",
    }
    current = {"demo_id": "new", "experiment_id": "new-exp", "datasets": [{"cues": [new]}]}
    exported = {
        "schema": "ducai-dialogue-casting-demo-review-v1",
        "label_source": "demo-assisted-review",
        "demo_id": "new",
        "experiment_id": "new-exp",
        "reviews": [
            {"cue_id": "a", "clip_sha256": "clip", "machine_label": "male", **carried["a"]}
        ],
    }
    assert validate_review(exported, current) == reviews
    with pytest.raises(ValueError, match="belong"):
        validate_review(exported, old)
    assert (old, reviews) == baseline


@pytest.mark.parametrize(
    "field", ["clip_sha256", "film_id", "start_us", "end_us", "target_start_us", "target_end_us"]
)
def test_changed_audio_or_timing_never_inherits_a_human_review(field):
    before = cue()
    old = {"demo_id": "old", "experiment_id": "exp", "datasets": [{"cues": [before]}]}
    after = copy.deepcopy(before)
    after[field] = after[field] + 1 if isinstance(after[field], int) else "different"
    with pytest.raises(ValueError, match="changed audio/timing"):
        carry_exact_reviews(old, [after], {"a": {"override": "male"}})


def test_missing_or_duplicate_stable_ids_reject_progress_migration():
    old = {"demo_id": "old", "experiment_id": "exp", "datasets": [{"cues": [cue()]}]}
    with pytest.raises(ValueError, match="changed audio/timing"):
        carry_exact_reviews(old, [], {"a": {"override": "male"}})
    with pytest.raises(ValueError, match="Duplicate"):
        carry_exact_reviews(old, [cue(), cue()], {})


def test_missing_ecapa_files_are_errors_before_heavy_framework_import(tmp_path):
    with pytest.raises(RuntimeError, match="Missing/incomplete"):
        verify_model(tmp_path)


def real_shape_case():
    item = cue()
    analysis = {
        "turns": [{"speaker": "speaker", "start_us": 0, "end_us": 2_000_000}],
        "evidence": [],
    }
    settings = Settings(confidence=0.95)
    row = classify_srt_cue(item, analysis, settings, lambda *_: 0.01)
    spec = {
        "experiment_id": "exp",
        "implementation_sha256": "code",
        "model_lock_sha256": "model",
        "settings": settings.__dict__,
    }
    base = {"srt_cues": [item], "excerpts": [{"id": "excerpt", "analysis": analysis}]}
    result = {
        **spec,
        "schema": "ducai-refined-srt-predictions-v1",
        "results": [row],
        "analyses": [{"excerpt_id": "excerpt", "analysis": analysis}],
    }
    return spec, base, result


def test_build_replays_exact_saved_probabilities_instead_of_trusting_labels():
    spec, base, result = real_shape_case()
    assert verify_predictions(spec, base, result)["a"]["machine"]["label"] == "male"
    result["results"][0]["machine"]["label"] = "female"
    with pytest.raises(ValueError, match="evidence"):
        verify_predictions(spec, base, result)


@pytest.mark.parametrize(
    "field", ["settings", "experiment_id", "model_lock_sha256", "implementation_sha256"]
)
def test_stale_model_code_settings_or_experiment_is_rejected(field):
    spec, base, result = real_shape_case()
    result[field] = "changed"
    with pytest.raises(ValueError, match="Stale"):
        verify_predictions(spec, base, result)


def test_speaker_times_cannot_be_silently_replaced():
    spec, base, result = real_shape_case()
    result = copy.deepcopy(result)
    result["analyses"][0]["analysis"]["turns"][0]["end_us"] = 1_000_000
    with pytest.raises(ValueError, match="timestamps"):
        verify_predictions(spec, base, result)
