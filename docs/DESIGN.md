# Trạm Dịch visual contract

Reference: workbench-concept.png (built-in Image Gen ui-mockup).
Pure white #fff main, navy #14233e text, #0866f5 primary, #f6f9fd chapter rail, #dbe3ee borders, #6b7d95 muted. 68px header, 260px chapter rail, 28px main gutters. System sans-serif chrome 14px, page heading 30px bold, reading text 18px / 1.8. Button radius 6px; pane radius 6px; thin lines; no card grids or marketing.
Desktop: persistent top header brand/book/upload/export, chapter sidebar with search/progress, main heading/toolbar, tabs, continuous whole-chapter reading document with novel prose formatting, view mode toggle (Bản dịch Tiếng Việt / Bản gốc Tiếng Hàn), explicit Chỉnh sửa mode, sticky save footer. Mobile: header wraps, chapter list opens in a searchable drawer above the header, document and editor responsive to mobile width.
Copy: Trạm Dịch; Tải EPUB; Xuất EPUB; Tìm chương…; Mục lục chương; Dịch chương; Tạm dừng; Tiếp tục; Bản dịch; Story Bible; Công việc; Bản dịch (Tiếng Việt); Bản gốc (Tiếng Hàn); Xem bản đọc; Chỉnh sửa; Chưa có bản dịch cho chương này. Chọn Dịch chương để bắt đầu.; Lưu chỉnh sửa; Đã lưu.
Functional additions allowed: book selector, model setting, Dịch toàn bộ with confirmation of chapter range, analyze Story Bible button, retry, errors/review status. Sample book name/text in mockup are illustrative: actual uploaded book title/chapter text MUST replace them. Empty first run must show upload prompt and no fabricated chapters.

## Final visual review (Chapter Workflow Update)

Compared the corrected whole-chapter workflow with desktop and mobile design contracts:

1. Layout: retained header, chapter rail, toolbar/tabs, and fixed save footer. Replaced paired side-by-side sentence rows with a continuous whole-chapter reading document as the primary UI. Mobile drawer preserved.
2. Typography: navy headings at 30px, 14px chrome controls, 18px / 1.8 novel prose reading text with paragraph spacing and heading hierarchy (H1-H3).
3. Colors: white canvas, pale blue-gray rail, slate borders and cobalt actions match the token contract.
4. Controls: view switcher ("Bản dịch (Tiếng Việt)" vs "Bản gốc (Tiếng Hàn)") and explicit "Chỉnh sửa" mode toggle ("Xem bản đọc" / "Chỉnh sửa") allow reading and editing without visual competition.
5. Content and spacing: 260px rail and 28px desktop gutters retained. Safe DOM grouping joins adjacent text/tail nodes belonging to the same paragraph, preventing inline tags from fragmenting sentences onto new lines. When toggled, draft edits are immediately rendered in reading mode.
6. Chapter-level notices: untranslated chapters display a single clean chapter-level notice card without empty textareas or placeholders. Partial chapters are clearly labeled and show only saved translated content in original order. No invented translations or Korean fallback into Vietnamese prose.
