pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Effects
import QtQuick.Layouts
import QtMultimedia
import DucaiStudio 1.0

Item {
    id: root
    objectName: "timelinePreview"

    required property var appModel
    property bool showProjectOverlays: false
    property bool editableOverlays: false
    property bool editableProjectOverlays: false
    property bool videoContextMenuEnabled: false
    signal videoContextMenuRequested(real x, real y)
    property string selectedOverlayId: "overlay-default-logo"
    // Optional editing mode used by the extraction/subtitle workspace. The
    // paused, painted video itself is the coordinate authority.
    property string ocrRegionEditorMode: ""
    property point regionPressPoint: Qt.point(0, 0)
    property rect regionDraftRect: Qt.rect(0, 0, 0, 0)
    property var subtitleMenuCue: ({})
    property bool subtitleGeometryActive: false
    property bool subtitleGeometryPending: false
    property string subtitleGeometryCueId: ""
    property string subtitleGeometryMode: ""
    property point subtitleGeometryPress: Qt.point(0, 0)
    property size subtitleGeometryBaseSize: Qt.size(1, 1)
    property var subtitleGeometrySeedArtwork: ({})
    readonly property var selectionIds: root.appModel.selectedCueIds
    property real subtitleGeometryDeltaX: 0
    property real subtitleGeometryDeltaY: 0
    property real subtitleGeometryScaleX: 1
    property real subtitleGeometryScaleY: 1
    property string colorPickerTarget: ""
    signal colorPicked(string target, string color)
    signal regionDrawn(string mode, real x, real y, real width, real height)
    property string errorText: ""
    property var activeDescriptor: ({"available": false})
    property real pendingSourcePositionMs: -1
    property bool pendingPlay: false
    property bool applyingTimelineSeek: false
    property real pendingVoicePositionMs: -1
    property bool pendingVoicePlay: false
    property bool updatingPlayheadFromPlayer: false
    // Playback chrome has its own space below the video; it never hides text.
    property bool controlsVisible: true
    property bool viewportHovered: false
    property bool transportHovered: false
    readonly property bool controlsHovered: viewportHovered || transportHovered
    property bool volumeHovered: false
    readonly property int playbackState: player.playbackState
    readonly property bool playing: player.playbackState === MediaPlayer.PlayingState
    readonly property int voicePlaybackState: voicePlayer.playbackState
    readonly property int mediaStatus: player.mediaStatus
    readonly property real positionMs: root.appModel.playheadUs / 1000
    readonly property real sourcePositionMs: player.position
    readonly property real voicePositionMs: voicePlayer.position
    readonly property real durationMs: root.appModel.currentProject.durationUs / 1000
    readonly property bool muted: audioOutput.muted && voiceAudio.muted
    readonly property real masterVolume: root.appModel.previewMasterVolumePercent / 100
    property var activeSubtitle: {
        if (!root.visible) return ({})
        const playhead = root.appModel.playheadUs
        const revision = root.appModel.currentProject.revision
        const visualRevision = root.appModel.currentProject.subtitleVisualRevision
        return root.appModel.currentProject.subtitleAt(playhead)
    }
    property var activeSubtitleRegions: {
        if (!root.visible) return []
        const playhead = root.appModel.playheadUs
        const revision = root.appModel.currentProject.revision
        const visualRevision = root.appModel.currentProject.subtitleVisualRevision
        return root.appModel.currentProject.sourceSubtitleRegionsAt(playhead)
    }
    property var activeSceneTexts: {
        if (!root.visible) return []
        const playhead = root.appModel.playheadUs
        const revision = root.appModel.currentProject.revision
        const visualRevision = root.appModel.currentProject.subtitleVisualRevision
        return root.appModel.currentProject.sceneTextsAt(playhead)
    }
    property var activeTitleTexts: {
        if (!root.visible) return []
        const playhead = root.appModel.playheadUs
        const revision = root.appModel.currentProject.revision
        const visualRevision = root.appModel.currentProject.subtitleVisualRevision
        return root.appModel.currentProject.titleTextsAt(playhead)
    }
    readonly property string activeSubtitleText: activeSubtitle.translated_text
        || activeSubtitle.source_text || ""
    property var blurOverlay: {
        const revision = root.appModel.currentProject.revision
        const stored = root.appModel.currentProject.overlay("overlay-default-blur")
        return Object.assign({id: "overlay-default-blur", kind: "blur",
            range: {start_us: 0, end_us: root.appModel.currentProject.durationUs},
            blur_radius: 22, opacity: 1, padding_x: 0.012, padding_y: 0.008,
            concealment_mode: "blur"}, stored, {
            enabled: root.appModel.exportSubtitleBackgroundEnabled
                && root.appModel.exportSubtitleBackgroundMode === "blur",
            tracking_mode: "source_subtitle"
        })
    }
    property var logoOverlay: {
        const revision = root.appModel.currentProject.revision
        return root.appModel.currentProject.overlay("overlay-default-logo")
    }

    function overlayNumber(overlay, key, fallback) {
        if (!overlay || overlay[key] === undefined || overlay[key] === null)
            return fallback
        const value = Number(overlay[key])
        return Number.isFinite(value) ? value : fallback
    }

    function exportCanvasSize() {
        const preset = root.appModel.exportPresetId
        if (preset === "1080p") return Qt.size(1920, 1080)
        if (preset === "720p") return Qt.size(1280, 720)
        if (preset === "480p") return Qt.size(854, 480)
        const source = videoOutput.sourceRect
        if (source && source.width > 0 && source.height > 0)
            return Qt.size(source.width, source.height)
        return Qt.size(1920, 1080)
    }

    function renderedVideoRect() {
        const canvas = exportCanvasSize()
        const aspect = canvas.width / Math.max(1, canvas.height)
        let width = videoViewport.width
        let height = width / aspect
        if (height > videoViewport.height) {
            height = videoViewport.height
            width = height * aspect
        }
        return Qt.rect(
            (videoViewport.width - width) / 2,
            (videoViewport.height - height) / 2,
            width,
            height
        )
    }

    function overlayX(overlay) {
        const rect = renderedVideoRect()
        return rect.x + rect.width * overlayNumber(overlay, "x", 0)
    }

    function overlayY(overlay) {
        const rect = renderedVideoRect()
        return rect.y + rect.height * overlayNumber(overlay, "y", 0)
    }

    function overlayWidth(overlay) {
        return renderedVideoRect().width * overlayNumber(overlay, "width", 0)
    }

    function overlayHeight(overlay) {
        return renderedVideoRect().height * overlayNumber(overlay, "height", 0)
    }

    function overlayActive(overlay) {
        if (!root.showProjectOverlays || !overlay || !overlay.enabled) return false
        const range = overlay.range || ({})
        const startUs = overlayNumber(range, "start_us", 0)
        const endUs = overlayNumber(range, "end_us", root.appModel.currentProject.durationUs)
        const withinRange = root.appModel.playheadUs >= startUs
            && root.appModel.playheadUs < endUs
        if (!withinRange) return false
        if (overlay.kind === "blur" && overlay.tracking_mode === "source_subtitle")
            return Boolean(root.activeSubtitleRegions && root.activeSubtitleRegions.length > 0)
        return true
    }

    function sceneFadeOpacity(cue) {
        if (!cue || !Boolean(cue.fade_enabled)) return 1.0
        const startUs = Number(cue.start_us || 0)
        const endUs = Number(cue.end_us || startUs)
        const durationUs = Math.max(1, endUs - startUs)
        const requestedUs = Math.max(50000, Number(cue.fade_duration_ms || 300) * 1000)
        const fadeUs = Math.min(requestedUs, durationUs / 2)
        if (fadeUs <= 0) return 1.0
        const playheadUs = Number(root.appModel.playheadUs || 0)
        const entering = Math.max(0, Math.min(1, (playheadUs - startUs) / fadeUs))
        const leaving = Math.max(0, Math.min(1, (endUs - playheadUs) / fadeUs))
        return Math.min(entering, leaving)
    }

    function trackedSubtitleBlur(overlay, activeCue) {
        if (!overlay || overlay.tracking_mode !== "source_subtitle") return overlay
        const cue = activeCue || ({})
        const canvas = root.exportCanvasSize()
        const artwork = root.artworkForCue(cue)
        if (artwork.concealment_rect || artwork.text_rect) {
            const rect = artwork.concealment_rect || artwork.text_rect
            const padX = root.overlayNumber(overlay, "padding_x", 0.012)
            const padY = root.overlayNumber(overlay, "padding_y", 0.008)
            const left = Math.max(0, rect[0] / canvas.width - padX)
            const top = Math.max(0, rect[1] / canvas.height - padY)
            const right = Math.min(1, (rect[0] + rect[2]) / canvas.width + padX)
            const bottom = Math.min(1, (rect[1] + rect[3]) / canvas.height + padY)
            return Object.assign({}, overlay, {
                "x": left, "y": top, "width": right - left, "height": bottom - top,
                "region_source": "estimated"
            })
        }
        const rawBox = cue.visual_box
        let x = 0
        let y = 0
        let width = 0
        let height = 0
        let source = "estimated"
        if (rawBox && rawBox.length === 4) {
            x = Number(rawBox[0])
            y = Number(rawBox[1])
            width = Number(rawBox[2])
            height = Number(rawBox[3])
            source = cue.visual_box_source || "ocr"
        } else {
            const lines = String(cue.source_text || "").split("\n")
            let longest = 0
            let lineCount = 0
            for (let index = 0; index < lines.length; ++index) {
                const line = lines[index].trim()
                if (!line.length) continue
                ++lineCount
                longest = Math.max(longest, line.length)
            }
            lineCount = Math.max(1, lineCount)
            width = Math.min(0.92, Math.max(0.24, longest * 0.043))
            height = Math.min(0.24, Math.max(0.075, lineCount * 0.068))
            x = (1.0 - width) / 2.0
            y = Math.min(Math.max(0, root.overlayNumber(overlay, "y", 0.82)), 1.0 - height)
        }
        const padX = root.overlayNumber(overlay, "padding_x", 0.012)
        const padY = root.overlayNumber(overlay, "padding_y", 0.008)
        const left = Math.max(0, x - padX)
        const top = Math.max(0, y - padY)
        const right = Math.min(1, x + width + padX)
        const bottom = Math.min(1, y + height + padY)
        const copy = Object.assign({}, overlay)
        copy.x = left
        copy.y = top
        copy.width = right - left
        copy.height = bottom - top
        copy.region_source = source
        return copy
    }

    function subtitleScale() {
        const rect = renderedVideoRect()
        const canvas = exportCanvasSize()
        return rect.height / Math.max(1, canvas.height)
    }

    function cueSelected(cueId) {
        return root.selectionIds.length > 0 && root.appModel.isCueSelected(String(cueId || ""))
    }

    function baseArtworkForCue(cue) {
        if (!cue || !cue.cue_id) return ({})
        // Read every setting here so edits refresh even while playback is paused.
        const settingsRevision = root.appModel.exportFontSize
            + root.appModel.exportSubtitleMarginBottom
            + root.appModel.exportSubtitleOutlineWidth
            + root.appModel.exportSubtitleAlignment
            + root.appModel.exportSubtitleOpacityPercent
            + root.appModel.exportFontFamily
            + root.appModel.exportSubtitlePrimaryColor
            + root.appModel.exportSubtitleOutlineColor
            + root.appModel.exportSubtitleBackgroundMode
            + root.appModel.exportSubtitleBackgroundEnabled
        const canvas = root.exportCanvasSize()
        return root.appModel.subtitleArtwork(cue, Math.round(canvas.width), Math.round(canvas.height))
    }

    function artworkForCue(cue) {
        if (!cue || !cue.cue_id) return ({})
        const base = root.subtitleGeometryActive && root.cueSelected(cue.cue_id)
            ? (root.subtitleGeometrySeedArtwork[String(cue.cue_id)] || root.baseArtworkForCue(cue))
            : root.baseArtworkForCue(cue)
        if (root.subtitleGeometryActive
                && root.cueSelected(cue.cue_id)) {
            const copy = Object.assign({}, cue)
            const panel = cue.cue_role !== "dialogue"
                ? root.scenePanelRect(cue) : root.dialoguePanelRect(cue)
            const video = root.renderedVideoRect()
            copy.panel_box = [(panel.x - video.x) / video.width,
                (panel.y - video.y) / video.height,
                panel.width / video.width, panel.height / video.height]
            const canvas = root.exportCanvasSize()
            return root.appModel.subtitleArtwork(copy, Math.round(canvas.width), Math.round(canvas.height))
        }
        return base
    }


    function normalizedSceneRect(cue, key) {
        const raw = cue && cue[key]
        if (!raw || raw.length !== 4) return Qt.rect(0, 0, 0, 0)
        const x = Math.max(0, Math.min(1, Number(raw[0])))
        const y = Math.max(0, Math.min(1, Number(raw[1])))
        const width = Math.max(0, Math.min(1 - x, Number(raw[2])))
        const height = Math.max(0, Math.min(1 - y, Number(raw[3])))
        return Qt.rect(x, y, width, height)
    }

    function sceneGlyphRect(cue) {
        const normalized = normalizedSceneRect(cue, "visual_box")
        const video = renderedVideoRect()
        return Qt.rect(
            video.x + normalized.x * video.width,
            video.y + normalized.y * video.height,
            normalized.width * video.width,
            normalized.height * video.height
        )
    }

    function scenePanelRect(cue) {
        let normalized = normalizedSceneRect(cue, "panel_box")
        if (normalized.width <= 0 || normalized.height <= 0) {
            const glyph = normalizedSceneRect(cue, "visual_box")
            const padX = Math.max(0.012, glyph.width * 0.12)
            const padY = Math.max(0.008, glyph.height * 0.16)
            const left = Math.max(0, glyph.x - padX)
            const top = Math.max(0, glyph.y - padY)
            const right = Math.min(1, glyph.x + glyph.width + padX)
            const bottom = Math.min(1, glyph.y + glyph.height + padY)
            normalized = Qt.rect(left, top, right - left, bottom - top)
        }
        const video = renderedVideoRect()
        let result = Qt.rect(
            video.x + normalized.x * video.width,
            video.y + normalized.y * video.height,
            normalized.width * video.width,
            normalized.height * video.height
        )
        if (root.subtitleGeometryActive
                && root.cueSelected(cue.cue_id)) {
            result.x += root.subtitleGeometryDeltaX * video.width
            result.y += root.subtitleGeometryDeltaY * video.height
            result.width *= root.subtitleGeometryScaleX
            result.height *= root.subtitleGeometryScaleY
            result.x = Math.max(video.x, Math.min(video.x + video.width - result.width, result.x))
            result.y = Math.max(video.y, Math.min(video.y + video.height - result.height, result.y))
        }
        return result
    }

    function dialoguePanelRect(cue) {
        const video = renderedVideoRect()
        let normalized = normalizedSceneRect(cue, "panel_box")
        if (normalized.width <= 0 || normalized.height <= 0) {
            const base = root.subtitleGeometrySeedArtwork[String(cue.cue_id)]
                || root.baseArtworkForCue(cue)
            const rect = base.placement_rect
            const canvas = root.exportCanvasSize()
            normalized = rect ? Qt.rect(rect[0] / canvas.width, rect[1] / canvas.height,
                rect[2] / canvas.width, rect[3] / canvas.height) : Qt.rect(0.1, 0.74, 0.8, 0.16)
        }
        let result = Qt.rect(
            video.x + normalized.x * video.width,
            video.y + normalized.y * video.height,
            normalized.width * video.width,
            normalized.height * video.height
        )
        if (root.subtitleGeometryActive
                && root.cueSelected(cue.cue_id)) {
            result.x += root.subtitleGeometryDeltaX * video.width
            result.y += root.subtitleGeometryDeltaY * video.height
            result.width *= root.subtitleGeometryScaleX
            result.height *= root.subtitleGeometryScaleY
            result.x = Math.max(video.x, Math.min(video.x + video.width - result.width, result.x))
            result.y = Math.max(video.y, Math.min(video.y + video.height - result.height, result.y))
        }
        return result
    }

    function beginSubtitleGeometry(cue, mode, viewportX, viewportY, modifiers) {
        if (!cue || !cue.cue_id || root.appModel.subtitleBusy
                || root.subtitleGeometryPending) return
        if (!root.cueSelected(cue.cue_id))
            root.appModel.selectTimelineCue(
                String(cue.cue_id),
                Boolean(Number(modifiers || Qt.NoModifier) & Qt.ControlModifier)
            )
        root.pause()
        root.subtitleGeometryCueId = String(cue.cue_id)
        root.subtitleGeometryMode = String(mode)
        root.subtitleGeometryPress = Qt.point(viewportX, viewportY)
        root.subtitleGeometryDeltaX = 0
        root.subtitleGeometryDeltaY = 0
        root.subtitleGeometryScaleX = 1
        root.subtitleGeometryScaleY = 1
        const panel = cue.cue_role !== "dialogue"
            ? root.scenePanelRect(cue) : root.dialoguePanelRect(cue)
        root.subtitleGeometryBaseSize = Qt.size(panel.width, panel.height)
        let seeds = ({})
        for (const visibleCue of root.activeSubtitleRegions)
            seeds[String(visibleCue.cue_id)] = root.baseArtworkForCue(visibleCue)
        root.subtitleGeometrySeedArtwork = seeds
        root.subtitleGeometryActive = true
    }

    function updateSubtitleGeometry(viewportX, viewportY, baseWidth, baseHeight) {
        if (!root.subtitleGeometryActive) return
        const video = renderedVideoRect()
        const dx = viewportX - root.subtitleGeometryPress.x
        const dy = viewportY - root.subtitleGeometryPress.y
        if (root.subtitleGeometryMode === "resize") {
            root.subtitleGeometryScaleX = Math.max(0.1,
                (root.subtitleGeometryBaseSize.width + dx) / Math.max(1, root.subtitleGeometryBaseSize.width))
            root.subtitleGeometryScaleY = Math.max(0.1,
                (root.subtitleGeometryBaseSize.height + dy) / Math.max(1, root.subtitleGeometryBaseSize.height))
        } else {
            root.subtitleGeometryDeltaX = dx / Math.max(1, video.width)
            root.subtitleGeometryDeltaY = dy / Math.max(1, video.height)
        }
    }

    function finishSubtitleGeometry() {
        if (!root.subtitleGeometryActive) return
        const changed = Math.abs(root.subtitleGeometryDeltaX) > 0.00001
            || Math.abs(root.subtitleGeometryDeltaY) > 0.00001
            || Math.abs(root.subtitleGeometryScaleX - 1) > 0.00001
            || Math.abs(root.subtitleGeometryScaleY - 1) > 0.00001
        const dx = root.subtitleGeometryDeltaX
        const dy = root.subtitleGeometryDeltaY
        const sx = root.subtitleGeometryScaleX
        const sy = root.subtitleGeometryScaleY
        if (changed) {
            root.subtitleGeometryPending = true
            const canvas = root.exportCanvasSize()
            if (!root.appModel.transformSelectedSubtitleGeometry(dx, dy, sx, sy,
                    Math.round(canvas.width), Math.round(canvas.height)))
                root.resetSubtitleGeometry()
        } else {
            root.resetSubtitleGeometry()
        }
    }

    function resetSubtitleGeometry() {
        root.subtitleGeometryActive = false
        root.subtitleGeometryPending = false
        root.subtitleGeometryCueId = ""
        root.subtitleGeometryMode = ""
        root.subtitleGeometryDeltaX = 0
        root.subtitleGeometryDeltaY = 0
        root.subtitleGeometryScaleX = 1
        root.subtitleGeometryScaleY = 1
        root.subtitleGeometrySeedArtwork = ({})
    }

    function cancelSubtitleGeometry() {
        // Only a released gesture commits. A destroyed grab must not leave
        // playback disabled or submit a partial movement against another cue.
        if (!root.subtitleGeometryPending) root.resetSubtitleGeometry()
    }

    function cancelHiddenSubtitleGeometry() {
        if (!root.subtitleGeometryActive || root.subtitleGeometryPending) return
        for (const cue of root.activeSceneTexts.concat(root.activeTitleTexts, root.activeSubtitleRegions)) {
            if (String(cue.cue_id) === root.subtitleGeometryCueId) return
        }
        root.cancelSubtitleGeometry()
    }

    onActiveSceneTextsChanged: root.cancelHiddenSubtitleGeometry()
    onActiveTitleTextsChanged: root.cancelHiddenSubtitleGeometry()
    onActiveSubtitleRegionsChanged: root.cancelHiddenSubtitleGeometry()


    function openSubtitleMenu(cue) {
        root.subtitleMenuCue = cue || ({})
        subtitleCueMenu.popup()
    }

    function openSubtitleEditor(cue) {
        if (!cue || !cue.cue_id) return
        root.appModel.openSubtitleCueInEditor(
            String(cue.cue_id),
            Number(cue.start_us || 0),
            Number(cue.index || 0)
        )
    }

    Menu {
        id: subtitleCueMenu
        MenuItem {
            text: "Mở trong Biên tập phụ đề"
            enabled: Boolean(root.subtitleMenuCue && root.subtitleMenuCue.cue_id)
            onTriggered: root.openSubtitleEditor(root.subtitleMenuCue)
        }
        MenuSeparator { }
        MenuItem {
            text: "Xóa cue chữ và nền"
            enabled: Boolean(root.subtitleMenuCue && root.subtitleMenuCue.cue_id)
            onTriggered: root.appModel.deleteSubtitleCue(
                String(root.subtitleMenuCue.cue_id)
            )
        }
    }

    function filterIntensity() {
        return root.appModel.exportVideoFilterIntensity / 100
    }

    function filterBrightness() {
        return root.appModel.exportVideoBrightness / 100
    }

    function filterContrast() {
        let value = (root.appModel.exportVideoContrast - 100) / 100
        const intensity = filterIntensity()
        if (root.appModel.exportVideoFilterPreset === "cinematic-warm")
            value += 0.08 * intensity
        else if (root.appModel.exportVideoFilterPreset === "cool")
            value += 0.05 * intensity
        return Math.max(-1, Math.min(1, value))
    }

    function filterSaturation() {
        let value = (root.appModel.exportVideoSaturation - 100) / 100
        const intensity = filterIntensity()
        if (root.appModel.exportVideoFilterPreset === "grayscale")
            value += (-1 - value) * intensity
        else if (root.appModel.exportVideoFilterPreset === "cinematic-warm")
            value += 0.12 * intensity
        return Math.max(-1, Math.min(1, value))
    }

    function filterColorization() {
        const preset = root.appModel.exportVideoFilterPreset
        return preset === "cinematic-warm" || preset === "cool"
            ? 0.14 * filterIntensity() : 0
    }

    function filterColor() {
        return root.appModel.exportVideoFilterPreset === "cool"
            ? "#6F9FD8" : "#D98E52"
    }

    function commitOverlayGeometry(overlay, frameX, frameY, frameWidth, frameHeight) {
        if (!overlay || !overlay.id) return
        const rect = renderedVideoRect()
        if (rect.width <= 0 || rect.height <= 0) return
        const width = Math.max(0.02, Math.min(1.0, frameWidth / rect.width))
        const height = Math.max(0.02, Math.min(1.0, frameHeight / rect.height))
        const x = Math.max(0, Math.min(1.0 - width, (frameX - rect.x) / rect.width))
        const y = Math.max(0, Math.min(1.0 - height, (frameY - rect.y) / rect.height))
        root.appModel.setOverlayGeometry(overlay.id, x, y, width, height)
    }

    function play() {
        if (root.pendingSourcePositionMs >= 0) {
            root.pendingPlay = true
            return
        }
        if (!activeDescriptor.available) seekTimeline(root.positionMs, true)
        else {
            player.play()
            if (activeDescriptor.voiceAvailable) voicePlayer.play()
        }
    }
    function pause() {
        pendingPlay = false
        pendingVoicePlay = false
        player.pause()
        voicePlayer.pause()
    }
    function togglePlayback() {
        if (!root.appModel.currentProject.hasVideo) return
        if (player.playbackState === MediaPlayer.PlayingState) root.pause()
        else root.play()
    }
    function seek(milliseconds) { seekTimeline(milliseconds, false) }
    function toggleMute() {
        const mute = !root.muted
        audioOutput.muted = mute
        voiceAudio.muted = mute
    }

    function applyMonitorVolumes(descriptor) {
        const audio = descriptor && descriptor.audioVolume !== undefined
            ? descriptor.audioVolume : 1.0
        const voice = descriptor && descriptor.voiceVolume !== undefined
            ? descriptor.voiceVolume : 1.0
        audioOutput.volume = Math.min(1.0, Math.max(0, audio * root.masterVolume))
        voiceAudio.volume = Math.min(1.0, Math.max(0, voice * root.masterVolume))
    }

    function adjustMasterVolume(deltaPercent) {
        root.appModel.setPreviewMasterVolumePercent(
            root.appModel.previewMasterVolumePercent + deltaPercent
        )
    }

    function showControls() {
        root.controlsVisible = true
        controlsHideTimer.restart()
    }

    function scheduleControlsHide() {
        controlsHideTimer.restart()
    }

    function maybeHideControls() {
        if (root.controlsHovered || root.volumeHovered) {
            root.controlsVisible = true
            return
        }
        root.controlsVisible = false
    }

    function seekRelative(deltaMilliseconds) {
        const target = Math.max(0, Math.min(root.durationMs, root.positionMs + deltaMilliseconds))
        root.seekTimeline(target, player.playbackState === MediaPlayer.PlayingState)
    }

    function expectedVoicePositionMs(descriptor, videoPositionMs) {
        const videoDeltaMs = videoPositionMs - descriptor.sourcePositionMs
        return Math.max(
            0,
            descriptor.voicePositionMs
                + videoDeltaMs / descriptor.speedRate * descriptor.voicePlaybackRate
        )
    }

    function textEditorHasFocus() {
        const window = root.Window.window
        const item = window ? window.activeFocusItem : null
        return Boolean(item && (
            "cursorPosition" in item
            || "selectionStart" in item
            || "inputMethodComposing" in item
        ))
    }

    function seekTimeline(milliseconds, resumePlayback) {
        const requestedUs = Math.max(0, milliseconds * 1000)
        const descriptor = root.appModel.currentProject.previewDescriptor(requestedUs)
        if (!descriptor.available) {
            player.pause()
            voicePlayer.pause()
            root.activeDescriptor = descriptor
            root.appModel.setPlayheadMs(milliseconds)
            return
        }
        const sameSource = player.source.toString() === descriptor.sourceUrl
        root.activeDescriptor = descriptor
        root.applyMonitorVolumes(descriptor)
        player.playbackRate = descriptor.speedRate
        syncVoice(descriptor, resumePlayback)
        root.applyingTimelineSeek = true
        root.appModel.setPlayheadMs(milliseconds)
        root.applyingTimelineSeek = false
        if (sameSource) {
            player.setPosition(descriptor.sourcePositionMs)
            if (resumePlayback) player.play()
            return
        }
        root.pendingSourcePositionMs = descriptor.sourcePositionMs
        root.pendingPlay = resumePlayback
        player.source = descriptor.sourceUrl
    }

    Timer {
        id: controlsHideTimer
        interval: 0
        repeat: false
        onTriggered: root.maybeHideControls()
    }

    function syncVoice(descriptor, resumePlayback) {
        if (!root.appModel.voicePlaybackEnabled || !descriptor.voiceAvailable) {
            voicePlayer.pause()
            voicePlayer.source = ""
            pendingVoicePositionMs = -1
            pendingVoicePlay = false
            return
        }
        root.applyMonitorVolumes(descriptor)
        voicePlayer.playbackRate = descriptor.voicePlaybackRate
        const sameSource = voicePlayer.source.toString() === descriptor.voiceSourceUrl
        if (sameSource) {
            voicePlayer.setPosition(descriptor.voicePositionMs)
            if (resumePlayback) voicePlayer.play()
            return
        }
        pendingVoicePositionMs = descriptor.voicePositionMs
        pendingVoicePlay = resumePlayback
        voicePlayer.source = descriptor.voiceSourceUrl
    }

    function advanceAfterClip() {
        const nextUs = root.activeDescriptor.timelineEndUs
        const descriptor = root.appModel.currentProject.previewDescriptor(nextUs)
        if (!descriptor.available) {
            player.pause()
            root.appModel.setPlayheadMs(nextUs / 1000)
            return
        }
        seekTimeline(nextUs / 1000, true)
    }

    function timeText(milliseconds) {
        const seconds = Math.max(0, Math.floor(milliseconds / 1000))
        const hours = Math.floor(seconds / 3600)
        const minutes = Math.floor((seconds % 3600) / 60)
        const rest = seconds % 60
        return String(hours).padStart(2, "0") + ":"
            + String(minutes).padStart(2, "0") + ":"
            + String(rest).padStart(2, "0")
            + "." + String(Math.floor(Math.max(0, milliseconds)) % 1000).padStart(3, "0")
    }

    function seekTimeText(value) {
        const match = /^(\d+):([0-5]\d):([0-5]\d)(?:\.(\d{1,3}))?$/.exec(String(value).trim())
        if (!match) return false
        const millis = Number(match[1]) * 3600000 + Number(match[2]) * 60000
            + Number(match[3]) * 1000 + Number(String(match[4] || "0").padEnd(3, "0"))
        if (millis > durationMs) return false
        seekTimeline(millis, false)
        return true
    }

    MediaPlayer {
        id: player
        audioOutput: audioOutput
        videoOutput: videoOutput
        // Qt 6.11 keeps the original soundtrack pitch stable while the
        // piecewise conform map changes playbackRate at block boundaries.
        pitchCompensation: true
        onErrorOccurred: function(_error, errorString) {
            root.errorText = errorString
        }
        onSourceChanged: root.errorText = ""
        onPlaybackStateChanged: {
            if (playbackState === MediaPlayer.PlayingState) root.showControls()
            else if (!root.controlsHovered && !root.volumeHovered)
                root.controlsVisible = false
        }
        onMediaStatusChanged: {
            if (root.pendingSourcePositionMs < 0) return
            if (mediaStatus !== MediaPlayer.LoadedMedia
                    && mediaStatus !== MediaPlayer.BufferedMedia) return
            const requested = root.pendingSourcePositionMs
            const shouldPlay = root.pendingPlay
            root.pendingSourcePositionMs = -1
            root.pendingPlay = false
            player.setPosition(requested)
            if (shouldPlay) player.play()
        }
        onPositionChanged: function(position) {
            if (root.applyingTimelineSeek || !root.activeDescriptor.available) return
            const sourceOffset = Math.max(
                0, position - root.activeDescriptor.sourceStartMs
            )
            const timelineMs = root.activeDescriptor.timelineStartUs / 1000
                + sourceOffset / root.activeDescriptor.speedRate
            root.updatingPlayheadFromPlayer = true
            root.appModel.setPlayheadMs(
                Math.min(root.activeDescriptor.timelineEndUs / 1000, timelineMs)
            )
            root.updatingPlayheadFromPlayer = false
            if (root.activeDescriptor.voiceAvailable) {
                const expectedVoiceMs = root.expectedVoicePositionMs(
                    root.activeDescriptor, position
                )
                if (playbackState === MediaPlayer.PlayingState
                        && voicePlayer.playbackState === MediaPlayer.PlayingState
                        && Math.abs(voicePlayer.position - expectedVoiceMs) > 200)
                    voicePlayer.setPosition(expectedVoiceMs)
            }
            if (playbackState === MediaPlayer.PlayingState
                    && position >= root.activeDescriptor.sourceEndMs) {
                root.advanceAfterClip()
            }
        }
    }

    MediaPlayer {
        id: voicePlayer
        audioOutput: voiceAudio
        onMediaStatusChanged: {
            if (root.pendingVoicePositionMs < 0) return
            if (mediaStatus !== MediaPlayer.LoadedMedia
                    && mediaStatus !== MediaPlayer.BufferedMedia) return
            const requested = root.pendingVoicePositionMs
            const shouldPlay = root.pendingVoicePlay
            root.pendingVoicePositionMs = -1
            root.pendingVoicePlay = false
            setPosition(requested)
            if (shouldPlay) play()
        }
    }

    Component.onCompleted: {
        root.applyMonitorVolumes(root.activeDescriptor)
        if (root.visible) seekTimeline(root.positionMs, false)
    }

    onVisibleChanged: {
        if (visible) seekTimeline(root.positionMs, false)
        else {
            pause()
            root.cancelSubtitleGeometry()
        }
    }

    Connections {
        target: root.appModel.currentProject
        function onChanged() {
            if (!root.visible) return
            root.seekTimeline(
                Math.min(root.positionMs, root.durationMs),
                player.playbackState === MediaPlayer.PlayingState
            )
        }
    }

    Connections {
        target: root.appModel
        function onOverlaySelectionRequested(overlayId) { root.selectedOverlayId = overlayId }
        function onPlayheadChanged() {
            if (!root.visible || root.applyingTimelineSeek
                    || root.updatingPlayheadFromPlayer) return
            root.seekTimeline(
                Math.min(root.positionMs, root.durationMs),
                player.playbackState === MediaPlayer.PlayingState
            )
        }
        function onPreviewMasterVolumeChanged() {
            root.applyMonitorVolumes(root.activeDescriptor)
        }
        function onVoicePlaybackEnabledChanged() {
            if (root.appModel.voicePlaybackEnabled) return
            voicePlayer.stop()
            voicePlayer.source = ""
            root.pendingVoicePositionMs = -1
            root.pendingVoicePlay = false
        }
        function onSubtitleGeometrySettled(generation, succeeded) {
            if (root.subtitleGeometryPending) root.resetSubtitleGeometry()
        }
    }

    Shortcut {
        sequence: "Space"
        context: Qt.WindowShortcut
        enabled: root.visible && !root.textEditorHasFocus()
            && root.ocrRegionEditorMode.length === 0
            && root.colorPickerTarget.length === 0
            && !root.editableOverlays
        onActivated: root.togglePlayback()
    }
    Shortcut {
        sequence: "Left"
        context: Qt.WindowShortcut
        enabled: root.visible && !root.textEditorHasFocus()
        onActivated: root.seekRelative(-root.appModel.timelineSeekStepMs)
    }
    Shortcut {
        sequence: "Right"
        context: Qt.WindowShortcut
        enabled: root.visible && !root.textEditorHasFocus()
        onActivated: root.seekRelative(root.appModel.timelineSeekStepMs)
    }

    AudioOutput {
        id: audioOutput
        volume: 1.0
    }

    AudioOutput {
        id: voiceAudio
        volume: 1.0
    }

    Rectangle {
        anchors.fill: parent
        color: Theme.canvas

        Item {
            id: videoViewport
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            // Effects mode is a static editing canvas. The playback transport
            // is not part of that surface, so every video pixel remains usable
            // for direct logo/blur manipulation.
            anchors.bottom: transport.top
            clip: true

            HoverHandler {
                id: viewportHover
                onHoveredChanged: {
                    root.viewportHovered = hovered
                    if (hovered) root.showControls()
                    else root.scheduleControlsHide()
                }
            }

            VideoOutput {
                id: videoOutput
                objectName: "previewVideoOutput"
                readonly property rect outputRect: root.renderedVideoRect()
                x: outputRect.x
                y: outputRect.y
                width: outputRect.width
                height: outputRect.height
                fillMode: VideoOutput.Stretch
            }

            ShaderEffectSource {
                id: baseVideoTexture
                x: videoOutput.x
                y: videoOutput.y
                width: videoOutput.width
                height: videoOutput.height
                sourceItem: videoOutput
                hideSource: true
                live: true
            }

            MultiEffect {
                id: filteredVideo
                x: videoOutput.x
                y: videoOutput.y
                width: videoOutput.width
                height: videoOutput.height
                source: baseVideoTexture
                brightness: root.filterBrightness()
                contrast: root.filterContrast()
                saturation: root.filterSaturation()
                colorization: root.filterColorization()
                colorizationColor: root.filterColor()
                autoPaddingEnabled: false
            }

            MouseArea {
                id: videoPlaybackToggleArea
                objectName: "videoPlaybackToggleArea"
                x: filteredVideo.x
                y: filteredVideo.y
                width: filteredVideo.width
                height: filteredVideo.height
                z: 20
                enabled: !root.editableOverlays
                    && root.ocrRegionEditorMode.length === 0
                    && root.colorPickerTarget.length === 0
                    && !root.subtitleGeometryActive
                acceptedButtons: root.videoContextMenuEnabled
                    ? Qt.LeftButton | Qt.RightButton : Qt.LeftButton
                cursorShape: Qt.PointingHandCursor
                onClicked: function(mouse) {
                    if (mouse.button === Qt.RightButton) {
                        const descriptor = root.appModel.currentProject.previewDescriptor(
                            root.appModel.playheadUs)
                        if (!descriptor.available) return
                        root.appModel.selectClip(descriptor.clipId)
                        const point = mapToItem(root, mouse.x, mouse.y)
                        root.videoContextMenuRequested(point.x, point.y)
                    } else root.togglePlayback()
                }
                Accessible.name: "Phát hoặc tạm dừng video"
            }

            // Scene cues render only their chosen text/background, never an
            // extra source-glyph blur. Manual blur and dialogue covers are separate.
            Repeater {
                model: (root.appModel.exportSubtitleEnabled ? root.activeSceneTexts : [])
                    .concat(root.activeTitleTexts)
                delegate: Item {
                    id: sceneTextFrame
                    required property var modelData
                    readonly property bool accepted:
                        String(modelData.review_state || "accepted") === "accepted"
                    readonly property rect glyphRect: root.sceneGlyphRect(modelData)
                    readonly property rect panelRect: root.scenePanelRect(modelData)
                    readonly property real presentationOpacity:
                        root.sceneFadeOpacity(modelData)
                    anchors.fill: parent
                    z: 60


                    Image {
                        readonly property rect videoRect: root.renderedVideoRect()
                        x: videoRect.x
                        y: videoRect.y
                        width: videoRect.width
                        height: videoRect.height
                        source: sceneTextFrame.accepted
                            ? (root.artworkForCue(sceneTextFrame.modelData).source || "") : ""
                        visible: sceneTextFrame.accepted
                        asynchronous: false
                        cache: true
                        opacity: sceneTextFrame.presentationOpacity
                        fillMode: Image.Stretch
                    }

                    Rectangle {
                        x: sceneTextFrame.panelRect.x
                        y: sceneTextFrame.panelRect.y
                        width: sceneTextFrame.panelRect.width
                        height: sceneTextFrame.panelRect.height
                        visible: !sceneTextFrame.accepted && width > 0 && height > 0
                        color: "transparent"
                        border.width: 2
                        border.color: Theme.warning

                        Text {
                            anchors.left: parent.left
                            anchors.bottom: parent.top
                            anchors.bottomMargin: 2
                            text: String(
                                sceneTextFrame.modelData.subtitle_track_id || "S2"
                            ) + " · CẦN DUYỆT"
                            color: Theme.warning
                            font.family: Theme.fontFamily
                            font.pixelSize: 9
                            font.weight: Font.DemiBold
                        }
                    }

                    MouseArea {
                        x: sceneTextFrame.panelRect.x
                        y: sceneTextFrame.panelRect.y
                        width: sceneTextFrame.panelRect.width
                        height: sceneTextFrame.panelRect.height
                        enabled: !root.editableOverlays
                            && root.ocrRegionEditorMode.length === 0
                            && width > 0 && height > 0
                        acceptedButtons: Qt.LeftButton | Qt.RightButton
                        cursorShape: Qt.PointingHandCursor
                        z: 220
                        property bool geometryDragged: false
                        property bool selectedOnPress: false
                        property point pressInViewport: Qt.point(0, 0)
                        onPressed: function(mouse) {
                            geometryDragged = false
                            if (mouse.button !== Qt.LeftButton) return
                            selectedOnPress = root.appModel.isCueSelected(
                                String(sceneTextFrame.modelData.cue_id || "")
                            )
                            pressInViewport = mapToItem(videoViewport, mouse.x, mouse.y)
                            root.beginSubtitleGeometry(
                                sceneTextFrame.modelData,
                                "move",
                                pressInViewport.x,
                                pressInViewport.y,
                                mouse.modifiers
                            )
                        }
                        onPositionChanged: function(mouse) {
                            if (!pressed || !(mouse.buttons & Qt.LeftButton)) return
                            const point = mapToItem(videoViewport, mouse.x, mouse.y)
                            if (Math.abs(point.x - pressInViewport.x) >= 3
                                    || Math.abs(point.y - pressInViewport.y) >= 3)
                                geometryDragged = true
                            if (geometryDragged)
                                root.updateSubtitleGeometry(
                                    point.x, point.y,
                                    sceneTextFrame.panelRect.width,
                                    sceneTextFrame.panelRect.height
                                )
                        }
                        onReleased: function(mouse) {
                            if (mouse.button === Qt.LeftButton) {
                                const point = mapToItem(videoViewport, mouse.x, mouse.y)
                                if (geometryDragged || Math.abs(point.x - pressInViewport.x) >= 3
                                        || Math.abs(point.y - pressInViewport.y) >= 3) {
                                    geometryDragged = true
                                    root.updateSubtitleGeometry(point.x, point.y, width, height)
                                }
                                root.finishSubtitleGeometry()
                            }
                        }
                        onCanceled: root.cancelSubtitleGeometry()
                        onClicked: function(mouse) {
                            if (geometryDragged) return
                            if (mouse.button === Qt.RightButton) {
                                root.openSubtitleMenu(sceneTextFrame.modelData)
                                return
                            }
                            if (selectedOnPress && (mouse.modifiers & Qt.ControlModifier))
                                root.appModel.selectTimelineCue(
                                    String(sceneTextFrame.modelData.cue_id || ""),
                                    Boolean(mouse.modifiers & Qt.ControlModifier)
                                )
                        }
                        onDoubleClicked: function(mouse) {
                            if (mouse.button === Qt.LeftButton)
                                root.openSubtitleEditor(sceneTextFrame.modelData)
                        }
                    }

                    Rectangle {
                        x: sceneTextFrame.panelRect.x + sceneTextFrame.panelRect.width - width / 2
                        y: sceneTextFrame.panelRect.y + sceneTextFrame.panelRect.height - height / 2
                        width: 14
                        height: 14
                        radius: 2
                        visible: !root.editableOverlays && sceneTextFrame.accepted
                            && root.cueSelected(sceneTextFrame.modelData.cue_id)
                        color: Theme.primary
                        border.width: 1
                        border.color: Theme.textPrimary
                        z: 230
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.SizeFDiagCursor
                            preventStealing: true
                            property point pressInViewport: Qt.point(0, 0)
                            property real baseWidth: 1
                            property real baseHeight: 1
                            onPressed: function(mouse) {
                                pressInViewport = mapToItem(videoViewport, mouse.x, mouse.y)
                                baseWidth = sceneTextFrame.panelRect.width
                                baseHeight = sceneTextFrame.panelRect.height
                                root.beginSubtitleGeometry(
                                    sceneTextFrame.modelData,
                                    "resize",
                                    pressInViewport.x,
                                    pressInViewport.y,
                                    Qt.NoModifier
                                )
                            }
                            onPositionChanged: function(mouse) {
                                if (!pressed) return
                                const point = mapToItem(videoViewport, mouse.x, mouse.y)
                                root.updateSubtitleGeometry(
                                    point.x, point.y,
                                    baseWidth,
                                    baseHeight
                                )
                            }
                            onReleased: function(mouse) {
                                const point = mapToItem(videoViewport, mouse.x, mouse.y)
                                root.updateSubtitleGeometry(point.x, point.y, baseWidth, baseHeight)
                                root.finishSubtitleGeometry()
                            }
                            onCanceled: root.cancelSubtitleGeometry()
                        }
                    }
                }
            }

            Item {
                id: liveSubtitlePreview
                objectName: "liveSubtitlePreview"
                readonly property rect videoRect: root.renderedVideoRect()
                readonly property real scaleFactor: root.subtitleScale()
                readonly property var artwork: root.artworkForCue(root.activeSubtitle)
                readonly property rect selectionRect: {
                    const r = artwork.selection_rect || artwork.text_rect
                    return r ? Qt.rect(r[0] * scaleFactor - 3, r[1] * scaleFactor - 3,
                        r[2] * scaleFactor + 6, r[3] * scaleFactor + 6) : Qt.rect(0, 0, 0, 0)
                }
                x: videoRect.x
                y: videoRect.y
                width: videoRect.width
                height: videoRect.height
                visible: root.appModel.exportSubtitleEnabled
                    && root.activeSubtitleText.length > 0
                    && videoRect.width > 0 && videoRect.height > 0
                // Blur/logo overlays affect the video layer only. Keep the
                // generated subtitle above them so an editor can deliberately
                // cover the original hard-sub without hiding the new subtitle.
                z: 110



                Repeater {
                    model: root.activeSubtitleRegions
                    delegate: Image {
                    required property var modelData
                    x: liveSubtitlePreview.videoRect.x - parent.x
                    y: liveSubtitlePreview.videoRect.y - parent.y
                    width: liveSubtitlePreview.videoRect.width
                    height: liveSubtitlePreview.videoRect.height
                    source: root.artworkForCue(modelData).source || ""
                    asynchronous: false
                    cache: true
                    fillMode: Image.Stretch
                    }
                }

                MouseArea {
                    objectName: "dialogueTextMoveArea"
                    x: liveSubtitlePreview.selectionRect.x
                    y: liveSubtitlePreview.selectionRect.y
                    width: liveSubtitlePreview.selectionRect.width
                    height: liveSubtitlePreview.selectionRect.height
                    enabled: !root.editableOverlays
                        && root.ocrRegionEditorMode.length === 0
                        && Boolean(root.activeSubtitle && root.activeSubtitle.cue_id)
                    acceptedButtons: Qt.LeftButton | Qt.RightButton
                    cursorShape: Qt.PointingHandCursor
                    z: 4
                    property bool geometryDragged: false
                    property bool selectedOnPress: false
                    property point pressInViewport: Qt.point(0, 0)
                    onPressed: function(mouse) {
                        geometryDragged = false
                        if (mouse.button !== Qt.LeftButton) return
                        selectedOnPress = root.appModel.isCueSelected(
                            String(root.activeSubtitle.cue_id || "")
                        )
                        pressInViewport = mapToItem(videoViewport, mouse.x, mouse.y)
                        root.beginSubtitleGeometry(
                            root.activeSubtitle,
                            "move",
                            pressInViewport.x,
                            pressInViewport.y,
                            mouse.modifiers
                        )
                    }
                    onPositionChanged: function(mouse) {
                        if (!pressed || !(mouse.buttons & Qt.LeftButton)) return
                        const point = mapToItem(videoViewport, mouse.x, mouse.y)
                        if (Math.abs(point.x - pressInViewport.x) >= 3
                                || Math.abs(point.y - pressInViewport.y) >= 3)
                            geometryDragged = true
                        if (geometryDragged)
                            root.updateSubtitleGeometry(
                                point.x, point.y,
                                liveSubtitlePreview.width,
                                liveSubtitlePreview.height
                            )
                    }
                    onReleased: function(mouse) {
                        if (mouse.button === Qt.LeftButton) {
                            const point = mapToItem(videoViewport, mouse.x, mouse.y)
                            if (geometryDragged || Math.abs(point.x - pressInViewport.x) >= 3
                                    || Math.abs(point.y - pressInViewport.y) >= 3) {
                                geometryDragged = true
                                root.updateSubtitleGeometry(point.x, point.y, width, height)
                            }
                            root.finishSubtitleGeometry()
                        }
                    }
                    onCanceled: root.cancelSubtitleGeometry()
                    onClicked: function(mouse) {
                        if (geometryDragged) return
                        if (mouse.button === Qt.RightButton) {
                            root.openSubtitleMenu(root.activeSubtitle)
                            return
                        }
                        if (selectedOnPress && (mouse.modifiers & Qt.ControlModifier))
                            root.appModel.selectTimelineCue(
                                String(root.activeSubtitle.cue_id || ""),
                                Boolean(mouse.modifiers & Qt.ControlModifier)
                            )
                    }
                    onDoubleClicked: function(mouse) {
                        if (mouse.button === Qt.LeftButton)
                            root.openSubtitleEditor(root.activeSubtitle)
                    }
                }

                Rectangle {
                    objectName: "dialogueTextSelectionFrame"
                    x: liveSubtitlePreview.selectionRect.x
                    y: liveSubtitlePreview.selectionRect.y
                    width: liveSubtitlePreview.selectionRect.width
                    height: liveSubtitlePreview.selectionRect.height
                    visible: !root.editableOverlays && root.cueSelected(root.activeSubtitle.cue_id)
                    color: "transparent"
                    border.width: 2
                    border.color: Theme.primary
                    z: 3
                }

                Rectangle {
                    x: liveSubtitlePreview.selectionRect.x + liveSubtitlePreview.selectionRect.width - width / 2
                    y: liveSubtitlePreview.selectionRect.y + liveSubtitlePreview.selectionRect.height - height / 2
                    width: 14
                    height: 14
                    radius: 2
                    visible: !root.editableOverlays && root.cueSelected(root.activeSubtitle.cue_id)
                    color: Theme.primary
                    border.width: 1
                    border.color: Theme.textPrimary
                    z: 6
                    MouseArea {
                        objectName: "dialogueTextResizeArea"
                        anchors.fill: parent
                        cursorShape: Qt.SizeFDiagCursor
                        preventStealing: true
                        property point pressInViewport: Qt.point(0, 0)
                        property real baseWidth: 1
                        property real baseHeight: 1
                        onPressed: function(mouse) {
                            pressInViewport = mapToItem(videoViewport, mouse.x, mouse.y)
                            baseWidth = liveSubtitlePreview.width
                            baseHeight = liveSubtitlePreview.height
                            root.beginSubtitleGeometry(
                                root.activeSubtitle,
                                "resize",
                                pressInViewport.x,
                                pressInViewport.y,
                                Qt.NoModifier
                            )
                        }
                        onPositionChanged: function(mouse) {
                            if (!pressed) return
                            const point = mapToItem(videoViewport, mouse.x, mouse.y)
                            root.updateSubtitleGeometry(
                                point.x, point.y, baseWidth, baseHeight
                            )
                        }
                        onReleased: function(mouse) {
                            const point = mapToItem(videoViewport, mouse.x, mouse.y)
                            root.updateSubtitleGeometry(point.x, point.y, baseWidth, baseHeight)
                            root.finishSubtitleGeometry()
                        }
                        onCanceled: root.cancelSubtitleGeometry()
                    }
                }
            }

            MouseArea {
                id: previewColorPicker
                x: filteredVideo.x
                y: filteredVideo.y
                width: filteredVideo.width
                height: filteredVideo.height
                z: 500
                visible: root.colorPickerTarget.length > 0
                enabled: visible
                acceptedButtons: Qt.LeftButton | Qt.RightButton
                cursorShape: Qt.CrossCursor
                preventStealing: true
                onClicked: function(mouse) {
                    if (mouse.button === Qt.RightButton) {
                        root.colorPickerTarget = ""
                        return
                    }
                    const target = root.colorPickerTarget
                    const sampleX = Math.round(mouse.x)
                    const sampleY = Math.round(mouse.y)
                    filteredVideo.grabToImage(function(result) {
                        const color = root.appModel.samplePreviewColor(
                            result.image, sampleX, sampleY
                        )
                        root.colorPickerTarget = ""
                        if (String(color || "").length > 0)
                            root.colorPicked(target, color)
                    }, Qt.size(
                        Math.max(1, Math.round(filteredVideo.width)),
                        Math.max(1, Math.round(filteredVideo.height))
                    ))
                }
            }

            // Each active dialogue cue supplies its final Vietnamese layout.
            // The list retains overlapping cues rather than a single OCR box.
            Repeater {
                model: root.showProjectOverlays
                    && Boolean(root.blurOverlay && root.blurOverlay.enabled)
                    && root.blurOverlay.tracking_mode === "source_subtitle"
                    && root.overlayActive(root.blurOverlay)
                    ? root.activeSubtitleRegions : []
                delegate: Rectangle {
                    id: trackedBlurFrame
                    required property var modelData
                    readonly property var displayedOverlay:
                        root.trackedSubtitleBlur(root.blurOverlay, modelData)

                    x: root.overlayX(displayedOverlay)
                    y: root.overlayY(displayedOverlay)
                    width: root.overlayWidth(displayedOverlay)
                    height: root.overlayHeight(displayedOverlay)
                    color: "transparent"
                    z: 40

                    ShaderEffectSource {
                        id: trackedBlurSource
                        width: Math.max(1, trackedBlurFrame.width)
                        height: Math.max(1, trackedBlurFrame.height)
                        visible: false
                        live: true
                        recursive: true
                        sourceItem: filteredVideo
                        sourceRect: Qt.rect(
                            trackedBlurFrame.x - filteredVideo.x,
                            trackedBlurFrame.y - filteredVideo.y,
                            width,
                            height
                        )
                    }

                    MultiEffect {
                        anchors.fill: parent
                        visible: width > 0 && height > 0
                        source: trackedBlurSource
                        blurEnabled: true
                        // blur_radius is stored in output pixels. Scale the
                        // preview kernel with the rendered canvas so preview
                        // and the FFmpeg result have the same relative blur.
                        blurMax: 64
                        blur: Math.max(0.01, Math.min(
                            1.0,
                            root.overlayNumber(
                                root.blurOverlay,
                                "blur_radius",
                                root.blurOverlay.concealment_mode === "cover" ? 46 : 22
                            ) * root.subtitleScale()
                                * (root.blurOverlay.concealment_mode === "cover"
                                    ? Math.SQRT2 : 1.0) / 64
                        ))
                        opacity: root.overlayNumber(root.blurOverlay, "opacity", 1.0)
                        autoPaddingEnabled: false
                    }

                    Rectangle {
                        anchors.fill: parent
                        visible: root.blurOverlay.concealment_mode === "cover"
                        color: "#000000"
                        opacity: 0.90
                    }

                    Rectangle {
                        anchors.fill: parent
                        visible: root.editableOverlays
                        color: "transparent"
                        border.width: 1
                        border.color: Theme.info
                    }

                    Text {
                        anchors.left: parent.left
                        anchors.bottom: parent.top
                        anchors.bottomMargin: 2
                        visible: root.editableOverlays
                        text: "BÁM CHỮ VIỆT"
                        color: Theme.info
                        font.family: Theme.fontFamily
                        font.pixelSize: 9
                        font.weight: Font.DemiBold
                    }
                }
            }

            Repeater {
                model: root.appModel.currentProject.overlayModel
                delegate: Rectangle {
                    id: overlayFrame
                    required property var overlay
                    property bool localOverride: false
                    property bool awaitingCommit: false
                    property bool moving: false
                    property bool resizing: false
                    property real manualX: 0
                    property real manualY: 0
                    property real manualWidth: 0
                    property real manualHeight: 0
                    readonly property bool tracksSourceSubtitle:
                        overlay.kind === "blur"
                        && overlay.tracking_mode === "source_subtitle"
                    readonly property bool tracksAutomaticRegion:
                        overlay.kind === "blur"
                        && (overlay.tracking_mode === "source_subtitle"
                            || overlay.tracking_mode === "ocr_watermark")
                    readonly property var displayedOverlay: overlay

                    visible: root.showProjectOverlays
                        && Boolean(overlay.enabled)
                        && overlay.id !== "overlay-default-blur"
                        && (overlay.kind === "blur" || overlay.kind === "logo")
                        && !tracksSourceSubtitle
                    x: localOverride ? manualX : root.overlayX(displayedOverlay)
                    y: localOverride ? manualY : root.overlayY(displayedOverlay)
                    width: localOverride ? manualWidth : root.overlayWidth(displayedOverlay)
                    height: localOverride ? manualHeight : root.overlayHeight(displayedOverlay)
                    color: "transparent"
                    border.width: 0
                    // The selected frame must stay reachable even when a
                    // damaged saved geometry sits underneath the transport.
                    // Non-editing previews keep the normal stacking order.
                    z: (root.editableOverlays || root.editableProjectOverlays)
                        && root.selectedOverlayId === overlayFrame.overlay.id
                        ? 100 : 40

                    function captureCurrentGeometry() {
                        // Read the bound geometry before enabling the manual
                        // branch. Flipping localOverride first would make x/y
                        // and width/height read their zero-valued manual fields,
                        // collapsing the overlay as soon as the pointer goes down.
                        const currentX = overlayFrame.x
                        const currentY = overlayFrame.y
                        const currentWidth = overlayFrame.width
                        const currentHeight = overlayFrame.height
                        overlayFrame.manualX = currentX
                        overlayFrame.manualY = currentY
                        overlayFrame.manualWidth = currentWidth
                        overlayFrame.manualHeight = currentHeight
                        overlayFrame.localOverride = true
                    }

                    function cancelManipulation() {
                        overlayFrame.moving = false
                        overlayFrame.resizing = false
                        overlayFrame.awaitingCommit = false
                        overlayFrame.localOverride = false
                    }

                    function compactHandleCorner() {
                        const rect = root.renderedVideoRect()
                        const onLeft = overlayFrame.x + overlayFrame.width / 2
                            <= rect.x + rect.width / 2
                        const onTop = overlayFrame.y + overlayFrame.height / 2
                            <= rect.y + rect.height / 2
                        if (onLeft && onTop) return "bottomRight"
                        if (onLeft && !onTop) return "topRight"
                        if (!onLeft && onTop) return "bottomLeft"
                        return "topLeft"
                    }

                    function resizeHandleVisible(corner) {
                        if (overlayFrame.tracksAutomaticRegion || !(root.editableOverlays || root.editableProjectOverlays)
                                || root.selectedOverlayId !== overlayFrame.overlay.id)
                            return false
                        const compact = overlayFrame.width < 52 || overlayFrame.height < 52
                        return !compact || overlayFrame.compactHandleCorner() === corner
                    }

                    function resizeHandleHitMargin() {
                        // A 13 px expansion turns an 18 px corner into a 44 px
                        // hit target. On a compact logo that covers nearly the
                        // entire body and steals ordinary move drags. Keep the
                        // compact target large enough to grab without crossing
                        // the frame centre; larger overlays can afford 34 px.
                        const compact = overlayFrame.width < 52 || overlayFrame.height < 52
                        return compact ? -4 : -8
                    }

                    ShaderEffectSource {
                        id: liveBlurSource
                        width: Math.max(1, overlayFrame.width)
                        height: Math.max(1, overlayFrame.height)
                        visible: false
                        live: true
                        recursive: true
                        sourceItem: filteredVideo
                        sourceRect: Qt.rect(
                            overlayFrame.x - filteredVideo.x,
                            overlayFrame.y - filteredVideo.y,
                            width,
                            height
                        )
                    }

                    MultiEffect {
                        anchors.fill: parent
                        visible: overlayFrame.overlay.kind === "blur"
                            && root.overlayActive(overlayFrame.overlay)
                            && width > 0 && height > 0
                        source: liveBlurSource
                        blurEnabled: true
                        blurMax: 64
                        blur: Math.max(0.01, Math.min(
                            1.0,
                            root.overlayNumber(
                                overlayFrame.overlay, "blur_radius", 22
                            ) * root.subtitleScale()
                                * (overlayFrame.overlay.concealment_mode === "cover"
                                    ? Math.SQRT2 : 1.0) / 64
                        ))
                        autoPaddingEnabled: false
                    }

                    Rectangle {
                        anchors.fill: parent
                        visible: overlayFrame.overlay.kind === "blur"
                            && overlayFrame.overlay.concealment_mode === "cover"
                            && root.overlayActive(overlayFrame.overlay)
                        color: "#000000"
                        opacity: 0.90
                    }

                    Image {
                        anchors.fill: parent
                        visible: overlayFrame.overlay.kind === "logo"
                            && root.overlayActive(overlayFrame.overlay)
                            && Boolean(overlayFrame.overlay.asset_id)
                            && width > 0 && height > 0
                        source: root.appModel.currentProject.mediaSource(
                            overlayFrame.overlay.asset_id
                        )
                        opacity: root.overlayNumber(
                            overlayFrame.overlay, "opacity", 0.9
                        )
                        fillMode: Image.Stretch
                        asynchronous: true
                        smooth: true
                        mipmap: true
                    }

                    Rectangle {
                        anchors.fill: parent
                        visible: (root.editableOverlays || root.editableProjectOverlays)
                        color: "transparent"
                        border.width: root.selectedOverlayId === overlayFrame.overlay.id
                            ? 2 : 1
                        border.color: overlayFrame.overlay.kind === "blur"
                            ? Theme.info : Theme.primary
                        z: 20
                    }

                    // Keep the pointer-tracked frame in pixels only while the
                    // asynchronous project command is in flight. Once the
                    // backend replies, rebind to normalized project geometry;
                    // otherwise a resize or a changed aspect ratio makes the
                    // frame appear to jump away from the saved position.
                    Connections {
                        target: root.appModel
                        function onOverlayGeometrySettled(overlayId, _saved) {
                            if (overlayId !== overlayFrame.overlay.id
                                    || !overlayFrame.awaitingCommit
                                    || overlayFrame.moving
                                    || overlayFrame.resizing)
                                return
                            overlayFrame.localOverride = false
                            overlayFrame.awaitingCommit = false
                        }
                    }

                    Text {
                        anchors.left: parent.left
                        anchors.bottom: parent.top
                        anchors.bottomMargin: 2
                        text: overlayFrame.overlay.kind === "blur"
                            ? (overlayFrame.tracksSourceSubtitle
                                ? (overlayFrame.displayedOverlay.region_source === "ocr"
                                    ? "BLUR THEO OCR" : "BLUR ƯỚC LƯỢNG")
                                : (overlayFrame.overlay.tracking_mode === "ocr_watermark"
                                    ? "CHE WATERMARK OCR" : "BLUR THỦ CÔNG"))
                            : "LOGO"
                        visible: (root.editableOverlays || root.editableProjectOverlays)
                        color: overlayFrame.overlay.kind === "blur"
                            ? Theme.info : Theme.primary
                        font.family: Theme.fontFamily
                        font.pixelSize: 9
                        font.weight: Font.DemiBold
                        z: 21
                    }

                    MouseArea {
                        id: moveArea
                        anchors.fill: parent
                        enabled: (root.editableOverlays || root.editableProjectOverlays) && !overlayFrame.tracksAutomaticRegion
                        cursorShape: Qt.SizeAllCursor
                        preventStealing: true
                        z: 25
                        property point pressPoint
                        property real originX: 0
                        property real originY: 0

                        onPressed: function(mouse) {
                            root.selectedOverlayId = overlayFrame.overlay.id
                            overlayFrame.captureCurrentGeometry()
                            overlayFrame.moving = true
                            pressPoint = mapToItem(videoViewport, mouse.x, mouse.y)
                            originX = overlayFrame.manualX
                            originY = overlayFrame.manualY
                        }
                        onPositionChanged: function(mouse) {
                            if (!pressed || !overlayFrame.moving) return
                            const point = mapToItem(videoViewport, mouse.x, mouse.y)
                            const rect = root.renderedVideoRect()
                            overlayFrame.manualX = Math.max(
                                rect.x,
                                Math.min(
                                    rect.x + rect.width - overlayFrame.manualWidth,
                                    originX + point.x - pressPoint.x
                                )
                            )
                            overlayFrame.manualY = Math.max(
                                rect.y,
                                Math.min(
                                    rect.y + rect.height - overlayFrame.manualHeight,
                                    originY + point.y - pressPoint.y
                                )
                            )
                        }
                        onReleased: {
                            overlayFrame.moving = false
                            overlayFrame.awaitingCommit = true
                            root.commitOverlayGeometry(
                                overlayFrame.overlay,
                                overlayFrame.manualX,
                                overlayFrame.manualY,
                                overlayFrame.manualWidth,
                                overlayFrame.manualHeight
                            )
                        }
                        onCanceled: overlayFrame.cancelManipulation()
                    }

                    Rectangle {
                        width: 18
                        height: 18
                        anchors.right: parent.right
                        anchors.bottom: parent.bottom
                        // Keep the visible handle inside the saved rectangle.
                        // A frame flush with a video edge would otherwise clip
                        // half of the handle and make a 2% overlay unrecoverable.
                        anchors.rightMargin: 0
                        anchors.bottomMargin: 0
                        radius: 4
                        visible: overlayFrame.resizeHandleVisible("bottomRight")
                        color: overlayFrame.overlay.kind === "blur"
                            ? Theme.info : Theme.primary
                        border.width: 2
                        border.color: Theme.textPrimary
                        z: 30

                        MouseArea {
                            objectName: "overlayBottomRightResizeHandle"
                            anchors.fill: parent
                            anchors.margins: overlayFrame.resizeHandleHitMargin()
                            cursorShape: Qt.SizeFDiagCursor
                            preventStealing: true
                            property point pressPoint
                            property real originWidth: 0
                            property real originHeight: 0

                            onPressed: function(mouse) {
                                mouse.accepted = true
                                root.selectedOverlayId = overlayFrame.overlay.id
                                overlayFrame.captureCurrentGeometry()
                                overlayFrame.resizing = true
                                pressPoint = mapToItem(videoViewport, mouse.x, mouse.y)
                                originWidth = overlayFrame.manualWidth
                                originHeight = overlayFrame.manualHeight
                            }
                            onPositionChanged: function(mouse) {
                                if (!pressed || !overlayFrame.resizing) return
                                const point = mapToItem(videoViewport, mouse.x, mouse.y)
                                const rect = root.renderedVideoRect()
                                overlayFrame.manualWidth = Math.max(
                                    20,
                                    Math.min(
                                        rect.x + rect.width - overlayFrame.manualX,
                                        originWidth + point.x - pressPoint.x
                                    )
                                )
                                overlayFrame.manualHeight = Math.max(
                                    20,
                                    Math.min(
                                        rect.y + rect.height - overlayFrame.manualY,
                                        originHeight + point.y - pressPoint.y
                                    )
                                )
                            }
                            onReleased: {
                                overlayFrame.resizing = false
                                overlayFrame.awaitingCommit = true
                                root.commitOverlayGeometry(
                                    overlayFrame.overlay,
                                    overlayFrame.manualX,
                                    overlayFrame.manualY,
                                    overlayFrame.manualWidth,
                                    overlayFrame.manualHeight
                                )
                            }
                            onCanceled: overlayFrame.cancelManipulation()
                        }
                    }

                    Rectangle {
                        width: 18
                        height: 18
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.rightMargin: 0
                        anchors.topMargin: 0
                        radius: 4
                        visible: overlayFrame.resizeHandleVisible("topRight")
                        color: overlayFrame.overlay.kind === "blur"
                            ? Theme.info : Theme.primary
                        border.width: 2
                        border.color: Theme.textPrimary
                        z: 32

                        MouseArea {
                            objectName: "overlayTopRightResizeHandle"
                            anchors.fill: parent
                            anchors.margins: overlayFrame.resizeHandleHitMargin()
                            cursorShape: Qt.SizeBDiagCursor
                            preventStealing: true
                            property point pressPoint
                            property real originX: 0
                            property real originY: 0
                            property real originWidth: 0
                            property real originHeight: 0

                            onPressed: function(mouse) {
                                mouse.accepted = true
                                root.selectedOverlayId = overlayFrame.overlay.id
                                overlayFrame.captureCurrentGeometry()
                                overlayFrame.resizing = true
                                pressPoint = mapToItem(videoViewport, mouse.x, mouse.y)
                                originX = overlayFrame.manualX
                                originY = overlayFrame.manualY
                                originWidth = overlayFrame.manualWidth
                                originHeight = overlayFrame.manualHeight
                            }
                            onPositionChanged: function(mouse) {
                                if (!pressed || !overlayFrame.resizing) return
                                const point = mapToItem(videoViewport, mouse.x, mouse.y)
                                const rect = root.renderedVideoRect()
                                const bottom = originY + originHeight
                                overlayFrame.manualWidth = Math.max(
                                    20,
                                    Math.min(
                                        rect.x + rect.width - originX,
                                        originWidth + point.x - pressPoint.x
                                    )
                                )
                                overlayFrame.manualY = Math.max(
                                    rect.y,
                                    Math.min(bottom - 20, originY + point.y - pressPoint.y)
                                )
                                overlayFrame.manualHeight = bottom - overlayFrame.manualY
                            }
                            onReleased: {
                                overlayFrame.resizing = false
                                overlayFrame.awaitingCommit = true
                                root.commitOverlayGeometry(
                                    overlayFrame.overlay,
                                    overlayFrame.manualX,
                                    overlayFrame.manualY,
                                    overlayFrame.manualWidth,
                                    overlayFrame.manualHeight
                                )
                            }
                            onCanceled: overlayFrame.cancelManipulation()
                        }
                    }

                    Rectangle {
                        width: 18
                        height: 18
                        anchors.left: parent.left
                        anchors.top: parent.top
                        anchors.leftMargin: 0
                        anchors.topMargin: 0
                        radius: 4
                        visible: overlayFrame.resizeHandleVisible("topLeft")
                        color: overlayFrame.overlay.kind === "blur"
                            ? Theme.info : Theme.primary
                        border.width: 2
                        border.color: Theme.textPrimary
                        z: 31

                        MouseArea {
                            objectName: "overlayTopLeftResizeHandle"
                            anchors.fill: parent
                            anchors.margins: overlayFrame.resizeHandleHitMargin()
                            cursorShape: Qt.SizeFDiagCursor
                            preventStealing: true
                            property point pressPoint
                            property real originX: 0
                            property real originY: 0
                            property real originWidth: 0
                            property real originHeight: 0

                            onPressed: function(mouse) {
                                mouse.accepted = true
                                root.selectedOverlayId = overlayFrame.overlay.id
                                overlayFrame.captureCurrentGeometry()
                                overlayFrame.resizing = true
                                pressPoint = mapToItem(videoViewport, mouse.x, mouse.y)
                                originX = overlayFrame.manualX
                                originY = overlayFrame.manualY
                                originWidth = overlayFrame.manualWidth
                                originHeight = overlayFrame.manualHeight
                            }
                            onPositionChanged: function(mouse) {
                                if (!pressed || !overlayFrame.resizing) return
                                const point = mapToItem(videoViewport, mouse.x, mouse.y)
                                const rect = root.renderedVideoRect()
                                const right = originX + originWidth
                                const bottom = originY + originHeight
                                overlayFrame.manualX = Math.max(
                                    rect.x,
                                    Math.min(right - 20, originX + point.x - pressPoint.x)
                                )
                                overlayFrame.manualY = Math.max(
                                    rect.y,
                                    Math.min(bottom - 20, originY + point.y - pressPoint.y)
                                )
                                overlayFrame.manualWidth = right - overlayFrame.manualX
                                overlayFrame.manualHeight = bottom - overlayFrame.manualY
                            }
                            onReleased: {
                                overlayFrame.resizing = false
                                overlayFrame.awaitingCommit = true
                                root.commitOverlayGeometry(
                                    overlayFrame.overlay,
                                    overlayFrame.manualX,
                                    overlayFrame.manualY,
                                    overlayFrame.manualWidth,
                                    overlayFrame.manualHeight
                                )
                            }
                            onCanceled: overlayFrame.cancelManipulation()
                        }
                    }

                    Rectangle {
                        width: 18
                        height: 18
                        anchors.left: parent.left
                        anchors.bottom: parent.bottom
                        anchors.leftMargin: 0
                        anchors.bottomMargin: 0
                        radius: 4
                        visible: overlayFrame.resizeHandleVisible("bottomLeft")
                        color: overlayFrame.overlay.kind === "blur"
                            ? Theme.info : Theme.primary
                        border.width: 2
                        border.color: Theme.textPrimary
                        z: 33

                        MouseArea {
                            objectName: "overlayBottomLeftResizeHandle"
                            anchors.fill: parent
                            anchors.margins: overlayFrame.resizeHandleHitMargin()
                            cursorShape: Qt.SizeBDiagCursor
                            preventStealing: true
                            property point pressPoint
                            property real originX: 0
                            property real originY: 0
                            property real originWidth: 0
                            property real originHeight: 0

                            onPressed: function(mouse) {
                                mouse.accepted = true
                                root.selectedOverlayId = overlayFrame.overlay.id
                                overlayFrame.captureCurrentGeometry()
                                overlayFrame.resizing = true
                                pressPoint = mapToItem(videoViewport, mouse.x, mouse.y)
                                originX = overlayFrame.manualX
                                originY = overlayFrame.manualY
                                originWidth = overlayFrame.manualWidth
                                originHeight = overlayFrame.manualHeight
                            }
                            onPositionChanged: function(mouse) {
                                if (!pressed || !overlayFrame.resizing) return
                                const point = mapToItem(videoViewport, mouse.x, mouse.y)
                                const rect = root.renderedVideoRect()
                                const right = originX + originWidth
                                overlayFrame.manualX = Math.max(
                                    rect.x,
                                    Math.min(right - 20, originX + point.x - pressPoint.x)
                                )
                                overlayFrame.manualWidth = right - overlayFrame.manualX
                                overlayFrame.manualHeight = Math.max(
                                    20,
                                    Math.min(
                                        rect.y + rect.height - originY,
                                        originHeight + point.y - pressPoint.y
                                    )
                                )
                            }
                            onReleased: {
                                overlayFrame.resizing = false
                                overlayFrame.awaitingCommit = true
                                root.commitOverlayGeometry(
                                    overlayFrame.overlay,
                                    overlayFrame.manualX,
                                    overlayFrame.manualY,
                                    overlayFrame.manualWidth,
                                    overlayFrame.manualHeight
                                )
                            }
                            onCanceled: overlayFrame.cancelManipulation()
                        }
                    }
                }
            }

            // Frame-backed OCR/manual-text region editor. It intentionally
            // masks pointer input above all cue/overlay delegates while active
            // and stores only normalized full-frame geometry.
            Item {
                id: regionEditorLayer
                anchors.fill: parent
                visible: root.ocrRegionEditorMode.length > 0
                z: 900

                Rectangle {
                    readonly property var region:
                        root.appModel.currentProject.ocrDialogueRegion || []
                    visible: region.length === 4
                    x: root.renderedVideoRect().x
                        + Number(region[0]) * root.renderedVideoRect().width
                    y: root.renderedVideoRect().y
                        + Number(region[1]) * root.renderedVideoRect().height
                    width: Number(region[2]) * root.renderedVideoRect().width
                    height: Number(region[3]) * root.renderedVideoRect().height
                    color: Qt.rgba(0.12, 0.50, 0.78, 0.12)
                    border.width: 2
                    border.color: Theme.info
                    Text {
                        anchors.left: parent.left
                        anchors.bottom: parent.top
                        anchors.bottomMargin: Theme.space4
                        text: "VÙNG S1"
                        color: Theme.info
                        font.family: Theme.fontFamily
                        font.pixelSize: 10
                        font.weight: Font.DemiBold
                    }
                }

                Repeater {
                    model: root.appModel.currentProject.ocrExclusionRegions || []
                    delegate: Rectangle {
                        required property var modelData
                        x: root.renderedVideoRect().x
                            + Number(modelData[0]) * root.renderedVideoRect().width
                        y: root.renderedVideoRect().y
                            + Number(modelData[1]) * root.renderedVideoRect().height
                        width: Number(modelData[2]) * root.renderedVideoRect().width
                        height: Number(modelData[3]) * root.renderedVideoRect().height
                        color: Qt.rgba(0.72, 0.16, 0.20, 0.20)
                        border.width: 2
                        border.color: Theme.error
                        Text {
                            anchors.left: parent.left
                            anchors.bottom: parent.top
                            anchors.bottomMargin: Theme.space4
                            text: "KHÔNG OCR"
                            color: Theme.error
                            font.family: Theme.fontFamily
                            font.pixelSize: 10
                            font.weight: Font.DemiBold
                        }
                    }
                }

                Rectangle {
                    visible: root.regionDraftRect.width > 0
                        && root.regionDraftRect.height > 0
                    x: root.regionDraftRect.x
                    y: root.regionDraftRect.y
                    width: root.regionDraftRect.width
                    height: root.regionDraftRect.height
                    color: root.ocrRegionEditorMode === "exclusion"
                        ? Qt.rgba(0.72, 0.16, 0.20, 0.22)
                        : Qt.rgba(0.12, 0.50, 0.78, 0.16)
                    border.width: 2
                    border.color: root.ocrRegionEditorMode === "exclusion"
                        ? Theme.error : Theme.info
                }

                MouseArea {
                    anchors.fill: parent
                    acceptedButtons: Qt.LeftButton
                    cursorShape: Qt.CrossCursor
                    preventStealing: true

                    function clampedPoint(mouse) {
                        const rect = root.renderedVideoRect()
                        return Qt.point(
                            Math.max(rect.x, Math.min(rect.x + rect.width, mouse.x)),
                            Math.max(rect.y, Math.min(rect.y + rect.height, mouse.y))
                        )
                    }

                    onPressed: function(mouse) {
                        mouse.accepted = true
                        root.pause()
                        const point = clampedPoint(mouse)
                        root.regionPressPoint = point
                        root.regionDraftRect = Qt.rect(point.x, point.y, 0, 0)
                    }
                    onPositionChanged: function(mouse) {
                        if (!pressed) return
                        const point = clampedPoint(mouse)
                        const left = Math.min(root.regionPressPoint.x, point.x)
                        const top = Math.min(root.regionPressPoint.y, point.y)
                        root.regionDraftRect = Qt.rect(
                            left,
                            top,
                            Math.abs(point.x - root.regionPressPoint.x),
                            Math.abs(point.y - root.regionPressPoint.y)
                        )
                    }
                    onReleased: function(mouse) {
                        const rect = root.renderedVideoRect()
                        const draft = root.regionDraftRect
                        if (draft.width >= 8 && draft.height >= 8
                                && rect.width > 0 && rect.height > 0) {
                            root.regionDrawn(
                                root.ocrRegionEditorMode,
                                (draft.x - rect.x) / rect.width,
                                (draft.y - rect.y) / rect.height,
                                draft.width / rect.width,
                                draft.height / rect.height
                            )
                        }
                        root.regionDraftRect = Qt.rect(0, 0, 0, 0)
                    }
                    onCanceled: root.regionDraftRect = Qt.rect(0, 0, 0, 0)
                }

                Rectangle {
                    anchors.left: parent.left
                    anchors.top: parent.top
                    anchors.margins: Theme.space12
                    width: editorHint.implicitWidth + Theme.space16
                    height: editorHint.implicitHeight + Theme.space8
                    color: Theme.surfaceRaised
                    border.width: 1
                    border.color: Theme.borderStrong
                    radius: Theme.radiusSmall
                    Text {
                        id: editorHint
                        anchors.centerIn: parent
                        text: root.ocrRegionEditorMode === "dialogue"
                            ? "Kéo vùng thoại S1 trên khung hình này"
                            : (root.ocrRegionEditorMode === "exclusion"
                                ? "Kéo vùng cần bỏ qua OCR"
                                : "Kéo vùng đặt chữ thủ công")
                        color: Theme.textPrimary
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                    }
                }
            }

            EmptyState {
                anchors.fill: parent
                visible: !root.appModel.currentProject.hasVideo || Boolean(root.errorText)
                title: root.errorText
                    ? "Không thể mở preview"
                    : "Preview chưa có media"
                description: root.errorText
                    ? root.errorText
                    : "Nhập video để phát, tạm dừng, tắt tiếng và seek trực tiếp."
                iconName: "panels-top-left"
            }
        }

        Rectangle {
            id: transport
            objectName: "previewTransport"
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            height: root.editableOverlays ? 0 : 46
            color: Theme.surfacePanel
            visible: !root.editableOverlays
            z: 120
            HoverHandler {
                onHoveredChanged: {
                    root.transportHovered = hovered
                    if (hovered) root.showControls()
                    else root.scheduleControlsHide()
                }
            }

            RowLayout {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: 42
                anchors.leftMargin: Theme.space8
                anchors.rightMargin: Theme.space8
                spacing: Theme.space8

                DsIconButton {
                    iconName: player.playbackState === MediaPlayer.PlayingState ? "pause" : "play"
                    tooltipText: player.playbackState === MediaPlayer.PlayingState ? "Tạm dừng" : "Phát"
                    enabled: root.appModel.currentProject.hasVideo
                    disabledReason: "Chưa có video để phát."
                    onClicked: root.togglePlayback()
                }

                DsIconButton {
                    iconName: "chevron-left"
                    tooltipText: "Lùi " + root.appModel.timelineSeekStepMs + " ms"
                    enabled: root.appModel.currentProject.hasVideo && root.positionMs > 0
                    disabledReason: "Video đang ở đầu timeline."
                    onClicked: root.seekRelative(-root.appModel.timelineSeekStepMs)
                }

                DsIconButton {
                    iconName: "chevron-right"
                    tooltipText: "Tiến " + root.appModel.timelineSeekStepMs + " ms"
                    enabled: root.appModel.currentProject.hasVideo
                        && root.positionMs < root.durationMs
                    disabledReason: "Video đang ở cuối timeline."
                    onClicked: root.seekRelative(root.appModel.timelineSeekStepMs)
                }

                TextField {
                    id: exactTimeField
                    objectName: "previewExactTimeField"
                    Layout.preferredWidth: 114
                    text: root.timeText(root.positionMs)
                    enabled: root.appModel.currentProject.hasVideo
                    selectByMouse: true
                    Accessible.name: "Thời gian phát chính xác HH:MM:SS.mmm"
                    onEditingFinished: {
                        root.seekTimeText(text)
                        text = root.timeText(root.positionMs)
                    }
                    Binding {
                        target: exactTimeField
                        property: "text"
                        value: root.timeText(root.positionMs)
                        when: !exactTimeField.activeFocus
                    }
                    ToolTip.visible: hovered
                    ToolTip.text: "Nhập HH:MM:SS.mmm rồi Enter để tua chính xác"
                    color: Theme.textSecondary
                    font.family: Theme.timecodeFamily
                    font.pixelSize: 10
                }

                Text {
                    text: "/ " + root.timeText(root.durationMs)
                    color: Theme.textSecondary
                    font.family: Theme.timecodeFamily
                    font.pixelSize: 10
                    font.letterSpacing: 0.2
                }

                Slider {
                    id: seekSlider
                    Layout.fillWidth: true
                    Layout.preferredHeight: 28
                    from: 0
                    to: Math.max(1, root.durationMs)
                    value: root.positionMs
                    enabled: root.appModel.currentProject.hasVideo
                    onMoved: root.seekTimeline(
                        value, player.playbackState === MediaPlayer.PlayingState
                    )
                    Accessible.name: "Vị trí phát video"
                    background: Rectangle {
                        x: seekSlider.leftPadding
                        y: seekSlider.topPadding + seekSlider.availableHeight / 2 - height / 2
                        width: seekSlider.availableWidth
                        height: 4
                        radius: 2
                        color: Theme.borderStrong
                        Rectangle {
                            width: seekSlider.visualPosition * parent.width
                            height: parent.height
                            radius: parent.radius
                            color: Theme.primary
                        }
                    }
                    handle: Rectangle {
                        x: seekSlider.leftPadding
                            + seekSlider.visualPosition * (seekSlider.availableWidth - width)
                        y: seekSlider.topPadding + seekSlider.availableHeight / 2 - height / 2
                        width: 14
                        height: 14
                        radius: 7
                        color: seekSlider.pressed ? Theme.primaryPressed : Theme.primaryHover
                        border.width: seekSlider.visualFocus ? 2 : 0
                        border.color: Theme.focusRing
                    }
                }

                Text {
                    text: root.appModel.previewProviderId
                    visible: root.width >= 760
                    color: Theme.textMuted
                    font.family: Theme.timecodeFamily
                    font.pixelSize: 9
                }

                Item {
                    id: volumeControl
                    implicitWidth: root.volumeHovered ? 132 : 36
                    implicitHeight: 36
                    Layout.preferredWidth: implicitWidth
                    HoverHandler {
                        onHoveredChanged: {
                            root.volumeHovered = hovered
                            if (hovered) root.showControls()
                            else root.scheduleControlsHide()
                        }
                    }
                    Behavior on implicitWidth {
                        NumberAnimation { duration: Theme.motionFast }
                    }
                    DsIconButton {
                        anchors.left: parent.left
                        anchors.verticalCenter: parent.verticalCenter
                        iconName: root.muted || root.masterVolume <= 0
                            ? "volume-x"
                            : (root.masterVolume < 0.5 ? "volume-1" : "volume-2")
                        tooltipText: root.muted ? "Bật tiếng" : "Tắt tiếng"
                        active: root.muted
                        onClicked: root.toggleMute()
                    }
                    Slider {
                        id: volumeSlider
                        visible: root.volumeHovered
                        anchors.left: parent.left
                        anchors.leftMargin: 34
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        from: 0; to: 100
                        value: root.appModel.previewMasterVolumePercent
                        onMoved: root.appModel.setPreviewMasterVolumePercent(value)
                        Accessible.name: "Âm lượng preview"
                    }
                }

                /* Keep compact step controls available for keyboard users and
                   for precise 5% adjustments when the hover slider is closed. */
                DsIconButton {
                    visible: false
                    enabled: root.appModel.currentProject.hasVideo
                    disabledReason: "Chưa có video để điều chỉnh âm thanh."
                }
            }
        }
    }
}
