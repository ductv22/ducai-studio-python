from __future__ import annotations

import copy
import wave

import pytest
from dialogue_casting.analysis import Settings
from dialogue_casting.corpus import srt_cues
from dialogue_casting.demo import write_demo_page
from dialogue_casting.srt_demo import (
    classify_srt_cue,
    prior_review_context,
    select_complete_cues,
    validate_review,
    write_exact_clip,
)


def cue(start=10_000_000, end=12_000_000):
    return {"id": "srt-a", "start_us": start, "end_us": end}


def turn(start=0, end=20_000_000, speaker="a"):
    return {"start_us": start, "end_us": end, "speaker": speaker}


def analysis():
    return {
        "turns": [turn()],
        "evidence": [
            {**turn(0, 5_000_000), "female_probability": 0.001},
            {**turn(15_000_000, 20_000_000), "female_probability": 0.999},
        ],
    }


def should_not_classify(*args):
    pytest.fail("Ambiguous/short cue must not manufacture a local classifier reference")


def test_srt_text_is_opt_in_so_independent_listening_pack_stays_blinded(tmp_path):
    path = tmp_path / "dialogue.srt"
    path.write_text("\ufeff1\n00:00:10,000 --> 00:00:12,000\n甲\n乙<script>\n", encoding="utf-8")
    assert srt_cues(path) == [{**cue(), "id": "srt-1"}]
    assert srt_cues(path, include_text=True) == [{**cue(), "id": "srt-1", "text": "甲\n乙<script>"}]


def test_select_whole_dialogue_cues_keeps_gaps_and_reports_incomplete_edges():
    excerpt = cue(10_000_000, 20_000_000)
    cues = [
        {**cue(9_000_000, 11_000_000), "id": "edge-left"},
        {**cue(11_000_000, 12_000_000), "id": "a"},
        {**cue(13_000_000, 16_000_000), "id": "b"},
        {**cue(15_000_000, 18_000_000), "id": "c"},
        {**cue(18_000_000, 19_000_000), "id": "scene", "role": "scene"},
        {**cue(19_000_000, 21_000_000), "id": "edge-right"},
    ]
    selected, excluded = select_complete_cues(cues, excerpt)
    assert [c["id"] for c in selected] == ["a", "b", "c"]
    assert selected[0]["srt_overlap_ids"] == []
    assert selected[1]["srt_overlap_ids"] == ["c"]
    assert selected[2]["srt_overlap_ids"] == ["b"]
    assert [c["id"] for c in excluded] == ["edge-left", "edge-right"]
    assert all(c["reason"] == "incomplete_at_excerpt_edge" for c in excluded)
    assert (excluded[0]["start_us"], excluded[1]["end_us"]) == (9_000_000, 21_000_000)


def test_long_complete_cue_is_an_explicit_error():
    with pytest.raises(ValueError, match="20-second"):
        select_complete_cues([cue(0, 21_000_000)], cue(0, 70_000_000))


def test_exact_pcm_clip_has_all_target_frames_and_no_adjacent_context(tmp_path):
    source, target = tmp_path / "source.wav", tmp_path / "target.wav"
    frames = b"\x01\x02" * 8000 + b"\x03\x04" * 32000 + b"\x05\x06" * 8000
    with wave.open(str(source), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(frames)
    write_exact_clip(source, target, 500_000, 2_500_000)
    with wave.open(str(target), "rb") as wav:
        assert wav.getnframes() == 32000
        assert wav.readframes(wav.getnframes()) == b"\x03\x04" * 32000
    with pytest.raises(ValueError, match="not available"):
        write_exact_clip(source, target, 0, 4_000_000)


def test_local_cue_evidence_does_not_inherit_conflicting_outside_speaker_references():
    calls = []

    def probability(start, end):
        calls.append((start, end))
        return 0.001

    result = classify_srt_cue(cue(), analysis(), Settings(), probability)
    assert calls == [(10_000_000, 12_000_000)]
    assert result["evidence_scope"] == "inside_srt"
    assert result["machine"]["label"] == "male"
    assert result["comparison_machine"]["reason"] == "contradictory_speaker_evidence"


def test_weak_local_evidence_is_not_overridden_by_a_strong_outside_reference():
    value = analysis()
    value["evidence"] = value["evidence"][:1]
    result = classify_srt_cue(cue(), value, Settings(), lambda *_: 0.6)
    assert result["comparison_machine"]["label"] == "male"
    assert result["machine"]["label"] == "unknown"
    assert result["machine"]["reason"] == "low_confidence"
    assert result["evidence_scope"] == "inside_srt"


def test_short_cue_explicitly_uses_speaker_reference_and_retains_conflict():
    result = classify_srt_cue(
        cue(10_000_000, 10_500_000), analysis(), Settings(), should_not_classify
    )
    assert result["evidence_scope"] == "speaker_reference"
    assert result["machine"]["reason"] == "contradictory_speaker_evidence"


@pytest.mark.parametrize("kind", ["overlap", "sequential", "subtitle", "silence"])
def test_mixed_or_unusable_cue_keeps_fallback_without_inference(kind):
    value, item = analysis(), cue()
    if kind == "overlap":
        value["turns"].append(turn(10_000_000, 12_000_000, "b"))
    elif kind == "sequential":
        value["turns"] = [turn(10_000_000, 11_000_000), turn(11_000_000, 12_000_000, "b")]
    elif kind == "subtitle":
        item["srt_overlap_ids"] = ["srt-b"]
    else:
        value["turns"], value["evidence"] = [], []
    result = classify_srt_cue(item, value, Settings(), should_not_classify)
    assert result["machine"]["label"] == "unknown"
    assert (
        result["machine"]["reason"]
        == {
            "overlap": "overlapping_speakers",
            "sequential": "multiple_speakers",
            "subtitle": "overlapping_subtitles",
            "silence": "no_speech",
        }[kind]
    )


def test_classifier_failure_is_not_converted_to_an_unknown_label():
    def broken(*args):
        raise RuntimeError("ONNX failed")

    with pytest.raises(RuntimeError, match="ONNX failed"):
        classify_srt_cue(cue(), analysis(), Settings(), broken)


def test_reference_text_is_not_evidence_for_a_gender_decision():
    first = classify_srt_cue({**cue(), "text": "他说"}, analysis(), Settings(), lambda *_: 0.001)
    second = classify_srt_cue({**cue(), "text": "她说"}, analysis(), Settings(), lambda *_: 0.001)
    assert first == second


def public_and_review():
    public = {
        "demo_id": "demo",
        "experiment_id": "experiment",
        "datasets": [
            {"cues": [{"id": "a", "clip_sha256": "audio", "machine": {"label": "unknown"}}]}
        ],
    }
    review = {
        "schema": "ducai-dialogue-casting-demo-review-v1",
        "label_source": "demo-assisted-review",
        "demo_id": "demo",
        "experiment_id": "experiment",
        "reviews": [
            {
                "cue_id": "a",
                "clip_sha256": "audio",
                "machine_label": "unknown",
                "override": "male",
                "reviewed": True,
                "notes": "hai người nói <script>",
            }
        ],
    }
    return public, review


@pytest.mark.parametrize(
    "field", ["demo", "source", "cue", "clip", "machine", "value", "bool", "duplicate"]
)
def test_assisted_review_intake_rejects_identity_or_value_changes_atomically(field):
    public, document = public_and_review()
    original = copy.deepcopy(document)
    if field == "demo":
        document["demo_id"] = "different"
    elif field == "source":
        document["label_source"] = "human-listening"
    elif field == "duplicate":
        document["reviews"].append(copy.deepcopy(document["reviews"][0]))
    else:
        key, value = {
            "cue": ("cue_id", "missing"),
            "clip": ("clip_sha256", "different"),
            "machine": ("machine_label", "female"),
            "value": ("override", "mixed"),
            "bool": ("reviewed", 1),
        }[field]
        document["reviews"][0][key] = value
    with pytest.raises(ValueError):
        validate_review(document, public)
    assert public_and_review()[1] == original


def test_review_preserves_automatic_notes_and_does_not_change_original_labels():
    public, document = public_and_review()
    document["reviews"][0]["override"] = None
    assert validate_review(document, public) == {
        "a": {"override": None, "reviewed": True, "notes": "hai người nói <script>"}
    }
    assert public["datasets"][0]["cues"][0]["machine"]["label"] == "unknown"


def test_inline_srt_and_notes_are_json_escaped_and_not_html_injected(tmp_path):
    public = {"srt_text": "甲</script><script>bad()</script>", "carried_reviews": {}}
    write_demo_page(tmp_path, public)
    page = (tmp_path / "MO-DEMO.html").read_text(encoding="utf-8")
    assert "甲</script>" not in page
    assert "甲\\u003c/script>" in page
    assert "__DEMO_DATA__" not in page
    assert "__REVIEW_LOGIC__" not in page


def test_changed_boundary_only_flags_conflicting_review_never_inherits_its_gender():
    item = {**cue(), "film_id": "film"}
    machine = {"cue_id": item["id"], "label": "female", "reason": "assigned"}
    known = {"old": {**item, "end_us": 11_000_000, "title": "Câu cũ"}}
    reviews = {"old": {"override": "male", "reviewed": True, "notes": "giọng nam"}}
    result = prior_review_context(item, machine, known, reviews)
    assert result["machine"]["label"] == "unknown"
    assert result["machine"]["reason"] == "prior_review_conflict"
    assert result["prior_feedback"][0]["override"] == "male"
    assert result["prior_feedback"][0]["overlap_us"] == 1_000_000
    assert machine["label"] == "female"
    reviews["old"]["override"] = None
    assert prior_review_context(item, machine, known, reviews)["machine"] == machine
    reviews["old"]["override"] = "male"
    known["old"]["film_id"] = "other-film"
    assert prior_review_context(item, machine, known, reviews)["prior_feedback"] == []


def test_old_uncertain_override_prevents_new_automatic_assignment_on_overlapping_audio():
    item = {**cue(), "film_id": "film"}
    machine = {"label": "male", "reason": "assigned"}
    known = {"old": {**item, "title": "Câu cũ"}}
    reviews = {"old": {"override": "unknown", "reviewed": False, "notes": "nhiều người nói"}}
    result = prior_review_context(item, machine, known, reviews)
    assert result["machine"]["label"] == "unknown"
    assert result["prior_feedback"][0]["notes"] == "nhiều người nói"
