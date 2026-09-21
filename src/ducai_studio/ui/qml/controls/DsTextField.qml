import QtQuick
import QtQuick.Controls
import DucaiStudio 1.0

TextField {
    id: control

    implicitHeight: 36
    leftPadding: 10
    rightPadding: 10
    color: Theme.textPrimary
    placeholderTextColor: Theme.textMuted
    selectionColor: Theme.primary
    selectedTextColor: Theme.textPrimary
    font.family: Theme.fontFamily
    font.pixelSize: 13
    Accessible.name: placeholderText

    background: Rectangle {
        color: control.enabled ? Theme.surfaceRaised : Theme.surfaceDisabled
        radius: Theme.radiusSmall
        border.width: control.activeFocus ? 2 : 1
        border.color: control.activeFocus ? Theme.focusRing : Theme.borderSubtle
        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
    }
}
