# Hướng dẫn DUCAI STUDIO 2

## Trước khi bắt đầu

Mở **Cài đặt → Chẩn đoán runtime** để kiểm tra bộ máy cho chức năng cần dùng.
Chỉ cắt/sửa và xuất video thì không cần OCR, dịch, model giọng hay phụ đề;
nhập video, sửa trong Timeline rồi mở **Xuất video**. Xuất cần FFmpeg/ffprobe;
PaddleOCR, transport dịch và Piper/VieNeu chỉ cần cho bước AI tương ứng.
NVENC có thể ở trạng thái không khả dụng; tool sẽ dùng CPU khi backend là `auto`.

Chẩn đoán Paddle không chỉ kiểm tra đã cài package: nó thử phép toán CUDA và
cuDNN nhỏ, ghi nhận GPU, phiên bản CUDA/cuDNN và compute capability. `degraded`
nghĩa là chưa xác minh được GPU hoặc CUDA bị lỗi, không phải GPU đã sẵn sàng.
Phép thử cơ bản này không thay thế một lượt OCR thực tế trên model đã chọn.
Đặc biệt khi dùng RTX 50, cần runtime tương thích kiến trúc GPU; không kết luận
CUDA hoạt động chỉ từ tên package hoặc phần trăm GPU trong overlay.
RTX 50 (compute capability 12.x) trên Windows cần bản Paddle CUDA 12.9 trở lên;
tool phát hiện runtime CUDA cũ trước khi nạp model và giải thích rõ lỗi thay vì
chỉ ghi `RuntimeError`. Phép chẩn đoán có giới hạn 45 giây để tránh báo thiếu
CUDA khi runtime mới đang nạp thư viện lần đầu. Driver NVIDIA vẫn phải phù hợp
với runtime; không cần tự cài Python/CUDA Toolkit vào máy theo phỏng đoán.
Nếu phép thử không trả kết quả, chẩn đoán phân biệt quá hạn, không chạy được
tiến trình, tiến trình thoát lỗi (kèm mã thoát) và kết quả không đọc được;
không suy đoán tất cả là lỗi driver. Khi đặt `DUCAI_PADDLE_PYTHON` trỏ tới một
Python có thật, cả OCR lẫn chẩn đoán đều ưu tiên runtime đó hơn đường dẫn cũ
trong cài đặt, nhưng không ghi đè lựa chọn đã lưu. Bỏ biến này để trở lại cấu
hình cũ. Đây là lựa chọn theo từng tiến trình, không cài CUDA vào Windows.

Với gói thử riêng RTX 50 a153, giải nén vào đường dẫn ngắn không dấu, chẳng hạn
`E:\DUCAI-RTX50`. Paddle có thể không mở được model ở thư mục có dấu dù phép
thử CUDA vẫn qua; đường dẫn DLL quá dài cũng có thể bị Windows từ chối. Launcher
`Mo-ban-thu-RTX50.vbs` của gói thử kiểm tra các điều kiện này trước khi mở tool.
Khoảng trắng dùng được. Gói thử dùng trạng thái dự án riêng, không kèm
TTS/Whisper và chưa thay thế bản portable/cập nhật chung.

Model, cache và giọng không nằm trong EXE. Chúng ở `DUCAI_STUDIO_DATA`; có thể
đổi gốc bằng biến `DUCAI_STUDIO_DATA_ROOT` mà không build lại GUI.

Bản phát hành Windows nằm trong thư mục `outputs/DUCAI-STUDIO-<version>-win64`.
Mở `DucaiStudio2.exe` trực tiếp; bản production dùng GUI subsystem nên không mở
thêm cửa sổ CMD.
Các worker và bộ máy OCR/TTS chạy nền cũng được đặt chế độ không tạo/nhận
console Windows; CMD/PowerShell phụ không cần mở thành cửa sổ riêng. Log lỗi và
khả năng tạm dừng/hủy tác vụ vẫn giữ nguyên; cửa sổ chính và hộp chọn file vẫn hiện.
Font đi kèm được dùng nguyên bản cả khi thư mục giải nén có dấu tiếng Việt.
Nếu backend font của Qt không mở được đường dẫn, tool nạp chính dữ liệu font
đó; font hỏng vẫn báo lỗi, không âm thầm đổi font trong Preview hay xuất video.

## Kích hoạt offline từ a139

Từ a140, key cấp mới mặc định là `DUCAI-L2.` gồm 128 ký tự, là giấy phép vĩnh
viễn gắn với một máy Windows. Chủ tool cũng có thể cấp `DUCAI-L3.` có thời hạn
theo số giờ để thành viên dùng thử; hết hạn, tool khóa các chức năng xử lý nhưng
không xóa dự án hay media. Key dài `DUCAI-L1.` đã cấp cho a139 vẫn được chấp
nhận, nên không cần kích hoạt lại khi nâng cấp; a138 và các bản cũ không bị thay
đổi hồi tố. Khi chưa kích hoạt, mở **Cài đặt → Kích
hoạt**, bấm sao chép mã máy `DUCAI-M1-…` và gửi nguyên mã đó cho người cấp key.
Không cần gửi MachineGuid, UUID, serial ổ đĩa hay dữ liệu project.

Khi nhận key, dán chuỗi vào ô kích hoạt hoặc bấm **Nhập file key** để chọn file
`.ducai-license`. Tool chỉ mở khóa sau khi Job Manager xác minh chữ ký và đúng
máy; key sai máy hoặc bị sửa sẽ bị từ chối bằng thông báo chung, và key lỗi không
ghi đè một kích hoạt hợp lệ. Màn Cài đặt, chẩn đoán, cập nhật,
tắt ứng dụng và thao tác dừng/dọn công việc vẫn dùng được để phục hồi khi đang
khóa.

Cài lại Windows, thay mainboard hoặc thay ổ hệ thống trên một số máy có thể làm
mã máy đổi và cần cấp lại. Đây là cấp phép offline: không có thu hồi qua mạng.
Key có thời hạn dựa vào đồng hồ UTC cục bộ của máy; đổi đồng hồ có thể làm sai
thời điểm hết hạn, nên cơ chế này không thay thế server-side revocation và không
có cam kết chống sửa binary.

## Bản a156: OCR tự tăng tiến trình khi bộ nhớ trống trở lại

Nếu OCR phải chạy một tiến trình do thiếu RAM/VRAM, tool kiểm tra lại bộ nhớ
khoảng mỗi 5 giây tại lượt nhận chữ kế tiếp. Khi cả RAM và VRAM đều còn ít nhất
4 GiB trống, tool tự nạp thêm hai predictor để chạy ba tiến trình, không cần
tạm dừng, mở lại ứng dụng hoặc quét lại phần đã xong. Nhãn tiến độ chỉ chuyển
sang **3 tiến trình nhận chữ** sau khi các model mới nạp thành công.

Nếu nạp thêm thất bại vì tài nguyên hoặc kết nối, tiến trình đang hoạt động
vẫn tiếp tục; tool giãn lần thử lại từ 60 đến tối đa 300 giây để tránh nạp/bỏ
model liên tục. Thiếu thông tin bộ nhớ không được coi là đủ bộ nhớ. RAM không
thay thế được VRAM; CPU hoặc thiết lập chủ động một tiến trình không tự tăng.
Các lỗi model/driver và lỗi kết nối lặp lại vẫn có cơ chế dự phòng an toàn,
không bị ép thử mãi. Model, mẫu quét, chữ nhận được và QA không bị giảm để tăng tốc.

## Bản a155: khôi phục OCR chạy song song

Nếu một predictor bị ngắt kết nối hoặc quá thời gian chờ, tool thử khôi phục
nhóm 3 tiến trình một lần, giữ nguyên mẫu quét, model và QA. Nếu lỗi lặp lại,
tool tiếp tục bằng một tiến trình và hiển thị lý do trong tiến độ. Trường hợp
thiếu bộ nhớ sẽ được kiểm tra lại tự động từ a156 như mô tả trên.
Đây vẫn là OCR trên GPU nếu nhãn thiết bị là GPU, không
phải tự động chuyển sang CPU. Nên kết thúc tác vụ AI khác dùng chung GPU khi
cần đo tốc độ; GPU có ít VRAM trống sẽ không được ép chạy 3 tiến trình.

Thời gian còn lại là ước lượng của giai đoạn đang chạy, không phải thời gian
đã chạy hoặc cam kết thời điểm hoàn thành cả OCR/Quality/QA. Tạm dừng và mở lại
từ checkpoint không cần quét lại từ đầu; giữ nguyên video và thiết lập lượt đó.

## Bản a154: phản hồi và dự án lớn

Bản này tối ưu tra cứu thời gian sau khớp giọng, sửa cue, Preview ẩn và việc
tái sử dụng ảnh/âm thanh Timeline đã có cache. Khung hình chưa có cache vẫn cần
được tạo lần đầu. Chẩn đoán trong Cài đặt chạy riêng để không chặn hàng đợi
dừng/hủy và tiến độ tác vụ; hãy chờ báo cáo hiện tại kết thúc trước khi bấm lại.
Ảnh cuối clip rất ngắn được xử lý thêm khi FFmpeg không trả về khung hình.
Bản này cũng sửa lỗi trình cập nhật nạp nhầm module giữa hai phiên bản PowerShell.

Không đổi model, số mẫu quét, QA OCR, nội dung dịch, chất lượng giọng hoặc
thiết lập render để đổi lấy tốc độ. Mức cải thiện phụ thuộc dự án; không có
cam kết rút ngắn mọi lượt nhận chữ hay độ trễ của dịch vụ API. Bản ứng dụng
không tự thay CUDA, driver, model hoặc dữ liệu dự án.

## Cập nhật ứng dụng từ GitHub

Từ bản `2.0.0a118`, mở **Cài đặt → Cập nhật DUCAI STUDIO** để kiểm tra thủ công.
Kênh **Ổn định** chỉ nhận bản chính thức; kênh **Thử nghiệm** nhận cả alpha, beta
và RC. Tool không tự kiểm tra, tự tải hoặc tự cài khi chưa có thao tác của người
dùng. Tiến độ tải là số byte thật, không phải bộ đếm ước lượng.

Mỗi bản cập nhật gồm gói ứng dụng nhị phân, manifest SHA-256 và chữ ký Ed25519.
Tool chỉ cho cài sau khi chữ ký, tên file, kích thước và toàn bộ hash khớp. Khi
bấm **Cài đặt**, mọi OCR, dịch, TTS, export và Multi video phải kết thúc trước;
nếu còn tác vụ, gói đã tải được giữ lại nhưng người dùng vẫn phải xác nhận lần
nữa trước khi khởi động lại. Updater không dừng cưỡng bức công việc đang chạy.

Trình cài chỉ thay các file ứng dụng có tên trong manifest đã ký. Project, media,
credential, QSettings, model, cache, runtime ngoài và toàn bộ
`DUCAI_STUDIO_DATA` không thuộc gói cập nhật. Sau khi mở bản mới, tool chỉ xác
nhận thành công khi đúng PID, phiên cài, version, thư mục cài và Job Manager đã
sẵn sàng; nếu không, helper khôi phục file cũ và mở lại bản cũ. Gói tải lớn được
dọn sau khi cài hoặc rollback.

Giới hạn bootstrap: a117 và các bản cũ chưa có updater nên phải nâng cấp thủ
công đúng một lần. Một số bản đóng gói, gồm a148–a150, có lỗi xác định EXE
khi bấm cài và báo **Tự cập nhật chỉ khả dụng trong bản DUCAI STUDIO đã đóng
gói**. Gói đã tải không hỏng, nhưng nút đó không thể tự nhận bản vá của chính
nó. a151 sửa lỗi này; những máy gặp lỗi cần nâng cấp phần ứng dụng thủ công
một lần, sau đó dùng lại cập nhật trong tool.

Để nâng cấp a148–a150 mà không tải lại model/runtime: kết thúc các tác vụ, lưu
dự án và đóng tool; tải `DUCAI-STUDIO-2.0.0a151-win64-update.zip` từ release
chính thức, giải nén vào thư mục tạm, rồi chép **toàn bộ nội dung đã giải nén**
vào thư mục đang có `DucaiStudio2.exe`, chọn thay thế file ứng dụng. Có thể
giữ một bản sao thư mục ứng dụng trước khi chép để quay lại nếu việc chép bị
ngắt. Không xóa thư mục cũ, `DUCAI_STUDIO_DATA` hoặc dữ liệu AppData. Mở lại
EXE ở cùng đường dẫn và kiểm tra version a151. Gói update chỉ có phần ứng dụng,
không phải gói dùng độc lập cho máy chưa cài tool; project, key, cấu hình và
runtime/model đã có vẫn được giữ lại.

Bản
portable dùng vùng staging trong LocalAppData để không tự ghi gói tải vào cây
ứng dụng đang được thay; thư mục dữ liệu portable cạnh EXE vẫn được giữ nguyên.

## Quy trình sản xuất an toàn

1. **Nhập video:** video vào V1, âm thanh gốc tách logic sang A1; blur và logo
   được gieo trên toàn timeline theo preset đã lưu.
2. **Trích xuất:** chọn rõ **OCR khung hình** hoặc **Whisper âm thanh**. OCR dùng
   **Tốc độ quét** hiện có. Whisper dùng audio tiếng Trung, model `large-v3`, ưu
   tiên CUDA và tự chạy lại bằng CPU khi CUDA lỗi. Cài Whisper một lần bằng
   `scripts/setup-whisper-runtime.ps1`; tool không tự tải model ngầm. Nút chính,
   tiến độ, tạm dừng, tiếp tục từ checkpoint và hủy dùng chung một luồng điều khiển.
   **OCR chữ trong cảnh** mặc định bật. Tắt mục này khi chỉ cần thoại S1 để
   bỏ các lượt dò/đọc lại chữ cảnh; việc bật/tắt không xóa S2… đã có. Thiết lập
   được đóng băng khi tạo tác vụ, kể cả Multi; tiếp tục tác vụ cũ vẫn dùng
   thiết lập của tác vụ đó, muốn đổi phải chạy lượt trích xuất mới.
   Trên GPU đủ tài nguyên, OCR chạy đồng thời **3 tiến trình nhận chữ**. Model,
   số frame, vùng quét và QA giữ nguyên; không cần bật chế độ quét nhanh hơn.
   Máy thiếu RAM/VRAM hoặc lỗi tiến trình sẽ quay về nhận chữ đơn luồng, không
   bỏ frame hay cue. CPU và chế độ tương thích vẫn đơn luồng. Thời gian nạp
   model có thể tăng; lợi ích tốc độ tùy GPU, độ phân giải và nội dung video.
   Từ a150, bộ đọc giữ lại frame đã giải mã trong cache RAM riêng cho video đang
   mở, tối đa 256 MiB và giảm/tắt khi RAM trống thấp. Các lượt quét bổ sung có
   thể dùng lại đúng frame thay vì giải mã lại; không giảm mẫu, đổi pixel, model,
   cue hay QA. Đóng/chuyển video xóa cache RAM này, không xóa cache trên ổ đĩa.
   Tiến độ quét ghi rõ **GPU · 3 tiến trình nhận chữ**, GPU đơn tiến trình hoặc
   CPU. Nếu Paddle lỗi rồi chuyển OCR tương thích, cảnh báo **CPU · OCR tương
   thích · Paddle lỗi…** tiếp tục hiển thị, không chỉ lóe lên một lần.
   Lỗi driver, kernel không hỗ trợ GPU, thiếu thư viện CUDA/cuDNN và hết VRAM
   có mã nguyên nhân riêng, không lưu nguyên thông báo thô chứa dữ liệu cá nhân.
   CPU tương thích không phải bằng chứng GPU đã hoạt động; chạy Chẩn đoán runtime
   và gửi kết quả khi OCR chậm bất thường.
   Giải mã/crop, ghép bằng chứng/QA, vẽ phụ đề và xử lý WAV vẫn có phần chạy CPU;
   NVENC/VieNeu CUDA không có nghĩa toàn bộ workflow chạy trên GPU.
   Từ a146, sau khi quét xong, thẻ hoạt động ghi rõ đang đọc bằng chứng, ghép
   thoại/chữ cảnh, kiểm tra độ phủ hoặc công bố SRT. Các bộ đếm phản ánh công
   việc thực; pha này không ngoại suy ETA từ tốc độ nhận chữ. Tạm dừng/hủy vẫn
   giữ checkpoint; tiếp tục pha hoàn thiện không phải quét lại toàn bộ video.
   Từ a149, nếu container có phần đuôi âm thanh dài hơn số frame video,
   OCR chỉ gửi các mốc còn đọc được tới decoder. Không đổi vùng bạn đã chọn,
   không nhân bản frame cuối để tạo cue và không rút ngắn Timeline; lỗi frame
   hỏng thật vẫn được báo. Cách xử lý áp dụng cả khi video đã cắt/đổi tốc độ.
3. **QA SRT Trung:** `_ZH_FINAL.srt` chỉ xuất khi PASS, không cue rỗng, không
   overlap và ít nhất 95% timestamp có chữ được cue cuối che phủ đúng nội dung.
   File `_ZH_FINAL_QA.json` phải khớp job, fingerprint và SHA-256 của SRT;
   REVIEW được giữ cùng QA bất biến, không thể đổi tên để lách gate.
   Khi QA dừng ở **Cần rà soát**, bản cần duyệt được giữ để kiểm tra. **Tiếp tục
   OCR từ cache** tiếp tục checkpoint hợp lệ của tác vụ; không phải nút tự sửa
   mọi lỗi OCR. Muốn thay vùng/quy tắc quét hoặc thiết lập chữ cảnh, hãy chạy
   lượt trích xuất mới; fingerprint khác không được dùng nguyên kết quả cũ.
   Kết quả Whisper dùng QA riêng theo hash/fingerprint/cue count và không bị ép
   qua coverage khung hình. Hai phương thức có fingerprint/cache tách biệt.
   OCR khung hình tự loại cụm watermark Bilibili và tên kênh/số đi kèm như
   `4K超清漫剧|3473`, `呼啦呼啦2313` hoặc `小李看漫剧` trước khi tạo SRT. Bộ lọc
   cần đồng thời có neo logo, vị trí gần nhau hoặc bằng chứng lặp xuyên video;
   câu thoại thật có số hay chữ `漫剧` không bị loại chỉ vì trùng từ. Khi cùng
   một cụm ở mép hình có box hợp lệ xuất hiện ở ít nhất ba frame Fast và trải
   qua một khoảng thời gian đủ dài, tool còn tạo một vùng
   **Che watermark OCR** phủ đúng union-box đó trong preview và video xuất. Một
   lần nhận nhầm đơn lẻ không đủ để tự che và không tạo cue rỗng trong SRT.
   Paddle Fast còn dùng mẫu ảnh xám nhẹ mỗi 200 ms trong cùng một lượt decode để
   quét burst quanh câu chỉ một hoặc hai chữ; các mẫu trung gian không gọi Paddle.
   Một chữ chỉ được giữ khi hai frame
   burst có confidence cao và box ổn định, nên không hạ cổng chống nhiễu cho toàn
   video. Với câu từ hai chữ trở lên xuất hiện quá nhanh, một frame Fast và một
   frame Quality vẫn có thể xác nhận lẫn nhau khi khác timestamp nhưng cùng chữ,
   gần thời gian và trùng box; một kết quả đơn lẻ hoặc lệch vị trí vẫn bị loại.
   Dải đọc thoại tách hàng ngang khỏi các cột tên/bản đồ trước khi ghép chữ.
   Cue chồng do trộn chữ cảnh chỉ được gộp khi có bản đọc rõ lặp lại và ít nhất
   hai frame cùng nguồn, cùng thời điểm, trùng vùng thoại xác nhận. Bản dài hơn
   không tự thắng bản rõ hơn; chữ lặp dư ở một frame chỉ được sửa khi có bản
   đọc khác được xác nhận cùng vùng. Hai câu khác nhau, thoại ngắn và các nhãn
   cảnh ở vị trí riêng không bị xóa chỉ vì chồng timestamp. Trường hợp chưa đủ
   bằng chứng vẫn hiện cần duyệt. Evidence chữ cảnh không được kéo dài thời gian
   thoại, kể cả khi tên trên cảnh giống một phần câu thoại và khi tiếp tục checkpoint.
   Để áp dụng quy tắc mới cho kết quả cũ, chạy
   một lượt trích xuất mới; tool không tự viết lại project đang chỉnh.
   Lượt sparse toàn khung nhận tên nhân vật, chiêu thức, pháp bảo và địa danh:
   hội thoại vẫn ở **S1**. Các chữ trong cảnh xuất hiện đồng thời được tách tự
   động sang **S2, S3, S4… · Chữ trong cảnh**; cue không trùng thời gian tái dùng
   lane thấp nhất. Người dùng có thể kéo một chữ cảnh lên S1 khi khoảng thời gian
   đó không có cue khác; đây là thao tác chủ động đổi cue thành **thoại chính**, vì
   vậy cue sẽ đi vào SRT thoại, TTS và D1 ở lần tạo tiếp theo. Kéo một cue thoại
   xuống S2/S3… chỉ đổi vị trí hiển thị, không làm mất vai trò thoại và vẫn được
   lồng tiếng. Nếu đè cue đang có trên lane đích, tool tự tìm lane trống hoặc báo
   rõ lý do khi S1 không thể nhận cue.
   Khi xem toàn bộ phim dài, S1 dày đặc có thể hiện khối tổng quan để giữ giao
   diện mượt; nhãn khối hướng dẫn phóng to để xem chữ. Các lane chữ cảnh thưa như
   S2/S3/S4 vẫn giữ từng cue riêng để có thể chọn và chỉnh trực tiếp.
   Timeline chỉ dựng các clip thuộc cả khoảng thời gian và các S-track đang nhìn
   thấy (kèm một lane đệm), nên thêm nhiều S phụ không buộc QML giữ delegate của
   toàn bộ lane ngoài màn hình.
   Chữ cảnh đã dịch xuất hiện trực tiếp theo playhead trong Timeline Preview, không cần
    xuất video trước. Lượt OCR toàn khung đối chiếu chữ, timestamp và vùng ảnh với
    S1 để không đưa lại hội thoại thường vào S2.
   Các cách đọc OCR gần giống nhau ở cùng vùng được gom trước khi quyết định.
   Chữ cảnh được dò tại chuyển cảnh và thêm mốc cách nhau 3 giây, rồi đọc lại
   vùng phát hiện bằng **Quality** với crop phóng lớn 2 lần. Tool lấy thêm frame
   gần đó để đọc rõ hơn và xác định thời gian xuất hiện/biến mất. Cột chữ dọc,
   mảnh chữ ngang cùng một dòng và nhãn một chữ được xử lý riêng; vùng S1 và
   **Vùng không OCR** vẫn được tôn trọng. Việc đọc lại này có thể tăng thời gian
   trích xuất. Chữ mờ, thư pháp quá khó hoặc xuất hiện quá nhanh vẫn có thể sót;
   không có cam kết bắt đủ 100% chữ cảnh.
   Nhóm chữ cần ít nhất một frame rõ hoặc hai lần đọc Quality đủ tin cậy;
   trường hợp chưa chắc giữ lại để duyệt, không tự chèn vào video.
   Một thẻ cảnh chỉ xuất hiện đúng một
   frame vẫn được chấp nhận khi confidence/độ tin cậy hình ảnh đặc biệt cao và
   không nằm ở góc watermark.
   Cue S2 được dịch cùng Film Bible nhưng không vào SRT hội thoại, VieNeu hoặc D1.
   Khi cue S2 đã được chấp nhận, preview/export vẽ chữ Việt và nền đã chọn,
   không còn tự làm mờ vùng chữ Trung của chữ cảnh, kể cả cue OCR cũ. Chữ cảnh
   mới mặc định dùng Bút lông Comforter, cỡ 40, chữ trắng trên nền mực loang đen
   độ đục 100% và mờ vào/ra; cue chưa đủ chắc chỉ
   hiện **Cần duyệt** và không tự ghi vào video. Sau khi sửa đúng chữ Việt, nhấp
   phải cue S2 rồi chọn **Chấp nhận chữ trong cảnh** để cho phép preview/export.
   Chọn một cue trên S1/S2/S3… để mở bảng chữ gọn kiểu trình soạn thảo. Click
   thường luôn thu lựa chọn về đúng một cue; chỉ Ctrl+click mới cộng hoặc bỏ từng
   cue khỏi nhóm. Việc focus hoặc đúp chuột vào ô nguồn/Việt không xóa nội dung
   đang có. Tab **Dịch thuật/Biên tập phụ đề** giữ ô sửa nội dung và **Thêm cue**;
   các nút định dạng chữ/nền riêng từng cue được gom vào **Timeline → Text**,
   không còn thanh định dạng lặp ở Biên tập. Tab Text áp dụng cho cue S1,
   chữ cảnh cũ/mới và các cue được chọn nhiều hoặc chọn tất cả. Trên Timeline,
   chọn cue sẽ tự mở tab **Text** trong khung thuộc tính;
   các nhóm Chữ, Nền và Hiệu ứng hiện trực tiếp, không còn hộp **Nâng cao**.
   Trong nhóm **Loại cue**, bấm **S1 · Thoại** hoặc **S2 · Chữ cảnh** để chuyển
   cả vai trò và hàng Timeline của một cue hoặc toàn bộ nhóm đang chọn. S1 được
   đưa vào lần tạo giọng tiếp theo; S2 chỉ hiển thị chữ. Nội dung nguồn/Việt,
   thời gian, vị trí, font, màu và nền được giữ nguyên. Đây là đổi loại rõ ràng,
   khác với kéo thoại xuống một hàng S khác (kéo hàng vẫn giữ vai trò thoại).
   Khi chuyển sang S1, chữ cảnh cũ đang nằm trên S1 trùng thời gian sẽ tự xuống
   hàng trống thấp nhất từ S2; chữ cảnh đó giữ nguyên nội dung, định dạng và
   không trở thành thoại. Một lần Ctrl+Z khôi phục cả chuyển loại và chuyển hàng.
   Nếu S2 trùng thời gian với cue đã có hoặc các cue trong nhóm trùng nhau,
   tool báo lỗi và không chuyển cue nào, không tự đẩy sang S3. Có thể hoàn tác
   bằng Ctrl+Z và lưu bằng Ctrl+S. Âm thanh D1 đã tạo không bị xóa khi đổi loại;
   hãy tạo lại giọng để D1 khớp phân loại mới, cache giọng hợp lệ vẫn được dùng.
   Chữ tiêu đề trên T1… được giữ riêng, không chuyển qua hai nút này.
   Chữ đang nhập được lưu vào đúng cue trước khi chuyển loại. Ô trống không
   phải thao tác xóa: tool báo rõ và chưa chuyển loại cho tới khi nhập chữ hợp lệ.
   Cỡ chữ, nền ngang/dọc và giãn chữ có thể vượt 100; độ đục vẫn từ 0–100%.
   Cỡ chữ là giá trị chủ động: kéo chữ lên cao hoặc thu khung đặt không
   tự làm chữ nhỏ lại. Chữ trong cảnh có thêm **Nền ngang/Nền dọc** để nền ôm
   theo chữ thay vì phủ cả khung kéo. Các font thư pháp Playwrite VN, Dancing
   Script, Lobster, Agbalumo, Comforter Brush, Water Brush, Splash và Hurricane
   đều có dấu tiếng Việt và được đóng gói cùng bản phát hành. Bốn font bổ sung là
   các họ OFL có nét gần những mẫu thư pháp Việt; tool hiển thị tên thật thay vì
   giả danh các font VNI/HL/UVN/SVN không có giấy phép phân phối rõ ràng.
   **Preset mực tàu** tạo nền mực bất quy tắc dưới chữ. Danh sách nền cho phép
   chọn **Không nền** hoặc **Nền mờ (blur)** ngay trong **Text → Nền câu đang chọn**.
   **Bỏ nền** tương đương **Không nền**: tắt cả nền màu và blur tự động của câu đã
   chọn, kể cả khi nền mặc định đang bật. Chọn nhiều câu để áp dụng cùng lúc;
   lựa chọn được lưu trong dự án. Blur riêng ôm theo khung chữ mới, mặc định
   dùng bán kính 46 và độ đục 100%; chỉnh **Đục nền (%)** nếu muốn trong hơn.
   Vùng blur thủ công và watermark vẫn độc lập với lựa chọn này. Danh sách còn
   chọn **Mực loang**, **Mực đại mặc**, **Mực tụ** hoặc **Nét cọ**. Hai đầu cọ giữ
   tỷ lệ theo bề dày, chỉ thân giữa co giãn nên nền không bị kéo méo với chữ ngắn
    hoặc dài; **Nền ngang/Nền dọc** tiếp tục điều chỉnh kích thước bao quanh chữ.
    Chữ trong cảnh mới, gồm cue OCR và cue thêm bằng nút **T**, mặc định dùng
    **Bút lông Comforter**, cỡ chữ **40** ngay khi OCR nhập vào project (không chỉ
    ở cửa sổ thêm tay), chữ trắng, nền đen **Mực loang** với độ đục 100%; cue cũ
    giữ nguyên định dạng đã lưu. Chữ cảnh mới tự bật **Mờ vào/ra 300 ms**. Cue được
    thêm thủ công hoặc do OCR tạo mặc định chỉ vẽ nền/chữ mới; chỉ thêm blur khi
    người dùng chọn **Nền mờ (blur)**.
    Nền mực có thể không phủ hết chữ Trung; có thể thêm vùng blur thủ công nếu cần.
    Blur S1 và vùng blur thủ công vẫn độc lập. Các nút định dạng và bộ nhập số hiển thị mô tả khi
    trỏ chuột; dấu giảm, giá trị và dấu tăng luôn được tách thành ba vùng rõ ràng.
   Cue/project cũ vẫn giữ nền phẳng cho tới khi người dùng tự chọn. Từng cue còn chỉnh
   được giãn chữ, đậm/nghiêng và độ đục riêng của chữ/nền. Chữ trong cảnh có thêm
   **Mờ vào/ra**: đặt thời lượng 50–2.000 ms cho mỗi đầu; mặc định 300 ms và cue
   ngắn tự kẹp tối đa nửa thời lượng để không biến mất hoàn toàn. Preview và video
   xuất dùng cùng hiệu ứng cho chữ và nền đã chọn; không tự thêm lớp che chữ Trung
   trong lúc fade. Hai nút **Ống hút**
   lấy trực tiếp màu chữ hoặc màu nền từ frame đang thấy trên Preview; nhấp phải
   khi đang lấy màu để hủy.
   Preview và bản xuất dùng cùng giá trị đã lưu. Click, Ctrl+click, kéo
   vùng quét hoặc Ctrl+A chọn nhiều cue để chỉnh/kéo theo nhóm. Kéo thân một cue
   dịch toàn bộ nhóm cùng khoảng thời gian; kéo mép trái/phải resize mọi cue được
   chọn theo cùng delta. Preview đổi tại chỗ và chỉ gửi một lệnh khi thả.
   Kéo dọc chuyển lane; nếu lane đích đang bận, tool dùng lane trống kế tiếp.
   Trên Preview, khung lựa chọn bao chữ/nền thực tế, không bao toàn video.
   Click một lần để chọn/kéo chữ S1/S2… giữ nguyên playhead và vùng Timeline;
   chỉ đúp chuột mở Biên tập mới tua về đầu câu. Hủy kéo hoặc cue ra khỏi frame
   không lưu vị trí dở dang và không khóa click video để phát/tạm dừng.
   Kéo vị trí giữ nguyên cỡ chữ; tăng cỡ sau khi kéo không bị khung cũ khóa lại.
   Khi đã chọn nhiều cue S1, kéo một cue trong nhóm hoặc chỉnh font, màu, nền,
   cỡ chữ ở tab Text áp dụng cho cả nhóm; cue ngoài nhóm giữ nguyên.
   Các ô chỉnh định dạng tiếp tục nhận thao tác khi đang đồng bộ bản chỉnh.
   Đúp chuột cue ở Preview hoặc Timeline sẽ
   mở đúng dòng trong **Biên tập phụ đề** và tua đến đầu câu. Nhấp phải cho phép
   xóa cue ở cả hai nơi; thao tác xóa bỏ đồng thời chữ dịch, nền/cover do tool tạo
   và clip Timeline, đồng thời vẫn hoàn tác được. Đây là phép che/thay chữ, không
   phải khôi phục pixel đã bị hard-sub ghi sẵn vào video nguồn.

   Dùng biểu tượng **T** trên Timeline hoặc Biên tập phụ đề để bổ sung cue bị sót
   tại playhead hiện tại. Chọn **Thoại chính · có lồng tiếng** hoặc **Chữ trong
   cảnh · không đọc**, rồi đặt nội dung, thời lượng, font, cỡ, hướng, màu, nền,
   padding, giãn chữ, đậm/nghiêng, độ đục và fade ngay trong một hộp dùng chung.
   Thoại mới tự kế thừa vị trí và kiểu chữ của thoại gần playhead, ưu tiên S1 và
   dùng lane trống tiếp theo nếu S1 đang bận nhưng vẫn được lồng tiếng. Chữ cảnh
   mới cho kéo vùng trên frame thật và tạo một S mới dưới các lane hiện có. Cue mới
   có thể di chuyển, kéo mép, sửa hoặc xóa như cue OCR và được giữ nguyên khi chạy
   lại OCR hoặc dịch.
   Nếu một thao tác lưu phụ đề ngắn đang chạy, nút **T** vẫn nhận yêu
   cầu và tự thêm chữ ngay sau khi lần lưu đó hoàn tất. Timeline có thanh cuộn dọc
   nhưng không tự cuộn khi đổi cue: vị trí dọc chỉ thay đổi khi người dùng lăn
   chuột hoặc kéo scrollbar. Project cũ có nhiều chữ
   cảnh cùng giờ trên S2 được tách an toàn sang các lane trống khi mở, không đổi
   text, timestamp, cue ID hoặc lane chữ thủ công.

   Trước khi trích xuất, **Quét vùng OCR** cho phép kéo vùng hội thoại trên một
   frame hiện tại của chính video. Chữ giao vùng này được đưa vào S1; chữ ngoài
   vùng được đánh giá riêng cho S2. **Vùng không OCR** cho phép thêm nhiều hình
   chữ nhật loại trừ watermark, logo hoặc chữ không được gửi vào OCR. Vùng loại
   trừ luôn có ưu tiên cao hơn hai lượt quét. Nếu không cấu hình vùng hội thoại,
   tool dùng các dải phụ đề mặc định hiện có; nếu không có vùng loại trừ thì không
   che vùng nào. Nút đặt lại xóa toàn bộ cấu hình tùy chỉnh này.

   Thoại OCR mới đặt tâm chữ ở tâm vùng hội thoại đã chọn khi bắt đầu lượt OCR;
   không chọn vùng riêng thì dùng tâm vùng thoại mặc định. Vùng khoanh chỉ xác
   định vị trí, không giới hạn độ rộng/cỡ chữ của câu dịch: câu dài có thể vượt
   vùng khoanh, nhưng được giữ trong khung video. Vị trí theo cue qua dịch và
   lưu/mở lại; kéo hoặc đặt khung chữ thủ công sẽ ưu tiên vị trí bạn sửa.
   Cue đã lưu ở bản cũ không bị tự di chuyển khi mở dự án. SRT thuần không chứa
   vị trí/font; các thuộc tính này được lưu trong dự án và dùng khi xuất MP4.

   Hội thoại một chữ xuất hiện rất nhanh như `爹` hoặc `娘` được lấy thêm một
   burst cục bộ quanh thời điểm chuyển cảnh. Vẫn cần hai quan sát rõ ở cùng vị
   trí nên độ phủ tăng mà không tăng tần suất OCR và nhiễu trên toàn video.
   Sau khi có S1 hoặc bản dịch Việt, dùng **Xuất SRT nguồn** hoặc
   **Xuất SRT tiếng Việt** để lưu đúng cue/timestamp đã sửa gần nhất dưới dạng
   UTF-8 BOM. Xuất SRT không sửa project, job hoặc cache.
4. **Dịch:** cấu hình miễn phí mặc định dùng Gemini Web chạy ẩn. Điều khiển Web
   nổi và đăng nhập/khách được ẩn để giao diện gọn và nhất quán. Mỗi
   request mở một chat mới, tối đa 150 cue dịch (và 8.000 ký tự nguồn); sau năm
   request thật tool đóng toàn bộ browser Guest rồi mở phiên sạch. Mỗi request dịch
   tự QA ngay trong cùng lượt; không còn một request Gemini QA thứ hai cho từng chunk.
   Film Bible, dịch và các cue thật sự cần sửa đều được tính là lần gửi lên Gemini.
   Tool chỉ chạy một browser/tab tuần tự và cache theo cue ID.
   Mỗi project có mục **Văn phong dịch** gồm **Tự nhận diện**, **Tu tiên cổ
   trang**, **Võ hiệp**, **Cung đình/cổ trang**, **Hiện đại**, **Trung tính** và
   ô **Quy tắc riêng** tối đa 4.000 ký tự. Cùng một preset, Film Bible và quy tắc
   được đưa vào mọi transport dịch, prompt sửa và QA. Với preset cổ trang, tool
   ưu tiên xưng hô theo vai vế, đặt tên trước hậu tố như `Hồng Ly tỷ tỷ`, và chỉ
   dùng `A Cẩu/A Miêu/Lão Cẩu` khi chữ nguồn là tên gọi `阿狗/阿猫/老狗`; từ
   chó/mèo chỉ động vật hoặc câu chửi không bị thay máy móc. Cue vi phạm được sửa
   đúng một lượt; nếu vẫn chưa đạt thì giữ bản dịch, báo warning và vẫn cho qua
   TTS/Multi. CJK còn sót vẫn theo blocker hiện có. Đổi preset/quy tắc tạo cache
   dịch riêng nhưng không làm mất cache OCR. Multi đóng băng lựa chọn của từng
   project khi bắt đầu lượt chạy. Tool chỉ tự xuống dòng bản dịch khi nội dung
   vượt 68 ký tự; dòng ngắn hơn được giữ nguyên trừ khi người dùng chủ động chèn
   xuống dòng trong Biên tập phụ đề. Muốn lưu một bộ quy tắc để dùng lại, nhập
   **Tên bản văn phong** rồi bấm **Lưu văn phong**. Bản lưu nằm trong project,
   xuất hiện ở **Bản đã lưu** và có thể chọn lại để nạp preset cùng toàn bộ quy
   tắc. Tên trùng được từ chối để tránh ghi đè nhầm; hãy dùng tên khác cho một
   phiên bản mới.
   Lỗi 1095/1905/about:blank chỉ được nhận từ bề mặt lỗi đã xác minh; mã cue như
   `id:1095` trong JSON không bị coi là lỗi. Phiên sẽ đóng, backoff có giới hạn
   và gửi lại đúng chunk chưa xong. Với API, chọn **Gemini API**, **DeepSeek API**,
   **Hy3 · TokenHub**, **Qwen API**, **GLM API** hoặc **OpenAI-compatible**. Các
   preset tự điền base URL/model; ô model có danh sách xổ xuống và vẫn gõ tay
   được. Sau khi lưu/chọn key, **Tải model** lấy danh sách API ở nền; lỗi mạng
   không làm mất tên đã nhập. Danh sách chỉ thuộc provider đang chọn và loại
   model chuyên ảnh, giọng nói, embedding hoặc live; vẫn không bảo đảm tài
   khoản có quyền dùng mọi model. Model nhập thủ công không bị giới hạn bởi
   danh sách. OpenAI-compatible yêu cầu base URL HTTPS (HTTP
   chỉ cho localhost). Điền **Tên key** vào ô rõ chữ và **API key** vào ô che
   ký tự riêng, rồi **Lưu key**; có thể thêm nhiều key lần lượt. Key chỉ nằm trong Windows
   Credential Manager; job chỉ lưu tên tham chiếu. Mỗi provider có pool riêng và
   chỉ xoay sang key kế tiếp sau lỗi xác thực hoặc quota đã xác minh; 429 không rõ
   nguyên nhân vẫn retry đúng key. Request API tự gửi User-Agent của DUCAI và
   `Accept: application/json` để tương thích các gateway/CDN OpenAI-compatible.
   Nếu gateway vẫn chặn client, lịch sử báo **Bị gateway/CDN chặn** thay vì báo
   nhầm key sai và không xoay hết pool. DeepSeek/GLM mặc định tắt thinking và Qwen tắt
   thinking để giảm trễ; JSON mode được bật cho phản hồi có cấu trúc. Các cue vừa
   checkpoint xuất hiện ngay trong cột **Bản dịch Việt** (preview sống, chưa phải
   SRT cuối). Nút dịch chính hoạt động giống nút OCR: **Bắt đầu dịch** khi chưa có
   job, **Tạm dừng dịch** khi đang chạy, **Tiếp tục dịch** khi đã tạm dừng và
   **Tiếp tục dịch từ cache** khi cache được chọn đang ở trạng thái đã hủy, lỗi,
   gián đoạn hoặc cần rà soát. Tool chỉ retry đúng job/ledger đã chọn khi provider,
   model, endpoint tùy chỉnh, key và văn phong/quy tắc riêng vẫn khớp job đó. Đổi lựa chọn thì nút
   chuyển thành **Dịch với lựa chọn mới**; job cũ được dừng an toàn trước khi tạo
   đúng một job mới, không chạy hai worker cùng lúc. Cache cũ được giữ nhưng
   không trộn bản dịch của cấu hình khác vào lượt mới. Nếu tiếp tục cùng cấu
   hình, các cue đã checkpoint không bị gửi lại. Tên key được nhớ riêng cho
   từng provider khi chuyển qua lại hoặc mở tool lần sau.
   Khi lượt dịch đang chạy hoặc tạm dừng, bảng tiến độ/QA tự bám đúng lượt đó;
   phản hồi cache cũ không được ghi đè kết quả mới. Chọn cache lịch sử khi không
   có lượt dịch đang bận.
   Khi đã tạm dừng, có thể đổi văn phong hoặc sửa quy tắc rồi bấm **Tiếp tục**:
   tool lưu bản mới trước khi tạo lượt dịch tương ứng. Nếu lưu lỗi, tác vụ không
   tiếp tục với prompt cũ. Với API, Tạm dừng/Hủy nhận lệnh cả khi đang đợi HTTP;
   phản hồi đang nhận được giữ riêng khi pause, không nhập trước khi resume.
   Đổi model/prompt tạo lượt mới nên những câu dùng cấu hình cũ không tự trộn vào.
   Giới hạn mặc định khoảng 80 cue/lượt giúp giữ ngữ cảnh và kiểm tra đủ cue;
   các lượt chạy liên tục, không có khoảng nghỉ bắt buộc giữa hai lượt thành công.
   Quota đủ chỉ cho phép gọi API, không bảo đảm model suy luận nhanh. ETA còn
   phụ thuộc model, độ dài câu, Film Bible, quy tắc và các lượt sửa QA thực tế.
   Khi mở tool, mỗi pool chỉ giữ các tên key đã gán cho đúng provider và vẫn còn
   trong Windows Credential Manager; tên key chưa gán không tự bị nhận làm Gemini.
   Nếu dán key mới cho một job API đã dừng nhưng giữ nguyên provider/model,
   tool cập nhật pool rồi retry từ checkpoint hiện có. Có thể dùng **Nhập SRT Việt** để nạp bản dịch từ bên ngoài; tool
   chỉ ghi vào dự án khi số cue, index và toàn bộ timestamp khớp SRT Trung.
   **Antigravity Tools cục bộ:** bật proxy trong Antigravity, chọn
   **OpenAI-compatible**, nhập `http://127.0.0.1:8045/v1`, lưu key proxy trong
   ô API key với tên riêng (ví dụ `Antigravity local`), rồi chọn đúng key đó.
   Bấm **Tải model** và chọn `gemini-3.8-flash-high` nếu endpoint của bạn có
   model này. DUCAI gọi `/v1/chat/completions`, không gọi thẳng Gemini API.
   Antigravity cần tiếp tục chạy; tài khoản/quota ở proxy vẫn có thể giới hạn
   tác vụ. Key không được lưu vào project, mã nguồn hoặc gói phát hành.
   Gemini Web phải bắt đầu trả lời trong 30 giây; nếu không, tool đóng phiên và
   thử lại bằng tab sạch thay vì đứng im nhiều phút. Nếu Gemini đã trả nhưng để
   JSON dở đứng im 45 giây, tool cũng đóng phiên và gửi lại đúng chunk. Lỗi
   transport không tự chia nhỏ chunk; chỉ phản hồi JSON hoàn chỉnh nhưng sai
   định dạng/nội dung mới được chia để cô lập an toàn. Circuit breaker theo
   job/pha dừng retry khi lỗi liên tiếp vượt ngưỡng an toàn.
   Cấu hình Guest miễn phí không tạo tiến trình Codex/Luna và không phát sinh phí
   OpenAI. Nếu Gemini vẫn lỗi sau các phiên sạch, job dừng ở checkpoint gần nhất
   để tiếp tục, không âm thầm chuyển sang dịch vụ trả phí.
   Mỗi response dịch chứa luôn bản dịch và blocker QA chưa tự sửa được trong cùng
   một JSON. Gate cục bộ kiểm tra thêm đủ ID, câu rỗng, CJK, Latin cấm, độ dài và
   tên riêng theo Film Bible. Mỗi cue còn gửi kèm `start_us/end_us`, duration slot
   và ngân sách ký tự mềm để AI ưu tiên câu Việt gọn, tự nhiên, đủ nghĩa, giữ tên
   riêng và phủ định. Đây chỉ là hướng dẫn/QA cảnh báo; tool không cắt câu theo số
   ký tự và chỉ xác nhận blocker timing sau khi đo WAV thật. Chỉ cue blocker được gửi sửa, không gửi lại toàn bộ
   chunk và không chạy QA từ xa lần hai. Khi tiếp tục từ cache, mọi câu đã có được
   giữ theo cue ID; chỉ phần chưa hoàn tất hoặc blocker thật mới chạy lại. Job
   REVIEW cũ vẫn có thể dùng nút **Sửa QA từ cache** mà không dịch lại toàn bộ phim.
   Tên và thuật ngữ đã chốt trong Film Bible được giữ nguyên cả cách viết và loại
   tên; gợi ý ở lượt sau chỉ thêm mục mới. Prompt yêu cầu giữ xưng hô theo vai và
   bằng chứng ngữ cảnh, nhưng nguồn không có nhãn người nói nên vẫn cần rà soát
   những đoạn mơ hồ. Tool giữ dịch tuần tự để chia sẻ ngữ cảnh giữa các lượt;
   không tự hạ model hoặc bỏ QA để tăng tốc.
   Nếu Gemini trả đủ bản dịch nhưng phần `issues` sai schema, tool giữ nguyên các
   câu vừa nhận, đánh dấu cả chunk cần rà soát và dịch tiếp chunk sau; lỗi định dạng
   QA không còn làm mất response hoặc khiến job chạy lại từ đầu.
   Mọi trường tiếng Việt của Film Bible, glossary và đề xuất QA đều bị loại nếu
   còn chữ Hán. Khi nạp ledger cũ, tool làm sạch riêng phần metadata bị nhiễm nhưng
   giữ nguyên toàn bộ cue đã cache; gate cuối vẫn chặn tuyệt đối SRT còn CJK.
   Phiên Web yêu cầu đăng nhập hoặc CAPTCHA được nhận diện trước bước chat và
   báo rõ rằng chế độ ẩn không thể xử lý; hãy thử lại sau hoặc chọn API/CLI. Khi đang chờ
   Playwright, worker vẫn gửi elapsed thật mỗi giây và nhận Tạm dừng/Hủy ngay
   tại control point, nên thao tác dừng không còn biến thành
   `worker_exit_without_ack`. Ở QA tích hợp, mỗi cue chỉ tính một lần vào tiến độ;
   số cue đã kiểm tra vẫn được hiển thị phụ nhưng không làm tổng tiến độ tăng gấp đôi.
   Bảng **QA bản dịch** ở khung bên phải cập nhật theo từng checkpoint và hiển thị
   một hàng cho mỗi cue blocker còn lỗi. Bấm vào hàng lỗi sẽ chọn đúng cue, cuộn
   danh sách tới dòng đó và tua playhead tới đầu câu để sửa thủ công. Sửa tay chỉ
   cập nhật nội dung; trạng thái vẫn là **Cần sửa** cho tới khi worker chạy QA lại.
   Khi không còn blocker và toàn bộ cue đã được kiểm tra, bảng chuyển sang **Đạt**;
   **Đang QA** nghĩa là chưa được phép kết luận đạt.
5. **Tạo giọng:** Trong **Thư viện giọng**, bấm **Nghe thử** để nghe mẫu 5-8 giây
   ngay cả khi chưa mở dự án hoặc chưa cài model. Với giọng Piper có nguồn đã xác
   minh, bấm **Cài** và theo dõi phần trăm ngay trên thẻ; model chỉ chuyển sang
   **Đã cài** sau khi kiểm tra xong. VieNeu v3 Turbo có 25 preset dựng sẵn để chọn
   trực tiếp, ngoài mục **Giọng clone của bạn**. Các preset VieNeu và VIVOS mang
   giấy phép phi thương mại, vì vậy cần kiểm tra quyền trước khi xuất bản nội dung.
   Trong Phòng thu → **Giọng**, mục **File mẫu clone VieNeu** cho chọn file rồi
   **Nghe file mẫu**, tạm dừng, tua hoặc **Dừng** ngay trong tool. Nghe file không
   cần tạo project, tải model hay tổng hợp giọng, không gửi file lên dịch vụ.
   Chọn file không tự phát; đổi file hoặc rời Phòng thu dừng bản đang nghe.
   File hỏng/không giải mã được có thông báo ngay dưới nút nghe. File này chỉ
   được dùng làm mẫu lồng tiếng khi bạn chọn chức năng clone VieNeu tương ứng.
   Piper là lựa chọn nhanh. VieNeu-TTS v3 Turbo 3.8.1 dùng profile
   clone 192D và reference codes, ưu tiên chất lượng. VieNeu gửi tối đa 32 cue
   trong một batch, tự hạ batch nếu GPU hết VRAM và giữ host/model nóng. Đường
   fused CUDA Graph do chính VieNeu 3.8.1 cung cấp; tool không còn vá API nội bộ
   của runtime. Profile 3.3 được giữ nguyên trên đĩa và chỉ bỏ frame pad cũ trong
   RAM khi bind vào engine mới.

   **Phân vai thoại (thử nghiệm):** Cài bộ nhận diện một lần bằng thao tác rõ ràng
   trong PowerShell: `scripts/setup-dialogue-casting-runtime.ps1 -DataRoot
   '<DUCAI_STUDIO_DATA>'`. Script cài runtime và model đã ghim vào data root;
   ứng dụng không tự tải model. Nếu chưa cài hoặc checksum sai, tác vụ báo lỗi
   cài đặt thay vì ghi nhãn “Cần kiểm tra” giả.
   Bộ cài gồm MOSS để nghe mạch người nói, cùng bộ tách người nói và nhận chất
   giọng. Sau khi cài xong, bước phân vai chạy offline. Nếu đã chuẩn bị đủ cache
   model và dependency trên máy, thêm `-Offline` khi cài; dùng `-DownloadCache`
   để chỉ thư mục cache model. Cache thiếu hoặc không khớp checksum sẽ báo lỗi.
   Xem [script cài đặt](../scripts/setup-dialogue-casting-runtime.ps1) để biết
   các tham số được hỗ trợ.

   Có thể bật **Phân vai thoại sau trích xuất** ngay ở màn hình OCR/Whisper.
   Sau khi trích xuất thành công, tool tự nhận diện từ âm thanh và SRT gốc;
   bạn vẫn có thể dịch trong lúc chờ. Khi bấm **Tạo giọng**, tool dùng lại kết
   quả còn khớp hoặc chờ lượt nhận diện đang chạy, không mở lượt trùng.
   Multi giữ luồng tuần tự và tự phân vai trước TTS theo cấu hình của lượt chạy.
   Mở lại ứng dụng hoặc vào Phòng thu chỉ nạp kết quả đã lưu; lịch sử OCR đã
   hoàn thành không tự khởi động lượt nhận diện mới. Bấm Tạo giọng sẽ kiểm tra
   lại độ khớp với âm thanh/thời gian hiện tại và chỉ phân tích câu còn thiếu.

   Trong **Phòng thu giọng**, bật chức năng,
   chọn và nghe thử giọng Nam, Nữ và Dự phòng. Khi bấm **Tạo giọng**, DUCAI phân
   tích âm thanh gốc trước, gán nhãn theo từng câu rồi dùng giọng tương ứng để
   tạo D1. Câu chồng tiếng, quá ngắn hoặc chưa đủ bằng chứng dùng giọng Dự phòng.
   Bạn có thể sửa nhãn ở danh sách thoại; nhãn sửa tay được lưu trong project.
   **Nghe gốc** phát đúng khoảng âm thanh nguồn của câu. Sửa từng câu hoặc nhiều
   câu giữ vị trí danh sách; nếu câu rời khỏi bộ lọc, danh sách ở gần vị trí đó.
   Các giọng đã cài có mẫu nghe được chuẩn bị sẵn, gồm từng profile clone hoàn
   chỉnh trên máy. Bấm nghe phát mẫu đúng giọng ngay; giọng clone mới hoặc mẫu
   thiếu/hỏng sẽ được tạo bằng tác vụ nghe thử và lưu lại cho lần sau.
   Ứng dụng phát mẫu khi bạn bấm nghe hoặc nhập file giọng mới; mở lại tool,
   mở project hay làm mới thư viện không tự phát. Mỗi vai có nút **Chọn file giọng…**
   để dùng WAV, MP3, M4A, FLAC hoặc OGG riêng qua VieNeu; tool sao chép mẫu vào
   kho giọng được quản lý rồi lưu riêng cho vai đó, không thay mẫu của các vai
   khác. Nhập thành công sẽ tự tạo một câu bằng giọng clone rồi phát để bạn kiểm
   tra; đây là giọng được tổng hợp, không phải phát lại bản ghi gốc. Lần đầu cần
   chờ nạp model và tạo mẫu; lần sau dùng mẫu đã lưu nếu còn khớp. Nút **Nghe thử**
   cho phép nghe lại. File lỗi hoặc nhập thất bại không phát mẫu giọng cũ.
   Sau khi nhập thành công, chuyển hoặc xóa file gốc không làm mất giọng
   đã nhập. VieNeu 3 Turbo không bắt buộc lời
   mẫu. Chọn lại giọng trong danh sách để thay file mẫu riêng bằng giọng đó.
   File nén như AAC/M4A được giải mã sang WAV tạm khi tạo profile; file gốc và
   bản sao đã nhập được giữ nguyên. Nếu file hỏng, thiếu FFmpeg hoặc giải mã quá
   lâu, tool báo lỗi file giọng cụ thể để bạn xử lý.

   Riêng câu máy không phát hiện lời thoại giữ nhãn **Cần kiểm tra** và chưa tạo
   giọng. Gán **Nam** hoặc **Nữ** nếu bạn xác nhận câu đó có thoại; chọn **Tự
   động** để trả về kết quả máy và bỏ clip cũ khỏi D1 khi tạo lại. Các lý do
   chưa chắc khác vẫn dùng giọng Dự phòng.
   Mở lại dự án, **Khớp giọng** hoặc đặt lại D1 chỉ xử lý các câu đã được tạo
   giọng; câu không có thoại vẫn được giữ trong phụ đề và danh sách kiểm tra.

   MOSS nghe từng khối âm thanh gốc và đối chiếu mốc người nói với SRT gốc.
   Một câu chưa rõ chỉ được gán thêm khi có các đoạn giọng rõ, độc lập và nhất
   quán của cùng người nói. MOSS và nội dung chữ trong SRT không tự quyết định
   nhãn Nam/Nữ; câu liền kề cũng không phải bằng chứng cho nhãn này.
   Tool kiểm tra thêm các đoạn âm thanh nằm trọn trong một lượt nói rõ của
   MOSS. Câu từng bị đánh dấu nhiều người nói hoặc bằng chứng mâu thuẫn chỉ
   được tự gán khi chính câu đó và ít nhất hai đoạn độc lập bên ngoài câu cùng
   xác nhận một chất giọng; bằng chứng trái chiều vẫn giữ câu để kiểm tra.
   Tạm dừng/tiếp tục giữ các bước phân tích đã xong; bước đang nghe sẽ chạy lại.
   Trong mỗi bước, model được giữ để xử lý các khối tiếp theo; chuyển bước sẽ
   giải phóng engine trước. Thời gian còn phụ thuộc độ dài và mật độ lời thoại.
   Tác vụ phân vai cũ không tương thích checkpoint mới cần được tạo lại.
   Khi bộ nhận diện đổi phiên bản, kết quả tự động cũ cần được phân tích lại
   trước khi dùng; nhãn sửa tay vẫn được giữ. Đây là tính năng thử nghiệm:
   độ chính xác trên hoạt hình Trung Quốc chưa qua cổng
   đánh giá độc lập và vẫn cần nghe lại các câu được máy tự gán.

   Sau khi dịch, bạn có thể sửa nội dung tiếng Việt, thời gian, thêm/xóa cue hoặc
   cắt video rồi bấm **Tạo giọng** mà không phải dịch lại. Tool chờ lưu toàn bộ
   chỉnh sửa mới nhất trước khi tạo tác vụ; cue nằm hoàn toàn trong phần đã cắt
   bỏ không được lồng tiếng. Nếu cắt ngang một cue, tool giữ nguyên lời của cue
   còn lại; bạn cần sửa lời nếu chỉ muốn đọc một phần câu.
   Cue gốc và bản dịch được ghép theo mã cue, không theo vị trí trong danh sách.
   **Tiếp tục từ cache** cũng lấy bản sửa mới nhất và chỉ tái sử dụng WAV còn hợp
   lệ; câu đã đổi nội dung hoặc thời gian sẽ được xử lý lại. Cue thoại cần có
   nội dung tiếng Việt hợp lệ; chữ cảnh không được lồng tiếng.
   QA/fit vẫn chạy riêng từng cue. **Tốc độ giọng gốc** áp dụng cho toàn bộ D1,
   chọn từ 1,10× đến 3,00× và mặc định 1,30× cho job mới. VieNeu vẫn inference ở
   1,00×; tool tạo WAV nhanh hơn bằng FFmpeg giữ cao độ từ WAV cache nền. Đổi tốc
   độ không gọi lại model và mỗi tốc độ có cache variant riêng, không stretch nối
   tiếp từ variant cũ. Job cũ thiếu setting này vẫn tiếp tục ở 1,00×.
   Phần xử lý WAV sau model được phân cho tối đa ba tiến trình CPU ẩn; model GPU,
   âm thanh, tốc độ đọc và QA không đổi. Việc này diễn ra tự động, không cần bật
   thêm model hay chia ba tác vụ. Mức nhanh hơn phụ thuộc thời gian model tạo
   giọng và cấu hình máy; không phải toàn bộ tác vụ đều nhanh lên ba lần.

   **Kịp theo câu** luôn được bật trong luồng **Tạo toàn bộ**. Nó chỉ tác động cue
   có WAV thực tế dài hơn slot SRT sau khi đã áp dụng tốc độ gốc. Ô tốc độ của nó
   là **giới hạn tổng cuối cùng**, không cộng mù vào tốc độ gốc: nếu gốc 1,50× và
   cap 2,25×, cue chỉ tăng tới đúng tốc độ cần thiết nhưng không quá 2,25×. Cue
   vừa hoặc ngắn giữ nguyên tốc độ gốc. Tool đo lại WAV và chấp nhận sai số tối đa
   50 ms; cue vẫn dài hơn mang `CUE_SYNC_LIMIT_EXCEEDED/CUE_TOO_LONG`, giữ trọn câu
   và không bị chồng, cắt hoặc bỏ. Thứ tự xử lý là tốc độ gốc → tăng cue dài trong
   cap → đo lại → chỉ khi vẫn lỗi mới tự rút gọn cue đó và tạo lại riêng WAV có text
   đổi. Người dùng chỉ cần bấm **Tạo toàn bộ**; không có checkbox hay nút sửa cue
   thủ công trong Phòng thu giọng.

   Khi đủ cue, voice tự vào D1. Với job mới, cue dài, tên riêng lệch, chữ Hán còn sót và lỗi ngữ nghĩa
   là **cảnh báo**: voice vẫn được ghép và Timeline D1 phân loại timing theo màu.
   Chỉ voice dài hơn slot SRT trên 50 ms mới có viền đỏ; voice ngắn hơn hoặc đúng
   thời lượng nhưng lệch vị trí có viền vàng; cue khớp trong sai số 50 ms chuyển
   xanh ngay. Badge nội dung được giữ riêng và không làm clip đỏ. Chỉ lỗi kỹ thuật (thiếu
   hoặc hỏng WAV/cache, dữ liệu không nhất quán) mới chặn việc vào D1. Bấm đúp cue
   sẽ mở đúng dòng trong Biên tập dịch thuật; tự rút gọn chỉ tạo lại cue đã đổi từ
   cache.
   D1 tách cue được tạo bởi bản cũ nhưng thiếu trường liên kết sẽ được phục hồi
   khi ID clip còn khớp duy nhất với ID cue nguồn. Tool không đoán theo thứ tự nếu
   liên kết mơ hồ; Inspector sẽ hướng dẫn tạo lại D1 từ cache trong trường hợp đó.
   Tiếp tục từ cache không suy diễn lại cue đã PASS với cùng setting. Khi chuyển một
   job cũ từ chế độ nghiêm sang luồng tự động, worker nhận cả fingerprint trước khi
   trường adaptive được thêm vào. Cue đã PASS ở chế độ nghiêm dùng nguyên WAV đã xử
   lý và QA cũ; chỉ cue từng vướng giới hạn thời lượng mới lấy WAV thô để tăng tốc/fit
   lại. VieNeu chỉ đọc những cue chưa từng có raw hợp lệ. Nếu toàn bộ cue đều được
   khôi phục từ cache, tool vẫn lưu ledger timing hoàn chỉnh trước khi công bố kết
   quả; D1 vào Timeline vẫn được chia thành từng cue, không trở lại thành một thanh
   audio duy nhất.

   Nếu chọn **Bỏ qua cảnh báo · giữ nguyên voice**, tool không đặt các WAV dài
   chồng lên timestamp kế tiếp. Các cue được xếp tuần tự theo chỉ số SRT và
   timeline được kéo dài đúng phần cần thiết; toàn bộ câu vẫn được giữ nguyên.
   Tùy chọn **Cho phép kéo dài timeline** một mình không đủ để bỏ qua overlap.
   Khi lỗi xảy ra, giao diện hiển thị hướng dẫn tiếng Việt; mã kỹ thuật chỉ lưu
   trong lịch sử/log để chẩn đoán.
   tra, số cue khôi phục/dùng nguyên/fit lại, số cue còn phải tạo và ETA riêng.
   Trong lúc dò cache, dòng hoạt động ghi rõ đang đọc ledger, đối chiếu cue nào
   và đã kiểm tra bao nhiêu; ETA của pha khôi phục tách riêng với ETA tổng để
   không bị hiểu nhầm là worker đứng ở một mốc. Trước event cache đầu tiên, thẻ
   ghi rõ VieNeu đang nạp model và chuẩn bị dò cache thay cho nhãn chờ chung.
   Số cue luôn là số đã hoàn tất thật; trong một batch 32 cue, đồng hồ hoạt động
   tăng mỗi giây và thanh tiến trình chuyển mượt tới checkpoint mới thay vì báo
   trước những cue GPU chưa trả kết quả.
   Sau tổng hợp, timeline giọng được ghi WAV streaming thay vì tạo một mảng float
   dài bằng cả phim. Cùng dòng PCM đó được đo loudness ngay trong lúc ghép, rồi
   mới chạy lượt chuẩn hóa EBU R128 thứ hai; không bỏ lượt đo và không đổi cache
   cue. Thẻ tác vụ hiện phần trăm một chữ số thập phân, heartbeat mỗi giây và mô
   tả riêng pha **ghép + đo**/chuẩn hóa. Khi WAV PASS tự vào D1, nút **Nghe voice thành phẩm** trong Phòng
   thu giọng phát trực tiếp toàn bộ file vừa ghép; nút preview mẫu vẫn tách riêng.
   Sau khi D1 nền đã PASS, khung **Đồng bộ sau lồng tiếng** cung cấp ba chế độ chỉ
   hậu xử lý cache WAV, không gọi lại VieNeu: **Giãn video** giữ voice 1× và làm
   chậm đúng block hình; **Tăng tốc audio** giữ nguyên hình và tăng riêng cue dài;
   **Kết hợp** tăng voice đến mức thanh kéo rồi để video hấp thụ phần còn lại.
   Thanh audio chạy từ 1,00× đến 2,00×, mặc định 1,25×; video không chậm dưới
   0,50×. Cue vượt cả hai giới hạn được đánh dấu `CUE_SYNC_LIMIT_EXCEEDED` như một
   cảnh báo, có thể bấm để sang Biên tập dịch thuật và không được âm thầm cắt, bỏ
   hoặc chồng lời. Export ghi rõ số cảnh báo được giữ nguyên.
   **Áp dụng đồng bộ** tạo D1 hậu xử lý mới từ WAV nền và cập nhật Timeline chỉ sau
   QA PASS. **Hoàn tác về nhịp gốc** tạo revision mới dùng lại D1 nền, tránh cộng
   dồn biến dạng khi đổi chế độ hoặc kéo tốc độ nhiều lần. Khi kéo thanh hoặc
   đổi chế độ, thẻ chỉ tính lại phần tóm tắt (số cue, tốc độ, duration và cue
   cần sửa); Job Manager giữ kế hoạch đầy đủ và cache WAV đã xác thực nên giao
   diện không phải đọc/băm lại toàn bộ ledger. Chỉ khi bấm **Áp dụng đồng bộ**
   kế hoạch đầy đủ mới được dùng để tạo tác vụ hậu xử lý.

   Timeline có mục **Khớp giọng** riêng cho trường hợp chỉ muốn tăng voice theo
   SRT. Chọn **chỉ tăng audio**, đặt cap và bấm **Áp dụng**. Mỗi cue lấy đúng
   `start_us/end_us` của SRT nguồn làm slot; duration WAV Việt thật được đối chiếu
   với slot đó. Cue dài mới tăng, cue bằng/ngắn giữ 1,00× ở bước hậu xử lý này.
   Sau PASS, track D1 được tách thành từng khối/clip cue (vẫn dùng chung một WAV
   master). Đỏ là dài hơn slot trên 50 ms, vàng là ngắn hoặc lệch vị trí, xanh là
   đã khớp. Rê chuột xem timestamp/duration/tốc độ và bấm để seek đúng cue. Kéo
   mép trái hoặc phải thay đổi tốc độ của toàn bộ WAV cue, không cắt nội dung;
   hình clip bám chuột tại chỗ và chỉ ghi một revision khi thả. Trong vùng hút
   tối thiểu 80 ms (mở rộng theo zoom), Timeline hiện vạch timestamp xuyên track,
   dải đo ngang, độ lệch và speed kết quả; thả đúng mốc SRT làm cue đổi xanh ngay.
   Giới hạn 0,50×–3,00× được báo ngay tại con trỏ và `Esc` hủy lần kéo. Khi zoom xa, khối
   được gom để Timeline 13k cue không lag; dữ liệu và export vẫn giữ đầy đủ từng
   cue. Timestamp này chính xác theo SRT nguồn ở microsecond, vì vậy nếu OCR/SRT
   nguồn vốn đặt ranh giới sai thì cần sửa cue nguồn trước—tool không thể suy ra
   ranh giới lời nói tốt hơn từ một timestamp sai.
6. **Preview:** kiểm tra đầu/giữa/cuối và các gap nghi vấn với video, sub và D1
   đồng bộ trước khi xuất. Pause rồi tua hoặc kéo playhead sẽ neo lại D1 đúng mốc
   trước khi phát, không hard-seek giọng liên tục. Nút âm lượng preview được lưu
   lại; `Space` hoặc click vùng video trống phát/tạm dừng tức thì. Bước tua
   `←/→` và hai nút mũi tên chỉnh được từ 1–1000 ms, mặc định 100 ms, nên có thể
   căn cue sát frame thay vì nhảy cố định 5 giây. Các phím này không chiếm thao tác khi đang gõ trong ô
   chữ. Ô tốc độ và âm lượng clip nhận cả dấu chấm lẫn dấu phẩy thập phân. Bấm cue
   trong bảng nguồn/bản dịch vẫn chọn và đưa video/playhead tới đầu cue. Riêng
   trong vùng track Timeline, bấm trực tiếp lên clip chỉ chọn cue và giữ nguyên
   playhead; bấm vùng trống mới bỏ lựa chọn cue hiện tại rồi tua Preview tới đúng
   timestamp dưới con trỏ. Kéo clip, kéo mép và quét chọn vẫn giữ chức năng riêng,
   không tua nhầm. Thanh phát/dừng, thời gian, tua và âm lượng nằm cố định dưới
   Preview, ngoài khung video nên không che chữ hoặc tranh vùng kéo cue.
   Timeline có tab
   **Phụ đề** riêng để xem và bấm trực tiếp cả chữ nguồn lẫn bản Việt. Tab
   **Kiểu sub** cho chọn bảng màu chữ, màu viền, độ dày viền, độ mờ, font, cỡ,
   căn ngang và khoảng cách đáy. Chữ Việt tại playhead xuất hiện trực tiếp trên
   mọi preview, gồm cả Timeline, và dùng đúng cùng style khi FFmpeg đóng cứng.
   Khi kéo hoặc resize cue chữ trong cảnh gần playhead tím, mép đầu/cuối tự hút
   trong vùng 8 px (tối đa 500 ms); kéo tiếp quá vùng hút sẽ đi
   xuyên qua bình thường. Cue đang nằm sát playhead vẫn kéo ra tự do, kể cả khi
   chỉnh rất nhỏ. Sau khi thả, cue giữ nguyên vị trí đến khi chính thao tác đó
   được lưu; phản hồi lưu chữ/màu không làm cue giật ngược. Cue thoại từ project
   cũ giữ đúng S1 khi chỉ đổi thời gian. Nếu không thể lưu hoặc kéo chồng lên S1
   đang có thoại, tool báo lý do và trả về vị trí đã xác nhận, không báo lưu giả.
   Thanh định dạng luôn giữ nguyên chiều cao và cue gần
   nhất, nên click vùng trống để seek không làm bảng hoặc Timeline nhảy dọc.
   `Ctrl + con lăn` zoom đúng thời điểm dưới con trỏ, `Shift + con lăn` pan ngang,
   con lăn thường cuộn các S-track; chuột giữa hoặc `Space + kéo` pan Timeline.
   Thanh dưới là overview: kéo thân để pan, kéo hai mép để đổi zoom, click vùng
   khác để đưa viewport tới đó. Các nút zoom in/out, vừa toàn bộ, vừa cue và
   **Theo playhead** có tooltip; Theo playhead bật mặc định và tự chuyển viewport
   khi video phát qua mép nhưng không giật khung trong lúc pause/chỉnh cue.
   Từ a138, clip video có dải ảnh khung hình thật, clip âm thanh có sóng âm thật
   trên nền tối. Tool chỉ đọc phần đang nhìn thấy ở nền và dùng cache; kéo/zoom
   nhanh có thể chờ một chút để ảnh mới xuất hiện, không phải ảnh minh họa giả.
   Ảnh và sóng âm bám phần nguồn đã trim/cắt, tốc độ clip và khớp giọng. Nguồn
   thiếu hoặc video không có âm thanh không được vẽ thành sóng giả.
   Dòng **T1 · Text** nằm trên V1 dành riêng cho tên phim, tên kênh hoặc người
   làm video. Bấm **T + Thêm text**: điểm bắt đầu là playhead đang dừng, nhập
   thời lượng theo giây (nhận số lẻ), chọn đến cuối hoặc toàn bộ video. Đây
   không phải thoại S1 hoặc chữ cảnh S2… và không được dịch/lồng tiếng/xuất SRT.
   Chọn khối Text rồi sửa nội dung, font, cỡ, màu, nền, fade và vị trí trong tab
   **Text**; kéo chữ trên Preview để đặt góc, kéo thân/mép khối trên Timeline để
   đổi thời điểm/thời lượng. Ctrl+click hoặc Ctrl+A khi đang chọn Text dùng cho
   các Text này; không trộn lựa chọn Text với phụ đề. Nhiều Text có thể cùng
   xuất hiện trên một dòng; khối đang chọn được đưa lên trước để vẫn kéo được.
   Delete, Ctrl+Z/Ctrl+Y và Ctrl+S/lưu-mở lại dùng chung luồng chỉnh sửa hiện có.
   Text được giữ khi trích xuất/dịch lại và vẫn xuất hiện trong Preview/MP4 khi
   tắt đóng cứng phụ đề. Khóa track chặn sửa cue; xóa cả track có xác nhận vẫn
   gỡ toàn bộ Text và có thể hoàn tác. File nguồn/cache không bị xóa.
   **Timeline → Thiết lập** chứa chất lượng xuất, kiểu chữ/nền phụ đề, filter màu,
   logo/blur và trộn âm. Các mục vẫn dùng chung dữ liệu, không cần chuyển sang
   màn Hiệu ứng hoặc Trộn âm riêng. Chọn chỉnh logo/blur để kéo khung trên
   video tại playhead; kéo khung hoặc
   nút neo để đổi vị trí/kích thước mà không cần render. Khung bám theo vùng video
   đã render, giữ trong biên và gom lệnh cuối nếu đang lưu; không còn nhảy về
   vị trí cũ khi cửa sổ thay đổi kích thước. Transport nằm dưới video để không
   che hoặc tranh chuột với logo/blur;
   bốn góc neo có vùng bắt chuột lớn và tự chọn góc phục hồi khi overlay quá nhỏ.
   **Che theo phụ đề Việt** bám thời gian và vùng chữ của bản dịch cuối cùng,
   kể cả sau khớp giọng. Chữ thoại giữ cỡ chữ đã chọn và tự xuống dòng; vùng che ôm
   chữ Việt cộng padding, không còn chạy theo box chữ Trung. Hai cue cùng thời
   điểm được che đồng thời. Project mới dùng blur mạnh với
   bán kính 46 và padding rộng hơn. Nút **Che kín** phủ thêm lớp tối 90% đúng
   vùng/timestamp chữ Việt nhưng vẫn giữ phụ đề
   Việt ở lớp trên; tắt Che kín vẫn giữ blur tự động. Có thể chỉnh padding và bán kính,
   còn **Blur thủ công** vẫn thêm/xóa, kéo, resize và đặt khoảng thời gian độc lập.
   Nền màu của thoại S1 là hình chữ nhật phẳng, có khoảng đệm hai bên và trên/dưới,
   tự co giãn theo độ dài bản dịch. Chữ thoại và chữ cảnh không tự giảm cỡ khi
   bản dịch dài; preview và bản xuất dùng cùng cách xuống dòng ở cỡ đã chọn.
   Màu chữ trắng trên nền đen luôn được tô phía trên viền; nếu màu chữ gần trùng
   nền màu (độ đục nền từ 50% trở lên), Preview/export tự chọn trắng hoặc đen dễ
   đọc hơn mà không sửa dữ liệu cue. Các màu vốn đủ tương phản vẫn giữ nguyên.
   Chữ và nền vẫn có độ trong riêng; nền mực của chữ cảnh không bị đổi thành nền thoại.
   Trong **Text → Nền mặc định cho thoại**, chọn **Không nền**, **Nền đen** hoặc
   **Nền mờ (blur)** cho các câu đang dùng thiết lập chung. **Viền phụ đề thoại**
   cũng nằm trong **Text**: chọn màu và độ dày 0–12 px, giá trị 0 tắt viền. Đây là
   thiết lập chung đã có, không thay đổi cách áp dụng. Blur tự bám được xử lý hai
   lượt cho mờ hơn; Preview và MP4 dùng cùng vùng, thời gian, độ đục và mức mờ
   tương ứng. Phóng to/thu nhỏ Preview vẫn giữ mức mờ theo tỷ lệ video.
   Nền mới dùng hình chữ nhật nhỏ hơn, có padding; chữ cảnh mới có giá trị cỡ chữ
   mặc định 40. Preview và bản xuất lấy cùng font đã đóng gói và cùng nét chữ đo
   được; nén MP4/khử răng cưa vẫn có thể làm pixel ở mép hơi khác.
   Watermark Bilibili lặp lại mà OCR đã loại khỏi SRT được hiển thị riêng là
   **CHE WATERMARK OCR** và dùng cùng blur mạnh + lớp tối 90%; trạng thái Hiệu
   ứng ghi rõ số vùng được tự che. Vùng tự động này không nằm trong danh sách
   Blur thủ công nên không thể bị kéo nhầm như một hiệu ứng do người dùng thêm.
   Project mới bật chế độ tự bám mặc định; project cũ giữ blur cố định cho tới khi
   bấm chuyển đổi. Khi kéo logo/blur, hình bám chuột cục bộ ngay và chỉ gửi một
   lệnh khi thả; bật/tắt, opacity, bán kính, thêm/xóa cũng hiện tức thì. Nếu lưu
   thất bại, tool trả về giá trị đã xác nhận gần nhất và báo nguyên nhân.
   Video nguồn không bị sửa. Với hard-sub đã cháy vào hình, Che kín chỉ làm
   chữ biến mất khỏi video xuất bằng blur và lớp phủ; nó không tái tạo chính xác
   phần nền vốn đã bị chữ gốc thay thế.
   Nếu dời chữ Việt ra xa chữ Trung, vùng che tự động cũng dời theo: thêm **Blur
   thủ công** tại vị trí chữ Trung nếu vẫn cần che vùng cũ. Preview và export dùng
   chung đường nét chữ, màu và nền mực đo theo glyph; không cần OCR/dịch lại để
   áp dụng bản sửa. Khác biệt rất nhỏ ở viền do raster hóa/nén video vẫn có thể có.
   Các preview phát video cũng dành thanh điều khiển riêng phía dưới, hiển thị
   playhead/thời lượng và mở thanh âm lượng khi rê vào biểu tượng loa. Blur/logo
   chỉ tác động lớp video; phụ đề Việt preview và ASS xuất cuối luôn nằm trên blur.
7. **Xuất video:** dùng backend `auto`, đặt âm lượng A1/D1, kiểm tra logo/blur,
   style phụ đề và filter rồi xuất. Khi xuất thành công, nút **Mở thư mục**
   mở đúng thư mục chứa MP4 vừa tạo; nút cũng có trong Thiết lập của Timeline.
   Các preset Không filter, Ấm điện ảnh, Lạnh,
   Đen trắng cùng cường độ/sáng/tương phản/bão hòa được biên dịch thành filter
   FFmpeg thật. Hàng đợi hiển thị đoạn đang render/tổng số đoạn, thời lượng đã
   xử lý/tổng, phần trăm, thời gian đã chạy, ETA còn lại và giờ dự kiến xong từ
   telemetry FFmpeg thật. Sau render, thẻ chuyển rõ qua **Ghép MP4**, **Kiểm tra
   video/audio**, **Xác minh SHA-256** và **Công bố file**; checksum hiển thị số
   GB thực đã đọc. **Tạm dừng** giữ nguyên chunk cache và có hiệu lực cả khi đang
   ghép cuối hoặc chạy SHA; probe/copy ngắn sẽ nhận lệnh ở điểm an toàn kế tiếp,
   không bị ép dừng thành job gián đoạn. Khi tiếp tục, ETA chỉ học từ tốc độ của
   pha đang chạy, không tính thời lượng cache được nạp tức thì. MP4 local không
   chạy `faststart` vì thao tác này chỉ phục vụ phát dần qua HTTP và buộc dời lại
   toàn bộ file nhiều GB; chất lượng, video/audio và khả năng phát local không
   thay đổi. Khi trộn nhiều track, export căn vị trí audio theo từng sample 48 kHz
   và giữ trần peak ở -1,5 dBFS thay vì tự nâng mix lên sát 0 dBFS. MP4 chỉ được
   công bố sau ffprobe, duration và SHA-256 QA.

   Từ a122, vùng blur/che bám cue được biên dịch thành mask vector ASS theo
   timestamp thay vì hàng trăm `drawbox` phải chạy trên mọi frame. Các clip D1
   cùng trỏ tới WAV master PCM được ghép trước thành một WAV tạm của riêng chunk,
   giữ nguyên mẫu âm và khoảng lặng rồi xóa ngay sau render. Trường hợp WAV/clip
   không đủ điều kiện an toàn sẽ tự dùng graph tương thích cũ. Thay đổi này không
   đổi preset, bitrate, chất lượng blur, subtitle, logo hoặc âm lượng đã chọn.

## Cắt video và OCR phần còn lại

Menu chuột phải trên đoạn/video gồm **Cắt, Xóa, Copy, Paste, Nhân bản, Tắt/Bật
âm thanh**. Phím tắt lần lượt là `X`, `Delete`, `Ctrl+C`, `Ctrl+V`, `Ctrl+D`.
Paste tạo V2/V3… tại playhead. Track video nhìn thấy cuối cùng đè lên V1 trong
thời gian chạy, sau đó trở lại V1; Preview, OCR và export chọn cùng video.
Âm gốc đi theo video đang hiện, còn D1/nhạc nền vẫn theo Timeline. Nhân bản đặt
ngay sau đoạn được chọn và dồn nội dung sau đó một lần. Mute là cờ riêng, nên
bật lại không mất âm lượng đã chỉnh. Ô thời gian dưới Preview cho nhập
`HH:MM:SS.mmm`; click thanh tiến trình cũng tua cùng playhead.

Các thay đổi cue/clip/hiệu ứng được đồng bộ theo thứ tự vào bản đang chỉnh trong
bộ nhớ, không ghi cả dự án xuống ổ đĩa sau từng thao tác. Nhấn **Ctrl+S** hoặc
nút **Lưu** để ghi bản mới nhất. Chỉ khi còn thay đổi chưa lưu trong **5 phút**,
tool mới tự lưu một lần. Ctrl+S lưu thành công sẽ hủy lịch đó; dự án sạch không
chạy lưu định kỳ. Thay đổi tiếp theo bắt đầu một lịch mới, không gửi chồng các
lệnh lưu đang chạy.
Ô chữ đang nhập được chốt trước khi lưu. Trước khi đổi/tạo dự án, đóng tool,
nhập media/SRT hoặc chạy OCR/dịch/TTS/export/Multi, tool đồng bộ và lưu trước.
Hoàn tác/Làm lại vẫn hoạt động trong bản chỉnh, không cần lưu từng bước.

Trạng thái **Chưa lưu** khác với **Đã lưu**; chỉ báo Đã lưu khi nhận xác nhận.
Lưu thất bại/hết thời gian chờ sẽ giữ bản chỉnh, không khoá editor và không chạy
tiếp thao tác phụ thuộc; dùng Ctrl+S để thử lại. **End Task, mất điện hoặc tiến
trình bị crash có thể mất các thay đổi từ lần lưu thành công gần nhất.**
Không có cơ chế tự xoá cache để khắc phục lỗi lưu.

OCR gom các bản đọc trùng/thiếu chữ ở cùng vùng và cùng lượt xuất hiện, ưu tiên
bản chữ Trung đầy đủ. Các mảnh chữ cùng hàng được sắp theo chiều ngang dù lệch
nhẹ toạ độ y. Hai câu thật nối tiếp chỉ hơi chồng thời gian do lấy mẫu không bị
gộp chỉ vì thời gian gần nhau; chữ cảnh, cue thêm tay và lời thoại lặp ở lượt
khác vẫn giữ riêng. Quy tắc mới áp dụng khi OCR/nhập kết quả OCR, không tự xoá
các cue của dự án đang mở hay sửa theo nghĩa bản dịch tiếng Việt.

Từ a133, crop thoại trên mặc định mở tới 98,2% chiều cao, scale 1,0 và đọc ở
chế độ thoại; crop dưới vẫn đối chiếu cùng frame. Vùng người dùng chọn không
bị mở rộng. Mảnh bị crop cắt chỉ bị loại trước khi tạo cue khi có bản đọc
trọn, độ tin cậy cao, cùng frame/đoạn nguồn/vùng chữ và được xác nhận ở frame
lân cận. Mọi mảnh của một dòng đều được xét; câu một/hai chữ, cue thêm tay và
chữ cảnh không bị bỏ chỉ vì ngắn. QA lưu confidence, evidence ID và bằng chứng
loại mảnh; trường hợp chưa đủ bằng chứng vẫn cần rà soát. Job mới không dùng
nguyên checkpoint thuộc chính sách OCR cũ; cache cũ không bị xóa.

Từ a141, đối chiếu này cũng xử lý crop cắt nét làm đọc sai nhưng vẫn giữ nguyên
số chữ, chẳng hạn `箭毒蛙` bị đọc thành `前毒蛙`. Bản trọn phải có ít nhất bằng
số chữ của mảnh và vẫn đáp ứng đầy đủ điều kiện cùng frame/vùng chữ/xác nhận;
với hai bản ngang số chữ, chỉ xét loại mảnh có độ tin cậy dưới 90%. Các bản đọc
ngang độ dài vẫn tham gia đối chiếu và chọn câu như trước; không xóa lượt đọc
sớm chỉ vì crop khác đọc được trọn hình chữ. Sau khi ghép câu, chỉ bỏ một cue
nếu mọi bằng chứng của nó đều chỉ tới đúng một cue trọn còn giữ lại, không có
bằng chứng dùng chung mơ hồ. Cue được giữ không bị đổi chữ hay bằng chứng;
thời gian có thể mở rộng để bao phủ lượt xuất hiện đó. Quét lại một cue và
nhập bản cần duyệt mới dùng cùng quy tắc chắc chắn này. Trường hợp thiếu bằng
chứng vẫn giữ cue và cảnh báo cần rà soát, không xóa để ép QA đạt.
Không gộp hai câu chỉ vì bản dịch có nghĩa giống nhau. Mở dự án cũ không tự
xóa cue đã lưu; chạy OCR lại để áp dụng quy tắc mới, rồi dịch/tạo voice lại
khi cần. Bản này không thay model, số frame hoặc chất lượng để lấy tốc độ.

Để bỏ một đoạn giữa, đặt playhead ở đầu đoạn rồi bấm biểu tượng **kéo** hoặc
phím **X**. Đặt playhead ở cuối đoạn và tách lần nữa. Bấm chọn đoạn video ở
**V1**, rồi nhấn **Delete** hoặc chuột phải → **Xóa**. Phần còn lại tự nối liền;
không cần chọn clip trước khi tách. Tách chỉ chia clip, chưa xóa nội dung.

Không còn nhóm giữ A/B, mốc A/B hoặc vùng tô chọn cắt theo phạm vi. Để bỏ đầu
hoặc cuối video, tách tại playhead rồi chọn và xóa đoạn muốn bỏ.
Video, âm thanh, D1, các S-track, logo và blur cùng theo điểm nối. Cue giao ranh
giới giữ nguyên chữ nhưng giới hạn thời gian; cắt giữa một từ vẫn có thể làm
tiếng cụt, nên hãy nghe lại điểm nối. Sau cắt video pause; dùng **Hoàn tác** để
khôi phục toàn bộ lần cắt trong phiên làm việc. **Ctrl+Z** hoàn tác và **Ctrl+Y**
làm lại thao tác dự án gần nhất: sửa chữ, định dạng, clip hoặc logo/blur dùng cùng
lịch sử theo thứ tự. Khi đang nhập văn bản, phím tắt vẫn sửa lịch sử ô nhập.
Sau khi một job nhập kết quả mới hoặc mở phiên mới, lịch sử chỉnh sửa trong phiên
trước không được dùng để ghi đè kết quả đó.

Để xóa phụ đề, chọn cue trên S1…Sn rồi bấm **Xóa**, chuột phải → **Xóa cue chữ
và nền**, hoặc **Delete**. Xóa cue không xóa đoạn video. Nút Xóa và Delete xóa
cả nhóm cue đang chọn trong một lần; chuột phải trên cue đang chọn giữ nhóm
để chỉnh tiếp, còn mục xóa cue của menu chỉ xóa cue tại đó. Khi đang nhập chữ,
X và Delete vẫn là phím sửa văn bản, không tách video hay xóa cue. Những cue
khác đang chồng thời gian không ngăn xóa cue đã chọn.

Chạy lại OCR/dịch/lồng tiếng không yêu cầu xóa dự án hay import lại video. OCR
thay bộ track phụ đề sinh tự động đồng bộ với media mới, giữ các cue thêm thủ
công. Dịch và tạo giọng thay đầu ra đang dùng nhưng không xóa file nguồn/cache;
media còn được track khác sử dụng vẫn được giữ.

Cắt không sửa file nguồn, xóa cache hoặc tự chạy OCR/dịch/TTS. Dữ liệu trước
cắt vẫn được giữ trong lịch sử revision. Mọi tác vụ OCR/dịch/TTS/export của dự
án phải hoàn tất hoặc hủy trước khi cắt; tạm dừng tác vụ chưa đủ.

Khi chủ động chạy OCR sau cắt, cả chạy đơn lẫn Multi chỉ quét các khoảng video
còn lại. Ví dụ bỏ 10 giây đầu: 00:00 trên Timeline tương ứng 00:10 của nguồn,
SRT mới dùng mốc thành phẩm. Cache toàn phim khác phạm vi không được nhập nguyên
trạng vào bản đã cắt. Preview và xuất MP4 dùng cùng thứ tự đoạn giữ lại; không
cần tạo MP4 trung gian trước OCR.

## Chạy lại và ghi đè kết quả

Một project đã có kết quả vẫn có thể chạy lại từng bước bằng nút chính của bước
đó. Giao diện ghi rõ **OCR lại · ghi đè SRT**, **Dịch lại · ghi đè bản Việt** và
**Tạo lại · ghi đè D1**. Đây là job mới; nút **Tiếp tục từ cache** chỉ dùng để
tiếp tục chính job đang dở, không được hiểu là yêu cầu tạo một bản mới.

Worker luôn ghi kết quả chạy lại vào file candidate riêng của job. Chỉ sau khi
đủ cue, QA và checksum hợp lệ, Job Manager mới thay nguyên tử file đang dùng và
công bố một project revision mới. OCR và dịch vẫn giữ kết quả đang dùng nếu job
thay thế thất bại.

Riêng khi bấm **Tạo lại · ghi đè D1**, tool tự dừng mọi preview D1 ở Phòng thu,
Timeline và Multi, rồi gỡ D1 cũ khỏi project trước khi tạo job mới. Việc này giúp
Windows nhả file WAV để bản mới có thể được ghi đè. WAV cũ vẫn còn trên đĩa;
cache cue, checkpoint, lịch sử job, video nguồn và các project revision cũ không
bị xóa. Nếu job mới thất bại, D1 cũ không tự quay lại Timeline; hãy tạo lại từ
cache hoặc mở một revision cũ nếu cần phục hồi bản trước.

Từ a121, **Tiếp tục từ cache** không cố chạy lại một job gắn với revision cũ.
Tool fingerprint riêng cue thoại theo ID, timestamp, nội dung Việt, provider,
model, voice và thiết lập TTS. Sửa font, màu, nền, chữ cảnh hoặc hiệu ứng không
làm mất cache thoại. Nếu sửa text/timestamp thoại khi TTS đang chạy, worker cũ
dừng ở checkpoint an toàn; nhiều lần sửa liên tiếp được gộp thành đúng một job
thay thế ở revision mới. Cue không đổi lấy lại WAV hợp lệ, chỉ cue đã sửa/chưa có
cache mới được tổng hợp. Giai đoạn nạp model hiển thị **Đang đo tốc độ…**; ETA
chỉ xuất hiện sau ít nhất hai batch làm nóng và dùng median batch gần nhất.

Các chỉnh sửa text/style phản hồi ngay trên màn hình rồi lưu nền bằng patch nhỏ,
coalesce trong 150 ms. Trạng thái đầu trang ghi **Đang lưu N thay đổi/Đã lưu**.
Đổi project, undo/redo, tạo job, export hoặc đóng tool sẽ chờ queue này flush để
không khóa snapshot cũ. Trong **Cài đặt → Tối ưu dữ liệu dự án**, thao tác thủ
công nén lịch sử revision nhưng giữ nguyên job/cache. Tool tạo database cạnh file
hiện tại, kiểm tra SHA-256, số hàng, foreign key và integrity trước atomic swap;
bản backup chỉ được xóa sau health-check của lần khởi động kế tiếp. Có thể bấm
**Hủy tối ưu** trong lúc đang copy/nén/VACUUM; nếu lệnh hủy tới trước bước swap,
database đang dùng vẫn nguyên vẹn.

Khi nâng từ a120/v5 lên a122, schema v6 chỉ thêm metadata cột theo cách tức thời;
tool không còn quét và ghi lại toàn bộ lịch sử revision lúc khởi động. Các revision
`json-v1` cũ vẫn mở trực tiếp và chỉ được nén khi người dùng chủ động bấm **Tối ưu
dữ liệu dự án**. Trong lúc Job Manager khởi tạo, lỗi socket tạm thời được giữ nội
bộ thay vì hiện banner đỏ; nếu backend thực sự không sẵn sàng sau thời gian chờ,
tool mới báo lỗi khởi động bằng hướng dẫn tiếng Việt.

Quan hệ phụ thuộc được áp dụng chặt để tránh dùng nhầm dữ liệu cũ:

- OCR mới thay đúng một S1 và làm bản dịch/D1 cũ hết hiệu lực.
- Bản dịch mới thay toàn bộ cue Việt và làm D1 cũ hết hiệu lực.
- Voice mới thay đúng một D1; không tạo thêm D1 trùng.

Sau OCR lại, hãy dịch lại rồi tạo lại voice. Sau dịch lại, hãy tạo lại voice.
Multi video vẫn ưu tiên bỏ qua stage có fingerprint/cache hợp lệ; muốn chủ động
làm mới một stage, mở project bằng **Chỉnh sửa chi tiết** và chạy lại tại màn
tương ứng.

## Xử lý nhiều video tuần tự

Khi mở **Multi video**, tool tự liệt kê mọi project hiện có trong cơ sở dữ liệu
đang chứa ít nhất một video, kể cả các project được tạo ở những thư mục khác
nhau. Các thẻ tự phát hiện chỉ hiển thị để theo dõi và không tự được chọn. Nút
**Nhập video** ngay trên đầu màn này mở bộ chọn chỉ nhận video và cho phép chọn
một hoặc nhiều file; các project được tạo/dùng lại từ thao tác này sẽ được thêm
vào danh sách và tự tích chọn. Nút **Khôi phục đã ẩn** đưa lại các project đã
chọn **Xóa khỏi Multi** trước đó. Xóa khỏi danh sách chỉ bỏ membership, không
xóa project, media, job, lịch sử hay cache.

- Nút **Nhập media** cho phép chọn nhiều file. Nếu chỉ chọn một file, tool giữ
  nguyên luồng dự án đơn: thêm vào project đang mở hoặc tạo project mới khi chưa
  có project. Nếu chọn từ hai video hợp lệ trở lên, tool tạo một project độc lập
  cho mỗi media chưa có, dùng lại project đang sở hữu media trùng, rồi thêm các
  project theo đúng thứ tự chọn vào màn **Multi video** và tự chuyển sang màn
  này. Audio và SRT vẫn phải nhập từng file trong luồng dự án đơn.
- Chọn từng thẻ hoặc **Chọn tất cả**, sau đó bấm **Chạy Multi video**. Checkbox
  được cập nhật ngay khi bấm và lưu bền vững ở nền; nếu lưu thất bại, tool tự
  hoàn tác rồi thông báo nguyên nhân. Danh sách và lựa chọn bị khóa trong lúc
  `Đang chạy` hoặc `Đang tạm dừng`; hãy chờ trạng
  thái **Đã tạm dừng** ở checkpoint an toàn trước khi thay đổi thành viên. Tool
  chỉ chạy một child job tại một thời điểm: project đầu tiên đi tuần tự qua
  **OCR → Dịch → Lồng tiếng**, rồi mới tới project kế tiếp theo thứ tự nhập.
- Nút chính đổi thành **Tạm dừng Multi video** khi đang chạy. Yêu cầu tạm dừng
  được phản hồi ngay trên giao diện. Worker có tối đa khoảng 1,5 giây để tự lưu
  checkpoint và xác nhận; nếu đang mắc trong một lệnh OCR/API dài, Job Manager
  ngắt riêng cây process do tool sở hữu rồi chuyển batch sang **Đã tạm dừng**.
  **Tiếp tục Multi video** chạy lại đúng job/stage từ checkpoint/cache gần nhất,
  không quay về OCR và không làm lại phần đã công bố. Nếu ứng dụng hoặc máy bị
  gián đoạn, lần mở sau chỉ đánh dấu batch là **Đã gián đoạn** và giữ cache;
  tool không tự khởi chạy lại. Kiểm
  tra media, runtime, model, API key và giọng vẫn còn hợp lệ rồi chủ động bấm
  **Tiếp tục Multi video**. Khi nâng phiên bản DUCAI, job OCR/TTS cũ dùng bộ
  điều phối được đóng gói sẽ tự chuyển sang bộ điều phối của bản đang mở, nhưng
  vẫn giữ nguyên job, fingerprint và checkpoint. Đường dẫn engine tùy chỉnh do
  người dùng chọn không bị tự thay.
- Khi bộ máy OCR hoặc VieNeu cần thời gian nạp, thẻ hiển thị **Đang khởi động bộ
  máy**; khi đọc lại ledger/WAV, thẻ hiển thị **Đang khôi phục cache**. Job
  Manager không còn tự dừng worker chỉ vì ACK khởi động đến chậm. Nếu worker
  thực sự bị gián đoạn, cả lượt chuyển sang **Đã gián đoạn** và chờ người dùng
  bấm **Tiếp tục Multi video**; thao tác này chạy lại đúng OCR/Dịch/Lồng tiếng
  đang dở từ checkpoint, không tự quay về OCR và không tạo retry giả.
- Mỗi stage dùng kết quả hoàn tất đã có chỉ khi fingerprint của input, provider,
  model và setting khớp snapshot của batch. Job chưa hoàn tất được retry trên
  đúng ledger/checkpoint thay vì tạo lại và bỏ cache. Một stage có một lần chạy
  đầu cùng tối đa năm lần thử lại ngay với backoff. Nếu vẫn lỗi, thẻ chuyển sang
  **sẽ thử lại cuối lượt** để các video còn lại tiếp tục; lượt hoãn chỉ thử thêm
  đúng một lần ở stage đã lỗi. Kết quả cuối có thể là hoàn tất toàn bộ hoặc hoàn
  tất có video lỗi, không có bước bị bỏ qua âm thầm.
- Trạng thái **OCR đã xong — có cảnh báo** là kết quả QA, không phải lỗi engine.
  Khi SRT review đã được kiểm tra và nhập hợp lệ, Multi giữ evidence cảnh báo rồi
  chuyển thẳng sang Dịch, không tiêu hao retry và không đưa video xuống cuối lượt.
  Bản cũ từng hoãn ở trạng thái này cũng được tiếp tục thẳng sang Dịch khi resume.
  Chỉ lỗi kỹ thuật như SRT/checkpoint hỏng, engine, runtime hoặc tài nguyên mới
  dùng chính sách retry ở trên.
- Mỗi thẻ hiển thị tiến độ 0–100% của riêng stage đang chạy; khi chuyển từ OCR
  sang Dịch hoặc từ Dịch sang Lồng tiếng, phần trăm hiển thị bắt đầu lại từ 0%.
  Tiến độ tổng ba stage vẫn chỉ được lưu nội bộ để resume và điều phối. Trạng
  thái thẻ gồm: hàng chờ,
  đang chạy, đang thử lại, chờ lượt thử cuối, hoàn tất hoặc lỗi. Thẻ lỗi giữ
  nguyên công đoạn, phần trăm và hướng dẫn khắc phục. Có thể **Phát video** với
  phụ đề Việt nếu đã có (nếu chưa thì dùng phụ đề Trung), hoặc **Nghe D1** khi
  đã có track voice. Cue phụ đề chỉ được tải khi bấm phát thẻ tương ứng; toàn
  màn chỉ dùng một player, nên phát thẻ mới sẽ dừng thẻ trước. Thumbnail cache
  được tải bất đồng bộ khi thẻ đi vào vùng nhìn và event tiến độ nhẹ không mang
  theo toàn bộ subtitle.
- Trước OCR/lồng tiếng và trước khi nhân bản một checkpoint lớn, tool kiểm tra
  dung lượng trống của ổ chứa dữ liệu. Nếu thiếu chỗ, thẻ báo rõ dung lượng và
  yêu cầu dọn thư mục dữ liệu DUCAI; cache/checkpoint hiện có vẫn được giữ để
  tiếp tục. Khi checkpoint `latest` mới đã lưu an toàn, snapshot kỹ thuật cũ của
  đúng key được thu hồi tự động; SRT/WAV, review đã xác nhận và thành phẩm không
  bị xóa.
- Menu `⋯` của thẻ có **Chỉnh sửa chi tiết** để mở duy nhất project đó trong
  **Timeline**; Multi không chứa công cụ timeline, logo, blur hay inspector.
  **Xóa khỏi Multi** chỉ bỏ tư cách thành viên, không xóa project, media, job,
  lịch sử hay cache. Multi chỉ tạo OCR, bản dịch và D1; nó **không tự xuất MP4**.
- Khi Multi đang hoạt động, tool chặn tác vụ OCR/dịch/TTS thủ công nếu trùng
  đúng stage đang chạy trên project hiện tại. Stage khác hoặc project khác sẽ
  hiện cảnh báo việc chạy song song có thể tranh CPU, GPU, VRAM và mạng; chỉ
  bấm **Vẫn chạy** khi đã chấp nhận tốc độ của cả hai luồng có thể giảm.

Multi dùng đúng mức **FPS OCR** đang chọn ở màn Trích xuất, thay vì tự đặt một
giá trị riêng. Mức thấp giảm số mẫu và thời gian quét nhưng có thể bỏ qua câu
hiện rất ngắn; mức cao tăng độ phủ và thời gian. Paddle xử lý hai dải phụ đề
trong một lượt gọi model khi engine hỗ trợ, còn các cửa sổ liền nhau vẫn được
giải mã tuần tự như luồng chạy đơn. Vì vậy một video trong Multi có cùng tốc độ
và độ chính xác với chạy đơn, còn nhiều video vẫn được xếp hàng tuần tự như
trước.

OCR tái sử dụng kết quả đọc của crop có byte ảnh giống hệt trong bộ nhớ giới hạn;
mỗi frame vẫn có timestamp, vùng chữ và bằng chứng riêng. Không giảm mật độ
lấy mẫu hay bỏ quét chữ ngắn để tăng tốc. Lợi ích tùy mức lặp của hình và GPU;
chữ động/thư pháp vẫn có thể tốn thời gian. Sau cắt, SRT dùng mốc Timeline;
nhập kết quả vào dự án quy đổi mốc nguồn đúng một lần cho cả PASS và Cần duyệt.
QA coverage chỉ tính trên frame có bằng chứng OCR, không phải tỷ lệ toàn bộ
thoại trong phim đã được bắt đủ.

### Chính sách QA tự động của Multi (a81)

QA OCR hợp lệ chỉ là cảnh báo và không chặn Dịch. Multi lưu cùng snapshot ba cờ
`qa_policy=multi_auto`, giữ chữ Trung/CJK là
blocker và cho phép tự rút gọn cue quá dài đúng **một lượt**. Sau lượt dịch đầu,
tool kiểm tra CJK trực tiếp trên text cuối cùng; chỉ các cue còn CJK mới được
retry theo checkpoint. Khi đã hết CJK, các cue dài được gửi rút gọn một lần;
nếu vẫn dài thì chuyển thành cảnh báo và pipeline đi tiếp sang TTS. Cảnh báo tên
riêng, Latin, phong cách, Film Bible và độ dài không làm dừng Multi. Lỗi kỹ thuật
(SRT hỏng, thiếu cache/WAV, response sai cấu trúc hoặc checkpoint không hợp lệ)
vẫn retry và chặn stage để không tạo đầu ra thiếu dữ liệu.

Khi Multi dùng Gemini Web, trình duyệt luôn chạy ẩn trong toàn bộ batch và không
có điều khiển Web nổi hay đăng nhập/khách trên giao diện.
Khi tiếp tục một batch cũ, tool cũng áp lại đầy đủ chính sách này. Nếu tác vụ
Dịch đang tạm dừng được tạo bởi bản cũ với QA nghiêm ngặt, tool dừng worker đó
ở checkpoint an toàn rồi tiếp tục chính tác vụ/cache ấy với chính sách CJK-only;
thao tác nâng cấp không bị tính là một lượt retry provider. Các chunk chưa dịch
đủ text hoặc đã có text nhưng chưa qua QA vẫn giữ trạng thái chờ khi đổi policy;
tool không đánh dấu nhầm là đã QA đạt và không bỏ mất phần cache đã hoàn thành.
Nếu một bản cũ đã tiêu hao các lượt retry vì lỗi nội bộ `QA-pass` thiếu text
cache, bản mới tự nhận diện đúng dấu vết đó, hoàn lại ngân sách retry và tiếp tục
chính job/checkpoint cũ. Lỗi provider hoặc nội dung thật không được hoàn retry.

Thẻ Multi phân biệt **Đang rút gọn cue dài**, **Đã dịch xong · còn cảnh báo**,
**Còn chữ Trung · đang thử lại lần N/5** và **Còn chữ Trung · đã bỏ qua video**.
Nếu hết retry mà vẫn còn CJK, video được hoãn tới vòng cuối rồi bỏ qua độc lập;
các video sau vẫn chạy. Job thủ công không có `qa_policy` tiếp tục semantics cũ
khi resume.

Khi một video dừng vì còn chữ Trung, menu `⋯` đổi thành **Sửa bản dịch còn chữ
Trung**. Nút này mở đúng project ở tab **Dịch thuật**, chọn đúng cache review và
hiện danh sách cue còn lỗi trong khung **QA bản dịch**. Bấm một dòng QA sẽ chọn
đúng cue và đưa danh sách tới vị trí đó; ô Việt bên cạnh có thể sửa trực tiếp.
Sau khi lưu, tool đo lại text trong project nên cue đã hết CJK biến mất khỏi danh
 sách lỗi, còn cue chưa sửa vẫn giữ nguyên. Số cue CJK trên thẻ Multi được tính
lại ngay từ nội dung đang biên tập, sau đó đối chiếu bằng trạng thái nhẹ từ Job
Manager; sửa 11/21 cue sẽ hiện còn 10, và sửa hết sẽ đổi thẻ sang xanh. Quay lại
Multi rồi bấm **Chạy lại video lỗi** hoặc **Tiếp tục Multi video**: nếu toàn bộ
SRT review đủ cue và không còn
chữ Trung, bước Dịch được đánh dấu hoàn tất thủ công và Multi chuyển thẳng sang
**Lồng tiếng**, không gọi Gemini lại. Bản dịch thiếu cue, sai timestamp hoặc lỗi
kỹ thuật vẫn không được bỏ qua.

Project lớn khi mở từ menu **Chỉnh sửa chi tiết** được Timeline hiển thị ngay
với lớp **Đang tải video vào Timeline…**. Job Manager nén snapshot và gửi checksum;
nếu nén vẫn vượt giới hạn IPC, snapshot nằm trong file staging tạm chỉ đọc trong
artifact root. Client giải nén, kiểm tra kích thước/SHA-256 ở worker nền rồi mới
thay model một lần; response cũ hoặc lỗi tải không thể ghi đè project đang mở.

## Điều hướng và mở dự án

Thứ tự menu: **Tổng quan → Dự án → Timeline → Trích xuất → Dịch thuật → Biên
tập phụ đề → Phòng thu giọng → Multi video → Xuất video → Hàng đợi → Cài đặt**.
Nhập media vẫn có trên thanh chính; các mục Hiệu ứng/Trộn âm chuyển vào
**Timeline → Thiết lập**. Tab Xuất video chỉ còn thành phẩm và tiến trình:
góc dưới phải có **Xuất video / Tạm dừng / Tiếp tục** theo trạng thái, cùng
**Hủy** để dừng hoàn toàn. Muốn thay thiết lập hoặc xuất revision mới, chỉnh
ở Timeline rồi tạo lượt xuất mới; tiếp tục job đã pause vẫn dùng snapshot cũ.

- **Dự án** là màn riêng: chọn **Mở dự án** để nạp snapshot đã lưu, hoặc
  **Tạo từ media** để luôn tạo một project mới, kể cả khi project khác đang mở.
  Với **Nhập media** trên thanh chính, một file đi vào project hiện tại (hoặc
  tạo project khi chưa mở dự án), còn từ hai video trở lên đi vào **Multi
  video**. Các thao tác này dùng hợp đồng riêng để tránh nối nhầm nhiều phim
  vào cuối một timeline.
- Khi media đã thuộc dự án khác, hộp xác nhận cho phép mở lại dự án có sẵn,
  tạo một dự án độc lập với tên mới, hoặc bấm `X` để đóng mà không thay đổi DB.
- Nút **Xóa** trong danh sách dự án yêu cầu xác nhận. X/Escape/Hủy là thao tác
  không làm gì; chỉ job, revision, cache và artifact nội bộ bị xóa. Tool từ chối
  xóa khi dự án còn job đang hoạt động và không xóa video/SRT nguồn. Nếu artifact
  nội bộ đang bị khóa hoặc không thể dọn sạch, thao tác dừng và vẫn giữ bản ghi
  dự án để người dùng có thể thử lại thay vì báo thành công giả.
- Mở dự án thành công sẽ tự chuyển sang **Timeline** để xem ngay media và playhead.
- Lịch sử trong màn **Biên tập phụ đề** chỉ thuộc project đang mở. Có thể xóa
  từng OCR/dịch ở mọi trạng thái. Nếu job còn chạy, tool gửi Hủy, chờ đúng worker
  dừng rồi mới tự xóa hàng và artifact của job đó; cache vẫn nguyên vẹn trong lúc
  đang chờ dừng. **Xóa toàn bộ lịch sử** áp dụng cả job dở và cũng xóa mọi cache
  dịch tốt của project, vì vậy hãy dùng nút X từng hàng khi cần giữ một bản dịch
  hoàn chỉnh. Checkpoint bất biến được giữ read-only khi làm việc và chỉ được Job
  Manager gỡ thuộc tính này trên đúng thư mục job đã xác minh trong lúc xoá. Khi
  **Phòng thu giọng** đang mở, lịch sử chỉ hiện job TTS của project đó và cũng cho
  xóa từng hàng hoặc toàn bộ; thao tác này không xóa global WAV cache dùng lại.
  Khi xóa project, hàng và trạng
  thái hiển thị ngay `Đang xoá: <tên>` cho tới khi Job Manager trả kết quả.
- Ô **Cache để tiếp tục** chỉ liệt kê ledger có fingerprint khớp chính xác phụ đề
   nguồn hiện tại. Cache có nhiều cue hoàn chỉnh hơn được xếp trước; nếu bằng nhau,
   cache ít blocker hơn được ưu tiên. Cache được chọn không bị một job mới ít cue
   che mất, và chỉ cache đó trở thành nguồn của nút tiếp tục khi không còn job dịch
   đang hoạt động. Số cache tương thích được hiển thị cạnh ô chọn. Cache ở trạng
   thái đã hủy vẫn giữ ledger và có thể tiếp tục; một cache cũ/hỏng hoặc sai
   fingerprint bị loại riêng, không làm ẩn các cache phù hợp khác. Mỗi project chỉ
   được có một job dịch hoạt động tại một thời điểm.
- Nút tác vụ dùng trạng thái có ngữ nghĩa (đang chờ, đang chạy, hoàn tất, lỗi)
  với màu, chữ và trạng thái trợ năng riêng. Nút OCR chính đổi giữa `Chạy OCR`,
  `Tạm dừng OCR` và `Tiếp tục OCR`; các nút khác đổi ngay sang `Đang dịch…`,
  `Đang dịch…`, `Đang tạo giọng…` hoặc `Đang xuất video…` mà không phụ thuộc thời
  điểm worker bắt đầu. Nút tạm dừng dùng cảnh báo vàng, nút hủy dùng nền đỏ/chữ
  vàng. Mọi nút tác vụ hiện toast trượt từ chính giữa mép trên để xác nhận thao
  tác đã được nhận. Thông báo lỗi và hoàn tất giữ tối đa 10 giây (có nút `×`
  để đóng ngay), còn cảnh báo giữ tối đa 8 giây. Mỗi thẻ hiển thị tên công đoạn tiếng
  Việt, tiến độ và ETA của chính công đoạn hiện tại; job đã kết thúc không giữ ETA
  cũ của lần đo trước. Khi Gemini đang xử lý một batch, số cue chỉ tăng sau khi
  nhận và lưu đủ response, nhưng đồng hồ vẫn chạy mỗi giây và thẻ hiển thị lượt
  batch hiện tại. ETA sau khi tiếp tục chỉ dùng tốc độ mới của phiên resume, không
  lấy hàng nghìn cue cache chia cho vài mili-giây rồi báo thời gian sai.
- Khi worker kết thúc lỗi, bị gián đoạn hoặc dừng ở QA, toast ghi rõ công đoạn,
  nguyên nhân bằng tiếng Việt và khả năng khôi phục; mã kỹ thuật chỉ được giữ
  trong lịch sử/log để chẩn đoán. Với TTS, thông báo nêu số cue timing vượt
  giới hạn; với dịch/OCR, toast chỉ rõ QA còn blocker. Cache/checkpoint không
  bị xóa và thông báo hướng dẫn nút tiếp tục hoặc mở đúng màn sửa. Lỗi IPC/API
  cũng được diễn giải thành hướng dẫn tiếng Việt thay vì chỉ ghi `Thất bại`;
  các thông báo nền không làm thay đổi nội dung bản dịch.
- Bấm đúp một cue voice trên **Timeline** sẽ chọn timestamp chuẩn của cue, seek
  video và mở đúng dòng trong **Biên tập dịch thuật** để sửa; cue aggregate khi
  đang zoom xa không được mở nhầm thành một cue cụ thể.
- Nếu worker đã ráp đủ WAV nhưng tác vụ lỗi đúng lúc nhập D1, bấm **Tiếp tục từ
  cache**. Tool lấy timing policy từ cấu hình/QA của lần chạy, xếp lại các cue
  tuần tự và chỉ ráp/kiểm tra/publish D1; nó không gọi lại VieNeu. Lỗi timestamp
  thật sẽ nêu số cue, khoảng thời gian sai và hành động cần làm bằng tiếng Việt.
- Thêm logo, thêm/xóa blur hoặc chỉnh timeline trong lúc dịch chỉ thay snapshot
  dự án. Preview tiếng Việt đang checkpoint được giữ nguyên và tự nạp lại từ
  ledger; thao tác hiệu ứng không thể làm trắng cột bản dịch hoặc làm rơi lệnh
  chọn logo đang chờ.
- **Trích xuất**, **Dịch thuật** và **Biên tập phụ đề** dùng chung dữ liệu cue
  nhưng mở đúng bộ điều khiển của từng công đoạn.
- SRT chỉ biểu diễn timestamp đến mili-giây. Khi nhập bản dịch, tool kiểm tra theo
  đúng độ phân giải đó rồi khôi phục timestamp micro-giây của cue nguồn; cue thủ
  công không còn bị báo sai timing chỉ vì phần lẻ nhỏ hơn một mili-giây.
- Trong **Biên tập phụ đề**, **Tìm / thay** chỉ tác động cột bản dịch Việt.
  Tool đếm chính xác số lần khớp, hỏi xác nhận rồi mới thay; khoảng trắng đầu/cuối
  trong ô thay thế được giữ nguyên. Chỉnh text hiển thị ngay trong danh sách và
  được lưu nền; nếu lưu lỗi, nội dung tự hoàn nguyên. Click phải một dòng để
  **Xóa cue này**; thao tác có xác nhận, gỡ đồng thời cue nguồn và cue Việt để
  không lệch timestamp, đồng thời hỗ trợ Hoàn tác. Click phải trên cả vùng chữ
  nguồn/bản dịch cũng mở đúng menu cue (không mở menu soạn thảo mặc định).
  File SRT, media và cache không bị xóa. Bảng **QA bản dịch** xuất hiện ở cả Dịch thuật và Biên tập phụ đề;
  khi sửa hết chữ Hán/CJK, cảnh báo đỏ và đánh dấu cue được gỡ ngay. Evidence QA
  cũ từ job không được phép ghi đè lại một cue Việt đã sửa hợp lệ.
- Trên **Timeline**, bấm đầu dòng `S1/S2…` hoặc `T1…` chọn toàn bộ cue chữ của
  dòng đó để sửa cỡ, hướng, nền và các kiểu chữ trong tab **Text**. Giữ Ctrl
  để thêm các dòng cùng loại vào nhóm; dòng đã khóa phải mở khóa trước khi sửa.
  Bôi chọn hoặc chọn đầu dòng có chữ cảnh, rồi chuột phải → **Chấp nhận chữ
  cảnh đã chọn** để duyệt cả nhóm; thao tác không duyệt nhầm thoại và có thể
  hoàn tác một lần. Đầu dòng `V1/A1/D1…` vẫn chọn toàn bộ track. Click
  một clip/cue trong phần nội dung sẽ bỏ lựa chọn track trước đó và chuyển về
  điều khiển riêng clip, nên speed, volume và kéo mép không bị khoá sau khi đã
  dùng Chọn tất cả. Với phụ đề, lựa chọn được giữ theo cue ID ổn định nên editor
  không thể lưu nội dung đang gõ sang cue khác khi model sắp xếp lại.
- **Thiết lập** trong Timeline, **Xuất video** và **Hàng đợi** dùng chung cấu hình
  export; Hàng đợi có màn theo dõi progress/elapsed/ETA riêng.
- FileDialog chuyển URL sang đường dẫn Windows ở lớp Python, nên tên file tiếng
  Việt có dấu và khoảng trắng được giữ nguyên. Khi chạy source ở chế độ phát
  triển, ký tự `?` trong console chỉ phản ánh codepage, không phải tên file trên
  ổ đĩa.

## Tạm dừng, tiếp tục và lỗi ngoài dịch vụ

Chuyển tab không dừng OCR, dịch, TTS hoặc export. Nút **Tạm dừng** chờ worker
xác nhận rồi giữ checkpoint; **Tiếp tục** chỉ dùng cache có cùng fingerprint
nguồn, engine, model và setting. CAPTCHA, đăng nhập hết hạn và quota xác minh là
những trường hợp cần người dùng xử lý; tool không tự nhập tài khoản hay lách xác
minh.

Nếu người dùng **Hủy** trong lúc provider đang chạy một batch chặn lâu hơn thời
gian dừng mềm, Job Manager có thể kết thúc worker sau checkpoint. Trạng thái cuối
vẫn là **Đã hủy**, không bị báo nhầm thành lỗi worker; raw/ledger đã checkpoint
được giữ để job mới tiếp tục dùng.

Chuyển tab, cuộn danh sách hay xem preview không làm dừng dịch. Chỉ **Tạm dừng**
hoặc **Hủy** gửi lệnh điều khiển tới worker. Nhiều key API là pool failover tuần
tự, không phải dịch đồng thời nhiều chunk; các key thuộc cùng một Google Cloud
project vẫn có thể dùng chung quota.

## Nhập dự án 0.1.99

Importer một chiều luôn chạy dry-run trước. Nó đọc JSON cũ, media/SRT/WAV,
caption, blur/logo và các setting được cho phép; không sửa file cũ. Credential
luôn bị bỏ qua. Cache chỉ được nhận khi có `identity.json` và toàn bộ fingerprint
khớp hợp đồng mới; các cache không chứng minh được danh tính sẽ được báo là
`skipped` thay vì sao chép mù.

## Bản portable để chuyển sang máy khác

Gói `DUCAI-STUDIO-<version>-PORTABLE-win64` đặt `DUCAI_STUDIO_DATA` ngay cạnh
`DucaiStudio2.exe`. Tool tự nhận runtime PaddleOCR, Piper, VieNeu, model offline
và `bin/ffmpeg.exe`; không cần sửa đường dẫn theo tên tài khoản Windows của máy
đóng gói. Launcher và DLL Python của từng runtime được lấy từ Python home đi kèm;
tool sẽ tự đồng bộ lại các file này sau khi chuyển thư mục. Hãy giải nén toàn bộ
gói vào thư mục có quyền ghi rồi mới chạy, không chép riêng file EXE.

Người dùng mới tải **bản PORTABLE đầy đủ** trên GitHub Releases. File
`win64-update.zip` dành cho cập nhật ứng dụng; nó không bao gồm các runtime,
giọng và model để cài trên máy mới. Nếu bản portable được chia thành
`.7z.001`, `.7z.002`…, tải tất cả các phần vào cùng một thư mục, dùng 7-Zip
giải nén từ phần `.001`, rồi mở `DucaiStudio2.exe` trong thư mục vừa giải nén.
Không giải nén mỗi phần riêng lẻ. Kiểm tra file hướng dẫn của đúng release để
biết số phần, dung lượng cần trống và thành phần Whisper có được kèm hay không.
Với bản a138, chọn đường dẫn không có dấu tiếng Việt, ví dụ `D:\DUCAI`:
thử nghiệm bản EXE đã phát hiện lỗi nạp font khi đường dẫn chứa tiếng Việt.

Nén 7z là nén **không mất dữ liệu**: OCR, model và giọng đọc sau giải nén giữ
nguyên chất lượng. Mức nén cao chỉ giảm file tải; không giảm dung lượng đã cài
và có thể làm thời gian nén/giải nén dài hơn. Người đóng gói có thể dùng
`build-portable-release.ps1 -SkipBuild -IncludeWhisper -CreateArchive
-ArchiveCompressionLevel 9 -ArchiveVolumeSize 1900m` để tạo bộ đầy đủ nén mạnh;
mặc định vẫn là mức 3. Mức nén tùy chọn yêu cầu 7-Zip, không chuyển âm thầm sang
tar.xz. Bộ đầy đủ vẫn cần khoảng 20 GB sau giải nén; dành thêm dung lượng cho
file tải và dữ liệu dự án. Với bản có khóa kích hoạt, người mới mở **Cài đặt >
Kích hoạt** để gửi mã máy và nhập key được cấp trước khi xử lý video. Gói đầy đủ
không thay thế key kích hoạt, API/account dịch hoặc driver GPU tương thích.

Whisper không nằm trong gói portable mặc định để tránh tăng thêm vài GB. Chỉ bản
được tạo bằng `build-portable-release.ps1 -IncludeWhisper` mới chứa runtime và
model `large-v3`; kiểm tra `whisper_included=true` trong manifest trước khi gửi.

Gói phát hành không chứa project, video, SRT, cache voice, cookie Gemini, API key,
profile đăng nhập hoặc profile clone giọng. Người nhận phải tự nhập key và tự tạo
giọng clone nếu dùng VieNeu. Gemini Web vẫn cần Chrome hoặc Edge trên máy nhận.
PaddleOCR GPU và VieNeu cần GPU NVIDIA cùng driver tương thích để đạt hiệu năng
như máy đóng gói; chuyển sang CPU không phải là chế độ tối ưu của gói này.

Runtime clone được cài/sửa bằng `scripts/setup-vieneu-runtime.ps1`. Script ghim
VieNeu 3.8.1 cùng PyTorch/Torchaudio CUDA 12.8 và dừng ngay nếu máy không nhận
CUDA. Job VieNeu 3.3 đang dang dở không được tiếp tục bằng 3.8.1 để tránh trộn
giọng từ hai engine; hãy tạo tác vụ voice mới, còn project/profile/cache cũ vẫn
được giữ.

Kiểm tra VieNeu cho phép tối đa 90 giây để nạp PyTorch/CUDA khi máy khởi động
nguội. Hết thời gian kiểm tra không đồng nghĩa runtime bị hỏng; thông báo sẽ
phân biệt trường hợp này và bạn có thể bấm Tiếp tục khi máy bớt tải. Kết quả
kiểm tra thất bại tạm thời không bị lưu cache; kiểm tra thành công được tái dùng
khi runtime/profile không đổi. Không cần cài lại model chỉ vì kiểm tra hết giờ.

Từ a104, tiến độ lồng tiếng phân biệt kiểm tra cache, kiểm tra bộ máy, nạp model,
chuẩn bị giọng và tổng hợp. Số câu chỉ tăng khi đã xử lý thật; một batch có thể
gồm 32 câu nên không phải cứ chưa tăng phần trăm là tác vụ đứng. Tiến độ cũ được
gộp để tránh giao diện chậm hơn worker, nhưng checkpoint và các câu đã làm vẫn
được giữ nguyên.

Sau khi một tác vụ VieNeu hoàn thành, tool giữ model tối đa 2 phút để job tiếp
theo bắt đầu nhanh hơn, vẫn gắn lại đúng giọng đã chọn. Model được giải phóng
khi hết thời gian chờ, khi chạy OCR hoặc khi đóng tool; job bị lỗi/hủy không để
lại model dùng dở. Lượt đầu sau khi mở tool vẫn cần nạp model. Cơ chế này không
đổi chất lượng, tốc độ đọc hoặc cache giọng.

Trong Trích xuất/Dịch thuật/Biên tập, phần đầu chỉ hiển thị tác vụ và điều khiển
cần thiết. Tên engine Paddle, mô tả pipeline, note kỹ thuật, nút Web nổi và chế
độ đăng nhập/khách không còn xuất hiện; Gemini Web chạy ẩn với thiết lập an toàn
mặc định.

## Cổng chất lượng phim dài

- Hard-sub coverage phải được đo bằng sample frame/gap evidence và đạt ít nhất
  95%. Hai band tại cùng timestamp chỉ tính một lần; cue count đơn thuần không
  đủ chứng minh. QA v2 còn từ chối khoảng trống evidence bất thường trên timeline
  để tránh trường hợp OCR chỉ bắt được một phần phim nhưng vẫn báo PASS; Job
  Manager từ chối SRT thiếu QA khớp hash.
- SRT Việt phải giữ đúng số cue/ID/timestamp nguồn, không cue rỗng và không còn
  blocker chữ Hán, sai ngữ cảnh, tên riêng hoặc câu quá dài.
- Voice report phải cho biết eligible/approved/blocked, cache hit và thời gian.
- MP4 cuối phải có video + audio, duration đúng snapshot và checksum trùng báo
  cáo QA.
