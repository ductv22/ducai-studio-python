# Hướng dẫn DUCAI STUDIO 2

## Trước khi bắt đầu

Mở **Cài đặt → Chẩn đoán runtime** và chỉ chạy phim khi các mục bắt buộc đều
`ready`: Python GUI 3.12, FFmpeg/ffprobe, PaddleOCR, một transport dịch (Gemini,
DeepSeek, Hy3/TokenHub, Qwen, GLM hoặc API tương thích), Piper và VieNeu 3.8 nếu
dùng clone. NVENC có thể ở trạng thái không khả dụng; tool sẽ dùng CPU khi
backend là `auto`.

Model, cache và giọng không nằm trong EXE. Chúng ở `DUCAI_STUDIO_DATA`; có thể
đổi gốc bằng biến `DUCAI_STUDIO_DATA_ROOT` mà không build lại GUI.

Bản phát hành Windows nằm trong thư mục `outputs/DUCAI-STUDIO-<version>-win64`.
Mở `DucaiStudio2.exe` trực tiếp; bản production dùng GUI subsystem nên không mở
thêm cửa sổ CMD.

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

Giới hạn bootstrap: a117 và các bản cũ chưa có updater nên phải cài a118 thủ
công đúng một lần. Từ a118 trở đi mới có thể nâng cấp ngay trong tool. Bản
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
3. **QA SRT Trung:** `_ZH_FINAL.srt` chỉ xuất khi PASS, không cue rỗng, không
   overlap và ít nhất 95% timestamp có chữ được cue cuối che phủ đúng nội dung.
   File `_ZH_FINAL_QA.json` phải khớp job, fingerprint và SHA-256 của SRT;
   REVIEW được giữ cùng QA bất biến, không thể đổi tên để lách gate.
   Khi QA dừng ở **Cần rà soát**, nút **Xử lý lại từ cache** chạy lại hợp nhất,
   finalization và QA trên checkpoint cùng fingerprint; không quét lại toàn bộ
   video chỉ để áp dụng bản sửa pipeline mới.
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
   Lượt sparse toàn khung nhận tên nhân vật, chiêu thức, pháp bảo và địa danh:
   hội thoại vẫn ở **S1**. Các chữ trong cảnh xuất hiện đồng thời được tách tự
   động sang **S2, S3, S4… · Chữ trong cảnh**; cue không trùng thời gian tái dùng
   lane thấp nhất. Người dùng có thể kéo một chữ cảnh lên S1 khi khoảng thời gian
   đó không có cue khác; đây là thao tác chủ động đổi cue thành **thoại chính**, vì
   vậy cue sẽ đi vào SRT thoại, TTS và D1 ở lần tạo tiếp theo. Kéo một cue thoại
   xuống S2/S3… chỉ đổi vị trí hiển thị, không làm mất vai trò thoại và vẫn được
   lồng tiếng. Nếu đè cue đang có trên lane đích, tool tự tìm lane trống hoặc báo
   rõ lý do khi S1 không thể nhận cue.
   Chữ cảnh đã dịch xuất hiện trực tiếp theo playhead trong Timeline Preview, không cần
    xuất video trước. Lượt OCR toàn khung đối chiếu chữ, timestamp và vùng ảnh với
    S1 để không đưa lại hội thoại thường vào S2.
   Các cách đọc OCR gần giống nhau ở cùng vùng được gom trước khi quyết định;
   cluster phải có ít nhất một frame rõ. Một thẻ cảnh chỉ xuất hiện đúng một
   frame vẫn được chấp nhận khi confidence/độ tin cậy hình ảnh đặc biệt cao và
   không nằm ở góc watermark.
   Cue S2 được dịch cùng Film Bible nhưng không vào SRT hội thoại, VieNeu hoặc D1.
   Khi cue S2 đã được chấp nhận, preview/export chỉ làm mờ-phủ vùng glyph Trung,
   giữ panel/viền trang trí rồi tự đặt chữ Việt ngang hoặc dọc vào panel. Nguồn
   được nhận diện là thư pháp dùng font Playwrite VN có dấu; cue chưa đủ chắc chỉ
   hiện **Cần duyệt** và không tự ghi vào video. Sau khi sửa đúng chữ Việt, nhấp
   phải cue S2 rồi chọn **Chấp nhận chữ trong cảnh** để cho phép preview/export.
   Chọn một cue trên S1/S2/S3… để mở bảng chữ gọn kiểu trình soạn thảo. Click
   thường luôn thu lựa chọn về đúng một cue; chỉ Ctrl+click mới cộng hoặc bỏ từng
   cue khỏi nhóm. Việc focus hoặc đúp chuột vào ô nguồn/Việt không xóa nội dung
   đang có. Bảng này
   sửa nội dung tiếng Việt, cỡ chữ từ 1–100, hướng ngang/dọc, kiểu chữ, màu chữ và
   màu nền. Cỡ chữ là giá trị chủ động: kéo chữ lên cao hoặc thu khung đặt không
   tự làm chữ nhỏ lại. Chữ trong cảnh có thêm **Nền ngang/Nền dọc** để nền ôm
   theo chữ thay vì phủ cả khung kéo. Các font thư pháp Playwrite VN, Dancing
   Script, Lobster, Agbalumo, Comforter Brush, Water Brush, Splash và Hurricane
   đều có dấu tiếng Việt và được đóng gói cùng bản phát hành. Bốn font bổ sung là
   các họ OFL có nét gần những mẫu thư pháp Việt; tool hiển thị tên thật thay vì
   giả danh các font VNI/HL/UVN/SVN không có giấy phép phân phối rõ ràng.
   **Preset mực tàu** tạo nền mực bất quy tắc dưới chữ. Danh sách nền cho phép
   chọn **Mực loang**, **Mực đại mặc**, **Mực tụ** hoặc **Nét cọ**. Hai đầu cọ giữ
   tỷ lệ theo bề dày, chỉ thân giữa co giãn nên nền không bị kéo méo với chữ ngắn
    hoặc dài; **Nền ngang/Nền dọc** tiếp tục điều chỉnh kích thước bao quanh chữ.
    Chữ trong cảnh mới, gồm cue OCR và cue thêm bằng nút **T**, mặc định dùng
    **Bút lông Comforter**, chữ trắng, nền đen **Mực loang** với độ đục 100%; cue cũ
    giữ nguyên định dạng đã lưu. Các nút định dạng và bộ nhập số hiển thị mô tả khi
    trỏ chuột; dấu giảm, giá trị và dấu tăng luôn được tách thành ba vùng rõ ràng.
   Cue/project cũ vẫn giữ nền phẳng cho tới khi người dùng tự chọn. Từng cue còn chỉnh
   được giãn chữ, đậm/nghiêng và độ đục riêng của chữ/nền. Chữ trong cảnh có thêm
   **Mờ vào/ra**: đặt thời lượng 50–2.000 ms cho mỗi đầu; mặc định 300 ms và cue
   ngắn tự kẹp tối đa nửa thời lượng để không biến mất hoàn toàn. Preview và video
   xuất dùng cùng hiệu ứng, còn vùng che chữ Trung luôn phủ đầy đủ để chữ gốc không
   ló lại trong lúc fade. Hai nút **Ống hút**
   lấy trực tiếp màu chữ hoặc màu nền từ frame đang thấy trên Preview; nhấp phải
   khi đang lấy màu để hủy.
   Preview và bản xuất dùng cùng giá trị đã lưu. Click, Ctrl+click, kéo
   vùng quét hoặc Ctrl+A chọn nhiều cue để chỉnh/kéo theo nhóm. Kéo ngang đổi thời
   gian, kéo dọc chuyển lane; nếu lane đích đang bận, tool dùng lane trống kế tiếp.
   Trên Preview, cue đã chọn có thể kéo vị trí và resize độc lập theo khung video.
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
   xuống dòng trong Biên tập phụ đề.
   Lỗi 1095/1905/about:blank chỉ được nhận từ bề mặt lỗi đã xác minh; mã cue như
   `id:1095` trong JSON không bị coi là lỗi. Phiên sẽ đóng, backoff có giới hạn
   và gửi lại đúng chunk chưa xong. Với API, chọn **Gemini API**, **DeepSeek API**,
   **Hy3 · TokenHub**, **Qwen API**, **GLM API** hoặc **OpenAI-compatible**. Các
   preset tự điền base URL/model; model vẫn gõ tay được. OpenAI-compatible yêu cầu
   base URL HTTPS (HTTP chỉ cho localhost). Dán nhiều dòng `tên=API_KEY` (hoặc
   phân cách bằng dấu `;`) vào pool của đúng provider. Key chỉ nằm trong Windows
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
   gián đoạn hoặc cần rà soát. Tool chỉ retry đúng job/ledger đã chọn khi provider
   và model trên giao diện vẫn khớp job đó; đổi provider hoặc model sẽ tạo job mới
   để không chạy nhầm transport cũ. Các cue đã checkpoint không bị gửi lại.
   Khi mở tool, mỗi pool chỉ giữ các tên key đã gán cho đúng provider và vẫn còn
   trong Windows Credential Manager; tên key chưa gán không tự bị nhận làm Gemini.
   Nếu dán key mới trong lúc tiếp tục cùng job API, tool cập nhật pool cho chính
   job đó rồi retry từ checkpoint hiện có. Có thể dùng **Nhập SRT Việt** để nạp bản dịch từ bên ngoài; tool
   chỉ ghi vào dự án khi số cue, index và toàn bộ timestamp khớp SRT Trung.
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
   Piper là lựa chọn nhanh. VieNeu-TTS v3 Turbo 3.8.1 dùng profile
   clone 192D và reference codes, ưu tiên chất lượng. VieNeu gửi tối đa 32 cue
   trong một batch, tự hạ batch nếu GPU hết VRAM và giữ host/model nóng. Đường
   fused CUDA Graph do chính VieNeu 3.8.1 cung cấp; tool không còn vá API nội bộ
   của runtime. Profile 3.3 được giữ nguyên trên đĩa và chỉ bỏ frame pad cũ trong
   RAM khi bind vào engine mới.
   QA/fit vẫn chạy riêng từng cue. **Tốc độ giọng gốc** áp dụng cho toàn bộ D1,
   chọn từ 1,10× đến 3,00× và mặc định 1,30× cho job mới. VieNeu vẫn inference ở
   1,00×; tool tạo WAV nhanh hơn bằng FFmpeg giữ cao độ từ WAV cache nền. Đổi tốc
   độ không gọi lại model và mỗi tốc độ có cache variant riêng, không stretch nối
   tiếp từ variant cũ. Job cũ thiếu setting này vẫn tiếp tục ở 1,00×.

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
   lại; `Space` hoặc click vùng video trống phát/tạm dừng tức thì, còn `←/→` và
   `−5s/+5s` tua đúng 5 giây. Các phím này không chiếm thao tác khi đang gõ trong ô
   chữ. Ô tốc độ và âm lượng clip nhận cả dấu chấm lẫn dấu phẩy thập phân. Bấm cue nguồn hoặc
   bản dịch sẽ chọn cue và đưa video/playhead tới đầu cue. Bấm một vị trí trống
   hoặc bấm trực tiếp lên clip trong Timeline sẽ tua Preview tới đúng timestamp
   dưới con trỏ; kéo clip, kéo mép và quét chọn vẫn giữ chức năng riêng, không tua
   nhầm. Thanh điều khiển Preview chỉ hiện khi con trỏ nằm trên Preview và ẩn ngay
   khi rời ra, kể cả lúc video đang pause. Timeline có tab
   **Phụ đề** riêng để xem và bấm trực tiếp cả chữ nguồn lẫn bản Việt. Tab
   **Kiểu sub** cho chọn bảng màu chữ, màu viền, độ dày viền, độ mờ, font, cỡ,
   căn ngang và khoảng cách đáy. Chữ Việt tại playhead xuất hiện trực tiếp trên
   mọi preview, gồm cả Timeline, và dùng đúng cùng style khi FFmpeg đóng cứng.
   Khi kéo hoặc resize cue chữ trong cảnh gần playhead tím, mép đầu/cuối tự hút
   trong vùng 8 px (tối thiểu 80 ms, tối đa 500 ms); kéo tiếp quá vùng hút sẽ đi
   xuyên qua bình thường. Thanh định dạng luôn giữ nguyên chiều cao và cue gần
   nhất, nên click vùng trống để seek không làm bảng hoặc Timeline nhảy dọc.
   Màn **Hiệu ứng** hiển thị logo, nhiều vùng blur và filter màu trực tiếp trên
   video tại playhead; kéo khung hoặc
   nút neo để đổi vị trí/kích thước mà không cần render. Khung bám theo vùng video
   đã render, giữ trong biên và gom lệnh cuối nếu đang lưu; không còn nhảy về
   vị trí cũ khi cửa sổ thay đổi kích thước. Riêng màn **Hiệu ứng** dùng canvas
   tĩnh toàn khung và ẩn transport để không che hoặc tranh chuột với logo/blur;
   bốn góc neo có vùng bắt chuột lớn và tự chọn góc phục hồi khi overlay quá nhỏ.
   **Blur theo phụ đề nguồn** bám timestamp SRT và box chữ Trung do OCR ghi lại;
   nếu cue không có box, preview ghi rõ **Ước lượng** và tính vùng từ dòng chữ.
   Hai cue cùng thời điểm được che đồng thời. Project mới dùng blur mạnh với
   bán kính 46 và padding rộng hơn. Nút **Che kín** phủ thêm lớp tối 90% đúng
   vùng/timestamp chữ gốc để nét chữ không còn đọc được nhưng vẫn giữ phụ đề
   Việt ở lớp trên; tắt Che kín vẫn giữ blur tự động. Có thể chỉnh padding và bán kính,
   còn **Blur thủ công** vẫn thêm/xóa, kéo, resize và đặt khoảng thời gian độc lập.
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
   Các preview khác dùng thanh điều khiển trong suốt như YouTube: mặc định ẩn,
   hiện khi con trỏ nằm trên video, bắt đầu mờ đi ngay khi rời video, hiển thị
   playhead/thời lượng và mở thanh âm lượng khi rê vào biểu tượng loa. Blur/logo
   chỉ tác động lớp video; phụ đề Việt preview và ASS xuất cuối luôn nằm trên blur.
7. **Xuất video:** dùng backend `auto`, đặt âm lượng A1/D1, kiểm tra logo/blur,
   style phụ đề và filter rồi xuất. Các preset Không filter, Ấm điện ảnh, Lạnh,
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
- Trên **Timeline**, chỉ vùng đầu dòng `V1/A1/D1/S1…` chọn toàn bộ track. Click
  một clip/cue trong phần nội dung sẽ bỏ lựa chọn track trước đó và chuyển về
  điều khiển riêng clip, nên speed, volume và kéo mép không bị khoá sau khi đã
  dùng Chọn tất cả. Với phụ đề, lựa chọn được giữ theo cue ID ổn định nên editor
  không thể lưu nội dung đang gõ sang cue khác khi model sắp xếp lại.
- **Hiệu ứng**, **Trộn âm**, **Xuất video** và **Hàng đợi** dùng chung snapshot
  export nhưng đưa đúng khu vực đang chọn vào trọng tâm; Hàng đợi có màn theo
  dõi progress/elapsed/ETA riêng.
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
