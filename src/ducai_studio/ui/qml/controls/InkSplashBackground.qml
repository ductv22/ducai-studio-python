import QtQuick

Item {
    id: root

    property color inkColor: "#080808"
    property string variant: "ink_splash"
    property bool vertical: true
    property bool mirrored: false

    function shapeData() {
        if (root.variant === "ink_wash") {
            return {
                capScale: 0.82,
                points: [
                    [0.43, 0.00], [0.62, 0.02], [0.58, 0.06], [0.78, 0.08],
                    [0.70, 0.12], [0.96, 0.16], [0.76, 0.20], [0.88, 0.25],
                    [0.74, 0.30], [0.93, 0.35], [0.80, 0.40], [0.89, 0.46],
                    [0.73, 0.51], [0.95, 0.57], [0.78, 0.62], [0.88, 0.68],
                    [0.76, 0.74], [0.91, 0.80], [0.72, 0.85], [0.78, 0.91],
                    [0.61, 0.94], [0.56, 1.00], [0.39, 0.97], [0.28, 0.93],
                    [0.32, 0.88], [0.11, 0.84], [0.23, 0.78], [0.06, 0.73],
                    [0.24, 0.67], [0.10, 0.61], [0.26, 0.56], [0.05, 0.50],
                    [0.25, 0.45], [0.12, 0.39], [0.28, 0.34], [0.07, 0.28],
                    [0.27, 0.23], [0.04, 0.18], [0.29, 0.13], [0.17, 0.08],
                    [0.38, 0.06]
                ],
                splats: [
                    [0.07, 0.11, 0.032, 0.020], [0.95, 0.22, 0.026, 0.018],
                    [0.03, 0.64, 0.020, 0.014], [0.92, 0.72, 0.036, 0.024],
                    [0.16, 0.91, 0.025, 0.018], [0.84, 0.06, 0.018, 0.013]
                ]
            }
        }
        if (root.variant === "ink_cloud") {
            return {
                capScale: 0.96,
                points: [
                    [0.38, 0.02], [0.56, 0.00], [0.70, 0.05], [0.78, 0.11],
                    [0.91, 0.17], [0.84, 0.24], [0.94, 0.31], [0.85, 0.38],
                    [0.92, 0.46], [0.84, 0.54], [0.94, 0.62], [0.83, 0.69],
                    [0.89, 0.77], [0.77, 0.84], [0.73, 0.92], [0.58, 0.98],
                    [0.43, 1.00], [0.30, 0.95], [0.22, 0.89], [0.24, 0.82],
                    [0.10, 0.76], [0.17, 0.68], [0.07, 0.61], [0.16, 0.53],
                    [0.06, 0.45], [0.17, 0.37], [0.09, 0.29], [0.20, 0.22],
                    [0.17, 0.14], [0.30, 0.09]
                ],
                splats: [
                    [0.11, 0.27, 0.022, 0.016], [0.91, 0.43, 0.018, 0.013],
                    [0.13, 0.72, 0.017, 0.012]
                ]
            }
        }
        if (root.variant === "brush_stroke") {
            return {
                capScale: 0.62,
                points: [
                    [0.36, 0.01], [0.52, 0.04], [0.66, 0.00], [0.75, 0.06],
                    [0.70, 0.12], [0.83, 0.19], [0.78, 0.29], [0.86, 0.40],
                    [0.80, 0.50], [0.85, 0.61], [0.77, 0.72], [0.82, 0.82],
                    [0.69, 0.91], [0.62, 1.00], [0.54, 0.94], [0.45, 1.00],
                    [0.40, 0.93], [0.27, 0.88], [0.22, 0.78], [0.18, 0.67],
                    [0.23, 0.56], [0.16, 0.45], [0.22, 0.34], [0.17, 0.24],
                    [0.28, 0.15], [0.24, 0.08]
                ],
                splats: [
                    [0.12, 0.18, 0.016, 0.010], [0.88, 0.83, 0.018, 0.011]
                ]
            }
        }
        return {
            capScale: 0.72,
            points: [
                [0.46, 0.01], [0.63, 0.04], [0.58, 0.09], [0.79, 0.12],
                [0.68, 0.17], [0.88, 0.22], [0.75, 0.27], [0.92, 0.34],
                [0.77, 0.39], [0.89, 0.45], [0.73, 0.50], [0.86, 0.57],
                [0.74, 0.62], [0.91, 0.69], [0.75, 0.74], [0.83, 0.81],
                [0.63, 0.84], [0.70, 0.92], [0.55, 0.89], [0.61, 0.99],
                [0.43, 0.95], [0.33, 1.00], [0.30, 0.90], [0.12, 0.85],
                [0.25, 0.79], [0.07, 0.73], [0.23, 0.67], [0.10, 0.60],
                [0.25, 0.54], [0.08, 0.48], [0.27, 0.42], [0.12, 0.35],
                [0.29, 0.30], [0.09, 0.23], [0.31, 0.18], [0.22, 0.10],
                [0.41, 0.08]
            ],
            splats: [
                [0.08, 0.16, 0.025, 0.016], [0.94, 0.29, 0.018, 0.012],
                [0.04, 0.64, 0.020, 0.013], [0.91, 0.82, 0.028, 0.018]
            ]
        }
    }

    function longCoordinate(value, length, thickness, capScale) {
        const edge = 0.24
        const cap = Math.min(length * 0.34, thickness * capScale)
        if (value <= edge)
            return value / edge * cap
        if (value >= 1.0 - edge)
            return length - (1.0 - value) / edge * cap
        return cap + (value - edge) / (1.0 - edge * 2.0)
            * Math.max(0, length - cap * 2.0)
    }

    function orient(u, v, capScale) {
        const thickness = root.vertical ? width : height
        const length = root.vertical ? height : width
        const cross = u * thickness
        const along = root.longCoordinate(v, length, thickness, capScale)
        let x = root.vertical ? cross : along
        let y = root.vertical ? along : thickness - cross
        if (root.mirrored) x = width - x
        return Qt.point(x, y)
    }

    onInkColorChanged: inkCanvas.requestPaint()
    onVariantChanged: inkCanvas.requestPaint()
    onVerticalChanged: inkCanvas.requestPaint()
    onMirroredChanged: inkCanvas.requestPaint()
    onWidthChanged: inkCanvas.requestPaint()
    onHeightChanged: inkCanvas.requestPaint()

    Canvas {
        id: inkCanvas
        anchors.fill: parent
        renderTarget: Canvas.Image

        onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            ctx.clearRect(0, 0, width, height)
            if (width <= 0 || height <= 0) return

            const shape = root.shapeData()
            const points = shape.points
            ctx.fillStyle = root.inkColor
            ctx.beginPath()
            const first = root.orient(
                points[0][0], points[0][1], shape.capScale
            )
            const last = root.orient(
                points[points.length - 1][0],
                points[points.length - 1][1],
                shape.capScale
            )
            ctx.moveTo((last.x + first.x) / 2, (last.y + first.y) / 2)
            let point = first
            for (let index = 0; index < points.length; ++index) {
                point = root.orient(
                    points[index][0], points[index][1], shape.capScale
                )
                const nextIndex = (index + 1) % points.length
                const next = root.orient(
                    points[nextIndex][0], points[nextIndex][1], shape.capScale
                )
                ctx.quadraticCurveTo(
                    point.x,
                    point.y,
                    (point.x + next.x) / 2,
                    (point.y + next.y) / 2
                )
            }
            ctx.closePath()
            ctx.fill()

            const thickness = root.vertical ? width : height
            const splats = shape.splats
            for (let splash = 0; splash < splats.length; ++splash) {
                point = root.orient(
                    splats[splash][0], splats[splash][1], shape.capScale
                )
                const crossRadius = Math.max(1, thickness * splats[splash][2])
                const longRadius = Math.max(1, thickness * splats[splash][3])
                const radiusX = root.vertical ? crossRadius : longRadius
                const radiusY = root.vertical ? longRadius : crossRadius
                ctx.beginPath()
                ctx.save()
                ctx.translate(point.x, point.y)
                ctx.scale(radiusX, radiusY)
                ctx.arc(0, 0, 1, 0, Math.PI * 2, false)
                ctx.restore()
                ctx.fill()
            }
        }
    }
}
