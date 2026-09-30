---
title: Xác minh và phát hành
status: completed
---

# Phase 02 — Xác minh và phát hành

## Requirements

- [x] Bump release `2.0.0a116` và cập nhật tài liệu người dùng/kiến trúc.
- [x] Kiểm tra thao tác thêm thoại/chữ cảnh, kéo S1↔S2, sửa định dạng và tạo TTS.
- [x] Không làm thay đổi cue/project legacy nếu chưa có thao tác người dùng.

## Validation

- [x] Focused pytest cho subtitle command, ViewModel, service và TTS.
- [x] Full pytest, Ruff, compileall, QML lint và `git diff --check`.
- [x] Clean release build, kiểm đủ 2.888 hash manifest và packaged EXE smoke.

## Risks and rollback

- Worktree đang chứa toàn bộ a100–a115; không reset hoặc tách bỏ thay đổi đang có.
- Nếu build lỗi do môi trường ngoài code, giữ source/test đã xác minh và ghi rõ bằng chứng.

