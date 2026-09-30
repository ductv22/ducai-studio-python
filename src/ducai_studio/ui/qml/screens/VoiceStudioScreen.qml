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
    activeFocusOnTab: true
    Accessible.name: "Phòng thu giọng"

    FileDialog {
        id: referenceDialog
        title: "Chọn file mẫu giọng VieNeu"
        fileMode: FileDialog.OpenFile
        nameFilters: ["Âm thanh (*.wav *.mp3 *.m4a *.flac)", "Tất cả file (*)"]
        onAccepted: root.appModel.setVieNeuReferenceAudio(selectedFile.toString())
    }

    function selectInspectorTab(value) {
        inspectorTab = value
        appModel.setUiStringValue("voiceInspectorTab", value)
    }

    function selectedValue(key, fallback) {
        const value = appModel.selectedVoice[key]
        return value === undefined || value === null || value === "" ? fallback : value
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
        source: root.appModel.voiceCatalogPreviewSource
            ? root.appModel.voiceCatalogPreviewSource
            : root.appModel.voicePreviewSource
        audioOutput: previewAudio
        onMediaStatusChanged: {
            if (mediaStatus === MediaPlayer.LoadedMedia && source) {
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
        source: root.appModel.voicePlaybackEnabled
            ? root.appModel.currentProject.voiceSource : ""
        audioOutput: productionAudio
    }

    AudioOutput {
        id: productionAudio
        volume: 0.9
    }

    Connections {
        target: root.appModel
        function onVoicePreviewChanged() {
            if (!root.appModel.voiceCatalogPreviewSource)
                previewPlayer.source = root.appModel.voicePreviewSource
        }
        function onVoiceCatalogPlaybackChanged() {
            previewPlayer.source = root.appModel.voiceCatalogPreviewSource
                ? root.appModel.voiceCatalogPreviewSource
                : root.appModel.voicePreviewSource
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
                }

                StackLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    currentIndex: root.inspectorTab === "mixer"
                        ? 1 : (root.inspectorTab === "history" ? 2 : 0)

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
                            visible: Boolean(root.selectedValue("is_clone", false))
                            spacing: Theme.space4
                            Text {
                                text: "Mẫu clone mới (để trống để dùng profile hiện có)"
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
                }
            }
        }
    }
}
