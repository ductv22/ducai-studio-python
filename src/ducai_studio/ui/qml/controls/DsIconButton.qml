import QtQuick
import QtQuick.Controls
import DucaiStudio 1.0

Button {
    id: control

    property string iconName: ""
    property string tooltipText: ""
    property string disabledReason: "Điều khiển chưa sẵn sàng."
    property bool active: false

    implicitWidth: 36
    implicitHeight: 36
    hoverEnabled: true
    padding: 0
    Accessible.name: tooltipText
    Accessible.description: enabled ? "" : disabledReason
    Accessible.role: Accessible.Button

    contentItem: DsIcon {
        anchors.centerIn: parent
        width: 20
        height: 20
        name: control.iconName
        opacity: control.enabled ? 1.0 : 0.45
    }

    background: Rectangle {
        radius: Theme.radiusSmall
        color: {
            if (!control.enabled) return "transparent"
            if (control.down) return Theme.primaryPressed
            if (control.active) return Theme.surfaceHover
            if (control.hovered) return Theme.surfaceRaised
            return "transparent"
        }
        border.width: control.visualFocus ? 2 : 0
        border.color: Theme.focusRing
        Behavior on color {
            ColorAnimation { duration: Theme.motionFast }
        }
    }

    ToolTip.visible: control.hovered && Boolean(control.tooltipText)
    ToolTip.text: control.tooltipText
    ToolTip.delay: 350
}
