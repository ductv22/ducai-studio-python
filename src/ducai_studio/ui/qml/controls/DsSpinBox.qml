import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import DucaiStudio 1.0

SpinBox {
    id: control

    editable: true
    hoverEnabled: true
    implicitHeight: 36
    implicitWidth: 112
    Layout.minimumWidth: 104
    font.family: Theme.fontFamily
    font.pixelSize: 13
    Accessible.name: accessibleName
    Accessible.role: Accessible.SpinBox
    Accessible.focusable: true

    property string accessibleName: "Giá trị số"
    readonly property int indicatorWidth: 28

    contentItem: TextInput {
        z: 2
        text: control.textFromValue(control.value, control.locale)
        color: control.enabled ? Theme.textPrimary : Theme.textMuted
        selectionColor: Theme.primary
        selectedTextColor: Theme.textPrimary
        font: control.font
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        readOnly: !control.editable
        validator: control.validator
        inputMethodHints: Qt.ImhFormattedNumbersOnly
        selectByMouse: true
        leftPadding: control.indicatorWidth + Theme.space4
        rightPadding: control.indicatorWidth + Theme.space4
    }

    up.indicator: Rectangle {
        x: control.mirrored ? 0 : parent.width - width
        height: parent.height
        implicitWidth: control.indicatorWidth
        color: control.up.pressed
            ? Theme.borderStrong
            : (control.up.hovered ? Theme.surfaceHover : Theme.surfaceRaised)
        radius: Theme.radiusSmall
        border.width: 1
        border.color: Theme.borderSubtle
        Text {
            anchors.centerIn: parent
            text: "+"
            color: control.enabled ? Theme.textSecondary : Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: 14
            font.weight: Font.DemiBold
        }
    }

    down.indicator: Rectangle {
        x: control.mirrored ? parent.width - width : 0
        height: parent.height
        implicitWidth: control.indicatorWidth
        color: control.down.pressed
            ? Theme.borderStrong
            : (control.down.hovered ? Theme.surfaceHover : Theme.surfaceRaised)
        radius: Theme.radiusSmall
        border.width: 1
        border.color: Theme.borderSubtle
        Text {
            anchors.centerIn: parent
            text: "−"
            color: control.enabled ? Theme.textSecondary : Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: 14
            font.weight: Font.DemiBold
        }
    }

    background: Rectangle {
        color: control.enabled ? Theme.surfaceRaised : Theme.surfaceDisabled
        radius: Theme.radiusSmall
        border.width: control.visualFocus ? 2 : 1
        border.color: control.visualFocus ? Theme.focusRing : Theme.borderSubtle
        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
    }

    ToolTip.visible: control.hovered && Boolean(control.accessibleName)
    ToolTip.text: control.accessibleName
        + ". Dùng nút giảm hoặc tăng, hoặc nhập giá trị trực tiếp."
    ToolTip.delay: 450
}
