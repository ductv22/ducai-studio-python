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
    // Transport is an overlay, not permanent chrome. It appears only while
    // the pointer is over the video/transport surface and fades immediately
    // after the pointer leaves, matching the familiar YouTube interaction.
    property bool controlsVisible: false
    property bool viewportHovered: false
    property bool transportHovered: false
    readonly property bool controlsHovered: viewportHovered || transportHovered
    property bool volumeHovered: false
    readonly property int playbackState: player.playbackState
    readonly property int voicePlaybackState: voicePlayer.playbackState
    readonly property int mediaStatus: player.mediaStatus
    readonly property real positionMs: root.appModel.playheadUs / 1000
    readonly property real sourcePositionMs: player.position
    readonly property real voicePositionMs: voicePlayer.position
    readonly property real durationMs: root.appModel.currentProject.durationUs / 1000
    readonly property bool muted: audioOutput.muted && voiceAudio.muted
    readonly property real masterVolume: root.appModel.previewMasterVolumePercent / 100
    property var activeSubtitle: {
        const playhead = root.appModel.playheadUs
        const revision = root.appModel.currentProject.revision
        return root.appModel.currentProject.subtitleAt(playhead)
    }
    property var activeSubtitleRegions: {
        const playhead = root.appModel.playheadUs
        const revision = root.appModel.currentProject.revision
        return root.appModel.currentProject.sourceSubtitleRegionsAt(playhead)
    }
    property var activeSceneTexts: {
        const playhead = root.appModel.playheadUs
        const revision = root.appModel.currentProject.revision
        const visualRevision = root.appModel.currentProject.subtitleVisualRevision
        return root.appModel.currentProject.sceneTextsAt(playhead)
    }
    readonly property string activeSubtitleText: activeSubtitle.translated_text
        || activeSubtitle.source_text || ""
    property var blurOverlay: {
        const revision = root.appModel.currentProject.revision
        return root.appModel.currentProject.overlay("overlay-default-blur")
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

    function subtitleHorizontalAlignment() {
        if (root.appModel.exportSubtitleAlignment === 1) return Text.AlignLeft
        if (root.appModel.exportSubtitleAlignment === 3) return Text.AlignRight
        return Text.AlignHCenter
    }

    function sceneDisplayText(cue) {
        const raw = String(cue.translated_text || cue.source_text || "").trim()
        const lines = raw.split("\n").map(function(line) {
            return line.trim().replace(/\s+/g, " ")
        }).filter(function(line) { return line.length > 0 })
        const value = lines.join("\n")
        if (cue.orientation !== "vertical") return value
        const words = value.split(/\s+/).filter(function(word) { return word.length > 0 })
        return (words.length > 1 ? words : value.split("")).join("\n")
    }

    function dialogueFontPixels(cue, scaleFactor) {
        const requested = Number(cue && cue.text_font_size || 12)
        const editorScale = Math.max(
            1,
            Math.min(100, isFinite(requested) ? requested : 12)
        ) / 12.0
        return Math.max(
            1,
            root.appModel.exportFontSize * scaleFactor * editorScale
        )
    }

    function subtitleFontFamily(cue, fallback) {
        const style = String(cue && cue.style_hint || "standard")
        if (style === "calligraphy") return "Playwrite VN"
        if (style === "calligraphy_soft") return "Dancing Script"
        if (style === "calligraphy_bold") return "Lobster"
        if (style === "calligraphy_ink") return "Agbalumo"
        if (style === "calligraphy_brush") return "Comforter Brush"
        if (style === "calligraphy_water") return "Water Brush"
        if (style === "calligraphy_splash") return "Splash"
        if (style === "calligraphy_hurricane") return "Hurricane"
        return fallback
    }

    function sceneFontPixels(cue, panelWidth, panelHeight) {
        const value = sceneDisplayText(cue)
        const lines = value.split("\n")
        const glyph = sceneGlyphRect(cue)
        const glyphWidth = Math.max(1, glyph.width)
        const glyphHeight = Math.max(1, glyph.height)
        const legacyScaleValue = Number(cue.scene_font_scale || 1.0)
        const legacyScale = Math.max(0.5, Math.min(2.0,
            isFinite(legacyScaleValue) ? legacyScaleValue : 1.0))
        const editorSizeValue = Number(cue.text_font_size || 12)
        const editorScale = Math.max(1, Math.min(100,
            isFinite(editorSizeValue) ? editorSizeValue : 12)) / 12.0
        const fontScale = legacyScale * editorScale
        let longest = 1
        for (let index = 0; index < lines.length; ++index)
            longest = Math.max(longest, lines[index].length)
        let panelFit
        let sourceSize
        const calligraphy = String(cue.style_hint || "").indexOf("calligraphy") === 0
        const widthRatio = calligraphy ? 1.96 : 1.28
        const lineHeightRatio = calligraphy ? 2.55 : 1.32
        if (cue.orientation === "vertical") {
            panelFit = Math.min(
                panelWidth * 0.88 / Math.max(1.0, longest * widthRatio),
                panelHeight * 0.88 / Math.max(1.25,
                    lines.length * lineHeightRatio)
            )
            sourceSize = Math.min(
                glyphWidth * 0.92,
                glyphHeight / Math.max(1.0, lines.length * 1.08)
            )
        } else {
            panelFit = Math.min(
                panelWidth * 0.88 / Math.max(1.0, longest * widthRatio),
                panelHeight * 0.88 / Math.max(1.0,
                    lines.length * lineHeightRatio)
            )
            sourceSize = glyphHeight * 0.92
        }
        const automaticSize = Math.min(sourceSize, panelFit)
        const safeRenderMax = Math.max(
            panelWidth, panelHeight, glyphWidth, glyphHeight
        ) * 8
        return Math.max(1, Math.min(automaticSize * fontScale, safeRenderMax))
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
                && root.appModel.isCueSelected(String(cue.cue_id || ""))) {
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
        if (normalized.width <= 0 || normalized.height <= 0)
            normalized = Qt.rect(0.10, 0.74, 0.80, 0.16)
        let result = Qt.rect(
            video.x + normalized.x * video.width,
            video.y + normalized.y * video.height,
            normalized.width * video.width,
            normalized.height * video.height
        )
        if (root.subtitleGeometryActive
                && root.appModel.isCueSelected(String(cue.cue_id || ""))) {
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
        if (!root.appModel.isCueSelected(String(cue.cue_id)))
            root.appModel.selectCueWithAdditive(
                String(cue.cue_id),
                Number(cue.start_us || 0),
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
        root.subtitleGeometryActive = true
    }

    function updateSubtitleGeometry(viewportX, viewportY, baseWidth, baseHeight) {
        if (!root.subtitleGeometryActive) return
        const video = renderedVideoRect()
        const dx = viewportX - root.subtitleGeometryPress.x
        const dy = viewportY - root.subtitleGeometryPress.y
        if (root.subtitleGeometryMode === "resize") {
            root.subtitleGeometryScaleX = Math.max(0.1, (baseWidth + dx) / Math.max(1, baseWidth))
            root.subtitleGeometryScaleY = Math.max(0.1, (baseHeight + dy) / Math.max(1, baseHeight))
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
            root.appModel.transformSelectedSubtitleGeometry(dx, dy, sx, sy)
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
    }

    function validSceneBackgroundColor(value) {
        return /^#[0-9a-fA-F]{6}$/.test(String(value || ""))
    }

    function isInkBackgroundStyle(value) {
        const style = String(value || "solid")
        return style === "ink_splash"
            || style === "ink_wash"
            || style === "ink_cloud"
            || style === "brush_stroke"
    }

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
        onPositionChanged: {
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
        else pause()
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
        function onSubtitleBusyChanged() {
            if (root.subtitleGeometryPending && !root.appModel.subtitleBusy)
                root.resetSubtitleGeometry()
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
        onActivated: root.seekRelative(-5_000)
    }
    Shortcut {
        sequence: "Right"
        context: Qt.WindowShortcut
        enabled: root.visible && !root.textEditorHasFocus()
        onActivated: root.seekRelative(5_000)
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
            anchors.bottom: parent.bottom
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
                x: filteredVideo.x
                y: filteredVideo.y
                width: filteredVideo.width
                height: filteredVideo.height
                z: 20
                enabled: !root.editableOverlays
                    && root.ocrRegionEditorMode.length === 0
                    && root.colorPickerTarget.length === 0
                    && !root.subtitleGeometryActive
                acceptedButtons: Qt.LeftButton
                cursorShape: Qt.PointingHandCursor
                onClicked: root.togglePlayback()
                Accessible.name: "Phát hoặc tạm dừng video"
            }

            // Scene text is separate from dialogue subtitles. Only accepted
            // cues alter the picture: blur/fill the detected Chinese glyphs,
            // retain the surrounding panel and place the Vietnamese label in
            // the panel. Review cues show a yellow editor guide but are not
            // silently rendered into the export.
            Repeater {
                model: root.appModel.exportSubtitleEnabled ? root.activeSceneTexts : []
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

                    Item {
                        id: sceneGlyphCover
                        x: sceneTextFrame.glyphRect.x
                        y: sceneTextFrame.glyphRect.y
                        width: sceneTextFrame.glyphRect.width
                        height: sceneTextFrame.glyphRect.height
                        visible: sceneTextFrame.accepted && width > 0 && height > 0

                        ShaderEffectSource {
                            id: sceneGlyphSource
                            width: Math.max(1, sceneGlyphCover.width)
                            height: Math.max(1, sceneGlyphCover.height)
                            visible: false
                            live: true
                            recursive: true
                            sourceItem: filteredVideo
                            sourceRect: Qt.rect(
                                sceneGlyphCover.x - filteredVideo.x,
                                sceneGlyphCover.y - filteredVideo.y,
                                width,
                                height
                            )
                        }

                        MultiEffect {
                            anchors.fill: parent
                            source: sceneGlyphSource
                            blurEnabled: true
                            blurMax: 64
                            blur: Math.max(0.01, Math.min(
                                1.0, 30 * root.subtitleScale() / 64
                            ))
                            autoPaddingEnabled: false
                        }

                    }

                    Rectangle {
                        id: sceneTextBackground
                        readonly property real padX: sceneTextLabel.font.pixelSize
                            * Number(sceneTextFrame.modelData.background_padding_x ?? 20)
                            / 100
                        readonly property real padY: sceneTextLabel.font.pixelSize
                            * Number(sceneTextFrame.modelData.background_padding_y ?? 10)
                            / 100
                        x: sceneTextFrame.panelRect.x
                            + (sceneTextFrame.panelRect.width - width) / 2
                        y: sceneTextFrame.panelRect.y
                            + (sceneTextFrame.panelRect.height - height) / 2
                        width: Math.max(1, sceneTextLabel.paintedWidth + padX * 2)
                        height: Math.max(1, sceneTextLabel.paintedHeight + padY * 2)
                        visible: sceneTextFrame.accepted
                            && root.validSceneBackgroundColor(
                                sceneTextFrame.modelData.background_color
                            )
                            && String(
                                sceneTextFrame.modelData.background_style || "solid"
                            ) === "solid"
                        color: sceneTextFrame.modelData.background_color || "transparent"
                        opacity: sceneTextFrame.presentationOpacity * Number(
                            sceneTextFrame.modelData.background_opacity_percent ?? 90
                        ) / 100
                        radius: Math.min(8, Math.max(2, sceneTextLabel.font.pixelSize * 0.08))
                    }

                    InkSplashBackground {
                        id: sceneInkBackground
                        readonly property rect videoRect: root.renderedVideoRect()
                        readonly property real extraX: Math.max(
                            3,
                            sceneTextLabel.font.pixelSize
                                * (vertical ? 0.34 : 0.22)
                        )
                        readonly property real extraY: Math.max(
                            3,
                            sceneTextLabel.font.pixelSize
                                * (vertical ? 0.22 : 0.34)
                        )
                        readonly property real leftEdge: Math.max(
                            videoRect.x, sceneTextBackground.x - extraX
                        )
                        readonly property real topEdge: Math.max(
                            videoRect.y, sceneTextBackground.y - extraY
                        )
                        readonly property real rightEdge: Math.min(
                            videoRect.x + videoRect.width,
                            sceneTextBackground.x + sceneTextBackground.width + extraX
                        )
                        readonly property real bottomEdge: Math.min(
                            videoRect.y + videoRect.height,
                            sceneTextBackground.y + sceneTextBackground.height + extraY
                        )
                        x: leftEdge
                        y: topEdge
                        width: Math.max(1, rightEdge - leftEdge)
                        height: Math.max(1, bottomEdge - topEdge)
                        visible: sceneTextFrame.accepted
                            && root.validSceneBackgroundColor(
                                sceneTextFrame.modelData.background_color
                            )
                            && root.isInkBackgroundStyle(
                                sceneTextFrame.modelData.background_style || "solid"
                            )
                        inkColor: sceneTextFrame.modelData.background_color || "#080808"
                        variant: String(
                            sceneTextFrame.modelData.background_style || "ink_splash"
                        )
                        vertical: sceneTextFrame.modelData.orientation === "vertical"
                        mirrored: Number(sceneTextFrame.modelData.index || 0) % 2 === 0
                        opacity: sceneTextFrame.presentationOpacity * Number(
                            sceneTextFrame.modelData.background_opacity_percent ?? 90
                        ) / 100
                    }

                    Text {
                        id: sceneTextLabel
                        x: sceneTextFrame.panelRect.x
                        y: sceneTextFrame.panelRect.y
                        width: sceneTextFrame.panelRect.width
                        height: sceneTextFrame.panelRect.height
                        visible: sceneTextFrame.accepted && width > 0 && height > 0
                        text: root.sceneDisplayText(sceneTextFrame.modelData)
                        color: sceneTextFrame.modelData.text_color || "#FFFFFFFF"
                        opacity: sceneTextFrame.presentationOpacity * Number(
                            sceneTextFrame.modelData.text_opacity_percent ?? 100
                        ) / 100
                        style: Text.Outline
                        styleColor: "#A0000000"
                        font.family: root.subtitleFontFamily(
                            sceneTextFrame.modelData, Theme.fontFamily
                        )
                        font.pixelSize: root.sceneFontPixels(
                            sceneTextFrame.modelData, width, height
                        )
                        font.letterSpacing: font.pixelSize * Number(
                            sceneTextFrame.modelData.letter_spacing_percent || 0
                        ) / 100
                        font.italic: Boolean(sceneTextFrame.modelData.font_italic)
                        font.weight: Boolean(sceneTextFrame.modelData.font_bold)
                            ? Font.Bold
                            : (String(
                                sceneTextFrame.modelData.style_hint || ""
                            ).indexOf("calligraphy") === 0
                                ? Font.Medium : Font.DemiBold)
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        wrapMode: Text.NoWrap
                        elide: Text.ElideNone
                        clip: false
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
                            if (mouse.button === Qt.LeftButton)
                                root.finishSubtitleGeometry()
                        }
                        onCanceled: root.finishSubtitleGeometry()
                        onClicked: function(mouse) {
                            if (geometryDragged) return
                            if (mouse.button === Qt.RightButton) {
                                root.openSubtitleMenu(sceneTextFrame.modelData)
                                return
                            }
                            if (selectedOnPress)
                                root.appModel.selectCueWithAdditive(
                                    String(sceneTextFrame.modelData.cue_id || ""),
                                    Number(sceneTextFrame.modelData.start_us || 0),
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
                        visible: !root.editableOverlays
                            && sceneTextFrame.accepted
                            && root.appModel.isCueSelected(
                                String(sceneTextFrame.modelData.cue_id || "")
                            )
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
                            onReleased: root.finishSubtitleGeometry()
                            onCanceled: root.finishSubtitleGeometry()
                        }
                    }
                }
            }

            Item {
                id: liveSubtitlePreview
                objectName: "liveSubtitlePreview"
                readonly property rect videoRect: root.renderedVideoRect()
                readonly property rect cuePanelRect: root.dialoguePanelRect(
                    root.activeSubtitle
                )
                readonly property bool customGeometry:
                    root.normalizedSceneRect(root.activeSubtitle, "panel_box").width > 0
                    || (root.subtitleGeometryActive
                        && root.appModel.isCueSelected(
                            String(root.activeSubtitle.cue_id || "")
                        ))
                readonly property real scaleFactor: root.subtitleScale()
                readonly property real outlinePixels: Math.max(
                    0,
                    root.appModel.exportSubtitleOutlineWidth * scaleFactor
                )
                readonly property var outlineOffsets: [
                    Qt.point(-1, -1), Qt.point(0, -1), Qt.point(1, -1),
                    Qt.point(-1, 0), Qt.point(1, 0),
                    Qt.point(-1, 1), Qt.point(0, 1), Qt.point(1, 1)
                ]
                x: customGeometry
                    ? cuePanelRect.x
                    : videoRect.x + Math.max(8, 40 * scaleFactor)
                y: customGeometry ? cuePanelRect.y : videoRect.y
                width: customGeometry
                    ? cuePanelRect.width
                    : Math.max(0, videoRect.width - Math.max(16, 80 * scaleFactor))
                height: customGeometry
                    ? cuePanelRect.height
                    : Math.max(
                        0,
                        videoRect.height
                            - root.appModel.exportSubtitleMarginBottom * scaleFactor
                    )
                visible: root.appModel.exportSubtitleEnabled
                    && root.activeSubtitleText.length > 0
                    && videoRect.width > 0 && videoRect.height > 0
                // Blur/logo overlays affect the video layer only. Keep the
                // generated subtitle above them so an editor can deliberately
                // cover the original hard-sub without hiding the new subtitle.
                z: 110

                Rectangle {
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.bottom: parent.bottom
                    width: Math.min(
                        parent.width,
                        Math.max(1, subtitleMainText.paintedWidth + Theme.space16)
                    )
                    height: Math.max(1, subtitleMainText.paintedHeight + Theme.space8)
                    visible: root.validSceneBackgroundColor(
                        root.activeSubtitle.background_color
                    )
                    color: root.activeSubtitle.background_color || "transparent"
                    opacity: Number(
                        root.activeSubtitle.background_opacity_percent ?? 90
                    ) / 100
                    radius: Theme.radiusSmall
                }

                Repeater {
                    model: root.appModel.exportSubtitleOutlineWidth > 0
                        ? liveSubtitlePreview.outlineOffsets : []
                    delegate: Text {
                        required property var modelData
                        x: modelData.x * liveSubtitlePreview.outlinePixels
                        y: modelData.y * liveSubtitlePreview.outlinePixels
                        width: liveSubtitlePreview.width
                        height: liveSubtitlePreview.height
                        text: root.sceneDisplayText(root.activeSubtitle)
                        color: root.appModel.exportSubtitleOutlineColor
                        font.family: root.subtitleFontFamily(
                            root.activeSubtitle, root.appModel.exportFontFamily
                        )
                        font.pixelSize: root.dialogueFontPixels(
                            root.activeSubtitle,
                            liveSubtitlePreview.scaleFactor
                        )
                        font.letterSpacing: font.pixelSize * Number(
                            root.activeSubtitle.letter_spacing_percent || 0
                        ) / 100
                        font.bold: Boolean(root.activeSubtitle.font_bold)
                        font.italic: Boolean(root.activeSubtitle.font_italic)
                        horizontalAlignment: root.subtitleHorizontalAlignment()
                        verticalAlignment: Text.AlignBottom
                        wrapMode: Text.Wrap
                        elide: Text.ElideNone
                    }
                }

                Text {
                    id: subtitleMainText
                    anchors.fill: parent
                    text: root.sceneDisplayText(root.activeSubtitle)
                    color: root.activeSubtitle.text_color
                        || root.appModel.exportSubtitlePrimaryColor
                    opacity: root.appModel.exportSubtitleOpacityPercent / 100
                        * Number(
                            root.activeSubtitle.text_opacity_percent ?? 100
                        ) / 100
                    font.family: root.subtitleFontFamily(
                        root.activeSubtitle, root.appModel.exportFontFamily
                    )
                    font.pixelSize: root.dialogueFontPixels(
                        root.activeSubtitle,
                        liveSubtitlePreview.scaleFactor
                    )
                    font.letterSpacing: font.pixelSize * Number(
                        root.activeSubtitle.letter_spacing_percent || 0
                    ) / 100
                    font.bold: Boolean(root.activeSubtitle.font_bold)
                    font.italic: Boolean(root.activeSubtitle.font_italic)
                    horizontalAlignment: root.subtitleHorizontalAlignment()
                    verticalAlignment: Text.AlignBottom
                    wrapMode: Text.Wrap
                    elide: Text.ElideNone
                }

                MouseArea {
                    x: liveSubtitlePreview.customGeometry
                        ? 0
                        : Math.max(0, (parent.width - width) / 2)
                    y: liveSubtitlePreview.customGeometry
                        ? 0
                        : Math.max(0, parent.height - height)
                    width: liveSubtitlePreview.customGeometry
                        ? parent.width
                        : Math.min(
                            parent.width,
                            Math.max(
                                48,
                                subtitleMainText.paintedWidth + Theme.space16
                            )
                        )
                    height: liveSubtitlePreview.customGeometry
                        ? parent.height
                        : Math.min(
                            parent.height,
                            Math.max(
                                32,
                                subtitleMainText.paintedHeight + Theme.space8
                            )
                        )
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
                        if (mouse.button === Qt.LeftButton)
                            root.finishSubtitleGeometry()
                    }
                    onCanceled: root.finishSubtitleGeometry()
                    onClicked: function(mouse) {
                        if (geometryDragged) return
                        if (mouse.button === Qt.RightButton) {
                            root.openSubtitleMenu(root.activeSubtitle)
                            return
                        }
                        if (selectedOnPress)
                            root.appModel.selectCueWithAdditive(
                                String(root.activeSubtitle.cue_id || ""),
                                Number(root.activeSubtitle.start_us || 0),
                                Boolean(mouse.modifiers & Qt.ControlModifier)
                            )
                    }
                    onDoubleClicked: function(mouse) {
                        if (mouse.button === Qt.LeftButton)
                            root.openSubtitleEditor(root.activeSubtitle)
                    }
                }

                Rectangle {
                    anchors.fill: parent
                    visible: !root.editableOverlays && root.appModel.isCueSelected(
                        String(root.activeSubtitle.cue_id || "")
                    )
                    color: "transparent"
                    border.width: 2
                    border.color: Theme.primary
                    z: 3
                }

                Rectangle {
                    x: parent.width - width / 2
                    y: parent.height - height / 2
                    width: 14
                    height: 14
                    radius: 2
                    visible: root.appModel.isCueSelected(
                        String(root.activeSubtitle.cue_id || "")
                    )
                    color: Theme.primary
                    border.width: 1
                    border.color: Theme.textPrimary
                    z: 6
                    MouseArea {
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
                        onReleased: root.finishSubtitleGeometry()
                        onCanceled: root.finishSubtitleGeometry()
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

            // A source SRT can contain overlapping cues. Render every active
            // source region instead of reusing subtitleAt(), which deliberately
            // returns only one text cue for the Vietnamese subtitle renderer.
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
                        text: trackedBlurFrame.displayedOverlay.region_source === "ocr"
                            ? "BLUR THEO OCR" : "BLUR ƯỚC LƯỢNG"
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
                    z: root.editableOverlays
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
                        if (overlayFrame.tracksAutomaticRegion || !root.editableOverlays
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
                        visible: root.editableOverlays
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
                        visible: root.editableOverlays
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
                        enabled: root.editableOverlays && !overlayFrame.tracksAutomaticRegion
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
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            height: 58
            // A translucent control strip keeps the video visible underneath
            // while retaining enough contrast for controls and timecode.
            color: Qt.rgba(0.03, 0.06, 0.09, 0.78)
            opacity: root.controlsVisible ? 1.0 : 0.0
            visible: !root.editableOverlays && opacity > 0.01
            z: 120
            Behavior on opacity {
                NumberAnimation { duration: Theme.motionFast }
            }

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
                    tooltipText: "Lùi 5 giây"
                    enabled: root.appModel.currentProject.hasVideo && root.positionMs > 0
                    disabledReason: "Video đang ở đầu timeline."
                    onClicked: root.seekRelative(-5_000)
                }

                DsIconButton {
                    iconName: "chevron-right"
                    tooltipText: "Tiến 5 giây"
                    enabled: root.appModel.currentProject.hasVideo
                        && root.positionMs < root.durationMs
                    disabledReason: "Video đang ở cuối timeline."
                    onClicked: root.seekRelative(5_000)
                }

                Text {
                    text: root.timeText(root.positionMs) + " / " + root.timeText(root.durationMs)
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
