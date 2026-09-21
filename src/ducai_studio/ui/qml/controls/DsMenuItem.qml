import QtQuick
import QtQuick.Controls
import DucaiStudio 1.0

MenuItem {
    id: control

    implicitHeight: 36
    implicitWidth: 210
    leftPadding: 12
    rightPadding: 12
    Accessible.name: text

    contentItem: Text {
        text: control.text
        color: control.enabled ? Theme.textPrimary : Theme.textMuted
        font.family: Theme.fontFamily
        font.pixelSize: 12
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    background: Rectangle {
        color: control.highlighted ? Theme.surfaceHover : Theme.surfaceRaised
    }
}
