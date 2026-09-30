---
title: Hợp đồng cue và trình thêm chữ
status: completed
---

# Phase 01 — Hợp đồng cue và trình thêm chữ

## Context

- `dialogue_cues()` đã là bộ lọc dùng chung cho SRT/TTS.
- `move_scene_texts()` hiện chỉ đổi timing/lane, không đổi role khi vào S1.
- `add_scene_text()` luôn tạo `scene_text` và chỉ nhận một phần typography.
- Hai màn hình đang duy trì hai dialog `T` gần giống nhau.

## Requirements

- [x] Tổng quát hóa lệnh thêm cue để nhận role, track và toàn bộ typography.
- [x] Tự tìm cue dialogue gần playhead và dùng làm style/geometry mặc định cho thoại thủ công.
- [x] Tổng quát hóa kéo/retime subtitle cue; chỉ scene→S1 mới được promote, không tự hạ dialogue khi rời S1.
- [x] Rebuild subtitle tracks trong cùng revision và giữ source/translated đồng bộ.
- [x] Tạo một dialog QML dùng chung cho Timeline và Subtitle Editor.
- [x] Với thoại, xác nhận dialog thêm ngay ở geometry kế thừa; với chữ cảnh, người dùng tiếp tục kéo vùng trên Preview.
- [x] Thêm tùy chọn fade vào/ra cho scene text, mặc định 300 ms mỗi đầu; Preview và ASS export dùng cùng giới hạn theo duration.
- [x] Giữ cả manual dialogue và manual scene text qua OCR/dịch lại mà không gửi lại provider.

## Files

- `src/ducai_studio/application/subtitle_commands.py`
- `src/ducai_studio/job_manager/service.py`
- `src/ducai_studio/ui/viewmodels/application_view_model.py`
- `src/ducai_studio/ui/viewmodels/project_job_models.py`
- `src/ducai_studio/ui/qml/controls/ManualSubtitleCueDialog.qml`
- `src/ducai_studio/ui/qml/screens/TimelineScreen.qml`
- `src/ducai_studio/ui/qml/screens/SubtitleEditorScreen.qml`
- `tests/subtitles/test_subtitle_commands.py`
- `tests/unit/test_ui_view_models.py`
- `tests/integration/test_tts_job_service.py`

## Validation

- [x] Domain tests cho add/move/role/style và lane collision.
- [x] ViewModel/IPC tests cho payload đầy đủ và default lookup.
- [x] TTS/SRT integration test xác nhận lọc theo role thay vì lane.
- [x] Resource/QML lint xác nhận cả hai màn hình dùng dialog chung.

## Risks and rollback

- Role dialogue trên S2 là chủ ý; tuyệt đối không suy lại role chỉ từ lane khi reload.
- Nếu lane S1 đang bận, manual dialogue được đặt ở lane trống thấp nhất và UI phải báo rõ vẫn được lồng tiếng.
- Giữ alias command cũ để rollback UI độc lập mà không làm project mới không đọc được.

