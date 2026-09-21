import QtQuick
import QtQuick.Controls
import DucaiStudio 1.0

Button {
    id: control

    property bool danger: false

    implicitWidth: 240
    implicitHeight: 36
    leftPadding: 12
    rightPadding: 12
    hoverEnabled: true
    Accessible.name: text
    Accessible.role: Accessible.MenuItem
    Accessible.focusable: true

    contentItem: Text {
        text: control.text
        color: control.enabled
            ? (control.danger ? Theme.danger : Theme.textPrimary)
            : Theme.textMuted
        font.family: Theme.fontFamily
        font.pixelSize: 12
        horizontalAlignment: Text.AlignLeft
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    background: Rectangle {
        radius: Theme.radiusSmall
        color: control.down || control.hovered
            ? Theme.surfaceHover : Theme.surfaceRaised

        Behavior on color {
            ColorAnimation { duration: Theme.motionFast }
        }
    }
}
