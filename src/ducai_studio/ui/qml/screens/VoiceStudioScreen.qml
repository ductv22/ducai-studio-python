pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import QtMultimedia
import DucaiStudio 1.0
import "../controls"

Item {
    id: root
    objectName: "voiceStudioScreen"

    property var appModel
    property string inspectorTab: appModel.uiStringValue("voiceInspectorTab", "voice")
    property string referenceAudioError: ""
    property string referenceVoiceId: ""
    property string castingAudioError: ""
    property var selectedCastingCueIds: []
    property var castingPlaybackParts: []
    property int castingPlaybackIndex: -1
    property var castingCurrentPart: null
    property bool castingPendingStart: false
    property int castingPlaybackLoadGeneration: 0
    property int voicePreviewReplayGeneration: 0
    property bool voicePreviewPlaybackRequested: false
    property bool castingViewportRestorePending: false
    property string castingViewportCueId: ""
    property int castingViewportIndex: -1
    property real castingViewportOffset: 0
    onVisibleChanged: {
        if (!visible) {
            root.voicePreviewPlaybackRequested = false
            root.voicePreviewReplayGeneration += 1
            previewPlayer.stop()
            previewPlayer.source = ""
            referencePlayer.stop()
            castingPlayer.stop()
            castingPlaybackParts = []
            castingPlaybackIndex = -1
            castingCurrentPart = null
            castingPendingStart = false
            castingPlaybackLoadGeneration += 1
        } else if (appModel.currentProject.hasProject) {
            appModel.refreshDialogueCasting()
        }
    }
    activeFocusOnTab: true
    Accessible.name: "Phòng thu giọng"

    FileDialog {
        id: referenceDialog
        title: "Chọn file mẫu giọng VieNeu"
        fileMode: FileDialog.OpenFile
        nameFilters: ["Âm thanh (*.wav *.mp3 *.m4a *.flac)", "Tất cả file (*)"]
        onAccepted: root.appModel.setVieNeuReferenceAudio(selectedFile.toString())
    }

    FileDialog {
        id: castingReferenceDialog
        property string role: ""
        property string projectId: ""
        title: "Chọn file giọng riêng cho phân vai"
        fileMode: FileDialog.OpenFile
        nameFilters: [
            "Âm thanh (*.wav *.mp3 *.m4a *.flac *.ogg)",
            "Tất cả file (*)"
        ]
        onAccepted: {
            const currentProjectId = String(root.appModel.currentProject.projectId || "")
            if (!root.appModel.currentProject.hasProject
                    || !projectId || projectId !== currentProjectId
                    || ["male", "female", "fallback"].indexOf(role) < 0)
                return
            root.appModel.setDialogueCastingReferenceAudio(
                role, selectedFile.toString()
            )
        }
    }

    function selectInspectorTab(value) {
        inspectorTab = value
        appModel.setUiStringValue("voiceInspectorTab", value)
    }

    function selectedValue(key, fallback) {
        const value = appModel.selectedVoice[key]
        return value === undefined || value === null || value === "" ? fallback : value
    }

    function castingConfig() {
        return (root.appModel.dialogueCasting || {}).config || {}
    }

    function castingVoiceId(role) {
        const voice = castingConfig()[role + "_voice"] || {}
        return String(voice.voice_id || "")
    }

    function castingReferencePath(role) {
        const voice = castingConfig()[role + "_voice"] || {}
        return String(voice.reference_audio_path || "")
    }

    function castingReferenceName(role) {
        const path = root.castingReferencePath(role).replace(/\\/g, "/")
        if (!path) return "Chưa chọn file riêng"
        const parts = path.split("/")
        return parts.length > 0 ? parts[parts.length - 1] : path
    }

    function openCastingReferenceDialog(role) {
        if (!root.appModel.currentProject.hasProject) return
        castingReferenceDialog.role = role
        castingReferenceDialog.projectId = String(
            root.appModel.currentProject.projectId || ""
        )
        castingReferenceDialog.open()
    }

    function castingVoiceIndex(role) {
        const wanted = root.castingVoiceId(role)
        if (!wanted) return -1
        const model = root.appModel.voiceCatalogModel
        for (let index = 0; index < model.count; index++) {
            if (String(model.get(index).voice_id || "") === wanted) return index
        }
        return -1
    }

    function filteredCastingRows() {
        const rows = (root.appModel.dialogueCasting || {}).rows || []
        if (castingFilter.currentIndex === 1)
            return rows.filter(row => row.label === "unknown")
        if (castingFilter.currentIndex === 2)
            return rows.filter(row => row.label === "male")
        if (castingFilter.currentIndex === 3)
            return rows.filter(row => row.label === "female")
        return rows
    }

    function toggleCastingCue(cueId, checked) {
        const next = root.selectedCastingCueIds.slice()
        const index = next.indexOf(cueId)
        if (checked && index < 0) next.push(cueId)
        else if (!checked && index >= 0) next.splice(index, 1)
        root.selectedCastingCueIds = next
    }

    function applyCastingLabel(cueIds, label) {
        root.captureCastingViewport()
        root.appModel.setDialogueCastingLabels(cueIds, label)
        root.selectedCastingCueIds = []
    }

    function captureCastingViewport() {
        const count = dialogueCastingRows.count
        if (count <= 0) {
            root.castingViewportRestorePending = false
            return
        }
        let index = dialogueCastingRows.indexAt(
            Math.max(1, dialogueCastingRows.width / 2),
            Math.max(0, dialogueCastingRows.contentY) + 1
        )
        if (index < 0) {
            const pitch = 126 + dialogueCastingRows.spacing
            index = Math.max(0, Math.min(count - 1,
                Math.floor(Math.max(0, dialogueCastingRows.contentY) / pitch)))
        }
        const rows = dialogueCastingRows.model
        const row = rows && index < rows.length ? rows[index] : null
        const item = dialogueCastingRows.itemAtIndex(index)
        const anchorY = item ? item.y : index * (126 + dialogueCastingRows.spacing)
        root.castingViewportCueId = String(row && row.id || "")
        root.castingViewportIndex = index
        root.castingViewportOffset = dialogueCastingRows.contentY - anchorY
        root.castingViewportRestorePending = true
    }

    function restoreCastingViewport(finalRestore) {
        if (!root.castingViewportRestorePending) return
        const rows = dialogueCastingRows.model
        const count = dialogueCastingRows.count
        if (!rows || count <= 0) {
            if (finalRestore) root.castingViewportRestorePending = false
            return
        }
        let index = -1
        for (let candidate = 0; candidate < count; candidate++) {
            if (String(rows[candidate].id || "") === root.castingViewportCueId) {
                index = candidate
                break
            }
        }
        if (index < 0)
            index = Math.max(0, Math.min(count - 1, root.castingViewportIndex))
        const restoreIndex = index
        dialogueCastingRows.positionViewAtIndex(restoreIndex, ListView.Beginning)
        Qt.callLater(function() {
            if (!root.castingViewportRestorePending) return
            const item = dialogueCastingRows.itemAtIndex(restoreIndex)
            const anchorY = item
                ? item.y : restoreIndex * (126 + dialogueCastingRows.spacing)
            const maximumY = Math.max(
                0, dialogueCastingRows.contentHeight - dialogueCastingRows.height
            )
            dialogueCastingRows.contentY = Math.max(
                0,
                Math.min(maximumY, anchorY + root.castingViewportOffset)
            )
            if (finalRestore && !root.appModel.dialogueCastingSaving)
                root.castingViewportRestorePending = false
        })
    }

    function pruneCastingSelection() {
        const rows = (root.appModel.dialogueCasting || {}).rows || []
        const available = {}
        for (let index = 0; index < rows.length; index++)
            available[String(rows[index].id || "")] = true
        const next = root.selectedCastingCueIds.filter(
            cueId => available[String(cueId)] === true
        )
        if (next.length !== root.selectedCastingCueIds.length)
            root.selectedCastingCueIds = next
    }

    function castingLabelText(label) {
        if (label === "male") return "Nam"
        if (label === "female") return "Nữ"
        return "Cần kiểm tra"
    }

    function castingLabelDetail(row) {
        const source = row && row.manual ? "sửa tay" : "tự động"
        return root.castingLabelText(row ? row.label : "unknown") + " · " + source
    }

    function castingReasonText(reason) {
        const messages = {
            "assigned": "Máy đã nhận diện",
            "needs_analysis": "Chưa chạy nhận diện",
            "no_speech": "Không phát hiện lời thoại",
            "overlapping_speakers": "Có người nói chồng tiếng",
            "overlapping_subtitles": "Các câu phụ đề bị chồng thời gian",
            "multiple_speakers": "Có nhiều người nói trong câu",
            "insufficient_speech": "Đoạn thoại quá ngắn hoặc quá ít tiếng nói",
            "insufficient_speaker_reference": "Chưa đủ mẫu giọng của người nói",
            "contradictory_speaker_evidence": "Bằng chứng giọng nói mâu thuẫn",
            "low_confidence": "Độ tin cậy thấp",
            "prior_review_conflict": "Mâu thuẫn với nhãn đã kiểm tra",
            "discontinuous_or_long_source": "Nguồn âm thanh bị ngắt quãng hoặc quá dài"
        }
        return messages[String(reason || "")] || "Tự nhận diện"
    }

    function playCastingSource(row) {
        const parts = row && row.parts ? row.parts : []
        if (!parts || Number(parts.length || 0) <= 0) {
            root.castingAudioError = "Câu này không còn đoạn âm thanh nguồn để phát."
            return
        }
        previewPlayer.pause()
        productionPlayer.pause()
        referencePlayer.pause()
        root.castingAudioError = ""
        root.castingPlaybackLoadGeneration += 1
        root.castingPlaybackParts = parts
        root.castingPlaybackIndex = 0
        root.playCastingPart()
    }

    function playCastingPart() {
        if (root.castingPlaybackIndex < 0
                || root.castingPlaybackIndex >= root.castingPlaybackParts.length) {
            castingPlayer.stop()
            castingPlayer.source = ""
            root.castingPlaybackParts = []
            root.castingPlaybackIndex = -1
            root.castingCurrentPart = null
            root.castingPendingStart = false
            root.castingPlaybackLoadGeneration += 1
            return
        }
        const part = root.castingPlaybackParts[root.castingPlaybackIndex]
        if (!part || !part.source_url) {
            castingPlayer.stop()
            castingPlayer.source = ""
            root.castingPlaybackParts = []
            root.castingPlaybackIndex = -1
            root.castingCurrentPart = null
            root.castingPendingStart = false
            root.castingAudioError = "Không tìm thấy file âm thanh nguồn của câu này."
            return
        }
        root.castingCurrentPart = part
        root.castingAudioError = ""
        const sourceUrl = String(part.source_url)
        const sourceReady = castingPlayer.mediaStatus === MediaPlayer.LoadedMedia
            || castingPlayer.mediaStatus === MediaPlayer.BufferedMedia
        if (String(castingPlayer.source) === sourceUrl && sourceReady) {
            root.castingPendingStart = false
            castingPlayer.stop()
            castingPlayer.setPosition(Math.floor(Number(part.start_us || 0) / 1000))
            castingPlayer.play()
        } else {
            const generation = root.castingPlaybackLoadGeneration + 1
            root.castingPlaybackLoadGeneration = generation
            root.castingPendingStart = true
            castingPlayer.stop()
            castingPlayer.source = ""
            Qt.callLater(function() {
                if (generation !== root.castingPlaybackLoadGeneration
                        || !root.castingCurrentPart
                        || String(root.castingCurrentPart.source_url || "") !== sourceUrl)
                    return
                castingPlayer.source = sourceUrl
            })
        }
    }

    function advanceCastingPart() {
        if (root.castingPlaybackIndex < 0) return
        root.castingPlaybackIndex += 1
        root.playCastingPart()
    }

    function replayVoicePreviewSource() {
        const nextSource = String(root.appModel.voicePreviewSource || "")
        const generation = root.voicePreviewReplayGeneration + 1
        root.voicePreviewReplayGeneration = generation
        previewPlayer.source = ""
        previewPlayer.stop()
        root.voicePreviewPlaybackRequested = Boolean(nextSource) && root.visible
        if (!root.voicePreviewPlaybackRequested) return
        Qt.callLater(function() {
            if (generation !== root.voicePreviewReplayGeneration
                    || !root.voicePreviewPlaybackRequested
                    || root.appModel.voiceCatalogPreviewSource
                    || String(root.appModel.voicePreviewSource || "") !== nextSource)
                return
            previewPlayer.source = nextSource
        })
    }

    function armVoicePreviewPlayback() {
        root.voicePreviewPlaybackRequested = root.visible
    }

    function replayVoiceCatalogSource() {
        const nextSource = String(root.appModel.voiceCatalogPreviewSource || "")
        const generation = root.voicePreviewReplayGeneration + 1
        root.voicePreviewReplayGeneration = generation
        previewPlayer.stop()
        previewPlayer.source = ""
        root.voicePreviewPlaybackRequested = Boolean(nextSource) && root.visible
        if (!root.voicePreviewPlaybackRequested) return
        Qt.callLater(function() {
            if (generation !== root.voicePreviewReplayGeneration
                    || !root.voicePreviewPlaybackRequested
                    || String(root.appModel.voiceCatalogPreviewSource || "") !== nextSource)
                return
            previewPlayer.source = nextSource
        })
    }

    function timeLabel(milliseconds) {
        const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000))
        const hours = Math.floor(totalSeconds / 3600)
        const minutes = Math.floor((totalSeconds % 3600) / 60)
        const seconds = totalSeconds % 60
        return (hours > 0 ? String(hours).padStart(2, "0") + ":" : "")
            + String(minutes).padStart(2, "0") + ":"
            + String(seconds).padStart(2, "0")
    }

    function conformEstimateValue(key, fallback) {
        const estimate = appModel.voiceConformEstimate || {}
        const value = estimate[key]
        return value === undefined || value === null ? fallback : value
    }

    function hasConformEstimate() {
        const status = String(conformEstimateValue("status", "")).toUpperCase()
        return status.length > 0 && status !== "NONE" && status !== "IDLE"
    }

    function conformMaxAudioSpeed() {
        const value = Number(appModel.voiceConformMaxAudioSpeed)
        return isFinite(value) && value >= 1.00 && value <= 2.00 ? value : 1.25
    }

    function conformModeDescription() {
        if (appModel.voiceConformMode === "stretch_video")
            return "Giãn video, giữ giọng ở 1,00×; giới hạn tốc độ giọng không được dùng."
        if (appModel.voiceConformMode === "speed_audio")
            return "Tăng tốc giọng trong giới hạn, giữ video ở 1,00×."
        return "Tăng tốc giọng trong giới hạn, rồi giãn video cho phần còn lại."
    }

    function durationUsLabel(microseconds) {
        const value = Number(microseconds)
        if (!isFinite(value))
            return "Chưa có"
        return timeLabel(Math.round(value / 1000))
    }

    function speedRangeLabel(minimum, maximum) {
        const low = Number(minimum)
        const high = Number(maximum)
        if (!isFinite(low) || !isFinite(high))
            return "Chưa có"
        if (Math.abs(low - high) < 0.001)
            return low.toFixed(2) + "×"
        return low.toFixed(2) + "×–" + high.toFixed(2) + "×"
    }

    function conformStatusText() {
        if (String(conformEstimateValue("status", "")).toUpperCase() === "PENDING")
            return "Đang ước lượng"
        const unresolved = Number(conformEstimateValue("unresolved_count", 0))
        if (appModel.voiceConformBusy)
            return appModel.voiceConformJobState === "queued" ? "Đang chờ" : "Đang xử lý"
        if (appModel.voiceConformApplied)
            return "Đã áp dụng"
        if (unresolved > 0)
            return "Còn " + unresolved + " cue"
        if (appModel.voiceConformCanApply)
            return "Sẵn sàng"
        return hasConformEstimate() ? "Chưa đạt QA" : "Chưa ước lượng"
    }

    function conformRuntimeState() {
        if (String(conformEstimateValue("status", "")).toUpperCase() === "PENDING")
            return "starting"
        const unresolved = Number(conformEstimateValue("unresolved_count", 0))
        if (appModel.voiceConformBusy)
            return "starting"
        if (unresolved > 0)
            return "error"
        if (appModel.voiceConformApplied || appModel.voiceConformCanApply)
            return "ready"
        return "starting"
    }

    function firstConformIssueText() {
        const issues = conformEstimateValue("issues", []) || []
        if (issues.length === 0)
            return "Còn cue chưa khớp nhưng chưa có chi tiết điều hướng."
        const issue = issues[0] || {}
        const cueLabel = issue.id === undefined || issue.id === null
            ? "Cue dài" : "Cue " + issue.id
        const detail = String(issue.message || issue.detail || "Vượt giới hạn đồng bộ an toàn.")
        return cueLabel + ": " + detail
    }

    function hasConformIssueTarget() {
        const issues = conformEstimateValue("issues", []) || []
        return issues.length > 0 && isFinite(Number((issues[0] || {}).id))
    }

    function previewOperationState() {
        if (root.appModel.ttsJobPurpose === "preview")
            return root.appModel.ttsJobState || "idle"
        return root.appModel.voicePreviewSource ? "succeeded" : "idle"
    }

    function previewButtonEnabled() {
        if (!Boolean(root.appModel.selectedVoiceId)
                || !root.appModel.currentProject.hasProject
                || root.appModel.voiceTimingAutoRepairBusy
                || root.appModel.voiceConformBusy)
            return false
        if (root.appModel.ttsBusy
                && root.appModel.ttsJobPurpose !== "preview")
            return false
        if (root.appModel.ttsJobPurpose !== "preview")
            return !root.appModel.ttsBusy
        return [
            "idle", "running", "paused", "queued", "interrupted",
            "failed", "review", "cancelled", "succeeded", ""
        ].indexOf(root.appModel.ttsJobState) >= 0
    }

    function previewAction() {
        if (!previewButtonEnabled())
            return
        if (root.appModel.ttsJobPurpose === "preview") {
            if (root.appModel.ttsJobState === "running") {
                root.appModel.pauseVoiceGeneration()
                return
            }
            if (["paused", "queued", "interrupted", "failed", "review"]
                    .indexOf(root.appModel.ttsJobState) >= 0) {
                root.appModel.resumeVoiceGeneration()
                return
            }
        }
        root.appModel.startVoicePreview()
    }

    function productionOperationState() {
        if (root.appModel.voiceTimingAutoRepairBusy)
            return "start_requested"
        if (root.appModel.ttsJobPurpose === "production")
            return root.appModel.ttsJobState || "idle"
        return root.appModel.currentProject.hasVoiceTrack ? "succeeded" : "idle"
    }

    function productionButtonEnabled() {
        if (!Boolean(root.appModel.selectedVoiceId)
                || !root.appModel.currentProject.hasProject
                || root.appModel.voiceTimingAutoRepairBusy
                || root.appModel.voiceConformBusy)
            return false
        if (root.appModel.ttsBusy
                && root.appModel.ttsJobPurpose !== "production")
            return false
        if (root.appModel.ttsJobPurpose !== "production")
            return !root.appModel.ttsBusy
        return [
            "idle", "running", "paused", "queued", "interrupted",
            "failed", "review", "cancelled", "succeeded", ""
        ].indexOf(root.appModel.ttsJobState) >= 0
    }

    function productionVoiceAction() {
        if (!productionButtonEnabled())
            return
        if (root.appModel.ttsJobPurpose === "production") {
            if (root.appModel.ttsJobState === "running") {
                root.appModel.pauseVoiceGeneration()
                return
            }
            if (["paused", "queued", "interrupted", "failed", "review"]
                    .indexOf(root.appModel.ttsJobState) >= 0) {
                root.appModel.resumeVoiceGeneration()
                return
            }
        }
        root.appModel.startVoiceGeneration()
    }

    MediaPlayer {
        id: previewPlayer
        objectName: "voicePreviewPlayer"
        source: ""
        audioOutput: previewAudio
        onPlaybackStateChanged: {
            if (playbackState === MediaPlayer.PlayingState) {
                referencePlayer.pause()
                productionPlayer.pause()
                castingPlayer.pause()
            }
        }
        onMediaStatusChanged: {
            if ((mediaStatus === MediaPlayer.LoadedMedia
                    || mediaStatus === MediaPlayer.BufferedMedia)
                    && source && root.voicePreviewPlaybackRequested) {
                root.voicePreviewPlaybackRequested = false
                productionPlayer.pause()
                play()
            } else if (mediaStatus === MediaPlayer.EndOfMedia
                    && root.appModel.voiceCatalogPreviewId) {
                root.appModel.stopVoiceCatalogPreview()
            }
        }
    }

    AudioOutput {
        id: previewAudio
        volume: 0.9
    }

    MediaPlayer {
        id: productionPlayer
        objectName: "voiceProductionPlayer"
        source: root.appModel.voicePlaybackEnabled
            ? root.appModel.currentProject.voiceSource : ""
        audioOutput: productionAudio
        onPlaybackStateChanged: {
            if (playbackState === MediaPlayer.PlayingState) {
                referencePlayer.pause()
                previewPlayer.pause()
                castingPlayer.pause()
            }
        }
    }

    MediaPlayer {
        id: castingPlayer
        objectName: "dialogueCastingSourcePlayer"
        audioOutput: castingAudio
        onPlaybackStateChanged: {
            if (playbackState === MediaPlayer.PlayingState) {
                referencePlayer.pause()
                previewPlayer.pause()
                productionPlayer.pause()
            }
        }
        onMediaStatusChanged: {
            if (root.castingPendingStart && root.castingCurrentPart
                    && (mediaStatus === MediaPlayer.LoadedMedia
                        || mediaStatus === MediaPlayer.BufferedMedia)) {
                root.castingPendingStart = false
                setPosition(Math.floor(Number(root.castingCurrentPart.start_us || 0) / 1000))
                play()
            } else if (mediaStatus === MediaPlayer.EndOfMedia) {
                root.advanceCastingPart()
            }
        }
        onErrorOccurred: function(_error, errorString) {
            root.castingPendingStart = false
            root.castingAudioError = "Không thể phát âm thanh nguồn: " + errorString
            stop()
        }
    }

    AudioOutput {
        id: castingAudio
        volume: 0.9
    }

    Timer {
        id: castingPlaybackEndTimer
        interval: 60
        repeat: true
        running: castingPlayer.playbackState === MediaPlayer.PlayingState
        onTriggered: {
            const part = root.castingCurrentPart
            if (part && castingPlayer.position >= Math.floor(Number(part.end_us || 0) / 1000))
                root.advanceCastingPart()
        }
    }

    AudioOutput {
        id: productionAudio
        volume: 0.9
    }

    MediaPlayer {
        id: referencePlayer
        objectName: "cloneReferencePlayer"
        source: root.appModel.vieneuReferenceSource
        audioOutput: referenceAudio
        onSourceChanged: {
            stop()
            root.referenceAudioError = ""
        }
        onPlaybackStateChanged: {
            if (playbackState === MediaPlayer.PlayingState) {
                previewPlayer.pause()
                productionPlayer.pause()
                castingPlayer.pause()
            }
        }
        onErrorOccurred: (error, errorString) => {
            root.referenceAudioError = "Không thể phát file mẫu: " + errorString
            stop()
        }
    }

    AudioOutput {
        id: referenceAudio
        volume: 0.9
    }

    Connections {
        target: root.appModel
        function onDialogueCastingChanged() {
            root.pruneCastingSelection()
            if (root.castingViewportRestorePending)
                root.restoreCastingViewport(!root.appModel.dialogueCastingSaving)
        }
        function onVoiceSettingsChanged() {
            if (root.referenceVoiceId !== root.appModel.selectedVoiceId) {
                root.referenceVoiceId = root.appModel.selectedVoiceId
                if (!Boolean(root.selectedValue("is_clone", false))) referencePlayer.stop()
            }
        }
        function onVoicePreviewChanged() {
            if (root.appModel.voiceCatalogPreviewSource)
                root.replayVoiceCatalogSource()
            else
                root.replayVoicePreviewSource()
        }
        function onVoiceCatalogPlaybackChanged() {
            root.replayVoiceCatalogSource()
        }
        function onVoicePlaybackEnabledChanged() {
            if (root.appModel.voicePlaybackEnabled) return
            productionPlayer.stop()
        }
    }

    Timer {
        id: persistWidth
        interval: 250
        onTriggered: root.appModel.setUiRealValue(
            "voiceInspectorRatio", inspector.width / Math.max(1, root.width)
        )
    }

    // Dragging the conform speed must not enqueue one expensive estimate per
    // pointer event.  Commit the final value after a short quiet period.
    Timer {
        id: conformEstimateDebounce
        interval: 220
        repeat: false
        onTriggered: root.appModel.setVoiceConformMaxAudioSpeed(
            Number(conformAudioSpeed.value.toFixed(2))
        )
    }

    Timer {
        id: readSpeedDebounce
        interval: 180
        repeat: false
        onTriggered: root.appModel.setVoiceReadSpeed(
            Number(voiceReadSpeedSlider.value.toFixed(1))
        )
    }

    Timer {
        id: cueSpeedDebounce
        interval: 180
        repeat: false
        onTriggered: root.appModel.setVoiceCueMaxSpeed(
            Number(voiceCueSpeedSlider.value.toFixed(1))
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

        ColumnLayout {
            SplitView.fillWidth: true
            SplitView.minimumWidth: 610
            spacing: Theme.space8

            Panel {
                Layout.fillWidth: true
                Layout.preferredHeight: Math.max(230, Math.min(330, root.height * 0.42))
                color: Theme.canvas
                TimelinePreview {
                    anchors.fill: parent
                    appModel: root.appModel
                }
            }

            Panel {
                Layout.fillWidth: true
                Layout.fillHeight: true

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: Theme.space12
                    spacing: Theme.space8

                    RowLayout {
                        Layout.fillWidth: true
                        SectionTitle { text: "Timeline giọng nói" }
                        Item { Layout.fillWidth: true }
                        StatusPill {
                            text: root.appModel.ttsBusy
                                ? "Đang chạy"
                                : (root.appModel.ttsJobState === "review" ? "Cần xem lại" : "Sẵn sàng")
                            runtimeState: root.appModel.ttsJobState === "review"
                                ? "error" : (root.appModel.ttsBusy ? "starting" : "ready")
                        }
                    }

                    Text {
                        Layout.fillWidth: true
                        text: root.appModel.currentProject.hasProject
                            ? (root.appModel.currentProject.hasVoiceTrack
                                ? "Tạo lại sẽ tự gỡ D1 cũ và nhả WAV trước khi chạy; file cũ và cache từng cue vẫn được giữ."
                                : "WAV đạt QA sẽ tự vào track D1. Cache từng cue được giữ lại khi tạm dừng hoặc mở lại tool.")
                            : "Hãy nhập video, SRT Trung và bản dịch Việt trước khi tạo giọng."
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.space8

                        DsButton {
                            text: previewPlayer.playbackState === MediaPlayer.PlayingState
                                ? "Tạm dừng preview mẫu" : "Nghe preview mẫu"
                            enabled: Boolean(root.appModel.voicePreviewSource)
                            disabledReason: "Hãy bấm Tạo preview trước."
                            onClicked: {
                                if (previewPlayer.playbackState === MediaPlayer.PlayingState)
                                    previewPlayer.pause()
                                else {
                                    productionPlayer.pause()
                                    previewPlayer.play()
                                }
                            }
                        }
                        Slider {
                            Layout.fillWidth: true
                            from: 0
                            to: Math.max(1, previewPlayer.duration)
                            value: previewPlayer.position
                            enabled: Boolean(root.appModel.voicePreviewSource)
                            onMoved: previewPlayer.setPosition(value)
                            Accessible.name: "Vị trí nghe thử giọng"
                        }
                        Text {
                            text: root.timeLabel(previewPlayer.position)
                            color: Theme.textMuted
                            font.family: Theme.timecodeFamily
                            font.pixelSize: 10
                        }
                    }


                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.space8

                        DsButton {
                            text: productionPlayer.playbackState === MediaPlayer.PlayingState
                                ? "Tạm dừng voice thành phẩm" : "Nghe voice thành phẩm"
                            enabled: root.appModel.currentProject.hasVoiceTrack
                            disabledReason: "Hãy tạo toàn bộ voice và chờ WAV đạt QA vào track D1."
                            onClicked: {
                                if (productionPlayer.playbackState === MediaPlayer.PlayingState)
                                    productionPlayer.pause()
                                else {
                                    previewPlayer.pause()
                                    productionPlayer.play()
                                }
                            }
                        }
                        Slider {
                            Layout.fillWidth: true
                            from: 0
                            to: Math.max(1, productionPlayer.duration)
                            value: productionPlayer.position
                            enabled: root.appModel.currentProject.hasVoiceTrack
                            onMoved: productionPlayer.setPosition(value)
                            Accessible.name: "Vị trí nghe voice thành phẩm"
                        }
                        Text {
                            text: root.timeLabel(productionPlayer.position)
                                + " / " + root.timeLabel(productionPlayer.duration)
                            color: Theme.textMuted
                            font.family: Theme.timecodeFamily
                            font.pixelSize: 10
                        }
                    }

                    JobActivityPanel {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        appModel: root.appModel
                        allowHistoryDeletion: true
                        historyScope: "tts"
                        jobModel: root.appModel.voiceJobModel
                        title: "Tác vụ tạo giọng"
                        emptyDescription: "Chưa có tác vụ. Preview và production đều chạy ngoài GUI."
                    }
                }
            }
        }

        Panel {
            id: inspector
            SplitView.preferredWidth: root.width * root.appModel.uiRealValue(
                "voiceInspectorRatio", 0.31
            )
            SplitView.minimumWidth: 310
            SplitView.maximumWidth: 430
            onWidthChanged: persistWidth.restart()

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: Theme.space12
                spacing: Theme.space8

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.space4
                    DsTabButton {
                        text: "Giọng"
                        checked: root.inspectorTab === "voice"
                        onClicked: root.selectInspectorTab("voice")
                    }
                    DsTabButton {
                        text: "Mixer"
                        checked: root.inspectorTab === "mixer"
                        onClicked: root.selectInspectorTab("mixer")
                    }
                    DsTabButton {
                        text: "Lịch sử"
                        checked: root.inspectorTab === "history"
                        onClicked: root.selectInspectorTab("history")
                    }
                    DsTabButton {
                        text: "Phân vai"
                        checked: root.inspectorTab === "casting"
                        onClicked: root.selectInspectorTab("casting")
                    }
                }

                StackLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    currentIndex: root.inspectorTab === "mixer"
                        ? 1 : (root.inspectorTab === "history" ? 2
                            : (root.inspectorTab === "casting" ? 3 : 0))

                    ColumnLayout {
                        spacing: Theme.space8

                        RowLayout {
                            Layout.fillWidth: true
                            SectionTitle { text: "Thư viện giọng" }
                            Item { Layout.fillWidth: true }
                            DsButton {
                                text: "Làm mới"
                                compact: true
                                onClicked: root.appModel.refreshVoiceCatalog()
                            }
                        }

                        ListView {
                            id: voiceList
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            spacing: Theme.space4
                            model: root.appModel.voiceCatalogModel
                            currentIndex: -1
                            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                            delegate: Rectangle {
                                id: voiceDelegate
                                required property string voiceId
                                required property string providerId
                                required property string voiceName
                                required property string voiceSource
                                required property string voiceDescription
                                required property string rightsLabel
                                required property bool installed
                                required property string previewSource
                                required property string previewText
                                required property bool previewAvailable
                                required property bool installable
                                required property string installState
                                required property int installProgress
                                required property string installError
                                required property bool isClone

                                width: ListView.view.width
                                height: 112
                                radius: Theme.radiusSmall
                                color: voiceId === root.appModel.selectedVoiceId
                                    ? Theme.selectionFill
                                    : (voiceMouse.containsMouse ? Theme.surfaceHover : Theme.surfaceRaised)
                                border.width: voiceId === root.appModel.selectedVoiceId ? 1 : 0
                                border.color: Theme.primary
                                MouseArea {
                                    id: voiceMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    enabled: voiceDelegate.installed
                                    onClicked: root.appModel.selectVoice(voiceDelegate.voiceId)
                                }

                                ColumnLayout {
                                    anchors.fill: parent
                                    anchors.margins: Theme.space8
                                    spacing: 3
                                    Text {
                                        Layout.fillWidth: true
                                        text: voiceDelegate.voiceName + "  •  " + voiceDelegate.voiceSource
                                        color: Theme.textPrimary
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 12
                                        font.weight: Font.DemiBold
                                        elide: Text.ElideRight
                                    }
                                    Text {
                                        Layout.fillWidth: true
                                        text: voiceDelegate.voiceDescription
                                        color: Theme.textSecondary
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 10
                                        elide: Text.ElideRight
                                    }
                                    Text {
                                        Layout.fillWidth: true
                                        text: (voiceDelegate.installed ? "Đã cài" : "Chưa cài")
                                            + " • " + voiceDelegate.rightsLabel
                                        color: voiceDelegate.installed ? Theme.success : Theme.warning
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 9
                                        elide: Text.ElideRight
                                    }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: Theme.space4

                                        DsButton {
                                            text: root.appModel.voiceCatalogPreviewId === voiceDelegate.voiceId
                                                ? "Dừng" : "Nghe thử"
                                            compact: true
                                            enabled: voiceDelegate.previewAvailable
                                            disabledReason: voiceDelegate.isClone
                                                ? "Chọn file mẫu clone để nghe giọng của bạn."
                                                : "Chưa có mẫu nghe được xác minh cho giọng này."
                                            feedbackEnabled: false
                                            onClicked: root.appModel.previewCatalogVoice(voiceDelegate.voiceId)
                                            ToolTip.visible: hovered && Boolean(voiceDelegate.previewText)
                                            ToolTip.text: voiceDelegate.previewText
                                            ToolTip.delay: 500
                                        }

                                        DsButton {
                                            visible: !voiceDelegate.installed && voiceDelegate.installable
                                            text: voiceDelegate.installState === "error" ? "Thử lại" : "Cài"
                                            compact: true
                                            primary: true
                                            enabled: voiceDelegate.installState !== "downloading"
                                            operationState: voiceDelegate.installState === "downloading"
                                                ? "running"
                                                : (voiceDelegate.installState === "error" ? "failed" : "idle")
                                            runningText: "Đang tải " + voiceDelegate.installProgress + "%"
                                            failedText: "Thử lại"
                                            feedbackEnabled: false
                                            onClicked: root.appModel.installVoice(voiceDelegate.voiceId)
                                            ToolTip.visible: hovered && Boolean(voiceDelegate.installError)
                                            ToolTip.text: voiceDelegate.installError
                                            ToolTip.delay: 350
                                        }

                                        Text {
                                            visible: voiceDelegate.installed
                                            text: voiceDelegate.voiceId === root.appModel.selectedVoiceId
                                                ? "Đang dùng" : "Sẵn sàng"
                                            color: voiceDelegate.voiceId === root.appModel.selectedVoiceId
                                                ? Theme.primary : Theme.success
                                            font.family: Theme.fontFamily
                                            font.pixelSize: 10
                                            font.weight: Font.DemiBold
                                        }

                                        Item { Layout.fillWidth: true }

                                        Text {
                                            visible: !voiceDelegate.installed && !voiceDelegate.installable
                                            text: "Chưa có gói cài hợp lệ"
                                            color: Theme.textMuted
                                            font.family: Theme.fontFamily
                                            font.pixelSize: 9
                                        }
                                    }
                                }
                            }
                        }

                        Text {
                            Layout.fillWidth: true
                            text: root.selectedValue("summary", "Chưa chọn giọng")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: 10
                            wrapMode: Text.Wrap
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: Theme.space4
                            Text {
                                Layout.fillWidth: true
                                text: "File mẫu clone VieNeu (để trống để dùng profile hiện có)"
                                wrapMode: Text.Wrap
                                color: Theme.textSecondary
                                font.family: Theme.fontFamily
                                font.pixelSize: 10
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                DsTextField {
                                    Layout.fillWidth: true
                                    text: root.appModel.vieneuReferenceAudio
                                    placeholderText: "Đường dẫn WAV/MP3/FLAC"
                                    onEditingFinished: root.appModel.setVieNeuReferenceAudio(text)
                                }
                                DsButton {
                                    text: "Chọn"
                                    compact: true
                                    onClicked: referenceDialog.open()
                                }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                DsButton {
                                    objectName: "cloneReferencePlayButton"
                                    text: referencePlayer.playbackState === MediaPlayer.PlayingState
                                        ? "Tạm dừng file mẫu" : "Nghe file mẫu"
                                    compact: true
                                    enabled: Boolean(root.appModel.vieneuReferenceSource)
                                    disabledReason: "Hãy chọn file mẫu clone để nghe thử."
                                    onClicked: {
                                        root.referenceAudioError = ""
                                        if (referencePlayer.playbackState === MediaPlayer.PlayingState)
                                            referencePlayer.pause()
                                        else
                                            referencePlayer.play()
                                    }
                                }
                                DsButton {
                                    objectName: "cloneReferenceStopButton"
                                    text: "Dừng"
                                    compact: true
                                    enabled: referencePlayer.playbackState !== MediaPlayer.StoppedState
                                    onClicked: referencePlayer.stop()
                                }
                                Item { Layout.fillWidth: true }
                                Text {
                                    text: root.timeLabel(referencePlayer.position)
                                        + " / " + root.timeLabel(referencePlayer.duration)
                                    color: Theme.textMuted
                                    font.family: Theme.timecodeFamily
                                    font.pixelSize: 10
                                }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                Slider {
                                    objectName: "cloneReferenceSeekSlider"
                                    Layout.fillWidth: true
                                    from: 0
                                    to: Math.max(1, referencePlayer.duration)
                                    value: referencePlayer.position
                                    enabled: referencePlayer.seekable
                                        && referencePlayer.duration > 0
                                    onMoved: referencePlayer.setPosition(value)
                                    Accessible.name: "Tua file mẫu clone"
                                }
                            }
                            Text {
                                Layout.fillWidth: true
                                text: root.referenceAudioError
                                    || "Nghe trực tiếp file mẫu đã chọn; không tạo giọng hoặc gửi file đi."
                                color: root.referenceAudioError ? Theme.error : Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: 10
                                wrapMode: Text.Wrap
                            }
                            DsTextField {
                                Layout.fillWidth: true
                                text: root.appModel.vieneuReferenceText
                                placeholderText: "Lời mẫu (ghi chú; VieNeu 3 Turbo không bắt buộc)"
                                onEditingFinished: root.appModel.setVieNeuReferenceText(text)
                            }
                            CheckBox {
                                text: "Khử nhiễu khi tạo profile"
                                checked: root.appModel.vieneuReferenceDenoise
                                onToggled: root.appModel.setVieNeuReferenceDenoise(checked)
                                Accessible.name: text
                            }
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: voiceSpeedContent.implicitHeight + Theme.space16
                            radius: Theme.radiusSmall
                            color: Theme.surfaceBase
                            border.width: 1
                            border.color: Theme.borderSubtle

                            ColumnLayout {
                                id: voiceSpeedContent
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.top: parent.top
                                anchors.margins: Theme.space8
                                spacing: Theme.space4

                                Text {
                                    text: "Tốc độ giọng gốc"
                                    color: Theme.textPrimary
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 11
                                    font.weight: Font.DemiBold
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: "Áp dụng cho toàn bộ voice sau inference. VieNeu luôn chạy model ở 1,00×."
                                    color: Theme.textMuted
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 9
                                    wrapMode: Text.Wrap
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: Theme.space4
                                    Slider {
                                        id: voiceReadSpeedSlider
                                        Layout.fillWidth: true
                                        from: 1.1
                                        to: 3.0
                                        stepSize: 0.1
                                        snapMode: Slider.SnapAlways
                                        value: root.appModel.voiceReadSpeed
                                        enabled: !root.appModel.ttsBusy
                                        onMoved: readSpeedDebounce.restart()
                                        Accessible.name: "Tốc độ giọng gốc "
                                            + Number(value).toFixed(1) + " lần"
                                    }
                                    DsTextField {
                                        Layout.preferredWidth: 62
                                        text: Number(voiceReadSpeedSlider.value).toFixed(1)
                                        enabled: !root.appModel.ttsBusy
                                        validator: DoubleValidator {
                                            bottom: 1.1
                                            top: 3.0
                                            decimals: 1
                                            notation: DoubleValidator.StandardNotation
                                        }
                                        onEditingFinished: root.appModel.setVoiceReadSpeed(Number(text))
                                        Accessible.name: "Nhập tốc độ giọng gốc từ 1,1 đến 3,0"
                                    }
                                    Text {
                                        text: "×"
                                        color: Theme.textSecondary
                                        font.family: Theme.timecodeFamily
                                        font.pixelSize: 11
                                    }
                                }

                                Rectangle {
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 1
                                    color: Theme.borderSubtle
                                }

                                Text {
                                    text: "Giới hạn tổng cho cue dài"
                                    color: Theme.textPrimary
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 11
                                    font.weight: Font.DemiBold
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: "Tool đo WAV thật của từng cue và chỉ tăng cue còn dài. Đây là tốc độ tổng cuối cùng, không cộng dồn với tốc độ gốc."
                                    color: Theme.textMuted
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 9
                                    wrapMode: Text.Wrap
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: Theme.space4
                                    Slider {
                                        id: voiceCueSpeedSlider
                                        Layout.fillWidth: true
                                        from: Math.max(1.0, root.appModel.voiceReadSpeed)
                                        to: 3.0
                                        stepSize: 0.1
                                        snapMode: Slider.SnapAlways
                                        value: Math.max(from, root.appModel.voiceCueMaxSpeed)
                                        enabled: !root.appModel.ttsBusy
                                            && !root.appModel.voiceTimingAutoRepairBusy
                                        onMoved: cueSpeedDebounce.restart()
                                        Accessible.name: "Giới hạn tổng cue dài "
                                            + Number(value).toFixed(1) + " lần"
                                    }
                                    DsTextField {
                                        Layout.preferredWidth: 62
                                        text: Number(voiceCueSpeedSlider.value).toFixed(1)
                                        enabled: voiceCueSpeedSlider.enabled
                                        validator: DoubleValidator {
                                            bottom: Math.max(1.0, root.appModel.voiceReadSpeed)
                                            top: 3.0
                                            decimals: 1
                                            notation: DoubleValidator.StandardNotation
                                        }
                                        onEditingFinished: root.appModel.setVoiceCueMaxSpeed(Number(text))
                                        Accessible.name: "Nhập giới hạn tổng cue dài"
                                    }
                                    Text {
                                        text: "×"
                                        color: Theme.textSecondary
                                        font.family: Theme.timecodeFamily
                                        font.pixelSize: 11
                                    }
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: "Phần tăng thêm tối đa so với giọng gốc: "
                                        + (root.appModel.voiceCueMaxSpeed
                                            / Math.max(1.0, root.appModel.voiceReadSpeed)).toFixed(2)
                                        + "×"
                                    color: Theme.textMuted
                                    font.family: Theme.timecodeFamily
                                    font.pixelSize: 9
                                }
                            }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            visible: ((root.appModel.voiceTimingQa || {}).status || "NONE") !== "NONE"
                            spacing: Theme.space4
                            Text {
                                Layout.fillWidth: true
                                text: (root.appModel.voiceTimingQa || {}).message || ""
                                color: ((root.appModel.voiceTimingQa || {}).status || "NONE") === "PASS"
                                    ? Theme.success : Theme.warning
                                font.family: Theme.fontFamily
                                font.pixelSize: 10
                                font.weight: Font.DemiBold
                                wrapMode: Text.Wrap
                            }
                            Text {
                                Layout.fillWidth: true
                                text: "Gốc "
                                    + Number((root.appModel.voiceTimingQa || {}).voice_read_speed || 1).toFixed(2)
                                    + "× · cap cue "
                                    + Number((root.appModel.voiceTimingQa || {}).cue_max_total_speed || 1).toFixed(2)
                                    + "× · đã tăng thêm "
                                    + String((root.appModel.voiceTimingQa || {}).adjusted_cue_count || 0)
                                    + " cue · hệ số bổ sung lớn nhất "
                                    + Number((root.appModel.voiceTimingQa || {}).maximum_additional_factor || 1).toFixed(2)
                                    + "×"
                                color: Theme.textMuted
                                font.family: Theme.timecodeFamily
                                font.pixelSize: 9
                                wrapMode: Text.Wrap
                            }
                        }

                        Text {
                            Layout.fillWidth: true
                            text: root.appModel.voiceTimingAutoRepairBusy
                                ? "Đang tự rút gọn đúng các cue còn vượt giới hạn, sau đó tạo lại riêng các WAV đã đổi từ cache."
                                : "Tự động: tốc độ gốc → tăng cue dài trong giới hạn → đo lại → rút gọn tối đa 3 vòng. Cue còn lỗi hiển thị đỏ trên Timeline D1."
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: 10
                            wrapMode: Text.Wrap
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            DsButton {
                                Layout.fillWidth: true
                                text: root.appModel.ttsJobPurpose === "preview"
                                        && root.appModel.ttsJobState === "running"
                                    ? "Tạm dừng preview"
                                    : (["paused", "queued", "interrupted", "failed", "review"]
                                            .indexOf(root.appModel.ttsJobState) >= 0
                                        && root.appModel.ttsJobPurpose === "preview"
                                        ? "Tiếp tục preview" : "Tạo preview")
                                operationState: root.previewOperationState()
                                runningText: "Đang tạo preview…"
                                queuedText: "Preview đang chờ…"
                                startingText: "Đang chuẩn bị preview…"
                                pausedText: "Preview đã tạm dừng"
                                resumingText: "Đang tiếp tục preview…"
                                cancellingText: "Đang hủy preview…"
                                succeededText: "Preview đã sẵn sàng"
                                failedText: "Preview gặp lỗi"
                                feedbackText: "Đang tạo preview giọng"
                                enabled: root.previewButtonEnabled()
                                disabledReason: "Cần project, giọng sẵn sàng và không có tác vụ TTS khác."
                                onClicked: root.previewAction()
                            }
                            DsButton {
                                Layout.fillWidth: true
                                text: root.appModel.ttsJobPurpose === "production"
                                        && root.appModel.ttsJobState === "running"
                                    ? "Tạm dừng tạo giọng"
                                    : (["paused", "queued", "interrupted", "failed", "review"]
                                            .indexOf(root.appModel.ttsJobState) >= 0
                                        && root.appModel.ttsJobPurpose === "production"
                                        ? "Tiếp tục từ cache"
                                        : (root.appModel.currentProject.hasVoiceTrack
                                            ? "Tạo lại · ghi đè D1" : "Tạo toàn bộ"))
                                primary: true
                                warning: root.appModel.ttsJobPurpose === "production"
                                    && root.appModel.ttsJobState === "running"
                                operationState: root.productionOperationState()
                                runningText: "Đang tạo toàn bộ…"
                                queuedText: "Voice đang chờ…"
                                startingText: root.appModel.voiceTimingAutoRepairBusy
                                    ? "Đang tự rút gọn cue…" : "Đang chuẩn bị toàn bộ…"
                                pausingText: "Đang tạm dừng tạo giọng…"
                                pausedText: "Tiếp tục từ cache"
                                resumingText: "Đang tiếp tục từ cache…"
                                cancellingText: "Đang hủy tạo toàn bộ…"
                                succeededText: "Tạo toàn bộ hoàn tất"
                                failedText: "Tiếp tục từ cache"
                                feedbackText: root.appModel.ttsJobPurpose === "production"
                                        && root.appModel.ttsJobState === "running"
                                    ? "Đang yêu cầu tạm dừng tạo giọng"
                                    : "Đang bắt đầu hoặc tiếp tục tạo toàn bộ giọng"
                                enabled: root.productionButtonEnabled()
                                disabledReason: "Cần video và bản dịch Việt hoàn chỉnh."
                                onClicked: root.productionVoiceAction()
                            }
                        }

                        DsButton {
                            Layout.fillWidth: true
                            visible: root.appModel.ttsBusy
                            text: root.appModel.ttsJobPurpose === "preview"
                                ? "Hủy preview" : "Hủy tạo toàn bộ"
                            danger: true
                            operationState: root.appModel.ttsJobState === "cancel_requested"
                                ? "cancel_requested" : "idle"
                            cancellingText: "Đang hủy tác vụ giọng…"
                            feedbackText: "Đang yêu cầu hủy tác vụ giọng"
                            enabled: root.appModel.ttsJobState !== "cancel_requested"
                            disabledReason: "Tác vụ đang được hủy."
                            onClicked: root.appModel.cancelVoiceGeneration()
                        }
                    }

                    ScrollView {
                        id: mixerScroll
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

                        ColumnLayout {
                            width: mixerScroll.availableWidth
                            spacing: Theme.space12

                            SectionTitle { text: "Mixer giọng" }
                            Text {
                                Layout.fillWidth: true
                                text: root.appModel.currentProject.hasVoiceTrack
                                    ? "Track D1 đã sẵn sàng. Preview Timeline phát cùng video; volume và mute được chỉnh trực tiếp trên Timeline."
                                    : "Track D1 được tự tạo sau khi đủ 100% cue hợp lệ. Hãy tạo toàn bộ giọng trước khi đồng bộ hậu kỳ."
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: 12
                                wrapMode: Text.Wrap
                            }

                            Rectangle {
                                id: conformPanel
                                Layout.fillWidth: true
                                Layout.preferredHeight: conformPanelContent.implicitHeight
                                    + Theme.space24
                                visible: root.appModel.currentProject.hasVoiceTrack
                                radius: Theme.radiusMedium
                                color: Theme.surfaceBase
                                border.width: 1
                                border.color: root.appModel.voiceConformApplied
                                    ? Theme.successBorder
                                    : (Number(root.conformEstimateValue("unresolved_count", 0)) > 0
                                        ? Theme.errorBorder : Theme.borderSubtle)

                                ButtonGroup {
                                    id: conformModeGroup
                                    exclusive: true
                                }

                                ColumnLayout {
                                    id: conformPanelContent
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.top: parent.top
                                    anchors.margins: Theme.space12
                                    spacing: Theme.space8

                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: Theme.space4
                                        SectionTitle { text: "Đồng bộ sau lồng tiếng" }
                                        StatusPill {
                                            Layout.alignment: Qt.AlignLeft
                                            text: root.conformStatusText()
                                            runtimeState: root.conformRuntimeState()
                                            Accessible.name: "Trạng thái đồng bộ: " + text
                                            Accessible.role: Accessible.StaticText
                                        }
                                    }

                                    Text {
                                        Layout.fillWidth: true
                                        text: "Dùng WAV D1 đã có, không gọi lại VieNeu. Chỉ áp dụng khi ước lượng đạt QA."
                                        color: Theme.textMuted
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 10
                                        wrapMode: Text.Wrap
                                    }

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: Theme.space4

                                        DsTabButton {
                                            Layout.fillWidth: true
                                            Layout.minimumWidth: 0
                                            text: "Video"
                                            checked: root.appModel.voiceConformMode === "stretch_video"
                                            enabled: !root.appModel.voiceConformBusy
                                            ButtonGroup.group: conformModeGroup
                                            Accessible.name: "Chỉ giãn video để khớp giọng"
                                            onClicked: root.appModel.setVoiceConformMode("stretch_video")
                                        }
                                        DsTabButton {
                                            Layout.fillWidth: true
                                            Layout.minimumWidth: 0
                                            text: "Giọng"
                                            checked: root.appModel.voiceConformMode === "speed_audio"
                                            enabled: !root.appModel.voiceConformBusy
                                            ButtonGroup.group: conformModeGroup
                                            Accessible.name: "Chỉ tăng tốc giọng để khớp video"
                                            onClicked: root.appModel.setVoiceConformMode("speed_audio")
                                        }
                                        DsTabButton {
                                            Layout.fillWidth: true
                                            Layout.minimumWidth: 0
                                            text: "Kết hợp"
                                            checked: root.appModel.voiceConformMode === "hybrid"
                                            enabled: !root.appModel.voiceConformBusy
                                            ButtonGroup.group: conformModeGroup
                                            Accessible.name: "Kết hợp tăng tốc giọng và giãn video"
                                            onClicked: root.appModel.setVoiceConformMode("hybrid")
                                        }
                                    }

                                    Text {
                                        Layout.fillWidth: true
                                        text: root.conformModeDescription()
                                        color: Theme.textMuted
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 9
                                        wrapMode: Text.Wrap
                                    }

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: Theme.space8
                                        Text {
                                            text: "Tốc độ giọng tối đa"
                                            color: Theme.textSecondary
                                            font.family: Theme.fontFamily
                                            font.pixelSize: 10
                                        }
                                        Slider {
                                            id: conformAudioSpeed
                                            Layout.fillWidth: true
                                            from: 1.00
                                            to: 2.00
                                            stepSize: 0.05
                                            snapMode: Slider.SnapAlways
                                            value: root.conformMaxAudioSpeed()
                                            enabled: !root.appModel.voiceConformBusy
                                                && root.appModel.voiceConformMode !== "stretch_video"
                                            onMoved: conformEstimateDebounce.restart()
                                            Accessible.name: "Tốc độ giọng tối đa, "
                                                + Number(value).toFixed(2)
                                                + " lần, từ 1,00 đến 2,00 lần"
                                        }
                                        Text {
                                            Layout.preferredWidth: 42
                                            text: root.conformMaxAudioSpeed().toFixed(2) + "×"
                                            color: conformAudioSpeed.enabled
                                                ? Theme.textPrimary : Theme.textMuted
                                            font.family: Theme.timecodeFamily
                                            font.pixelSize: 11
                                            horizontalAlignment: Text.AlignRight
                                        }
                                    }

                                    GridLayout {
                                        Layout.fillWidth: true
                                        visible: root.hasConformEstimate()
                                        columns: 2
                                        columnSpacing: Theme.space8
                                        rowSpacing: Theme.space4

                                        Text { text: "Cue điều chỉnh"; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 10 }
                                        Text {
                                            Layout.alignment: Qt.AlignRight
                                            text: String(root.conformEstimateValue("adjusted_cue_count", 0))
                                            color: Theme.textPrimary
                                            font.family: Theme.timecodeFamily
                                            font.pixelSize: 10
                                        }
                                        Text { text: "Thời lượng đầu ra"; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 10 }
                                        Text {
                                            Layout.alignment: Qt.AlignRight
                                            text: root.durationUsLabel(root.conformEstimateValue("output_duration_us", NaN))
                                            color: Theme.textPrimary
                                            font.family: Theme.timecodeFamily
                                            font.pixelSize: 10
                                        }
                                        Text { text: "Thời lượng tăng thêm"; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 10 }
                                        Text {
                                            Layout.alignment: Qt.AlignRight
                                            text: "+" + root.durationUsLabel(root.conformEstimateValue("added_duration_us", NaN))
                                            color: Theme.textPrimary
                                            font.family: Theme.timecodeFamily
                                            font.pixelSize: 10
                                        }
                                        Text { text: "Dải tốc độ giọng"; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 10 }
                                        Text {
                                            Layout.alignment: Qt.AlignRight
                                            text: root.speedRangeLabel(
                                                root.conformEstimateValue("min_audio_speed", NaN),
                                                root.conformEstimateValue("max_audio_speed", NaN)
                                            )
                                            color: Theme.textPrimary
                                            font.family: Theme.timecodeFamily
                                            font.pixelSize: 10
                                        }
                                        Text { text: "Dải tốc độ video"; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 10 }
                                        Text {
                                            Layout.alignment: Qt.AlignRight
                                            text: root.speedRangeLabel(
                                                root.conformEstimateValue("min_video_speed", NaN),
                                                root.conformEstimateValue("max_video_speed", NaN)
                                            )
                                            color: Theme.textPrimary
                                            font.family: Theme.timecodeFamily
                                            font.pixelSize: 10
                                        }
                                    }

                                    Rectangle {
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: issueContent.implicitHeight + Theme.space16
                                        visible: Number(root.conformEstimateValue("unresolved_count", 0)) > 0
                                        radius: Theme.radiusSmall
                                        color: Theme.errorTint
                                        border.width: 1
                                        border.color: Theme.errorBorder

                                        ColumnLayout {
                                            id: issueContent
                                            anchors.left: parent.left
                                            anchors.right: parent.right
                                            anchors.top: parent.top
                                            anchors.margins: Theme.space8
                                            spacing: Theme.space4
                                            Text {
                                                Layout.fillWidth: true
                                                text: root.firstConformIssueText()
                                                color: Theme.textPrimary
                                                font.family: Theme.fontFamily
                                                font.pixelSize: 10
                                                font.weight: Font.DemiBold
                                                wrapMode: Text.Wrap
                                                maximumLineCount: 3
                                                elide: Text.ElideRight
                                            }
                                            DsButton {
                                                Layout.fillWidth: true
                                                compact: true
                                                text: "Mở cue chưa khớp"
                                                feedbackEnabled: false
                                                enabled: root.hasConformIssueTarget()
                                                disabledReason: "Ước lượng chưa cung cấp cue để điều hướng."
                                                onClicked: {
                                                    const issues = root.conformEstimateValue("issues", []) || []
                                                    if (issues.length > 0)
                                                        root.appModel.selectVoiceConformIssue(Number(issues[0].id))
                                                }
                                            }
                                        }
                                    }

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: Theme.space8
                                        DsButton {
                                            Layout.fillWidth: true
                                            compact: true
                                            text: root.hasConformEstimate() ? "Ước lượng lại" : "Tính ước lượng"
                                            enabled: !root.appModel.voiceConformBusy
                                            disabledReason: "Đang có tác vụ đồng bộ giọng và video."
                                            feedbackText: "Đang cập nhật ước lượng đồng bộ"
                                            onClicked: root.appModel.refreshVoiceConformEstimate()
                                        }
                                        DsButton {
                                            Layout.fillWidth: true
                                            compact: true
                                            text: "Hoàn tác về nhịp gốc"
                                            enabled: !root.appModel.voiceConformBusy
                                                && root.appModel.voiceConformApplied
                                            disabledReason: "Chưa có bản đồng bộ đã áp dụng để hoàn tác."
                                            feedbackText: "Đang đặt lại đồng bộ sau lồng tiếng"
                                            onClicked: root.appModel.resetVoiceConform()
                                        }
                                    }

                                    DsButton {
                                        Layout.fillWidth: true
                                        text: "Áp dụng đồng bộ"
                                        primary: true
                                        operationState: root.appModel.voiceConformBusy
                                            ? (root.appModel.voiceConformJobState || "running")
                                            : (root.appModel.voiceConformApplied
                                                ? "succeeded"
                                                : (["failed", "interrupted", "review"]
                                                    .indexOf(root.appModel.voiceConformJobState) >= 0
                                                    ? root.appModel.voiceConformJobState : "idle"))
                                        runningText: "Đang áp dụng đồng bộ…"
                                        queuedText: "Đồng bộ đang chờ…"
                                        startingText: "Đang chuẩn bị đồng bộ…"
                                        cancellingText: "Đang hủy đồng bộ…"
                                        succeededText: "Đã áp dụng đồng bộ"
                                        failedText: "Đồng bộ cần xử lý"
                                        enabled: root.appModel.voiceConformCanApply
                                            && !root.appModel.voiceConformBusy
                                            && !root.appModel.voiceConformApplied
                                        disabledReason: Number(root.conformEstimateValue("unresolved_count", 0)) > 0
                                            ? "Mở cue chưa khớp và sửa trước khi áp dụng."
                                            : (root.appModel.voiceConformApplied
                                                ? "Đồng bộ hiện tại đã được áp dụng."
                                                : "Cần D1 và ước lượng đạt QA trước khi áp dụng.")
                                        feedbackText: "Đang áp dụng đồng bộ sau lồng tiếng"
                                        onClicked: root.appModel.applyVoiceConform()
                                    }
                                }
                            }

                            DsButton {
                                Layout.fillWidth: true
                                text: "Mở Timeline"
                                enabled: root.appModel.currentProject.hasProject
                                disabledReason: "Chưa có project."
                                onClicked: root.appModel.navigate("timeline")
                            }
                        }
                    }

                    ColumnLayout {
                        spacing: Theme.space8
                        SectionTitle { text: "Lịch sử tác vụ" }
                        JobActivityPanel {
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            appModel: root.appModel
                            allowHistoryDeletion: true
                            historyScope: "tts"
                            jobModel: root.appModel.voiceJobModel
                            title: "Piper / VieNeu"
                            emptyDescription: "Chưa có lịch sử tạo giọng."
                        }
                    }

                    ColumnLayout {
                        id: castingTab
                        spacing: Theme.space8

                        RowLayout {
                            Layout.fillWidth: true
                            SectionTitle { text: "Phân vai thoại" }
                            Item { Layout.fillWidth: true }
                            DsButton {
                                text: "Làm mới"
                                compact: true
                                enabled: !root.appModel.dialogueCastingLoading
                                onClicked: root.appModel.refreshDialogueCasting()
                            }
                        }

                        Text {
                            Layout.fillWidth: true
                            text: "Thử nghiệm trên âm thanh gốc. Câu chưa chắc dùng giọng dự phòng; câu không phát hiện lời thoại được giữ ở Cần kiểm tra và chưa tạo giọng đến khi gán Nam/Nữ."
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: 10
                            wrapMode: Text.Wrap
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            Switch {
                                id: dialogueCastingSwitch
                                objectName: "dialogueCastingEnabledSwitch"
                                text: "Bật phân vai trước khi tạo D1"
                                checked: root.castingConfig().enabled === true
                                enabled: root.appModel.currentProject.hasProject
                                    && !root.appModel.dialogueCastingSaving
                                    && !root.appModel.dialogueCastingBusy
                                    && !root.appModel.ttsBusy
                                onToggled: root.appModel.setDialogueCastingEnabled(checked)
                            }
                            Item { Layout.fillWidth: true }
                        }

                        Repeater {
                            model: [
                                { role: "male", title: "Giọng nam" },
                                { role: "female", title: "Giọng nữ" },
                                { role: "fallback", title: "Giọng dự phòng" }
                            ]
                            delegate: ColumnLayout {
                                id: castingVoiceRole
                                required property string role
                                required property string title
                                Layout.fillWidth: true
                                spacing: Theme.space4

                                Text {
                                    Layout.fillWidth: true
                                    text: parent.title
                                    color: Theme.textSecondary
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 10
                                    font.weight: Font.DemiBold
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    ComboBox {
                                        id: castingVoiceSelector
                                        Layout.fillWidth: true
                                        model: root.appModel.voiceCatalogModel
                                        textRole: "voiceName"
                                        valueRole: "voiceId"
                                        currentIndex: root.castingVoiceIndex(castingVoiceRole.role)
                                        Accessible.name: "Chọn " + castingVoiceRole.title.toLowerCase()
                                        enabled: !root.appModel.dialogueCastingSaving
                                            && !root.appModel.dialogueCastingBusy
                                            && !root.appModel.ttsBusy
                                        delegate: ItemDelegate {
                                            required property string voiceName
                                            required property bool installed
                                            width: castingVoiceSelector.width
                                            text: voiceName + (installed ? "" : " · Chưa cài")
                                            enabled: installed
                                        }
                                        onActivated: root.appModel.setDialogueCastingVoice(
                                            castingVoiceRole.role, currentValue
                                        )
                                    }
                                    DsButton {
                                        text: "Nghe thử"
                                        compact: true
                                        enabled: !root.appModel.ttsBusy
                                            && !root.appModel.dialogueCastingBusy
                                            && !root.appModel.dialogueCastingSaving
                                            && Boolean(root.castingVoiceId(castingVoiceRole.role))
                                        disabledReason: "Chọn một giọng đã cài trước."
                                        onClicked: root.appModel.previewDialogueCastingVoice(
                                            castingVoiceRole.role
                                        )
                                    }
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    DsButton {
                                        objectName: "dialogueCastingReferenceButton-"
                                            + castingVoiceRole.role
                                        text: root.appModel.dialogueCastingPreviewRole
                                            === castingVoiceRole.role
                                            ? "Đang tạo mẫu clone…" : "Chọn file giọng…"
                                        compact: true
                                        enabled: root.appModel.currentProject.hasProject
                                            && !root.appModel.dialogueCastingSaving
                                            && !root.appModel.dialogueCastingBusy
                                            && !root.appModel.ttsBusy
                                        disabledReason: "Mở project và chờ tác vụ phân vai hiện tại kết thúc."
                                        onClicked: root.openCastingReferenceDialog(
                                            castingVoiceRole.role
                                        )
                                    }
                                    Text {
                                        id: castingReferenceLabel
                                        Layout.fillWidth: true
                                        text: root.castingReferenceName(castingVoiceRole.role)
                                        color: root.castingReferencePath(castingVoiceRole.role)
                                            ? Theme.textSecondary : Theme.textMuted
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 9
                                        elide: Text.ElideMiddle
                                        Accessible.name: castingVoiceRole.title
                                            + ": " + text
                                        HoverHandler { id: castingReferenceHover }
                                        ToolTip.visible: castingReferenceHover.hovered
                                            && Boolean(root.castingReferencePath(
                                                castingVoiceRole.role))
                                        ToolTip.text: root.castingReferencePath(
                                            castingVoiceRole.role)
                                        ToolTip.delay: 350
                                    }
                                }
                            }
                        }

                        Text {
                            Layout.fillWidth: true
                            text: "Chọn file sẽ tự tạo và phát một câu bằng giọng clone. Lần đầu cần chờ nạp mẫu; bấm Nghe thử để nghe lại."
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: 10
                            wrapMode: Text.Wrap
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            DsButton {
                                Layout.fillWidth: true
                                text: root.appModel.dialogueCastingBusy
                                    ? (root.appModel.dialogueCastingJobState === "running"
                                        ? "Tạm dừng nhận diện" : "Tiếp tục nhận diện")
                                    : (root.appModel.dialogueCastingJobState === "succeeded"
                                        ? "Cập nhật nhãn" : "Nhận diện thoại")
                                operationState: root.appModel.dialogueCastingBusy
                                    ? root.appModel.dialogueCastingJobState : "idle"
                                runningText: "Đang nhận diện thoại…"
                                startingText: "Đang chuẩn bị nhận diện…"
                                pausedText: "Tiếp tục nhận diện"
                                failedText: "Thử nhận diện lại"
                                enabled: root.appModel.currentProject.hasProject
                                    && !root.appModel.ttsBusy
                                    && !root.appModel.dialogueCastingSaving
                                onClicked: {
                                    if (root.appModel.dialogueCastingJobState === "running")
                                        root.appModel.pauseDialogueCasting()
                                    else if (root.appModel.dialogueCastingBusy)
                                        root.appModel.resumeDialogueCasting()
                                    else
                                        root.appModel.analyzeDialogueCasting()
                                }
                            }
                            DsButton {
                                text: "Hủy"
                                danger: true
                                visible: root.appModel.dialogueCastingBusy
                                enabled: root.appModel.dialogueCastingJobState !== "cancel_requested"
                                onClicked: root.appModel.cancelDialogueCasting()
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            Text {
                                text: "Câu: " + String(((root.appModel.dialogueCasting || {}).rows || []).length)
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: 10
                            }
                            Item { Layout.fillWidth: true }
                            ComboBox {
                                id: castingFilter
                                objectName: "dialogueCastingFilter"
                                Layout.preferredWidth: 132
                                model: ["Tất cả", "Cần kiểm tra", "Nam", "Nữ"]
                                currentIndex: 0
                                Accessible.name: "Lọc câu theo nhãn phân vai"
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            visible: root.selectedCastingCueIds.length > 0
                            Text {
                                text: String(root.selectedCastingCueIds.length) + " câu đã chọn"
                                color: Theme.textSecondary
                                font.family: Theme.fontFamily
                                font.pixelSize: 10
                            }
                            Item { Layout.fillWidth: true }
                            DsButton {
                                text: "Nam"
                                compact: true
                                enabled: !root.appModel.dialogueCastingSaving
                                    && !root.appModel.dialogueCastingBusy
                                onClicked: root.applyCastingLabel(root.selectedCastingCueIds, "male")
                            }
                            DsButton {
                                text: "Nữ"
                                compact: true
                                enabled: !root.appModel.dialogueCastingSaving
                                    && !root.appModel.dialogueCastingBusy
                                onClicked: root.applyCastingLabel(root.selectedCastingCueIds, "female")
                            }
                            DsButton {
                                text: "Kiểm tra"
                                compact: true
                                enabled: !root.appModel.dialogueCastingSaving
                                    && !root.appModel.dialogueCastingBusy
                                onClicked: root.applyCastingLabel(root.selectedCastingCueIds, "unknown")
                            }
                            DsButton {
                                text: "Tự động"
                                compact: true
                                enabled: !root.appModel.dialogueCastingSaving
                                    && !root.appModel.dialogueCastingBusy
                                onClicked: root.applyCastingLabel(root.selectedCastingCueIds, "auto")
                            }
                        }

                        Text {
                            objectName: "dialogueCastingAudioError"
                            Layout.fillWidth: true
                            visible: Boolean(root.castingAudioError)
                            text: root.castingAudioError
                            color: Theme.danger
                            font.family: Theme.fontFamily
                            font.pixelSize: 10
                            wrapMode: Text.Wrap
                            Accessible.name: text
                        }

                        ListView {
                            id: dialogueCastingRows
                            objectName: "dialogueCastingRows"
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            spacing: Theme.space4
                            model: root.filteredCastingRows()
                            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                            Text {
                                anchors.centerIn: parent
                                width: Math.max(0, parent.width - Theme.space16)
                                visible: dialogueCastingRows.count === 0
                                text: root.appModel.dialogueCastingLoading
                                    ? "Đang tải kết quả phân vai…"
                                    : (castingFilter.currentIndex === 0
                                        ? "Chưa có kết quả. Bấm Nhận diện thoại để bắt đầu."
                                        : "Không có câu nào trong bộ lọc này.")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: 11
                                horizontalAlignment: Text.AlignHCenter
                                wrapMode: Text.Wrap
                            }

                            delegate: Rectangle {
                                id: castingRow
                                required property var modelData
                                width: ListView.view.width
                                height: 126
                                radius: Theme.radiusSmall
                                color: Theme.surfaceRaised
                                border.width: 1
                                border.color: modelData.label === "unknown"
                                    ? Theme.warningBorder : Theme.borderSubtle

                                ColumnLayout {
                                    anchors.fill: parent
                                    anchors.margins: Theme.space4
                                    spacing: 3

                                    RowLayout {
                                        Layout.fillWidth: true
                                        CheckBox {
                                            checked: root.selectedCastingCueIds.indexOf(castingRow.modelData.id) >= 0
                                            enabled: !root.appModel.dialogueCastingSaving
                                                && !root.appModel.dialogueCastingBusy
                                            onToggled: root.toggleCastingCue(castingRow.modelData.id, checked)
                                            Accessible.name: "Chọn câu " + String(castingRow.modelData.index)
                                        }
                                        Text {
                                            text: "#" + String(castingRow.modelData.index)
                                                + " · " + root.timeLabel(Number(castingRow.modelData.start_us || 0) / 1000)
                                                + "–" + root.timeLabel(Number(castingRow.modelData.end_us || 0) / 1000)
                                            color: Theme.textMuted
                                            font.family: Theme.timecodeFamily
                                            font.pixelSize: 9
                                        }
                                        StatusPill {
                                            text: root.castingLabelDetail(castingRow.modelData)
                                            runtimeState: castingRow.modelData.label === "unknown" ? "starting" : "ready"
                                            Accessible.name: "Phân vai: " + text
                                        }
                                        Item { Layout.fillWidth: true }
                                        DsButton {
                                            objectName: "dialogueCastingSourceListenButton"
                                            text: "Nghe gốc"
                                            compact: true
                                            enabled: (castingRow.modelData.parts || []).length > 0
                                            onClicked: root.playCastingSource(castingRow.modelData)
                                        }
                                    }

                                    Text {
                                        Layout.fillWidth: true
                                        text: String(castingRow.modelData.translated_text
                                            || castingRow.modelData.text || "")
                                        color: Theme.textPrimary
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 10
                                        elide: Text.ElideRight
                                    }

                                    Text {
                                        Layout.fillWidth: true
                                        visible: Boolean(castingRow.modelData.manual
                                            || castingRow.modelData.reason)
                                        text: castingRow.modelData.tts_suppressed
                                            ? "Không phát hiện lời thoại · chưa tạo giọng"
                                            : (castingRow.modelData.manual
                                                ? "Nhãn sửa tay"
                                                : root.castingReasonText(castingRow.modelData.reason))
                                        color: Theme.textMuted
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 9
                                        elide: Text.ElideRight
                                    }

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: Theme.space4
                                        DsButton {
                                            Layout.fillWidth: true
                                            text: "Nam"
                                            compact: true
                                            emphasized: castingRow.modelData.label === "male"
                                            enabled: !root.appModel.dialogueCastingSaving
                                                && !root.appModel.dialogueCastingBusy
                                            onClicked: root.applyCastingLabel([castingRow.modelData.id], "male")
                                        }
                                        DsButton {
                                            Layout.fillWidth: true
                                            text: "Nữ"
                                            compact: true
                                            emphasized: castingRow.modelData.label === "female"
                                            enabled: !root.appModel.dialogueCastingSaving
                                                && !root.appModel.dialogueCastingBusy
                                            onClicked: root.applyCastingLabel([castingRow.modelData.id], "female")
                                        }
                                        DsButton {
                                            Layout.fillWidth: true
                                            text: "Kiểm tra"
                                            compact: true
                                            emphasized: castingRow.modelData.label === "unknown"
                                            enabled: !root.appModel.dialogueCastingSaving
                                                && !root.appModel.dialogueCastingBusy
                                            onClicked: root.applyCastingLabel([castingRow.modelData.id], "unknown")
                                        }
                                        DsButton {
                                            Layout.fillWidth: true
                                            text: "Tự động"
                                            compact: true
                                            emphasized: !castingRow.modelData.manual
                                            enabled: !root.appModel.dialogueCastingSaving
                                                && !root.appModel.dialogueCastingBusy
                                            onClicked: root.applyCastingLabel([castingRow.modelData.id], "auto")
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
