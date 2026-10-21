import QtQuick
import QtQuick.Controls
import DucaiStudio 1.0

Button {
    id: control

    property bool primary: false
    property bool warning: false
    property bool danger: false
    property bool compact: false
    property bool emphasized: false
    property bool feedbackEnabled: true
    property string feedbackText: ""
    property string feedbackKind: ""
    property int feedbackDurationMs: 4000
    property string operationState: "idle"
    property string runningText: "Đang xử lý…"
    property string queuedText: "Đang chờ…"
    property string startingText: "Đang bắt đầu…"
    property string pausingText: "Đang tạm dừng…"
    property string pausedText: "Đã tạm dừng"
    property string resumingText: "Đang tiếp tục…"
    property string cancellingText: "Đang hủy…"
    property string cancelledText: "Đã hủy"
    property string succeededText: "Đã hoàn tất"
    property string failedText: "Thao tác lỗi"
    property string disabledReason: "Chức năng này chưa sẵn sàng."
    property string tooltipText: ""

    readonly property string displayText: {
        if (operationState === "start_requested") return startingText
        if (operationState === "running") return runningText
        if (operationState === "queued") return queuedText
        if (operationState === "pause_requested") return pausingText
        if (operationState === "paused") return pausedText
        if (operationState === "resume_requested") return resumingText
        if (operationState === "cancel_requested") return cancellingText
        if (operationState === "cancelled") return cancelledText
        if (operationState === "succeeded") return succeededText
        if (operationState === "failed" || operationState === "interrupted"
                || operationState === "review") return failedText
        return text
    }
    readonly property bool failedState: operationState === "failed"
        || operationState === "interrupted" || operationState === "review"
    readonly property bool successState: operationState === "succeeded"
    readonly property bool activeState: [
        "queued", "start_requested", "running", "pause_requested",
        "resume_requested", "cancel_requested"
    ].indexOf(operationState) >= 0
    readonly property string effectiveFeedbackKind: feedbackKind.length > 0
        ? feedbackKind : (danger ? "error" : (warning ? "warning" : "info"))

    implicitHeight: 36
    implicitWidth: compact
        ? Math.max(44, contentItem.implicitWidth + 16)
        : Math.max(96, contentItem.implicitWidth + 28)
    leftPadding: compact ? 8 : 14
    rightPadding: compact ? 8 : 14
    hoverEnabled: true
    scale: down ? 0.98 : 1
    Accessible.name: displayText
    Accessible.description: enabled
        ? (tooltipText.length > 0 ? tooltipText : operationState)
        : disabledReason
    Accessible.role: Accessible.Button
    Accessible.focusable: true

    function acceptedFeedbackText() {
        if (feedbackText.trim().length > 0) return feedbackText.trim()
        if (danger) return "Đã nhận yêu cầu: " + text
        return "Đã nhận thao tác: " + text
    }

    Connections {
        target: control
        function onClicked() {
            if (control.feedbackEnabled)
                UiFeedback.show(
                    control.acceptedFeedbackText(),
                    control.effectiveFeedbackKind,
                    control.feedbackDurationMs
                )
        }
    }

    contentItem: Text {
        text: control.displayText
        color: {
            if (control.danger) return control.enabled ? Theme.warning : Theme.textMuted
            if (control.warning) return control.enabled ? Theme.warning : Theme.textMuted
            if (control.failedState) return Theme.error
            if (control.successState) return Theme.success
            if (control.activeState) return Theme.info
            return control.enabled
                ? (control.primary || control.emphasized ? Theme.textPrimary : Theme.textSecondary)
                : Theme.textMuted
        }
        font.family: Theme.fontFamily
        font.pixelSize: 13
        font.weight: control.primary || control.danger || control.warning
            ? Font.DemiBold : Font.Medium
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight

        Behavior on color {
            ColorAnimation { duration: Theme.motionFast }
        }
    }

    background: Rectangle {
        radius: Theme.radiusSmall
        color: {
            if (control.danger && control.enabled)
                return control.down ? Theme.dangerPressed : Theme.danger
            if (control.warning && control.enabled)
                return control.down ? Theme.warningBorder : Theme.warningTint
            if (control.failedState) return Theme.errorTint
            if (control.successState) return Theme.successTint
            if (control.operationState === "running"
                    || control.operationState === "start_requested"
                    || control.operationState === "resume_requested")
                return Theme.selectionFill
            if (control.operationState === "queued"
                    || control.operationState === "pause_requested")
                return Theme.warningTint
            if (control.operationState === "cancel_requested") return Theme.danger
            if (!control.enabled) return Theme.surfaceBase
            if (control.primary) {
                if (control.down) return Theme.primaryPressed
                if (control.hovered) return Theme.primaryHover
                return Theme.primary
            }
            if (control.down) return Theme.borderStrong
            if (control.hovered) return Theme.surfaceHover
            return control.emphasized ? Theme.surfaceHover : Theme.surfaceRaised
        }
        border.width: control.visualFocus ? 2 : 1
        border.color: control.visualFocus ? Theme.focusRing
            : (control.danger ? Theme.errorBorder
            : (control.warning ? Theme.warningBorder
            : (control.failedState ? Theme.errorBorder
            : (control.successState ? Theme.successBorder
            : (control.activeState ? Theme.warningBorder
            : (control.emphasized && control.enabled ? Theme.iconMuted : Theme.borderSubtle))))))

        Behavior on color {
            ColorAnimation { duration: Theme.motionFast }
        }
        Behavior on border.color {
            ColorAnimation { duration: Theme.motionFast }
        }
    }

    Behavior on scale {
        NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutQuart }
    }

    ToolTip.visible: control.enabled && control.hovered
        && Boolean(control.tooltipText)
    ToolTip.text: control.tooltipText
    ToolTip.delay: 350

    MouseArea {
        anchors.fill: parent
        enabled: !control.enabled && Boolean(control.disabledReason)
        acceptedButtons: Qt.NoButton
        hoverEnabled: true

        ToolTip.visible: containsMouse
        ToolTip.text: control.disabledReason
        ToolTip.delay: 350
    }
}
