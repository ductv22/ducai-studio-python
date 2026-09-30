---
title: a116 — Cue thoại thủ công và TTS theo loại cue
status: completed
priority: P1
effort: high
branch: main
tags: [subtitles, tts, timeline, qml, export]
created: 2026-09-30
---

# a116 — Cue thoại thủ công và TTS theo loại cue

Status: completed
Progress: 2/2 phases (100%)

## Outcome

- Cue được lồng tiếng theo `role=dialogue`, không phụ thuộc đang nằm ở S1 hay S2+.
- Kéo cue `scene_text` lên S1 chuyển cả cue nguồn và cue dịch thành thoại.
- Nút `T` cho chọn rõ `Thoại chính` hoặc `Chữ trong cảnh` và cấu hình đầy đủ định dạng ngay lúc thêm.
- Thoại thêm thủ công kế thừa hình thức và vị trí của cue thoại gần playhead để khớp phụ đề hiện có.

## Constraints

- Không migration SQLite; tiếp tục lưu trong project snapshot hiện có.
- Project cũ không đổi role nếu người dùng chưa kéo hoặc thêm cue mới.
- `role` là nguồn sự thật cho TTS; tên/lane track chỉ quyết định bố cục Timeline.
- Source/translated pair phải giữ cùng role, timing, track và định dạng.
- Giữ tương thích command `add_scene_text` và API QML cũ trong lúc mở rộng hợp đồng.

## Non-goals

- Không tự chạy lại TTS khi người dùng vừa thêm/sửa cue.
- Không đổi thuật toán TTS, OCR hoặc dịch.
- Không biến mọi cue trên S1 của project legacy thành thoại khi mở project.

## Phases

- [phase-01-cue-contract-and-editor.md](phase-01-cue-contract-and-editor.md)
- [phase-02-verification-and-release.md](phase-02-verification-and-release.md)

## Acceptance criteria

- [x] Scene cue kéo lên S1 đổi thành dialogue và xuất hiện trong đầu vào SRT/TTS.
- [x] Dialogue cue kéo xuống S2+ vẫn là dialogue và vẫn được lồng tiếng.
- [x] Manual dialogue mặc định S1; nếu S1 trùng thời gian, dùng lane trống kế tiếp nhưng vẫn là dialogue.
- [x] Manual scene text không được đưa vào TTS.
- [x] Manual dialogue kế thừa geometry, font/style, cỡ, màu, nền, padding, spacing, đậm/nghiêng và opacity từ thoại gần nhất.
- [x] Hộp `T` ở Timeline và Biên tập phụ đề dùng cùng một control và cho chỉnh toàn bộ định dạng trước khi thêm.
- [x] Scene text có chế độ mờ dần vào/ra, khớp giữa Preview và video xuất; cue ngắn tự giới hạn fade để không biến mất hoàn toàn.
- [x] Source/translated pair, undo/redo, OCR/dịch lại và reload giữ đúng role/style/track.
- [x] Focused/full pytest, Ruff, compileall, QML lint, build và packaged smoke đều đạt.
- [x] Artifact là `DUCAI-STUDIO-2.0.0a116-win64`.

