// Registration is invoked explicitly by orchestration, never by DllMain.
#include <windows.h>
#include <msctf.h>
#include "tsf_compat.h"
#include <string>
#include "ime_ids.h"

namespace {
class ComScope {
public:
    ComScope() : result(CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED)) {}
    ~ComScope() { if (SUCCEEDED(result)) CoUninitialize(); }
    HRESULT result;
};
template<class T> class ReleaseOnExit {
public:
    ~ReleaseOnExit() { if (value) value->Release(); }
    T* value = nullptr;
};
const GUID* const kCategories[] = {
    &GUID_TFCAT_TIP_KEYBOARD,
    &GUID_TFCAT_DISPLAYATTRIBUTEPROVIDER,
    &GUID_TFCAT_TIPCAP_UIELEMENTENABLED,
    &GUID_TFCAT_TIPCAP_SYSTRAYSUPPORT,
    &GUID_TFCAT_TIPCAP_IMMERSIVESUPPORT,
    &GUID_TFCAT_TIPCAP_COMLESS,
};
std::wstring ClassKey() {
    wchar_t value[64]{};
    StringFromGUID2(CLSID_CantoneseIME, value, 64);
    return std::wstring(L"Software\\Classes\\CLSID\\") + value;
}
HRESULT SetString(HKEY root, const std::wstring& path, const wchar_t* name, const std::wstring& value) {
    HKEY key = nullptr;
    LONG status = RegCreateKeyExW(root, path.c_str(), 0, nullptr, REG_OPTION_NON_VOLATILE, KEY_SET_VALUE, nullptr, &key, nullptr);
    if (status != ERROR_SUCCESS) return HRESULT_FROM_WIN32(status);
    status = RegSetValueExW(key, name, 0, REG_SZ, reinterpret_cast<const BYTE*>(value.c_str()), static_cast<DWORD>((value.size() + 1) * sizeof(wchar_t)));
    RegCloseKey(key);
    return HRESULT_FROM_WIN32(status);
}
HRESULT ModulePath(std::wstring& path) {
    HMODULE module = nullptr;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
        reinterpret_cast<LPCWSTR>(&ModulePath), &module)) return HRESULT_FROM_WIN32(GetLastError());
    std::wstring buffer(32768, L'\0');
    const DWORD length = GetModuleFileNameW(module, buffer.data(), static_cast<DWORD>(buffer.size()));
    if (!length || length >= buffer.size()) return HRESULT_FROM_WIN32(length ? ERROR_INSUFFICIENT_BUFFER : GetLastError());
    buffer.resize(length); path = std::move(buffer); return S_OK;
}
HRESULT RemoveService(ITfInputProcessorProfiles* profiles, ITfCategoryMgr* categories) {
    HRESULT first = S_OK;
    for (const GUID* category : kCategories) {
        const HRESULT status = categories->UnregisterCategory(CLSID_CantoneseIME, *category, CLSID_CantoneseIME);
        if (FAILED(status) && SUCCEEDED(first)) first = status;
    }
    profiles->RemoveLanguageProfile(CLSID_CantoneseIME, kCantoneseLanguage, GUID_CantoneseProfile);
    const HRESULT status = profiles->Unregister(CLSID_CantoneseIME);
    return FAILED(status) && SUCCEEDED(first) ? status : first;
}
}  // namespace

extern "C" HRESULT WINAPI DllRegisterServer() {
    try {
        ComScope com;
        if (FAILED(com.result) && com.result != RPC_E_CHANGED_MODE) return com.result;
        ReleaseOnExit<ITfInputProcessorProfiles> profiles;
        ReleaseOnExit<ITfCategoryMgr> categories;
        HRESULT result = CoCreateInstance(CLSID_TF_InputProcessorProfiles, nullptr, CLSCTX_INPROC_SERVER,
            IID_ITfInputProcessorProfiles, reinterpret_cast<void**>(&profiles.value));
        if (FAILED(result)) return result;
        result = CoCreateInstance(CLSID_TF_CategoryMgr, nullptr, CLSCTX_INPROC_SERVER,
            IID_ITfCategoryMgr, reinterpret_cast<void**>(&categories.value));
        if (FAILED(result)) return result;
        std::wstring path;
        result = ModulePath(path);
        if (FAILED(result)) return result;
        const auto key = ClassKey();
        // Do not overwrite a previous install or another binary's registration.
        HKEY existing = nullptr;
        const LONG present = RegOpenKeyExW(HKEY_CURRENT_USER, key.c_str(), 0, KEY_READ, &existing);
        if (present == ERROR_SUCCESS) { RegCloseKey(existing); return HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS); }
        if (present != ERROR_FILE_NOT_FOUND) return HRESULT_FROM_WIN32(present);
        result = SetString(HKEY_CURRENT_USER, key, nullptr, kProductName);
        if (SUCCEEDED(result)) result = SetString(HKEY_CURRENT_USER, key + L"\\InprocServer32", nullptr, path);
        if (SUCCEEDED(result)) result = SetString(HKEY_CURRENT_USER, key + L"\\InprocServer32", L"ThreadingModel", L"Apartment");
        bool profile_registered = false;
        if (SUCCEEDED(result)) { result = profiles.value->Register(CLSID_CantoneseIME); profile_registered = SUCCEEDED(result); }
        if (SUCCEEDED(result)) result = profiles.value->AddLanguageProfile(CLSID_CantoneseIME, kCantoneseLanguage,
            GUID_CantoneseProfile, kProductName, static_cast<ULONG>(std::wstring(kProductName).size()), path.c_str(), static_cast<ULONG>(path.size()), 0);
        if (SUCCEEDED(result)) {
            for (const GUID* category : kCategories) {
                result = categories.value->RegisterCategory(CLSID_CantoneseIME, *category, CLSID_CantoneseIME);
                if (FAILED(result)) break;
            }
        }
        if (SUCCEEDED(result)) result = profiles.value->EnableLanguageProfile(CLSID_CantoneseIME, kCantoneseLanguage, GUID_CantoneseProfile, TRUE);
        if (FAILED(result)) {
            if (profile_registered) RemoveService(profiles.value, categories.value);
            RegDeleteTreeW(HKEY_CURRENT_USER, key.c_str());
        }
        return result;
    } catch (...) { return E_FAIL; }
}

extern "C" HRESULT WINAPI DllUnregisterServer() {
    try {
        ComScope com;
        if (FAILED(com.result) && com.result != RPC_E_CHANGED_MODE) return com.result;
        ReleaseOnExit<ITfInputProcessorProfiles> profiles;
        ReleaseOnExit<ITfCategoryMgr> categories;
        HRESULT result = CoCreateInstance(CLSID_TF_InputProcessorProfiles, nullptr, CLSCTX_INPROC_SERVER,
            IID_ITfInputProcessorProfiles, reinterpret_cast<void**>(&profiles.value));
        if (FAILED(result)) return result;
        result = CoCreateInstance(CLSID_TF_CategoryMgr, nullptr, CLSCTX_INPROC_SERVER,
            IID_ITfCategoryMgr, reinterpret_cast<void**>(&categories.value));
        if (FAILED(result)) return result;
        const HRESULT removed = RemoveService(profiles.value, categories.value);
        const LONG registry = RegDeleteTreeW(HKEY_CURRENT_USER, ClassKey().c_str());
        if (registry != ERROR_SUCCESS && registry != ERROR_FILE_NOT_FOUND) return HRESULT_FROM_WIN32(registry);
        return removed;
    } catch (...) { return E_FAIL; }
}
