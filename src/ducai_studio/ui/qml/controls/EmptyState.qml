import QtQuick
import QtQuick.Layouts
import DucaiStudio 1.0

Item {
    id: root

    property string title: "Chưa có dữ liệu"
    property string description: ""
    property string iconName: "panels-top-left"

    implicitHeight: content.implicitHeight + 48

    ColumnLayout {
        id: content
        anchors.centerIn: parent
        width: Math.min(parent.width - 48, 460)
        spacing: 8

        Rectangle {
            Layout.alignment: Qt.AlignHCenter
            Layout.preferredWidth: 38
            Layout.preferredHeight: 38
            radius: Theme.radiusMedium
            color: Theme.surfaceRaised
            border.width: 1
            border.color: Theme.borderStrong

            DsIcon {
                anchors.centerIn: parent
                width: 20
                height: 20
                name: root.iconName
                color: Theme.iconMuted
            }
        }

        Text {
            Layout.fillWidth: true
            text: root.title
            color: Theme.textPrimary
            font.family: Theme.fontFamily
            font.pixelSize: 15
            font.weight: Font.DemiBold
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.Wrap
        }

        Text {
            Layout.fillWidth: true
            text: root.description
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: 12
            lineHeight: 1.3
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.Wrap
        }
    }
}
