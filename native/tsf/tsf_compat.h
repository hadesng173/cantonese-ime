#pragma once
#include <msctf.h>
#include <ctfutb.h>

// ABI-only declarations missing from LLVM-MinGW's msctf.h. Signatures, order
// and interface UUIDs are checked against Microsoft's generated SDK header:
// https://github.com/microsoft/win32metadata/blob/main/generation/WinSDK/RecompiledIdlHeaders/um/msctf.h
// No implementation or SampleIME source is included here. Existing complete
// SDK interfaces take precedence through their standard MIDL guards.

#ifndef __ITfTextInputProcessorEx_INTERFACE_DEFINED__
#define __ITfTextInputProcessorEx_INTERFACE_DEFINED__
EXTERN_C const IID IID_ITfTextInputProcessorEx;
MIDL_INTERFACE("6e4e2102-f9cd-433d-b496-303ce03a6507")
ITfTextInputProcessorEx : public ITfTextInputProcessor {
    virtual HRESULT STDMETHODCALLTYPE ActivateEx(ITfThreadMgr*, TfClientId, DWORD) = 0;
};
#endif

#ifndef __ITfTextLayoutSink_INTERFACE_DEFINED__
#define __ITfTextLayoutSink_INTERFACE_DEFINED__
typedef enum { TF_LC_CREATE = 0, TF_LC_CHANGE = 1, TF_LC_DESTROY = 2 } TfLayoutCode;
EXTERN_C const IID IID_ITfTextLayoutSink;
MIDL_INTERFACE("2af2d06a-dd5b-4927-a0b4-54f19c91fade")
ITfTextLayoutSink : public IUnknown {
    virtual HRESULT STDMETHODCALLTYPE OnLayoutChange(ITfContext*, TfLayoutCode, ITfContextView*) = 0;
};
#endif

#ifndef __ITfDisplayAttributeProvider_INTERFACE_DEFINED__
#define __ITfDisplayAttributeProvider_INTERFACE_DEFINED__
EXTERN_C const IID IID_ITfDisplayAttributeProvider;
MIDL_INTERFACE("fee47777-163c-4769-996a-6e9c50ad8f54")
ITfDisplayAttributeProvider : public IUnknown {
    virtual HRESULT STDMETHODCALLTYPE EnumDisplayAttributeInfo(IEnumTfDisplayAttributeInfo**) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetDisplayAttributeInfo(REFGUID, ITfDisplayAttributeInfo**) = 0;
};
#endif

#ifndef __ITfCandidateListUIElement_INTERFACE_DEFINED__
#define __ITfCandidateListUIElement_INTERFACE_DEFINED__
EXTERN_C const IID IID_ITfCandidateListUIElement;
MIDL_INTERFACE("ea1ea138-19df-11d7-a6d2-00065b84435c")
ITfCandidateListUIElement : public ITfUIElement {
    virtual HRESULT STDMETHODCALLTYPE GetUpdatedFlags(DWORD*) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetDocumentMgr(ITfDocumentMgr**) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetCount(UINT*) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetSelection(UINT*) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetString(UINT, BSTR*) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetPageIndex(UINT*, UINT, UINT*) = 0;
    virtual HRESULT STDMETHODCALLTYPE SetPageIndex(UINT*, UINT) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetCurrentPage(UINT*) = 0;
};
#endif

#ifndef __ITfCandidateListUIElementBehavior_INTERFACE_DEFINED__
#define __ITfCandidateListUIElementBehavior_INTERFACE_DEFINED__
EXTERN_C const IID IID_ITfCandidateListUIElementBehavior;
MIDL_INTERFACE("85fad185-58ce-497a-9460-355366b64b9a")
ITfCandidateListUIElementBehavior : public ITfCandidateListUIElement {
    virtual HRESULT STDMETHODCALLTYPE SetSelection(UINT) = 0;
    virtual HRESULT STDMETHODCALLTYPE Finalize() = 0;
    virtual HRESULT STDMETHODCALLTYPE Abort() = 0;
};
#endif

// Declaration only; the GUID value is supplied by the Windows UUID import
// library when available. The fallback definitions live in tsf_guids.cpp.
EXTERN_C const GUID GUID_TFCAT_TIPCAP_UIELEMENTENABLED;
EXTERN_C const GUID GUID_TFCAT_TIPCAP_SYSTRAYSUPPORT;
EXTERN_C const GUID GUID_TFCAT_TIPCAP_IMMERSIVESUPPORT;
EXTERN_C const GUID GUID_TFCAT_TIPCAP_COMLESS;
EXTERN_C const GUID GUID_LBI_INPUTMODE;

#ifndef __ITfLangBarItemButton_INTERFACE_DEFINED__
#define __ITfLangBarItemButton_INTERFACE_DEFINED__
// ctfutb.h SDK ABI; LLVM-MinGW currently supplies ITfLangBarItem only.
struct ITfMenu;
typedef enum { TF_LBI_CLK_RIGHT = 1, TF_LBI_CLK_LEFT = 2 } TfLBIClick;
EXTERN_C const IID IID_ITfLangBarItemButton;
MIDL_INTERFACE("28c7f1d0-de25-11d2-afdd-00105a2799b5")
ITfLangBarItemButton : public ITfLangBarItem {
    virtual HRESULT STDMETHODCALLTYPE OnClick(TfLBIClick, POINT, const RECT*) = 0;
    virtual HRESULT STDMETHODCALLTYPE InitMenu(ITfMenu*) = 0;
    virtual HRESULT STDMETHODCALLTYPE OnMenuSelect(UINT) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetIcon(HICON*) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetText(BSTR*) = 0;
};
#endif

#ifndef TF_LBI_ICON
#define TF_LBI_ICON 0x00000001
#define TF_LBI_TEXT 0x00000002
#define TF_LBI_TOOLTIP 0x00000004
#define TF_LBI_STATUS 0x00010000
#define TF_LBI_STYLE_SHOWNINTRAY 0x00000002
#define TF_LBI_STYLE_TEXTCOLORICON 0x00000020
#define TF_LBI_STYLE_BTN_BUTTON 0x00010000
#define TF_LBI_STATUS_HIDDEN 0x00000001
#define TF_LBI_STATUS_DISABLED 0x00000002
#endif

#ifndef TF_INVALID_GUIDATOM
#define TF_INVALID_GUIDATOM ((TfGuidAtom)0)
#endif
#ifndef TF_CLIENTID_NULL
#define TF_CLIENTID_NULL ((TfClientId)0)
#endif
#ifndef TF_RP_LOCALPROCESS
#define TF_RP_LOCALPROCESS 0x00000004
#endif
#ifndef TF_URP_LOCALPROCESS
#define TF_URP_LOCALPROCESS 0x00000004
#endif
#ifndef TF_IPPMF_FORPROCESS
#define TF_IPPMF_FORPROCESS 0x10000000
#endif
#ifndef TF_CLUIE_DOCUMENTMGR
#define TF_CLUIE_DOCUMENTMGR 0x00000001
#define TF_CLUIE_COUNT 0x00000002
#define TF_CLUIE_SELECTION 0x00000004
#define TF_CLUIE_STRING 0x00000008
#define TF_CLUIE_PAGEINDEX 0x00000010
#define TF_CLUIE_CURRENTPAGE 0x00000020
#endif
