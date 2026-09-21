pragma Singleton

import QtQuick

QtObject {
    readonly property color canvas: "#080D12"
    readonly property color surfaceBase: "#0D141B"
    readonly property color surfacePanel: "#111A22"
    readonly property color surfaceRaised: "#18232D"
    readonly property color surfaceHover: "#202C37"
    readonly property color surfaceDisabled: "#10171E"
    readonly property color borderSubtle: "#25313C"
    readonly property color borderStrong: "#344250"
    readonly property color textPrimary: "#F3F6F8"
    readonly property color textSecondary: "#A8B3BE"
    readonly property color textMuted: "#7E8A96"
    readonly property color iconMuted: "#8D99A5"
    readonly property color primary: "#7C3AED"
    readonly property color primaryHover: "#8B5CF6"
    readonly property color primaryPressed: "#6D28D9"
    readonly property color selectionFill: "#2D2144"
    readonly property color focusRing: "#BCA7FF"
    readonly property color success: "#42C77A"
    readonly property color successTint: "#143124"
    readonly property color successBorder: "#245E3D"
    readonly property color warning: "#F6B84A"
    readonly property color warningTint: "#332816"
    readonly property color warningBorder: "#6E5422"
    readonly property color error: "#FF646D"
    readonly property color errorTint: "#351A20"
    readonly property color errorBorder: "#6E2D36"
    readonly property color danger: "#8F1E28"
    readonly property color dangerPressed: "#731821"
    readonly property color info: "#52A8FF"
    readonly property color audio: "#27BFA3"
    readonly property color video: "#71869A"
    readonly property color transparent: "#00000000"

    readonly property string fontFamily: "Be Vietnam Pro"
    readonly property string cjkFontFamily: "Noto Sans CJK SC"
    readonly property string timecodeFamily: "Cascadia Mono"
    readonly property int radiusSmall: 4
    readonly property int radiusMedium: 8
    readonly property int radiusLarge: 12
    readonly property int space4: 4
    readonly property int space8: 8
    readonly property int space12: 12
    readonly property int space16: 16
    readonly property int space24: 24
    readonly property int space32: 32
    readonly property int motionFast: 120
    readonly property int motionNormal: 200
    readonly property int motionSlow: 320
}
