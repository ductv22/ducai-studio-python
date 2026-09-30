import QtQuick
import QtQuick.Controls
import DucaiStudio 1.0

TabButton {
    id: control

    property string tooltipText: ""

    implicitHeight: 36
    implicitWidth: Math.max(88, contentItem.implicitWidth + 24)
    padding: 8
    hoverEnabled: true
    Accessible.name: text
    Accessible.role: Accessible.PageTab
    Accessible.selected: checked
    Accessible.focusable: true

    contentItem: Text {
        text: control.text
        color: control.checked ? Theme.textPrimary : Theme.textSecondary
        font.family: Theme.fontFamily
        font.pixelSize: 12
        font.weight: control.checked ? Font.DemiBold : Font.Medium
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    background: Rectangle {
        color: control.checked
            ? Theme.selectionFill
            : (control.hovered ? Theme.surfaceHover : Theme.transparent)
        radius: Theme.radiusSmall
        border.width: control.visualFocus ? 2 : 0
        border.color: Theme.focusRing

        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            height: 2
            visible: control.checked
            color: Theme.primary
        }

        Behavior on color {
            ColorAnimation { duration: Theme.motionFast }
        }
    }

    ToolTip.visible: control.hovered && Boolean(control.tooltipText)
    ToolTip.text: control.tooltipText
    ToolTip.delay: 350
}
