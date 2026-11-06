"""Safe EPUB parser and rebuilder.

Provides robust, security-hardened EPUB parsing and rebuilding conforming to
the EPUB 2 and EPUB 3 specifications. Handles ZIP container structures, OPF
manifests, and spine reading order without filename sorting. Extracts text
and tail segments with stable DOM locators, preserves inline formatting, and
rebuilds valid EPUB archives with mimetype stored uncompressed first.
"""

from __future__ import annotations

import html.entities
import io
import os
import posixpath
import re
import urllib.parse
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from lxml import etree

EPUB_MIMETYPE = b"application/epub+zip"
CONTAINER_PATH = "META-INF/container.xml"

# Resource security limits
DEFAULT_MAX_TOTAL_SIZE = 500 * 1024 * 1024  # 500 MB
DEFAULT_MAX_SINGLE_FILE_SIZE = 100 * 1024 * 1024  # 100 MB
DEFAULT_MAX_FILE_COUNT = 10_000
DEFAULT_MAX_COMPRESSION_RATIO = 200

# XML Namespaces
NAMESPACES = {
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "opf_legacy": "http://www.openebook.org/namespaces/oebps-package/core/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
}

# Tags to skip when extracting translatable text
SKIP_TAGS = {"script", "style"}

# Regex to detect dangerous external entity definitions (XXE)
XXE_PATTERN = re.compile(
    rb"<!ENTITY\s+[%]?\s*[\w\.\-:]+\s+(SYSTEM|PUBLIC)", re.IGNORECASE
)


class EpubError(Exception):
    """Base exception for all EPUB processing errors."""


class EpubSecurityError(EpubError):
    """Raised when security boundaries (Zip Slip, Zip Bomb, XXE) are violated."""


class EpubParseError(EpubError):
    """Raised when an EPUB file is malformed or missing required structures."""


class EpubDuplicateEntryError(EpubSecurityError, EpubParseError):
    """Raised when duplicate entry paths are detected in archive."""


@dataclass
class TextSegment:
    """Represents an identified text or tail segment within an XHTML document."""

    segment_id: str
    dom_path: str
    node_type: str  # 'text' or 'tail'
    text: str
    parent_tag: str
    is_whitespace: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dictionary representation."""
        return asdict(self)


@dataclass
class ChapterDocument:
    """Represents a chapter document in the EPUB spine."""

    chapter_id: str
    href: str
    archive_path: str
    title: str | None
    spine_index: int
    media_type: str
    raw_bytes: bytes
    segments: list[TextSegment] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dictionary representation."""
        return {
            "chapter_id": self.chapter_id,
            "href": self.href,
            "archive_path": self.archive_path,
            "title": self.title,
            "spine_index": self.spine_index,
            "media_type": self.media_type,
            "raw_bytes_len": len(self.raw_bytes),
            "segments": [s.to_dict() for s in self.segments],
        }


@dataclass
class EpubMetadata:
    """Metadata extracted from the OPF package document."""

    title: str | None = None
    language: str | None = None
    creator: str | None = None
    identifier: str | None = None
    publisher: str | None = None
    description: str | None = None
    rights: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dictionary representation."""
        return asdict(self)


@dataclass
class EpubManifestItem:
    """Represents an item in the OPF manifest."""

    id: str
    href: str
    media_type: str
    archive_path: str
    properties: str | None = None
    fallback: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dictionary representation."""
        return asdict(self)


@dataclass
class ParsedEpub:
    """Complete parsed EPUB structure with preserved raw bytes and resources."""

    metadata: EpubMetadata
    opf_path: str
    manifest: dict[str, EpubManifestItem]
    spine: list[str]  # Manifest item IDs in exact spine reading order
    spine_chapters: list[ChapterDocument]  # Ordered according to spine
    resources: dict[str, bytes]  # All files from archive keyed by archive path
    raw_archive_bytes: bytes

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dictionary representation."""
        return {
            "metadata": self.metadata.to_dict(),
            "opf_path": self.opf_path,
            "manifest": {k: v.to_dict() for k, v in self.manifest.items()},
            "spine": list(self.spine),
            "spine_chapters": [c.to_dict() for c in self.spine_chapters],
            "resource_paths": list(self.resources.keys()),
            "archive_size": len(self.raw_archive_bytes),
        }


def validate_zip_entry_path(name: str) -> None:
    """Validate archive entry path against Zip Slip and invalid characters.

    Raises EpubSecurityError if path is suspicious or attempts directory traversal.
    """
    if not name or not name.strip():
        raise EpubSecurityError("Invalid archive entry: empty name")

    # Reject drive letters like C:
    if re.match(r"^[a-zA-Z]:", name) or ":" in name:
        raise EpubSecurityError(f"Invalid archive entry with drive letter or colon: {name}")

    # Reject absolute paths
    if name.startswith("/") or name.startswith("\\"):
        raise EpubSecurityError(f"Absolute path in archive entry: {name}")

    # Normalize backslashes to forward slashes
    normalized = posixpath.normpath(name.replace("\\", "/"))

    # Reject traversal attempts
    if (
        normalized == ".."
        or normalized.startswith("../")
        or "/../" in f"/{normalized}/"
    ):
        raise EpubSecurityError(f"Directory traversal detected in archive entry: {name}")


def assert_safe_xml(xml_bytes: bytes) -> None:
    """Check XML byte content for dangerous external entity definitions (XXE).

    Raises EpubSecurityError if external entity declarations are found.
    """
    if XXE_PATTERN.search(xml_bytes):
        raise EpubSecurityError(
            "External entity declaration (XXE) detected in XML content"
        )


def create_safe_xml_parser(recover: bool = False) -> etree.XMLParser:
    """Create an lxml XMLParser configured with strict security settings."""
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        dtd_validation=False,
        load_dtd=False,
        huge_tree=False,
        recover=recover,
        remove_comments=False,
        remove_pis=False,
        strip_cdata=False,
    )


def parse_xml_safely(xml_bytes: bytes, recover: bool = False) -> etree._Element:
    """Parse XML safely, denying external entities and network access."""
    assert_safe_xml(xml_bytes)
    parser = create_safe_xml_parser(recover=recover)
    try:
        return etree.fromstring(xml_bytes, parser=parser)
    except etree.XMLSyntaxError as e:
        raise EpubParseError(f"XML syntax error: {e}") from e


# Known HTML void elements that do not require an end tag in HTML
HTML_VOID_TAGS = {
    "area",
    "base",
    "basefont",
    "br",
    "col",
    "embed",
    "frame",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}

# XML predefined entity names that must never be converted to plain characters
XML_PREDEFINED_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}


def _detect_encoding(raw_bytes: bytes) -> str:
    """Detect encoding of XML bytes from BOM or declaration, defaulting to utf-8."""
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    m = re.search(
        rb'<\?xml[^>]+encoding=[\'"]([a-zA-Z0-9_\-]+)[\'"]',
        raw_bytes[:1024],
        re.IGNORECASE,
    )
    if m:
        enc = m.group(1).decode("ascii", errors="ignore").strip()
        try:
            "".encode(enc)
            return enc
        except LookupError:
            pass
    return "utf-8"


def _replace_html_entity(match: re.Match[str]) -> str:
    """Replace a named HTML entity with its Unicode character, preserving XML builtins."""
    name = match.group(1)
    if name.lower() in XML_PREDEFINED_ENTITIES:
        return match.group(0)
    key = f"{name};"
    if hasattr(html.entities, "html5") and key in html.entities.html5:
        return html.entities.html5[key]
    if name in html.entities.name2codepoint:
        return chr(html.entities.name2codepoint[name])
    return match.group(0)


def _normalize_entities_in_text(text_segment: str) -> str:
    """Replace recognized HTML named entities with Unicode characters in a string."""
    return re.sub(r"&([a-zA-Z][a-zA-Z0-9]*);", _replace_html_entity, text_segment)


def _normalize_xhtml(text: str) -> str:
    """Narrowly normalize HTML void start tags to self-closing and HTML entities to Unicode.

    Respects quoted attributes, preserves comments, CDATA, PIs, DOCTYPE, and
    script/style contents without alteration. Does not modify arbitrary tag-like text.
    """
    out: list[str] = []
    i = 0
    n = len(text)

    while i < n:
        if text[i] != "<":
            next_lt = text.find("<", i)
            if next_lt == -1:
                out.append(_normalize_entities_in_text(text[i:]))
                break
            out.append(_normalize_entities_in_text(text[i:next_lt]))
            i = next_lt

        # 1. Comments: <!-- ... -->
        if text.startswith("<!--", i):
            end = text.find("-->", i + 4)
            if end != -1:
                out.append(text[i : end + 3])
                i = end + 3
            else:
                out.append(text[i:])
                break
            continue

        # 2. CDATA: <![CDATA[ ... ]]>
        if text.startswith("<![CDATA[", i):
            end = text.find("]]>", i + 9)
            if end != -1:
                out.append(text[i : end + 3])
                i = end + 3
            else:
                out.append(text[i:])
                break
            continue

        # 3. Processing Instructions: <? ... ?>
        if text.startswith("<?", i):
            end = text.find("?>", i + 2)
            if end != -1:
                out.append(text[i : end + 2])
                i = end + 2
            else:
                out.append(text[i:])
                break
            continue

        # 4. DOCTYPE and declarations: <!DOCTYPE ...>
        if text.startswith("<!", i):
            j = i + 2
            in_subset = False
            while j < n:
                c = text[j]
                if c == "[":
                    in_subset = True
                elif c == "]":
                    in_subset = False
                elif c == ">" and not in_subset:
                    j += 1
                    break
                j += 1
            out.append(text[i:j])
            i = j
            continue

        # 5. End tags: </tag>
        if text.startswith("</", i):
            m = re.match(r"</([a-zA-Z][a-zA-Z0-9:_.-]*)", text[i:])
            if m:
                close_tag = m.group(1).split(":")[-1].lower()
                end = text.find(">", i + len(m.group(0)))
                if end != -1:
                    end += 1
                else:
                    end = n
                if close_tag in HTML_VOID_TAGS:
                    # Void tags have no closing tag in HTML; drop redundant void closing tag
                    i = end
                else:
                    out.append(text[i:end])
                    i = end
                continue
            else:
                out.append(text[i])
                i += 1
                continue

        # 6. Start tags: <tagname ...>
        m = re.match(r"<([a-zA-Z][a-zA-Z0-9:_.-]*)", text[i:])
        if not m:
            out.append(text[i])
            i += 1
            continue

        tag_name = m.group(1)
        local_tag = tag_name.split(":")[-1].lower()

        # Scan attributes to closing '>', respecting quotes
        j = i + len(m.group(0))
        quote_char: str | None = None
        while j < n:
            c = text[j]
            if quote_char:
                if c == quote_char:
                    quote_char = None
            else:
                if c in ('"', "'"):
                    quote_char = c
                elif c == ">":
                    j += 1
                    break
            j += 1

        tag_str = text[i:j]
        if quote_char is not None or not tag_str.endswith(">"):
            out.append(tag_str)
            i = j
            continue

        # Preserve script and style content without alteration
        if local_tag in ("script", "style"):
            if tag_str[:-1].rstrip().endswith("/"):
                out.append(tag_str)
                i = j
            else:
                close_pat = re.compile(rf"</{re.escape(local_tag)}\s*>", re.IGNORECASE)
                close_m = close_pat.search(text, j)
                if close_m:
                    out.append(text[i : close_m.end()])
                    i = close_m.end()
                else:
                    out.append(tag_str)
                    i = j
            continue

        # Narrowly normalize HTML void tags to self-closing
        if local_tag in HTML_VOID_TAGS:
            tag_body = tag_str[:-1].rstrip()
            if tag_body.endswith("/"):
                out.append(_normalize_entities_in_text(tag_str))
            else:
                out.append(_normalize_entities_in_text(f"{tag_body} />"))
            i = j
            continue

        # Normal tag: normalize entities in attributes
        out.append(_normalize_entities_in_text(tag_str))
        i = j

    return "".join(out)


def parse_xhtml_safely(xhtml_bytes: bytes | str) -> etree._Element:
    """Parse XHTML safely: strict XML first, then targeted normalization if needed.

    Ensures security (XXE rejection) and strict XML validation. If strict XML fails,
    narrowly normalizes unclosed known HTML void tags (like <img ...>) to self-closing
    and common HTML entities to Unicode, then re-parses with strict XML. Malformed XML
    structures (such as mismatched <p>) are strictly rejected.
    """
    if isinstance(xhtml_bytes, str):
        raw_bytes = xhtml_bytes.encode("utf-8")
    else:
        raw_bytes = xhtml_bytes

    assert_safe_xml(raw_bytes)

    # 1. Strict XML parse first
    try:
        return parse_xml_safely(raw_bytes, recover=False)
    except EpubParseError:
        pass

    # 2. Targeted normalization
    encoding = _detect_encoding(raw_bytes)
    try:
        text = raw_bytes.decode(encoding)
    except UnicodeDecodeError:
        text = raw_bytes.decode("utf-8", errors="replace")
        encoding = "utf-8"

    normalized_text = _normalize_xhtml(text)
    normalized_bytes = normalized_text.encode(encoding)

    # 3. Strict XML parse again (rejects malformed XML like mismatched <p>)
    return parse_xml_safely(normalized_bytes, recover=False)


def inspect_and_extract_zip(
    file_bytes: bytes,
    max_total_size: int = DEFAULT_MAX_TOTAL_SIZE,
    max_file_size: int = DEFAULT_MAX_SINGLE_FILE_SIZE,
    max_file_count: int = DEFAULT_MAX_FILE_COUNT,
) -> dict[str, bytes]:
    """Inspect and extract all entries from a ZIP archive with security checks.

    Validates file count, total uncompressed size, individual file size,
    compression ratio, and file paths.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(file_bytes), mode="r")
    except zipfile.BadZipFile as e:
        raise EpubParseError(f"Corrupted or invalid ZIP archive: {e}") from e

    entries = zf.infolist()
    if len(entries) > max_file_count:
        raise EpubSecurityError(
            f"Archive entry count exceeds limit ({len(entries)} > {max_file_count})"
        )

    total_uncompressed = 0
    extracted_files: dict[str, bytes] = {}

    seen_paths: set[str] = set()
    for info in entries:
        validate_zip_entry_path(info.filename)

        # Reject duplicate ZIP paths
        norm_name = posixpath.normpath(info.filename.replace("\\", "/"))
        if info.filename in seen_paths or norm_name in seen_paths:
            raise EpubDuplicateEntryError(
                f"Duplicate entry path detected in archive: {info.filename}"
            )
        seen_paths.add(info.filename)
        seen_paths.add(norm_name)

        # Skip pure directory entries
        if info.filename.endswith("/"):
            continue

        if info.file_size > max_file_size:
            raise EpubSecurityError(
                f"File '{info.filename}' uncompressed size exceeds limit "
                f"({info.file_size} > {max_file_size})"
            )

        total_uncompressed += info.file_size
        if total_uncompressed > max_total_size:
            raise EpubSecurityError(
                f"Total uncompressed size exceeds limit "
                f"({total_uncompressed} > {max_total_size})"
            )

        # Detect suspicious compression ratio for files > 10KB
        if info.compress_size > 0 and info.file_size > 1024 * 10:
            ratio = info.file_size / info.compress_size
            if ratio > DEFAULT_MAX_COMPRESSION_RATIO:
                raise EpubSecurityError(
                    f"Suspicious compression ratio on '{info.filename}': {ratio:.1f}:1"
                )

        # Read actual bytes bounded by max_file_size + 1 to prevent decompression bombs
        with zf.open(info) as stream:
            data = stream.read(max_file_size + 1)
            if len(data) > max_file_size:
                raise EpubSecurityError(
                    f"Actual size of '{info.filename}' exceeds limit ({max_file_size})"
                )
            extracted_files[info.filename] = data

    return extracted_files


def _get_node_tag(elem: etree._Element) -> str:
    """Return local tag name or specialized identifier for comments and PIs."""
    if isinstance(elem.tag, str):
        return etree.QName(elem).localname
    if isinstance(elem, etree._Comment) or (
        callable(elem.tag) and "comment" in getattr(elem.tag, "__name__", "").lower()
    ):
        return "comment"
    if isinstance(elem, etree._ProcessingInstruction) or (
        callable(elem.tag) and "processing" in getattr(elem.tag, "__name__", "").lower()
    ):
        return "pi"
    return "node"


def get_dom_path(elem: etree._Element) -> str:
    """Generate a stable, deterministic DOM locator path for an element.

    Uses 1-based sibling indexing with tag local names, namespace-agnostic.
    Example: /html[1]/body[1]/p[2]/span[1]
    """
    parts: list[str] = []
    curr: etree._Element | None = elem
    while curr is not None:
        parent = curr.getparent()
        tag = _get_node_tag(curr)
        if parent is not None:
            same_tag_siblings = [
                c
                for c in parent
                if _get_node_tag(c) == tag
            ]
            try:
                idx = same_tag_siblings.index(curr) + 1
            except ValueError:
                idx = 1
            parts.append(f"{tag}[{idx}]")
            curr = parent
        else:
            # Root element
            parts.append(f"{tag}[1]")
            break

    parts.reverse()
    return "/" + "/".join(parts)


def find_element_by_dom_path(
    root: etree._Element, dom_path: str
) -> etree._Element | None:
    """Locate an element in the XML tree matching the stable DOM locator path."""
    base_path = dom_path.split("#")[0]
    parts = [p for p in base_path.strip("/").split("/") if p]
    if not parts:
        return None

    root_tag = _get_node_tag(root)
    root_part_tag = parts[0].split("[")[0]
    if root_tag != root_part_tag:
        return None

    curr = root
    for part in parts[1:]:
        if "[" in part and part.endswith("]"):
            tag_name, idx_str = part[:-1].split("[")
            try:
                idx = int(idx_str)
            except ValueError:
                return None
        else:
            tag_name = part
            idx = 1

        matches = [
            c
            for c in curr
            if _get_node_tag(c) == tag_name
        ]
        if idx < 1 or idx > len(matches):
            return None
        curr = matches[idx - 1]

    return curr


def extract_title_from_xhtml(tree: etree._Element) -> str | None:
    """Extract chapter title from XHTML <title> tag or first <h1> tag."""
    title_nodes = tree.xpath("//*[local-name()='title']")
    if title_nodes and title_nodes[0].text and title_nodes[0].text.strip():
        return title_nodes[0].text.strip()

    h1_nodes = tree.xpath("//*[local-name()='h1']")
    if h1_nodes:
        text = "".join(h1_nodes[0].itertext()).strip()
        if text:
            return text

    return None


def extract_text_segments_from_tree(
    root: etree._Element,
    chapter_id: str = "c0",
    include_whitespace: bool = False,
    skip_tags: set[str] | None = None,
) -> list[TextSegment]:
    """Extract text and tail segments in document reading order.

    Single pass carrying precomputed local-name indexed paths.
    Skips comment/PI text but preserves their tails.
    Preserves tail text following script/style.
    """
    if skip_tags is None:
        skip_tags = SKIP_TAGS

    segments: list[TextSegment] = []
    seq_counter = 0

    root_tag = _get_node_tag(root)
    root_path = f"/{root_tag}[1]"

    def traverse(elem: etree._Element, elem_path: str) -> None:
        nonlocal seq_counter

        is_comment_or_pi = isinstance(
            elem, (etree._Comment, etree._ProcessingInstruction)
        ) or (callable(elem.tag) and not isinstance(elem.tag, str))
        tag = _get_node_tag(elem)

        # Do not extract comment/PI text or text inside skip_tags (script/style)
        if not is_comment_or_pi and tag.lower() not in skip_tags:
            if elem.text:
                is_ws = elem.text.strip() == ""
                if include_whitespace or not is_ws:
                    seq_counter += 1
                    seg_id = f"{chapter_id}_s{seq_counter:04d}"
                    segments.append(
                        TextSegment(
                            segment_id=seg_id,
                            dom_path=f"{elem_path}#text",
                            node_type="text",
                            text=elem.text,
                            parent_tag=tag,
                            is_whitespace=is_ws,
                        )
                    )

            # Traverse children carrying precomputed sibling paths
            tag_counts: dict[str, int] = {}
            for child in elem:
                child_tag = _get_node_tag(child)
                idx = tag_counts.get(child_tag, 0) + 1
                tag_counts[child_tag] = idx
                child_path = f"{elem_path}/{child_tag}[{idx}]"

                traverse(child, child_path)

                # Check child tail - preserve tail even after script/style or comment/PI
                if child.tail:
                    is_ws = child.tail.strip() == ""
                    if include_whitespace or not is_ws:
                        seq_counter += 1
                        seg_id = f"{chapter_id}_s{seq_counter:04d}"
                        segments.append(
                            TextSegment(
                                segment_id=seg_id,
                                dom_path=f"{child_path}#tail",
                                node_type="tail",
                                text=child.tail,
                                parent_tag=tag,
                                is_whitespace=is_ws,
                            )
                        )

    traverse(root, root_path)
    return segments


def extract_text_segments(
    html_bytes_or_tree: bytes | etree._Element,
    chapter_id: str = "c0",
    include_whitespace: bool = False,
) -> list[TextSegment]:
    """Convenience function to extract text segments from XHTML bytes or Element."""
    if isinstance(html_bytes_or_tree, (bytes, str)):
        tree = parse_xhtml_safely(html_bytes_or_tree)
    else:
        tree = html_bytes_or_tree
    return extract_text_segments_from_tree(
        tree, chapter_id=chapter_id, include_whitespace=include_whitespace
    )


def apply_translations_to_tree(
    tree: etree._Element,
    translations: dict[str, str],
    segments: list[TextSegment] | None = None,
) -> int:
    """Apply translations to an XML tree using stable DOM locators or segment IDs.

    Returns the count of successfully applied translations.
    """
    applied_count = 0

    seg_map: dict[str, tuple[str, str]] = {}
    if segments:
        for seg in segments:
            seg_map[seg.segment_id] = (seg.dom_path, seg.node_type)

    for key, new_text in translations.items():
        dom_path = key
        node_type: str | None = None

        if key in seg_map:
            dom_path, node_type = seg_map[key]

        if node_type is None:
            if "#" in dom_path:
                dom_path, node_type = dom_path.split("#", 1)
            else:
                node_type = "text"

        elem = find_element_by_dom_path(tree, dom_path)
        if elem is not None:
            if node_type == "text":
                elem.text = new_text
                applied_count += 1
            elif node_type == "tail":
                elem.tail = new_text
                applied_count += 1

    return applied_count


def serialize_xhtml(
    tree: etree._Element, original_bytes: bytes | None = None
) -> bytes:
    """Serialize an XML tree back to XHTML bytes preserving DOCTYPE if present."""
    doctype: str | None = None
    if original_bytes:
        match = re.search(rb"(<!DOCTYPE[^>]*>)", original_bytes, re.IGNORECASE)
        if match:
            doctype = match.group(1).decode("utf-8", errors="replace")

    return etree.tostring(
        tree,
        encoding="utf-8",
        xml_declaration=True,
        doctype=doctype,
        method="xml",
    )


def apply_translations_to_xhtml(
    xhtml_bytes: bytes,
    translations: dict[str, str],
    segments: list[TextSegment] | None = None,
) -> bytes:
    """Apply translations to XHTML bytes and return the modified XHTML bytes."""
    tree = parse_xhtml_safely(xhtml_bytes)
    apply_translations_to_tree(tree, translations, segments=segments)
    return serialize_xhtml(tree, original_bytes=xhtml_bytes)


def update_opf_language_and_title(
    opf_bytes: bytes,
    target_language: str = "vi",
    title_translation: str | None = None,
) -> bytes:
    """Update language code and optional title in OPF package metadata."""
    tree = parse_xml_safely(opf_bytes, recover=False)

    metadata_nodes = tree.xpath("//*[local-name()='metadata']")
    if metadata_nodes:
        metadata_elem = metadata_nodes[0]

        # Update or create dc:language
        lang_nodes = metadata_elem.xpath("./*[local-name()='language']")
        if lang_nodes:
            lang_nodes[0].text = target_language
        else:
            ns = NAMESPACES["dc"]
            new_lang = etree.SubElement(metadata_elem, f"{{{ns}}}language")
            new_lang.text = target_language

        # Update title if translation provided
        if title_translation:
            title_nodes = metadata_elem.xpath("./*[local-name()='title']")
            if title_nodes:
                title_nodes[0].text = title_translation

    return etree.tostring(tree, encoding="utf-8", xml_declaration=True, method="xml")


def parse_epub(
    file_input: str | Path | bytes | BinaryIO,
    max_total_size: int = DEFAULT_MAX_TOTAL_SIZE,
    max_file_size: int = DEFAULT_MAX_SINGLE_FILE_SIZE,
    max_file_count: int = DEFAULT_MAX_FILE_COUNT,
    include_whitespace_segments: bool = False,
) -> ParsedEpub:
    """Parse an EPUB archive into typed data structures with security safeguards.

    Strictly preserves OPF spine reading order (no filename sorting), extracts
    text/tail segments with stable DOM paths, and retains original document bytes.
    """
    if isinstance(file_input, (str, Path)):
        with open(file_input, "rb") as f:
            raw_archive_bytes = f.read()
    elif isinstance(file_input, bytes):
        raw_archive_bytes = file_input
    elif hasattr(file_input, "read"):
        raw_archive_bytes = file_input.read()
    else:
        raise TypeError(f"Unsupported file_input type: {type(file_input)}")

    # Step 1: Safe ZIP extraction and validation
    extracted_files = inspect_and_extract_zip(
        raw_archive_bytes,
        max_total_size=max_total_size,
        max_file_size=max_file_size,
        max_file_count=max_file_count,
    )

    # Step 1b: Validate mimetype compliance if present
    if "mimetype" in extracted_files:
        if extracted_files["mimetype"].strip() != EPUB_MIMETYPE:
            raise EpubParseError(
                f"Invalid mimetype in archive: {extracted_files['mimetype']!r}"
            )

    # Step 2: Validate META-INF/container.xml
    if CONTAINER_PATH not in extracted_files:
        raise EpubParseError(f"Missing required container file: {CONTAINER_PATH}")

    container_bytes = extracted_files[CONTAINER_PATH]
    container_tree = parse_xml_safely(container_bytes, recover=False)

    rootfiles = container_tree.xpath("//*[local-name()='rootfile'][@full-path]")
    if not rootfiles:
        raise EpubParseError(
            "No rootfile element with full-path attribute found in container.xml"
        )

    opf_path = rootfiles[0].get("full-path", "").strip()
    validate_zip_entry_path(opf_path)

    if opf_path not in extracted_files:
        raise EpubParseError(f"OPF package document not found in archive: {opf_path}")

    # Step 3: Parse OPF package document
    opf_bytes = extracted_files[opf_path]
    opf_tree = parse_xml_safely(opf_bytes, recover=False)
    opf_dir = posixpath.dirname(opf_path)

    # 3a. Metadata
    metadata = EpubMetadata()
    for title_el in opf_tree.xpath("//*[local-name()='metadata']/*[local-name()='title']"):
        if title_el.text:
            metadata.title = title_el.text.strip()
            break
    for lang_el in opf_tree.xpath("//*[local-name()='metadata']/*[local-name()='language']"):
        if lang_el.text:
            metadata.language = lang_el.text.strip()
            break
    for creator_el in opf_tree.xpath("//*[local-name()='metadata']/*[local-name()='creator']"):
        if creator_el.text:
            metadata.creator = creator_el.text.strip()
            break
    for id_el in opf_tree.xpath("//*[local-name()='metadata']/*[local-name()='identifier']"):
        if id_el.text:
            metadata.identifier = id_el.text.strip()
            break
    for pub_el in opf_tree.xpath("//*[local-name()='metadata']/*[local-name()='publisher']"):
        if pub_el.text:
            metadata.publisher = pub_el.text.strip()
            break
    for desc_el in opf_tree.xpath("//*[local-name()='metadata']/*[local-name()='description']"):
        if desc_el.text:
            metadata.description = desc_el.text.strip()
            break
    for rights_el in opf_tree.xpath("//*[local-name()='metadata']/*[local-name()='rights']"):
        if rights_el.text:
            metadata.rights = rights_el.text.strip()
            break

    # 3b. Manifest
    manifest: dict[str, EpubManifestItem] = {}
    manifest_items = opf_tree.xpath("//*[local-name()='manifest']/*[local-name()='item']")
    for item in manifest_items:
        item_id = item.get("id")
        raw_href = item.get("href")
        media_type = item.get("media-type", "")
        properties = item.get("properties")
        fallback = item.get("fallback")

        if not item_id or not raw_href:
            continue

        clean_href = raw_href.split("#")[0]
        decoded_href = urllib.parse.unquote(clean_href)
        if opf_dir:
            archive_path = posixpath.normpath(posixpath.join(opf_dir, decoded_href))
        else:
            archive_path = posixpath.normpath(decoded_href)

        validate_zip_entry_path(archive_path)

        manifest[item_id] = EpubManifestItem(
            id=item_id,
            href=raw_href,
            media_type=media_type,
            archive_path=archive_path,
            properties=properties,
            fallback=fallback,
        )

    # 3c. Spine - strictly maintain spine order!
    spine_itemrefs = opf_tree.xpath("//*[local-name()='spine']/*[local-name()='itemref']")
    spine: list[str] = []
    for itemref in spine_itemrefs:
        idref = itemref.get("idref")
        if idref:
            spine.append(idref)

    # 3d. Build spine chapters
    spine_chapters: list[ChapterDocument] = []
    for spine_index, idref in enumerate(spine):
        manifest_item = manifest.get(idref)
        if not manifest_item:
            raise EpubParseError(f"Spine itemref '{idref}' not found in manifest")

        archive_path = manifest_item.archive_path
        if archive_path not in extracted_files:
            raise EpubParseError(
                f"Spine item resource '{archive_path}' (id: '{idref}') not found in archive"
            )

        # Non-XHTML spine resources must be skipped explicitly, not parsed as XML
        clean_media = manifest_item.media_type.split(";")[0].strip().lower()
        if clean_media not in ("application/xhtml+xml", "text/html"):
            continue

        raw_doc_bytes = extracted_files[archive_path]
        doc_tree = parse_xhtml_safely(raw_doc_bytes)
        title = extract_title_from_xhtml(doc_tree)
        segments = extract_text_segments_from_tree(
            doc_tree,
            chapter_id=idref,
            include_whitespace=include_whitespace_segments,
        )

        chapter = ChapterDocument(
            chapter_id=idref,
            href=manifest_item.href,
            archive_path=archive_path,
            title=title,
            spine_index=spine_index,
            media_type=manifest_item.media_type,
            raw_bytes=raw_doc_bytes,
            segments=segments,
        )
        spine_chapters.append(chapter)

    return ParsedEpub(
        metadata=metadata,
        opf_path=opf_path,
        manifest=manifest,
        spine=spine,
        spine_chapters=spine_chapters,
        resources=extracted_files,
        raw_archive_bytes=raw_archive_bytes,
    )


def rebuild_epub(
    parsed_or_bytes: ParsedEpub | bytes,
    translations: dict[str, dict[str, str]] | None = None,
    target_language: str = "vi",
    update_opf_metadata: bool = True,
    title_translation: str | None = None,
    modified_chapters: dict[str, bytes] | None = None,
) -> bytes:
    """Rebuild an EPUB archive with translated chapters and intact resources.

    Ensures the mimetype entry is written FIRST and UNCOMPRESSED (ZIP_STORED).
    Applies node locator translations to the original DOM without destroying
    inline formatting, preserving all untouched files (CSS, images, fonts).
    """
    if isinstance(parsed_or_bytes, bytes):
        parsed = parse_epub(parsed_or_bytes)
    elif isinstance(parsed_or_bytes, ParsedEpub):
        parsed = parsed_or_bytes
    else:
        raise TypeError(f"Expected ParsedEpub or bytes, got {type(parsed_or_bytes)}")

    all_files: dict[str, bytes] = dict(parsed.resources)
    translated_titles: dict[str, tuple[str, str]] = {}

    # 1. Apply any directly supplied modified chapter bytes
    if modified_chapters:
        for path, data in modified_chapters.items():
            all_files[path] = data

    # 2. Apply segment translations to chapters
    if translations:
        # Build quick lookups for chapter identification
        for chapter in parsed.spine_chapters:
            chapter_translations: dict[str, str] | None = None

            # Look up by archive path, href, chapter ID, or filename
            if chapter.archive_path in translations:
                chapter_translations = translations[chapter.archive_path]
            elif chapter.href in translations:
                chapter_translations = translations[chapter.href]
            elif chapter.chapter_id in translations:
                chapter_translations = translations[chapter.chapter_id]
            else:
                filename = posixpath.basename(chapter.archive_path)
                if filename in translations:
                    chapter_translations = translations[filename]

            if chapter_translations:
                for segment in chapter.segments:
                    if segment.text.strip() == (chapter.title or '').strip():
                        translated_title = chapter_translations.get(segment.dom_path, chapter_translations.get(segment.segment_id))
                        if translated_title and translated_title.strip():
                            translated_titles[chapter.archive_path] = (chapter.title, translated_title.strip())
                            break
                updated_bytes = apply_translations_to_xhtml(
                    chapter.raw_bytes,
                    chapter_translations,
                    segments=chapter.segments,
                )
                all_files[chapter.archive_path] = updated_bytes

    # Update matching navigation labels without changing target URLs or structure.
    for item in parsed.manifest.values():
        if not translated_titles or not ('nav' in (item.properties or '').split() or item.media_type == 'application/x-dtbncx+xml'):
            continue
        tree = parse_xhtml_safely(all_files[item.archive_path])
        changed = False
        for node in tree.iter():
            tag = _get_node_tag(node)
            if tag == 'a':
                href = node.get('href', '')
                label = node
            elif tag == 'navPoint':
                content = next((n for n in node if _get_node_tag(n) == 'content'), None)
                label = next((n for n in node.iter() if _get_node_tag(n) == 'text'), None)
                if content is None or label is None:
                    continue
                href = content.get('src', '')
            else:
                continue
            url = urllib.parse.urlsplit(href)
            if url.scheme or url.netloc:
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(item.archive_path), urllib.parse.unquote(url.path)))
            title_pair = translated_titles.get(target)
            if title_pair and len(label) == 0 and (label.text or '').strip() == title_pair[0].strip():
                label.text = title_pair[1]
                changed = True
        if changed:
            all_files[item.archive_path] = etree.tostring(tree, encoding='utf-8', xml_declaration=True)

    # 3. Update OPF metadata language and title
    if update_opf_metadata and parsed.opf_path in all_files:
        all_files[parsed.opf_path] = update_opf_language_and_title(
            all_files[parsed.opf_path],
            target_language=target_language,
            title_translation=title_translation,
        )

    # 4. Construct output ZIP with mimetype FIRST and UNCOMPRESSED
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        # Mimetype entry must be first, ZIP_STORED, no extra fields
        mimetype_bytes = all_files.get("mimetype", EPUB_MIMETYPE)
        mimetype_clean = mimetype_bytes.strip()
        if mimetype_clean != EPUB_MIMETYPE:
            mimetype_clean = EPUB_MIMETYPE
        zinfo_mime = zipfile.ZipInfo("mimetype")
        zinfo_mime.compress_type = zipfile.ZIP_STORED
        zinfo_mime.external_attr = 0o644 << 16
        zinfo_mime.extra = b""
        zf.writestr(zinfo_mime, mimetype_clean)

        # Write all remaining entries with ZIP_DEFLATED
        for path, data in all_files.items():
            if path == "mimetype":
                continue
            zinfo = zipfile.ZipInfo(path)
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            zinfo.external_attr = 0o644 << 16
            zf.writestr(zinfo, data)

    return buf.getvalue()
