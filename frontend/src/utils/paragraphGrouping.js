/**
 * Utility functions for grouping DOM text/tail segments into coherent paragraphs.
 *
 * Preserves document order, whitespace, and DOM locator metadata while joining
 * adjacent text and tail nodes that belong to the same block element (paragraph, heading, etc.).
 * This prevents inline markup (like <em> or <span>) from splitting sentences across separate lines.
 */

export const GAP_INDICATOR = '[...]';

const BLOCK_TAGS = new Set([
  'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'blockquote', 'li', 'dt', 'dd', 'pre',
  'figcaption', 'caption', 'td', 'th', 'section', 'article', 'div'
]);

const GENERIC_BLOCK_CONTAINERS = new Set(['div', 'body', 'section', 'article']);

/**
 * Check if a segment belongs to the <head> element hierarchy (e.g. metadata title).
 */
export function isHeadSegment(seg) {
  if (!seg) return false;
  const domPath = seg.dom_path || '';
  const [basePath] = domPath.split('#');
  const parts = basePath.split('/').filter(Boolean);
  return parts.some((p) => p.replace(/\[\d+\]$/, '').toLowerCase() === 'head');
}

/**
 * Check if a segment is a tail node directly following a <br> line break.
 */
export function isBrTailSegment(seg) {
  if (!seg) return false;
  if (seg.parent_tag === 'br' && seg.node_type === 'tail') return true;
  const domPath = seg.dom_path || '';
  const [basePath, nodeType] = domPath.split('#');
  if (nodeType !== 'tail') return false;
  const parts = basePath.split('/').filter(Boolean);
  if (parts.length === 0) return false;
  const lastPart = parts[parts.length - 1].replace(/\[\d+\]$/, '').toLowerCase();
  return lastPart === 'br';
}

/**
 * Extract block-level container path and tag information from a segment's DOM path.
 *
 * Example DOM paths:
 * - "/html[1]/head[1]/title[1]#text" -> key: "/html[1]/head[1]/title[1]", tag: "title", isHead: true
 * - "/html[1]/body[1]/p[2]#text" -> key: "/html[1]/body[1]/p[2]", tag: "p"
 * - "/html[1]/body[1]/p[2]/em[1]#text" -> key: "/html[1]/body[1]/p[2]", tag: "p"
 * - "/html[1]/body[1]/p[2]/em[1]#tail" -> key: "/html[1]/body[1]/p[2]", tag: "p"
 * - "/html[1]/body[1]/h1[1]#text" -> key: "/html[1]/body[1]/h1[1]", tag: "h1", isHeading: true
 */
export function getSegmentBlockInfo(seg) {
  if (isHeadSegment(seg)) {
    const domPath = seg.dom_path || '';
    const [basePath] = domPath.split('#');
    return {
      key: basePath || '/html/head',
      tag: seg.parent_tag || 'head',
      isHead: true,
      isHeading: false,
      headingLevel: 0,
    };
  }

  const domPath = seg.dom_path || '';
  const [basePath, nodeType] = domPath.split('#');
  const parts = basePath.split('/').filter(Boolean);

  // If nodeType is 'tail', the tail node follows an element inside that element's parent.
  // E.g., /html[1]/body[1]/p[1]/em[1]#tail is text inside p[1] following em[1].
  const effectiveParts = (nodeType === 'tail' && parts.length > 1)
    ? parts.slice(0, -1)
    : parts;

  // Search from innermost to outermost for the deepest recognized block tag
  for (let i = effectiveParts.length - 1; i >= 0; i--) {
    const part = effectiveParts[i];
    const tag = part.replace(/\[\d+\]$/, '').toLowerCase();
    if (BLOCK_TAGS.has(tag)) {
      const isHeading = /^h[1-6]$/.test(tag);
      const headingLevel = isHeading ? parseInt(tag.charAt(1), 10) : 0;
      return {
        key: '/' + effectiveParts.slice(0, i + 1).join('/'),
        tag,
        isHeading,
        headingLevel,
        isHead: false,
      };
    }
  }

  // Fallback: use effective container
  if (effectiveParts.length > 0) {
    const lastPart = effectiveParts[effectiveParts.length - 1];
    const tag = lastPart.replace(/\[\d+\]$/, '').toLowerCase();
    const isHeading = /^h[1-6]$/.test(tag);
    return {
      key: '/' + effectiveParts.join('/'),
      tag: seg.parent_tag || tag || 'p',
      isHeading,
      headingLevel: isHeading ? parseInt(tag.charAt(1), 10) : 0,
      isHead: false,
    };
  }

  return {
    key: seg.id || 'p-fallback',
    tag: seg.parent_tag || 'p',
    isHeading: false,
    headingLevel: 0,
    isHead: false,
  };
}

/**
 * Group flat segments into an array of paragraph objects.
 *
 * Each paragraph object contains:
 * - key: string (guaranteed unique key across discontiguous runs with same container)
 * - containerKey: string (underlying block container DOM path)
 * - tag: string ('p', 'h1', 'h2', 'div', etc.)
 * - isHeading: boolean
 * - headingLevel: number (1-6, or 0)
 * - isHead: boolean (true if head descendant)
 * - segments: Array of original segment objects in document order
 */
export function groupSegmentsIntoParagraphs(segments = []) {
  if (!Array.isArray(segments) || segments.length === 0) {
    return [];
  }

  const paragraphs = [];
  const containerRunCount = new Map();
  let currentGroup = null;

  for (const seg of segments) {
    const info = getSegmentBlockInfo(seg);
    const isBrBreak = isBrTailSegment(seg) && GENERIC_BLOCK_CONTAINERS.has(info.tag);

    const shouldStartNewGroup =
      !currentGroup ||
      currentGroup.containerKey !== info.key ||
      currentGroup.isHead !== info.isHead ||
      isBrBreak;

    if (shouldStartNewGroup) {
      const runIndex = (containerRunCount.get(info.key) || 0) + 1;
      containerRunCount.set(info.key, runIndex);
      const uniqueKey = runIndex === 1 ? info.key : `${info.key}::run-${runIndex}`;

      currentGroup = {
        key: uniqueKey,
        containerKey: info.key,
        tag: info.tag,
        isHeading: info.isHeading,
        headingLevel: info.headingLevel,
        isHead: Boolean(info.isHead),
        segments: [seg],
      };
      paragraphs.push(currentGroup);
    } else {
      currentGroup.segments.push(seg);
    }
  }

  return paragraphs;
}

/**
 * Retrieve the active text of a segment, honoring draft edits if present.
 */
export function getSegmentText(seg, field = 'translation', draftEdits = {}) {
  if (!seg) return '';
  const draft = draftEdits[seg.id];
  if (draft && draft[field] !== undefined) {
    return draft[field];
  }
  return seg[field] || '';
}

/**
 * Check if a segment has non-empty translated content (saved or draft).
 */
export function isSegmentTranslated(seg, draftEdits = {}) {
  const text = getSegmentText(seg, 'translation', draftEdits);
  return Boolean(text && text.trim().length > 0);
}

/**
 * Check if a paragraph has any non-empty translated content.
 */
export function isParagraphTranslated(paragraph, draftEdits = {}) {
  if (!paragraph || !Array.isArray(paragraph.segments)) return false;
  return paragraph.segments.some((s) => isSegmentTranslated(s, draftEdits));
}

/**
 * Pure reading-selection helper returning renderable paragraphs for document reading mode.
 *
 * Applies reading domain rules:
 * 1. Excludes all <head> descendants from the reading document.
 * 2. Treats <br> runs in generic containers (div) as logical reading paragraphs.
 * 3. In translation view, filters out untranslated paragraphs (no blank lines from missing nodes).
 * 4. For partially translated inline paragraphs (e.g. p#text / em#text / em#tail with missing middle),
 *    renders an explicit concise gap indicator ([...]) instead of deceptively fusing adjacent pieces,
 *    and never uses source text as fallback.
 * 5. Preserves source <br> line breaks in source view, and only emits <br> for translated nodes in translation view.
 */
export function getReadingParagraphs(segments = [], { field = 'translation', draftEdits = {} } = {}) {
  if (!Array.isArray(segments) || segments.length === 0) {
    return [];
  }

  // Exclude head descendants from reading document
  const bodySegments = segments.filter((s) => !isHeadSegment(s));
  const rawParagraphs = groupSegmentsIntoParagraphs(bodySegments);

  const result = [];
  for (const para of rawParagraphs) {
    // Check if paragraph is whitespace only
    const nonWsSegs = para.segments.filter((s) => !s.is_whitespace);
    if (nonWsSegs.length === 0) continue;

    // In translation view, skip paragraphs that have zero translated content
    if (field === 'translation' && !isParagraphTranslated(para, draftEdits)) {
      continue;
    }

    const nodes = [];
    const isComplete = nonWsSegs.every((s) => isSegmentTranslated(s, draftEdits));

    for (const seg of para.segments) {
      if (field === 'source') {
        const text = getSegmentText(seg, 'source', draftEdits);
        const isBr = isBrTailSegment(seg);
        nodes.push({
          segId: seg.id,
          text,
          isBr,
          isGap: false,
        });
      } else {
        // Translation view
        const isTrans = isSegmentTranslated(seg, draftEdits);
        const isBr = isBrTailSegment(seg);

        if (seg.is_whitespace) {
          nodes.push({
            segId: seg.id,
            text: seg.source || ' ',
            isBr: false,
            isGap: false,
          });
        } else if (isTrans) {
          const text = getSegmentText(seg, 'translation', draftEdits);
          nodes.push({
            segId: seg.id,
            text,
            isBr, // Emit br only if the node is translated
            isGap: false,
          });
        } else {
          // Missing translation in a partially translated paragraph:
          // Filter missing node BEFORE emitting associated br (never emit br for absent span)
          // Render concise gap indicator to prevent deceptive concatenation
          nodes.push({
            segId: seg.id,
            text: GAP_INDICATOR,
            isBr: false,
            isGap: true,
          });
        }
      }
    }

    // Combine prose text
    const text = nodes.map((n) => n.text).join('');

    result.push({
      key: para.key,
      tag: para.tag,
      isHeading: para.isHeading,
      headingLevel: para.headingLevel,
      isComplete,
      segments: para.segments,
      nodes,
      text,
    });
  }

  return result;
}
