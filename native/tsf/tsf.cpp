// Project-authored TSF frontend. No Microsoft SampleIME source is incorporated.
#include <windows.h>
#include <msctf.h>
#include "tsf_compat.h"
#include <inputscope.h>
#include <oleauto.h>
#include <algorithm>
#include <cstdint>
#include <memory>
#include <new>
#include <set>
#include <string>
#include <utility>
#include <vector>
#include "ime_ids.h"
#include "ipc.h"
#include "candidate_layout.h"
#include "candidate_font.h"
#include "candidate_appearance.h"
#include "input_mode_button.h"

namespace {
HINSTANCE g_instance = nullptr;
volatile LONG g_objects = 0;
volatile LONG g_locks = 0;
constexpr wchar_t kWindowClass[] = L"CantoneseIME.Candidates";

template<class T> class Ptr {
public:
    Ptr() = default;
    explicit Ptr(T* value) : value_(value) { if (value_) value_->AddRef(); }
    Ptr(const Ptr& other) : Ptr(other.value_) {}
    Ptr(Ptr&& other) noexcept : value_(other.Detach()) {}
    ~Ptr() { Reset(); }
    Ptr& operator=(const Ptr& other) { if (this != &other) { Ptr copy(other); Swap(copy); } return *this; }
    Ptr& operator=(Ptr&& other) noexcept { if (this != &other) { Reset(); value_ = other.Detach(); } return *this; }
    T* Get() const { return value_; }
    T* operator->() const { return value_; }
    explicit operator bool() const { return value_ != nullptr; }
    T** Put() { Reset(); return &value_; }
    void Attach(T* value) { Reset(); value_ = value; }
    T* Detach() { T* value = value_; value_ = nullptr; return value; }
    void Reset() { if (value_) { T* old = value_; value_ = nullptr; old->Release(); } }
    void Swap(Ptr& other) { std::swap(value_, other.value_); }
private:
    T* value_ = nullptr;
};

class Counted {
protected:
    Counted() { InterlockedIncrement(&g_objects); }
    virtual ~Counted() { InterlockedDecrement(&g_objects); }
    ULONG Retain() { return static_cast<ULONG>(InterlockedIncrement(&references_)); }
    ULONG Drop() { const ULONG count = static_cast<ULONG>(InterlockedDecrement(&references_)); if (!count) delete this; return count; }
private:
    volatile LONG references_ = 1;
};
#define REF_METHODS \
    ULONG STDMETHODCALLTYPE AddRef() override { return Retain(); } \
    ULONG STDMETHODCALLTYPE Release() override { return Drop(); }

struct SelectionMemory {
    std::wstring raw;
    cime::Candidate candidate;
    bool strict = false;
    // The typed letters as an English word: committed with a trailing space,
    // remembered by the broker, and not part of a suggestion sequence.
    bool English() const { return candidate.reading.empty() && !raw.empty() && candidate.text == raw; }
};

struct LearningTask {
    SelectionMemory selection;
    LearningTask* next = nullptr;
    // A consecutive-commit record for learned suggestions instead of a selection.
    std::wstring previous, following;
};
// One queue/worker per loaded process. These are POD globals, so DLL detach
// never joins a worker or destroys a static queue under the loader lock.
SRWLOCK g_learning_lock = SRWLOCK_INIT;
LearningTask* g_learning_head = nullptr;
LearningTask* g_learning_tail = nullptr;
unsigned g_pending_learning = 0;  // Queued plus the one in-flight request; under lock.
bool g_learning_worker = false;

class LearningLock {
public:
    LearningLock() { AcquireSRWLockExclusive(&g_learning_lock); }
    ~LearningLock() { Unlock(); }
    void Unlock() { if (held_) { ReleaseSRWLockExclusive(&g_learning_lock); held_ = false; } }
private:
    bool held_ = true;
};

DWORD WINAPI SaveSelection(void* parameter) {
    const HMODULE module = static_cast<HMODULE>(parameter);
    for (;;) {
        LearningTask* next = nullptr;
        AcquireSRWLockExclusive(&g_learning_lock);
        if (g_learning_head) {
            next = g_learning_head; g_learning_head = next->next;
            if (!g_learning_head) g_learning_tail = nullptr;
        } else {
            // A producer can start a new worker after this transition. Its new
            // module pin is independent, and this worker has completed all I/O.
            g_learning_worker = false;
        }
        ReleaseSRWLockExclusive(&g_learning_lock);
        if (!next) {
            InterlockedDecrement(&g_locks);
            // No owning C++ object remains live here. Release the final worker
            // module reference and exit atomically rather than returning into it.
            FreeLibraryAndExitThread(module, 0);
        }
        {
            std::unique_ptr<LearningTask> task(next);
            try {
                cime::Backend backend;
                // Exactly one attempt, with the transport's existing 250 ms
                // deadline. Never retry a possibly already-persisted request.
                if (!task->previous.empty()) backend.RecordSequence(task->previous, task->following);
                else backend.RecordSelection(task->selection.raw, task->selection.strict, task->selection.candidate);
            } catch (...) { /* Preference storage must not affect application input. */ }
        }
        AcquireSRWLockExclusive(&g_learning_lock);
        --g_pending_learning;
        ReleaseSRWLockExclusive(&g_learning_lock);
    }
}

void QueueLearning(LearningTask entry) noexcept;
void QueueSelectionLearning(const SelectionMemory& selection) noexcept {
    if (selection.raw.empty()) return;
    try { QueueLearning(LearningTask{selection, nullptr, {}, {}}); } catch (...) { }
}
void QueueSequenceLearning(const std::wstring& previous, const std::wstring& following) noexcept {
    if (previous.empty() || following.empty()) return;
    try { QueueLearning(LearningTask{{}, nullptr, previous, following}); } catch (...) { }
}
void QueueLearning(LearningTask entry) noexcept {
    try {
        LearningLock lock;
        // Drop only the new optional preference if the broker is unavailable
        // and 32 records are already queued/in flight. Application input stays
        // committed. Enqueue order is linearized under this process's SRW lock.
        if (g_pending_learning >= 32) return;
        auto task = std::make_unique<LearningTask>(std::move(entry));
        HANDLE thread = nullptr;
        if (!g_learning_worker) {
            HMODULE module = nullptr;
            if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS,
                    reinterpret_cast<LPCWSTR>(g_instance), &module)) return;
            InterlockedIncrement(&g_locks);
            // The new worker waits on the held queue lock until its first item
            // is linked. Nothing can throw after successful thread creation.
            thread = CreateThread(nullptr, 0, SaveSelection, module, 0, nullptr);
            if (!thread) {
                InterlockedDecrement(&g_locks);
                lock.Unlock(); FreeLibrary(module); return;
            }
            g_learning_worker = true;
        }
        LearningTask* item = task.release();
        if (g_learning_tail) g_learning_tail->next = item; else g_learning_head = item;
        g_learning_tail = item; ++g_pending_learning;
        lock.Unlock();
        if (thread) CloseHandle(thread);  // No wait/join on the TSF input thread.
    } catch (...) { }
}

class TextService;
class CandidateElement final : public ITfCandidateListUIElementBehavior, public Counted {
public:
    explicit CandidateElement(TextService* owner) : owner_(owner) {}
    void Detach() { owner_ = nullptr; }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID, void**) override;
    REF_METHODS
    HRESULT STDMETHODCALLTYPE GetDescription(BSTR*) override;
    HRESULT STDMETHODCALLTYPE GetGUID(GUID*) override;
    HRESULT STDMETHODCALLTYPE Show(BOOL) override;
    HRESULT STDMETHODCALLTYPE IsShown(BOOL*) override;
    HRESULT STDMETHODCALLTYPE GetUpdatedFlags(DWORD*) override;
    HRESULT STDMETHODCALLTYPE GetDocumentMgr(ITfDocumentMgr**) override;
    HRESULT STDMETHODCALLTYPE GetCount(UINT*) override;
    HRESULT STDMETHODCALLTYPE GetSelection(UINT*) override;
    HRESULT STDMETHODCALLTYPE GetString(UINT, BSTR*) override;
    HRESULT STDMETHODCALLTYPE GetPageIndex(UINT*, UINT, UINT*) override;
    HRESULT STDMETHODCALLTYPE SetPageIndex(UINT*, UINT) override;
    HRESULT STDMETHODCALLTYPE GetCurrentPage(UINT*) override;
    HRESULT STDMETHODCALLTYPE SetSelection(UINT) override;
    HRESULT STDMETHODCALLTYPE Finalize() override;
    HRESULT STDMETHODCALLTYPE Abort() override;
private:
    TextService* owner_;
};

class AttributeInfo final : public ITfDisplayAttributeInfo, public Counted {
public:
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** result) override {
        if (!result) return E_POINTER;
        *result = nullptr;
        if (iid != IID_IUnknown && iid != IID_ITfDisplayAttributeInfo) return E_NOINTERFACE;
        *result = static_cast<ITfDisplayAttributeInfo*>(this); AddRef(); return S_OK;
    }
    REF_METHODS
    HRESULT STDMETHODCALLTYPE GetGUID(GUID* value) override { if (!value) return E_POINTER; *value = GUID_CantoneseDisplay; return S_OK; }
    HRESULT STDMETHODCALLTYPE GetDescription(BSTR* value) override { if (!value) return E_POINTER; *value = SysAllocString(L"Cantonese phonetic composition"); return *value ? S_OK : E_OUTOFMEMORY; }
    HRESULT STDMETHODCALLTYPE GetAttributeInfo(TF_DISPLAYATTRIBUTE* value) override {
        if (!value) return E_POINTER;
        *value = {}; value->crText.type = TF_CT_NONE; value->crBk.type = TF_CT_NONE;
        value->lsStyle = TF_LS_SOLID; value->fBoldLine = FALSE;
        value->crLine.type = TF_CT_COLORREF; value->crLine.cr = RGB(30, 90, 170); value->bAttr = TF_ATTR_INPUT;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE SetAttributeInfo(const TF_DISPLAYATTRIBUTE*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE Reset() override { return S_OK; }
};
class AttributeEnum final : public IEnumTfDisplayAttributeInfo, public Counted {
public:
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** result) override {
        if (!result) return E_POINTER;
        *result = nullptr;
        if (iid != IID_IUnknown && iid != IID_IEnumTfDisplayAttributeInfo) return E_NOINTERFACE;
        *result = static_cast<IEnumTfDisplayAttributeInfo*>(this); AddRef(); return S_OK;
    }
    REF_METHODS
    HRESULT STDMETHODCALLTYPE Clone(IEnumTfDisplayAttributeInfo** result) override {
        if (!result) return E_POINTER;
        auto* copy = new (std::nothrow) AttributeEnum;
        if (!copy) return E_OUTOFMEMORY;
        copy->done_ = done_; *result = copy; return S_OK;
    }
    HRESULT STDMETHODCALLTYPE Next(ULONG count, ITfDisplayAttributeInfo** items, ULONG* fetched) override {
        if (!items || (!fetched && count != 1)) return E_POINTER;
        if (fetched) *fetched = 0;
        if (!count) return S_OK;
        if (done_) return S_FALSE;
        items[0] = new (std::nothrow) AttributeInfo;
        if (!items[0]) return E_OUTOFMEMORY;
        done_ = true; if (fetched) *fetched = 1; return count == 1 ? S_OK : S_FALSE;
    }
    HRESULT STDMETHODCALLTYPE Reset() override { done_ = false; return S_OK; }
    HRESULT STDMETHODCALLTYPE Skip(ULONG count) override { if (!count) return S_OK; const bool prior = done_; done_ = true; return !prior && count == 1 ? S_OK : S_FALSE; }
private:
    bool done_ = false;
};

// Insert: commit a suggestion at the caret without a composition.
enum class EditKind { CheckInput, Render, Position, Insert };
class EditSession final : public ITfEditSession, public Counted {
public:
    EditSession(TextService*, ITfContext*, EditKind, std::wstring, bool, std::uint64_t, SelectionMemory = {});
    ~EditSession() override;
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** result) override {
        if (!result) return E_POINTER;
        *result = nullptr;
        if (iid != IID_IUnknown && iid != IID_ITfEditSession) return E_NOINTERFACE;
        *result = static_cast<ITfEditSession*>(this); AddRef(); return S_OK;
    }
    REF_METHODS
    HRESULT STDMETHODCALLTYPE DoEditSession(TfEditCookie) override;
    bool allowed = false;
private:
    TextService* owner_;
    Ptr<ITfContext> context_;
    EditKind kind_;
    std::wstring text_;
    bool finish_;
    std::uint64_t generation_;
    SelectionMemory selection_;
};
class DetachedFinish final : public ITfEditSession, public Counted {
public:
    DetachedFinish(ITfComposition* composition, std::wstring text) : composition_(composition), text_(std::move(text)) {}
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** result) override {
        if (!result) return E_POINTER;
        *result = nullptr;
        if (iid != IID_IUnknown && iid != IID_ITfEditSession) return E_NOINTERFACE;
        *result = static_cast<ITfEditSession*>(this); AddRef(); return S_OK;
    }
    REF_METHODS
    HRESULT STDMETHODCALLTYPE DoEditSession(TfEditCookie cookie) override {
        Ptr<ITfRange> range;
        HRESULT result = composition_->GetRange(range.Put());
        if (SUCCEEDED(result)) result = range->SetText(cookie, 0, text_.c_str(), static_cast<LONG>(text_.size()));
        const HRESULT ended = composition_->EndComposition(cookie);
        return FAILED(result) ? result : ended;
    }
private:
    Ptr<ITfComposition> composition_;
    std::wstring text_;
};

class TextService final : public ITfTextInputProcessorEx, public ITfKeyEventSink,
    public ITfCompositionSink, public ITfThreadMgrEventSink, public ITfTextLayoutSink,
    public ITfDisplayAttributeProvider, public cime::ui::InputModeOwner, public Counted {
public:
    ~TextService() override { Deactivate(); }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID, void**) override;
    REF_METHODS
    HRESULT STDMETHODCALLTYPE Activate(ITfThreadMgr* manager, TfClientId client) override { return ActivateEx(manager, client, 0); }
    HRESULT STDMETHODCALLTYPE ActivateEx(ITfThreadMgr*, TfClientId, DWORD) override;
    HRESULT STDMETHODCALLTYPE Deactivate() override;
    HRESULT STDMETHODCALLTYPE OnSetFocus(BOOL foreground) override { if (!foreground) DetachComposition(); return S_OK; }
    HRESULT STDMETHODCALLTYPE OnTestKeyDown(ITfContext*, WPARAM, LPARAM, BOOL*) override;
    HRESULT STDMETHODCALLTYPE OnKeyDown(ITfContext*, WPARAM, LPARAM, BOOL*) override;
    HRESULT STDMETHODCALLTYPE OnTestKeyUp(ITfContext*, WPARAM key, LPARAM, BOOL* eaten) override { if (!eaten) return E_POINTER; *eaten = eaten_keys_.count(key) ? TRUE : FALSE; return S_OK; }
    HRESULT STDMETHODCALLTYPE OnKeyUp(ITfContext*, WPARAM key, LPARAM, BOOL* eaten) override { if (!eaten) return E_POINTER; *eaten = eaten_keys_.erase(key) ? TRUE : FALSE; return S_OK; }
    HRESULT STDMETHODCALLTYPE OnPreservedKey(ITfContext*, REFGUID, BOOL* eaten) override { if (!eaten) return E_POINTER; *eaten = FALSE; return S_OK; }
    HRESULT STDMETHODCALLTYPE OnCompositionTerminated(TfEditCookie, ITfComposition* composition) override { if (!ending_ && composition_.Get() == composition) ClearState(); return S_OK; }
    HRESULT STDMETHODCALLTYPE OnInitDocumentMgr(ITfDocumentMgr*) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnUninitDocumentMgr(ITfDocumentMgr*) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnSetFocus(ITfDocumentMgr* focused, ITfDocumentMgr*) override {
        if (context_) { Ptr<ITfContext> top; if (!focused || FAILED(focused->GetTop(top.Put())) || top.Get() != context_.Get()) DetachComposition(); }
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnPushContext(ITfContext*) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnPopContext(ITfContext* context) override { if (context_.Get() == context) DetachComposition(); return S_OK; }
    HRESULT STDMETHODCALLTYPE OnLayoutChange(ITfContext* context, TfLayoutCode code, ITfContextView*) override {
        if (context_.Get() == context) { if (code == TF_LC_DESTROY) DetachComposition(); else Request(context, EditKind::Position, L"", false, false); }
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE EnumDisplayAttributeInfo(IEnumTfDisplayAttributeInfo** value) override { if (!value) return E_POINTER; *value = new (std::nothrow) AttributeEnum; return *value ? S_OK : E_OUTOFMEMORY; }
    HRESULT STDMETHODCALLTYPE GetDisplayAttributeInfo(REFGUID guid, ITfDisplayAttributeInfo** value) override {
        if (!value) return E_POINTER;
        *value = nullptr; if (guid != GUID_CantoneseDisplay) return E_INVALIDARG;
        *value = new (std::nothrow) AttributeInfo; return *value ? S_OK : E_OUTOFMEMORY;
    }
    HRESULT DoEdit(ITfContext*, TfEditCookie, EditKind, const std::wstring&, bool, std::uint64_t, bool&, const SelectionMemory&);
    const cime::Page& CurrentPage() const { return page_; }
    UINT Selection() const { return selected_; }
    bool Visible() const { return window_ && IsWindowVisible(window_); }
    void ShowCandidates(bool show) { show_own_ui_ = show; UpdateWindowState(); }
    HRESULT DocumentManager(ITfDocumentMgr** result) { if (!result) return E_POINTER; *result = nullptr; return context_ ? context_->GetDocumentMgr(result) : E_FAIL; }
    HRESULT Select(UINT, bool synchronous = true);
    HRESULT Finish(bool cancel, bool synchronous = true);
    HRESULT CommitLatin();  // The typed letters plus a space, as Space does in English.
    HRESULT Highlight(UINT index) { if (index >= page_.candidates.size()) return E_INVALIDARG; selected_ = index; UpdateCandidates(); return S_OK; }
    bool EnglishMode() const override { return english_; }
    HRESULT ToggleInputMode() override {
        // A taskbar click occurs outside a key edit session. End the borrowed
        // composition with the existing asynchronous focus-change path.
        DetachComposition(); SetEnglishMode(!english_); return S_OK;
    }
    HRESULT ShowAppearanceMenu(POINT point) override;
private:
    void SetEnglishMode(bool english) { english_ = english; if (mode_button_) mode_button_->ModeChanged(); }
    // Learned next-word suggestions. A "sequence" is the last committed text
    // that the next commit may follow; anything else typed breaks it.
    bool PredictionEnabled() { return candidate_appearance_.Current().prediction; }
    void AfterCommit(ITfContext* context, const std::wstring& text);
    void StartPrediction();
    void EndPrediction();
    void BreakSequence() { last_commit_.clear(); ++prediction_ticket_; }
    void PassThroughKey(WPARAM key);
    HRESULT CommitPrediction(UINT index, bool synchronous);
    bool EnsureWindow(int x, int y);
    bool Enabled(ITfContext*) const;
    bool WantsKey(ITfContext*, WPARAM) const;
    bool CheckInput(ITfContext*);
    HRESULT Request(ITfContext*, EditKind, const std::wstring&, bool finish, bool synchronous, SelectionMemory = {});
    void ClearState();
    void DetachComposition();
    void AdviseLayout(ITfContext*);
    void UnadviseLayout();
    void UpdatePosition(ITfContext*, TfEditCookie, ITfRange*);
    void UpdateCandidates();
    void EndCandidates();
    void UpdateWindowState();
    bool LoadPage(std::uint32_t, bool forward);
    void MoveSelection(int);
    static LRESULT CALLBACK WindowProcedure(HWND, UINT, WPARAM, LPARAM);
    void Paint(HWND);
    HFONT CandidateFont(HDC dc);
    int CandidateFontHeight();
    std::wstring CandidateLabel(std::size_t index) const;
    static constexpr UINT kReflowCandidates = WM_APP + 0x4c1;
    Ptr<ITfThreadMgr> manager_;
    Ptr<ITfKeystrokeMgr> keystrokes_;
    Ptr<ITfSource> manager_source_;
    Ptr<ITfSource> context_source_;
    Ptr<ITfContext> context_;
    Ptr<ITfComposition> composition_;
    Ptr<ITfUIElementMgr> ui_manager_;
    Ptr<ITfLangBarItemMgr> language_bar_;
    Ptr<cime::ui::InputModeButton> mode_button_;
    Ptr<CandidateElement> ui_element_;
    TfClientId client_ = TF_CLIENTID_NULL;
    TfGuidAtom attribute_ = TF_INVALID_GUIDATOM;
    DWORD manager_cookie_ = TF_INVALID_COOKIE;
    DWORD layout_cookie_ = TF_INVALID_COOKIE;
    DWORD element_id_ = 0;
    bool element_started_ = false;
    bool key_sink_advised_ = false;
    bool ending_ = false;
    bool english_ = false;
    bool strict_ = false;
    bool show_own_ui_ = true;
    HWND window_ = nullptr;
    RECT caret_{0, 0, 0, 0};
    unsigned candidate_dpi_ = 96;
    cime::ui::Layout candidate_layout_;
    cime::ui::PrivateCandidateFont private_candidate_font_;
    cime::ui::CandidateAppearance candidate_appearance_;
    std::wstring raw_;
    cime::Page page_;
    cime::Backend backend_;
    UINT selected_ = 0;
    std::uint32_t offset_ = 0;
    std::vector<std::uint32_t> previous_offsets_;
    std::set<WPARAM> eaten_keys_;
    std::uint64_t generation_ = 0;
    static constexpr UINT kStartPrediction = WM_APP + 0x4c2;
    static constexpr ULONGLONG kSequenceTimeoutMs = 20000;
    bool predicting_ = false;          // page_ holds suggestions, not a composition's candidates.
    std::wstring last_commit_;         // Previous commit a following one may extend.
    ULONGLONG last_commit_tick_ = 0;
    std::uint64_t prediction_ticket_ = 0;  // Invalidates a posted StartPrediction.
    Ptr<ITfContext> pending_context_;
    std::wstring pending_text_;
};

EditSession::EditSession(TextService* owner, ITfContext* context, EditKind kind, std::wstring text,
    bool finish, std::uint64_t generation, SelectionMemory selection) : owner_(owner), context_(context), kind_(kind),
    text_(std::move(text)), finish_(finish), generation_(generation), selection_(std::move(selection)) { owner_->AddRef(); }
EditSession::~EditSession() { owner_->Release(); }
HRESULT EditSession::DoEditSession(TfEditCookie cookie) {
    try { return owner_->DoEdit(context_.Get(), cookie, kind_, text_, finish_, generation_, allowed, selection_); }
    catch (...) { return E_FAIL; }
}

HRESULT TextService::QueryInterface(REFIID iid, void** result) {
    if (!result) return E_POINTER;
    *result = nullptr;
    if (iid == IID_IUnknown || iid == IID_ITfTextInputProcessor || iid == IID_ITfTextInputProcessorEx) *result = static_cast<ITfTextInputProcessorEx*>(this);
    else if (iid == IID_ITfKeyEventSink) *result = static_cast<ITfKeyEventSink*>(this);
    else if (iid == IID_ITfCompositionSink) *result = static_cast<ITfCompositionSink*>(this);
    else if (iid == IID_ITfThreadMgrEventSink) *result = static_cast<ITfThreadMgrEventSink*>(this);
    else if (iid == IID_ITfTextLayoutSink) *result = static_cast<ITfTextLayoutSink*>(this);
    else if (iid == IID_ITfDisplayAttributeProvider) *result = static_cast<ITfDisplayAttributeProvider*>(this);
    else return E_NOINTERFACE;
    AddRef(); return S_OK;
}

HRESULT TextService::ActivateEx(ITfThreadMgr* manager, TfClientId client, DWORD) {
    if (!manager) return E_INVALIDARG;
    if (manager_) return E_UNEXPECTED;
    candidate_appearance_.Load();
    {
        // The settings EXE is installed beside this DLL in <root>\native.
        wchar_t module[MAX_PATH]{};
        const DWORD length = GetModuleFileNameW(g_instance, module, MAX_PATH);
        std::wstring path(module, length < MAX_PATH ? length : 0);
        const auto slash = path.find_last_of(L'\\');
        if (slash != std::wstring::npos) candidate_appearance_.SetSettingsExecutable(path.substr(0, slash + 1) + L"CantoneseImeSettings.exe");
    }
    manager_ = Ptr<ITfThreadMgr>(manager); client_ = client;
    HRESULT result = manager_->QueryInterface(IID_ITfKeystrokeMgr, reinterpret_cast<void**>(keystrokes_.Put()));
    if (SUCCEEDED(result)) result = keystrokes_->AdviseKeyEventSink(client_, static_cast<ITfKeyEventSink*>(this), TRUE);
    if (FAILED(result)) { Deactivate(); return result; }
    key_sink_advised_ = true;
    result = manager_->QueryInterface(IID_ITfLangBarItemMgr, reinterpret_cast<void**>(language_bar_.Put()));
    if (SUCCEEDED(result)) {
        mode_button_.Attach(new (std::nothrow) cime::ui::InputModeButton(g_instance, this, &g_objects));
        result = mode_button_ ? language_bar_->AddItem(mode_button_.Get()) : E_OUTOFMEMORY;
    }
    // Some TSF hosts do not expose a language bar. Composition remains usable
    // there; Windows can fall back to its language abbreviation for that host.
    if (FAILED(result)) {
        if (mode_button_) mode_button_->Detach();
        mode_button_.Reset(); language_bar_.Reset();
    }
    if (SUCCEEDED(manager_->QueryInterface(IID_ITfSource, reinterpret_cast<void**>(manager_source_.Put()))))
        manager_source_->AdviseSink(IID_ITfThreadMgrEventSink, static_cast<ITfThreadMgrEventSink*>(this), &manager_cookie_);
    manager_->QueryInterface(IID_ITfUIElementMgr, reinterpret_cast<void**>(ui_manager_.Put()));
    Ptr<ITfCategoryMgr> categories;
    if (SUCCEEDED(CoCreateInstance(CLSID_TF_CategoryMgr, nullptr, CLSCTX_INPROC_SERVER, IID_ITfCategoryMgr, reinterpret_cast<void**>(categories.Put()))))
        categories->RegisterGUID(GUID_CantoneseDisplay, &attribute_);
    return S_OK;
}
HRESULT TextService::ShowAppearanceMenu(POINT point) {
    // TrackPopupMenu runs a nested message loop. Keep the service alive even
    // if a host deactivates the input processor while that loop is running.
    AddRef();
    HRESULT result = S_OK;
    try {
        if (candidate_appearance_.ShowMenu(window_, point)) {
            UpdateWindowState();
            if (window_) InvalidateRect(window_, nullptr, FALSE);
        }
    } catch (...) { result = E_FAIL; }
    Release();
    return result;
}
HRESULT TextService::Deactivate() {
    if (mode_button_) {
        mode_button_->Detach();
        if (language_bar_) language_bar_->RemoveItem(mode_button_.Get());
        mode_button_.Reset();
    }
    language_bar_.Reset();
    DetachComposition();
    if (key_sink_advised_ && keystrokes_) keystrokes_->UnadviseKeyEventSink(client_);
    key_sink_advised_ = false;
    if (manager_source_ && manager_cookie_ != TF_INVALID_COOKIE) manager_source_->UnadviseSink(manager_cookie_);
    manager_cookie_ = TF_INVALID_COOKIE;
    manager_source_.Reset(); keystrokes_.Reset(); ui_manager_.Reset(); manager_.Reset();
    client_ = TF_CLIENTID_NULL; eaten_keys_.clear();
    if (window_) { DestroyWindow(window_); window_ = nullptr; }
    private_candidate_font_.Reset();
    return S_OK;
}

bool CompartmentDisabled(IUnknown* object, REFGUID guid) {
    Ptr<ITfCompartmentMgr> manager;
    if (!object || FAILED(object->QueryInterface(IID_ITfCompartmentMgr, reinterpret_cast<void**>(manager.Put())))) return false;
    Ptr<ITfCompartment> compartment;
    if (FAILED(manager->GetCompartment(guid, compartment.Put()))) return false;
    VARIANT value; VariantInit(&value);
    const bool disabled = SUCCEEDED(compartment->GetValue(&value)) && value.vt == VT_I4 && value.lVal != 0;
    VariantClear(&value); return disabled;
}
bool TextService::Enabled(ITfContext* context) const {
    if (!context || !manager_) return false;
    TF_STATUS status{};
    if (FAILED(context->GetStatus(&status)) || (status.dwDynamicFlags & TF_SD_READONLY)) return false;
    return !CompartmentDisabled(context, GUID_COMPARTMENT_KEYBOARD_DISABLED) && !CompartmentDisabled(context, GUID_COMPARTMENT_EMPTYCONTEXT);
}
// The syllable separator is whichever key types an apostrophe in the active
// layout (VK_OEM_7 on US, VK_OEM_3 on UK), not a fixed virtual key.
static bool SeparatorKey(WPARAM key) {
    const UINT typed = MapVirtualKeyExW(static_cast<UINT>(key), MAPVK_VK_TO_CHAR, GetKeyboardLayout(0)) & 0x7FFFFFFF;
    return typed ? typed == L'\'' : key == VK_OEM_7;
}
bool TextService::WantsKey(ITfContext* context, WPARAM key) const {
    if (!Enabled(context)) return false;
    const bool control = (GetKeyState(VK_CONTROL) & 0x8000) != 0;
    const bool alt = (GetKeyState(VK_MENU) & 0x8000) != 0;
    if (control && !alt && key == VK_SPACE) return true;
    if (english_ || control || alt || (GetKeyState(VK_LWIN) & 0x8000) || (GetKeyState(VK_RWIN) & 0x8000)) return false;
    const bool shift = (GetKeyState(VK_SHIFT) & 0x8000) != 0;
    if (shift && key == VK_SPACE && !raw_.empty()) return true;  // Shift+Space commits the letters as English.
    if (shift || (GetKeyState(VK_CAPITAL) & 1)) return false;
    if (key >= 'A' && key <= 'Z') return true;
    // Showing suggestions: only Escape and a listed digit belong to the IME;
    // everything else reaches the application and dismisses them.
    if (predicting_ && raw_.empty())
        return key == VK_ESCAPE || (key >= '1' && key <= '9' && static_cast<std::size_t>(key - '1') < page_.candidates.size());
    if (raw_.empty()) return false;
    return SeparatorKey(key) || key == VK_BACK || key == VK_SPACE || key == VK_RETURN || key == VK_ESCAPE ||
        key == VK_UP || key == VK_DOWN || key == VK_PRIOR || key == VK_NEXT || (key >= '1' && key <= '9');
}
bool TextService::CheckInput(ITfContext* context) {
    Ptr<EditSession> session;
    session.Attach(new (std::nothrow) EditSession(this, context, EditKind::CheckInput, L"", false, generation_));
    if (!session) return false;
    HRESULT outcome = E_FAIL;
    const HRESULT result = context->RequestEditSession(client_, session.Get(), TF_ES_SYNC | TF_ES_READ, &outcome);
    return SUCCEEDED(result) && SUCCEEDED(outcome) && session->allowed;
}
HRESULT TextService::OnTestKeyDown(ITfContext* context, WPARAM key, LPARAM, BOOL* eaten) {
    if (!eaten) return E_POINTER;
    *eaten = WantsKey(context, key) ? TRUE : FALSE;
    if (!*eaten) PassThroughKey(key);
    return S_OK;
}
void TextService::PassThroughKey(WPARAM key) {
    switch (key) {
    case VK_SHIFT: case VK_LSHIFT: case VK_RSHIFT: case VK_CONTROL: case VK_LCONTROL: case VK_RCONTROL:
    case VK_MENU: case VK_LMENU: case VK_RMENU: case VK_LWIN: case VK_RWIN: case VK_CAPITAL:
        return;  // A modifier alone neither dismisses suggestions nor breaks a sequence.
    default:
        if (predicting_) EndPrediction();
        BreakSequence();
    }
}

HRESULT TextService::OnKeyDown(ITfContext* context, WPARAM key, LPARAM, BOOL* eaten) {
    if (!eaten) return E_POINTER;
    *eaten = FALSE;
    try {
        if (!WantsKey(context, key)) { PassThroughKey(key); return S_OK; }
        if (!CheckInput(context)) return S_OK;
        ++prediction_ticket_;
        if (predicting_) {
            if (context_.Get() != context) { EndPrediction(); BreakSequence(); return S_OK; }
            if (key == VK_ESCAPE) { EndPrediction(); eaten_keys_.insert(key); *eaten = TRUE; return S_OK; }
            if (key >= '1' && key <= '9') {
                *eaten = SUCCEEDED(CommitPrediction(static_cast<UINT>(key - '1'), true));
                if (*eaten) eaten_keys_.insert(key);
                return S_OK;
            }
            EndPrediction();  // A letter starts a new composition; the sequence continues.
        }
        if ((GetKeyState(VK_CONTROL) & 0x8000) && key == VK_SPACE) {
            if (!raw_.empty() && FAILED(Finish(false))) return S_OK;
            SetEnglishMode(!english_); BreakSequence(); *eaten = TRUE;
        } else if (context_ && context_.Get() != context) {
            DetachComposition(); return S_OK;
        } else if (key == VK_ESCAPE || key == VK_RETURN) {
            if (SUCCEEDED(Finish(key == VK_ESCAPE))) *eaten = TRUE;
        } else if (key == VK_SPACE) {
            // With no candidate, or with Shift, the letters were an English word.
            if (page_.candidates.empty() || (GetKeyState(VK_SHIFT) & 0x8000)) *eaten = SUCCEEDED(CommitLatin());
            else *eaten = SUCCEEDED(Select(selected_));
        } else if (key >= '1' && key <= '9') {
            const UINT index = static_cast<UINT>(key - '1');
            *eaten = index < page_.candidates.size() ? SUCCEEDED(Select(index)) : TRUE;
        } else if (key == VK_UP || key == VK_DOWN) {
            MoveSelection(key == VK_UP ? -1 : 1); *eaten = TRUE;
        } else if (key == VK_NEXT || key == VK_PRIOR) {
            if (key == VK_NEXT && page_.next_offset >= 0) LoadPage(static_cast<std::uint32_t>(page_.next_offset), true);
            if (key == VK_PRIOR && !previous_offsets_.empty()) LoadPage(previous_offsets_.back(), false);
            *eaten = TRUE;
        } else {
            std::wstring next = raw_;
            if (key == VK_BACK) { if (!next.empty()) next.pop_back(); }
            else if (next.size() >= cime::kMaxInputLength) *eaten = TRUE;
            else next.push_back(SeparatorKey(key) ? L'\'' : static_cast<wchar_t>(key - 'A' + L'a'));
            if (!*eaten) {
                cime::Page proposed;
                const bool available = next.empty() || backend_.Lookup(next, strict_, 0, proposed);
                // A stopped engine must not capture a new ordinary Latin word.
                if (!available && raw_.empty()) return S_OK;
                const HRESULT result = Request(context, EditKind::Render, next, next.empty(), true);
                if (SUCCEEDED(result)) {
                    raw_ = std::move(next); page_ = std::move(proposed); selected_ = 0; offset_ = 0; previous_offsets_.clear();
                    if (raw_.empty()) ClearState(); else UpdateCandidates();
                    *eaten = TRUE;
                }
            }
        }
        if (*eaten) eaten_keys_.insert(key);
        return S_OK;
    } catch (...) { return E_FAIL; }
}

HRESULT TextService::Request(ITfContext* context, EditKind kind, const std::wstring& text, bool finish, bool synchronous, SelectionMemory selection) {
    if (!context || client_ == TF_CLIENTID_NULL) return E_UNEXPECTED;
    Ptr<EditSession> session;
    session.Attach(new (std::nothrow) EditSession(this, context, kind, text, finish, generation_, std::move(selection)));
    if (!session) return E_OUTOFMEMORY;
    HRESULT outcome = E_FAIL;
    const DWORD flags = (synchronous ? TF_ES_SYNC : TF_ES_ASYNCDONTCARE) |
        (kind == EditKind::Render || kind == EditKind::Insert ? TF_ES_READWRITE : TF_ES_READ);
    const HRESULT requested = context->RequestEditSession(client_, session.Get(), flags, &outcome);
    return FAILED(requested) ? requested : outcome;
}

HRESULT TextService::DoEdit(ITfContext* context, TfEditCookie cookie, EditKind kind,
    const std::wstring& text, bool finish, std::uint64_t generation, bool& allowed, const SelectionMemory& selection_memory) {
    if (generation != generation_) return E_ABORT;
    if (kind == EditKind::CheckInput) {
        allowed = Enabled(context);
        TF_SELECTION selection{}; ULONG fetched = 0;
        if (allowed && SUCCEEDED(context->GetSelection(cookie, TF_DEFAULT_SELECTION, 1, &selection, &fetched)) && fetched) {
            Ptr<ITfReadOnlyProperty> property;
            if (SUCCEEDED(context->GetAppProperty(GUID_PROP_INPUTSCOPE, property.Put()))) {
                VARIANT value; VariantInit(&value);
                if (SUCCEEDED(property->GetValue(cookie, selection.range, &value)) && value.vt == VT_UNKNOWN && value.punkVal) {
                    Ptr<ITfInputScope> scopes;
                    if (SUCCEEDED(value.punkVal->QueryInterface(IID_ITfInputScope, reinterpret_cast<void**>(scopes.Put())))) {
                        InputScope* items = nullptr; UINT count = 0;
                        if (SUCCEEDED(scopes->GetInputScopes(&items, &count))) {
                            for (UINT i = 0; i < count; ++i) if (items[i] == IS_PASSWORD) allowed = false;
                            CoTaskMemFree(items);
                        }
                    }
                }
                VariantClear(&value);
            }
            selection.range->Release();
        }
        return S_OK;
    }
    if (kind == EditKind::Position) {
        if (composition_) { Ptr<ITfRange> range; if (SUCCEEDED(composition_->GetRange(range.Put()))) UpdatePosition(context, cookie, range.Get()); }
        return S_OK;
    }
    if (kind == EditKind::Insert) {
        // A chosen suggestion is ordinary committed text at the caret.
        if (composition_ || context_.Get() != context) return E_UNEXPECTED;
        Ptr<ITfInsertAtSelection> inserter;
        HRESULT result = context->QueryInterface(IID_ITfInsertAtSelection, reinterpret_cast<void**>(inserter.Put()));
        if (FAILED(result)) return result;
        Ptr<ITfRange> range;
        result = inserter->InsertTextAtSelection(cookie, 0, text.c_str(), static_cast<LONG>(text.size()), range.Put());
        if (FAILED(result) || !range) return FAILED(result) ? result : E_FAIL;
        range->Collapse(cookie, TF_ANCHOR_END);
        TF_SELECTION caret{}; caret.range = range.Get(); caret.style.ase = TF_AE_NONE; caret.style.fInterimChar = FALSE;
        context->SetSelection(cookie, 1, &caret);
        UpdatePosition(context, cookie, range.Get());
        ClearState();
        AfterCommit(context, text);
        return S_OK;
    }
    if (!composition_) {
        TF_SELECTION selection{}; ULONG fetched = 0;
        HRESULT result = context->GetSelection(cookie, TF_DEFAULT_SELECTION, 1, &selection, &fetched);
        if (FAILED(result) || !fetched) return FAILED(result) ? result : E_FAIL;
        Ptr<ITfRange> range; range.Attach(selection.range);
        Ptr<ITfContextComposition> compositions;
        result = context->QueryInterface(IID_ITfContextComposition, reinterpret_cast<void**>(compositions.Put()));
        if (FAILED(result)) return result;
        result = compositions->StartComposition(cookie, range.Get(), static_cast<ITfCompositionSink*>(this), composition_.Put());
        if (FAILED(result) || !composition_) return FAILED(result) ? result : E_FAIL;
        context_ = Ptr<ITfContext>(context); AdviseLayout(context);
    }
    if (context_.Get() != context) return E_UNEXPECTED;
    Ptr<ITfRange> range;
    HRESULT result = composition_->GetRange(range.Put());
    if (FAILED(result)) return result;
    result = range->SetText(cookie, 0, text.c_str(), static_cast<LONG>(text.size()));
    if (FAILED(result)) return result;
    Ptr<ITfProperty> attribute;
    if (SUCCEEDED(context->GetProperty(GUID_PROP_ATTRIBUTE, attribute.Put()))) {
        if (finish) attribute->Clear(cookie, range.Get());
        else if (attribute_ != TF_INVALID_GUIDATOM) {
            VARIANT value; VariantInit(&value); value.vt = VT_I4; value.lVal = static_cast<LONG>(attribute_);
            attribute->SetValue(cookie, range.Get(), &value);
        }
    }
    Ptr<ITfRange> caret;
    if (SUCCEEDED(range->Clone(caret.Put()))) {
        caret->Collapse(cookie, TF_ANCHOR_END);
        TF_SELECTION selection{}; selection.range = caret.Get(); selection.style.ase = TF_AE_NONE; selection.style.fInterimChar = FALSE;
        context->SetSelection(cookie, 1, &selection);
        UpdatePosition(context, cookie, caret.Get());
    }
    if (finish) {
        ending_ = true;
        result = composition_->EndComposition(cookie);
        ending_ = false;
        if (SUCCEEDED(result)) {
            ClearState();
            // Only a completed candidate commit reaches this point. Rendering,
            // cancellation, literal Enter and stale/failed edits never learn.
            QueueSelectionLearning(selection_memory);
            if (!selection_memory.raw.empty() && !selection_memory.English()) AfterCommit(context, selection_memory.candidate.text);
            else BreakSequence();  // Latin text or a cancel is not part of a sequence.
        }
    }
    return result;
}

void TextService::AdviseLayout(ITfContext* context) {
    UnadviseLayout();
    if (SUCCEEDED(context->QueryInterface(IID_ITfSource, reinterpret_cast<void**>(context_source_.Put()))))
        context_source_->AdviseSink(IID_ITfTextLayoutSink, static_cast<ITfTextLayoutSink*>(this), &layout_cookie_);
}
void TextService::UnadviseLayout() {
    if (context_source_ && layout_cookie_ != TF_INVALID_COOKIE) context_source_->UnadviseSink(layout_cookie_);
    layout_cookie_ = TF_INVALID_COOKIE; context_source_.Reset();
}
void TextService::ClearState() {
    ++generation_; predicting_ = false; EndCandidates(); UnadviseLayout(); composition_.Reset(); context_.Reset();
    raw_.clear(); page_ = {}; selected_ = 0; offset_ = 0; previous_offsets_.clear();
}
void TextService::DetachComposition() {
    if (context_ && composition_) {
        Ptr<DetachedFinish> edit;
        edit.Attach(new (std::nothrow) DetachedFinish(composition_.Get(), raw_));
        if (edit) { HRESULT outcome = E_FAIL; context_->RequestEditSession(client_, edit.Get(), TF_ES_ASYNCDONTCARE | TF_ES_READWRITE, &outcome); }
    }
    ClearState();
    BreakSequence();  // Focus moved: the next commit does not follow the last one.
}
HRESULT TextService::Finish(bool cancel, bool synchronous) {
    if (!context_ || !composition_) return S_FALSE;
    return Request(context_.Get(), EditKind::Render, cancel ? L"" : raw_, true, synchronous);
}
HRESULT TextService::CommitLatin() {
    if (!context_ || !composition_) return S_FALSE;
    SelectionMemory selection{raw_, cime::Candidate{raw_, L""}, strict_};
    return Request(context_.Get(), EditKind::Render, raw_ + L" ", true, true, std::move(selection));
}
HRESULT TextService::Select(UINT index, bool synchronous) {
    if (predicting_) return CommitPrediction(index, synchronous);
    if (!context_ || index >= page_.candidates.size()) return E_INVALIDARG;
    SelectionMemory selection{raw_, page_.candidates[index], strict_};
    const std::wstring chosen = selection.English() ? selection.candidate.text + L" " : selection.candidate.text;
    return Request(context_.Get(), EditKind::Render, chosen, true, synchronous, std::move(selection));
}
bool TextService::LoadPage(std::uint32_t offset, bool forward) {
    cime::Page next;
    if (!backend_.Lookup(raw_, strict_, offset, next)) { page_.error = next.error; UpdateCandidates(); return false; }
    if (forward) previous_offsets_.push_back(offset_); else if (!previous_offsets_.empty()) previous_offsets_.pop_back();
    offset_ = offset; page_ = std::move(next); selected_ = 0; UpdateCandidates(); return true;
}
void TextService::MoveSelection(int direction) {
    if (direction > 0) {
        if (selected_ + 1 < page_.candidates.size()) ++selected_;
        else if (page_.next_offset >= 0) { LoadPage(static_cast<std::uint32_t>(page_.next_offset), true); return; }
    } else if (selected_) --selected_;
    else if (!previous_offsets_.empty()) {
        if (LoadPage(previous_offsets_.back(), false) && !page_.candidates.empty()) selected_ = static_cast<UINT>(page_.candidates.size() - 1);
    }
    UpdateCandidates();
}

void TextService::UpdatePosition(ITfContext* context, TfEditCookie cookie, ITfRange* range) {
    Ptr<ITfContextView> view;
    BOOL clipped = FALSE; RECT rect{};
    if (SUCCEEDED(context->GetActiveView(view.Put())) && SUCCEEDED(view->GetTextExt(cookie, range, &rect, &clipped)) &&
        (rect.left || rect.top || rect.right || rect.bottom)) caret_ = rect;
    UpdateWindowState();
}
void TextService::UpdateCandidates() {
    if (!ui_element_) ui_element_.Attach(new (std::nothrow) CandidateElement(this));
    if (!element_started_ && ui_manager_ && ui_element_) {
        BOOL show = TRUE;
        if (SUCCEEDED(ui_manager_->BeginUIElement(ui_element_.Get(), &show, &element_id_))) {
            element_started_ = true; show_own_ui_ = show != FALSE;
        }
    } else if (element_started_ && ui_manager_) ui_manager_->UpdateUIElement(element_id_);
    UpdateWindowState();
}
void TextService::EndCandidates() {
    // A host may call back into a UI element while ending it. Disconnect its
    // non-owning service pointer before making that external COM call.
    if (ui_element_) ui_element_->Detach();
    if (element_started_ && ui_manager_) ui_manager_->EndUIElement(element_id_);
    element_started_ = false;
    ui_element_.Reset();
    if (window_) ShowWindow(window_, SW_HIDE);
    show_own_ui_ = true;
}
bool TextService::EnsureWindow(int x, int y) {
    if (!window_) {
        WNDCLASSEXW type{}; type.cbSize = sizeof(type); type.lpfnWndProc = WindowProcedure;
        type.hInstance = g_instance; type.hCursor = LoadCursorW(nullptr, IDC_ARROW); type.lpszClassName = kWindowClass;
        RegisterClassExW(&type);
        window_ = CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, kWindowClass, kProductName,
            WS_POPUP, x, y, 1, 1, nullptr, nullptr, g_instance, this);
    }
    return window_ != nullptr;
}
void TextService::AfterCommit(ITfContext* context, const std::wstring& text) {
    // Runs inside the commit's edit session: record and schedule only. The
    // broker query and UI update happen afterwards from a posted message.
    if (!PredictionEnabled() || text.empty()) { BreakSequence(); return; }
    const ULONGLONG now = GetTickCount64();
    if (!last_commit_.empty() && now - last_commit_tick_ <= kSequenceTimeoutMs) QueueSequenceLearning(last_commit_, text);
    last_commit_ = text; last_commit_tick_ = now;
    pending_context_ = Ptr<ITfContext>(context); pending_text_ = text;
    const auto ticket = ++prediction_ticket_;
    if (EnsureWindow(caret_.left, caret_.bottom)) PostMessageW(window_, kStartPrediction, static_cast<WPARAM>(ticket), 0);
}
void TextService::StartPrediction() {
    Ptr<ITfContext> context = std::move(pending_context_);
    if (!context || !raw_.empty() || composition_ || english_ || !PredictionEnabled() || !Enabled(context.Get())) return;
    cime::Page page;
    if (!backend_.Predict(pending_text_, page) || page.candidates.empty()) return;
    predicting_ = true; context_ = std::move(context); page_ = std::move(page);
    selected_ = 0; offset_ = 0; previous_offsets_.clear();
    UpdateCandidates();
}
void TextService::EndPrediction() {
    if (predicting_) ClearState();
}
HRESULT TextService::CommitPrediction(UINT index, bool synchronous) {
    if (!predicting_ || !context_ || index >= page_.candidates.size()) return E_INVALIDARG;
    const std::wstring text = page_.candidates[index].text;
    return Request(context_.Get(), EditKind::Insert, text, false, synchronous);
}
void TextService::UpdateWindowState() {
    if ((raw_.empty() && !predicting_) || page_.candidates.empty() || !show_own_ui_) {
        candidate_layout_ = {};
        if (window_) ShowWindow(window_, SW_HIDE);
        return;
    }
    cime::ui::Rect anchor{caret_.left, caret_.top, caret_.right, caret_.bottom};
    if (!caret_.left && !caret_.top && !caret_.right && !caret_.bottom) {
        POINT cursor{}; GetCursorPos(&cursor); anchor = {cursor.x, cursor.y, cursor.x, cursor.y};
    }
    MONITORINFO monitor{}; monitor.cbSize = sizeof(monitor);
    const POINT point{anchor.left, anchor.bottom};
    cime::ui::Rect work{0, 0, std::max(1, GetSystemMetrics(SM_CXSCREEN)), std::max(1, GetSystemMetrics(SM_CYSCREEN))};
    if (GetMonitorInfoW(MonitorFromPoint(point, MONITOR_DEFAULTTONEAREST), &monitor))
        work = {monitor.rcWork.left, monitor.rcWork.top, monitor.rcWork.right, monitor.rcWork.bottom};
    if (!EnsureWindow(anchor.left, anchor.bottom)) return;
    // Another thread/instance of this app may have saved an appearance choice.
    // Refresh when opening a panel, rather than reading preferences per key.
    if (!IsWindowVisible(window_)) candidate_appearance_.Reload();
    const UINT dpi = GetDpiForWindow(window_); candidate_dpi_ = dpi ? dpi : 96;
    HDC dc = GetDC(window_);
    if (!dc) { candidate_layout_ = {}; ShowWindow(window_, SW_HIDE); return; }
    const HFONT font = CandidateFont(dc);
    HGDIOBJ old = SelectObject(dc, font ? font : GetStockObject(DEFAULT_GUI_FONT));
    TEXTMETRICW metrics{}; GetTextMetricsW(dc, &metrics);
    std::vector<int> widths; widths.reserve(page_.candidates.size());
    for (std::size_t i = 0; i < page_.candidates.size(); ++i) {
        const std::wstring text = CandidateLabel(i);
        SIZE extent{};
        widths.push_back(GetTextExtentPoint32W(dc, text.c_str(), static_cast<int>(text.size()), &extent)
            ? extent.cx : cime::ui::Scale(480, candidate_dpi_));
    }
    if (old && old != HGDI_ERROR) SelectObject(dc, old);
    if (font) DeleteObject(font);
    ReleaseDC(window_, dc);
    candidate_layout_ = cime::ui::MakeLayout(widths, std::max<int>(metrics.tmHeight, cime::ui::Scale(CandidateFontHeight(), candidate_dpi_)),
        std::min(cime::ui::Scale(480, candidate_dpi_), work.right - work.left), work.bottom - work.top, candidate_dpi_, selected_);
    const auto placed = cime::ui::PlacePopup(candidate_layout_, anchor, work, cime::ui::Scale(3, candidate_dpi_));
    if (placed.Empty()) { ShowWindow(window_, SW_HIDE); return; }
    candidate_appearance_.Apply(window_);
    SetWindowPos(window_, HWND_TOPMOST, placed.left, placed.top, placed.right - placed.left, placed.bottom - placed.top, SWP_NOACTIVATE | SWP_SHOWWINDOW);
    InvalidateRect(window_, nullptr, FALSE);
}
LRESULT CALLBACK TextService::WindowProcedure(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    auto* owner = reinterpret_cast<TextService*>(GetWindowLongPtrW(window, GWLP_USERDATA));
    if (message == WM_NCCREATE) {
        owner = static_cast<TextService*>(reinterpret_cast<CREATESTRUCTW*>(lparam)->lpCreateParams);
        SetWindowLongPtrW(window, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(owner));
    }
    if (message == WM_MOUSEACTIVATE) return MA_NOACTIVATE;
    if (message == WM_ERASEBKGND) return 1;
    if (owner && (message == WM_DPICHANGED || message == WM_DISPLAYCHANGE || message == WM_SETTINGCHANGE
            || message == WM_SYSCOLORCHANGE || message == WM_THEMECHANGED)) {
        // Re-measure with this host window's effective DPI after the move has
        // completed, without changing the application's DPI-awareness context.
        PostMessageW(window, kReflowCandidates, 0, 0); return 0;
    }
    if (owner && message == kReflowCandidates) { owner->UpdateWindowState(); return 0; }
    if (owner && message == kStartPrediction) {
        // Stale if any key, focus change or newer commit happened since posting.
        if (static_cast<std::uint64_t>(wparam) == owner->prediction_ticket_) owner->StartPrediction();
        return 0;
    }
    if (owner && message == WM_PAINT) { owner->Paint(window); return 0; }
    if (owner && message == WM_CONTEXTMENU) {
        POINT point{static_cast<short>(LOWORD(lparam)), static_cast<short>(HIWORD(lparam))};
        if (point.x == -1 && point.y == -1) GetCursorPos(&point);
        owner->ShowAppearanceMenu(point); return 0;
    }
    if (owner && message == WM_LBUTTONDOWN) {
        const int x = static_cast<short>(LOWORD(lparam)), y = static_cast<short>(HIWORD(lparam));
        const int index = cime::ui::HitTest(owner->candidate_layout_, x, y);
        if (index >= 0 && static_cast<std::size_t>(index) < owner->page_.candidates.size()) owner->Select(static_cast<UINT>(index), false);
        return 0;
    }
    if (message == WM_NCDESTROY) SetWindowLongPtrW(window, GWLP_USERDATA, 0);
    return DefWindowProcW(window, message, wparam, lparam);
}
int TextService::CandidateFontHeight() {
    const int size = candidate_appearance_.Current().font_size;
    return size ? size : cime::ui::kCandidateFontLogicalHeight;
}
HFONT TextService::CandidateFont(HDC dc) {
    const bool bundled = private_candidate_font_.EnsureForModule(g_instance);
    const auto settings = candidate_appearance_.Current();
    return cime::ui::CreateCandidateFont(dc, candidate_dpi_, bundled ? cime::ui::kBundledCandidateFontFamily : nullptr,
        CandidateFontHeight(), settings.font_family.empty() ? nullptr : settings.font_family.c_str());
}
std::wstring TextService::CandidateLabel(std::size_t index) const {
    if (index >= page_.candidates.size()) return {};
    return std::to_wstring(index + 1) + L"  " + page_.candidates[index].text;
}
void TextService::Paint(HWND window) {
    PAINTSTRUCT paint{}; HDC dc = BeginPaint(window, &paint);
    const auto colors = candidate_appearance_.Effective();
    const auto fill = [dc](const RECT& rectangle, COLORREF color) {
        if (HBRUSH brush = CreateSolidBrush(color)) { FillRect(dc, &rectangle, brush); DeleteObject(brush); }
    };
    RECT bounds{}; GetClientRect(window, &bounds); fill(bounds, colors.background);
    if (HBRUSH border = CreateSolidBrush(colors.border)) { FrameRect(dc, &bounds, border); DeleteObject(border); }
    const HFONT font = CandidateFont(dc);
    HGDIOBJ old = SelectObject(dc, font ? font : GetStockObject(DEFAULT_GUI_FONT)); SetBkMode(dc, TRANSPARENT);
    for (std::size_t i = 0; i < page_.candidates.size() && i < candidate_layout_.cells.size(); ++i) {
        const auto& cell = candidate_layout_.cells[i];
        if (cell.Empty()) continue;
        RECT box{cell.left, cell.top, cell.right, cell.bottom};
        if (i == selected_) { fill(box, colors.highlight); SetTextColor(dc, colors.highlight_text); }
        else SetTextColor(dc, colors.text);
        box.left += candidate_layout_.text_padding_x; box.right -= candidate_layout_.text_padding_x;
        const std::wstring text = CandidateLabel(i);
        DrawTextW(dc, text.c_str(), static_cast<int>(text.size()), &box, DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX);
    }
    if (old && old != HGDI_ERROR) SelectObject(dc, old);
    if (font) DeleteObject(font);
    EndPaint(window, &paint);
}

HRESULT CandidateElement::QueryInterface(REFIID iid, void** result) {
    if (!result) return E_POINTER;
    *result = nullptr;
    if (iid != IID_IUnknown && iid != IID_ITfUIElement && iid != IID_ITfCandidateListUIElement && iid != IID_ITfCandidateListUIElementBehavior) return E_NOINTERFACE;
    *result = static_cast<ITfCandidateListUIElementBehavior*>(this); AddRef(); return S_OK;
}
HRESULT CandidateElement::GetDescription(BSTR* value) { if (!value) return E_POINTER; *value = SysAllocString(kProductName); return *value ? S_OK : E_OUTOFMEMORY; }
HRESULT CandidateElement::GetGUID(GUID* value) { if (!value) return E_POINTER; *value = GUID_CantoneseCandidates; return S_OK; }
HRESULT CandidateElement::Show(BOOL show) { if (owner_) owner_->ShowCandidates(show != FALSE); return S_OK; }
HRESULT CandidateElement::IsShown(BOOL* value) { if (!value) return E_POINTER; *value = owner_ && owner_->Visible(); return S_OK; }
HRESULT CandidateElement::GetUpdatedFlags(DWORD* value) { if (!value) return E_POINTER; *value = TF_CLUIE_DOCUMENTMGR | TF_CLUIE_COUNT | TF_CLUIE_SELECTION | TF_CLUIE_STRING | TF_CLUIE_PAGEINDEX | TF_CLUIE_CURRENTPAGE; return S_OK; }
HRESULT CandidateElement::GetDocumentMgr(ITfDocumentMgr** value) { if (!value) return E_POINTER; *value = nullptr; return owner_ ? owner_->DocumentManager(value) : E_FAIL; }
HRESULT CandidateElement::GetCount(UINT* value) { if (!value) return E_POINTER; *value = owner_ ? static_cast<UINT>(owner_->CurrentPage().candidates.size()) : 0; return S_OK; }
HRESULT CandidateElement::GetSelection(UINT* value) { if (!value) return E_POINTER; *value = owner_ ? owner_->Selection() : 0; return S_OK; }
HRESULT CandidateElement::GetString(UINT index, BSTR* value) { if (!value) return E_POINTER; *value = nullptr; if (!owner_ || index >= owner_->CurrentPage().candidates.size()) return E_INVALIDARG; *value = SysAllocString(owner_->CurrentPage().candidates[index].text.c_str()); return *value ? S_OK : E_OUTOFMEMORY; }
HRESULT CandidateElement::GetPageIndex(UINT* index, UINT size, UINT* pages) { if (!pages) return E_POINTER; *pages = 1; if (!index || size < 1) return S_FALSE; index[0] = 0; return S_OK; }
HRESULT CandidateElement::SetPageIndex(UINT* index, UINT pages) { return index && pages == 1 && index[0] == 0 ? S_OK : E_INVALIDARG; }
HRESULT CandidateElement::GetCurrentPage(UINT* value) { if (!value) return E_POINTER; *value = 0; return S_OK; }
HRESULT CandidateElement::SetSelection(UINT value) { return owner_ ? owner_->Highlight(value) : E_FAIL; }
HRESULT CandidateElement::Finalize() { return owner_ ? owner_->Select(owner_->Selection(), false) : E_FAIL; }
HRESULT CandidateElement::Abort() { return owner_ ? owner_->Finish(true, false) : E_FAIL; }

class Factory final : public IClassFactory, public Counted {
public:
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** result) override {
        if (!result) return E_POINTER;
        *result = nullptr;
        if (iid != IID_IUnknown && iid != IID_IClassFactory) return E_NOINTERFACE;
        *result = static_cast<IClassFactory*>(this); AddRef(); return S_OK;
    }
    REF_METHODS
    HRESULT STDMETHODCALLTYPE CreateInstance(IUnknown* outer, REFIID iid, void** result) override {
        if (!result) return E_POINTER;
        *result = nullptr; if (outer) return CLASS_E_NOAGGREGATION;
        auto* service = new (std::nothrow) TextService; if (!service) return E_OUTOFMEMORY;
        const HRESULT status = service->QueryInterface(iid, result); service->Release(); return status;
    }
    HRESULT STDMETHODCALLTYPE LockServer(BOOL lock) override { if (lock) InterlockedIncrement(&g_locks); else InterlockedDecrement(&g_locks); return S_OK; }
};
}  // namespace

extern "C" BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) { g_instance = instance; DisableThreadLibraryCalls(instance); }
    return TRUE;
}
extern "C" HRESULT WINAPI DllCanUnloadNow() { return !g_objects && !g_locks ? S_OK : S_FALSE; }
extern "C" HRESULT WINAPI DllGetClassObject(REFCLSID clsid, REFIID iid, void** result) {
    if (!result) return E_POINTER;
    *result = nullptr; if (clsid != CLSID_CantoneseIME) return CLASS_E_CLASSNOTAVAILABLE;
    auto* factory = new (std::nothrow) Factory; if (!factory) return E_OUTOFMEMORY;
    const HRESULT status = factory->QueryInterface(iid, result); factory->Release(); return status;
}
