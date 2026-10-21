pragma ComponentBehavior: Bound

import QtQuick

Item {
    id: root
    required property var projectModel
    required property string clipId
    required property real startUs
    required property real endUs
    property bool loadingEnabled: true
    property var tiles: []

    // Restart on gesture changes. Image requests begin only after the viewport
    // settles; already loaded images stay on screen while pan/zoom is active.
    Timer {
        id: requestDelay
        interval: 120
        onTriggered: {
            if (root.loadingEnabled && root.width >= 24 && root.endUs > root.startUs)
                root.tiles = root.projectModel.timelineMediaTiles(
                    root.clipId, root.startUs, root.endUs, root.width)
            else
                root.tiles = []
        }
    }
    onClipIdChanged: { tiles = []; requestDelay.restart() }
    onStartUsChanged: requestDelay.restart()
    onEndUsChanged: requestDelay.restart()
    onWidthChanged: requestDelay.restart()
    onLoadingEnabledChanged: requestDelay.restart()
    Component.onCompleted: requestDelay.restart()

    Repeater {
        model: root.tiles
        delegate: Item {
            id: tileItem
            required property var modelData
            x: modelData.waveform ? 0 : modelData.x
            width: modelData.waveform ? root.width : modelData.width
            height: root.height
            clip: true
            onModelDataChanged: {
                if (modelData.waveform) waveformCanvas.loadImage(modelData.url)
                waveformCanvas.requestPaint()
            }
            Image {
                visible: !tileItem.modelData.waveform
                width: tileItem.width
                height: tileItem.height
                source: tileItem.modelData.waveform ? "" : tileItem.modelData.url
                asynchronous: true
                cache: true
                fillMode: tileItem.modelData.waveform ? Image.Stretch : Image.PreserveAspectCrop
            }
            Canvas {
                id: waveformCanvas
                anchors.fill: parent
                visible: tileItem.modelData.waveform
                onImageLoaded: requestPaint()
                Component.onCompleted: {
                    if (visible) loadImage(tileItem.modelData.url)
                }
                onPaint: {
                    const ctx = getContext("2d")
                    ctx.clearRect(0, 0, width, height)
                    if (!isImageLoaded(tileItem.modelData.url)) return
                    const regions = tileItem.modelData.regions || []
                    for (let i = 0; i < regions.length; i++) {
                        const region = regions[i]
                        ctx.drawImage(tileItem.modelData.url,
                            region.crop_start * 256, 0,
                            (region.crop_end - region.crop_start) * 256, 40,
                            region.x, 0, region.width, height)
                    }
                }
            }
        }
    }
}
