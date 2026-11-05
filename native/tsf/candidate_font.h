#pragma once
// Candidate-popup GDI fonts only. Bundled fonts are private memory resources;
// no system font registration or application DPI-awareness change is made.
#include <windows.h>
#include <string>
#include <vector>
#include "candidate_layout.h"

namespace cime::ui {
constexpr wchar_t kBundledCandidateFontFamily[] = L"LXGW WenKai TC";
constexpr int kCandidateFontLogicalHeight = 20;

inline bool EqualCandidatePath(const std::wstring& left, const std::wstring& right) {
    return CompareStringOrdinal(left.c_str(), -1, right.c_str(), -1, TRUE) == CSTR_EQUAL;
}

// The product DLL path that anchors bundled files. A release registers a copy
// in System32 (games such as World of Warcraft load text services only from
// there); the product files, with the identical DLL, stay in 64-bit Program Files.
inline std::wstring ProductModulePath(const std::wstring& module_path) {
    const std::wstring suffix = L"\\native\\CantoneseIME.dll";
    if (module_path.size() > suffix.size() &&
        EqualCandidatePath(module_path.substr(module_path.size() - suffix.size()), suffix)) return module_path;
    wchar_t expanded[MAX_PATH]{};
    const DWORD length = ExpandEnvironmentStringsW(L"%ProgramW6432%\\CantoneseIME\\native\\CantoneseIME.dll", expanded, MAX_PATH);
    if (!length || length > MAX_PATH || expanded[0] == L'%') return module_path;
    return expanded;
}

inline bool CandidateFontPaths(const std::wstring& module_path, std::wstring& root, std::wstring& font_path) {
    // The release is a local AMD64 desktop DLL. Do not resolve through the CWD,
    // PATH, a network path or a caller-controlled relative font search path.
    if (module_path.size() < 4 || module_path[1] != L':' || module_path[2] != L'\\' ||
        module_path.find(L'/') != std::wstring::npos) return false;
    const std::wstring suffix = L"\\native\\CantoneseIME.dll";
    if (module_path.size() <= suffix.size() ||
        !EqualCandidatePath(module_path.substr(module_path.size() - suffix.size()), suffix)) return false;
    root = module_path.substr(0, module_path.size() - suffix.size());
    if (root.find(L"\\..") != std::wstring::npos || root.find(L"\\.\\") != std::wstring::npos) return false;
    font_path = root + L"\\assets\\fonts\\lxgw-wenkai-tc\\LXGWWenKaiTC-Regular.ttf";
    return true;
}

class CandidateFontFile {
public:
    CandidateFontFile(const std::wstring& path, bool directory, bool read_data = false) {
        handle_ = CreateFileW(path.c_str(), read_data ? GENERIC_READ : FILE_READ_ATTRIBUTES,
            FILE_SHARE_READ, nullptr, OPEN_EXISTING,
            FILE_FLAG_OPEN_REPARSE_POINT | (directory ? FILE_FLAG_BACKUP_SEMANTICS : 0), nullptr);
        if (handle_ == INVALID_HANDLE_VALUE) return;
        BY_HANDLE_FILE_INFORMATION info{};
        if (GetFileType(handle_) != FILE_TYPE_DISK || !GetFileInformationByHandle(handle_, &info) ||
            (info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) ||
            ((info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0) != directory) { Close(); return; }
        try {
            const DWORD size = GetFinalPathNameByHandleW(handle_, nullptr, 0, FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
            if (!size || size > 32768) { Close(); return; }
            std::vector<wchar_t> buffer(size + 1);
            const DWORD copied = GetFinalPathNameByHandleW(handle_, buffer.data(), static_cast<DWORD>(buffer.size()),
                FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
            if (!copied || copied >= buffer.size() ||
                !EqualCandidatePath(std::wstring(buffer.data(), copied), L"\\\\?\\" + path)) Close();
        } catch (...) { Close(); } // A constructor allocation failure must not leak its file handle.
    }
    ~CandidateFontFile() { Close(); }
    CandidateFontFile(const CandidateFontFile&) = delete;
    CandidateFontFile& operator=(const CandidateFontFile&) = delete;
    explicit operator bool() const { return handle_ != INVALID_HANDLE_VALUE; }
    HANDLE Get() const { return handle_; }
private:
    void Close() { if (handle_ != INVALID_HANDLE_VALUE) CloseHandle(handle_); handle_ = INVALID_HANDLE_VALUE; }
    HANDLE handle_ = INVALID_HANDLE_VALUE;
};

inline std::vector<BYTE> ReadBundledCandidateFont(const std::wstring& module_path) {
    std::wstring root, path;
    if (!CandidateFontPaths(module_path, root, path)) return {};
    // The registered release DLL lives below the protected install root. Hold
    // each exact directory while reading, refusing junctions/symlinks and any
    // final path that differs from that module-anchored location.
    CandidateFontFile root_directory(root, true), native(root + L"\\native", true), module(module_path, false);
    CandidateFontFile assets(root + L"\\assets", true), fonts(root + L"\\assets\\fonts", true);
    CandidateFontFile family(root + L"\\assets\\fonts\\lxgw-wenkai-tc", true), file(path, false, true);
    if (!root_directory || !native || !module || !assets || !fonts || !family || !file) return {};
    LARGE_INTEGER size{};
    constexpr LONGLONG maximum_bytes = 64LL * 1024 * 1024;
    if (!GetFileSizeEx(file.Get(), &size) || size.QuadPart < 12 || size.QuadPart > maximum_bytes) return {};
    std::vector<BYTE> bytes(static_cast<std::size_t>(size.QuadPart));
    DWORD read = 0;
    if (!ReadFile(file.Get(), bytes.data(), static_cast<DWORD>(bytes.size()), &read, nullptr) || read != bytes.size()) return {};
    // The bundled asset is an ordinary TrueType sfnt, not a collection or a
    // device font. GDI performs the remaining font-format validation.
    if (bytes[0] || bytes[1] != 1 || bytes[2] || bytes[3]) return {};
    return bytes; // All path/file handles close before GDI sees the memory image.
}

class PrivateCandidateFont {
public:
    PrivateCandidateFont() = default;
    ~PrivateCandidateFont() { Reset(); }
    PrivateCandidateFont(const PrivateCandidateFont&) = delete;
    PrivateCandidateFont& operator=(const PrivateCandidateFont&) = delete;
    bool EnsureForModule(HMODULE module) {
        if (attempted_) return resource_ != nullptr;
        if (!module) { attempted_ = true; return false; }
        try {
            std::vector<wchar_t> path(32768);
            const DWORD copied = GetModuleFileNameW(module, path.data(), static_cast<DWORD>(path.size()));
            if (!copied || copied >= path.size()) { attempted_ = true; return false; }
            return EnsureForModulePath(ProductModulePath(std::wstring(path.data(), copied)));
        } catch (...) { attempted_ = true; return false; }
    }
    // The production caller passes GetModuleFileNameW for the actual TSF DLL.
    // A separate entry point permits native fixture tests without registering
    // an IME or loading project DLL code into a test desktop application.
    bool EnsureForModulePath(const std::wstring& module_path) {
        if (attempted_) return resource_ != nullptr;
        attempted_ = true;
        try {
            auto bytes = ReadBundledCandidateFont(module_path);
            if (bytes.empty()) return false;
            DWORD count = 0;
            resource_ = AddFontMemResourceEx(bytes.data(), static_cast<DWORD>(bytes.size()), nullptr, &count);
            if (!resource_) return false;
            font_count_ = count;
            // GDI copies the image, so bytes and file handles need not survive.
            return true;
        } catch (...) { return false; } // Font failure must not break conversion.
    }
    bool Reset() {
        if (resource_ && !RemoveFontMemResourceEx(resource_)) return false;
        resource_ = nullptr; font_count_ = 0; attempted_ = false; return true;
    }
    bool Loaded() const { return resource_ != nullptr; }
    DWORD FontCount() const { return font_count_; }
private:
    bool attempted_ = false;
    HANDLE resource_ = nullptr;
    DWORD font_count_ = 0;
};

inline HFONT TryCandidateFont(HDC dc, unsigned dpi, const wchar_t* family, int logical_height) {
    if (!dc || !family || !*family || logical_height <= 0) return nullptr;
    HFONT font = CreateFontW(-Scale(logical_height, dpi), 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
        DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY, DEFAULT_PITCH, family);
    if (!font) return nullptr;
    HGDIOBJ previous = SelectObject(dc, font);
    if (!previous || previous == HGDI_ERROR) { DeleteObject(font); return nullptr; }
    wchar_t realized[LF_FACESIZE]{};
    const bool exact = GetTextFaceW(dc, LF_FACESIZE, realized) > 0 &&
        CompareStringOrdinal(realized, -1, family, -1, TRUE) == CSTR_EQUAL;
    SelectObject(dc, previous);
    // CreateFontW may succeed while silently substituting an unrelated face.
    // Reject that result so the explicit Traditional Chinese fallback order is
    // tried before the caller's stock-font last resort.
    if (!exact) { DeleteObject(font); return nullptr; }
    return font;
}

inline int CALLBACK CandidateFontFound(const LOGFONTW*, const TEXTMETRICW*, DWORD, LPARAM found) {
    *reinterpret_cast<bool*>(found) = true;
    return 0;
}

// A user-chosen face may be written in English or its localized name (for
// example Microsoft JhengHei's Chinese name), so GetTextFaceW can legitimately
// report the other name.
// Enumeration matches either, and still rejects an absent font that GDI would
// otherwise silently substitute.
inline HFONT TryUserCandidateFont(HDC dc, unsigned dpi, const wchar_t* family, int logical_height) {
    if (!dc || !family || !*family || logical_height <= 0 || lstrlenW(family) >= LF_FACESIZE) return nullptr;
    LOGFONTW query{};
    query.lfCharSet = DEFAULT_CHARSET;
    lstrcpynW(query.lfFaceName, family, LF_FACESIZE);
    bool found = false;
    EnumFontFamiliesExW(dc, &query, CandidateFontFound, reinterpret_cast<LPARAM>(&found), 0);
    if (!found) return nullptr;
    return CreateFontW(-Scale(logical_height, dpi), 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
        DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY, DEFAULT_PITCH, family);
}

inline HFONT CreateCandidateFont(HDC dc, unsigned dpi, const wchar_t* preferred_family, int logical_height,
                                 const wchar_t* user_family = nullptr) {
    if (HFONT font = TryUserCandidateFont(dc, dpi, user_family, logical_height)) return font;
    const wchar_t* families[]{preferred_family, L"Microsoft JhengHei UI", L"Microsoft JhengHei", L"Segoe UI"};
    for (const wchar_t* family : families)
        if (HFONT font = TryCandidateFont(dc, dpi, family, logical_height)) return font;
    return nullptr; // The caller can use its existing DEFAULT_GUI_FONT fallback.
}
} // namespace cime::ui
