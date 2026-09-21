pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import DucaiStudio 1.0
import "../controls"

Item {
    id: root

    property var appModel
    activeFocusOnTab: true
    Accessible.name: "Dự án"

    Dialog {
        id: deleteProjectDialog
        property string projectId: ""
        property string projectName: ""

        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(520, root.width - Theme.space24)
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape
        padding: 0
        background: Rectangle {
            color: Theme.surfaceRaised
            border.width: 1
            border.color: Theme.borderStrong
            radius: Theme.radiusMedium
        }
        contentItem: ColumnLayout {
            spacing: Theme.space16
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 52
                color: Theme.surfaceBase
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: Theme.space16
                    anchors.rightMargin: Theme.space8
                    SectionTitle { text: "Xóa dự án khỏi DUCAI?" }
                    Item { Layout.fillWidth: true }
                    DsButton {
                        text: "X"
                        compact: true
                        onClicked: deleteProjectDialog.close()
                    }
                }
            }
            Text {
                Layout.fillWidth: true
                Layout.leftMargin: Theme.space16
                Layout.rightMargin: Theme.space16
                text: "Dự án “" + deleteProjectDialog.projectName
                    + "” và toàn bộ job/cache nội bộ sẽ bị xóa. Video, audio và SRT nguồn trên ổ đĩa vẫn được giữ nguyên."
                color: Theme.textSecondary
                font.family: Theme.fontFamily
                font.pixelSize: 12
                wrapMode: Text.Wrap
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: Theme.space16
                Layout.rightMargin: Theme.space16
                Layout.bottomMargin: Theme.space16
                Item { Layout.fillWidth: true }
                DsButton {
                    text: "Hủy"
                    onClicked: deleteProjectDialog.close()
                }
                DsButton {
                    text: "Xóa dự án"
                    danger: true
                    onClicked: {
                        root.appModel.deleteProject(
                            deleteProjectDialog.projectId,
                            deleteProjectDialog.projectName
                        )
                        deleteProjectDialog.close()
                    }
                }
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Theme.space12
        spacing: Theme.space12

        Panel {
            Layout.fillWidth: true
            Layout.preferredHeight: 88

            RowLayout {
                anchors.fill: parent
                anchors.margins: Theme.space16
                spacing: Theme.space12

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Theme.space4
                    SectionTitle { text: "Dự án của bạn" }
                    Text {
                        Layout.fillWidth: true
                        text: "Mở lại project đã lưu hoặc nhập media để tạo project mới."
                        color: Theme.textSecondary
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                    }
                }

                StatusPill {
                    runtimeState: root.appModel.projectDeleteBusy ? "starting"
                        : (root.appModel.backendReady ? "ready" : "starting")
                    text: root.appModel.projectDeleteBusy
                        ? "Đang xoá: " + root.appModel.deletingProjectName
                        : root.appModel.projectModel.count + " dự án"
                }

                DsButton {
                    text: root.appModel.importBusy ? "Đang nhập..." : "Tạo từ media"
                    primary: true
                    enabled: root.appModel.backendReady && !root.appModel.importBusy
                    disabledReason: root.appModel.importBusy
                        ? "Một file đang được nhập."
                        : "Job Manager chưa sẵn sàng."
                    onClicked: root.appModel.showNewProjectDialog()
                }
            }
        }

        Panel {
            Layout.fillWidth: true
            Layout.fillHeight: true

            EmptyState {
                anchors.fill: parent
                visible: root.appModel.projectModel.count === 0
                title: "Chưa có dự án"
                description: "Nhập video đầu tiên; tool sẽ tạo project và đưa V1/A1 vào timeline."
                iconName: "folder"
            }

            ListView {
                id: projectList
                objectName: "projectsList"
                anchors.fill: parent
                anchors.margins: Theme.space12
                visible: root.appModel.projectModel.count > 0
                model: root.appModel.projectModel
                spacing: Theme.space8
                clip: true
                boundsBehavior: Flickable.StopAtBounds

                delegate: Rectangle {
                    id: projectRow
                    required property var model

                    width: ListView.view.width
                    height: 76
                    radius: Theme.radiusMedium
                    color: projectRow.model.projectId === root.appModel.currentProject.projectId
                        ? Theme.selectionFill
                        : Theme.surfaceRaised
                    border.width: projectRow.model.projectId === root.appModel.currentProject.projectId
                        ? 1 : 0
                    border.color: Theme.primary

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: Theme.space16
                        anchors.rightMargin: Theme.space12
                        spacing: Theme.space12

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: Theme.space4
                            Text {
                                Layout.fillWidth: true
                                text: projectRow.model.name
                                color: Theme.textPrimary
                                font.family: Theme.fontFamily
                                font.pixelSize: 13
                                font.weight: Font.DemiBold
                                elide: Text.ElideRight
                            }
                            Text {
                                Layout.fillWidth: true
                                text: projectRow.model.path
                                color: Theme.textMuted
                                font.family: Theme.timecodeFamily
                                font.pixelSize: 9
                                elide: Text.ElideMiddle
                            }
                            Text {
                                Layout.fillWidth: true
                                text: "Cập nhật: " + projectRow.model.updatedAt
                                color: Theme.textSecondary
                                font.family: Theme.fontFamily
                                font.pixelSize: 10
                                elide: Text.ElideRight
                            }
                        }

                        StatusPill {
                            visible: projectRow.model.projectId === root.appModel.currentProject.projectId
                            runtimeState: "ready"
                            text: "Đang mở"
                        }

                        DsButton {
                            text: projectRow.model.projectId === root.appModel.currentProject.projectId
                                ? "Tải lại" : "Mở dự án"
                            enabled: root.appModel.backendReady
                            disabledReason: "Job Manager chưa sẵn sàng."
                            onClicked: root.appModel.openProject(projectRow.model.projectId)
                        }
                        DsButton {
                            text: root.appModel.projectDeleteBusy
                                && root.appModel.deletingProjectId === projectRow.model.projectId
                                ? "Đang xoá..." : "Xóa"
                            compact: true
                            danger: true
                            enabled: root.appModel.backendReady
                                && !root.appModel.projectDeleteBusy
                            disabledReason: root.appModel.projectDeleteBusy
                                ? "Đang xoá dự án; hãy chờ hoàn tất."
                                : "Job Manager chưa sẵn sàng."
                            onClicked: {
                                deleteProjectDialog.projectId = projectRow.model.projectId
                                deleteProjectDialog.projectName = projectRow.model.name
                                deleteProjectDialog.open()
                            }
                        }
                    }
                }
            }
        }
    }
}
