pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import DucaiStudio 1.0

Panel {
    id: root

    required property var jobModel
    property var appModel
    property bool allowHistoryDeletion: false
    property string historyScope: "subtitle"
    property string title: "Tác vụ"
    property string emptyTitle: "Chưa có tác vụ"
    property string emptyDescription: "Tác vụ thật sẽ xuất hiện tại đây."

    function kindLabel(kind) {
        if (kind === "ocr") return "Trích xuất phụ đề"
        if (kind === "translate") return "Dịch phụ đề"
        if (kind === "tts") return "Tạo giọng"
        if (kind === "export") return "Xuất video"
        return kind
    }

    function reviewRequired(errorCode) {
        return errorCode === "ocr_review_required"
            || errorCode === "translation_review_required"
            || errorCode === "tts_review_required"
    }

    function stateLabel(state, errorCode) {
        if (state === "failed" && reviewRequired(errorCode)) return "Cần rà soát"
        if (state === "failed" && (errorCode === "tts_timeline_overlap_guard_failed"
                || errorCode === "voice_conform_timeline_overlap_guard_failed"))
            return "Cần xếp lại voice"
        if (state === "failed" && errorCode === "provider_access_blocked")
            return "Bị gateway/CDN chặn"
        if (state === "failed" && errorCode === "authentication_required")
            return "Cần xác thực lại"
        const labels = {
            "queued": "Đang chờ",
            "start_requested": "Đang khởi động",
            "running": "Đang chạy",
            "pause_requested": "Đang tạm dừng",
            "paused": "Đã tạm dừng",
            "resume_requested": "Đang tiếp tục",
            "cancel_requested": "Đang hủy",
            "cancelled": "Đã hủy",
            "succeeded": "Hoàn tất",
            "failed": "Thất bại",
            "interrupted": "Bị gián đoạn"
        }
        return labels[state] || state
    }

    function stateColor(state, errorCode) {
        if (state === "failed" && reviewRequired(errorCode)) return Theme.warning
        if (state === "succeeded") return Theme.success
        if (state === "failed" || state === "cancelled") return Theme.error
        if (state === "running" || state === "resume_requested") return Theme.info
        return Theme.warning
    }

    function activeState(state) {
        return ["queued", "start_requested", "running", "pause_requested",
            "resume_requested", "cancel_requested"].indexOf(state) >= 0
    }

    function activityLabel(model) {
        if (model.phase) return phaseLabel(model.phase)
        if (model.kind === "tts" && activeState(model.state))
            return "Đang nạp VieNeu và chuẩn bị dò cache"
        return ""
    }

    function jobStateLabel(model) {
        if (model.kind === "tts" && !model.phase) {
            if (model.state === "queued") return "Chuẩn bị giọng"
            if (model.state === "start_requested") return "Đang nạp model"
            if (model.state === "running") return "Chuẩn bị cache"
            if (model.state === "resume_requested") return "Đang mở cache"
        }
        return stateLabel(model.state, model.errorCode)
    }

    function phaseLabel(phase) {
        const labels = {
            "preparing": "Đang chuẩn bị",
            "fast": "Quét phụ đề nhanh",
            "scene_quality": "Đọc lại chữ trong cảnh bằng Quality",
            "quality": "Rà soát vùng nghi vấn",
            "rapid_fallback": "Chuyển OCR tương thích",
            "extract_audio": "Đang tách audio 16 kHz",
            "load_model": "Đang nạp Whisper large-v3",
            "transcribe": "Whisper đang nhận dạng lời nói",
            "cpu_fallback": "CUDA lỗi · đang chuyển sang CPU",
            "finalize": "Đang kiểm tra và xuất SRT",
            "film_bible": "Lập Film Bible",
            "translation": "Đang dịch",
            "translation_qa": "Kiểm tra bản dịch",
            "qa_repair": "Sửa cue chưa đạt QA",
            "recovery_backoff": "Chờ tự khôi phục dịch vụ",
            "cache_restore": "Đang kiểm tra cache",
            "runtime_check": "Đang kiểm tra bộ máy giọng nói",
            "model_loading": "Đang nạp VieNeu lên GPU",
            "voice_binding": "Đang chuẩn bị giọng đã chọn",
            "warm_ready": "VieNeu đã sẵn sàng từ lượt trước",
            "warmup": "Đang làm nóng bộ máy giọng nói",
            "synthesis": "Đang tổng hợp giọng",
            "targeted_retry": "Tạo lại cue lỗi",
            "timeline_assembly": "Đang ghép timeline giọng",
            "timeline_assembly_measure": "Đang ghép và đo timeline giọng",
            "final_master_measure": "Đang đo độ lớn âm thanh",
            "final_master_apply": "Đang chuẩn hóa âm lượng",
            "render": "Đang render video",
            "finalize_concat": "Đang ghép MP4",
            "finalize_probe": "Đang kiểm tra video/audio",
            "finalize_checksum": "Đang xác minh SHA-256",
            "finalize_publish": "Đang công bố file thành phẩm"
        }
        return labels[phase] || phase
    }

    function percentLabel(current, total) {
        if (!(total > 0)) return "0,0"
        const value = Math.max(0, Math.min(100, 100.0 * current / total))
        return value.toFixed(1).replace(".", ",")
    }

    function progressSummary(model) {
        if (model.kind === "ocr" && model.phase === "finalize") {
            return (model.phaseDetail || phaseLabel(model.phase))
                + (model.phaseTotal > 0 ? " · "
                    + percentLabel(model.phaseCurrent, model.phaseTotal) + "%" : "")
                + " · đã chạy " + durationLabel(model.elapsedMs)
        }
        if (model.kind === "tts" && !model.phase && activeState(model.state)) {
            return "Đang khởi động tác vụ và chuẩn bị dò cache"
                + " · chưa có batch cue hoàn tất"
                + " · hoạt động " + durationLabel(model.progressAgeMs)
        }
        if (model.kind === "tts"
                && ["runtime_check", "model_loading", "voice_binding", "warm_ready"]
                    .indexOf(model.phase) >= 0) {
            return (model.phaseDetail || phaseLabel(model.phase))
                + " · đã hoàn tất " + model.current + "/" + model.total + " câu"
                + " · đang chờ " + durationLabel(model.progressAgeMs)
        }
        if (model.kind === "tts" && model.phase === "cache_restore") {
            return "Đã kiểm tra cache " + model.phaseCurrent + "/" + model.phaseTotal
                + " · khôi phục " + model.cacheRestoredCues + " cue"
                + " (dùng nguyên " + model.reusedProcessedCues
                + ", fit lại " + model.reprocessedRawCues + ")"
                + " · cần tạo " + model.pendingSynthesisCues + " cue"
                + (model.phaseDetail ? " · " + model.phaseDetail : "")
                + " · đã chạy " + durationLabel(model.elapsedMs)
        }
        if (model.kind === "tts"
                && (model.phase === "warmup" || model.phase === "synthesis")) {
            return (model.phase === "warmup" ? "Đang làm nóng · " : "Đã tổng hợp ")
                + model.phaseCurrent + "/" + model.phaseTotal
                + " cue cần tạo · tổng đã đạt " + model.current + "/" + model.total
                + (model.phaseDetail ? " · " + model.phaseDetail : "")
                + " · batch hiện tại " + durationLabel(model.progressAgeMs)
                + " · đã chạy " + durationLabel(model.elapsedMs)
        }
        if (model.kind === "tts" && model.phase === "targeted_retry") {
            return "Đang tạo lại cue lỗi · đã xử lý " + model.phaseCurrent
                + "/" + model.phaseTotal + " cue của lượt tạo"
                + " · đã chạy " + durationLabel(model.elapsedMs)
        }
        if (model.kind === "tts" && model.phase === "timeline_assembly") {
            return "Đã ghép " + model.phaseCurrent + "/" + model.phaseTotal
                + " cue vào timeline · đã chạy " + durationLabel(model.elapsedMs)
        }
        if (model.kind === "tts"
                && (model.phase === "timeline_assembly_measure"
                    || model.phase === "final_master_measure"
                    || model.phase === "final_master_apply")) {
            return phaseLabel(model.phase) + " "
                + percentLabel(model.phaseCurrent, model.phaseTotal) + "%"
                + (model.phaseDetail ? " · " + model.phaseDetail : "")
                + " · đã chạy " + durationLabel(model.elapsedMs)
        }
        if (model.kind === "translate" && model.cueTotal > 0) {
            return "Đã dịch " + model.translatedCues + "/" + model.cueTotal
                + " · QA " + model.qaCues + "/" + model.cueTotal
                + (model.chunkTotal > 0
                    ? " · lượt " + Math.min(model.chunkCurrent + 1, model.chunkTotal)
                        + "/" + model.chunkTotal : "")
                + " · đã chạy " + durationLabel(model.elapsedMs)
        }
        if (model.kind === "ocr"
                && ["extract_audio", "load_model", "transcribe", "cpu_fallback",
                    "finalize"].indexOf(model.phase) >= 0
                && model.phaseTotal > 0) {
            return phaseLabel(model.phase) + " "
                + percentLabel(model.phaseCurrent, model.phaseTotal) + "%"
                + (model.phaseDetail ? " · " + model.phaseDetail : "")
                + " · đã chạy " + durationLabel(model.elapsedMs)
        }
        if (model.kind === "export" && model.phaseTotal > 0) {
            if (model.phase === "finalize_checksum") {
                return phaseLabel(model.phase) + " "
                    + percentLabel(model.phaseCurrent, model.phaseTotal) + "%"
                    + " · đã đọc " + sizeLabel(model.phaseCurrent)
                    + "/" + sizeLabel(model.phaseTotal)
                    + " · đã chạy " + durationLabel(model.elapsedMs)
            }
            if (model.phase === "finalize_probe" || model.phase === "finalize_publish") {
                return phaseLabel(model.phase) + " "
                    + percentLabel(model.phaseCurrent, model.phaseTotal) + "%"
                    + " · đã chạy " + durationLabel(model.elapsedMs)
            }
            return phaseLabel(model.phase) + " "
                + percentLabel(model.phaseCurrent, model.phaseTotal) + "%"
                + (model.chunkTotal > 0
                    ? " · đoạn " + model.chunkCurrent + "/" + model.chunkTotal : "")
                + " · đã xử lý " + durationLabel(model.phaseCurrent / 1000)
                + "/" + durationLabel(model.phaseTotal / 1000)
                + " · bắt đầu " + clockLabel(model.startedAt || model.createdAt, 0)
                + " · đã chạy " + durationLabel(model.elapsedMs)
        }
        if (model.phaseTotal > 0) {
            return phaseLabel(model.phase) + " " + model.phaseCurrent + "/" + model.phaseTotal
                + (model.phaseDetail ? " · " + model.phaseDetail : "")
                + " · bắt đầu " + clockLabel(model.startedAt || model.createdAt, 0)
                + " · đã chạy " + durationLabel(model.elapsedMs)
        }
        return model.total > 0
            ? model.current + "/" + model.total
                + " · bắt đầu " + clockLabel(model.startedAt || model.createdAt, 0)
                + " · đã chạy " + durationLabel(model.elapsedMs)
            : "Tạo lúc " + model.createdAt
    }

    function sizeLabel(bytes) {
        if (!(bytes >= 0)) return "--"
        const units = ["B", "KB", "MB", "GB", "TB"]
        let value = Number(bytes)
        let unit = 0
        while (value >= 1024 && unit < units.length - 1) {
            value /= 1024
            unit += 1
        }
        return (unit === 0 ? value.toFixed(0) : value.toFixed(1).replace(".", ","))
            + " " + units[unit]
    }

    function phasePercent(model) {
        if (model.state === "succeeded") return 100
        return model.phaseTotal > 0
            ? 100.0 * model.phaseCurrent / model.phaseTotal
            : model.progressPercent
    }

    function durationLabel(milliseconds) {
        if (milliseconds === undefined || milliseconds === null || milliseconds < 0)
            return "--:--"
        const totalSeconds = Math.floor(milliseconds / 1000)
        const hours = Math.floor(totalSeconds / 3600)
        const minutes = Math.floor((totalSeconds % 3600) / 60)
        const seconds = totalSeconds % 60
        if (hours > 0)
            return hours + "g " + String(minutes).padStart(2, "0") + "p"
        return minutes + "p " + String(seconds).padStart(2, "0") + "s"
    }

    function clockLabel(isoText, offsetMilliseconds) {
        const base = isoText ? new Date(isoText) : new Date()
        if (Number.isNaN(base.getTime())) return "--:--"
        base.setTime(base.getTime() + Math.max(0, offsetMilliseconds || 0))
        return String(base.getHours()).padStart(2, "0")
            + ":" + String(base.getMinutes()).padStart(2, "0")
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Theme.space12
        spacing: Theme.space8

        RowLayout {
            Layout.fillWidth: true
            SectionTitle { text: root.title }
            Item { Layout.fillWidth: true }
            DsButton {
                visible: root.allowHistoryDeletion
                compact: true
                text: "Xoá toàn bộ lịch sử"
                operationState: root.appModel && root.appModel.jobHistoryBusy
                    ? "running" : "idle"
                runningText: "Đang xoá…"
                danger: true
                enabled: Boolean(root.appModel)
                    && root.appModel.backendReady
                    && !root.appModel.jobHistoryBusy
                    && root.appModel.currentProject.hasProject
                disabledReason: root.appModel && root.appModel.jobHistoryBusy
                    ? "Đang xoá lịch sử; hãy chờ hoàn tất."
                    : (root.historyScope === "tts"
                        ? "Tác vụ đang chạy sẽ được hủy an toàn rồi tự xóa; cache WAV dùng lại toàn cục vẫn được giữ."
                        : "Tác vụ đang chạy sẽ được hủy an toàn rồi tự xóa; cache riêng của lịch sử đã xóa sẽ mất.")
                onClicked: root.appModel.clearJobHistory(root.historyScope)
            }
        }

        EmptyState {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: root.jobModel.count === 0
            title: root.emptyTitle
            description: root.emptyDescription
            iconName: "list-todo"
        }

        ListView {
            id: jobList

            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: root.jobModel.count > 0
            clip: true
            spacing: Theme.space4
            boundsBehavior: Flickable.StopAtBounds
            model: root.jobModel
            Accessible.name: root.title
            Accessible.role: Accessible.List

            delegate: Rectangle {
                id: jobRow

                required property var model

                width: ListView.view.width
                height: jobRow.model.kind === "tts" ? 96 : 78
                radius: Theme.radiusSmall
                color: Theme.surfaceRaised
                Accessible.name: root.kindLabel(model.kind) + ", "
                    + root.jobStateLabel(model)
                Accessible.role: Accessible.ListItem

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: Theme.space8
                    spacing: Theme.space4

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Theme.space8
                        Rectangle {
                            Layout.preferredWidth: 8
                            Layout.preferredHeight: 8
                            radius: 4
                            color: root.stateColor(jobRow.model.state, jobRow.model.errorCode)
                        }
                        Text {
                            Layout.fillWidth: true
                            text: root.kindLabel(jobRow.model.kind)
                                + (root.activityLabel(jobRow.model)
                                    ? " · " + root.activityLabel(jobRow.model) : "")
                            color: Theme.textPrimary
                            font.family: Theme.fontFamily
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                            elide: Text.ElideRight
                        }
                        Text {
                            Layout.fillWidth: true
                            text: root.jobStateLabel(jobRow.model)
                            color: root.stateColor(jobRow.model.state, jobRow.model.errorCode)
                            font.family: Theme.fontFamily
                            font.pixelSize: 11
                            font.weight: Font.DemiBold
                            horizontalAlignment: Text.AlignRight
                        }
                        DsButton {
                            visible: root.allowHistoryDeletion
                            compact: true
                            text: "X"
                            danger: true
                            operationState: jobRow.model.state === "cancel_requested"
                                ? "cancel_requested"
                                : (root.appModel && root.appModel.jobHistoryBusy
                                    && root.appModel.deletingJobId === jobRow.model.jobId
                                    ? "running" : "idle")
                            runningText: "Đang xoá…"
                            cancellingText: "Đang hủy…"
                            enabled: Boolean(root.appModel)
                                && root.appModel.backendReady
                                && !root.appModel.jobHistoryBusy
                                && jobRow.model.state !== "cancel_requested"
                            disabledReason: "Tác vụ đang chạy sẽ được hủy an toàn rồi tự xóa."
                            feedbackText: "Đang hủy và xoá lịch sử tác vụ"
                            onClicked: root.appModel.deleteJobHistory(jobRow.model.jobId)
                        }
                    }
                    ProgressBar {
                        Layout.fillWidth: true
                        from: 0
                        to: 100
                        value: root.phasePercent(jobRow.model)
                        indeterminate: root.activeState(jobRow.model.state)
                            && jobRow.model.phaseTotal <= 0
                            && jobRow.model.total <= 0
                        visible: jobRow.model.total > 0 || jobRow.model.phaseTotal > 0
                            || indeterminate
                        Behavior on value {
                            NumberAnimation {
                                duration: 900
                                easing.type: Easing.OutCubic
                            }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Text {
                            Layout.fillWidth: true
                            text: root.progressSummary(jobRow.model)
                            color: Theme.textMuted
                            font.family: Theme.timecodeFamily
                            font.pixelSize: 10
                            wrapMode: Text.Wrap
                            maximumLineCount: 2
                            elide: Text.ElideRight
                        }
                        Text {
                            readonly property bool measuringTts:
                                jobRow.model.kind === "tts"
                                && ["warmup", "synthesis"].indexOf(
                                    jobRow.model.phase
                                ) >= 0
                                && (jobRow.model.etaMs === undefined
                                    || jobRow.model.etaMs === null)
                            visible: measuringTts || (
                                jobRow.model.etaMs !== undefined
                                && jobRow.model.etaMs !== null
                                && (jobRow.model.total > 0 || jobRow.model.phaseTotal > 0)
                            )
                            text: measuringTts
                                ? "Đang đo tốc độ…"
                                : ((jobRow.model.kind === "export"
                                        && String(jobRow.model.phase).indexOf("finalize_") === 0
                                        ? "pha xong ~" : "xong ~")
                                    + root.clockLabel("", jobRow.model.etaMs)
                                    + " · còn " + root.durationLabel(jobRow.model.etaMs))
                            color: Theme.textSecondary
                            font.family: Theme.timecodeFamily
                            font.pixelSize: 10
                        }
                    }
                }
            }
        }
    }
}
