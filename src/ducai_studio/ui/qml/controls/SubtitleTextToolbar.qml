import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import DucaiStudio 1.0

Rectangle {
    id: root
    objectName: "subtitleTextToolbar"

    required property var appModel
    property bool allowManualPlacement: false
    signal manualPlacementRequested()
    signal colorPickRequested(string target)

    readonly property var cue: root.appModel.selectedCue || ({})
    readonly property string cueId: String(cue.cue_id || "")
    readonly property bool hasCue: cueId.length > 0
    readonly property bool isSceneText:
        String(cue.cue_role || "dialogue") === "scene_text"
    readonly property string editedCueId: String(
        cue.translation_cue_id || cue.cue_id || ""
    )
    readonly property string editedLanguage:
        String(cue.translation_cue_id || "").length > 0 ? "translated" : "source"

    implicitHeight: toolbarColumn.implicitHeight + Theme.space16
    color: Theme.surfaceRaised
    border.width: 1
    border.color: root.hasCue ? Theme.info : Theme.borderSubtle
    radius: Theme.radiusSmall

    function overrideValue(values, key, fallback) {
        return values && values[key] !== undefined ? values[key] : fallback
    }

    function applyCurrent(values) {
        if (!root.hasCue || root.appModel.subtitleBusy) return
        const overrides = values || ({})
        root.appModel.setSelectedSubtitleTextFormat(
            Math.max(1, Math.min(100, Number(root.overrideValue(
                overrides, "fontSize", sizeSpin.value
            )))),
            String(root.overrideValue(
                overrides, "orientation", orientationBox.currentValue || "horizontal"
            )),
            String(root.overrideValue(
                overrides, "backgroundColor", root.cue.background_color || ""
            )),
            String(root.overrideValue(
                overrides, "textColor", root.cue.text_color || ""
            )),
            String(root.overrideValue(
                overrides, "styleHint", styleBox.currentValue || "standard"
            )),
            Math.max(0, Math.min(100, Number(root.overrideValue(
                overrides, "paddingX", backgroundPaddingX.value
            )))),
            Math.max(0, Math.min(100, Number(root.overrideValue(
                overrides, "paddingY", backgroundPaddingY.value
            )))),
            String(root.overrideValue(
                overrides, "backgroundStyle", backgroundStyleBox.currentValue || "solid"
            )),
            Math.max(0, Math.min(100, Number(root.overrideValue(
                overrides, "letterSpacing", letterSpacingSpin.value
            )))),
            Boolean(root.overrideValue(
                overrides, "fontBold", Boolean(root.cue.font_bold)
            )),
            Boolean(root.overrideValue(
                overrides, "fontItalic", Boolean(root.cue.font_italic)
            )),
            Math.max(0, Math.min(100, Number(root.overrideValue(
                overrides, "textOpacity", textOpacitySpin.value
            )))),
            Math.max(0, Math.min(100, Number(root.overrideValue(
                overrides, "backgroundOpacity", backgroundOpacitySpin.value
            ))))
        )
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

    ColumnLayout {
        id: toolbarColumn
        anchors.fill: parent
        anchors.margins: Theme.space8
        spacing: Theme.space4

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.space8

            Text {
                Layout.preferredWidth: 72
                text: root.hasCue
                    ? String(root.cue.subtitle_track_id || "S")
                        + " · " + Number(root.appModel.selectedCueCount || 1)
                        + " cue"
                    : "CHỮ"
                color: root.hasCue ? Theme.info : Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: 11
                font.weight: Font.DemiBold
            }

            DsTextField {
                id: textInput
                Layout.fillWidth: true
                Layout.minimumWidth: 180
                placeholderText: root.hasCue
                    ? "Nội dung cue đang chọn"
                    : "Cue gần nhất sẽ được giữ để bảng không nhảy"
                enabled: root.hasCue && !root.appModel.subtitleBusy
                Accessible.name: "Nội dung cue chữ đang chọn"
                Binding {
                    target: textInput
                    property: "text"
                    value: String(
                        root.cue.translated_text || root.cue.source_text || ""
                    )
                    when: !textInput.activeFocus
                }
                onEditingFinished: {
                    const current = String(
                        root.cue.translated_text || root.cue.source_text || ""
                    )
                    if (root.editedCueId.length > 0 && text.trim().length > 0
                            && text !== current) {
                        root.appModel.updateSubtitleText(
                            root.editedCueId, text, root.editedLanguage
                        )
                    }
                }
            }

            DsButton {
                visible: root.allowManualPlacement
                text: "Thêm chữ"
                compact: true
                primary: true
                tooltipText: "Thêm một cue thoại hoặc chữ trong cảnh tại playhead."
                enabled: root.appModel.currentProject.hasVideo
                    && !root.appModel.subtitleBusy
                disabledReason: "Cần mở video và chờ thao tác phụ đề hiện tại hoàn tất."
                onClicked: root.manualPlacementRequested()
            }

            DsButton {
                text: "Xóa"
                compact: true
                danger: true
                tooltipText: "Xóa cue đang chọn, gồm cả chữ và nền của cue."
                enabled: root.hasCue && !root.appModel.subtitleBusy
                disabledReason: "Hãy chọn một cue chữ trước."
                onClicked: root.appModel.deleteSubtitleCue(root.cueId)
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.space4

            Text {
                Layout.preferredWidth: 72
                text: "ĐỊNH DẠNG"
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: 10
                font.weight: Font.DemiBold
            }

            Text { text: "Cỡ"; color: Theme.textSecondary; font.pixelSize: 11 }
            DsSpinBox {
                id: sizeSpin
                Layout.preferredWidth: 104
                from: 1
                to: 100
                stepSize: 1
                enabled: root.hasCue && !root.appModel.subtitleBusy
                accessibleName: "Kích thước chữ từ 1 đến 100"
                Binding {
                    target: sizeSpin
                    property: "value"
                    value: Number(root.cue.text_font_size || 12)
                    when: !sizeSpin.activeFocus
                }
                onValueModified: root.applyCurrent({"fontSize": value})
            }

            ComboBox {
                id: orientationBox
                Layout.preferredWidth: 104
                implicitHeight: 36
                enabled: root.hasCue && !root.appModel.subtitleBusy
                model: [
                    { text: "Ngang", value: "horizontal" },
                    { text: "Dọc", value: "vertical" }
                ]
                textRole: "text"
                valueRole: "value"
                Accessible.name: "Hướng chữ"
                Binding {
                    target: orientationBox
                    property: "currentIndex"
                    value: root.cue.orientation === "vertical" ? 1 : 0
                    when: !orientationBox.popup.visible
                }
                onActivated: root.applyCurrent({"orientation": currentValue})
            }

            ComboBox {
                id: styleBox
                Layout.preferredWidth: 220
                Layout.maximumWidth: 280
                implicitHeight: 36
                enabled: root.hasCue && !root.appModel.subtitleBusy
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
                Accessible.name: "Kiểu chữ"
                Binding {
                    target: styleBox
                    property: "currentIndex"
                    value: {
                        const style = String(root.cue.style_hint || "standard")
                        if (style === "calligraphy_hurricane") return 9
                        if (style === "calligraphy_splash") return 8
                        if (style === "calligraphy_water") return 7
                        if (style === "calligraphy_brush") return 6
                        if (style === "calligraphy_ink") return 5
                        if (style === "calligraphy_bold") return 4
                        if (style === "calligraphy_soft") return 3
                        if (style === "calligraphy") return 2
                        if (style === "display") return 1
                        return 0
                    }
                    when: !styleBox.popup.visible
                }
                onActivated: root.applyCurrent({"styleHint": currentValue})
            }

            DsButton {
                text: "B"
                compact: true
                primary: Boolean(root.cue.font_bold)
                tooltipText: "Bật hoặc tắt chữ in đậm cho cue đang chọn."
                enabled: root.hasCue && !root.appModel.subtitleBusy
                onClicked: root.applyCurrent({"fontBold": !Boolean(root.cue.font_bold)})
                Accessible.name: "In đậm chữ"
            }
            DsButton {
                text: "I"
                compact: true
                primary: Boolean(root.cue.font_italic)
                tooltipText: "Bật hoặc tắt chữ in nghiêng cho cue đang chọn."
                enabled: root.hasCue && !root.appModel.subtitleBusy
                onClicked: root.applyCurrent({"fontItalic": !Boolean(root.cue.font_italic)})
                Accessible.name: "In nghiêng chữ"
            }
            Item { Layout.fillWidth: true }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.space4

            Text {
                Layout.preferredWidth: 72
                text: "HIỂN THỊ"
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: 10
                font.weight: Font.DemiBold
            }
            Text { text: "Giãn chữ"; color: Theme.textSecondary; font.pixelSize: 11 }
            DsSpinBox {
                id: letterSpacingSpin
                Layout.preferredWidth: 104
                from: 0
                to: 100
                stepSize: 1
                enabled: root.hasCue && !root.appModel.subtitleBusy
                accessibleName: "Khoảng cách chữ theo phần trăm cỡ chữ"
                Binding {
                    target: letterSpacingSpin
                    property: "value"
                    value: Number(root.cue.letter_spacing_percent || 0)
                    when: !letterSpacingSpin.activeFocus
                }
                onValueModified: root.applyCurrent({"letterSpacing": value})
            }
            Text { text: "Đục chữ"; color: Theme.textSecondary; font.pixelSize: 11 }
            DsSpinBox {
                id: textOpacitySpin
                Layout.preferredWidth: 104
                from: 0
                to: 100
                stepSize: 1
                enabled: root.hasCue && !root.appModel.subtitleBusy
                accessibleName: "Độ đục của chữ"
                Binding {
                    target: textOpacitySpin
                    property: "value"
                    value: Number(root.cue.text_opacity_percent ?? 100)
                    when: !textOpacitySpin.activeFocus
                }
                onValueModified: root.applyCurrent({"textOpacity": value})
            }
            DsButton {
                text: "Màu chữ"
                compact: true
                tooltipText: "Mở bảng chọn màu chữ cho cue đang chọn."
                enabled: root.hasCue && !root.appModel.subtitleBusy
                onClicked: textColorDialog.open()
            }
            DsButton {
                text: "Ống hút"
                compact: true
                tooltipText: "Lấy màu chữ trực tiếp từ một điểm trên Preview."
                enabled: root.hasCue && !root.appModel.subtitleBusy
                disabledReason: "Chọn cue trước rồi lấy màu trực tiếp trên Preview."
                onClicked: root.colorPickRequested("text")
            }
            Rectangle {
                Layout.preferredWidth: 24
                Layout.preferredHeight: 24
                radius: Theme.radiusSmall
                color: /^#[0-9a-fA-F]{6}$/.test(String(root.cue.text_color || ""))
                    ? root.cue.text_color : root.appModel.exportSubtitlePrimaryColor
                border.width: 1
                border.color: Theme.borderStrong
            }
            Item { Layout.fillWidth: true }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.space4

            Text {
                Layout.preferredWidth: 72
                text: "NỀN CHỮ"
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: 10
                font.weight: Font.DemiBold
            }
            ComboBox {
                id: backgroundStyleBox
                Layout.preferredWidth: 144
                implicitHeight: 36
                enabled: root.hasCue && root.isSceneText && !root.appModel.subtitleBusy
                model: [
                    { text: "Nền phẳng", value: "solid" },
                    { text: "Mực loang", value: "ink_splash" },
                    { text: "Mực đại mặc", value: "ink_wash" },
                    { text: "Mực tụ", value: "ink_cloud" },
                    { text: "Nét cọ", value: "brush_stroke" }
                ]
                textRole: "text"
                valueRole: "value"
                Accessible.name: "Kiểu nền chữ"
                Binding {
                    target: backgroundStyleBox
                    property: "currentIndex"
                    value: {
                        const style = String(root.cue.background_style || "solid")
                        if (style === "brush_stroke") return 4
                        if (style === "ink_cloud") return 3
                        if (style === "ink_wash") return 2
                        if (style === "ink_splash") return 1
                        return 0
                    }
                    when: !backgroundStyleBox.popup.visible
                }
                onActivated: root.applyCurrent({"backgroundStyle": currentValue})
            }
            Text {
                text: "Ngang"
                color: Theme.textSecondary
                font.pixelSize: 11
                Accessible.name: "Độ rộng nền theo chiều ngang"
            }
            DsSpinBox {
                id: backgroundPaddingX
                Layout.preferredWidth: 104
                from: 0
                to: 100
                stepSize: 1
                enabled: root.hasCue && root.isSceneText && !root.appModel.subtitleBusy
                accessibleName: "Độ rộng nền chữ"
                Binding {
                    target: backgroundPaddingX
                    property: "value"
                    value: Number(root.cue.background_padding_x ?? 20)
                    when: !backgroundPaddingX.activeFocus
                }
                onValueModified: root.applyCurrent({"paddingX": value})
            }
            Text {
                text: "Dọc"
                color: Theme.textSecondary
                font.pixelSize: 11
                Accessible.name: "Độ rộng nền theo chiều dọc"
            }
            DsSpinBox {
                id: backgroundPaddingY
                Layout.preferredWidth: 104
                from: 0
                to: 100
                stepSize: 1
                enabled: root.hasCue && root.isSceneText && !root.appModel.subtitleBusy
                accessibleName: "Độ cao nền chữ"
                Binding {
                    target: backgroundPaddingY
                    property: "value"
                    value: Number(root.cue.background_padding_y ?? 10)
                    when: !backgroundPaddingY.activeFocus
                }
                onValueModified: root.applyCurrent({"paddingY": value})
            }
            Text { text: "Đục nền"; color: Theme.textSecondary; font.pixelSize: 11 }
            DsSpinBox {
                id: backgroundOpacitySpin
                Layout.preferredWidth: 104
                from: 0
                to: 100
                stepSize: 1
                enabled: root.hasCue && !root.appModel.subtitleBusy
                accessibleName: "Độ đục của nền"
                Binding {
                    target: backgroundOpacitySpin
                    property: "value"
                    value: Number(root.cue.background_opacity_percent ?? 90)
                    when: !backgroundOpacitySpin.activeFocus
                }
                onValueModified: root.applyCurrent({"backgroundOpacity": value})
            }
            Item { Layout.fillWidth: true }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.space4

            Text {
                Layout.preferredWidth: 72
                text: "MÀU & HIỆU ỨNG"
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: 10
                font.weight: Font.DemiBold
            }
            DsButton {
                text: "Màu nền"
                compact: true
                tooltipText: "Mở bảng chọn màu nền của cue."
                enabled: root.hasCue && !root.appModel.subtitleBusy
                onClicked: backgroundDialog.open()
            }
            DsButton {
                text: "Ống hút nền"
                compact: true
                tooltipText: "Lấy màu nền trực tiếp từ một điểm trên Preview."
                enabled: root.hasCue && !root.appModel.subtitleBusy
                disabledReason: "Chọn cue trước rồi lấy màu trực tiếp trên Preview."
                onClicked: root.colorPickRequested("background")
                Accessible.name: "Ống hút lấy màu nền từ Preview"
            }
            Rectangle {
                Layout.preferredWidth: 24
                Layout.preferredHeight: 24
                radius: Theme.radiusSmall
                color: /^#[0-9a-fA-F]{6}$/.test(
                    String(root.cue.background_color || "")
                ) ? root.cue.background_color : "transparent"
                border.width: 1
                border.color: Theme.borderStrong
            }
            DsButton {
                text: "Bỏ nền"
                compact: true
                tooltipText: "Bỏ màu nền nhưng giữ nguyên nội dung cue."
                enabled: root.hasCue
                    && String(root.cue.background_color || "").length > 0
                    && !root.appModel.subtitleBusy
                onClicked: root.applyCurrent({"backgroundColor": ""})
                Accessible.name: "Bỏ màu nền của cue chữ"
            }
            DsButton {
                text: "Mực tàu"
                compact: true
                primary: root.cue.background_style === "ink_splash"
                tooltipText: "Áp nhanh font thư pháp và nền mực tàu cho chữ trong cảnh."
                enabled: root.hasCue && root.isSceneText && !root.appModel.subtitleBusy
                disabledReason: "Preset mực tàu chỉ áp dụng cho cue chữ trong cảnh S2 trở đi."
                onClicked: root.applyInkPreset()
                Accessible.name: "Áp preset chữ và nền mực tàu"
            }
            DsButton {
                text: "Mờ vào/ra"
                compact: true
                primary: Boolean(root.cue.fade_enabled)
                tooltipText: "Cho chữ trong cảnh xuất hiện và biến mất mờ dần."
                enabled: root.hasCue && root.isSceneText && !root.appModel.subtitleBusy
                disabledReason: "Hiệu ứng mờ vào/ra chỉ áp dụng cho chữ trong cảnh."
                onClicked: root.appModel.setSelectedSceneTextFade(
                    !Boolean(root.cue.fade_enabled), fadeDurationSpin.value
                )
                Accessible.name: "Bật hoặc tắt hiệu ứng chữ xuất hiện và biến mất mờ dần"
            }
            Text { text: "Mỗi đầu"; color: Theme.textSecondary; font.pixelSize: 11 }
            DsSpinBox {
                id: fadeDurationSpin
                Layout.preferredWidth: 112
                from: 50
                to: 2000
                stepSize: 50
                enabled: root.hasCue && root.isSceneText && !root.appModel.subtitleBusy
                accessibleName: "Thời lượng mờ dần mỗi đầu tính bằng mili giây"
                Binding {
                    target: fadeDurationSpin
                    property: "value"
                    value: Number(root.cue.fade_duration_ms ?? 300)
                    when: !fadeDurationSpin.activeFocus
                }
                onValueModified: root.appModel.setSelectedSceneTextFade(
                    Boolean(root.cue.fade_enabled), value
                )
            }
            Text { text: "ms"; color: Theme.textSecondary; font.pixelSize: 11 }
            Item { Layout.fillWidth: true }
        }
    }
}
