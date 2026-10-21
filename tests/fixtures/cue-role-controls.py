"""Native isolated verification of Timeline Text cue-role controls."""

from __future__ import annotations

import faulthandler
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QEventLoop, QPointF, QSettings, QStandardPaths, Qt, QTimer
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import delete

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
from ducai_studio.infrastructure.project_repository import (
    encode_project_snapshot,
)
from ducai_studio.job_manager.service import JobManagerService
from ducai_studio.providers.subtitle_artwork import _ensure_fonts
from ducai_studio.ui.qt_runtime import configure_qt_plugin_dll_search
from ducai_studio.ui.subtitle_artwork_provider import SubtitleArtworkProvider
from ducai_studio.ui.timeline_media_provider import TimelineMediaProvider
from ducai_studio.ui.viewmodels.application_view_model import ApplicationViewModel


def descendants(item):
    yield item
    for child in item.childItems():
        yield from descendants(child)


def _cue(
    cue_id: str,
    index: int,
    start_us: int,
    end_us: int,
    text: str,
    *,
    role: str = "dialogue",
    track: str = "S1",
    style_hint: str = "display",
    visual_box=(0.10, 0.72, 0.80, 0.14),
    panel_box=(0.12, 0.70, 0.76, 0.18),
) -> SubtitleCue:
    return SubtitleCue(
        cue_id,
        index,
        TimeRange(start_us, end_us),
        text,
        "zh",
        source="fixture",
        confidence=0.97,
        evidence_ids=(f"frame-{cue_id}-1", f"frame-{cue_id}-2"),
        visual_box=visual_box,
        visual_box_source="ocr",
        role=role,
        orientation="horizontal",
        style_hint=style_hint,
        panel_box=panel_box,
        background_color="#101820",
        text_color="#FFE6A3",
        review_state="accepted",
        scene_font_scale=1.1,
        text_font_size=34,
        subtitle_track_id=track,
        background_padding_x=28,
        background_padding_y=18,
        background_style="ink_splash",
        letter_spacing_percent=12,
        font_bold=True,
        font_italic=True,
        text_opacity_percent=91,
        background_opacity_percent=84,
        fade_enabled=role == "scene_text",
        fade_duration_ms=250,
    )


def _snapshot(video: Path) -> ProjectSnapshot:
    source = (
        _cue(
            "c1",
            1,
            0,
            1_000_000,
            "原文一",
            role="scene_text",
            track="S2",
            style_hint="calligraphy_brush",
            visual_box=(0.66, 0.12, 0.22, 0.10),
            panel_box=(0.62, 0.10, 0.30, 0.15),
        ),
        _cue("c2", 2, 1_000_000, 2_000_000, "原文二"),
        _cue("c3", 3, 2_000_000, 3_000_000, "原文三"),
        _cue(
            "c4",
            4,
            200_000,
            800_000,
            "未選択のシーン",
            role="scene_text",
            track="S1",
            style_hint="calligraphy_brush",
            visual_box=(0.66, 0.12, 0.22, 0.10),
            panel_box=(0.62, 0.10, 0.30, 0.15),
        ),
        _cue(
            "c5",
            5,
            3_000_000,
            4_000_000,
            "原文五",
        ),
        _cue(
            "c6",
            6,
            1_200_000,
            1_800_000,
            "同一S2の文字",
            role="scene_text",
            track="S2",
            style_hint="calligraphy_brush",
            visual_box=(0.66, 0.12, 0.22, 0.10),
            panel_box=(0.62, 0.10, 0.30, 0.15),
        ),
    )
    translated = tuple(
        replace(
            cue,
            text="original cue two" if cue.id == "c2" else f"Bản dịch {cue.index}",
            language="vi",
        )
        for cue in source
    )
    title = SubtitleCue(
        "title-1",
        1,
        TimeRange(3_100_000, 3_800_000),
        "Tiêu đề T",
        "vi",
        source="manual_title",
        role="title_text",
        subtitle_track_id="T1",
        visual_box=(0.20, 0.05, 0.60, 0.12),
        visual_box_source="estimated",
        panel_box=(0.18, 0.04, 0.64, 0.15),
        style_hint="display",
        text_font_size=30,
    )

    def subtitle_clip(cue: SubtitleCue, track_id: str, translated_cue: SubtitleCue) -> TimelineClip:
        return TimelineClip(
            f"clip-{cue.id}",
            track_id,
            cue.range,
            cue_id=cue.id,
            cue_index=cue.index,
            cue_text=translated_cue.text,
            payload_revision=4,
        )

    translated_by_id = {cue.id: cue for cue in translated}
    s1 = tuple(
        subtitle_clip(cue, "S1", translated_by_id[cue.id])
        for cue in source
        if cue.subtitle_track_id == "S1"
    )
    s2 = tuple(
        subtitle_clip(cue, "S2", translated_by_id[cue.id])
        for cue in source
        if cue.subtitle_track_id == "S2"
    )
    return ProjectSnapshot(
        "cue-role-fixture",
        "Cue role controls",
        1,
        media=(MediaAsset("m", str(video), MediaKind.VIDEO, 4_000_000),),
        tracks=(
            TimelineTrack(
                "V1",
                TrackKind.VIDEO,
                "V1",
                (
                    TimelineClip(
                        "video",
                        "V1",
                        TimeRange(0, 4_000_000),
                        TimeRange(0, 4_000_000),
                        media_id="m",
                        link_group_id="av",
                    ),
                ),
            ),
            TimelineTrack(
                "A1",
                TrackKind.ORIGINAL_AUDIO,
                "A1",
                (
                    TimelineClip(
                        "audio",
                        "A1",
                        TimeRange(0, 4_000_000),
                        TimeRange(0, 4_000_000),
                        media_id="m",
                        link_group_id="av",
                    ),
                ),
            ),
            TimelineTrack("S1", TrackKind.SUBTITLE, "Phụ đề thoại", s1),
            TimelineTrack("S2", TrackKind.SUBTITLE, "Chữ trong cảnh", s2),
            TimelineTrack(
                "T1",
                TrackKind.TEXT,
                "T1 · Tiêu đề",
                (
                    TimelineClip(
                        "clip-title-1",
                        "T1",
                        title.range,
                        cue_id=title.id,
                        cue_index=title.index,
                        cue_text=title.text,
                        payload_revision=2,
                    ),
                ),
            ),
        ),
        source_cues=source,
        translated_cues=translated,
        title_cues=(title,),
    )


def _disjoint_bulk_snapshot(video: Path) -> ProjectSnapshot:
    source = (
        _cue("bulk-1", 1, 0, 800_000, "First source", track="S1"),
        _cue("bulk-2", 2, 2_000_000, 2_800_000, "Second source", track="S1"),
    )
    translated = tuple(
        replace(cue, text=f"bulk translation {cue.index}", language="vi")
        for cue in source
    )
    clips = tuple(
        TimelineClip(
            f"clip-{cue.id}",
            "S1",
            cue.range,
            cue_id=cue.id,
            cue_index=cue.index,
            cue_text=translated[cue.index - 1].text,
            payload_revision=1,
        )
        for cue in source
    )
    return ProjectSnapshot(
        "cue-role-bulk-s2",
        "Disjoint bulk S2 conversion",
        1,
        media=(MediaAsset("bulk-media", str(video), MediaKind.VIDEO, 4_000_000),),
        tracks=(
            TimelineTrack(
                "V1",
                TrackKind.VIDEO,
                "V1",
                (
                    TimelineClip(
                        "bulk-video",
                        "V1",
                        TimeRange(0, 4_000_000),
                        TimeRange(0, 4_000_000),
                        media_id="bulk-media",
                    ),
                ),
            ),
            TimelineTrack("S1", TrackKind.SUBTITLE, "Thoại", clips),
            TimelineTrack("S2", TrackKind.SUBTITLE, "Chữ cảnh"),
        ),
        source_cues=source,
        translated_cues=translated,
    )


def main() -> int:
    faulthandler.dump_traceback_later(30, repeat=True)
    directory = Path(sys.argv[1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "cue-role-source.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "4",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    configure_qt_plugin_dll_search()
    app = QApplication([])
    QStandardPaths.setTestModeEnabled(True)
    QQuickStyle.setStyle("Basic")
    _ensure_fonts()
    model = ApplicationViewModel(app)
    model.settings = QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat)
    model.runtime.start = lambda: None
    snapshot = _snapshot(source)
    encoded, _ = encode_project_snapshot(snapshot)
    project = json.loads(encoded)
    service = JobManagerService(
        database_path=directory / "state.db",
        artifact_root=directory / "artifacts",
    )
    service.projects.save(project_path=directory / "cue-role-fixture.ducai", snapshot=snapshot)
    model.project_state.replace(project)
    requests: list[tuple[str, dict, object]] = []
    failures: list[str] = []
    expected_failures: list[str] = []
    held_callbacks: list[object] = []
    hold_next_role = False
    expect_next_failure = False

    def request(action, payload=None, context=None):
        nonlocal hold_next_role, expect_next_failure
        payload = payload if isinstance(payload, dict) else {}
        requests.append((action, payload, context))

        def acknowledge():
            nonlocal expect_next_failure
            try:
                if action == "subtitle_command":
                    result = service.subtitle_command(
                        project_id=payload["project_id"],
                        command=payload["command"],
                        cue_id=payload.get("cue_id"),
                        language=payload.get("language", "source"),
                        params=payload.get("values", {}),
                        compact=True,
                        deferred=True,
                    )
                elif action == "timeline_command":
                    result = service.timeline_command(**payload, deferred=True)
                elif action == "save_project_edits":
                    result = service.save_project_edits(payload["project_id"])
                else:
                    return
                model._action_succeeded(action, result, context)
            except Exception as exc:
                if expect_next_failure:
                    expected_failures.append(str(exc))
                    expect_next_failure = False
                else:
                    failures.append(f"{action}: {exc}")
                model._action_failed(action, {"message": str(exc)}, context)

        if action == "subtitle_command" and hold_next_role:
            hold_next_role = False
            held_callbacks.append(acknowledge)
        else:
            QTimer.singleShot(0, acknowledge)

    model.runtime.request = request
    qml = Path(
        os.environ.get("DUCAI_TEST_QML_ROOT")
        or (Path(__file__).resolve().parents[2] / "src/ducai_studio/ui/qml")
    )
    engine = QQmlApplicationEngine(model)
    engine.addImageProvider("subtitle-artwork", SubtitleArtworkProvider())
    engine.addImageProvider("timeline-media", TimelineMediaProvider(directory / "tiles"))
    engine.addImportPath(str(qml))
    engine.setInitialProperties({"appModel": model})
    engine.load(qml.joinpath("Main.qml").as_uri())
    roots = engine.rootObjects()
    if not roots:
        raise RuntimeError("QML root failed to load")
    window = roots[0]
    window.setWidth(1180)
    window.setHeight(720)
    window.show()
    model.navigate("timeline")
    model.navigate("timeline")

    def wait(duration_ms: int) -> None:
        loop = QEventLoop()
        QTimer.singleShot(duration_ms, loop.quit)
        loop.exec()

    def wait_idle(timeout_ms: int = 4_000) -> None:
        deadline = time.monotonic() + timeout_ms / 1_000
        while time.monotonic() < deadline:
            app.processEvents()
            if (
                not model.subtitleBusy
                and not model.timelineBusy
                and not model.projectSaveBusy
                and not model._subtitle_save_in_flight
                and not model._subtitle_save_queue
            ):
                return
            wait(30)
        raise AssertionError(
            f"busy timeout subtitle={model.subtitleBusy} timeline={model.timelineBusy} "
            f"save={model.projectSaveBusy}"
        )

    def locate(name: str):
        items = list(descendants(window.contentItem()))
        match = next(
            (item for item in items if item.objectName() == name and item.isVisible()),
            None,
        )
        if match is None:
            candidates = [
                (item.objectName(), item.isVisible(), item.width(), item.height())
                for item in items
                if item.objectName() == name or name.startswith("timelineClipBody-")
                and item.objectName().startswith("timelineClipBody-")
            ]
            raise AssertionError(
                f"visible QML item {name!r} not found; workspace={model.currentWorkspace!r} "
                f"index={model.currentIndex} project={model.currentProject.projectId!r} "
                f"candidates={candidates[:20]}"
            )
        return match

    def visible_track_header(track_id: str) -> str:
        prefix = f"{track_id}  ·  "
        labels = [
            str(item.property("text"))
            for item in descendants(window.contentItem())
            if item.isVisible() and str(item.property("text") or "").startswith(prefix)
        ]
        assert len(labels) == 1, (track_id, labels)
        return labels[0]

    def point(item):
        return item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()

    def show_subtitle_tracks() -> None:
        track_list = locate("timelineTrackList")
        track_list.setProperty(
            "contentY",
            max(0.0, float(track_list.property("contentHeight")) - track_list.height()),
        )
        wait(150)

    def show_top_tracks() -> None:
        locate("timelineTrackList").setProperty("contentY", 0.0)
        wait(150)

    def click(item, modifier=Qt.NoModifier) -> None:
        QTest.mouseClick(window, Qt.LeftButton, modifier, point(item))
        app.processEvents()

    def source_cue(cue_id: str):
        return next(
            cue for cue in service.projects.load(project["id"]).source_cues if cue.id == cue_id
        )

    def translated_cue(cue_id: str):
        return next(
            cue for cue in service.projects.load(project["id"]).translated_cues if cue.id == cue_id
        )

    def button_enabled(name: str) -> bool:
        return bool(locate(name).property("enabled"))

    def type_ascii(value: str) -> None:
        for char in value:
            QTest.keyClick(
                window,
                Qt.Key(ord(char.upper())),
                Qt.ShiftModifier if char.isupper() else Qt.NoModifier,
            )

    wait(500)
    timeline = locate("timelineScreen")
    click(locate("timelineTextInspectorTab"))
    wait(100)
    show_subtitle_tracks()
    timeline.forceActiveFocus()
    model.setPlayheadMs(1_000)
    wait(500)
    if button_enabled("convertCueToS1Button") or button_enabled("convertCueToS2Button"):
        raise AssertionError("role buttons enabled without a selection")

    try:
        before_viewport = float(timeline.property("viewportStartUs"))
        before_playhead = int(model.playheadUs)
        before_source = source_cue("c1")
        before_translation = translated_cue("c1")
        before_clip = next(
            clip
            for track in service.projects.load(project["id"]).tracks
            for clip in track.clips
            if clip.cue_id == "c1"
        )
        rehome_before = source_cue("c4")
        rehome_translation_before = translated_cue("c4")
        rehome_clip_before = next(
            clip
            for track in service.projects.load(project["id"]).tracks
            for clip in track.clips
            if clip.cue_id == "c4"
        )
        click(locate("timelineClipBody-clip-c1"))
        wait(150)
        text_field = locate("timelineCueTextField")
        click(text_field)
        QTest.keyClick(window, Qt.Key_A, Qt.ControlModifier)
        type_ascii("edited before role conversion")
        click(locate("convertCueToS1Button"))
        wait_idle()
        show_subtitle_tracks()
        after_source = source_cue("c1")
        after_translation = translated_cue("c1")
        after_clip = next(
            clip
            for track in service.projects.load(project["id"]).tracks
            for clip in track.clips
            if clip.cue_id == "c1"
        )
        assert after_source.id == before_source.id
        assert after_source.role == "dialogue" and after_source.subtitle_track_id == "S1"
        assert after_source.range == before_source.range
        assert after_source.visual_box == before_source.visual_box
        assert after_source.panel_box == before_source.panel_box
        assert after_source.style_hint == before_source.style_hint
        assert after_source.evidence_ids == before_source.evidence_ids
        assert after_translation.id == before_translation.id
        assert after_translation.text == "edited before role conversion", (
            after_translation.text,
            locate("timelineCueTextField").property("text"),
            [(action, payload.get("command"), payload.get("cue_id"))
             for action, payload, _context in requests],
            failures,
            model.statusMessage,
        )
        assert after_translation.range == before_translation.range
        assert after_translation.panel_box == before_translation.panel_box
        assert after_clip.id == before_clip.id
        assert after_clip.track_id == "S1"
        assert after_clip.timeline_range == before_clip.timeline_range
        assert after_clip.cue_text == "edited before role conversion"
        rehome_after = source_cue("c4")
        rehome_translation_after = translated_cue("c4")
        rehome_clip_after = next(
            clip
            for track in service.projects.load(project["id"]).tracks
            for clip in track.clips
            if clip.cue_id == "c4"
        )
        assert rehome_after.role == "scene_text" and rehome_after.subtitle_track_id == "S2"
        assert rehome_after.range == rehome_before.range
        assert rehome_after.visual_box == rehome_before.visual_box
        assert rehome_after.panel_box == rehome_before.panel_box
        assert rehome_after.evidence_ids == rehome_before.evidence_ids
        assert rehome_translation_after.id == rehome_translation_before.id
        assert rehome_translation_after.subtitle_track_id == "S2"
        assert rehome_clip_after.id == rehome_clip_before.id
        assert rehome_clip_after.track_id == "S2"
        rehomed_project = service.projects.load(project["id"])
        track_names = {track.id: track.name for track in rehomed_project.tracks}
        assert track_names["S1"] == "Phụ đề thoại"
        assert track_names["S2"] == "Chữ trong cảnh"
        assert int(model.playheadUs) == before_playhead
        assert float(timeline.property("viewportStartUs")) == before_viewport

        # Enter commits while focus remains. A second same-focus edit must be
        # retained by the role command, then converted onto the disjoint S2 lane.
        show_subtitle_tracks()
        click(locate("timelineClipBody-clip-c5"))
        click(locate("timelineCueTextField"))
        QTest.keyClick(window, Qt.Key_A, Qt.ControlModifier)
        type_ascii("first edit before enter")
        QTest.keyClick(window, Qt.Key_Return)
        wait(350)
        wait_idle()
        assert translated_cue("c5").text == "first edit before enter"
        assert bool(locate("timelineCueTextField").property("activeFocus"))
        QTest.keyClick(window, Qt.Key_A, Qt.ControlModifier)
        type_ascii("latest edit after enter")
        click(locate("convertCueToS2Button"))
        wait_idle()
        show_subtitle_tracks()
        assert source_cue("c5").role == "scene_text"
        assert source_cue("c5").subtitle_track_id == "S2"
        assert translated_cue("c5").text == "latest edit after enter"

        # A blank edit remains visible and cannot be interpreted as deletion.
        click(locate("timelineClipBody-clip-c3"))
        text_field = locate("timelineCueTextField")
        click(text_field)
        QTest.keyClick(window, Qt.Key_A, Qt.ControlModifier)
        QTest.keyClick(window, Qt.Key_Backspace)
        click(locate("convertCueToS2Button"))
        wait(150)
        validation = locate("timelineCueTextValidation")
        assert validation.isVisible()
        assert "không được để trống" in str(validation.property("text")).lower()
        assert str(locate("timelineCueTextField").property("text")) == ""
        blank_blocked = source_cue("c3")
        assert blank_blocked.role == "dialogue" and blank_blocked.subtitle_track_id == "S1"
        assert translated_cue("c3").text == "Bản dịch 3"

        # Switching away discards the invalid blank draft, not the saved cue.
        click(locate("timelineClipBody-clip-c2"))
        wait(100)
        assert str(locate("timelineCueTextField").property("text")) == "original cue two"
        click(locate("timelineCueTextField"))
        QTest.keyClick(window, Qt.Key_A, Qt.ControlModifier)
        type_ascii("first edit after blank")
        QTest.keyClick(window, Qt.Key_Return)
        wait(350)
        wait_idle()
        assert translated_cue("c2").text == "first edit after blank"
        assert bool(locate("timelineCueTextField").property("activeFocus"))
        QTest.keyClick(window, Qt.Key_A, Qt.ControlModifier)
        type_ascii("original cue two")
        # Returning to the value from before Enter is still a real second edit.
        collision_before = service.projects.load(project["id"])
        expect_next_failure = True
        click(locate("convertCueToS2Button"))
        wait_idle()
        show_subtitle_tracks()
        collision_after = service.projects.load(project["id"])
        assert expected_failures
        assert source_cue("c2").role == "dialogue" and source_cue("c2").subtitle_track_id == "S1"
        assert translated_cue("c2").text == "original cue two"
        assert collision_after.revision >= collision_before.revision
        assert source_cue("c3").text == "原文三"
        assert translated_cue("c3").text == "Bản dịch 3"

        # Valid text is committed first, but the role change fails atomically
        # because another S2 cue overlaps the selected c2 range.
        click(locate("timelineClipBody-clip-c2"))
        click(locate("timelineCueTextField"))
        QTest.keyClick(window, Qt.Key_A, Qt.ControlModifier)
        type_ascii("committed despite lane collision")
        collision_before = service.projects.load(project["id"])
        expect_next_failure = True
        click(locate("convertCueToS2Button"))
        wait_idle()
        show_subtitle_tracks()
        collision_after = service.projects.load(project["id"])
        assert expected_failures
        assert source_cue("c2").role == "dialogue" and source_cue("c2").subtitle_track_id == "S1"
        assert translated_cue("c2").text == "committed despite lane collision"
        assert collision_after.revision >= collision_before.revision
        assert source_cue("c6").role == "scene_text" and source_cue("c6").subtitle_track_id == "S2"
        assert not failures, failures

        # Native Ctrl+A selects all subtitle cues; S1 conversion is one bulk command.
        show_subtitle_tracks()
        click(locate("timelineClipBody-clip-c1"))
        timeline.forceActiveFocus()
        QTest.keyClick(window, Qt.Key_A, Qt.ControlModifier)
        app.processEvents()
        assert model.selectedCueCount == 6
        assert button_enabled("convertCueToS1Button")
        bulk_before = service.projects.load(project["id"])
        click(locate("convertCueToS1Button"))
        wait_idle()
        bulk_after = service.projects.load(project["id"])
        assert all(
            cue.subtitle_track_id == "S1" and cue.role == "dialogue"
            for cue in bulk_after.source_cues
        )
        assert all(
            cue.id == before.id
            for cue, before in zip(bulk_after.source_cues, bulk_before.source_cues, strict=True)
        )
        assert all(
            cue.range == before.range
            for cue, before in zip(bulk_after.source_cues, bulk_before.source_cues, strict=True)
        )

        # Undo/redo must restore/reapply the single bulk role edit.
        timeline.forceActiveFocus()
        QTest.keyClick(window, Qt.Key_Z, Qt.ControlModifier)
        wait_idle()
        undone = service.projects.load(project["id"])
        undone_by_id = {cue.id: cue for cue in undone.source_cues}
        assert undone_by_id["c4"].role == "scene_text"
        assert undone_by_id["c4"].subtitle_track_id == "S2"
        assert undone_by_id["c6"].role == "scene_text"
        assert undone_by_id["c6"].subtitle_track_id == "S2"
        QTest.keyClick(window, Qt.Key_Y, Qt.ControlModifier)
        wait_idle()
        redone = service.projects.load(project["id"])
        assert all(
            cue.role == "dialogue" and cue.subtitle_track_id == "S1"
            for cue in redone.source_cues
        )

        # Ctrl+S and a fresh JobManager must retain role, text, identity and style.
        timeline.forceActiveFocus()
        QTest.keyClick(window, Qt.Key_S, Qt.ControlModifier)
        wait_idle()
        reopened = JobManagerService(
            database_path=directory / "state.db", artifact_root=directory / "artifacts"
        ).projects.load(project["id"])
        assert reopened.source_cues == redone.source_cues
        assert reopened.translated_cues == redone.translated_cues
        assert reopened.tracks == redone.tracks

        # Repeating S1 is a no-op and keeps the same revision.
        no_op_revision = service.projects.load(project["id"]).revision
        click(locate("convertCueToS1Button"))
        wait_idle()
        assert service.projects.load(project["id"]).revision == no_op_revision

        # The pending role command keeps both role buttons disabled while busy.
        hold_next_role = True
        click(locate("convertCueToS1Button"))
        wait(100)
        assert model.subtitleBusy
        assert not button_enabled("convertCueToS1Button")
        assert not button_enabled("convertCueToS2Button")
        assert len(held_callbacks) == 1
        held_callbacks.pop(0)()
        wait_idle()

        # Title selection is not a subtitle role conversion target.
        show_top_tracks()
        click(locate("timelineClipBody-clip-title-1"))
        wait(100)
        assert model.selectedCueId == "title-1"
        assert not button_enabled("convertCueToS1Button")
        assert not button_enabled("convertCueToS2Button")
        model.clearTimelineCueSelection()
        app.processEvents()
        assert not button_enabled("convertCueToS1Button")
        assert not button_enabled("convertCueToS2Button")

        # Ctrl+A converts a disjoint multi-selection to S2 in one command.
        bulk_s2_snapshot = _disjoint_bulk_snapshot(source)
        bulk_s2_encoded, _ = encode_project_snapshot(bulk_s2_snapshot)
        bulk_s2_project = json.loads(bulk_s2_encoded)
        service.projects.save(
            project_path=directory / "cue-role-bulk-s2.ducai",
            snapshot=bulk_s2_snapshot,
        )
        model.clearTimelineCueSelection()
        model.project_state.replace(bulk_s2_project)
        wait(250)
        show_subtitle_tracks()
        click(locate("timelineClipBody-clip-bulk-1"))
        timeline.forceActiveFocus()
        QTest.keyClick(window, Qt.Key_A, Qt.ControlModifier)
        app.processEvents()
        assert model.selectedCueCount == 2
        assert button_enabled("convertCueToS2Button")
        click(locate("convertCueToS2Button"))
        wait_idle()
        bulk_s2_after = service.projects.load(bulk_s2_snapshot.id)
        assert all(
            cue.role == "scene_text" and cue.subtitle_track_id == "S2"
            for cue in bulk_s2_after.source_cues
        )
        assert [cue.text for cue in bulk_s2_after.translated_cues] == [
            "bulk translation 1",
            "bulk translation 2",
        ]
        assert {
            clip.cue_id: (clip.id, clip.track_id)
            for track in bulk_s2_after.tracks
            for clip in track.clips
            if clip.cue_id
        } == {"bulk-1": ("clip-bulk-1", "S2"), "bulk-2": ("clip-bulk-2", "S2")}
        show_subtitle_tracks()
        s2_header = visible_track_header("S2")

        screenshot = directory / "cue-role-controls-1180x720.png"
        window.grabWindow().save(str(screenshot))
        report = {
            "status": "PASS",
            "window": [1180, 720],
            "single_conversion": True,
            "pending_text_committed": True,
            "enter_then_edit_latest_converted": True,
            "return_to_pre_enter_value_saved": True,
            "bulk_ctrl_a_conversion": True,
            "positive_bulk_s2_conversion": True,
            "s2_visible_track_header": s2_header,
            "identity_geometry_style_evidence_preserved": True,
            "seek_and_viewport_unchanged": True,
            "s2_collision_atomic": True,
            "title_and_no_selection_disabled": True,
            "busy_buttons_disabled": True,
            "save_reopen": True,
            "undo_redo": True,
            "same_role_noop_revision": True,
            "request_count": len(requests),
            "expected_collision_errors": expected_failures,
            "screenshot": str(screenshot),
        }
        (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
        return 0
    finally:
        timeline.forceActiveFocus()
        window.setProperty("shutdownComplete", True)
        window.close()
        delete(engine)
        app.processEvents()
        faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    raise SystemExit(main())
