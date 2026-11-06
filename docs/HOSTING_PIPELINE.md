# Pipeline triển khai Trạm Dịch cho nhóm cộng tác

> Bản kế hoạch ngày 25/10/2026. Trạng thái: **chưa triển khai công khai**. Mục tiêu: một URL HTTPS, nhiều người đăng nhập và cùng hiệu đính trên một dữ liệu bền vững, máy Windows có thể tắt.

## 1. Quyết định kiến trúc

**Bản đầu tiên (khuyên dùng):** private GitHub repository → GitHub Actions (kiểm thử) → **Railway Hobby**, một Docker service chạy **FastAPI phục vụ React đã build** và **một worker** → Railway persistent volume `/app/data` chứa SQLite/EPUB. Đăng nhập và phân quyền được thực thi trong chính ứng dụng. Dùng HTTPS domain do Railway cấp trước, có thể thêm custom domain sau.

```text
Editor A ─┐
Editor B ─┼─ HTTPS ─ Railway ingress ─ FastAPI (React + REST API + 1 worker)
Admin    ─┘                                  │
                                       /app/data (volume)
                                       ├── translator.db (+ WAL)
                                       └── <book-id>.epub
                                                │
                                          Gemini API (server-side)

Code: local checkout → reviewed commit → private GitHub → CI → staging → production
Data: online SQLite backup → integrity + hashes → import → verify → single cutover
```

**Vì sao chưa tách Vercel?** Frontend hiện đã được FastAPI phục vụ (`/assets`, fallback React), client gọi API tương đối `/api`; một domain tránh CORS/session cross-site và giảm dịch vụ vận hành. Nếu cần giao diện ở dạng `tram-dich.vercel.app` về sau, có thể đưa React lên Vercel nhưng FastAPI + volume + worker vẫn ở Railway; cần cấu hình API URL, CORS, cookie/CSRF và kiểm soát toàn bộ API trước khi tách.

**Giới hạn:** Dùng **một replica và một Uvicorn worker** khi còn SQLite/worker nền nội bộ; không chạy cùng lúc local và Railway cùng ghi vào hai bản DB. SQLite WAL phù hợp nhóm nhỏ nhưng không thể gắn cùng volume cho nhiều replicas. Khi tải thực tế tăng, thiết kế chuyển PostgreSQL và worker/queue tách riêng.

## 2. Hiện trạng xác nhận trong checkout

| Thành phần | Kết quả | Việc cần làm |
| --- | --- | --- |
| Frontend | React/Vite; `frontend/src/api.js` gọi `/api` | Giữ cùng origin trong bản đầu |
| API | `backend/app/main.py`, FastAPI; `GET /api/health` | Bảo vệ tất cả API đọc/ghi nhạy cảm |
| Database | `backend/app/db.py` dùng SQLite WAL; `data/translator.db` 130.981.888 byte (lúc kiểm tra) | SQLite online backup, kiểm tra tính toàn vẹn |
| Tài liệu truyện | `data/<book-id>.epub`, `books.file_path` có đường dẫn cũ; mã export đã fallback theo book ID | Chuyển tệp EPUB tương ứng, test export |
| Tác vụ AI | `JobWorker` chạy bên trong app, khôi phục việc dang dở khi khởi động | 1 instance, kiểm tra hành vi shutdown/restart và checkpoint |
| Docker | `Dockerfile` multi-stage và `compose.yaml` | Đã cập nhật `COPY fonts/ ./fonts/`, dynamic `PORT`, 1 uvicorn worker |
| Quyền trên volume | Railway mount volume dưới root, image cần chạy UID 1000 | Đã xử lý qua entrypoint + `gosu`: khởi động kiểm tra/chown thư mục `$DATA_DIR` cho `translator`, drop privilege về UID 1000, không chmod 777, không đổi tệp trong `data/` |
| Auth/conflict | Chưa có user/login/role/revision; PATCH chương ghi bản dịch hiện tại | Bắt buộc bổ sung trước public launch |
| Backup lịch sử | 67 tệp `translator*` trong `data/` cộng ~5.613.162.496 byte | Không chuyển toàn bộ history lên Railway Hobby volume mặc định 5 GB |
| Công việc đang làm | Working tree Git có nhiều thay đổi staged/untracked | Giữ nguyên; không `git add .`, không push khi chưa rà soát |

Điểm quan trọng: `.gitignore` và `.dockerignore` đã loại `data/` và `.env`. Kiểm tra lại những đường dẫn mới phát sinh và nội dung commit trước khi tạo remote. **Không** tải `.env`, API key, nguyên EPUB hay SQLite lên GitHub/image/build log.

## 3. Các mốc và điều kiện nghiệm thu (gates)

### G0 — Chốt bản nguồn và an toàn dữ liệu (trước khi sửa code)

1. Thống kê schema, bản dịch, số sách/chương/segments, trạng thái jobs/audit, dung lượng file thực; ghi nhận mốc SHA-256 file EPUB và snapshot. Không in nội dung bản dịch hay credential vào log công khai.
2. Dùng API SQLite `Connection.backup()` lấy snapshot nhất quán khi DB đang dùng; không copy riêng tệp `.db` khi WAL có thể đang ghi. Kiểm tra `PRAGMA integrity_check`, số lượng records quan trọng, thử phục hồi trên **bản sao cô lập**. Sao lưu bảo mật ngoài máy chủ đích.
3. Xem lại mọi thay đổi hiện có trong Git. Tạo nhánh triển khai riêng từ một commit đã rà soát, bảo vệ data và việc của các agent khác; xác nhận `.env`, `data/`, log, EPUB nằm ngoài Git/image.

**Đạt:** có snapshot phục hồi được, có đối chiếu số bản ghi, có commit nguồn đã duyệt. **Dừng:** không có snapshot nhất quán / chưa xác định được phiên bản code.

### G1 — Làm ứng dụng an toàn cho cộng tác (Antigravity CLI triển khai)

1. **Danh tính:** email invite-only hoặc OIDC; session cookie `HttpOnly`, `Secure`, `SameSite`, hạn sử dụng; CSRF protection cho các request ghi; đăng xuất/thu hồi session; secret từ environment. Tất cả endpoint đọc/ghi sách, chương, thuật ngữ, story bible, cài đặt, job, audits và xuất EPUB/PDF phải kiểm tra đăng nhập.
2. **Quyền:** `admin` (cấu hình, upload, Gemini jobs, quản lý cộng tác viên), `editor` (đọc/sửa bản dịch và thuật ngữ theo quyền được giao), `viewer` (chỉ xem/xuất theo chính sách); quyền enforced ở backend, không chỉ ẩn nút frontend. Hạn chế chi phí API: người thường không được tự gọi job trả phí.
3. **Chống ghi đè:** cấp `revision` cho bản ghi chỉnh sửa (ít nhất từng segment; glossary/story bible cũng cần chính sách xung đột). Khi PATCH truyền `expected_revision` và thực hiện `UPDATE ... WHERE id=? AND revision=?` trong transaction; không trùng phiên bản trả HTTP **409** với trạng thái mới nhất, UI hiển thị diff và cách tải lại/hợp nhất. Tránh `last write wins`.
4. **Nhật ký chỉnh sửa:** ai, khi nào, sách/chương/segment, old/new revision và nội dung cần đủ để khôi phục; chỉ admin xem lịch sử đầy đủ. Có UI xác nhận trước khi khôi phục. Ghi audit cùng transaction của edit.
5. **Tác vụ nền:** 1 worker duy nhất; quyền riêng cho start/pause/resume/retry, bảo vệ checkpoint và trạng thái jobs; kiểm tra shutdown/restart trong lúc có việc, không tự phát sinh lần gọi Gemini chỉ từ test.
6. **Đóng gói & hardening:** include `fonts/` vào Docker image, cấu hình `PORT` đúng Railway, thông báo lỗi upload theo giới hạn, không trả stack trace/khóa, giới hạn kích thước file, an toàn cookie/CSRF và health endpoint chỉ chứa thông tin tối thiểu. Kiểm thử quyền ghi volume lúc runtime.

**Kiểm thử bắt buộc:** khách không đăng nhập không thể `GET /api/books` hay PATCH; viewer bị 403 khi sửa/gọi job; editor chỉ được thao tác đúng quyền; hai editor đọc cùng revision thì một update thành công và update cũ nhận 409, không mất nội dung; audit có user; PDF (tiếng Việt/Hàn) và EPUB export thành công; tác vụ AI được mock trong CI.

**Đạt:** security + concurrency + export integration pass. **Dừng:** bất kỳ endpoint nhạy cảm nào truy cập ẩn danh được hoặc ghi đè im lặng. Cloudflare Access có thể tăng bảo vệ nhưng không thay thế app auth, vì endpoint Railway có thể truy cập trực tiếp.

### G2 — CI và staging an toàn

1. GitHub repo **private**, nhánh `main` cho production và `develop`/nhánh staging. GitHub Actions chạy khi mở PR: `pytest` (backend), frontend test/build (`pnpm install --frozen-lockfile`, `pnpm build`), kiểm tra Docker build, secrets scan, smoke auth/concurrency không dùng Gemini thật. Đặt `main` branch protection.
2. Tạo Railway project với **staging service + staging volume** tách dữ liệu. Deploy Dockerfile từ nhánh staging hoặc manual; đặt Variables qua dashboard, không commit credential. Trước khi có app auth, chỉ test ở môi trường không public và với dữ liệu demo, không đưa bản dịch thật lên host public.
3. Cấu hình Railway `DATA_DIR=/app/data`, `GEMINI_MODEL=gemini-3.5-flash-lite`, secrets phù hợp `GEMINI_API_KEY` (đặt khi thực sự cần), và các biến auth **sau khi Antigravity định nghĩa** (ví dụ `SESSION_SECRET`, `APP_BASE_URL` — chưa được code hiện tại hỗ trợ). Dùng `PORT` do Railway cấp, bind `0.0.0.0`.
4. Railway volume mount **`/app/data`**; kiểm tra quyền ghi/read trước start worker. Theo tài liệu Railway, volume mount bằng root; với image non-root có thể dùng `RAILWAY_RUN_UID=0` như giải pháp platform, nhưng ưu tiên xử lý ownership an toàn qua entrypoint được kiểm thử rồi drop về UID hạn chế.
5. Railway Networking → Generate Domain (domain tạm), configure healthcheck `/api/health`, một replica, một worker. Cho chạy test demo theo cùng-origin. Kiểm tra crash/restart/deploy với volume giữ dữ liệu; gắn cảnh báo lỗi và theo dõi quota.

**Đạt:** commit sạch tạo Docker image chạy được; staging login và API hợp lệ, file bền vững qua restart, auth gate xanh. **Dừng:** volume permission error, thừa quyền, mất dữ liệu hoặc lỗi export.

### G3 — Di chuyển dữ liệu thực (cutover có kiểm soát)

1. **Trước cutover:** nhắc nhóm ngừng sửa; đợi hoặc chủ động tạm dừng các job qua UI để giữ checkpoint. Chặn thao tác ghi trên máy Windows cũ. Ghi lại lần cuối số sách/chương/segment, jobs/audit và hash của EPUB.
2. Tạo **online snapshot nhất quán cuối** bằng SQLite backup API, kiểm tra `integrity_check` rồi giữ bản gốc/snapshot bất biến ở máy nguồn và thêm một bản offsite đã mã hóa.
3. Chỉ đưa lên volume mới: `translator.db` snapshot + các `<book-id>.epub` thực sự tham chiếu; không đưa logs, hàng chục snapshot cũ, `.env`, những output thử nghiệm. Dùng upload/browse Railway volume hoặc quy trình import riêng ở **maintenance mode**, khi app/worker không mở DB để ghi; tuyệt đối không ghi đè DB trong lúc service đang hoạt động. Nếu init DB trước tạo DB trống, dừng worker và thay thế đúng DB sau khi quiesce.
4. Ứng dụng đã có logic export fallback về `/app/data/<book-id>.epub` khi đường dẫn Windows trong `books.file_path` không tồn tại; test từng trường hợp thật. Có thể chuẩn hóa đường dẫn trong **bản sao migration** nếu cần, nhưng không sửa DB gốc.
5. Sau import: `PRAGMA integrity_check`, số books/chapters/segments/translations/jobs/audit so với snapshot, kiểm tra file EPUB và checksum, mở các chương đại diện, tải PDF và EPUB, test login/editor/409. Kiểm tra server không tự chạy lại tác vụ dịch ngoài ý muốn.
6. Chỉ khi mọi gate xanh mới mở URL cho cộng tác viên. Máy Windows cũ chuyển **read-only/archive** để tránh hai nguồn dữ liệu khác nhau; nếu cần đảo chiều, khóa ghi bên Railway, snapshot Railway và chọn nguồn khôi phục duy nhất.

**Đạt:** dữ liệu đầy đủ, verified, người dùng có thể đọc/sửa và không còn writer thứ hai. **Dừng:** lệch checksum/tổng số bản ghi hoặc worker không kiểm soát được.

### G4 — Vận hành hàng ngày và CI/CD

- **Luồng thay đổi:** feature branch → PR → CI/test → merge nhánh staging → staging smoke → duyệt release → merge `main` → CI → deploy Railway production có kiểm soát. Railway có tính năng `Wait for CI`; production nên ưu tiên approval/manual deployment khi có job dài.
- **Deploy production:** tạm dừng/kiểm tra jobs dài, backup trước thay đổi schema, bảo đảm migrations tương thích; healthcheck `/api/health`, sau deploy kiểm thử đăng nhập và API có quyền, một thao tác đọc thực, xuất mẫu. Railway volume-backed service sẽ có downtime ngắn khi thay deployment.
- **Sao lưu:** bật snapshot volume daily + weekly trên Railway. Bổ sung snapshot SQLite thông qua API backup vào lưu trữ **ngoài Railway** (mã hóa), kèm tệp EPUB và manifest/checksum, tối thiểu mỗi ngày; thử phục hồi định kỳ trên môi trường riêng. Volume backup không thay thế offsite backup và không phải Git rollback.
- **Giám sát:** uptime, HTTP 5xx, auth failures, dung lượng volume, lượng request Gemini/cost, job lỗi/đứng, số bản dịch mới; log được lọc credential/nội dung nhạy cảm. Quy định ai có quyền xem log.
- **Hạn chế:** không scale web/worker >1 khi vẫn SQLite; không chạy migration phá schema không tương thích với rollback. `Rollback` Railway đổi lại image/variables, **không tự khôi phục database**; DB schema/data cần snapshot riêng.
- **Cứu hộ:** đóng thao tác ghi → chụp trạng thái hỏng nếu cần điều tra → restore snapshot dữ liệu đã kiểm thử trên môi trường cô lập → xác minh count/checksum → chuyển dịch vụ lại. Có runbook và ghi rõ mốc khôi phục.

## 4. Checklist theo vai trò

| Người thực hiện | Hạng mục |
| --- | --- |
| Codex | Điều phối, review diff, chạy check read-only và kiểm thử, kiểm soát dữ liệu/snapshot, nghiệm thu gates |
| Antigravity CLI (`gemini-3.8-flash-high`) | Viết auth/roles/revisions/audit, test, điều chỉnh Dockerfile/start và quy trình backup/migration; tuân thủ AGENTS.md |
| Chủ dự án | Tạo/đăng nhập GitHub và Railway, cấu hình thanh toán/gói, sở hữu secrets và chọn email cộng tác viên; duyệt thời điểm mở URL và cutover dữ liệu thật |
| Cộng tác viên | Đăng nhập account được mời, chỉnh sửa đúng quyền, xử lý 409, báo lỗi có metadata nhưng không gửi key/data riêng tư |

## 5. Các lệnh tham khảo để triển khai (chưa chạy)

Các lệnh dưới đây là mẫu kiểm thử sau khi Antigravity hoàn tất các thay đổi. Không chạy trên dữ liệu sản xuất khi chưa hoàn thành G0–G2.

```powershell
# Tại E:\Comic\Code; dùng môi trường local và kiểm tra thay đổi trước commit
git -c safe.directory=E:/Comic/Code status --short
git -c safe.directory=E:/Comic/Code diff --cached --name-status

# Kiểm thử phù hợp (không gọi Gemini thật)
.\.venv\Scripts\python.exe -m pytest backend/tests -q
cd frontend
pnpm install --frozen-lockfile
pnpm build

# Docker build khi host có Docker; hiện máy Windows đã từng được kiểm tra chưa có Docker
cd ..
docker build -t tram-dich:staging .

# Khi đã cấu hình Railway CLI và xác thực với tài khoản dự án
# railway login
# railway link
# railway up
```

Railway UI: **New Project → GitHub Repo → service dùng Dockerfile → Variables → Add Volume `/app/data` → Settings/Networking → Generate Domain → Healthcheck `/api/health`**. Không giả định `compose.yaml` chạy trực tiếp giống trên Docker Compose; Railway có công cụ import Compose nhưng biến cấu hình/volume/service vẫn phải kiểm tra.

**Lưu ý cập nhật 25/10/2026:** Railway đã ngừng cho dịch vụ mới chọn cơ chế `railway.json`/`railway.toml` Config as Code cũ; với dự án mới, cấu hình trên Railway Dashboard hoặc dùng Infrastructure as Code (`.railway/railway.ts`) sau khi đã có tên project/service chính thức. Không thêm `railway.json` mới vào repo chỉ để cấu hình staging. Tài liệu: [Railway Config as Code](https://docs.railway.com/config-as-code), [Infrastructure as Code](https://docs.railway.com/infrastructure-as-code/reference).

**Lưu ý phục hồi:** Railway volume backup có lịch daily/weekly/monthly, nhưng restore chỉ trong cùng project và environment. Bản sao SQLite nhất quán cùng EPUB ở nơi lưu trữ độc lập vẫn cần thiết. Railway hiện giới hạn manual backup bằng 50% dung lượng volume; đối chiếu trước khi dựa vào snapshot làm bước G0/G4. Tài liệu: [Volume backup](https://docs.railway.com/volumes/backups).

## 6. Chi phí & giới hạn lưu trữ (tra cứu 25/10/2026)

- Railway **Free** có hạn mức volume khoảng **0,5 GB**, RAM 0,5 GB và $1 usage credit/tháng; phù hợp demo nhỏ, không khuyên dùng sản xuất.
- Railway **Hobby** phí nền **$5/tháng**, gồm $5 usage credit; volume mặc định **5 GB**, có thể tăng dung lượng; CPU/RAM/egress/volume và backup được tính theo mức sử dụng. Không coi $5 là trần chi phí tổng.
- `data/translator.db` hiện xấp xỉ **125 MiB**, nhưng tổng backup `translator*` khoảng **5,6 GB**, nên đưa toàn bộ các tệp lịch sử lên volume Hobby mặc định sẽ quá dung lượng; chuyển snapshot hiện hành + EPUB, giữ nhiều bản backup bên ngoài.
- Trước sản xuất, bật budget/notification từ Railway, giám sát cả phí Gemini và lưu lượng export.

Tài liệu chính thức: [FastAPI deploy](https://docs.railway.com/guides/fastapi), [Dockerfiles](https://docs.railway.com/builds/dockerfiles), [Volumes](https://docs.railway.com/volumes), [Volume backup](https://docs.railway.com/volumes/backups), [GitHub Wait for CI](https://docs.railway.com/deployments/github-autodeploys), [Pricing](https://docs.railway.com/pricing/plans), [Rollback](https://docs.railway.com/deployments/deployment-actions).

## 7. Definition of done

- [ ] Chạy HTTPS 24/7 trên Railway; tắt máy Windows vẫn hoạt động.
- [ ] Không tài khoản nào truy cập bản dịch nếu chưa xác thực; roles thực thi đúng trên backend.
- [ ] Hai người sửa một segment không bao giờ âm thầm ghi đè; 409 và hướng giải quyết rõ ràng.
- [ ] Dữ liệu sách/chương/segment/jobs/EPUB đối chiếu đúng snapshot, PDF/EPUB export đạt.
- [ ] 1 replica/1 worker, không phát sinh job trả phí do restart hay smoke test.
- [ ] CI + staging pass; backup hằng ngày và thực hành restore được xác nhận.
- [ ] Có phương án cutover/rollback dữ liệu và nguồn đọc/ghi duy nhất.

## 8. Biên bản chuẩn bị cục bộ — 25/10/2026

| Hạng mục | Kết quả đã kiểm tra |
| --- | --- |
| Khảo sát mã nguồn / kiến trúc | Đã kiểm tra FastAPI phục vụ React từ cùng origin; SQLite WAL và worker nội bộ. |
| Dockerfile và `.dockerignore` | Đã có sẵn trong working tree. Dockerfile hiện thiếu `COPY fonts/`, sử dụng cổng 8000 cố định và chưa chứng minh quyền ghi volume Railway. Cần Antigravity sửa và kiểm thử. |
| CI GitHub Actions | Chưa có workflow tại `.github/workflows/`; cần Antigravity tạo. |
| Kiểm soát cộng tác | Chưa triển khai xác thực, phân quyền hoặc kiểm tra revision chống ghi đè trong API. Không được mở dữ liệu dịch thật ra Internet ở trạng thái này. |
| Antigravity CLI | Lệnh kết nối nhỏ trả `AGY_CONNECTED` khi chạy theo đường cấp quyền được duyệt. Các lượt giao chỉnh sửa Dockerfile/CI chạm thời hạn `--print-timeout` và chưa tạo thay đổi quan sát được. Đây là lỗi hoàn thành tác vụ CLI, chưa có bằng chứng về từ chối quyền. |
| Build/kiểm thử | Máy hiện không tìm thấy Docker CLI. `pnpm --dir frontend test` đạt **16/16**. Bộ pytest cục bộ được khởi chạy nhưng không trả kết quả trước khi chủ động ngắt; chưa thể xác nhận kiểm thử backend đạt. |
| Git / dữ liệu / máy chủ | Working tree đang có nhiều thay đổi sẵn có, chưa commit/push. Không di chuyển hoặc sửa cơ sở dữ liệu, EPUB, khóa bí mật; chưa tạo Railway service/volume hoặc phát hành URL công khai. |

**Ưu tiên triển khai tiếp:** (1) hoàn tất Dockerfile và CI bằng Antigravity; (2) xác thực invite-only, phân quyền và xử lý HTTP 409 khi nhiều biên tập viên sửa đồng thời; (3) chạy test trên dữ liệu demo; (4) tạo private GitHub/Railway staging với tài khoản dự án; (5) thử phục hồi snapshot SQLite và kiểm chứng export rồi mới thực hiện cutover. Các bước cần thông tin thanh toán, tài khoản và secret thực hiện qua bảng điều khiển tương ứng; không đưa secret vào file Git hoặc bản báo cáo.
