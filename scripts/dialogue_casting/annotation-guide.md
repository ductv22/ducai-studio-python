# Nghe và gán nhãn phân vai thoại

1. Mở **NGHE-VA-GAN-NHAN.html** bằng Edge hoặc Chrome. Giữ thư mục `clips` cạnh tệp HTML.
2. Điền tên người nghe. Xác nhận từng nguồn là âm thanh gốc tiếng Trung của phim hoạt hình.
   Không xác nhận nguồn nếu chưa biết chắc; ghi chú lại để bổ sung.
3. Nghe phần câu được đánh dấu. Mỗi clip có thêm tối đa 0,5 giây trước và sau;
   bỏ qua người nói ngoài phần câu. Nhấn 1 = Nam, 2 = Nữ, 3 = Nhiều người nói,
   4 = Không rõ. Dùng nút nghe lại khi cần. Giọng nhân vật trẻ em/biến đổi khó phân
   biệt có thể gán Không rõ; không suy đoán từ tên, nội dung hoặc ngoại hình.
4. Ghi chú các trường hợp nhạc lớn, giọng trẻ em, tiếng hét, chồng tiếng hoặc câu
   lệch mốc. Nhiều người nối tiếp nhau trong cùng câu cũng chọn Nhiều người nói.
5. Nhấn **Xuất nhãn JSON** và gửi lại tệp. Có thể xuất dở rồi nhập lại để tiếp tục.
   Trang lưu nháp trong trình duyệt nếu được hỗ trợ; tệp JSON là bản lưu đáng tin cậy.

Trang nghe không hiển thị dự đoán hay phân chia hiệu chỉnh/kiểm tra. Nhãn được
người nghe chọn, không tự điền bằng model. Tệp nhãn lưu tên người nghe, danh tính
bộ nghe và checksum clip để không ghép nhầm với nguồn đã thay đổi. Khi bổ sung
phim, có thể nhập nhãn cũ vào bộ mới: chỉ những câu có cùng ID và checksum được nhận.

Nghiệm thu cần **ít nhất 200 câu kiểm tra từ ba phim hoạt hình Trung Quốc khác
nhau**, ngoài tập hiệu chỉnh. Nếu bộ hiện tại có ít phim hơn thì đây là bộ ban đầu
để chuẩn bị dữ liệu, chưa đủ điều kiện nghiệm thu. Cần xác nhận các phim thực sự
khác nhau; ba tập của cùng một phim không được tính là ba phim.

Kết quả chỉ đạt khi đúng ít nhất 95% câu tự gán và tự gán ít nhất 70% tổng số câu
kiểm tra. Câu khó, chồng tiếng, không rõ và dùng dự phòng đều giữ trong báo cáo;
không bỏ những câu này để làm đẹp số liệu. Ngưỡng được chọn từ tập hiệu chỉnh và
đóng băng trước khi mở kết quả kiểm tra. Khi không đạt, dừng tích hợp sản phẩm.
