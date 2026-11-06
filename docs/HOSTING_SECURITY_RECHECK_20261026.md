# Rà soát chuẩn bị host Trạm Dịch — 26/10/2026

Trạng thái: CHƯA SẴN SÀNG PUBLIC; chưa triển khai Railway, chưa commit/push và chưa di chuyển dữ liệu thật. Kiến trúc mục tiêu ở `docs/HOSTING_PIPELINE.md`: private GitHub + CI + một Railway Docker service + một Uvicorn worker + một persistent volume `/app/data` + HTTPS cùng origin. Mục tiêu có nhiều tài khoản cùng hiệu đính.

## Phát hiện xác minh trực tiếp trong source

1. `backend/app/auth.py::get_current_user_from_request`: tin vai trò ghi trong session token; chưa tra lại `AUTH_USERS` khi mỗi request tới. Người bị hạ quyền hoặc xóa tài khoản có thể dùng quyền cũ đến khi hết token. Sửa bằng xác minh chữ ký và hạn rồi tìm lại user hiện hành, từ chối nếu bị xóa và lấy role hiện hành.
2. `backend/app/auth.py::can_user_access_endpoint`: editor bị chặn một số URL nguy hiểm nhưng cuối hàm `return True`, mở đường cho những endpoint mutation chưa được liệt kê. Đổi thành allowlist chính xác theo method và đường dẫn thực sự được phép; mặc định deny; admin vẫn toàn quyền, viewer read-only.
3. `backend/app/main.py`: middleware cho phép `auth_override_local_only` cấp admin không kiểm tra loopback peer. `get_current_user_from_request` cũng cấp local admin khi bật biến đó. Khóa override chỉ khi truy cập trực tiếp loopback, không dùng `X-Forwarded-*` để chứng minh loopback; tắt trong Railway. Bảo vệ cả `/api/auth/me` và chỉnh sửa chương.
4. `backend/app/main.py`: logout được miễn CSRF. Nếu dùng cookie session, yêu cầu token CSRF khi logout như các request ghi khác (hoặc cân nhắc chính sách cụ thể với test).
5. `backend/app/auth.py`: thiếu yêu cầu `SESSION_SECRET` cố định khi chạy production; fallback là bí mật sinh ngẫu nhiên mỗi process. Cookie Secure đang dựa vào `X-Forwarded-Proto` do client có thể gửi. Khi public: bắt buộc secret cấu hình đủ dài; `COOKIE_SECURE=true` ở Railway; chỉ tin proxy headers từ proxy đáng tin, không tự tin header do client truyền. Không đưa secret vào repo hoặc test report.
6. `backend/app/auth.py`: `verify_password` còn so sánh trực tiếp plain text khi hash không đúng định dạng; chỉ chấp nhận hash mật khẩu mạnh cho cấu hình production.
7. `backend/app/db.py::update_chapter_segments`: đọc revision rồi `UPDATE ... WHERE id = ?` không ràng buộc revision; hai request cạnh tranh có thể không đạt atomic CAS theo yêu cầu. Giao Antigravity bổ sung transaction phù hợp (`BEGIN IMMEDIATE` hoặc CAS guarded), `UPDATE ... WHERE id = ? AND revision = ?`, phát hiện rowcount=0, rollback toàn batch khi conflict và trả 409 với phiên bản hiện tại. Kiểm thử cạnh tranh thực bằng hai connections vào temp SQLite database. Không được sửa dữ liệu production.
8. `frontend/src/api.js`, `frontend/src/App.jsx`, `frontend/src/useWorkbench.js`: chưa có flow `/api/auth/me` → login/logout, header CSRF cho mọi mutation (kể cả FormData/upload/export), role-aware UI, expected_revision cho PATCH và xử lý 409 an toàn giữ lại draft của người dùng. Backend đang yêu cầu expected_revision khi auth thật, nên frontend hiện chưa dùng được trong môi trường nhiều người.

## Quy trình giao Antigravity / kiểm thử

- Theo `E:/Comic/AGENTS.md` và `E:/Comic/Code/AGENTS.md`, Codex chỉ lập kế hoạch, điều phối và chạy tests; **Antigravity CLI** sửa code/tests. Chỉ sửa file trong `E:/Comic/Code`, giữ nguyên staged/untracked khác; không truy cập `.env`, dữ liệu dịch, EPUB, jobs/checkpoints hay API key; không triển khai công khai.
- Chia nhỏ từng lượt AGY: (a) session revalidation/editor allowlist, (b) override/secret/cookie/CSRF backend, (c) transaction CAS và test đối kháng, (d) frontend auth/role/CSRF/revision/conflict. Xác nhận diff rồi mới sang lượt kế. CLI từng trả `print timeout ... turn in progress` mà không có bản vá; log 26/10 ghi nhận silent OAuth thành công, nhưng model stream chưa hoàn tất trước timeout. Không xem timeout là thành công hoặc là bị chặn quyền.
- Nghiệm thu: anonymous 401, viewer read-only, editor không kích hoạt Gemini/upload/settings, removed/demoted token mất quyền, remote override vô hiệu; mutation với cookie thiếu CSRF phải 403, đúng CSRF mới được xử lý; hai editors sửa cùng segment: một ghi thành công, một 409, không lost update/partial batch; UI login+logout hoạt động, 409 giữ draft; frontend `pnpm test` và `pnpm build`; backend pytest ở temp DB; Docker/CI khi môi trường hỗ trợ.

## Những gì cần trước public launch

- Nguồn Git sạch và được review, private repo + CI; staging Railway với 1 replica, 1 worker và persistent volume, HTTPS, `COOKIE_SECURE=true`, secret do chủ dự án nhập trên Railway dashboard.
- SQLite online backup + integrity/count/hash + khôi phục bản sao cô lập; chỉ chuyển snapshot hiện hành và EPUB cần thiết; backup offsite; cutover bảo đảm chỉ có một nguồn ghi.
- Chưa có bằng chứng các gate bảo mật và migration data đã đạt. Tuyệt đối không bật truy cập Internet tới dữ liệu thật ở trạng thái hiện tại.

## Tiến độ sửa và xác minh ngày 26/10 (sau báo cáo ban đầu)

- Đã giao Antigravity sửa `backend/app/auth.py`: session token sau khi kiểm tra chữ ký và hạn được đối chiếu với `AUTH_USERS` hiện tại; tài khoản bị xóa bị từ chối và vai trò bị hạ quyền được áp dụng ngay.
- Đã giao Antigravity sửa `can_user_access_endpoint`: Editor có danh sách các mutation được phép theo method và đường dẫn đầy đủ, mặc định từ chối URL khác; Viewer có thể đăng xuất, Admin toàn quyền. Kiểm tra độc lập AST và 12 trường hợp phân quyền cộng kiểm tra xóa/hạ vai trò đã đạt.
- Phát hiện rủi ro kiểm thử: import `backend.app.main` tạo `app = create_app(start_worker=True)` ở module scope. Trước khi pytest, đặt `DATA_DIR` sang thư mục thử nghiệm cô lập để tránh chạm DB thật; không gọi backend qua lifecycle với key thật.
- Đã xác minh `backend/app/db.py::update_chapter_segments` có `BEGIN IMMEDIATE` trước thao tác đọc và `UPDATE ... WHERE id = ? AND chapter_id = ? AND revision = ?` (phát hiện `rowcount == 0`, ném `RevisionConflictError`; `get_connection` rollback khi lỗi). Chưa có chứng cứ kiểm thử cạnh tranh thực với hai kết nối SQLite; cần xử lý input trùng segment ID và kiểm tra regression.
- Antigravity đã bổ sung `backend/app/auth.py::is_direct_loopback_request` so sánh `request.client.host` với `127.0.0.1` hoặc `::1`; `get_current_user_from_request` chỉ cấp `local_admin` khi bật override và peer là loopback. Đã rà source và chạy kiểm tra cú pháp thành công. `main.py` còn cần áp dụng nhất quán điều kiện này trong middleware, `/api/auth/me`, PATCH chapter và CSRF logout.
- Lượt giao Antigravity sửa rộng `main.py` trả trạng thái `print timeout after 2m0s with turn in progress` lúc 15:21, chưa quan sát được thay đổi file. Cần kiểm tra lại nội dung file trước khi coi công việc đó hoàn tất.
- `frontend` qua 24/24 ca `pnpm test` và `pnpm build` thành công ngày 26/10. Luồng đăng nhập, CSRF, expected_revision, giữ draft khi 409 chưa được bổ sung/chứng minh.
- Backend `pytest backend/tests/test_hosting_ci.py` qua 3 bài đầu; bài `test_health_endpoint_contract_and_no_leak` vẫn bị treo qua `TestClient` ngay cả khi đặt DATA_DIR và --basetemp cô lập; lượt kiểm thử được dừng. Chưa xác nhận backend full suite. Chưa triển khai công khai.

## Xác minh tiếp — 26/10, lượt chuẩn bị giao diện đăng nhập

- `frontend/src/api.js`: Antigravity đã bổ sung `credentials: same-origin` và `X-CSRF-Token` khi đã có session cho cả `uploadBook()` (`POST /api/books`) và `exportBookPdf()` (`POST /api/books/{id}/export/pdf`); giữ tự động multipart boundary của `FormData`.
- `frontend/src/AuthGate.jsx`: Antigravity đã tạo màn hình đăng nhập sử dụng `/api/auth/me`, `/api/auth/login` và `/api/auth/logout`, khôi phục phiên sau reload; `frontend/src/main.jsx` đã render `AuthGate` trong `PreferencesProvider`. Đây là kiểm tra mã nguồn và build, chưa có kiểm thử UI bằng trình duyệt/các vai trò thực tế.
- Kiểm thử lại: `pytest backend/tests/test_hosting_ci.py backend/tests/test_collaboration.py -q -p no:cacheprovider` với `DATA_DIR` và `--basetemp` riêng biệt: 13 passed, 1 cảnh báo thư viện, 4.59 s; frontend `pnpm test`: 24/24 passed; `pnpm build`: thành công. Không sử dụng dữ liệu thật.
- Chưa hoàn tất: `AuthGate` vẫn ẩn giao diện ngay cả khi API logout thất bại; chưa có bằng chứng phần UI phân quyền theo role hoặc hiển thị xung đột 409 hữu ích. Lượt giao Antigravity chỉnh `auth.py` fail-closed ở production (`APP_ENV`, `SESSION_SECRET`, cookie Secure, mật khẩu băm) và lượt loại bỏ segment ID trùng trong batch `db.py` đều hết thời gian in CLI và không có thay đổi quan sát được. Các mục này vẫn đang mở.
- Vẫn **CHƯA SẴN SÀNG PUBLIC**: chưa xác minh Docker runtime/staging, cấu hình Railway và sao lưu/khôi phục SQLite, chưa push hay deploy; phải hoàn tất cổng bảo mật production và tests role/CSRF/concurrency trước khi mở truy cập.

## Xác minh bổ sung ngày 26/10, sau 17:00

- Xác minh trực tiếp `backend/app/main.py`: trong `APP_ENV=production`, `create_app()` kiểm tra `SESSION_SECRET` dài tối thiểu 32 ký tự trước khởi tạo DB và chặn local auth override nếu biến môi trường bật hoặc tham số truyền vào `True`, kể cả khi tham số khác đặt `False`. `backend/app/auth.py::verify_password` từ chối fallback mật khẩu plaintext trong production.
- Antigravity cập nhật `backend/app/auth.py::get_configured_users` để ném lỗi khi `AUTH_USERS` chứa khóa `password` hoặc `AUTH_ADMIN_PASSWORD` chứa plaintext trong production. Chạy probe chỉ với tài khoản giả `audit_demo` cho kết quả `ValueError: Plaintext AUTH_USERS passwords forbidden in production`. Cần tiếp tục xác thực định dạng `password_hash`, kiểm tra cấu hình người dùng ngay tại startup và kiểm thử cấu hình malformed/empty.
- Antigravity cập nhật `frontend/src/api.js::logout` để chỉ xóa CSRF token sau khi API logout thành công; lỗi mạng/403 vẫn giữ token trong bộ nhớ. `frontend/src/AuthGate.jsx` chỉ đặt user về null khi logout thành công.
- Kiểm thử lại sau thay đổi: backend `backend/tests/test_hosting_ci.py` + `test_collaboration.py` **13 passed**, 1 cảnh báo thư viện Starlette, dùng thư mục `DATA_DIR`/`--basetemp` mới và không dùng DB thật; frontend `pnpm test` **24 passed**, `pnpm build` thành công (Vite 1614 modules).
- Antigravity bổ sung `frontend/src/api.js::request()` để hiển thị `data.detail.message` khi FastAPI trả HTTP 409 với `detail` dạng object. Probe Node dùng `fetch` giả xác nhận **CONFLICT_MESSAGE_VISIBLE=true** và **CSRF_PRESERVED_ON_FAILED_LOGOUT=true**. `pnpm test` vẫn **24 passed** và `pnpm build` thành công sau thay đổi.
- Những việc đang mở: frontend chưa gắn metadata `conflicts` có cấu trúc hoặc màn hình merge/diff, UI chưa phân quyền hiển thị theo `admin/editor/viewer`; kiểm thử hai kết nối SQLite thực, startup production, end-to-end trình duyệt, Docker/staging và backup/restore còn thiếu. Những mục này không được tính là đã nghiệm thu.
- Git working tree có nhiều tệp staged/untracked từ các luồng công việc khác. Không dọn Git, commit/push hay triển khai công khai. Không truy cập credentials, EPUB hoặc dữ liệu thật.


## Kiểm chứng nối tiếp cuối ngày 26/10 — phân quyền giao diện và CLI

- Đã đọc trực tiếp hai tệp hướng dẫn hiện hành `E:/Comic/AGENTS.md` và `E:/Comic/Code/AGENTS.md`. Toàn bộ thay đổi mã ở lượt này do Antigravity CLI thực hiện; Codex chỉ kiểm tra, chạy tests và cập nhật biên bản.
- Antigravity chạy trong sandbox mặc định gặp `You are not logged into Antigravity` rồi `authentication timed out` do không truy cập được dữ liệu xác thực. Chạy tuyến quyền `require_escalated` được duyệt và kiểm tra kết nối tối thiểu đã nhận `AGY_CONNECTED`. Tuy nhiên các yêu cầu backend `auth.py` kiểm tra cấu hình production nhiều điều kiện đều `print timeout` và tệp vẫn có mtime 17:12:03, nên tuyệt đối chưa coi là đã sửa.
- Đã sửa bằng Antigravity và kiểm tra nội dung: `AuthGate.jsx` truyền `user` xuống `App`; `App.jsx` nhận `user`, truyền `readOnly` cho `EditorPane`, ẩn `SaveFooter` cho `viewer`; `EditorPane.jsx` có prop `readOnly=false` và ngăn render ô nhập khi `readOnly=true`. Đây chỉ là bảo vệ hiển thị một phần; nút chuyển sang chỉnh sửa chưa bị khóa, các điều khiển jobs/upload/model/presets và các màn hình khác chưa phân quyền đầy đủ. Quyền ghi thật phải tiếp tục được kiểm tra bởi backend.
- Kiểm thử dùng `DATA_DIR` + `--basetemp` hoàn toàn mới, `APP_ENV=development`, vô hiệu hóa khóa Gemini và local auth override. Chạy bằng tuyến thực thi đã kiểm chứng: `backend/tests/test_collaboration.py` **8 passed**, `backend/tests/test_hosting_ci.py` **5 passed**; 1 cảnh báo khấu hao Starlette/TestClient. Frontend sau các sửa đổi có `pnpm test` **24/24 passed** và `pnpm build` thành công (1614 modules). Các test frontend hiện chưa bao phủ tương tác RBAC/409 qua trình duyệt.
- Vẫn thiếu: production fail-closed validation của `AUTH_USERS` (JSON root, entries, role, tên trùng và định dạng hash), `AUTH_ADMIN_PASSWORD_HASH`, kiểm thử startup production với tài khoản giả; toàn bộ UI admin/editor/viewer; giải quyết 409 giữ nháp; kiểm thử xung đột hai kết nối SQLite độc lập, Docker/staging và backup/restore được kiểm chứng. Không deploy, không chuyển DB/EPUB thật, không commit/push.
- Kết luận nghiệm thu: **CHƯA SẴN SÀNG PUBLIC**. Lần tiếp theo ưu tiên cho Antigravity các thay đổi một tệp/ít dòng, xác minh mtime/nội dung sau mỗi lần; trước khi cấp domain phải hoàn tất cổng bảo mật và các bài kiểm thử phù hợp.

