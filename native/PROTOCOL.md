# Native lookup and selection-memory protocol

The x64 TSF DLL owns Windows composition, keyboard handling and candidate presentation. The project-local Python broker runs the existing dictionary engine. The DLL exchanges lookup messages with that broker; the broker does not insert text into applications or register an IME. A working broker alone does not prove native IME operation.

The wire implementation is shared by `cantonese_ime/native_protocol.py` and `native/tsf/ipc.cpp`. Integers are little-endian. Strings are strictly decoded UTF-8; lengths count bytes, not characters. Every message begins with a `u32` body length, from 1 through 1,048,576 inclusive. Lookup bodies and all replies use magic `0x31454D49` (bytes `IME1`). Explicit selection-memory requests use `0x314C4D49` (bytes `IML1`). Existing lookup messages are unchanged. There is no JSON, terminating NUL, alignment padding or implicit string encoding on this wire.

## Request body

| Field | Type | Allowed value |
| --- | --- | --- |
| magic | u32 | `0x31454D49` |
| request ID | u32 | Echoed unchanged in the response |
| mode | u32 | 0 forgiving; 1 strict Jyutping |
| offset | u32 | 0 through the engine's `MAX_OFFSET`, currently 10,000 |
| page size | u32 | 1 through 9 |
| input byte length | u32 | Must match the remaining body exactly |
| input | bytes | At most 256 ASCII letters, digits 1–6, apostrophes or spaces |

Empty input is permitted. Digits on this wire can express tones; the native frontend's current digit keys select candidates. A request never changes learning data.

## Explicit selection-memory request

The `IML1` body has six little-endian `u32` fields: magic, request ID, mode, raw-input byte length, selected-text byte length, canonical-reading byte length. They are followed by those three strings in that order, without additional length prefixes. Raw input is nonempty and uses the lookup grammar, up to 256 ASCII bytes. Selected text is 1–256 Unicode codepoints, up to 1,024 UTF-8 bytes. Reading is 1–256 single-space-separated canonical tokens matching `[a-z]{1,12}[1-6]`, up to 3,583 ASCII bytes. The one exception is an English word: the reading may be empty when the selected text equals the raw input and is 2–24 lowercase letters; the broker then remembers the word instead of a selection. All three lengths must consume the body exactly. These bounds extend the earlier 64-byte input/64-token reading limit without changing the wire layout.

The reply is an ordinary `IME1` response with the same request ID, no candidates, next offset -1 and truncated 0. Status 0 acknowledges a completed persistent save. Malformed messages are status 1. Refused candidates or a corrupt memory discovered during save return status 1 with a generic message; disabled/unwritable memory returns status 3; bounded search capacity can return status 2. No exception text, filesystem path, raw input or selected text is echoed in a learning failure.

`Engine.record_selection` validates the exact text/reading against its current bounded candidate snapshot for the full input and mode under the engine lock before any write. Arbitrary supplied candidate strings are rejected. Previously selected personal phrases for that same full input/mode may also be valid candidates. The local memory is user-owned data, not cryptographically authenticated linguistic evidence. This protocol cannot prove that a same-user client actually committed text; the native frontend must send the request only after successful selected-candidate `EndComposition`. Lookups, literal Enter, cancellation, failed commits and focus-departure completion do not notify learning. A learning notification is best effort and must not roll back a successful document commit. It is not automatically retried because duplicate notifications would count as additional selections.

## Learned suggestion requests

Two further request operations serve optional next-word suggestions learned only from the user's own typing. The native frontend sends them only while `prediction=on` is set in `appearance.ini`.

* `IMP1` (`0x31504D49`) asks for suggestions: three `u32` fields (magic, request ID, context byte length) followed by the UTF-8 context. The context is the last committed text, 1–8 codepoints without whitespace or control characters. The reply is an ordinary `IME1` response with up to 9 candidates whose readings are empty, next offset -1 and truncated 0. Without prediction memory the reply has zero candidates.
* `IMS1` (`0x31534D49`) records one consecutive commit: four `u32` fields (magic, request ID, previous byte length, following byte length) followed by the two UTF-8 strings, each 1–8 codepoints with the same restrictions. The reply is an `IME1` acknowledgement: status 0 when counted, 1 for malformed or refused text, 3 when prediction memory is unavailable.

The broker keeps these counts in `%LOCALAPPDATA%\CantoneseIME\prediction.json` (passed as `--prediction-file`), separate from selection memory. It also derives "first character → rest" pairs from learned multi-character selections at lower weight; it never uses dictionary frequencies. Deleting the file clears the counts, and the broker drops its in-memory copy when it notices. Neither request echoes text in errors or logs.

## Response body

| Field | Type | Allowed value |
| --- | --- | --- |
| magic | u32 | `0x31454D49` |
| request ID | u32 | Must match the pending request |
| status | u32 | 0 success; 1 invalid request; 2 engine capacity; 3 internal error |
| next offset | i32 | -1 means no next page; otherwise a valid engine offset |
| completion truncated | u32 | 0 or 1 |
| candidate count | u32 | 0 through 9 |
| error byte length | u32 | 0 through 8,192 |
| error | bytes | UTF-8 diagnostic |
| candidates | repeated | Exactly `candidate count` records |

Each candidate consists of `u32 textByteLength`, UTF-8 text, `u32 readingByteLength`, UTF-8 reading. An error response has zero candidates and next offset -1. Trailing data, truncated fields, invalid UTF-8, unknown statuses and mismatched request IDs are rejected. Internal exceptions return a generic diagnostic without Python tracebacks or local paths.

## Transport and failure behavior

The named-pipe endpoint is `\\.\pipe\CantoneseIME-<Windows user SID>-<Windows session ID>`. The broker grants full access to the current user and SYSTEM and read/write to app packages (`AC`, `S-1-15-2-2`) with a low mandatory label, so AppContainer hosts of the same user can connect; it has no Everyone/anonymous ACE, rejects remote clients and requests exclusive ownership when creating its first pipe instance. The native client checks the server process's user SID against its own token. Inside an AppContainer, where the broker process cannot be opened, it instead compares the pipe object's owner SID with its own user SID. This is a local same-user boundary; it does not protect against another process already controlled by that same user.

The native client uses a bounded lookup deadline and discards failed or stale responses. Failure must not commit a guessed result. Initial failure can leave a Latin key available to the host; an existing composition must remain recoverable through its documented literal-commit or cancel controls. Actual transport timing, focus behavior and recovery still require Windows testing.

The broker has no network dependency. An explicit `--learning-file` enables local selection memory; the runtime controller supplies a per-user path by default. A corrupt startup file is preserved and the broker opens a conversion-only engine. Save failures return a generic acknowledgement error without breaking ordinary lookup. Operational logs contain readiness and connection counts, without typed input, candidate text or document contents. The personal learning file itself necessarily stores selected input/text/reading. The separate diagnostic test host deliberately records its test document to establish composition and commit evidence.

## Verification boundary

Pure Python protocol tests can establish byte layout, validation and engine dispatch. Compilation, named-pipe interoperability, x64 DLL loading, real TSF composition, candidate selection and commit/cancel behavior are separate checks in `docs/NATIVE_X64_VALIDATION.md`. They remain unverified until observed on Windows.
