"""Native IME lookup and explicit selection-memory messages; no Windows dependency.

Little-endian length-framed UTF-8, shared with native/tsf. The transport binds
the endpoint to one Windows user/session; this module only validates messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import re
import struct

from .engine import Candidate, CompositionCapacityError, Engine, PaginationCapacityError
from .english import WORD as ENGLISH_WORD


MAGIC = 0x31454D49  # IME1
LEARNING_MAGIC = 0x314C4D49  # IML1; separate request operation, IME1 acknowledgement.
PREDICTION_MAGIC = 0x31504D49  # IMP1; next-text suggestions, IME1 reply with reading-less candidates.
SEQUENCE_MAGIC = 0x31534D49  # IMS1; one consecutive-commit record, IME1 acknowledgement.
PREDICTION_HEADER = struct.Struct("<3I")
SEQUENCE_HEADER = struct.Struct("<4I")
MAX_PREDICTION_TEXT = 8
MAX_FRAME_BYTES = 1_048_576
MAX_NATIVE_PAGE = 9
MAX_ERROR_BYTES = 8192
MAX_CANDIDATE_UTF16_UNITS = 8192
MAX_LEARNING_SYLLABLES = Engine.MAX_INPUT_LENGTH
MAX_LEARNING_READING_BYTES = 14 * MAX_LEARNING_SYLLABLES - 1
REQUEST_HEADER = struct.Struct("<6I")
LEARNING_HEADER = struct.Struct("<6I")
RESPONSE_HEADER = struct.Struct("<IIIiIII")
U32 = struct.Struct("<I")
MODES = ("forgiving", "jyutping")
RAW = re.compile(r"[A-Za-z1-6' ]{0," + str(Engine.MAX_INPUT_LENGTH) + r"}\Z")
LEARNING_READING = re.compile(r"[a-z]{1,12}[1-6](?: [a-z]{1,12}[1-6]){0," + str(MAX_LEARNING_SYLLABLES - 1) + r"}\Z")


class ProtocolError(ValueError):
    """The message is malformed or outside the native protocol bounds."""


class Status(IntEnum):
    OK = 0
    INVALID = 1
    CAPACITY = 2
    INTERNAL = 3


@dataclass(frozen=True)
class LookupRequest:
    request_id: int
    mode: int
    offset: int
    limit: int
    raw: str


@dataclass(frozen=True)
class LearningRequest:
    request_id: int
    mode: int
    raw: str
    text: str
    reading: str


@dataclass(frozen=True)
class PredictionRequest:
    request_id: int
    context: str


@dataclass(frozen=True)
class SequenceRequest:
    request_id: int
    previous: str
    following: str


@dataclass(frozen=True)
class WireCandidate:
    text: str
    reading: str


@dataclass(frozen=True)
class LookupResponse:
    request_id: int
    status: Status
    next_offset: int | None = None
    completion_truncated: bool = False
    candidates: tuple[WireCandidate, ...] = ()
    error: str = ""


def _integer(value: int, minimum: int, maximum: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ProtocolError(f"Invalid {label}.")


def validate_request(request: LookupRequest) -> None:
    if not isinstance(request, LookupRequest):
        raise ProtocolError("Invalid lookup request.")
    _integer(request.request_id, 0, 0xFFFFFFFF, "request id")
    _integer(request.mode, 0, 1, "mode")
    _integer(request.offset, 0, Engine.MAX_OFFSET, "offset")
    _integer(request.limit, 1, MAX_NATIVE_PAGE, "page size")
    if not isinstance(request.raw, str) or not RAW.fullmatch(request.raw):
        raise ProtocolError(f"Input must contain at most {Engine.MAX_INPUT_LENGTH} ASCII phonetic characters.")


def _payload_bytes(payload: bytes) -> bytes:
    if not isinstance(payload, (bytes, bytearray)):
        raise ProtocolError("Payload must be bytes.")
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError("Payload exceeds the frame limit.")
    return bytes(payload)


def encode_request(request: LookupRequest) -> bytes:
    validate_request(request)
    raw = request.raw.encode("utf-8")
    return REQUEST_HEADER.pack(MAGIC, request.request_id, request.mode,
                               request.offset, request.limit, len(raw)) + raw


def decode_request(payload: bytes) -> LookupRequest:
    payload = _payload_bytes(payload)
    if not REQUEST_HEADER.size <= len(payload) <= MAX_FRAME_BYTES:
        raise ProtocolError("Invalid request length.")
    magic, request_id, mode, offset, limit, raw_length = REQUEST_HEADER.unpack_from(payload)
    if magic != MAGIC or len(payload) != REQUEST_HEADER.size + raw_length:
        raise ProtocolError("Invalid request header or payload length.")
    try:
        raw = payload[REQUEST_HEADER.size:].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProtocolError("Input is not UTF-8.") from error
    request = LookupRequest(request_id, mode, offset, limit, raw)
    validate_request(request)
    return request


def validate_learning_request(request: LearningRequest) -> None:
    if not isinstance(request, LearningRequest):
        raise ProtocolError("Invalid learning request.")
    validate_request(LookupRequest(request.request_id, request.mode, 0, 1, request.raw))
    if not request.raw.strip(" "):
        raise ProtocolError("Learning requires nonempty input.")
    if not isinstance(request.text, str) or not 1 <= len(request.text) <= 256:
        raise ProtocolError("Invalid selected text length.")
    _text_bytes(request.text)
    if request.reading == "" and request.text == request.raw and ENGLISH_WORD.fullmatch(request.raw):
        return  # The typed letters committed as an English word.
    if not isinstance(request.reading, str) or not LEARNING_READING.fullmatch(request.reading):
        raise ProtocolError("Learning requires a canonical toned reading.")


def encode_learning_request(request: LearningRequest) -> bytes:
    validate_learning_request(request)
    raw = request.raw.encode("ascii")
    text = request.text.encode("utf-8")
    reading = request.reading.encode("ascii")
    return LEARNING_HEADER.pack(LEARNING_MAGIC, request.request_id, request.mode,
                                len(raw), len(text), len(reading)) + raw + text + reading


def decode_learning_request(payload: bytes) -> LearningRequest:
    payload = _payload_bytes(payload)
    if len(payload) < LEARNING_HEADER.size:
        raise ProtocolError("Invalid learning request length.")
    magic, request_id, mode, raw_length, text_length, reading_length = LEARNING_HEADER.unpack_from(payload)
    if (magic != LEARNING_MAGIC or raw_length > Engine.MAX_INPUT_LENGTH or text_length > 1024 or reading_length > MAX_LEARNING_READING_BYTES
            or len(payload) != LEARNING_HEADER.size + raw_length + text_length + reading_length):
        raise ProtocolError("Invalid learning header or payload length.")
    position = LEARNING_HEADER.size
    try:
        raw = payload[position:position + raw_length].decode("ascii")
        position += raw_length
        text = payload[position:position + text_length].decode("utf-8")
        position += text_length
        reading = payload[position:].decode("ascii")
    except UnicodeDecodeError as error:
        raise ProtocolError("Invalid learning text encoding.") from error
    request = LearningRequest(request_id, mode, raw, text, reading)
    validate_learning_request(request)
    return request


def _prediction_text(value: object, label: str) -> bytes:
    if (not isinstance(value, str) or not 1 <= len(value) <= MAX_PREDICTION_TEXT
            or any(c.isspace() or ord(c) < 32 for c in value)):
        raise ProtocolError(f"Invalid {label} text.")
    return _text_bytes(value)


def encode_prediction_request(request: PredictionRequest) -> bytes:
    _integer(request.request_id, 0, 0xFFFFFFFF, "request id")
    context = _prediction_text(request.context, "prediction context")
    return PREDICTION_HEADER.pack(PREDICTION_MAGIC, request.request_id, len(context)) + context


def encode_sequence_request(request: SequenceRequest) -> bytes:
    _integer(request.request_id, 0, 0xFFFFFFFF, "request id")
    previous = _prediction_text(request.previous, "previous")
    following = _prediction_text(request.following, "following")
    return SEQUENCE_HEADER.pack(SEQUENCE_MAGIC, request.request_id, len(previous), len(following)) + previous + following


def _decode_texts(payload: bytes, position: int, lengths: tuple[int, ...]) -> list[str]:
    if any(length > 4 * MAX_PREDICTION_TEXT for length in lengths) or len(payload) != position + sum(lengths):
        raise ProtocolError("Invalid prediction payload length.")
    texts = []
    for length in lengths:
        try:
            texts.append(payload[position:position + length].decode("utf-8"))
        except UnicodeDecodeError as error:
            raise ProtocolError("Prediction text is not UTF-8.") from error
        position += length
    return texts


def decode_prediction_request(payload: bytes) -> PredictionRequest:
    payload = _payload_bytes(payload)
    if len(payload) < PREDICTION_HEADER.size:
        raise ProtocolError("Invalid prediction request length.")
    magic, request_id, length = PREDICTION_HEADER.unpack_from(payload)
    if magic != PREDICTION_MAGIC:
        raise ProtocolError("Invalid prediction header.")
    (context,) = _decode_texts(payload, PREDICTION_HEADER.size, (length,))
    _prediction_text(context, "prediction context")
    return PredictionRequest(request_id, context)


def decode_sequence_request(payload: bytes) -> SequenceRequest:
    payload = _payload_bytes(payload)
    if len(payload) < SEQUENCE_HEADER.size:
        raise ProtocolError("Invalid sequence request length.")
    magic, request_id, previous_length, following_length = SEQUENCE_HEADER.unpack_from(payload)
    if magic != SEQUENCE_MAGIC:
        raise ProtocolError("Invalid sequence header.")
    previous, following = _decode_texts(payload, SEQUENCE_HEADER.size, (previous_length, following_length))
    _prediction_text(previous, "previous")
    _prediction_text(following, "following")
    return SequenceRequest(request_id, previous, following)


def frame(payload: bytes) -> bytes:
    payload = _payload_bytes(payload)
    if not 1 <= len(payload) <= MAX_FRAME_BYTES:
        raise ProtocolError("Frame is empty or exceeds the size limit.")
    return U32.pack(len(payload)) + payload


def decode_frame_length(header: bytes) -> int:
    header = _payload_bytes(header)
    if len(header) != U32.size:
        raise ProtocolError("Invalid frame length header.")
    length = U32.unpack(header)[0]
    if not 1 <= length <= MAX_FRAME_BYTES:
        raise ProtocolError("Frame is empty or exceeds the size limit.")
    return length


def _text_bytes(value: str) -> bytes:
    if not isinstance(value, str):
        raise ProtocolError("Invalid text field.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ProtocolError("Invalid text field.") from error
    return encoded


def _field(value: str, *, allow_empty: bool) -> bytes:
    encoded = _text_bytes(value)
    # Match the native consumer's UTF-16 length bound, including supplementary
    # ideographs, which occupy two Windows wchar_t units.
    if (not allow_empty and not value) or len(value.encode("utf-16-le")) // 2 > MAX_CANDIDATE_UTF16_UNITS:
        raise ProtocolError("Invalid candidate text length.")
    return U32.pack(len(encoded)) + encoded


def encode_response(response: LookupResponse) -> bytes:
    if not isinstance(response, LookupResponse):
        raise ProtocolError("Invalid lookup response.")
    _integer(response.request_id, 0, 0xFFFFFFFF, "request id")
    _integer(response.status, Status.OK, Status.INTERNAL, "response status")
    status = Status(response.status)
    if response.next_offset is not None:
        _integer(response.next_offset, 0, Engine.MAX_OFFSET, "next offset")
    if (not isinstance(response.completion_truncated, bool)
            or not isinstance(response.candidates, (tuple, list))
            or len(response.candidates) > MAX_NATIVE_PAGE):
        raise ProtocolError("Invalid native page.")
    if status != Status.OK and (response.candidates or response.next_offset is not None or response.completion_truncated):
        raise ProtocolError("An error response must not publish candidates.")
    if response.next_offset is not None and not response.candidates:
        raise ProtocolError("An empty page must not advertise a next offset.")
    error_bytes = _text_bytes(response.error)
    if len(error_bytes) > MAX_ERROR_BYTES:
        raise ProtocolError("Error message exceeds its limit.")
    payload = RESPONSE_HEADER.pack(
        MAGIC, response.request_id, status,
        response.next_offset if response.next_offset is not None else -1,
        int(response.completion_truncated), len(response.candidates), len(error_bytes),
    ) + error_bytes
    for candidate in response.candidates:
        if not isinstance(candidate, WireCandidate):
            raise ProtocolError("Invalid native candidate.")
        payload += _field(candidate.text, allow_empty=False) + _field(candidate.reading, allow_empty=True)
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError("Response exceeds the frame limit.")
    return payload


def decode_response(payload: bytes, *, expected_request_id: int | None = None) -> LookupResponse:
    payload = _payload_bytes(payload)
    if not RESPONSE_HEADER.size <= len(payload) <= MAX_FRAME_BYTES:
        raise ProtocolError("Invalid response length.")
    magic, request_id, status_number, next_offset, truncated, count, error_length = RESPONSE_HEADER.unpack_from(payload)
    if magic != MAGIC or truncated not in (0, 1) or count > MAX_NATIVE_PAGE:
        raise ProtocolError("Invalid response header.")
    if expected_request_id is not None:
        _integer(expected_request_id, 0, 0xFFFFFFFF, "expected request id")
        if request_id != expected_request_id:
            raise ProtocolError("Stale or mismatched response id.")
    if next_offset != -1:
        _integer(next_offset, 0, Engine.MAX_OFFSET, "next offset")
    try:
        status = Status(status_number)
    except ValueError as error:
        raise ProtocolError("Unknown response status.") from error
    position = RESPONSE_HEADER.size

    def read_text(length: int) -> str:
        nonlocal position
        if length > len(payload) - position:
            raise ProtocolError("Truncated text field.")
        encoded = payload[position:position + length]
        position += length
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ProtocolError("Response text is not UTF-8.") from error

    if error_length > MAX_ERROR_BYTES:
        raise ProtocolError("Error message exceeds its limit.")
    error = read_text(error_length)
    candidates = []
    for _ in range(count):
        fields = []
        for _ in range(2):
            if len(payload) - position < U32.size:
                raise ProtocolError("Missing text length.")
            length = U32.unpack_from(payload, position)[0]
            position += U32.size
            fields.append(read_text(length))
        candidates.append(WireCandidate(*fields))
    if position != len(payload):
        raise ProtocolError("Trailing response bytes.")
    response = LookupResponse(request_id, status, None if next_offset == -1 else next_offset,
                              bool(truncated), tuple(candidates), error)
    # Apply the same field and page invariants to incoming and outgoing data.
    encode_response(response)
    return response


def _failure(request_id: int, status: Status, error: Exception) -> LookupResponse:
    try:
        message = str(error).encode("utf-8", errors="replace")[:MAX_ERROR_BYTES].decode("utf-8", errors="ignore")
    except Exception:
        message = "Conversion request could not finish."
    return LookupResponse(request_id, status, error=message)


def _internal_failure(request_id: int) -> LookupResponse:
    # Do not expose Python tracebacks, local paths, or arbitrary backend data.
    return LookupResponse(request_id, Status.INTERNAL, error="Conversion backend could not finish the request.")


def _learned_selections(engine: Engine) -> dict:
    """A consistent copy of learned selections, taken under the engine lock."""
    selections = getattr(engine, "_selections", None)
    lock = getattr(engine, "_lock", None)
    if not isinstance(selections, dict):
        return {}
    if lock is None:
        return dict(selections)
    with lock:
        return dict(engine._selections)


def _dispatch_prediction(engine: Engine, payload: bytes, predictions, magic: int) -> LookupResponse:
    if magic == PREDICTION_MAGIC:
        request = decode_prediction_request(payload)
        if predictions is None:
            return LookupResponse(request.request_id, Status.OK)
        try:
            texts = predictions.suggest(request.context, limit=MAX_NATIVE_PAGE, selections=_learned_selections(engine))
            predictions.save()
        except Exception:
            return LookupResponse(request.request_id, Status.INTERNAL, error="Prediction memory could not answer.")
        return LookupResponse(request.request_id, Status.OK,
                              candidates=tuple(WireCandidate(text, "") for text in texts[:MAX_NATIVE_PAGE]))
    request = decode_sequence_request(payload)
    if predictions is None:
        return LookupResponse(request.request_id, Status.INTERNAL, error="Prediction memory is unavailable.")
    try:
        predictions.record(request.previous, request.following)
    except ValueError:
        return LookupResponse(request.request_id, Status.INVALID, error="Prediction memory did not accept this record.")
    except Exception:
        return LookupResponse(request.request_id, Status.INTERNAL, error="Prediction memory could not save this record.")
    return LookupResponse(request.request_id, Status.OK)


def dispatch(engine: Engine, payload: bytes, predictions=None) -> LookupResponse:
    """Translate a lookup, an engine-validated selection, or a prediction request.

    Lookups never write. Engine.record_selection validates text+reading against
    its bounded current input/mode candidate snapshot while holding its lock;
    a supplied string alone cannot create an arbitrary learned candidate.
    Prediction requests read or count user sequences in the separate store;
    without one, suggestions are empty and records are refused.
    """
    request_id = 0
    try:
        payload = _payload_bytes(payload)
        request_id = U32.unpack_from(payload, 4)[0] if len(payload) >= 8 else 0
        magic = U32.unpack_from(payload)[0] if len(payload) >= 4 else 0
        if magic in (PREDICTION_MAGIC, SEQUENCE_MAGIC):
            return _dispatch_prediction(engine, payload, predictions, magic)
        if magic == LEARNING_MAGIC:
            request = decode_learning_request(payload)
            if getattr(engine, "learning_path", None) is None:
                return LookupResponse(request_id, Status.INTERNAL, error="Local selection memory is unavailable.")
            try:
                engine.record_selection(request.raw, Candidate(request.text, request.reading, 0.0,
                                                               "native-selection", len(request.raw)),
                                        mode=MODES[request.mode])
            except (CompositionCapacityError, PaginationCapacityError):
                return LookupResponse(request_id, Status.CAPACITY, error="Selection validation exceeded the bounded search capacity.")
            except ValueError:
                # Includes refused candidates or a changed/corrupt memory file.
                # Never echo filesystem paths, inputs or arbitrary exception text.
                return LookupResponse(request_id, Status.INVALID, error="Local selection memory did not accept this selection.")
            except Exception:
                return LookupResponse(request_id, Status.INTERNAL, error="Local selection memory could not save this selection.")
            return LookupResponse(request_id, Status.OK)
        request = decode_request(payload)
    except ProtocolError as error:
        return _failure(request_id, Status.INVALID, error)
    try:
        page = engine.lookup_page(request.raw, offset=request.offset,
                                  page_size=request.limit, mode=MODES[request.mode])
        response = LookupResponse(
            request.request_id, Status.OK, page.next_offset, page.completion_truncated,
            tuple(WireCandidate(candidate.text, candidate.reading) for candidate in page.candidates),
        )
    except (CompositionCapacityError, PaginationCapacityError) as error:
        return _failure(request_id, Status.CAPACITY, error)
    except ValueError as error:
        return _failure(request_id, Status.INVALID, error)
    except Exception:
        return _internal_failure(request_id)
    try:
        # A broken backend page must become a complete error response rather
        # than fail later while the server tries to encode its reply.
        encode_response(response)
        if len(response.candidates) > request.limit or (
            response.next_offset is not None
            and response.next_offset != request.offset + len(response.candidates)
        ):
            raise ProtocolError("Backend returned an inconsistent page.")
    except ProtocolError:
        return _internal_failure(request_id)
    return response
