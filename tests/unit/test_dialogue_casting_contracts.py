from __future__ import annotations

from dataclasses import replace

from ducai_studio.application.dialogue_casting import casting_inputs, effective_labels
from ducai_studio.domain.dialogue_casting import (
    CastingOverride,
    CastingResult,
    DialogueCastingState,
)
from ducai_studio.domain.project import (
    MediaAsset,
    MediaKind,
    ProjectSnapshot,
    SubtitleCue,
    TimelineClip,
    TimelineTrack,
    TrackKind,
)
from ducai_studio.domain.timebase import TimeRange
from ducai_studio.engine_hosts.dialogue_casting_engine_host import (
    _classification_result_payload,
)


def _snapshot(*, first_cue: SubtitleCue | None = None) -> ProjectSnapshot:
    media = MediaAsset("video-1", "film.mp4", MediaKind.VIDEO, 12_000_000)
    video = TimelineClip(
        "video-clip-1",
        "V1",
        TimeRange(0, 10_000_000),
        source_range=TimeRange(2_000_000, 12_000_000),
        media_id=media.id,
    )
    first = first_cue or SubtitleCue(
        "cue-1",
        1,
        TimeRange(1_000_000, 3_000_000),
        "第一句话",
        "zh",
    )
    second = SubtitleCue(
        "cue-2",
        2,
        TimeRange(2_500_000, 4_000_000),
        "第二句话",
        "zh",
    )
    scene_text = SubtitleCue(
        "scene-1",
        3,
        TimeRange(4_500_000, 5_000_000),
        "三年后",
        "zh",
        role="scene_text",
    )
    return ProjectSnapshot(
        "project-1",
        "Casting fixture",
        1,
        media=(media,),
        tracks=(TimelineTrack("V1", TrackKind.VIDEO, "Video", (video,)),),
        source_cues=(first, second, scene_text),
    )


def test_casting_inputs_use_source_audio_and_ignore_text_only_edits() -> None:
    identities = {"video-1": "media-identity-v1"}
    original = casting_inputs(_snapshot(), identities)

    assert [row["id"] for row in original] == ["cue-1", "cue-2"]
    assert original[0]["parts"] == [
        {
            "media_id": "video-1",
            "media_path": "film.mp4",
            "start_us": 3_000_000,
            "end_us": 5_000_000,
            "source_identity": "media-identity-v1",
        }
    ]
    assert original[0]["srt_overlap_ids"] == ["cue-2"]

    text_only = replace(
        _snapshot().source_cues[0],
        text="Nội dung dịch và kiểu chữ không phải bằng chứng âm thanh",
        style_hint="display",
    )
    after_text_edit = casting_inputs(_snapshot(first_cue=text_only), identities)
    assert after_text_edit[0]["audio_key"] == original[0]["audio_key"]

    retimed = replace(_snapshot().source_cues[0], range=TimeRange(1_100_000, 3_000_000))
    after_timing_edit = casting_inputs(_snapshot(first_cue=retimed), identities)
    assert after_timing_edit[0]["audio_key"] != original[0]["audio_key"]

    after_media_change = casting_inputs(_snapshot(), {"video-1": "media-identity-v2"})
    assert after_media_change[0]["audio_key"] != original[0]["audio_key"]


def test_effective_labels_invalidate_stale_machine_and_manual_evidence() -> None:
    rows = casting_inputs(_snapshot(), {"video-1": "media-identity-v1"})
    keys = {row["id"]: row["audio_key"] for row in rows}
    state = DialogueCastingState(
        results=(
            CastingResult("cue-1", keys["cue-1"], "male", "classified", 0.91),
            CastingResult("cue-2", keys["cue-2"], "male", "classified", 0.88),
        ),
        overrides=(CastingOverride("cue-2", keys["cue-2"], "female"),),
        analyzer_id="analyzer-v1",
    )

    resolved = effective_labels(rows, state, "analyzer-v1")
    assert resolved["cue-1"] == {
        "label": "male",
        "manual": False,
        "ready": True,
        "reason": "classified",
        "confidence": 0.91,
        "machine_label": "male",
        "machine_reason": "classified",
        "tts_suppressed": False,
    }
    assert resolved["cue-2"]["label"] == "female"
    assert resolved["cue-2"]["manual"] is True
    assert resolved["cue-2"]["machine_label"] == "male"

    changed_rows = [dict(row) for row in rows]
    changed_rows[1]["audio_key"] = "changed-audio"
    stale = effective_labels(changed_rows, state, "analyzer-v1")
    assert stale["cue-2"] == {
        "label": "unknown",
        "manual": False,
        "ready": False,
        "reason": "needs_analysis",
        "confidence": 0.0,
        "machine_label": "unknown",
        "machine_reason": "needs_analysis",
        "tts_suppressed": False,
    }

    analyzer_changed = effective_labels(rows, state, "analyzer-v2")
    assert analyzer_changed["cue-1"]["ready"] is False
    assert analyzer_changed["cue-1"]["label"] == "unknown"
    assert analyzer_changed["cue-2"]["ready"] is True
    assert analyzer_changed["cue-2"]["label"] == "female"


def test_unknown_classifier_result_without_confidence_is_a_valid_fallback() -> None:
    payload = _classification_result_payload(
        {"id": "cue-unknown", "audio_key": "audio-unknown"},
        {
            "label": "unknown",
            "reason": "no_speech",
            "confidence": None,
        },
    )

    assert payload == {
        "cue_id": "cue-unknown",
        "audio_key": "audio-unknown",
        "label": "unknown",
        "reason": "no_speech",
        "confidence": 0.0,
    }
    CastingResult(**payload)


def test_no_speech_stays_suppressed_until_a_manual_gender_is_selected() -> None:
    rows = casting_inputs(_snapshot(), {"video-1": "media-identity-v1"})
    state = DialogueCastingState(
        results=(CastingResult("cue-1", rows[0]["audio_key"], "unknown", "no_speech"),),
        analyzer_id="analyzer-v1",
    )

    automatic = effective_labels(rows[:1], state, "analyzer-v1")["cue-1"]
    assert automatic["label"] == "unknown"
    assert automatic["reason"] == "no_speech"
    assert automatic["machine_reason"] == "no_speech"
    assert automatic["tts_suppressed"] is True

    male = replace(
        state,
        overrides=(CastingOverride("cue-1", rows[0]["audio_key"], "male"),),
    )
    assigned = effective_labels(rows[:1], male, "analyzer-v1")["cue-1"]
    assert assigned["label"] == "male"
    assert assigned["machine_reason"] == "no_speech"
    assert assigned["tts_suppressed"] is False

    needs_review = replace(
        state,
        overrides=(CastingOverride("cue-1", rows[0]["audio_key"], "unknown"),),
    )
    review = effective_labels(rows[:1], needs_review, "analyzer-v1")["cue-1"]
    assert review["manual"] is True
    assert review["tts_suppressed"] is True


def test_manual_unknown_requires_reanalysis_after_analyzer_upgrade() -> None:
    rows = casting_inputs(_snapshot(), {"video-1": "media-identity-v1"})[:1]
    state = DialogueCastingState(
        results=(CastingResult("cue-1", rows[0]["audio_key"], "unknown", "no_speech"),),
        overrides=(CastingOverride("cue-1", rows[0]["audio_key"], "unknown"),),
        analyzer_id="analyzer-v1",
    )

    stale = effective_labels(rows, state, "analyzer-v2")["cue-1"]
    assert stale["manual"] is True
    assert stale["label"] == "unknown"
    assert stale["machine_reason"] == "needs_analysis"
    assert stale["ready"] is False
    assert state.overrides[0].label == "unknown"

    reanalyzed = effective_labels(
        rows, replace(state, analyzer_id="analyzer-v2"), "analyzer-v2"
    )["cue-1"]
    assert reanalyzed["manual"] is True
    assert reanalyzed["ready"] is True
    assert reanalyzed["tts_suppressed"] is True
