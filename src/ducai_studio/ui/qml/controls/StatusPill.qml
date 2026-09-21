import QtQuick
import QtQuick.Layouts
import DucaiStudio 1.0

Rectangle {
    id: root

    property string runtimeState: "starting"
    property string text: runtimeState === "ready" ? "Sẵn sàng" : "Đang khởi động"

    readonly property bool ready: runtimeState === "ready"
    readonly property bool failed: runtimeState === "error"

    implicitHeight: 32
    implicitWidth: label.implicitWidth + 36
    radius: 16
    color: failed ? Theme.errorTint : (ready ? Theme.successTint : Theme.warningTint)
    border.width: 1
    border.color: failed ? Theme.errorBorder : (ready ? Theme.successBorder : Theme.warningBorder)

    RowLayout {
        anchors.centerIn: parent
        spacing: 8

        Rectangle {
            Layout.preferredWidth: 8
            Layout.preferredHeight: 8
            radius: 4
            color: root.failed ? Theme.error : (root.ready ? Theme.success : Theme.warning)
        }

        Text {
            id: label
            text: root.text
            color: Theme.textPrimary
            font.family: Theme.fontFamily
            font.pixelSize: 12
            font.weight: Font.DemiBold
        }
    }
}
