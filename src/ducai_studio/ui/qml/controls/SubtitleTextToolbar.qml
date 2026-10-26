import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import DucaiStudio 1.0

Rectangle {
    id: root
    objectName: "subtitleTextToolbar"

    required property var appModel
    property bool inspectorMode: false
    property bool allowManualPlacement: false
    signal manualPlacementRequested()
    signal colorPickRequested(string target)

    readonly property var cue: root.appModel.selectedCue || ({})
    readonly property string cueId: String(cue.cue_id || "")
    readonly property bool hasCue: cueId.length > 0
    readonly property var classification: root.appModel.selectedCueClassification || ({})
    readonly property bool canConvert: Boolean(classification.can_convert)
    readonly property bool selectionLocked:
        Boolean(root.appModel.selectedTextCueEditingLocked)
    readonly property bool isSceneText:
        String(cue.cue_role || "dialogue") !== "dialogue"
    readonly property string editedCueId: String(
        cue.translation_cue_id || cue.cue_id || ""
    )
    readonly property string editedLanguage:
        String(cue.translation_cue_id || "").length > 0 ? "translated" : "source"

    enabled: !root.appModel.timelineBusy && !root.selectionLocked
    implicitHeight: root.inspectorMode ? 620 : 196
    color: Theme.surfaceRaised
    border.width: 1
    border.color: root.hasCue ? Theme.info : Theme.borderSubtle
    radius: Theme.radiusSmall

    component TextAction: DsButton { emphasized: true }
    component TextNumber: DsSpinBox { emphasized: true }

    function convertSelection(role) {
        if (!root.canConvert || root.selectionLocked
                || root.appModel.subtitleBusy || root.appModel.timelineBusy) return
        if (!root.commitText()) return
        textInput.focus = false
        root.appModel.setSelectedCueRole(role)
    }

    function overrideValue(values, key, fallback) {
        return values && values[key] !== undefined ? values[key] : fallback
    }

    function applyCurrent(values) {
        if (!root.hasCue || root.selectionLocked
                || root.appModel.subtitleBusy || root.appModel.timelineBusy) return
        const overrides = values || ({})
        root.appModel.setSelectedSubtitleTextFormat(
            Math.max(1, Number(root.overrideValue(
                overrides, "fontSize", root.cue.text_font_size || 12
            ))),
            String(root.overrideValue(
                overrides, "orientation", root.cue.orientation || "horizontal"
            )),
            String(root.overrideValue(
                overrides, "backgroundColor", root.cue.background_color || ""
            )),
            String(root.overrideValue(
                overrides, "textColor", root.cue.text_color || ""
            )),
            String(root.overrideValue(
                overrides, "styleHint", root.cue.style_hint || "standard"
            )),
            Math.max(0, Number(root.overrideValue(
                overrides, "paddingX", root.cue.background_padding_x ?? 20
            ))),
            Math.max(0, Number(root.overrideValue(
                overrides, "paddingY", root.cue.background_padding_y ?? 10
            ))),
            String(root.overrideValue(
                overrides, "backgroundStyle", root.cue.background_style || "solid"
            )),
            Math.max(0, Number(root.overrideValue(
                overrides, "letterSpacing", root.cue.letter_spacing_percent || 0
            ))),
            Boolean(root.overrideValue(
                overrides, "fontBold", Boolean(root.cue.font_bold)
            )),
            Boolean(root.overrideValue(
                overrides, "fontItalic", Boolean(root.cue.font_italic)
            )),
            Math.max(0, Math.min(100, Number(root.overrideValue(
                overrides, "textOpacity", root.cue.text_opacity_percent ?? 100
            )))),
            Math.max(0, Math.min(100, Number(root.overrideValue(
                overrides, "backgroundOpacity", root.cue.background_opacity_percent ?? 90
            ))))
        )
    }

    function chooseBackground(value) {
        const changes = {"backgroundStyle": value}
        if (value === "blur") changes.backgroundOpacity = 100
        if (value !== "none" && value !== "blur" && !root.cue.background_color)
            changes.backgroundColor = "#000000"
        root.applyCurrent(changes)
    }

    function applyPickedColor(target, colorValue) {
        const color = String(colorValue || "").toUpperCase()
        if (!/^#[0-9A-F]{6}$/.test(color)) return
        if (target === "background")
            root.applyCurrent({"backgroundColor": color})
        else
            root.applyCurrent({"textColor": color})
    }

    function applyInkPreset() {
        root.applyCurrent({
            "styleHint": "calligraphy_ink",
            "backgroundStyle": "ink_splash",
            "backgroundColor": "#080808",
            "textColor": "#FFFFFF",
            "paddingX": 34,
            "paddingY": 26,
            "backgroundOpacity": 94
        })
    }

    ColorDialog {
        id: backgroundDialog
        title: "Chọn màu nền của cue chữ"
        onAccepted: root.applyCurrent({
            "backgroundColor": selectedColor.toString().slice(0, 7)
        })
    }

    ColorDialog {
        id: textColorDialog
        title: "Chọn màu chữ của cue"
        onAccepted: root.applyCurrent({
            "textColor": selectedColor.toString().slice(0, 7)
        })
    }

    ColorDialog {
        id: outlineColorDialog
        title: "Chọn màu viền chữ"
        onAccepted: root.appModel.setExportSubtitleOutlineColor(selectedColor.toString().slice(0, 7))
    }


    property string inputCueId: ""
    property string inputLanguage: "source"
    property string inputDraft: ""
    property string inputError: ""

    function commitText() {
        const id = root.inputCueId
        if (id.length === 0) return true
        const draft = root.inputDraft
        if (draft.trim().length === 0) {
            root.inputError = "Nội dung cue không được để trống. Nhập chữ hoặc dùng nút Xóa."
            UiFeedback.show(root.inputError, "error", 5000)
            return false
        }
        const language = root.inputLanguage
        const changed = draft !== textInput.originalText
        root.inputCueId = ""
        root.inputError = ""
        textInput.originalText = draft
        if (changed)
            root.appModel.updateSubtitleText(id, draft, language)
        return true
    }

    Connections {
        target: root.appModel
        function onSelectedCueChanged() {
            if (root.inputCueId && root.inputCueId !== root.editedCueId) {
                root.commitText()
                // A blank draft is invalid, not a deletion. Changing the
                // selection leaves the previous cue's stored text intact.
                root.inputCueId = ""
                root.inputError = ""
                textInput.focus = false
            }
        }
    }

    ScrollView {
        id: textPropertiesScroll
        anchors.fill: parent
        anchors.margins: Theme.space8
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ColumnLayout {
            id: toolbarColumn
            width: textPropertiesScroll.availableWidth
            spacing: Theme.space8
            Text {
                Layout.fillWidth: true
                text: root.hasCue
                    ? (root.classification.role === "mixed" ? "Nhiều loại cue"
                        : (root.classification.role === "title_text" ? "Chữ tiêu đề"
                        : (root.classification.role === "scene_text" ? "Chữ cảnh" : "Thoại")))
                        + (root.classification.track ? " · " + root.classification.track : " · nhiều hàng")
                        + " · " + Number(root.classification.count || 1) + " cue"
                    : "Chọn cue để sửa · bảng giữ cue gần nhất"
                color: root.hasCue ? Theme.info : Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: 11
                wrapMode: Text.Wrap
            }
            DsTextField {
                id: textInput
                objectName: "timelineCueTextField"
                property string originalText: ""
                Layout.fillWidth: true
                placeholderText: "Nội dung cue đang chọn"
                enabled: root.hasCue && !root.appModel.subtitleBusy
                Accessible.name: "Nội dung cue chữ đang chọn"
                Binding {
                    target: textInput
                    property: "text"
                    value: String(root.cue.translated_text || root.cue.source_text || "")
                    when: !textInput.activeFocus
                        && (root.inputCueId.length === 0 || root.inputCueId !== root.editedCueId)
                }
                onActiveFocusChanged: {
                    if (activeFocus && root.inputCueId !== root.editedCueId) {
                        root.inputCueId = root.editedCueId
                        root.inputLanguage = root.editedLanguage
                        root.inputDraft = text
                        root.inputError = ""
                        originalText = text
                    }
                }
                onTextEdited: {
                    if (root.inputCueId.length === 0) {
                        root.inputCueId = root.editedCueId
                        root.inputLanguage = root.editedLanguage
                    }
                    root.inputDraft = text
                    root.inputError = ""
                }
                onEditingFinished: root.commitText()
            }
            Text {
                objectName: "timelineCueTextValidation"
                Layout.fillWidth: true
                visible: root.inputError.length > 0
                text: root.inputError
                color: Theme.error
                font.family: Theme.fontFamily
                font.pixelSize: 11
                wrapMode: Text.Wrap
                Accessible.name: text
            }
            RowLayout {
                Layout.fillWidth: true
                TextAction {
                    visible: root.allowManualPlacement
                    text: "Thêm chữ"; compact: true
                    tooltipText: "Thêm thoại hoặc chữ cảnh tại playhead."
                    enabled: root.appModel.currentProject.hasVideo
                    onClicked: root.manualPlacementRequested()
                }
                Item { Layout.fillWidth: true }
                TextAction {
                    objectName: "deleteTimelineCueButton"
                    text: "Xóa"; compact: true; danger: true
                    tooltipText: "Xóa cue đang chọn, gồm cả chữ và nền."
                    enabled: root.hasCue
                    onClicked: root.appModel.deleteSelectedSubtitleCues()
                }
            }

            SectionTitle { text: "Loại cue" }
            RowLayout {
                Layout.fillWidth: true
                spacing: Theme.space8
                TextAction {
                    objectName: "convertCueToS1Button"
                    Layout.fillWidth: true
                    text: "S1 · Thoại"
                    compact: true
                    primary: root.classification.role === "dialogue" && root.classification.track === "S1"
                    enabled: root.canConvert && !root.appModel.subtitleBusy
                    feedbackEnabled: false
                    tooltipText: "Chuyển cue đang chọn thành thoại S1, có lồng tiếng. Chữ cảnh cũ trùng trên S1 tự chuyển xuống hàng trống bên dưới; giữ chữ, thời gian và định dạng."
                    disabledReason: root.appModel.subtitleBusy ? "Đang cập nhật cue; hãy chờ hoàn tất."
                        : "Chọn cue phụ đề S để chuyển loại; chữ tiêu đề T được giữ riêng."
                    Accessible.name: "Chuyển cue được chọn thành thoại trên S1"
                    onClicked: root.convertSelection("dialogue")
                }
                TextAction {
                    objectName: "convertCueToS2Button"
                    Layout.fillWidth: true
                    text: "S2 · Chữ cảnh"
                    compact: true
                    primary: root.classification.role === "scene_text" && root.classification.track === "S2"
                    enabled: root.canConvert && !root.appModel.subtitleBusy
                    feedbackEnabled: false
                    tooltipText: "Chuyển tất cả cue đang chọn thành chữ cảnh trên S2, không lồng tiếng. Giữ chữ, thời gian và định dạng; báo lỗi nếu S2 trùng cue."
                    disabledReason: root.appModel.subtitleBusy ? "Đang cập nhật cue; hãy chờ hoàn tất."
                        : "Chọn cue phụ đề S để chuyển loại; chữ tiêu đề T được giữ riêng."
                    Accessible.name: "Chuyển cue được chọn thành chữ trong cảnh trên S2"
                    onClicked: root.convertSelection("scene_text")
                }
            }
            Text {
                Layout.fillWidth: true
                text: root.canConvert
                    ? "Áp dụng cho toàn bộ lựa chọn. D1 đã tạo giữ nguyên; tạo lại giọng để áp dụng loại mới."
                    : "Chọn một hoặc nhiều cue S để đổi loại. Chữ tiêu đề T không đổi."
                color: Theme.textSecondary
                font.family: Theme.fontFamily
                font.pixelSize: 11
                wrapMode: Text.Wrap
            }

            SectionTitle { text: "Chữ" }
            GridLayout {
                Layout.fillWidth: true
                columns: root.inspectorMode ? 2 : 6
                columnSpacing: Theme.space8
                rowSpacing: Theme.space8
                enabled: root.hasCue && !root.appModel.subtitleBusy

                Text { text: "Font chữ"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
                ComboBox {
                    id: styleBox
                    objectName: "styleBox"
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    model: [{text: "Chuẩn", value: "standard"},{text: "Hiển thị", value: "display"},{text: "Thư pháp Playwrite", value: "calligraphy"},{text: "Thư pháp mềm", value: "calligraphy_soft"},{text: "Thư pháp đậm", value: "calligraphy_bold"},{text: "Mực tàu", value: "calligraphy_ink"},{text: "Bút lông Comforter", value: "calligraphy_brush"},{text: "Mực nước Water Brush", value: "calligraphy_water"},{text: "Phóng bút Splash", value: "calligraphy_splash"},{text: "Thư pháp Hurricane", value: "calligraphy_hurricane"}]
                    textRole: "text"; valueRole: "value"
                    font.family: Theme.fontFamily
                    font.pixelSize: 12
                    palette.button: Theme.surfaceRaised
                    palette.buttonText: Theme.textPrimary
                    palette.base: Theme.surfaceRaised
                    palette.text: Theme.textPrimary
                    palette.window: Theme.surfaceRaised
                    palette.highlight: Theme.primary
                    Accessible.name: "Font chữ"
                    currentIndex: Math.max(0, model.findIndex(function(item) {
                        return item.value === String(root.cue.style_hint || "standard")
                    }))
                    onActivated: root.applyCurrent({"styleHint": currentValue})
                }

                Text { text: "Cỡ chữ"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
                TextNumber {
                    id: sizeSpin
                    objectName: "sizeSpin"
                    Layout.fillWidth: true
                    from: 1; to: 2147483647
                    accessibleName: "Cỡ chữ"
                    value: Number(root.cue.text_font_size ?? 12)
                    onValueModified: root.applyCurrent({"fontSize": value})
                }

                Text { text: "Hướng chữ"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
                ComboBox {
                    id: orientationBox
                    objectName: "orientationBox"
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    model: [{text: "Ngang", value: "horizontal"},{text: "Dọc", value: "vertical"}]
                    textRole: "text"; valueRole: "value"
                    font.family: Theme.fontFamily
                    font.pixelSize: 12
                    palette.button: Theme.surfaceRaised
                    palette.buttonText: Theme.textPrimary
                    palette.base: Theme.surfaceRaised
                    palette.text: Theme.textPrimary
                    palette.window: Theme.surfaceRaised
                    palette.highlight: Theme.primary
                    Accessible.name: "Hướng chữ"
                    currentIndex: Math.max(0, model.findIndex(function(item) {
                        return item.value === String(root.cue.orientation || "horizontal")
                    }))
                    onActivated: root.applyCurrent({"orientation": currentValue})
                }

                Text { text: "Giãn chữ (%)"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
                TextNumber {
                    id: letterSpacingSpin
                    objectName: "letterSpacingSpin"
                    Layout.fillWidth: true
                    from: 0; to: 2147483647
                    accessibleName: "Giãn chữ (%)"
                    value: Number(root.cue.letter_spacing_percent ?? 0)
                    onValueModified: root.applyCurrent({"letterSpacing": value})
                }

                Text { text: "Đục chữ (%)"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
                TextNumber {
                    id: textOpacitySpin
                    objectName: "textOpacitySpin"
                    Layout.fillWidth: true
                    from: 0; to: 100
                    accessibleName: "Đục chữ (%)"
                    value: Number(root.cue.text_opacity_percent ?? 100)
                    onValueModified: root.applyCurrent({"textOpacity": value})
                }
                Text { text: "Kiểu chữ"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
                RowLayout {
                    TextAction { text: "B"; compact: true; tooltipText: "Bật/tắt in đậm cho các cue đang chọn."; onClicked: root.applyCurrent({"fontBold": !Boolean(root.cue.font_bold)}); primary: Boolean(root.cue.font_bold); Accessible.name: "In đậm chữ" }
                    TextAction { text: "I"; compact: true; tooltipText: "Bật/tắt in nghiêng cho các cue đang chọn."; onClicked: root.applyCurrent({"fontItalic": !Boolean(root.cue.font_italic)}); primary: Boolean(root.cue.font_italic); Accessible.name: "In nghiêng chữ" }
                }
                Text { text: "Màu chữ"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
                RowLayout {
                    TextAction { text: "Màu"; compact: true; tooltipText: "Chọn màu chữ cho các cue đang chọn."; onClicked: textColorDialog.open();  }
                    TextAction { text: "Ống hút"; compact: true; tooltipText: "Lấy màu chữ từ frame Preview."; onClicked: root.colorPickRequested("text");  }
                }
            }

            SectionTitle { text: "Viền phụ đề thoại" }
            GridLayout {
                Layout.fillWidth: true
                columns: 2
                Text { text: "Độ dày (px)"; color: Theme.textSecondary; font.pixelSize: 11 }
                TextNumber {
                    objectName: "textOutlineWidthSpin"
                    Layout.fillWidth: true
                    from: 0; to: 12
                    value: root.appModel.exportSubtitleOutlineWidth
                    accessibleName: "Độ dày viền chữ, 0 là không viền"
                    onValueModified: root.appModel.setExportSubtitleOutlineWidth(value)
                }
                Text { text: "Màu viền"; color: Theme.textSecondary; font.pixelSize: 11 }
                RowLayout {
                    Rectangle {
                        Layout.preferredWidth: 24; Layout.preferredHeight: 24
                        color: root.appModel.exportSubtitleOutlineColor
                        border.color: Theme.borderSubtle; border.width: 1
                    }
                    TextAction {
                        objectName: "textOutlineColorButton"
                        text: "Chọn màu"; compact: true
                        onClicked: {
                            outlineColorDialog.selectedColor = root.appModel.exportSubtitleOutlineColor
                            outlineColorDialog.open()
                        }
                    }
                }
            }
            SectionTitle { text: "Nền mặc định cho thoại" }
            ComboBox {
                objectName: "subtitleBackgroundMode"
                Layout.fillWidth: true
                model: ["Không nền", "Nền đen", "Nền mờ (blur)"]
                palette.dark: Theme.textSecondary
                currentIndex: !root.appModel.exportSubtitleBackgroundEnabled ? 0
                    : (root.appModel.exportSubtitleBackgroundMode === "blur" ? 2 : 1)
                onActivated: {
                    const choice = currentIndex
                    root.appModel.setExportSubtitleBackgroundEnabled(choice !== 0)
                    if (choice !== 0)
                        root.appModel.setExportSubtitleBackgroundMode(choice === 2 ? "blur" : "black")
                }
                Accessible.name: "Nền mặc định cho thoại"
            }
            SectionTitle { text: "Nền câu đang chọn" }
            GridLayout {
                Layout.fillWidth: true
                columns: root.inspectorMode ? 2 : 6
                columnSpacing: Theme.space8
                rowSpacing: Theme.space8
                enabled: root.hasCue && !root.appModel.subtitleBusy

                Text { text: "Kiểu nền"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
                ComboBox {
                    id: backgroundStyleBox
                    objectName: "backgroundStyleBox"
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    model: [{text: "Nền phẳng", value: "solid"},
                        {text: "Không nền", value: "none"},
                        {text: "Nền mờ (blur)", value: "blur"},
                        {text: "Mực loang", value: "ink_splash"},
                        {text: "Mực đại mặc", value: "ink_wash"},
                        {text: "Mực tụ", value: "ink_cloud"},
                        {text: "Nét cọ", value: "brush_stroke"}]
                    textRole: "text"; valueRole: "value"
                    font.family: Theme.fontFamily
                    font.pixelSize: 12
                    palette.button: Theme.surfaceRaised
                    palette.buttonText: Theme.textPrimary
                    palette.base: Theme.surfaceRaised
                    palette.text: Theme.textPrimary
                    palette.window: Theme.surfaceRaised
                    palette.highlight: Theme.primary
                    Accessible.name: "Kiểu nền"
                    currentIndex: Math.max(0, model.findIndex(function(item) {
                        return item.value === String(root.cue.background_style || "solid")
                    }))
                    onActivated: root.chooseBackground(currentValue)
                }

                Text { text: "Nền ngang (%)"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
                TextNumber {
                    id: backgroundPaddingX
                    objectName: "backgroundPaddingX"
                    Layout.fillWidth: true
                    from: 0; to: 2147483647
                    accessibleName: "Nền ngang (%)"
                    value: Number(root.cue.background_padding_x ?? 20)
                    onValueModified: root.applyCurrent({"paddingX": value})
                }

                Text { text: "Nền dọc (%)"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
                TextNumber {
                    id: backgroundPaddingY
                    objectName: "backgroundPaddingY"
                    Layout.fillWidth: true
                    from: 0; to: 2147483647
                    accessibleName: "Nền dọc (%)"
                    value: Number(root.cue.background_padding_y ?? 10)
                    onValueModified: root.applyCurrent({"paddingY": value})
                }

                Text { text: "Đục nền (%)"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
                TextNumber {
                    id: backgroundOpacitySpin
                    objectName: "backgroundOpacitySpin"
                    Layout.fillWidth: true
                    from: 0; to: 100
                    accessibleName: "Đục nền (%)"
                    value: Number(root.cue.background_opacity_percent ?? 90)
                    onValueModified: root.applyCurrent({"backgroundOpacity": value})
                }
                Text { text: "Màu nền"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
                RowLayout {
                    TextAction { text: "Màu"; compact: true; tooltipText: "Chọn màu nền cho các cue đang chọn."; onClicked: backgroundDialog.open();  }
                    TextAction { text: "Ống hút"; compact: true; tooltipText: "Lấy màu nền từ frame Preview."; onClicked: root.colorPickRequested("background");  }
                }
                Text { text: "Tùy chọn"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
                RowLayout {
                    TextAction { objectName: "removeCueBackgroundButton"; text: "Bỏ nền"; compact: true; tooltipText: "Tắt nền và blur tự động của các câu đang chọn."; onClicked: root.chooseBackground("none"); }
                    TextAction { text: "Mực tàu"; compact: true; tooltipText: "Áp dụng kiểu chữ và nền mực tàu cho các cue đang chọn."; onClicked: root.applyInkPreset();  }
                }
            }
            SectionTitle { text: "Hiệu ứng" }
            GridLayout {
                Layout.fillWidth: true
                columns: root.inspectorMode ? 2 : 6
                columnSpacing: Theme.space8
                rowSpacing: Theme.space8
                enabled: root.hasCue && root.isSceneText && !root.appModel.subtitleBusy
                Text { text: "Mờ vào/ra"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
                TextAction {
                    text: Boolean(root.cue.fade_enabled) ? "Đang bật" : "Đang tắt"
                    compact: true; primary: Boolean(root.cue.fade_enabled)
                    tooltipText: "Bật/tắt mờ dần hai đầu chữ cảnh."
                    onClicked: root.appModel.setSelectedSceneTextFade(
                        !Boolean(root.cue.fade_enabled), fadeDurationSpin.value
                    )
                }
                Text { text: "Mỗi đầu (ms)"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
                TextNumber {
                    id: fadeDurationSpin
                    objectName: "fadeDurationSpin"
                    Layout.fillWidth: true
                    from: 50; to: 2000; stepSize: 50
                    accessibleName: "Thời lượng mờ dần mỗi đầu tính bằng mili giây"
                    value: Number(root.cue.fade_duration_ms ?? 300)
                    onValueModified: root.appModel.setSelectedSceneTextFade(
                        Boolean(root.cue.fade_enabled), value
                    )
                }
            }
        }
    }
}
