pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import DucaiStudio 1.0
import "../controls"

Item {
    id: root
    objectName: "timelineScreen"

    property var appModel
    property string inspectorTab: appModel.uiStringValue("timelineInspectorTab", "clip")
    property string fittedProjectId: ""
    property real viewportStartUs: 0
    property real viewportEndUs: 60_000_000
    readonly property real viewportDurationUs: Math.max(1, viewportEndUs - viewportStartUs)
    readonly property real projectDurationUs: Math.max(1, appModel.currentProject.durationUs)
    readonly property real minimumViewportUs: Math.min(1_000_000, projectDurationUs)
    readonly property real maximumZoom: Math.max(1, projectDurationUs / minimumViewportUs)
    readonly property real zoomLevel: maximumZoom <= 1 ? 0 : Math.max(
        0,
        Math.min(1, Math.log(projectDurationUs / viewportDurationUs) / Math.log(maximumZoom))
    )
    property real pendingZoomLevel: 0
    property real pendingVoiceSyncSpeed: 1.25
    property bool clipResizeActive: false
    property bool clipResizePending: false
    property string clipResizeClipId: ""
    property string clipResizeEdge: ""
    property real clipResizeOriginalStartUs: 0
    property real clipResizeOriginalEndUs: 0
    property real clipResizePreviewStartUs: 0
    property real clipResizePreviewEndUs: 0
    property real clipResizeRawEdgeUs: 0
    property real clipResizeTargetUs: -1
    property real clipResizeSourceDurationUs: 0
    property real clipResizeResultSpeed: 1
    property int clipResizeTrackIndex: -1
    property bool clipResizeGuideVisible: false
    property bool clipResizeSnapActive: false
    property bool clipResizeSpeedLimited: false
    property string clipResizeLimitText: ""
    activeFocusOnTab: true
    focus: true
    Accessible.name: "Timeline"

    // Let focused text fields consume editing shortcuts (Ctrl+A, Ctrl+C, ...)
    // before the timeline-level selection handler sees them.  Without this,
    // Ctrl+A in the speed/volume inspector selected every track and cleared the
    // clip inspector, making per-cue editing appear unavailable.
    Keys.priority: Keys.AfterItem
    Keys.onPressed: function(event) {
        if (event.key === Qt.Key_Escape && root.clipResizeActive) {
            root.cancelClipResize()
            event.accepted = true
            return
        }
        if (event.key === Qt.Key_A && (event.modifiers & Qt.ControlModifier)) {
            root.appModel.selectAllTracks()
            event.accepted = true
        }
    }

    function clipUsesResizePreview(clipId) {
        return clipResizeClipId === clipId
            && (clipResizeActive || clipResizePending)
    }

    function timingStateForRange(trackKind, startUs, endUs, slotStartUs, slotEndUs) {
        if (trackKind !== "voice" || slotStartUs < 0 || slotEndUs <= slotStartUs)
            return "unknown"
        const durationDelta = (endUs - startUs) - (slotEndUs - slotStartUs)
        if (durationDelta > 50_000) return "overflow"
        if (durationDelta < -50_000) return "underflow"
        if (Math.abs(startUs - slotStartUs) > 50_000
                || Math.abs(endUs - slotEndUs) > 50_000)
            return "position_mismatch"
        return "matched"
    }

    function timingStateText(state, durationDeltaUs, startDeltaUs, requiredSpeed) {
        if (state === "overflow")
            return "Dài hơn SRT " + Math.round(Math.abs(durationDeltaUs) / 1000)
                + " ms · cần khoảng " + Number(requiredSpeed).toFixed(2) + "×"
        if (state === "underflow")
            return "Ngắn hơn SRT " + Math.round(Math.abs(durationDeltaUs) / 1000) + " ms"
        if (state === "position_mismatch")
            return "Độ dài phù hợp nhưng lệch vị trí "
                + (startDeltaUs >= 0 ? "+" : "")
                + Math.round(startDeltaUs / 1000) + " ms"
        if (state === "matched") return "Đã khớp SRT trong sai số ±50 ms"
        return "Chưa có mốc SRT để đối chiếu"
    }

    function clipFillColor(state, trackKind, selected) {
        if (trackKind === "voice") {
            if (state === "overflow") return Theme.errorTint
            if (state === "underflow" || state === "position_mismatch")
                return Theme.warningTint
            if (state === "matched") return Theme.successTint
        }
        if (selected) return Theme.selectionFill
        if (trackKind === "original_audio" || trackKind === "voice") return Theme.audio
        return Theme.video
    }

    function clipBorderColor(state, trackKind, selected) {
        if (trackKind === "voice") {
            if (state === "overflow") return Theme.errorBorder
            if (state === "underflow" || state === "position_mismatch")
                return Theme.warningBorder
            if (state === "matched") return Theme.successBorder
        }
        return selected ? Theme.primary : Theme.borderStrong
    }

    function clipResizeSnapToleranceUs() {
        const eightPixelsUs = root.viewportDurationUs * 8 / Math.max(1, clipLayer.width)
        return Math.min(500_000, Math.max(80_000, eightPixelsUs))
    }

    function clipLayerTimeAt(pointerX) {
        const ratio = Math.max(0, Math.min(1, pointerX / Math.max(1, clipLayer.width)))
        return root.viewportStartUs + ratio * root.viewportDurationUs
    }

    function beginClipResize(clipId, edge, startUs, endUs, slotStartUs,
                             slotEndUs, sourceDurationUs, trackIndex, pointerX) {
        if (root.clipResizePending || root.appModel.timelineBusy) return
        root.forceActiveFocus()
        root.appModel.selectClip(clipId)
        root.selectInspectorTab("clip")
        root.clipResizeClipId = clipId
        root.clipResizeEdge = edge
        root.clipResizeOriginalStartUs = Number(startUs)
        root.clipResizeOriginalEndUs = Number(endUs)
        root.clipResizePreviewStartUs = Number(startUs)
        root.clipResizePreviewEndUs = Number(endUs)
        root.clipResizeRawEdgeUs = root.clipLayerTimeAt(pointerX)
        root.clipResizeTargetUs = edge === "left" ? Number(slotStartUs) : Number(slotEndUs)
        root.clipResizeSourceDurationUs = Math.max(1, Number(sourceDurationUs))
        root.clipResizeTrackIndex = Number(trackIndex)
        root.clipResizeResultSpeed = root.clipResizeSourceDurationUs
            / Math.max(1, Number(endUs) - Number(startUs))
        root.clipResizeGuideVisible = false
        root.clipResizeSnapActive = false
        root.clipResizeSpeedLimited = false
        root.clipResizeLimitText = ""
        root.clipResizeActive = true
    }

    function updateClipResize(pointerX) {
        if (!root.clipResizeActive) return
        const rawTime = root.clipLayerTimeAt(pointerX)
        // Keep the edge bounds integer-safe before Python converts the final
        // microseconds. A fractional source/3 followed by truncation could
        // otherwise submit a duration just below the 3.00x limit.
        const minimumDuration = Math.ceil(root.clipResizeSourceDurationUs / 3.0)
        const maximumDuration = Math.floor(root.clipResizeSourceDurationUs / 0.5)
        let minimumEdge
        let maximumEdge
        if (root.clipResizeEdge === "left") {
            minimumEdge = Math.max(0, root.clipResizeOriginalEndUs - maximumDuration)
            maximumEdge = root.clipResizeOriginalEndUs - minimumDuration
        } else {
            minimumEdge = root.clipResizeOriginalStartUs + minimumDuration
            maximumEdge = root.clipResizeOriginalStartUs + maximumDuration
        }
        const clamped = Math.max(minimumEdge, Math.min(maximumEdge, rawTime))
        root.clipResizeRawEdgeUs = rawTime
        root.clipResizeSpeedLimited = Math.abs(clamped - rawTime) > 0.5
        if (root.clipResizeSpeedLimited) {
            const limitedToMinimumSpeed = root.clipResizeEdge === "left"
                ? rawTime < minimumEdge : rawTime > maximumEdge
            root.clipResizeLimitText = limitedToMinimumSpeed
                ? "Đã chạm giới hạn tốc độ 0,50×"
                : "Đã chạm giới hạn tốc độ 3,00×"
        } else {
            root.clipResizeLimitText = ""
        }

        const target = root.clipResizeTargetUs
        const snapTolerance = root.clipResizeSnapToleranceUs()
        const guideTolerance = Math.min(750_000, snapTolerance * 1.75)
        const targetInRange = target >= minimumEdge && target <= maximumEdge
        const targetDistance = target >= 0 ? Math.abs(rawTime - target) : Number.POSITIVE_INFINITY
        root.clipResizeGuideVisible = target >= 0 && targetDistance <= guideTolerance
        root.clipResizeSnapActive = targetInRange && targetDistance <= snapTolerance
        const edgeTime = root.clipResizeSnapActive ? target : clamped
        if (root.clipResizeEdge === "left")
            root.clipResizePreviewStartUs = edgeTime
        else
            root.clipResizePreviewEndUs = edgeTime
        const durationUs = Math.max(
            1,
            root.clipResizePreviewEndUs - root.clipResizePreviewStartUs
        )
        root.clipResizeResultSpeed = root.clipResizeSourceDurationUs / durationUs
    }

    function finishClipResize() {
        if (!root.clipResizeActive) return
        const changed = Math.abs(
            root.clipResizePreviewStartUs - root.clipResizeOriginalStartUs
        ) >= 1 || Math.abs(
            root.clipResizePreviewEndUs - root.clipResizeOriginalEndUs
        ) >= 1
        root.clipResizeActive = false
        root.clipResizeGuideVisible = false
        root.clipResizeSnapActive = false
        if (!changed) {
            root.resetClipResize()
            return
        }
        root.clipResizePending = true
        root.appModel.resizeSelectedClip(
            root.clipResizePreviewStartUs,
            root.clipResizePreviewEndUs
        )
    }

    function cancelClipResize() {
        if (!root.clipResizeActive) return
        root.resetClipResize()
    }

    function resetClipResize() {
        root.clipResizeActive = false
        root.clipResizePending = false
        root.clipResizeClipId = ""
        root.clipResizeEdge = ""
        root.clipResizeGuideVisible = false
        root.clipResizeSnapActive = false
        root.clipResizeSpeedLimited = false
        root.clipResizeLimitText = ""
        root.clipResizeTrackIndex = -1
    }

    function selectInspectorTab(value) {
        inspectorTab = value
        appModel.setUiStringValue("timelineInspectorTab", value)
    }

    function applyViewport(startUs, durationUs) {
        const duration = Math.max(
            minimumViewportUs,
            Math.min(projectDurationUs, durationUs)
        )
        const maximumStart = Math.max(0, projectDurationUs - duration)
        viewportStartUs = Math.max(0, Math.min(maximumStart, startUs))
        viewportEndUs = viewportStartUs + duration
        const prefetchUs = Math.min(
            10_000_000, Math.max(250_000, duration * 0.25)
        )
        appModel.currentProject.clipModel.setViewport(
            viewportStartUs,
            viewportEndUs,
            prefetchUs
        )
        appModel.currentProject.voiceConformCueModel.setViewport(
            viewportStartUs,
            viewportEndUs,
            prefetchUs
        )
    }

    function showProjectStart() {
        applyViewport(0, Math.min(projectDurationUs, 60_000_000))
    }

    function fitProject() {
        applyViewport(0, projectDurationUs)
    }

    function zoomAt(factor, anchorRatio) {
        const ratio = Math.max(0, Math.min(1, anchorRatio))
        const anchorUs = viewportStartUs + viewportDurationUs * ratio
        const duration = Math.max(
            minimumViewportUs,
            Math.min(projectDurationUs, viewportDurationUs * factor)
        )
        applyViewport(anchorUs - duration * ratio, duration)
    }

    function zoomBy(factor) {
        const playhead = appModel.playheadUs
        const playheadVisible = playhead >= viewportStartUs && playhead <= viewportEndUs
        const anchorRatio = playheadVisible
            ? (playhead - viewportStartUs) / viewportDurationUs
            : 0.5
        zoomAt(factor, anchorRatio)
    }

    function applyZoomLevel(level) {
        const normalized = Math.max(0, Math.min(1, level))
        const duration = maximumZoom <= 1
            ? projectDurationUs
            : projectDurationUs / Math.pow(maximumZoom, normalized)
        const playhead = appModel.playheadUs
        const visible = playhead >= viewportStartUs && playhead <= viewportEndUs
        const ratio = visible
            ? (playhead - viewportStartUs) / viewportDurationUs
            : 0.5
        const anchorUs = viewportStartUs + viewportDurationUs * ratio
        applyViewport(anchorUs - duration * ratio, duration)
    }

    function scheduleZoom(level) {
        pendingZoomLevel = level
        zoomThrottle.restart()
    }

    function parseDecimalText(value) {
        return Number(String(value).trim().replace(",", "."))
    }

    function decimalTextInRange(value, minimum, maximum) {
        const parsed = parseDecimalText(value)
        return Number.isFinite(parsed) && parsed >= minimum && parsed <= maximum
    }

    function voiceEstimateValue(key, fallback) {
        const estimate = appModel.voiceConformEstimate || ({})
        if (estimate[key] === undefined || estimate[key] === null)
            return fallback
        const value = Number(estimate[key])
        return Number.isFinite(value) ? value : fallback
    }

    function refreshClipFields() {
        speedField.text = Number(appModel.selectedClipSpeedPercent).toFixed(2)
        volumeField.text = Number(appModel.selectedClipVolumePercent).toFixed(2)
    }

    Component.onCompleted: {
        fittedProjectId = appModel.currentProject.projectId
        fitProject()
        refreshClipFields()
    }

    Connections {
        target: root.appModel.currentProject
        function onChanged() {
            if (root.fittedProjectId !== root.appModel.currentProject.projectId) {
                root.fittedProjectId = root.appModel.currentProject.projectId
                root.fitProject()
                return
            }
            root.applyViewport(root.viewportStartUs, root.viewportDurationUs)
        }
    }

    Connections {
        target: root.appModel
        function onSelectedClipChanged() { root.refreshClipFields() }
        function onTimelineBusyChanged() {
            if (root.clipResizePending && !root.appModel.timelineBusy)
                root.resetClipResize()
        }
    }

    Timer {
        id: persistWidth
        interval: 250
        onTriggered: root.appModel.setUiRealValue(
            "timelineInspectorRatio", inspector.width / Math.max(1, root.width)
        )
    }

    Timer {
        id: persistPreviewRatio
        interval: 250
        onTriggered: {
            if (previewPanel.height > 0 && verticalTimelineSplit.height > 0)
                root.appModel.setUiRealValue(
                    "timelinePreviewRatio",
                    previewPanel.height / verticalTimelineSplit.height
                )
        }
    }

    Timer {
        id: zoomThrottle
        interval: 32
        onTriggered: root.applyZoomLevel(root.pendingZoomLevel)
    }

    Timer {
        id: voiceSyncSpeedDebounce
        interval: 90
        onTriggered: root.appModel.setVoiceConformMaxAudioSpeed(
            root.pendingVoiceSyncSpeed
        )
    }

    SplitView {
        anchors.fill: parent
        anchors.margins: Theme.space12
        orientation: Qt.Horizontal
        handle: Rectangle {
            implicitWidth: 8
            color: SplitHandle.hovered || SplitHandle.pressed
                ? Theme.surfaceHover : Theme.canvas
            Rectangle {
                anchors.centerIn: parent
                width: 1
                height: parent.height
                color: Theme.borderSubtle
            }
        }

        SplitView {
            id: verticalTimelineSplit
            SplitView.fillWidth: true
            SplitView.minimumWidth: 650
            orientation: Qt.Vertical
            handle: Rectangle {
                implicitHeight: 10
                color: SplitHandle.hovered || SplitHandle.pressed
                    ? Theme.surfaceHover : Theme.canvas
                Rectangle {
                    anchors.centerIn: parent
                    width: parent.width
                    height: 2
                    color: Theme.borderStrong
                }
            }

            Panel {
                id: previewPanel
                SplitView.fillWidth: true
                SplitView.preferredHeight: verticalTimelineSplit.height
                    * root.appModel.uiRealValue("timelinePreviewRatio", 0.55)
                SplitView.minimumHeight: 260
                SplitView.maximumHeight: Math.max(280, verticalTimelineSplit.height - 240)
                color: Theme.canvas
                onHeightChanged: persistPreviewRatio.restart()
                TimelinePreview {
                    anchors.fill: parent
                    appModel: root.appModel
                }
            }

            Item {
                SplitView.fillWidth: true
                SplitView.fillHeight: true
                SplitView.minimumHeight: 230

                ColumnLayout {
                    anchors.fill: parent
                    spacing: Theme.space8

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.space8
                DsButton {
                    text: "Tách"
                    compact: true
                    enabled: root.appModel.hasSelectedClip && !root.appModel.timelineBusy
                    disabledReason: "Cần chọn clip và đặt playhead bên trong clip."
                    onClicked: root.appModel.splitSelected(true)
                }
                DsButton {
                    text: "Khớp voice theo SRT"
                    compact: true
                    enabled: root.appModel.currentProject.hasVoiceTrack
                    disabledReason: "Cần tạo D1 trước khi khớp từng cue theo timestamp SRT."
                    onClicked: root.selectInspectorTab("voice-sync")
                }
                DsButton {
                    text: "↶"
                    compact: true
                    enabled: root.appModel.canUndoTimeline && !root.appModel.timelineBusy
                    disabledReason: "Chưa có thao tác để hoàn tác."
                    onClicked: root.appModel.undoTimeline()
                }
                DsButton {
                    text: "↷"
                    compact: true
                    enabled: root.appModel.canRedoTimeline && !root.appModel.timelineBusy
                    disabledReason: "Chưa có thao tác để làm lại."
                    onClicked: root.appModel.redoTimeline()
                }
                Item { Layout.fillWidth: true }
                DsButton {
                    text: "Đầu 60s"
                    compact: true
                    enabled: root.appModel.currentProject.hasProject
                    disabledReason: "Timeline chưa có nội dung."
                    onClicked: root.showProjectStart()
                }
                Text {
                    text: Math.round(root.projectDurationUs / root.viewportDurationUs * 100) + "%"
                    color: Theme.textSecondary
                    font.family: Theme.timecodeFamily
                    font.pixelSize: 10
                }
                Slider {
                    id: zoomSlider
                    Layout.preferredWidth: 180
                    from: 0
                    to: 1
                    value: root.zoomLevel
                    enabled: root.appModel.currentProject.hasProject
                    onMoved: root.scheduleZoom(value)
                    onPressedChanged: {
                        if (!pressed) root.applyZoomLevel(value)
                    }
                    Accessible.name: "Mức kéo dãn timeline"
                }
                DsButton {
                    text: "Vừa khung"
                    compact: true
                    enabled: root.appModel.currentProject.hasProject
                    disabledReason: "Timeline chưa có nội dung."
                    onClicked: root.fitProject()
                }
            }

            Panel {
                Layout.fillWidth: true
                Layout.fillHeight: true
                ColumnLayout {
                    anchors.fill: parent
                    spacing: 0
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 34
                        color: Theme.surfaceRaised
                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: Theme.space12
                            anchors.rightMargin: Theme.space12
                            Text {
                                text: "TRACK"
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: 10
                                font.weight: Font.DemiBold
                            }
                            Item { Layout.fillWidth: true }
                            Text {
                                text: root.appModel.currentProject.hasProject
                                    ? root.appModel.currentProject.durationText
                                    : "Chưa có timebase"
                                color: Theme.textSecondary
                                font.family: Theme.timecodeFamily
                                font.pixelSize: 11
                            }
                        }
                    }
                    EmptyState {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        visible: root.appModel.currentProject.trackModel.count === 0
                        title: "Timeline đang trống"
                        description: "Viewport sẽ chỉ tạo delegate cho clip đang nhìn thấy."
                        iconName: "panels-top-left"
                    }
                    Item {
                        id: timelineBody
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        visible: root.appModel.currentProject.trackModel.count > 0
                        clip: true

                        ListView {
                            id: trackList
                            objectName: "timelineTrackList"
                            anchors.fill: parent
                            model: root.appModel.currentProject.trackModel
                            property real contentYBeforeModelReset: 0
                            property bool modelResetInProgress: false
                            clip: true
                            spacing: Theme.space4
                            boundsBehavior: Flickable.StopAtBounds
                            onContentYChanged: {
                                if (!modelResetInProgress)
                                    contentYBeforeModelReset = contentY
                            }
                            Connections {
                                target: trackList.model
                                function onModelAboutToBeReset() {
                                    trackList.contentYBeforeModelReset = trackList.contentY
                                    trackList.modelResetInProgress = true
                                }
                                function onModelReset() {
                                    Qt.callLater(function() {
                                        const maximumContentY = Math.max(
                                            0,
                                            trackList.contentHeight - trackList.height
                                        )
                                        trackList.contentY = Math.max(
                                            0,
                                            Math.min(
                                                trackList.contentYBeforeModelReset,
                                                maximumContentY
                                            )
                                        )
                                        trackList.modelResetInProgress = false
                                    })
                                }
                            }
                            delegate: Rectangle {
                                id: trackRow
                                required property var model
                                width: ListView.view.width
                                height: 58
                                color: root.appModel.selectedTrackIds.indexOf(model.trackId) >= 0
                                    ? Theme.selectionFill : Theme.surfacePanel
                                border.width: root.appModel.selectedTrackIds.indexOf(model.trackId) >= 0 ? 1 : 0
                                border.color: Theme.primary
                                RowLayout {
                                    anchors.fill: parent
                                    anchors.leftMargin: Theme.space12
                                    anchors.rightMargin: Theme.space12
                                    spacing: Theme.space12
                                    Rectangle {
                                        Layout.preferredWidth: 4
                                        Layout.fillHeight: true
                                        color: trackRow.model.kind === "original_audio"
                                            ? Theme.audio : Theme.video
                                    }
                                    ColumnLayout {
                                        Layout.preferredWidth: 148
                                        spacing: 0
                                        Text {
                                            text: trackRow.model.displayLabel + "  ·  " + trackRow.model.name
                                            color: Theme.textPrimary
                                            font.family: Theme.fontFamily
                                            font.pixelSize: 12
                                            font.weight: Font.DemiBold
                                        }
                                        Text {
                                            text: trackRow.model.trackId
                                                + (trackRow.model.muted ? "  ·  Tắt tiếng" : "")
                                                + (trackRow.model.locked ? "  ·  Đã khóa" : "")
                                            color: Theme.textMuted
                                            font.family: Theme.timecodeFamily
                                            font.pixelSize: 10
                                        }
                                    }
                                    Rectangle {
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: 34
                                        radius: Theme.radiusSmall
                                        color: Theme.canvas
                                        border.width: 1
                                        border.color: Theme.borderSubtle
                                    }
                                }
                                MouseArea {
                                    objectName: "timelineTrackHeaderMouseArea"
                                    anchors.left: parent.left
                                    anchors.top: parent.top
                                    anchors.bottom: parent.bottom
                                    width: 188
                                    acceptedButtons: Qt.LeftButton | Qt.RightButton
                                    onClicked: function(mouse) {
                                        if (mouse.modifiers & Qt.ControlModifier)
                                            root.appModel.toggleTrackSelection(trackRow.model.trackId)
                                        else
                                            root.appModel.selectTrack(trackRow.model.trackId)
                                        root.selectInspectorTab("track")
                                        if (mouse.button === Qt.RightButton) {
                                            trackMenu.openAt(trackRow, mouse.x, mouse.y)
                                        }
                                    }
                                }
                                Popup {
                                    id: trackMenu

                                    function openAt(sourceItem, localX, localY) {
                                        const popupPoint = sourceItem.mapToItem(
                                            Overlay.overlay, localX, localY
                                        )
                                        x = Math.max(
                                            Theme.space8,
                                            Math.min(
                                                Overlay.overlay.width - width - Theme.space8,
                                                popupPoint.x
                                            )
                                        )
                                        y = Math.max(
                                            Theme.space8,
                                            Math.min(
                                                Overlay.overlay.height - height - Theme.space8,
                                                popupPoint.y
                                            )
                                        )
                                        open()
                                    }

                                    parent: Overlay.overlay
                                    width: 240
                                    height: trackMenuColumn.implicitHeight + padding * 2
                                    padding: Theme.space4
                                    popupType: Popup.Item
                                    modal: false
                                    focus: true
                                    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
                                    z: 1000
                                    background: Rectangle {
                                        color: Theme.surfaceRaised
                                        border.width: 1
                                        border.color: Theme.borderStrong
                                        radius: Theme.radiusSmall
                                    }

                                    contentItem: ColumnLayout {
                                        id: trackMenuColumn
                                        spacing: 0
                                        DsContextButton {
                                            Layout.fillWidth: true
                                            text: trackRow.model.muted
                                                ? "Bật tiếng track" : "Tắt tiếng track"
                                            onClicked: {
                                                trackMenu.close()
                                                root.appModel.setSelectedTrackMuted(
                                                    !trackRow.model.muted
                                                )
                                            }
                                        }
                                        DsContextButton {
                                            Layout.fillWidth: true
                                            text: trackRow.model.locked
                                                ? "Mở khóa track" : "Khóa track"
                                            onClicked: {
                                                trackMenu.close()
                                                root.appModel.setSelectedTrackLocked(
                                                    !trackRow.model.locked
                                                )
                                            }
                                        }
                                        DsContextButton {
                                            Layout.fillWidth: true
                                            visible: trackRow.model.kind === "voice"
                                            text: "Tự khớp các cue D1 bị lệch"
                                            onClicked: {
                                                trackMenu.close()
                                                root.appModel.autoFitVoiceTiming()
                                            }
                                        }
                                        MenuSeparator { Layout.fillWidth: true }
                                        DsContextButton {
                                            Layout.fillWidth: true
                                            danger: true
                                            text: "Xoá timeline này"
                                            onClicked: {
                                                trackMenu.close()
                                                deleteTrackDialog.open()
                                            }
                                        }
                                    }
                                }
                                Dialog {
                                    id: deleteTrackDialog
                                    title: "Xoá timeline?"
                                    modal: true
                                    standardButtons: Dialog.Cancel | Dialog.Ok
                                    anchors.centerIn: Overlay.overlay
                                    contentItem: Text {
                                        width: 320
                                        wrapMode: Text.Wrap
                                        text: "Bạn sắp gỡ " + trackRow.model.displayLabel +
                                            " (" + trackRow.model.clipCount + " clip) khỏi dự án. " +
                                            "File video, WAV, SRT và cache sẽ không bị xoá. Có thể hoàn tác."
                                        color: Theme.textPrimary
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 12
                                    }
                                    onAccepted: root.appModel.deleteSelectedTracks()
                                }
                            }
                        }

                        Item {
                            id: clipLayer
                            anchors.fill: parent
                            anchors.leftMargin: 188
                            anchors.rightMargin: Theme.space12

                            Repeater {
                                model: root.appModel.currentProject.clipModel
                                delegate: Rectangle {
                                    id: clipItem
                                    required property var model
                                    readonly property real laneWidth: clipLayer.width
                                    readonly property bool usesResizePreview: root.clipUsesResizePreview(
                                        model.clipId
                                    )
                                    readonly property real effectiveStart: usesResizePreview
                                        ? root.clipResizePreviewStartUs : model.startUs
                                    readonly property real effectiveEnd: usesResizePreview
                                        ? root.clipResizePreviewEndUs : model.endUs
                                    readonly property real clippedStart: Math.max(
                                        root.viewportStartUs, effectiveStart
                                    )
                                    readonly property real clippedEnd: Math.min(
                                        root.viewportEndUs, effectiveEnd
                                    )
                                    readonly property string effectiveTimingState:
                                        root.timingStateForRange(
                                            model.trackKind,
                                            effectiveStart,
                                            effectiveEnd,
                                            model.slotStartUs,
                                            model.slotEndUs
                                        )
                                    readonly property real effectiveDurationDeltaUs:
                                        model.slotEndUs > model.slotStartUs
                                            ? (effectiveEnd - effectiveStart)
                                                - (model.slotEndUs - model.slotStartUs)
                                            : 0
                                    readonly property real effectiveRequiredSpeed:
                                        model.slotEndUs > model.slotStartUs
                                            ? (model.durationUs * model.speedPercent / 100)
                                                / (model.slotEndUs - model.slotStartUs)
                                            : 1
                                    visible: clippedEnd > clippedStart
                                    x: (clippedStart - root.viewportStartUs)
                                        / root.viewportDurationUs * laneWidth
                                    y: model.trackIndex * 62 - trackList.contentY + 12
                                    width: Math.max(
                                        8,
                                        (clippedEnd - clippedStart)
                                            / root.viewportDurationUs * laneWidth
                                    )
                                    height: 34
                                    radius: Theme.radiusSmall
                                    // A dense D1 timeline can make a cue narrower
                                    // than the label's horizontal padding. Clip the
                                    // delegate itself so a narrow cue can never paint
                                    // into its neighbours; the full text is still
                                    // available from the hover tooltip/Inspector.
                                    clip: true
                                    color: model.isAggregate
                                        ? Theme.surfaceRaised
                                        : root.clipFillColor(
                                            effectiveTimingState,
                                            model.trackKind,
                                            root.appModel.selectedClipId === model.clipId
                                        )
                                    border.width: !model.isAggregate
                                        && effectiveTimingState === "overflow"
                                        ? 3
                                        : (!model.isAggregate
                                            && (effectiveTimingState === "underflow"
                                                || effectiveTimingState === "position_mismatch"
                                                || effectiveTimingState === "matched"
                                                || root.appModel.selectedClipId === model.clipId)
                                            ? 2 : 1)
                                    border.color: model.isAggregate
                                        ? Theme.borderStrong
                                        : root.clipBorderColor(
                                            effectiveTimingState,
                                            model.trackKind,
                                            root.appModel.selectedClipId === model.clipId
                                        )
                                    Accessible.name: model.isAggregate
                                        ? model.trackName + ", " + model.aggregateCount + " clip được gom khi thu nhỏ"
                                        : model.trackName + ", clip " + model.clipId
                                    Accessible.role: Accessible.ListItem

                                    Text {
                                        anchors.centerIn: parent
                                        visible: clipItem.model.isAggregate || parent.width >= 28
                                        width: Math.max(0, parent.width - Theme.space12)
                                        text: clipItem.model.isAggregate
                                            ? clipItem.model.aggregateCount + " clip"
                                            : ((clipItem.model.cueText || (clipItem.model.trackKind === "voice" ? "D1" : ""))
                                                + "  "
                                                + Number(clipItem.model.speedPercent / 100).toFixed(2)
                                                + "×  |  "
                                                + Math.round(clipItem.model.volume * 100) + "%")
                                        color: Theme.textPrimary
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 10
                                        horizontalAlignment: Text.AlignHCenter
                                        elide: Text.ElideRight
                                    }
                                    Rectangle {
                                        visible: clipItem.model.contentWarning && !clipItem.model.isAggregate
                                        anchors.right: parent.right
                                        anchors.top: parent.top
                                        width: 18
                                        height: 18
                                        radius: 9
                                        color: Theme.warning
                                        Text {
                                            anchors.centerIn: parent
                                            text: "!"
                                            color: Theme.canvas
                                            font.bold: true
                                            font.pixelSize: 11
                                        }
                                    }

                                    HoverHandler { id: clipHover }
                                    ToolTip.visible: clipHover.hovered && !clipItem.model.isAggregate
                                    ToolTip.text: clipItem.model.cueText
                                        ? ("Cue #" + clipItem.model.cueIndex + " · "
                                            + clipItem.model.cueText
                                            + " · " + root.timingStateText(
                                                clipItem.effectiveTimingState,
                                                clipItem.effectiveDurationDeltaUs,
                                                clipItem.effectiveStart - clipItem.model.slotStartUs,
                                                clipItem.effectiveRequiredSpeed
                                            ))
                                        : ""

                                    MouseArea {
                                        objectName: "timelineClipBody-" + clipItem.model.clipId
                                        anchors.fill: parent
                                        enabled: !clipItem.model.isAggregate
                                        acceptedButtons: Qt.LeftButton | Qt.RightButton
                                        onClicked: function(mouse) {
                                            root.appModel.selectClip(clipItem.model.clipId)
                                            root.selectInspectorTab("clip")
                                            if (mouse.button === Qt.LeftButton)
                                                root.appModel.setPlayheadMs(
                                                    clipItem.model.startUs / 1000
                                                )
                                            if (mouse.button === Qt.RightButton) {
                                                clipMenu.openAt(clipItem, mouse.x, mouse.y)
                                            }
                                        }
                                        onDoubleClicked: {
                                            if (clipItem.model.trackKind === "voice" && clipItem.model.cueId)
                                                root.appModel.openCueInEditor(
                                                    String(clipItem.model.cueId),
                                                    Number(clipItem.model.startUs),
                                                    Number(clipItem.model.cueIndex)
                                                )
                                        }
                                    }

                                    MouseArea {
                                        objectName: "timelineClipLeftHandle-" + clipItem.model.clipId
                                        visible: clipItem.model.trackKind === "voice"
                                        width: Math.min(14, parent.width / 3)
                                        anchors.left: parent.left
                                        anchors.top: parent.top
                                        anchors.bottom: parent.bottom
                                        cursorShape: Qt.SizeHorCursor
                                        preventStealing: true
                                        onPressed: function(mouse) {
                                            const point = mapToItem(clipLayer, mouse.x, mouse.y)
                                            root.beginClipResize(
                                                clipItem.model.clipId,
                                                "left",
                                                clipItem.model.startUs,
                                                clipItem.model.endUs,
                                                clipItem.model.slotStartUs,
                                                clipItem.model.slotEndUs,
                                                clipItem.model.durationUs
                                                    * clipItem.model.speedPercent / 100,
                                                clipItem.model.trackIndex,
                                                point.x
                                            )
                                        }
                                        onPositionChanged: function(mouse) {
                                            if (!pressed) return
                                            const point = mapToItem(clipLayer, mouse.x, mouse.y)
                                            root.updateClipResize(point.x)
                                        }
                                        onReleased: root.finishClipResize()
                                        onCanceled: root.cancelClipResize()
                                    }
                                    MouseArea {
                                        objectName: "timelineClipRightHandle-" + clipItem.model.clipId
                                        visible: clipItem.model.trackKind === "voice"
                                        width: Math.min(14, parent.width / 3)
                                        anchors.right: parent.right
                                        anchors.top: parent.top
                                        anchors.bottom: parent.bottom
                                        cursorShape: Qt.SizeHorCursor
                                        preventStealing: true
                                        onPressed: function(mouse) {
                                            const point = mapToItem(clipLayer, mouse.x, mouse.y)
                                            root.beginClipResize(
                                                clipItem.model.clipId,
                                                "right",
                                                clipItem.model.startUs,
                                                clipItem.model.endUs,
                                                clipItem.model.slotStartUs,
                                                clipItem.model.slotEndUs,
                                                clipItem.model.durationUs
                                                    * clipItem.model.speedPercent / 100,
                                                clipItem.model.trackIndex,
                                                point.x
                                            )
                                        }
                                        onPositionChanged: function(mouse) {
                                            if (!pressed) return
                                            const point = mapToItem(clipLayer, mouse.x, mouse.y)
                                            root.updateClipResize(point.x)
                                        }
                                        onReleased: root.finishClipResize()
                                        onCanceled: root.cancelClipResize()
                                    }

                                    Popup {
                                        id: clipMenu

                                        function openAt(sourceItem, localX, localY) {
                                            const popupPoint = sourceItem.mapToItem(
                                                Overlay.overlay, localX, localY
                                            )
                                            x = Math.max(
                                                Theme.space8,
                                                Math.min(
                                                    Overlay.overlay.width - width - Theme.space8,
                                                    popupPoint.x
                                                )
                                            )
                                            y = Math.max(
                                                Theme.space8,
                                                Math.min(
                                                    Overlay.overlay.height - height - Theme.space8,
                                                    popupPoint.y
                                                )
                                            )
                                            open()
                                        }

                                        parent: Overlay.overlay
                                        width: 272
                                        height: clipMenuColumn.implicitHeight + padding * 2
                                        padding: Theme.space4
                                        popupType: Popup.Item
                                        modal: false
                                        focus: true
                                        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
                                        z: 1000
                                        background: Rectangle {
                                            color: Theme.surfaceRaised
                                            border.width: 1
                                            border.color: Theme.borderStrong
                                            radius: Theme.radiusSmall
                                        }

                                        contentItem: ColumnLayout {
                                            id: clipMenuColumn
                                            spacing: 0
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                text: "Tách video và audio tại playhead"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.splitSelected(true)
                                                }
                                            }
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                text: "Cắt phần trước, cả linked clip"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.trimSelectedHead(true)
                                                }
                                            }
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                text: "Cắt phần sau, cả linked clip"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.trimSelectedTail(true)
                                                }
                                            }
                                            MenuSeparator { Layout.fillWidth: true }
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                text: "Cắt phần trước, chỉ clip này"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.trimSelectedHead(false)
                                                }
                                            }
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                text: "Cắt phần sau, chỉ clip này"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.trimSelectedTail(false)
                                                }
                                            }
                                            MenuSeparator { Layout.fillWidth: true }
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                text: "Tốc độ 85% cho clip liên kết"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.setSelectedSpeed(85, true)
                                                }
                                            }
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                text: "Tốc độ 100% cho clip liên kết"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.setSelectedSpeed(100, true)
                                                }
                                            }
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                text: "Tắt tiếng clip này"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.setSelectedVolume(0)
                                                }
                                            }
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                text: "Đặt lại âm lượng 100%"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.setSelectedVolume(100)
                                                }
                                            }
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                danger: true
                                                text: "Xóa clip liên kết"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.deleteSelected(true)
                                                }
                                            }
                                        }
                                    }
                                }
                            }

                            Repeater {
                                model: root.appModel.currentProject.voiceConformCueModel
                                delegate: Rectangle {
                                    id: voiceCueItem
                                    required property var model
                                    readonly property real timingDurationDeltaUs:
                                        (model.voiceEndUs - model.voiceStartUs)
                                            - (model.slotEndUs - model.slotStartUs)
                                    readonly property string timingState:
                                        timingDurationDeltaUs > 50_000
                                            ? "overflow"
                                            : (timingDurationDeltaUs < -50_000
                                                ? "underflow"
                                                : ((Math.abs(model.voiceStartUs - model.slotStartUs) > 50_000
                                                        || Math.abs(model.voiceEndUs - model.slotEndUs) > 50_000)
                                                    ? "position_mismatch" : "matched"))
                                    readonly property real clippedStart: Math.max(
                                        root.viewportStartUs, model.voiceStartUs
                                    )
                                    readonly property real clippedEnd: Math.min(
                                        root.viewportEndUs, model.voiceEndUs
                                    )
                                    visible: root.appModel.currentProject.voiceTrackIndex >= 0
                                        && !root.appModel.currentProject.voiceClipsAreCueScoped
                                        && clippedEnd > clippedStart
                                    x: (clippedStart - root.viewportStartUs)
                                        / root.viewportDurationUs * clipLayer.width
                                    y: root.appModel.currentProject.voiceTrackIndex * 62
                                        - trackList.contentY + 14
                                    width: Math.max(
                                        3,
                                        (clippedEnd - clippedStart)
                                            / root.viewportDurationUs * clipLayer.width
                                    )
                                    height: 30
                                    radius: Theme.radiusSmall
                                    color: root.clipFillColor(timingState, "voice", false)
                                    opacity: timingState === "unknown" ? 0.72 : 0.96
                                    border.width: timingState === "overflow" ? 3 : 2
                                    border.color: root.clipBorderColor(timingState, "voice", false)
                                    z: 8
                                    Accessible.name: model.isAggregate
                                        ? model.aggregateCount + " cue voice theo timestamp SRT"
                                        : "Cue voice " + model.cueIndex
                                            + ", tốc độ "
                                            + Number(model.audioSpeed).toFixed(2) + " lần"
                                    Accessible.role: Accessible.ListItem

                                    HoverHandler { id: voiceCueHover }
                                    ToolTip.visible: voiceCueHover.hovered
                                    ToolTip.text: model.isAggregate
                                        ? model.aggregateCount + " cue được gom khi thu nhỏ"
                                        : "Cue #" + model.cueIndex
                                            + " · SRT nguồn "
                                            + (model.sourceStartUs / 1_000_000).toFixed(3)
                                            + "–"
                                            + (model.sourceEndUs / 1_000_000).toFixed(3)
                                            + "s · WAV "
                                            + (model.audioDurationUs / 1_000_000).toFixed(3)
                                            + "s → "
                                            + (model.renderedAudioDurationUs / 1_000_000).toFixed(3)
                                            + "s · "
                                            + Number(model.audioSpeed).toFixed(2) + "× · "
                                            + root.timingStateText(
                                                voiceCueItem.timingState,
                                                voiceCueItem.timingDurationDeltaUs,
                                                model.voiceStartUs - model.slotStartUs,
                                                model.audioDurationUs
                                                    / Math.max(1, model.slotEndUs - model.slotStartUs)
                                            )

                                    Text {
                                        anchors.centerIn: parent
                                        width: parent.width - Theme.space4
                                        text: voiceCueItem.model.isAggregate
                                            ? voiceCueItem.model.aggregateCount + " cue"
                                            : "#" + voiceCueItem.model.cueIndex + "  "
                                                + Number(voiceCueItem.model.audioSpeed).toFixed(2)
                                                + "×"
                                        color: Theme.canvas
                                        font.family: Theme.timecodeFamily
                                        font.pixelSize: 9
                                        font.weight: Font.DemiBold
                                        horizontalAlignment: Text.AlignHCenter
                                        elide: Text.ElideRight
                                    }

                                    MouseArea {
                                        id: voiceCueMouse
                                        anchors.fill: parent
                                        enabled: !voiceCueItem.model.isAggregate
                                        acceptedButtons: Qt.LeftButton
                                        onClicked: {
                                            if (voiceCueItem.model.unresolved) {
                                                root.appModel.selectVoiceConformIssue(voiceCueItem.model.cueIndex)
                                            } else {
                                                root.appModel.selectCue(
                                                    voiceCueItem.model.cueId,
                                                    voiceCueItem.model.sourceStartUs
                                                )
                                            }
                                        }
                                        onDoubleClicked: {
                                            root.appModel.openCueInEditor(
                                                String(voiceCueItem.model.cueId),
                                                Number(voiceCueItem.model.sourceStartUs),
                                                Number(voiceCueItem.model.cueIndex)
                                            )
                                        }
                                    }
                                }
                            }

                            Rectangle {
                                id: resizeVerticalGuide
                                visible: root.clipResizeActive
                                    && root.clipResizeGuideVisible
                                    && root.clipResizeTargetUs >= root.viewportStartUs
                                    && root.clipResizeTargetUs <= root.viewportEndUs
                                x: (root.clipResizeTargetUs - root.viewportStartUs)
                                    / root.viewportDurationUs * clipLayer.width - width / 2
                                y: 0
                                width: 2
                                height: clipLayer.height
                                color: root.clipResizeSnapActive ? Theme.success : Theme.warning
                                opacity: visible ? 0.95 : 0
                                z: 31
                                Behavior on opacity { NumberAnimation { duration: 80 } }
                            }

                            Rectangle {
                                id: resizeDistanceGuide
                                readonly property real targetX:
                                    (root.clipResizeTargetUs - root.viewportStartUs)
                                        / root.viewportDurationUs * clipLayer.width
                                readonly property real edgeUs: root.clipResizeEdge === "left"
                                    ? root.clipResizePreviewStartUs : root.clipResizePreviewEndUs
                                readonly property real edgeX:
                                    (edgeUs - root.viewportStartUs)
                                        / root.viewportDurationUs * clipLayer.width
                                visible: root.clipResizeActive && root.clipResizeGuideVisible
                                x: Math.abs(edgeX - targetX) < 4
                                    ? targetX - 6 : Math.min(edgeX, targetX)
                                y: root.clipResizeTrackIndex * 62
                                    - trackList.contentY + 28
                                width: Math.abs(edgeX - targetX) < 4
                                    ? 12 : Math.abs(edgeX - targetX)
                                height: 3
                                radius: 2
                                color: root.clipResizeSnapActive ? Theme.success : Theme.warning
                                opacity: visible ? 1 : 0
                                z: 32
                                Behavior on opacity { NumberAnimation { duration: 80 } }
                            }

                            Rectangle {
                                id: resizeGuideLabel
                                readonly property real rawX:
                                    (root.clipResizeRawEdgeUs - root.viewportStartUs)
                                        / root.viewportDurationUs * clipLayer.width
                                visible: root.clipResizeActive
                                    && (root.clipResizeGuideVisible || root.clipResizeSpeedLimited)
                                x: Math.max(
                                    Theme.space4,
                                    Math.min(clipLayer.width - width - Theme.space4, rawX + 10)
                                )
                                y: Math.max(
                                    Theme.space4,
                                    Math.min(
                                        clipLayer.height - height - Theme.space4,
                                        root.clipResizeTrackIndex * 62
                                            - trackList.contentY - height + 10
                                    )
                                )
                                width: resizeGuideText.implicitWidth + Theme.space16
                                height: resizeGuideText.implicitHeight + Theme.space8
                                radius: Theme.radiusSmall
                                color: Theme.surfaceRaised
                                border.width: 1
                                border.color: root.clipResizeSpeedLimited
                                    ? Theme.errorBorder
                                    : (root.clipResizeSnapActive
                                        ? Theme.successBorder : Theme.warningBorder)
                                z: 33

                                Text {
                                    id: resizeGuideText
                                    anchors.centerIn: parent
                                    text: root.clipResizeSpeedLimited
                                        ? root.clipResizeLimitText
                                            + " · kết quả "
                                            + Number(root.clipResizeResultSpeed).toFixed(2) + "×"
                                        : ((root.clipResizeTargetUs / 1_000_000).toFixed(3)
                                            + " s · lệch "
                                            + ((root.clipResizeRawEdgeUs - root.clipResizeTargetUs) / 1000 >= 0 ? "+" : "")
                                            + ((root.clipResizeRawEdgeUs - root.clipResizeTargetUs) / 1000).toFixed(0)
                                            + " ms · "
                                            + Number(root.clipResizeResultSpeed).toFixed(2) + "×")
                                    color: root.clipResizeSpeedLimited
                                        ? Theme.error
                                        : (root.clipResizeSnapActive
                                            ? Theme.success : Theme.warning)
                                    font.family: Theme.timecodeFamily
                                    font.pixelSize: 10
                                    font.weight: Font.DemiBold
                                }
                            }

                            Rectangle {
                                visible: root.appModel.playheadUs >= root.viewportStartUs
                                    && root.appModel.playheadUs <= root.viewportEndUs
                                x: (root.appModel.playheadUs - root.viewportStartUs)
                                    / root.viewportDurationUs * clipLayer.width
                                y: 0
                                width: 2
                                height: clipLayer.height
                                color: Theme.primary
                                z: 20
                            }
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 32
                        color: Theme.surfaceRaised
                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: Theme.space12
                            anchors.rightMargin: Theme.space12
                            spacing: Theme.space8
                            Text {
                                text: "Đầu"
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: 10
                            }
                            Slider {
                                id: horizontalViewport
                                Layout.fillWidth: true
                                from: 0
                                to: Math.max(0, root.projectDurationUs - root.viewportDurationUs)
                                value: root.viewportStartUs
                                enabled: to > 0
                                onMoved: root.applyViewport(value, root.viewportDurationUs)
                                Accessible.name: "Cuộn timeline theo chiều ngang"
                                background: Rectangle {
                                    x: horizontalViewport.leftPadding
                                    y: horizontalViewport.topPadding
                                        + horizontalViewport.availableHeight / 2 - height / 2
                                    width: horizontalViewport.availableWidth
                                    height: 4
                                    radius: 2
                                    color: Theme.borderStrong
                                    Rectangle {
                                        width: horizontalViewport.visualPosition * parent.width
                                        height: parent.height
                                        radius: parent.radius
                                        color: Theme.primary
                                    }
                                }
                                handle: Rectangle {
                                    x: horizontalViewport.leftPadding
                                        + horizontalViewport.visualPosition
                                            * (horizontalViewport.availableWidth - width)
                                    y: horizontalViewport.topPadding
                                        + horizontalViewport.availableHeight / 2 - height / 2
                                    width: 14
                                    height: 14
                                    radius: 7
                                    color: horizontalViewport.pressed
                                        ? Theme.primaryPressed : Theme.primaryHover
                                }
                            }
                            Text {
                                text: "Cuối"
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: 10
                            }
                        }
                    }
                }
            }
                }
            }
        }

        Panel {
            id: inspector
            SplitView.preferredWidth: root.width * root.appModel.uiRealValue("timelineInspectorRatio", 0.27)
            SplitView.minimumWidth: 272
            SplitView.maximumWidth: 370
            onWidthChanged: persistWidth.restart()
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: Theme.space12
                spacing: Theme.space8
                RowLayout {
                    Layout.fillWidth: true
                    DsTabButton {
                        text: "Clip"
                        checked: root.inspectorTab === "clip"
                        onClicked: root.selectInspectorTab("clip")
                    }
                    DsTabButton {
                        text: "Track"
                        checked: root.inspectorTab === "track"
                        onClicked: root.selectInspectorTab("track")
                    }
                    DsTabButton {
                        text: "Phụ đề"
                        checked: root.inspectorTab === "subtitles"
                        onClicked: root.selectInspectorTab("subtitles")
                    }
                    DsTabButton {
                        text: "Khớp"
                        checked: root.inspectorTab === "voice-sync"
                        onClicked: root.selectInspectorTab("voice-sync")
                    }
                    DsTabButton {
                        text: "Dự án"
                        checked: root.inspectorTab === "project"
                        onClicked: root.selectInspectorTab("project")
                    }
                }
                StackLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    currentIndex: root.inspectorTab === "track"
                        ? 1 : (root.inspectorTab === "subtitles"
                            ? 2 : (root.inspectorTab === "voice-sync"
                                ? 3 : (root.inspectorTab === "project" ? 4 : 0)))
                    Item {
                        EmptyState {
                            anchors.fill: parent
                            visible: !root.appModel.hasSelectedClip
                            title: "Chưa chọn clip"
                            description: "Chuột trái để chọn, chuột phải để cắt riêng hoặc cắt linked clip."
                        }
                        ColumnLayout {
                            anchors.fill: parent
                            visible: root.appModel.hasSelectedClip
                            spacing: Theme.space12
                            SectionTitle { text: "Thuộc tính clip" }
                            Text {
                                Layout.fillWidth: true
                                text: root.appModel.selectedClipId
                                color: Theme.textMuted
                                font.family: Theme.timecodeFamily
                                font.pixelSize: 10
                                elide: Text.ElideMiddle
                            }
                            Text {
                                Layout.fillWidth: true
                                visible: root.appModel.selectedClipTrackKind === "voice"
                                text: root.appModel.selectedClipCueText
                                    ? root.appModel.selectedClipCueText
                                    : (root.appModel.selectedClipCueId
                                        ? "Cue D1 chưa có nội dung hiển thị"
                                        : "D1 cũ chưa có liên kết cue; không thể mở bằng double-click. "
                                            + "Hãy tạo lại D1 từ cache để khôi phục liên kết.")
                                color: root.appModel.selectedClipCueId
                                    ? Theme.textPrimary : Theme.warning
                                font.family: Theme.fontFamily
                                font.pixelSize: 12
                                wrapMode: Text.Wrap
                                maximumLineCount: 3
                                elide: Text.ElideRight
                            }
                            Text {
                                Layout.fillWidth: true
                                visible: root.appModel.selectedClipTrackKind === "voice"
                                text: root.appModel.selectedClipTimingText
                                color: root.appModel.selectedClipTimingState === "overflow"
                                    ? Theme.error
                                    : ((root.appModel.selectedClipTimingState === "underflow"
                                            || root.appModel.selectedClipTimingState === "position_mismatch")
                                        ? Theme.warning
                                        : (root.appModel.selectedClipTimingState === "matched"
                                            ? Theme.success : Theme.textSecondary))
                                font.family: Theme.timecodeFamily
                                font.pixelSize: 10
                                wrapMode: Text.Wrap
                            }
                            Text {
                                Layout.fillWidth: true
                                visible: root.appModel.selectedClipTrackKind === "voice"
                                    && root.appModel.selectedClipTimingState !== "matched"
                                    && root.appModel.selectedClipTimingState !== "unknown"
                                text: root.appModel.selectedClipTimingState === "overflow"
                                    ? "Cue dài hơn slot: kéo mép về mốc SRT hoặc dùng Tự khớp lệch."
                                    : (root.appModel.selectedClipTimingState === "underflow"
                                        ? "Cue ngắn hơn slot nên chỉ cảnh báo vàng; không bắt buộc sửa."
                                        : "Cue lệch vị trí so với SRT nên chỉ cảnh báo vàng.")
                                color: root.appModel.selectedClipTimingState === "overflow"
                                    ? Theme.error : Theme.warning
                                font.family: Theme.fontFamily
                                font.pixelSize: 10
                                wrapMode: Text.Wrap
                            }
                            Text {
                                text: "Tốc độ nội dung (%)"
                                color: Theme.textSecondary
                                font.family: Theme.fontFamily
                                font.pixelSize: 12
                            }
                            DsTextField {
                                id: speedField
                                readonly property real numericValue: root.parseDecimalText(text)
                                readonly property bool valueIsValid: root.decimalTextInRange(
                                    text,
                                    root.appModel.selectedClipTrackKind === "voice" ? 50 : 5,
                                    root.appModel.selectedClipTrackKind === "voice" ? 300 : 1000
                                )
                                Layout.fillWidth: true
                                placeholderText: "100.00"
                                inputMethodHints: Qt.ImhFormattedNumbersOnly
                                validator: RegularExpressionValidator {
                                    regularExpression: /^[0-9]+([\.,][0-9]{0,2})?$/
                                }
                                Accessible.name: "Tốc độ nội dung phần trăm"
                            }
                            CheckBox {
                                id: linkedCheck
                                text: root.appModel.selectedClipTrackKind === "voice"
                                    ? "D1 chỉ áp dụng cho audio cue này"
                                    : "Áp dụng cả video và audio liên kết"
                                checked: root.appModel.selectedClipTrackKind !== "voice"
                                enabled: root.appModel.selectedClipTrackKind !== "voice"
                                spacing: Theme.space8
                                indicator: Rectangle {
                                    implicitWidth: 18
                                    implicitHeight: 18
                                    x: linkedCheck.leftPadding
                                    y: parent.height / 2 - height / 2
                                    radius: Theme.radiusSmall
                                    color: linkedCheck.checked ? Theme.primary : Theme.surfaceRaised
                                    border.width: linkedCheck.activeFocus ? 2 : 1
                                    border.color: linkedCheck.activeFocus
                                        ? Theme.focusRing : Theme.borderStrong
                                    Text {
                                        anchors.centerIn: parent
                                        text: linkedCheck.checked ? "✓" : ""
                                        color: Theme.textPrimary
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 12
                                    }
                                }
                                contentItem: Text {
                                    text: linkedCheck.text
                                    color: Theme.textSecondary
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 11
                                    leftPadding: linkedCheck.indicator.width + linkedCheck.spacing
                                    verticalAlignment: Text.AlignVCenter
                                    wrapMode: Text.Wrap
                                }
                            }
                            DsButton {
                                Layout.fillWidth: true
                                text: "Áp dụng tốc độ"
                                primary: true
                                enabled: speedField.valueIsValid && !root.appModel.timelineBusy
                                disabledReason: root.appModel.selectedClipTrackKind === "voice"
                                    ? "Cue D1 chỉ cho phép 50% đến 300%."
                                    : "Nhập tốc độ từ 5% đến 1000%."
                                onClicked: root.appModel.setSelectedSpeed(
                                    speedField.numericValue, linkedCheck.checked
                                )
                            }
                            Text {
                                text: "Âm lượng clip (%)"
                                color: Theme.textSecondary
                                font.family: Theme.fontFamily
                                font.pixelSize: 12
                            }
                            DsTextField {
                                id: volumeField
                                readonly property real numericValue: root.parseDecimalText(text)
                                readonly property bool valueIsValid: root.decimalTextInRange(
                                    text, 0, 200
                                )
                                Layout.fillWidth: true
                                placeholderText: "100.00"
                                inputMethodHints: Qt.ImhFormattedNumbersOnly
                                validator: RegularExpressionValidator {
                                    regularExpression: /^[0-9]+([\.,][0-9]{0,2})?$/
                                }
                                Accessible.name: "Âm lượng clip phần trăm"
                            }
                            DsButton {
                                Layout.fillWidth: true
                                text: "Áp dụng âm lượng clip này"
                                enabled: volumeField.valueIsValid && !root.appModel.timelineBusy
                                disabledReason: "Nhập âm lượng từ 0% đến 200%."
                                onClicked: root.appModel.setSelectedVolume(volumeField.numericValue)
                            }
                            Item { Layout.fillHeight: true }
                        }
                    }
                    Item {
                        EmptyState {
                            anchors.fill: parent
                            visible: !root.appModel.hasSelectedTrack
                            title: "Chưa chọn track"
                            description: "Chuột trái hoặc chuột phải vào V1, A1, A2 hoặc S1."
                        }
                        ColumnLayout {
                            anchors.fill: parent
                            visible: root.appModel.hasSelectedTrack
                            spacing: Theme.space12
                            SectionTitle { text: "Thuộc tính track" }
                            Text {
                                Layout.fillWidth: true
                                text: root.appModel.selectedTrackId
                                color: Theme.textPrimary
                                font.family: Theme.timecodeFamily
                                font.pixelSize: 12
                            }
                            Text {
                                Layout.fillWidth: true
                                text: root.appModel.selectedTrackMuted
                                    ? "Track đang tắt tiếng" : "Track đang phát âm thanh"
                                color: Theme.textSecondary
                                font.family: Theme.fontFamily
                                font.pixelSize: 11
                            }
                            DsButton {
                                Layout.fillWidth: true
                                text: root.appModel.selectedTrackMuted
                                    ? "Bật tiếng track" : "Tắt tiếng track"
                                enabled: !root.appModel.timelineBusy
                                disabledReason: "Timeline đang cập nhật."
                                onClicked: root.appModel.setSelectedTrackMuted(
                                    !root.appModel.selectedTrackMuted
                                )
                            }
                            DsButton {
                                Layout.fillWidth: true
                                text: root.appModel.selectedTrackLocked
                                    ? "Mở khóa track" : "Khóa track"
                                enabled: !root.appModel.timelineBusy
                                disabledReason: "Timeline đang cập nhật."
                                onClicked: root.appModel.setSelectedTrackLocked(
                                    !root.appModel.selectedTrackLocked
                                )
                            }
                            Text {
                                Layout.fillWidth: true
                                text: root.appModel.selectedTrackLocked
                                    ? "Clip trên track này không thể cắt, xóa, đổi tốc độ hoặc âm lượng."
                                    : "Khóa track để tránh chỉnh nhầm."
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: 10
                                wrapMode: Text.Wrap
                            }
                            Item { Layout.fillHeight: true }
                        }
                    }
                    Item {
                        EmptyState {
                            anchors.fill: parent
                            visible: root.appModel.currentProject.subtitleCueModel.count === 0
                            title: "Chưa có phụ đề"
                            description: "Trích xuất hoặc nhập SRT để xem cue trên Timeline."
                        }
                        ColumnLayout {
                            anchors.fill: parent
                            visible: root.appModel.currentProject.subtitleCueModel.count > 0
                            spacing: Theme.space8
                            SectionTitle { text: "Cue phụ đề" }
                            Text {
                                Layout.fillWidth: true
                                text: "Bấm vào chữ nguồn hoặc bản Việt để chọn cue và đưa video tới đúng thời gian."
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: 10
                                wrapMode: Text.Wrap
                            }
                            ListView {
                                id: timelineCueList
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                model: root.appModel.currentProject.subtitleCueModel
                                spacing: Theme.space4
                                clip: true
                                boundsBehavior: Flickable.StopAtBounds
                                delegate: Rectangle {
                                    id: timelineCueRow
                                    required property var model
                                    width: ListView.view.width
                                    height: Math.max(88, cueContent.implicitHeight + Theme.space12)
                                    radius: Theme.radiusSmall
                                    color: timelineCueRow.model.cueId === root.appModel.selectedCueId
                                        ? Theme.selectionFill
                                        : (cueHover.hovered ? Theme.surfaceHover : Theme.surfaceRaised)
                                    border.width: timelineCueRow.model.cueId === root.appModel.selectedCueId
                                        ? 1 : 0
                                    border.color: Theme.primary
                                    Accessible.name: "Cue " + timelineCueRow.model.cueIndex
                                    Accessible.role: Accessible.ListItem

                                    HoverHandler { id: cueHover }
                                    TapHandler {
                                        onTapped: root.appModel.selectCue(
                                            timelineCueRow.model.cueId,
                                            timelineCueRow.model.startUs
                                        )
                                    }
                                    ColumnLayout {
                                        id: cueContent
                                        anchors.left: parent.left
                                        anchors.right: parent.right
                                        anchors.verticalCenter: parent.verticalCenter
                                        anchors.leftMargin: Theme.space8
                                        anchors.rightMargin: Theme.space8
                                        spacing: 2
                                        Text {
                                            Layout.fillWidth: true
                                            text: "#" + timelineCueRow.model.cueIndex
                                                + "  " + timelineCueRow.model.startText
                                            color: Theme.textMuted
                                            font.family: Theme.timecodeFamily
                                            font.pixelSize: 9
                                        }
                                        Text {
                                            Layout.fillWidth: true
                                            text: timelineCueRow.model.sourceText
                                            color: Theme.textSecondary
                                            font.family: Theme.cjkFontFamily
                                            font.pixelSize: 11
                                            wrapMode: Text.Wrap
                                            maximumLineCount: 2
                                            elide: Text.ElideRight
                                        }
                                        Text {
                                            Layout.fillWidth: true
                                            text: timelineCueRow.model.translatedText || "Chưa dịch"
                                            color: timelineCueRow.model.translatedText
                                                ? Theme.textPrimary : Theme.textMuted
                                            font.family: Theme.fontFamily
                                            font.pixelSize: 11
                                            font.italic: !timelineCueRow.model.translatedText
                                            wrapMode: Text.Wrap
                                            maximumLineCount: 2
                                            elide: Text.ElideRight
                                        }
                                    }
                                }
                            }
                        }
                    }
                    Item {
                        EmptyState {
                            anchors.fill: parent
                            visible: !root.appModel.currentProject.hasVoiceTrack
                            title: "Chưa có voice D1"
                            description: "Hãy tạo toàn bộ giọng trước khi đối chiếu WAV theo timestamp SRT nguồn."
                        }
                        ScrollView {
                            anchors.fill: parent
                            visible: root.appModel.currentProject.hasVoiceTrack
                            clip: true
                            contentWidth: availableWidth

                            ColumnLayout {
                                width: parent.width
                                spacing: Theme.space12

                                SectionTitle { text: "Khớp voice theo timestamp SRT" }
                                Text {
                                    visible: root.appModel.currentProject.voiceConformCueModel.totalCount > 0
                                    Layout.fillWidth: true
                                    text: "Các cue voice được chia theo timestamp SRT"
                                    color: Theme.textSecondary
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 10
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: "Mỗi cue dùng nguyên start_us/end_us của SRT nguồn làm slot. Duration WAV Việt được đo thực tế; chỉ cue dài hơn slot mới tăng tốc, cue vừa hoặc ngắn giữ 1,00×."
                                    color: Theme.textSecondary
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 10
                                    wrapMode: Text.Wrap
                                }

                                Rectangle {
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: timestampContract.implicitHeight
                                        + Theme.space16
                                    radius: Theme.radiusSmall
                                    color: Theme.surfaceBase
                                    border.width: 1
                                    border.color: Theme.borderSubtle
                                    ColumnLayout {
                                        id: timestampContract
                                        anchors.left: parent.left
                                        anchors.right: parent.right
                                        anchors.verticalCenter: parent.verticalCenter
                                        anchors.margins: Theme.space8
                                        spacing: Theme.space4
                                        Text {
                                            Layout.fillWidth: true
                                            text: "Chuẩn thời gian: microsecond → sample WAV"
                                            color: Theme.textPrimary
                                            font.family: Theme.fontFamily
                                            font.pixelSize: 10
                                            font.weight: Font.DemiBold
                                            wrapMode: Text.Wrap
                                        }
                                        Text {
                                            Layout.fillWidth: true
                                            text: "Không dò bằng số ký tự và không time-stretch nối tiếp. Mọi biến thể được tạo lại trực tiếp từ WAV cache của cue."
                                            color: Theme.textMuted
                                            font.family: Theme.fontFamily
                                            font.pixelSize: 9
                                            wrapMode: Text.Wrap
                                        }
                                    }
                                }

                                DsButton {
                                    Layout.fillWidth: true
                                    text: root.appModel.voiceConformMode === "speed_audio"
                                        ? "Đã chọn: chỉ tăng audio cue dài"
                                        : "Chọn chế độ chỉ tăng audio"
                                    primary: root.appModel.voiceConformMode === "speed_audio"
                                    enabled: !root.appModel.voiceConformBusy
                                        && root.appModel.voiceConformMode !== "speed_audio"
                                    disabledReason: root.appModel.voiceConformMode === "speed_audio"
                                        ? "Chế độ đúng đã được chọn."
                                        : "Đang xử lý một bản đồng bộ khác."
                                    feedbackText: "Đang tính lại từng cue theo timestamp SRT"
                                    onClicked: root.appModel.setVoiceConformMode("speed_audio")
                                }

                                Text {
                                    text: "Tốc độ tối đa cho riêng cue dài: "
                                        + Number(root.appModel.voiceConformMaxAudioSpeed).toFixed(2)
                                        + "×"
                                    color: Theme.textPrimary
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 11
                                    font.weight: Font.DemiBold
                                }
                                Slider {
                                    id: timelineVoiceSyncSpeed
                                    Layout.fillWidth: true
                                    from: 1.0
                                    to: 2.0
                                    stepSize: 0.05
                                    snapMode: Slider.SnapAlways
                                    value: root.appModel.voiceConformMaxAudioSpeed
                                    enabled: root.appModel.voiceConformMode === "speed_audio"
                                        && !root.appModel.voiceConformBusy
                                    onMoved: {
                                        root.pendingVoiceSyncSpeed = value
                                        voiceSyncSpeedDebounce.restart()
                                    }
                                    onPressedChanged: {
                                        if (!pressed) {
                                            voiceSyncSpeedDebounce.stop()
                                            root.appModel.setVoiceConformMaxAudioSpeed(value)
                                        }
                                    }
                                    Accessible.name: "Giới hạn tăng tốc audio cue dài"
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: "Đây là hệ số hậu xử lý của WAV D1 hiện tại. Nếu cue cần nhanh hơn giới hạn, tool báo lỗi thay vì cắt hoặc chồng lời."
                                    color: Theme.textMuted
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 9
                                    wrapMode: Text.Wrap
                                }

                                Rectangle {
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: syncEvidence.implicitHeight
                                        + Theme.space16
                                    radius: Theme.radiusSmall
                                    color: Theme.surfaceRaised
                                    border.width: 1
                                    border.color: root.voiceEstimateValue(
                                        "unresolved_count", 0
                                    ) > 0 ? Theme.warningBorder : Theme.borderSubtle
                                    ColumnLayout {
                                        id: syncEvidence
                                        anchors.left: parent.left
                                        anchors.right: parent.right
                                        anchors.verticalCenter: parent.verticalCenter
                                        anchors.margins: Theme.space8
                                        spacing: Theme.space4
                                        Text {
                                            Layout.fillWidth: true
                                            text: "Cue cần tăng: "
                                                + root.voiceEstimateValue("adjusted_cue_count", 0)
                                                + " · cue vượt giới hạn: "
                                                + root.voiceEstimateValue("unresolved_count", 0)
                                            color: root.voiceEstimateValue(
                                                "unresolved_count", 0
                                            ) > 0 ? Theme.warning : Theme.success
                                            font.family: Theme.fontFamily
                                            font.pixelSize: 10
                                            font.weight: Font.DemiBold
                                            wrapMode: Text.Wrap
                                        }
                                        Text {
                                            Layout.fillWidth: true
                                            text: "Tốc độ áp dụng: "
                                                + root.voiceEstimateValue(
                                                    "min_audio_speed", 1
                                                ).toFixed(2)
                                                + "×–"
                                                + root.voiceEstimateValue(
                                                    "max_audio_speed", 1
                                                ).toFixed(2)
                                                + "× · duration đầu ra "
                                                + (root.voiceEstimateValue(
                                                    "output_duration_us",
                                                    root.appModel.currentProject.durationUs
                                                ) / 1_000_000).toFixed(3)
                                                + "s"
                                            color: Theme.textSecondary
                                            font.family: Theme.timecodeFamily
                                            font.pixelSize: 9
                                            wrapMode: Text.Wrap
                                        }
                                    }
                                }

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: Theme.space8
                                    DsButton {
                                        Layout.fillWidth: true
                                        text: "Tính lại"
                                        enabled: !root.appModel.voiceConformBusy
                                            && root.appModel.voiceConformMode === "speed_audio"
                                        disabledReason: "Đang xử lý hoặc chưa chọn chế độ tăng audio."
                                        feedbackText: "Đang đo lại WAV từng cue"
                                        onClicked: root.appModel.refreshVoiceConformEstimate()
                                    }
                                    DsButton {
                                        Layout.fillWidth: true
                                        text: root.appModel.voiceConformApplied
                                            ? "Đã áp dụng" : "Áp dụng"
                                        primary: true
                                        operationState: root.appModel.voiceConformBusy
                                            ? (root.appModel.voiceConformJobState
                                                || "start_requested")
                                            : (root.appModel.voiceConformApplied
                                                ? "succeeded" : "idle")
                                        runningText: "Đang khớp cue…"
                                        startingText: "Đang chuẩn bị…"
                                        succeededText: "Đã khớp timestamp"
                                        enabled: root.appModel.voiceConformMode === "speed_audio"
                                            && root.appModel.voiceConformCanApply
                                            && !root.appModel.voiceConformBusy
                                            && !root.appModel.voiceConformApplied
                                        disabledReason: "Cần bản ước lượng PASS và không còn cue vượt giới hạn."
                                        feedbackText: "Đang tạo D1 theo timestamp SRT"
                                        onClicked: root.appModel.applyVoiceConform()
                                    }
                                }
                                DsButton {
                                    Layout.fillWidth: true
                                    text: "Hoàn tác về D1 trước khi khớp"
                                    warning: true
                                    enabled: root.appModel.voiceConformApplied
                                        && !root.appModel.voiceConformBusy
                                    disabledReason: "Chưa có bản đồng bộ đã áp dụng."
                                    feedbackText: "Đang khôi phục D1 nền"
                                    onClicked: root.appModel.resetVoiceConform()
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: root.appModel.voiceConformApplied
                                        ? "Các khối D1 dùng màu timing: đỏ là dài hơn slot trên 50 ms, vàng là ngắn hoặc lệch vị trí, xanh là đã khớp. Tốc độ nằm trong từng khối; bấm để seek đúng cue."
                                        : "Sau khi áp dụng, Timeline sẽ vẽ từng cue voice trên track D1 để kiểm tra trực tiếp."
                                    color: Theme.textMuted
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 9
                                    wrapMode: Text.Wrap
                                }
                                Item { Layout.preferredHeight: Theme.space8 }
                            }
                        }
                    }
                    EmptyState { title: "Chưa mở dự án"; description: "Timebase và proxy sẽ lấy từ project hiện tại." }
                }
            }
        }
    }

    Rectangle {
        anchors.fill: parent
        z: 100
        visible: root.appModel.multiDetailBusy
        color: "#CC081018"
        opacity: visible ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: 120 } }

        Column {
            anchors.centerIn: parent
            spacing: Theme.space8

            BusyIndicator {
                anchors.horizontalCenter: parent.horizontalCenter
                running: parent.parent.visible
                width: 34
                height: 34
            }
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "Đang tải video vào Timeline…"
                color: Theme.textPrimary
                font.family: Theme.fontFamily
                font.pixelSize: 14
                font.weight: Font.DemiBold
            }
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "Dữ liệu lớn đang được giải nén ở nền; giao diện vẫn phản hồi."
                color: Theme.textSecondary
                font.family: Theme.fontFamily
                font.pixelSize: 10
            }
        }
    }
}
