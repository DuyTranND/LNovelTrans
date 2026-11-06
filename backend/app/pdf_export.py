"""PDF export engine for Trạm Dịch.

Generates continuous-reading novel PDFs on A4 paper using ReportLab.
Supports Unicode Vietnamese, Korean, Thai, and combining symbols using verified
bundled OFL fonts with per-character/run font fallback.
Includes paragraph grouping, inline formatting preservation, two-pass page numbering,
chapter headings, and book metadata.
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    from reportlab.platypus import HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False


class PdfExportError(Exception):
    """Base exception for PDF export errors."""
    pass


class PdfFontError(PdfExportError):
    """Raised when no suitable Unicode font is found or unsupported glyphs are detected."""
    pass


class IncompleteChapterError(PdfExportError):
    """Raised when attempting to export an incomplete translation."""
    pass


BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "li", "dt", "dd", "pre",
    "figcaption", "caption", "td", "th", "section", "article", "div",
}

GENERIC_BLOCK_CONTAINERS = {"div", "body", "section", "article"}

# Directory for verified open OFL font assets
BUNDLED_FONTS_DIR = Path(__file__).resolve().parent.parent.parent / "fonts"


class LoadedFont:
    """Represents a registered TrueType font with cmap coverage checking."""

    def __init__(self, name: str, path: str, ttfont: Any) -> None:
        self.name = name
        self.path = path
        self.ttfont = ttfont
        face = getattr(ttfont, "face", None)
        self.char_to_glyph: dict[int, Any] = getattr(face, "charToGlyph", {}) or {}

    def supports(self, cp: int) -> bool:
        """Check if font supports a given Unicode code point."""
        if cp in (10, 13, 9):  # newline, carriage return, tab
            return True
        glyph = self.char_to_glyph.get(cp)
        return glyph is not None and glyph != 0 and glyph != ".notdef"


_LOADED_FONTS: dict[str, LoadedFont] = {}


def get_or_register_font(name: str, path: str) -> LoadedFont:
    """Register TTFont with ReportLab and return LoadedFont wrapper."""
    if not HAS_REPORTLAB:
        raise PdfExportError(
            "Thư viện ReportLab chưa được cài đặt. Vui lòng cài đặt ReportLab để xuất PDF."
        )

    if name in _LOADED_FONTS:
        return _LOADED_FONTS[name]

    registered = pdfmetrics.getRegisteredFontNames()
    if name not in registered:
        is_ttc = path.lower().endswith(".ttc")
        kwargs: dict[str, Any] = {"subfontIndex": 0} if is_ttc else {}
        ttfont = TTFont(name, path, **kwargs)
        pdfmetrics.registerFont(ttfont)
    else:
        ttfont = pdfmetrics.getFont(name)

    loaded = LoadedFont(name, path, ttfont)
    _LOADED_FONTS[name] = loaded
    return loaded


def get_font_cascade(custom_font_path: str | None = None) -> tuple[LoadedFont, list[LoadedFont]]:
    """Discover, register, and return (base_font, font_cascade) in priority order.

    Priority:
    1. Bundled verified OFL fonts in Code/fonts (NotoSansKR, NotoSans, NotoSansThai)
    2. Optional explicit custom font (PDF_FONT_PATH) placed at head of cascade
    3. System font fallbacks if bundled fonts are absent
    """
    cascade: list[LoadedFont] = []

    # 1. Bundled open OFL fonts (always preferred over system/Windows fonts)
    kr_path = BUNDLED_FONTS_DIR / "NotoSansKR-Regular.ttf"
    sans_path = BUNDLED_FONTS_DIR / "NotoSans-Regular.ttf"
    thai_path = BUNDLED_FONTS_DIR / "NotoSansThai-Regular.ttf"

    if kr_path.is_file():
        kr_font = get_or_register_font("NotoSansKR", str(kr_path))
        pdfmetrics.registerFontFamily(
            "NotoSansKR",
            normal="NotoSansKR",
            bold="NotoSansKR",
            italic="NotoSansKR",
            boldItalic="NotoSansKR",
        )
        cascade.append(kr_font)

    if sans_path.is_file():
        sans_font = get_or_register_font("NotoSans", str(sans_path))
        cascade.append(sans_font)

    if thai_path.is_file():
        thai_font = get_or_register_font("NotoSansThai", str(thai_path))
        cascade.append(thai_font)

    # 2. Check explicit custom font from argument or environment
    env_font = custom_font_path or os.environ.get("PDF_FONT_PATH", "").strip()
    if env_font and Path(env_font).is_file():
        custom_font = get_or_register_font("CustomPdfFont", str(env_font))
        # Place custom font first so its glyphs take precedence, while bundled fonts serve as fallbacks
        cascade.insert(0, custom_font)

    # 3. If bundled fonts were not found, search system fonts
    if not cascade:
        linux_candidates = [
            ("NotoSansCJK", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            ("NotoSansKR", "/usr/share/fonts/truetype/noto/NotoSansKR-Regular.ttf"),
            ("NanumGothic", "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
            ("UnDotum", "/usr/share/fonts/truetype/unfonts-core/UnDotum.ttf"),
        ]
        for fname, fpath in linux_candidates:
            if Path(fpath).is_file():
                sys_font = get_or_register_font(fname, fpath)
                cascade.append(sys_font)
                break

        win_dir = os.environ.get("WINDIR", "C:\\Windows")
        malgun_path = Path(win_dir) / "Fonts" / "malgun.ttf"
        if not cascade and malgun_path.is_file():
            win_font = get_or_register_font("MalgunGothic", str(malgun_path))
            cascade.append(win_font)

    if not cascade:
        raise PdfFontError(
            "Không tìm thấy font chữ Unicode hỗ trợ tiếng Việt và tiếng Hàn trong thư mục fonts/ "
            "hoặc hệ thống. Vui lòng kiểm tra các tệp font NotoSansKR-Regular.ttf hoặc cấu hình PDF_FONT_PATH."
        )

    base_font = cascade[0]
    return base_font, cascade


def find_unicode_font() -> tuple[str, str | None]:
    """Compatibility helper returning discovered base font file path."""
    base_font, _ = get_font_cascade()
    return base_font.path, None


def register_unicode_fonts(
    regular_path: str, bold_path: str | None = None
) -> tuple[str, str]:
    """Compatibility helper registering fonts and returning font names."""
    base_font, _ = get_font_cascade(regular_path)
    return base_font.name, base_font.name


def format_text_with_font_fallback(
    text: str,
    base_font: LoadedFont,
    font_cascade: list[LoadedFont],
) -> str:
    """Format plain text into ReportLab XML markup with per-character/run font fallback.

    - Characters covered by base_font are emitted directly (escaped for XML).
    - Characters covered by a fallback font are grouped into runs and wrapped with <font name="...">...</font>.
    - Genuinely unsupported characters raise PdfFontError with actionable details instead of squares.
    """
    if not text:
        return ""

    runs: list[tuple[str, list[str]]] = []
    current_font_name: str | None = None
    current_chars: list[str] = []

    for char in text:
        cp = ord(char)
        if cp in (10, 13, 9):  # whitespace control characters
            target_font_name = base_font.name
        elif base_font.supports(cp):
            target_font_name = base_font.name
        else:
            # Check fallback fonts in cascade
            matched = next((f for f in font_cascade if f.supports(cp)), None)
            if matched is not None:
                target_font_name = matched.name
            else:
                raise PdfFontError(
                    f"Phát hiện ký tự không được hỗ trợ bởi các font hiện có: U+{cp:04X} ('{char}'). "
                    "Vui lòng bổ sung font hỗ trợ qua biến môi trường PDF_FONT_PATH."
                )

        if target_font_name == current_font_name:
            current_chars.append(char)
        else:
            if current_chars and current_font_name is not None:
                runs.append((current_font_name, "".join(current_chars)))
            current_font_name = target_font_name
            current_chars = [char]

    if current_chars and current_font_name is not None:
        runs.append((current_font_name, "".join(current_chars)))

    markup_parts: list[str] = []
    for font_name, run_text in runs:
        escaped = xml_escape(run_text)
        if font_name == base_font.name:
            markup_parts.append(escaped)
        else:
            markup_parts.append(f'<font name="{font_name}">{escaped}</font>')

    return "".join(markup_parts)


def is_head_segment(dom_path: str) -> bool:
    """Check if segment belongs to HTML <head> metadata."""
    if not dom_path:
        return False
    base = dom_path.split("#")[0]
    parts = [p for p in base.split("/") if p]
    for part in parts:
        clean = re.sub(r"\[\d+\]$", "", part).lower()
        if clean == "head":
            return True
    return False


def is_br_tail_segment(dom_path: str, parent_tag: str | None, node_type: str | None) -> bool:
    """Check if segment is a tail node directly following a <br> line break."""
    if parent_tag == "br" and node_type == "tail":
        return True
    if not dom_path:
        return False
    parts = dom_path.split("#")
    if len(parts) > 1 and parts[1] == "tail":
        base_parts = [p for p in parts[0].split("/") if p]
        if base_parts:
            last = re.sub(r"\[\d+\]$", "", base_parts[-1]).lower()
            return last == "br"
    return False


def get_segment_block_info(
    dom_path: str, parent_tag: str | None, node_type: str | None
) -> dict[str, Any]:
    """Extract block-level container path and tag information from DOM path."""
    if is_head_segment(dom_path):
        base_path = dom_path.split("#")[0] if dom_path else "/html/head"
        return {
            "key": base_path,
            "tag": parent_tag or "head",
            "is_head": True,
            "is_heading": False,
            "heading_level": 0,
        }

    base_path = dom_path.split("#")[0] if dom_path else ""
    parts = [p for p in base_path.split("/") if p]
    effective_parts = parts[:-1] if (node_type == "tail" and len(parts) > 1) else parts

    for i in range(len(effective_parts) - 1, -1, -1):
        part = effective_parts[i]
        tag = re.sub(r"\[\d+\]$", "", part).lower()
        if tag in BLOCK_TAGS:
            is_heading = bool(re.match(r"^h[1-6]$", tag))
            heading_level = int(tag[1]) if is_heading else 0
            return {
                "key": "/" + "/".join(effective_parts[: i + 1]),
                "tag": tag,
                "is_heading": is_heading,
                "heading_level": heading_level,
                "is_head": False,
            }

    if effective_parts:
        last = effective_parts[-1]
        tag = re.sub(r"\[\d+\]$", "", last).lower()
        is_heading = bool(re.match(r"^h[1-6]$", tag))
        return {
            "key": "/" + "/".join(effective_parts),
            "tag": parent_tag or tag or "p",
            "is_heading": is_heading,
            "heading_level": int(tag[1]) if is_heading else 0,
            "is_head": False,
        }

    return {
        "key": dom_path or "p-fallback",
        "tag": parent_tag or "p",
        "is_heading": False,
        "heading_level": 0,
        "is_head": False,
    }


def group_segments_into_paragraphs(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group flat DOM segments into coherent paragraphs, preserving order and joins."""
    body_segments = [s for s in segments if not is_head_segment(s.get("dom_path", ""))]
    if not body_segments:
        return []

    paragraphs: list[dict[str, Any]] = []
    current_group: dict[str, Any] | None = None

    for seg in body_segments:
        info = get_segment_block_info(
            seg.get("dom_path", ""),
            seg.get("parent_tag"),
            seg.get("node_type"),
        )
        is_br_break = (
            is_br_tail_segment(
                seg.get("dom_path", ""), seg.get("parent_tag"), seg.get("node_type")
            )
            and info["tag"] in GENERIC_BLOCK_CONTAINERS
        )

        should_start_new = (
            current_group is None
            or current_group["key"] != info["key"]
            or is_br_break
        )

        if should_start_new:
            current_group = {
                "key": info["key"],
                "tag": info["tag"],
                "is_heading": info["is_heading"],
                "heading_level": info["heading_level"],
                "segments": [seg],
            }
            paragraphs.append(current_group)
        else:
            current_group["segments"].append(seg)

    return paragraphs


def check_translation_completeness(
    chapters_with_segments: list[dict[str, Any]]
) -> list[str]:
    """Check each chapter for missing non-whitespace body segment translations.

    Returns a list of human-readable descriptions of incomplete chapters.
    """
    incomplete_details: list[str] = []
    for c in chapters_with_segments:
        c_id = c.get("id", "")
        c_title = c.get("title") or f"Chương #{c_id[:8]}"
        segments = c.get("segments", [])

        body_non_ws = [
            s
            for s in segments
            if not is_head_segment(s.get("dom_path", "")) and not s.get("is_whitespace")
        ]

        if body_non_ws:
            missing = [
                s
                for s in body_non_ws
                if not s.get("translation") or not str(s["translation"]).strip()
            ]
            if missing:
                incomplete_details.append(
                    f"{c_title} (còn {len(missing)}/{len(body_non_ws)} đoạn chưa dịch)"
                )
        else:
            all_non_ws = [s for s in segments if not s.get("is_whitespace")]
            if all_non_ws:
                missing = [
                    s
                    for s in all_non_ws
                    if not s.get("translation") or not str(s["translation"]).strip()
                ]
                if missing:
                    incomplete_details.append(
                        f"{c_title} (còn {len(missing)} đoạn chưa dịch)"
                    )
    return incomplete_details


class NumberedCanvas(canvas.Canvas):
    """Two-pass ReportLab canvas for total page count, running headers, and footers."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._saved_page_states: list[dict[str, Any]] = []
        self.default_font_name: str = "Helvetica"
        self.doc_book_title: str = ""
        self.base_font: LoadedFont | None = None
        self.font_cascade: list[LoadedFont] = []

    def showPage(self) -> None:
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        num_pages = len(self._saved_page_states)
        if num_pages == 0:
            super().save()
            return

        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def _draw_string_with_fallback(
        self,
        x: float,
        y: float,
        text: str,
        font_size: float = 9.0,
        color: Any = colors.HexColor("#71717A"),
    ) -> None:
        """Draw string on canvas with multi-font fallback support."""
        self.setFillColor(color)
        base = self.base_font
        cascade = self.font_cascade

        if base and cascade:
            runs: list[tuple[str, str]] = []
            cur_font = None
            cur_chars: list[str] = []

            for ch in text:
                cp = ord(ch)
                if cp in (10, 13, 9) or base.supports(cp):
                    target_name = base.name
                else:
                    matched = next((f for f in cascade if f.supports(cp)), None)
                    target_name = matched.name if matched else base.name

                if target_name == cur_font:
                    cur_chars.append(ch)
                else:
                    if cur_chars and cur_font:
                        runs.append((cur_font, "".join(cur_chars)))
                    cur_font = target_name
                    cur_chars = [ch]

            if cur_chars and cur_font:
                runs.append((cur_font, "".join(cur_chars)))

            cur_x = x
            for fname, rtext in runs:
                self.setFont(fname, font_size)
                self.drawString(cur_x, y, rtext)
                cur_x += self.stringWidth(rtext, fname, font_size)
        else:
            fname = getattr(self, "default_font_name", "Helvetica")
            self.setFont(fname, font_size)
            self.drawString(x, y, text)

    def draw_page_decorations(self, total_pages: int) -> None:
        self.saveState()
        page_w, page_h = self._pagesize
        margin = 54.0

        # Running header on page 2 and later
        if self._pageNumber > 1:
            book_title = getattr(self, "doc_book_title", "")
            if book_title:
                display_title = (
                    book_title if len(book_title) <= 60 else book_title[:57] + "..."
                )
                self._draw_string_with_fallback(
                    margin, page_h - 36, display_title, font_size=9, color=colors.HexColor("#71717A")
                )

            self.setStrokeColor(colors.HexColor("#E4E4E7"))
            self.setLineWidth(0.5)
            self.line(margin, page_h - 42, page_w - margin, page_h - 42)

        # Running footer with page numbering: e.g. "1 / 15"
        page_str = f"{self._pageNumber} / {total_pages}"
        fname = getattr(self, "default_font_name", "Helvetica")
        self.setFont(fname, 9)
        self.setFillColor(colors.HexColor("#71717A"))
        self.drawRightString(page_w - margin, 32, page_str)

        self.setStrokeColor(colors.HexColor("#E4E4E7"))
        self.setLineWidth(0.5)
        self.line(margin, 44, page_w - margin, 44)

        self.restoreState()


SCENE_BREAK_PATTERNS = {
    "* * *", "***", "---", "- - -", "◆ ◆ ◆", "■ ■ ■", "...", "*** ***",
}


def is_scene_break(text: str) -> bool:
    """Check if paragraph represents a scene separator / break."""
    clean = text.strip()
    if clean in SCENE_BREAK_PATTERNS:
        return True
    if re.fullmatch(r"(\*\s*){3,7}", clean) or re.fullmatch(r"(-\s*){3,7}", clean):
        return True
    return False


def generate_pdf_export(
    book: dict[str, Any],
    chapters_with_segments: list[dict[str, Any]],
    content_mode: str = "translation",
    font_path: str | None = None,
) -> bytes:
    """Generate a clean, continuous-reading PDF for the requested book chapters.

    Args:
        book: Book metadata dictionary.
        chapters_with_segments: Ordered list of chapter dicts with segments.
        content_mode: 'translation' (Vietnamese) or 'source' (Korean original).
        font_path: Optional explicit font path override.

    Returns:
        Generated PDF bytes.
    """
    if not HAS_REPORTLAB:
        raise PdfExportError(
            "Thư viện ReportLab chưa được cài đặt. Vui lòng cài đặt reportlab."
        )

    # 1. Discover and register Unicode font cascade
    base_font, font_cascade = get_font_cascade(font_path)

    # 2. Check translation completeness if translation mode
    if content_mode == "translation":
        incomplete = check_translation_completeness(chapters_with_segments)
        if incomplete:
            raise IncompleteChapterError(
                f"Không thể xuất PDF bản dịch: Phát hiện {len(incomplete)} chương chưa hoàn thành dịch "
                f"({'; '.join(incomplete)}). Bản dịch tiếng Việt không được phép bỏ sót câu văn "
                "hoặc tự động thay thế bằng tiếng Hàn."
            )

    # 3. Setup ReportLab styles using base Unicode font
    styles = getSampleStyleSheet()

    style_book_title = ParagraphStyle(
        "PdfBookTitle",
        parent=styles["Normal"],
        fontName=base_font.name,
        fontSize=22,
        leading=28,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#14233E"),
        spaceAfter=8,
    )

    style_book_meta = ParagraphStyle(
        "PdfBookMeta",
        parent=styles["Normal"],
        fontName=base_font.name,
        fontSize=10,
        leading=14,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#6B7D95"),
        spaceAfter=14,
    )

    style_chapter_title = ParagraphStyle(
        "PdfChapterTitle",
        parent=styles["Normal"],
        fontName=base_font.name,
        fontSize=15,
        leading=21,
        alignment=TA_LEFT,
        textColor=colors.HexColor("#14233E"),
        spaceBefore=14,
        spaceAfter=12,
        keepWithNext=True,
    )

    style_heading = ParagraphStyle(
        "PdfHeading",
        parent=styles["Normal"],
        fontName=base_font.name,
        fontSize=13,
        leading=18,
        alignment=TA_LEFT,
        textColor=colors.HexColor("#14233E"),
        spaceBefore=10,
        spaceAfter=6,
        keepWithNext=True,
    )

    style_body = ParagraphStyle(
        "PdfBody",
        parent=styles["Normal"],
        fontName=base_font.name,
        fontSize=10.5,
        leading=16.5,
        alignment=TA_JUSTIFY,
        textColor=colors.HexColor("#1E293B"),
        firstLineIndent=16,
        spaceAfter=5,
    )

    style_body_no_indent = ParagraphStyle(
        "PdfBodyNoIndent",
        parent=style_body,
        firstLineIndent=0,
    )

    style_scene_break = ParagraphStyle(
        "PdfSceneBreak",
        parent=styles["Normal"],
        fontName=base_font.name,
        fontSize=11,
        leading=15,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#64748B"),
        spaceBefore=8,
        spaceAfter=8,
        keepWithNext=False,
    )

    # 4. Build Document Story Flowables
    story: list[Any] = []

    # Book Cover / Title Header
    raw_title = book.get("title") or "Tác phẩm"
    raw_author = book.get("author") or ""
    raw_lang = book.get("language") or ""

    story.append(Spacer(1, 14))
    story.append(
        Paragraph(
            format_text_with_font_fallback(raw_title, base_font, font_cascade),
            style_book_title,
        )
    )

    meta_parts: list[str] = []
    if raw_author:
        meta_parts.append(f"Tác giả: {raw_author}")
    if raw_lang:
        meta_parts.append(f"Ngôn ngữ gốc: {raw_lang.upper()}")
    mode_label = "Bản dịch Tiếng Việt" if content_mode == "translation" else "Nguyên tác Tiếng Hàn"
    meta_parts.append(mode_label)

    meta_line = " · ".join(meta_parts)
    story.append(
        Paragraph(
            format_text_with_font_fallback(meta_line, base_font, font_cascade),
            style_book_meta,
        )
    )
    story.append(
        HRFlowable(
            width="100%",
            thickness=0.5,
            color=colors.HexColor("#CBD5E1"),
            spaceBefore=4,
            spaceAfter=18,
        )
    )

    # 5. Process Chapters in stable spine reading order
    for chap_idx, chapter in enumerate(chapters_with_segments):
        if chap_idx > 0:
            story.append(PageBreak())

        segments = chapter.get("segments", [])
        paragraphs = group_segments_into_paragraphs(segments)

        # Derive visible chapter title from first real body heading using selected content mode
        first_heading_idx: int | None = None
        visible_chap_title: str | None = None

        for idx, para in enumerate(paragraphs):
            if para["is_heading"]:
                heading_pieces: list[str] = []
                for seg in para["segments"]:
                    if content_mode == "source":
                        seg_text = seg.get("source") or seg.get("original_source") or ""
                    else:
                        if seg.get("is_whitespace"):
                            seg_text = seg.get("source") or " "
                        else:
                            seg_text = seg.get("translation") or ""
                    heading_pieces.append(seg_text)
                h_text = "".join(heading_pieces).strip()
                if h_text:
                    first_heading_idx = idx
                    visible_chap_title = h_text
                    break

        # Fall back to metadata title only if no real body heading exists
        if not visible_chap_title:
            visible_chap_title = chapter.get("title") or f"Chương {chap_idx + 1}"

        # Add visible chapter heading with font fallback (rendered exactly once)
        formatted_heading = format_text_with_font_fallback(
            visible_chap_title, base_font, font_cascade
        )
        story.append(Paragraph(formatted_heading, style_chapter_title))

        first_para = True

        for idx, para in enumerate(paragraphs):
            # Skip the first body heading paragraph because it has already been rendered as chapter title
            if idx == first_heading_idx:
                continue

            para_segments = para["segments"]
            text_pieces: list[str] = []

            for seg in para_segments:
                if content_mode == "source":
                    seg_text = seg.get("source") or seg.get("original_source") or ""
                else:
                    if seg.get("is_whitespace"):
                        seg_text = seg.get("source") or " "
                    else:
                        seg_text = seg.get("translation") or ""

                formatted_seg = format_text_with_font_fallback(seg_text, base_font, font_cascade)

                # If br tail inside paragraph, prepend <br/>
                if is_br_tail_segment(
                    seg.get("dom_path", ""),
                    seg.get("parent_tag"),
                    seg.get("node_type"),
                ) and para["tag"] not in GENERIC_BLOCK_CONTAINERS:
                    formatted_seg = "<br/>" + formatted_seg

                text_pieces.append(formatted_seg)

            markup = "".join(text_pieces)
            plain_text = re.sub(r"<[^>]+>", "", markup).strip()

            if not plain_text:
                continue

            # Exclude duplicate heading if body heading matches visible chapter title
            if para["is_heading"] and plain_text.lower() == visible_chap_title.strip().lower():
                continue

            # Scene separator
            if is_scene_break(plain_text):
                formatted_scene = format_text_with_font_fallback(plain_text, base_font, font_cascade)
                story.append(Paragraph(formatted_scene, style_scene_break))
                continue

            # Headings
            if para["is_heading"]:
                story.append(Paragraph(markup, style_heading))
                first_para = True
                continue

            # Regular body paragraph
            active_style = style_body_no_indent if first_para else style_body
            story.append(Paragraph(markup, active_style))
            first_para = False

    # 6. Render Document to buffer using NumberedCanvas
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=54,
        rightMargin=54,
        topMargin=54,
        bottomMargin=54,
        title=raw_title,
        author=raw_author,
    )

    def canvas_factory(*args: Any, **kwargs: Any) -> NumberedCanvas:
        c = NumberedCanvas(*args, **kwargs)
        c.default_font_name = base_font.name
        c.base_font = base_font
        c.font_cascade = font_cascade
        c.doc_book_title = raw_title
        return c

    doc.build(story, canvasmaker=canvas_factory)
    return buffer.getvalue()
