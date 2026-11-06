# Quy trình Git

Repository: https://github.com/DuyTranND/LNovelTrans (private).

- `main`: bản ổn định đã nghiệm thu; không đồng nghĩa mã vừa push đã sẵn sàng public.
- `develop`: nhánh tích hợp cho công việc đang phát triển.
- `feature/<ten-ngan>` và `fix/<ten-ngan>`: tạo từ `develop`, mỗi nhánh một mục tiêu.
- Pull request vào `develop`, review và CI trước khi merge. Khi nghiệm thu bản phát hành, mở PR `develop` → `main`.
- Dùng commit rõ mục đích (`feat:`, `fix:`, `docs:`, `chore:`); không force-push lên `main` hoặc `develop`.
- Không commit `.env`, credential, database, EPUB gốc, bản dịch, output hoặc dữ liệu thử nghiệm. Rà `git diff --cached` và danh sách tệp trước mỗi commit; tránh `git add .`.

## Khởi tạo ngày 06/11/2026

Giữ commit README ban đầu trên `main`. Bản nguồn hiện tại được nhập vào `develop` để tiếp tục kiểm chứng và sửa qua PR; chưa phát hành production.

Frontend đã qua 24 test và production build trên máy. Backend full suite chưa được xác nhận ở lượt chuẩn bị này; Docker runtime, staging và backup/restore chưa được nghiệm thu. Các kiểm tra trên dữ liệu thật không được tự chạy chỉ để hoàn thành CI.

CI hiện chạy cho push lên `main`/`develop` và pull request. Branch protection và required checks phải được cấu hình trên GitHub riêng; tài liệu này không khẳng định chúng đã bật.

## Bản dùng thử local ngày 07/11/2026

Các sửa auth, lịch sử/revision, glossary, quyền và xử lý xung đột nằm ở `fix/release-readiness`. Kiểm tra local đạt 373 backend test (6 skip), 43 frontend test, build và QA desktop/mobile với API giả. Xem `MILESTONES.md` để biết phạm vi nghiệm thu và phần cloud còn thiếu. Chưa xác minh CI từ tài khoản GitHub được kết nối; push lên nhánh fix không tự chứng minh CI đã chạy, vì workflow hiện lọc push vào `main`/`develop`. Mở PR vào `develop` để chạy kiểm tra theo luồng review.
