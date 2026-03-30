import json
import tempfile
import unittest
from pathlib import Path

from app.config import get_settings
from app.lead_scoring import (
    classify_persona,
    compute_lead_score,
    contactability_score,
    lead_status_for_score,
)
from app.lead_sources import LeadSourceDefinition, PublicHtmlClient, PublicRosterAdapter
from app.llm_provider import HeuristicProvider, build_llm_provider


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "leads"


class LeadLayerTests(unittest.TestCase):
    def test_provider_defaults_to_heuristic_without_key(self):
        original = __import__("os").environ.get("OPENAI_API_KEY")
        try:
            __import__("os").environ.pop("OPENAI_API_KEY", None)
            provider = build_llm_provider(get_settings())
            self.assertEqual(provider.name, "heuristic")
        finally:
            if original is not None:
                __import__("os").environ["OPENAI_API_KEY"] = original

    def test_lead_score_and_status(self):
        score = compute_lead_score(90.0, 86.0, 74.0, 80.0)
        self.assertAlmostEqual(score, 84.3)
        self.assertEqual(lead_status_for_score(score, True, "developer", True), "auto_promote")
        self.assertEqual(lead_status_for_score(60.0, False, "planner", True), "needs_review")

    def test_contactability_score_uses_business_profile_and_email(self):
        score = contactability_score(
            [
                {"contact_type": "website_profile"},
                {"contact_type": "email"},
                {"contact_type": "phone"},
            ]
        )
        self.assertGreaterEqual(score, 88.0)

    def test_persona_classification(self):
        self.assertEqual(classify_persona("Managing Member", "ownership_officer"), "developer")
        self.assertEqual(classify_persona("Principal Architect", "firm_team"), "architect")
        self.assertEqual(classify_persona("Planning Board Chair", "civic_roster"), "planner")

    def test_roster_adapter_extracts_people_from_fixture(self):
        provider = HeuristicProvider()
        client = PublicHtmlClient(cache_dir=Path(tempfile.mkdtemp()), fixtures_dir=FIXTURES_DIR)
        adapter = PublicRosterAdapter(client, provider)
        result = adapter.fetch_people(
            LeadSourceDefinition(
                id="newark-planning",
                source_family="civic_roster",
                persona_hint="planner",
                title="Newark Planning Department",
                url="https://www.newarknj.gov/departments/planning",
                organization_name="City of Newark Planning Department",
            )
        )
        self.assertEqual(result.people[0]["full_name"], "Maria Planner")
        self.assertEqual(result.people[0]["email"], "mplanner@newarknj.gov")
        self.assertTrue(result.contacts["phones"])
