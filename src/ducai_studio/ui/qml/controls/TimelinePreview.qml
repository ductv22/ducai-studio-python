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
        return root.appModel.playheadUs >= startUs && root.appModel.playheadUs < endUs
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
        if (root.controlsHovered || root.volumeHovered
                || player.playbackState !== MediaPlayer.PlayingState) {
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

            Item {
                id: liveSubtitlePreview
                objectName: "liveSubtitlePreview"
                readonly property rect videoRect: root.renderedVideoRect()
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
                x: videoRect.x + Math.max(8, 40 * scaleFactor)
                y: videoRect.y
                width: Math.max(0, videoRect.width - Math.max(16, 80 * scaleFactor))
                height: Math.max(
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

                Repeater {
                    model: root.appModel.exportSubtitleOutlineWidth > 0
                        ? liveSubtitlePreview.outlineOffsets : []
                    delegate: Text {
                        required property var modelData
                        x: modelData.x * liveSubtitlePreview.outlinePixels
                        y: modelData.y * liveSubtitlePreview.outlinePixels
                        width: liveSubtitlePreview.width
                        height: liveSubtitlePreview.height
                        text: root.activeSubtitleText
                        color: root.appModel.exportSubtitleOutlineColor
                        font.family: root.appModel.exportFontFamily
                        font.pixelSize: Math.max(
                            8,
                            root.appModel.exportFontSize * liveSubtitlePreview.scaleFactor
                        )
                        horizontalAlignment: root.subtitleHorizontalAlignment()
                        verticalAlignment: Text.AlignBottom
                        wrapMode: Text.Wrap
                        elide: Text.ElideNone
                    }
                }

                Text {
                    anchors.fill: parent
                    text: root.activeSubtitleText
                    color: root.appModel.exportSubtitlePrimaryColor
                    opacity: root.appModel.exportSubtitleOpacityPercent / 100
                    font.family: root.appModel.exportFontFamily
                    font.pixelSize: Math.max(
                        8,
                        root.appModel.exportFontSize * liveSubtitlePreview.scaleFactor
                    )
                    horizontalAlignment: root.subtitleHorizontalAlignment()
                    verticalAlignment: Text.AlignBottom
                    wrapMode: Text.Wrap
                    elide: Text.ElideNone
                }
            }

            Repeater {
                model: root.appModel.currentProject.overlays
                delegate: Rectangle {
                    id: overlayFrame
                    required property var modelData
                    property bool localOverride: false
                    property bool awaitingCommit: false
                    property bool moving: false
                    property bool resizing: false
                    property real manualX: 0
                    property real manualY: 0
                    property real manualWidth: 0
                    property real manualHeight: 0

                    visible: root.showProjectOverlays
                        && Boolean(modelData.enabled)
                        && (modelData.kind === "blur" || modelData.kind === "logo")
                    x: localOverride ? manualX : root.overlayX(modelData)
                    y: localOverride ? manualY : root.overlayY(modelData)
                    width: localOverride ? manualWidth : root.overlayWidth(modelData)
                    height: localOverride ? manualHeight : root.overlayHeight(modelData)
                    color: "transparent"
                    border.width: 0
                    // The selected frame must stay reachable even when a
                    // damaged saved geometry sits underneath the transport.
                    // Non-editing previews keep the normal stacking order.
                    z: root.editableOverlays
                        && root.selectedOverlayId === overlayFrame.modelData.id
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
                        if (!root.editableOverlays
                                || root.selectedOverlayId !== overlayFrame.modelData.id)
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
                        visible: overlayFrame.modelData.kind === "blur"
                            && root.overlayActive(overlayFrame.modelData)
                            && width > 0 && height > 0
                        source: liveBlurSource
                        blurEnabled: true
                        blur: Math.max(0.01, Math.min(
                            1.0,
                            root.overlayNumber(
                                overlayFrame.modelData, "blur_radius", 22
                            ) / 100
                        ))
                        autoPaddingEnabled: false
                    }

                    Image {
                        anchors.fill: parent
                        visible: overlayFrame.modelData.kind === "logo"
                            && root.overlayActive(overlayFrame.modelData)
                            && Boolean(overlayFrame.modelData.asset_id)
                            && width > 0 && height > 0
                        source: root.appModel.currentProject.mediaSource(
                            overlayFrame.modelData.asset_id
                        )
                        opacity: root.overlayNumber(
                            overlayFrame.modelData, "opacity", 0.9
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
                        border.width: root.selectedOverlayId === overlayFrame.modelData.id
                            ? 2 : 1
                        border.color: overlayFrame.modelData.kind === "blur"
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
                            if (overlayId !== overlayFrame.modelData.id
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
                        text: overlayFrame.modelData.kind === "blur" ? "BLUR" : "LOGO"
                        visible: root.editableOverlays
                        color: overlayFrame.modelData.kind === "blur"
                            ? Theme.info : Theme.primary
                        font.family: Theme.fontFamily
                        font.pixelSize: 9
                        font.weight: Font.DemiBold
                        z: 21
                    }

                    MouseArea {
                        id: moveArea
                        anchors.fill: parent
                        enabled: root.editableOverlays
                        cursorShape: Qt.SizeAllCursor
                        preventStealing: true
                        z: 25
                        property point pressPoint
                        property real originX: 0
                        property real originY: 0

                        onPressed: function(mouse) {
                            root.selectedOverlayId = overlayFrame.modelData.id
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
                                overlayFrame.modelData,
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
                        color: overlayFrame.modelData.kind === "blur"
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
                                root.selectedOverlayId = overlayFrame.modelData.id
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
                                    overlayFrame.modelData,
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
                        color: overlayFrame.modelData.kind === "blur"
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
                                root.selectedOverlayId = overlayFrame.modelData.id
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
                                    overlayFrame.modelData,
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
                        color: overlayFrame.modelData.kind === "blur"
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
                                root.selectedOverlayId = overlayFrame.modelData.id
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
                                    overlayFrame.modelData,
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
                        color: overlayFrame.modelData.kind === "blur"
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
                                root.selectedOverlayId = overlayFrame.modelData.id
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
                                    overlayFrame.modelData,
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
                    onClicked: {
                        if (player.playbackState === MediaPlayer.PlayingState) root.pause()
                        else root.play()
                    }
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
