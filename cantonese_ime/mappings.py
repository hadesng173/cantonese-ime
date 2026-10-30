"""Auditable, bounded spelling mappings applied to individual syllables.

Jyutping identities and optional input conveniences are deliberately separate.
The LSHK character table is an observed reading list, not an exhaustive list of
legal onset/final combinations. Absence from it never proves a loanword invalid.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from pathlib import Path
import re
from typing import Iterable, Iterator, Mapping


SCHEMA_VERSION = 3
PROFILE_VERSION = "cantonese-yale-ascii-v3"
MAX_SYLLABLE_LENGTH = 12
MAX_RULE_STEPS = 2
_BASE = re.compile(r"[a-z]{1,12}\Z", re.ASCII)
_TONED = re.compile(r"([a-z]+)([1-6])\Z", re.ASCII)
_INPUT_SYLLABLE = re.compile(r"([a-z]{1,12})([1-6])?\Z", re.ASCII)
MODES = ("jyutping", "forgiving")

ONSETS = ("b", "p", "m", "f", "d", "t", "n", "l", "g", "k", "ng", "h", "gw", "kw", "w", "z", "c", "s", "j")
FINALS = (
    "i", "ip", "it", "ik", "im", "in", "ing", "iu",
    "yu", "yut", "yun",
    "u", "up", "ut", "uk", "um", "un", "ung", "ui",
    "e", "ep", "et", "ek", "em", "en", "eng", "ei", "eu",
    "eot", "eon", "eoi", "oe", "oet", "oek", "oeng",
    "o", "ot", "ok", "on", "ong", "oi", "ou",
    "a", "ap", "at", "ak", "am", "an", "ang", "ai", "au",
    "aa", "aap", "aat", "aak", "aam", "aan", "aang", "aai", "aau",
)
SYLLABIC_NASALS = ("m", "ng")

# A final is looked up as a whole, rather than rewritten letter by letter.
# In particular only open aa becomes a; aan/aai/aau/etc retain their aa.
# These are spelling correspondences, not a Cartesian legal-syllable inventory.
YALE_FINALS = {
    "aa": "a", "aai": "aai", "aau": "aau", "aam": "aam",
    "aap": "aap", "aan": "aan", "aat": "aat", "aang": "aang", "aak": "aak",
    "ai": "ai", "au": "au", "am": "am", "ap": "ap", "an": "an",
    "at": "at", "ang": "ang", "ak": "ak",
    "e": "e", "eng": "eng", "ek": "ek", "ei": "ei",
    "oe": "eu", "oeng": "eung", "oek": "euk",
    "eoi": "eui", "eon": "eun", "eot": "eut",
    "i": "i", "iu": "iu", "im": "im", "ip": "ip", "in": "in",
    "it": "it", "ing": "ing", "ik": "ik",
    "o": "o", "oi": "oi", "on": "on", "ot": "ot",
    "ong": "ong", "ok": "ok", "ou": "ou",
    "u": "u", "ui": "ui", "un": "un", "ut": "ut", "ung": "ung", "uk": "uk",
    "yu": "yu", "yun": "yun", "yut": "yut",
}
YALE_ONSETS = {onset: {"z": "j", "c": "ch", "j": "y"}.get(onset, onset) for onset in ONSETS}
# Marginal finals absent from the ordinary Yale charts. These project typing
# extensions follow the same nucleus/coda spelling convention, but do not gain
# table-backed status merely because their components can be concatenated.
YALE_EXTENDED_FINALS = {
    "a": "a", "em": "em", "en": "en", "ep": "ep", "et": "et",
    "om": "om", "op": "op", "um": "um", "up": "up", "oet": "eut", "eu": "ew",
}
YALE_RULE_ID = "yale.ascii_syllable"
YALE_ALIAS_PENALTY = 0.45
YALE_TABLE_SOURCES = ("yale_cpime", "yale_eduhk")
# Published numeric-Yale spellings differ from the retained eu->ew convention
# for these six attested bases. They are additional whole-syllable routes,
# never input to legacy onset/final rewrites or tone substitution.
YALE_ASCII_ALTERNATES = {
    "deu": ("deeu",), "eu": ("eeu",), "meu": ("meeu",),
    "peu": ("peeu",), "seu": ("seeu",), "zeu": ("jeeu",),
}
YALE_ALTERNATE_RULE_ID = "yale.ascii_kfcd_alternate"

SOURCES = {
    "yue_wiktionary": {
        "title": "Yue Wiktionary explicit own-headword Cantonese fields",
        "url": "https://dumps.wikimedia.org/yuewiktionary/20261001/yuewiktionary-20261001-pages-articles.xml.bz2",
        "license": "CC-BY-SA-4.0",
        "scope": "Pinned official dump: accepted literal Cantonese fields in own-headword Yue sections, with source revision/history and complete exclusion ledgers. All accepted source readings contribute before dictionary winner deduplication; template execution, inferred readings and independent linguistic approval are excluded.",
    },
    "yale_kfcd_numeric": {
        "title": "KFCD published numeric-Yale/Jyutping conversion table",
        "url": "https://github.com/kfcd/pingyam/blob/d0ceb32c2da07d156db99e50b192aaf45236ca95/pingyambiu",
        "license": "CC-BY-3.0",
        "scope": "Numeric Yale field0 versus Jyutping field6 supplies six additional whole-syllable ASCII spellings. Only the terminal tone is omitted; existing ew routes remain. No diacritic/low-register-h conversion or frequency/linguistic approval is inferred.",
    },
    "lshk_scheme": {
        "title": "LSHK Jyutping Cantonese Romanization Scheme",
        "url": "https://lshk.org/jyutping-scheme/",
        "scope": "Authoritative onset/final spelling and ASCII tone digits. Finals a and oet were added in 2018. Shape compatibility does not assert an attested syllable.",
    },
    "lshk_table": {
        "title": "LSHK Cantonese Pronunciation List of the Characters for Computers",
        "url": "https://github.com/lshk-org/jyutping-table/tree/dad2dd6d6f02fc51138ecc6818f7b38eba5c2ad3",
        "license": "CC-BY-4.0",
        "scope": "Observed single-syllable JP readings; descriptive and multi-syllable rows remain separately classified.",
    },
    "rime_dictionary": {
        "title": "Original pinned Rime Cantonese explicit dictionary readings",
        "url": "https://github.com/rime/rime-cantonese/tree/259f0e48bba840c3a2e0d117539e96937f3d89bc",
        "license": "CC-BY-4.0",
        "scope": "Explicit chars/words/phrase readings before autoencoding or supplemental imports. This is distinct from the full generated dictionary inventory.",
    },
    "wordshk_wordlist": {
        "title": "words.hk public wordlist and pronunciations",
        "url": "https://words.hk/faiman/analysis/wordslist/",
        "license": "Public-domain declaration for the wordlist/pronunciation endpoint only",
        "scope": "Accepted explicit pronunciations from the pinned wordlist. Source annotations are handled by the audited importer; quarantined readings are excluded. Overlapping source attestation is retained even when dictionary deduplication selects a different source row.",
    },
    "typeduck_principal": {
        "title": "TypeDuck principal dictionary export",
        "url": "https://github.com/TypeDuck-HK/schema/tree/7f9d90d4185eca293576de5b5dddaaf1d265f3c9",
        "license": "CC-BY-4.0",
        "scope": "Accepted explicit export rows from the pinned principal table, excluding reverse lookups and marked pronunciation notation. Source attestation does not assert independent human phrase review; source percentages are not imported frequencies.",
    },
    "unihan_kcantonese": {
        "title": "Unicode 18.0.0 Unihan kCantonese",
        "url": "https://www.unicode.org/Public/18.0.0/ucd/Unihan.zip",
        "license": "Unicode-3.0",
        "property_status": "Provisional",
        "scope": "Direct character readings for CLDR collation/transliteration, independently parsed before deduplication. Provisional customary-reading attestation is not normative pronunciation, exhaustive alternatives, inherited variants or whole-word evidence.",
    },
    "dictionary_database": {
        "title": "Generated local multi-source Cantonese dictionary",
        "scope": "Records present in the generated SQLite dictionary; retained source/origin labels describe the selected output rows, not every upstream attestation.",
    },
    "cccanto_dictionary": {
        "title": "CC-Canto dictionary",
        "url": "https://www.cc-canto.org/download.html",
        "license": "CC-BY-SA-3.0",
        "scope": "Accepted explicit Cantonese readings for aligned traditional and simplified text. Mandarin readings and English definitions are excluded; no whole-word pronunciation is inferred from them.",
    },
    "cccanto_cedict_readings": {
        "title": "CC-CEDICT Cantonese readings",
        "url": "https://www.cc-canto.org/download.html",
        "license": "CC-BY-SA-3.0",
        "scope": "Accepted explicit Cantonese annotations for aligned traditional and simplified CC-CEDICT text. Mandarin readings and English definitions are excluded; source frequency is unknown rather than copied from another pronunciation.",
    },
    "wordnet_dictionary": {
        "title": "Cantonese Wordnet explicit Cantonese Lemma/Form readings",
        "url": "https://github.com/lmorgadodacosta/CantoneseWN/tree/6434cbc1d692d3061266352aa223a8df4a9c883e",
        "license": "CC-BY-4.0",
        "scope": "Accepted own-Lemma/own-Form Cantonese readings from the pinned WN-LMF XML, independently parsed before dictionary winner deduplication. Blank, malformed, unknown-base, unaligned, non-Han and English sibling forms are excluded. New zero-weight pairs append after autoencoding without changing earlier pairs or supplying encoder inputs; source attestation is not independent linguistic approval.",
    },
    "google_examples": {
        "title": "Google Input Tools Cantonese IME guide",
        "url": "https://www.google.com/intl/ka/inputtools/services/features/input-method.html#cantonese",
        "scope": "Documents lay/nay/lei for nei, failok, and ngoshuengsikchasiubao. These examples do not specify Google's complete decoder or license its dictionary.",
    },
    "cantoneseinput_examples": {
        "title": "Online Cantonese Input Method public input guide",
        "url": "https://www.cantoneseinput.com/",
        "scope": "Documents alternative initial conventions j/ch/y, bohim, and ngoh. Used as behaviour evidence, not copied dictionary data or a complete Sidney Lau implementation.",
    },
    "rime_schema": {
        "title": "Rime Cantonese published schema",
        "url": "https://github.com/rime/rime-cantonese/blob/259f0e48bba840c3a2e0d117539e96937f3d89bc/jyut6ping3.schema.yaml",
        "scope": "Published n/l and ng/zero options and selected vowel spelling conveniences. This project's penalties and combination policy are independently authored.",
    },
    "yale_cpime": {
        "title": "CPIME Cantonese romanization comparison tables",
        "url": "https://www.cpime.hk/p/pingyam-table.html",
        "scope": "Parallel Jyutping/Yale initials and ordinary final spellings. Used as spelling evidence, not copied program code or dictionary data. Its yu rows abbreviate the displayed final; the full yu/yun/yut spelling follows EdUHK.",
        "accessed": "2026-10-23",
    },
    "yale_eduhk": {
        "title": "EdUHK Survival Cantonese, Appendix I: Comparative Chart of Three Romanization Systems",
        "url": "https://www.eduhk.hk/cle/resources/cep/cantonese-survival-package/Appendix%20I.html",
        "scope": "Yale onset/final inventory, including kw and yu/yun/yut. Independently authored ASCII typing conversion uses these spelling facts; the teaching chart is not redistributed.",
        "accessed": "2026-10-23",
    },
    "yale_pycantonese": {
        "title": "PyCantonese 4.3.0 Jyutping-to-Yale implementation documentation",
        "url": "https://docs.pycantonese.org/v4.3.0/_modules/pycantonese/jyutping/yale.html",
        "version": "4.3.0",
        "scope": "Supplementary evidence for open aa, avoiding yyu, marginal eu-to-ew conversion, and the nucleus/coda spelling convention used by clearly marked project extensions. No dependency or source code is copied. Its kw-to-k onset mapping is not adopted because the primary comparison tables retain kw. Diacritics and low-register h input are outside this ASCII profile.",
        "accessed": "2026-10-23",
    },
    "project_policy": {
        "title": "Project Yale ASCII and safe spelling policy v2",
        "scope": "One independent whole-syllable Yale ASCII route plus preserved convenience rules and ranking costs. Legacy conveniences have at most two operations and one onset rewrite per syllable; Yale output is never fed back into them. Rare unverified forms retain explicit Jyutping fallback rather than being presented as formal Yale.",
    },
}


@dataclass(frozen=True, slots=True)
class SpellingRule:
    rule_id: str
    operation: str
    original: str
    replacement: str
    penalty: float
    kind: str
    family: str
    source: tuple[str, ...]
    evidence: str


RULES = (
    SpellingRule("onset.n_to_l", "onset", "n", "l", 0.8, "pronunciation_merger", "onset", ("rime_schema", "google_examples"), "Optional n/l merger; a canonical ng onset is excluded."),
    SpellingRule("onset.drop_ng", "onset", "ng", "", 0.8, "pronunciation_merger", "onset", ("rime_schema",), "Optional ng/zero onset merger; bare syllabic ng is not deleted."),
    SpellingRule("onset.z_to_j", "onset", "z", "j", 0.8, "orthographic_alias", "onset", ("cantoneseinput_examples",), "Alternative input convention for the z onset. This is not Jyutping j."),
    SpellingRule("onset.c_to_ch", "onset", "c", "ch", 0.45, "orthographic_alias", "onset", ("cantoneseinput_examples",), "Alternative input convention for the c onset."),
    SpellingRule("onset.j_to_y", "onset", "j", "y", 0.45, "orthographic_alias", "onset", ("cantoneseinput_examples", "rime_schema", "yale_eduhk", "yale_pycantonese"), "Alternative y spelling for a canonical Jyutping j onset; jyu/jyun/jyut retain a single y."),
    SpellingRule("vowel.shorten_aa", "contains", "aa", "a", 0.3, "heuristic_alias", "vowel", ("google_examples", "rime_schema", "project_policy"), "Example-backed typing convenience, including fai for faai. It must not erase the strict mode's aa/a distinction."),
    SpellingRule("vowel.ou_to_o", "suffix", "ou", "o", 0.35, "heuristic_alias", "vowel", ("cantoneseinput_examples", "project_policy"), "Example-aligned convenience, including bo for bou; not a complete scheme conversion."),
    SpellingRule("vowel.aau_to_ao", "suffix", "aau", "ao", 0.4, "heuristic_alias", "vowel", ("google_examples", "project_policy"), "Inferred from bao in Google's chasiubao example; generalized only within one canonical syllable."),
    SpellingRule("vowel.eo_to_oe", "contains", "eo", "oe", 0.3, "heuristic_alias", "vowel", ("rime_schema", "project_policy"), "Input spelling tolerance; not a claim that distinct Jyutping finals are identical."),
    SpellingRule("vowel.oe_to_eo", "contains", "oe", "eo", 0.3, "heuristic_alias", "vowel", ("rime_schema", "project_policy"), "Input spelling tolerance; strict mode keeps eo and oe distinct."),
    SpellingRule("onset.s_to_sh", "onset", "s", "sh", 0.45, "orthographic_alias", "onset", ("project_policy",), "Hong Kong romanization of the s onset, as in Shing, Shek and Shun; sh is not a Jyutping onset."),
    SpellingRule("vowel.eoi_to_ui", "suffix", "eoi", "ui", 0.45, "orthographic_alias", "vowel", ("project_policy",), "Hong Kong casual spelling of the eoi final (sui, chui, lui, gui); generalizes the explicit sui/hui/kui aliases. Real ui syllables keep their exact readings."),
    SpellingRule("vowel.eon_to_un", "suffix", "eon", "un", 0.45, "orthographic_alias", "vowel", ("project_policy",), "Hong Kong casual spelling of the eon final (chun, lun, shun); no Jyutping un syllable takes these onsets."),
    SpellingRule("vowel.eot_to_ut", "suffix", "eot", "ut", 0.45, "orthographic_alias", "vowel", ("project_policy",), "Hong Kong casual spelling of the eot final (chut, lut)."),
    SpellingRule("onset.z_to_ch", "onset", "z", "ch", 0.8, "orthographic_alias", "onset", ("project_policy",), "Hong Kong romanization writes the z onset as ch, as in Chu and Cheung; costs the same as z -> j and more than c -> ch, so chun keeps its aspirated readings."),
    SpellingRule("vowel.yu_to_u", "suffix", "yu", "u", 0.45, "orthographic_alias", "vowel", ("project_policy",), "Hong Kong casual spelling of the yu final without the y glide (chu, su, zu)."),
    SpellingRule("vowel.yu_to_u_closed", "contains", "yu", "u", 0.8, "orthographic_alias", "vowel", ("project_policy",), "Glide-less lun, dun, tut for lyun, dyun, tyut; costs more than eon -> un so chun and sun keep their eon readings first."),
    SpellingRule("vowel.yun_to_uen", "suffix", "yun", "uen", 0.45, "orthographic_alias", "vowel", ("project_policy",), "Hong Kong romanization of the yun final (chuen, suen, luen, tuen, huen); generalizes the explicit -uen aliases."),
    SpellingRule("vowel.yut_to_uet", "suffix", "yut", "uet", 0.45, "orthographic_alias", "vowel", ("project_policy",), "Hong Kong romanization of the yut final (suet, huet, kuet)."),
)

# Explicit examples are not recursively rewritten. That prevents an exception
# from silently becoming a new general-purpose romanization conversion rule.
EXPLICIT_ALIASES = (
    ("nei", "lay", 1.15, "example.lay_nei", "documented_example", ("google_examples",)),
    ("nei", "nay", 0.5, "example.nay_nei", "documented_example", ("google_examples",)),
    ("ngo", "ngoh", 0.3, "example.ngoh_ngo", "documented_example", ("cantoneseinput_examples",)),
    ("bou", "boh", 0.4, "casual.boh_bou", "project_alias", ("project_policy",)),
    ("soeng", "sheung", 0.45, "casual.sheung_soeng", "project_alias", ("project_policy",)),
    ("soeng", "seung", 0.45, "casual.seung_soeng", "project_alias", ("project_policy",)),
    ("soeng", "shueng", 0.8, "example.shueng_soeng", "documented_example", ("google_examples",)),
    ("hoeng", "heung", 0.45, "casual.heung_hoeng", "project_alias", ("cantoneseinput_examples", "project_policy")),
    ("coeng", "cheung", 0.45, "casual.cheung_coeng", "project_alias", ("project_policy",)),
    ("zoeng", "jeung", 0.45, "casual.jeung_zoeng", "project_alias", ("project_policy",)),
    ("keoi", "keui", 0.45, "casual.keui_keoi", "project_alias", ("project_policy",)),
    ("keoi", "kui", 0.65, "casual.kui_keoi", "project_alias", ("cantoneseinput_examples", "project_policy")),
    ("heoi", "heui", 0.45, "casual.heui_heoi", "project_alias", ("project_policy",)),
    ("heoi", "hui", 0.65, "casual.hui_heoi", "project_alias", ("cantoneseinput_examples", "project_policy")),
    ("seoi", "seui", 0.45, "casual.seui_seoi", "project_alias", ("project_policy",)),
    ("seoi", "sui", 0.65, "casual.sui_seoi", "project_alias", ("cantoneseinput_examples", "project_policy")),
    ("zyun", "juen", 0.45, "casual.juen_zyun", "project_alias", ("project_policy",)),
    ("cyun", "chuen", 0.45, "casual.chuen_cyun", "project_alias", ("project_policy",)),
    ("syun", "suen", 0.45, "casual.suen_syun", "project_alias", ("project_policy",)),
    ("jyun", "yuen", 0.45, "casual.yuen_jyun", "project_alias", ("project_policy",)),
    ("jyut", "yuet", 0.45, "casual.yuet_jyut", "project_alias", ("project_policy",)),
    ("tyun", "tuen", 0.45, "casual.tuen_tyun", "project_alias", ("project_policy",)),
)


@dataclass(frozen=True, slots=True)
class MappingRecord:
    spelling: str
    canonical: str
    penalty: float
    kind: str
    rule_id: str
    source: tuple[str, ...]
    derivation: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SyllableMatch:
    canonical: str
    tone: str | None
    penalty: float
    rule_id: str


@dataclass(frozen=True, slots=True)
class YaleAsciiMapping:
    canonical: str
    spelling: str
    status: str
    source: tuple[str, ...]
    reason: str


def yale_ascii_mapping(base: str) -> YaleAsciiMapping:
    """Return one documented ASCII typing route, including explicit fallbacks.

    This accepts one tone-free canonical inventory key, not a phrase. Ordinary
    routes omit Yale diacritics and the low-register h marker. Numeric filters
    are handled separately by resolve_syllable/the decoder, unchanged.
    """
    if not isinstance(base, str) or not _BASE.fullmatch(base):
        raise ValueError("Expected one lowercase ASCII canonical syllable without a tone.")
    if base in SYLLABIC_NASALS:
        return YaleAsciiMapping(base, base, "table", YALE_TABLE_SOURCES,
                                "Table-backed syllabic nasal; no tone mark is inserted.")
    if base in {"hm", "hng"}:
        return YaleAsciiMapping(base, base, "extension", ("yale_kfcd_numeric",),
                                "KFCD's published numeric-Yale table explicitly attests this marginal syllabic nasal. Preserve every literal letter; this is supplementary source attestation, not ordinary Yale-chart coverage or independent linguistic approval.")
    onset, final, _ = scheme_parts(base)
    if final not in YALE_FINALS and final not in YALE_EXTENDED_FINALS:
        return YaleAsciiMapping(base, base, "jyutping_fallback", ("project_policy",),
                                "No verified Yale whole-final correspondence for this inventory form; retain its canonical Jyutping spelling.")
    yale_onset = YALE_ONSETS.get(onset, onset)
    if final in YALE_FINALS:
        yale_final = YALE_FINALS[final]
        status, source = "table", YALE_TABLE_SOURCES
        reason = "Table-backed onset and whole-final correspondence; tones omitted for ASCII input."
    else:
        yale_final = YALE_EXTENDED_FINALS[final]
        status, source = "extension", ("project_policy", "yale_pycantonese")
        reason = ("Project extension for a marginal final absent from the ordinary Yale charts, using the nucleus/coda spelling convention. "
                  "Final eu uses ew; oet uses eut. This is a typing policy, not table-backed formal Yale attestation.")
    if yale_onset == "y" and yale_final in ("yu", "yun", "yut"):
        yale_onset = ""
    return YaleAsciiMapping(base, yale_onset + yale_final, status, source, reason)


def yale_ascii(base: str) -> str:
    """Convert one tone-free syllable; rare forms use a documented JP fallback."""
    return yale_ascii_mapping(base).spelling


def yale_ascii_variants(base: str) -> tuple[str, ...]:
    """Primary Yale route plus independently sourced whole-syllable alternates."""
    return (yale_ascii(base), *YALE_ASCII_ALTERNATES.get(base, ()))


@dataclass(frozen=True, slots=True)
class LshkRecord:
    character: str
    codepoint: str
    reading: str
    onset: str
    final: str
    tone: str
    description: str
    description_reading: str
    kind: str
    line_number: int


def _check_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"Unknown spelling mode: {mode!r}")


def _apply(rule: SpellingRule, spelling: str) -> str | None:
    if rule.operation == "onset":
        if not spelling.startswith(rule.original):
            return None
        if rule.original == "n" and spelling.startswith("ng"):
            return None
        if len(spelling) == len(rule.original):
            return None
        if rule.rule_id == "onset.j_to_y" and spelling in ("jyu", "jyun", "jyut"):
            return spelling[1:]
        return rule.replacement + spelling[len(rule.original):]
    if rule.rule_id in ("vowel.yu_to_u", "vowel.yu_to_u_closed", "vowel.yun_to_uen", "vowel.yut_to_uet") and (
        spelling.startswith(rule.original) or spelling.startswith("jyu")):
        return None  # a bare vowel, or Jyutping-j ju/jun that would crowd z -> j input (jun for zeon)
    if rule.operation == "contains" and rule.original in spelling:
        return spelling.replace(rule.original, rule.replacement)
    if rule.operation == "suffix" and spelling.endswith(rule.original):
        return spelling[:-len(rule.original)] + rule.replacement
    return None


def syllable_mappings(base: str, mode: str = "forgiving") -> tuple[MappingRecord, ...]:
    """Generate bounded variants with their best auditable derivation.

    Input is a single canonical, tone-free inventory key. This function never
    treats a whole phrase as a global replacement buffer. Inventory membership
    is separate from this spelling policy so dictionary extensions remain usable.
    """
    _check_mode(mode)
    if not isinstance(base, str) or not _BASE.fullmatch(base):
        raise ValueError("Expected one lowercase ASCII canonical syllable without a tone.")
    # Identity means preserving the caller's canonical inventory key. It must
    # not imply that LSHK attests every dictionary extension supplied here.
    identity = MappingRecord(base, base, 0.0, "canonical", "canonical.identity", ("project_policy",), ())
    if mode == "jyutping":
        return (identity,)
    best: dict[str, MappingRecord] = {base: identity}
    # State includes onset usage, so alternative initial conventions cannot be
    # composed into spurious z -> j -> y aliases. Final changes may still combine.
    frontier = [(base, 0.0, (), (), False)]
    for _ in range(MAX_RULE_STEPS):
        following = []
        for spelling, cost, derivation, sources, onset_used in frontier:
            for rule in RULES:
                if rule.family == "onset" and onset_used:
                    continue
                changed = _apply(rule, spelling)
                if changed is None or not _BASE.fullmatch(changed):
                    continue
                path = derivation + (rule.rule_id,)
                source = tuple(sorted(set(sources) | set(rule.source)))
                penalty = round(cost + rule.penalty, 12)
                kind = rule.kind if len(path) == 1 else "composed_alias"
                record = MappingRecord(changed, base, penalty, kind, "+".join(path), source, path)
                previous = best.get(changed)
                if previous is None or (record.penalty, record.derivation) < (previous.penalty, previous.derivation):
                    best[changed] = record
                following.append((changed, penalty, path, source, onset_used or rule.family == "onset"))
        frontier = following
    for canonical, spelling, penalty, rule_id, kind, source in EXPLICIT_ALIASES:
        if canonical == base:
            record = MappingRecord(spelling, base, penalty, kind, rule_id, source, (rule_id,))
            previous = best.get(spelling)
            if previous is None or (record.penalty, record.derivation) < (previous.penalty, previous.derivation):
                best[spelling] = record
    # Compute Yale from the original syllable only. It cannot be recursively
    # merged/shortened (e.g. z -> j -> y), and existing alias costs stay stable.
    yale = yale_ascii_mapping(base)
    if yale.spelling not in best:
        best[yale.spelling] = MappingRecord(yale.spelling, base, YALE_ALIAS_PENALTY,
                                           "yale_ascii", YALE_RULE_ID, yale.source,
                                           (YALE_RULE_ID,))
    for spelling in YALE_ASCII_ALTERNATES.get(base, ()):
        if spelling not in best:
            best[spelling] = MappingRecord(spelling, base, YALE_ALIAS_PENALTY,
                "yale_ascii_alternate", YALE_ALTERNATE_RULE_ID, ("yale_kfcd_numeric",),
                (YALE_ALTERNATE_RULE_ID,))
    return tuple(sorted(best.values(), key=lambda record: (record.spelling != base, record.penalty, record.spelling, record.rule_id)))


def spelling_variants(base: str, mode: str = "forgiving") -> dict[str, float]:
    """Runtime adapter: spelling -> penalty, with identity in both modes."""
    return {record.spelling: record.penalty for record in syllable_mappings(base, mode)}


def mapping_records(canonical_syllables: Iterable[str], mode: str = "forgiving") -> list[MappingRecord]:
    return [record for base in sorted(set(canonical_syllables)) for record in syllable_mappings(base, mode)]


def resolve_syllable(raw: str, canonical_syllables: Iterable[str], mode: str = "forgiving") -> list[SyllableMatch]:
    """Resolve one spelling, retaining all readings and an unchanged tone digit."""
    _check_mode(mode)
    if not isinstance(raw, str):
        return []
    parsed = _INPUT_SYLLABLE.fullmatch(raw.strip().lower())
    if parsed is None:
        return []
    spelling, tone = parsed.groups()
    return sorted(
        (SyllableMatch(record.canonical, tone, record.penalty, record.rule_id)
         for record in mapping_records(canonical_syllables, mode) if record.spelling == spelling),
        key=lambda match: (match.penalty, match.canonical, match.rule_id),
    )


def read_lshk_table(path: str | Path) -> Iterator[LshkRecord]:
    """Read every LSHK TSV row without substituting descriptive pronunciations.

    ``kind`` is single_syllable, multi_syllable, or description_only. Only the
    first kind is a single-character/single-syllable reading inventory entry.
    """
    path = Path(path)
    columns = ["CH", "UCODE", "JP", "INIT", "FINL", "TONE", "DESC", "DESC_JP"]
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if reader.fieldnames != columns:
            raise ValueError(f"{path}:1: unsupported LSHK table columns")
        for line_number, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{path}:{line_number}: malformed LSHK table row")
            character, codepoint = row["CH"], row["UCODE"]
            if len(character) != 1 or codepoint != "U+" + format(ord(character), "04X"):
                raise ValueError(f"{path}:{line_number}: character/codepoint mismatch")
            reading = row["JP"]
            if _TONED.fullmatch(reading):
                kind = "single_syllable"
                if row["INIT"] + row["FINL"] + row["TONE"] != reading:
                    raise ValueError(f"{path}:{line_number}: inconsistent pronunciation components")
            elif not reading:
                kind = "description_only"
            elif all(_TONED.fullmatch(token) for token in reading.split()) and len(reading.split()) > 1:
                kind = "multi_syllable"
            else:
                raise ValueError(f"{path}:{line_number}: invalid JP pronunciation")
            yield LshkRecord(character, codepoint, reading, row["INIT"], row["FINL"], row["TONE"], row["DESC"], row["DESC_JP"], kind, line_number)


def scheme_parts(base: str) -> tuple[str, str, bool]:
    """Report scheme spelling shape, without claiming phonotactic attestation."""
    if base in SYLLABIC_NASALS:
        return "", base, True
    for onset in sorted(ONSETS, key=lambda value: (-len(value), value)):
        if base.startswith(onset):
            final = base[len(onset):]
            return onset, final, final in FINALS
    return "", base, base in FINALS


def build_mapping_artifacts(
    records: Iterable[LshkRecord],
    dictionary_bases: Iterable[str],
    *,
    source_metadata: dict | None = None,
    source_inventories: Mapping[str, Iterable[str]] | None = None,
    source_tones: Mapping[str, Mapping[str, Iterable[str]]] | None = None,
    retained_sources: Mapping[str, Iterable[str]] | None = None,
    retained_origins: Mapping[str, Iterable[str]] | None = None,
) -> tuple[dict, dict]:
    records = list(records)
    readings = {record.reading for record in records if record.kind == "single_syllable"}
    observed: dict[str, set[str]] = {}
    for reading in readings:
        observed.setdefault(reading[:-1], set()).add(reading[-1])
    dictionary = set(dictionary_bases)
    inventories = {source: set(bases) for source, bases in (source_inventories or {}).items()}
    if any(source not in SOURCES for source in inventories):
        raise ValueError("Mapping source inventory needs a declared provenance source ID.")
    inventories["lshk_table"] = set(observed)
    inventories["dictionary_database"] = dictionary
    rime = inventories.get("rime_dictionary", set())
    wordshk = inventories.get("wordshk_wordlist", set())
    typeduck = inventories.get("typeduck_principal", set())
    unihan = inventories.get("unihan_kcantonese", set())
    union = set().union(*inventories.values())
    tones_by_source = {source: {base: sorted(set(tones)) for base, tones in values.items()} for source, values in (source_tones or {}).items()}
    if any(source not in SOURCES for source in tones_by_source):
        raise ValueError("Observed tone inventories need a declared provenance source ID.")
    tones_by_source["lshk_table"] = {base: sorted(tones) for base, tones in observed.items()}
    if any(tone not in "123456" or len(tone) != 1 for values in tones_by_source.values() for tones in values.values() for tone in tones):
        raise ValueError("Observed source tones must be individual ASCII digits1..6.")
    for base in union:
        if not _BASE.fullmatch(base):
            raise ValueError(f"Unsupported canonical inventory key: {base!r}")
    syllables = []
    for base in sorted(union):
        onset, final, shape = scheme_parts(base)
        attestations = sorted(source for source, bases in inventories.items() if source != "dictionary_database" and base in bases)
        syllables.append({
            "base": base, "lshk_observed": base in observed, "dictionary_observed": base in dictionary,
            "rime_observed": base in rime, "wordshk_observed": base in wordshk,
            "typeduck_observed": base in typeduck, "unihan_observed": base in unihan,
            "source_attestations": attestations,
            "provisional_source_attestations": [source for source in attestations if SOURCES[source].get("property_status") == "Provisional"],
            "observed_tones_by_source": {source: values[base] for source, values in sorted(tones_by_source.items()) if base in values},
            "lshk_tones": sorted(observed.get(base, ())), "scheme_shape": shape, "onset": onset, "final": final,
            "yale_ascii": asdict(yale_ascii_mapping(base)),
            "retained_dictionary_sources": sorted(set((retained_sources or {}).get(base, ()))),
            "retained_dictionary_origins": sorted(set((retained_origins or {}).get(base, ()))),
            "classification": ("+".join(attestations) if attestations else "dictionary_only_unknown_source") + (":scheme_shape" if shape else ":outside_scheme_shape"),
        })
    baseline = set(observed) | rime
    additions = sorted(dictionary - baseline)
    inventory = {
        "schema_version": SCHEMA_VERSION, "profile_version": PROFILE_VERSION,
        "policy": "Observed inventory union across independently parsed LSHK, original Rime, accepted words.hk, TypeDuck principal export, provisional Unihan kCantonese, accepted CC-Canto/CC-CEDICT Cantonese and Cantonese Wordnet, and the full generated dictionary; not the Cartesian product of initials and finals. dictionary_observed means present in the full database; rime_observed is the original pinned Rime source before supplements. Source attestations survive dictionary winner deduplication. Wordnet appends after autoencoding without changing earlier pairs or supplying encoder inputs; source acceptance is not independent linguistic approval. Unihan remains explicitly provisional and TypeDuck export rows do not imply independent human phrase review. Absence from LSHK is not proof of invalidity. Scheme shape is not a complete legal-syllable validator; loan/dialect status requires separate evidence.",
        "scheme": {"onsets": list(ONSETS), "zero_onset": "", "finals": list(FINALS), "syllabic_nasals": list(SYLLABIC_NASALS), "tone_digits": ["1", "2", "3", "4", "5", "6"], "finals_added_in_2018": ["a", "oet"]},
        "sources": SOURCES, "source_metadata": dict(source_metadata or {}),
        "counts": {
            "lshk_rows": len(records), "lshk_single_syllable_rows": sum(record.kind == "single_syllable" for record in records),
            "lshk_description_only_rows": sum(record.kind == "description_only" for record in records),
            "lshk_multi_syllable_rows": sum(record.kind == "multi_syllable" for record in records),
            "lshk_observed_bases": len(observed), "lshk_observed_readings": len(readings), "dictionary_bases": len(dictionary),
            "rime_bases": len(rime), "wordshk_bases": len(wordshk),
            "typeduck_bases": len(typeduck), "unihan_bases": len(unihan),
            "union_bases": len(union), "shared_bases": len(set(observed) & dictionary),
            "lshk_only_bases": len(set(observed) - dictionary), "dictionary_only_bases": len(dictionary - set(observed)),
            "original_rime_lshk_union_bases": len(baseline), "original_rime_lshk_shared_bases": len(rime & set(observed)),
            "rime_only_vs_lshk_bases": len(rime - set(observed)), "lshk_only_vs_rime_bases": len(set(observed) - rime),
            "additional_dictionary_bases": len(additions), "unmapped_dictionary_bases": 0,
            "outside_scheme_shape_bases": sum(not item["scheme_shape"] for item in syllables),
            "yale_table_bases": sum(item["yale_ascii"]["status"] == "table" for item in syllables),
            "yale_extension_bases": sum(item["yale_ascii"]["status"] == "extension" for item in syllables),
            "yale_fallback_bases": sum(item["yale_ascii"]["status"] == "jyutping_fallback" for item in syllables),
        },
        "source_inventory_counts": {source: len(bases) for source, bases in sorted(inventories.items())},
        "source_observed_toned_form_counts": {source: sum(len(tones) for tones in values.values()) for source, values in sorted(tones_by_source.items())},
        "inventory_delta": {"baseline": "Original pinned Rime plus LSHK single-syllable observations", "baseline_union_bases": len(baseline), "added_dictionary_bases": additions},
        "syllables": syllables,
    }
    aliases = mapping_records(union)
    inventory["counts"]["unmapped_dictionary_bases"] = len(dictionary - {record.canonical for record in aliases})
    spellings: dict[str, set[str]] = {}
    for record in aliases:
        spellings.setdefault(record.spelling, set()).add(record.canonical)
    rulebook = [asdict(rule) for rule in RULES]
    rulebook.append({"rule_id": YALE_RULE_ID, "operation": "whole_syllable",
                     "original": "canonical onset + whole final", "replacement": "Yale ASCII route",
                     "penalty": YALE_ALIAS_PENALTY, "kind": "yale_ascii", "family": "yale",
                     "source": list(YALE_TABLE_SOURCES) + ["yale_pycantonese", "project_policy"],
                     "evidence": "Independent onset/final table lookup from the original canonical syllable; never recursively rewritten. Existing spelling/canonical alias costs are retained."})
    rulebook.append({"rule_id": YALE_ALTERNATE_RULE_ID, "operation": "whole_syllable",
                     "original": sorted(YALE_ASCII_ALTERNATES),
                     "replacement": {base: list(forms) for base, forms in YALE_ASCII_ALTERNATES.items()},
                     "penalty": YALE_ALIAS_PENALTY, "kind": "yale_ascii_alternate", "family": "yale",
                     "source": ["yale_kfcd_numeric"],
                     "evidence": "KFCD numeric-Yale field0 and Jyutping field6 attest these six additional tone-free whole-syllable routes. Primary ew spellings remain; alternates are never fed through merger/onset/vowel rules."})
    for canonical, spelling, penalty, rule_id, kind, source in EXPLICIT_ALIASES:
        rulebook.append({"rule_id": rule_id, "operation": "explicit", "original": canonical, "replacement": spelling, "penalty": penalty, "kind": kind, "family": "explicit", "source": list(source), "evidence": "Narrow exception; not recursively rewritten."})
    mapping = {
        "schema_version": SCHEMA_VERSION, "profile_version": PROFILE_VERSION,
        "policy": {"default_mode": "forgiving", "strict_mode": "jyutping", "max_rule_steps": MAX_RULE_STEPS, "max_onset_rewrites": 1, "tone_policy": "Numeric tone1..6 is passed through unchanged; aliases never substitute tones or imply all six tones are attested. Yale diacritics and low-register h are not parsed by this ASCII profile.", "ambiguity_policy": "Keep one best derivation per spelling/canonical pair; retain different canonical readings for the same spelling.", "excluded_formal_modes": ["Yale diacritics/tonal h", "Sidney Lau"], "narrowing_from_prototype": "Forbid composing alternative onset conventions, including zaa->jaa->yaa. Onset+final combinations such as caa->chaa->cha remain.", "yale_ascii": "One original-syllable table conversion in forgiving mode, never fed into legacy heuristics. Preserve existing alias costs; new aliases cost 0.45. Every base has a classified route, with explicit Jyutping fallback for unverified rare forms."},
        "yale_ascii": {"onsets": {"": "", **YALE_ONSETS}, "finals": YALE_FINALS,
                       "extension_finals": YALE_EXTENDED_FINALS,
                       "alternate_spellings": YALE_ASCII_ALTERNATES,
                       "syllabic_nasals": list(SYLLABIC_NASALS),
                       "routes": [asdict(yale_ascii_mapping(base)) for base in sorted(union)],
                       "counts": {status: sum(item["yale_ascii"]["status"] == status for item in syllables)
                                  for status in ("table", "extension", "jyutping_fallback")}},
        "sources": SOURCES, "source_metadata": dict(source_metadata or {}), "rules": rulebook,
        "counts": {"canonical_bases": len(union), "records": len(aliases), "identity_records": len(union), "nonidentity_records": len(aliases) - len(union), "distinct_spellings": len(spellings), "ambiguous_spellings": sum(len(values) > 1 for values in spellings.values())},
        "aliases": [asdict(record) for record in aliases],
    }
    return inventory, mapping
