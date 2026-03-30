import unittest

from app.scoring import (
    classify_property_code,
    compute_priority_score,
    place_density_signal,
    priority_tier_for_score,
)


class ScoringTests(unittest.TestCase):
    def test_classification_mapping(self):
        self.assertEqual(classify_property_code("4A"), "COMMERCIAL")
        self.assertEqual(classify_property_code("15D"), "EXEMPT_RELIGIOUS_CHARITABLE")
        self.assertEqual(classify_property_code(""), "UNKNOWN")

    def test_place_density_signal_caps_at_ten_places(self):
        self.assertEqual(place_density_signal(0), 0.0)
        self.assertEqual(place_density_signal(5), 50.0)
        self.assertEqual(place_density_signal(20), 100.0)

    def test_priority_score_formula_and_tiers(self):
        score = compute_priority_score(
            assessed_value_percentile=90.0,
            classification="COMMERCIAL",
            building_sqft_percentile=80.0,
            place_count=4,
        )
        self.assertAlmostEqual(score, 89.5)
        self.assertEqual(priority_tier_for_score(score), "Tier 1")
        self.assertEqual(priority_tier_for_score(61), "Tier 2")
        self.assertEqual(priority_tier_for_score(45), "Tier 3")
        self.assertEqual(priority_tier_for_score(10), "Tier 4")
