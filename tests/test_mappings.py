"""Independent spelling-policy and official-table semantic tests."""

from __future__ import annotations

from dataclasses import asdict
import json
import math
from pathlib import Path
import tempfile
import unittest

from cantonese_ime.mappings import (
    EXPLICIT_ALIASES, FINALS, ONSETS, PROFILE_VERSION, RULES, SOURCES, YALE_RULE_ID,
    build_mapping_artifacts, mapping_records, read_lshk_table,
    resolve_syllable, scheme_parts, spelling_variants, syllable_mappings,
    yale_ascii, yale_ascii_mapping,
)


HEADER = "CH\tUCODE\tJP\tINIT\tFINL\tTONE\tDESC\tDESC_JP\n"
TABLE = (
    HEADER
    + "你\tU+4F60\tnei5\tn\tei\t5\t\t\n"
    + "理\tU+7406\tlei5\tl\tei\t5\t\t\n"
    + "有\tU+6709\tjau5\tj\tau\t5\t\t\n"
    + "就\tU+5C31\tzau6\tz\tau\t6\t\t\n"
    + "㓁\tU+34C1\t\t\t\t\t网字頭\tmong5 zi6 tau4\n"
    + "卅\tU+5345\tsaa1 aa6\t\t\t\t\tsaa1 aa6\n"
)


class MappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cantonese-mapping-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def fixture_records(self):
        path = self.directory / "list.tsv"
        path.write_text(TABLE, encoding="utf-8")
        return list(read_lshk_table(path))

    def test_identity_roundtrip_in_both_modes(self) -> None:
        for base in ("nei", "jau", "zau", "caa", "aa", "a", "oet", "m", "ng"):
            for mode in ("jyutping", "forgiving"):
                with self.subTest(base=base, mode=mode):
                    self.assertEqual(spelling_variants(base, mode)[base], 0)
                    match = next(match for match in resolve_syllable(base, [base], mode) if match.canonical == base)
                    self.assertEqual(match.penalty, 0)
                    self.assertIsNone(match.tone)

    def test_strict_mode_contains_identities_only(self) -> None:
        for base in ("nei", "ngo", "bou", "caa", "zau", "soeng", "faai"):
            self.assertEqual(spelling_variants(base, "jyutping"), {base: 0.0})
        self.assertEqual(resolve_syllable("lay", ["nei"], "jyutping"), [])

    def test_documented_examples_and_preserved_costs(self) -> None:
        for canonical, spelling, cost in (
            ("nei", "lay", 1.15), ("nei", "nay", 0.5), ("nei", "lei", 0.8),
            ("ngo", "ngoh", 0.3), ("bou", "bo", 0.35), ("bou", "boh", 0.4),
            ("zau", "jau", 0.8), ("caa", "cha", 0.75),
            ("faai", "fai", 0.3), ("baau", "bao", 0.4),
            ("soeng", "shueng", 0.8), ("soeng", "sheung", 0.45),
            ("zyun", "juen", 0.45), ("cyun", "chuen", 0.45), ("syun", "suen", 0.45),
            ("jyun", "yuen", 0.45), ("tyun", "tuen", 0.45),
            # Hong Kong casual finals and the sh onset, alone and combined.
            ("leoi", "lui", 0.45), ("seoi", "sui", 0.45), ("ceoi", "chui", 0.9),
            ("leon", "lun", 0.45), ("seon", "shun", 0.9), ("ceot", "chut", 0.9), ("sik", "shik", 0.45),
            # The yu glide may be dropped; open yu is cheaper than closed yun/yut.
            ("syu", "su", 0.45), ("cyu", "chu", 0.9), ("zyu", "chu", 1.25), ("zyu", "zu", 0.45),
            ("lyun", "luen", 0.45), ("hyut", "huet", 0.45), ("lyun", "lun", 0.8), ("jyut", "yuet", 0.45),
        ):
            with self.subTest(canonical=canonical, spelling=spelling):
                self.assertAlmostEqual(spelling_variants(canonical)[spelling], cost)

    def test_dropped_yu_glide_never_leaves_a_bare_vowel_or_jyutping_j(self) -> None:
        for canonical, spelling in (("jyu", "u"), ("jyun", "uen"), ("jyut", "uet"), ("jyun", "jun"), ("jyu", "ju")):
            with self.subTest(canonical=canonical, spelling=spelling):
                self.assertNotIn(spelling, spelling_variants(canonical))

    def test_ambiguous_initials_retain_exact_and_alternative_readings(self) -> None:
        matches = resolve_syllable("jau", ["jau", "zau"])
        self.assertEqual([(match.canonical, match.penalty) for match in matches], [("jau", 0), ("zau", 0.8)])
        self.assertEqual({match.canonical for match in resolve_syllable("lei", ["lei", "nei"])}, {"lei", "nei"})

    def test_alternative_onset_conventions_are_not_composed(self) -> None:
        self.assertIn("jaa", spelling_variants("zaa"))
        self.assertNotIn("yaa", spelling_variants("zaa"))
        self.assertNotIn("ya", spelling_variants("zaa"))
        self.assertIn("cha", spelling_variants("caa"))
        self.assertNotIn("chhaa", spelling_variants("caa"))

    def test_nasal_syllables_are_not_erased_or_globally_rewritten(self) -> None:
        self.assertEqual(spelling_variants("ng"), {"ng": 0})
        self.assertNotIn("lg", spelling_variants("ng"))
        self.assertIn("o", spelling_variants("ngo"))
        with self.assertRaises(ValueError):
            spelling_variants("nei'hou")
        with self.assertRaises(ValueError):
            spelling_variants("nei hou")

    def test_numeric_tones_roundtrip_without_merging(self) -> None:
        for tone in "123456":
            matches = resolve_syllable("lay" + tone, ["nei"])
            self.assertEqual([(match.canonical, match.tone) for match in matches], [("nei", tone)])
            strict = resolve_syllable("nei" + tone, ["nei"], "jyutping")
            self.assertEqual(strict[0].tone, tone)
        for raw in ("nei0", "nei7", "nei9", "nei55", "nei⁵", "你好", "🙂", "nei5hou2"):
            with self.subTest(raw=raw):
                self.assertEqual(resolve_syllable(raw, ["nei"]), [])

    def test_rules_and_derivations_have_provenance_and_bounded_costs(self) -> None:
        ids = {rule.rule_id for rule in RULES} | {item[3] for item in EXPLICIT_ALIASES} | {YALE_RULE_ID}
        for record in mapping_records(["zaa", "caa", "nei", "baau", "soeng"]):
            with self.subTest(record=record):
                self.assertTrue(math.isfinite(record.penalty) and record.penalty >= 0)
                self.assertLessEqual(len(record.derivation), 2)
                self.assertTrue(set(record.derivation).issubset(ids))
                self.assertTrue(set(record.source).issubset(SOURCES))
                onset_rules = sum(rule_id.startswith("onset.") for rule_id in record.derivation)
                self.assertLessEqual(onset_rules, 1)
        serialized = [asdict(record) for record in mapping_records(["nei", "jau", "zau"])]
        self.assertEqual(len(serialized), len({(record["spelling"], record["canonical"]) for record in serialized}))

    def test_yale_whole_final_reference_table(self) -> None:
        # Independently specified expected correspondences from the CPIME
        # parallel table and EdUHK Appendix I. A zero onset isolates each final.
        expected = {
            "aa": "a", "aai": "aai", "aau": "aau", "aam": "aam",
            "aap": "aap", "aan": "aan", "aat": "aat", "aang": "aang", "aak": "aak",
            "ai": "ai", "au": "au", "am": "am", "ap": "ap", "an": "an",
            "at": "at", "ang": "ang", "ak": "ak", "e": "e", "eng": "eng",
            "ek": "ek", "ei": "ei", "oe": "eu", "oeng": "eung", "oek": "euk",
            "eoi": "eui", "eon": "eun", "eot": "eut", "i": "i", "iu": "iu",
            "im": "im", "ip": "ip", "in": "in", "it": "it", "ing": "ing", "ik": "ik",
            "o": "o", "oi": "oi", "on": "on", "ot": "ot", "ong": "ong", "ok": "ok",
            "ou": "ou", "u": "u", "ui": "ui", "un": "un", "ut": "ut", "ung": "ung",
            "uk": "uk", "yu": "yu", "yun": "yun", "yut": "yut", "m": "m", "ng": "ng",
        }
        for canonical, spelling in expected.items():
            with self.subTest(canonical=canonical):
                self.assertEqual(yale_ascii(canonical), spelling)
                self.assertEqual(yale_ascii_mapping(canonical).status, "table")
                self.assertIn(spelling, spelling_variants(canonical))
                self.assertEqual(spelling_variants(canonical, "jyutping"), {canonical: 0})

    def test_yale_all_onsets_and_no_recursive_transformation(self) -> None:
        expected = {
            "baa": "ba", "paa": "pa", "maa": "ma", "faa": "fa", "daa": "da",
            "taa": "ta", "naa": "na", "laa": "la", "gaa": "ga", "kaa": "ka",
            "ngaa": "nga", "haa": "ha", "gwaa": "gwa", "kwaa": "kwa", "waa": "wa",
            "zaa": "ja", "caa": "cha", "saa": "sa", "jaa": "ya",
            "geoi": "geui", "zoeng": "jeung", "coek": "cheuk", "cyu": "chyu",
            "jyu": "yu", "jyun": "yun", "jyut": "yut", "zyu": "jyu",
            "kwun": "kwun", "gwok": "gwok",
        }
        for canonical, spelling in expected.items():
            with self.subTest(canonical=canonical):
                self.assertEqual(yale_ascii(canonical), spelling)
                self.assertIn(spelling, spelling_variants(canonical))
        self.assertNotIn("ya", spelling_variants("zaa"))
        self.assertNotIn("yyu", spelling_variants("jyu"))
        self.assertNotIn("cheui", spelling_variants("geoi"))
        self.assertNotIn("leui", spelling_variants("neoi"))
        yale = next(record for record in syllable_mappings("coek") if record.spelling == "cheuk")
        self.assertEqual(yale.derivation, (YALE_RULE_ID,))

    def test_yale_closed_aa_distinctions_and_whole_words(self) -> None:
        for canonical, expected in (("caa", "cha"), ("can", "chan"), ("caan", "chaan"),
                                    ("caai", "chaai"), ("caau", "chaau"),
                                    ("caam", "chaam"), ("caap", "chaap"), ("caang", "chaang")):
            with self.subTest(canonical=canonical):
                self.assertEqual(yale_ascii(canonical), expected)
        for canonical, expected in (
            ("fo geoi", "fogeui"), ("hoeng gong", "heunggong"),
            ("gwong dung waa", "gwongdungwa"), ("jyut jyu", "yutyu"),
            ("sin saang", "sinsaang"), ("zoeng coek cyut", "jeungcheukchyut"),
        ):
            with self.subTest(canonical=canonical):
                self.assertEqual("".join(yale_ascii(base) for base in canonical.split()), expected)
        # The old relaxed aa alias remains available, but is not called Yale.
        self.assertIn("sang", spelling_variants("saang"))
        self.assertEqual(yale_ascii("saang"), "saang")

    def test_yale_collisions_and_numeric_filters_preserve_all_readings(self) -> None:
        inventory = ["jyu", "yu", "jyut", "yut", "zaa", "ja", "geoi", "geui"]
        for spelling, expected in (("yu", {"jyu", "yu"}), ("yut", {"jyut", "yut"}),
                                    ("ja", {"zaa", "ja"}), ("geui", {"geoi", "geui"})):
            for tone in "123456":
                with self.subTest(spelling=spelling, tone=tone):
                    matches = resolve_syllable(spelling + tone, inventory)
                    self.assertEqual({match.canonical for match in matches}, expected)
                    self.assertTrue(all(match.tone == tone for match in matches))
            strict = resolve_syllable(spelling + "6", inventory, "jyutping")
            self.assertEqual({match.canonical for match in strict}, {spelling})
        self.assertEqual(resolve_syllable("geui6", ["geoi"], "jyutping"), [])

    def test_marginal_yale_extension_and_explicit_fallbacks(self) -> None:
        for canonical, expected in (("eu", "ew"), ("deu", "dew"), ("ceu", "chew"),
                                    ("a", "a"), ("em", "em"), ("en", "en"), ("ep", "ep"), ("et", "et"),
                                    ("om", "om"), ("op", "op"), ("um", "um"), ("up", "up"), ("oet", "eut"),
                                    ("cet", "chet"), ("jen", "yen"), ("za", "ja"),
                                    ("zop", "jop"), ("zoet", "jeut")):
            with self.subTest(canonical=canonical):
                route = yale_ascii_mapping(canonical)
                self.assertEqual(route.spelling, expected)
                self.assertEqual(route.status, "extension")
                self.assertIn("yale_pycantonese", route.source)
                self.assertIn(expected, spelling_variants(canonical))
        for canonical in ("hm", "hng"):
            with self.subTest(canonical=canonical):
                route = yale_ascii_mapping(canonical)
                self.assertEqual(route.spelling, canonical)
                self.assertEqual(route.status, "extension")
                self.assertEqual(route.source, ("yale_kfcd_numeric",))
                self.assertIn(canonical, spelling_variants(canonical))
        for canonical in ("eo", "soei", "ngm"):
            with self.subTest(canonical=canonical):
                route = yale_ascii_mapping(canonical)
                self.assertEqual(route.spelling, canonical)
                self.assertEqual(route.status, "jyutping_fallback")
                self.assertIn("No verified Yale", route.reason)
                self.assertIn(canonical, spelling_variants(canonical))

    def test_yale_input_contract_rejects_whole_phrases_and_tones(self) -> None:
        for invalid in (None, "", "GEoi", "geoi6", "fo geoi", "fo'geoi", "炬", "a" * 13):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    yale_ascii(invalid)

    def test_rule_application_is_repeatable_and_inventory_order_independent(self) -> None:
        self.assertEqual(mapping_records(["nei", "jau", "zau"]), mapping_records(["zau", "nei", "jau", "nei"]))
        self.assertEqual(syllable_mappings("caa"), syllable_mappings("caa"))

    def test_invalid_options_are_explicit(self) -> None:
        for base in ("", "NEI", "nei5", "你", "n" * 13):
            with self.subTest(base=base), self.assertRaises(ValueError):
                spelling_variants(base)
        with self.assertRaises(ValueError):
            spelling_variants("nei", "Yale")
        with self.assertRaises(ValueError):
            resolve_syllable("nei", ["nei"], "unknown")

    def test_lshk_description_and_multi_syllable_rows_remain_distinct(self) -> None:
        records = self.fixture_records()
        self.assertEqual(len(records), 6)
        self.assertEqual(records[4].kind, "description_only")
        self.assertEqual(records[4].reading, "")
        self.assertEqual(records[4].description_reading, "mong5 zi6 tau4")
        self.assertEqual(records[5].kind, "multi_syllable")
        self.assertEqual(records[5].reading, "saa1 aa6")

    def test_lshk_component_and_codepoint_mismatch_are_rejected(self) -> None:
        for content in (
            TABLE.replace("nei5\tn\tei\t5", "nei5\tn\tei\t6"),
            TABLE.replace("你\tU+4F60", "你\tU+4F61"),
        ):
            path = self.directory / "invalid.tsv"
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(ValueError):
                list(read_lshk_table(path))

    def test_inventory_separates_observation_from_scheme_shape(self) -> None:
        inventory, mapping = build_mapping_artifacts(self.fixture_records(), ["nei", "a", "oet", "ki"])
        by_base = {item["base"]: item for item in inventory["syllables"]}
        self.assertFalse(by_base["a"]["lshk_observed"])
        self.assertTrue(by_base["a"]["scheme_shape"])
        self.assertTrue(by_base["oet"]["scheme_shape"])
        self.assertFalse(by_base["lei"]["dictionary_observed"])
        self.assertEqual(inventory["counts"]["lshk_observed_bases"], 4)
        self.assertEqual(inventory["counts"]["lshk_multi_syllable_rows"], 1)
        self.assertEqual(inventory["counts"]["lshk_description_only_rows"], 1)
        self.assertNotIn("mong", by_base)
        self.assertNotIn("saa", by_base)
        self.assertEqual(mapping["counts"]["identity_records"], len(by_base))
        self.assertEqual(mapping["profile_version"], PROFILE_VERSION)
        self.assertEqual(inventory["schema_version"], 3)
        self.assertEqual(by_base["a"]["yale_ascii"]["status"], "extension")
        self.assertEqual(by_base["jau"]["yale_ascii"]["spelling"], "yau")
        self.assertEqual(sum(mapping["yale_ascii"]["counts"].values()), len(by_base))
        self.assertEqual(len(mapping["yale_ascii"]["routes"]), len(by_base))
        aliases = {(record["canonical"], record["spelling"]) for record in mapping["aliases"]}
        self.assertTrue(all((route["canonical"], route["spelling"]) in aliases for route in mapping["yale_ascii"]["routes"]))
        json.dumps(inventory)
        json.dumps(mapping)

    def test_scheme_uses_authoritative_initials_and_2018_finals(self) -> None:
        self.assertEqual(len(ONSETS), 19)
        self.assertIn("a", FINALS)
        self.assertIn("oet", FINALS)
        self.assertEqual(scheme_parts("ng"), ("", "ng", True))
        self.assertEqual(scheme_parts("ngm"), ("ng", "m", False))
        self.assertEqual(scheme_parts("soei"), ("s", "oei", False))

    def test_supplemental_bases_keep_their_own_source_attestation(self) -> None:
        inventory, mappings = build_mapping_artifacts(
            self.fixture_records(), ["nei", "lei", "jau", "zau", "ben"],
            source_inventories={"rime_dictionary": ["nei"], "wordshk_wordlist": ["nei", "ben"]},
            source_tones={"rime_dictionary": {"nei": ["5"]}, "wordshk_wordlist": {"nei": ["5"], "ben": ["1"]}},
            retained_sources={"nei": ["jyut6ping3.chars.dict.yaml"], "ben": ["words.hk:wordlist"]},
            retained_origins={"nei": ["explicit"], "ben": ["supplement"]},
        )
        by_base = {item["base"]: item for item in inventory["syllables"]}
        self.assertTrue(by_base["ben"]["dictionary_observed"])
        self.assertTrue(by_base["ben"]["wordshk_observed"])
        self.assertFalse(by_base["ben"]["rime_observed"])
        self.assertFalse(by_base["ben"]["lshk_observed"])
        self.assertEqual(by_base["ben"]["source_attestations"], ["wordshk_wordlist"])
        self.assertEqual(by_base["ben"]["observed_tones_by_source"], {"wordshk_wordlist": ["1"]})
        self.assertEqual(by_base["ben"]["retained_dictionary_origins"], ["supplement"])
        self.assertEqual(inventory["counts"]["dictionary_bases"], 5)
        self.assertEqual(inventory["counts"]["rime_bases"], 1)
        self.assertEqual(inventory["counts"]["wordshk_bases"], 2)
        self.assertEqual(inventory["inventory_delta"]["added_dictionary_bases"], ["ben"])
        self.assertEqual(inventory["counts"]["unmapped_dictionary_bases"], 0)
        self.assertIn(("ben", "ben"), {(item["canonical"], item["spelling"]) for item in mappings["aliases"]})

    def test_source_attestation_is_not_lost_when_dictionary_dedup_chooses_another_source(self) -> None:
        inventory, _ = build_mapping_artifacts(
            self.fixture_records(), ["nei"],
            source_inventories={"rime_dictionary": ["nei"], "wordshk_wordlist": ["nei"]},
            retained_sources={"nei": ["jyut6ping3.chars.dict.yaml"]},
        )
        nei = next(item for item in inventory["syllables"] if item["base"] == "nei")
        self.assertEqual(nei["source_attestations"], ["lshk_table", "rime_dictionary", "wordshk_wordlist"])
        self.assertEqual(nei["retained_dictionary_sources"], ["jyut6ping3.chars.dict.yaml"])
        self.assertTrue(nei["wordshk_observed"])

    def test_typeduck_and_unihan_attestations_survive_another_retained_source(self) -> None:
        inventory, _ = build_mapping_artifacts(
            self.fixture_records(), ["nei", "bap", "fing"],
            source_inventories={"rime_dictionary": ["nei"], "typeduck_principal": ["nei", "fing"],
                                "unihan_kcantonese": ["nei", "bap"]},
            source_tones={"rime_dictionary": {"nei": ["5"]},
                          "typeduck_principal": {"nei": ["5"], "fing": ["6"]},
                          "unihan_kcantonese": {"nei": ["5"], "bap": ["6"]}},
            retained_sources={"nei": ["jyut6ping3.chars.dict.yaml"]},
        )
        by_base = {item["base"]: item for item in inventory["syllables"]}
        nei = by_base["nei"]
        self.assertEqual(nei["retained_dictionary_sources"], ["jyut6ping3.chars.dict.yaml"])
        self.assertTrue(nei["typeduck_observed"])
        self.assertTrue(nei["unihan_observed"])
        self.assertIn("typeduck_principal", nei["source_attestations"])
        self.assertIn("unihan_kcantonese", nei["source_attestations"])
        self.assertEqual(nei["provisional_source_attestations"], ["unihan_kcantonese"])
        self.assertEqual(nei["observed_tones_by_source"]["unihan_kcantonese"], ["5"])
        self.assertEqual(inventory["counts"]["typeduck_bases"], 2)
        self.assertEqual(inventory["counts"]["unihan_bases"], 2)
        self.assertEqual(inventory["source_observed_toned_form_counts"]["unihan_kcantonese"], 2)
        self.assertEqual(by_base["fing"]["provisional_source_attestations"], [])
        self.assertFalse(by_base["bap"]["typeduck_observed"])

    def test_new_unihan_forms_are_shape_attested_without_normative_claims(self) -> None:
        bases = ["bap", "dun", "eot", "kwaat"]
        inventory, mappings = build_mapping_artifacts(
            self.fixture_records(), bases,
            source_inventories={"unihan_kcantonese": bases},
            source_tones={"unihan_kcantonese": {"bap": ["6"], "dun": ["1"], "eot": ["1"], "kwaat": ["1"]}},
        )
        by_base = {item["base"]: item for item in inventory["syllables"]}
        for base in bases:
            with self.subTest(base=base):
                self.assertTrue(by_base[base]["scheme_shape"])
                self.assertTrue(by_base[base]["unihan_observed"])
                self.assertFalse(by_base[base]["lshk_observed"])
                self.assertEqual(by_base[base]["source_attestations"], ["unihan_kcantonese"])
                self.assertEqual(by_base[base]["provisional_source_attestations"], ["unihan_kcantonese"])
                identity = next(item for item in mappings["aliases"] if item["spelling"] == item["canonical"] == base)
                self.assertEqual(identity["penalty"], 0)
        self.assertEqual(SOURCES["unihan_kcantonese"]["property_status"], "Provisional")
        self.assertIn("not normative", SOURCES["unihan_kcantonese"]["scope"])

    def test_undeclared_sources_and_invalid_source_tones_are_rejected(self) -> None:
        records = self.fixture_records()
        with self.assertRaises(ValueError):
            build_mapping_artifacts(records, ["nei"], source_inventories={"unknown": ["nei"]})
        with self.assertRaises(ValueError):
            build_mapping_artifacts(records, ["nei"], source_tones={"wordshk_wordlist": {"nei": ["0"]}})


if __name__ == "__main__":
    unittest.main()
