# Cấp giấy phép offline — hướng dẫn dành cho chủ tool

## Phạm vi và nguyên tắc an toàn

Từ a140, giấy phép DUCAI STUDIO mặc định là key ngắn `DUCAI-L2.` gồm 128 ký tự,
vĩnh viễn cho đúng một máy Windows. Bộ cấp cũng hỗ trợ `DUCAI-L3.` có thời hạn
theo số giờ cho thành viên test. Bản a140 vẫn đọc key dài `DUCAI-L1.` đã cấp
cho a139, nên khách hàng đang kích hoạt không phải cấp lại chỉ để nâng cấp.
Bộ cấp key và khóa riêng là tài sản của chủ tool; khách hàng chỉ được nhận file
`.ducai-license` hoặc chuỗi key do bộ cấp tạo ra. **Không gửi toàn bộ thư mục
`DUCAI-LICENSE-OWNER-PRIVATE-<version>`, file sao lưu PEM, mật khẩu sao lưu hoặc sổ cấp key
cho khách.**

Khóa cấp giấy phép nằm trong Windows Credential Manager của đúng tài khoản
Windows, dưới namespace `DUCAI_LICENSE_ISSUER/`, tách khỏi khóa ký updater và
credential API của khách hàng. Bộ cấp trên máy Windows mới phải khôi phục đúng
khóa hiện có; một khóa khác sẽ không được bản khách hàng tin cậy và bị bộ cấp từ
chối. Quy tắc này được thực thi bởi
[`scripts/license-issuer.py`](../scripts/license-issuer.py).
Quy trình hoàn toàn offline và không cần VPS; không chuyển vault hoặc bộ cấp lên
máy chủ chỉ để cấp key.

## Mở bộ cấp key

Build bằng [`scripts/build-license-issuer.ps1`](../scripts/build-license-issuer.ps1).
Kết quả riêng của chủ tool nằm tại:

```text
outputs/DUCAI-LICENSE-OWNER-PRIVATE-<version>/license-issuer.dist/DUCAI-Key-Issuer.exe
```

Không chép thư mục này vào gói khách hàng. Script build kèm chính tài liệu này
dưới tên `HUONG-DAN-CHU-TOOL.md`; ranh giới gói khách hàng được kiểm tra bởi
[`scripts/check-license-release.py`](../scripts/check-license-release.py).

## Cấp key cho khách

1. Khách mở **Cài đặt → Kích hoạt**, bấm sao chép mã máy và gửi nguyên mã
   `DUCAI-M1-…` cho chủ tool.
2. Chủ tool mở `DUCAI-Key-Issuer.exe`, dán mã máy, chọn **Vĩnh viễn (L2)** hoặc
   **Có thời hạn (L3)**, nhập số giờ (ví dụ `168` = 7 ngày), rồi bấm **Cấp và lưu
   key**. Mặc định bộ cấp tạo key vĩnh viễn L2 cho a140 trở lên. Chỉ tích **Key
   dài cho a139** khi khách vẫn dùng a139; L1 không hỗ trợ thời hạn. Với CLI,
   dùng `issue --duration-hours 168` cho L3 hoặc `issue --legacy` cho L1.
3. Lưu file `.ducai-license`, rồi gửi **chỉ file này** cho khách. Có thể dùng
   **Sao chép key** nếu cần chuyển chuỗi key thay cho file.
4. Khách trở lại **Cài đặt → Kích hoạt** để dán key, hoặc bấm **Nhập file key**
   và chọn file vừa nhận.

Mỗi lần cấp được ghi vào
`%LOCALAPPDATA%\DUCAI License Issuer\issued-licenses.json`. Đây là sổ cục bộ của
chủ tool, không phải thành phần gói khách hàng. Nếu file đầu ra đã tồn tại, bộ
cấp không ghi đè; hãy chọn tên mới thay vì xóa dấu vết của lần cấp trước.

## Sao lưu và khôi phục khóa cấp

Ngay trên máy chủ tool hiện tại:

1. Bấm **Sao lưu khóa riêng** và chọn nơi lưu ngoài thư mục gửi khách.
2. Dùng mật khẩu ít nhất 12 ký tự. Giữ mật khẩu ở nơi khác với file PEM.
3. Giữ ít nhất một bản sao ngoại tuyến mà chỉ chủ tool truy cập được.

File PEM xuất ra đã được mã hóa nhưng vẫn là bản sao khóa gốc: mất cả file lẫn
Credential Manager sẽ làm mất khả năng cấp key tương thích; lộ file cùng mật khẩu
sẽ làm mất quyền kiểm soát trust root.

Trên một máy Windows mới, mở bộ cấp và chọn **Khôi phục khóa**, nhập file PEM và
mật khẩu cũ trước khi cấp giấy phép. Không tạo hoặc thay bằng một khóa mới. Bộ cấp
từ chối backup không khớp trust root, không ghi đè một khóa khác đang có, và không
đưa mật khẩu hay khóa riêng lên dòng lệnh.

## Khi nào phải cấp lại

Mã máy kết hợp MachineGuid với UUID SMBIOS hợp lệ; nếu firmware không cung cấp
UUID dùng được, nó kết hợp MachineGuid với serial của ổ đĩa hệ thống. Vì vậy cài
lại Windows, thay mainboard/UUID, hoặc thay ổ hệ thống trên máy đang dùng nhánh
fallback có thể tạo mã máy khác và cần cấp lại.

Giấy phép offline không có máy chủ thu hồi. Key L3 dùng đồng hồ UTC cục bộ để
kiểm tra hết hạn, nên không chống được việc người dùng chỉnh đồng hồ. Việc cấp lại hoặc chuyển thủ công
không vô hiệu hóa máy cũ, và cơ chế này không cam kết chống sửa binary. Khi cần
đổi máy, hãy ghi nhận quyết định trong sổ cấp key và cấp file mới cho mã máy mới.

## Xử lý sự cố

- **Báo thiếu khóa trong Windows:** khôi phục backup đã mã hóa; không tạo root mới.
- **Backup hoặc vault không khớp:** dừng cấp key và tìm đúng backup. Không xóa
  `key-info.json` để lách kiểm tra.
- **File key khách nhập bị từ chối:** yêu cầu khách sao chép lại mã máy hiện tại,
  rồi cấp file mới. Với key ngắn L2, lỗi sai máy hoặc key bị sửa chỉ hiện thông
  báo chung; không yêu cầu khách gửi MachineGuid, UUID hay serial thô.
- **Tool khách chưa kích hoạt:** màn Cài đặt và các thao tác phục hồi vẫn dùng
  được; project và media không bị xóa khi kích hoạt thất bại.
