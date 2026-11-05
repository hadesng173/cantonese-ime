// Project-authored, deliberately narrow machine registration helper.
// Fresh machine install does not enable a user profile. Metadata refresh preserves
// the calling account's existing enabled flags. Separate user actions reject elevation.
// No action copies files, launches the text service or calls a default-setting API.
#include <windows.h>
#include <aclapi.h>
#include <sddl.h>
#include <shlobj.h>
#include <wincrypt.h>
#include <msctf.h>
#include <algorithm>
#include <cstdint>
#include <cwchar>
#include <iomanip>
#include <iostream>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
#include "../tsf/ime_ids.h"
#include "../tsf/resource.h"
#include "../tsf/tsf_compat.h"

namespace {
static_assert(sizeof(void*) == 8, "Only the AMD64 helper is supported");
struct Handle { HANDLE value = INVALID_HANDLE_VALUE; ~Handle() { if (value && value != INVALID_HANDLE_VALUE) CloseHandle(value); } };
template<class T> struct Com { T* value = nullptr; ~Com() { if (value) value->Release(); } };
struct Local { void* value = nullptr; ~Local() { if (value) LocalFree(value); } };
struct Failure { HRESULT result; std::wstring detail; };
const LANGID kLanguages[] = {0x0409, 0x0c04};
const GUID* const kCategories[] = {&GUID_TFCAT_TIP_KEYBOARD, &GUID_TFCAT_DISPLAYATTRIBUTEPROVIDER,
    &GUID_TFCAT_TIPCAP_UIELEMENTENABLED, &GUID_TFCAT_TIPCAP_SYSTRAYSUPPORT, &GUID_TFCAT_TIPCAP_IMMERSIVESUPPORT,
    // Games such as World of Warcraft run their UI thread without COM; TSF
    // activates only COM-less text services there.
    &GUID_TFCAT_TIPCAP_COMLESS};
const wchar_t* const kCategoryNames[] = {L"keyboard", L"display_attribute", L"ui_element", L"system_tray", L"immersive", L"comless"};
constexpr unsigned kCategoryCount = sizeof(kCategories) / sizeof(kCategories[0]);
// Modern TSF uses a signed resource location packed in the ULONG argument,
// matching Microsoft's SampleIME RegisterProfile((UINT)-resourceID) pattern.
// Legacy AddLanguageProfile has a separate documented ordinal contract.
constexpr ULONG kBrandingIconLocation = static_cast<ULONG>(-IDI_CANTONESE_BRAND);

std::wstring Hex(ULONG value) { std::wostringstream out; out << L"0x" << std::hex << std::setw(8) << std::setfill(L'0') << value; return out.str(); }
std::wstring Guid(REFGUID value) { wchar_t buffer[64]{}; StringFromGUID2(value, buffer, 64); return buffer; }
bool Equal(const std::wstring& a, const std::wstring& b) { return CompareStringOrdinal(a.c_str(), -1, b.c_str(), -1, TRUE) == CSTR_EQUAL; }
std::wstring Wide(const std::string& text) {
    if (text.empty()) return {};
    const int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text.data(), static_cast<int>(text.size()), nullptr, 0);
    if (!count) throw Failure{E_INVALIDARG, L"Invalid UTF-8"};
    std::wstring result(count, L'\0');
    MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text.data(), static_cast<int>(text.size()), result.data(), count);
    return result;
}
std::string Json(const std::wstring& value) {
    const int size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
    std::string bytes(size, '\0');
    if (size) WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()), bytes.data(), size, nullptr, nullptr);
    std::ostringstream out; out << '"';
    for (unsigned char c : bytes) {
        if (c == '\\' || c == '"') out << '\\' << static_cast<char>(c);
        else if (c < 32) out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << unsigned(c) << std::dec;
        else out << static_cast<char>(c);
    }
    out << '"'; return out.str();
}
void Log(const std::wstring& stage, HRESULT result, const std::wstring& detail = L"") {
    std::cout << "{\"stage\":" << Json(stage) << ",\"hresult\":" << Json(Hex(static_cast<ULONG>(result)))
              << ",\"succeeded\":" << (SUCCEEDED(result) ? "true" : "false") << ",\"detail\":" << Json(detail) << "}\n";
    std::cout.flush();
}
void Require(bool condition, HRESULT result, const std::wstring& detail) { if (!condition) throw Failure{result, detail}; }
void Checked(const std::wstring& stage, HRESULT result, const std::wstring& detail = L"") {
    Log(stage, result, detail); if (FAILED(result)) throw Failure{result, stage};
}
HRESULT WinError(DWORD error) { return HRESULT_FROM_WIN32(error); }
bool Digest(const std::wstring& value) {
    return value.size() == 64 && std::all_of(value.begin(), value.end(), [](wchar_t c) { return (c >= L'0' && c <= L'9') || (c >= L'a' && c <= L'f'); });
}
void OpenRead(const std::wstring& path, Handle& handle, bool directory = false) {
    handle.value = CreateFileW(path.c_str(), GENERIC_READ | READ_CONTROL, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
        directory ? FILE_FLAG_BACKUP_SEMANTICS : FILE_ATTRIBUTE_NORMAL, nullptr);
    if (handle.value == INVALID_HANDLE_VALUE) throw Failure{WinError(GetLastError()), L"Cannot open " + path};
}
std::wstring FinalPath(HANDLE file) {
    std::wstring path(32768, L'\0');
    const DWORD count = GetFinalPathNameByHandleW(file, path.data(), static_cast<DWORD>(path.size()), FILE_NAME_NORMALIZED);
    Require(count && count < path.size(), WinError(count ? ERROR_INSUFFICIENT_BUFFER : GetLastError()), L"Cannot resolve final file path");
    path.resize(count); return path;
}
void Rewind(HANDLE file) { LARGE_INTEGER zero{}; if (!SetFilePointerEx(file, zero, nullptr, FILE_BEGIN)) throw Failure{WinError(GetLastError()), L"Cannot rewind file"}; }
std::wstring Hash(HANDLE file) {
    HCRYPTPROV provider = 0; HCRYPTHASH hash = 0;
    if (!CryptAcquireContextW(&provider, nullptr, nullptr, PROV_RSA_AES, CRYPT_VERIFYCONTEXT)) throw Failure{WinError(GetLastError()), L"SHA256 provider"};
    struct CryptoCleanup { HCRYPTPROV provider; HCRYPTHASH& hash; ~CryptoCleanup() { if (hash) CryptDestroyHash(hash); CryptReleaseContext(provider, 0); } } cleanup{provider, hash};
    if (!CryptCreateHash(provider, CALG_SHA_256, 0, 0, &hash)) throw Failure{WinError(GetLastError()), L"SHA256 create"};
    Rewind(file); BYTE buffer[65536]; DWORD count = 0;
    for (;;) {
        if (!ReadFile(file, buffer, sizeof(buffer), &count, nullptr)) throw Failure{WinError(GetLastError()), L"SHA256 read"};
        if (!count) break;
        if (!CryptHashData(hash, buffer, count, 0)) throw Failure{WinError(GetLastError()), L"SHA256 update"};
    }
    BYTE digest[32]{}; DWORD size = sizeof(digest);
    if (!CryptGetHashParam(hash, HP_HASHVAL, digest, &size, 0) || size != sizeof(digest)) throw Failure{E_FAIL, L"SHA256 result"};
    std::wostringstream out; out << std::hex << std::setfill(L'0'); for (BYTE c : digest) out << std::setw(2) << unsigned(c); return out.str();
}
std::string ReadSmall(HANDLE file) {
    LARGE_INTEGER size{};
    Require(GetFileSizeEx(file, &size) && size.QuadPart > 0 && size.QuadPart <= 65536, E_INVALIDARG, L"Manifest must contain 1..65536 bytes");
    Rewind(file); std::string result(static_cast<size_t>(size.QuadPart), '\0'); DWORD count = 0;
    Require(ReadFile(file, result.data(), static_cast<DWORD>(result.size()), &count, nullptr) && count == result.size(), E_FAIL, L"Cannot read manifest");
    return result;
}
void CheckPe(HANDLE file, bool require_dll = true) {
    LARGE_INTEGER size{};
    Require(GetFileSizeEx(file, &size) && size.QuadPart >= 128 && size.QuadPart <= 64 * 1024 * 1024, E_INVALIDARG, L"PE file size must be 128 bytes..64 MiB");
    Rewind(file); IMAGE_DOS_HEADER dos{}; DWORD count = 0;
    Require(ReadFile(file, &dos, sizeof(dos), &count, nullptr) && count == sizeof(dos) && dos.e_magic == IMAGE_DOS_SIGNATURE && dos.e_lfanew >= static_cast<LONG>(sizeof(dos)) && dos.e_lfanew <= 1024 * 1024, E_INVALIDARG, L"Invalid PE DOS header");
    LARGE_INTEGER offset{}; offset.QuadPart = dos.e_lfanew;
    Require(SetFilePointerEx(file, offset, nullptr, FILE_BEGIN), E_FAIL, L"Cannot seek PE header");
    struct { DWORD signature; IMAGE_FILE_HEADER header; } nt{};
    Require(ReadFile(file, &nt, sizeof(nt), &count, nullptr) && count == sizeof(nt) && nt.signature == IMAGE_NT_SIGNATURE &&
        nt.header.Machine == IMAGE_FILE_MACHINE_AMD64 && (nt.header.Characteristics & IMAGE_FILE_EXECUTABLE_IMAGE) &&
        ((nt.header.Characteristics & IMAGE_FILE_DLL) != 0) == require_dll, E_INVALIDARG,
        require_dll ? L"Only an AMD64 PE DLL is accepted" : L"Only an AMD64 PE EXE is accepted");
}
bool Privileged(PSID sid) {
    for (const wchar_t* text : {L"S-1-5-18", L"S-1-5-32-544", L"S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"}) {
        Local expected; if (ConvertStringSidToSidW(text, &expected.value) && EqualSid(sid, expected.value)) return true;
    }
    return false;
}
void ProtectedAcl(HANDLE file, const std::wstring& label) {
    PSID owner = nullptr; PACL dacl = nullptr; PSECURITY_DESCRIPTOR descriptor = nullptr;
    const DWORD error = GetSecurityInfo(file, SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION, &owner, nullptr, &dacl, nullptr, &descriptor);
    Local memory; memory.value = descriptor;
    Require(error == ERROR_SUCCESS && owner && dacl && Privileged(owner), error ? WinError(error) : E_ACCESSDENIED, L"Protected owner/DACL required: " + label);
    constexpr DWORD writes = GENERIC_WRITE | GENERIC_ALL | FILE_WRITE_DATA | FILE_APPEND_DATA | FILE_WRITE_EA | FILE_WRITE_ATTRIBUTES | FILE_DELETE_CHILD | DELETE | WRITE_DAC | WRITE_OWNER;
    for (DWORD i = 0; i < dacl->AceCount; ++i) {
        void* raw = nullptr; Require(GetAce(dacl, i, &raw), E_FAIL, L"Cannot inspect ACL");
        auto* header = static_cast<ACE_HEADER*>(raw);
        if (header->AceFlags & INHERIT_ONLY_ACE) continue;
        if (header->AceType == ACCESS_DENIED_ACE_TYPE) continue;
        Require(header->AceType == ACCESS_ALLOWED_ACE_TYPE, E_ACCESSDENIED, L"Unsupported effective ACL entry: " + label);
        auto* ace = static_cast<ACCESS_ALLOWED_ACE*>(raw);
        Require(!(ace->Mask & writes) || Privileged(reinterpret_cast<PSID>(&ace->SidStart)), E_ACCESSDENIED, L"Unprivileged write permission: " + label);
    }
    Log(L"protected_acl", S_OK, label);
}
std::wstring FullPath(const std::wstring& path) {
    Require(path.size() > 3 && path[1] == L':' && path[2] == L'\\' && path.find(L'/') == std::wstring::npos && path.find(L':', 2) == std::wstring::npos, E_INVALIDARG, L"Require absolute drive path without alternate streams");
    std::wstring result(32768, L'\0'); const DWORD size = GetFullPathNameW(path.c_str(), static_cast<DWORD>(result.size()), result.data(), nullptr);
    Require(size && size < result.size(), E_INVALIDARG, L"Invalid absolute path"); result.resize(size);
    Require(Equal(result, path), E_INVALIDARG, L"Paths must already be normalized (no dot segments)"); return result;
}
// A system plan registers a copy in the System32 directory (games such as World
// of Warcraft load text-service DLLs only from there) while the product files,
// including the identical source DLL, stay under Program Files.
struct Plan { std::wstring source, target, hash, program_files, product_root, relative, system_directory; bool system = false; };
std::wstring LegacyTarget(const Plan& plan) { return plan.product_root + L"\\native\\CantoneseIME.dll"; }
std::wstring BrandingPath(const Plan& plan) {
    return plan.product_root + L"\\native\\CantoneseMachineRegistration.exe";
}
template<class ProfileManager>
HRESULT RegisterReleaseProfile(ProfileManager* profiles, LANGID language, const Plan& plan) {
    const auto icon_file = BrandingPath(plan);
    return profiles->RegisterProfile(CLSID_CantoneseIME, language, GUID_CantoneseProfile,
        kProductName, static_cast<ULONG>(std::wcslen(kProductName)), icon_file.c_str(),
        static_cast<ULONG>(icon_file.size()), kBrandingIconLocation, nullptr, 0, FALSE, 0);
}
void CheckBrandingSelfPath(const Plan& plan, const std::wstring& module_path) {
    Require(Equal(FullPath(module_path), BrandingPath(plan)), E_ACCESSDENIED,
        L"Profile branding requires the executing helper at its fixed protected native path");
}
Plan ReadPlan(const std::wstring& path, const std::wstring& expected_hash) {
    Require(Digest(expected_hash), E_INVALIDARG, L"--manifest-sha256 must be 64 lowercase hexadecimal digits");
    Handle file; OpenRead(FullPath(path), file);
    const std::string bytes = ReadSmall(file.value);
    Checked(L"manifest_sha256", Hash(file.value) == expected_hash ? S_OK : E_ACCESSDENIED, expected_hash);
    std::istringstream input(bytes); std::string line; std::map<std::string, std::wstring> fields;
    Require(static_cast<bool>(std::getline(input, line)), E_INVALIDARG, L"Missing manifest header"); if (!line.empty() && line.back() == '\r') line.pop_back();
    Require(line == "CANTONESE_IME_MACHINE_V1", E_INVALIDARG, L"Unknown manifest version (UTF-8 without BOM required)");
    while (std::getline(input, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        const size_t split = line.find('=');
        Require(split != std::string::npos && split > 0 && split + 1 < line.size() && line.find('\0') == std::string::npos && line.find('\r') == std::string::npos, E_INVALIDARG, L"Malformed manifest line");
        const std::string key = line.substr(0, split); const std::wstring value = Wide(line.substr(split + 1));
        Require(fields.emplace(key, value).second && std::none_of(value.begin(), value.end(), [](wchar_t c) { return c < 32; }), E_INVALIDARG, L"Duplicate/control-character manifest field");
    }
    const std::vector<std::string> names = {"clsid", "profile", "languages", "dll_source", "dll_target", "dll_sha256"};
    Require(fields.size() == names.size(), E_INVALIDARG, L"Exactly six known manifest fields are required");
    for (const auto& name : names) Require(fields.count(name) != 0, E_INVALIDARG, L"Missing/unknown manifest field");
    Require(Equal(fields.at("clsid"), Guid(CLSID_CantoneseIME)) && Equal(fields.at("profile"), Guid(GUID_CantoneseProfile)) && fields.at("languages") == L"0409,0c04", E_ACCESSDENIED, L"Manifest IDs/languages differ from this product");
    Plan plan; plan.source = FullPath(fields.at("dll_source")); plan.target = FullPath(fields.at("dll_target")); plan.hash = fields.at("dll_sha256");
    Require(Digest(plan.hash), E_INVALIDARG, L"Invalid DLL digest");
    wchar_t program_files[MAX_PATH]{}; Checked(L"program_files_known_folder", SHGetFolderPathW(nullptr, CSIDL_PROGRAM_FILES, nullptr, SHGFP_TYPE_CURRENT, program_files));
    plan.program_files = program_files; plan.product_root = plan.program_files + L"\\CantoneseIME";
    wchar_t system_directory[MAX_PATH]{}; const UINT system_length = GetSystemDirectoryW(system_directory, MAX_PATH);
    Require(system_length && system_length < MAX_PATH, WinError(GetLastError()), L"Cannot read the System32 directory");
    plan.system_directory = system_directory;
    if (Equal(plan.target, plan.product_root + L"\\CantoneseIME.dll")) plan.relative = L"\\CantoneseIME.dll";
    else if (Equal(plan.target, LegacyTarget(plan))) plan.relative = L"\\native\\CantoneseIME.dll";
    else if (Equal(plan.target, plan.system_directory + L"\\CantoneseIME.dll") && Equal(plan.source, LegacyTarget(plan))) {
        plan.system = true; plan.relative = L"\\native\\CantoneseIME.dll";
    } else throw Failure{E_ACCESSDENIED, L"Target must be a fixed CantoneseIME DLL path under the actual 64-bit Program Files known folder, or its System32 copy"};
    Handle source; OpenRead(plan.source, source); CheckPe(source.value);
    Checked(L"source_dll_sha256", Hash(source.value) == plan.hash ? S_OK : E_ACCESSDENIED, plan.source);
    Log(L"plan_ids", S_OK, Guid(CLSID_CantoneseIME) + L" " + Guid(GUID_CantoneseProfile) + L" languages=0409,0c04");
    Log(L"plan_target", S_OK, plan.target); return plan;
}
bool Exists(HKEY hive, const std::wstring& path) {
    HKEY key = nullptr; const LONG error = RegOpenKeyExW(hive, path.c_str(), 0, KEY_READ | KEY_WOW64_64KEY, &key);
    if (error == ERROR_SUCCESS) { RegCloseKey(key); return true; }
    if (error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND) return false;
    throw Failure{WinError(error), L"Cannot inspect registry " + path};
}
std::wstring ClassKey() { return L"Software\\Classes\\CLSID\\" + Guid(CLSID_CantoneseIME); }
std::wstring TipKey() { return L"Software\\Microsoft\\CTF\\TIP\\" + Guid(CLSID_CantoneseIME); }
bool Elevated() {
    Handle token; HANDLE value = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &value)) throw Failure{WinError(GetLastError()), L"Cannot open token"}; token.value = value;
    TOKEN_ELEVATION elevation{}; DWORD size = 0;
    if (!GetTokenInformation(token.value, TokenElevation, &elevation, sizeof(elevation), &size)) throw Failure{WinError(GetLastError()), L"Cannot inspect token elevation"};
    DWORD needed = 0; GetTokenInformation(token.value, TokenUser, nullptr, 0, &needed);
    Require(needed && needed <= 65536, E_FAIL, L"Cannot query token SID size");
    std::vector<BYTE> user(needed);
    if (!GetTokenInformation(token.value, TokenUser, user.data(), needed, &needed)) throw Failure{WinError(GetLastError()), L"Cannot query token user"};
    LPWSTR sid = nullptr;
    if (!ConvertSidToStringSidW(reinterpret_cast<TOKEN_USER*>(user.data())->User.Sid, &sid)) throw Failure{WinError(GetLastError()), L"Cannot format token SID"};
    Local sid_memory; sid_memory.value = sid; Log(L"token_user_sid", S_OK, sid);
    return elevation.TokenIsElevated != 0;
}
void SetString(HKEY key, const wchar_t* name, const std::wstring& value) {
    const LONG error = RegSetValueExW(key, name, 0, REG_SZ, reinterpret_cast<const BYTE*>(value.c_str()), static_cast<DWORD>((value.size() + 1) * sizeof(wchar_t)));
    if (error) throw Failure{WinError(error), L"Cannot set owned COM registry value"};
}
void CreateCom(const Plan& plan, bool& created) {
    HKEY key = nullptr; DWORD disposition = 0;
    LONG error = RegCreateKeyExW(HKEY_LOCAL_MACHINE, ClassKey().c_str(), 0, nullptr, REG_OPTION_NON_VOLATILE, KEY_WRITE | KEY_WOW64_64KEY, nullptr, &key, &disposition);
    Checked(L"HKLM_COM_create", WinError(error));
    if (disposition != REG_CREATED_NEW_KEY) { RegCloseKey(key); throw Failure{WinError(ERROR_ALREADY_EXISTS), L"Owned COM key appeared after preflight"}; }
    created = true;
    try { SetString(key, nullptr, kProductName); } catch (...) { RegCloseKey(key); throw; } RegCloseKey(key);
    error = RegCreateKeyExW(HKEY_LOCAL_MACHINE, (ClassKey() + L"\\InprocServer32").c_str(), 0, nullptr, REG_OPTION_NON_VOLATILE, KEY_WRITE | KEY_WOW64_64KEY, nullptr, &key, nullptr);
    Checked(L"HKLM_COM_InprocServer32_create", WinError(error));
    try { SetString(key, nullptr, plan.target); SetString(key, L"ThreadingModel", L"Apartment"); } catch (...) { RegCloseKey(key); throw; } RegCloseKey(key);
    Log(L"HKLM_COM_values", S_OK, plan.target);
}
HRESULT DeleteCom() {
    HKEY parent = nullptr; LONG error = RegOpenKeyExW(HKEY_LOCAL_MACHINE, L"Software\\Classes\\CLSID", 0, DELETE | KEY_WRITE | KEY_READ | KEY_WOW64_64KEY, &parent);
    if (error) return WinError(error);
    error = RegDeleteTreeW(parent, Guid(CLSID_CantoneseIME).c_str()); RegCloseKey(parent);
    return error == ERROR_FILE_NOT_FOUND ? S_OK : WinError(error);
}
std::wstring ReadRegString(HKEY key, const wchar_t* name) {
    DWORD kind = 0, size = 0; LONG error = RegQueryValueExW(key, name, nullptr, &kind, nullptr, &size);
    Require(!error && kind == REG_SZ && size >= sizeof(wchar_t) && size <= 65536 && !(size % sizeof(wchar_t)), error ? WinError(error) : E_ACCESSDENIED, L"Unexpected COM registry value");
    std::vector<wchar_t> result(size / sizeof(wchar_t)); error = RegQueryValueExW(key, name, nullptr, &kind, reinterpret_cast<BYTE*>(result.data()), &size);
    Require(!error && result.back() == 0 && std::wcslen(result.data()) == result.size() - 1, error ? WinError(error) : E_ACCESSDENIED, L"Malformed COM registry value"); return result.data();
}
// Returns the bound DLL path. A system plan also accepts the Program Files
// binding of an install made before the System32 copy; refresh rebinds it.
std::wstring RemovalBinding(const Plan& plan) {
    HKEY key = nullptr; LONG error = RegOpenKeyExW(HKEY_LOCAL_MACHINE, (ClassKey() + L"\\InprocServer32").c_str(), 0, KEY_READ | KEY_WOW64_64KEY, &key);
    Checked(L"remove_HKLM_COM_open", WinError(error));
    std::wstring bound;
    try {
        bound = ReadRegString(key, nullptr);
        Require((Equal(bound, plan.target) || (plan.system && Equal(bound, LegacyTarget(plan)))) && ReadRegString(key, L"ThreadingModel") == L"Apartment",
            E_ACCESSDENIED, L"Existing COM DLL/thread model differs from this exact install plan");
    } catch (...) { RegCloseKey(key); throw; }
    RegCloseKey(key); Log(L"remove_exact_binding", S_OK, bound); return bound;
}
void SetBinding(const std::wstring& path) {
    HKEY key = nullptr;
    Checked(L"rebind_HKLM_COM_open", WinError(RegOpenKeyExW(HKEY_LOCAL_MACHINE, (ClassKey() + L"\\InprocServer32").c_str(), 0,
        KEY_QUERY_VALUE | KEY_SET_VALUE | KEY_WOW64_64KEY, &key)));
    try { SetString(key, nullptr, path); } catch (...) { RegCloseKey(key); throw; }
    RegCloseKey(key); Log(L"rebind_HKLM_COM_value", S_OK, path);
}
std::vector<std::wstring> Subkeys(HKEY hive, const std::wstring& path) {
    HKEY key = nullptr; const LONG opened = RegOpenKeyExW(hive, path.c_str(), 0, KEY_READ | KEY_WOW64_64KEY, &key);
    if (opened == ERROR_FILE_NOT_FOUND || opened == ERROR_PATH_NOT_FOUND) return {};
    if (opened) throw Failure{WinError(opened), L"Cannot inspect machine TIP namespace"};
    std::vector<std::wstring> result;
    for (DWORD index = 0; ; ++index) {
        wchar_t name[512]{}; DWORD size = 512; const LONG error = RegEnumKeyExW(key, index, name, &size, nullptr, nullptr, nullptr, nullptr);
        if (error == ERROR_NO_MORE_ITEMS) break;
        if (error || index >= 128) { RegCloseKey(key); throw Failure{error ? WinError(error) : E_FAIL, L"Unexpected/bounded TIP subkey enumeration"}; }
        result.emplace_back(name, size);
    }
    RegCloseKey(key); return result;
}
std::vector<LANGID> GuardTipIdentities(HKEY hive = HKEY_LOCAL_MACHINE) {
    // Read only: never manufacture or directly delete Windows' CTF profile keys.
    std::vector<LANGID> languages;
    const auto root = TipKey() + L"\\LanguageProfile";
    for (const auto& name : Subkeys(hive, root)) {
        LANGID language = 0;
        if (Equal(name, L"0x00000409")) language = 0x0409;
        else if (Equal(name, L"0x00000c04")) language = 0x0c04;
        else throw Failure{E_ACCESSDENIED, L"Unexpected language in owned TIP namespace; refusing shared-category removal"};
        for (const auto& profile : Subkeys(hive, root + L"\\" + name)) {
            Require(Equal(profile, Guid(GUID_CantoneseProfile)), E_ACCESSDENIED, L"Unexpected profile GUID inside product TIP namespace");
            Require(std::find(languages.begin(), languages.end(), language) == languages.end(), E_FAIL, L"Duplicate product profile language"); languages.push_back(language);
        }
    }
    Log(hive == HKEY_LOCAL_MACHINE ? L"machine_tip_identity_inventory" : L"user_tip_preference_inventory", S_OK, std::to_wstring(languages.size()) + L" owned profile(s); no unexpected language/profile GUID");
    return languages;
}
std::vector<LANGID> ExistingProfiles(ITfInputProcessorProfileMgr* profiles) {
    std::vector<LANGID> found;
    for (LANGID language : GuardTipIdentities()) {
        Com<IEnumTfInputProcessorProfiles> values; Checked(L"remove_EnumProfiles_" + Hex(language), profiles->EnumProfiles(language, &values.value));
        Require(values.value != nullptr, E_FAIL, L"Missing profile enumerator");
        unsigned scanned = 0;
        for (;;) {
            TF_INPUTPROCESSORPROFILE value{}; ULONG fetched = 0; const HRESULT hr = values.value->Next(1, &value, &fetched);
            Require((hr == S_OK && fetched == 1) || (hr == S_FALSE && fetched == 0), FAILED(hr) ? hr : E_FAIL, L"Unexpected profile enumeration result");
            if (hr == S_FALSE) break;
            Require(++scanned <= 16384, E_FAIL, L"Profile enumeration exceeded bound");
            if (!IsEqualCLSID(value.clsid, CLSID_CantoneseIME)) continue;
            Require(value.dwProfileType == TF_PROFILETYPE_INPUTPROCESSOR && value.langid == language && IsEqualGUID(value.guidProfile, GUID_CantoneseProfile), E_ACCESSDENIED, L"Unexpected profile inside this product's CLSID; refusing removal");
            Require(std::find(found.begin(), found.end(), language) == found.end(), E_FAIL, L"Duplicate owned profile"); found.push_back(language);
        }
    }
    return found;
}
std::vector<unsigned> ExistingCategories(ITfCategoryMgr* categories) {
    Com<IEnumGUID> values; Checked(L"remove_EnumCategoriesInItem", categories->EnumCategoriesInItem(CLSID_CantoneseIME, &values.value));
    Require(values.value != nullptr, E_FAIL, L"Missing category enumerator"); std::vector<unsigned> found;
    for (;;) {
        GUID value{}; ULONG fetched = 0; const HRESULT hr = values.value->Next(1, &value, &fetched);
        Require((hr == S_OK && fetched == 1) || (hr == S_FALSE && fetched == 0), FAILED(hr) ? hr : E_FAIL, L"Unexpected category enumeration result");
        if (hr == S_FALSE) break;
        unsigned index = kCategoryCount; for (unsigned i = 0; i < kCategoryCount; ++i) if (IsEqualGUID(value, *kCategories[i])) index = i;
        Require(index < kCategoryCount && std::find(found.begin(), found.end(), index) == found.end(), E_ACCESSDENIED, L"Unexpected category in product item; refusing removal"); found.push_back(index);
    }
    for (unsigned index : found) Log(L"category_inventory_member", S_OK, Guid(*kCategories[index]));
    Log(L"category_inventory_count", S_OK, std::to_wstring(found.size()));
    return found;
}

struct CategoryLinks {
    std::vector<std::wstring> item_roots;
    std::vector<std::wstring> forward;
    std::map<std::wstring, std::vector<std::wstring>> reverse;
};
// Pure validation over a bounded read-only snapshot. TSF registration APIs own
// every write. Empty known category parents are normal UnregisterCategory
// residue; only member leaves constitute a category membership.
std::vector<unsigned> ValidateCategoryLinks(const CategoryLinks& links) {
    Require(links.item_roots.size() <= 1 && (links.item_roots.empty() || Equal(links.item_roots[0], Guid(CLSID_CantoneseIME))),
        E_ACCESSDENIED, L"Unexpected item in the owned machine category namespace");
    auto category_index = [](const std::wstring& name) {
        for (unsigned index = 0; index < kCategoryCount; ++index)
            if (Equal(name, Guid(*kCategories[index]))) return index;
        throw Failure{E_ACCESSDENIED, L"Unexpected category in the owned persistent category namespace"};
    };
    std::vector<unsigned> forward, reverse;
    for (const auto& name : links.forward) {
        const unsigned index = category_index(name);
        Require(std::find(forward.begin(), forward.end(), index) == forward.end(), E_ACCESSDENIED, L"Duplicate persistent category membership");
        forward.push_back(index);
    }
    Require(forward.empty() || links.item_roots.size() == 1, E_ACCESSDENIED, L"Persistent category item root is missing");
    for (const auto& pair : links.reverse) {
        const unsigned index = category_index(pair.first);
        Require(pair.second.size() <= 1 && (pair.second.empty() || Equal(pair.second[0], Guid(CLSID_CantoneseIME))),
            E_ACCESSDENIED, L"Unexpected reverse member in the owned machine category namespace");
        if (!pair.second.empty()) {
            Require(std::find(reverse.begin(), reverse.end(), index) == reverse.end(), E_ACCESSDENIED, L"Duplicate reverse category membership");
            reverse.push_back(index);
        }
    }
    std::sort(forward.begin(), forward.end()); std::sort(reverse.begin(), reverse.end());
    Require(forward == reverse, E_ACCESSDENIED, L"Persistent category indexes disagree or a reverse membership leaf is missing");
    return forward;
}
std::vector<unsigned> PersistentCategories() {
    CategoryLinks links;
    const auto root = TipKey() + L"\\Category";
    links.item_roots = Subkeys(HKEY_LOCAL_MACHINE, root + L"\\Item");
    links.forward = Subkeys(HKEY_LOCAL_MACHINE, root + L"\\Item\\" + Guid(CLSID_CantoneseIME));
    for (const auto& category : Subkeys(HKEY_LOCAL_MACHINE, root + L"\\Category"))
        links.reverse.emplace(category, Subkeys(HKEY_LOCAL_MACHINE, root + L"\\Category\\" + category));
    const auto found = ValidateCategoryLinks(links);
    for (unsigned index : found) Log(L"persistent_category_member", S_OK, Guid(*kCategories[index]));
    Log(L"persistent_category_count", S_OK, std::to_wstring(found.size()));
    return found;
}
// Hold verified file/directory handles through all mutations. Do not accept junctions,
// unprivileged writable product folders or a target swapped after digest verification.
void TargetGuard(const Plan& plan, std::vector<Handle*>& held, Handle& pf, Handle& root, Handle& native, Handle& dll) {
    OpenRead(plan.program_files, pf, true); held.push_back(&pf); const auto base = FinalPath(pf.value);
    auto directory = [&](const std::wstring& path, Handle& handle, const std::wstring& suffix) {
        const DWORD attrs = GetFileAttributesW(path.c_str());
        Require(attrs != INVALID_FILE_ATTRIBUTES && !(attrs & FILE_ATTRIBUTE_REPARSE_POINT), E_ACCESSDENIED, L"Missing/reparse product directory: " + path);
        OpenRead(path, handle, true); held.push_back(&handle);
        Require(Equal(FinalPath(handle.value), base + suffix), E_ACCESSDENIED, L"Product directory resolves outside fixed Program Files location"); ProtectedAcl(handle.value, path);
    };
    directory(plan.product_root, root, L"\\CantoneseIME");
    if (plan.relative == L"\\native\\CantoneseIME.dll") directory(plan.product_root + L"\\native", native, L"\\CantoneseIME\\native");
    const DWORD attrs = GetFileAttributesW(plan.target.c_str());
    Require(attrs != INVALID_FILE_ATTRIBUTES && !(attrs & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY)), E_ACCESSDENIED, L"Target DLL must be an existing regular file");
    OpenRead(plan.target, dll); held.push_back(&dll);
    if (plan.system) {
        // The System32 copy must be the exact bytes of the protected product DLL,
        // which still anchors the product folder for fonts and settings.
        Handle system; OpenRead(plan.system_directory, system, true);
        Require(Equal(FinalPath(dll.value), FinalPath(system.value) + L"\\CantoneseIME.dll"), E_ACCESSDENIED, L"System DLL resolves outside the System32 directory");
        Handle product; OpenRead(LegacyTarget(plan), product);
        Require(Equal(FinalPath(product.value), base + L"\\CantoneseIME" + plan.relative), E_ACCESSDENIED, L"Product DLL resolves outside fixed protected path");
        ProtectedAcl(product.value, LegacyTarget(plan)); CheckPe(product.value);
        Checked(L"protected_product_dll_sha256", Hash(product.value) == plan.hash ? S_OK : E_ACCESSDENIED, plan.hash);
    } else Require(Equal(FinalPath(dll.value), base + L"\\CantoneseIME" + plan.relative), E_ACCESSDENIED, L"DLL resolves outside fixed protected path");
    ProtectedAcl(dll.value, plan.target); CheckPe(dll.value); Checked(L"protected_target_dll_sha256", Hash(dll.value) == plan.hash ? S_OK : E_ACCESSDENIED, plan.hash);
}
// This resource container is deliberately separate from the COM service DLL:
// a long-lived host can still map an old service image at the canonical DLL
// path after upgrade. Loading that path as data may reuse its stale code image.
// Only actual install/refresh writes require this new-container guard. Old
// installations, removal and read-only workspace preflights remain inspectable.
void BrandingResources(HMODULE module) {
    Require(module != nullptr, E_FAIL, L"Missing executing helper module");
    HRSRC group = FindResourceW(module, MAKEINTRESOURCEW(IDI_CANTONESE_BRAND), RT_GROUP_ICON);
    Require(group && SizeofResource(module, group) > 6, E_FAIL, L"Executing helper lacks branding icon resource101");
    HGLOBAL loaded = LoadResource(module, group);
    Require(loaded && LockResource(loaded), E_FAIL, L"Cannot read helper branding group");
    for (int size : {16, 20, 24, 32, 40, 48}) {
        HICON icon = static_cast<HICON>(LoadImageW(module, MAKEINTRESOURCEW(IDI_CANTONESE_BRAND),
            IMAGE_ICON, size, size, LR_DEFAULTCOLOR));
        Require(icon != nullptr, E_FAIL, L"Helper branding resource cannot produce a required icon size");
        DestroyIcon(icon);
    }
    Log(L"branding_resources", S_OK, L"Fixed group101 and all six required DPI icon sizes are readable in the executing helper");
}
void BrandingGuard(const Plan& plan, Handle& pf, Handle& directory, Handle& executable) {
    std::wstring module_path(32768, L'\0');
    const DWORD length = GetModuleFileNameW(nullptr, module_path.data(), static_cast<DWORD>(module_path.size()));
    Require(length && length < module_path.size(), E_FAIL, L"Cannot resolve executing helper path");
    module_path.resize(length); CheckBrandingSelfPath(plan, module_path);
    const auto native_path = plan.product_root + L"\\native";
    const DWORD directory_attributes = GetFileAttributesW(native_path.c_str());
    Require(directory_attributes != INVALID_FILE_ATTRIBUTES && (directory_attributes & FILE_ATTRIBUTE_DIRECTORY) &&
        !(directory_attributes & FILE_ATTRIBUTE_REPARSE_POINT), E_ACCESSDENIED, L"Missing/reparse branding container directory");
    OpenRead(native_path, directory, true);
    const auto expected_native = FinalPath(pf.value) + L"\\CantoneseIME\\native";
    Require(Equal(FinalPath(directory.value), expected_native), E_ACCESSDENIED, L"Branding directory resolves outside fixed Program Files path");
    ProtectedAcl(directory.value, native_path);
    const auto icon_file = BrandingPath(plan);
    const DWORD attributes = GetFileAttributesW(icon_file.c_str());
    Require(attributes != INVALID_FILE_ATTRIBUTES && !(attributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY)),
        E_ACCESSDENIED, L"Branding helper must be an existing regular non-reparse EXE");
    OpenRead(icon_file, executable);
    Require(Equal(FinalPath(executable.value), expected_native + L"\\CantoneseMachineRegistration.exe"),
        E_ACCESSDENIED, L"Branding EXE resolves outside its fixed protected path");
    ProtectedAcl(executable.value, icon_file); CheckPe(executable.value, false);
    BrandingResources(GetModuleHandleW(nullptr));
    Log(L"branding_exact_container", S_OK, icon_file);
}
void Rollback(ITfInputProcessorProfileMgr* profiles, ITfCategoryMgr* categories, bool com_created, const std::vector<LANGID>& attempted, const std::vector<unsigned>& registered) {
    bool clean = true;
    for (auto i = registered.rbegin(); i != registered.rend(); ++i) { const HRESULT hr = categories->UnregisterCategory(CLSID_CantoneseIME, *kCategories[*i], CLSID_CantoneseIME); Log(L"rollback_category_" + std::wstring(kCategoryNames[*i]), hr); clean &= SUCCEEDED(hr); }
    for (auto i = attempted.rbegin(); i != attempted.rend(); ++i) { const HRESULT hr = profiles->UnregisterProfile(CLSID_CantoneseIME, *i, GUID_CantoneseProfile, 0); Log(L"rollback_profile_" + Hex(*i), hr); clean &= SUCCEEDED(hr); }
    bool tip_remains = true;
    try { tip_remains = Exists(HKEY_LOCAL_MACHINE, TipKey()); Log(L"rollback_HKLM_TIP", tip_remains ? E_FAIL : S_OK, tip_remains ? L"present" : L"absent"); }
    catch (const Failure& failure) { clean = false; Log(L"rollback_HKLM_TIP_query", failure.result, failure.detail); }
    clean &= !tip_remains;
    if (com_created && clean) { const HRESULT hr = DeleteCom(); Log(L"rollback_HKLM_COM", hr); clean &= SUCCEEDED(hr); }
    else if (com_created) Log(L"rollback_HKLM_COM_retained", S_FALSE, L"TSF rollback failed or TIP absence is uncertain; preserve the binding and files for inspection/retry");
    Log(L"rollback_summary", clean ? S_OK : E_FAIL, L"API results only; inspect persistent state before retrying. Files are retained.");
}
struct ProfileMetadata {
    LANGID language = 0;
    std::wstring description, icon;
    ULONG icon_index = 0;
    BOOL enabled = FALSE;
};
ProfileMetadata SnapshotProfile(ITfInputProcessorProfiles* profiles, LANGID language) {
    ProfileMetadata result; result.language = language;
    BSTR description = nullptr;
    Checked(L"refresh_snapshot_description_" + Hex(language), profiles->GetLanguageProfileDescription(
        CLSID_CantoneseIME, language, GUID_CantoneseProfile, &description));
    struct BstrCleanup { BSTR value; ~BstrCleanup() { SysFreeString(value); } } cleanup{description};
    Require(description && SysStringLen(description) > 0 && SysStringLen(description) < 4096, E_FAIL, L"Invalid old profile description");
    result.description.assign(description, SysStringLen(description));
    Checked(L"refresh_snapshot_enabled_" + Hex(language), profiles->IsEnabledLanguageProfile(
        CLSID_CantoneseIME, language, GUID_CantoneseProfile, &result.enabled));
    // The profile APIs have no icon getter. Read exactly the already-guarded
    // machine profile's icon values to make a failed metadata refresh reversible.
    // All profile updates, including rollback, still go through the TSF APIs.
    HKEY key = nullptr;
    const auto path = TipKey() + L"\\LanguageProfile\\" + Hex(language) + L"\\" + Guid(GUID_CantoneseProfile);
    Checked(L"refresh_snapshot_icon_open_" + Hex(language), WinError(RegOpenKeyExW(HKEY_LOCAL_MACHINE,
        path.c_str(), 0, KEY_READ | KEY_WOW64_64KEY, &key)));
    try {
        DWORD type = 0, bytes = 0;
        LONG status = RegQueryValueExW(key, L"IconFile", nullptr, &type, nullptr, &bytes);
        if (status != ERROR_FILE_NOT_FOUND) {
            Require(status == ERROR_SUCCESS && type == REG_SZ, status ? WinError(status) : E_FAIL, L"Unexpected old icon file type");
            result.icon = ReadRegString(key, L"IconFile");
        }
        type = 0; bytes = sizeof(result.icon_index);
        status = RegQueryValueExW(key, L"IconIndex", nullptr, &type, reinterpret_cast<BYTE*>(&result.icon_index), &bytes);
        Require(status == ERROR_FILE_NOT_FOUND || (status == ERROR_SUCCESS && type == REG_DWORD && bytes == sizeof(result.icon_index)),
            status ? WinError(status) : E_FAIL, L"Unexpected old icon index type");
        if (status == ERROR_FILE_NOT_FOUND) result.icon_index = 0;
    } catch (...) { RegCloseKey(key); throw; }
    RegCloseKey(key);
    Log(L"refresh_snapshot_metadata_" + Hex(language), S_OK, result.description + L"; icon=" + result.icon + L"; enabled=" + (result.enabled ? L"true" : L"false"));
    return result;
}
HRESULT PreserveEnabled(ITfInputProcessorProfiles* profiles, const ProfileMetadata& prior) {
    BOOL enabled = FALSE;
    HRESULT result = profiles->IsEnabledLanguageProfile(CLSID_CantoneseIME, prior.language, GUID_CantoneseProfile, &enabled);
    if (SUCCEEDED(result) && (enabled != FALSE) != (prior.enabled != FALSE))
        result = profiles->EnableLanguageProfile(CLSID_CantoneseIME, prior.language, GUID_CantoneseProfile, prior.enabled);
    if (SUCCEEDED(result)) {
        result = profiles->IsEnabledLanguageProfile(CLSID_CantoneseIME, prior.language, GUID_CantoneseProfile, &enabled);
        if (SUCCEEDED(result) && (enabled != FALSE) != (prior.enabled != FALSE)) result = E_FAIL;
    }
    Log(L"refresh_preserve_enabled_" + Hex(prior.language), result, prior.enabled ? L"true" : L"false"); return result;
}
void VerifyProfileMetadata(LANGID language, const std::wstring& description, const std::wstring& icon, ULONG icon_index) {
    HKEY key = nullptr;
    const auto path = TipKey() + L"\\LanguageProfile\\" + Hex(language) + L"\\" + Guid(GUID_CantoneseProfile);
    Checked(L"refresh_verify_metadata_open_" + Hex(language), WinError(RegOpenKeyExW(HKEY_LOCAL_MACHINE,
        path.c_str(), 0, KEY_READ | KEY_WOW64_64KEY, &key)));
    try {
        Require(ReadRegString(key, L"Description") == description, E_FAIL, L"Persistent profile description differs from the requested metadata");
        std::wstring actual_icon;
        DWORD type = 0, bytes = 0;
        LONG status = RegQueryValueExW(key, L"IconFile", nullptr, &type, nullptr, &bytes);
        if (status != ERROR_FILE_NOT_FOUND) actual_icon = ReadRegString(key, L"IconFile");
        Require(Equal(actual_icon, icon), E_FAIL, L"Persistent profile icon path differs from the requested metadata");
        ULONG actual_index = 0; type = 0; bytes = sizeof(actual_index);
        status = RegQueryValueExW(key, L"IconIndex", nullptr, &type, reinterpret_cast<BYTE*>(&actual_index), &bytes);
        Require(status == ERROR_FILE_NOT_FOUND || (status == ERROR_SUCCESS && type == REG_DWORD && bytes == sizeof(actual_index)),
            status ? WinError(status) : E_FAIL, L"Unexpected persistent profile icon index type");
        Require(actual_index == icon_index, E_FAIL, L"Persistent profile icon index differs from the requested metadata");
    } catch (...) { RegCloseKey(key); throw; }
    RegCloseKey(key);
    Log(L"refresh_verified_metadata_" + Hex(language), S_OK, description + L"; icon=" + icon + L"; index=" + std::to_wstring(icon_index));
}
void VerifyRefreshedRegistration(const Plan& plan) {
    // RegisterCategory documents persistent registry membership. A category
    // manager that enumerated before that write need not refresh its process
    // view immediately. Verify both exact persistent indexes independently,
    // then record a newly obtained COM manager's observable view separately.
    Require(PersistentCategories().size() == kCategoryCount, E_FAIL, L"Persisted release category inventory is incomplete");
    Com<ITfInputProcessorProfileMgr> fresh_profiles; Com<ITfCategoryMgr> fresh_categories;
    Checked(L"refresh_verify_CoCreate_profiles", CoCreateInstance(CLSID_TF_InputProcessorProfiles, nullptr, CLSCTX_INPROC_SERVER,
        IID_ITfInputProcessorProfileMgr, reinterpret_cast<void**>(&fresh_profiles.value)));
    Checked(L"refresh_verify_CoCreate_categories", CoCreateInstance(CLSID_TF_CategoryMgr, nullptr, CLSCTX_INPROC_SERVER,
        IID_ITfCategoryMgr, reinterpret_cast<void**>(&fresh_categories.value)));
    Require(ExistingProfiles(fresh_profiles.value).size() == 2, E_FAIL, L"Refreshed profile identity inventory is incomplete");
    for (LANGID language : kLanguages) {
        TF_INPUTPROCESSORPROFILE profile{};
        Checked(L"refresh_verify_GetProfile_" + Hex(language), fresh_profiles.value->GetProfile(TF_PROFILETYPE_INPUTPROCESSOR,
            language, CLSID_CantoneseIME, GUID_CantoneseProfile, nullptr, &profile));
        Require(profile.dwProfileType == TF_PROFILETYPE_INPUTPROCESSOR && profile.langid == language &&
            IsEqualCLSID(profile.clsid, CLSID_CantoneseIME) && IsEqualGUID(profile.guidProfile, GUID_CantoneseProfile),
            E_FAIL, L"Refreshed persistent profile has an unexpected identity");
        VerifyProfileMetadata(language, kProductName, BrandingPath(plan), kBrandingIconLocation);
    }
    const auto observed = ExistingCategories(fresh_categories.value);
    Log(L"refresh_verify_COM_category_view", observed.size() == kCategoryCount ? S_OK : S_FALSE,
        std::to_wstring(observed.size()) + L" COM-visible category memberships; all persisted memberships and both directional indexes verified independently");
}
int RefreshRegistration(const Plan& plan, ITfInputProcessorProfileMgr* profiles, ITfCategoryMgr* categories) {
    const auto existing_profiles = ExistingProfiles(profiles);
    // Snapshot persisted membership before any write: a cached COM omission
    // must not make rollback remove a pre-existing category membership.
    ExistingCategories(categories);
    const auto existing_categories = PersistentCategories();
    Require(existing_profiles.size() == 2, E_ACCESSDENIED, L"Refresh requires both exact existing product language profiles");
    for (unsigned index = 0; index < 3; ++index)
        Require(std::find(existing_categories.begin(), existing_categories.end(), index) != existing_categories.end(), E_ACCESSDENIED, L"Refresh requires existing keyboard/display/UI categories");
    Com<ITfInputProcessorProfiles> legacy;
    Checked(L"refresh_CoCreate_legacy_profiles", CoCreateInstance(CLSID_TF_InputProcessorProfiles, nullptr, CLSCTX_INPROC_SERVER,
        IID_ITfInputProcessorProfiles, reinterpret_cast<void**>(&legacy.value)));
    std::vector<ProfileMetadata> metadata;
    for (LANGID language : kLanguages) metadata.push_back(SnapshotProfile(legacy.value, language));
    HKEY key = nullptr;
    Checked(L"refresh_COM_open", WinError(RegOpenKeyExW(HKEY_LOCAL_MACHINE, ClassKey().c_str(), 0,
        KEY_QUERY_VALUE | KEY_SET_VALUE | KEY_WOW64_64KEY, &key)));
    struct KeyCleanup { HKEY value; ~KeyCleanup() { RegCloseKey(value); } } close{key};
    const std::wstring old_name = ReadRegString(key, nullptr);
    const std::wstring old_binding = RemovalBinding(plan);
    bool rebound = false;
    std::vector<unsigned> added_categories;
    unsigned attempted_profiles = 0;
    try {
        for (const auto& prior : metadata) {
            ++attempted_profiles;
            Checked(L"refresh_RegisterProfile_" + Hex(prior.language), RegisterReleaseProfile(profiles, prior.language, plan));
            Checked(L"refresh_enabled_verified_" + Hex(prior.language), PreserveEnabled(legacy.value, prior));
        }
        for (unsigned index = 0; index < kCategoryCount; ++index) {
            if (std::find(existing_categories.begin(), existing_categories.end(), index) != existing_categories.end()) continue;
            added_categories.push_back(index);
            Checked(L"refresh_RegisterCategory_" + std::wstring(kCategoryNames[index]), categories->RegisterCategory(
                CLSID_CantoneseIME, *kCategories[index], CLSID_CantoneseIME));
        }
        if (!Equal(old_binding, plan.target)) { rebound = true; SetBinding(plan.target); }
        SetString(key, nullptr, kProductName);
        VerifyRefreshedRegistration(plan);
    } catch (...) {
        bool restored = true;
        if (rebound) {
            try { SetBinding(old_binding); } catch (...) { restored = false; Log(L"refresh_rollback_binding", E_FAIL, old_binding); }
        }
        for (unsigned i = 0; i < attempted_profiles; ++i) {
            const auto& prior = metadata[i];
            const HRESULT result = profiles->RegisterProfile(CLSID_CantoneseIME, prior.language, GUID_CantoneseProfile,
                prior.description.c_str(), static_cast<ULONG>(prior.description.size()), prior.icon.c_str(),
                static_cast<ULONG>(prior.icon.size()), prior.icon_index, nullptr, 0, FALSE, 0);
            Log(L"refresh_rollback_profile_" + Hex(prior.language), result); restored &= SUCCEEDED(result);
            restored &= SUCCEEDED(PreserveEnabled(legacy.value, prior));
            try { VerifyProfileMetadata(prior.language, prior.description, prior.icon, prior.icon_index); }
            catch (const Failure& failure) { restored = false; Log(L"refresh_rollback_metadata_verification_" + Hex(prior.language), failure.result, failure.detail); }
        }
        for (unsigned index : added_categories) {
            const HRESULT result = categories->UnregisterCategory(CLSID_CantoneseIME, *kCategories[index], CLSID_CantoneseIME);
            Log(L"refresh_rollback_category_" + std::wstring(kCategoryNames[index]), result); restored &= SUCCEEDED(result);
        }
        try {
            Require(PersistentCategories() == existing_categories, E_FAIL, L"Rollback persistent category memberships differ from their original snapshot");
            Log(L"refresh_rollback_category_verification", S_OK, L"Original persistent membership set restored");
        } catch (const Failure& failure) {
            restored = false; Log(L"refresh_rollback_category_verification", failure.result, failure.detail);
        }
        try { SetString(key, nullptr, old_name); } catch (...) { restored = false; }
        Log(L"refresh_rollback_summary", restored ? S_OK : E_FAIL, L"Previous metadata/enable flags restoration attempted through TSF; no profile unregistration/default setter. Files retained.");
        throw;
    }
    Log(L"refresh_complete", S_OK, L"Owned existing profiles refreshed with release name, separate protected helper EXE branding icon and system-tray/immersive categories; current-token enable flags preserved. No layout-list, activation or default API called.");
    return 0;
}
void InspectRefresh() {
    const HRESULT initialized = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED); Checked(L"refresh_inspect_CoInitializeEx", initialized);
    struct Cleanup { ~Cleanup() { CoUninitialize(); } } cleanup;
    Com<ITfInputProcessorProfileMgr> profiles; Com<ITfCategoryMgr> categories; Com<ITfInputProcessorProfiles> legacy;
    Checked(L"refresh_inspect_CoCreate_profiles", CoCreateInstance(CLSID_TF_InputProcessorProfiles, nullptr, CLSCTX_INPROC_SERVER,
        IID_ITfInputProcessorProfileMgr, reinterpret_cast<void**>(&profiles.value)));
    Checked(L"refresh_inspect_CoCreate_categories", CoCreateInstance(CLSID_TF_CategoryMgr, nullptr, CLSCTX_INPROC_SERVER,
        IID_ITfCategoryMgr, reinterpret_cast<void**>(&categories.value)));
    Checked(L"refresh_inspect_CoCreate_legacy", CoCreateInstance(CLSID_TF_InputProcessorProfiles, nullptr, CLSCTX_INPROC_SERVER,
        IID_ITfInputProcessorProfiles, reinterpret_cast<void**>(&legacy.value)));
    const auto inventory = ExistingProfiles(profiles.value);
    ExistingCategories(categories.value);
    const auto memberships = PersistentCategories();
    Require(inventory.size() == 2, E_ACCESSDENIED, L"Refresh requires both exact existing product language profiles");
    for (unsigned index = 0; index < 3; ++index)
        Require(std::find(memberships.begin(), memberships.end(), index) != memberships.end(), E_ACCESSDENIED,
            L"Refresh requires existing keyboard/display/UI categories");
    for (LANGID language : kLanguages) SnapshotProfile(legacy.value, language);
    Log(L"refresh_inspect_complete", S_OK, L"Exact existing profile/category inventory and old metadata are readable; no registration, enabling, activation or default change.");
}
int UserAction(const std::wstring& action, LANGID language, ITfInputProcessorProfileMgr* modern) {
    TF_INPUTPROCESSORPROFILE profile{};
    Checked(L"user_GetProfile", modern->GetProfile(TF_PROFILETYPE_INPUTPROCESSOR, language, CLSID_CantoneseIME, GUID_CantoneseProfile, nullptr, &profile));
    Require(profile.dwProfileType == TF_PROFILETYPE_INPUTPROCESSOR && profile.langid == language && IsEqualCLSID(profile.clsid, CLSID_CantoneseIME) && IsEqualGUID(profile.guidProfile, GUID_CantoneseProfile), E_FAIL, L"Wrong user-action profile identity");
    Log(L"user_profile_details", S_OK, L"language=" + Hex(profile.langid) + L" category=" + Guid(profile.catid) + L" flags=" + Hex(profile.dwFlags) + L" caps=" + Hex(profile.dwCaps));
    Com<ITfInputProcessorProfiles> legacy;
    Checked(L"user_CoCreate_legacy_profiles", CoCreateInstance(CLSID_TF_InputProcessorProfiles, nullptr, CLSCTX_INPROC_SERVER, IID_ITfInputProcessorProfiles, reinterpret_cast<void**>(&legacy.value)));
    BOOL before = FALSE; Checked(L"user_IsEnabled_before", legacy.value->IsEnabledLanguageProfile(CLSID_CantoneseIME, language, GUID_CantoneseProfile, &before), L"Result reported by following value");
    Log(L"user_enabled_before", S_OK, before ? L"true" : L"false");
    CLSID default_class{}; GUID default_profile{};
    const HRESULT default_result = legacy.value->GetDefaultLanguageProfile(language, GUID_TFCAT_TIP_KEYBOARD, &default_class, &default_profile);
    Log(L"user_default_before", default_result, Guid(default_class) + L" " + Guid(default_profile));
    if (action == L"inspect-user") { Log(L"inspect_user_complete", S_OK, L"Read-only query; no activation, enabling or default change"); return 0; }
    HMODULE input = LoadLibraryExW(L"input.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
    Require(input != nullptr, WinError(GetLastError()), L"Cannot load System32 input.dll");
    struct ModuleCleanup { HMODULE module; ~ModuleCleanup() { FreeLibrary(module); } } module_cleanup{input};
    using Install = BOOL (CALLBACK*)(LPCWSTR, DWORD);
    const auto install = reinterpret_cast<Install>(GetProcAddress(input, "InstallLayoutOrTip"));
    Require(install != nullptr, WinError(ERROR_PROC_NOT_FOUND), L"System32 InstallLayoutOrTip unavailable");
    const bool enable = action == L"enable-user";
    Checked(L"user_EnableLanguageProfile", legacy.value->EnableLanguageProfile(CLSID_CantoneseIME, language, GUID_CantoneseProfile, enable ? TRUE : FALSE));
    const std::wstring token = (language == 0x0409 ? L"0x0409:" : L"0x0C04:") + Guid(CLSID_CantoneseIME) + Guid(GUID_CantoneseProfile);
    // Only 0 (add) or 1 (uninstall). Never DEFPROFILE, DEFUSER or CLEANINSTALL.
    SetLastError(ERROR_SUCCESS); const BOOL result = install(token.c_str(), enable ? 0 : 1); const DWORD diagnostic_error = GetLastError();
    Log(L"user_InstallLayoutOrTip", result ? S_OK : E_FAIL, token + L" flags=" + Hex(enable ? 0 : 1) + L" BOOL=" + (result ? L"true" : L"false") + L" GetLastError(diagnostic only)=" + Hex(diagnostic_error));
    if (!result) {
        Log(L"user_restore_enabled_after_layout_failure", legacy.value->EnableLanguageProfile(CLSID_CantoneseIME, language, GUID_CantoneseProfile, before));
        throw Failure{E_FAIL, L"InstallLayoutOrTip failed; prior enabled flag restoration attempted. Inspect user list; no atomic layout-list rollback is claimed."};
    }
    BOOL after = FALSE; Checked(L"user_IsEnabled_after", legacy.value->IsEnabledLanguageProfile(CLSID_CantoneseIME, language, GUID_CantoneseProfile, &after));
    Log(L"user_enabled_after", S_OK, after ? L"true" : L"false");
    Require((after != FALSE) == enable, E_FAIL, L"Enabled state does not match request; inspect current user state");
    CLSID after_class{}; GUID after_profile{};
    const HRESULT after_default_result = legacy.value->GetDefaultLanguageProfile(language, GUID_TFCAT_TIP_KEYBOARD, &after_class, &after_profile);
    Log(L"user_default_after", after_default_result, Guid(after_class) + L" " + Guid(after_profile));
    if (SUCCEEDED(default_result) && SUCCEEDED(after_default_result))
        Log(L"user_default_comparison", IsEqualCLSID(default_class, after_class) && IsEqualGUID(default_profile, after_profile) ? S_OK : S_FALSE, L"No API to set a default is called; Windows may choose a fallback when disabling a default profile");
    Log(L"user_action_complete", S_OK, L"Current-user enabling only; not profile activation or proof of switcher/UI discovery"); return 0;
}
} // namespace

int wmain(int argc, wchar_t** argv) {
    try {
        std::wstring action = L"dry-run", manifest, expected_hash, user_language;
        std::map<std::wstring, bool> seen;
        for (int i = 1; i < argc; ++i) {
            Require(i + 1 < argc, E_INVALIDARG, L"Missing argument value"); const std::wstring key = argv[i], value = argv[++i];
            Require(seen.emplace(key, true).second, E_INVALIDARG, L"Duplicate argument");
            if (key == L"--action") action = value; else if (key == L"--manifest") manifest = value; else if (key == L"--manifest-sha256") expected_hash = value; else if (key == L"--language") user_language = value; else throw Failure{E_INVALIDARG, L"Unknown argument"};
        }
        const bool user = action == L"inspect-user" || action == L"enable-user" || action == L"disable-user";
        const bool refresh = action == L"refresh-registration" || action == L"dry-run-refresh";
        Require(action == L"dry-run" || action == L"dry-run-remove" || action == L"install" || action == L"remove" || refresh || user, E_INVALIDARG, L"Unknown action");
        Require(user ? (user_language == L"0409" || user_language == L"0c04") : user_language.empty(), E_INVALIDARG, L"User actions require --language 0409 or 0c04; machine actions forbid it");
        Require(!manifest.empty() && !expected_hash.empty(), E_INVALIDARG, L"Require --manifest absolute-path --manifest-sha256 digest");
        const bool dry = action == L"dry-run" || action == L"dry-run-remove" || action == L"dry-run-refresh", remove = action == L"remove" || action == L"dry-run-remove";
        Log(L"action", S_OK, action); const Plan plan = ReadPlan(manifest, expected_hash);
        const bool elevated = Elevated(); Log(L"token", S_OK, elevated ? L"elevated" : L"not_elevated");
        bool user_com_override = false, user_tip_preferences = false;
        for (const auto& pair : std::vector<std::pair<std::wstring, HKEY>>{{L"HKCU", HKEY_CURRENT_USER}, {L"HKLM", HKEY_LOCAL_MACHINE}}) {
            const bool com = Exists(pair.second, ClassKey()), tip = Exists(pair.second, TipKey());
            Log(pair.first + L"_COM", S_OK, com ? L"present" : L"absent"); Log(pair.first + L"_TIP", S_OK, tip ? L"present" : L"absent");
            if (pair.second == HKEY_CURRENT_USER) { user_com_override = com; user_tip_preferences = tip; }
            else if (!remove && !user && !refresh) Require(!com && !tip, WinError(ERROR_ALREADY_EXISTS), L"Install refuses any pre-existing machine namespace for this product");
        }
        // A read-only dry run may run before files are copied or HKCU COM is removed.
        Handle pf, root, native, dll, branding_directory, branding_executable; std::vector<Handle*> held;
        const DWORD target_attributes = GetFileAttributesW(plan.target.c_str());
        if (dry && target_attributes == INVALID_FILE_ATTRIBUTES && (GetLastError() == ERROR_FILE_NOT_FOUND || GetLastError() == ERROR_PATH_NOT_FOUND))
            Log(L"protected_target_pending", S_FALSE, L"Target not copied; actual installation will require protected owner/ACL, exact final path and matching SHA256");
        else TargetGuard(plan, held, pf, root, native, dll);
        if (remove || user || refresh) RemovalBinding(plan);
        if (user_com_override) Log(L"HKCU_override_pending", S_FALSE, L"Explicitly remove only the known current-user project COM binding before machine registration. Other users may retain independent bindings.");
        if (user_tip_preferences) GuardTipIdentities(HKEY_CURRENT_USER);
        if (dry) {
            if (refresh) InspectRefresh();
            Log(L"dry_run_complete", S_OK, L"No registry writes, file copies, enabling, activation or default changes. Actual action requires an elevated token and all pending guards to pass."); return 0;
        }
        Require(!user_com_override && (user ? !elevated : elevated), E_ACCESSDENIED, user ? L"User action requires a NON-elevated token and no HKCU COM override" : L"Actual machine action requires elevation and no current-token HKCU COM override");
        if (!user && !remove) BrandingGuard(plan, pf, branding_directory, branding_executable);
        const HRESULT initialized = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED); Checked(L"CoInitializeEx", initialized);
        struct CoCleanup { ~CoCleanup() { CoUninitialize(); } } cleanup;
        Com<ITfInputProcessorProfileMgr> profiles; Com<ITfCategoryMgr> categories;
        Checked(L"CoCreate_profile_manager", CoCreateInstance(CLSID_TF_InputProcessorProfiles, nullptr, CLSCTX_INPROC_SERVER, IID_ITfInputProcessorProfileMgr, reinterpret_cast<void**>(&profiles.value)));
        if (user) { GuardTipIdentities(); return UserAction(action, user_language == L"0409" ? 0x0409 : 0x0c04, profiles.value); }
        Checked(L"CoCreate_category_manager", CoCreateInstance(CLSID_TF_CategoryMgr, nullptr, CLSCTX_INPROC_SERVER, IID_ITfCategoryMgr, reinterpret_cast<void**>(&categories.value)));
        if (refresh) return RefreshRegistration(plan, profiles.value, categories.value);
        if (remove) {
            // Exact fixed profiles only; never use TF_URP_ALLPROFILES or a direct TIP delete.
            bool success = true;
            const auto existing_profiles = ExistingProfiles(profiles.value);
            ExistingCategories(categories.value); // Diagnostic view only; it may predate a registration change.
            const auto existing_categories = PersistentCategories();
            for (unsigned i : existing_categories) { const HRESULT hr = categories.value->UnregisterCategory(CLSID_CantoneseIME, *kCategories[i], CLSID_CantoneseIME); Log(L"remove_category_" + std::wstring(kCategoryNames[i]), hr); success &= SUCCEEDED(hr); }
            for (LANGID language : existing_profiles) { const HRESULT hr = profiles.value->UnregisterProfile(CLSID_CantoneseIME, language, GUID_CantoneseProfile, 0); Log(L"remove_profile_" + Hex(language), hr); success &= SUCCEEDED(hr); }
            Require(success, E_FAIL, L"A profile/category removal failed; COM and files retained for inspection/retry");
            Require(ExistingProfiles(profiles.value).empty() && PersistentCategories().empty(), E_FAIL, L"Owned persistent profiles/categories still present; COM retained");
            Require(!Exists(HKEY_LOCAL_MACHINE, TipKey()), E_FAIL, L"Machine TIP namespace remains (possibly an unexpected profile); COM retained rather than deleting a live service binding");
            Checked(L"remove_HKLM_COM", DeleteCom()); Log(L"remove_complete", S_OK, L"User enabling and package files require separate reviewed cleanup; no default changed"); return 0;
        }
        bool com_created = false; std::vector<LANGID> attempted; std::vector<unsigned> registered;
        try {
            CreateCom(plan, com_created);
            for (LANGID language : kLanguages) {
                attempted.push_back(language);
                Checked(L"RegisterProfile_" + Hex(language), RegisterReleaseProfile(profiles.value, language, plan));
            }
            for (unsigned i = 0; i < kCategoryCount; ++i) { registered.push_back(i); Checked(L"RegisterCategory_" + std::wstring(kCategoryNames[i]), categories.value->RegisterCategory(CLSID_CantoneseIME, *kCategories[i], CLSID_CantoneseIME)); }
            for (LANGID language : kLanguages) {
                TF_INPUTPROCESSORPROFILE profile{}; Checked(L"GetProfile_after_" + Hex(language), profiles.value->GetProfile(TF_PROFILETYPE_INPUTPROCESSOR, language, CLSID_CantoneseIME, GUID_CantoneseProfile, nullptr, &profile), L"Persistent discovery query");
                Require(profile.dwProfileType == TF_PROFILETYPE_INPUTPROCESSOR && profile.langid == language && IsEqualCLSID(profile.clsid, CLSID_CantoneseIME) && IsEqualGUID(profile.guidProfile, GUID_CantoneseProfile), E_FAIL, L"Persistent discovery returned unexpected profile identity");
                VerifyProfileMetadata(language, kProductName, BrandingPath(plan), kBrandingIconLocation);
            }
        } catch (...) { Rollback(profiles.value, categories.value, com_created, attempted, registered); throw; }
        Log(L"install_complete", S_OK, L"Machine COM/profiles/categories only. Enable selected profile in the intended non-elevated account separately; default unchanged."); return 0;
    } catch (const Failure& failure) { Log(L"failed", failure.result, failure.detail); return 2; }
      catch (const std::exception& exception) { Log(L"exception", E_FAIL, Wide(exception.what())); return 2; }
      catch (...) { Log(L"exception", E_FAIL, L"Unknown exception"); return 2; }
}
