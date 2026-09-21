pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtMultimedia
import DucaiStudio 1.0
import "../controls"

Item {
    id: root

    property var appModel
    property string activeProjectId: ""
    property url activeVideoUrl: ""
    property url activeVoiceUrl: ""
    property var activeSubtitleCues: []
    property bool useVoiceAudio: false
    property string activeSubtitle: ""

    activeFocusOnTab: true
    Accessible.name: "Multi video"

    function isRunningState() {
        return appModel.multiBatchState === "running"
            || appModel.multiBatchState === "pause_requested"
    }

    function statusColor(state, errorCode) {
        if (errorCode === "ocr_review_required"
                && (state === "deferred" || state === "failed"))
            return Theme.warning
        if (errorCode === "translation_review_required"
                && (state === "deferred" || state === "failed"))
            return Theme.error
        if (state === "completed") return Theme.success
        if (state === "failed" || state === "deferred") return Theme.error
        if (state === "running" || state === "retrying") return Theme.info
        if (state === "paused" || state === "interrupted") return Theme.textMuted
        return Theme.warning
    }

    function stopActivePreview() {
        sharedPlayer.stop()
        sharedPlayer.source = ""
        sharedPlayer.videoOutput = null
        activeProjectId = ""
        activeVideoUrl = ""
        activeVoiceUrl = ""
        activeSubtitleCues = []
        useVoiceAudio = false
        activeSubtitle = ""
    }

    function requestCard(projectId, voiceMode) {
        const sameCard = activeProjectId === projectId
        const requestedVoice = Boolean(voiceMode)
        if (sameCard && useVoiceAudio === requestedVoice
                && sharedPlayer.playbackState === MediaPlayer.PlayingState) {
            sharedPlayer.pause()
            return
        }
        appModel.requestMultiVideoPreview(projectId, requestedVoice)
    }

    function playDescriptor(descriptor, voiceMode) {
        const projectId = String(descriptor.project_id || "")
        const videoUrl = descriptor.video_url || ""
        const voiceUrl = descriptor.voice_audio_url || ""
        const requestedVoice = Boolean(voiceMode)
            && root.appModel.voicePlaybackEnabled
            && String(voiceUrl).length > 0
        if (projectId.length === 0)
            return
        activeProjectId = projectId
        activeVideoUrl = videoUrl
        activeVoiceUrl = voiceUrl
        activeSubtitleCues = descriptor.subtitle_cues || []
        useVoiceAudio = requestedVoice
        activeSubtitle = ""
        sharedPlayer.stop()
        sharedPlayer.source = requestedVoice ? voiceUrl : videoUrl
        sharedPlayer.play()
    }

    Connections {
        target: root.appModel
        function onMultiPreviewReady(descriptor, useVoice) {
            root.playDescriptor(descriptor, useVoice)
        }
        function onVoicePlaybackEnabledChanged() {
            if (!root.appModel.voicePlaybackEnabled && root.useVoiceAudio)
                root.stopActivePreview()
        }
    }

    function removeCard(projectId) {
        if (activeProjectId === projectId)
            stopActivePreview()
        appModel.removeMultiVideo(projectId)
    }

    function updateSubtitle() {
        if (!activeSubtitleCues || activeSubtitleCues.length === 0) {
            activeSubtitle = ""
            return
        }
        const nowUs = Number(sharedPlayer.position) * 1000
        for (let index = 0; index < activeSubtitleCues.length; ++index) {
            const cue = activeSubtitleCues[index]
            if (nowUs >= Number(cue.startUs) && nowUs < Number(cue.endUs)) {
                activeSubtitle = String(cue.translatedText || cue.sourceText || "")
                return
            }
        }
        activeSubtitle = ""
    }

    MediaPlayer {
        id: sharedPlayer
        audioOutput: sharedAudio
        onPositionChanged: root.updateSubtitle()
        onMediaStatusChanged: {
            if (mediaStatus === MediaPlayer.InvalidMedia)
                UiFeedback.show("Không thể phát preview của video này.", "error", 8000)
        }
    }

    AudioOutput {
        id: sharedAudio
        volume: root.appModel.previewMasterVolumePercent / 100.0
    }

    onVisibleChanged: {
        if (!visible)
            stopActivePreview()
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Theme.space16
        spacing: Theme.space12

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.space12

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2
                Text {
                    text: "Multi video"
                    color: Theme.textPrimary
                    font.family: Theme.fontFamily
                    font.pixelSize: 22
                    font.weight: Font.Bold
                }
                Text {
                    text: root.appModel.multiVideoModel.count + " video · Đã chọn "
                        + root.appModel.multiSelectedCount + "/"
                        + root.appModel.multiVideoModel.count
                    color: Theme.textSecondary
                    font.family: Theme.fontFamily
                    font.pixelSize: 11
                }
            }

            DsButton {
                text: "Nhập video"
                primary: true
                compact: true
                enabled: !root.appModel.multiListLocked
                Accessible.name: "Nhập một hoặc nhiều video vào Multi video"
                onClicked: root.appModel.showMultiImportDialog()
            }

            DsButton {
                visible: root.appModel.multiHiddenCount > 0
                text: "Khôi phục đã ẩn (" + root.appModel.multiHiddenCount + ")"
                compact: true
                enabled: !root.appModel.multiListLocked
                Accessible.name: "Khôi phục video đã ẩn khỏi Multi video"
                onClicked: root.appModel.restoreHiddenMultiVideos()
            }

            CheckBox {
                text: "Chọn tất cả"
                enabled: !root.appModel.multiListLocked
                Accessible.name: "Chọn hoặc bỏ chọn tất cả video Multi"
                checked: root.appModel.multiVideoModel.count > 0
                    && root.appModel.multiSelectedCount === root.appModel.multiVideoModel.count
                onClicked: root.appModel.selectAllMultiVideos(checked)
            }

            Rectangle {
                Layout.preferredHeight: 38
                Layout.preferredWidth: Math.min(
                    320, Math.max(220, configText.implicitWidth + Theme.space24))
                Layout.maximumWidth: 320
                radius: Theme.radiusMedium
                color: Theme.surfaceRaised
                border.width: 1
                border.color: Theme.borderSubtle
                Text {
                    id: configText
                    anchors.fill: parent
                    anchors.leftMargin: Theme.space12
                    anchors.rightMargin: Theme.space12
                    text: "OCR " + Number(root.appModel.ocrSampleFps).toFixed(2)
                        + " FPS GPU · " + root.appModel.uiStringValue(
                        "translation/provider", "gemini-web") + " · "
                        + (root.appModel.selectedVoice.name || "Chưa chọn giọng")
                    color: Theme.textSecondary
                    font.family: Theme.fontFamily
                    font.pixelSize: 10
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                }
            }

            DsButton {
                primary: true
                warning: root.isRunningState()
                text: {
                    if (root.appModel.multiBatchState === "running") return "Tạm dừng Multi video"
                    if (root.appModel.multiBatchState === "pause_requested") return "Đang tạm dừng…"
                    if (["paused", "interrupted"]
                            .indexOf(root.appModel.multiBatchState) >= 0)
                        return "Tiếp tục Multi video"
                    if (root.appModel.multiBatchState === "completed_with_errors")
                        return "Chạy lại video lỗi"
                    return "Chạy Multi video"
                }
                enabled: root.appModel.multiSelectedCount > 0
                    && root.appModel.multiBatchState !== "pause_requested"
                disabledReason: root.appModel.multiBatchState === "pause_requested"
                    ? "Đã nhận lệnh; đang chốt checkpoint hoặc dừng worker trong vài giây."
                    : "Chưa chọn video nào."
                onClicked: {
                    if (root.appModel.multiBatchState === "running")
                        root.appModel.pauseMultiVideo()
                    else if (["paused", "interrupted"]
                            .indexOf(root.appModel.multiBatchState) >= 0)
                        root.appModel.resumeMultiVideo()
                    else
                        root.appModel.startMultiVideo()
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            implicitHeight: 38
            radius: Theme.radiusMedium
            color: Theme.surfacePanel
            border.width: 1
            border.color: Theme.borderSubtle
            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: Theme.space12
                anchors.rightMargin: Theme.space12
                Text {
                    Layout.fillWidth: true
                    text: root.appModel.multiBatchState === "interrupted"
                        ? "Tác vụ bị gián đoạn. Cache/checkpoint vẫn còn; bấm Tiếp tục để chạy đúng công đoạn hiện tại."
                        : (root.appModel.multiBatchState === "pause_requested"
                            ? "Đã nhận lệnh tạm dừng; đang chờ worker chốt checkpoint hoặc xác nhận đã dừng."
                            : "Chạy tuần tự: OCR → Dịch → Lồng tiếng. Multi video không tự xuất thành phẩm.")
                    color: ["interrupted", "pause_requested"]
                        .indexOf(root.appModel.multiBatchState) >= 0
                        ? Theme.warning : Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: 10
                    elide: Text.ElideRight
                }
            }
        }

        GridView {
            id: grid
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            model: root.appModel.multiVideoModel
            readonly property int columns: width >= 1050 ? 3 : (width >= 700 ? 2 : 1)
            cellWidth: Math.floor(width / columns)
            cellHeight: Math.max(318, (cellWidth - Theme.space12) * 9 / 16 + 166)

            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

            Column {
                anchors.centerIn: parent
                spacing: Theme.space8
                visible: root.appModel.multiVideoModel.count === 0

                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "Chưa có video trong Multi"
                    color: Theme.textSecondary
                    font.family: Theme.fontFamily
                    font.pixelSize: 16
                    font.weight: Font.DemiBold
                }
                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "Nhập video bằng nút Nhập video hoặc mở các project đã có trong ứng dụng."
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: 11
                }
            }

            delegate: Item {
                id: cardHost
                required property var model
                readonly property bool inViewport: y + height >= grid.contentY
                    && y <= grid.contentY + grid.height

                width: GridView.view.cellWidth
                height: GridView.view.cellHeight

                Rectangle {
                    anchors.fill: parent
                    anchors.margins: Theme.space4
                    radius: Theme.radiusLarge
                    color: Theme.surfacePanel
                    border.width: 1
                    border.color: Theme.borderSubtle

                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: Theme.space8
                        spacing: Theme.space8

                        RowLayout {
                            Layout.fillWidth: true
                            CheckBox {
                                checked: cardHost.model.selected
                                enabled: !root.appModel.multiListLocked
                                Accessible.name: "Chọn video " + cardHost.model.name
                                onClicked: root.appModel.selectMultiVideo(
                                    cardHost.model.projectId, checked)
                            }
                            Text {
                                Layout.fillWidth: true
                                text: cardHost.model.name
                                color: Theme.textPrimary
                                font.family: Theme.fontFamily
                                font.pixelSize: 13
                                font.weight: Font.DemiBold
                                elide: Text.ElideRight
                            }
                            DsButton {
                                text: "⋯"
                                compact: true
                                feedbackEnabled: false
                                Accessible.name: "Tùy chọn video " + cardHost.model.name
                                onClicked: cardMenu.open()
                                Menu {
                                    id: cardMenu
                                    MenuItem {
                                        text: cardHost.model.stage === "translate"
                                            && cardHost.model.errorCode === "translation_review_required"
                                            ? "Sửa bản dịch còn chữ Trung"
                                            : "Chỉnh sửa chi tiết"
                                        enabled: !root.appModel.multiDetailBusy
                                        onTriggered: root.appModel.openMultiVideoDetail(
                                            cardHost.model.projectId)
                                    }
                                    MenuSeparator {}
                                    MenuItem {
                                        text: "Xóa khỏi Multi"
                                        enabled: !root.appModel.multiListLocked
                                        onTriggered: root.removeCard(
                                            cardHost.model.projectId)
                                    }
                                }
                            }
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: Math.max(120, width * 9 / 16)
                            radius: Theme.radiusMedium
                            color: "#05080B"
                            clip: true

                            Image {
                                id: thumbnail
                                anchors.fill: parent
                                visible: root.activeProjectId !== cardHost.model.projectId
                                    || root.useVoiceAudio
                                source: cardHost.inViewport ? cardHost.model.thumbnailUrl : ""
                                sourceSize.width: 640
                                sourceSize.height: 360
                                asynchronous: true
                                cache: true
                                fillMode: Image.PreserveAspectFit
                            }

                            VideoOutput {
                                id: videoSurface
                                anchors.fill: parent
                                fillMode: VideoOutput.PreserveAspectFit
                                visible: root.activeProjectId === cardHost.model.projectId
                                    && !root.useVoiceAudio
                                Component.onCompleted: {
                                    if (root.activeProjectId === cardHost.model.projectId
                                            && !root.useVoiceAudio)
                                        sharedPlayer.videoOutput = this
                                }
                                Connections {
                                    target: root
                                    function onActiveProjectIdChanged() {
                                        if (root.activeProjectId === cardHost.model.projectId
                                                && !root.useVoiceAudio)
                                            sharedPlayer.videoOutput = videoSurface
                                    }
                                    function onUseVoiceAudioChanged() {
                                        if (root.activeProjectId === cardHost.model.projectId
                                                && !root.useVoiceAudio)
                                            sharedPlayer.videoOutput = videoSurface
                                    }
                                }
                            }

                            Text {
                                anchors.centerIn: parent
                                visible: root.activeProjectId !== cardHost.model.projectId
                                    || root.useVoiceAudio
                                text: root.activeProjectId === cardHost.model.projectId
                                    && root.useVoiceAudio ? "D1 · chỉ âm thanh"
                                    : (thumbnail.status === Image.Ready ? "" : "▶")
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: root.activeProjectId === cardHost.model.projectId
                                    && root.useVoiceAudio ? 12 : 32
                            }

                            Rectangle {
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.bottom: parent.bottom
                                implicitHeight: subtitleLabel.visible ? subtitleLabel.implicitHeight + 14 : 0
                                color: "#B3000000"
                                visible: subtitleLabel.visible
                                Text {
                                    id: subtitleLabel
                                    anchors.fill: parent
                                    anchors.margins: 7
                                    visible: root.activeProjectId === cardHost.model.projectId
                                        && root.activeSubtitle.length > 0
                                    text: root.activeSubtitle
                                    color: "white"
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 11
                                    horizontalAlignment: Text.AlignHCenter
                                    wrapMode: Text.Wrap
                                }
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            DsButton {
                                text: root.activeProjectId === cardHost.model.projectId
                                    && !root.useVoiceAudio
                                    && sharedPlayer.playbackState === MediaPlayer.PlayingState
                                    ? "Tạm dừng video" : "Phát video"
                                compact: true
                                onClicked: root.requestCard(
                                    cardHost.model.projectId,
                                    false)
                            }
                            DsButton {
                                text: root.activeProjectId === cardHost.model.projectId
                                    && root.useVoiceAudio
                                    && sharedPlayer.playbackState === MediaPlayer.PlayingState
                                    ? "Tạm dừng D1" : "Nghe D1"
                                compact: true
                                enabled: cardHost.model.hasVoiceTrack
                                disabledReason: "Video này chưa có D1."
                                onClicked: root.requestCard(
                                    cardHost.model.projectId,
                                    true)
                            }
                            Text {
                                Layout.fillWidth: true
                                text: cardHost.model.durationText
                                color: Theme.textMuted
                                font.family: Theme.timecodeFamily
                                font.pixelSize: 10
                                horizontalAlignment: Text.AlignRight
                            }
                        }

                        ProgressBar {
                            Layout.fillWidth: true
                            from: 0
                            to: 1
                            value: cardHost.model.stageProgress
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            Rectangle {
                                Layout.preferredWidth: 8
                                Layout.preferredHeight: 8
                                radius: 4
                                color: root.statusColor(cardHost.model.state,
                                    cardHost.model.errorCode)
                            }
                            Text {
                                Layout.fillWidth: true
                                text: cardHost.model.statusText
                                color: root.statusColor(cardHost.model.state,
                                    cardHost.model.errorCode)
                                font.family: Theme.fontFamily
                                font.pixelSize: 10
                                font.weight: Font.DemiBold
                                elide: Text.ElideRight
                            }
                        }
                        Text {
                            Layout.fillWidth: true
                            visible: (cardHost.model.state === "failed"
                                || cardHost.model.state === "interrupted"
                                || cardHost.model.state === "deferred")
                                && String(cardHost.model.errorMessage).length > 0
                            text: cardHost.model.errorMessage
                            color: root.statusColor(cardHost.model.state,
                                cardHost.model.errorCode)
                            font.family: Theme.fontFamily
                            font.pixelSize: 9
                            elide: Text.ElideRight
                            ToolTip.visible: detailHover.containsMouse
                            ToolTip.text: text
                            MouseArea {
                                id: detailHover
                                anchors.fill: parent
                                hoverEnabled: true
                                acceptedButtons: Qt.NoButton
                            }
                        }
                    }
                }
            }
        }
    }
}
