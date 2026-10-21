pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import DucaiStudio 1.0

Popup {
    id: root
    objectName: "videoClipMenu"
    required property var appModel
    signal pauseRequested()
    parent: Overlay.overlay
    width: 246
    height: menuColumn.implicitHeight + padding * 2
    padding: Theme.space4
    popupType: Popup.Item
    modal: false
    focus: true
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    z: 1000

    function openAt(sourceItem, localX, localY) {
        const point = sourceItem.mapToItem(Overlay.overlay, localX, localY)
        x = Math.max(Theme.space8, Math.min(Overlay.overlay.width - width - Theme.space8, point.x))
        y = Math.max(Theme.space8, Math.min(Overlay.overlay.height - height - Theme.space8, point.y))
        open()
    }

    background: Rectangle {
        color: Theme.surfaceRaised
        border.width: 1
        border.color: Theme.borderStrong
        radius: Theme.radiusSmall
    }
    contentItem: ColumnLayout {
        id: menuColumn
        spacing: 0
        DsContextButton {
            Layout.fillWidth: true
            text: "Cắt    X"
            onClicked: { root.close(); root.pauseRequested(); root.appModel.splitVideoAtPlayhead() }
        }
        DsContextButton {
            Layout.fillWidth: true
            text: "Xóa    Delete"
            danger: true
            onClicked: { root.close(); root.pauseRequested(); root.appModel.deleteTimelineSelection() }
        }
        DsContextButton {
            Layout.fillWidth: true
            text: "Copy    Ctrl+C"
            onClicked: { root.close(); root.appModel.copySelectedVideo() }
        }
        DsContextButton {
            Layout.fillWidth: true
            text: "Paste    Ctrl+V"
            enabled: root.appModel.canPasteVideo
            onClicked: { root.close(); root.pauseRequested(); root.appModel.pasteVideoAtPlayhead() }
        }
        DsContextButton {
            Layout.fillWidth: true
            text: "Nhân bản    Ctrl+D"
            onClicked: { root.close(); root.pauseRequested(); root.appModel.duplicateSelectedVideo() }
        }
        DsContextButton {
            Layout.fillWidth: true
            text: root.appModel.selectedClipMuted ? "Bật âm thanh" : "Tắt âm thanh"
            onClicked: { root.close(); root.appModel.toggleSelectedVideoMuted() }
        }
    }
}
