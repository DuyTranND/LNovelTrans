import test from 'node:test';
import assert from 'node:assert/strict';
import {
  isHeadSegment,
  isBrTailSegment,
  getSegmentBlockInfo,
  groupSegmentsIntoParagraphs,
  getReadingParagraphs,
  isSegmentTranslated,
  isParagraphTranslated,
  GAP_INDICATOR,
} from '../src/utils/paragraphGrouping.js';

test('1. Head exclusion: identifies head segments and excludes them from reading document', () => {
  const headTitleSeg = {
    id: 'seg-head-title',
    dom_path: '/html[1]/head[1]/title[1]#text',
    parent_tag: 'title',
    node_type: 'text',
    source: '각성 (1)',
    translation: 'Thức tỉnh (1)',
  };

  const bodyH1Seg = {
    id: 'seg-body-h1',
    dom_path: '/html[1]/body[1]/h1[1]#text',
    parent_tag: 'h1',
    node_type: 'text',
    source: '각성 (1)',
    translation: 'Thức tỉnh (1)',
  };

  const bodyParaSeg = {
    id: 'seg-body-p',
    dom_path: '/html[1]/body[1]/p[1]#text',
    parent_tag: 'p',
    node_type: 'text',
    source: '첫 번째 문장입니다.',
    translation: 'Đây là câu đầu tiên.',
  };

  // isHeadSegment unit verification
  assert.equal(isHeadSegment(headTitleSeg), true, 'Head title segment must be identified as head');
  assert.equal(isHeadSegment(bodyH1Seg), false, 'Body H1 segment must NOT be identified as head');
  assert.equal(isHeadSegment(bodyParaSeg), false, 'Body paragraph segment must NOT be identified as head');

  // getSegmentBlockInfo tags head properly
  const headInfo = getSegmentBlockInfo(headTitleSeg);
  assert.equal(headInfo.isHead, true);

  const bodyInfo = getSegmentBlockInfo(bodyH1Seg);
  assert.equal(bodyInfo.isHead, false);
  assert.equal(bodyInfo.isHeading, true);

  // getReadingParagraphs must exclude head title so article does not show duplicate title
  const segments = [headTitleSeg, bodyH1Seg, bodyParaSeg];
  const readingParas = getReadingParagraphs(segments, { field: 'translation' });

  assert.equal(readingParas.length, 2, 'Should only contain body h1 and body paragraph');
  assert.equal(readingParas[0].isHeading, true);
  assert.equal(readingParas[0].text, 'Thức tỉnh (1)');
  assert.equal(readingParas[1].text, 'Đây là câu đầu tiên.');

  // Verify that head-only translation does NOT mistake head translation as readable body
  const headOnlySegments = [
    headTitleSeg,
    {
      id: 'seg-body-p-untrans',
      dom_path: '/html[1]/body[1]/p[1]#text',
      parent_tag: 'p',
      node_type: 'text',
      source: '번역 안 됨.',
      translation: '',
    },
  ];

  const headOnlyReadingParas = getReadingParagraphs(headOnlySegments, { field: 'translation' });
  assert.equal(
    headOnlyReadingParas.length,
    0,
    'Reading document should be empty when only head metadata is translated'
  );
});

test('2. Inline joins: joins p#text, em#text, and em#tail into coherent prose without gaps when complete', () => {
  const segments = [
    {
      id: 'seg-p1-text',
      dom_path: '/html[1]/body[1]/p[1]#text',
      parent_tag: 'p',
      node_type: 'text',
      source: 'Alice nhìn thấy ',
      translation: 'Alice nhìn thấy ',
    },
    {
      id: 'seg-p1-em-text',
      dom_path: '/html[1]/body[1]/p[1]/em[1]#text',
      parent_tag: 'em',
      node_type: 'text',
      source: 'con thỏ trắng',
      translation: 'con thỏ trắng',
    },
    {
      id: 'seg-p1-em-tail',
      dom_path: '/html[1]/body[1]/p[1]/em[1]#tail',
      parent_tag: 'em',
      node_type: 'tail',
      source: ' đang vội vã chạy.',
      translation: ' đang vội vã chạy.',
    },
  ];

  const grouped = groupSegmentsIntoParagraphs(segments);
  assert.equal(grouped.length, 1, 'Inline elements within p must be grouped into a single paragraph');
  assert.equal(grouped[0].segments.length, 3);

  const readingParas = getReadingParagraphs(segments, { field: 'translation' });
  assert.equal(readingParas.length, 1);
  assert.equal(readingParas[0].isComplete, true);
  assert.equal(readingParas[0].text, 'Alice nhìn thấy con thỏ trắng đang vội vã chạy.');
  assert.equal(readingParas[0].nodes.some((n) => n.isGap), false, 'No gap indicator in complete block');
});

test('3. br logical lines: div with hundreds of br-tail nodes treats br runs as logical paragraphs and avoids blank lines', () => {
  // Simulate sample chapter structure: div#text followed by multiple br-tail segments
  const segments = [
    {
      id: 'seg-div-line1',
      dom_path: '/html[1]/body[1]/div[1]#text',
      parent_tag: 'div',
      node_type: 'text',
      source: '첫 번째 줄 문장입니다.',
      translation: 'Dòng thứ nhất đã được dịch.',
    },
    {
      id: 'seg-div-line2',
      dom_path: '/html[1]/body[1]/div[1]/br[1]#tail',
      parent_tag: 'br',
      node_type: 'tail',
      source: '두 번째 줄 문장입니다.',
      translation: '', // Untranslated
    },
    {
      id: 'seg-div-line3',
      dom_path: '/html[1]/body[1]/div[1]/br[2]#tail',
      parent_tag: 'br',
      node_type: 'tail',
      source: '세 번째 줄 문장입니다.',
      translation: '', // Untranslated
    },
    {
      id: 'seg-div-line4',
      dom_path: '/html[1]/body[1]/div[1]/br[3]#tail',
      parent_tag: 'br',
      node_type: 'tail',
      source: '네 번째 줄 문장입니다.',
      translation: '', // Untranslated
    },
  ];

  // Helper verification for br-tail detection
  assert.equal(isBrTailSegment(segments[0]), false);
  assert.equal(isBrTailSegment(segments[1]), true);
  assert.equal(isBrTailSegment(segments[2]), true);

  // groupSegmentsIntoParagraphs should separate each br run into a logical paragraph
  const grouped = groupSegmentsIntoParagraphs(segments);
  assert.equal(grouped.length, 4, 'Each br run in div must be treated as an individual logical paragraph');

  // Translation view: only translated line 1 should be rendered; lines 2-4 untranslated must NOT emit blank lines
  const transReadingParas = getReadingParagraphs(segments, { field: 'translation' });
  assert.equal(transReadingParas.length, 1, 'Only translated first line must be rendered in reading document');
  assert.equal(transReadingParas[0].text, 'Dòng thứ nhất đã được dịch.');
  assert.equal(transReadingParas[0].nodes[0].isBr, false, 'No br tag emitted for lone translated line');

  // Source view: all lines and their br breaks must be preserved
  const sourceReadingParas = getReadingParagraphs(segments, { field: 'source' });
  assert.equal(sourceReadingParas.length, 4, 'All lines preserved in source view');
  assert.equal(sourceReadingParas[0].nodes[0].isBr, false);
  assert.equal(sourceReadingParas[1].nodes[0].isBr, true, 'Source br preserved on line 2');
  assert.equal(sourceReadingParas[2].nodes[0].isBr, true, 'Source br preserved on line 3');
});

test('4. Partially translated inline paragraph: concise gap indicator prevents deceptive concatenation without using source fallback', () => {
  const segments = [
    {
      id: 'seg-part-text',
      dom_path: '/html[1]/body[1]/p[1]#text',
      parent_tag: 'p',
      node_type: 'text',
      source: '그는 조용히 ',
      translation: 'Anh ấy lặng lẽ ',
    },
    {
      id: 'seg-part-em-text',
      dom_path: '/html[1]/body[1]/p[1]/em[1]#text',
      parent_tag: 'em',
      node_type: 'text',
      source: '검을 뽑아들고', // Korean source
      translation: '', // MISSING middle translation!
    },
    {
      id: 'seg-part-em-tail',
      dom_path: '/html[1]/body[1]/p[1]/em[1]#tail',
      parent_tag: 'em',
      node_type: 'tail',
      source: ' 앞으로 나아갔다.',
      translation: ' tiến về phía trước.',
    },
  ];

  const readingParas = getReadingParagraphs(segments, { field: 'translation' });
  assert.equal(readingParas.length, 1);
  const para = readingParas[0];

  assert.equal(para.isComplete, false, 'Paragraph with missing middle translation must be marked incomplete');

  // Must NOT deceptively concatenate "Anh ấy lặng lẽ " directly with " tiến về phía trước."
  assert.notEqual(
    para.text,
    'Anh ấy lặng lẽ  tiến về phía trước.',
    'Must NOT deceptively fuse text across missing node'
  );

  // Must NOT use Korean source text as fallback
  assert.equal(
    para.text.includes('검을 뽑아들고'),
    false,
    'Korean source text must NEVER be used as fallback in translation view'
  );

  // Must render concise gap indicator [...] for the missing segment
  assert.equal(
    para.text,
    `Anh ấy lặng lẽ ${GAP_INDICATOR} tiến về phía trước.`,
    'Must contain concise gap indicator [...] where middle translation is missing'
  );

  // Gap node structure check
  const gapNode = para.nodes.find((n) => n.segId === 'seg-part-em-text');
  assert.ok(gapNode);
  assert.equal(gapNode.isGap, true);
  assert.equal(gapNode.text, GAP_INDICATOR);
});

test('5. Noncontiguous container unique keys: div text / p / div tail avoids duplicate React keys', () => {
  const segments = [
    {
      id: 'seg-div-start',
      dom_path: '/html[1]/body[1]/div[1]#text',
      parent_tag: 'div',
      node_type: 'text',
      source: 'Div intro text',
      translation: 'Văn bản mở đầu của Div',
    },
    {
      id: 'seg-p-inner',
      dom_path: '/html[1]/body[1]/div[1]/p[1]#text',
      parent_tag: 'p',
      node_type: 'text',
      source: 'Paragraph inside div',
      translation: 'Đoạn văn bên trong div',
    },
    {
      id: 'seg-div-tail',
      dom_path: '/html[1]/body[1]/div[1]/p[1]#tail', // Tail following p, directly inside div
      parent_tag: 'p',
      node_type: 'tail',
      source: 'Div tail text after paragraph',
      translation: 'Văn bản nối tiếp trong div sau đoạn văn',
    },
  ];

  const grouped = groupSegmentsIntoParagraphs(segments);

  assert.equal(grouped.length, 3, 'Must split into 3 distinct groups preserving document order');
  assert.equal(grouped[0].tag, 'div');
  assert.equal(grouped[1].tag, 'p');
  assert.equal(grouped[2].tag, 'div');

  // Verify all keys are strictly unique across discontiguous runs with same container
  const keys = grouped.map((g) => g.key);
  const uniqueKeysSet = new Set(keys);

  assert.equal(
    uniqueKeysSet.size,
    keys.length,
    `All group keys must be strictly unique to avoid React duplicate key warnings. Found keys: ${keys.join(', ')}`
  );

  assert.equal(keys[0], '/html[1]/body[1]/div[1]');
  assert.equal(keys[1], '/html[1]/body[1]/div[1]/p[1]');
  assert.equal(keys[2], '/html[1]/body[1]/div[1]::run-2', 'Second run for div container must have unique suffix');
});

test('6. Filter missing translation nodes before rendering br; preserve source br line breaks', () => {
  const segments = [
    {
      id: 'seg-p-line1',
      dom_path: '/html[1]/body[1]/p[1]#text',
      parent_tag: 'p',
      node_type: 'text',
      source: 'Line 1 in P',
      translation: 'Dòng 1 trong P',
    },
    {
      id: 'seg-p-line2',
      dom_path: '/html[1]/body[1]/p[1]/br[1]#tail',
      parent_tag: 'br',
      node_type: 'tail',
      source: 'Line 2 in P',
      translation: '', // Untranslated
    },
  ];

  // In translation view: missing node must NOT emit associated br break
  const transParas = getReadingParagraphs(segments, { field: 'translation' });
  assert.equal(transParas.length, 1);
  const untransNode = transParas[0].nodes.find((n) => n.segId === 'seg-p-line2');
  assert.ok(untransNode);
  assert.equal(untransNode.isBr, false, 'Do not emit br for untranslated span');
  assert.equal(untransNode.isGap, true);

  // In source view: br break must be preserved
  const sourceParas = getReadingParagraphs(segments, { field: 'source' });
  assert.equal(sourceParas.length, 1);
  const sourceNode2 = sourceParas[0].nodes.find((n) => n.segId === 'seg-p-line2');
  assert.ok(sourceNode2);
  assert.equal(sourceNode2.isBr, true, 'Source br break must be preserved');
});
