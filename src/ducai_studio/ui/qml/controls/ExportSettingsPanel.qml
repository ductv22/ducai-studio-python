pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import DucaiStudio 1.0

ScrollView {
    id: root
    objectName: "timelineExportSettings"
    clip: true
    contentWidth: availableWidth
    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
    property var appModel
    property string selectedBlurId: "overlay-default-blur"
    property var automaticBlurOverlay: {
        const revision = appModel.currentProject.revision
        return appModel.currentProject.overlay("overlay-default-blur")
    }
    property var blurOverlays: {
        const revision = appModel.currentProject.revision
        return appModel.currentProject.overlays.filter(function(item) {
            return item.kind === "blur" && item.tracking_mode === "fixed"
        })
    }
    property var watermarkBlurOverlays: {
        const revision = appModel.currentProject.revision
        return appModel.currentProject.overlays.filter(function(item) {
            return item.kind === "blur" && item.tracking_mode === "ocr_watermark"
        })
    }
    property var blurOverlay: {
        const revision = appModel.currentProject.revision
        const selected = appModel.currentProject.overlay(selectedBlurId)
        if (selected && selected.id && selected.tracking_mode === "fixed")
            return selected
        return blurOverlays.length > 0 ? blurOverlays[0] : ({})
    }
    property var logoOverlay: {
        const revision = appModel.currentProject.revision
        return appModel.currentProject.overlay("overlay-default-logo")
    }

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


    Connections {
        target: root.appModel
        function onOverlaySelectionRequested(overlayId) {
            root.selectedBlurId = overlayId
        }
    }

ColumnLayout {
    width: root.availableWidth
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
        Text { text: "Chất lượng"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
        ComboBox {
            Layout.fillWidth: true
            model: ["source-quality", "1080p", "720p", "480p"]
            palette.dark: Theme.textSecondary
            currentIndex: Math.max(0, model.indexOf(root.appModel.exportPresetId))
            onActivated: root.appModel.setExportPresetId(currentText)
            Accessible.name: "Preset xuất"
        }
        Text { text: "Mã hóa"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
        ComboBox {
            Layout.fillWidth: true
            model: ["auto", "nvenc", "cpu"]
            palette.dark: Theme.textSecondary
            currentIndex: Math.max(0, model.indexOf(root.appModel.exportBackend))
            onActivated: root.appModel.setExportBackend(currentText)
            Accessible.name: "Backend xuất"
        }
        Text { text: "Checkpoint"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
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
        objectName: "subtitleBackgroundToggle"
        text: "Bật nền phụ đề"
        checked: root.appModel.exportSubtitleBackgroundEnabled
        onClicked: root.appModel.setExportSubtitleBackgroundEnabled(checked)
        Accessible.name: text
    }
    ComboBox {
        objectName: "subtitleBackgroundMode"
        Layout.fillWidth: true
        model: ["Nền đen", "Nền blur/mờ"]
        palette.dark: Theme.textSecondary
        currentIndex: root.appModel.exportSubtitleBackgroundMode === "blur" ? 1 : 0
        enabled: root.appModel.exportSubtitleBackgroundEnabled
        onActivated: root.appModel.setExportSubtitleBackgroundMode(currentIndex === 1 ? "blur" : "black")
        Accessible.name: "Kiểu nền phụ đề"
    }
    CheckBox {
        text: "Đóng cứng phụ đề vào video"
        checked: root.appModel.exportSubtitleEnabled
        onClicked: root.appModel.setExportSubtitleEnabled(checked)
        Accessible.name: text
    }
    GridLayout {
        Layout.fillWidth: true
        columns: 2
        Text { text: "Font"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
        DsTextField {
            Layout.fillWidth: true
            text: root.appModel.exportFontFamily
            onEditingFinished: root.appModel.setExportFontFamily(text)
        }
        Text { text: "Cỡ chữ"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
        DsSpinBox {
            accessibleName: "Cỡ chữ phụ đề"
            from: 12; to: 120; value: root.appModel.exportFontSize
            onValueModified: root.appModel.setExportFontSize(value)
        }
        Text { text: "Cách đáy"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
        DsSpinBox {
            accessibleName: "Khoảng cách phụ đề với đáy"
            from: 0; to: 500; value: root.appModel.exportSubtitleMarginBottom
            onValueModified: root.appModel.setExportSubtitleMarginBottom(value)
        }
        Text { text: "Màu chữ"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
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
        Text { text: "Màu viền"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
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
        Text { text: "Độ dày viền"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
        DsSpinBox {
            Layout.fillWidth: true
            accessibleName: "Độ dày viền phụ đề"
            from: 0; to: 12
            value: root.appModel.exportSubtitleOutlineWidth
            onValueModified: root.appModel.setExportSubtitleOutlineWidth(value)
        }
        Text { text: "Độ mờ (%)"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
        DsSpinBox {
            Layout.fillWidth: true
            accessibleName: "Độ mờ phụ đề"
            from: 0; to: 100
            value: root.appModel.exportSubtitleOpacityPercent
            onValueModified: root.appModel.setExportSubtitleOpacityPercent(value)
        }
        Text { text: "Căn ngang"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
        ComboBox {
            Layout.fillWidth: true
            model: ["Trái", "Giữa", "Phải"]
            palette.dark: Theme.textSecondary
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
        Text { text: "Độ mờ"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
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
        SectionTitle { text: "Che theo phụ đề Việt" }
        Item { Layout.fillWidth: true }
        Text {
            text: "Bám chữ đã dịch · tự vừa khung"
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: 10
        }
    }
    CheckBox {
        text: "Tự bám chữ Việt theo thời gian cue"
        checked: Boolean(root.automaticBlurOverlay.enabled)
            && root.automaticBlurOverlay.tracking_mode === "source_subtitle"
        enabled: Boolean(root.automaticBlurOverlay.id)
            && root.automaticBlurOverlay.tracking_mode === "source_subtitle"
        onClicked: root.appModel.setOverlayEnabled(
            root.automaticBlurOverlay.id, checked
        )
        Accessible.name: text
    }
    DsButton {
        Layout.fillWidth: true
        visible: Boolean(root.automaticBlurOverlay.id)
            && root.automaticBlurOverlay.tracking_mode !== "source_subtitle"
        text: "Chuyển blur cố định cũ sang tự bám phụ đề"
        onClicked: root.appModel.setOverlayTrackingMode(
            root.automaticBlurOverlay.id, "source_subtitle"
        )
    }
    DsButton {
        Layout.fillWidth: true
        enabled: Boolean(root.automaticBlurOverlay.id)
            && root.automaticBlurOverlay.tracking_mode === "source_subtitle"
        primary: root.automaticBlurOverlay.concealment_mode === "cover"
        text: root.automaticBlurOverlay.concealment_mode === "cover"
            ? "Che kín: Đang bật" : "Bật Che kín chữ gốc"
        feedbackText: root.automaticBlurOverlay.concealment_mode === "cover"
            ? "Đã tắt Che kín; vẫn giữ blur tự động"
            : "Đã bật Che kín chữ gốc"
        onClicked: root.appModel.setOverlayConcealmentMode(
            root.automaticBlurOverlay.id,
            root.automaticBlurOverlay.concealment_mode === "cover"
                ? "blur" : "cover"
        )
    }
    GridLayout {
        Layout.fillWidth: true
        columns: 2
        columnSpacing: Theme.space8
        rowSpacing: Theme.space8
        enabled: root.automaticBlurOverlay.tracking_mode === "source_subtitle"
        Text { text: "Bán kính"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
        DsSpinBox {
            Layout.fillWidth: true
            accessibleName: "Bán kính blur tự bám"
            from: 1; to: 100
            value: root.overlayNumber(
                root.automaticBlurOverlay, "blur_radius", 46
            )
            onValueModified: root.appModel.setOverlayBlurRadius(
                root.automaticBlurOverlay.id, value
            )
        }
        Text { text: "Padding ngang (%)"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
        DsSpinBox {
            id: automaticBlurPaddingX
            Layout.fillWidth: true
            accessibleName: "Padding ngang blur tự bám"
            from: 0; to: 200; stepSize: 1
            value: Math.round(root.overlayNumber(
                root.automaticBlurOverlay, "padding_x", 0.02
            ) * 1000)
            textFromValue: function(value, locale) {
                return Number(value / 10).toLocaleString(locale, "f", 1)
            }
            valueFromText: function(text, locale) {
                return Math.round(Number.fromLocaleString(locale, text) * 10)
            }
            onValueModified: root.appModel.setOverlayPadding(
                root.automaticBlurOverlay.id,
                value / 1000,
                automaticBlurPaddingY.value / 1000
            )
        }
        Text { text: "Padding dọc (%)"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
        DsSpinBox {
            id: automaticBlurPaddingY
            Layout.fillWidth: true
            accessibleName: "Padding dọc blur tự bám"
            from: 0; to: 200; stepSize: 1
            value: Math.round(root.overlayNumber(
                root.automaticBlurOverlay, "padding_y", 0.014
            ) * 1000)
            textFromValue: function(value, locale) {
                return Number(value / 10).toLocaleString(locale, "f", 1)
            }
            valueFromText: function(text, locale) {
                return Math.round(Number.fromLocaleString(locale, text) * 10)
            }
            onValueModified: root.appModel.setOverlayPadding(
                root.automaticBlurOverlay.id,
                automaticBlurPaddingX.value / 1000,
                value / 1000
            )
        }
    }
    Text {
        Layout.fillWidth: true
        text: "Vùng che bám chữ Việt và thời gian cue. Che kín phủ thêm lớp tối trên blur mạnh, luôn nằm dưới phụ đề Việt. Nếu dời chữ Việt ra xa chữ gốc, hãy thêm blur thủ công để che vùng cũ."
        color: Theme.textMuted
        font.family: Theme.fontFamily
        font.pixelSize: 10
        wrapMode: Text.Wrap
    }
    Text {
        Layout.fillWidth: true
        visible: root.watermarkBlurOverlays.length > 0
        text: "Đã tự che " + root.watermarkBlurOverlays.length
            + " vùng watermark Bilibili lặp lại; các vùng này không được đưa vào SRT."
        color: Theme.success
        font.family: Theme.fontFamily
        font.pixelSize: 10
        wrapMode: Text.Wrap
    }

    Rectangle {
        Layout.fillWidth: true
        Layout.preferredHeight: 1
        color: Theme.borderSubtle
    }
    RowLayout {
        Layout.fillWidth: true
        SectionTitle { text: "Blur thủ công" }
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
            font.family: Theme.fontFamily
            font.pixelSize: 11
        }
        ComboBox {
            id: blurRegionPicker
            palette.dark: Theme.textSecondary
            Layout.fillWidth: true
            model: root.blurOverlays
            textRole: "id"
            currentIndex: root.blurOverlays.findIndex(
                function(item) { return item.id === root.blurOverlay.id }
            )
            displayText: count > 0 ? currentText : "Chưa có vùng thủ công"
            onActivated: {
                const selected = root.blurOverlays[currentIndex]
                if (!selected) return
                root.selectedBlurId = selected.id
                root.appModel.selectProjectOverlay(selected.id)
            }
        }
        DsButton {
            text: "Xóa vùng"
            compact: true
            danger: true
            enabled: root.blurOverlay.id !== "overlay-default-blur"
                && Boolean(root.blurOverlay.id)
                && !root.appModel.timelineBusy
            disabledReason: "Vùng mặc định chỉ có thể tắt."
            onClicked: {
                root.appModel.deleteOverlay(root.blurOverlay.id)
                root.selectedBlurId = "overlay-default-blur"
                root.appModel.selectProjectOverlay(root.selectedBlurId)
            }
        }
    }
    CheckBox {
        text: "Bật vùng blur thủ công"
        checked: Boolean(root.blurOverlay.enabled)
        enabled: Boolean(root.blurOverlay.id)
        onClicked: root.appModel.setOverlayEnabled(root.blurOverlay.id, checked)
        Accessible.name: text
    }
    Text {
        Layout.fillWidth: true
        text: "Blur thủ công độc lập với vùng che theo chữ Việt."
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
        enabled: Boolean(root.blurOverlay.id)
        Text { text: "Bán kính"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
        DsSpinBox {
            Layout.fillWidth: true
            accessibleName: "Bán kính blur"
            from: 1; to: 100
            value: root.overlayNumber(root.blurOverlay, "blur_radius", 22)
            onValueModified: root.appModel.setOverlayBlurRadius(
                root.blurOverlay.id, value
            )
        }
        Text { text: "Bắt đầu"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
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
        Text { text: "Kết thúc"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
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

        Text { text: "Preset"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
        ComboBox {
            Layout.fillWidth: true
            model: ["Không filter", "Ấm điện ảnh", "Lạnh", "Đen trắng"]
            palette.dark: Theme.textSecondary
            currentIndex: root.filterPresetIndex(
                root.appModel.exportVideoFilterPreset
            )
            onActivated: root.appModel.setExportVideoFilterPreset(
                root.filterPresetId(currentIndex)
            )
            Accessible.name: "Preset filter video"
        }
        Text { text: "Cường độ (%)"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
        DsSpinBox {
            Layout.fillWidth: true
            accessibleName: "Cường độ filter"
            from: 0; to: 100
            value: root.appModel.exportVideoFilterIntensity
            onValueModified: root.appModel.setExportVideoFilterIntensity(value)
        }
        Text { text: "Độ sáng"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
        DsSpinBox {
            Layout.fillWidth: true
            accessibleName: "Độ sáng video"
            from: -50; to: 50
            value: root.appModel.exportVideoBrightness
            onValueModified: root.appModel.setExportVideoBrightness(value)
        }
        Text { text: "Tương phản (%)"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
        DsSpinBox {
            Layout.fillWidth: true
            accessibleName: "Độ tương phản video"
            from: 50; to: 150
            value: root.appModel.exportVideoContrast
            onValueModified: root.appModel.setExportVideoContrast(value)
        }
        Text { text: "Bão hòa (%)"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
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
                objectName: "mixerTrackLabel-" + parent.modelData.id
                Layout.preferredWidth: 105
                text: parent.modelData.name
                color: Theme.textSecondary
                font.family: Theme.fontFamily
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

    }
}
