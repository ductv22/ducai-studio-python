# Phase 01 — Contract và rendering

## Context

- Ảnh tham chiếu dùng chữ trắng dọc trên vệt mực tàu đen loang.
- a114 đã có typography theo cue, panel geometry, padding và Preview/ASS scene text.

## Requirements

- Thêm `background_style` tùy chọn, mặc định `solid`, hỗ trợ `ink_splash`,
  `ink_wash`, `ink_cloud` và `brush_stroke`.
- Thêm typography bền vững theo cue: spacing, bold, italic và hai opacity.
- Thêm `calligraphy_ink` cùng bốn biến thể brush OFL hỗ trợ tiếng Việt:
  Comforter Brush, Water Brush, Splash và Hurricane.
- Toolbar có kiểu nền và preset mực tàu một chạm.
- Preview vẽ silhouette vector nhẹ, không tranh chuột với cue; các đầu cọ dùng
  kích thước theo bề dày thay vì bị kéo giãn theo chiều dài.
- Export vẽ đúng cùng silhouette bằng ASS vector trước lớp chữ, không tạo input
  ảnh theo cue.
- Toolbar luôn hiển thị, giữ cue cuối và lấy màu từ pixel Preview bằng frame grab nội bộ.
- Scene timing preview snap vào playhead với hysteresis, không gọi backend trong lúc kéo.

## Files

- `src/ducai_studio/domain/project.py`
- `src/ducai_studio/domain/effects.py`
- `src/ducai_studio/application/subtitle_commands.py`
- `src/ducai_studio/application/export_pipeline.py`
- `src/ducai_studio/providers/export/ffmpeg.py`
- `src/ducai_studio/infrastructure/project_repository.py`
- `src/ducai_studio/job_manager/service.py`
- `src/ducai_studio/ui/viewmodels/`
- `src/ducai_studio/ui/qml/controls/`
- `src/ducai_studio/ui/assets/fonts/`

## Validation

- Legacy decode, validation, command persist và optimistic patch.
- Preview resource contract, bốn biến thể ngang/dọc, tỷ lệ rất ngắn/rất dài và
  ASS ink event/painter order.
- Timeline contentY không đổi khi selection/model refresh; snap có thể kéo xuyên qua.
- Font family, license và Vietnamese glyph coverage.

## Risks and rollback

- Qt/libass có metric khác nhau: giữ helper fit riêng và test bounding geometry.
- Nếu vector export lỗi, rollback riêng `ink_splash`; cue `solid` không bị ảnh hưởng.
