# Translation Robustness and Error Recovery Specification

Tài liệu quy định kiến trúc xử lý lỗi tự phục hồi (Self-Recovery), phân loại lỗi cấu trúc (Structured Validation Error Classification), và các định mức thử lại cố định (Fixed Bounded Budgets) cho Trạm Dịch.

---

## 1. Phân loại lỗi kiểm định cấu trúc (Unified Structured Validation Errors)

Tất cả các lỗi kiểm định dữ liệu từ mô hình AI đều kế thừa từ `ProviderValidationError` thay vì kiểm tra chuỗi ký tự cục bộ:

- **`ProviderValidationError(ProviderError)`**: Lớp cơ sở cho toàn bộ lỗi kiểm định phản hồi từ provider.
- **`ProviderResponseFormatError(ProviderValidationError)`**: Lỗi dữ liệu không phải JSON hợp lệ, văn bản rỗng, hoặc ứng đáp thiếu phần nội dung (parts).
- **`ProviderSchemaValidationError(ProviderValidationError)`**: Dữ liệu JSON vi phạm schema yêu cầu (thiếu trường `translations`, `issues`, `corrections`, `summary`, hoặc sai kiểu dữ liệu).
- **`ProviderIdValidationError(ProviderValidationError)`**: Lỗi phân đoạn ID (chứa ID lạ/thừa không có trong yêu cầu `extra_ids`, hoặc ID bị trùng lặp `duplicate_ids`).
- **`ProviderEmptyContentError(ProviderValidationError)`**: Lỗi câu dịch hoặc bản sửa đổi ngôn ngữ bị rỗng sau khi đã qua các lần phục hồi (`empty_ids`).
- **`ProviderSafetyError(ProviderError)`**: Lỗi chặn bởi bộ lọc an toàn Gemini (SAFETY hoặc RECITATION). Lỗi này mang tính quyết định, không thử lại (non-retryable).

---

## 2. Định mức thử lại có giới hạn (Fixed Bounded Budgets)

| Thao tác | Sự cố kích hoạt | Ngân sách (Budget) | Hành vi xử lý |
| :--- | :--- | :--- | :--- |
| **Dịch đợt ban đầu (Initial Batch)** | JSON không hợp lệ, sai schema, ID trùng lặp hoặc thừa | **Tối đa 1 lần yêu cầu lại (1 re-request)**, tổng cộng 2 cuộc gọi API cho đợt | Gửi prompt hướng dẫn đặc biệt sao chép chính xác ID và tuân thủ JSON schema. Giữ nguyên mô hình và ngữ cảnh. |
| **Phục hồi phân đoạn thiếu / rỗng (Targeted Recovery)** | Phân đoạn bị mô hình bỏ sót, hoặc phân đoạn văn bản thông thường bị dịch rỗng | **Tối đa 2 lần phục hồi có mục tiêu (max 2 recovery attempts)** | **Giữ lại các phân đoạn đã dịch hợp lệ**. Chỉ đưa các ID chưa giải quyết vào danh sách mục tiêu phục hồi `missing_ids`. |
| **Thẩm định biên tập (Review)** | JSON lỗi định dạng, sai schema, ID không khớp, hoặc bản sửa đổi ngôn ngữ bị rỗng | **Tối đa 1 lần yêu cầu lại (1 re-request)**, tổng cộng 2 cuộc gọi API cho đợt thẩm định | Hướng dẫn reviewer chỉ sửa đổi các phân đoạn cần thiết và không để chuỗi rỗng. Giữ nguyên mô hình và ngữ cảnh. |
| **Tóm tắt chương (Summarize)** | JSON lỗi định dạng hoặc trường `summary` rỗng | **Tối đa 1 lần yêu cầu lại (1 re-request)**, tổng cộng 2 cuộc gọi API | Nhắc nhở mô hình trả về tóm tắt văn học hoàn chỉnh bằng tiếng Việt. |
| **Lỗi mạng & HTTP tạm thời** | HTTP 429, 500, 502, 503, 504, Timeout, Network Error | **Tối đa 3 lần thử lại (max_retries = 3)** | Exponential backoff (độ trễ tăng theo hàm mũ 2^attempt). |

---

## 3. Cơ chế bảo vệ và xử lý dữ liệu chi tiết

### 3.1. Phân biệt văn bản ngôn ngữ và ký hiệu thuần túy (Symbols vs. Language)
- **Ký hiệu thuần túy (`is_symbol_only_source`)**: Chỉ chứa khoảng trắng hoặc ký tự thuộc Unicode Category `P` (Dấu câu) hoặc `S` (Ký hiệu) như dấu phân cách cảnh `*`, `****`, `―`, `…`, `◆`. Khi mô hình dịch rỗng hoặc bỏ sót, hệ thống tự động bảo toàn nguyên tác và khoảng trắng gốc (`preserve_whitespace`), không phát sinh lệnh gọi phục hồi không cần thiết. Trong giai đoạn thẩm định, nếu reviewer trả về chuỗi sửa đổi rỗng cho ký hiệu, ký hiệu được khôi phục nguyên vẹn và ghi nhận trạng thái đã thẩm định bền vững (`review_data`).
- **Văn bản ngôn ngữ thông thường (Chữ, số, văn bản Hàn/Việt)**: Tuyệt đối KHÔNG fallback về nguyên tác tiếng Hàn hay sao chép số/ngôn ngữ. Nếu dịch rỗng, phân đoạn này bắt buộc phải đi vào quy trình phục hồi có mục tiêu (`targeted recovery`). Nếu sau 2 lần phục hồi vẫn rỗng, hệ thống báo lỗi rõ ràng `ProviderEmptyContentError` để người dùng biết chính xác phân đoạn có vấn đề.

### 3.2. Không độc hóa bộ nhớ đệm (Cache Poisoning Quarantine & Container Validation)
- Khi đọc bộ nhớ đệm bản dịch (`load_memory`) hoặc bộ nhớ đệm thẩm định (`load_review_memory`):
  - Dữ liệu đệm được kiểm tra tính hợp lệ toàn diện trước khi sử dụng. Đối với bộ nhớ đệm thẩm định, bắt buộc cả hai trường `corrections` và `issues` phải hiện diện dưới dạng từ điển (`dict`). Bất kỳ bản ghi nào thiếu trường (như chỉ có `kind` và `version`) đều bị từ chối tuyệt đối, không cho phép bỏ qua bước thẩm định.
  - Nếu bản ghi trong cơ sở dữ liệu bị hỏng, thiếu trường bắt buộc, rỗng, hoặc không hợp lệ, hệ thống ghi log cảnh báo và **cách ly (quarantine)** bằng cách xóa bản ghi hỏng khỏi bảng `translation_memory`.
  - Tiến trình tiếp tục gọi API provider tươi (fresh request) để hoàn thành công việc, ngăn chặn triệt để tình trạng lỗi vĩnh viễn hoặc bỏ qua thẩm định khi người dùng bấm Thử lại (Retry).

### 3.3. Tính nguyên tử và bảo toàn tiến trình (Atomicity & Progress Preservation)
- Khi một đợt thẩm định gặp sự cố sau khi đã thử lại, không có bản ghi nào bị ghi đè cục bộ. Bản dịch hiện tại trong cơ sở dữ liệu được bảo toàn nguyên vẹn 100%.
- Khi tạm dừng (`pause`) hoặc khôi phục (`resume`), đợt xử lý đang thực hiện được hoàn thành trọn vẹn và lưu vào cơ sở dữ liệu trước khi chuyển sang trạng thái `paused`.
- Khi thử lại sau khi tóm tắt thất bại (`summary failure`), giai đoạn dịch và thẩm định đã hoàn thành sẽ không bị thực hiện lại (0 lần gọi translate/review thừa).
