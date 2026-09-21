# Tốc độ giọng gốc và khớp cue chính xác — a58

Status: completed

## Outcome

Tách tốc độ đọc toàn bộ voice khỏi cơ chế tăng riêng cue dài. Job mới từ UI
dùng tốc độ gốc 1,30×; VieNeu luôn inference ở 1,00× rồi hậu xử lý WAV giữ
cao độ. Kịp theo câu đo duration WAV thực tế và chỉ nâng tổng tốc độ của cue
dài đến giới hạn người dùng chọn, tối đa 3,00×.

Timeline có mục **Khớp giọng** riêng dùng chế độ chỉ tăng audio: mỗi WAV cue
được đối chiếu với chính slot `start_us/end_us` của SRT nguồn, cue dài mới tăng
tốc và cue vừa/ngắn giữ 1,00×. Track D1 hiển thị các đoạn cue đã áp dụng cùng
tốc độ thực tế để người dùng kiểm tra và seek chính xác.

## Constraints

- Cache WAV nền ở tốc độ 1,00× là nguồn chuẩn và không phụ thuộc chính sách
  tốc độ; mọi biến thể luôn được tạo trực tiếp từ WAV nền.
- Job cũ thiếu `voice_read_speed` tiếp tục ở 1,00×; Piper giữ `engine_speed`.
- Cue vượt slot không quá 50 ms được chấp nhận; vượt hơn phải có evidence và
  không bị cắt, bỏ hoặc chồng âm.
- Đổi tốc độ VieNeu chỉ hậu xử lý cache, không gọi model lại.
- Luật dịch theo thời lượng là ngân sách mềm, không được cắt hoặc loại câu.
- Giữ nguyên release a57; chỉ xuất a58 sau khi các gate đạt.

## Non-goals

- Không thay đổi timestamp nguồn hoặc cộng dồn time-stretch.
- Không thay thế workflow Đồng bộ sau lồng tiếng đã có.
- Không migration SQLite.

## Acceptance criteria

- UI có tốc độ giọng gốc 1,10–3,00× (nội bộ cho phép 1,00×), mặc định 1,30×,
  và giới hạn tổng cue dài độc lập, mặc định `max(base, 1,50×)`.
- VieNeu luôn nhận 1,00×; Piper tiếp tục nhận `engine_speed` cũ.
- Base 1,30×/1,50× làm WAV ngắn hơn bằng hậu xử lý giữ cao độ; 3,00× dùng
  chuỗi `atempo` hợp lệ.
- Cache variant khóa theo SHA WAV nền, tổng speed, processor, sample rate và QA;
  cache 1,30×/1,50× không va chạm và cache legacy là 1,00×.
- Kịp theo câu áp dụng ngay trong D1, dùng tốc độ tổng cuối cùng và ngưỡng 50 ms.
- QA timing lưu slot, duration nền/sau base/cuối, tốc độ cần/đã áp dụng và overflow.
- Tự rút gọn chỉ gửi cue còn lỗi sau hậu xử lý tốc độ.
- Prompt dịch chứa start/end, slot duration và soft character budget.
- Timeline hiển thị ảo hóa từng đoạn D1 theo cue, dùng timestamp nguồn ở độ
  phân giải microsecond; bấm đoạn sẽ seek đúng cue. Mục Khớp giọng tái dùng
  `speed_audio`/cache hiện có và không gọi VieNeu.
- Focused/full tests, Ruff, compileall, QML lint và build release a58 PASS.

## Touchpoints

- `src/ducai_studio/application/voice_pipeline.py`
- `src/ducai_studio/providers/audio_processing.py`
- `src/ducai_studio/workers/tts_worker.py`
- `src/ducai_studio/job_manager/service.py`
- `src/ducai_studio/ui/viewmodels/application_view_model.py`
- `src/ducai_studio/ui/qml/screens/VoiceStudioScreen.qml`
- `src/ducai_studio/application/translation_pipeline.py`
- Tests, docs and release metadata.

## Validation

1. Focused unit/integration tests for fit, worker cache/provider speed, service,
   translation prompt and QML contract.
2. Full pytest, Ruff, compileall and QML lint.
3. Build a58, SHA manifest and smoke launch with `Start-Process`.

## Completion evidence

- Focused conform/service/Timeline suite: 18 tests PASS, including a drifted
  Vietnamese timing fixture that still resolves the exact untranslated source
  slots `0–1,000,000 µs` and `1,200,000–2,200,000 µs`.
- Full pytest PASS (one environment-gated skip); Ruff, compileall and QML lint
  PASS.
- Release `DUCAI-STUDIO-2.0.0a58-win64` built successfully. Manifest version is
  `2.0.0a58`; all 2,840 declared files exist and match SHA-256.
- An initial a58 package smoke launched with `Start-Process`, started its Job
  Manager child, rendered the Timeline/Khớp UI, exited with code 0 and left no
  child process. After the final source-timestamp hardening rebuild, another
  live launch was intentionally not attempted because the user's a57 instance
  and OCR worker were active; the final wheel content and complete manifest were
  verified offline without touching that running job.
