---
title: Phục hồi DUCAI STUDIO DATA sau xóa nhầm
date: 2026-09-02
summary: Dựng lại runtime OCR/TTS và model dùng chung; toàn bộ diagnostics đã ready.
---

# Phục hồi DUCAI STUDIO DATA sau xóa nhầm

## What happened
Các thư mục runtime/model trong DUCAI_STUDIO_DATA bị xóa một phần. Metadata package còn sót làm VieNeu import nhầm môi trường hỏng; Paddle OCR runtime bị mất hoàn toàn.

## Recovery
Phục hồi Piper 1.6.0 và giọng vi_VN-vais1000-medium; sửa VieNeu 3.3.0 cùng torch 2.8.0+cu128 và tải lại model Turbo/tokenizer; dựng PaddleOCR 3.7.0 + PaddlePaddle GPU 3.2.2 + RapidOCR trên Python 3.11; tạo lại profile directory Gemini Web. Giữ nguyên vieneu_profiles và voice_cue_cache.

## Verification
Runtime diagnostics trả overall=ready. Piper và VieNeu đều tổng hợp WAV thử thành công. PaddleOCR nhận RTX 3060 và khởi tạo model Fast từ cache. Mọi WAV thử đã xóa và không để lại process nền.

## Next steps
Người dùng phải đăng nhập lại Gemini Web vì cookie đã xóa vĩnh viễn. Không xóa file riêng lẻ bên trong DUCAI_STUDIO_DATA; chỉ dọn các cache được xác định rõ.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.
