import chapterTitles from './chapterTitles.json';

// Matches raw block, tofu, geometric square, or replacement characters
const RAW_BLOCK_GLYPH_REGEX = /[\u2580-\u259F\u25A0-\u25FF\u2B1B\u2B1C\uFFFD]/;

/**
 * Check if a string contains raw block or tofu glyphs.
 * @param {unknown} str
 * @returns {boolean}
 */
export function hasRawBlockGlyphs(str) {
  return typeof str === 'string' && RAW_BLOCK_GLYPH_REGEX.test(str);
}

/**
 * Returns the localized chapter title. If originalTitle or the translated title
 * consists of raw glyph/block characters (e.g. ■), it gracefully resolves to a clean
 * translated title or falls back so that raw glyphs/boxes never appear as a title or indicator.
 *
 * @param {string} originalTitle - Original chapter title
 * @param {string} locale - 'vi' | 'en' | 'ko'
 * @param {string} [fallbackTitle] - Optional fallback title (e.g. "Chương 86")
 * @returns {string} Clean display title
 */
export function getChapterTitle(originalTitle, locale, fallbackTitle = null) {
  if (!originalTitle) {
    return fallbackTitle || originalTitle;
  }

  const translations = chapterTitles[originalTitle];
  if (translations) {
    if (translations[locale] && !hasRawBlockGlyphs(translations[locale])) {
      return translations[locale];
    }
    if (locale === 'ko' && translations.ko && !hasRawBlockGlyphs(translations.ko)) {
      return translations.ko;
    }
    if (translations.vi && !hasRawBlockGlyphs(translations.vi) && locale !== 'en') {
      return translations.vi;
    }
    if (translations.en && !hasRawBlockGlyphs(translations.en)) {
      return translations.en;
    }
  }

  if (locale === 'ko' && !hasRawBlockGlyphs(originalTitle)) {
    return originalTitle;
  }

  if (hasRawBlockGlyphs(originalTitle)) {
    if (fallbackTitle) {
      const partMatch = originalTitle.match(/\(\d+\)/);
      return partMatch ? `${fallbackTitle} ${partMatch[0]}` : fallbackTitle;
    }
  }

  return originalTitle;
}

export function getChapterTitleSearchStrings(originalTitle) {
  const strings = [originalTitle];
  if (!originalTitle) return strings;
  const translations = chapterTitles[originalTitle];
  if (translations) {
    if (translations['ko'] && !strings.includes(translations['ko'])) strings.push(translations['ko']);
    if (translations['vi'] && !strings.includes(translations['vi'])) strings.push(translations['vi']);
    if (translations['en'] && !strings.includes(translations['en'])) strings.push(translations['en']);
  }
  return strings;
}
