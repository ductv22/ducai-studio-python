import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import DucaiStudio 1.0

Dialog {
    id: root
    objectName: "manualSubtitleCueDialog"

    required property var appModel
    property real startUs: 0
    property string cueRole: "dialogue"
    property bool titleMode: false
    property bool fullVideo: false
    property bool untilEnd: false
    property string pendingText: ""
    property real durationSeconds: 2
    property var inheritedBox: [0.10, 0.74, 0.80, 0.16]
    property int fontSize: 12
    property string orientation: "horizontal"
    property string styleHint: "standard"
    property string backgroundColor: ""
    property string textColor: ""
    property int paddingX: 20
    property int paddingY: 10
    property string backgroundStyle: "solid"
    property int letterSpacing: 0
    property bool fontBold: false
    property bool fontItalic: false
    property int textOpacity: 100
    property int backgroundOpacity: 90
    property bool fadeEnabled: false
    property int fadeDurationMs: 300

    signal placementRequested()
    signal cueSubmitted()

    parent: Overlay.overlay
    anchors.centerIn: Overlay.overlay
    width: Math.min(800, Overlay.overlay.width - 40)
    height: Math.min(560, Overlay.overlay.height - 40)
    modal: true
    title: titleMode ? "Thêm Text tên phim/kênh" : "Thêm chữ tại vị trí playhead"
    standardButtons: Dialog.NoButton
    closePolicy: Popup.CloseOnEscape

    function styleIndex(value) {
        const values = [
            "standard", "display", "calligraphy", "calligraphy_soft",
            "calligraphy_bold", "calligraphy_ink", "calligraphy_brush",
            "calligraphy_water", "calligraphy_splash", "calligraphy_hurricane"
        ]
        return Math.max(0, values.indexOf(String(value || "standard")))
    }

    function backgroundIndex(value) {
        const values = ["solid", "ink_splash", "ink_wash", "ink_cloud", "brush_stroke"]
        return Math.max(0, values.indexOf(String(value || "solid")))
    }

    function applyDefaults(values) {
        const item = values || ({})
        inheritedBox = item.visual_box || (cueRole === "dialogue"
            ? [0.10, 0.74, 0.80, 0.16] : [0.35, 0.35, 0.30, 0.14])
        fontSize = Number(item.font_size || (cueRole === "scene_text" ? 40 : 12))
        orientation = String(item.orientation || "horizontal")
        styleHint = String(item.style_hint || "standard")
        backgroundColor = String(item.background_color || "")
        textColor = String(item.text_color || "")
        paddingX = Number(item.background_padding_x ?? 20)
        paddingY = Number(item.background_padding_y ?? 10)
        backgroundStyle = String(item.background_style || "solid")
        letterSpacing = Number(item.letter_spacing_percent || 0)
        fontBold = Boolean(item.font_bold)
        fontItalic = Boolean(item.font_italic)
        textOpacity = Number(item.text_opacity_percent ?? 100)
        backgroundOpacity = Number(item.background_opacity_percent ?? 90)
        fadeEnabled = item.fade_enabled === undefined
            ? cueRole === "scene_text" : Boolean(item.fade_enabled)
        fadeDurationMs = Number(item.fade_duration_ms || 300)
    }

    function openForPlayhead(playheadUs) {
        titleMode = false
        fullVideo = false
        untilEnd = false
        startUs = Math.max(0, Number(playheadUs || 0))
        cueRole = "dialogue"
        pendingText = ""
        applyDefaults(appModel.manualSubtitleCueDefaults(cueRole, startUs))
        open()
        textInput.forceActiveFocus()
    }

    function openForTitle(playheadUs) {
        titleMode = true
        fullVideo = false
        untilEnd = false
        startUs = Math.max(0, Number(playheadUs || 0))
        cueRole = "title_text"
        pendingText = ""
        durationSeconds = 5
        applyDefaults({visual_box: [0.04, 0.04, 0.40, 0.14], font_size: 40,
                       text_color: "#FFFFFF", background_opacity_percent: 100,
                       fade_enabled: false})
        open()
        textInput.forceActiveFocus()
    }

    function currentOptions() {
        return {
            "cue_role": cueRole,
            "font_size": fontSize,
            "orientation": orientation,
            "style_hint": styleHint,
            "background_color": backgroundColor,
            "text_color": textColor,
            "background_padding_x": paddingX,
            "background_padding_y": paddingY,
            "background_style": backgroundStyle,
            "letter_spacing_percent": letterSpacing,
            "font_bold": fontBold,
            "font_italic": fontItalic,
            "text_opacity_percent": textOpacity,
            "background_opacity_percent": backgroundOpacity,
            "fade_enabled": cueRole !== "dialogue" && fadeEnabled,
            "fade_duration_ms": fadeDurationMs
        }
    }

    function submitAtRegion(x, y, width, height) {
        const value = pendingText.trim()
        if (value.length === 0) return
        if (titleMode) {
            appModel.addTitleText(value, fullVideo ? 0 : startUs,
                fullVideo || untilEnd ? appModel.currentProject.durationUs
                    : startUs + durationSeconds * 1000000,
                [Number(x), Number(y), Number(width), Number(height)], currentOptions())
            cueSubmitted()
            return
        }
        appModel.addManualSubtitleCue(
            value,
            startUs,
            startUs + durationSeconds * 1000000,
            [Number(x), Number(y), Number(width), Number(height)],
            currentOptions()
        )
        cueSubmitted()
    }

    ColorDialog {
        id: textColorDialog
        title: "Chọn màu chữ"
        onAccepted: root.textColor = selectedColor.toString().slice(0, 7).toUpperCase()
    }

    footer: Rectangle {
        implicitHeight: 56
        color: Theme.surfacePanel

        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            height: 1
            color: Theme.borderSubtle
        }

        RowLayout {
            anchors.fill: parent
            anchors.margins: Theme.space8
            spacing: Theme.space8

            Item { Layout.fillWidth: true }
            DsButton {
                text: "Hủy"
                tooltipText: "Đóng hộp thoại mà không thêm cue."
                feedbackEnabled: false
                onClicked: root.reject()
            }
            DsButton {
                objectName: "confirmNewCueButton"
                text: root.titleMode ? "Thêm Text" : root.cueRole === "dialogue"
                    ? "Thêm thoại" : "Chọn vùng đặt chữ"
                primary: true
                tooltipText: root.titleMode
                    ? "Thêm Text độc lập trên video với thời lượng và định dạng đã chọn."
                    : root.cueRole === "dialogue"
                    ? "Thêm cue thoại tại playhead với định dạng đã chọn."
                    : "Xác nhận định dạng rồi kéo vùng đặt chữ trên Preview."
                enabled: root.pendingText.trim().length > 0
                disabledReason: "Hãy nhập nội dung cue trước."
                feedbackEnabled: false
                onClicked: root.accept()
            }
        }
    }

    ColorDialog {
        id: backgroundColorDialog
        title: "Chọn màu nền"
        onAccepted: root.backgroundColor = selectedColor.toString().slice(0, 7).toUpperCase()
    }

    contentItem: ScrollView {
        clip: true
        contentWidth: availableWidth

        ColumnLayout {
            width: parent.availableWidth
            spacing: Theme.space8

            Text {
                Layout.fillWidth: true
                text: root.titleMode
                    ? "Text nằm riêng trên V1 để đặt tên phim/kênh, không tham gia OCR, dịch hay lồng tiếng. Sau khi thêm, chọn Text để sửa hoặc kéo vị trí trực tiếp trên Preview."
                    : root.cueRole === "dialogue"
                    ? "Thoại chính sẽ được lồng tiếng. Tool đang kế thừa vị trí và định dạng của cue thoại gần nhất; nếu S1 trùng thời gian, cue được đặt ở lane trống nhưng vẫn được đọc."
                    : "Chữ trong cảnh chỉ hiển thị hình ảnh, không được lồng tiếng. Sau khi xác nhận, kéo vùng đặt chữ trực tiếp trên Preview."
                color: root.cueRole === "dialogue" ? Theme.success : Theme.info
                font.family: Theme.fontFamily
                font.pixelSize: 11
                wrapMode: Text.Wrap
            }

            RowLayout {
                Layout.fillWidth: true
                Text { visible: !root.titleMode; text: "Loại cue"; color: Theme.textSecondary }
                ComboBox {
                    id: roleBox
                    visible: !root.titleMode
                    Layout.preferredWidth: 250
                    model: [
                        { text: "Thoại chính · có lồng tiếng", value: "dialogue" },
                        { text: "Chữ trong cảnh · không đọc", value: "scene_text" }
                    ]
                    textRole: "text"
                    valueRole: "value"
                    currentIndex: root.cueRole === "scene_text" ? 1 : 0
                    onActivated: {
                        root.cueRole = currentValue
                        root.applyDefaults(root.appModel.manualSubtitleCueDefaults(
                            root.cueRole, root.startUs
                        ))
                    }
                    Accessible.name: "Loại cue thoại hoặc chữ trong cảnh"
                }
                Text {
                    text: "Bắt đầu " + ((root.fullVideo ? 0 : root.startUs) / 1000000).toFixed(3) + " giây"
                    color: Theme.textMuted
                    font.family: Theme.timecodeFamily
                }
                Item { Layout.fillWidth: true }
            }

            DsTextField {
                id: textInput
                objectName: "manualCueTextInput"
                Layout.fillWidth: true
                text: root.pendingText
                placeholderText: root.titleMode ? "Tên phim, kênh hoặc người làm video"
                    : root.cueRole === "dialogue"
                    ? "Nội dung thoại tiếng Việt bị sót"
                    : "Nội dung chữ trong cảnh"
                onTextChanged: root.pendingText = text
                Accessible.name: "Nội dung cue mới"
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.space8
                Text { text: "Thời lượng"; color: Theme.textSecondary }
                DsSpinBox {
                    Layout.preferredWidth: 112
                    visible: !root.titleMode
                    from: 1
                    to: 300
                    value: root.durationSeconds
                    onValueModified: root.durationSeconds = value
                    accessibleName: "Thời lượng cue tính bằng giây"
                }
                DsTextField {
                    visible: root.titleMode
                    objectName: "titleDurationField"
                    Layout.preferredWidth: 112
                    text: root.durationSeconds.toString()
                    enabled: !root.fullVideo && !root.untilEnd
                    validator: DoubleValidator { bottom: 0.1; top: 86400; decimals: 3; locale: "C" }
                    onEditingFinished: {
                        const value = Number(text.replace(",", "."))
                        if (isFinite(value) && value >= 0.1) root.durationSeconds = value
                    }
                    Accessible.name: "Thời lượng Text tính bằng giây"
                }
                Text { text: "Cỡ chữ"; color: Theme.textSecondary }
                DsSpinBox {
                    Layout.preferredWidth: 104
                    from: 1
                    to: 2147483647
                    value: root.fontSize
                    onValueModified: root.fontSize = value
                    accessibleName: "Kích thước chữ từ 1 đến 100"
                }
                ComboBox {
                    Layout.preferredWidth: 112
                    model: [
                        { text: "Ngang", value: "horizontal" },
                        { text: "Dọc", value: "vertical" }
                    ]
                    textRole: "text"
                    valueRole: "value"
                    currentIndex: root.orientation === "vertical" ? 1 : 0
                    onActivated: root.orientation = currentValue
                    Accessible.name: "Hướng chữ"
                }
                Item { Layout.fillWidth: true }
            }

            RowLayout {
                visible: root.titleMode
                Layout.fillWidth: true
                CheckBox {
                    text: "Đến hết video"
                    checked: root.untilEnd
                    onToggled: { root.untilEnd = checked; root.fullVideo = false }
                }
                DsButton {
                    objectName: "titleFullVideoButton"
                    text: "Toàn video"
                    compact: true
                    primary: root.fullVideo
                    tooltipText: "Đặt Text từ 00:00 đến hết video."
                    onClicked: { root.fullVideo = !root.fullVideo; root.untilEnd = false }
                }
                Item { Layout.fillWidth: true }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.space8
                Text { text: "Kiểu chữ"; color: Theme.textSecondary }
                ComboBox {
                    Layout.fillWidth: true
                    model: [
                        { text: "Chuẩn", value: "standard" },
                        { text: "Hiển thị", value: "display" },
                        { text: "Thư pháp Playwrite", value: "calligraphy" },
                        { text: "Thư pháp mềm", value: "calligraphy_soft" },
                        { text: "Thư pháp đậm", value: "calligraphy_bold" },
                        { text: "Mực tàu", value: "calligraphy_ink" },
                        { text: "Bút lông Comforter", value: "calligraphy_brush" },
                        { text: "Mực nước Water Brush", value: "calligraphy_water" },
                        { text: "Phóng bút Splash", value: "calligraphy_splash" },
                        { text: "Thư pháp Hurricane", value: "calligraphy_hurricane" }
                    ]
                    textRole: "text"
                    valueRole: "value"
                    currentIndex: root.styleIndex(root.styleHint)
                    onActivated: root.styleHint = currentValue
                    Accessible.name: "Font và kiểu chữ"
                }
                DsButton {
                    text: "B"
                    compact: true
                    primary: root.fontBold
                    tooltipText: "Bật hoặc tắt chữ in đậm cho cue mới."
                    onClicked: root.fontBold = !root.fontBold
                    Accessible.name: "In đậm"
                }
                DsButton {
                    text: "I"
                    compact: true
                    primary: root.fontItalic
                    tooltipText: "Bật hoặc tắt chữ in nghiêng cho cue mới."
                    onClicked: root.fontItalic = !root.fontItalic
                    Accessible.name: "In nghiêng"
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.space8
                Text { text: "Giãn chữ"; color: Theme.textSecondary }
                DsSpinBox {
                    Layout.preferredWidth: 104
                    from: 0
                    to: 2147483647
                    value: root.letterSpacing
                    onValueModified: root.letterSpacing = value
                    accessibleName: "Khoảng cách chữ theo phần trăm"
                }
                Text { text: "Đục chữ"; color: Theme.textSecondary }
                DsSpinBox {
                    Layout.preferredWidth: 104
                    from: 0
                    to: 100
                    value: root.textOpacity
                    onValueModified: root.textOpacity = value
                    accessibleName: "Độ đục chữ"
                }
                DsButton {
                    text: "Màu chữ"
                    compact: true
                    tooltipText: "Mở bảng chọn màu chữ cho cue mới."
                    onClicked: textColorDialog.open()
                }
                Rectangle {
                    Layout.preferredWidth: 28
                    Layout.preferredHeight: 28
                    radius: Theme.radiusSmall
                    color: /^#[0-9A-Fa-f]{6}$/.test(root.textColor)
                        ? root.textColor : root.appModel.exportSubtitlePrimaryColor
                    border.width: 1
                    border.color: Theme.borderStrong
                }
                Item { Layout.fillWidth: true }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.space8
                Text { text: "Kiểu nền"; color: Theme.textSecondary }
                ComboBox {
                    Layout.fillWidth: true
                    Layout.maximumWidth: 240
                    enabled: root.cueRole !== "dialogue"
                    model: [
                        { text: "Nền phẳng", value: "solid" },
                        { text: "Mực loang", value: "ink_splash" },
                        { text: "Mực đại mặc", value: "ink_wash" },
                        { text: "Mực tụ", value: "ink_cloud" },
                        { text: "Nét cọ", value: "brush_stroke" }
                    ]
                    textRole: "text"
                    valueRole: "value"
                    currentIndex: root.backgroundIndex(root.backgroundStyle)
                    onActivated: root.backgroundStyle = currentValue
                    Accessible.name: "Kiểu nền chữ"
                }
                DsButton {
                    text: "Màu nền"
                    compact: true
                    tooltipText: "Mở bảng chọn màu nền của cue mới."
                    onClicked: backgroundColorDialog.open()
                }
                Rectangle {
                    Layout.preferredWidth: 28
                    Layout.preferredHeight: 28
                    radius: Theme.radiusSmall
                    color: /^#[0-9A-Fa-f]{6}$/.test(root.backgroundColor)
                        ? root.backgroundColor : "transparent"
                    border.width: 1
                    border.color: Theme.borderStrong
                }
                DsButton {
                    text: "Bỏ nền"
                    compact: true
                    tooltipText: "Bỏ màu nền của cue mới."
                    onClicked: root.backgroundColor = ""
                }
                Item { Layout.fillWidth: true }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.space8
                Text { text: "Nền ngang"; color: Theme.textSecondary }
                DsSpinBox {
                    Layout.preferredWidth: 104
                    from: 0
                    to: 2147483647
                    value: root.paddingX
                    onValueModified: root.paddingX = value
                    accessibleName: "Độ rộng nền"
                }
                Text { text: "Nền dọc"; color: Theme.textSecondary }
                DsSpinBox {
                    Layout.preferredWidth: 104
                    from: 0
                    to: 2147483647
                    value: root.paddingY
                    onValueModified: root.paddingY = value
                    accessibleName: "Độ cao nền"
                }
                Text { text: "Đục nền"; color: Theme.textSecondary }
                DsSpinBox {
                    Layout.preferredWidth: 104
                    from: 0
                    to: 100
                    value: root.backgroundOpacity
                    onValueModified: root.backgroundOpacity = value
                    accessibleName: "Độ đục nền"
                }
                Item { Layout.fillWidth: true }
            }

            RowLayout {
                Layout.fillWidth: true
                visible: root.cueRole !== "dialogue"
                spacing: Theme.space8
                DsButton {
                    text: "Mờ dần vào/ra"
                    compact: true
                    primary: root.fadeEnabled
                    tooltipText: "Cho chữ mới xuất hiện và biến mất mờ dần."
                    onClicked: root.fadeEnabled = !root.fadeEnabled
                }
                Text { text: "Mỗi đầu"; color: Theme.textSecondary }
                DsSpinBox {
                    Layout.preferredWidth: 112
                    from: 50
                    to: 2000
                    stepSize: 50
                    value: root.fadeDurationMs
                    onValueModified: root.fadeDurationMs = value
                    accessibleName: "Thời lượng mờ dần mỗi đầu tính bằng mili giây"
                }
                Text { text: "ms"; color: Theme.textMuted }
                Text {
                    Layout.fillWidth: true
                    text: "Preview và video xuất dùng cùng hiệu ứng; cue ngắn tự giảm thời lượng fade."
                    color: Theme.textMuted
                    font.pixelSize: 11
                    wrapMode: Text.Wrap
                }
            }
        }
    }

    onAccepted: {
        pendingText = textInput.text
        if (pendingText.trim().length === 0) return
        if (cueRole === "dialogue" || titleMode) {
            const box = inheritedBox || [0.10, 0.74, 0.80, 0.16]
            submitAtRegion(box[0], box[1], box[2], box[3])
        } else {
            placementRequested()
        }
    }
}
