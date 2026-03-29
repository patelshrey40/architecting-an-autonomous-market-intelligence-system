import unittest

from app.pipeline import build_demo_state, classify_property


class PipelineTests(unittest.TestCase):
    def test_classification_adds_expected_flags(self):
        result = classify_property(
            {
                "property_class_code": "4A",
                "zoning_code": "MU-H",
                "place_count": 12,
                "improvement_value": 1000,
            }
        )
        self.assertEqual(result["primary"], "COMMERCIAL")
        self.assertIn("mixed_use_signal", result["flags"])
        self.assertIn("commercial_multi_tenant", result["flags"])

    def test_demo_state_contains_ranked_properties_and_targets(self):
        state = build_demo_state()
        self.assertEqual(state["summary"]["property_count"], 6)
        self.assertEqual(state["summary"]["cities_covered"], 2)
        self.assertGreaterEqual(state["summary"]["tier_1_count"], 2)
        self.assertGreater(state["properties"][0]["priority_score"], state["properties"][-1]["priority_score"])
        self.assertGreater(state["targets"][0]["target_score"], state["targets"][-1]["target_score"])

    def test_property_detail_includes_owner_and_evidence(self):
        state = build_demo_state()
        detail = state["property_details"]["prop-jc-001"]
        self.assertEqual(detail["owner"]["name"], "Journal Square Revive LLC")
        self.assertTrue(detail["claims"])
        self.assertEqual(detail["property"]["priority_tier"], "Tier 1")


if __name__ == "__main__":
    unittest.main()
