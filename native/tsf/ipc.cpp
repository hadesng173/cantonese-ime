#include "ipc.h"

#include <aclapi.h>
#include <sddl.h>
#include <algorithm>
#include <limits>
#include <memory>
#include <stdexcept>

namespace cime {
namespace {
constexpr std::uint32_t kMagic = 0x31454d49;
constexpr std::uint32_t kLearningMagic = 0x314c4d49;
constexpr std::uint32_t kPredictionMagic = 0x31504d49;
constexpr std::uint32_t kSequenceMagic = 0x31534d49;
constexpr std::size_t kMaxPredictionText = 8;
constexpr std::size_t kMaxFrame = 1024 * 1024;
constexpr DWORD kTimeoutMs = 250;

struct Handle {
    HANDLE value = INVALID_HANDLE_VALUE;
    explicit Handle(HANDLE handle = INVALID_HANDLE_VALUE) : value(handle) {}
    ~Handle() { if (value && value != INVALID_HANDLE_VALUE) CloseHandle(value); }
    Handle(const Handle&) = delete;
    Handle& operator=(const Handle&) = delete;
};

std::wstring UserSid(HANDLE token) {
    DWORD length = 0;
    GetTokenInformation(token, TokenUser, nullptr, 0, &length);
    if (!length) return {};
    std::vector<BYTE> buffer(length);
    if (!GetTokenInformation(token, TokenUser, buffer.data(), length, &length)) return {};
    LPWSTR text = nullptr;
    if (!ConvertSidToStringSidW(reinterpret_cast<TOKEN_USER*>(buffer.data())->User.Sid, &text)) return {};
    std::wstring result(text);
    LocalFree(text);
    return result;
}

std::wstring CurrentSid() {
    HANDLE raw = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &raw)) return {};
    Handle token(raw);
    return UserSid(token.value);
}

bool CurrentProcessIsAppContainer() {
    HANDLE raw = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &raw)) return false;
    Handle token(raw);
    DWORD value = 0, length = 0;
    return GetTokenInformation(token.value, TokenIsAppContainer, &value, sizeof(value), &length) && value;
}

// An AppContainer token cannot open the broker process (its DACL has no
// app-package ACE). The pipe handle is already open with READ_CONTROL, so
// compare the pipe object's owner, which is the creating token's owner.
bool PipeOwnerIsCurrentUser(HANDLE pipe) {
    PSID owner = nullptr;
    PSECURITY_DESCRIPTOR descriptor = nullptr;
    if (GetSecurityInfo(pipe, SE_KERNEL_OBJECT, OWNER_SECURITY_INFORMATION, &owner, nullptr, nullptr, nullptr,
            &descriptor) != ERROR_SUCCESS) return false;
    std::wstring result;
    LPWSTR text = nullptr;
    if (owner && ConvertSidToStringSidW(owner, &text)) { result = text; LocalFree(text); }
    LocalFree(descriptor);
    return !result.empty() && result == CurrentSid();
}

bool ServerIsCurrentUser(HANDLE pipe) {
    ULONG pid = 0;
    if (!GetNamedPipeServerProcessId(pipe, &pid)) return false;
    Handle process(OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid));
    if (process.value == INVALID_HANDLE_VALUE || !process.value)
        return CurrentProcessIsAppContainer() && PipeOwnerIsCurrentUser(pipe);
    HANDLE raw = nullptr;
    if (!OpenProcessToken(process.value, TOKEN_QUERY, &raw)) return false;
    Handle token(raw);
    const auto server = UserSid(token.value);
    return !server.empty() && server == CurrentSid();
}

void Put(std::vector<BYTE>& bytes, std::uint32_t value) {
    for (unsigned shift = 0; shift < 32; shift += 8) bytes.push_back(static_cast<BYTE>(value >> shift));
}

std::uint32_t Get(const std::vector<BYTE>& bytes, std::size_t& position) {
    if (position + 4 > bytes.size()) throw std::runtime_error("short frame");
    std::uint32_t value = 0;
    for (unsigned shift = 0; shift < 32; shift += 8) value |= std::uint32_t(bytes[position++]) << shift;
    return value;
}

std::wstring GetString(const std::vector<BYTE>& bytes, std::size_t& position, std::size_t maximum_bytes = kMaxFrame) {
    const auto length = Get(bytes, position);
    if (length > maximum_bytes || position + length > bytes.size()) throw std::runtime_error("bad string length");
    if (!length) return {};
    const char* first = reinterpret_cast<const char*>(bytes.data() + position);
    const int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, first, static_cast<int>(length), nullptr, 0);
    if (!count) throw std::runtime_error("invalid UTF-8");
    std::wstring result(count, L'\0');
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, first, static_cast<int>(length), result.data(), count))
        throw std::runtime_error("invalid UTF-8");
    position += length;
    return result;
}

bool Transfer(HANDLE pipe, bool writing, BYTE* bytes, DWORD count, ULONGLONG deadline) {
    DWORD position = 0;
    while (position < count) {
        if (GetTickCount64() >= deadline) return false;
        Handle event(CreateEventW(nullptr, TRUE, FALSE, nullptr));
        if (!event.value) return false;
        OVERLAPPED operation{};
        operation.hEvent = event.value;
        DWORD transferred = 0;
        const BOOL immediate = writing
            ? WriteFile(pipe, bytes + position, count - position, &transferred, &operation)
            : ReadFile(pipe, bytes + position, count - position, &transferred, &operation);
        if (!immediate) {
            if (GetLastError() != ERROR_IO_PENDING) return false;
            const auto now = GetTickCount64();
            const DWORD remaining = now < deadline ? static_cast<DWORD>(deadline - now) : 0;
            if (WaitForSingleObject(event.value, remaining) != WAIT_OBJECT_0) {
                // OVERLAPPED must remain alive until cancellation completes.
                CancelIoEx(pipe, &operation);
                GetOverlappedResult(pipe, &operation, &transferred, TRUE);
                return false;
            }
            if (!GetOverlappedResult(pipe, &operation, &transferred, FALSE)) return false;
        }
        if (!transferred) return false;
        position += transferred;
    }
    return true;
}

bool Exchange(const std::wstring& name, std::vector<BYTE>& request, std::vector<BYTE>& response) {
    const auto deadline = GetTickCount64() + kTimeoutMs;
    if (name.empty()) return false;
    HANDLE raw_pipe = INVALID_HANDLE_VALUE;
    while (GetTickCount64() < deadline) {
        raw_pipe = CreateFileW(name.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr, OPEN_EXISTING,
            FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION, nullptr);
        if (raw_pipe != INVALID_HANDLE_VALUE || GetLastError() != ERROR_PIPE_BUSY) break;
        const auto now = GetTickCount64();
        if (now >= deadline || !WaitNamedPipeW(name.c_str(), static_cast<DWORD>(deadline - now))) break;
    }
    Handle pipe(raw_pipe);
    if (pipe.value == INVALID_HANDLE_VALUE || !ServerIsCurrentUser(pipe.value)) return false;
    if (!Transfer(pipe.value, true, request.data(), static_cast<DWORD>(request.size()), deadline)) return false;
    std::vector<BYTE> length_bytes(4);
    if (!Transfer(pipe.value, false, length_bytes.data(), 4, deadline)) return false;
    std::size_t position = 0;
    const auto length = Get(length_bytes, position);
    if (length < 28 || length > kMaxFrame) return false;
    response.resize(length);
    return Transfer(pipe.value, false, response.data(), length, deadline);
}

std::string Utf8(const std::wstring& value) {
    if (value.empty() || value.size() > 8192) throw std::runtime_error("invalid string length");
    const int size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
        static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
    if (!size) throw std::runtime_error("invalid UTF-16");
    std::string output(size, '\0');
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
        static_cast<int>(value.size()), output.data(), size, nullptr, nullptr))
        throw std::runtime_error("invalid UTF-16");
    return output;
}
}  // namespace

std::wstring Backend::PipeName() {
    const auto sid = CurrentSid();
    DWORD session = 0;
    if (sid.empty() || !ProcessIdToSessionId(GetCurrentProcessId(), &session)) return {};
    return L"\\\\.\\pipe\\CantoneseIME-" + sid + L"-" + std::to_wstring(session);
}

bool Backend::Lookup(const std::wstring& raw, bool strict, std::uint32_t offset, Page& output) {
    Page page;
    try {
        if (raw.empty() || raw.size() > kMaxInputLength || offset > 10000) throw std::runtime_error("invalid request");
        std::string input;
        for (wchar_t letter : raw) {
            if (!((letter >= L'a' && letter <= L'z') || letter == L'\'')) throw std::runtime_error("non-ASCII input");
            input.push_back(static_cast<char>(letter));
        }
        std::vector<BYTE> request;
        Put(request, static_cast<std::uint32_t>(24 + input.size()));
        Put(request, kMagic);
        const std::uint32_t sequence = ++sequence_;
        Put(request, sequence); Put(request, strict ? 1 : 0); Put(request, offset); Put(request, 9);
        Put(request, static_cast<std::uint32_t>(input.size()));
        request.insert(request.end(), input.begin(), input.end());
        std::vector<BYTE> response;
        if (!Exchange(PipeName(), request, response)) throw std::runtime_error("backend unavailable");
        std::size_t position = 0;
        if (Get(response, position) != kMagic || Get(response, position) != sequence) throw std::runtime_error("response mismatch");
        const auto status = Get(response, position);
        const auto next = Get(response, position);
        const auto truncated = Get(response, position);
        const auto count = Get(response, position);
        if (status > 3 || count > 9 || truncated > 1) throw std::runtime_error("invalid response fields");
        page.error = GetString(response, position, 8192);
        if (status != 0) {
            if (count != 0 || next != 0xffffffffu || position != response.size())
                throw std::runtime_error("invalid error response");
            page.completion_truncated = truncated != 0;
            if (page.error.empty()) page.error = L"Engine lookup failed. Enter keeps Latin text; Escape cancels.";
            output = std::move(page); return false;
        }
        if (next != 0xffffffffu && (next <= offset || next > 10000))
            throw std::runtime_error("invalid page");
        page.next_offset = next == 0xffffffffu ? -1 : static_cast<std::int32_t>(next);
        page.completion_truncated = truncated != 0;
        for (std::uint32_t i = 0; i < count; ++i) {
            Candidate item{GetString(response, position, 32768), GetString(response, position, 32768)};
            if (item.text.empty() || item.text.size() > 8192 || item.reading.size() > 8192) throw std::runtime_error("invalid candidate");
            page.candidates.push_back(std::move(item));
        }
        if (position != response.size() || (page.next_offset >= 0 && count == 0) ||
            (page.next_offset >= 0 && static_cast<std::uint32_t>(page.next_offset) != offset + count))
            throw std::runtime_error("invalid response tail");
        output = std::move(page);
        return true;
    } catch (...) {
        output = {};
        output.error = L"Engine unavailable. Enter keeps Latin text; Escape cancels.";
        return false;
    }
}

bool Backend::RecordSelection(const std::wstring& raw, bool strict, const Candidate& candidate) {
    try {
        if (raw.empty() || raw.size() > kMaxInputLength || candidate.text.empty() || candidate.text.size() > 512 ||
            (candidate.reading.empty() && candidate.text != raw) ||  // Reading-less only for an English word.
            candidate.reading.size() > kMaxLearningReadingBytes) return false;
        std::string input;
        for (wchar_t letter : raw) {
            if (!((letter >= L'a' && letter <= L'z') || letter == L'\'')) return false;
            input.push_back(static_cast<char>(letter));
        }
        const auto text = Utf8(candidate.text);
        const auto reading = Utf8(candidate.reading);
        std::vector<BYTE> request;
        Put(request, static_cast<std::uint32_t>(24 + input.size() + text.size() + reading.size()));
        Put(request, kLearningMagic);
        const auto sequence = ++sequence_;
        Put(request, sequence); Put(request, strict ? 1 : 0);
        Put(request, static_cast<std::uint32_t>(input.size()));
        Put(request, static_cast<std::uint32_t>(text.size()));
        Put(request, static_cast<std::uint32_t>(reading.size()));
        request.insert(request.end(), input.begin(), input.end());
        request.insert(request.end(), text.begin(), text.end());
        request.insert(request.end(), reading.begin(), reading.end());
        std::vector<BYTE> response;
        if (!Exchange(PipeName(), request, response)) return false;
        std::size_t position = 0;
        // A successful acknowledgement contains only the existing IME1 header.
        return response.size() == 28 && Get(response, position) == kMagic &&
            Get(response, position) == sequence && Get(response, position) == 0 &&
            Get(response, position) == 0xffffffffu && Get(response, position) == 0 &&
            Get(response, position) == 0 && Get(response, position) == 0;
    } catch (...) { return false; }
}

namespace {
// Prediction text: 1-8 UTF-16 units, no spaces or controls (the broker
// re-validates by codepoint).
bool PredictionText(const std::wstring& value) {
    if (value.empty() || value.size() > kMaxPredictionText) return false;
    for (wchar_t c : value) if (c <= L' ' || c == 0x3000) return false;
    return true;
}
// The last kMaxPredictionText units of a commit, without a split surrogate pair.
std::wstring PredictionTail(const std::wstring& value) {
    std::wstring tail = value.size() > kMaxPredictionText ? value.substr(value.size() - kMaxPredictionText) : value;
    if (!tail.empty() && IS_LOW_SURROGATE(tail.front())) tail.erase(0, 1);
    return tail;
}
bool Acknowledged(const std::vector<BYTE>& response, std::uint32_t sequence) {
    std::size_t position = 0;
    return response.size() == 28 && Get(response, position) == kMagic &&
        Get(response, position) == sequence && Get(response, position) == 0 &&
        Get(response, position) == 0xffffffffu && Get(response, position) == 0 &&
        Get(response, position) == 0 && Get(response, position) == 0;
}
}  // namespace

bool Backend::Predict(const std::wstring& context, Page& output) {
    output = {};
    try {
        const std::wstring tail = PredictionTail(context);
        if (!PredictionText(tail)) return false;
        const auto text = Utf8(tail);
        std::vector<BYTE> request;
        Put(request, static_cast<std::uint32_t>(12 + text.size()));
        Put(request, kPredictionMagic);
        const auto sequence = ++sequence_;
        Put(request, sequence); Put(request, static_cast<std::uint32_t>(text.size()));
        request.insert(request.end(), text.begin(), text.end());
        std::vector<BYTE> response;
        if (!Exchange(PipeName(), request, response)) return false;
        std::size_t position = 0;
        if (Get(response, position) != kMagic || Get(response, position) != sequence) return false;
        const auto status = Get(response, position), next = Get(response, position);
        const auto truncated = Get(response, position), count = Get(response, position);
        const auto error = GetString(response, position, 8192);
        if (status != 0 || next != 0xffffffffu || truncated != 0 || count > 9 || !error.empty()) return false;
        Page page;
        for (std::uint32_t i = 0; i < count; ++i) {
            Candidate item{GetString(response, position, 64), GetString(response, position, 64)};
            if (!PredictionText(item.text) || !item.reading.empty()) return false;
            page.candidates.push_back(std::move(item));
        }
        if (position != response.size()) return false;
        output = std::move(page);
        return true;
    } catch (...) { output = {}; return false; }
}

bool Backend::RecordSequence(const std::wstring& previous, const std::wstring& following) {
    try {
        const std::wstring tail = PredictionTail(previous);
        if (!PredictionText(tail) || !PredictionText(following)) return false;
        const auto before = Utf8(tail), after = Utf8(following);
        std::vector<BYTE> request;
        Put(request, static_cast<std::uint32_t>(16 + before.size() + after.size()));
        Put(request, kSequenceMagic);
        const auto sequence = ++sequence_;
        Put(request, sequence);
        Put(request, static_cast<std::uint32_t>(before.size())); Put(request, static_cast<std::uint32_t>(after.size()));
        request.insert(request.end(), before.begin(), before.end());
        request.insert(request.end(), after.begin(), after.end());
        std::vector<BYTE> response;
        return Exchange(PipeName(), request, response) && Acknowledged(response, sequence);
    } catch (...) { return false; }
}

}  // namespace cime
