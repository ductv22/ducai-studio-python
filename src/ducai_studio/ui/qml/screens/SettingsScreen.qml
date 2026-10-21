pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import DucaiStudio 1.0
import "../controls"

Item {
    id: root

    property var appModel
    activeFocusOnTab: true
    Accessible.name: "Cài đặt và chẩn đoán"

    FileDialog {
        id: licenseDialog
        title: "Chọn file key DUCAI STUDIO"
        fileMode: FileDialog.OpenFile
        nameFilters: ["Key DUCAI (*.ducai-license *.txt)", "Tất cả file (*)"]
        onAccepted: root.appModel.activation.importLicense(selectedFile)
    }

    ScrollView {
        anchors.fill: parent
        anchors.margins: Theme.space12
        contentWidth: availableWidth

        ColumnLayout {
            width: parent.width
            spacing: Theme.space12

            Panel {
                Layout.fillWidth: true
                Layout.preferredHeight: activationColumn.implicitHeight + Theme.space32
                ColumnLayout {
                    id: activationColumn
                    anchors.fill: parent
                    anchors.margins: Theme.space16
                    spacing: Theme.space8
                    RowLayout {
                        Layout.fillWidth: true
                        SectionTitle { text: "Kích hoạt DUCAI STUDIO" }
                        Item { Layout.fillWidth: true }
                        Text {
                            text: root.appModel.activation.active ? "Đã kích hoạt" : "Chưa kích hoạt"
                            color: root.appModel.activation.active ? Theme.success : Theme.warning
                            font.family: Theme.fontFamily
                            font.pixelSize: 12
                        }
                    }
                    Text {
                        Layout.fillWidth: true
                        text: "Sao chép mã máy và gửi người cấp key. Bạn có thể được cấp key vĩnh viễn hoặc key thử nghiệm có thời hạn; key chỉ dùng trên máy này và không cần Internet để kiểm tra."
                        color: Theme.textSecondary
                        font.family: Theme.fontFamily
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        TextField {
                            Layout.fillWidth: true
                            text: root.appModel.activation.requestCode
                            readOnly: true
                            selectByMouse: true
                            placeholderText: "Đang đọc mã máy…"
                            font.family: Theme.fontFamily
                            font.pixelSize: 12
                            Accessible.name: "Mã máy để cấp key"
                        }
                        DsButton {
                            text: "Sao chép mã máy"
                            enabled: root.appModel.activation.requestCode.length > 0
                            tooltipText: "Sao chép đầy đủ mã máy để gửi người cấp key."
                            onClicked: root.appModel.activation.copyRequestCode()
                        }
                    }
                    ScrollView {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 76
                        TextArea {
                            id: licenseInput
                            placeholderText: "Dán key DUCAI-L3. có thời hạn, DUCAI-L2. vĩnh viễn hoặc key cũ DUCAI-L1. vào đây"
                            wrapMode: TextEdit.WrapAnywhere
                            selectByMouse: true
                            font.family: Theme.fontFamily
                            font.pixelSize: 11
                            Accessible.name: "Key kích hoạt được cấp"
                            background: Rectangle {
                                color: Theme.surfaceRaised
                                border.color: Theme.borderSubtle
                                border.width: 1
                                radius: Theme.radiusSmall
                            }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        DsButton {
                            text: root.appModel.activation.busy ? "Đang kiểm tra…" : "Kích hoạt"
                            primary: true
                            enabled: !root.appModel.activation.busy && licenseInput.text.trim().length > 0
                            tooltipText: "Kiểm tra chữ ký và mã máy, sau đó lưu kích hoạt trên máy này."
                            feedbackEnabled: false
                            onClicked: root.appModel.activation.activate(licenseInput.text)
                        }
                        DsButton {
                            text: "Nhập file key"
                            enabled: !root.appModel.activation.busy
                            tooltipText: "Chọn file giấy phép do người cấp key gửi."
                            feedbackEnabled: false
                            onClicked: licenseDialog.open()
                        }
                        DsButton {
                            text: "Kiểm tra lại"
                            enabled: !root.appModel.activation.busy
                            tooltipText: "Kiểm tra lại mã máy và giấy phép đã lưu."
                            feedbackEnabled: false
                            onClicked: root.appModel.activation.refresh()
                        }
                        Item { Layout.fillWidth: true }
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.appModel.activation.message
                        color: root.appModel.activation.hasError ? Theme.error
                            : (root.appModel.activation.active ? Theme.success : Theme.warning)
                        font.family: Theme.fontFamily
                        font.pixelSize: 12
                        wrapMode: Text.Wrap
                        textFormat: Text.PlainText
                        Accessible.name: "Trạng thái kích hoạt: " + text
                    }
                    Text {
                        Layout.fillWidth: true
                        visible: root.appModel.activation.licenseId.length > 0
                        text: "Mã giấy phép: " + root.appModel.activation.licenseId
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                    }
                    Text {
                        Layout.fillWidth: true
                        visible: root.appModel.activation.expiresAt.length > 0
                        text: "Hết hạn (UTC): " + root.appModel.activation.expiresAt
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                    }
                }
            }

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
                Layout.preferredHeight: 124
                RowLayout {
                    anchors.fill: parent
                    anchors.margins: Theme.space16
                    spacing: Theme.space12
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Theme.space4
                        SectionTitle { text: "Tối ưu dữ liệu dự án" }
                        Text {
                            Layout.fillWidth: true
                            text: root.appModel.databaseMaintenance.state === "completed"
                                ? "Đã nén "
                                    + root.appModel.databaseMaintenance.compressed_revisions
                                    + " revision. Bản sao an toàn sẽ được xóa sau lần health-check khởi động kế tiếp."
                                : (root.appModel.databaseMaintenance.state === "cancelled"
                                    ? "Đã hủy trước khi đổi file; database hiện tại không bị thay đổi."
                                    : "Nén lịch sử revision theo batch, xác minh SHA-256, foreign key và integrity_check rồi mới đổi file. Cache voice không bị xóa.")
                            color: Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: 11
                            wrapMode: Text.Wrap
                        }
                    }
                    StatusPill {
                        runtimeState: root.appModel.databaseMaintenance.state === "completed"
                            ? "ready" : "starting"
                        text: root.appModel.databaseMaintenanceBusy
                            ? (root.appModel.databaseMaintenance.state === "cancelling"
                                ? "Đang hủy" : "Đang tối ưu")
                            : (root.appModel.databaseMaintenance.state === "failed"
                                ? "Thất bại" : (root.appModel.databaseMaintenance.state === "completed"
                                    ? "Đã hoàn tất" : "Chưa chạy"))
                    }
                    DsButton {
                        text: root.appModel.databaseMaintenanceBusy
                            ? (root.appModel.databaseMaintenance.state === "cancelling"
                                ? "Đang hủy…" : "Hủy tối ưu")
                            : "Tối ưu dữ liệu dự án"
                        primary: !root.appModel.databaseMaintenanceBusy
                        enabled: root.appModel.backendReady
                            && (root.appModel.activation.active || root.appModel.databaseMaintenanceBusy)
                            && root.appModel.databaseMaintenance.state !== "cancelling"
                        disabledReason: root.appModel.databaseMaintenance.state === "cancelling"
                            ? "Đang chờ checkpoint an toàn trước khi đổi database."
                            : "Chỉ chạy khi không còn job hoặc Multi video hoạt động."
                        onClicked: {
                            if (root.appModel.databaseMaintenanceBusy)
                                root.appModel.cancelProjectDatabaseOptimization()
                            else
                                root.appModel.optimizeProjectDatabase()
                        }
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
                            enabled: root.appModel.activation.active
                            onClicked: root.appModel.showLegacyImportDialog()
                        }
                        DsButton {
                            text: "Nhập sau dry-run"
                            primary: true
                            enabled: root.appModel.activation.active
                                && root.appModel.legacyMigrationReport.dry_run === true
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
