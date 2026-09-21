import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import DucaiStudio 1.0

ItemDelegate {
    id: control

    property string shortCode: ""
    property string iconName: ""
    property bool compact: false
    property bool selected: false

    implicitHeight: 40
    leftPadding: compact ? 10 : 12
    rightPadding: 10
    hoverEnabled: true
    Accessible.name: text
    Accessible.role: Accessible.Button
    Accessible.selected: selected
    Accessible.focusable: true

    contentItem: RowLayout {
        spacing: 10

        DsIcon {
            Layout.preferredWidth: 26
            Layout.preferredHeight: 26
            name: control.iconName
            color: control.selected ? Theme.textPrimary : Theme.iconMuted
        }

        Text {
            Layout.fillWidth: true
            visible: !control.compact
            text: control.text
            color: control.selected ? Theme.textPrimary : Theme.textSecondary
            font.family: Theme.fontFamily
            font.pixelSize: 13
            font.weight: control.selected ? Font.DemiBold : Font.Normal
            elide: Text.ElideRight
        }
    }

    background: Rectangle {
        radius: Theme.radiusMedium
        color: control.selected
            ? Theme.selectionFill
            : (control.hovered ? Theme.surfaceHover : Theme.transparent)
        border.width: control.visualFocus ? 2 : 0
        border.color: Theme.focusRing
    }

    ToolTip.visible: compact && hovered
    ToolTip.text: text
    ToolTip.delay: 600
}
