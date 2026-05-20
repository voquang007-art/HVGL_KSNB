# HVGL_KSNB - Cổng làm việc Ban Kiểm soát nội bộ

## Chức năng đã có trong bản khung

1. Đăng ký sử dụng không qua xét duyệt.
2. Người đăng ký mặc định thuộc unit `Thành viên trưng tập`.
3. Admin gán vị trí và đơn vị cho user:
   - Ban Kiểm soát nội bộ.
   - Hội đồng thành viên.
   - Thành viên trưng tập.
4. Module Phiếu kiểm soát hồ sơ thu, chi quan trọng:
   - Tạo Phiếu.
   - Tick Mục II, tick mục nào xuất Phiếu mục đó.
   - Import file ĐNTT để sinh Mục III.
   - Import file Danh sách vốn tự có để sinh Mục III.
   - Cột Ban KSNB KT: đúng thì tick, sai thì nhập số tiền kiểm tra.
   - Mục IV, V, VI có ô chọn/nhập nội dung.
   - Nhân viên chọn trình qua Trưởng ban hoặc không trình qua Trưởng ban.
   - Nếu không trình qua Trưởng ban, ô ký Trưởng ban ghi: `Đã ủy quyền. Đồng ý với nội dung kiểm tra.`
   - Ký số nội bộ theo vị trí Người kiểm soát và Trưởng ban.
   - Xuất Excel và xem PDF nếu máy chủ có LibreOffice.
5. Để sẵn menu/route nền cho:
   - Trao đổi nội bộ.
   - Kho tài liệu.
   - Họp trực tuyến.
   - Phê duyệt dự thảo văn bản.

## Cài đặt

```bat
cd HVGL_KSNB
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
run.bat
```

Truy cập:

```text
http://127.0.0.1:5010
```

Tài khoản quản trị mặc định được seed khi khởi động lần đầu:

```text
admin / HvgL@2026
```

## Ghi chú LibreOffice

Nếu máy chủ có LibreOffice, nút `Xem bằng LibreOffice/PDF` sẽ xuất PDF. Nếu chưa có LibreOffice, hệ thống trả file Excel để mở/xem trực tiếp.

## File mẫu

File template Phiếu nằm tại:

```text
app/template_excel/Mau.xlsx
```

Bản xuất hiện tại đang sinh Phiếu Excel theo bố cục động để bảo đảm Mục III co giãn theo số dòng import. Khi cần bám tuyệt đối từng ô của file Mẫu.xlsx, bước tiếp theo là mapping lại theo tọa độ cell cụ thể của mẫu chính thức.
