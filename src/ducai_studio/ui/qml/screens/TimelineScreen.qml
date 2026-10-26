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
    property bool followPlayhead: appModel.uiIntValue("timelineFollowPlayhead", 1) === 1
    property bool spacePanHeld: false
    property bool timelinePanActive: false
    property real timelinePanOriginX: 0
    property real timelinePanOriginStartUs: 0
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
    property int openContextMenus: 0
    property bool sceneTimingActive: false
    property bool sceneTimingPending: false
    property int sceneTimingGeneration: 0
    property var sceneTimingBases: ({})
    property bool sceneTimingSnapStartEligible: false
    property bool sceneTimingSnapEndEligible: false
    property string sceneTimingClipId: ""
    property string sceneTimingCueId: ""
    property string sceneTimingMode: ""
    property real sceneTimingOriginalStartUs: 0
    property real sceneTimingOriginalEndUs: 0
    property real sceneTimingPreviewStartUs: 0
    property real sceneTimingPreviewEndUs: 0
    property real sceneTimingPointerOriginUs: 0
    property real sceneTimingPointerOriginY: 0
    property int sceneTimingTrackIndex: -1
    property string sceneTimingTrackId: ""
    property real sceneTimingPreviewDeltaUs: 0
    property real sceneTimingMinimumDeltaUs: 0
    property real sceneTimingMaximumDeltaUs: 0
    property int sceneTimingMinimumLaneDelta: 0
    property int sceneTimingLaneDelta: 0
    property bool sceneTimingFixedLane: false
    property bool sceneTimingSnapActive: false
    property string sceneTimingSnapEdge: ""
    property real sceneTimingSnapTargetUs: -1
    property string regionEditorMode: ""
    property bool marqueeActive: false
    property point marqueeStart: Qt.point(0, 0)
    property point marqueeEnd: Qt.point(0, 0)
    property bool clipMoveActive: false
    property bool clipMovePending: false
    property string clipMoveClipId: ""
    property string clipMoveLinkGroupId: ""
    property real clipMoveOriginalStartUs: 0
    property real clipMoveOriginalEndUs: 0
    property real clipMovePointerOriginUs: 0
    property real clipMovePreviewDeltaUs: 0
    property int clipMoveTrackIndex: -1
    property bool clipMoveSnapActive: false
    property real fittedProjectDurationUs: 0
    activeFocusOnTab: true
    focus: true
    Accessible.name: "Timeline"

    ManualSubtitleCueDialog {
        id: manualCueDialog
        appModel: root.appModel
        onPlacementRequested: {
            timelinePreview.pause()
            root.regionEditorMode = "manual_text"
        }
        onCueSubmitted: root.regionEditorMode = ""
    }

    Timer {
        id: trackViewportTimer
        interval: 40
        repeat: false
        onTriggered: root.applyTrackViewport()
    }

    Timer {
        id: timelineViewportTimer
        // Geometry follows the pointer immediately in QML.  Coalescing the
        // Python model synchronization to roughly one frame prevents a burst
        // of wheel/drag events from rebuilding hundreds of clip delegates.
        interval: 24
        repeat: false
        onTriggered: root.syncTimelineViewportModels()
    }

    // Let focused text fields consume editing shortcuts (Ctrl+A, Ctrl+C, ...)
    // before the timeline-level selection handler sees them.  Without this,
    // Ctrl+A in the speed/volume inspector selected every track and cleared the
    // clip inspector, making per-cue editing appear unavailable.
    Keys.priority: Keys.AfterItem
    Keys.onPressed: function(event) {
        if (timelinePreview.textEditorHasFocus()) return
        if ((event.modifiers & Qt.ControlModifier) && !event.isAutoRepeat
                && (root.appModel.selectedClipTrackKind === "video" || event.key === Qt.Key_V)) {
            if (event.key === Qt.Key_C) root.appModel.copySelectedVideo()
            else if (event.key === Qt.Key_V) {
                timelinePreview.pause()
                root.appModel.pasteVideoAtPlayhead()
            } else if (event.key === Qt.Key_D) {
                timelinePreview.pause()
                root.appModel.duplicateSelectedVideo()
            } else return
            event.accepted = true
            return
        }
        if ((event.modifiers & Qt.ControlModifier)
                && (event.key === Qt.Key_Z || event.key === Qt.Key_Y)) {
            if (event.key === Qt.Key_Z) root.appModel.undoTimeline()
            else root.appModel.redoTimeline()
            event.accepted = true
            return
        }
        if (event.key === Qt.Key_Delete && event.modifiers === Qt.NoModifier) {
            timelinePreview.pause()
            root.appModel.deleteTimelineSelection()
            event.accepted = true
            return
        }
        if (event.key === Qt.Key_X && event.modifiers === Qt.NoModifier
                && !event.isAutoRepeat) {
            timelinePreview.pause()
            root.appModel.splitVideoAtPlayhead()
            event.accepted = true
            return
        }
        if (event.key === Qt.Key_M && !event.isAutoRepeat) {
            if (event.modifiers & Qt.ShiftModifier)
                root.appModel.removeTimelineMarkerAtPlayhead()
            else
                root.appModel.addTimelineMarker()
            event.accepted = true
            return
        }
        if (event.key === Qt.Key_Space && !event.isAutoRepeat) {
            root.spacePanHeld = true
            return
        }
        if (event.key === Qt.Key_Escape
                && (root.clipResizeActive || root.sceneTimingActive || root.clipMoveActive)) {
            if (root.clipResizeActive) root.cancelClipResize()
            if (root.sceneTimingActive) root.cancelSceneTiming()
            if (root.clipMoveActive) root.cancelClipMove()
            event.accepted = true
            return
        }
        if (event.key === Qt.Key_A && (event.modifiers & Qt.ControlModifier)) {
            if (root.appModel.selectedCueCount > 0
                    || root.appModel.selectedClipTrackKind === "subtitle")
                root.appModel.selectAllSubtitleCues()
            else
                root.appModel.selectAllTracks()
            event.accepted = true
            return
        }
    }
    Keys.onReleased: function(event) {
        if (event.key === Qt.Key_Space && !event.isAutoRepeat)
            root.spacePanHeld = false
    }

    function clipUsesResizePreview(clipId) {
        return clipResizeClipId === clipId
            && (clipResizeActive || clipResizePending)
    }

    readonly property var cueSelectionLookup: {
        const ids = root.appModel.selectedCueIds
        const lookup = Object.create(null)
        for (let i = 0; i < ids.length; ++i) lookup[ids[i]] = true
        return lookup
    }

    function cueSelected(cueId) {
        return root.cueSelectionLookup[String(cueId || "")] === true
    }

    function clipUsesSceneTimingPreview(clipId, cueId) {
        if (!(sceneTimingActive || sceneTimingPending)) return false
        return Boolean(sceneTimingBases[String(cueId || "")])
    }

    function sceneTimingBase(cueId, edge, fallbackUs) {
        const timing = sceneTimingBases[String(cueId || "")]
        return timing ? Number(timing[edge === "start" ? 0 : 1]) : fallbackUs
    }

    function sceneTimingSnapThresholdUs() {
        return Math.min(500_000, 8 * root.viewportDurationUs / Math.max(1, clipLayer.width))
    }

    function clipUsesMovePreview(clipId, linkGroupId) {
        if (!(clipMoveActive || clipMovePending)) return false
        if (clipMoveLinkGroupId)
            return String(linkGroupId || "") === clipMoveLinkGroupId
        return clipMoveClipId === clipId
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
        if (trackKind === "original_audio" || trackKind === "voice"
                || trackKind === "background_music") return Qt.darker(Theme.audio, 3.0)
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

    function seekTimelineAt(pointerX) {
        const targetUs = root.clipLayerTimeAt(pointerX)
        root.appModel.setPlayheadMs(targetUs / 1000)
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

    function beginSceneTiming(clipId, cueId, mode, startUs, endUs, trackIndex,
                              trackId, pointerX, pointerY) {
        if (!cueId || root.sceneTimingPending || root.appModel.subtitleBusy
                || root.appModel.timelineBusy) return
        root.forceActiveFocus()
        root.appModel.selectClip(clipId)
        if (!root.cueSelected(cueId))
            root.appModel.selectTimelineCue(String(cueId), false)
        root.selectInspectorTab("text")
        root.sceneTimingClipId = String(clipId)
        root.sceneTimingCueId = String(cueId)
        root.sceneTimingMode = String(mode)
        root.sceneTimingOriginalStartUs = Number(startUs)
        root.sceneTimingOriginalEndUs = Number(endUs)
        root.sceneTimingPreviewStartUs = Number(startUs)
        root.sceneTimingPreviewEndUs = Number(endUs)
        root.sceneTimingPointerOriginUs = root.clipLayerTimeAt(pointerX)
        root.sceneTimingPointerOriginY = Number(pointerY)
        root.sceneTimingTrackIndex = Number(trackIndex)
        root.sceneTimingTrackId = String(trackId || "")
        root.sceneTimingPreviewDeltaUs = 0
        const timingBounds = root.appModel.selectedSceneTimingDeltaBounds(mode)
        root.sceneTimingBases = timingBounds.timings || ({})
        root.sceneTimingMinimumDeltaUs = Number(timingBounds.minimum || 0)
        root.sceneTimingMaximumDeltaUs = Number(timingBounds.maximum || 0)
        root.sceneTimingMinimumLaneDelta = Number(timingBounds.minimumLaneDelta || 0)
        root.sceneTimingFixedLane = Boolean(timingBounds.fixedLane)
        root.sceneTimingLaneDelta = 0
        root.sceneTimingSnapActive = false
        root.sceneTimingSnapEdge = ""
        root.sceneTimingSnapTargetUs = -1
        const snapThreshold = root.sceneTimingSnapThresholdUs()
        // An edge already near the playhead must be free to move away; only
        // an approaching edge is attracted during this gesture.
        root.sceneTimingSnapStartEligible = Math.abs(
            Number(startUs) - root.appModel.playheadUs) > snapThreshold
        root.sceneTimingSnapEndEligible = Math.abs(
            Number(endUs) - root.appModel.playheadUs) > snapThreshold
        root.sceneTimingActive = true
    }

    function updateSceneTiming(pointerX, pointerY) {
        if (!root.sceneTimingActive) return
        const pointerUs = root.clipLayerTimeAt(pointerX)
        const snapThresholdUs = root.sceneTimingSnapThresholdUs()
        const playheadUs = Number(root.appModel.playheadUs)
        root.sceneTimingSnapActive = false
        root.sceneTimingSnapEdge = ""
        root.sceneTimingSnapTargetUs = -1
        if (root.sceneTimingMode === "move") {
            const rawDelta = pointerUs - root.sceneTimingPointerOriginUs
            const minimumDelta = root.sceneTimingMinimumDeltaUs
            const maximumDelta = root.sceneTimingMaximumDeltaUs
            let delta = Math.max(minimumDelta, Math.min(maximumDelta, rawDelta))
            const rawStartUs = root.sceneTimingOriginalStartUs + delta
            const rawEndUs = root.sceneTimingOriginalEndUs + delta
            const startDistance = root.sceneTimingSnapStartEligible
                ? Math.abs(rawStartUs - playheadUs) : Infinity
            const endDistance = root.sceneTimingSnapEndEligible
                ? Math.abs(rawEndUs - playheadUs) : Infinity
            if (Math.min(startDistance, endDistance) <= snapThresholdUs) {
                const snapStart = startDistance <= endDistance
                const desiredDelta = playheadUs - (snapStart
                    ? root.sceneTimingOriginalStartUs
                    : root.sceneTimingOriginalEndUs)
                delta = Math.max(
                    minimumDelta, Math.min(maximumDelta, desiredDelta)
                )
                root.sceneTimingSnapActive = true
                root.sceneTimingSnapEdge = snapStart ? "start" : "end"
                root.sceneTimingSnapTargetUs = playheadUs
            }
            root.sceneTimingPreviewDeltaUs = delta
            root.sceneTimingPreviewStartUs = root.sceneTimingOriginalStartUs + delta
            root.sceneTimingPreviewEndUs = root.sceneTimingOriginalEndUs + delta
            const rawLaneDelta = Math.round(
                (Number(pointerY) - root.sceneTimingPointerOriginY) / 62
            )
            root.sceneTimingLaneDelta = root.sceneTimingFixedLane ? 0 : Math.max(
                root.sceneTimingMinimumLaneDelta, rawLaneDelta
            )
        } else if (root.sceneTimingMode === "left") {
            let delta = Math.max(
                root.sceneTimingMinimumDeltaUs,
                Math.min(
                    root.sceneTimingMaximumDeltaUs,
                    pointerUs - root.sceneTimingPointerOriginUs
                )
            )
            let startUs = root.sceneTimingOriginalStartUs + delta
            if (root.sceneTimingSnapStartEligible
                    && Math.abs(startUs - playheadUs) <= snapThresholdUs) {
                delta = Math.max(
                    root.sceneTimingMinimumDeltaUs,
                    Math.min(
                        root.sceneTimingMaximumDeltaUs,
                        playheadUs - root.sceneTimingOriginalStartUs
                    )
                )
                startUs = root.sceneTimingOriginalStartUs + delta
                root.sceneTimingSnapActive = true
                root.sceneTimingSnapEdge = "start"
                root.sceneTimingSnapTargetUs = playheadUs
            }
            root.sceneTimingPreviewStartUs = startUs
            root.sceneTimingPreviewEndUs = root.sceneTimingOriginalEndUs
            root.sceneTimingPreviewDeltaUs = delta
        } else {
            root.sceneTimingPreviewStartUs = root.sceneTimingOriginalStartUs
            let delta = Math.max(
                root.sceneTimingMinimumDeltaUs,
                Math.min(
                    root.sceneTimingMaximumDeltaUs,
                    pointerUs - root.sceneTimingPointerOriginUs
                )
            )
            let endUs = root.sceneTimingOriginalEndUs + delta
            if (root.sceneTimingSnapEndEligible
                    && Math.abs(endUs - playheadUs) <= snapThresholdUs) {
                delta = Math.max(
                    root.sceneTimingMinimumDeltaUs,
                    Math.min(
                        root.sceneTimingMaximumDeltaUs,
                        playheadUs - root.sceneTimingOriginalEndUs
                    )
                )
                endUs = root.sceneTimingOriginalEndUs + delta
                root.sceneTimingSnapActive = true
                root.sceneTimingSnapEdge = "end"
                root.sceneTimingSnapTargetUs = playheadUs
            }
            root.sceneTimingPreviewEndUs = endUs
            root.sceneTimingPreviewDeltaUs = delta
        }
        if (root.sceneTimingMode === "move")
            root.appModel.previewSelectedSceneTextMove(root.sceneTimingPreviewDeltaUs)
        else
            root.appModel.previewSelectedSceneTextResize(
                root.sceneTimingMode,
                root.sceneTimingPreviewDeltaUs
            )
    }

    function finishSceneTiming() {
        if (!root.sceneTimingActive) return
        const changed = Math.abs(
            root.sceneTimingPreviewStartUs - root.sceneTimingOriginalStartUs
        ) >= 1 || Math.abs(
            root.sceneTimingPreviewEndUs - root.sceneTimingOriginalEndUs
        ) >= 1 || (root.sceneTimingMode === "move"
            && root.sceneTimingLaneDelta !== 0)
        root.sceneTimingActive = false
        if (!changed) {
            root.resetSceneTiming()
            return
        }
        root.sceneTimingPending = true
        if (root.sceneTimingMode === "move")
            root.sceneTimingGeneration = root.appModel.moveSelectedSceneTexts(
                root.sceneTimingPreviewDeltaUs,
                root.sceneTimingLaneDelta
            )
        else
            root.sceneTimingGeneration = root.appModel.resizeSelectedSceneTexts(
                root.sceneTimingMode,
                root.sceneTimingPreviewDeltaUs
            )
        if (!root.sceneTimingGeneration)
            root.cancelPendingSceneTiming()
    }

    function cancelPendingSceneTiming() {
        if (root.sceneTimingMode === "move")
            root.appModel.previewSelectedSceneTextMove(0)
        else
            root.appModel.previewSelectedSceneTextResize(root.sceneTimingMode, 0)
        root.resetSceneTiming()
    }

    function cancelSceneTiming() {
        if (!root.sceneTimingActive) return
        if (root.sceneTimingMode === "move")
            root.appModel.previewSelectedSceneTextMove(0)
        else
            root.appModel.previewSelectedSceneTextResize(root.sceneTimingMode, 0)
        root.resetSceneTiming()
    }

    function resetSceneTiming() {
        root.sceneTimingActive = false
        root.sceneTimingPending = false
        root.sceneTimingGeneration = 0
        root.sceneTimingBases = ({})
        root.sceneTimingClipId = ""
        root.sceneTimingCueId = ""
        root.sceneTimingMode = ""
        root.sceneTimingTrackIndex = -1
        root.sceneTimingTrackId = ""
        root.sceneTimingPreviewDeltaUs = 0
        root.sceneTimingMinimumDeltaUs = 0
        root.sceneTimingMaximumDeltaUs = 0
        root.sceneTimingMinimumLaneDelta = 0
        root.sceneTimingLaneDelta = 0
        root.sceneTimingSnapActive = false
        root.sceneTimingSnapEdge = ""
        root.sceneTimingSnapTargetUs = -1
    }

    function beginClipMove(clipId, linkGroupId, startUs, endUs, trackIndex, pointerX) {
        if (root.clipMovePending || root.appModel.timelineBusy) return
        root.forceActiveFocus()
        root.appModel.selectClip(clipId)
        root.selectInspectorTab("clip")
        root.clipMoveClipId = String(clipId)
        root.clipMoveLinkGroupId = String(linkGroupId || "")
        root.clipMoveOriginalStartUs = Number(startUs)
        root.clipMoveOriginalEndUs = Number(endUs)
        root.clipMovePointerOriginUs = root.clipLayerTimeAt(pointerX)
        root.clipMovePreviewDeltaUs = 0
        root.clipMoveTrackIndex = Number(trackIndex)
        root.clipMoveSnapActive = false
        root.clipMoveActive = true
    }

    function updateClipMove(pointerX) {
        if (!root.clipMoveActive) return
        const rawDelta = root.clipLayerTimeAt(pointerX) - root.clipMovePointerOriginUs
        const minimumDelta = -root.clipMoveOriginalStartUs
        const maximumDelta = root.projectDurationUs - root.clipMoveOriginalEndUs
        let delta = Math.max(minimumDelta, Math.min(maximumDelta, rawDelta))
        const candidateStartUs = root.clipMoveOriginalStartUs + delta
        root.clipMoveSnapActive = candidateStartUs <= root.clipResizeSnapToleranceUs()
        if (root.clipMoveSnapActive) delta = -root.clipMoveOriginalStartUs
        root.clipMovePreviewDeltaUs = delta
    }

    function finishClipMove() {
        if (!root.clipMoveActive) return
        const changed = Math.abs(root.clipMovePreviewDeltaUs) >= 1
        root.clipMoveActive = false
        if (!changed) {
            root.resetClipMove()
            return
        }
        root.clipMovePending = true
        root.appModel.moveSelectedClip(
            root.clipMoveOriginalStartUs + root.clipMovePreviewDeltaUs
        )
    }

    function cancelClipMove() {
        if (!root.clipMoveActive) return
        root.resetClipMove()
    }

    function resetClipMove() {
        root.clipMoveActive = false
        root.clipMovePending = false
        root.clipMoveClipId = ""
        root.clipMoveLinkGroupId = ""
        root.clipMovePreviewDeltaUs = 0
        root.clipMoveTrackIndex = -1
        root.clipMoveSnapActive = false
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
        if (!timelineViewportTimer.running) timelineViewportTimer.start()
    }

    function syncTimelineViewportModels() {
        // Do not recycle the delegate currently holding a drag/resize grab.
        if (sceneTimingActive || clipMoveActive || clipResizeActive || openContextMenus > 0) {
            if (!timelineViewportTimer.running) timelineViewportTimer.start()
            return
        }
        const prefetchUs = Math.min(
            10_000_000, Math.max(250_000, viewportDurationUs * 0.25)
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

    function scheduleTrackViewportUpdate() {
        if (!trackViewportTimer.running) trackViewportTimer.start()
    }

    function applyTrackViewport() {
        if (sceneTimingActive || clipMoveActive || clipResizeActive || openContextMenus > 0) {
            if (!trackViewportTimer.running) trackViewportTimer.start()
            return
        }
        if (!trackList || trackList.height <= 0) return
        const firstTrack = Math.max(0, Math.floor(trackList.contentY / 62))
        const lastTrack = Math.max(
            firstTrack,
            Math.ceil((trackList.contentY + trackList.height) / 62) - 1
        )
        appModel.currentProject.clipModel.setTrackViewport(
            firstTrack,
            lastTrack,
            1
        )
    }

    function showProjectStart() {
        applyViewport(0, Math.min(projectDurationUs, 60_000_000))
    }

    function fitProject() {
        applyViewport(0, projectDurationUs)
    }

    function fitSelection() {
        const cue = appModel.selectedCue || ({})
        const startUs = Number(cue.start_us ?? -1)
        const endUs = Number(cue.end_us ?? -1)
        if (startUs < 0 || endUs <= startUs) {
            zoomBy(0.7)
            return
        }
        const padding = Math.max(250_000, (endUs - startUs) * 0.35)
        applyViewport(startUs - padding, endUs - startUs + padding * 2)
    }

    function panBy(deltaUs) {
        applyViewport(viewportStartUs + deltaUs, viewportDurationUs)
    }

    function keepPlayheadVisible() {
        if (!followPlayhead || !timelinePreview.playing || clipResizeActive
                || sceneTimingActive || clipMoveActive) return
        const playhead = Number(appModel.playheadUs)
        const leftGuard = viewportStartUs + viewportDurationUs * 0.04
        const rightGuard = viewportEndUs - viewportDurationUs * 0.04
        if (playhead >= rightGuard)
            applyViewport(playhead - viewportDurationUs * 0.12, viewportDurationUs)
        else if (playhead < leftGuard)
            applyViewport(playhead - viewportDurationUs * 0.88, viewportDurationUs)
    }

    function setFollowPlayhead(enabled) {
        followPlayhead = enabled
        appModel.setUiIntValue("timelineFollowPlayhead", enabled ? 1 : 0)
        if (enabled) keepPlayheadVisible()
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
        fittedProjectDurationUs = appModel.currentProject.durationUs
        fitProject()
        refreshClipFields()
    }

    VideoClipMenu {
        id: videoMenu
        appModel: root.appModel
        onPauseRequested: timelinePreview.pause()
        onAboutToShow: ++root.openContextMenus
        onClosed: root.openContextMenus = Math.max(0, root.openContextMenus - 1)
    }

    Connections {
        target: root.appModel.currentProject
        function onChanged() {
            const duration = Math.max(0, root.appModel.currentProject.durationUs)
            // Import creates the empty project before its media/track ACK.
            // Initialize once when that same project first receives a duration.
            if (root.fittedProjectId !== root.appModel.currentProject.projectId
                    || (root.fittedProjectDurationUs <= 0 && duration > 0)) {
                root.fittedProjectId = root.appModel.currentProject.projectId
                root.fittedProjectDurationUs = duration
                root.fitProject()
                return
            }
            root.fittedProjectDurationUs = duration
            root.applyViewport(root.viewportStartUs, root.viewportDurationUs)
        }
    }

    Connections {
        target: root.appModel
        function onTimelineSettingsRequested() { root.selectInspectorTab("settings") }
        function onSelectedClipChanged() { root.refreshClipFields() }
        function onSelectedCueChanged() {
            if (root.appModel.selectedCueCount > 0) root.selectInspectorTab("text")
        }
        function onPlayheadChanged() { root.keepPlayheadVisible() }
        function onTimelineBusyChanged() {
            if (root.clipResizePending && !root.appModel.timelineBusy)
                root.resetClipResize()
            if (root.clipMovePending && !root.appModel.timelineBusy)
                root.resetClipMove()
        }
        function onSceneTimingSettled(generation, succeeded) {
            if (root.sceneTimingPending && generation === root.sceneTimingGeneration)
                root.resetSceneTiming()
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
                SplitView.minimumHeight: 200
                // Typography lives in the side inspector. Reserve track space
                // without forcing the overview outside the SplitView hit area.
                SplitView.maximumHeight: Math.max(
                    200, verticalTimelineSplit.height - 270
                )
                color: Theme.canvas
                onHeightChanged: persistPreviewRatio.restart()
                TimelinePreview {
                    id: timelinePreview
                    anchors.fill: parent
                    appModel: root.appModel
                    showProjectOverlays: true
                    editableProjectOverlays: root.inspectorTab === "settings" || root.inspectorTab === "project"
                    videoContextMenuEnabled: true
                    onVideoContextMenuRequested: function(x, y) {
                        videoMenu.openAt(timelinePreview, x, y)
                    }
                    ocrRegionEditorMode: root.regionEditorMode
                    onColorPicked: function(target, color) {
                        subtitleTextToolbar.applyPickedColor(target, color)
                    }
                    onRegionDrawn: function(mode, x, y, width, height) {
                        if (mode !== "manual_text") return
                        manualCueDialog.submitAtRegion(x, y, width, height)
                        root.regionEditorMode = ""
                    }
                }
            }

            Item {
                SplitView.fillWidth: true
                SplitView.fillHeight: true
                SplitView.minimumHeight: 260

                ColumnLayout {
                    anchors.fill: parent
                    spacing: Theme.space8

            Flow {
                Layout.fillWidth: true
                Layout.preferredHeight: implicitHeight
                spacing: Theme.space8
                DsButton {
                    text: "T"
                    compact: true
                    primary: root.regionEditorMode === "manual_text"
                    tooltipText: "Thêm cue thoại hoặc chữ trong cảnh tại playhead."
                    enabled: root.appModel.currentProject.hasVideo
                    disabledReason: "Cần mở video trước khi thêm chữ."
                    onClicked: manualCueDialog.openForPlayhead(root.appModel.playheadUs)
                    Accessible.name: "Thêm chữ vào video tại playhead"
                }
                DsButton {
                    text: "＋ Mark"
                    compact: true
                    primary: false
                    tooltipText: "Đánh dấu vị trí playhead (phím M)."
                    enabled: root.appModel.currentProject.hasProject
                        && !root.appModel.timelineBusy
                    onClicked: root.appModel.addTimelineMarker()
                    Accessible.name: "Thêm mark tại playhead"
                }
                DsButton {
                    text: "Xoá mark"
                    compact: true
                    danger: true
                    tooltipText: "Xoá mark gần playhead (Shift+M)."
                    enabled: root.appModel.currentProject.hasProject
                        && !root.appModel.timelineBusy
                    onClicked: root.appModel.removeTimelineMarkerAtPlayhead()
                    Accessible.name: "Xoá mark gần playhead"
                }
                DsIconButton {
                    objectName: "timelineScissorsButton"
                    iconName: "scissors"
                    tooltipText: "Tách video tại playhead (X). Tách hai đầu rồi chọn đoạn và nhấn Delete để xóa."
                    enabled: root.appModel.currentProject.hasVideo && !root.appModel.timelineBusy
                    onClicked: {
                        timelinePreview.pause()
                        root.appModel.splitVideoAtPlayhead()
                    }
                    Accessible.name: "Tách video tại playhead, phím X"
                }
                DsButton {
                    text: "Khớp D1"
                    compact: true
                    tooltipText: "Mở công cụ cân thời lượng từng cue D1 theo mốc SRT."
                    enabled: root.appModel.currentProject.hasVoiceTrack
                    disabledReason: "Cần tạo D1 trước khi khớp từng cue theo timestamp SRT."
                    onClicked: root.selectInspectorTab("voice-sync")
                }
                DsButton {
                    text: "↶"
                    compact: true
                    tooltipText: "Hoàn tác thay đổi Timeline gần nhất."
                    enabled: root.appModel.canUndoTimeline && !root.appModel.timelineBusy
                    disabledReason: "Chưa có thao tác để hoàn tác."
                    onClicked: root.appModel.undoTimeline()
                }
                DsButton {
                    text: "↷"
                    compact: true
                    tooltipText: "Làm lại thay đổi Timeline vừa hoàn tác."
                    enabled: root.appModel.canRedoTimeline && !root.appModel.timelineBusy
                    disabledReason: "Chưa có thao tác để làm lại."
                    onClicked: root.appModel.redoTimeline()
                }
                DsButton {
                    objectName: "timelineSettingsButton"
                    text: "Thiết lập"
                    compact: true
                    tooltipText: "Thiết lập xuất, phụ đề, logo, blur, filter và âm thanh."
                    onClicked: root.selectInspectorTab("settings")
                }
                DsButton {
                    text: "Đầu"
                    compact: true
                    tooltipText: "Đưa khung nhìn Timeline về 60 giây đầu dự án."
                    enabled: root.appModel.currentProject.hasProject
                    disabledReason: "Timeline chưa có nội dung."
                    onClicked: root.showProjectStart()
                }
                DsIconButton {
                    iconName: "zoom-out"
                    tooltipText: "Thu nhỏ Timeline"
                    enabled: root.appModel.currentProject.hasProject
                    onClicked: root.zoomBy(1.35)
                }
                DsIconButton {
                    iconName: "zoom-in"
                    tooltipText: "Phóng to Timeline tại playhead"
                    enabled: root.appModel.currentProject.hasProject
                    onClicked: root.zoomBy(0.74)
                }
                DsIconButton {
                    iconName: "focus"
                    tooltipText: "Vừa cue hoặc vùng đang chọn"
                    enabled: root.appModel.currentProject.hasProject
                    onClicked: root.fitSelection()
                }
                DsIconButton {
                    iconName: "maximize"
                    tooltipText: "Vừa toàn bộ dự án"
                    enabled: root.appModel.currentProject.hasProject
                    onClicked: root.fitProject()
                }
                DsIconButton {
                    iconName: "locate"
                    tooltipText: root.followPlayhead
                        ? "Tắt tự bám playhead" : "Bật tự bám playhead"
                    active: root.followPlayhead
                    enabled: root.appModel.currentProject.hasProject
                    onClicked: root.setFollowPlayhead(!root.followPlayhead)
                }
                Text {
                    text: Math.round(root.projectDurationUs / root.viewportDurationUs * 100) + "%"
                    color: Theme.textSecondary
                    font.family: Theme.timecodeFamily
                    font.pixelSize: 10
                }
                Slider {
                    id: zoomSlider
                    width: 120
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
                Text {
                    text: "Bước"
                    color: Theme.textMuted
                    font.pixelSize: 10
                }
                DsSpinBox {
                    width: 104
                    from: 1
                    to: 1000
                    value: root.appModel.timelineSeekStepMs
                    accessibleName: "Bước tua chính xác từ 1 đến 1000 mili giây"
                    onValueModified: root.appModel.setTimelineSeekStepMs(value)
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
                            ScrollBar.vertical: ScrollBar {
                                policy: ScrollBar.AsNeeded
                            }
                            onContentYChanged: {
                                if (!modelResetInProgress)
                                    contentYBeforeModelReset = contentY
                                root.scheduleTrackViewportUpdate()
                            }
                            onHeightChanged: root.scheduleTrackViewportUpdate()
                            Component.onCompleted: root.scheduleTrackViewportUpdate()
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
                                        root.scheduleTrackViewportUpdate()
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
                                            visible: trackRow.model.kind !== "text"
                                            text: trackRow.model.trackId
                                                + (trackRow.model.muted ? "  ·  Tắt tiếng" : "")
                                                + (trackRow.model.locked ? "  ·  Đã khóa" : "")
                                            color: Theme.textMuted
                                            font.family: Theme.timecodeFamily
                                            font.pixelSize: 10
                                        }
                                        DsButton {
                                            id: addTimelineTitleButton
                                            objectName: "addTimelineTitleButton"
                                            visible: trackRow.model.kind === "text"
                                            text: "T + Thêm text"
                                            compact: true
                                            enabled: root.appModel.currentProject.hasVideo
                                                && !trackRow.model.locked
                                            tooltipText: "Thêm tên phim/kênh trên track Text riêng, không lồng tiếng."
                                            onClicked: {
                                                timelinePreview.pause()
                                                manualCueDialog.openForTitle(root.appModel.playheadUs)
                                            }
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
                                    property string trackId: String(trackRow.model.trackId || "")
                                    anchors.left: parent.left
                                    anchors.top: parent.top
                                    anchors.bottom: parent.bottom
                                    width: 188
                                    acceptedButtons: Qt.LeftButton | Qt.RightButton
                                    onClicked: function(mouse) {
                                        const titleButtonPoint = addTimelineTitleButton.mapFromItem(
                                            this, mouse.x, mouse.y
                                        )
                                        if (mouse.button === Qt.LeftButton
                                                && addTimelineTitleButton.visible
                                                && addTimelineTitleButton.enabled
                                                && titleButtonPoint.x >= 0
                                                && titleButtonPoint.y >= 0
                                                && titleButtonPoint.x < addTimelineTitleButton.width
                                                && titleButtonPoint.y < addTimelineTitleButton.height) {
                                            timelinePreview.pause()
                                            manualCueDialog.openForTitle(root.appModel.playheadUs)
                                            return
                                        }
                                        const textTrack = trackRow.model.kind === "subtitle"
                                            || trackRow.model.kind === "text"
                                        if (textTrack && mouse.button === Qt.LeftButton) {
                                            root.appModel.selectTimelineTrackCues(
                                                trackRow.model.trackId,
                                                Boolean(mouse.modifiers & Qt.ControlModifier)
                                            )
                                            root.selectInspectorTab("text")
                                            return
                                        }
                                        if (mouse.modifiers & Qt.ControlModifier)
                                            root.appModel.toggleTrackSelection(trackRow.model.trackId)
                                        else
                                            root.appModel.selectTrack(trackRow.model.trackId)
                                        root.selectInspectorTab("track")
                                        if (mouse.button === Qt.RightButton && root.appModel.hasSelectedTrack) {
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
                                            visible: trackRow.model.kind !== "text"
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
                                    width: 352
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

                            WheelHandler {
                                target: null
                                acceptedModifiers: Qt.ControlModifier
                                onWheel: function(event) {
                                    root.zoomAt(
                                        event.angleDelta.y >= 0 ? 0.8 : 1.25,
                                        Math.max(0, Math.min(
                                            1, event.x / Math.max(1, clipLayer.width)
                                        ))
                                    )
                                    event.accepted = true
                                }
                            }

                            WheelHandler {
                                target: null
                                acceptedModifiers: Qt.ShiftModifier
                                onWheel: function(event) {
                                    root.panBy(
                                        (event.angleDelta.y >= 0 ? -1 : 1)
                                            * root.viewportDurationUs * 0.12
                                    )
                                    event.accepted = true
                                }
                            }

                            MouseArea {
                                id: subtitleMarqueeArea
                                objectName: "timelineMarqueeArea"
                                anchors.fill: parent
                                z: 0
                                acceptedButtons: Qt.LeftButton | Qt.MiddleButton
                                preventStealing: true
                                property bool additive: false
                                onPressed: function(mouse) {
                                    root.forceActiveFocus()
                                    if (mouse.button === Qt.MiddleButton || root.spacePanHeld) {
                                        root.timelinePanActive = true
                                        root.timelinePanOriginX = mouse.x
                                        root.timelinePanOriginStartUs = root.viewportStartUs
                                        return
                                    }
                                    root.marqueeActive = false
                                    root.marqueeStart = Qt.point(mouse.x, mouse.y)
                                    root.marqueeEnd = root.marqueeStart
                                    additive = Boolean(mouse.modifiers & Qt.ControlModifier)
                                    if (!additive)
                                        root.appModel.clearTimelineCueSelection()
                                }
                                onPositionChanged: function(mouse) {
                                    if (!pressed) return
                                    if (root.timelinePanActive) {
                                        const deltaUs = (root.timelinePanOriginX - mouse.x)
                                            / Math.max(1, width) * root.viewportDurationUs
                                        root.applyViewport(
                                            root.timelinePanOriginStartUs + deltaUs,
                                            root.viewportDurationUs
                                        )
                                        return
                                    }
                                    root.marqueeEnd = Qt.point(mouse.x, mouse.y)
                                    if (Math.abs(root.marqueeEnd.x - root.marqueeStart.x) >= 4
                                            || Math.abs(root.marqueeEnd.y - root.marqueeStart.y) >= 4)
                                        root.marqueeActive = true
                                }
                                onReleased: function(mouse) {
                                    if (root.timelinePanActive) {
                                        root.timelinePanActive = false
                                        return
                                    }
                                    if (!root.marqueeActive) {
                                        root.seekTimelineAt(mouse.x)
                                        return
                                    }
                                    const left = Math.min(root.marqueeStart.x, root.marqueeEnd.x)
                                    const right = Math.max(root.marqueeStart.x, root.marqueeEnd.x)
                                    const top = Math.min(root.marqueeStart.y, root.marqueeEnd.y)
                                        + trackList.contentY
                                    const bottom = Math.max(root.marqueeStart.y, root.marqueeEnd.y)
                                        + trackList.contentY
                                    const startUs = root.clipLayerTimeAt(left)
                                    const endUs = root.clipLayerTimeAt(right)
                                    const firstTrack = Math.max(0, Math.floor(top / 62))
                                    const lastTrack = Math.max(0, Math.floor(bottom / 62))
                                    const ids = root.appModel.currentProject.subtitleCueIdsInRange(
                                        startUs, endUs, firstTrack, lastTrack
                                    )
                                    root.appModel.selectSubtitleCueIds(ids, additive)
                                    root.marqueeActive = false
                                }
                                onCanceled: {
                                    root.marqueeActive = false
                                    root.timelinePanActive = false
                                }
                                onWheel: function(wheel) {
                                    const direction = wheel.angleDelta.y >= 0 ? 1 : -1
                                    if (wheel.modifiers & Qt.ControlModifier) {
                                        root.zoomAt(
                                            direction > 0 ? 0.8 : 1.25,
                                            Math.max(0, Math.min(1, wheel.x / Math.max(1, width)))
                                        )
                                    } else if (wheel.modifiers & Qt.ShiftModifier) {
                                        root.panBy(-direction * root.viewportDurationUs * 0.12)
                                    } else {
                                        const maximumY = Math.max(
                                            0, trackList.contentHeight - trackList.height
                                        )
                                        trackList.contentY = Math.max(
                                            0,
                                            Math.min(maximumY, trackList.contentY - wheel.angleDelta.y)
                                        )
                                    }
                                    wheel.accepted = true
                                }
                            }

                            Rectangle {
                                visible: root.marqueeActive
                                z: 20
                                x: Math.min(root.marqueeStart.x, root.marqueeEnd.x)
                                y: Math.min(root.marqueeStart.y, root.marqueeEnd.y)
                                width: Math.abs(root.marqueeEnd.x - root.marqueeStart.x)
                                height: Math.abs(root.marqueeEnd.y - root.marqueeStart.y)
                                color: Qt.rgba(0.20, 0.55, 0.95, 0.16)
                                border.width: 1
                                border.color: Theme.primary
                            }

                            Repeater {
                                id: timelineMarkerRepeater
                                z: 3
                                model: root.appModel.currentProject.timelineMarkers
                                delegate: Item {
                                    required property var modelData
                                    readonly property real markerUs: Number(modelData)
                                    readonly property bool inViewport:
                                        markerUs >= root.viewportStartUs
                                        && markerUs <= root.viewportEndUs
                                    visible: inViewport
                                    x: (markerUs - root.viewportStartUs)
                                        / root.viewportDurationUs * clipLayer.width - 1
                                    y: 0
                                    width: 2
                                    height: clipLayer.height
                                    Rectangle {
                                        anchors.fill: parent
                                        color: Theme.primary
                                        opacity: 0.82
                                    }
                                    Rectangle {
                                        x: -5
                                        y: 0
                                        width: 12
                                        height: 16
                                        radius: 3
                                        color: Theme.primary
                                        border.color: Theme.canvas
                                        border.width: 1
                                        Text {
                                            anchors.centerIn: parent
                                            text: "M"
                                            color: Theme.canvas
                                            font.bold: true
                                            font.pixelSize: 9
                                        }
                                    }
                                    MouseArea {
                                        anchors.fill: parent
                                        anchors.leftMargin: -5
                                        anchors.rightMargin: -5
                                        acceptedButtons: Qt.LeftButton | Qt.RightButton
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: function(mouse) {
                                            if (mouse.button === Qt.RightButton)
                                                root.appModel.removeTimelineMarker(markerUs)
                                            else
                                                root.appModel.setPlayheadMs(markerUs / 1000)
                                        }
                                        ToolTip.visible: containsMouse
                                        ToolTip.text: "Mark " + (markerUs / 1000000).toFixed(3) + " s"
                                    }
                                }
                            }

                            Repeater {
                                z: 2
                                model: root.appModel.currentProject.clipModel
                                delegate: Rectangle {
                                    id: clipItem
                                    required property var model
                                    // Repeater delegates are siblings of the
                                    // Repeater, so its z value is not inherited.
                                    // Keep clip gestures above the blank seek/
                                    // marquee MouseArea at every scroll offset.
                                    // Overlapping labels share the one Text line.
                                    // A selected label must remain reachable above
                                    // a longer title spanning the whole video.
                                    z: model.trackKind === "text" && root.cueSelected(model.cueId) ? 4 : 2
                                    readonly property real laneWidth: clipLayer.width
                                    readonly property bool usesResizePreview: root.clipUsesResizePreview(
                                        model.clipId
                                    )
                                    readonly property bool usesSceneTimingPreview:
                                        root.clipUsesSceneTimingPreview(
                                            model.clipId, model.cueId
                                        )
                                    readonly property bool usesMovePreview: root.clipUsesMovePreview(
                                        model.clipId, model.linkGroupId
                                    )
                                    readonly property bool isSceneTextClip:
                                        (model.trackKind === "subtitle" || model.trackKind === "text")
                                        && Boolean(model.cueId)
                                    readonly property bool isSubtitleClip:
                                        (model.trackKind === "subtitle" || model.trackKind === "text")
                                        && Boolean(model.cueId)
                                    readonly property bool isMovableMediaClip:
                                        (model.trackKind === "video"
                                            || model.trackKind === "original_audio")
                                        && !model.trackLocked
                                    readonly property real effectiveStart: usesSceneTimingPreview
                                        ? (root.sceneTimingMode === "move"
                                            || root.sceneTimingMode === "left"
                                            ? root.sceneTimingBase(model.cueId, "start", model.startUs)
                                                + root.sceneTimingPreviewDeltaUs
                                            : root.sceneTimingBase(model.cueId, "start", model.startUs))
                                        : (usesMovePreview
                                            ? model.startUs + root.clipMovePreviewDeltaUs
                                            : (usesResizePreview
                                                ? root.clipResizePreviewStartUs : model.startUs))
                                    readonly property real effectiveEnd: usesSceneTimingPreview
                                        ? (root.sceneTimingMode === "move"
                                            || root.sceneTimingMode === "right"
                                            ? root.sceneTimingBase(model.cueId, "end", model.endUs)
                                                + root.sceneTimingPreviewDeltaUs
                                            : root.sceneTimingBase(model.cueId, "end", model.endUs))
                                        : (usesMovePreview
                                            ? model.endUs + root.clipMovePreviewDeltaUs
                                            : (usesResizePreview
                                                ? root.clipResizePreviewEndUs : model.endUs))
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
                                    y: (model.trackIndex
                                            + (usesSceneTimingPreview
                                                && root.sceneTimingMode === "move"
                                                ? root.sceneTimingLaneDelta : 0))
                                        * 62 - trackList.contentY + 12
                                    width: Math.max(
                                        8,
                                        (clippedEnd - clippedStart)
                                            / root.viewportDurationUs * laneWidth
                                    )
                                    height: model.trackKind === "video" ? 46 : 34
                                    radius: Theme.radiusSmall
                                    // A dense D1 timeline can make a cue narrower
                                    // than the label's horizontal padding. Clip the
                                    // delegate itself so a narrow cue can never paint
                                    // into its neighbours; the full text is still
                                    // available from the hover tooltip/Inspector.
                                    clip: true
                                    color: model.isAggregate
                                        ? Theme.surfaceRaised
                                        : (model.overlapWarning && clipItem.isSubtitleClip
                                            ? Theme.errorTint
                                        : root.clipFillColor(
                                            effectiveTimingState,
                                            model.trackKind,
                                            root.cueSelected(model.cueId)
                                                || root.appModel.selectedClipId === model.clipId
                                        ))
                                    border.width: !model.isAggregate
                                        && model.overlapWarning && clipItem.isSubtitleClip
                                        ? 3
                                        : (!model.isAggregate
                                            && effectiveTimingState === "overflow"
                                            ? 3
                                            : (!model.isAggregate
                                                && (effectiveTimingState === "underflow"
                                                    || effectiveTimingState === "position_mismatch"
                                                    || effectiveTimingState === "matched"
                                                    || root.cueSelected(model.cueId)
                                                    || root.appModel.selectedClipId === model.clipId)
                                                ? 2 : 1))
                                    border.color: model.isAggregate
                                        ? Theme.borderStrong
                                        : (model.overlapWarning && clipItem.isSubtitleClip
                                            ? Theme.errorBorder
                                        : root.clipBorderColor(
                                            effectiveTimingState,
                                            model.trackKind,
                                            root.cueSelected(model.cueId)
                                                || root.appModel.selectedClipId === model.clipId
                                        ))
                                    Accessible.name: model.isAggregate
                                        ? model.trackName + ", " + model.aggregateCount
                                            + " cue tổng quan; phóng to để xem chữ"
                                        : model.trackName + ", clip " + model.clipId
                                    Accessible.role: Accessible.ListItem

                                    TimelineMediaStrip {
                                        anchors.fill: parent
                                        anchors.margins: 2
                                        visible: !clipItem.model.isAggregate && !clipItem.isSubtitleClip
                                        projectModel: root.appModel.currentProject
                                        clipId: clipItem.model.clipId
                                        startUs: clipItem.clippedStart
                                        endUs: clipItem.clippedEnd
                                        loadingEnabled: visible && !root.timelinePanActive
                                            && !root.clipMoveActive && !root.clipResizeActive
                                        opacity: 0.9
                                    }
                                    Text {
                                        z: 1
                                        anchors.centerIn: parent
                                        visible: clipItem.model.isAggregate || parent.width >= 28
                                        width: Math.max(0, parent.width - Theme.space12)
                                        text: clipItem.model.isAggregate
                                            ? clipItem.model.aggregateCount
                                                + " cue · phóng to để xem chữ"
                                            : (clipItem.isSubtitleClip
                                                ? (clipItem.model.cueText || clipItem.model.trackId)
                                                : ((clipItem.model.cueText || (clipItem.model.trackKind === "voice" ? "D1" : ""))
                                                + "  "
                                                + Number(clipItem.model.speedPercent / 100).toFixed(2)
                                                + "×  |  "
                                                + (clipItem.model.muted ? "Tắt tiếng" : Math.round(clipItem.model.volume * 100) + "%")))
                                        color: Theme.textPrimary
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 10
                                        style: clipItem.isSubtitleClip ? Text.Normal : Text.Outline
                                        styleColor: Theme.canvas
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
                                    Rectangle {
                                        visible: clipItem.model.overlapWarning
                                            && clipItem.isSubtitleClip
                                            && !clipItem.model.isAggregate
                                        anchors.left: parent.left
                                        anchors.top: parent.top
                                        width: 18
                                        height: 18
                                        radius: 9
                                        color: Theme.error
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
                                        ? ((clipItem.model.overlapWarning && clipItem.isSubtitleClip
                                            ? "⚠ Cue chồng nhau trên 2/3 thời lượng; hãy kiểm tra.\n"
                                            : "")
                                            + (clipItem.model.trackKind === "text"
                                            ? ("Text #" + clipItem.model.cueIndex + " · "
                                                + clipItem.model.cueText + " · "
                                                + (clipItem.effectiveStart / 1000000).toFixed(3)
                                                + "–" + (clipItem.effectiveEnd / 1000000).toFixed(3) + " s")
                                            : ("Cue #" + clipItem.model.cueIndex + " · "
                                            + clipItem.model.cueText
                                            + " · " + root.timingStateText(
                                                clipItem.effectiveTimingState,
                                                clipItem.effectiveDurationDeltaUs,
                                                clipItem.effectiveStart - clipItem.model.slotStartUs,
                                                clipItem.effectiveRequiredSpeed
                                            ))))
                                        : ""

                                    MouseArea {
                                        objectName: "timelineClipBody-" + clipItem.model.clipId
                                        anchors.fill: parent
                                        enabled: !clipItem.model.isAggregate
                                        acceptedButtons: Qt.LeftButton | Qt.RightButton
                                        cursorShape: clipItem.isSceneTextClip
                                            || clipItem.isMovableMediaClip
                                            ? Qt.SizeAllCursor : Qt.ArrowCursor
                                        preventStealing: true
                                        property real dragStartX: 0
                                        property real dragStartY: 0
                                        property bool dragStarted: false
                                        property bool selectionHandledOnPress: false
                                        onPressed: function(mouse) {
                                            dragStarted = false
                                            selectionHandledOnPress = false
                                            if (mouse.button !== Qt.LeftButton
                                                    || (!clipItem.isSceneTextClip
                                                        && !clipItem.isMovableMediaClip))
                                                return
                                            if (clipItem.isSubtitleClip
                                                    && !root.cueSelected(clipItem.model.cueId)) {
                                                root.appModel.selectTimelineCue(
                                                    String(clipItem.model.cueId),
                                                    Boolean(
                                                        mouse.modifiers
                                                            & Qt.ControlModifier
                                                    )
                                                )
                                                selectionHandledOnPress = true
                                            }
                                            const point = mapToItem(clipLayer, mouse.x, mouse.y)
                                            dragStartX = point.x
                                            dragStartY = point.y
                                        }
                                        onPositionChanged: function(mouse) {
                                            if (!pressed || !(mouse.buttons & Qt.LeftButton)
                                                    || (!clipItem.isSceneTextClip
                                                        && !clipItem.isMovableMediaClip))
                                                return
                                            const point = mapToItem(clipLayer, mouse.x, mouse.y)
                                            if (!dragStarted
                                                    && (Math.abs(point.x - dragStartX) >= 4
                                                        || Math.abs(point.y - dragStartY) >= 4)) {
                                                dragStarted = true
                                                if (clipItem.isSceneTextClip) {
                                                    root.beginSceneTiming(
                                                        clipItem.model.clipId,
                                                        clipItem.model.cueId,
                                                        "move",
                                                        clipItem.model.startUs,
                                                        clipItem.model.endUs,
                                                        clipItem.model.trackIndex,
                                                        clipItem.model.trackId,
                                                        dragStartX,
                                                        dragStartY
                                                    )
                                                } else {
                                                    root.beginClipMove(
                                                        clipItem.model.clipId,
                                                        clipItem.model.linkGroupId,
                                                        clipItem.model.startUs,
                                                        clipItem.model.endUs,
                                                        clipItem.model.trackIndex,
                                                        dragStartX
                                                    )
                                                }
                                            }
                                            if (!dragStarted) return
                                            if (clipItem.isSceneTextClip)
                                                root.updateSceneTiming(point.x, point.y)
                                            else
                                                root.updateClipMove(point.x)
                                        }
                                        onReleased: function(mouse) {
                                            if (!dragStarted || mouse.button !== Qt.LeftButton) return
                                            if (clipItem.isSceneTextClip) {
                                                const point = mapToItem(clipLayer, mouse.x, mouse.y)
                                                root.updateSceneTiming(point.x, point.y)
                                                root.finishSceneTiming()
                                            } else
                                                root.finishClipMove()
                                        }
                                        onCanceled: {
                                            if (!dragStarted) return
                                            if (clipItem.isSceneTextClip)
                                                root.cancelSceneTiming()
                                            else
                                                root.cancelClipMove()
                                        }
                                        onClicked: function(mouse) {
                                            if (dragStarted) {
                                                dragStarted = false
                                                return
                                            }
                                            root.forceActiveFocus()
                                            if (selectionHandledOnPress
                                                    || (mouse.button === Qt.RightButton
                                                        && root.cueSelected(clipItem.model.cueId)))
                                                root.appModel.selectClip(clipItem.model.clipId)
                                            else
                                                root.appModel.selectTimelineClip(
                                                    clipItem.model.clipId,
                                                    Boolean(mouse.modifiers & Qt.ControlModifier)
                                                )
                                            root.selectInspectorTab(clipItem.model.cueId ? "text" : "clip")
                                            if (mouse.button === Qt.RightButton) {
                                                if (clipItem.model.trackKind === "video")
                                                    videoMenu.openAt(clipItem, mouse.x, mouse.y)
                                                else
                                                    clipMenuLoader.openAt(clipItem, mouse.x, mouse.y)
                                            }
                                        }
                                        onDoubleClicked: {
                                            if (clipItem.isSubtitleClip)
                                                root.appModel.openSubtitleCueInEditor(
                                                    String(clipItem.model.cueId),
                                                    Number(clipItem.model.startUs),
                                                    Number(clipItem.model.cueIndex)
                                                )
                                            else if (clipItem.model.trackKind === "voice" && clipItem.model.cueId)
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
                                            || clipItem.isSceneTextClip
                                        width: Math.min(14, parent.width / 3)
                                        anchors.left: parent.left
                                        anchors.top: parent.top
                                        anchors.bottom: parent.bottom
                                        cursorShape: Qt.SizeHorCursor
                                        preventStealing: true
                                        z: 5
                                        onPressed: function(mouse) {
                                            const point = mapToItem(clipLayer, mouse.x, mouse.y)
                                            if (clipItem.isSceneTextClip)
                                                root.beginSceneTiming(
                                                    clipItem.model.clipId,
                                                    clipItem.model.cueId,
                                                    "left",
                                                    clipItem.model.startUs,
                                                    clipItem.model.endUs,
                                                    clipItem.model.trackIndex,
                                                    clipItem.model.trackId,
                                                    point.x,
                                                    point.y
                                                )
                                            else
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
                                            if (clipItem.isSceneTextClip)
                                                root.updateSceneTiming(point.x, point.y)
                                            else
                                                root.updateClipResize(point.x)
                                        }
                                        onReleased: function(mouse) {
                                            if (clipItem.isSceneTextClip) {
                                                const point = mapToItem(clipLayer, mouse.x, mouse.y)
                                                root.updateSceneTiming(point.x, point.y)
                                                root.finishSceneTiming()
                                            } else
                                                root.finishClipResize()
                                        }
                                        onCanceled: {
                                            if (clipItem.isSceneTextClip)
                                                root.cancelSceneTiming()
                                            else
                                                root.cancelClipResize()
                                        }
                                    }
                                    MouseArea {
                                        objectName: "timelineClipRightHandle-" + clipItem.model.clipId
                                        visible: clipItem.model.trackKind === "voice"
                                            || clipItem.isSceneTextClip
                                        width: Math.min(14, parent.width / 3)
                                        anchors.right: parent.right
                                        anchors.top: parent.top
                                        anchors.bottom: parent.bottom
                                        cursorShape: Qt.SizeHorCursor
                                        preventStealing: true
                                        z: 5
                                        onPressed: function(mouse) {
                                            const point = mapToItem(clipLayer, mouse.x, mouse.y)
                                            if (clipItem.isSceneTextClip)
                                                root.beginSceneTiming(
                                                    clipItem.model.clipId,
                                                    clipItem.model.cueId,
                                                    "right",
                                                    clipItem.model.startUs,
                                                    clipItem.model.endUs,
                                                    clipItem.model.trackIndex,
                                                    clipItem.model.trackId,
                                                    point.x,
                                                    point.y
                                                )
                                            else
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
                                            if (clipItem.isSceneTextClip)
                                                root.updateSceneTiming(point.x, point.y)
                                            else
                                                root.updateClipResize(point.x)
                                        }
                                        onReleased: function(mouse) {
                                            if (clipItem.isSceneTextClip) {
                                                const point = mapToItem(clipLayer, mouse.x, mouse.y)
                                                root.updateSceneTiming(point.x, point.y)
                                                root.finishSceneTiming()
                                            } else
                                                root.finishClipResize()
                                        }
                                        onCanceled: {
                                            if (clipItem.isSceneTextClip)
                                                root.cancelSceneTiming()
                                            else
                                                root.cancelClipResize()
                                        }
                                    }

                                    Loader {
                                        id: clipMenuLoader
                                        active: false
                                        function openAt(sourceItem, localX, localY) {
                                            active = true
                                            item.openAt(sourceItem, localX, localY)
                                        }
                                        sourceComponent: Popup {
                                        id: clipMenu
                                        onAboutToShow: ++root.openContextMenus
                                        onClosed: root.openContextMenus = Math.max(0, root.openContextMenus - 1)

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
                                                visible: clipItem.isSubtitleClip
                                                text: clipItem.model.trackKind === "text"
                                                    ? "Sửa Text" : "Mở trong Biên tập phụ đề"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.openSubtitleCueInEditor(
                                                        String(clipItem.model.cueId),
                                                        Number(clipItem.model.startUs),
                                                        Number(clipItem.model.cueIndex)
                                                    )
                                                }
                                            }
                                            DsContextButton {
                                                objectName: "acceptSelectedSceneTextButton"
                                                Layout.fillWidth: true
                                                visible: clipItem.isSubtitleClip
                                                    && root.appModel.selectedSceneTextCueCount > 0
                                                enabled: !clipItem.model.trackLocked
                                                    && !root.appModel.selectedTextCueEditingLocked
                                                    && !root.appModel.subtitleBusy
                                                    && !root.appModel.timelineBusy
                                                text: "Chấp nhận chữ cảnh đã chọn"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.acceptSelectedSceneTextCues()
                                                }
                                            }
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                visible: clipItem.isSubtitleClip
                                                danger: true
                                                text: "Xóa cue chữ và nền"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.deleteSubtitleCue(
                                                        String(clipItem.model.cueId)
                                                    )
                                                }
                                            }
                                            MenuSeparator {
                                                Layout.fillWidth: true
                                                visible: clipItem.isSubtitleClip
                                            }
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                visible: !clipItem.isSubtitleClip && clipItem.model.trackKind !== "video"
                                                text: "Tách video và audio tại playhead"
                                                onClicked: {
                                                    clipMenu.close()
                                                    timelinePreview.pause()
                                                    root.appModel.splitVideoAtPlayhead()
                                                }
                                            }
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                visible: !clipItem.isSubtitleClip && clipItem.model.trackKind !== "video"
                                                text: "Bỏ đầu clip đến playhead, cả clip liên kết"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.trimSelectedHead(true)
                                                }
                                            }
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                visible: !clipItem.isSubtitleClip && clipItem.model.trackKind !== "video"
                                                text: "Bỏ đuôi clip từ playhead, cả clip liên kết"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.trimSelectedTail(true)
                                                }
                                            }
                                            MenuSeparator {
                                                Layout.fillWidth: true
                                                visible: !clipItem.isSubtitleClip && clipItem.model.trackKind !== "video"
                                            }
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                visible: !clipItem.isSubtitleClip && clipItem.model.trackKind !== "video"
                                                text: "Bỏ đầu clip đến playhead, chỉ clip này"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.trimSelectedHead(false)
                                                }
                                            }
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                visible: !clipItem.isSubtitleClip && clipItem.model.trackKind !== "video"
                                                text: "Bỏ đuôi clip từ playhead, chỉ clip này"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.trimSelectedTail(false)
                                                }
                                            }
                                            MenuSeparator {
                                                Layout.fillWidth: true
                                                visible: !clipItem.isSubtitleClip && clipItem.model.trackKind !== "video"
                                            }
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                visible: !clipItem.isSubtitleClip && clipItem.model.trackKind !== "video"
                                                text: "Tốc độ 85% cho clip liên kết"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.setSelectedSpeed(85, true)
                                                }
                                            }
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                visible: !clipItem.isSubtitleClip && clipItem.model.trackKind !== "video"
                                                text: "Tốc độ 100% cho clip liên kết"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.setSelectedSpeed(100, true)
                                                }
                                            }
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                visible: !clipItem.isSubtitleClip && clipItem.model.trackKind !== "video"
                                                text: "Tắt tiếng clip này"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.setSelectedVolume(0)
                                                }
                                            }
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                visible: !clipItem.isSubtitleClip && clipItem.model.trackKind !== "video"
                                                text: "Đặt lại âm lượng 100%"
                                                onClicked: {
                                                    clipMenu.close()
                                                    root.appModel.setSelectedVolume(100)
                                                }
                                            }
                                            DsContextButton {
                                                Layout.fillWidth: true
                                                visible: !clipItem.isSubtitleClip && clipItem.model.trackKind !== "video"
                                                danger: true
                                                text: "Xóa clip liên kết"
                                                onClicked: {
                                                    clipMenu.close()
                                                    timelinePreview.pause()
                                                    root.appModel.deleteTimelineSelection()
                                                }
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
                                id: sceneTimingSnapGuide
                                visible: root.sceneTimingActive
                                    && root.sceneTimingSnapActive
                                    && root.sceneTimingSnapTargetUs >= root.viewportStartUs
                                    && root.sceneTimingSnapTargetUs <= root.viewportEndUs
                                x: (root.sceneTimingSnapTargetUs - root.viewportStartUs)
                                    / root.viewportDurationUs * clipLayer.width
                                    - width / 2
                                y: 0
                                width: 3
                                height: clipLayer.height
                                color: Theme.success
                                opacity: 0.95
                                z: 32
                            }

                            Rectangle {
                                id: sceneTimingGuideLabel
                                visible: root.sceneTimingActive
                                x: Math.max(
                                    Theme.space4,
                                    Math.min(
                                        clipLayer.width - width - Theme.space4,
                                        (root.sceneTimingPreviewStartUs - root.viewportStartUs)
                                            / root.viewportDurationUs * clipLayer.width
                                    )
                                )
                                y: Math.max(
                                    Theme.space4,
                                    Math.min(
                                        clipLayer.height - height - Theme.space4,
                                        root.sceneTimingTrackIndex * 62
                                            - trackList.contentY - height + 8
                                    )
                                )
                                width: sceneTimingGuideText.implicitWidth + Theme.space16
                                height: sceneTimingGuideText.implicitHeight + Theme.space8
                                radius: Theme.radiusSmall
                                color: Theme.surfaceRaised
                                border.width: 1
                                border.color: root.sceneTimingSnapActive
                                    ? Theme.success : Theme.primary
                                z: 33

                                Text {
                                    id: sceneTimingGuideText
                                    anchors.centerIn: parent
                                    text: (root.sceneTimingSnapActive
                                        ? "Đã căn "
                                            + (root.sceneTimingSnapEdge === "start"
                                                ? "đầu" : "cuối") + " · "
                                        : "")
                                        + (root.sceneTimingPreviewStartUs / 1_000_000).toFixed(3)
                                        + "s → "
                                        + (root.sceneTimingPreviewEndUs / 1_000_000).toFixed(3)
                                        + "s · "
                                        + ((root.sceneTimingPreviewEndUs
                                            - root.sceneTimingPreviewStartUs) / 1_000_000).toFixed(3)
                                        + "s"
                                    color: root.sceneTimingSnapActive
                                        ? Theme.success : Theme.primary
                                    font.family: Theme.timecodeFamily
                                    font.pixelSize: 10
                                    font.weight: Font.DemiBold
                                }
                            }

                            Rectangle {
                                id: clipMoveStartGuide
                                visible: root.clipMoveActive
                                    && root.clipMoveSnapActive
                                    && root.viewportStartUs <= 0
                                x: -width / 2
                                y: 0
                                width: 3
                                height: clipLayer.height
                                color: Theme.success
                                opacity: 0.95
                                z: 34
                            }

                            Rectangle {
                                id: clipMoveGuideLabel
                                visible: root.clipMoveActive
                                x: Math.max(
                                    Theme.space4,
                                    Math.min(
                                        clipLayer.width - width - Theme.space4,
                                        (root.clipMoveOriginalStartUs
                                            + root.clipMovePreviewDeltaUs
                                            - root.viewportStartUs)
                                            / root.viewportDurationUs * clipLayer.width
                                    )
                                )
                                y: Math.max(
                                    Theme.space4,
                                    Math.min(
                                        clipLayer.height - height - Theme.space4,
                                        root.clipMoveTrackIndex * 62
                                            - trackList.contentY - height + 8
                                    )
                                )
                                width: clipMoveGuideText.implicitWidth + Theme.space16
                                height: clipMoveGuideText.implicitHeight + Theme.space8
                                radius: Theme.radiusSmall
                                color: Theme.surfaceRaised
                                border.width: root.clipMoveSnapActive ? 2 : 1
                                border.color: root.clipMoveSnapActive
                                    ? Theme.success : Theme.primary
                                z: 35

                                Text {
                                    id: clipMoveGuideText
                                    anchors.centerIn: parent
                                    text: root.clipMoveSnapActive
                                        ? "Đã căn 00:00"
                                        : "Bắt đầu "
                                            + ((root.clipMoveOriginalStartUs
                                                + root.clipMovePreviewDeltaUs)
                                                / 1_000_000).toFixed(3) + "s"
                                    color: root.clipMoveSnapActive
                                        ? Theme.success : Theme.primary
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
                        Layout.preferredHeight: 38
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
                            Rectangle {
                                id: horizontalViewport
                                objectName: "timelineOverview"
                                Layout.fillWidth: true
                                Layout.preferredHeight: 18
                                radius: 9
                                color: Theme.borderStrong
                                border.width: 1
                                border.color: Theme.borderSubtle
                                Accessible.name: "Cuộn timeline theo chiều ngang"

                                MouseArea {
                                    anchors.fill: parent
                                    acceptedButtons: Qt.LeftButton
                                    onClicked: function(mouse) {
                                        const centerUs = mouse.x / Math.max(1, width)
                                            * root.projectDurationUs
                                        root.applyViewport(
                                            centerUs - root.viewportDurationUs / 2,
                                            root.viewportDurationUs
                                        )
                                    }
                                }

                                Rectangle {
                                    id: overviewWindow
                                    objectName: "timelineOverviewWindow"
                                    x: root.viewportStartUs / root.projectDurationUs
                                        * horizontalViewport.width
                                    y: 2
                                    width: Math.max(
                                        24,
                                        root.viewportDurationUs / root.projectDurationUs
                                            * horizontalViewport.width
                                    )
                                    height: parent.height - 4
                                    radius: 7
                                    color: Theme.primary
                                    opacity: 0.78
                                    border.width: 1
                                    border.color: Theme.primaryHover

                                    MouseArea {
                                        id: overviewBodyDrag
                                        objectName: "timelineOverviewDrag"
                                        anchors.fill: parent
                                        acceptedButtons: Qt.LeftButton
                                        cursorShape: Qt.SizeHorCursor
                                        property real pressX: 0
                                        property real pressStartUs: 0
                                        onPressed: function(mouse) {
                                            pressX = mapToItem(horizontalViewport, mouse.x, 0).x
                                            pressStartUs = root.viewportStartUs
                                        }
                                        onPositionChanged: function(mouse) {
                                            if (!pressed) return
                                            root.applyViewport(
                                                pressStartUs
                                                    + (mapToItem(horizontalViewport, mouse.x, 0).x - pressX)
                                                        / Math.max(1, horizontalViewport.width)
                                                        * root.projectDurationUs,
                                                root.viewportDurationUs
                                            )
                                        }
                                    }

                                    Rectangle {
                                        anchors.left: parent.left
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: 3
                                        height: parent.height - 4
                                        radius: 2
                                        color: Theme.canvas
                                        z: 3
                                    }
                                    MouseArea {
                                        anchors.left: parent.left
                                        anchors.leftMargin: -6
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: 14
                                        height: parent.height + 8
                                        z: 4
                                        cursorShape: Qt.SizeHorCursor
                                        property real pressX: 0
                                        property real pressStartUs: 0
                                        property real fixedEndUs: 0
                                        onPressed: function(mouse) {
                                            pressX = mapToItem(horizontalViewport, mouse.x, 0).x
                                            pressStartUs = root.viewportStartUs
                                            fixedEndUs = root.viewportEndUs
                                        }
                                        onPositionChanged: function(mouse) {
                                            if (!pressed) return
                                            const currentX = mapToItem(
                                                horizontalViewport, mouse.x, 0
                                            ).x
                                            const proposed = pressStartUs
                                                + (currentX - pressX)
                                                    / Math.max(1, horizontalViewport.width)
                                                    * root.projectDurationUs
                                            const nextStart = Math.max(
                                                0,
                                                Math.min(
                                                    fixedEndUs - root.minimumViewportUs,
                                                    proposed
                                                )
                                            )
                                            root.applyViewport(nextStart, fixedEndUs - nextStart)
                                        }
                                    }

                                    Rectangle {
                                        anchors.right: parent.right
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: 3
                                        height: parent.height - 4
                                        radius: 2
                                        color: Theme.canvas
                                        z: 3
                                    }
                                    MouseArea {
                                        anchors.right: parent.right
                                        anchors.rightMargin: -6
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: 14
                                        height: parent.height + 8
                                        z: 4
                                        cursorShape: Qt.SizeHorCursor
                                        property real pressX: 0
                                        property real fixedStartUs: 0
                                        property real pressEndUs: 0
                                        onPressed: function(mouse) {
                                            pressX = mapToItem(horizontalViewport, mouse.x, 0).x
                                            fixedStartUs = root.viewportStartUs
                                            pressEndUs = root.viewportEndUs
                                        }
                                        onPositionChanged: function(mouse) {
                                            if (!pressed) return
                                            const currentX = mapToItem(
                                                horizontalViewport, mouse.x, 0
                                            ).x
                                            const proposed = pressEndUs
                                                + (currentX - pressX)
                                                    / Math.max(1, horizontalViewport.width)
                                                    * root.projectDurationUs
                                            const nextEnd = Math.min(
                                                root.projectDurationUs,
                                                Math.max(
                                                    fixedStartUs + root.minimumViewportUs,
                                                    proposed
                                                )
                                            )
                                            root.applyViewport(fixedStartUs, nextEnd - fixedStartUs)
                                        }
                                    }
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
            objectName: "timelineInspector"
            SplitView.preferredWidth: root.width * root.appModel.uiRealValue("timelineInspectorRatio", 0.27)
            SplitView.minimumWidth: 272
            SplitView.maximumWidth: 370
            clip: true
            onWidthChanged: persistWidth.restart()
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: Theme.space12
                spacing: Theme.space8
                GridLayout {
                    Layout.fillWidth: true
                    columns: 3
                    rowSpacing: Theme.space4
                    columnSpacing: Theme.space4
                    DsTabButton {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        text: "Clip"
                        tooltipText: "Thuộc tính clip đang chọn."
                        checked: root.inspectorTab === "clip"
                        onClicked: root.selectInspectorTab("clip")
                    }
                    DsTabButton {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        text: "Track"
                        tooltipText: "Khóa và âm thanh của track đang chọn."
                        checked: root.inspectorTab === "track"
                        onClicked: root.selectInspectorTab("track")
                    }
                    DsTabButton {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        text: "Phụ đề"
                        tooltipText: "Danh sách cue phụ đề của dự án."
                        checked: root.inspectorTab === "subtitles"
                        onClicked: root.selectInspectorTab("subtitles")
                    }
                    DsTabButton {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        text: "Khớp"
                        tooltipText: "Khớp thời lượng voice với timestamp SRT."
                        checked: root.inspectorTab === "voice-sync"
                        onClicked: root.selectInspectorTab("voice-sync")
                    }
                    DsTabButton {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        text: "Thiết lập"
                        tooltipText: "Thiết lập xuất, hiệu ứng và âm thanh của dự án."
                        checked: root.inspectorTab === "settings" || root.inspectorTab === "project"
                        onClicked: root.selectInspectorTab("settings")
                    }
                    DsTabButton {
                        objectName: "timelineTextInspectorTab"
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        text: "Text"
                        tooltipText: "Nội dung, chữ, nền và hiệu ứng của cue."
                        checked: root.inspectorTab === "text"
                        onClicked: root.selectInspectorTab("text")
                    }
                }
                StackLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.minimumWidth: 0
                    clip: true
                    currentIndex: root.inspectorTab === "track"
                        ? 1 : (root.inspectorTab === "subtitles"
                            ? 2 : (root.inspectorTab === "voice-sync"
                                ? 3 : (root.inspectorTab === "settings" || root.inspectorTab === "project"
                                    ? 4 : (root.inspectorTab === "text" ? 5 : 0))))
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
                                tooltipText: "Áp dụng phần trăm tốc độ đã nhập cho clip đang chọn."
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
                                tooltipText: "Áp dụng mức âm lượng đã nhập cho riêng clip đang chọn."
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
                                visible: root.appModel.currentProject.track(root.appModel.selectedTrackId).kind !== "text"
                                text: root.appModel.selectedTrackMuted
                                    ? "Track đang tắt tiếng" : "Track đang phát âm thanh"
                                color: Theme.textSecondary
                                font.family: Theme.fontFamily
                                font.pixelSize: 11
                            }
                            DsButton {
                                Layout.fillWidth: true
                                visible: root.appModel.currentProject.track(root.appModel.selectedTrackId).kind !== "text"
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
                                    color: root.appModel.isCueSelected(
                                        timelineCueRow.model.cueId
                                    )
                                        ? Theme.selectionFill
                                        : (cueHover.hovered ? Theme.surfaceHover : Theme.surfaceRaised)
                                    border.width: root.appModel.isCueSelected(
                                        timelineCueRow.model.cueId
                                    )
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
                                            text: timelineCueRow.model.subtitleTrackId
                                                + (timelineCueRow.model.cueRole === "scene_text"
                                                    ? " · Chữ trong cảnh" : " · Hội thoại")
                                                + "  #" + timelineCueRow.model.cueIndex
                                                + "  " + timelineCueRow.model.startText
                                            color: timelineCueRow.model.cueRole === "scene_text"
                                                ? Theme.info : Theme.textMuted
                                            font.family: Theme.timecodeFamily
                                            font.pixelSize: 9
                                        }
                                        Text {
                                            Layout.fillWidth: true
                                            visible: timelineCueRow.model.cueRole === "scene_text"
                                            text: (timelineCueRow.model.orientation === "vertical"
                                                ? "Dọc" : "Ngang")
                                                + " · "
                                                + (timelineCueRow.model.styleHint === "calligraphy"
                                                    ? "Thư pháp" : "Chữ hiển thị")
                                                + (timelineCueRow.model.reviewState === "review"
                                                    ? " · Cần duyệt" : "")
                                            color: timelineCueRow.model.reviewState === "review"
                                                ? Theme.warning : Theme.textMuted
                                            font.family: Theme.fontFamily
                                            font.pixelSize: 9
                                            elide: Text.ElideRight
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
                    ExportSettingsPanel { appModel: root.appModel }
                    SubtitleTextToolbar {
                        id: subtitleTextToolbar
                        appModel: root.appModel
                        inspectorMode: true
                        allowManualPlacement: true
                        onManualPlacementRequested: timelinePreview.beginManualSceneText()
                        onColorPickRequested: function(target) {
                            timelinePreview.colorPickerTarget = target
                        }
                    }
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
