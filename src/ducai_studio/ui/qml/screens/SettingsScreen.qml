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
    Accessible.name: "Cài đặt và chẩn đoán"

    ScrollView {
        anchors.fill: parent
        anchors.margins: Theme.space12
        contentWidth: availableWidth

        ColumnLayout {
            width: parent.width
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
                        SectionTitle { text: "Chẩn đoán runtime" }
                        Text {
                            Layout.fillWidth: true
                            text: "Kiểm tra Python, FFmpeg/NVENC, Paddle, Gemini, Piper và VieNeu thật."
                            color: Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: 11
                            wrapMode: Text.Wrap
                        }
                    }
                    StatusPill {
                        runtimeState: root.appModel.runtimeDiagnostics.overall_status === "ready"
                            ? "ready"
                            : "starting"
                        text: root.appModel.runtimeDiagnostics.overall_status || "Chưa chạy"
                    }
                    DsButton {
                        text: "Chạy chẩn đoán"
                        primary: true
                        enabled: root.appModel.backendReady
                        onClicked: root.appModel.runRuntimeDiagnostics()
                    }
                }
            }

            Panel {
                Layout.fillWidth: true
                Layout.preferredHeight: Math.max(
                    176, diagnosticsColumn.implicitHeight + Theme.space24
                )
                ColumnLayout {
                    id: diagnosticsColumn
                    anchors.fill: parent
                    anchors.margins: Theme.space12
                    spacing: Theme.space8
                    SectionTitle { text: "Engine và provider" }
                    Repeater {
                        model: root.appModel.runtimeDiagnostics.checks || []
                        delegate: Rectangle {
                            id: checkRow
                            required property var modelData
                            Layout.fillWidth: true
                            Layout.preferredHeight: 44
                            radius: Theme.radiusSmall
                            color: Theme.surfaceRaised
                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: Theme.space12
                                anchors.rightMargin: Theme.space12
                                spacing: Theme.space8
                                Text {
                                    Layout.preferredWidth: 122
                                    text: checkRow.modelData.component
                                    color: Theme.textPrimary
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 11
                                    font.weight: Font.DemiBold
                                    elide: Text.ElideRight
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: checkRow.modelData.summary
                                    color: Theme.textSecondary
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 10
                                    elide: Text.ElideRight
                                }
                                Text {
                                    text: checkRow.modelData.status
                                    color: checkRow.modelData.status === "ready"
                                        ? Theme.success
                                        : Theme.warning
                                    font.family: Theme.timecodeFamily
                                    font.pixelSize: 10
                                }
                            }
                        }
                    }
                    EmptyState {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 100
                        visible: !(root.appModel.runtimeDiagnostics.checks || []).length
                        title: "Chưa có báo cáo"
                        description: "Bấm Chạy chẩn đoán trước khi sản xuất phim dài."
                        iconName: "settings"
                    }
                }
            }

            Panel {
                Layout.fillWidth: true
                Layout.preferredHeight: 188
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: Theme.space16
                    spacing: Theme.space8
                    RowLayout {
                        Layout.fillWidth: true
                        SectionTitle { text: "Nhập một chiều từ 0.1.99" }
                        Item { Layout.fillWidth: true }
                        DsButton {
                            text: "Chọn dự án cũ"
                            onClicked: root.appModel.showLegacyImportDialog()
                        }
                        DsButton {
                            text: "Nhập sau dry-run"
                            primary: true
                            enabled: root.appModel.legacyMigrationReport.dry_run === true
                                && root.appModel.legacyMigrationReport.source_unchanged === true
                            disabledReason: "Phải dry-run nguồn bất biến trước."
                            onClicked: root.appModel.importInspectedLegacyProject()
                        }
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.appModel.legacyMigrationReport.source_path
                            || "Chưa chọn file JSON/.ducai của bản 0.1.99."
                        color: Theme.textSecondary
                        font.family: Theme.fontFamily
                        font.pixelSize: 10
                        elide: Text.ElideMiddle
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.appModel.legacyMigrationReport.dry_run === true
                            ? "Dry-run: media "
                                + (root.appModel.legacyMigrationReport.imported_media || []).length
                                + ", SRT "
                                + (root.appModel.legacyMigrationReport.imported_subtitles || []).length
                                + ", skipped "
                                + (root.appModel.legacyMigrationReport.skipped || []).length
                            : "Credential luôn bị bỏ qua; cache không đủ fingerprint không được sao chép."
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                    }
                }
            }
        }
    }
}
