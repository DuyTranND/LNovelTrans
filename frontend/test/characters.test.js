import test from 'node:test';
import assert from 'node:assert/strict';
import { translations } from '../src/i18n/translations.js';
import * as api from '../src/api.js';

test('1. i18n dictionary completeness for character & discovery keys', () => {
  const characterKeys = [
    'tabCharacters',
    'charactersTitle',
    'charactersSubtitle',
    'searchCharacters',
    'addCharacter',
    'addCharacterTitle',
    'editCharacterTitle',
    'charCanonicalSource',
    'charCanonicalTarget',
    'charAliases',
    'charGender',
    'charGenderUnknown',
    'charGenderMale',
    'charGenderFemale',
    'charGenderOther',
    'charSpeechStyle',
    'charRelationships',
    'charLock',
    'charFirstOccurrence',
    'charLockedConfirm',
    'charDeleteConfirm',
    'saveCharacter',
    'loadingCharacters',
    'noCharactersFound',
    'charactersEmpty',
    'colCharSource',
    'colCharTarget',
    'colCharAliases',
    'colCharGender',
    'colCharSpeech',
    'colCharNotes',
    'colCharStatus',
    'discoveryTitle',
    'discoverySubtitle',
    'startDiscoveryBtn',
    'startingDiscovery',
    'discoveryInProgress',
    'discoveryProgressText',
    'discoveryCompleted',
    'discoveryCancelled',
    'discoveryFailed',
    'cancelDiscovery',
    'proposalsFilterAll',
    'proposalsFilterPending',
    'proposalsFilterApproved',
    'proposalsFilterRejected',
    'proposalsFilterCategoryAll',
    'catCharacter',
    'catGlossary',
    'catPlace',
    'catOrganization',
    'approveProposal',
    'rejectProposal',
    'editAndApproveProposal',
    'evidenceSnippet',
    'lockedCollisionWarning',
    'noProposalsFound',
    'discoveryDisclaimer',
  ];

  for (const lang of ['vi', 'en']) {
    const dict = translations[lang];
    assert.ok(dict, `Dictionary for '${lang}' must exist`);
    for (const key of characterKeys) {
      assert.ok(
        dict[key] !== undefined && dict[key] !== '',
        `Missing translation for key '${key}' in language '${lang}'`
      );
    }
  }
});

test('2. Character & Discovery API client functions are exported and callable', () => {
  assert.equal(typeof api.getCharacters, 'function');
  assert.equal(typeof api.createCharacter, 'function');
  assert.equal(typeof api.getCharacter, 'function');
  assert.equal(typeof api.updateCharacter, 'function');
  assert.equal(typeof api.deleteCharacter, 'function');
  assert.equal(typeof api.startDiscovery, 'function');
  assert.equal(typeof api.getDiscoveryRuns, 'function');
  assert.equal(typeof api.getDiscoveryRun, 'function');
  assert.equal(typeof api.cancelDiscoveryRun, 'function');
  assert.equal(typeof api.getDiscoveryProposals, 'function');
  assert.equal(typeof api.reviewDiscoveryProposal, 'function');
});

test('3. Character filter and collision detection logic', () => {
  const characters = [
    {
      id: 'char_1',
      canonical_source: '성진우',
      canonical_target: 'Sung Jin-woo',
      source_aliases: ['진우', '그림자 군주'],
      locked: true,
    },
    {
      id: 'char_2',
      canonical_source: '차해인',
      canonical_target: 'Cha Hae-in',
      source_aliases: ['해인'],
      locked: false,
    },
  ];

  // Helper function to check collision with locked characters
  function isProposalColliding(proposalSource, charList) {
    const norm = (proposalSource || '').trim().toLowerCase();
    return charList.some((c) => c.locked && c.canonical_source.trim().toLowerCase() === norm);
  }

  // Colliding with locked character
  assert.equal(isProposalColliding('성진우', characters), true);
  assert.equal(isProposalColliding(' 성진우 ', characters), true);

  // Unlocked character -> no locked collision
  assert.equal(isProposalColliding('차해인', characters), false);

  // Brand new character -> no collision
  assert.equal(isProposalColliding('유진호', characters), false);

  // Filter characters by query
  function filterCharacters(list, query) {
    if (!query) return list;
    const q = query.toLowerCase();
    return list.filter((c) =>
      c.canonical_source.toLowerCase().includes(q) ||
      c.canonical_target.toLowerCase().includes(q) ||
      c.source_aliases.some((a) => a.toLowerCase().includes(q))
    );
  }

  assert.equal(filterCharacters(characters, 'jin-woo').length, 1);
  assert.equal(filterCharacters(characters, '진우').length, 1);
  assert.equal(filterCharacters(characters, '해인').length, 1);
  assert.equal(filterCharacters(characters, 'none').length, 0);
});
