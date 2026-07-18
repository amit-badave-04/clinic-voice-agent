"""Pure-unit tests for the patient-name integrity gate (no DB needed).

The live failures these encode: a caller booked into the PMS in Devanagari
("साकेत स्थित"), and an ASR chain that produced "Three Watch Toshniwal" for
"Shrivatsa Toshniwal" — with intermediate variants that should fuzzy-match the
patient once one correct spelling exists.
"""
from app.services import names


class TestNormalizeForRecords:
    def test_devanagari_full_name_romanized(self):
        result = names.normalize_for_records("श्रीवत्स तोष्णीवाल")
        assert not names.contains_devanagari(result)
        assert result.split()[0] == "Shrivatsa"

    def test_latin_name_untouched(self):
        assert names.normalize_for_records("Rahul Sharma") == "Rahul Sharma"

    def test_mixed_script_only_devanagari_words_change(self):
        result = names.normalize_for_records("श्रीवत्स Toshniwal")
        assert result.endswith("Toshniwal")
        assert not names.contains_devanagari(result)

    def test_original_live_bug_saket(self):
        result = names.normalize_for_records("साकेत स्थित")
        assert not names.contains_devanagari(result)
        assert result.split()[0] == "Saketa"  # readable ASCII, roman schwa retained

    def test_whitespace_collapsed(self):
        assert names.normalize_for_records("  Rahul   Sharma ") == "Rahul Sharma"


class TestIsPlausible:
    def test_real_names_pass(self):
        for name in ("Shrivatsa Toshniwal", "Rahul Sharma", "Pooja Pandey Tripathi",
                     "Mohammed Farhan", "Anamika Lyngdoh"):
            assert names.is_plausible(name), name

    def test_live_bug_three_watch_rejected(self):
        assert not names.is_plausible("Three Watch Toshniwal")

    def test_app_and_object_words_rejected(self):
        assert not names.is_plausible("WhatsApp Toshniwal")
        assert not names.is_plausible("Hello Test")

    def test_digits_rejected(self):
        assert not names.is_plausible("Rahul 42")

    def test_single_token_rejected(self):
        assert not names.is_plausible("Rahul")

    def test_initials_allowed(self):
        assert names.is_plausible("Netaji D")


class TestRosterSuggestion:
    ROSTER = ["Tushar Badey", "Shrivatsa Toshniwal"]

    def test_live_variant_matches_existing_patient(self):
        # ASR variants heard on the 18-July call before the correct spelling.
        assert names.roster_suggestion("Shrivaad Toshniwal", self.ROSTER) == "Shrivatsa Toshniwal"
        assert names.roster_suggestion("Shri Vadsa Toshniwal", self.ROSTER) == "Shrivatsa Toshniwal"

    def test_exact_match_no_suggestion(self):
        assert names.roster_suggestion("Shrivatsa Toshniwal", self.ROSTER) is None
        assert names.roster_suggestion("shrivatsa toshniwal", self.ROSTER) is None

    def test_unrelated_name_no_suggestion(self):
        assert names.roster_suggestion("Priya Sharma", self.ROSTER) is None

    def test_empty_roster(self):
        assert names.roster_suggestion("Rahul Sharma", []) is None
