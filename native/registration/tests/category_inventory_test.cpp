// Pure fixtures for the production registry-snapshot validator. This includes
// the helper's implementation but never calls its entrypoint or any registry,
// registration, profile activation, enabling or process-control operation.
#define wmain CimeMachineRegistrationProgram
#include "../machine.cpp"
#undef wmain
#include <cassert>
#include <functional>

namespace {
unsigned checks = 0;
void Reject(const std::function<void()>& body) {
    bool rejected = false;
    try { body(); } catch (const Failure&) { rejected = true; }
    assert(rejected); ++checks;
}
CategoryLinks Fixture(unsigned count) {
    CategoryLinks links;
    links.item_roots = {Guid(CLSID_CantoneseIME)};
    for (unsigned index = 0; index < count; ++index) {
        links.forward.push_back(Guid(*kCategories[index]));
        links.reverse.emplace(Guid(*kCategories[index]), std::vector<std::wstring>{Guid(CLSID_CantoneseIME)});
    }
    return links;
}
void Release(const CategoryLinks& links) {
    Require(ValidateCategoryLinks(links).size() == kCategoryCount, E_FAIL, L"Incomplete release category inventory");
}
}

int main() {
    auto expected = Fixture(kCategoryCount);
    assert(ValidateCategoryLinks(expected) == std::vector<unsigned>({0, 1, 2, 3, 4, 5})); ++checks;
    // A 1.0.8-patched install has five memberships; refresh adds COM-less support.
    const auto pre_comless = Fixture(5);
    assert(ValidateCategoryLinks(pre_comless) == std::vector<unsigned>({0, 1, 2, 3, 4})); ++checks;
    Reject([&] { Release(pre_comless); });
    // A 1.0.8 install has the four pre-immersive memberships; refresh adds the fifth.
    const auto pre_immersive = Fixture(4);
    assert(ValidateCategoryLinks(pre_immersive) == std::vector<unsigned>({0, 1, 2, 3})); ++checks;
    Reject([&] { Release(pre_immersive); });
    auto original = Fixture(3);
    // Empty known category parents left by an earlier rollback are not members.
    original.reverse.emplace(Guid(*kCategories[3]), std::vector<std::wstring>{});
    assert(ValidateCategoryLinks(original) == std::vector<unsigned>({0, 1, 2})); ++checks;
    const std::vector<unsigned> cached_com_view = {0, 1, 2};
    Release(expected);
    assert(cached_com_view.size() != ValidateCategoryLinks(expected).size()); ++checks;
    const auto removal_members = ValidateCategoryLinks(expected);
    assert(removal_members.size() == kCategoryCount && std::find(removal_members.begin(), removal_members.end(), 3) != removal_members.end());
    // Removal follows persisted membership, including SYSTRAY omitted by an
    // older COM view. A residual forward/reverse SYSTRAY leaf still blocks it.
    auto only_systray_left = Fixture(4);
    only_systray_left.forward.erase(only_systray_left.forward.begin(), only_systray_left.forward.begin() + 3);
    for (unsigned index = 0; index < 3; ++index) only_systray_left.reverse[Guid(*kCategories[index])].clear();
    assert(ValidateCategoryLinks(only_systray_left) == std::vector<unsigned>({3}));
    auto all_removed = only_systray_left;
    all_removed.forward.clear(); all_removed.reverse[Guid(*kCategories[3])].clear();
    assert(ValidateCategoryLinks(all_removed).empty());
    // Neither a successful registration HRESULT nor a full cached view can
    // substitute for an actually missing persisted release membership.
    Reject([&] { Release(original); });
    auto missing_reverse = expected;
    missing_reverse.reverse[Guid(*kCategories[3])].clear();
    Reject([&] { Release(missing_reverse); });
    auto missing_forward = expected; missing_forward.forward.pop_back();
    Reject([&] { Release(missing_forward); });
    const std::wstring foreign = L"{11111111-2222-3333-4444-555555555555}";
    auto foreign_root = expected; foreign_root.item_roots = {foreign};
    Reject([&] { Release(foreign_root); });
    auto extra_root = expected; extra_root.item_roots.push_back(foreign);
    Reject([&] { Release(extra_root); });
    auto unknown_forward = expected; unknown_forward.forward[3] = foreign;
    Reject([&] { Release(unknown_forward); });
    auto unknown_reverse = expected; unknown_reverse.reverse.emplace(foreign, std::vector<std::wstring>{});
    Reject([&] { Release(unknown_reverse); });
    auto foreign_reverse_item = expected; foreign_reverse_item.reverse[Guid(*kCategories[0])] = {foreign};
    Reject([&] { Release(foreign_reverse_item); });
    auto duplicate_forward = expected; duplicate_forward.forward.push_back(Guid(*kCategories[0]));
    Reject([&] { Release(duplicate_forward); });
    auto missing_root = expected; missing_root.item_roots.clear();
    Reject([&] { Release(missing_root); });
    auto wrong_reverse_item_count = expected;
    wrong_reverse_item_count.reverse[Guid(*kCategories[0])].push_back(Guid(CLSID_CantoneseIME));
    Reject([&] { Release(wrong_reverse_item_count); });
    assert(ValidateCategoryLinks(CategoryLinks{}).empty()); ++checks;
    std::cout << "{\"passed\":true,\"checks\":" << checks
              << ",\"scope\":\"x64 production persistent-category validator fixtures, including divergent cached/stored views; no OS state mutations\"}\n";
    return 0;
}
