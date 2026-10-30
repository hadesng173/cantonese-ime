# Third-party dictionary notices

## Combined database adaptation

Full builds that select CC-Canto/CC-CEDICT Cantonese Readings include a transformed
combined data adaptation offered under **CC BY-SA 4.0 International**. Preserve
[DATA_LICENSE.md](DATA_LICENSE.md), [the full license](licenses/CC-BY-SA-4.0.txt)
and all original source notices below. The original component licenses remain
applicable; this is not a claim that all original data was exclusively CC BY 4.0.
Independent program code is separate from this dictionary-data license.

## CC-Canto and CC-CEDICT Cantonese Readings

The project retains **CC-Canto**, Version 2017-02-02, copyright **(c) 2015-17
Pleco Inc.**, and **CC-CEDICT Cantonese Readings**, Version 2015-09-23, copyright
**(c) 2015 Pleco Software Incorporated**, under **Creative Commons
Attribution-ShareAlike 3.0 Unported**. The exact source headers and download-page
copyright wording are retained; the latter also credits the separate CC-CEDICT
title's maintainers and Paul Andrew Denisowski.

- Official [download/declaration](https://www.cc-canto.org/download.html) and
  [project background](https://www.cc-canto.org/about.html)
- Exact archives, extracted members, pages and hashes:
  [cccanto.sources.lock.json](supplements/cccanto.sources.lock.json)
- Retained original [full CC BY-SA 3.0 license](vendor/cc-canto/2017-02-02-and-2015-09-23/LICENSE-CC-BY-SA-3.0.txt)
  and [download declaration](vendor/cc-canto/2017-02-02-and-2015-09-23/download.html)
- [Source admission and overlap audit](../docs/cccanto-data-audit.md)

Vendor archives and extracted source texts are unmodified. The separately marked
`parser-attested-bases.json` is project-generated validation metadata, derived
from the preserved pre-expansion database rather than claimed as a Pleco file.
Import transformations select traditional/simplified headword forms and explicit
brace Jyutping, normalize reading case/spacing, require codepoint/syllable
alignment, quarantine unsupported notation and un-attested bases, derive lookup
fields, and remove duplicates after earlier sources. New pairs retain unknown
frequency weight zero. Bracketed Mandarin and English definitions do not become
lookup or review-field data. No automatic spelling, tone, punctuation or glyph
repair has been inferred. Quarantine is not a linguistic invalidity judgement.
Additional CC-Canto character readings remain available for direct lookup but
do not feed Rime mechanical phrase autoencoding; their unmeasured zero weights
must not widen unrelated contextual readings. Accepted authored whole-word
readings remain live and use the existing explicit-reading precedence policy.

These modifications are this project's responsibility, with no Pleco endorsement
implied. CC BY-SA 3.0 section4(b)(ii) permits the transformed adaptation under a
later license with the same elements; the combined transformed database and
derived CC-Canto review data are offered under CC BY-SA 4.0. Preserve attribution,
source/change notices and ShareAlike terms when redistributing adaptations.

## Rime Cantonese

This project includes a snapshot of **Rime Cantonese Input / 中州韻粵語拼音輸入方案**,
developed and maintained by the **Cantonese Computational Linguistics Infrastructure
Development Workgroup (CanCLID) and contributors**. Individual authors and source
acknowledgements appear in the retained upstream README and table headers.

- Project: <https://github.com/rime/rime-cantonese>
- Exact revision: <https://github.com/rime/rime-cantonese/tree/259f0e48bba840c3a2e0d117539e96937f3d89bc>
- Upstream word/reading source: <https://github.com/CanCLID/rime-cantonese-upstream>
- License: [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/)
- Full local license: [LICENSE-CC-BY](vendor/rime-cantonese/259f0e48bba840c3a2e0d117539e96937f3d89bc/LICENSE-CC-BY)
- Retained attribution and licensing statement: [upstream README](vendor/rime-cantonese/259f0e48bba840c3a2e0d117539e96937f3d89bc/README-en.md)
- Exact included files and their hashes: [sources.lock.json](sources.lock.json)

Included upstream files are `jyut6ping3.chars.dict.yaml`,
`jyut6ping3.words.dict.yaml`, `jyut6ping3.phrase.dict.yaml`,
`essay-cantonese.txt`, `README-en.md` and `LICENSE-CC-BY`. The fetched vendor
files are unmodified. Text-only phrase rows contribute mechanically generated
readings through the bounded, attributed autoencoder described below.

Changes in the generated SQLite representation: explicit tab-separated rows are
parsed into indexed fields; reading spacing/case is normalized; additional
tone-free lookup fields are generated; duplicate text/reading pairs retain their
highest existing weight. Missing and percentage weights are resolved against
the included preset frequency table. The frequency table supplies weights only.
Text-only phrases are encoded from qualifying existing one-syllable translation
tokens. Complete results retain all enumerated alternatives and a generated
origin; bounded partial results remain quarantined in an audit ledger. Generated
possibilities have not been independently verified as contextual pronunciations.
No new frequency measurements have been asserted.

The project's authors are responsible for those changes. CanCLID and upstream
contributors do not endorse this project. The original license and attribution
must accompany redistributions of the data and generated database. CC BY 4.0
permits commercial reuse subject to its terms; there is no noncommercial restriction
on these included files.

The separately licensed OpenStreetMap-derived `jyut6ping3.maps` dataset and the
lettered dictionary are excluded. No Google or cantoneseinput.com dictionaries
are copied. No Rime native runtime, frontend or optional plugin is bundled.

## LSHK character pronunciation table

The project includes **Cantonese Pronunciation List of the Characters for
Computers / 電腦用漢字粵語拼音表**, maintained by the **Jyutping Workgroup of the
Linguistic Society of Hong Kong**. The upstream acknowledgements credit
**Dr Cheung Kwan Hin, Prof Lu Qin and Nathan Hammond**.

- Source: [lshk-org/jyutping-table](https://github.com/lshk-org/jyutping-table)
- Revision: [dad2dd6d6f02fc51138ecc6818f7b38eba5c2ad3](https://github.com/lshk-org/jyutping-table/tree/dad2dd6d6f02fc51138ecc6818f7b38eba5c2ad3)
- License: Creative Commons Attribution 4.0 International
- Retained [full license](vendor/lshk-jyutping-table/dad2dd6d6f02fc51138ecc6818f7b38eba5c2ad3/LICENSE), [README and credits](vendor/lshk-jyutping-table/dad2dd6d6f02fc51138ecc6818f7b38eba5c2ad3/README.md), and [source lock](mappings/lshk.sources.lock.json)

Retained vendor files are unmodified. Explicit `JP` readings are imported with
zero weight when they add a pair. Blank-`JP` descriptions remain review records;
`DESC_JP` is never imported as the character's own reading. Tones, alternative
readings and the three explicit multisyllable readings are preserved. Derived
inventory and spelling exports distinguish source attestation from project aliases.
These format and selection changes are this project's responsibility; no
upstream endorsement is implied. Preserve the attribution, license and change
notice with redistributions of derived data.

## words.hk public-domain wordlist

The project includes the **words.hk 粵典 wordlist and pronunciations** snapshot
retrieved on 22 October 2026. Credit is retained to **words.hk contributors**.

- Source and declaration: [words.hk wordlist page](https://words.hk/faiman/analysis/wordslist/)
- Data: [official wordlist JSON](https://words.hk/faiman/analysis/wordslist.json)
- Retained [exact declaration page](vendor/words-hk/2026-10-22/license-declaration.html) and [source lock](supplements/wordshk.sources.lock.json)

The retained page declares the wordlist/pronunciation data public domain and
requests credit. That scope does not cover definitions, examples, media or other
API datasets; none is imported here. Raw files are retained unmodified. The
import normalizes whitespace in valid Jyutping readings and exactly one documented
trailing variant-star marker in text, retaining raw provenance. Malformed or
non-Jyutping notation and ellipsis templates stay in review records. Overlapping
pairs do not override existing entries. All new supplemental pairs retain zero
weight as an unknown-frequency sentinel. Whole-text Rime counts are not assigned
as pronunciation-specific frequencies. These transformations are
attributed to this project.

## TypeDuck principal dictionary

The project includes the principal `jyut6ping3.dict.yaml` from **TypeDuck Schema**,
with credit to the **TypeDuck project and contributors**.

- Source: [TypeDuck-HK/schema](https://github.com/TypeDuck-HK/schema)
- Exact revision: [7f9d90d4185eca293576de5b5dddaaf1d265f3c9](https://github.com/TypeDuck-HK/schema/tree/7f9d90d4185eca293576de5b5dddaaf1d265f3c9)
- License: Creative Commons Attribution 4.0 International for the principal data
- Retained [full license](vendor/typeduck-schema/7f9d90d4185eca293576de5b5dddaaf1d265f3c9/LICENSE), [README declaration](vendor/typeduck-schema/7f9d90d4185eca293576de5b5dddaaf1d265f3c9/README.md) and [selected source lock](supplements/typeduck.sources.lock.json)

Only the principal table is imported. Reverse lookup files, rich dictionary
annotations, native code, schemas and the separate frequency file are excluded.
Vendor files are unmodified. Valid explicit text/readings are normalized for
reading case/spacing and deduplicated after earlier selected sources. Two
tilde-marked phonetic annotations remain quarantined with raw lines intact.
Source percentage scores are not imported as probabilities or measured
pronunciation frequencies; new pairs retain unknown weight zero.

The source explicitly declares the principal data CC BY 4.0. Historical versions
imported a separate OSM map table, removed before the replacement monolithic
dictionary. The inspected history does not establish current map inheritance or
complete row-level authorship. See [the source audit](../docs/typeduck-data-audit.md)
for that distinction. Physical explicit rows are not represented as independently
human-reviewed pronunciations. These import transformations are this project's
responsibility. Retain attribution, license and modification notices on redistribution;
no TypeDuck endorsement is implied.

## Unicode Unihan character readings

This project includes Unicode 18.0.0 Unihan data, **Copyright 1991–2026 Unicode,
Inc.**, under **Unicode License V3**, SPDX `Unicode-3.0`.

- Official [versioned archive](https://www.unicode.org/Public/18.0.0/ucd/Unihan.zip)
- Retained [full notice](vendor/unihan/18.0.0/LICENSE.txt), [release ReadMe](vendor/unihan/18.0.0/ReadMe.txt), and [source lock](supplements/unihan.sources.lock.json)
- Property definition: [UAX #38, kCantonese](https://www.unicode.org/reports/tr38/tr38-41.html#kCantonese)

The raw archive and extracted readings are retained unchanged. Only direct
`kCantonese` codepoint/reading values are imported; other properties and character
variant inheritance are excluded. These **provisional** customary readings target
collation and transliteration. They are additional character attestations, not
normative pronunciation decisions or whole-word readings. New pairs have unknown
weight zero and never override existing entries. This source does not feed the
mechanical phrase encoder. Format conversion to indexed Unicode text/reading
fields is this project's change. Preserve the full copyright and permission
notice with copies of the data or associated documentation. No fonts or code-chart
glyphs are included and no Unicode endorsement is implied.

## Cantonese Wordnet candidate snapshot

An additional candidate source is retained from **Cantonese Wordnet**, created
and maintained by **Joanna Ut-Seong Sio and Luis Morgado da Costa**, under
**Creative Commons Attribution 4.0 International**. Its unmodified XML and README
are pinned to `6434cbc1d692d3061266352aa223a8df4a9c883e`. This snapshot and any
separate candidate dictionary do not change the installed IME or the staged
`3fccc5bc…` dictionary. The default full builder now selects Wordnet and appends
its accepted zero-weight pairs after phrase generation. Two separate clean
builds are retained for verification, and the development database now selects
these readings. Native installation remains separately identified by its
package manifest.

- [Source and transformation attribution](vendor/cantonese-wordnet/6434cbc1d692d3061266352aa223a8df4a9c883e/ATTRIBUTION.md)
- [Full CC BY 4.0 terms](vendor/cantonese-wordnet/6434cbc1d692d3061266352aa223a8df4a9c883e/LICENSE-CC-BY-4.0.txt)
- [Pinned source lock](supplements/wordnet.sources.lock.json) and [candidate research](../docs/ADDITIONAL_CANTONESE_SOURCES.md)
- Citation: Sio, Joanna Ut-Seong and Morgado da Costa, Luis (2019),
  *Building the Cantonese Wordnet*, Proceedings of the Tenth Global Wordnet
  Conference, pp.206–215. [Paper](https://aclanthology.org/2019.gwc-1.26/).

The candidate transformation selects explicit readings from the `cantown-yue`
lexicon and their own lemma, excludes the English sibling lexicon, normalizes
case/whitespace, deduplicates exact pairs and quarantines missing, malformed,
unaligned or unknown-base readings. It does not infer pronunciations, import
definitions or relations, repair changed-tone notation, or assign measured
frequency. Any new pairs retain zero as unknown frequency. Existing pairs and
weights are preserved; character additions do not feed phrase autoencoding.
These transformations are this project's responsibility. Preserve the authors'
attribution, citation, source license and change notices when sharing derived
candidate data. The combined adaptation retains its CC BY-SA 4.0 notice while
the original Wordnet component remains CC BY 4.0; application code is separate.
No source endorsement or independent linguistic approval is implied.

## CC-CEDICT written-headword candidate

An additional written-form snapshot is retained from **CC-CEDICT contributors**,
published by **MDBG**; original CEDICT copyright **1997, 1998 Paul Andrew
Denisowski**. The retained 2026-10-21 release and its publisher page declare
**Creative Commons Attribution-ShareAlike 4.0 International**. This is distinct
from the earlier CC-CEDICT dataset containing authored Cantonese readings.

- [Pinned source and release declaration](supplements/cccedict-headwords.sources.lock.json)
- [Source attribution and transformation notice](vendor/cc-cedict/2026-10-21-f6b2248b589a/ATTRIBUTION.md)
- [Full source terms](vendor/cc-cedict/2026-10-21-f6b2248b589a/LICENSE-CC-BY-SA-4.0.txt)
- Original [publisher page](https://www.mdbg.net/chinese/dictionary?lang=en&page=cc-cedict)

The candidate uses Han traditional/simplified headwords only. Mandarin Pinyin
and definitions supply no Cantonese lookup readings. New readings are generated
mechanically from a separately frozen set of earlier Cantonese character
translations, with complete occurrence and encoding ledgers; incomplete results
are quarantined entirely. Generated rows have unknown-frequency zero, preserve
earlier entries, and never supply new encoder translations. These changes are
this project's responsibility, not independent pronunciation approval. Retain
original attribution, ShareAlike terms and change notices with derived data.
The source lock records candidate preparation; each later dictionary manifest
separately identifies whether the source is selected. Retaining this notice does
not update an installed IME or retroactively change historical packages.

## KFCD numeric Yale spelling evidence

**KFCD / 開放詞典**, copyright **2015**, publishes *pingyam* under **Creative
Commons Attribution 3.0 Unported**. The reviewed source is pinned to
`d0ceb32c2da07d156db99e50b192aaf45236ca95`.

- [Original README](vendor/kfcd-pingyam/d0ceb32c2da07d156db99e50b192aaf45236ca95/README.md) and [original license declaration](vendor/kfcd-pingyam/d0ceb32c2da07d156db99e50b192aaf45236ca95/LICENSE)
- [Full CC BY 3.0 terms](vendor/kfcd-pingyam/d0ceb32c2da07d156db99e50b192aaf45236ca95/LICENSE-CC-BY-3.0.txt)
- [Attribution and change notice](vendor/kfcd-pingyam/d0ceb32c2da07d156db99e50b192aaf45236ca95/ATTRIBUTION.md) and [retained source lock](mappings/kfcd.sources.lock.json)
- Original [conversion table](https://github.com/kfcd/pingyam/blob/d0ceb32c2da07d156db99e50b192aaf45236ca95/pingyambiu)

The reviewed transformation selects six additional literal whole-syllable
numeric-Yale spellings after omitting only terminal tones, plus literal `hm/hng`
extension attestations. It retains existing primary routes, every lexical
letter and raw learning inputs. The table describes possible spellings and
tones, not measured word frequencies or independent linguistic approval.
Original source files are unmodified; selection, alias metadata and runtime
matching are this project's changes. Preserve the source's original attribution,
license and change notice. No KFCD endorsement is implied. Current build and
package hashes identify which runtime actually selects these routes; a retained
source or notice alone does not change a historical or installed mapping.

## Yue Wiktionary explicit Cantonese fields

**Yue Wiktionary contributors** supply additional literal Cantonese readings
from the official Wikimedia dump dated **2026-10-01**, under **Creative Commons
Attribution-ShareAlike 4.0 International**, as declared by the retained page
footer and Wikimedia Terms of Use.

- [Pinned official source, checksums and template revisions](supplements/yue-wiktionary.sources.lock.json)
- [Attribution and transformation notice](vendor/yue-wiktionary/2026-10-01-e09d60b43588/ATTRIBUTION.md)
- [Full license terms](vendor/yue-wiktionary/2026-10-01-e09d60b43588/LICENSE-CC-BY-SA-4.0.txt), [original page licensing footer](vendor/yue-wiktionary/2026-10-01-e09d60b43588/yue-page-licensing-sample.html), and [Wikimedia Terms of Use](vendor/yue-wiktionary/2026-10-01-e09d60b43588/wikimedia-terms.html)
- Original [dated dump](https://dumps.wikimedia.org/yuewiktionary/20261001/) and [contributor site](https://yue.wiktionary.org/)

The transformation accepts only main-namespace Han own-headword entries with
explicit readings in their exact Yue language section. Supported literal fields
are line-leading flat `zh-pron` with one `c=` value, the documented first
positional field of `Yue-rom`, and the documented paired index/display fields
of `粵拼`. Case, whitespace and explicitly documented superscript tones may
normalize; raw fields, locations, source revision URLs and contributor-history
links remain in the database ledgers. No templates or Lua execute. Unsupported,
ambiguous, marked, unknown-base or unaligned fields remain review events.

Every page, language section, recognized field and review event is accounted.
Accepted new pairs are appended after earlier generation with unknown-frequency
zero. Existing rows, weights and IDs remain; these readings never supply new
character translations to an earlier encoder. These changes are this project's
responsibility and do not establish independent pronunciation approval. Preserve
contributor attribution, revision/history links, additional original attribution
where applicable, full ShareAlike terms and change notices with derivatives.
Wikimedia and its contributors do not endorse this IME. The raw source lock and
attribution preserve the original candidate preparation status; build and package
manifests separately identify actual selection.

## Adapted librime autoencoding algorithm

`cantonese_ime/autoencode.py` adapts the algorithms in librime's
`EntryCollector.TranslateWord` and `ScriptEncoder`, pinned to
`1b3417d2b6a11b5e50e0e1fa9dc396056aa11dce`. It is not the librime binary runtime.

- Copyright: **(c) 2014, RIME Developers**
- License: BSD 3-Clause; [full retained notice](licenses/librime-BSD-3-Clause.txt)
- Upstream [entry collector](https://github.com/rime/librime/blob/1b3417d2b6a11b5e50e0e1fa9dc396056aa11dce/src/rime/dict/entry_collector.cc) and [script encoder](https://github.com/rime/librime/blob/1b3417d2b6a11b5e50e0e1fa9dc396056aa11dce/src/rime/algo/encoder.cc)

The adaptation uses Python Unicode boundaries, suffix reachability pruning and
a finite work guard, deduplicates emitted codes, and conservatively quarantines
the 32-path limit. Algorithm metadata records these changes. Retain the full BSD
copyright, conditions and disclaimer with source or binary distributions.
Shipping additional native components later requires their own applicable notices.

## HKCanCor evaluation annotations

The project retains the original **Hong Kong Cantonese Corpus (HKCanCor)**
transcriptions by **K. K. Luke and May L. Y. Wong**, hosted by Francis Bond,
under Creative Commons Attribution 4.0 International.

- Source: [fcbond/hkcancor, pinned revision](https://github.com/fcbond/hkcancor/tree/39aeadf920e0b5ca93d0ad7792c59e740e7bdd65)
- Retained [full license](../eval/vendor/hkcancor/39aeadf920e0b5ca93d0ad7792c59e740e7bdd65/data/LICENSE), [declaration](../eval/vendor/hkcancor/39aeadf920e0b5ca93d0ad7792c59e740e7bdd65/data/README), and [evaluation source lock](../eval/hkcancor.sources.lock.json)
- Citation: K. K. Luke and May L. Y. Wong (2015), *The Hong Kong Cantonese
  Corpus: Design and Uses*, Journal of Chinese Linguistics Monograph Series 25,
  312–333.

Original UTF8 transcript bytes, documentation and the format specification are
retained unchanged. This source is used only for evaluation; its readings and
frequencies are not imported into the dictionary or used to fit this engine.
The derived evaluation file joins eligible source readings into spaced
syllables and compact canonical inputs, deduplicates source types and selects
them using the recorded hash policy. Unsupported annotations and long spans
have explicit exclusion counts. These changes are this project's responsibility.
Retain attribution, the license and the change notice when redistributing the
derived cases. No endorsement or independent approval of this IME is implied.

## SCOWL English word list

English candidates use the **SCOWL** British (-ise) speller word list, size 60,
release 2026.02.25, Copyright 2000-2026 by **Kevin Atkinson**, distributed under
its permissive notice (use, copy, modify, distribute and sell, with the
copyright and permission notice retained).

- Source: [en-wl/wordlist-diff, rel-2026.02.25](https://github.com/en-wl/wordlist-diff/tree/rel-2026.02.25)
- Retained [copyright and permission notice](vendor/scowl/2026.02.25/Copyright) and [source lock](vendor/scowl/2026.02.25/source.lock.json)

The list file is unmodified. At run time only lowercase letter-only entries
are used, to recognise a whole typed input as an English word.
