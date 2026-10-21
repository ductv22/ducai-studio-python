pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtMultimedia
import DucaiStudio 1.0
import "../controls"

Item {
    id: root
    objectName: "exportScreen"
    property var appModel
    readonly property bool queueOnly: appModel.currentWorkspace === "queue"
    readonly property string jobState: appModel.exportJobState
    readonly property bool waiting: ["start_requested", "pause_requested",
        "resume_requested", "cancel_requested"].indexOf(jobState) >= 0
    readonly property string actionText: jobState === "paused" ? "Tiếp tục"
        : (["running", "pause_requested"].indexOf(jobState) >= 0 ? "Tạm dừng" : "Xuất video")
    activeFocusOnTab: true
    Accessible.name: queueOnly ? "Hàng đợi" : "Xuất video"

    MediaPlayer {
        id: outputPlayer
        source: root.appModel.lastExportSource
        audioOutput: outputAudio
        videoOutput: outputVideo
    }
    AudioOutput { id: outputAudio; volume: 0.9 }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Theme.space12
        spacing: Theme.space12
        RowLayout {
            Layout.fillWidth: true
            SectionTitle { text: root.queueOnly ? "Hàng đợi" : "Xuất video" }
            Item { Layout.fillWidth: true }
            DsButton {
                text: "Thiết lập trong Timeline"
                compact: true
                tooltipText: "Chỉnh chất lượng, chữ, nền, hiệu ứng và âm thanh trước khi xuất."
                onClicked: root.appModel.openTimelineSettings()
            }
        }
        Text {
            Layout.fillWidth: true
            text: root.appModel.exportOutputPath
            color: Theme.textSecondary
            font.family: Theme.fontFamily
            font.pixelSize: 11
            elide: Text.ElideMiddle
        }
        Panel {
            id: outputPreviewPanel
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: !root.queueOnly
            color: Theme.canvas
            VideoOutput {
                id: outputVideo
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.bottom: outputTransport.top
                fillMode: VideoOutput.PreserveAspectFit
            }
            EmptyState {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.bottom: outputTransport.top
                visible: !Boolean(root.appModel.lastExportSource)
                title: "Thành phẩm"
                description: "Video đã xuất và kiểm tra thành công sẽ phát tại đây."
                iconName: "panels-top-left"
            }
            Rectangle {
                id: outputTransport
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: 46
                color: Theme.surfaceBase
                RowLayout {
                    anchors.fill: parent
                    anchors.margins: Theme.space8
                    DsIconButton {
                        iconName: outputPlayer.playbackState === MediaPlayer.PlayingState
                            ? "pause" : "play"
                        tooltipText: "Phát hoặc tạm dừng thành phẩm"
                        enabled: Boolean(root.appModel.lastExportSource)
                        onClicked: {
                            const wasPlaying = outputPlayer.playbackState === MediaPlayer.PlayingState
                            if (wasPlaying) {
                                outputPlayer.pause()
                                UiFeedback.show("Đã tạm dừng thành phẩm", "info")
                            } else {
                                outputPlayer.play()
                                UiFeedback.show("Đang phát thành phẩm", "info")
                            }
                        }
                    }
                    Slider {
                        Layout.fillWidth: true
                        from: 0; to: Math.max(1, outputPlayer.duration)
                        value: outputPlayer.position
                        enabled: Boolean(root.appModel.lastExportSource)
                        onMoved: outputPlayer.setPosition(value)
                        Accessible.name: "Vị trí phát thành phẩm"
                    }
                    DsIconButton {
                        iconName: outputAudio.muted ? "volume-x" : "volume-2"
                        tooltipText: outputAudio.muted ? "Bật tiếng" : "Tắt tiếng"
                        enabled: Boolean(root.appModel.lastExportSource)
                        onClicked: outputAudio.muted = !outputAudio.muted
                    }
                }
            }
        }
        Panel {
            id: queuePanel
            Layout.fillWidth: true
            Layout.fillHeight: root.queueOnly
            Layout.preferredHeight: 160
            JobActivityPanel {
                anchors.fill: parent
                anchors.margins: Theme.space8
                jobModel: root.appModel.exportJobModel
                title: "Tiến trình xuất"
                emptyTitle: "Chưa có tác vụ xuất"
                emptyDescription: "Thiết lập ở Timeline rồi nhấn Xuất video."
            }
        }
        RowLayout {
            objectName: "exportPrimaryActions"
            Layout.fillWidth: true
            visible: !root.queueOnly
            Item { Layout.fillWidth: true }
            DsButton {
                objectName: "exportPrimaryButton"
                text: root.actionText
                primary: true
                feedbackEnabled: false
                enabled: !root.waiting && (["queued", "paused", "running"].indexOf(root.jobState) >= 0
                    || (!root.appModel.exportBusy && root.appModel.currentProject.hasVideo
                        && Boolean(root.appModel.exportOutputPath)))
                tooltipText: root.jobState === "paused" ? "Tiếp tục tác vụ đã tạm dừng"
                    : (root.jobState === "running" ? "Tạm dừng render, giữ checkpoint"
                        : "Xuất bản chỉnh sửa hiện tại")
                onClicked: {
                    if (root.jobState === "running") root.appModel.pauseExport()
                    else if (root.jobState === "paused" || root.jobState === "queued") root.appModel.resumeExport()
                    else root.appModel.startExport()
                }
            }
            DsButton {
                objectName: "exportCancelButton"
                text: "Hủy"
                danger: true
                feedbackEnabled: false
                enabled: root.appModel.exportBusy && root.jobState !== "cancel_requested"
                tooltipText: "Dừng hoàn toàn tác vụ xuất video"
                onClicked: root.appModel.cancelExport()
            }
        }
    }
}
