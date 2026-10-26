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
    property string inspectorTab: appModel.uiStringValue("subtitleInspectorTab", "qa")
    property bool headedWebEnabled: false
    property bool guestWebEnabled: true
    property bool findBarOpen: false
    property string pendingReplaceFind: ""
    property string pendingReplacement: ""
    property int pendingReplaceCount: 0
    property int pendingReplaceCueCount: 0
    readonly property bool whisperExtraction:
        appModel.ocrExtractionMethod === "whisper_audio"
    property string contextCueId: ""
    property int contextCueIndex: 0
    property string contextCueStart: ""
    property string contextCueEnd: ""
    property string contextCueRole: "dialogue"
    property string contextCueReviewState: "accepted"
    property string regionEditorMode: ""
    property string translationPreferencesSyncKey: ""
    readonly property var translationProviderIds: [
        "gemini-web", "gemini-api", "gemini-cli", "deepseek-api",
        "tokenhub-api", "qwen-api", "glm-api", "openai-compatible"
    ]
    readonly property var translationProviderLabels: [
        "Gemini Web", "Gemini API", "Gemini CLI", "DeepSeek API",
        "Hy3 · TokenHub", "Qwen API", "GLM API", "OpenAI-compatible"
    ]
    readonly property var translationProviderDefaults: [
        "selected-on-web", "gemini-3.5-flash-lite", "gemini-2.5-flash",
        "deepseek-v4-flash", "hy3", "qwen3.7-flash", "glm-5.2", ""
    ]
    readonly property var translationProviderModels: [
        ["selected-on-web"],
        ["gemini-3.5-flash-lite", "gemini-2.5-flash"],
        ["gemini-2.5-flash"],
        ["deepseek-v4-flash", "deepseek-v4-pro"],
        ["hy3", "hy-mt2-pro", "hy-mt2-plus", "deepseek-v4-flash", "glm-5.2"],
        ["qwen3.7-flash", "qwen3.7-plus", "qwen3.8-max", "qwen-plus"],
        ["glm-5.2", "glm-5.1", "glm-4.7-flash", "glm-4.5-air"],
        []
    ]
    readonly property var translationStyleIds: [
        "auto", "xianxia", "wuxia", "palace", "modern", "neutral"
    ]
    readonly property var translationStyleLabels: [
        "Tự nhận diện", "Tu tiên cổ trang", "Võ hiệp",
        "Cung đình/cổ trang", "Hiện đại", "Trung tính"
    ]
    readonly property var selectedTranslationCredentialReferences:
        providerBox.currentIndex === 1 ? appModel.geminiApiCredentialReferences
        : (providerBox.currentIndex === 3 ? appModel.deepseekApiCredentialReferences
        : (providerBox.currentIndex === 4 ? appModel.tokenhubApiCredentialReferences
        : (providerBox.currentIndex === 5 ? appModel.qwenApiCredentialReferences
        : (providerBox.currentIndex === 6 ? appModel.glmApiCredentialReferences
        : (providerBox.currentIndex === 7 ? appModel.openaiCompatibleCredentialReferences : [])))))
    function selectedTranslationProviderId() {
        return translationProviderIds[providerBox.currentIndex] || "gemini-web"
    }
    function selectedTranslationProviderIsApi() {
        var id = selectedTranslationProviderId()
        return id === "gemini-api" || id === "deepseek-api" || id === "tokenhub-api"
            || id === "qwen-api" || id === "glm-api" || id === "openai-compatible"
    }
    function selectedTranslationProviderIsCustom() {
        return selectedTranslationProviderId() === "openai-compatible"
    }
    function selectedTranslationModelId() {
        return selectedTranslationProviderId() === "gemini-web"
            ? "selected-on-web" : translationModel.chosenModel.trim()
    }
    readonly property bool translationChoiceMatchesJob:
        appModel.translationJobState.length > 0 && appModel.translationSelectionMatches(
            selectedTranslationProviderId(), selectedTranslationModelId(), customBaseUrl.text,
            root.selectedTranslationProviderIsApi() ? savedCredentialBox.currentText : ""
        )
    readonly property bool translationChoiceChanged:
        ["paused", "queued", "cancelled", "failed", "interrupted", "review"]
            .indexOf(appModel.translationJobState) >= 0 && !translationChoiceMatchesJob
    readonly property bool translationStyleEditable:
        ["running", "start_requested", "pause_requested", "resume_requested", "cancel_requested"]
            .indexOf(appModel.translationJobState) < 0

    Component.onCompleted: {
        var savedProvider = appModel.uiStringValue("translation/provider", "gemini-web")
        var savedIndex = translationProviderIds.indexOf(savedProvider)
        if (savedIndex >= 0)
            providerBox.currentIndex = savedIndex
        root.syncTranslationPreferences()
    }
    function syncTranslationPreferences() {
        var syncKey = JSON.stringify({
            projectId: appModel.currentProject.projectId,
            preset: appModel.currentProject.translationStylePreset,
            rules: appModel.currentProject.translationCustomRules,
            styles: appModel.currentProject.translationStylePresets,
            activeName: appModel.currentProject.translationActiveStyleName
        })
        // Cue saves also emit changed; they must not reset draft style inputs.
        if (syncKey === translationPreferencesSyncKey)
            return
        translationPreferencesSyncKey = syncKey
        var styleIndex = translationStyleIds.indexOf(
            appModel.currentProject.translationStylePreset
        )
        if (styleIndex < 0) styleIndex = 0
        if (translationStyleBox.currentIndex !== styleIndex)
            translationStyleBox.currentIndex = styleIndex
        if (!translationRules.activeFocus)
            translationRules.text = appModel.currentProject.translationCustomRules
        var savedStyles = appModel.currentProject.translationStylePresets || []
        var matchingIndex = -1
        for (var index = 0; index < savedStyles.length; index++) {
            var saved = savedStyles[index]
            if (saved.name === appModel.currentProject.translationActiveStyleName) {
                matchingIndex = index
                break
            }
        }
        if (savedTranslationStyleBox.currentIndex !== matchingIndex)
            savedTranslationStyleBox.currentIndex = matchingIndex
        if (!translationStyleName.activeFocus) {
            translationStyleName.text = matchingIndex >= 0
                ? String(savedStyles[matchingIndex].name || "") : ""
        }
    }
    function loadSavedTranslationStyle(index) {
        var savedStyles = appModel.currentProject.translationStylePresets || []
        if (index < 0 || index >= savedStyles.length)
            return
        var saved = savedStyles[index]
        var styleIndex = translationStyleIds.indexOf(String(saved.preset || "auto"))
        if (styleIndex < 0) styleIndex = 0
        translationStyleBox.currentIndex = styleIndex
        translationStyleName.text = String(saved.name || "")
        translationRules.text = String(saved.custom_rules || "")
        root.appModel.setTranslationPreferences(
            root.translationStyleIds[styleIndex] || "auto",
            translationRules.text,
            String(saved.name || "")
        )
    }
    readonly property string workspaceMode: appModel.currentWorkspace
    readonly property string workspaceTitle: workspaceMode === "transcribe"
        ? "Trích xuất phụ đề"
        : (workspaceMode === "translate" ? "Dịch thuật" : "Biên tập phụ đề")
    function translationQaStatusLabel(status) {
        if (status === "PASS") return "Đạt"
        if (status === "REVIEW") return "Cần sửa"
        if (status === "PENDING") return "Đang QA"
        return "Chưa có kết quả QA"
    }

    function translationQaStatusColor(status) {
        if (status === "PASS") return Theme.success
        if (status === "REVIEW") return Theme.error
        if (status === "PENDING") return Theme.warning
        return Theme.textMuted
    }
    function translationJobStateLabel(state) {
        const labels = {
            "queued": "Đang chờ",
            "start_requested": "Đang khởi động",
            "running": "Đang dịch",
            "pause_requested": "Đang tạm dừng",
            "paused": "Đã tạm dừng",
            "resume_requested": "Đang tiếp tục từ cache",
            "cancel_requested": "Đang hủy",
            "cancelled": "Đã hủy — có thể tiếp tục từ cache",
            "failed": "Tạm dừng do lỗi — cache vẫn an toàn",
            "interrupted": "Bị gián đoạn — có thể tiếp tục từ cache",
            "review": "Cần sửa QA — có thể tiếp tục từ cache",
            "succeeded": "Đã dịch xong"
        }
        return labels[state] || "Chưa chạy"
    }
    activeFocusOnTab: true
    focus: true
    Accessible.name: workspaceTitle

    FileDialog {
        id: translationImportDialog
        title: "Nhập SRT Việt đã dịch bên ngoài"
        fileMode: FileDialog.OpenFile
        nameFilters: ["Phụ đề SRT (*.srt)", "Tất cả file (*)"]
        onAccepted: root.appModel.importTranslationPath(selectedFile.toString())
    }

    FileDialog {
        id: sourceSrtExportDialog
        title: "Xuất SRT nguồn"
        fileMode: FileDialog.SaveFile
        nameFilters: ["Phụ đề SRT (*.srt)"]
        defaultSuffix: "srt"
        onAccepted: root.appModel.exportSubtitleSrt("source", selectedFile.toString())
    }

    FileDialog {
        id: translatedSrtExportDialog
        title: "Xuất SRT tiếng Việt"
        fileMode: FileDialog.SaveFile
        nameFilters: ["Phụ đề SRT (*.srt)"]
        defaultSuffix: "srt"
        onAccepted: root.appModel.exportSubtitleSrt("translated", selectedFile.toString())
    }

    ColorDialog {
        id: subtitleTextColorDialog
        title: "Chọn màu chữ phụ đề"
        onAccepted: root.appModel.setExportSubtitlePrimaryColor(
            selectedColor.toString()
        )
    }

    ManualSubtitleCueDialog {
        id: manualCueDialog
        appModel: root.appModel
        onPlacementRequested: {
            subtitlePreview.pause()
            root.regionEditorMode = "manual_text"
        }
        onCueSubmitted: root.regionEditorMode = ""
    }

    Dialog {
        id: replaceConfirmDialog
        parent: Overlay.overlay
        anchors.centerIn: Overlay.overlay
        width: 460
        modal: true
        title: "Thay tất cả trong bản dịch Việt?"
        closePolicy: Popup.CloseOnEscape

        contentItem: Text {
            width: 420
            wrapMode: Text.Wrap
            text: "Tìm thấy " + root.pendingReplaceCount + " lần trong "
                + root.pendingReplaceCueCount + " cue Việt. Bạn có muốn thay “"
                + root.pendingReplaceFind + "” "
                + (root.pendingReplacement.length > 0
                    ? "bằng “" + root.pendingReplacement + "”?"
                    : "bằng chuỗi rỗng?")
            color: Theme.textPrimary
            font.family: Theme.fontFamily
            font.pixelSize: 12
        }

        footer: RowLayout {
            spacing: Theme.space8
            Item { Layout.fillWidth: true }
            DsButton {
                text: "Hủy"
                onClicked: replaceConfirmDialog.reject()
            }
            DsButton {
                primary: true
                text: "Thay " + root.pendingReplaceCount + " lần"
                onClicked: {
                    const find = root.pendingReplaceFind
                    const replacement = root.pendingReplacement
                    const count = root.pendingReplaceCount
                    replaceConfirmDialog.accept()
                    root.appModel.findReplaceSubtitles(find, replacement, count)
                }
            }
        }
    }

    Popup {
        id: cueContextMenu

        function openAt(sourceItem, localX, localY) {
            const popupPoint = sourceItem.mapToItem(
                Overlay.overlay, localX, localY
            )
            x = Math.max(
                Theme.space8,
                Math.min(Overlay.overlay.width - width - Theme.space8, popupPoint.x)
            )
            y = Math.max(
                Theme.space8,
                Math.min(Overlay.overlay.height - height - Theme.space8, popupPoint.y)
            )
            open()
        }

        parent: Overlay.overlay
        width: 240
        height: cueContextColumn.implicitHeight + padding * 2
        padding: Theme.space4
        popupType: Popup.Item
        modal: false
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        z: 1000
        background: Rectangle {
            color: Theme.surfaceRaised
            border.width: 1
            border.color: Theme.borderStrong
            radius: Theme.radiusSmall
        }
        contentItem: ColumnLayout {
            id: cueContextColumn
            spacing: 0
            DsContextButton {
                Layout.fillWidth: true
                visible: root.contextCueRole === "scene_text"
                    && root.contextCueReviewState === "review"
                text: "Chấp nhận chữ trong cảnh"
                enabled: root.contextCueId.length > 0 && !root.appModel.subtitleBusy
                onClicked: {
                    const cueId = root.contextCueId
                    cueContextMenu.close()
                    root.appModel.acceptSceneTextCue(cueId)
                }
            }
            DsContextButton {
                Layout.fillWidth: true
                danger: true
                text: "Xóa cue này"
                enabled: root.contextCueId.length > 0 && !root.appModel.subtitleBusy
                onClicked: {
                    cueContextMenu.close()
                    deleteCueDialog.open()
                }
            }
        }
    }

    Dialog {
        id: deleteCueDialog
        parent: Overlay.overlay
        anchors.centerIn: Overlay.overlay
        width: 460
        modal: true
        title: "Xóa cue?"
        closePolicy: Popup.CloseOnEscape
        contentItem: Text {
            width: 420
            wrapMode: Text.Wrap
            text: "Bạn sắp xóa cue #" + root.contextCueIndex + " ("
                + root.contextCueStart + " – " + root.contextCueEnd + "). "
                + "Cả dòng nguồn và dòng dịch Việt tương ứng sẽ được gỡ để không lệch timestamp. "
                + "File SRT, media và cache không bị xóa; có thể bấm Hoàn tác."
            color: Theme.textPrimary
            font.family: Theme.fontFamily
            font.pixelSize: 12
        }
        footer: RowLayout {
            spacing: Theme.space8
            Item { Layout.fillWidth: true }
            DsButton {
                text: "Hủy"
                onClicked: deleteCueDialog.reject()
            }
            DsButton {
                danger: true
                text: "Xóa cue"
                enabled: root.contextCueId.length > 0 && !root.appModel.subtitleBusy
                onClicked: {
                    const cueId = root.contextCueId
                    deleteCueDialog.accept()
                    root.appModel.deleteSubtitleCue(cueId)
                }
            }
        }
    }

    ColorDialog {
        id: subtitleOutlineColorDialog
        title: "Chọn màu viền phụ đề"
        onAccepted: root.appModel.setExportSubtitleOutlineColor(
            selectedColor.toString()
        )
    }

    function selectInspectorTab(value) {
        inspectorTab = value
        appModel.setUiStringValue("subtitleInspectorTab", value)
    }

    // TextArea handles the secondary mouse button itself (native edit menu),
    // so the row-level TapHandler never receives a right click made directly
    // on source/translated text. Route those clicks through the same cue menu
    // as the rest of the row while preserving left-click text editing.
    function openCueContext(row, localX, localY) {
        root.appModel.selectCue(row.model.cueId, row.model.startUs)
        root.contextCueId = row.model.cueId
        root.contextCueIndex = row.model.cueIndex
        root.contextCueStart = row.model.startText
        root.contextCueEnd = row.model.endText
        root.contextCueRole = row.model.cueRole
        root.contextCueReviewState = row.model.reviewState
        cueContextMenu.openAt(row, localX, localY)
    }

    Timer {
        id: persistWidth
        interval: 250
        onTriggered: root.appModel.setUiRealValue(
            "subtitleInspectorRatio", inspector.width / Math.max(1, root.width)
        )
    }

    SplitView {
        anchors.fill: parent
        anchors.margins: Theme.space12
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

        ColumnLayout {
            SplitView.fillWidth: true
            SplitView.minimumWidth: 620
            spacing: Theme.space8

            Panel {
                Layout.fillWidth: true
                Layout.preferredHeight: 56

                RowLayout {
                    anchors.fill: parent
                    anchors.margins: Theme.space12
                    spacing: Theme.space12

                    SectionTitle {
                        Layout.fillWidth: true
                        text: root.workspaceTitle
                    }

                    DsButton {
                        text: "Trích xuất"
                        compact: true
                        primary: root.workspaceMode === "transcribe"
                        onClicked: root.appModel.navigate("transcribe")
                    }
                    DsButton {
                        text: "Dịch thuật"
                        compact: true
                        primary: root.workspaceMode === "translate"
                        onClicked: root.appModel.navigate("translate")
                    }
                    DsButton {
                        text: "Biên tập"
                        compact: true
                        primary: root.workspaceMode === "subtitles"
                        onClicked: root.appModel.navigate("subtitles")
                    }
                }
            }

            Panel {
                Layout.fillWidth: true
                Layout.preferredHeight: 196
                visible: root.workspaceMode === "transcribe"
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: Theme.space8
                    spacing: Theme.space4
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.space8
                        Text {
                            text: "Nguồn trích xuất"
                            color: Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: 11
                        }
                        ComboBox {
                            id: extractionMethodBox
                            Layout.preferredWidth: 184
                            model: ["OCR khung hình", "Whisper âm thanh"]
                            currentIndex: root.whisperExtraction ? 1 : 0
                            enabled: !root.appModel.ocrBusy
                            Accessible.name: "Phương thức trích xuất phụ đề"
                            onActivated: root.appModel.setOcrExtractionMethod(
                                currentIndex === 1 ? "whisper_audio" : "frame_ocr"
                            )
                        }
                        Text {
                            Layout.fillWidth: true
                            text: root.whisperExtraction
                                ? root.appModel.whisperHealthText
                                : "Đọc phụ đề cứng trực tiếp từ từng khung hình video"
                            color: root.whisperExtraction && !root.appModel.whisperReady
                                ? Theme.warning : Theme.textMuted
                            elide: Text.ElideRight
                            font.family: Theme.fontFamily
                            font.pixelSize: 11
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.space8
                        Text {
                            Layout.fillWidth: true
                            text: root.whisperExtraction
                                ? "large-v3 · tiếng Trung · beam 5 · timestamp theo từ"
                                : "Tốc độ quét"
                            color: Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: 11
                        }
                        Timer {
                            id: invalidOcrFpsReset
                            interval: 1200
                            repeat: false
                            onTriggered: ocrFps.restoreIfInvalid()
                        }
                        DsTextField {
                            id: ocrFps
                            property string lastValidText: "1.25"
                            readonly property real numericValue: parseFps(text)
                            readonly property bool valueIsValid: isFpsTextValid(text)

                            function parseFps(value) {
                                return Number(String(value).replace(",", "."))
                            }

                            function isFpsTextValid(value) {
                                const parsed = parseFps(value)
                                return isFinite(parsed) && parsed >= 0.67 && parsed <= 2.5
                            }

                            function restoreIfInvalid() {
                                if (!isFpsTextValid(text)) text = lastValidText
                            }

                            Layout.preferredWidth: 64
                            visible: !root.whisperExtraction
                            text: Number(root.appModel.ocrSampleFps).toFixed(2)
                            inputMethodHints: Qt.ImhFormattedNumbersOnly
                            validator: RegularExpressionValidator {
                                regularExpression: /^[0-9]+([\.,][0-9]{0,2})?$/
                            }
                            Accessible.name: "Số khung hình OCR mỗi giây"
                            onTextChanged: {
                                if (isFpsTextValid(text)) {
                                    lastValidText = text
                                    invalidOcrFpsReset.stop()
                                } else {
                                    invalidOcrFpsReset.restart()
                                }
                            }
                            onEditingFinished: {
                                restoreIfInvalid()
                                if (valueIsValid)
                                    root.appModel.setOcrSampleFps(numericValue)
                            }
                            onActiveFocusChanged: {
                                if (!activeFocus) restoreIfInvalid()
                            }
                        }
                        DsButton {
                            Layout.preferredWidth: 178
                            primary: !warning
                            warning: ["running", "pause_requested"]
                                .indexOf(root.appModel.ocrJobState) >= 0
                            text: root.appModel.ocrJobState === "running"
                                ? (root.whisperExtraction
                                    ? "Tạm dừng Whisper" : "Tạm dừng OCR")
                                : (root.appModel.ocrJobState === "paused"
                                    ? (root.whisperExtraction
                                        ? "Tiếp tục Whisper" : "Tiếp tục OCR")
                                    : (root.appModel.ocrJobState === "queued"
                                        ? (root.whisperExtraction
                                            ? "Bắt đầu Whisper" : "Bắt đầu OCR")
                                        : (["failed", "interrupted"]
                                            .indexOf(root.appModel.ocrJobState) >= 0
                                            ? (root.whisperExtraction
                                                ? "Tiếp tục Whisper từ cache"
                                                : "Tiếp tục OCR từ cache")
                                            : (root.appModel.currentProject.subtitleCueModel.count > 0
                                                ? (root.whisperExtraction
                                                    ? "Whisper lại · ghi đè SRT"
                                                    : "OCR lại · ghi đè SRT")
                                                : (root.whisperExtraction
                                                    ? "Chạy Whisper" : "Chạy OCR")))))
                            operationState: ["succeeded", "cancelled", ""]
                                .indexOf(root.appModel.ocrJobState) >= 0
                                ? "idle" : root.appModel.ocrJobState
                            runningText: root.whisperExtraction
                                ? "Tạm dừng Whisper" : "Tạm dừng OCR"
                            queuedText: root.whisperExtraction
                                ? "Bắt đầu Whisper" : "Bắt đầu OCR"
                            startingText: root.whisperExtraction
                                ? "Đang khởi động Whisper…" : "Đang khởi động OCR…"
                            pausingText: root.whisperExtraction
                                ? "Đang tạm dừng Whisper…" : "Đang tạm dừng OCR…"
                            pausedText: root.whisperExtraction
                                ? "Tiếp tục Whisper" : "Tiếp tục OCR"
                            resumingText: root.whisperExtraction
                                ? "Đang tiếp tục Whisper…" : "Đang tiếp tục OCR…"
                            cancellingText: root.whisperExtraction
                                ? "Đang hủy Whisper…" : "Đang hủy OCR…"
                            failedText: root.appModel.ocrJobState === "failed"
                                ? (root.whisperExtraction
                                    ? "Tiếp tục Whisper từ cache"
                                    : "Tiếp tục OCR từ cache")
                                : (root.whisperExtraction ? "Khôi phục Whisper" : "Khôi phục OCR")
                            feedbackText: root.appModel.ocrJobState === "running"
                                ? (root.whisperExtraction
                                    ? "Đang yêu cầu tạm dừng Whisper"
                                    : "Đang yêu cầu tạm dừng OCR")
                                : (root.appModel.ocrJobState === "paused"
                                    ? (root.whisperExtraction
                                        ? "Đang tiếp tục Whisper từ cache"
                                        : "Đang tiếp tục OCR từ cache")
                                    : (root.whisperExtraction
                                        ? "Đang bắt đầu Whisper"
                                        : "Đang bắt đầu OCR"))
                            enabled: root.appModel.currentProject.hasVideo
                                && (!root.whisperExtraction || root.appModel.whisperReady)
                                && ["start_requested", "pause_requested",
                                    "resume_requested", "cancel_requested"]
                                    .indexOf(root.appModel.ocrJobState) < 0
                                && (["", "succeeded", "cancelled"]
                                        .indexOf(root.appModel.ocrJobState) < 0
                                    || root.whisperExtraction || ocrFps.valueIsValid)
                            disabledReason: !root.appModel.currentProject.hasVideo
                                ? "Hãy nhập video trước."
                                : (root.whisperExtraction && !root.appModel.whisperReady
                                    ? "Hãy cài runtime và model Whisper large-v3 trước."
                                : (!root.whisperExtraction && !ocrFps.valueIsValid
                                    ? "FPS OCR phải nằm trong khoảng 0,67–2,5."
                                    : "Tác vụ trích xuất đang chuyển trạng thái; hãy chờ xác nhận."))
                            onClicked: root.appModel.toggleOcr(
                                ocrFps.valueIsValid
                                    ? ocrFps.numericValue : root.appModel.ocrSampleFps
                            )
                        }
                        DsButton {
                            text: "Hủy"
                            danger: true
                            operationState: root.appModel.ocrJobState === "cancel_requested"
                                ? "cancel_requested" : "idle"
                            cancellingText: root.whisperExtraction
                                ? "Đang hủy Whisper…" : "Đang hủy OCR…"
                            feedbackText: root.whisperExtraction
                                ? "Đang yêu cầu hủy Whisper" : "Đang yêu cầu hủy OCR"
                            enabled: ["queued", "running", "pause_requested", "paused",
                                "resume_requested"].indexOf(root.appModel.ocrJobState) >= 0
                            disabledReason: "Không có tác vụ trích xuất đang hoạt động."
                            onClicked: root.appModel.cancelOcr()
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        CheckBox {
                            objectName: "sceneOcrToggle"
                            visible: !root.whisperExtraction
                            text: "OCR chữ trong cảnh (S2…)"
                            checked: root.appModel.ocrSceneTextEnabled
                            enabled: !root.appModel.ocrBusy
                            Accessible.name: text
                            onToggled: root.appModel.setOcrSceneTextEnabled(checked)
                            ToolTip.visible: hovered
                            ToolTip.text: "Tắt để lần trích xuất mới chỉ quét thoại. Không xóa chữ cảnh đã có; Multi dùng cùng lựa chọn."
                        }
                        CheckBox {
                            objectName: "castingAfterOcrToggle"
                            text: "Phân vai thoại sau trích xuất"
                            checked: ((root.appModel.dialogueCasting || {}).config || {}).enabled === true
                            enabled: root.appModel.currentProject.hasProject
                                && !root.appModel.ocrBusy && !root.appModel.ttsBusy
                                && !root.appModel.dialogueCastingBusy
                                && !root.appModel.dialogueCastingSaving
                                && !root.appModel.dialogueCastingLoading
                            Accessible.name: text
                            onToggled: root.appModel.setDialogueCastingEnabled(checked)
                            ToolTip.visible: hovered
                            ToolTip.text: "Tự nhận diện Nam/Nữ sau khi trích xuất thành công. Tạo giọng dùng lại kết quả; chọn giọng và sửa nhãn trong Phòng thu giọng."
                        }
                        Item { Layout.fillWidth: true }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.space8
                        Text {
                            Layout.fillWidth: true
                            text: root.appModel.currentProject.ocrDialogueRegion.length === 4
                                ? "S1 dùng vùng thoại đã chọn · ngoài vùng là S2"
                                : "Mặc định dùng vùng phụ đề hiện tại · ngoài vùng là S2"
                            color: Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: 10
                            elide: Text.ElideRight
                        }
                        DsButton {
                            text: root.regionEditorMode === "dialogue"
                                ? "Đang chọn vùng S1…" : "Quét vùng OCR"
                            compact: true
                            primary: root.regionEditorMode === "dialogue"
                            enabled: root.appModel.currentProject.hasVideo
                                && !root.appModel.ocrBusy
                            disabledReason: "Cần mở video và dừng tác vụ OCR trước."
                            onClicked: {
                                subtitlePreview.pause()
                                root.regionEditorMode = "dialogue"
                            }
                        }
                        DsButton {
                            text: root.regionEditorMode === "exclusion"
                                ? "Đang chọn vùng bỏ qua…" : "Vùng không OCR"
                            compact: true
                            warning: root.regionEditorMode === "exclusion"
                            enabled: root.appModel.currentProject.hasVideo
                                && !root.appModel.ocrBusy
                            disabledReason: "Cần mở video và dừng tác vụ OCR trước."
                            onClicked: {
                                subtitlePreview.pause()
                                root.regionEditorMode = "exclusion"
                            }
                        }
                        DsButton {
                            text: "Xóa vùng cuối"
                            compact: true
                            enabled: root.appModel.currentProject.ocrExclusionRegions.length > 0
                                && !root.appModel.ocrBusy
                            disabledReason: "Chưa có vùng không OCR để xóa."
                            onClicked: {
                                const values = root.appModel.currentProject.ocrExclusionRegions.slice()
                                values.pop()
                                root.appModel.setOcrRegions(
                                    root.appModel.currentProject.ocrDialogueRegion,
                                    values
                                )
                            }
                        }
                        DsButton {
                            text: "Đặt lại vùng"
                            compact: true
                            enabled: (root.appModel.currentProject.ocrDialogueRegion.length === 4
                                    || root.appModel.currentProject.ocrExclusionRegions.length > 0)
                                && !root.appModel.ocrBusy
                            disabledReason: "Đang dùng vùng OCR mặc định."
                            onClicked: {
                                root.regionEditorMode = ""
                                root.appModel.setOcrRegions([], [])
                            }
                        }
                        DsButton {
                            text: "Hủy chọn vùng"
                            compact: true
                            visible: root.regionEditorMode.length > 0
                            onClicked: root.regionEditorMode = ""
                        }
                    }
                }
            }

            Panel {
                Layout.fillWidth: true
                Layout.preferredHeight: root.selectedTranslationProviderIsCustom()
                    ? 342 : (root.selectedTranslationProviderIsApi() ? 306 : 246)
                visible: root.workspaceMode === "translate"
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: Theme.space8
                    spacing: Theme.space4
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.space8
                        ComboBox {
                            id: providerBox
                            objectName: "translationProviderChooser"
                            Layout.preferredWidth: 164
                            model: root.translationProviderLabels
                            Accessible.name: "Nguồn dịch và model API"
                            onCurrentIndexChanged: {
                                root.appModel.clearTranslationModelCatalog()
                                var defaults = root.translationProviderModels[currentIndex] || []
                                var fallback = defaults.length > 0 ? defaults[0]
                                    : root.translationProviderDefaults[currentIndex] || ""
                                translationModel.setModelText(root.appModel.uiStringValue(
                                    "translation/model/" + root.selectedTranslationProviderId(), fallback
                                ))
                                customBaseUrl.text = root.appModel.uiStringValue(
                                    "translationBaseUrl", "https://api.openai.com/v1"
                                )
                                apiCredentialInput.text = ""
                                Qt.callLater(savedCredentialBox.restoreSelection)
                            }
                            onActivated: root.appModel.setUiStringValue(
                                "translation/provider", root.selectedTranslationProviderId()
                            )
                        }
                        ComboBox {
                            id: translationModel
                            objectName: "translationModelChooser"
                            property alias text: translationModel.editText
                            property string chosenModel: root.appModel.uiStringValue(
                                "translationModel", "selected-on-web"
                            )
                            function setModelText(value) {
                                chosenModel = value
                                editText = value
                            }
                            function rememberModel() {
                                chosenModel = editText
                                root.appModel.setUiStringValue("translationModel", editText)
                                root.appModel.setUiStringValue(
                                    "translation/model/" + root.selectedTranslationProviderId(), editText
                                )
                            }
                            Layout.preferredWidth: 216
                            visible: providerBox.currentIndex !== 0
                            editable: true
                            currentIndex: -1
                            editText: chosenModel
                            model: root.appModel.translationModelCatalogProvider
                                    === root.selectedTranslationProviderId()
                                && root.appModel.translationAvailableModels.length > 0
                                ? root.appModel.translationAvailableModels
                                : root.translationProviderModels[providerBox.currentIndex] || []
                            Accessible.name: "Chọn model hoặc nhập tên model thủ công"
                            onModelChanged: Qt.callLater(function() { editText = chosenModel })
                            onActivated: rememberModel()
                            onAccepted: rememberModel()
                            Connections {
                                target: translationModel.contentItem
                                // Only user typing changes the remembered input.
                                // ComboBox also rewrites editText when its model
                                // arrives; that must not replace a manual name.
                                function onTextEdited() {
                                    const input = translationModel.contentItem as TextInput
                                    if (input)
                                        translationModel.chosenModel = input.text
                                }
                            }
                            onActiveFocusChanged: { if (!activeFocus) rememberModel() }
                        }
                        DsButton {
                            text: root.appModel.translationModelCatalogBusy ? "Đang tải…" : "Tải model"
                            compact: true
                            visible: root.selectedTranslationProviderIsApi()
                            enabled: !root.appModel.translationModelCatalogBusy
                                && savedCredentialBox.currentText.length > 0
                            disabledReason: "Lưu và chọn một API key trước."
                            onClicked: root.appModel.refreshTranslationModels(
                                root.selectedTranslationProviderId(), savedCredentialBox.currentText,
                                customBaseUrl.text
                            )
                        }
                        DsTextField {
                            id: customBaseUrl
                            Layout.fillWidth: true
                            visible: root.selectedTranslationProviderIsCustom()
                            placeholderText: "Base URL /v1 của API tương thích"
                            text: root.appModel.uiStringValue(
                                "translationBaseUrl", "https://api.openai.com/v1"
                            )
                            onEditingFinished: root.appModel.setUiStringValue(
                                "translationBaseUrl", text
                            )
                        }
                        DsButton {
                            text: "Nhập SRT Việt"
                            compact: true
                            enabled: !root.appModel.translationBusy
                                && root.appModel.currentProject.subtitleCueModel.count > 0
                            disabledReason: root.appModel.currentProject.subtitleCueModel.count > 0
                                ? "Hãy tạm dừng hoặc hoàn tất tác vụ dịch trước."
                                : "Chưa có cue nguồn để đối chiếu."
                            onClicked: translationImportDialog.open()
                        }
                    }
                    Text {
                        Layout.fillWidth: true
                        visible: providerBox.currentIndex !== 0
                        text: root.appModel.translationModelCatalogStatus
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                        elide: Text.ElideRight
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.space8
                        Text {
                            text: "Văn phong"
                            color: Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: 10
                        }
                        ComboBox {
                            id: translationStyleBox
                            objectName: "translationBuiltinStyle"
                            Layout.preferredWidth: 174
                            model: root.translationStyleLabels
                            Accessible.name: "Văn phong dịch của project"
                        }
                        ScrollView {
                            id: translationRulesScroll
                            Layout.fillWidth: true
                            Layout.preferredHeight: 76
                            clip: true
                            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

                            TextArea {
                                id: translationRules
                                objectName: "translationStyleRules"
                                width: translationRulesScroll.availableWidth
                                height: Math.max(
                                    translationRulesScroll.availableHeight,
                                    contentHeight + topPadding + bottomPadding
                                )
                                placeholderText: "Quy tắc riêng cho project (tối đa 4.000 ký tự)"
                                color: Theme.textPrimary
                                placeholderTextColor: Theme.textMuted
                                selectionColor: Theme.primary
                                selectedTextColor: Theme.textPrimary
                                font.family: Theme.fontFamily
                                font.pixelSize: 12
                                wrapMode: TextEdit.Wrap
                                selectByMouse: true
                                padding: 8
                                Accessible.name: "Quy tắc dịch riêng"
                                onTextChanged: {
                                    if (text.length <= 4000) return
                                    const position = cursorPosition
                                    text = text.slice(0, 4000)
                                    cursorPosition = Math.min(position, 4000)
                                }
                                background: Rectangle {
                                    color: translationRules.enabled
                                        ? Theme.surfaceRaised : Theme.surfaceDisabled
                                    radius: Theme.radiusSmall
                                    border.width: translationRules.activeFocus ? 2 : 1
                                    border.color: translationRules.activeFocus
                                        ? Theme.focusRing : Theme.borderSubtle
                                }
                            }
                        }
                        Text {
                            text: translationRules.text.length + "/4000"
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: 9
                        }
                        DsButton {
                            objectName: "applyTranslationStyle"
                            text: "Áp dụng"
                            tooltipText: "Dùng các quy tắc hiện tại cho dự án, không tạo bản mới."
                            compact: true
                            enabled: root.appModel.currentProject.hasProject
                                && root.translationStyleEditable
                                && !root.appModel.translationPreferencesBusy
                            operationState: root.appModel.translationPreferencesBusy
                                ? "running" : "idle"
                            runningText: "Đang lưu…"
                            disabledReason: root.appModel.translationPreferencesBusy
                                ? "Văn phong đang được lưu."
                                : "Hãy mở project và chờ tác vụ dịch dừng."
                            onClicked: root.appModel.setTranslationPreferences(
                                root.translationStyleIds[translationStyleBox.currentIndex]
                                    || "auto",
                                translationRules.text
                            )
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.space8
                        Text {
                            text: "Bản đã lưu"
                            color: Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: 10
                        }
                        ComboBox {
                            id: savedTranslationStyleBox
                            objectName: "savedTranslationStyle"
                            Layout.preferredWidth: 190
                            model: root.appModel.currentProject.translationStylePresets || []
                            textRole: "name"
                            currentIndex: -1
                            enabled: count > 0 && root.translationStyleEditable
                                && !root.appModel.translationPreferencesBusy
                            Accessible.name: "Các văn phong đã lưu"
                            ToolTip.visible: hovered
                            ToolTip.delay: 500
                            ToolTip.text: "Chọn để nạp và áp dụng văn phong đã lưu cho dự án."
                            onActivated: (index) => root.loadSavedTranslationStyle(index)
                        }
                        Text {
                            text: "Tên bản mới"
                            color: Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: 10
                        }
                        DsTextField {
                            id: translationStyleName
                            objectName: "translationStyleName"
                            Layout.fillWidth: true
                            placeholderText: "Tên bản văn phong mới"
                            maximumLength: 80
                            Accessible.name: "Tên bản văn phong"
                        }
                        DsButton {
                            objectName: "saveTranslationStyle"
                            text: "Lưu văn phong"
                            primary: true
                            tooltipText: "Lưu quy tắc và văn phong hiện tại thành một bản có tên riêng."
                            compact: true
                            enabled: root.appModel.currentProject.hasProject
                                && root.translationStyleEditable
                                && !root.appModel.translationPreferencesBusy
                                && translationStyleName.text.trim().length > 0
                            operationState: root.appModel.translationPreferencesBusy
                                ? "running" : "idle"
                            runningText: "Đang lưu…"
                            disabledReason: translationStyleName.text.trim().length === 0
                                ? "Hãy nhập tên bản văn phong."
                                : (root.appModel.translationPreferencesBusy
                                    ? "Văn phong đang được lưu."
                                    : "Hãy mở project và chờ tác vụ dịch dừng.")
                            onClicked: root.appModel.saveTranslationStyle(
                                translationStyleName.text,
                                root.translationStyleIds[translationStyleBox.currentIndex]
                                    || "auto",
                                translationRules.text
                            )
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        visible: root.selectedTranslationProviderIsApi()
                        spacing: Theme.space8
                        Text {
                            text: "Tên key"
                            color: Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: 11
                        }
                        DsTextField {
                            id: apiCredentialName
                            objectName: "translationKeyName"
                            Layout.preferredWidth: 132
                            placeholderText: "Ví dụ: Key chính"
                            Accessible.name: "Tên API key (không phải secret)"
                        }
                        Text {
                            text: "API key"
                            color: Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: 11
                        }
                        DsTextField {
                            id: apiCredentialInput
                            objectName: "translationKeySecret"
                            Layout.fillWidth: true
                            placeholderText: "Dán API key"
                            echoMode: TextInput.Password
                            Accessible.name: "API key được che ký tự"
                        }
                        DsButton {
                            text: "Lưu key"
                            enabled: apiCredentialInput.text.trim().length > 0
                                && apiCredentialName.text.trim().length > 0
                            disabledReason: "Nhập tên và API key vào hai ô riêng."
                            onClicked: root.appModel.saveTranslationApiCredential(
                                root.selectedTranslationProviderId(), apiCredentialName.text,
                                apiCredentialInput.text
                            )
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        visible: root.selectedTranslationProviderIsApi()
                        spacing: Theme.space8
                        ComboBox {
                            id: savedCredentialBox
                            objectName: "translationSavedKeyChooser"
                            function restoreSelection() {
                                const preferred = root.appModel.uiStringValue(
                                    "translation/selectedCredential/" + root.selectedTranslationProviderId(),
                                    ""
                                )
                                const references = root.selectedTranslationCredentialReferences || []
                                const preferredIndex = references.indexOf(preferred)
                                currentIndex = preferredIndex >= 0 ? preferredIndex
                                    : (references.length > 0 ? 0 : -1)
                            }
                            Layout.preferredWidth: 150
                            model: root.selectedTranslationCredentialReferences
                            Accessible.name: "Danh sách API key đã lưu theo tên"
                            displayText: count > 0 ? currentText : "Chưa có key"
                            Component.onCompleted: restoreSelection()
                            onModelChanged: Qt.callLater(restoreSelection)
                            onActivated: root.appModel.setUiStringValue(
                                "translation/selectedCredential/" + root.selectedTranslationProviderId(),
                                currentText
                            )
                        }
                        DsButton {
                            text: "Xóa key"
                            enabled: savedCredentialBox.currentText.length > 0
                            disabledReason: "Chưa chọn key đã lưu."
                            onClicked: root.appModel.deleteTranslationApiCredential(
                                root.selectedTranslationProviderId(), savedCredentialBox.currentText
                            )
                        }
                        Text {
                            Layout.preferredWidth: 82
                            text: root.selectedTranslationCredentialReferences.length + " key"
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: 9
                            elide: Text.ElideRight
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.space8
                        Text {
                            text: "Cache để tiếp tục"
                            color: Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: 10
                        }
                        Text {
                            text: root.appModel.translationCaches.length + " cache tương thích"
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: 9
                        }
                        ComboBox {
                            id: translationCacheBox
                            objectName: "translationCacheChooser"
                            Layout.fillWidth: true
                            model: root.appModel.translationCaches
                            textRole: "label"
                            valueRole: "job_id"
                            Accessible.name: "Chọn cache dịch tương thích với video hiện tại"
                            displayText: {
                                // ComboBox may select its first row when the model
                                // resets, before the selected job exists in that list.
                                var caches = root.appModel.translationCaches
                                var selected = root.appModel.selectedTranslationCacheId
                                for (var index = 0; index < caches.length; ++index) {
                                    if (caches[index].job_id === selected) {
                                        var cache = caches[index]
                                        var qa = root.appModel.translationQa || {}
                                        if (selected === root.appModel.translationJobId
                                                && qa.cue_total > 0
                                                && cache.provider_id && cache.model_id) {
                                            return (qa.completed_cues || 0) + "/" + qa.cue_total
                                                + " cue · " + root.translationJobStateLabel(
                                                    root.appModel.translationJobState)
                                                + " · " + cache.provider_id + " / " + cache.model_id
                                        }
                                        return caches[index].label
                                    }
                                }
                                return root.appModel.translationBusy
                                    ? "Cache của lượt dịch hiện tại" : "Chưa có cache tương thích"
                            }
                            enabled: count > 0 && !root.appModel.translationBusy
                            currentIndex: {
                                for (var index = 0; index < count; ++index) {
                                    if (valueAt(index) === root.appModel.selectedTranslationCacheId)
                                        return index
                                }
                                return -1
                            }
                            onActivated: root.appModel.selectTranslationCache(
                                valueAt(currentIndex)
                            )
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.space8
                        Text {
                            Layout.fillWidth: true
                            text: "Trạng thái: " + root.translationJobStateLabel(
                                root.appModel.translationJobState
                            )
                            color: Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: 10
                        }
                        DsButton {
                            Layout.preferredWidth: 190
                            primary: !warning
                            objectName: "translationPrimaryControl"
                            text: root.translationChoiceChanged ? "Dịch với lựa chọn mới"
                                : root.appModel.translationJobState === "cancelled"
                                || root.appModel.translationJobState === "failed"
                                || root.appModel.translationJobState === "interrupted"
                                || root.appModel.translationJobState === "review"
                                ? "Tiếp tục dịch từ cache"
                                : (root.appModel.translationJobState === "paused"
                                    ? "Tiếp tục dịch"
                                    : (root.appModel.currentProject.hasTranslatedSubtitles
                                        ? "Dịch lại · ghi đè bản Việt" : "Bắt đầu dịch"))
                            operationState: root.translationChoiceChanged ? "idle" : ["succeeded", ""]
                                .indexOf(root.appModel.translationJobState) >= 0
                                ? "idle" : root.appModel.translationJobState
                            warning: ["running", "pause_requested", "failed", "review",
                                "interrupted", "cancelled"]
                                .indexOf(root.appModel.translationJobState) >= 0
                            runningText: "Tạm dừng dịch"
                            queuedText: "Tiếp tục dịch từ cache"
                            startingText: "Đang khởi động dịch…"
                            pausingText: "Đang tạm dừng dịch…"
                            pausedText: "Tiếp tục dịch"
                            resumingText: "Đang tiếp tục dịch…"
                            cancellingText: "Đang hủy dịch…"
                            cancelledText: "Tiếp tục dịch từ cache"
                            failedText: "Tiếp tục dịch từ cache"
                            feedbackText: root.appModel.translationJobState === "running"
                                ? "Đang yêu cầu tạm dừng dịch"
                                : (["paused", "queued", "failed", "review", "interrupted",
                                    "cancelled"].indexOf(root.appModel.translationJobState) >= 0
                                    ? "Đang tiếp tục dịch từ cache" : "Đang bắt đầu dịch")
                            enabled: root.appModel.currentProject.subtitleCueModel.count > 0
                                && !root.appModel.translationPreferencesBusy
                                && ["start_requested", "pause_requested", "resume_requested",
                                    "cancel_requested"]
                                    .indexOf(root.appModel.translationJobState) < 0
                                && (root.appModel.translationJobState !== ""
                                    || !root.selectedTranslationProviderIsCustom()
                                    || customBaseUrl.text.trim().length > 0)
                            disabledReason: root.appModel.currentProject.subtitleCueModel.count <= 0
                                ? "Chưa có cue nguồn."
                                : "Dịch đang chuyển trạng thái; hãy chờ xác nhận."
                            onClicked: root.appModel.toggleTranslationWithStyle(
                                root.selectedTranslationProviderId(),
                                root.selectedTranslationModelId(),
                                root.headedWebEnabled,
                                root.guestWebEnabled,
                                root.selectedTranslationProviderIsApi()
                                    ? apiCredentialInput.text : "",
                                root.selectedTranslationProviderIsCustom()
                                    ? customBaseUrl.text : "",
                                root.translationStyleIds[translationStyleBox.currentIndex] || "auto",
                                translationRules.text
                            )
                        }
                        DsButton {
                            text: "Hủy"
                            danger: true
                            operationState: root.appModel.translationJobState === "cancel_requested"
                                ? "cancel_requested" : "idle"
                            cancellingText: "Đang hủy dịch…"
                            feedbackText: "Đang yêu cầu hủy dịch"
                            enabled: root.appModel.translationBusy
                            disabledReason: "Không có tác vụ dịch đang chạy."
                            onClicked: root.appModel.cancelTranslation()
                        }
                    }
                }
                Connections {
                    target: root.appModel
                    function onGeminiApiCredentialsStored() {
                        apiCredentialInput.text = ""
                    }
                }
                Connections {
                    target: root.appModel.currentProject
                    function onChanged() { root.syncTranslationPreferences() }
                }
            }

            Flow {
                Layout.fillWidth: true
                Layout.preferredHeight: implicitHeight
                visible: root.workspaceMode === "transcribe"
                    || root.workspaceMode === "translate"
                spacing: Theme.space8
                DsButton {
                    id: sourceSrtExportWorkflowButton
                    visible: root.workspaceMode === "transcribe"
                    text: "Xuất SRT nguồn"
                    enabled: root.appModel.currentProject.subtitleCueModel.count > 0
                        && !root.appModel.subtitleExportBusy
                    disabledReason: root.appModel.subtitleExportBusy
                        ? "Đang xuất một file SRT."
                        : "Chưa có phụ đề nguồn để xuất."
                    onClicked: sourceSrtExportDialog.open()
                }
                DsButton {
                    id: translatedSrtExportWorkflowButton
                    visible: root.workspaceMode === "translate"
                    text: "Xuất SRT tiếng Việt"
                    enabled: root.appModel.currentProject.hasTranslatedSubtitles
                        && !root.appModel.subtitleExportBusy
                    disabledReason: root.appModel.subtitleExportBusy
                        ? "Đang xuất một file SRT."
                        : "Chưa có phụ đề tiếng Việt để xuất."
                    onClicked: translatedSrtExportDialog.open()
                }
            }

            Flow {
                Layout.fillWidth: true
                Layout.preferredHeight: implicitHeight
                visible: root.workspaceMode === "subtitles"
                spacing: Theme.space8
                DsButton {
                    text: "Thêm cue"
                    enabled: root.appModel.currentProject.hasProject && !root.appModel.subtitleBusy
                    disabledReason: "Hãy mở dự án trước."
                    onClicked: root.appModel.addSubtitleCue()
                }
                DsButton {
                    text: "Tách"
                    enabled: root.appModel.selectedCueId !== "" && !root.appModel.subtitleBusy
                    disabledReason: "Cần chọn một cue."
                    onClicked: root.appModel.splitSelectedCue()
                }
                DsButton {
                    text: "Gộp cue sau"
                    enabled: root.appModel.selectedCueId !== "" && !root.appModel.subtitleBusy
                    disabledReason: "Cần chọn một cue."
                    onClicked: root.appModel.mergeSelectedCue()
                }
                DsButton {
                    text: "−100 ms"
                    enabled: root.appModel.selectedCueId !== "" && !root.appModel.subtitleBusy
                    disabledReason: "Cần chọn cue."
                    onClicked: root.appModel.shiftSelectedCue(-100)
                }
                DsButton {
                    text: "+100 ms"
                    enabled: root.appModel.selectedCueId !== "" && !root.appModel.subtitleBusy
                    disabledReason: "Cần chọn cue."
                    onClicked: root.appModel.shiftSelectedCue(100)
                }
                DsButton {
                    text: "Tự xuống dòng"
                    enabled: root.appModel.currentProject.subtitleCueModel.count > 0
                        && !root.appModel.subtitleBusy
                    disabledReason: "Cần có phụ đề."
                    onClicked: root.appModel.autoBreakSubtitles(false)
                }
                DsButton {
                    text: "Hoàn tác"
                    enabled: root.appModel.canUndoSubtitle && !root.appModel.subtitleBusy
                    disabledReason: "Chưa có chỉnh sửa để hoàn tác."
                    onClicked: root.appModel.undoSubtitle()
                }
                DsButton {
                    text: "Làm lại"
                    enabled: root.appModel.canRedoSubtitle && !root.appModel.subtitleBusy
                    disabledReason: "Chưa có chỉnh sửa để làm lại."
                    onClicked: root.appModel.redoSubtitle()
                }
                DsButton {
                    text: root.findBarOpen ? "Đóng tìm" : "Tìm / thay"
                    enabled: root.appModel.currentProject.subtitleCueModel.count > 0
                    disabledReason: "Chưa có cue để tìm."
                    onClicked: root.findBarOpen = !root.findBarOpen
                }
                DsButton {
                    text: "Xuất SRT nguồn"
                    enabled: root.appModel.currentProject.subtitleCueModel.count > 0
                        && !root.appModel.subtitleExportBusy
                    disabledReason: root.appModel.subtitleExportBusy
                        ? "Đang xuất một file SRT."
                        : "Chưa có phụ đề nguồn để xuất."
                    onClicked: sourceSrtExportDialog.open()
                }
                DsButton {
                    text: "Xuất SRT tiếng Việt"
                    enabled: root.appModel.currentProject.hasTranslatedSubtitles
                        && !root.appModel.subtitleExportBusy
                    disabledReason: root.appModel.subtitleExportBusy
                        ? "Đang xuất một file SRT."
                        : "Chưa có phụ đề tiếng Việt để xuất."
                    onClicked: translatedSrtExportDialog.open()
                }
            }

            RowLayout {
                id: findBar
                Layout.fillWidth: true
                visible: root.workspaceMode === "subtitles" && root.findBarOpen
                spacing: Theme.space8
                DsTextField {
                    id: findText
                    Layout.fillWidth: true
                    placeholderText: "Tìm trong bản dịch Việt"
                }
                DsTextField {
                    id: replacementText
                    Layout.fillWidth: true
                    placeholderText: "Thay bằng"
                }
                DsButton {
                    text: "Thay tất cả"
                    enabled: findText.text.length > 0 && !root.appModel.subtitleBusy
                    disabledReason: "Nhập nội dung cần tìm."
                    onClicked: {
                        const summary = root.appModel.previewSubtitleReplacement(
                            findText.text
                        )
                        if (summary.match_count <= 0)
                            return
                        root.pendingReplaceFind = findText.text
                        root.pendingReplacement = replacementText.text
                        root.pendingReplaceCount = summary.match_count
                        root.pendingReplaceCueCount = summary.cue_count
                        replaceConfirmDialog.open()
                    }
                }
            }

            Panel {
                Layout.fillWidth: true
                Layout.fillHeight: true
                ColumnLayout {
                    anchors.fill: parent
                    spacing: 0
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 38
                        color: Theme.surfaceRaised
                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: Theme.space12
                            anchors.rightMargin: Theme.space12
                            spacing: Theme.space12
                            Repeater {
                                model: ["Track", "Bắt đầu", "Kết thúc", "Nguồn Trung", "Bản dịch Việt", "Trạng thái"]
                                Text {
                                    required property int index
                                    required property string modelData
                                    Layout.fillWidth: index >= 3
                                    Layout.preferredWidth: index < 3 ? 76 : -1
                                    text: modelData
                                    color: Theme.textSecondary
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 11
                                    font.weight: Font.DemiBold
                                    elide: Text.ElideRight
                                }
                            }
                        }
                    }
                    EmptyState {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        visible: root.appModel.currentProject.subtitleCueModel.count === 0
                        title: "Chưa có phụ đề"
                        description: "Nhập SRT hoặc hoàn tất OCR để mở bảng cue ảo hóa."
                        iconName: "captions"
                    }
                    ListView {
                        id: cueList
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        visible: root.appModel.currentProject.subtitleCueModel.count > 0
                        clip: true
                        reuseItems: true
                        cacheBuffer: 180
                        boundsBehavior: Flickable.StopAtBounds
                        model: root.appModel.currentProject.subtitleCueModel
                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                        delegate: Rectangle {
                            id: cueRow
                            required property var model
                            function editorContains(position) {
                                const sourcePoint = sourceEditorHost.mapFromItem(
                                    cueRow, position.x, position.y
                                )
                                if (sourcePoint.x >= 0 && sourcePoint.y >= 0
                                        && sourcePoint.x <= sourceEditorHost.width
                                        && sourcePoint.y <= sourceEditorHost.height)
                                    return true
                                const translatedPoint = translatedEditorHost.mapFromItem(
                                    cueRow, position.x, position.y
                                )
                                return translatedPoint.x >= 0 && translatedPoint.y >= 0
                                    && translatedPoint.x <= translatedEditorHost.width
                                    && translatedPoint.y <= translatedEditorHost.height
                            }
                            width: ListView.view.width
                            height: model.qaIssueCode ? 76 : 66
                            color: root.appModel.isCueSelected(model.cueId)
                                ? Theme.selectionFill
                                : (model.timingTooLong ? Theme.warningTint
                                    : (hoverHandler.hovered ? Theme.surfaceHover : Theme.surfaceBase))
                            border.width: model.qaIssueCode ? 1 : 0
                            border.color: model.timingTooLong ? Theme.warning : Theme.errorBorder

                            Rectangle {
                                anchors.fill: parent
                                color: "transparent"
                                border.width: 2
                                border.color: Theme.warning
                                radius: Theme.radiusSmall
                                visible: cueRow.model.timingTooLong
                                SequentialAnimation on opacity {
                                    running: cueRow.model.timingTooLong
                                    loops: Animation.Infinite
                                    NumberAnimation { from: 0.28; to: 1.0; duration: 650 }
                                    NumberAnimation { from: 1.0; to: 0.28; duration: 650 }
                                }
                            }

                            TapHandler {
                                acceptedButtons: Qt.LeftButton
                                acceptedModifiers: Qt.NoModifier
                                onTapped: {
                                    if (!cueRow.editorContains(point.position))
                                        root.appModel.selectCue(
                                            cueRow.model.cueId, cueRow.model.startUs
                                        )
                                }
                            }
                            TapHandler {
                                acceptedButtons: Qt.LeftButton
                                acceptedModifiers: Qt.ControlModifier
                                onTapped: {
                                    if (!cueRow.editorContains(point.position))
                                        root.appModel.selectCueWithAdditive(
                                            cueRow.model.cueId,
                                            cueRow.model.startUs,
                                            true
                                        )
                                }
                            }
                            TapHandler {
                                acceptedButtons: Qt.RightButton
                                onTapped: {
                                    root.openCueContext(
                                        cueRow, point.position.x, point.position.y
                                    )
                                }
                            }
                            HoverHandler { id: hoverHandler }

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: Theme.space8
                                anchors.rightMargin: Theme.space8
                                spacing: Theme.space8
                                ColumnLayout {
                                    Layout.preferredWidth: 34
                                    spacing: 0
                                    Text {
                                        Layout.alignment: Qt.AlignHCenter
                                        text: cueRow.model.subtitleTrackId
                                        color: cueRow.model.cueRole === "scene_text"
                                            ? Theme.info : Theme.textMuted
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 9
                                        font.weight: Font.DemiBold
                                    }
                                    Text {
                                        Layout.alignment: Qt.AlignHCenter
                                        text: cueRow.model.cueIndex
                                        color: Theme.textMuted
                                        font.family: Theme.timecodeFamily
                                        font.pixelSize: 9
                                    }
                                    Text {
                                        Layout.alignment: Qt.AlignHCenter
                                        visible: cueRow.model.cueRole === "scene_text"
                                        text: cueRow.model.styleHint === "calligraphy"
                                            ? "THƯ" : (cueRow.model.orientation === "vertical"
                                                ? "DỌC" : "NGANG")
                                        color: cueRow.model.reviewState === "review"
                                            ? Theme.warning : Theme.textMuted
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 7
                                        font.weight: Font.DemiBold
                                    }
                                }
                                Text {
                                    Layout.preferredWidth: 76
                                    text: cueRow.model.startText
                                    color: Theme.textSecondary
                                    font.family: Theme.timecodeFamily
                                    font.pixelSize: 10
                                }
                                Text {
                                    Layout.preferredWidth: 76
                                    text: cueRow.model.endText
                                    color: Theme.textSecondary
                                    font.family: Theme.timecodeFamily
                                    font.pixelSize: 10
                                }
                                // TextArea handles a right click internally and
                                // opens Qt's native edit menu before a child
                                // MouseArea can intercept it. Keep the editor in
                                // an Item and put the right-click catcher beside
                                // it, above it in the stacking order, so only the
                                // application's cue menu is opened.
                                Item {
                                    id: sourceEditorHost
                                    Layout.fillWidth: true
                                    Layout.fillHeight: true
                                    Layout.preferredWidth: 220
                                    Layout.minimumHeight: 32
                                    TextArea {
                                        id: sourceEditor
                                        property string editTargetCueId: ""
                                        property string editOriginalText: ""
                                        anchors.fill: parent
                                        enabled: !root.appModel.subtitleBusy
                                        color: Theme.textPrimary
                                        font.family: Theme.cjkFontFamily
                                        font.pixelSize: 12
                                        wrapMode: Text.Wrap
                                        selectByMouse: true
                                        padding: 5
                                        background: Rectangle {
                                            color: parent.activeFocus ? Theme.surfaceRaised : "transparent"
                                            radius: Theme.radiusSmall
                                            border.width: parent.activeFocus ? 1 : 0
                                            border.color: Theme.focusRing
                                        }
                                        Binding {
                                            target: sourceEditor
                                            property: "text"
                                            value: cueRow.model.sourceText
                                            when: !sourceEditor.activeFocus
                                            restoreMode: Binding.RestoreNone
                                        }
                                        onPressed: function(mouse) {
                                            root.appModel.selectCueWithModifiers(
                                                cueRow.model.cueId,
                                                cueRow.model.startUs,
                                                Number(mouse.modifiers || Qt.NoModifier)
                                            )
                                        }
                                        onActiveFocusChanged: {
                                            if (activeFocus) {
                                                editTargetCueId = String(cueRow.model.cueId)
                                                editOriginalText = String(cueRow.model.sourceText)
                                            } else if (editTargetCueId.length > 0
                                                    && text !== editOriginalText) {
                                                root.appModel.updateSubtitleText(
                                                    editTargetCueId, text, "source"
                                                )
                                                editTargetCueId = ""
                                                editOriginalText = ""
                                            } else {
                                                editTargetCueId = ""
                                                editOriginalText = ""
                                            }
                                        }
                                    }
                                    MouseArea {
                                        id: sourceContextCatcher
                                        anchors.fill: parent
                                        z: 10
                                        acceptedButtons: Qt.RightButton
                                        preventStealing: true
                                        propagateComposedEvents: false
                                        onPressed: function(mouse) {
                                            mouse.accepted = true
                                            const localPoint = cueRow.mapFromItem(
                                                sourceEditorHost, mouse.x, mouse.y
                                            )
                                            root.openCueContext(
                                                cueRow, localPoint.x, localPoint.y
                                            )
                                        }
                                    }
                                }
                                Item {
                                    id: translatedEditorHost
                                    Layout.fillWidth: true
                                    Layout.fillHeight: true
                                    Layout.preferredWidth: 220
                                    Layout.minimumHeight: 32
                                    TextArea {
                                        id: translatedEditor
                                        property string editTargetCueId: ""
                                        property string editOriginalText: ""
                                        anchors.fill: parent
                                        color: cueRow.model.translatedText
                                            ? Theme.textPrimary : Theme.textMuted
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 12
                                        font.italic: !cueRow.model.translatedText
                                        wrapMode: Text.Wrap
                                        readOnly: cueRow.model.translationCueId === ""
                                        enabled: !root.appModel.subtitleBusy
                                        selectByMouse: true
                                        padding: 5
                                        background: Rectangle { color: "transparent" }
                                        Binding {
                                            target: translatedEditor
                                            property: "text"
                                            value: cueRow.model.translatedText || "Chưa dịch"
                                            when: !translatedEditor.activeFocus
                                            restoreMode: Binding.RestoreNone
                                        }
                                        onPressed: function(mouse) {
                                            root.appModel.selectCueWithModifiers(
                                                cueRow.model.cueId,
                                                cueRow.model.startUs,
                                                Number(mouse.modifiers || Qt.NoModifier)
                                            )
                                        }
                                        onActiveFocusChanged: {
                                            if (activeFocus) {
                                                editTargetCueId = String(
                                                    cueRow.model.translationCueId || ""
                                                )
                                                editOriginalText = String(
                                                    cueRow.model.translatedText || ""
                                                )
                                            } else if (editTargetCueId.length > 0
                                                    && text !== editOriginalText) {
                                                root.appModel.updateSubtitleText(
                                                    editTargetCueId,
                                                    text,
                                                    "translated"
                                                )
                                                editTargetCueId = ""
                                                editOriginalText = ""
                                            } else {
                                                editTargetCueId = ""
                                                editOriginalText = ""
                                            }
                                        }
                                    }
                                    MouseArea {
                                        id: translatedContextCatcher
                                        anchors.fill: parent
                                        z: 10
                                        acceptedButtons: Qt.RightButton
                                        preventStealing: true
                                        propagateComposedEvents: false
                                        onPressed: function(mouse) {
                                            mouse.accepted = true
                                            const localPoint = cueRow.mapFromItem(
                                                translatedEditorHost, mouse.x, mouse.y
                                            )
                                            root.openCueContext(
                                                cueRow, localPoint.x, localPoint.y
                                            )
                                        }
                                    }
                                }
                                ColumnLayout {
                                    Layout.preferredWidth: 82
                                    spacing: 1
                                    Rectangle {
                                        Layout.alignment: Qt.AlignHCenter
                                        implicitWidth: timingBadge.implicitWidth + 12
                                        implicitHeight: 20
                                        radius: 10
                                        color: Theme.warningTint
                                        border.width: 1
                                        border.color: Theme.warning
                                        visible: cueRow.model.timingTooLong
                                        Text {
                                            id: timingBadge
                                            anchors.centerIn: parent
                                            text: "Cue dài"
                                            color: Theme.warning
                                            font.family: Theme.fontFamily
                                            font.pixelSize: 9
                                            font.weight: Font.DemiBold
                                        }
                                    }
                                    Text {
                                        Layout.alignment: Qt.AlignHCenter
                                        text: cueRow.model.status
                                        color: cueRow.model.status === "Đã dịch"
                                            ? Theme.success : Theme.info
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 10
                                    }
                                    Text {
                                        Layout.alignment: Qt.AlignHCenter
                                        visible: cueRow.model.confidence >= 0
                                        text: Math.round(cueRow.model.confidence * 100) + "%"
                                        color: cueRow.model.confidence >= 0.86
                                            ? Theme.textMuted : Theme.warning
                                        font.family: Theme.timecodeFamily
                                        font.pixelSize: 9
                                    }
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
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: root.regionEditorMode.length > 0 ? 420 : 210
                Layout.minimumHeight: 180
                Layout.maximumHeight: root.regionEditorMode.length > 0 ? 420 : 210
                spacing: Theme.space8
                JobActivityPanel {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.minimumWidth: 280
                    visible: root.regionEditorMode.length === 0
                    appModel: root.appModel
                    allowHistoryDeletion: true
                    jobModel: root.appModel.subtitleJobModel
                    title: "OCR và dịch thuật"
                    emptyDescription: "Các tác vụ sẽ xuất hiện tại đây."
                }
                Panel {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.minimumWidth: 280
                    TimelinePreview {
                        id: subtitlePreview
                        anchors.fill: parent
                        anchors.margins: Theme.space8
                        appModel: root.appModel
                        ocrRegionEditorMode: root.regionEditorMode
                        onRegionDrawn: function(mode, x, y, width, height) {
                            if (mode === "dialogue") {
                                root.appModel.setOcrRegions(
                                    [x, y, width, height],
                                    root.appModel.currentProject.ocrExclusionRegions
                                )
                            } else if (mode === "exclusion") {
                                const values = root.appModel.currentProject.ocrExclusionRegions.slice()
                                values.push([x, y, width, height])
                                root.appModel.setOcrRegions(
                                    root.appModel.currentProject.ocrDialogueRegion,
                                    values
                                )
                            } else if (mode === "manual_text") {
                                manualCueDialog.submitAtRegion(x, y, width, height)
                            }
                            root.regionEditorMode = ""
                        }
                    }
                }
            }
        }

        Panel {
            id: inspector
            SplitView.preferredWidth: root.width * root.appModel.uiRealValue("subtitleInspectorRatio", 0.28)
            SplitView.minimumWidth: 272
            SplitView.maximumWidth: 340
            onWidthChanged: persistWidth.restart()

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: Theme.space12
                spacing: Theme.space8
                RowLayout {
                    Layout.fillWidth: true
                    DsTabButton {
                        text: "QA"
                        checked: root.inspectorTab === "qa"
                        onClicked: root.selectInspectorTab("qa")
                    }
                    DsTabButton {
                        text: "Kiểu sub"
                        checked: root.inspectorTab === "style"
                        onClicked: root.selectInspectorTab("style")
                    }
                }
                StackLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    currentIndex: root.inspectorTab === "style" ? 1 : 0
                    ColumnLayout {
                        spacing: Theme.space8
                        ColumnLayout {
                            Layout.fillWidth: true
                            visible: ((root.appModel.voiceTimingQa || {}).status || "NONE") !== "NONE"
                            spacing: Theme.space4
                            RowLayout {
                                Layout.fillWidth: true
                                SectionTitle { text: "Cue dài của voice" }
                                Item { Layout.fillWidth: true }
                                Text {
                                    text: ((root.appModel.voiceTimingQa || {}).status || "NONE") === "PASS"
                                        ? "Đạt" : "Cần rút gọn"
                                    color: ((root.appModel.voiceTimingQa || {}).status || "NONE") === "PASS"
                                        ? Theme.success : Theme.warning
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 11
                                    font.weight: Font.DemiBold
                                }
                            }
                            Text {
                                Layout.fillWidth: true
                                text: (root.appModel.voiceTimingQa || {}).message || ""
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: 9
                                wrapMode: Text.Wrap
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                visible: ((root.appModel.voiceTimingQa || {}).issue_count || 0) > 0
                                DsButton {
                                    Layout.fillWidth: true
                                    compact: true
                                    text: "Tự rút gọn"
                                    primary: true
                                    enabled: (root.appModel.voiceTimingQa || {}).repairable === true
                                        && !root.appModel.translationBusy
                                    disabledReason: "Đã chạm giới hạn tự sửa hoặc đang có job dịch."
                                    onClicked: root.appModel.repairVoiceTimingCues()
                                }
                                DsButton {
                                    Layout.fillWidth: true
                                    compact: true
                                    text: "Sửa thủ công"
                                    onClicked: {
                                        var items = (root.appModel.voiceTimingQa || {}).issues || []
                                        if (items.length > 0)
                                            root.appModel.selectTranslationQaIssue(Number(items[0].id))
                                    }
                                }
                            }
                            DsButton {
                                Layout.fillWidth: true
                                visible: ((root.appModel.voiceTimingQa || {}).issue_count || 0) > 0
                                compact: true
                                text: "Bỏ qua cảnh báo · giữ nguyên voice"
                                warning: true
                                operationState: root.appModel.voiceTimingAutoRepairBusy
                                    ? "start_requested" : "idle"
                                startingText: "Đang chờ tự rút gọn…"
                                enabled: !root.appModel.ttsBusy
                                    && !root.appModel.translationBusy
                                    && !root.appModel.voiceTimingAutoRepairBusy
                                disabledReason: root.appModel.voiceTimingAutoRepairBusy
                                    ? "Tác vụ tự rút gọn đang chạy; chưa tạo override job. Hãy chờ tác vụ này kết thúc rồi bấm lại."
                                    : "Hãy chờ tác vụ dịch/rút gọn hoặc tạo giọng hiện tại kết thúc."
                                feedbackText: "Đang xếp tuần tự voice và kéo dài Timeline nếu cần"
                                onClicked: root.appModel.addCurrentVoiceToTimeline()
                            }
                            ListView {
                                id: voiceTimingIssueList
                                Layout.fillWidth: true
                                Layout.preferredHeight: Math.min(
                                    150,
                                    Math.max(0, ((root.appModel.voiceTimingQa || {}).issues || []).length * 58)
                                )
                                visible: ((root.appModel.voiceTimingQa || {}).issues || []).length > 0
                                clip: true
                                model: (root.appModel.voiceTimingQa || {}).issues || []
                                spacing: Theme.space4
                                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                                delegate: Rectangle {
                                    id: voiceTimingIssueRow
                                    required property var modelData
                                    width: ListView.view.width
                                    height: 54
                                    radius: Theme.radiusSmall
                                    color: Theme.warningTint
                                    border.width: 1
                                    border.color: Theme.warning
                                    TapHandler {
                                        onTapped: root.appModel.selectTranslationQaIssue(
                                            Number(voiceTimingIssueRow.modelData.id)
                                        )
                                    }
                                    ColumnLayout {
                                        anchors.fill: parent
                                        anchors.margins: Theme.space8
                                        spacing: 1
                                        Text {
                                            Layout.fillWidth: true
                                            text: "Cue #" + voiceTimingIssueRow.modelData.id + " · Cue dài"
                                            color: Theme.warning
                                            font.family: Theme.fontFamily
                                            font.pixelSize: 10
                                            font.weight: Font.DemiBold
                                        }
                                        Text {
                                            Layout.fillWidth: true
                                            text: voiceTimingIssueRow.modelData.message || "Cần rút gọn"
                                            color: Theme.textSecondary
                                            font.family: Theme.fontFamily
                                            font.pixelSize: 9
                                            elide: Text.ElideRight
                                        }
                                    }
                                }
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            visible: root.workspaceMode === "translate"
                                || root.workspaceMode === "subtitles"
                            spacing: Theme.space4
                            RowLayout {
                                Layout.fillWidth: true
                                SectionTitle { text: "QA bản dịch" }
                                Item { Layout.fillWidth: true }
                                Text {
                                    text: {
                                        var qa = root.appModel.translationQa || {}
                                        var status = qa.status || "NONE"
                                        return status === "PASS" && (qa.warning_count || 0) > 0
                                            ? "Đạt — có cảnh báo"
                                            : root.translationQaStatusLabel(status)
                                    }
                                    color: root.translationQaStatusColor(
                                        (root.appModel.translationQa || {}).status || "NONE"
                                    )
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 12
                                    font.weight: Font.DemiBold
                                }
                            }
                            Text {
                                Layout.fillWidth: true
                                text: {
                                    var qa = root.appModel.translationQa || {}
                                    var checked = qa.checked_cues || 0
                                    var total = qa.cue_total || 0
                                    var blockers = qa.blocking_count || 0
                                    var warnings = qa.warning_count || 0
                                    return "Đã kiểm tra " + checked + "/" + total
                                        + " · " + blockers + " cue cần sửa"
                                        + (warnings > 0 ? " · " + warnings + " cảnh báo" : "")
                                }
                                color: Theme.textMuted
                                font.family: Theme.timecodeFamily
                                font.pixelSize: 10
                                elide: Text.ElideRight
                            }
                            ListView {
                                id: translationQaList
                                Layout.fillWidth: true
                                Layout.preferredHeight: Math.min(
                                    188,
                                    Math.max(0, ((root.appModel.translationQa || {}).issues || []).length * 74)
                                )
                                visible: (root.workspaceMode === "translate"
                                    || root.workspaceMode === "subtitles")
                                    && ((root.appModel.translationQa || {}).status || "NONE") === "REVIEW"
                                    && (((root.appModel.translationQa || {}).issues || []).length > 0)
                                clip: true
                                model: (root.appModel.translationQa || {}).issues || []
                                spacing: Theme.space4
                                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                                delegate: Rectangle {
                                    id: translationQaRow
                                    required property var modelData
                                    width: ListView.view.width
                                    height: 70
                                    radius: Theme.radiusSmall
                                    color: Theme.surfaceRaised
                                    border.width: 1
                                    border.color: Theme.borderSubtle
                                    TapHandler {
                                        onTapped: root.appModel.selectTranslationQaIssue(
                                            Number(translationQaRow.modelData.id)
                                        )
                                    }
                                    ColumnLayout {
                                        anchors.fill: parent
                                        anchors.margins: Theme.space8
                                        spacing: 1
                                        Text {
                                            Layout.fillWidth: true
                                            text: "Cue #" + translationQaRow.modelData.id + " · "
                                                + root.appModel.localizedIssueLabel(
                                                    translationQaRow.modelData.code || ""
                                                )
                                            color: Theme.error
                                            font.family: Theme.fontFamily
                                            font.pixelSize: 10
                                            font.weight: Font.DemiBold
                                            elide: Text.ElideRight
                                        }
                                        Text {
                                            Layout.fillWidth: true
                                            text: "Việt: "
                                                + (translationQaRow.modelData.translated_text
                                                    || "Chưa có bản dịch")
                                            color: Theme.textPrimary
                                            font.family: Theme.fontFamily
                                            font.pixelSize: 9
                                            elide: Text.ElideRight
                                        }
                                        Text {
                                            Layout.fillWidth: true
                                            text: translationQaRow.modelData.message
                                                || "Cần kiểm tra lại bản dịch"
                                            color: Theme.textSecondary
                                            font.family: Theme.fontFamily
                                            font.pixelSize: 9
                                            elide: Text.ElideRight
                                        }
                                    }
                                }
                            }
                            Text {
                                Layout.fillWidth: true
                                visible: ((root.appModel.translationQa || {}).status || "NONE") === "PASS"
                                text: "✓ Toàn bộ cue đã dịch và QA đạt."
                                color: Theme.success
                                font.family: Theme.fontFamily
                                font.pixelSize: 10
                                font.weight: Font.DemiBold
                            }
                        }
                        EmptyState {
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            visible: root.appModel.selectedCueId === ""
                                && root.workspaceMode !== "translate"
                                && (((root.appModel.translationQa || {}).issues || []).length === 0)
                            title: "Chọn một cue"
                            description: "Xem nguồn OCR, độ tin cậy và chỉnh đúng vùng cần thiết."
                            iconName: "list-todo"
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            visible: root.appModel.selectedCueId !== ""
                            spacing: Theme.space8
                            SectionTitle { text: "Nguồn và QA" }
                            Text {
                                Layout.fillWidth: true
                                text: "Cue #" + (root.appModel.selectedCue.index || "")
                                color: Theme.textPrimary
                                font.family: Theme.fontFamily
                                font.pixelSize: 13
                                font.weight: Font.DemiBold
                            }
                            Text {
                                Layout.fillWidth: true
                                text: (root.appModel.selectedCue.start_text || "")
                                    + "  →  " + (root.appModel.selectedCue.end_text || "")
                                color: Theme.textSecondary
                                font.family: Theme.timecodeFamily
                                font.pixelSize: 11
                            }
                            Text {
                                Layout.fillWidth: true
                                text: "Nguồn: " + (root.appModel.selectedCue.provenance || "manual")
                                color: Theme.textSecondary
                                font.family: Theme.fontFamily
                                font.pixelSize: 11
                                wrapMode: Text.Wrap
                            }
                            Text {
                                Layout.fillWidth: true
                                text: root.appModel.selectedCue.confidence >= 0
                                    ? "Độ tin cậy: "
                                        + Math.round(root.appModel.selectedCue.confidence * 100)
                                        + "%"
                                    : "Độ tin cậy: không có dữ liệu"
                                color: root.appModel.selectedCue.confidence >= 0.86
                                    ? Theme.success : Theme.warning
                                font.family: Theme.fontFamily
                                font.pixelSize: 11
                            }
                            DsButton {
                                text: "Tự xuống dòng cue này"
                                onClicked: root.appModel.autoBreakSubtitles(true)
                            }
                            DsButton {
                                text: "Quét lại đúng vùng"
                                enabled: root.appModel.currentProject.hasVideo
                                    && !root.appModel.ocrBusy
                                disabledReason: root.appModel.currentProject.hasVideo
                                    ? "Một tác vụ OCR khác đang chạy."
                                    : "Project chưa có video."
                                onClicked: root.appModel.rescanSelectedCue()
                            }
                            Item { Layout.fillHeight: true }
                        }
                    }
                    ScrollView {
                        id: subtitleStyleScroll
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

                        ColumnLayout {
                            width: subtitleStyleScroll.availableWidth
                            spacing: Theme.space12

                            SectionTitle { text: "Hiển thị phụ đề Việt" }
                            Text {
                                Layout.fillWidth: true
                                text: "Thay đổi được xem ngay trên video và dùng nguyên vẹn khi xuất."
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: 10
                                wrapMode: Text.Wrap
                            }
                            CheckBox {
                                text: "Đóng cứng phụ đề khi xuất"
                                checked: root.appModel.exportSubtitleEnabled
                                onClicked: root.appModel.setExportSubtitleEnabled(checked)
                                Accessible.name: text
                            }
                            GridLayout {
                                Layout.fillWidth: true
                                columns: 2
                                columnSpacing: Theme.space8
                                rowSpacing: Theme.space8

                                Text {
                                    text: "Font"
                                    color: Theme.textSecondary
                                    font.pixelSize: 11
                                }
                                DsTextField {
                                    Layout.fillWidth: true
                                    text: root.appModel.exportFontFamily
                                    Accessible.name: "Font phụ đề"
                                    onEditingFinished: root.appModel.setExportFontFamily(text)
                                }
                                Text {
                                    text: "Cỡ chữ"
                                    color: Theme.textSecondary
                                    font.pixelSize: 11
                                }
                                DsSpinBox {
                                    Layout.fillWidth: true
                                    accessibleName: "Cỡ chữ phụ đề"
                                    from: 12
                                    to: 120
                                    value: root.appModel.exportFontSize
                                    onValueModified: root.appModel.setExportFontSize(value)
                                }
                                Text {
                                    text: "Màu chữ"
                                    color: Theme.textSecondary
                                    font.pixelSize: 11
                                }
                                Button {
                                    id: subtitleTextColorButton
                                    Layout.fillWidth: true
                                    implicitHeight: 36
                                    hoverEnabled: true
                                    Accessible.name: "Chọn màu chữ phụ đề"
                                    onClicked: {
                                        subtitleTextColorDialog.selectedColor
                                            = root.appModel.exportSubtitlePrimaryColor
                                        subtitleTextColorDialog.open()
                                    }
                                    contentItem: RowLayout {
                                        spacing: Theme.space8
                                        Rectangle {
                                            implicitWidth: 20
                                            implicitHeight: 20
                                            radius: Theme.radiusSmall
                                            color: root.appModel.exportSubtitlePrimaryColor
                                            border.width: 1
                                            border.color: Theme.borderStrong
                                        }
                                        Text {
                                            Layout.fillWidth: true
                                            text: root.appModel.exportSubtitlePrimaryColor
                                            color: Theme.textPrimary
                                            font.family: Theme.timecodeFamily
                                            font.pixelSize: 11
                                        }
                                    }
                                    background: Rectangle {
                                        color: subtitleTextColorButton.down
                                            ? Theme.borderStrong
                                            : (subtitleTextColorButton.hovered
                                                ? Theme.surfaceHover : Theme.surfaceRaised)
                                        radius: Theme.radiusSmall
                                        border.width: subtitleTextColorButton.visualFocus ? 2 : 1
                                        border.color: subtitleTextColorButton.visualFocus
                                            ? Theme.focusRing : Theme.borderSubtle
                                    }
                                }
                                Text {
                                    text: "Màu viền"
                                    color: Theme.textSecondary
                                    font.pixelSize: 11
                                }
                                Button {
                                    id: subtitleOutlineColorButton
                                    Layout.fillWidth: true
                                    implicitHeight: 36
                                    hoverEnabled: true
                                    Accessible.name: "Chọn màu viền phụ đề"
                                    onClicked: {
                                        subtitleOutlineColorDialog.selectedColor
                                            = root.appModel.exportSubtitleOutlineColor
                                        subtitleOutlineColorDialog.open()
                                    }
                                    contentItem: RowLayout {
                                        spacing: Theme.space8
                                        Rectangle {
                                            implicitWidth: 20
                                            implicitHeight: 20
                                            radius: Theme.radiusSmall
                                            color: root.appModel.exportSubtitleOutlineColor
                                            border.width: 1
                                            border.color: Theme.borderStrong
                                        }
                                        Text {
                                            Layout.fillWidth: true
                                            text: root.appModel.exportSubtitleOutlineColor
                                            color: Theme.textPrimary
                                            font.family: Theme.timecodeFamily
                                            font.pixelSize: 11
                                        }
                                    }
                                    background: Rectangle {
                                        color: subtitleOutlineColorButton.down
                                            ? Theme.borderStrong
                                            : (subtitleOutlineColorButton.hovered
                                                ? Theme.surfaceHover : Theme.surfaceRaised)
                                        radius: Theme.radiusSmall
                                        border.width: subtitleOutlineColorButton.visualFocus ? 2 : 1
                                        border.color: subtitleOutlineColorButton.visualFocus
                                            ? Theme.focusRing : Theme.borderSubtle
                                    }
                                }
                                Text {
                                    text: "Độ dày viền"
                                    color: Theme.textSecondary
                                    font.pixelSize: 11
                                }
                                DsSpinBox {
                                    Layout.fillWidth: true
                                    accessibleName: "Độ dày viền phụ đề"
                                    from: 0
                                    to: 12
                                    value: root.appModel.exportSubtitleOutlineWidth
                                    onValueModified: root.appModel.setExportSubtitleOutlineWidth(value)
                                }
                                Text {
                                    text: "Độ mờ (%)"
                                    color: Theme.textSecondary
                                    font.pixelSize: 11
                                }
                                DsSpinBox {
                                    Layout.fillWidth: true
                                    accessibleName: "Độ mờ phụ đề theo phần trăm"
                                    from: 0
                                    to: 100
                                    value: root.appModel.exportSubtitleOpacityPercent
                                    onValueModified: root.appModel.setExportSubtitleOpacityPercent(value)
                                }
                                Text {
                                    text: "Cách đáy"
                                    color: Theme.textSecondary
                                    font.pixelSize: 11
                                }
                                DsSpinBox {
                                    Layout.fillWidth: true
                                    accessibleName: "Khoảng cách phụ đề với đáy"
                                    from: 0
                                    to: 500
                                    value: root.appModel.exportSubtitleMarginBottom
                                    onValueModified: root.appModel.setExportSubtitleMarginBottom(value)
                                }
                            }

                            Text {
                                text: "Căn ngang"
                                color: Theme.textSecondary
                                font.family: Theme.fontFamily
                                font.pixelSize: 11
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Theme.space4
                                DsTabButton {
                                    Layout.fillWidth: true
                                    text: "Trái"
                                    checked: root.appModel.exportSubtitleAlignment === 1
                                    onClicked: root.appModel.setExportSubtitleAlignment(1)
                                }
                                DsTabButton {
                                    Layout.fillWidth: true
                                    text: "Giữa"
                                    checked: root.appModel.exportSubtitleAlignment === 2
                                    onClicked: root.appModel.setExportSubtitleAlignment(2)
                                }
                                DsTabButton {
                                    Layout.fillWidth: true
                                    text: "Phải"
                                    checked: root.appModel.exportSubtitleAlignment === 3
                                    onClicked: root.appModel.setExportSubtitleAlignment(3)
                                }
                            }
                            DsButton {
                                Layout.fillWidth: true
                                text: "Khôi phục kiểu mặc định"
                                onClicked: root.appModel.resetExportSubtitleStyle()
                            }
                            Item { Layout.fillHeight: true }
                        }
                    }
                }
            }
        }
    }

    Connections {
        target: root.appModel
        function onTranslationQaNavigationRequested(rowIndex) {
            Qt.callLater(function() {
                if (cueList.count > rowIndex)
                    cueList.positionViewAtIndex(rowIndex, ListView.Center)
            })
        }
        function onSubtitleNavigationRequested(cueId) {
            Qt.callLater(function() {
                const rowIndex = root.appModel.currentProject.subtitleCueModel.rowForCueId(
                    String(cueId)
                )
                if (rowIndex >= 0 && cueList.count > rowIndex)
                    cueList.positionViewAtIndex(rowIndex, ListView.Center)
            })
        }
    }
}
