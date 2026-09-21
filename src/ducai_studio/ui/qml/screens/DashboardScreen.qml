pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import DucaiStudio 1.0
import "../controls"

Item {
    id: root

    property var appModel
    property string inspectorTab: appModel.uiStringValue("dashboardInspectorTab", "project")
    activeFocusOnTab: true
    Accessible.name: "Tổng quan dự án"

    function selectInspectorTab(value) {
        inspectorTab = value
        appModel.setUiStringValue("dashboardInspectorTab", value)
    }

    Timer {
        id: persistPaneWidths
        interval: 250
        onTriggered: {
            root.appModel.setUiRealValue("dashboardLeftRatio", leftPane.width / Math.max(1, root.width))
            root.appModel.setUiRealValue("dashboardRightRatio", rightPane.width / Math.max(1, root.width))
        }
    }

    SplitView {
        anchors.fill: parent
        anchors.margins: Theme.space12
        orientation: Qt.Horizontal

        handle: Rectangle {
            implicitWidth: 8
            color: SplitHandle.hovered || SplitHandle.pressed
                ? Theme.surfaceHover
                : Theme.canvas
            Rectangle {
                anchors.centerIn: parent
                width: 1
                height: parent.height
                color: Theme.borderSubtle
            }
        }

        ColumnLayout {
            id: leftPane

            SplitView.preferredWidth: root.width * root.appModel.uiRealValue("dashboardLeftRatio", 0.22)
            SplitView.minimumWidth: 208
            SplitView.maximumWidth: 310
            spacing: Theme.space12
            onWidthChanged: persistPaneWidths.restart()

            Panel {
                Layout.fillWidth: true
                Layout.preferredHeight: 144
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: Theme.space16
                    spacing: Theme.space12
                    SectionTitle { text: "Trạng thái hệ thống" }
                    RowLayout {
                        Layout.fillWidth: true
                        Text {
                            text: "Job Manager"
                            color: Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: 12
                        }
                        Item { Layout.fillWidth: true }
                        Text {
                            Layout.maximumWidth: 128
                            text: root.appModel.runtimeState === "ready"
                                ? "Sẵn sàng"
                                : root.appModel.statusMessage
                            color: root.appModel.runtimeState === "ready"
                                ? Theme.success
                                : (root.appModel.runtimeState === "error" ? Theme.error : Theme.warning)
                            font.family: Theme.fontFamily
                            font.pixelSize: 11
                            font.weight: Font.DemiBold
                            elide: Text.ElideRight
                        }
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.appModel.jobModel.count > 0
                            ? "Đang theo dõi " + root.appModel.jobModel.count + " tác vụ bền vững."
                            : "Telemetry không có nguồn thật sẽ được ẩn."
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                    }
                }
            }

            Panel {
                Layout.fillWidth: true
                Layout.preferredHeight: 212
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: Theme.space16
                    spacing: Theme.space8
                    SectionTitle { text: "Thao tác nhanh" }
                    GridLayout {
                        Layout.fillWidth: true
                        columns: 2
                        columnSpacing: Theme.space8
                        rowSpacing: Theme.space8
                        DsButton {
                            Layout.fillWidth: true
                            text: "Nhập video"
                            enabled: root.appModel.backendReady && !root.appModel.importBusy
                            disabledReason: root.appModel.importBusy
                                ? "Một file đang được nhập."
                                : "Job Manager chưa sẵn sàng."
                            onClicked: root.appModel.showImportDialog()
                        }
                        DsButton {
                            Layout.fillWidth: true
                            text: "Trích sub"
                            onClicked: root.appModel.navigate("transcribe")
                        }
                        DsButton {
                            Layout.fillWidth: true
                            text: "Dịch SRT"
                            onClicked: root.appModel.navigate("translate")
                        }
                        DsButton {
                            Layout.fillWidth: true
                            text: "Tạo giọng"
                            onClicked: root.appModel.navigate("voice")
                        }
                    }
                }
            }

            Panel {
                Layout.fillWidth: true
                Layout.fillHeight: true
                EmptyState {
                    anchors.fill: parent
                    visible: root.appModel.projectModel.count === 0
                    title: "Chưa có dự án gần đây"
                    description: "Danh sách chỉ hiện dự án đã lưu thật."
                    iconName: "folder"
                }
                ListView {
                    objectName: "dashboardProjectList"
                    anchors.fill: parent
                    anchors.margins: Theme.space8
                    visible: root.appModel.projectModel.count > 0
                    model: root.appModel.projectModel
                    spacing: Theme.space4
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    delegate: Rectangle {
                        id: projectRow
                        required property var model
                        readonly property bool selected: projectRow.model.projectId
                            === root.appModel.currentProject.projectId
                        width: ListView.view.width
                        height: 58
                        radius: Theme.radiusSmall
                        color: projectRow.selected
                            ? Theme.selectionFill
                            : (projectHover.hovered ? Theme.surfaceHover : Theme.surfaceRaised)
                        border.width: projectRow.selected || activeFocus ? 1 : 0
                        border.color: activeFocus ? Theme.focusRing : Theme.primary
                        activeFocusOnTab: true
                        Accessible.role: Accessible.Button
                        Accessible.name: "Mở dự án " + projectRow.model.name
                        Accessible.focusable: true

                        function openSavedProject() {
                            root.appModel.openProject(projectRow.model.projectId)
                        }

                        HoverHandler { id: projectHover }
                        TapHandler { onTapped: projectRow.openSavedProject() }
                        Keys.onReturnPressed: projectRow.openSavedProject()
                        Keys.onEnterPressed: projectRow.openSavedProject()
                        Keys.onSpacePressed: projectRow.openSavedProject()

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: Theme.space8
                            anchors.rightMargin: projectRow.selected ? 72 : Theme.space8
                            spacing: 0
                            Text {
                                Layout.fillWidth: true
                                text: projectRow.model.name
                                color: Theme.textPrimary
                                font.family: Theme.fontFamily
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                                elide: Text.ElideRight
                            }
                            Text {
                                Layout.fillWidth: true
                                text: projectRow.model.updatedAt
                                color: Theme.textMuted
                                font.family: Theme.timecodeFamily
                                font.pixelSize: 10
                                elide: Text.ElideRight
                            }
                        }

                        Text {
                            anchors.right: parent.right
                            anchors.rightMargin: Theme.space8
                            anchors.verticalCenter: parent.verticalCenter
                            visible: projectRow.selected
                            text: "Đang mở"
                            color: Theme.primary
                            font.family: Theme.fontFamily
                            font.pixelSize: 9
                            font.weight: Font.DemiBold
                        }
                    }
                }
            }
        }

        ColumnLayout {
            SplitView.fillWidth: true
            SplitView.minimumWidth: 430
            spacing: Theme.space12

            Panel {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: 260
                color: Theme.canvas
                TimelinePreview {
                    anchors.fill: parent
                    appModel: root.appModel
                    visible: root.appModel.currentProject.hasVideo
                }
                EmptyState {
                    anchors.fill: parent
                    visible: !root.appModel.currentProject.hasVideo
                    title: "Preview chưa có media"
                    description: "Mở dự án hoặc nhập video để xem hình, sub và voice đồng bộ."
                    iconName: "panels-top-left"
                }
            }

            Panel {
                Layout.fillWidth: true
                Layout.preferredHeight: 188
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: Theme.space12
                    spacing: Theme.space8
                    RowLayout {
                        Layout.fillWidth: true
                        SectionTitle { text: "Tổng quan timeline" }
                        Item { Layout.fillWidth: true }
                        Text {
                            text: root.appModel.currentProject.hasProject
                                ? root.appModel.currentProject.durationText
                                : "Không có project"
                            color: Theme.textMuted
                            font.family: Theme.timecodeFamily
                            font.pixelSize: 10
                        }
                    }
                    EmptyState {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        visible: root.appModel.currentProject.trackModel.count === 0
                        title: "Chưa có track"
                        description: "Video, audio, phụ đề và overlay sẽ nằm trên cùng một timebase."
                        iconName: "panels-top-left"
                    }
                    ListView {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        visible: root.appModel.currentProject.trackModel.count > 0
                        model: root.appModel.currentProject.trackModel
                        spacing: Theme.space4
                        clip: true
                        boundsBehavior: Flickable.StopAtBounds
                        delegate: Rectangle {
                            id: trackRow
                            required property var model
                            width: ListView.view.width
                            height: 30
                            radius: Theme.radiusSmall
                            color: Theme.surfaceRaised
                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: Theme.space8
                                anchors.rightMargin: Theme.space8
                                spacing: Theme.space8
                                Text {
                                    text: trackRow.model.trackId
                                    color: Theme.primary
                                    font.family: Theme.timecodeFamily
                                    font.pixelSize: 10
                                    font.weight: Font.DemiBold
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: trackRow.model.name
                                    color: Theme.textSecondary
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 10
                                    elide: Text.ElideRight
                                }
                                Text {
                                    text: trackRow.model.clipCount + " clip"
                                    color: Theme.textMuted
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 9
                                }
                            }
                        }
                    }
                }
            }
        }

        Item {
            id: rightPane
            SplitView.preferredWidth: root.width * root.appModel.uiRealValue("dashboardRightRatio", 0.27)
            SplitView.minimumWidth: 260
            SplitView.maximumWidth: 380
            onWidthChanged: persistPaneWidths.restart()

            ColumnLayout {
                anchors.fill: parent
                spacing: Theme.space8
                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.space4
                    DsTabButton {
                        text: "Dự án"
                        checked: root.inspectorTab === "project"
                        onClicked: root.selectInspectorTab("project")
                    }
                    DsTabButton {
                        text: "Tác vụ"
                        checked: root.inspectorTab === "jobs"
                        onClicked: root.selectInspectorTab("jobs")
                    }
                }
                StackLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    currentIndex: root.inspectorTab === "jobs" ? 1 : 0
                    Panel {
                        EmptyState {
                            anchors.fill: parent
                            visible: !root.appModel.currentProject.hasProject
                            title: "Chưa chọn dự án"
                            description: "Thuộc tính và lịch sử chỉ hiện từ project thật."
                            iconName: "folder"
                        }
                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: Theme.space16
                            spacing: Theme.space12
                            visible: root.appModel.currentProject.hasProject
                            SectionTitle {
                                Layout.fillWidth: true
                                text: root.appModel.currentProject.name
                                elide: Text.ElideRight
                            }
                            Text {
                                Layout.fillWidth: true
                                text: root.appModel.currentProject.primaryVideoPath
                                color: Theme.textMuted
                                font.family: Theme.timecodeFamily
                                font.pixelSize: 9
                                elide: Text.ElideMiddle
                            }
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 1
                                color: Theme.borderSubtle
                            }
                            GridLayout {
                                Layout.fillWidth: true
                                columns: 2
                                columnSpacing: Theme.space12
                                rowSpacing: Theme.space8

                                Text { text: "Thời lượng"; color: Theme.textSecondary; font.pixelSize: 10 }
                                Text {
                                    Layout.fillWidth: true
                                    text: root.appModel.currentProject.durationText
                                    color: Theme.textPrimary
                                    font.family: Theme.timecodeFamily
                                    font.pixelSize: 10
                                    horizontalAlignment: Text.AlignRight
                                }
                                Text { text: "Revision"; color: Theme.textSecondary; font.pixelSize: 10 }
                                Text {
                                    Layout.fillWidth: true
                                    text: String(root.appModel.currentProject.revision)
                                    color: Theme.textPrimary
                                    font.family: Theme.timecodeFamily
                                    font.pixelSize: 10
                                    horizontalAlignment: Text.AlignRight
                                }
                                Text { text: "Video"; color: Theme.textSecondary; font.pixelSize: 10 }
                                Text {
                                    Layout.fillWidth: true
                                    text: root.appModel.currentProject.hasVideo ? "Sẵn sàng" : "Chưa có"
                                    color: root.appModel.currentProject.hasVideo ? Theme.success : Theme.warning
                                    font.pixelSize: 10
                                    horizontalAlignment: Text.AlignRight
                                }
                                Text { text: "Phụ đề"; color: Theme.textSecondary; font.pixelSize: 10 }
                                Text {
                                    Layout.fillWidth: true
                                    text: root.appModel.currentProject.subtitleCueModel.count + " cue"
                                    color: root.appModel.currentProject.subtitleCueModel.count > 0
                                        ? Theme.success : Theme.warning
                                    font.pixelSize: 10
                                    horizontalAlignment: Text.AlignRight
                                }
                                Text { text: "Bản Việt"; color: Theme.textSecondary; font.pixelSize: 10 }
                                Text {
                                    Layout.fillWidth: true
                                    text: root.appModel.currentProject.hasTranslatedSubtitles
                                        ? "Đã import" : "Chưa có"
                                    color: root.appModel.currentProject.hasTranslatedSubtitles
                                        ? Theme.success : Theme.warning
                                    font.pixelSize: 10
                                    horizontalAlignment: Text.AlignRight
                                }
                                Text { text: "Thuyết minh"; color: Theme.textSecondary; font.pixelSize: 10 }
                                Text {
                                    Layout.fillWidth: true
                                    text: root.appModel.currentProject.hasVoiceTrack ? "Đã có D1" : "Chưa có"
                                    color: root.appModel.currentProject.hasVoiceTrack
                                        ? Theme.success : Theme.warning
                                    font.pixelSize: 10
                                    horizontalAlignment: Text.AlignRight
                                }
                            }
                            Item { Layout.fillHeight: true }
                            DsButton {
                                Layout.fillWidth: true
                                text: "Mở timeline"
                                primary: true
                                onClicked: root.appModel.navigate("timeline")
                            }
                        }
                    }
                    JobActivityPanel {
                        jobModel: root.appModel.jobModel
                        title: "Hoạt động gần đây"
                        emptyDescription: "OCR, dịch, TTS và export sẽ được theo dõi ở đây."
                    }
                }
            }
        }
    }
}
