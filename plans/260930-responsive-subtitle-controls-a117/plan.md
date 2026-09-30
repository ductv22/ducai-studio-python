---
title: a117 — Điều khiển phụ đề responsive và tooltip thống nhất
status: completed
priority: P1
effort: medium
branch: main
tags: [qml, ui, subtitles, timeline, accessibility]
created: 2026-09-30
---

# a117 — Điều khiển phụ đề responsive và tooltip thống nhất

Status: completed
Progress: 2/2 phases (100%)

## Outcome

- Bộ nhập số luôn tách rõ nút giảm, giá trị và nút tăng, không chồng chữ khi cửa sổ hẹp.
- Thanh định dạng cue và hộp thêm chữ chia nhóm gọn, không tràn ngang ở kích thước cửa sổ hỗ trợ.
- Inspector Timeline hiển thị đủ năm tab và các trường không vượt khỏi panel.
- Nút định dạng khó hiểu có tooltip tiếng Việt mô tả đúng hành vi.
- Chữ trong cảnh mới mặc định Comforter Brush, chữ trắng và nền đen Mực loang đục 100%.

## Constraints

- Giữ nguyên hành vi tạo cue, định dạng, fade, chọn màu và inspector.
- Không đổi dữ liệu project, database, OCR, dịch, TTS hoặc export.
- Chỉ đổi định dạng khởi tạo của cue chữ cảnh mới; cue cũ và override thủ công giữ nguyên.
- Không xóa chức năng chỉ vì giao diện dày; chỉ bỏ nhãn hoặc khoảng trống trùng lặp.
- Giữ mọi thay đổi a100–a116 đang có trong worktree.

## Non-goals

- Không thiết kế lại toàn bộ nhận diện ứng dụng.
- Không thay đổi theme hoặc hệ font hiện hành.
- Không thêm animation hay hiệu ứng trang trí mới.

## Phases

1. [x] Sửa component dùng chung, thanh định dạng, dialog thêm cue và Inspector.
2. [x] Chạy focused/full UI tests, QML lint, build và smoke test bản đóng gói.

## Acceptance criteria

- [x] `DsSpinBox` có chiều rộng tối thiểu an toàn và tooltip mô tả cách nhập.
- [x] Không còn `DsSpinBox` 70–90 px làm đè `− / giá trị / +` ở hai giao diện trong ảnh.
- [x] Dialog thêm cue dùng nhãn Việt, footer gọn và không có vùng trống lớn không cần thiết.
- [x] Năm tab Inspector cùng nằm trong panel 272–370 px.
- [x] Các nút B, I, màu, ống hút, nền, preset và fade có tooltip chức năng.
- [x] Cue chữ cảnh mới từ OCR và nút T dùng đúng mặc định Comforter Brush/Mực loang.
- [x] Focused pytest, Ruff, compileall, QML lint, build và packaged smoke đều đạt.
- [x] Artifact là `DUCAI-STUDIO-2.0.0a117-win64`.
