# Phase 02 — Xác minh và phát hành

## Steps

1. Chạy focused tests cho domain, subtitle command, ViewModel, Preview và export.
2. Raster probe ASS fixture có chữ Việt dọc trên nền mực.
3. Chạy Ruff, compileall, full pytest và QML lint.
4. Cập nhật hướng dẫn người dùng, kiến trúc, notice và version.
5. Build sạch a115, kiểm manifest/hash và packaged smoke.

## Success criteria

- Preview/Export không đổi cue nền phẳng.
- Ink cue có vệt mực bất quy tắc dưới chữ và đúng thời gian cue.
- Không có missing glyph trong bộ ký tự tiếng Việt.
- Không tăng số FFmpeg input theo số ink cue.
- Không có lỗi lint, test, load QML hoặc startup bản đóng gói.
