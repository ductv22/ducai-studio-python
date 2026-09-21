pragma Singleton

import QtQuick

QtObject {
    property string message: ""
    property string kind: "info"
    property int durationMs: 4000
    property int serial: 0

    function show(nextMessage, nextKind, nextDurationMs) {
        const normalized = String(nextMessage || "").trim()
        if (normalized.length === 0) return
        message = normalized
        kind = String(nextKind || "info")
        // Backend errors and completion notices may need a little more time
        // to read.  The toast remains dismissible, so a 10s upper bound does
        // not trap the user in a notification.
        durationMs = Math.max(3000, Math.min(10000, Number(nextDurationMs || 4000)))
        serial += 1
    }
}
