pragma ComponentBehavior: Bound

import QtQuick

Item {
    id: root

    property string name: ""
    property color color: "white"

    implicitWidth: 20
    implicitHeight: 20

    Image {
        id: iconImage
        anchors.centerIn: parent
        width: Math.min(parent.width, 20)
        height: width
        source: root.name ? Qt.resolvedUrl("../../assets/icons/" + root.name + ".svg") : ""
        sourceSize.width: 24
        sourceSize.height: 24
        fillMode: Image.PreserveAspectFit
        smooth: true
        mipmap: true
        opacity: root.color.a
    }
}
