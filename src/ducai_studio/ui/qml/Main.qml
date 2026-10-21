pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import DucaiStudio 1.0
import "controls"
import "screens"

ApplicationWindow {
    id: window

    required property var appModel
    property bool shutdownComplete: false
    property string parallelWarningMessage: ""
    readonly property bool featuresEnabled: window.appModel.activation.active

    function publicStatusMessage(value) {
        const message = String(value || "")
        if (message.indexOf("Đang tạo tác vụ Paddle Fast") >= 0)
            return "Đang tạo tác vụ trích xuất phụ đề"
        if (message.indexOf("Đang nạp Paddle OCR") >= 0)
            return "Đang nạp bộ máy trích xuất; giao diện vẫn có thể sử dụng"
        if (message.indexOf("Python của Paddle OCR không tồn tại") >= 0)
            return "Bộ máy trích xuất phụ đề chưa sẵn sàng. Hãy mở Cài đặt → Chẩn đoán runtime."
        if (message.indexOf("bật Web nổi") >= 0)
            return "Gemini Web yêu cầu xác minh và không thể hoàn tất ở chế độ ẩn. Hãy thử lại sau hoặc chọn Gemini API/CLI."
        return message
    }

    readonly property bool compactSidebar: width < 1320
    readonly property int sidebarWidth: compactSidebar ? 64 : 208
    readonly property var mediaNameFilters: [
        "Media và phụ đề (*.mp4 *.mkv *.mov *.avi *.webm *.mp3 *.wav *.m4a *.flac *.aac *.srt)",
        "Video (*.mp4 *.mkv *.mov *.avi *.webm)",
        "Audio (*.mp3 *.wav *.m4a *.flac *.aac)",
        "Phụ đề (*.srt)",
        "Tất cả file (*)"
    ]

    visible: true
    width: 1512
    height: 900
    minimumWidth: 1180
    minimumHeight: 720
    color: Theme.canvas
    font.family: Theme.fontFamily
    font.pixelSize: 13
    palette.window: Theme.surfaceBase
    palette.windowText: Theme.textPrimary
    palette.base: Theme.surfaceRaised
    palette.alternateBase: Theme.surfacePanel
    palette.text: Theme.textPrimary
    palette.button: Theme.surfaceRaised
    palette.buttonText: Theme.textPrimary
    palette.highlight: Theme.primary
    palette.highlightedText: Theme.textPrimary
    palette.mid: Theme.borderStrong
    palette.dark: Theme.borderSubtle
    palette.light: Theme.surfaceHover
    palette.placeholderText: Theme.textMuted
    title: "DUCAI STUDIO 2"

    onClosing: function(close) {
        if (!window.shutdownComplete) {
            close.accepted = false
            window.appModel.requestQuit()
        }
    }

    Shortcut { sequence: "Ctrl+1"; onActivated: window.appModel.navigate("dashboard") }
    function saveFocusedProject() {
        if (!window.featuresEnabled) return
        // Commit the focused text field before flushing its queued domain edit.
        window.contentItem.forceActiveFocus()
        Qt.callLater(window.appModel.saveProject)
    }
    Shortcut {
        sequences: [ StandardKey.Save ]
        context: Qt.WindowShortcut
        onActivated: window.saveFocusedProject()
    }
    function textEditorHasFocus() {
        const item = window.activeFocusItem
        return Boolean(item && ("cursorPosition" in item
            || "selectionStart" in item || "inputMethodComposing" in item))
    }
    Shortcut {
        sequence: "Ctrl+Z"
        context: Qt.WindowShortcut
        enabled: window.featuresEnabled && !window.textEditorHasFocus()
        onActivated: window.appModel.undoTimeline()
    }
    Shortcut {
        sequence: "Ctrl+Y"
        context: Qt.WindowShortcut
        enabled: window.featuresEnabled && !window.textEditorHasFocus()
        onActivated: window.appModel.redoTimeline()
    }
    Shortcut { sequence: "Ctrl+2"; onActivated: window.appModel.navigate("subtitles") }
    Shortcut { sequence: "Ctrl+3"; onActivated: window.appModel.navigate("voice") }
    Shortcut { sequence: "Ctrl+4"; onActivated: window.appModel.navigate("timeline") }
    Shortcut { sequence: "Ctrl+5"; onActivated: window.appModel.navigate("export") }

    Connections {
        target: window.appModel
        function onProjectAutosaveRequested() { window.saveFocusedProject() }
        function onStatusNotificationRequested(message, kind, durationMs) {
            UiFeedback.show(window.publicStatusMessage(message), kind, durationMs)
        }
        function onStatusNotificationDismissRequested() {
            actionToast.dismiss()
        }
        function onCurrentIndexChanged() {
            Qt.callLater(function() {
                const target = workspaceHost.children[window.appModel.currentIndex]
                if (target) target.forceActiveFocus()
            })
        }
        function onImportDialogRequested() {
            importDialog.open()
        }
        function onMultiImportDialogRequested() {
            multiImportDialog.open()
        }
        function onNewProjectDialogRequested() {
            newProjectDialog.open()
        }
        function onLegacyImportDialogRequested() {
            legacyImportDialog.open()
        }
        function onDuplicateMediaDialogRequested() {
            duplicateMediaDialog.open()
        }
        function onParallelJobWarningRequested(message) {
            window.parallelWarningMessage = message
            parallelJobDialog.open()
        }
    }

    Connections {
        target: window.appModel.updater
        function onInstallConfirmationRequested() {
            updateInstallDialog.open()
        }
    }

    Dialog {
        id: updateInstallDialog
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(520, window.width - Theme.space32)
        modal: true
        title: "Xác nhận cài đặt bản cập nhật"
        standardButtons: Dialog.NoButton

        contentItem: ColumnLayout {
            spacing: Theme.space16
            Text {
                Layout.fillWidth: true
                text: "DUCAI STUDIO sẽ xác nhận mọi tác vụ đã kết thúc, sau đó đóng và khởi động lại để cài v"
                    + window.appModel.updater.version + ". Nếu còn tác vụ, bản tải sẽ được giữ lại."
                color: Theme.textSecondary
                font.family: Theme.fontFamily
                font.pixelSize: 12
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
            }
            Text {
                Layout.fillWidth: true
                text: "Dự án, media, DUCAI_STUDIO_DATA, model và runtime ngoài không bị thay thế."
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: 11
                wrapMode: Text.Wrap
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                DsButton {
                    text: "Để sau"
                    feedbackEnabled: false
                    onClicked: updateInstallDialog.close()
                }
                DsButton {
                    text: "Cài đặt và khởi động lại"
                    primary: true
                    feedbackEnabled: false
                    onClicked: {
                        updateInstallDialog.close()
                        window.appModel.updater.installConfirmed()
                    }
                }
            }
        }
    }

    Dialog {
        id: parallelJobDialog
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(520, window.width - Theme.space32)
        modal: true
        title: "Tác vụ song song có thể làm máy chậm"
        standardButtons: Dialog.NoButton

        contentItem: ColumnLayout {
            spacing: Theme.space16
            Text {
                Layout.fillWidth: true
                text: window.parallelWarningMessage
                color: Theme.textSecondary
                font.family: Theme.fontFamily
                font.pixelSize: 12
                wrapMode: Text.Wrap
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                DsButton {
                    text: "Quay lại"
                    onClicked: {
                        window.appModel.resolveParallelJobWarning(false)
                        parallelJobDialog.close()
                    }
                }
                DsButton {
                    text: "Vẫn chạy"
                    warning: true
                    onClicked: {
                        window.appModel.resolveParallelJobWarning(true)
                        parallelJobDialog.close()
                    }
                }
            }
        }
        onRejected: window.appModel.resolveParallelJobWarning(false)
    }

    FileDialog {
        id: importDialog
        title: "Nhập video, audio hoặc phụ đề"
        fileMode: FileDialog.OpenFiles
        nameFilters: window.mediaNameFilters
        onAccepted: {
            const values = []
            for (let index = 0; index < selectedFiles.length; ++index)
                values.push(selectedFiles[index].toString())
            if (values.length <= 1)
                window.appModel.importPath(values.length === 1 ? values[0] : "")
            else
                window.appModel.importPaths(values)
        }
    }

    FileDialog {
        id: multiImportDialog
        title: "Nhập video vào Multi video"
        fileMode: FileDialog.OpenFiles
        nameFilters: ["Video (*.mp4 *.mkv *.mov *.avi *.webm)"]
        onAccepted: {
            const values = []
            for (let index = 0; index < selectedFiles.length; ++index)
                values.push(selectedFiles[index].toString())
            window.appModel.importMultiPaths(values)
        }
    }

    FileDialog {
        id: newProjectDialog
        title: "Tạo dự án mới từ media"
        fileMode: FileDialog.OpenFile
        nameFilters: window.mediaNameFilters
        onAccepted: window.appModel.createProjectFromPath(selectedFile.toString())
    }

    FileDialog {
        id: legacyImportDialog
        title: "Dry-run dự án DUCAI 0.1.99"
        fileMode: FileDialog.OpenFile
        nameFilters: ["Dự án JSON cũ (*.json *.ducai)", "Tất cả file (*)"]
        onAccepted: window.appModel.inspectLegacyProjectPath(selectedFile.toString())
    }

    Dialog {
        id: duplicateMediaDialog

        property bool decisionSubmitted: false
        property var info: window.appModel.duplicateMediaInfo
        property var firstUsage: info.usages && info.usages.length > 0 ? info.usages[0] : ({})

        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(560, window.width - Theme.space32)
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape
        padding: 0
        onOpened: {
            decisionSubmitted = false
            duplicateProjectName.text = String(info.suggested_project_name || "")
            if (info.intent === "new_project") duplicateProjectName.forceActiveFocus()
        }
        onClosed: {
            if (!decisionSubmitted) window.appModel.cancelDuplicateMediaResolution()
        }

        background: Rectangle {
            color: Theme.surfacePanel
            radius: Theme.radiusLarge
            border.width: 1
            border.color: Theme.borderStrong
        }

        header: Rectangle {
            implicitHeight: 60
            color: Theme.surfaceBase
            radius: Theme.radiusLarge

            Rectangle {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: 1
                color: Theme.borderSubtle
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: Theme.space16
                anchors.rightMargin: Theme.space8
                spacing: Theme.space8

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 0
                    Text {
                        text: "Media đã có trong dự án"
                        color: Theme.textPrimary
                        font.family: Theme.fontFamily
                        font.pixelSize: 15
                        font.weight: Font.DemiBold
                    }
                    Text {
                        Layout.fillWidth: true
                        text: String(duplicateMediaDialog.info.media_name || "")
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 10
                        elide: Text.ElideMiddle
                    }
                }

                DsButton {
                    text: "X"
                    compact: true
                    Accessible.name: "Đóng, không thay đổi dự án"
                    onClicked: duplicateMediaDialog.close()
                }
            }
        }

        contentItem: ColumnLayout {
            spacing: Theme.space16

            Item { Layout.preferredHeight: Theme.space4 }

            Text {
                Layout.fillWidth: true
                Layout.leftMargin: Theme.space16
                Layout.rightMargin: Theme.space16
                text: {
                    const projectName = String(duplicateMediaDialog.firstUsage.project_name || "")
                    const more = Math.max(0, Number(duplicateMediaDialog.info.usage_count || 1) - 1)
                    return "File này đang được dùng trong “" + projectName + "”"
                        + (more > 0 ? " và " + more + " dự án khác." : ".")
                }
                color: Theme.textSecondary
                font.family: Theme.fontFamily
                font.pixelSize: 13
                wrapMode: Text.Wrap
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.leftMargin: Theme.space16
                Layout.rightMargin: Theme.space16
                implicitHeight: usageColumn.implicitHeight + Theme.space24
                color: Theme.surfaceRaised
                radius: Theme.radiusMedium

                ColumnLayout {
                    id: usageColumn
                    anchors.fill: parent
                    anchors.margins: Theme.space12
                    spacing: Theme.space4
                    Text {
                        Layout.fillWidth: true
                        text: String(duplicateMediaDialog.firstUsage.project_name || "Dự án đã có")
                        color: Theme.textPrimary
                        font.family: Theme.fontFamily
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                        elide: Text.ElideRight
                    }
                    Text {
                        Layout.fillWidth: true
                        text: String(duplicateMediaDialog.firstUsage.project_path || "")
                        color: Theme.textMuted
                        font.family: Theme.timecodeFamily
                        font.pixelSize: 9
                        elide: Text.ElideMiddle
                    }
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                Layout.leftMargin: Theme.space16
                Layout.rightMargin: Theme.space16
                spacing: Theme.space8
                visible: duplicateMediaDialog.info.intent === "new_project"

                Text {
                    text: "Tên dự án mới"
                    color: Theme.textSecondary
                    font.family: Theme.fontFamily
                    font.pixelSize: 11
                }
                DsTextField {
                    id: duplicateProjectName
                    Layout.fillWidth: true
                    placeholderText: "Nhập tên dự án mới"
                    selectByMouse: true
                    Accessible.name: "Tên dự án mới"
                }
            }

            Text {
                Layout.fillWidth: true
                Layout.leftMargin: Theme.space16
                Layout.rightMargin: Theme.space16
                text: "Dùng bản đã có sẽ mở đúng project cũ. Tạo bản mới giữ nguyên project cũ và không sửa file media nguồn."
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: 10
                wrapMode: Text.Wrap
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: Theme.space16
                Layout.rightMargin: Theme.space16
                spacing: Theme.space8

                Item { Layout.fillWidth: true }
                DsButton {
                    text: duplicateMediaDialog.firstUsage.project_id === window.appModel.currentProject.projectId
                        ? "Giữ media hiện có" : "Dùng dự án đã có"
                    onClicked: {
                        duplicateMediaDialog.decisionSubmitted = true
                        window.appModel.resolveDuplicateMedia("use_existing", "")
                        duplicateMediaDialog.close()
                    }
                }
                DsButton {
                    text: duplicateMediaDialog.info.intent === "new_project"
                        ? "Tạo bản đổi tên" : "Vẫn thêm vào project này"
                    primary: true
                    enabled: duplicateMediaDialog.info.intent !== "new_project"
                        || duplicateProjectName.text.trim().length > 0
                    disabledReason: "Tên dự án mới không được để trống."
                    onClicked: {
                        duplicateMediaDialog.decisionSubmitted = true
                        window.appModel.resolveDuplicateMedia(
                            "create_copy", duplicateProjectName.text
                        )
                        duplicateMediaDialog.close()
                    }
                }
            }

            Item { Layout.preferredHeight: Theme.space4 }
        }
    }

    background: Rectangle { color: Theme.canvas }

    header: Rectangle {
        implicitHeight: 60
        color: Theme.surfaceBase
        border.width: 0

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 16
            anchors.rightMargin: 16
            spacing: 12

            Rectangle {
                Layout.preferredWidth: 30
                Layout.preferredHeight: 30
                radius: Theme.radiusMedium
                color: Theme.primary

                Text {
                    anchors.centerIn: parent
                    text: "D"
                    color: Theme.textPrimary
                    font.family: Theme.fontFamily
                    font.pixelSize: 16
                    font.weight: Font.Bold
                }
            }

            ColumnLayout {
                spacing: 0
                Text {
                    text: "DUCAI STUDIO"
                    color: Theme.textPrimary
                    font.family: Theme.fontFamily
                    font.pixelSize: 13
                    font.weight: Font.Bold
                }
                Text {
                    text: "VIDEO LOCALIZATION"
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: 9
                    font.letterSpacing: 1.1
                }
            }

            Rectangle {
                Layout.preferredWidth: 1
                Layout.preferredHeight: 30
                color: Theme.borderSubtle
            }

            ColumnLayout {
                Layout.minimumWidth: 160
                Layout.maximumWidth: 330
                spacing: 0
                Text {
                    Layout.fillWidth: true
                    text: window.appModel.projectName
                    color: Theme.textPrimary
                    font.family: Theme.fontFamily
                    font.pixelSize: 12
                    font.weight: Font.DemiBold
                    elide: Text.ElideMiddle
                }
                Text {
                    Layout.fillWidth: true
                    text: window.appModel.saveState
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: 10
                    elide: Text.ElideRight
                }
            }

            Item { Layout.fillWidth: true }

            DsButton {
                text: window.appModel.projectSaveBusy ? "Đang lưu…" : "Lưu"
                enabled: window.featuresEnabled && window.appModel.backendReady
                    && window.appModel.currentProject.hasProject
                tooltipText: "Lưu dự án (Ctrl+S). Tự lưu mỗi 5 phút khi có thay đổi."
                feedbackEnabled: false
                onClicked: window.saveFocusedProject()
            }

            StatusPill {
                runtimeState: window.appModel.runtimeState
                text: {
                    if (window.appModel.runtimeState === "ready") return "Lõi sẵn sàng"
                    if (window.appModel.runtimeState === "error") return "Lõi gặp lỗi"
                    if (window.appModel.runtimeState === "shutting_down") return "Đang dừng"
                    if (window.appModel.runtimeState === "stopped") return "Đã dừng"
                    return "Đang khởi động"
                }
            }

            DsButton {
                text: window.appModel.importBusy ? "Đang nhập..." : "Nhập media"
                enabled: window.featuresEnabled && window.appModel.backendReady
                    && !window.appModel.importBusy
                disabledReason: window.appModel.importBusy
                    ? "Một file đang được nhập."
                    : "Job Manager chưa sẵn sàng."
                onClicked: window.appModel.showImportDialog()
            }

            DsButton {
                text: "Xuất video"
                enabled: window.featuresEnabled
                disabledReason: "Hãy kích hoạt tại Cài đặt để sử dụng chức năng."
                primary: true
                onClicked: window.appModel.navigate("export")
            }
        }

        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            height: 1
            color: Theme.borderSubtle
        }
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0

        Rectangle {
            property int targetWidth: window.sidebarWidth

            Layout.preferredWidth: targetWidth
            Layout.minimumWidth: targetWidth
            Layout.maximumWidth: targetWidth
            Layout.fillHeight: true
            color: Theme.surfaceBase

            ListView {
                id: navigationList
                anchors.fill: parent
                anchors.topMargin: 10
                anchors.bottomMargin: 10
                anchors.leftMargin: 8
                anchors.rightMargin: 8
                spacing: 2
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                model: window.appModel.navigationModel
                currentIndex: -1

                delegate: NavItem {
                    required property var model

                    width: ListView.view.width
                    text: model.title
                    shortCode: model.shortCode
                    iconName: model.iconName
                    compact: window.compactSidebar
                    selected: window.appModel.currentWorkspace === model.workspaceId
                    opacity: window.featuresEnabled || model.workspaceId === "settings" ? 1 : 0.45
                    onClicked: window.appModel.navigate(model.workspaceId)
                }
            }

            Rectangle {
                anchors.top: parent.top
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                width: 1
                color: Theme.borderSubtle
            }
        }

        StackLayout {
            id: workspaceHost
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: window.appModel.currentIndex

            DashboardScreen { appModel: window.appModel; enabled: window.featuresEnabled }
            ProjectsScreen { appModel: window.appModel; enabled: window.featuresEnabled }
            SubtitleEditorScreen { appModel: window.appModel; enabled: window.featuresEnabled }
            VoiceStudioScreen { appModel: window.appModel; enabled: window.featuresEnabled }
            MultiVideoScreen { appModel: window.appModel; enabled: window.featuresEnabled }
            TimelineScreen { appModel: window.appModel; enabled: window.featuresEnabled }
            ExportScreen { appModel: window.appModel; enabled: window.featuresEnabled }
            SettingsScreen { appModel: window.appModel }
        }
    }

    ActionToast {
        id: actionToast
        anchors.top: parent.top
        anchors.horizontalCenter: parent.horizontalCenter
        z: 900
    }

    footer: Rectangle {
        implicitHeight: 36
        color: Theme.surfaceBase

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 12
            anchors.rightMargin: 12
            spacing: 8

            Rectangle {
                Layout.preferredWidth: 7
                Layout.preferredHeight: 7
                radius: 4
                color: window.appModel.runtimeState === "ready"
                    ? Theme.success
                    : (window.appModel.runtimeState === "error" ? Theme.error : Theme.warning)
            }
            Text {
                Layout.fillWidth: true
                text: window.publicStatusMessage(window.appModel.statusMessage)
                color: Theme.textSecondary
                font.family: Theme.fontFamily
                font.pixelSize: 10
                elide: Text.ElideRight
            }
            Text {
                text: "v" + window.appModel.buildVersion
                color: Theme.textMuted
                font.family: Theme.timecodeFamily
                font.pixelSize: 10
            }
        }

        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            height: 1
            color: Theme.borderSubtle
        }
    }
}
