# Cantonese IME 1.0.9 for Windows x64

This release packages the latest merged main-branch features. It is an unsigned,
offline Windows x64 installer with its own private Python runtime, dictionary
and LXGW WenKai TC font. No separate Python or font installation is needed.
Building or downloading this release does not install it automatically.

[Download Setup](https://github.com/hadesng173/cantonese-ime/releases/download/v1.0.9/CantoneseIME-1.0.9-windows-x64-setup.exe)
and [verify SHA-256 checksums](https://github.com/hadesng173/cantonese-ime/releases/download/v1.0.9/SHA256SUMS.txt).
Save work and close the Settings window before upgrading; reopen applications
afterward so they load the updated IME. Personal learning and custom entries
are preserved.

## Changes

- Additional forgiving spellings for `-yun` syllables: `juen`, `chuen`, `suen`,
  `yuen` and `tuen`.
- AppContainer compatibility code for sandboxed Windows applications.
- Custom phrase shortcuts and spelling aliases in a local UTF-8 `custom.txt`;
  changes reload while typing.
- Native Settings window for managing custom entries and choosing the candidate
  font family and size. Open it from the Start menu or candidate appearance menu.
- Reverse lookup in Settings: inspect stored character/word readings and accepted
  spellings, then create a custom entry from a selected reading.
- Experimental next-word suggestions, off by default. Enable them in Settings;
  they learn only from local typing and phrase preferences. Digits 1-9 select a
  suggestion; Escape dismisses it. Settings can clear the sequence records.
- Prediction storage now evicts the weakest context even when it is newly added,
  keeping the configured context limit during continued use.
- Broker authentication checks the client's Windows user and session before
  dispatching each request, preserving isolation with the broader AppContainer
  pipe permissions.

## Validation and known limits

The full automated suite completed **722 tests**, with **3 conditional skips**, zero failures and zero errors. Native x64 settings, appearance, mode button, category, icon, geometry and 18 registration dry-run guards passed. The actual private Python 3.13 payload passed **16 checks**, including all 132 payload hashes, new spelling aliases, custom-entry reload, reverse lookup, prediction persistence and controller verification. Three real Windows pipe/token probes passed with native identification flags; these used the current desktop token, with expected-identity mismatch controls. See the [verification record](release-1.0.9-validation.json) for exact scope and skips.

Long-sentence ranking remains limited. The historical
[1.0.8 phrase review](LONG_PHRASE_RELEASE_REVIEW.md) and
[failed quality decision](long-phrase-release-decision.json) remain unchanged;
this release makes no new general accuracy claim. Experimental contextual
ranking and the unmerged horizontal/vertical layout option are not included.

Clean installation, live game clients and AppContainer user acceptance are not
verified for this release. Native macOS compilation, installation and typing
remain pending; no macOS binary is provided. Windows ARM64, 32-bit applications
and secure/password fields are outside this release's scope.
