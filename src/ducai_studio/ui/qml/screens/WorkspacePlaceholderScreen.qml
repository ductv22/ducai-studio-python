import QtQuick
import QtQuick.Layouts
import "../controls"

Item {
    id: root

    property var appModel

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 12

        Panel {
            Layout.fillWidth: true
            Layout.preferredHeight: 64

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 16
                anchors.rightMargin: 16

                SectionTitle { text: root.appModel.currentWorkspaceTitle }
                Item { Layout.fillWidth: true }
                StatusPill { runtimeState: "starting"; text: "Chưa nối provider" }
            }
        }

        Panel {
            Layout.fillWidth: true
            Layout.fillHeight: true

            EmptyState {
                anchors.fill: parent
                title: root.appModel.currentWorkspaceTitle + " đang được xây dựng"
                description: "Workspace này sẽ nhận dữ liệu thật ở giai đoạn tương ứng; không dùng màn hình hoặc số liệu demo."
            }
        }
    }
}
