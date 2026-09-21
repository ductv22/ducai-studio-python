pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import QtMultimedia
import DucaiStudio 1.0
import "../controls"

Item {
    id: root
    objectName: "exportScreen"

    property var appModel
    property string selectedBlurId: "overlay-default-blur"
    property var blurOverlays: {
        const revision = appModel.currentProject.revision
        return appModel.currentProject.overlays.filter(function(item) {
            return item.kind === "blur"
        })
    }
    property var blurOverlay: {
        const revision = appModel.currentProject.revision
        const selected = appModel.currentProject.overlay(selectedBlurId)
        return selected && selected.id
            ? selected
            : appModel.currentProject.overlay("overlay-default-blur")
    }
    property var logoOverlay: {
        const revision = appModel.currentProject.revision
        return appModel.currentProject.overlay("overlay-default-logo")
    }
    readonly property string workspaceMode: appModel.currentWorkspace
    readonly property string workspaceTitle: workspaceMode === "effects"
        ? "Hiệu ứng"
        : (workspaceMode === "mixer"
            ? "Trộn âm"
            : (workspaceMode === "queue" ? "Hàng đợi" : "Xuất video"))
    readonly property string workspaceDescription: workspaceMode === "effects"
        ? "Chỉnh phụ đề, filter màu, logo và blur; preview và export dùng cùng thông số."
        : (workspaceMode === "mixer"
            ? "Điều chỉnh A1, D1 và nhạc nền trên cùng hợp đồng âm lượng của preview/export."
            : (workspaceMode === "queue"
                ? "Theo dõi tiến độ, thời gian chạy, ETA và tiếp tục checkpoint của FFmpeg."
                : "Chọn preset, phụ đề, codec và tạo MP4 qua cổng QA cuối."))
    activeFocusOnTab: true
    Accessible.name: workspaceTitle

    FileDialog {
        id: outputDialog
        title: "Chọn file MP4 hoàn chỉnh"
        fileMode: FileDialog.SaveFile
        defaultSuffix: "mp4"
        nameFilters: ["Video MP4 (*.mp4)"]
        onAccepted: root.appModel.setExportOutputPath(selectedFile.toString())
    }

    FileDialog {
        id: logoDialog
        title: "Chọn logo"
        fileMode: FileDialog.OpenFile
        nameFilters: ["Hình ảnh (*.png *.jpg *.jpeg *.webp *.bmp)", "Tất cả file (*)"]
        onAccepted: root.appModel.setLogoPath(selectedFile.toString())
    }

    ColorDialog {
        id: effectsSubtitleTextColorDialog
        title: "Chọn màu chữ phụ đề"
        onAccepted: root.appModel.setExportSubtitlePrimaryColor(
            selectedColor.toString()
        )
    }

    ColorDialog {
        id: effectsSubtitleOutlineColorDialog
        title: "Chọn màu viền phụ đề"
        onAccepted: root.appModel.setExportSubtitleOutlineColor(
            selectedColor.toString()
        )
    }

    MediaPlayer {
        id: outputPlayer
        source: root.appModel.lastExportSource
        audioOutput: outputAudio
        videoOutput: outputVideo
    }
    AudioOutput { id: outputAudio; volume: 0.9 }

    function overlayNumber(overlay, key, fallback) {
        const value = overlay ? overlay[key] : undefined
        return value === undefined || value === null ? fallback : Number(value)
    }

    function setGeometry(overlay, key, percent) {
        if (!overlay || !overlay.id) return
        let x = overlayNumber(overlay, "x", 0)
        let y = overlayNumber(overlay, "y", 0)
        let width = overlayNumber(overlay, "width", 0.1)
        let height = overlayNumber(overlay, "height", 0.1)
        const normalized = Math.max(0, Math.min(100, percent)) / 100
        if (key === "x") x = normalized
        else if (key === "y") y = normalized
        else if (key === "width") width = normalized
        else height = normalized
        root.appModel.setOverlayGeometry(overlay.id, x, y, width, height)
    }

    function filterPresetIndex(value) {
        const presets = ["none", "cinematic-warm", "cool", "grayscale"]
        return Math.max(0, presets.indexOf(value))
    }

    function filterPresetId(index) {
        const presets = ["none", "cinematic-warm", "cool", "grayscale"]
        return presets[Math.max(0, Math.min(presets.length - 1, index))]
    }

    function focusWorkspace() {
        if (workspaceMode === "queue") {
            queuePanel.forceActiveFocus()
            return
        }
        const target = workspaceMode === "effects"
            ? effectsSubtitleTitle
            : (workspaceMode === "mixer" ? mixerTitle : exportHeader)
        const flickable = settingsScroll.contentItem
        if (flickable && target)
            flickable.contentY = Math.max(0, target.y - Theme.space8)
    }

    onWorkspaceModeChanged: Qt.callLater(focusWorkspace)
    Component.onCompleted: Qt.callLater(focusWorkspace)

    Connections {
        target: root.appModel
        function onOverlaySelectionRequested(overlayId) {
            root.selectedBlurId = overlayId
            effectsPreview.selectedOverlayId = overlayId
        }
    }

    Timer {
        id: persistWidth
        interval: 250
        onTriggered: root.appModel.setUiRealValue(
            "exportSettingsRatio", settingsPane.width / Math.max(1, root.width)
        )
    }

    Panel {
        id: workflowHeader
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: Theme.space12
        height: 72
        z: 1

        RowLayout {
            anchors.fill: parent
            anchors.margins: Theme.space12
            spacing: Theme.space12

            ColumnLayout {
                Layout.fillWidth: true
                spacing: Theme.space4
                SectionTitle { text: root.workspaceTitle }
                Text {
                    Layout.fillWidth: true
                    text: root.workspaceDescription
                    color: Theme.textSecondary
                    font.family: Theme.fontFamily
                    font.pixelSize: 10
                    elide: Text.ElideRight
                }
            }

            DsButton {
                text: "Hiệu ứng"
                compact: true
                primary: root.workspaceMode === "effects"
                onClicked: root.appModel.navigate("effects")
            }
            DsButton {
                text: "Trộn âm"
                compact: true
                primary: root.workspaceMode === "mixer"
                onClicked: root.appModel.navigate("mixer")
            }
            DsButton {
                text: "Xuất video"
                compact: true
                primary: root.workspaceMode === "export"
                onClicked: root.appModel.navigate("export")
            }
            DsButton {
                text: "Hàng đợi"
                compact: true
                primary: root.workspaceMode === "queue"
                onClicked: root.appModel.navigate("queue")
            }
        }
    }

    SplitView {
        anchors.fill: parent
        anchors.margins: Theme.space12
        anchors.topMargin: 96
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

        Panel {
            id: settingsPane
            visible: root.workspaceMode !== "queue"
            SplitView.preferredWidth: root.width * root.appModel.uiRealValue(
                "exportSettingsRatio", 0.38
            )
            SplitView.minimumWidth: 360
            SplitView.maximumWidth: 520
            onWidthChanged: persistWidth.restart()

            ScrollView {
                id: settingsScroll
                anchors.fill: parent
                anchors.margins: Theme.space12
                clip: true
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

                ColumnLayout {
                    width: settingsScroll.availableWidth
                    spacing: Theme.space12

                    RowLayout {
                        id: exportHeader
                        Layout.fillWidth: true
                        SectionTitle { text: "Thiết lập xuất" }
                        Item { Layout.fillWidth: true }
                        StatusPill {
                            text: root.appModel.exportBusy ? "Đang render" : "Sẵn sàng"
                            runtimeState: root.appModel.exportBusy ? "starting" : "ready"
                        }
                    }

                    Text {
                        Layout.fillWidth: true
                        text: "Snapshot bất biến; blur, logo, sub và mixer được mã hóa cùng một lượt mỗi checkpoint."
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                        wrapMode: Text.Wrap
                    }

                    GridLayout {
                        Layout.fillWidth: true
                        columns: 2
                        columnSpacing: Theme.space8
                        rowSpacing: Theme.space4
                        Text { text: "Chất lượng"; color: Theme.textSecondary; font.pixelSize: 11 }
                        ComboBox {
                            Layout.fillWidth: true
                            model: ["source-quality", "1080p", "720p", "480p"]
                            currentIndex: Math.max(0, model.indexOf(root.appModel.exportPresetId))
                            onActivated: root.appModel.setExportPresetId(currentText)
                            Accessible.name: "Preset xuất"
                        }
                        Text { text: "Mã hóa"; color: Theme.textSecondary; font.pixelSize: 11 }
                        ComboBox {
                            Layout.fillWidth: true
                            model: ["auto", "nvenc", "cpu"]
                            currentIndex: Math.max(0, model.indexOf(root.appModel.exportBackend))
                            onActivated: root.appModel.setExportBackend(currentText)
                            Accessible.name: "Backend xuất"
                        }
                        Text { text: "Checkpoint"; color: Theme.textSecondary; font.pixelSize: 11 }
                        DsSpinBox {
                            id: exportCheckpointSpinBox
                            objectName: "exportCheckpointSpinBox"
                            accessibleName: "Số phút mỗi checkpoint xuất"
                            from: 1
                            to: 60
                            value: root.appModel.exportChunkMinutes
                            editable: true
                            onValueModified: root.appModel.setExportChunkMinutes(value)
                        }
                    }

                    RowLayout {
                        id: outputPathRow
                        Layout.fillWidth: true
                        DsTextField {
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            text: root.appModel.exportOutputPath
                            placeholderText: "Đường dẫn MP4 đầu ra"
                            Accessible.name: "Đường dẫn MP4 đầu ra"
                            onEditingFinished: root.appModel.setExportOutputPath(text)
                        }
                        DsButton {
                            objectName: "exportOutputPicker"
                            text: "Chọn"
                            compact: true
                            onClicked: outputDialog.open()
                        }
                    }
                    CheckBox {
                        text: "Cho phép ghi đè file đã tồn tại"
                        checked: root.appModel.exportOverwrite
                        onClicked: root.appModel.setExportOverwrite(checked)
                        Accessible.name: text
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 1
                        color: Theme.borderSubtle
                    }
                    SectionTitle { id: effectsSubtitleTitle; text: "Phụ đề Việt" }
                    CheckBox {
                        text: "Đóng cứng phụ đề vào video"
                        checked: root.appModel.exportSubtitleEnabled
                        onClicked: root.appModel.setExportSubtitleEnabled(checked)
                        Accessible.name: text
                    }
                    GridLayout {
                        Layout.fillWidth: true
                        columns: 2
                        Text { text: "Font"; color: Theme.textSecondary; font.pixelSize: 11 }
                        DsTextField {
                            Layout.fillWidth: true
                            text: root.appModel.exportFontFamily
                            onEditingFinished: root.appModel.setExportFontFamily(text)
                        }
                        Text { text: "Cỡ chữ"; color: Theme.textSecondary; font.pixelSize: 11 }
                        DsSpinBox {
                            accessibleName: "Cỡ chữ phụ đề"
                            from: 12; to: 120; value: root.appModel.exportFontSize
                            onValueModified: root.appModel.setExportFontSize(value)
                        }
                        Text { text: "Cách đáy"; color: Theme.textSecondary; font.pixelSize: 11 }
                        DsSpinBox {
                            accessibleName: "Khoảng cách phụ đề với đáy"
                            from: 0; to: 500; value: root.appModel.exportSubtitleMarginBottom
                            onValueModified: root.appModel.setExportSubtitleMarginBottom(value)
                        }
                        Text { text: "Màu chữ"; color: Theme.textSecondary; font.pixelSize: 11 }
                        DsButton {
                            Layout.fillWidth: true
                            compact: true
                            text: root.appModel.exportSubtitlePrimaryColor
                            onClicked: {
                                effectsSubtitleTextColorDialog.selectedColor
                                    = root.appModel.exportSubtitlePrimaryColor
                                effectsSubtitleTextColorDialog.open()
                            }
                        }
                        Text { text: "Màu viền"; color: Theme.textSecondary; font.pixelSize: 11 }
                        DsButton {
                            Layout.fillWidth: true
                            compact: true
                            text: root.appModel.exportSubtitleOutlineColor
                            onClicked: {
                                effectsSubtitleOutlineColorDialog.selectedColor
                                    = root.appModel.exportSubtitleOutlineColor
                                effectsSubtitleOutlineColorDialog.open()
                            }
                        }
                        Text { text: "Độ dày viền"; color: Theme.textSecondary; font.pixelSize: 11 }
                        DsSpinBox {
                            Layout.fillWidth: true
                            accessibleName: "Độ dày viền phụ đề"
                            from: 0; to: 12
                            value: root.appModel.exportSubtitleOutlineWidth
                            onValueModified: root.appModel.setExportSubtitleOutlineWidth(value)
                        }
                        Text { text: "Độ mờ (%)"; color: Theme.textSecondary; font.pixelSize: 11 }
                        DsSpinBox {
                            Layout.fillWidth: true
                            accessibleName: "Độ mờ phụ đề"
                            from: 0; to: 100
                            value: root.appModel.exportSubtitleOpacityPercent
                            onValueModified: root.appModel.setExportSubtitleOpacityPercent(value)
                        }
                        Text { text: "Căn ngang"; color: Theme.textSecondary; font.pixelSize: 11 }
                        ComboBox {
                            Layout.fillWidth: true
                            model: ["Trái", "Giữa", "Phải"]
                            currentIndex: root.appModel.exportSubtitleAlignment - 1
                            onActivated: root.appModel.setExportSubtitleAlignment(currentIndex + 1)
                            Accessible.name: "Căn ngang phụ đề"
                        }
                    }

                    DsButton {
                        Layout.fillWidth: true
                        text: "Khôi phục kiểu phụ đề"
                        onClicked: root.appModel.resetExportSubtitleStyle()
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 1
                        color: Theme.borderSubtle
                    }
                    RowLayout {
                        id: logoHeader
                        Layout.fillWidth: true
                        SectionTitle { text: "Logo" }
                        Item { Layout.fillWidth: true }
                        DsButton { text: "Chọn logo"; compact: true; onClicked: logoDialog.open() }
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.logoOverlay.asset_id
                            ? root.appModel.currentProject.mediaName(root.logoOverlay.asset_id)
                            : "Chưa chọn logo"
                        color: root.logoOverlay.asset_id ? Theme.textSecondary : Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 10
                        elide: Text.ElideMiddle
                    }
                    CheckBox {
                        text: "Bật logo toàn timeline"
                        checked: Boolean(root.logoOverlay.enabled)
                        enabled: Boolean(root.logoOverlay.asset_id)
                        onClicked: root.appModel.setOverlayEnabled(root.logoOverlay.id, checked)
                        Accessible.name: text
                    }
                    Text {
                        Layout.fillWidth: true
                        text: "Kéo logo trực tiếp trên khung preview để đặt vị trí và kéo nút góc để đổi kích thước."
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 10
                        wrapMode: Text.Wrap
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Text { text: "Độ mờ"; color: Theme.textSecondary; font.pixelSize: 11 }
                        DsSpinBox {
                            accessibleName: "Độ mờ logo"
                            from: 0; to: 100
                            value: Math.round(root.overlayNumber(root.logoOverlay, "opacity", 0.9) * 100)
                            onValueModified: root.appModel.setOverlayOpacity(
                                root.logoOverlay.id, value / 100
                            )
                        }
                        Item { Layout.fillWidth: true }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 1
                        color: Theme.borderSubtle
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        SectionTitle { text: "Che phụ đề cứng / Blur" }
                        Item { Layout.fillWidth: true }
                        DsButton {
                            text: "+ Thêm blur"
                            compact: true
                            enabled: !root.appModel.timelineBusy
                            disabledReason: "Đang lưu một thay đổi hiệu ứng."
                            onClicked: root.appModel.addBlurRegion()
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Text {
                            text: "Vùng đang chỉnh"
                            color: Theme.textSecondary
                            font.pixelSize: 11
                        }
                        ComboBox {
                            id: blurRegionPicker
                            Layout.fillWidth: true
                            model: root.blurOverlays
                            textRole: "id"
                            currentIndex: Math.max(0, root.blurOverlays.findIndex(
                                function(item) { return item.id === root.blurOverlay.id }
                            ))
                            onActivated: {
                                const selected = root.blurOverlays[currentIndex]
                                if (!selected) return
                                root.selectedBlurId = selected.id
                                effectsPreview.selectedOverlayId = selected.id
                            }
                        }
                        DsButton {
                            text: "Xóa vùng"
                            compact: true
                            danger: true
                            enabled: root.blurOverlay.id !== "overlay-default-blur"
                                && !root.appModel.timelineBusy
                            disabledReason: "Vùng mặc định chỉ có thể tắt."
                            onClicked: {
                                root.appModel.deleteOverlay(root.blurOverlay.id)
                                root.selectedBlurId = "overlay-default-blur"
                                effectsPreview.selectedOverlayId = root.selectedBlurId
                            }
                        }
                    }
                    CheckBox {
                        text: "Bật blur toàn timeline"
                        checked: Boolean(root.blurOverlay.enabled)
                        onClicked: root.appModel.setOverlayEnabled(root.blurOverlay.id, checked)
                        Accessible.name: text
                    }
                    Text {
                        Layout.fillWidth: true
                        text: "Kéo vùng blur trong preview. Vị trí và kích thước sẽ được giữ nguyên trong phạm vi video."
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 10
                        wrapMode: Text.Wrap
                    }
                    GridLayout {
                        Layout.fillWidth: true
                        columns: 2
                        columnSpacing: Theme.space8
                        rowSpacing: Theme.space8
                        Text { text: "Bán kính"; color: Theme.textSecondary; font.pixelSize: 11 }
                        DsSpinBox {
                            Layout.fillWidth: true
                            accessibleName: "Bán kính blur"
                            from: 1; to: 100
                            value: root.overlayNumber(root.blurOverlay, "blur_radius", 22)
                            onValueModified: root.appModel.setOverlayBlurRadius(
                                root.blurOverlay.id, value
                            )
                        }
                        Text { text: "Bắt đầu"; color: Theme.textSecondary; font.pixelSize: 11 }
                        DsSpinBox {
                            id: blurStart
                            Layout.fillWidth: true
                            accessibleName: "Thời điểm bắt đầu blur"
                            from: 0
                            to: Math.max(1, Math.ceil(root.appModel.currentProject.durationUs / 1000000))
                            value: Math.round(root.overlayNumber(
                                root.blurOverlay.range || {}, "start_us", 0
                            ) / 1000000)
                            onValueModified: root.appModel.setOverlayRange(
                                root.blurOverlay.id, value, blurEnd.value
                            )
                        }
                        Text { text: "Kết thúc"; color: Theme.textSecondary; font.pixelSize: 11 }
                        DsSpinBox {
                            id: blurEnd
                            Layout.fillWidth: true
                            accessibleName: "Thời điểm kết thúc blur"
                            from: 1
                            to: Math.max(1, Math.ceil(root.appModel.currentProject.durationUs / 1000000))
                            value: Math.round(root.overlayNumber(
                                root.blurOverlay.range || {}, "end_us",
                                root.appModel.currentProject.durationUs
                            ) / 1000000)
                            onValueModified: root.appModel.setOverlayRange(
                                root.blurOverlay.id, blurStart.value, value
                            )
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 1
                        color: Theme.borderSubtle
                    }
                    SectionTitle { id: filterTitle; text: "Filter màu" }
                    Text {
                        Layout.fillWidth: true
                        text: "Filter được áp dụng tức thì trên preview và mã hóa thật khi xuất."
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 10
                        wrapMode: Text.Wrap
                    }
                    GridLayout {
                        Layout.fillWidth: true
                        columns: 2
                        columnSpacing: Theme.space8
                        rowSpacing: Theme.space8

                        Text { text: "Preset"; color: Theme.textSecondary; font.pixelSize: 11 }
                        ComboBox {
                            Layout.fillWidth: true
                            model: ["Không filter", "Ấm điện ảnh", "Lạnh", "Đen trắng"]
                            currentIndex: root.filterPresetIndex(
                                root.appModel.exportVideoFilterPreset
                            )
                            onActivated: root.appModel.setExportVideoFilterPreset(
                                root.filterPresetId(currentIndex)
                            )
                            Accessible.name: "Preset filter video"
                        }
                        Text { text: "Cường độ (%)"; color: Theme.textSecondary; font.pixelSize: 11 }
                        DsSpinBox {
                            Layout.fillWidth: true
                            accessibleName: "Cường độ filter"
                            from: 0; to: 100
                            value: root.appModel.exportVideoFilterIntensity
                            onValueModified: root.appModel.setExportVideoFilterIntensity(value)
                        }
                        Text { text: "Độ sáng"; color: Theme.textSecondary; font.pixelSize: 11 }
                        DsSpinBox {
                            Layout.fillWidth: true
                            accessibleName: "Độ sáng video"
                            from: -50; to: 50
                            value: root.appModel.exportVideoBrightness
                            onValueModified: root.appModel.setExportVideoBrightness(value)
                        }
                        Text { text: "Tương phản (%)"; color: Theme.textSecondary; font.pixelSize: 11 }
                        DsSpinBox {
                            Layout.fillWidth: true
                            accessibleName: "Độ tương phản video"
                            from: 50; to: 150
                            value: root.appModel.exportVideoContrast
                            onValueModified: root.appModel.setExportVideoContrast(value)
                        }
                        Text { text: "Bão hòa (%)"; color: Theme.textSecondary; font.pixelSize: 11 }
                        DsSpinBox {
                            Layout.fillWidth: true
                            accessibleName: "Độ bão hòa video"
                            from: 0; to: 200
                            value: root.appModel.exportVideoSaturation
                            onValueModified: root.appModel.setExportVideoSaturation(value)
                        }
                    }
                    DsButton {
                        Layout.fillWidth: true
                        text: "Khôi phục filter"
                        onClicked: root.appModel.resetExportVideoFilter()
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 1
                        color: Theme.borderSubtle
                    }
                    SectionTitle { id: mixerTitle; text: "Mixer âm thanh" }
                    Repeater {
                        model: [
                            {"id": "A1", "name": "Âm thanh gốc"},
                            {"id": "D1", "name": "Thuyết minh"},
                            {"id": "A2", "name": "Nhạc nền"}
                        ]
                        delegate: RowLayout {
                            required property var modelData
                            property var trackData: {
                                const revision = root.appModel.currentProject.revision
                                return root.appModel.currentProject.track(modelData.id)
                            }
                            Layout.fillWidth: true
                            Text {
                                Layout.preferredWidth: 105
                                text: parent.modelData.name
                                color: Theme.textSecondary
                                font.pixelSize: 11
                                elide: Text.ElideRight
                            }
                            DsSpinBox {
                                Layout.fillWidth: true
                                Layout.minimumWidth: 108
                                accessibleName: "Âm lượng " + parent.modelData.name
                                from: 0
                                to: 200
                                value: {
                                    const revision = root.appModel.currentProject.revision
                                    return Math.round(
                                        root.appModel.currentProject.trackVolumePercent(
                                            parent.modelData.id
                                        )
                                    )
                                }
                                enabled: Boolean(parent.trackData.id)
                                onValueModified: root.appModel.setTrackVolumePercent(
                                    parent.modelData.id, value
                                )
                            }
                            CheckBox {
                                text: "Tắt"
                                checked: Boolean(parent.trackData.muted)
                                enabled: Boolean(parent.trackData.id)
                                onClicked: root.appModel.setTrackMuted(
                                    parent.modelData.id, checked
                                )
                                Accessible.name: "Tắt " + parent.modelData.name
                            }
                        }
                    }

                    DsButton {
                        Layout.fillWidth: true
                        text: "Thêm vào hàng đợi"
                        primary: true
                        operationState: root.appModel.exportBusy
                            ? (root.appModel.exportJobState || "start_requested")
                            : (root.appModel.exportJobState === "failed"
                                || root.appModel.exportJobState === "interrupted"
                                ? "failed" : root.appModel.lastExportSource ? "succeeded" : "idle")
                        runningText: "Đang xuất video…"
                        queuedText: "Xuất đang chờ…"
                        startingText: "Đang chuẩn bị xuất…"
                        pausedText: "Xuất đã tạm dừng"
                        resumingText: "Đang tiếp tục xuất…"
                        cancellingText: "Đang hủy xuất…"
                        succeededText: "Xuất video hoàn tất"
                        failedText: "Xuất video gặp lỗi"
                        feedbackText: "Đang bắt đầu xuất video"
                        enabled: !root.appModel.exportBusy
                            && root.appModel.currentProject.hasVideo
                            && Boolean(root.appModel.exportOutputPath)
                        disabledReason: "Cần video, đường dẫn đầu ra và không có render đang chạy."
                        onClicked: root.appModel.startExport()
                    }
                    GridLayout {
                        id: exportJobActions
                        Layout.fillWidth: true
                        columns: 2
                        columnSpacing: Theme.space8
                        rowSpacing: Theme.space8
                        DsButton {
                            Layout.fillWidth: true
                            text: "Tạm dừng"
                            warning: true
                            operationState: root.appModel.exportJobState === "pause_requested"
                                ? "pause_requested" : "idle"
                            pausingText: "Đang tạm dừng xuất…"
                            feedbackText: "Đang yêu cầu tạm dừng xuất video"
                            enabled: root.appModel.exportJobState === "running"
                            disabledReason: root.appModel.exportJobState === "start_requested"
                                ? "Đang chờ worker xác nhận bắt đầu; nút sẽ bật khi render chạy."
                                : "Chỉ có thể tạm dừng khi export đang chạy."
                            onClicked: root.appModel.pauseExport()
                        }
                        DsButton {
                            Layout.fillWidth: true
                            text: "Tiếp tục cache"
                            operationState: ["start_requested", "resume_requested"]
                                .indexOf(root.appModel.exportJobState) >= 0
                                ? root.appModel.exportJobState : "idle"
                            startingText: "Đang bắt đầu xuất…"
                            resumingText: "Đang tiếp tục xuất…"
                            feedbackText: "Đang tiếp tục xuất từ cache"
                            enabled: ["paused", "interrupted", "failed", "queued"]
                                .indexOf(root.appModel.exportJobState) >= 0
                            onClicked: root.appModel.resumeExport()
                        }
                        DsButton {
                            objectName: "exportCancelButton"
                            Layout.columnSpan: 2
                            Layout.fillWidth: true
                            text: "Hủy"
                            danger: true
                            operationState: root.appModel.exportJobState === "cancel_requested"
                                ? "cancel_requested" : "idle"
                            cancellingText: "Đang hủy xuất…"
                            feedbackText: "Đang yêu cầu hủy xuất video"
                            enabled: root.appModel.exportBusy
                            onClicked: root.appModel.cancelExport()
                        }
                    }
                }
            }
        }

        ColumnLayout {
            SplitView.fillWidth: true
            SplitView.minimumWidth: 520
            spacing: Theme.space12

            Panel {
                id: outputPreviewPanel
                Layout.fillWidth: true
                Layout.preferredHeight: Math.max(250, root.height * 0.46)
                visible: root.workspaceMode !== "queue"
                color: Theme.canvas
                TimelinePreview {
                    id: effectsPreview
                    objectName: "effectsLivePreview"
                    anchors.fill: parent
                    visible: root.workspaceMode === "effects"
                    appModel: root.appModel
                    showProjectOverlays: true
                    editableOverlays: true
                    selectedOverlayId: root.selectedBlurId
                    onSelectedOverlayIdChanged: {
                        const overlay = root.appModel.currentProject.overlay(selectedOverlayId)
                        if (overlay.kind === "blur") root.selectedBlurId = selectedOverlayId
                    }
                }
                VideoOutput {
                    id: outputVideo
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.bottom: outputTransport.top
                    fillMode: VideoOutput.PreserveAspectFit
                    visible: root.workspaceMode !== "effects"
                }
                EmptyState {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.bottom: outputTransport.top
                    visible: root.workspaceMode !== "effects"
                        && !Boolean(root.appModel.lastExportSource)
                    title: "Preview đầu ra"
                    description: "Sau khi QA PASS, video hoàn chỉnh phát trực tiếp tại đây."
                    iconName: "panels-top-left"
                }
                Rectangle {
                    id: outputTransport
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    height: 46
                    visible: root.workspaceMode !== "effects"
                    color: Theme.surfaceBase
                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: Theme.space8
                        DsButton {
                            text: outputPlayer.playbackState === MediaPlayer.PlayingState
                                ? "Tạm dừng" : "Phát thành phẩm"
                            compact: true
                            feedbackEnabled: false
                            enabled: Boolean(root.appModel.lastExportSource)
                            onClicked: {
                                const wasPlaying = outputPlayer.playbackState
                                    === MediaPlayer.PlayingState
                                if (wasPlaying) {
                                    outputPlayer.pause()
                                    UiFeedback.show("Đã tạm dừng thành phẩm", "info", 3000)
                                } else {
                                    outputPlayer.play()
                                    UiFeedback.show("Đang phát thành phẩm", "info", 3000)
                                }
                            }
                        }
                        Slider {
                            Layout.fillWidth: true
                            from: 0; to: Math.max(1, outputPlayer.duration)
                            value: outputPlayer.position
                            enabled: Boolean(root.appModel.lastExportSource)
                            onMoved: outputPlayer.setPosition(value)
                        }
                        DsButton {
                            text: outputAudio.muted ? "Bật tiếng" : "Tắt tiếng"
                            compact: true
                            enabled: Boolean(root.appModel.lastExportSource)
                            onClicked: outputAudio.muted = !outputAudio.muted
                        }
                    }
                }
            }

            SplitView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                orientation: Qt.Horizontal
                handle: Rectangle {
                    implicitWidth: 8
                    color: Theme.canvas
                    Rectangle {
                        anchors.centerIn: parent
                        width: 1
                        height: parent.height
                        color: Theme.borderSubtle
                    }
                }
                Panel {
                    id: queuePanel
                    SplitView.fillWidth: true
                    SplitView.minimumWidth: 280
                    JobActivityPanel {
                        anchors.fill: parent
                        anchors.margins: Theme.space8
                        jobModel: root.appModel.exportJobModel
                        title: "Hàng đợi render"
                        emptyTitle: "Hàng đợi trống"
                        emptyDescription: "Progress, elapsed và ETA lấy từ FFmpeg thật."
                    }
                }
                Panel {
                    SplitView.fillWidth: true
                    SplitView.minimumWidth: 240
                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: Theme.space12
                        SectionTitle { text: "Checklist chất lượng" }
                        StatusPill {
                            text: root.appModel.lastExportSource ? "PASS" : "Chưa có kết quả"
                            runtimeState: root.appModel.lastExportSource ? "ready" : "starting"
                        }
                        Text {
                            Layout.fillWidth: true
                            text: root.appModel.lastExportSource
                                ? "✓ File MP4 đã finalize nguyên tử\n✓ ffprobe thấy video/audio\n✓ duration đúng snapshot\n✓ SHA-256 khớp báo cáo QA"
                                : "Không đánh dấu PASS trước khi file playable và báo cáo QA khớp checksum."
                            color: Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: 11
                            wrapMode: Text.Wrap
                        }
                        Text {
                            Layout.fillWidth: true
                            text: root.appModel.lastExportQaPath
                            color: Theme.textMuted
                            font.family: Theme.timecodeFamily
                            font.pixelSize: 9
                            elide: Text.ElideMiddle
                        }
                        Item { Layout.fillHeight: true }
                    }
                }
            }
        }
    }
}
