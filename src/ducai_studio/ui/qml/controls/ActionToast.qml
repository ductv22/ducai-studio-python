import QtQuick
import QtQuick.Controls
import DucaiStudio 1.0

Item {
    id: root

    width: parent ? parent.width : 0
    height: toastCard.height + Theme.space32
    visible: false

    function backgroundColor() {
        if (UiFeedback.kind === "success") return Theme.successTint
        if (UiFeedback.kind === "warning") return Theme.warningTint
        if (UiFeedback.kind === "error") return Theme.errorTint
        return Theme.surfaceRaised
    }

    function borderColor() {
        if (UiFeedback.kind === "success") return Theme.successBorder
        if (UiFeedback.kind === "warning") return Theme.warningBorder
        if (UiFeedback.kind === "error") return Theme.errorBorder
        return Theme.primary
    }

    function textColor() {
        if (UiFeedback.kind === "success") return Theme.success
        if (UiFeedback.kind === "warning") return Theme.warning
        if (UiFeedback.kind === "error") return Theme.error
        return Theme.textPrimary
    }

    function present() {
        hideTimer.stop()
        exitAnimation.stop()
        entranceAnimation.stop()
        root.visible = true
        toastCard.y = -toastCard.height - Theme.space8
        toastCard.opacity = 0
        entranceAnimation.start()
        hideTimer.interval = UiFeedback.durationMs
        hideTimer.restart()
    }

    function dismiss() {
        hideTimer.stop()
        if (root.visible)
            exitAnimation.start()
    }

    Connections {
        target: UiFeedback
        function onSerialChanged() { root.present() }
    }

    Timer {
        id: hideTimer
        interval: 4000
        repeat: false
        onTriggered: exitAnimation.start()
    }

    Rectangle {
        id: toastCard
        anchors.horizontalCenter: parent.horizontalCenter
        width: Math.min(560, Math.max(280, root.width - Theme.space32))
        height: Math.max(48, toastText.implicitHeight + Theme.space24)
        radius: Theme.radiusMedium
        color: root.backgroundColor()
        border.width: 1
        border.color: root.borderColor()
        opacity: 0

        Text {
            id: toastText
            anchors.fill: parent
            anchors.leftMargin: Theme.space16
            anchors.rightMargin: Theme.space32 + Theme.space12
            text: UiFeedback.message
            color: root.textColor()
            font.family: Theme.fontFamily
            font.pixelSize: 13
            font.weight: Font.DemiBold
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            wrapMode: Text.Wrap
        }

        Button {
            id: dismissButton
            anchors.top: parent.top
            anchors.right: parent.right
            anchors.topMargin: Theme.space4
            anchors.rightMargin: Theme.space4
            width: 32
            height: 32
            text: "×"
            display: AbstractButton.TextOnly
            Accessible.name: "Đóng thông báo"
            onClicked: root.dismiss()
            background: Rectangle {
                radius: Theme.radiusSmall
                color: dismissButton.hovered ? Theme.surfaceHover : "transparent"
            }
            contentItem: Text {
                text: dismissButton.text
                color: root.textColor()
                font.family: Theme.fontFamily
                font.pixelSize: 18
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
            }
        }
    }

    ParallelAnimation {
        id: entranceAnimation
        NumberAnimation {
            target: toastCard
            property: "y"
            from: -toastCard.height - Theme.space8
            to: Theme.space16
            duration: 260
            easing.type: Easing.OutQuart
        }
        NumberAnimation {
            target: toastCard
            property: "opacity"
            from: 0
            to: 1
            duration: 220
            easing.type: Easing.OutQuart
        }
    }

    ParallelAnimation {
        id: exitAnimation
        NumberAnimation {
            target: toastCard
            property: "y"
            to: -toastCard.height - Theme.space8
            duration: 180
            easing.type: Easing.InQuart
        }
        NumberAnimation {
            target: toastCard
            property: "opacity"
            to: 0
            duration: 150
            easing.type: Easing.InQuart
        }
        onFinished: root.visible = false
    }
}
