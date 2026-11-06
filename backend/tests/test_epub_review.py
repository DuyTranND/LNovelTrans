import io
import zipfile
import pytest
from backend.app.epub import (
    parse_epub,
    rebuild_epub,
    parse_xhtml_safely,
    EpubParseError,
    EpubSecurityError,
)
from backend.tests.test_epub import build_test_epub_bytes
from lxml import etree


def fixture(content='<html><body><p>A</p></body></html>'):
    return build_test_epub_bytes([{'id': 'c1', 'href': 'c1.xhtml', 'content': content}])


def test_matching_nav_and_ncx_titles_follow_translation_without_changing_links():
    opf = b'<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Book</dc:title></metadata><manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest><spine><itemref idref="c1"/></spine></package>'
    epub = parse_epub(build_test_epub_bytes(
        [{'id': 'c1', 'href': 'c1.xhtml', 'content': '<html><head><title>Start</title></head><body><h1>Start</h1><p>Story</p></body></html>'}],
        custom_opf=opf,
        extra_files={'nav.xhtml': b'<html><body><nav><a href="c1.xhtml#start">Start</a></nav></body></html>', 'toc.ncx': b'<ncx><navMap><navPoint id="p1"><navLabel><text>Start</text></navLabel><content src="c1.xhtml#start"/></navPoint></navMap></ncx>'},
    ))
    segment = epub.spine_chapters[0].segments[0]
    result = parse_epub(rebuild_epub(epub, {'c1': {segment.segment_id: 'Mở đầu'}}))
    nav = etree.fromstring(result.resources['OEBPS/nav.xhtml'])
    ncx = etree.fromstring(result.resources['OEBPS/toc.ncx'])
    assert nav.find('.//a').text == 'Mở đầu'
    assert nav.find('.//a').get('href') == 'c1.xhtml#start'
    assert ncx.find('.//text').text == 'Mở đầu'
    assert ncx.find('.//content').get('src') == 'c1.xhtml#start'


def test_comment_and_script_tails_survive_translation():
    epub = parse_epub(fixture('<html><body><p>A<!--private-->B<script>evil()</script>C<i>D</i>E</p></body></html>'))
    chapter = epub.spine_chapters[0]
    assert [s.text for s in chapter.segments] == ['A', 'B', 'C', 'D', 'E']
    translated = {s.segment_id: s.text + '-vi' for s in chapter.segments}
    output = parse_epub(rebuild_epub(epub, {chapter.chapter_id: translated}))
    assert [s.text for s in output.spine_chapters[0].segments] == ['A-vi', 'B-vi', 'C-vi', 'D-vi', 'E-vi']
    assert b'<!--private-->' in output.spine_chapters[0].raw_bytes


def test_comment_and_script_with_tag_like_text_preserved():
    content = (
        '<html><body><p>A<!-- <img src="private.jpg"> comment -->B'
        '<script><![CDATA[var img = "<img src=\'fake.jpg\'>"; evil()]]></script>'
        'C<i>D</i>E</p></body></html>'
    )
    epub = parse_epub(fixture(content))
    chapter = epub.spine_chapters[0]
    assert [s.text for s in chapter.segments] == ['A', 'B', 'C', 'D', 'E']
    translated = {s.segment_id: s.text + '-vi' for s in chapter.segments}
    output = parse_epub(rebuild_epub(epub, {chapter.chapter_id: translated}))
    assert [s.text for s in output.spine_chapters[0].segments] == ['A-vi', 'B-vi', 'C-vi', 'D-vi', 'E-vi']
    assert b'<!-- <img src="private.jpg"> comment -->' in output.spine_chapters[0].raw_bytes
    assert b'var img = "<img src=\'fake.jpg\'>"; evil()' in output.spine_chapters[0].raw_bytes


def test_malformed_chapter_rejected():
    with pytest.raises(EpubParseError):
        parse_epub(fixture('<html><body><p>broken</body></html>'))
    with pytest.raises(EpubParseError):
        parse_epub(fixture('<html><body><p>line1<p>line2</body></html>'))


def test_html_void_img_roundtrip_produces_valid_xml():
    # Sample with unclosed HTML-style <img ...> void tag followed by story text
    content = '<html><body><p><img src="images/illustration.png" alt="Cover">Following caption text</p></body></html>'
    epub = parse_epub(fixture(content))
    chapter = epub.spine_chapters[0]

    # Verify segment extraction produces stable locator for following text
    assert [s.text for s in chapter.segments] == ['Following caption text']
    seg = chapter.segments[0]
    assert "img[1]#tail" in seg.dom_path
    assert seg.node_type == "tail"

    # Translate the following text
    translated = {seg.segment_id: 'Văn bản chú thích tiếp theo'}
    rebuilt_bytes = rebuild_epub(epub, {chapter.chapter_id: translated})

    # Verify rebuilt EPUB parses cleanly and produces valid XML with same image resource
    output = parse_epub(rebuilt_bytes)
    out_chapter = output.spine_chapters[0]
    assert [s.text for s in out_chapter.segments] == ['Văn bản chú thích tiếp theo']
    assert b'src="images/illustration.png"' in out_chapter.raw_bytes
    assert b'alt="Cover"' in out_chapter.raw_bytes
    assert 'Văn bản chú thích tiếp theo'.encode("utf-8") in out_chapter.raw_bytes


def test_untouched_chapter_preserves_original_bytes():
    raw_ch1 = '<html><body><p><img src="ch1.png">Chapter 1 text</p></body></html>'
    raw_ch2 = '<html><body><p><img src="ch2.png">Chapter 2 untouched text</p></body></html>'
    raw_epub = build_test_epub_bytes([
        {'id': 'c1', 'href': 'c1.xhtml', 'content': raw_ch1},
        {'id': 'c2', 'href': 'c2.xhtml', 'content': raw_ch2},
    ])
    epub = parse_epub(raw_epub)

    # Translate only chapter 1
    c1 = epub.spine_chapters[0]
    c2 = epub.spine_chapters[1]
    rebuilt_bytes = rebuild_epub(epub, {c1.chapter_id: {c1.segments[0].segment_id: 'Ch1-translated'}})

    # Verify untouched chapter 2 preserves exact original bytes
    zf = zipfile.ZipFile(io.BytesIO(rebuilt_bytes))
    assert zf.read(c2.archive_path) == c2.raw_bytes


def test_html_named_entities_normalized():
    content = '<html><body><p>Tom &amp; Jerry &copy; 2024&nbsp;&mdash; end</p></body></html>'
    epub = parse_epub(fixture(content))
    chapter = epub.spine_chapters[0]
    assert [s.text for s in chapter.segments] == ['Tom & Jerry © 2024\xa0— end']


def test_duplicate_archive_names_rejected():
    raw = fixture()
    buffer = io.BytesIO(raw)
    with zipfile.ZipFile(buffer, 'a') as z:
        with pytest.warns(UserWarning):
            z.writestr('mimetype', 'application/epub+zip')
    with pytest.raises(EpubSecurityError):
        parse_epub(buffer.getvalue())
