# API contract for Trạm Dịch (EPUB Translator)

Tất cả đường dẫn API đều có tiền tố `/api`. Trả về dữ liệu chuẩn định dạng JSON và thông báo lỗi tiếng Việt dễ hiểu. Không chứa API key hay thông tin nhạy cảm ở phía client.

---

## 1. Hệ thống & Cài đặt (System & Settings)

### `GET /api/health`
- **Mô tả**: Kiểm tra trạng thái hoạt động của máy chủ backend.
- **Phản hồi**: `200 OK`
  ```json
  {
    "status": "ok",
    "app": "Tram Dich"
  }
  ```

### `GET /api/settings`
- **Mô tả**: Lấy thông tin cấu hình mô hình hiện tại và trạng thái cấu hình API key. Mặc định là `gemini-3.5-flash-lite`.
- **Phản hồi**: `200 OK`
  ```json
  {
    "model": "gemini-3.5-flash-lite",
    "key_configured": true
  }
  ```

### `PATCH /api/settings`
- **Mô tả**: Cập nhật mô hình AI sử dụng. Có kiểm tra định dạng và tính hợp lệ của tên mô hình.
- **Yêu cầu**:
  ```json
  {
    "model": "gemini-3.5-flash-lite"
  }
  ```
- **Phản hồi**: `200 OK`
  ```json
  {
    "model": "gemini-3.5-flash-lite",
    "key_configured": true
  }
  ```
- **Lỗi**: `400 Bad Request` nếu tên mô hình không hợp lệ.

### `GET /api/models`
- **Mô tả**: Danh sách các mô hình hỗ trợ, được truy vấn động trực tiếp từ API provider.
- **Phản hồi**: `200 OK`
  ```json
  {
    "models": ["gemini-2.5-flash", "gemini-3.8-flash"],
    "key_configured": true
  }
  ```

---

## 2. Quản lý Sách (Books Management)

### `POST /api/books`
- **Mô tả**: Tải lên tập tin EPUB (multipart/form-data), giới hạn kích thước đọc tối đa 50MB chống memory bomb, kiểm tra bảo mật (Zip Slip, XXE, Zip Bomb), phân tích cú pháp và lưu trữ vào database.
- **Yêu cầu**: `file: UploadFile` (form field `file`)
- **Phản hồi**: `201 Created`
  ```json
  {
    "id": "b3e0c...",
    "title": "Tên sách",
    "author": "Tác giả",
    "language": "ko",
    "identifier": "urn:uuid:...",
    "file_path": "data/b3e0c....epub",
    "chapter_count": 157,
    "translated_count": 0,
    "total_segments": 4200,
    "translated_segments": 0,
    "created_at": "2026-10-07T12:00:00Z",
    "updated_at": "2026-10-07T12:00:00Z"
  }
  ```
- **Lỗi**:
  - `400 Bad Request`: Tập tin rỗng hoặc lỗi cú pháp EPUB/XML.
  - `413 Request Entity Too Large`: Tập tin vượt quá giới hạn 50MB.

### `GET /api/books`
- **Mô tả**: Lấy danh sách toàn bộ sách đã tải lên kèm theo tiến độ dịch.
- **Phản hồi**: `200 OK`

### `GET /api/books/{id}`
- **Mô tả**: Xem chi tiết thông tin và tiến độ của một quyển sách.
- **Phản hồi**: `200 OK`
- **Lỗi**: `404 Not Found` nếu không tìm thấy ID.

### `GET /api/books/{id}/chapters`
- **Mô tả**: Lấy danh sách các chương theo đúng thứ tự đọc (spine reading order) từ OPF manifest.
- **Phản hồi**: `200 OK`

---

## 3. Quản lý Chương & Soạn thảo (Chapters & Editor)

### `GET /api/chapters/{id}`
- **Mô tả**: Xem nội dung chi tiết của chương để hiển thị văn bản đọc truyện liên tục hoặc trình biên tập theo chương. Trả về danh sách các đoạn văn bản (safe preview) bảo toàn thứ tự đọc và metadata DOM.
- **Phản hồi**: `200 OK`

### `PATCH /api/chapters/{id}`
- **Mô tả**: Chỉnh sửa văn bản gốc hoặc bản dịch của các đoạn trong chương.
  - Thay đổi `source` sẽ tự động vô hiệu hóa bản dịch cũ (`translation = null`) và đánh giá cũ (`review_data = null`) trừ khi bản dịch mới được cung cấp cụ thể.
  - Trạng thái chương được tính toán tự động dựa trên số đoạn đã dịch (`untranslated`, `partial`, `translated`), không chấp nhận trạng thái tùy tiện từ client.
  - Từ chối chỉnh sửa nếu chương thuộc sách đang có tác vụ chạy ngầm (`running` hoặc `pausing`).
- **Yêu cầu**:
  ```json
  {
    "segments": [
      {
        "id": "s9f8...",
        "source": "Chỉnh sửa câu gốc nếu cần",
        "translation": "Bản dịch được cập nhật"
      }
    ]
  }
  ```
- **Phản hồi**: `200 OK`
- **Lỗi**:
  - `400 Bad Request`: Mã đoạn không tồn tại.
  - `404 Not Found`: Không tìm thấy chương.
  - `409 Conflict`: Sách đang có tác vụ đang chạy (`running` hoặc `pausing`).

---

## 4. Thuật ngữ & Từ điển (Glossary - Legacy / Không còn sử dụng trong tiến trình dịch)

> [!NOTE]
> Tính năng Từ điển / Glossary đã được gỡ bỏ hoàn toàn khỏi giao diện người dùng và tiến trình dịch/thẩm định để tránh rủi ro khóa cứng thuật ngữ gây lỗi biên dịch. Các endpoint dưới đây được duy trì ở chế độ trơ (inert) nhằm bảo đảm tương thích dữ liệu SQLite cũ mà không tác động đến luồng dịch thực tế.

### `GET /api/books/{id}/glossary`
- **Mô tả**: Lấy danh sách thuật ngữ / từ điển lưu trong cơ sở dữ liệu cũ của quyển sách.
- **Phản hồi**: `200 OK`

### `POST /api/books/{id}/glossary`
- **Mô tả**: Tạo mục từ điển (tương thích cũ).
- **Phản hồi**: `201 Created`

### `PATCH /api/glossary/{id}`
- **Mô tả**: Cập nhật mục từ điển (tương thích cũ).
- **Phản hồi**: `200 OK`

### `DELETE /api/glossary/{id}`
- **Mô tả**: Xóa mục từ điển (tương thích cũ).
- **Phản hồi**: `200 OK`

---

## 5. Story Bible

### `GET /api/books/{id}/bible`
- **Mô tả**: Lấy nội dung story bible (định dạng `{"text": ...}` cho frontend).
- **Phản hồi**: `200 OK`
  ```json
  {
    "book_id": "b3e0c...",
    "text": "Ghi chú bối cảnh, xưng hô, thế giới quan...",
    "story_bible": "Ghi chú bối cảnh, xưng hô, thế giới quan..."
  }
  ```

### `PUT /api/books/{id}/bible`
- **Mô tả**: Lưu cập nhật story bible.
- **Yêu cầu**:
  ```json
  {
    "text": "Nội dung cập nhật mới..."
  }
  ```
- **Phản hồi**: `200 OK`
  ```json
  {
    "book_id": "b3e0c...",
    "text": "Nội dung cập nhật mới...",
    "story_bible": "Nội dung cập nhật mới..."
  }
  ```
- **Lỗi**:
  - `409 Conflict`: Sách đang có tác vụ dịch đang thực hiện.

---

## 6. Xuất bản EPUB & PDF (Export)

### `GET /api/books/{id}/export`
- **Mô tả**: Tái tạo và tải về tập tin EPUB đã dịch.
  - Mimetype ghi đầu tiên uncompressed (ZIP_STORED).
  - Áp dụng các thay đổi dịch và chỉnh sửa DOM an toàn.
  - Giữ nguyên toàn bộ tài nguyên không dịch.
  - Tiêu đề Content-Disposition hỗ trợ ASCII fallback và RFC 5987 UTF-8 filename* tránh lỗi `UnicodeEncodeError`.
- **Phản hồi**: `200 OK`
  - `Content-Type`: `application/epub+zip`
  - `Content-Disposition`: `attachment; filename="book_vi.epub"; filename*=UTF-8''...`

### `POST /api/books/{id}/export/pdf`
- **Mô tả**: Xuất các chương đã chọn (hoặc toàn bộ sách) ra định dạng tài liệu PDF A4 chất lượng cao phục vụ đọc liên tục như tiểu thuyết.
  - Hỗ trợ Unicode đầy đủ cho tiếng Việt (dấu thanh) và tiếng Hàn (Hangul) nhờ tự động phát hiện và nhúng font TTF thực thụ.
  - Định dạng văn bản liên tục: nối các phần tử nội dòng (`span`, `em`), ngắt dòng `<br>`, phân đoạn hội thoại, hiển thị phân cảnh `* * *`, và loại trừ metadata `<head><title>` trùng lặp.
  - Phân trang tự động hai lượt (NumberedCanvas) với tiêu đề chạy và số trang (e.g. `1 / 15`).
  - Thứ tự chương được đảm bảo theo đúng thứ tự đọc (spine reading order) của sách.
  - Thực thi CPU trong luồng worker riêng biệt (`asyncio.to_thread`) tránh làm nghẽn tiến trình FastAPI.
  - Chỉ xuất snapshot dữ liệu đã lưu trong database; không tạo tác vụ nền, không ghi database, không gọi API Gemini.
- **Yêu cầu (Body JSON)**:
  ```json
  {
    "chapter_ids": ["c1a2b..."],
    "content": "translation"
  }
  ```
  - `chapter_ids`: Danh sách ID chương cần xuất. Bỏ qua (`null` hoặc không truyền) để xuất toàn bộ sách. Mảng rỗng `[]` là không hợp lệ. Phải thuộc cùng quyển sách và không trùng lặp.
  - `content`: `"translation"` (Bản dịch tiếng Việt đã lưu) hoặc `"source"` (Nguyên tác tiếng Hàn). Mặc định là `"translation"`.
- **Quy tắc kiểm tra bản dịch (Translation mode)**:
  - Khi `content == "translation"`: Hệ thống **từ chối xuất** và báo lỗi `400 Bad Request` nếu phát hiện bất kỳ chương nào chưa hoàn thành bản dịch (có đoạn văn bản non-whitespace thiếu bản dịch). Bản dịch tuyệt đối không được tự ý bỏ sót câu hoặc tự động thay bằng tiếng Hàn.
  - Khi `content == "source"`: Hoạt động xuất nguyên tác tiếng Hàn luôn thành công mà không phụ thuộc vào trạng thái dịch.
- **Phản hồi**: `200 OK`
  - `Content-Type`: `application/pdf`
  - `Content-Disposition`: `attachment; filename="book_vi.pdf"; filename*=UTF-8''...`
- **Lỗi**:
  - `400 Bad Request`: Danh sách `chapter_ids` rỗng, trùng lặp, chứa ID không thuộc sách, `content` không hợp lệ, hoặc chương chưa hoàn thành dịch khi xuất `translation`.
  - `404 Not Found`: Không tìm thấy sách.
  - `500 Internal Server Error`: Không tìm thấy font Unicode hỗ trợ trên hệ thống.

### Cấu hình Font chữ PDF (PDF Font Configuration)
Hệ thống tự động phát hiện và đăng ký font chữ TTF/TTC hỗ trợ Unicode tiếng Việt, tiếng Hàn và các ký tự đặc biệt theo thứ tự ưu tiên:
1. **Bộ font mở đóng gói sẵn (Bundled OFL Fonts)** trong thư mục `fonts/`:
   - `NotoSansKR-Regular.ttf`: Font chính bao phủ toàn diện tiếng Việt (đầy đủ các dấu thanh phức hợp `ế`, `ệ`, `ỉ`, `ở`, `ứ`) và tiếng Hàn (Hangul).
   - `NotoSans-Regular.ttf`: Font phụ fallback cho dấu gạch ngang kết hợp (combining strikethrough `U+0336`).
   - `NotoSansThai-Regular.ttf`: Font phụ fallback cho các ký tự tiếng Thái (`U+0E01`, `U+0E49`) có trong nguyên tác.
2. **Biến môi trường `PDF_FONT_PATH`**: Cho phép chỉ định đường dẫn tệp font TTF/TTC tùy biến đặt lên đầu danh sách ưu tiên; các font trong `fonts/` vẫn tự động đóng vai trò fallback.
3. **Font hệ thống** (fallback dự phòng khi thiếu thư mục fonts): Linux/Docker (`NotoSansCJK`, `NanumGothic`, `UnDotum`) hoặc Windows (`malgun.ttf`).

- **Cơ chế phân tách ký tự (Glyph Run Fallback)**: ReportLab áp dụng cơ chế phân tách chuỗi ký tự theo bảng mã `cmap` thực tế của từng font và nhúng thẻ `<font name="...">` tương ứng cho từng đoạn ký tự. Ký tự không được font nào trong cascade hỗ trợ sẽ kích hoạt ngoại lệ `PdfFontError` chứa mã `U+XXXX` rõ ràng thay vì in ra ô vuông không đọc được.

---

## 7. Phân tích & Tác vụ Dịch Bền vững (Analyze & Durable Worker Jobs)

### `POST /api/books/{id}/analyze`
- **Mô tả**: Phân tích bối cảnh chương đầu tiên (giới hạn <= 30 đoạn, ~6000 ký tự). Trích xuất và cập nhật Story Bible để bảo đảm tính nhất quán của bối cảnh, nhân vật và phong cách (không tự động tạo thuật ngữ từ điển).
- **Phản hồi**: `200 OK`
  ```json
  {
    "book_id": "b3e0c...",
    "text": "Bối cảnh thế giới...",
    "story_bible": "Bối cảnh thế giới...",
    "suggested_count": 0
  }
  ```
- **Lỗi**:
  - `400 Bad Request`: Sách không có chương để phân tích hoặc thiếu API key.
  - `409 Conflict`: Sách đang có tác vụ dịch đang thực hiện.

### `POST /api/books/{id}/jobs`
- **Mô tả**: Tạo tác vụ dịch nền bền vững (durable job worker).
  - `chapter_ids`: Danh sách ID chương cần dịch. Mỗi lần dịch là dịch trọn vẹn toàn bộ văn bản trong chương. Đợt chia nhỏ theo giới hạn của provider (tối đa 40 đoạn/lượt) được thực hiện ngầm và tự động. Quy trình xử lý theo thứ tự: hoàn thành giai đoạn dịch toàn bộ chương, sau đó thẩm định một lần trên nội dung đã dịch, và tạo tóm tắt chương. Nếu bỏ qua, mặc định chọn toàn bộ chương theo spine reading order. Kiểm tra không rỗng, không trùng lặp, thuộc đúng quyển sách.
  - `params`:
    - `review`: `true`/`false` bật giai đoạn thẩm định biên tập cho chương.
    - `max_segments`: Giới hạn tổng số đoạn cho toàn bộ tác vụ (chỉ dùng cho smoke test chẩn đoán, không dùng trong yêu cầu UI thông thường).
    - `model`: Mô hình AI áp dụng cho tác vụ. Mặc định chụp lại cấu hình hiện tại (`gemini-3.5-flash-lite`).
  - Snapshot mô hình AI hiện tại cho tác vụ, giữ nguyên cả khi cài đặt UI thay đổi sau đó.
  - Trạng thái khởi tạo: `queued`.
- **Yêu cầu (UI thông thường)**:
  ```json
  {
    "chapter_ids": ["c1a2b..."],
    "params": {
      "review": true
    }
  }
  ```
- **Phản hồi**: `201 Created`
  ```json
  {
    "id": "j9a8...",
    "book_id": "b3e0c...",
    "job_type": "translation",
    "status": "queued",
    "params": {
      "chapter_ids": ["c1a2b..."],
      "review": true,
      "max_segments": 10,
      "model": "gemini-3.5-flash-lite"
    },
    "progress": {
      "completed_segments": 0,
      "total_segments": 10,
      "stage": "queued"
    },
    "completed_segments": 0,
    "total_segments": 10,
    "error": null,
    "created_at": "2026-10-07T12:00:00Z",
    "updated_at": "2026-10-07T12:00:00Z"
  }
  ```
- **Lỗi**:
  - `400 Bad Request`: Danh sách chapter_ids không hợp lệ hoặc rỗng.
  - `409 Conflict`: Sách đã có tác vụ dịch đang hoạt động (`queued`, `running`, `pausing`).

### `GET /api/books/{id}/jobs`
- **Mô tả**: Lấy danh sách toàn bộ tác vụ dịch của sách theo thời gian tạo mới nhất.
- **Phản hồi**: `200 OK` (mảng các object tác vụ).

### `GET /api/jobs/{id}`
- **Mô tả**: Xem chi tiết tiến độ tác vụ (`queued`, `running`, `pausing`, `paused`, `completed`, `failed`).
- **Phản hồi**: `200 OK`

### `POST /api/jobs/{id}/pause`
- **Mô tả**: Tạm dừng tác vụ dịch.
  - Nếu tác vụ đang `running`: Chuyển sang `pausing`. Đợt dịch đang gửi API sẽ hoàn tất và lưu vào database trước khi chuyển sang `paused`. Trình soạn thảo bị chặn chỉnh sửa khi đang pausing.
  - Nếu tác vụ đang `queued`: Chuyển ngay sang `paused`.
- **Phản hồi**: `200 OK` (object tác vụ cập nhật).

### `POST /api/jobs/{id}/resume`
- **Mô tả**: Tiếp tục tác vụ đang tạm dừng. Chuyển trạng thái từ `paused` về `queued`.
- **Phản hồi**: `200 OK`

### `POST /api/jobs/{id}/retry`
- **Mô tả**: Thử lại tác vụ bị lỗi (`failed`).
  - Tác vụ giữ nguyên tiến độ các đợt đã lưu trước đó, không dịch lại các đoạn đã dịch thành công.
  - Nếu lỗi ở khâu tóm tắt (summary), retry sẽ thực hiện lại tóm tắt mà không cần gọi lại API dịch.
- **Phản hồi**: `200 OK`
- **Lỗi**:
  - `400 Bad Request`: Nếu tác vụ không ở trạng thái `failed`.
  - `409 Conflict`: Nếu sách đang có tác vụ khác đang chạy.
