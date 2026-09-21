---
title: DUCAI STUDIO a96 portable delivery
date: 2026-09-05
summary: "UI dịch thuật được tối giản và bản portable offline sạch, relocatable, đã smoke/test/archive."
---

# DUCAI STUDIO a96 portable delivery

## Điều đã xảy ra

Người dùng cần bản DUCAI STUDIO 2.0.0a96 portable để gửi cho máy khác, đồng thời ẩn Web nổi, đăng nhập/khách, tên Paddle/Quality/Rapid, note và chi tiết kỹ thuật khỏi Dịch thuật/Trích xuất. Kiểm tra relocation phát hiện launcher uv trong Piper/VieNeu đóng cứng đường dẫn Python của máy build; việc chỉ sửa `pyvenv.cfg` là chưa đủ.

## Thay đổi và quyết định

- Thay launcher/DLL của ba managed venv bằng Python home đóng gói; loại console entrypoint, activation script và pyc phụ thuộc máy build.
- Giữ marker `__DUCAI_PORTABLE_PYTHON_HOME__`; app sửa marker sau khi người nhận giải nén vào thư mục có quyền ghi.
- Manifest hash cả engine host, Paddle/Piper/VieNeu runtime và hai Python home.
- Không đóng gói project, cache voice, cookie, credential hay VieNeu clone profile.
- RAR5 solid mức tối đa được chọn để giảm dung lượng bàn giao.

## Bằng chứng

- FFmpeg/ffprobe chạy; Piper và VieNeu tạo WAV; Paddle GPU suy luận bằng model cache offline.
- 690 test qua, 1 test tùy chọn bỏ qua; Ruff, compileall, QML lint qua.
- Package sạch: 59.363 file, 14.496.244.807 byte, marker còn nguyên, không có đường dẫn máy build.
- Archive: 4.261.879.502 byte, RAR test qua, SHA-256 `84030b6a5717d3ce2322f2e4220aaa534984549442fb4e2291ea91e7a9a97929`.

## Lưu ý

Người nhận phải giải nén toàn bộ vào thư mục có quyền ghi. VieNeu cần tự tạo/chọn profile clone; API và Gemini Web dùng credential/profile riêng của người nhận.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
