# a115 — Nền mực tàu và chữ thư pháp cho scene text

Status: completed

## Outcome

- Scene-text cue có bốn preset nền mực tàu bất quy tắc và nhiều font brush hỗ trợ đủ tiếng Việt.
- Preview và Export thể hiện cùng vị trí, màu, padding và silhouette.
- Timeline giữ nguyên vị trí cuộn khi đổi cue; bảng typography luôn chiếm chỗ cố định.
- Cue kéo gần playhead tự hút mép nhưng có thể kéo xuyên qua bằng một chuyển động tiếp tục.
- Typography theo cue có letter spacing, đậm/nghiêng, opacity chữ/nền và ống hút màu.
- Space hoặc click vùng video trống toggle phát/tạm dừng mà không tranh gesture chỉnh cue/effect.
- Cue/project cũ không thay đổi hình ảnh nếu người dùng chưa chọn preset.

## Constraints

- Không sao chép nguyên artwork tham chiếu; dùng silhouette mới cùng ngôn ngữ thị giác.
- Nền là vector co giãn theo painted bounds; đầu cọ và độ gồ ghề không bị kéo méo khi chữ rất ngắn hoặc rất dài.
- Không thêm raster/decoder hoặc FFmpeg input cho từng cue.
- Mọi thay đổi theo cue, undo được và cập nhật lạc quan như bảng typography hiện tại.
- Font phát hành phải có giấy phép phân phối và đủ glyph tiếng Việt.
- Không nhúng VNI-ThuPhap, HL Thu Phap, UVN-HanNom hoặc SVN-Chu Nom khi không
  có giấy phép phân phối rõ ràng; dùng các họ OFL có nét tương đương.

## Non-goals

- Không thay nền mặc định của S1 hoặc mọi S2 hiện có.
- Không tạo hiệu ứng mực động.
- Không thay đổi OCR, dịch hoặc timing cue.
- Không tự cuộn track đến cue vừa chọn; người dùng tự cuộn bằng wheel/scrollbar.

## Phases

- [phase-01-contract-and-rendering.md](phase-01-contract-and-rendering.md)
- [phase-02-verification-and-release.md](phase-02-verification-and-release.md)

## Acceptance criteria

- Một click áp preset mực tàu cho cue đã chọn; chọn nhiều cue vẫn là một revision.
- Có thể chọn riêng `Mực loang`, `Mực đại mặc`, `Mực tụ` hoặc `Nét cọ`; mọi kiểu hoạt động với chữ ngang và dọc.
- Nền mực ôm painted bounds, không là hình chữ nhật và không tràn frame.
- `Nền ngang` và `Nền dọc` tiếp tục chỉnh kích thước nền của mọi preset mà không làm biến dạng hai đầu cọ.
- Font brush hiển thị đúng toàn bộ dấu tiếng Việt ở Preview và libass.
- Bộ chọn chữ bổ sung Comforter Brush, Water Brush, Splash và Hurricane; tên
  thật được hiển thị để không khiến người dùng hiểu nhầm đó là bốn font thương mại trong ảnh.
- Nền được vẽ dưới chữ, màu nền/màu chữ/padding vẫn chỉnh độc lập.
- Legacy snapshot mặc định `solid`; nền phẳng hiện tại không đổi.
- Click cue/blank timeline không làm track nhảy dọc hoặc làm toolbar đổi chiều cao.
- Drag scene cue snap start/end vào playhead trong vùng 8 px/80–500 ms và thoát snap khi kéo tiếp.
- Letter spacing, bold, italic, text/background opacity và eyedropper khớp Preview/Export.
- Playback toggle bằng Space/click không kích hoạt khi đang nhập chữ hoặc thao tác overlay/OCR.
- Focused/full pytest, Ruff, compileall, QML lint, build và packaged smoke đều đạt.
- Artifact là `DUCAI-STUDIO-2.0.0a115-win64`.

## Completion evidence

- Bốn font brush OFL đã được đóng gói bằng tên thật: Comforter Brush, Water
  Brush, Splash và Hurricane; QRawFont xác nhận đầy đủ dấu Việt dùng trong mẫu.
- Bốn nền vector chạy ở cả ngang/dọc trong QML Canvas và ASS/libass; cue cũ vẫn
  giải mã thành `solid`.
- Smoke giao diện 1280×720 xác nhận toolbar ba hàng không đổi chiều cao và mọi
  nút nền vẫn nằm trong vùng Timeline hẹp.
- Focused pytest: 301 passed. Full pytest: 909 passed, 1 skipped. Ruff,
  compileall, QML lint và `git diff --check`: passed.
- Clean build và packaged EXE smoke: passed. Manifest `2.0.0a115` có 2.887 hash,
  xác minh toàn bộ không thiếu hoặc lệch hash.
- Artifact: `E:\Codex\2026-07-21\create-a-scheduled-task-called-weekday\outputs\DUCAI-STUDIO-2.0.0a115-win64`.
