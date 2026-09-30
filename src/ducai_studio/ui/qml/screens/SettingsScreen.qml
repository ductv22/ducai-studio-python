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
                Layout.preferredHeight: Math.max(220, updateColumn.implicitHeight + Theme.space32)

                ColumnLayout {
                    id: updateColumn
                    anchors.fill: parent
                    anchors.margins: Theme.space16
                    spacing: Theme.space8

                    RowLayout {
                        Layout.fillWidth: true
                        SectionTitle { text: "Cập nhật DUCAI STUDIO" }
                        Item { Layout.fillWidth: true }
                        Text {
                            text: "Bản hiện tại: v" + root.appModel.buildVersion
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: 11
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.space8
                        Text {
                            text: "Kênh phát hành"
                            color: Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: 12
                        }
                        ComboBox {
                            id: updateChannel
                            Layout.preferredWidth: 160
                            model: ["Ổn định", "Thử nghiệm"]
                            currentIndex: root.appModel.updater.channel === "testing" ? 1 : 0
                            enabled: root.appModel.updater.state !== "checking"
                                && root.appModel.updater.state !== "downloading"
                                && root.appModel.updater.state !== "installing"
                            Accessible.name: "Kênh cập nhật"
                            Accessible.description: "Ổn định chỉ nhận bản chính thức; Thử nghiệm nhận cả bản alpha."
                            onActivated: (index) => root.appModel.updater.setChannel(
                                index === 1 ? "testing" : "stable"
                            )
                        }
                        Item { Layout.fillWidth: true }
                        DsButton {
                            text: root.appModel.updater.state === "checking"
                                ? "Đang kiểm tra…" : "Kiểm tra cập nhật"
                            enabled: root.appModel.updater.state !== "checking"
                                && root.appModel.updater.state !== "downloading"
                                && root.appModel.updater.state !== "installing"
                            disabledReason: "Vui lòng chờ thao tác hiện tại hoàn tất."
                            tooltipText: "Kiểm tra thủ công bản phát hành đã ký trên GitHub."
                            feedbackEnabled: false
                            onClicked: root.appModel.updater.check()
                        }
                    }

                    Text {
                        Layout.fillWidth: true
                        text: root.appModel.updater.version
                            ? "Bản mới: v" + root.appModel.updater.version
                                + " · " + (root.appModel.updater.totalBytes / 1048576).toFixed(1) + " MiB"
                            : "Ứng dụng không tự kiểm tra hoặc tự cài đặt bản cập nhật."
                        color: Theme.textSecondary
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                        textFormat: Text.PlainText
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.appModel.updater.message
                        color: root.appModel.updater.state === "error" ? Theme.error : Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                        textFormat: Text.PlainText
                        Accessible.name: "Trạng thái cập nhật: " + text
                    }
                    Text {
                        Layout.fillWidth: true
                        visible: root.appModel.updater.releaseNotes.length > 0
                        text: root.appModel.updater.releaseNotes
                        color: Theme.textSecondary
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                        textFormat: Text.PlainText
                    }
                    ProgressBar {
                        Layout.fillWidth: true
                        visible: root.appModel.updater.state === "downloading"
                        from: 0
                        to: root.appModel.updater.totalBytes
                        value: root.appModel.updater.receivedBytes
                        Accessible.name: "Tiến độ tải bản cập nhật"
                        Accessible.description: root.appModel.updater.receivedBytes
                            + " trên " + root.appModel.updater.totalBytes + " byte"
                    }
                    Text {
                        visible: root.appModel.updater.state === "downloading"
                        text: root.appModel.updater.receivedBytes + " / "
                            + root.appModel.updater.totalBytes + " byte ("
                            + root.appModel.updater.percent + "%)"
                        color: Theme.textSecondary
                        font.family: Theme.timecodeFamily
                        font.pixelSize: 11
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.space8
                        DsButton {
                            text: "Tải bản cập nhật"
                            primary: true
                            visible: root.appModel.updater.state === "available"
                            feedbackEnabled: false
                            onClicked: root.appModel.updater.download()
                        }
                        DsButton {
                            text: "Hủy tải"
                            visible: root.appModel.updater.state === "downloading"
                            feedbackEnabled: false
                            onClicked: root.appModel.updater.cancelDownload()
                        }
                        DsButton {
                            text: "Thử lại"
                            visible: root.appModel.updater.state === "error"
                            feedbackEnabled: false
                            onClicked: root.appModel.updater.check()
                        }
                        DsButton {
                            text: "Cài đặt"
                            primary: true
                            visible: root.appModel.updater.state === "ready"
                                || root.appModel.updater.state === "waiting_jobs"
                            feedbackEnabled: false
                            tooltipText: "Xác nhận trước khi khởi động lại để cài bản đã xác minh."
                            onClicked: root.appModel.updater.requestInstallConfirmation()
                        }
                        CheckBox {
                            visible: root.appModel.updater.state === "waiting_jobs"
                            text: "Cài đặt khi xong tác vụ"
                            checked: root.appModel.updater.installAfterJobs
                            Accessible.name: "Theo dõi tác vụ để sẵn sàng cài đặt"
                            Accessible.description: "Chỉ kiểm tra khi tác vụ xong; vẫn cần bấm Cài đặt để xác nhận."
                            onToggled: root.appModel.updater.setInstallAfterJobs(checked)
                        }
                        Item { Layout.fillWidth: true }
                    }
                    Text {
                        Layout.fillWidth: true
                        text: "Chỉ thay file ứng dụng; dữ liệu DUCAI_STUDIO_DATA, dự án, media, model, cache và runtime ngoài được giữ nguyên. Bản 0.1.99/a117 cần cài a118 lần đầu bằng tay."
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 10
                        wrapMode: Text.Wrap
                        textFormat: Text.PlainText
                    }
                }
            }

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
