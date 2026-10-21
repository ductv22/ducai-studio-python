from pathlib import Path

import pytest

from ducai_studio.ui.app import (
    _application_data_root,
    _seed_signed_update_smoke_candidate,
    _ui_resource_root,
)


def test_signed_update_smoke_is_inert_on_normal_launch(monkeypatch):
    monkeypatch.delenv("DUCAI_SMOKE_UPDATE_CANDIDATE", raising=False)
    _seed_signed_update_smoke_candidate(None)


def test_signed_update_smoke_requires_explicit_isolated_bounded_launch(monkeypatch):
    monkeypatch.setenv("DUCAI_SMOKE_UPDATE_CANDIDATE", "missing-candidate")
    monkeypatch.delenv("DUCAI_APP_DATA_ROOT", raising=False)
    monkeypatch.delenv("DUCAI_SMOKE_EXIT_MS", raising=False)
    with pytest.raises(ValueError, match="isolated app data"):
        _seed_signed_update_smoke_candidate(None)


def test_signed_update_smoke_rejects_unsigned_candidate(monkeypatch, tmp_path):
    monkeypatch.setenv("DUCAI_SMOKE_UPDATE_CANDIDATE", str(tmp_path))
    monkeypatch.setenv("DUCAI_APP_DATA_ROOT", str(tmp_path / "app-data"))
    monkeypatch.setenv("DUCAI_SMOKE_EXIT_MS", "15000")
    (tmp_path / "update-manifest.json").write_bytes(b"{}")
    (tmp_path / "update-manifest.sig").write_bytes(b"x" * 64)
    with pytest.raises(ValueError):
        _seed_signed_update_smoke_candidate(None)


def test_settings_update_panel_requires_explicit_restart_confirmation() -> None:
    root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    settings = (root / "screens" / "SettingsScreen.qml").read_text(encoding="utf-8")
    main = (root / "Main.qml").read_text(encoding="utf-8")

    assert 'SectionTitle { text: "Cập nhật DUCAI STUDIO" }' in settings
    assert 'text: "Cài đặt khi xong tác vụ"' in settings
    assert "root.appModel.updater.requestInstallConfirmation()" in settings
    assert "textFormat: Text.PlainText" in settings
    assert "onInstallConfirmationRequested" in main
    assert "window.appModel.updater.installConfirmed()" in main


def test_locked_settings_keep_diagnostics_but_gate_database_optimization() -> None:
    settings = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "SettingsScreen.qml"
    ).read_text(encoding="utf-8")
    compact = " ".join(line.strip() for line in settings.splitlines())

    diagnostics = compact[compact.index('text: "Chạy chẩn đoán"') :]
    diagnostics = diagnostics[: diagnostics.index("onClicked:")]
    assert "enabled: root.appModel.backendReady" in diagnostics
    assert "activation.active" not in diagnostics

    optimizer = compact[compact.index('text: root.appModel.databaseMaintenanceBusy') :]
    optimizer = optimizer[: optimizer.index("onClicked:")]
    assert (
        "enabled: root.appModel.backendReady && (root.appModel.activation.active || "
        "root.appModel.databaseMaintenanceBusy) && "
        'root.appModel.databaseMaintenance.state !== "cancelling"'
    ) in optimizer


def test_application_data_root_is_canonical_before_single_instance_lock(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    indirect = target / ".." / target.name

    assert _application_data_root(str(indirect)) == target.resolve()


def test_file_dialogs_pass_url_strings_to_python_for_unicode_safe_normalization() -> None:
    qml = (
        Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml" / "Main.qml"
    ).read_text(encoding="utf-8")

    assert "fileMode: FileDialog.OpenFiles" in qml
    assert "selectedFiles[index].toString()" in qml
    assert "window.appModel.importPaths(values)" in qml
    assert "inspectLegacyProjectPath(selectedFile.toString())" in qml
    assert "selectedFile.toLocalFile()" not in qml


def test_scene_text_preview_is_glyph_aware_monotonic_and_manually_adjustable() -> None:
    qml_root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    preview = (qml_root / "controls" / "TimelinePreview.qml").read_text(encoding="utf-8")
    editor = (qml_root / "screens" / "SubtitleEditorScreen.qml").read_text(encoding="utf-8")
    toolbar = (qml_root / "controls" / "SubtitleTextToolbar.qml").read_text(encoding="utf-8")

    assert "root.artworkForCue(sceneTextFrame.modelData)" in preview
    assert "root.artworkForCue(root.activeSubtitle)" in preview
    assert "Math.min(240" not in preview
    assert "fontSizeMode: Text.Fit" not in preview
    artwork = (qml_root.parents[1] / "providers" / "subtitle_artwork.py").read_text(
        encoding="utf-8"
    )
    assert "background_padding_x" in artwork
    assert "background_padding_y" in artwork
    assert "clip: true" in preview
    timeline = (qml_root / "screens" / "TimelineScreen.qml").read_text(encoding="utf-8")
    assert "SubtitleTextToolbar {" not in editor
    assert "SubtitleTextToolbar {" in timeline
    assert 'text: "Kích thước chữ S2"' not in editor
    assert "from: 1" in toolbar
    assert "to: 2147483647" in toolbar
    assert "setSelectedSubtitleTextFormat" in toolbar
    assert preview.count("enabled: !root.editableOverlays") >= 2
    assert preview.count("visible: !root.editableOverlays") >= 2
    assert 'text: "Ngang"' in toolbar
    assert 'text: "Dọc"' in toolbar
    assert 'accessibleName: "Nền ngang (%)"' in toolbar
    assert 'accessibleName: "Nền dọc (%)"' in toolbar
    assert 'value: "calligraphy_soft"' in toolbar
    assert 'value: "calligraphy_bold"' in toolbar
    assert 'value: "calligraphy_ink"' in toolbar
    assert 'value: "calligraphy_brush"' in toolbar
    assert 'value: "calligraphy_water"' in toolbar
    assert 'value: "calligraphy_splash"' in toolbar
    assert 'value: "calligraphy_hurricane"' in toolbar
    assert 'value: "ink_splash"' in toolbar
    assert 'value: "ink_wash"' in toolbar
    assert 'value: "ink_cloud"' in toolbar
    assert 'value: "brush_stroke"' in toolbar
    assert 'text: "Mực tàu"' in toolbar
    assert "root.applyInkPreset()" in toolbar
    assert "root.appModel.subtitleArtwork(" in preview
    assert "background_style" in artwork


def test_subtitle_cues_can_be_opened_deleted_formatted_and_drawn_from_ui() -> None:
    qml_root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    preview = (qml_root / "controls" / "TimelinePreview.qml").read_text(encoding="utf-8")
    timeline = (qml_root / "screens" / "TimelineScreen.qml").read_text(encoding="utf-8")
    editor = (qml_root / "screens" / "SubtitleEditorScreen.qml").read_text(encoding="utf-8")
    manual_dialog = (qml_root / "controls" / "ManualSubtitleCueDialog.qml").read_text(
        encoding="utf-8"
    )

    assert "readonly property bool isSubtitleClip" in timeline
    assert "root.appModel.openSubtitleCueInEditor(" in timeline
    assert 'text: "Xóa cue chữ và nền"' in timeline
    assert "root.appModel.deleteSubtitleCue(" in timeline
    assert "SubtitleTextToolbar {" in timeline
    assert "onDoubleClicked" in preview
    assert 'text: "Xóa cue chữ và nền"' in preview
    assert '"Quét vùng OCR"' in editor
    assert '"Vùng không OCR"' in editor
    assert 'root.regionEditorMode = "manual_text"' in editor
    assert "ManualSubtitleCueDialog {" in editor
    assert "appModel.addManualSubtitleCue(" in manual_dialog
    assert 'value: "dialogue"' in manual_dialog
    assert 'value: "scene_text"' in manual_dialog
    assert 'text: "Mờ dần vào/ra"' in manual_dialog
    assert "root.appModel.setOcrRegions(" in editor


def test_timeline_exposes_persistent_marks_and_heavy_overlap_warning() -> None:
    timeline = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "TimelineScreen.qml"
    ).read_text(encoding="utf-8")

    assert 'text: "＋ Mark"' in timeline
    assert 'text: "Xoá mark"' in timeline
    assert "addTimelineMarker()" in timeline
    assert "removeTimelineMarkerAtPlayhead()" in timeline
    assert "timelineMarkers" in timeline
    assert "removeTimelineMarker(markerUs)" in timeline
    assert "model.overlapWarning" in timeline
    assert "Cue chồng nhau trên 2/3 thời lượng" in timeline


def test_projects_screen_uses_a_dedicated_new_project_dialog_contract() -> None:
    root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    main = (root / "Main.qml").read_text(encoding="utf-8")
    projects = (root / "screens" / "ProjectsScreen.qml").read_text(encoding="utf-8")

    assert "onNewProjectDialogRequested" in main
    assert "createProjectFromPath(selectedFile.toString())" in main
    assert "showNewProjectDialog()" in projects


def test_duplicate_media_dialog_has_safe_choices_and_a_no_op_close_button() -> None:
    root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    main = (root / "Main.qml").read_text(encoding="utf-8")

    assert "onDuplicateMediaDialogRequested" in main
    assert 'text: "X"' in main
    assert "cancelDuplicateMediaResolution()" in main
    assert 'resolveDuplicateMedia("use_existing"' in main
    assert '"create_copy", duplicateProjectName.text' in main


def test_every_integer_spinbox_is_editable_and_uses_the_shared_control() -> None:
    root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    export = (root / "controls" / "ExportSettingsPanel.qml").read_text(encoding="utf-8")
    control = (root / "controls" / "DsSpinBox.qml").read_text(encoding="utf-8")

    assert not any(line.lstrip().startswith("SpinBox {") for line in export.splitlines())
    # Coordinate fields for logo/blur are intentionally removed. Geometry is
    # edited directly on the video preview to avoid pointer jumps.
    assert export.count("DsSpinBox {") == 17
    assert "editable: true" in control
    assert "selectByMouse: true" in control
    assert export.count("accessibleName:") >= export.count("DsSpinBox {")


def test_subtitle_numeric_controls_and_timeline_inspector_are_width_safe() -> None:
    root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    spinbox = (root / "controls" / "DsSpinBox.qml").read_text(encoding="utf-8")
    button = (root / "controls" / "DsButton.qml").read_text(encoding="utf-8")
    toolbar = (root / "controls" / "SubtitleTextToolbar.qml").read_text(encoding="utf-8")
    dialog = (root / "controls" / "ManualSubtitleCueDialog.qml").read_text(encoding="utf-8")
    timeline = (root / "screens" / "TimelineScreen.qml").read_text(encoding="utf-8")

    assert "Layout.minimumWidth: 104" in spinbox
    assert "readonly property int indicatorWidth: 28" in spinbox
    assert "ToolTip.text: control.accessibleName" in spinbox
    assert 'property string tooltipText: ""' in button
    assert "ToolTip.text: control.tooltipText" in button
    assert toolbar.count("Layout.fillWidth: true") >= 10
    assert 'SectionTitle { text: "Hiệu ứng" }' in toolbar
    assert 'tooltipText: "Bật/tắt mờ dần' in toolbar
    assert "standardButtons: Dialog.NoButton" in dialog
    assert 'text: "Hủy"' in dialog
    assert 'text: root.titleMode ? "Thêm Text" : root.cueRole === "dialogue"' in dialog
    assert timeline.count("Layout.minimumWidth: 0") >= 6
    assert 'tooltipText: "Thuộc tính clip đang chọn."' in timeline
    assert 'tooltipText: "Áp dụng phần trăm tốc độ' in timeline


def test_timeline_context_menus_render_inside_the_main_window() -> None:
    timeline = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "TimelineScreen.qml"
    ).read_text(encoding="utf-8")
    compact = "".join(timeline.split())

    assert timeline.count("Popup {") == 2
    assert timeline.count("popupType: Popup.Item") == 2
    assert timeline.count("parent: Overlay.overlay") == 2
    assert timeline.count("closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside") == 2
    assert "trackMenu.openAt(trackRow,mouse.x,mouse.y)" in compact
    assert "clipMenuLoader.openAt(clipItem,mouse.x,mouse.y)" in compact
    assert "sourceComponent: Popup" in timeline
    assert "openContextMenus > 0" in timeline
    assert 'root.selectInspectorTab("clip")' in timeline
    assert timeline.count("sourceItem.mapToItem(") == 2
    assert "DsContextButton {" in timeline
    assert "trackMenu.close()" in timeline
    assert "clipMenu.close()" in timeline


def test_review_required_jobs_are_not_presented_as_engine_failures() -> None:
    panel = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "controls"
        / "JobActivityPanel.qml"
    ).read_text(encoding="utf-8")

    assert 'errorCode === "ocr_review_required"' in panel
    assert 'errorCode === "translation_review_required"' in panel
    assert 'errorCode === "tts_review_required"' in panel
    assert 'return "Cần rà soát"' in panel
    assert "jobStateLabel(jobRow.model)" in panel
    assert "stateColor(jobRow.model.state, jobRow.model.errorCode)" in panel

    multi = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "MultiVideoScreen.qml"
    ).read_text(encoding="utf-8")
    assert "function statusColor(state, errorCode)" in multi
    assert 'errorCode === "ocr_review_required"' in multi


def test_translation_history_can_cancel_delete_active_rows_and_select_a_cache() -> None:
    root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    panel = (root / "controls" / "JobActivityPanel.qml").read_text(encoding="utf-8")
    subtitle = (root / "screens" / "SubtitleEditorScreen.qml").read_text(encoding="utf-8")

    assert 'text: "Xoá toàn bộ lịch sử"' in panel
    assert "root.appModel.deleteJobHistory(jobRow.model.jobId)" in panel
    assert "Tác vụ đang chạy sẽ được hủy an toàn rồi tự xóa" in panel
    assert 'textRole: "label"' in subtitle
    assert "root.appModel.translationCaches" in subtitle
    assert "root.appModel.selectTranslationCache" in subtitle


def test_voice_studio_exposes_finished_voice_player_and_detailed_tts_phases() -> None:
    root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    voice = (root / "screens" / "VoiceStudioScreen.qml").read_text(encoding="utf-8")
    panel = (root / "controls" / "JobActivityPanel.qml").read_text(encoding="utf-8")

    assert "root.appModel.voicePlaybackEnabled" in voice
    assert "root.appModel.currentProject.voiceSource" in voice
    assert '"Nghe voice thành phẩm"' in voice
    assert "productionPlayer.setPosition(value)" in voice
    assert 'model.phase === "cache_restore"' in panel
    assert 'model.phase === "synthesis"' in panel
    assert 'model.phase === "targeted_retry"' in panel
    assert 'model.phase === "timeline_assembly"' in panel
    assert 'model.phase === "timeline_assembly_measure"' in panel
    assert 'model.phase === "final_master_measure"' in panel
    assert 'model.phase === "final_master_apply"' in panel
    assert '"Đã kiểm tra cache " + model.phaseCurrent + "/" + model.phaseTotal' in panel
    assert 'model.phase === "warmup" ? "Đang làm nóng · " : "Đã tổng hợp "' in panel
    assert '+ model.phaseCurrent + "/" + model.phaseTotal' in panel
    assert '"Đã ghép " + model.phaseCurrent + "/" + model.phaseTotal' in panel


def test_voice_library_separates_preview_install_and_selection_actions() -> None:
    qml = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "VoiceStudioScreen.qml"
    ).read_text(encoding="utf-8")

    assert '"Dừng" : "Nghe thử"' in qml
    assert "root.appModel.previewCatalogVoice(voiceDelegate.voiceId)" in qml
    assert "root.appModel.installVoice(voiceDelegate.voiceId)" in qml
    assert 'runningText: "Đang tải " + voiceDelegate.installProgress + "%"' in qml
    assert "enabled: voiceDelegate.installed" in qml
    assert "opacity: installed ? 1.0 : 0.55" not in qml


def test_job_activity_panel_localizes_whisper_phases() -> None:
    panel = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "controls"
        / "JobActivityPanel.qml"
    ).read_text(encoding="utf-8")

    assert '"extract_audio": "Đang tách audio 16 kHz"' in panel
    assert '"load_model": "Đang nạp Whisper large-v3"' in panel
    assert '"transcribe": "Whisper đang nhận dạng lời nói"' in panel
    assert '"cpu_fallback": "CUDA lỗi · đang chuyển sang CPU"' in panel
    assert "function percentLabel(current, total)" in panel
    assert ".toFixed(1)" in panel
    assert '"Đang nạp VieNeu và chuẩn bị dò cache"' in panel
    assert "model.progressAgeMs" in panel
    assert '" · batch hiện tại "' in panel
    assert "Behavior on value" in panel
    assert "indeterminate:" in panel
    assert "maximumLineCount: 2" in panel


def test_dashboard_uses_the_open_project_instead_of_static_placeholders() -> None:
    root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    dashboard = (root / "screens" / "DashboardScreen.qml").read_text(encoding="utf-8")

    assert 'objectName: "dashboardProjectList"' in dashboard
    assert "openProject(projectRow.model.projectId)" in dashboard
    assert "readonly property bool selected:" in dashboard
    assert "root.appModel.currentProject.projectId" in dashboard
    assert "TimelinePreview {" in dashboard
    assert "model: root.appModel.currentProject.trackModel" in dashboard
    assert "root.appModel.currentProject.subtitleCueModel.count" in dashboard
    assert "root.appModel.currentProject.hasTranslatedSubtitles" in dashboard
    assert "root.appModel.currentProject.hasVoiceTrack" in dashboard


def test_ocr_fps_field_blocks_invalid_input_and_restores_the_last_valid_value() -> None:
    qml = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "SubtitleEditorScreen.qml"
    ).read_text(encoding="utf-8")

    assert "property string lastValidText" in qml
    assert 'String(value).replace(",", ".")' in qml
    assert "function isFpsTextValid(value)" in qml
    assert "onTextChanged:" in qml
    assert "onEditingFinished:" in qml
    assert "onActiveFocusChanged:" in qml
    assert "id: invalidOcrFpsReset" in qml
    assert "invalidOcrFpsReset.restart()" in qml
    assert "ocrFps.valueIsValid" in qml
    assert '"Tốc độ quét"' in qml


def test_srt_export_actions_are_available_in_matching_workflows() -> None:
    qml = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "SubtitleEditorScreen.qml"
    ).read_text(encoding="utf-8")

    assert "id: sourceSrtExportWorkflowButton" in qml
    assert "id: translatedSrtExportWorkflowButton" in qml
    assert 'visible: root.workspaceMode === "transcribe"' in qml
    assert 'visible: root.workspaceMode === "translate"' in qml


def test_export_settings_keep_critical_controls_visible_in_a_narrow_pane() -> None:
    qml = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "controls"
        / "ExportSettingsPanel.qml"
    ).read_text(encoding="utf-8")

    assert "width: root.availableWidth" in qml
    assert 'objectName: "exportOutputPicker"' in qml
    assert 'objectName: "exportCheckpointSpinBox"' in qml
    assert "exportJobActions" not in qml
    assert "columns: 2" in qml


def test_gemini_web_floating_window_defaults_to_off() -> None:
    qml = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "SubtitleEditorScreen.qml"
    ).read_text(encoding="utf-8")

    assert "property bool headedWebEnabled: false" in qml

    activity = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "controls"
        / "JobActivityPanel.qml"
    ).read_text(encoding="utf-8")
    assert '" · đoạn " + model.chunkCurrent + "/" + model.chunkTotal' in activity
    assert '" · đã xử lý " + durationLabel(model.phaseCurrent / 1000)' in activity
    assert '"finalize_concat": "Đang ghép MP4"' in activity
    assert '"finalize_probe": "Đang kiểm tra video/audio"' in activity
    assert '"finalize_checksum": "Đang xác minh SHA-256"' in activity
    assert '"finalize_publish": "Đang công bố file thành phẩm"' in activity
    assert "function sizeLabel(bytes)" in activity


def test_export_preview_owns_feedback_before_playback_state_changes() -> None:
    export = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "ExportScreen.qml"
    ).read_text(encoding="utf-8")

    assert "feedbackEnabled: false" in export
    assert "const wasPlaying = outputPlayer.playbackState" in export
    assert 'UiFeedback.show("Đã tạm dừng thành phẩm"' in export
    assert 'UiFeedback.show("Đang phát thành phẩm"' in export


def test_effects_screen_has_live_logo_and_blur_preview() -> None:
    root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    export = (root / "controls" / "ExportSettingsPanel.qml").read_text(encoding="utf-8")
    preview = (root / "controls" / "TimelinePreview.qml").read_text(encoding="utf-8")

    timeline = (root / "screens" / "TimelineScreen.qml").read_text(encoding="utf-8")
    assert "showProjectOverlays: true" in timeline
    assert "editableProjectOverlays:" in timeline
    assert "mediaName(root.logoOverlay.asset_id)" in export
    assert "import QtQuick.Effects" in preview
    assert "property bool showProjectOverlays: false" in preview
    assert "ShaderEffectSource {" in preview
    assert "MultiEffect {" in preview
    assert "* root.subtitleScale()" in preview
    assert "? Math.SQRT2 : 1.0" in preview
    assert 'root.blurOverlay.concealment_mode === "cover"' in preview
    assert "opacity: 0.90" in preview
    assert 'overlayFrame.overlay.concealment_mode === "cover"' in preview
    assert 'overlayFrame.overlay.tracking_mode === "ocr_watermark"' in preview
    assert '"CHE WATERMARK OCR"' in preview
    assert "overlayFrame.tracksAutomaticRegion" in preview
    assert 'item.tracking_mode === "ocr_watermark"' in export
    assert "Đã tự che " in export
    assert "Bật Che kín chữ gốc" in export
    assert "setOverlayConcealmentMode" in export
    assert "automaticBlurPaddingY.value / 1000" in export
    assert "Number(value / 10).toLocaleString" in export
    assert 'overlay("overlay-default-blur")' in preview
    assert 'overlay("overlay-default-logo")' in preview
    assert "sourceSubtitleRegionsAt(playhead)" in preview
    assert "const withinRange = root.appModel.playheadUs >= startUs" in preview
    assert "return Boolean(root.activeSubtitleRegions" in preview
    assert "&& root.overlayActive(root.blurOverlay)" in preview
    assert "? root.activeSubtitleRegions : []" in preview
    assert "root.trackedSubtitleBlur(root.blurOverlay, modelData)" in preview
    assert "mediaSource(\n                            overlayFrame.overlay.asset_id" in preview
    assert "function captureCurrentGeometry()" in preview
    assert preview.index("const currentX = overlayFrame.x") < preview.index(
        "overlayFrame.localOverride = true"
    )
    assert "function resizeHandleHitMargin()" in preview
    assert "return compact ? -4 : -8" in preview
    assert "anchors.margins: overlayFrame.resizeHandleHitMargin()" in preview
    assert "anchors.margins: -13" not in preview
    assert "anchors.rightMargin: 0" in preview
    assert "anchors.bottomMargin: 0" in preview
    assert 'objectName: "overlayTopLeftResizeHandle"' in preview
    assert "overlayFrame.manualWidth = right - overlayFrame.manualX" in preview
    assert "overlayFrame.manualHeight = bottom - overlayFrame.manualY" in preview
    assert "anchors.bottom: parent.bottom" in preview
    assert "visible: !root.editableOverlays" in preview
    assert "height: root.editableOverlays ? 0 : 46" in preview
    assert "root.selectedOverlayId === overlayFrame.overlay.id" in preview
    assert "? 100 : 40" in preview
    assert 'objectName: "overlayTopRightResizeHandle"' in preview
    assert 'objectName: "overlayBottomLeftResizeHandle"' in preview
    assert "function compactHandleCorner()" in preview


def test_translation_style_rules_use_multiline_editor_and_serialized_save() -> None:
    editor = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "SubtitleEditorScreen.qml"
    ).read_text(encoding="utf-8")

    assert "id: translationRulesScroll" in editor
    assert "id: translationRules" in editor
    assert "wrapMode: TextEdit.Wrap" in editor
    assert "if (text.length <= 4000) return" in editor
    assert "!root.appModel.translationPreferencesBusy" in editor
    assert 'runningText: "Đang lưu…"' in editor
    assert "id: translationStyleName" in editor
    assert "id: savedTranslationStyleBox" in editor
    assert "saveTranslationStyle" in editor
    assert "loadSavedTranslationStyle" in editor


def test_shared_buttons_expose_text_states_and_top_center_feedback() -> None:
    root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    button = (root / "controls" / "DsButton.qml").read_text(encoding="utf-8")
    toast = (root / "controls" / "ActionToast.qml").read_text(encoding="utf-8")
    feedback = (root / "DucaiStudio" / "UiFeedback.qml").read_text(encoding="utf-8")
    main = (root / "Main.qml").read_text(encoding="utf-8")
    subtitle = (root / "screens" / "SubtitleEditorScreen.qml").read_text(encoding="utf-8")

    assert "readonly property string displayText" in button
    assert "runningText" in button
    assert "pausingText" in button
    assert "cancellingText" in button
    assert "UiFeedback.show" in button
    assert "property bool warning" in button
    assert "durationMs: 4000" in feedback
    assert "ParallelAnimation" in toast
    assert "anchors.horizontalCenter: parent.horizontalCenter" in toast
    assert "ActionToast" in main
    assert "root.appModel.setOcrExtractionMethod" in subtitle
    assert '"OCR khung hình", "Whisper âm thanh"' in subtitle
    assert "root.appModel.toggleOcr(" in subtitle
    assert 'runningText: "Tạm dừng dịch"' in subtitle
    assert "warning: true" in subtitle
    assert subtitle.count("danger: true") >= 2


def test_subtitle_style_and_video_filters_are_real_preview_controls() -> None:
    root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    subtitle = (root / "screens" / "SubtitleEditorScreen.qml").read_text(encoding="utf-8")
    export = (root / "controls" / "ExportSettingsPanel.qml").read_text(encoding="utf-8")
    preview = (root / "controls" / "TimelinePreview.qml").read_text(encoding="utf-8")

    assert 'title: "Chọn màu chữ phụ đề"' in subtitle
    assert 'title: "Chọn màu viền phụ đề"' in subtitle
    assert "setExportSubtitleOpacityPercent" in subtitle
    assert 'objectName: "liveSubtitlePreview"' in preview
    assert "currentProject.subtitleAt(playhead)" in preview
    assert "exportSubtitlePrimaryColor" in preview
    assert "exportSubtitleOpacityPercent" in preview
    assert 'SectionTitle { id: filterTitle; text: "Filter màu" }' in export
    assert "setExportVideoFilterPreset" in export
    assert "id: filteredVideo" in preview
    assert "colorization: root.filterColorization()" in preview
    assert "function exportCanvasSize()" in preview
    assert "VideoOutput.Stretch" in preview
    assert "fillMode: Image.Stretch" in preview
    assert 'objectName: "liveSubtitlePreview"' in preview
    artwork = (root.parents[1] / "providers" / "subtitle_artwork.py").read_text(encoding="utf-8")
    assert "root.artworkForCue(root.activeSubtitle)" in preview
    assert "style.outline_width" in artwork
    assert "style.opacity_percent / 100" in artwork


def test_scene_text_preview_conceals_glyphs_and_preserves_review_cues() -> None:
    project_root = Path(__file__).parents[2]
    preview = (
        project_root / "src" / "ducai_studio" / "ui" / "qml" / "controls" / "TimelinePreview.qml"
    ).read_text(encoding="utf-8")
    fonts = project_root / "src" / "ducai_studio" / "ui" / "assets" / "fonts"

    assert "currentProject.sceneTextsAt(playhead)" in preview
    assert "root.sceneGlyphRect(modelData)" in preview
    assert "root.scenePanelRect(modelData)" in preview
    assert 'String(modelData.review_state || "accepted") === "accepted"' in preview

    editor = (
        project_root
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "SubtitleEditorScreen.qml"
    ).read_text(encoding="utf-8")
    assert "Chấp nhận chữ trong cảnh" in editor
    assert "acceptSceneTextCue" in editor
    assert ') + " · CẦN DUYỆT"' in preview
    pipeline = (project_root / "src/ducai_studio/application/export_pipeline.py").read_text(
        encoding="utf-8"
    )
    assert '"calligraphy": "Playwrite VN"' in pipeline
    for key, family in {
        "calligraphy_soft": "Dancing Script",
        "calligraphy_bold": "Lobster",
        "calligraphy_ink": "Agbalumo",
        "calligraphy_brush": "Comforter Brush",
        "calligraphy_water": "Water Brush",
        "calligraphy_splash": "Splash",
        "calligraphy_hurricane": "Hurricane",
    }.items():
        assert f'"{key}": "{family}"' in pipeline
    assert (fonts / "PlaywriteVN-Variable.ttf").is_file()
    assert (fonts / "PLAYWRITE-VN-OFL.txt").is_file()


def test_subtitle_editor_confirms_vietnamese_replace_and_right_click_delete() -> None:
    subtitle = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "SubtitleEditorScreen.qml"
    ).read_text(encoding="utf-8")

    assert 'placeholderText: "Tìm trong bản dịch Việt"' in subtitle
    assert 'title: "Thay tất cả trong bản dịch Việt?"' in subtitle
    assert "previewSubtitleReplacement" in subtitle
    assert "findReplaceSubtitles(find, replacement, count)" in subtitle
    assert "acceptedButtons: Qt.RightButton" in subtitle
    # TextArea owns the native secondary-button menu. Each editor therefore
    # lives in a host Item with a sibling right-button catcher above it, so
    # native Qt actions cannot leak alongside the cue menu.
    assert "id: sourceEditorHost" in subtitle
    assert "id: translatedEditorHost" in subtitle
    assert "id: sourceContextCatcher" in subtitle
    assert "id: translatedContextCatcher" in subtitle
    assert subtitle.count("MouseArea {") >= 2
    assert subtitle.count("acceptedButtons: Qt.RightButton") >= 3
    assert subtitle.count("propagateComposedEvents: false") >= 2
    assert subtitle.count("root.openCueContext(") >= 3
    assert 'text: "Xóa cue này"' in subtitle
    assert "deleteSubtitleCue(cueId)" in subtitle
    assert "Cả dòng nguồn và dòng dịch Việt tương ứng" in subtitle
    qa_workspace_condition = (
        'root.workspaceMode === "translate"\n'
        '                                || root.workspaceMode === "subtitles"'
    )
    assert qa_workspace_condition in subtitle


def test_timeline_has_resizable_preview_zoom_slider_and_preview_transport_shortcuts() -> None:
    root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    timeline = (root / "screens" / "TimelineScreen.qml").read_text(encoding="utf-8")
    preview = (root / "controls" / "TimelinePreview.qml").read_text(encoding="utf-8")

    assert '"timelinePreviewRatio"' in timeline
    assert "orientation: Qt.Vertical" in timeline
    assert "id: zoomSlider" in timeline
    assert "onActivated: root.seekRelative(-root.appModel.timelineSeekStepMs)" in preview
    assert "onActivated: root.seekRelative(root.appModel.timelineSeekStepMs)" in preview
    assert 'iconName: "chevron-left"' in preview
    assert 'iconName: "chevron-right"' in preview
    assert 'tooltipText: "Lùi " + root.appModel.timelineSeekStepMs + " ms"' in preview
    assert 'tooltipText: "Tiến " + root.appModel.timelineSeekStepMs + " ms"' in preview
    assert "anchors.bottom: transport.top" in preview
    assert 'objectName: "previewTransport"' in preview
    assert "color: Theme.surfacePanel" in preview
    assert "id: sceneGlyphCover" not in preview
    assert preview.index("z: 110") < preview.index(
        "model: root.appModel.currentProject.overlayModel"
    )
    assert "z: 120" in preview
    assert 'text: "Phụ đề"' in timeline
    assert "model: root.appModel.currentProject.subtitleCueModel" in timeline
    assert "root.appModel.selectCue(" in timeline
    assert "function onOverlayGeometrySettled" in preview


def test_timeline_scissors_replace_all_retention_controls_and_open_text_inspector() -> None:
    root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    timeline = (root / "screens" / "TimelineScreen.qml").read_text(encoding="utf-8")
    toolbar = (root / "controls" / "SubtitleTextToolbar.qml").read_text(encoding="utf-8")
    for removed in ("cutPanel", "cutMarker", "cutPreviewOperation", "CutMarkerHandle",
                    "Giữ từ đây", "Giữ đến đây", "Giữ đoạn A", "onHoveredChanged",
                    "applyTimelineCutAction"):
        assert removed not in timeline
    assert 'objectName: "timelineScissorsButton"' in timeline
    assert 'iconName: "scissors"' in timeline
    for key in ("X", "Delete", "Z", "Y"):
        assert f"event.key === Qt.Key_{key}" in timeline
    assert "Keys.priority: Keys.AfterItem" in timeline
    assert "root.appModel.splitVideoAtPlayhead()" in timeline
    assert "root.appModel.deleteTimelineSelection()" in timeline
    assert 'objectName: "timelineTextInspectorTab"' in timeline
    assert 'inspectorMode: true' in timeline
    assert 'root.selectInspectorTab("text")' in timeline
    assert timeline.index("id: inspector") < timeline.index("SubtitleTextToolbar {")
    assert "advancedPopup" not in toolbar and "Nâng cao" not in toolbar
    assert "ScrollView {" in toolbar and "inputCueId" in toolbar
    assert 'columns: root.inspectorMode ? 2 : 6' in toolbar


def test_timeline_clip_decimal_fields_are_locale_independent() -> None:
    timeline = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "TimelineScreen.qml"
    ).read_text(encoding="utf-8")

    assert "function parseDecimalText(value)" in timeline
    assert 'String(value).trim().replace(",", ".")' in timeline
    assert "function decimalTextInRange(value, minimum, maximum)" in timeline
    assert timeline.count("regularExpression: /^[0-9]+([\\.,][0-9]{0,2})?$/") == 2
    assert "speedField.valueIsValid && !root.appModel.timelineBusy" in timeline
    assert "volumeField.valueIsValid && !root.appModel.timelineBusy" in timeline
    assert "speedField.numericValue, linkedCheck.checked" in timeline
    assert "root.appModel.setSelectedVolume(volumeField.numericValue)" in timeline


def test_timeline_track_selection_is_limited_to_header_and_clip_selection_is_independent() -> None:
    timeline = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "TimelineScreen.qml"
    ).read_text(encoding="utf-8")

    assert 'objectName: "timelineTrackHeaderMouseArea"' in timeline
    assert "width: 188" in timeline
    assert "root.appModel.selectClip(clipItem.model.clipId)" in timeline


def test_timeline_voice_sync_uses_the_current_seek_anchor_and_skips_paused_players() -> None:
    preview = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "controls"
        / "TimelinePreview.qml"
    ).read_text(encoding="utf-8")

    assert "function expectedVoicePositionMs(descriptor, videoPositionMs)" in preview
    assert "videoPositionMs - descriptor.sourcePositionMs" in preview
    assert "root.expectedVoicePositionMs(" in preview
    assert "root.activeDescriptor, position" in preview
    assert "playbackState === MediaPlayer.PlayingState" in preview
    assert "voicePlayer.playbackState === MediaPlayer.PlayingState" in preview
    assert "Math.abs(voicePlayer.position - expectedVoiceMs) > 200" in preview


def test_translation_review_state_exposes_cache_qa_retry() -> None:
    qml = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "SubtitleEditorScreen.qml"
    ).read_text(encoding="utf-8")

    assert 'translationJobState === "review"' in qml
    assert 'translationJobState === "cancelled"' in qml
    assert 'cancelledText: "Tiếp tục dịch từ cache"' in qml
    assert 'failedText: "Tiếp tục dịch từ cache"' in qml
    assert "root.appModel.toggleTranslationWithStyle(" in qml
    assert "root.translationStyleIds[translationStyleBox.currentIndex]" in qml
    assert "&& !root.appModel.translationPreferencesBusy" in qml
    assert '"cancelled": "Đã hủy — có thể tiếp tục từ cache"' in qml
    assert '"Đang tiếp tục dịch…"' in qml


def test_subtitle_workspace_hides_engine_notes_and_web_session_switches() -> None:
    project_root = Path(__file__).parents[2]
    qml = (
        project_root
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "SubtitleEditorScreen.qml"
    ).read_text(encoding="utf-8")

    assert '"Tốc độ quét"' in qml
    assert "Paddle Fast · Quality cứu lỗi · Rapid dự phòng" not in qml
    assert "checkpoint an toàn" not in qml
    assert "Web nổi:" not in qml
    assert "Guest:" not in qml
    assert "workspaceDescription" not in qml
    assert "visible: providerBox.currentIndex !== 0" in qml

    main_qml = (project_root / "src" / "ducai_studio" / "ui" / "qml" / "Main.qml").read_text(
        encoding="utf-8"
    )
    view_model = (
        project_root / "src" / "ducai_studio" / "ui" / "viewmodels" / "application_view_model.py"
    ).read_text(encoding="utf-8")
    gemini_web = (
        project_root / "src" / "ducai_studio" / "providers" / "translation" / "gemini_web.py"
    ).read_text(encoding="utf-8")

    assert "window.publicStatusMessage(window.appModel.statusMessage)" in main_qml
    assert "Đang tạo tác vụ Paddle Fast + Quality rescue" not in view_model
    assert "Đang nạp Paddle OCR; giao diện vẫn có thể sử dụng" not in view_model
    assert "bật Web nổi" not in gemini_web


def test_projects_screen_delete_is_confirmed_and_effects_support_added_blur() -> None:
    root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    projects = (root / "screens" / "ProjectsScreen.qml").read_text(encoding="utf-8")
    export = (root / "controls" / "ExportSettingsPanel.qml").read_text(encoding="utf-8")

    assert 'text: "Xóa dự án"' in projects
    assert "deleteProjectDialog.close()" in projects
    assert "root.appModel.deleteProject" in projects
    assert "root.appModel.addBlurRegion()" in export
    assert "root.appModel.deleteOverlay" in export
    timeline = (root / "screens" / "TimelineScreen.qml").read_text(encoding="utf-8")
    assert "editableProjectOverlays:" in timeline


def test_voice_studio_uses_automatic_timing_repair_and_exposes_speed_controls() -> None:
    screens = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml" / "screens"
    qml = (screens / "VoiceStudioScreen.qml").read_text(encoding="utf-8")
    editor = (screens / "SubtitleEditorScreen.qml").read_text(encoding="utf-8")

    assert "voiceTimingAutoRepairBusy" in qml
    assert "function productionVoiceAction()" in qml
    assert "tốc độ tổng cuối cùng, không cộng dồn với tốc độ gốc" in qml
    assert "root.appModel.voiceReadSpeed" in qml
    assert "root.appModel.voiceCueMaxSpeed" in qml
    assert "from: 1.1" in qml
    assert "adjusted_cue_count" in qml
    assert '"Tạo toàn bộ"' in qml
    assert '"Tạo preview"' in qml
    assert 'text: "Bỏ qua cảnh báo · giữ nguyên voice"' in editor
    assert "root.appModel.addCurrentVoiceToTimeline()" in editor
    assert 'text: "Cue dài"' in editor
    assert "root.appModel.toggleOcr(" in editor


def test_voice_studio_exposes_post_dubbing_conform_controls() -> None:
    qml = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "VoiceStudioScreen.qml"
    ).read_text(encoding="utf-8")

    assert 'text: "Đồng bộ sau lồng tiếng"' in qml
    assert "visible: root.appModel.currentProject.hasVoiceTrack" in qml
    assert 'setVoiceConformMode("stretch_video")' in qml
    assert 'setVoiceConformMode("speed_audio")' in qml
    assert 'setVoiceConformMode("hybrid")' in qml
    assert "ButtonGroup.group: conformModeGroup" in qml
    assert "from: 1.00" in qml
    assert "to: 2.00" in qml
    assert "stepSize: 0.05" in qml
    assert "function conformMaxAudioSpeed()" in qml
    assert "appModel.voiceConformMaxAudioSpeed" in qml
    assert "? value : 1.25" in qml
    assert "setVoiceConformMaxAudioSpeed(" in qml
    assert 'conformEstimateValue("adjusted_cue_count"' in qml
    assert 'conformEstimateValue("added_duration_us"' in qml
    assert 'conformEstimateValue("output_duration_us"' in qml
    assert 'conformEstimateValue("min_audio_speed"' in qml
    assert 'conformEstimateValue("max_audio_speed"' in qml
    assert 'conformEstimateValue("min_video_speed"' in qml
    assert 'conformEstimateValue("max_video_speed"' in qml
    assert 'conformEstimateValue("unresolved_count"' in qml
    assert 'conformEstimateValue("issues"' in qml
    assert "function hasConformIssueTarget()" in qml
    assert "Ước lượng chưa cung cấp cue để điều hướng." in qml
    assert "root.appModel.voiceConformCanApply" in qml
    assert "root.appModel.voiceConformApplied" in qml
    assert "root.appModel.voiceConformBusy" in qml
    assert "root.appModel.voiceConformJobState" in qml
    assert '["failed", "interrupted", "review"]' in qml
    assert "refreshVoiceConformEstimate()" in qml
    assert "applyVoiceConform()" in qml
    assert "resetVoiceConform()" in qml
    assert "selectVoiceConformIssue(Number(issues[0].id))" in qml


def test_timeline_exposes_timestamped_per_cue_voice_sync() -> None:
    qml = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "TimelineScreen.qml"
    ).read_text(encoding="utf-8")

    assert 'text: "Khớp D1"' in qml
    assert 'tooltipText: "Mở công cụ cân thời lượng từng cue D1 theo mốc SRT."' in qml
    assert 'root.selectInspectorTab("voice-sync")' in qml
    assert "currentProject.voiceConformCueModel.setViewport" in qml
    assert "model: root.appModel.currentProject.voiceConformCueModel" in qml
    assert 'setVoiceConformMode("speed_audio")' in qml
    assert "root.appModel.voiceConformMaxAudioSpeed" in qml
    assert "root.appModel.refreshVoiceConformEstimate()" in qml
    assert "root.appModel.applyVoiceConform()" in qml
    assert "voiceCueItem.model.sourceStartUs" in qml
    assert "voiceCueItem.model.audioSpeed" in qml
    assert "voiceConformCueModel.totalCount > 0" in qml
    assert "selectVoiceConformIssue(voiceCueItem.model.cueIndex)" in qml
    assert "onDoubleClicked" in qml
    assert "openCueInEditor" in qml


def test_timeline_voice_resize_handles_leave_a_double_click_target() -> None:
    qml = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "TimelineScreen.qml"
    ).read_text(encoding="utf-8")

    # The hit area is deliberately wider for easier grabbing, while the one-third
    # cap keeps a centre target available on short cues for click/double-click.
    assert qml.count("width: Math.min(14, parent.width / 3)") == 2
    assert "width: 10\n" not in qml
    assert 'objectName: "timelineClipBody-" + clipItem.model.clipId' in qml


def test_timeline_exposes_multicue_scene_layers_text_tool_and_marquee() -> None:
    qml_root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    timeline = (qml_root / "screens" / "TimelineScreen.qml").read_text(encoding="utf-8")
    preview = (qml_root / "controls" / "TimelinePreview.qml").read_text(encoding="utf-8")
    toolbar = (qml_root / "controls" / "SubtitleTextToolbar.qml").read_text(encoding="utf-8")
    editor = (qml_root / "screens" / "SubtitleEditorScreen.qml").read_text(encoding="utf-8")
    manual_dialog = (qml_root / "controls" / "ManualSubtitleCueDialog.qml").read_text(
        encoding="utf-8"
    )

    assert 'text: "T"' in timeline
    assert "id: manualCueDialog" in timeline
    assert "id: subtitleMarqueeArea" in timeline
    assert "subtitleCueIdsInRange(" in timeline
    assert "selectSubtitleCueIds(ids, additive)" in timeline
    assert "moveSelectedSceneTexts(" in timeline
    assert "previewSelectedSceneTextMove" in timeline
    assert "!root.cueSelected(clipItem.model.cueId)" in timeline
    assert "property bool selectionHandledOnPress" in timeline
    assert "if (selectionHandledOnPress" in timeline
    assert "root.appModel.selectTimelineClip(" in timeline
    assert "&& root.sceneTimingLaneDelta !== 0)" in timeline
    assert "manualCueDialog.openForPlayhead(root.appModel.playheadUs)" in timeline
    assert "manualCueDialog.submitAtRegion(x, y, width, height)" in timeline
    assert "startUs + durationSeconds * 1000000" in manual_dialog
    assert "projectEndUs - 100000" not in timeline
    assert "isSceneTextCue(" not in timeline
    assert "root.sceneTimingMinimumLaneDelta, rawLaneDelta" in timeline
    assert "root.seekTimelineAt(mouse.x)" in timeline
    assert "root.seekTimelineAt(point.x)" not in timeline
    assert "root.appModel.clearTimelineCueSelection()" in timeline
    assert "root.appModel.selectTimelineCue(" in timeline
    assert "root.appModel.previewSelectedSceneTextResize(" in timeline
    assert "root.appModel.resizeSelectedSceneTexts(" in timeline
    assert "setTrackViewport(" in timeline
    assert "id: subtitleTextToolbar" in timeline
    assert "enabled: !root.appModel.timelineBusy" in toolbar
    assert "root.appModel.subtitleBusy || root.appModel.timelineBusy" in toolbar
    assert "visible: root.appModel.selectedCueCount > 0" not in timeline
    assert "transformSelectedSubtitleGeometry" in preview
    assert "subtitleGeometryPending" in preview
    assert preview.count("property bool selectedOnPress") >= 2
    assert "property string editTargetCueId" in editor
    assert editor.count("restoreMode: Binding.RestoreNone") == 2
    assert "selectCueWithAdditive(" in editor
    assert "selectCueWithAdditive(" not in timeline
    assert "selectCueWithAdditive(" not in preview
    assert "root.appModel.selectTimelineCue(" in preview
    assert "onSubtitleGeometrySettled" in preview
    assert "onActiveSceneTextsChanged: root.cancelHiddenSubtitleGeometry()" in preview
    assert "onCanceled: root.finishSubtitleGeometry()" not in preview
    assert "ScrollBar.vertical: ScrollBar" in timeline
    assert "ensureSelectedCueTrackVisible" not in timeline
    assert "selectSubtitleCueIds([], false)" not in timeline
    stable_editor_commit = (
        "root.appModel.updateSubtitleText(\n"
        "                                                    editTargetCueId"
    )
    assert stable_editor_commit in editor
    assert '" cue · phóng to để xem chữ"' in timeline


def test_translation_editor_plain_click_collapses_selection_and_exposes_manual_text() -> None:
    editor = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "SubtitleEditorScreen.qml"
    ).read_text(encoding="utf-8")

    assert "SubtitleTextToolbar {" not in editor
    assert 'text: "Thêm cue"' in editor
    assert "root.appModel.updateSubtitleText(" in editor
    assert "acceptedModifiers: Qt.NoModifier" in editor
    assert "acceptedModifiers: Qt.ControlModifier" in editor
    assert editor.count("root.appModel.selectCueWithModifiers(") >= 2
    assert "Boolean(point.modifiers & Qt.ControlModifier)" not in editor


def test_timeline_voice_resize_is_local_until_release_and_has_visual_snap_guides() -> None:
    qml = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "TimelineScreen.qml"
    ).read_text(encoding="utf-8")

    assert "function beginClipResize(" in qml
    assert "function updateClipResize(pointerX)" in qml
    assert "function finishClipResize()" in qml
    assert qml.count("root.appModel.resizeSelectedClip(") == 1
    assert qml.count("root.updateClipResize(point.x)") == 2
    # Cue body/handles also sample the final release position.
    assert qml.count("mapToItem(clipLayer, mouse.x, mouse.y)") == 9
    assert "id: resizeVerticalGuide" in qml
    assert "id: resizeDistanceGuide" in qml
    assert "id: resizeGuideLabel" in qml
    assert "Math.min(500_000, Math.max(80_000, eightPixelsUs))" in qml
    assert "Math.ceil(root.clipResizeSourceDurationUs / 3.0)" in qml
    assert "root.clipResizeActive || root.sceneTimingActive || root.clipMoveActive" in qml
    assert "Keys.priority: Keys.AfterItem" in qml
    assert 'state === "overflow"' in qml
    assert 'state === "underflow" || state === "position_mismatch"' in qml
    assert 'state === "matched"' in qml


def test_voice_players_release_the_active_d1_before_manual_rerun() -> None:
    qml_root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    voice = (qml_root / "screens" / "VoiceStudioScreen.qml").read_text(encoding="utf-8")
    timeline = (qml_root / "controls" / "TimelinePreview.qml").read_text(encoding="utf-8")
    multi = (qml_root / "screens" / "MultiVideoScreen.qml").read_text(encoding="utf-8")

    assert "voicePlaybackEnabled" in voice
    assert "onVoicePlaybackEnabledChanged" in voice
    assert "productionPlayer.stop()" in voice
    assert "voicePlaybackEnabled" in timeline
    assert "onVoicePlaybackEnabledChanged" in timeline
    assert "onVoicePlaybackEnabledChanged" in multi


def test_timeline_preserves_vertical_track_scroll_across_model_refresh() -> None:
    qml = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "TimelineScreen.qml"
    ).read_text(encoding="utf-8")

    assert "property real contentYBeforeModelReset: 0" in qml
    assert "function onModelAboutToBeReset()" in qml
    assert "trackList.contentYBeforeModelReset = trackList.contentY" in qml
    assert "function onModelReset()" in qml
    assert "trackList.contentHeight - trackList.height" in qml
    assert "trackList.modelResetInProgress = false" in qml
    assert "positionViewAtIndex" not in qml
    assert "id: timelineViewportTimer" in qml
    assert "onTriggered: root.syncTimelineViewportModels()" in qml
    assert "if (!timelineViewportTimer.running) timelineViewportTimer.start()" in qml


def test_preview_playback_toggle_color_picker_and_scene_snap_are_direct() -> None:
    qml_root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    preview = (qml_root / "controls" / "TimelinePreview.qml").read_text(encoding="utf-8")
    timeline = (qml_root / "screens" / "TimelineScreen.qml").read_text(encoding="utf-8")
    toolbar = (qml_root / "controls" / "SubtitleTextToolbar.qml").read_text(encoding="utf-8")

    assert "function togglePlayback()" in preview
    assert "id: videoPlaybackToggleArea" in preview
    assert "id: previewColorPicker" in preview
    assert "filteredVideo.grabToImage" in preview
    assert "samplePreviewColor(" in preview
    assert "signal colorPickRequested(string target)" in toolbar
    assert "applyPickedColor" in toolbar
    assert 'sequence: "Space"' in preview
    assert "!root.textEditorHasFocus()" in preview
    assert "sceneTimingSnapActive" in timeline
    assert "Math.min(500_000, 8 * root.viewportDurationUs" in timeline
    assert "sceneTimingSnapStartEligible" in timeline
    assert "onSceneTimingSettled(generation, succeeded)" in timeline
    assert "id: sceneTimingSnapGuide" in timeline


def test_global_status_notifications_are_readable_and_dismissible() -> None:
    root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui" / "qml"
    main = (root / "Main.qml").read_text(encoding="utf-8")
    feedback = (root / "DucaiStudio" / "UiFeedback.qml").read_text(encoding="utf-8")
    toast = (root / "controls" / "ActionToast.qml").read_text(encoding="utf-8")

    assert "onStatusNotificationRequested" in main
    assert "Math.min(10000" in feedback
    assert "function dismiss()" in toast
    assert 'Accessible.name: "Đóng thông báo"' in toast


def test_voice_studio_has_one_stateful_production_action_without_manual_repair_clutter() -> None:
    qml = (
        Path(__file__).parents[2]
        / "src"
        / "ducai_studio"
        / "ui"
        / "qml"
        / "screens"
        / "VoiceStudioScreen.qml"
    ).read_text(encoding="utf-8")

    assert "function productionVoiceAction()" in qml
    assert "root.appModel.ttsJobPurpose" in qml
    assert 'runningText: "Đang tạo preview…"' in qml
    assert 'runningText: "Đang tạo toàn bộ…"' in qml
    assert 'text: "Rút gọn ngay"' not in qml
    assert 'text: "Tạo lại cue đã chọn"' not in qml
    assert 'text: "Tiếp tục cache"' not in qml


def test_ui_resource_root_prefers_source_tree_then_frozen_layout(tmp_path: Path) -> None:
    source = tmp_path / "source" / "app.py"
    source.parent.mkdir()
    source.write_text("", encoding="utf-8")
    executable = tmp_path / "release" / "DucaiStudio2.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"")
    frozen_qml = executable.parent / "ui" / "qml" / "Main.qml"
    frozen_qml.parent.mkdir(parents=True)
    frozen_qml.write_text("Item {}", encoding="utf-8")

    assert _ui_resource_root(module_path=source, executable_path=executable) == (
        executable.parent / "ui"
    )

    source_qml = source.parent / "qml" / "Main.qml"
    source_qml.parent.mkdir()
    source_qml.write_text("Item {}", encoding="utf-8")
    assert _ui_resource_root(module_path=source, executable_path=executable) == source.parent


def test_ui_resource_root_fails_closed_when_package_is_incomplete(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="QML"):
        _ui_resource_root(
            module_path=tmp_path / "missing" / "app.py",
            executable_path=tmp_path / "missing" / "DucaiStudio2.exe",
        )


def test_multi_video_has_dedicated_video_import_and_restore_controls() -> None:
    root = Path(__file__).parents[2] / "src" / "ducai_studio" / "ui"
    main = (root / "qml" / "Main.qml").read_text(encoding="utf-8")
    multi = (root / "qml" / "screens" / "MultiVideoScreen.qml").read_text(encoding="utf-8")
    view_model = (root / "viewmodels" / "application_view_model.py").read_text(encoding="utf-8")

    assert 'title: "Nhập video vào Multi video"' in main
    assert 'nameFilters: ["Video (*.mp4 *.mkv *.mov *.avi *.webm)"]' in main
    assert "importMultiPaths(values)" in main
    assert 'text: "Nhập video"' in multi
    assert "showMultiImportDialog()" in multi
    assert "Khôi phục đã ẩn" in multi
    assert 'Accessible.name: "Chọn video "' in multi
    assert "def importMultiPaths" in view_model
    assert "def restoreHiddenMultiVideos" in view_model
