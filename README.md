# Cantonese IME

An offline Cantonese input method for native Windows x64 applications. Type
plain Yale-style or Jyutping letters, select compact numbered word choices,
and build personal word and phrase preferences through normal typing.

The native **macOS port** uses InputMethodKit with the same offline engine,
dictionary and learning. Its source kit and Mac app/PKG build instructions are
in [macos/README.md](macos/README.md). It targets Apple silicon and Intel on
macOS 13 or later. Native Mac compilation, installation and typing validation
remain pending; see [Mac validation](docs/MACOS_VALIDATION.md).
The Mac sources and installer builder are included in this repository. The first
published binary release is for Windows x64.

## Install

Use [the Windows x64 installer](https://github.com/hadesng173/cantonese-ime/releases/download/v1.0.8/CantoneseIME-1.0.8-windows-x64-setup.exe)
and [its SHA-256 checksums](https://github.com/hadesng173/cantonese-ime/releases/download/v1.0.8/SHA256SUMS.txt)
from [release 1.0.8](https://github.com/hadesng173/cantonese-ime/releases/tag/v1.0.8).
Version 1.0.8 is published at the maintainer's request with known long-phrase
limitations. The [expanded review](docs/LONG_PHRASE_RELEASE_REVIEW.md) found
66/89 ordinary phrase targets in the first nine choices; the sentence-quality
gates remain failed. See the [publication scope](docs/RELEASE_PUBLICATION_1.0.8.md)
for validation limits and the ongoing engine-improvement work.
Run Setup normally and approve Windows' administrator prompt. The installer
includes its own private Python runtime; no system Python, developer tools,
or Internet connection is required on the target computer.

After installation, press **Win+Space** and choose **Cantonese IME** under
**Chinese (Traditional, Hong Kong SAR)**. The English US keyboard remains
available separately; **ENG** identifies that English source. For example,
type `fogeui` for 火炬 or `neihou` for 你好. Press Space to commit the highlighted
choice, or 1–9 to choose its numbered candidate. Escape cancels; Enter commits
the typed Latin letters. Space commits them as an English word with a trailing
space when nothing matches, and Shift+Space always does. A whole input that is a
common English word (the bundled SCOWL list) or one you have entered this way
before is offered as the word itself, instead of per-syllable combinations. Ctrl+Space or the native **粵 / 英** button changes modes.
Windows hosts that expose the TSF system tray/language bar can show the new
branding and mode icons. Reopen applications after an upgrade so they load
the new DLL. Chrome may keep background processes alive after its windows close;
save unfinished forms and enter `chrome://restart` to restart it fully.

Plain Yale spelling is treated consistently inside longer phrases: `dijiying`
can produce 啲字型 and `wunjodijiying` can produce 換咗啲字型. Selecting a full
phrase learns that input's preference separately from shorter words. Learning
transferred into longer input is calibrated against static alternatives, so
several frequently chosen short pieces do not receive an accumulating fixed
bonus for splitting the input. Valid alternative boundaries remain available;
long-sentence ranking still has limits.

The installer offers startup at sign-in. Start menu shortcuts enable or stop
the engine. Windows Settings > Apps provides the standard uninstall entry.
Upgrades and uninstall preserve personal learning at
`%LOCALAPPDATA%\CantoneseIME\learning.json`.

## Custom phrases and spellings

The easiest way is **Start menu > Cantonese IME > Cantonese IME Settings**, or
right-click the candidate panel or the **粵 / 英** button and choose **Custom
phrases, spellings and font...**. The settings window lists, adds, edits and
deletes entries and also sets the candidate font and size. Its **查拼音** tab
looks up the stored readings of a character or word, shows how common each is
and which spellings already work, and can start a custom spelling or phrase
from the selected reading. It runs as its own
program and only edits the two files described here, keeping comments in
`custom.txt`. Close it before upgrading.

You can also create `%LOCALAPPDATA%\CantoneseIME\custom.txt` (UTF-8) yourself.
Changes apply on the next keystroke; no restart is needed.

```text
# A phrase: typing exactly hh offers 曈曈, then 瞳瞳, before other candidates
hh 曈曈
hh 瞳瞳
# A spelling: moon is read as the Jyutping syllable mun (moon -> 門, moonhau -> 門口)
moon = mun
# The target may also be a spelling the IME already accepts: cheut means ceot
chut = cheut
```

Keys and spellings are 1–32 ASCII letters; phrase text is up to 64 characters.
An alias target is a toneless Jyutping syllable or a spelling the IME already
accepts; targets resolve against built-in spellings only, so aliases do not
chain. Invalid lines and unresolved targets are skipped. Custom phrases are never written to learning memory, and the file
is only read, never modified. Upgrades and uninstall keep it.

## Learned next-word suggestions (experimental)

Off by default. Turn it on in the settings window's **字型同聯想** tab. After you
commit a word, the candidate panel suggests what you usually type next: digits
1–9 choose a suggestion (which then suggests again), Escape closes the panel,
and any other key types normally.

Suggestions come only from your own typing: consecutive commits in the same
text box (a letter keeps the sequence going; other keys, focus changes or a
20-second pause break it) and phrases you have already chosen. Dictionary
frequencies are not used, so suggestions start sparse and improve with use.
Password and read-only fields are never recorded. The counts are stored only
in `%LOCALAPPDATA%\CantoneseIME\prediction.json`; **清除聯想記錄…** deletes them.
While suggestions are showing, digit keys choose a suggestion instead of
typing a digit.

## Candidate appearance

Version 1.0.8 uses **LXGW WenKai TC Regular (霞鶩文楷 TC)** for the numbered
word choices, at 20 logical pixels with Windows DPI scaling. The installer
includes the unmodified font; no separate Windows font installation is needed.
It is loaded privately for the IME's candidate panel. The receiving application
continues to control the font of committed text.

Right-click the candidate panel or the native **粵 / 英** mode button to change
**Background** to **Dark**, **Light** or **Windows system colors**, and **Panel
opacity** to **100%**, **95%**, **90%** or **85%**. The default is **Dark at 95%**.
Opacity applies to the entire panel, including its text; choose 100% for an
opaque panel. Windows High Contrast automatically uses opaque Windows colors.

Appearance choices are saved separately for each application. **Reset appearance
for this app** restores its default. Settings are stored at
`%LOCALAPPDATA%\CantoneseIME\appearance.ini` and preserved during upgrades and
uninstall. Applications that draw their own TSF candidate list control that
list's appearance.

To use another installed font or size, use the settings window's font tab, or
edit `appearance.ini` (UTF-8) in a text editor. Put the lines under `[global]` for every application, or under an
application's `[app-…]` section for that application only:

```ini
[global]
font=Microsoft JhengHei
font_size=24
```

`font` accepts an installed family's English or localized name (for example
`微軟正黑體`). If that font is not installed, the panel falls back to the default
order. `font_size` is 10–48 logical pixels before DPI scaling; the default is 20.
An empty `font=` restores the bundled font. Changes apply the next time the panel
opens, and choices made from the right-click menu keep these lines. Sandboxed
(AppContainer) applications cannot read the file and use the defaults.

The package is unsigned. Windows may display an unknown-publisher or SmartScreen
warning. Support is scoped to native 64-bit desktop applications on Windows x64;
32-bit applications, ARM64 and secure/password fields are outside this release.
Validation and remaining platform checks are recorded in
[the 1.0.8 release notes](docs/RELEASE_NOTES_1.0.8.md) and
[verification record](docs/release-1.0.8-validation.json). The
[first public release summary](docs/GITHUB_RELEASE_1.0.5.md) retains the
historical 1.0.5 checks.

## Build the installer

Use Python 3.11 or later and Windows x64. Build the dictionary from the retained
source snapshots, then build the native DLL/helper with the pinned project
toolchain before packaging:

```powershell
python tools/build_dictionary.py
python tools/build_release.py --fetch-tools
```

The build pins official application-local Python and Inno Setup downloads and
stages the release under `dist/CantoneseIME-1.0.8-x64`. It verifies source,
dictionary, runtime and attribution files before compiling the single setup
executable. Running the builder does not install the resulting IME.
The native sources, toolchain lock and build scripts are in [native/](native/tsf/README.md)
and [native/registration/](native/registration/README.md). The packager requires
fresh native build and guard evidence; a source checkout does not include the
maintainer's local build cache or machine-specific validation logs.

Dictionary data retains its original component licenses and attribution;
the combined adaptation is distributed under CC BY-SA 4.0. See
[data licensing](data/DATA_LICENSE.md),
[third-party notices](data/THIRD_PARTY_NOTICES.md) and
[release notices](installer/RELEASE_NOTICES.txt). The bundled LXGW WenKai TC
font retains its [SIL Open Font License 1.1](assets/fonts/lxgw-wenkai-tc/OFL.txt)
and [original credits and source notice](assets/fonts/lxgw-wenkai-tc/NOTICE.md).
The independent application
code is separate from these data licenses and currently has no general
redistribution license declaration. See the [release notices](installer/RELEASE_NOTICES.txt).

## Dictionary and development evidence

The included catalog contains **776,335 text/pronunciation pairs across 644,427
texts**. It includes authored readings and explicitly identified mechanically
encoded headwords. Pronunciation and ranking refinement remain ongoing work.

The previous v3 runtime completed an exhaustive finite-catalog retrieval check
over 503,708 compact Yale input groups. Its
[frozen catalog proof](docs/final-yale-catalog-coverage.json) and
locally retained installed validation remain historical evidence for those exact
bytes. Machine-specific installation logs are excluded from this public
repository. The release has its own
native, learning and installer checks; earlier evidence is not a substitute
for checking the new mode indicator or a new Windows version.

## Run the engine

From the repository root:

```powershell
python tools/build_dictionary.py
python -m cantonese_ime lookup neihou
python -m cantonese_ime lookup bohim --mode forgiving
python -m cantonese_ime lookup "nei5'hou2" --mode jyutping --json
python -m cantonese_ime lookup lei4 --mode jyutping --offset 108 --limit 9 --json
python -m cantonese_ime reverse 出去
```

The dictionary build uses the pinned source files already in this project. It
requires no network access, package installation, compiler, or administrator access.
The generated database is already present on this machine; rebuild it when needed.

Examples observed with the bundled data:

| Input | Mode | Candidate |
| --- | --- | --- |
| `neihou` | Jyutping | 你好, first |
| `bouhim` | Jyutping | 保險, first |
| `bohim` | Forgiving | 保險, first |
| `lay` | Forgiving | 你, first |
| `failok` | Forgiving | 快樂, first |
| `ngosheungsikchasiubao` | Forgiving | 我想食叉燒包, second |

## Replay typing and selection

```powershell
python -m cantonese_ime type bohim --keys Space --trace
python -m cantonese_ime type neihou --keys Down Space --json
python -m cantonese_ime type Hello --keys Enter
```

`type` feeds individual characters into the composition state machine. Space
commits the highlighted candidate; 1–9 select a visible slot; Up/Down and
PageUp/PageDown navigate; Enter commits the original Latin text; Escape cancels.
Apostrophes mark syllable boundaries. Numeric tones are accepted by `lookup`;
the typing session reserves digits for candidate selection. Pages are fetched
as you navigate; stored homophones remain selectable beyond the first 90 results.
Broad prefix completions have a separate bound and may require more typing.

The installed native IME learns committed selections and phrases for the full entered spelling locally
in `%LOCALAPPDATA%\CantoneseIME\learning.json`; the update preserved existing
personal data. CLI selection memory remains opt-in with an explicit path:

```powershell
python -m cantonese_ime type bohim --keys Space --learning .local/learning.json
python -m cantonese_ime lookup bohim --learning .local/learning.json
```

## Dictionary and verification

The development SQLite lexicon contains **776,335 text/pronunciation pairs**
across **644,427 distinct texts** and 878 observed
syllable bases. It combines pinned Rime Cantonese, LSHK, words.hk, TypeDuck,
Unihan, CC-Canto, Cantonese Wordnet, CC-CEDICT written headwords and Yue Wiktionary
data: 327,259 source-explicit pairs and 449,076 mechanically generated
phrase readings. Unihan's provisional readings support direct character lookup;
they do not generate whole-word readings. CC-Canto adds explicit traditional and
simplified headwords. Wordnet adds only explicit own-entry readings after phrase
generation, preserving earlier IDs, readings and weights. CC-CEDICT contributes
36,884 generated pairs for 21,038 new texts; its Mandarin readings are excluded.
Yue Wiktionary adds 323 explicit pairs and 166 texts after overlap removal.
The combined development data uses
[CC BY-SA 4.0](data/DATA_LICENSE.md), with original component notices retained.
Every one of the 330,701 text-only source phrases has an encoding record;
80 reach the bounded encoder's path limit and stay quarantined. Generated
pronunciations and polyphonic alternatives still need linguistic review. See the
[dictionary documentation](data/README.md) and [license notices](data/THIRD_PARTY_NOTICES.md).

```powershell
python -m unittest discover -s tests -v
python -m cantonese_ime evaluate --repeat 3 --output docs/engine-evaluation.json
python tools/build_mappings.py
python tools/audit_dictionary.py --evaluation docs/engine-evaluation.json --output data/generated/dictionary-audit.json
```

The 40 development seed cases find their target in the first nine candidates;
34 targets rank first with the v3 dictionary. These examples guide development and are not
a held-out accuracy benchmark. The [saved evaluation](docs/final-yale-engine-evaluation.json)
records actual ranks and timings.

Synthetic checks exercise every exported spelling relation and collision.
A stored-prefix search fix also makes
all 222 long target texts reachable from each tested shorter prefix form in that
historical check. The current input limit is 256 characters; suffix-reading
ambiguity and bounded composition remain.

An earlier [HKCanCor external baseline](docs/HKCANCOR_EVALUATION.md) tests 500
fixed corpus-derived cases. Fully toned top-9 coverage is 232/250 word types and
160/250 conversational spans; tone-free spans reach 111/250. This exposes a
composition/ranking gap. Upstream source overlap is unresolved, so these are
external annotation results rather than clean held-out or natural typing scores.
Human pronunciation, spelling-profile and typing review are still required.

The newer [composition search](docs/COMPOSITION_SEARCH_UPDATE.md) fixes duplicate
paths hiding distinct suggestions. On a replay of those same cases, top-90
text hits rise from 778 to 810 out of 1,000 requests, while top-9 hits fall from
722 to 720. Some completions move to later pages. These are development
comparisons with all gains and losses retained; ranking still needs work.

## Implementation

- [Engine design and current limits](docs/ENGINE.md)
- [Dictionary readiness and acceptance requirements](docs/DICTIONARY_READINESS.md)
- [Source and license audit](docs/dictionary-source-audit.md)
- [Spelling map policy and provenance](data/mappings/README.md)
- [Current pronunciation and spelling review pack](data/review-v2/README.md)
- [Contextual evidence for seven apparent reading gaps](docs/PRONUNCIATION_GAP_REVIEW.md)
- [Independent typing-corpus collection template](eval/HELDOUT_COLLECTION.md)
- [External corpus source and evaluation](docs/HKCANCOR_EVALUATION.md)
- [Initial Windows research proposal](docs/RESEARCH_AND_PLAN.md)
- [Validation and later Windows checks](docs/VALIDATION.md)

The decoder is our Python implementation over licensed data, independent of the
librime runtime. The native TSF frontend uses it through a local broker. Learned
complete words and phrases also help longer composition: choosing `ng` for 唔
can bring 唔好 into the choices for `ngho` and 唔係咁 for `nghaigam`. Choices made
for a full input retain their own priority. This is local preference learning;
broader contextual language modeling and pronunciation refinement remain future
work. Historical native typing evidence is retained separately from the current
release checks and the user's application/visual acceptance.
