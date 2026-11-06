# Trạm Dịch

Ứng dụng tự host để dịch EPUB tiếng Hàn sang tiếng Việt, biên tập theo chương và xuất lại EPUB giữ tài nguyên gốc.

## Docker

Lưu `GEMINI_API_KEY` trong `.env` (xem `.env.example`). Tệp này không được đưa vào Git hoặc Docker image.

```sh
docker compose up -d --build
```

Mở http://localhost:8000. Database, EPUB gốc và tiến trình nằm trong `data/`, được giữ khi container restart. Chạy một instance backend để tránh hai worker cùng xử lý một job.

## Chạy trực tiếp

Python 3.11+ và Node 20+:

```sh
python -m venv .venv
# Windows: .venv\Scripts\python -m pip install -r backend/requirements.txt
# macOS/Linux: .venv/bin/python -m pip install -r backend/requirements.txt
cd frontend
pnpm install
pnpm build
cd ..
# Windows:
.venv\Scripts\python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Giao diện build được phục vụ bởi FastAPI. Khi phát triển, `pnpm dev` trong `frontend/` proxy `/api` về cổng 8000.

Trên máy hiện tại, môi trường và giao diện đã được chuẩn bị. Chạy `.\start.ps1` từ thư mục `Code` để mở server khi chưa có server đang chạy.

## Kiểm thử

```sh
.venv\Scripts\python -m pytest backend/tests -q
```

Các test dùng provider giả, không tiêu tốn quota Gemini. Kiểm thử API thật chỉ dùng một vài đoạn truyện; không tự dịch toàn bộ EPUB mẫu trong development.

Muốn thử lại Gemini thật với tối đa ba đoạn đầu: `.venv\Scripts\python scripts/live_smoke.py --model gemini-3.5-flash-lite` (hoặc thêm `--review` để chạy tiếp một lượt thẩm định trên cùng các đoạn đó). Kết quả thành công được lưu vào `data/live-smoke.json`; script không thay đổi bản dịch trong database.

Trạng thái kiểm tra và sẵn sàng cục bộ:
- Luồng Gemini từng được kiểm chứng ở các lượt trước; lượt nghiệm thu ngày 07/11/2026 dùng provider giả, không chạy lại dịch sách. Docker runtime và triển khai cloud chưa được kiểm chứng. Không cần key Gemini để mở sách, biên tập thủ công hoặc xuất EPUB.
- **Backend suite**: Đạt 373 passed, 6 skipped trên bản sao mã không có `.env`, với `DATA_DIR` và thư mục pytest riêng. Không chạy test trên database thật: import ứng dụng có khởi tạo schema. Bốn test snapshot sách/báo cáo lịch sử cần bật `COMIC_RUN_PRIVATE_BOOK_TESTS=1`; các test dữ liệu riêng còn lại tự bỏ qua khi thiếu fixture. Chúng không phải yêu cầu CI.
- **Frontend**: 43 test đạt, `pnpm build` thành công; QA Playwright dùng API giả kiểm tra desktop/mobile, quyền, lỗi đăng nhập/đăng xuất, giữ nháp khi 409 và lịch sử phục hồi. Chạy `pnpm test`, `pnpm build` trong `frontend/`; smoke UI: `node frontend/e2e/release-smoke.cjs` từ root. Có thể đặt `PLAYWRIGHT_CORE_PATH` và `QA_OUTPUT_DIR` theo máy chạy.
- **Kế hoạch triển khai Vercel**: Xem chi tiết kiến trúc và lộ trình tại phần Vercel trong `docs/HOSTING_PIPELINE.md`.

## Sử dụng

1. Tải EPUB, chọn chương theo đúng thứ tự spine.
2. Lưu Story Bible hoặc phân tích một mẫu ngắn để thiết lập bối cảnh, nhân vật và văn phong.
3. Chọn model (mặc định `gemini-3.5-flash-lite`) và bấm **Dịch chương** để dịch trọn vẹn cả chương đã chọn. Quy trình thực hiện theo thứ tự: hoàn thành giai đoạn dịch toàn bộ chương, sau đó thẩm định một lần trên nội dung đã dịch, và tạo tóm tắt chương. Các đợt xử lý dưới 40 đoạn được chia tự động và hoàn toàn ngầm; không cần bấm từng câu hay can thiệp thủ công. **Dịch toàn bộ** chỉ chạy khi bạn chủ động xác nhận.
4. Theo dõi tiến trình trong tab **Công việc**, tạm dừng/tiếp tục/thử lại khi cần. Các đoạn đã lưu được dùng lại khi resume.
5. Đọc truyện liên tục như sách hoàn chỉnh trong chế độ đọc tiểu thuyết. Chuyển sang **Bản gốc (Tiếng Hàn)** để xem toàn bộ chương gốc, hoặc bật chế độ **Chỉnh sửa** để biên tập từng đoạn văn bản. Lưu chỉnh sửa rồi **Xuất EPUB** hoặc **Xuất PDF** (hỗ trợ chọn chương hiện tại, tùy chọn nhiều chương, hoặc toàn bộ sách; xuất bản dịch tiếng Việt yêu cầu các chương đã chọn phải được hoàn thành trọn vẹn; xuất nguyên tác tiếng Hàn có thể xuất ngay bất kỳ lúc nào).

## Xuất PDF & Cấu hình Font chữ

Ứng dụng tạo tệp PDF A4 liên tục theo chuẩn in ấn và đọc tiểu thuyết bằng ReportLab:
- **Bộ font mở đóng gói sẵn (Offline Bundled OFL Fonts)**: Dự án tích hợp sẵn các font chuẩn mã nguồn mở (giấy phép SIL Open Font License) trong thư mục `fonts/` bao gồm:
  - `NotoSansKR-Regular.ttf`: Font chính bao phủ toàn bộ ký tự tiếng Việt (kể cả các nguyên âm có dấu phức `ế`, `ệ`, `ỉ`, `ở`, `ứ`) và Hangul tiếng Hàn.
  - `NotoSans-Regular.ttf`: Font phụ hỗ trợ dấu gạch ngang kết hợp (combining strikethrough `U+0336`).
  - `NotoSansThai-Regular.ttf`: Font phụ hỗ trợ ký tự tiếng Thái xuất hiện trong nguyên tác.
  Hệ thống tự động phân tách văn bản thành từng cụm ký tự và chuyển font linh hoạt theo cmap của từng font mà không phụ thuộc vào font hệ thống hay kết nối mạng ngoài.
- **Biến môi trường `PDF_FONT_PATH`**: Cho phép chỉ định đường dẫn tệp font TTF/TTC tùy biến đặt lên đầu danh sách ưu tiên; các font trong `fonts/` vẫn tự động đóng vai trò fallback.
- **Kiểm tra phủ ký tự nghiêm ngặt**: Nếu gặp ký tự thực sự không được bất kỳ font nào hỗ trợ, ứng dụng sẽ báo lỗi cụ thể kèm mã Unicode (`U+XXXX`) thay vì tạo PDF bị lỗi ô vuông chữ nhật rỗng.

Gemini có thể trả lỗi quá tải hoặc quota. Ứng dụng phải lưu lỗi, giữ bản dịch đã hoàn thành và cho phép retry; không coi API lỗi là bản dịch thành công.

## Cấu hình khóa phụ Gemini & Chuyển đổi dự phòng (Failover)

Ứng dụng hỗ trợ cấu hình thêm khóa phụ riêng biệt thông qua biến `GEMINI_API_KEY_SECONDARY` trong `.env` (tùy chọn):
- **Chuyển đổi dự phòng khi quá tải (HTTP 429 / RESOURCE_EXHAUSTED)**: Khi khóa chính bị giới hạn tần suất yêu cầu (HTTP 429 hoặc `RESOURCE_EXHAUSTED`), hệ thống sẽ tự động chuyển sang khóa phụ ngay lập tức mà không làm gián đoạn tiến trình.
- **Thời gian Cooldown & Tự động phục hồi**: Khóa chạm giới hạn sẽ tạm dừng theo thời gian quy định bởi header `Retry-After` hoặc trường `retryDelay` trong phản hồi của Gemini (hoặc cooldown mặc định). Khi khóa chính hết thời gian cooldown, hệ thống sẽ tự động ưu tiên quay trở lại dùng khóa chính.
- **Lỗi không phải rate-limit không kích hoạt chuyển khóa**: Các lỗi khác như xác thực không hợp lệ (401, 403), mô hình không tìm thấy (404), lỗi cú pháp hoặc lỗi mạng tạm thời sẽ không gây chuyển khóa phụ.
- **Hạn mức dùng chung giữa các khóa cùng dự án**: Nếu khóa chính và khóa phụ thuộc cùng một project trên Google Cloud / Google AI Studio, chúng có thể dùng chung hạn mức quota; cơ chế này không giúp vượt qua giới hạn quota của cùng một dự án.
- **Khởi động lại máy chủ**: Cần khởi động lại server backend sau khi thêm hoặc cập nhật biến môi trường trong `.env` để cấu hình mới có hiệu lực.

Spec: `SPEC.md`; kiến trúc API: `docs/API.md`; tiến độ triển khai: `MILESTONES.md`; thiết kế: `docs/DESIGN.md`.
