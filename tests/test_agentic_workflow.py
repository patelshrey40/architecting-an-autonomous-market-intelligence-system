import unittest
from unittest.mock import patch

from app.ownership_workflow import OwnershipWorkflowContext, build_ownership_workflow


class _FakeResult:
    def __init__(self, payload):
        self.payload = payload

    def to_state(self):
        return self.payload


class _FakeRecorderTool:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def fetch_latest_deed(self, parcel_ref, force_refresh=False):
        self.calls.append({"parcel_ref": parcel_ref, "force_refresh": force_refresh})
        return _FakeResult(self.payload)


class _FakeBusinessTool:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def match_owner(self, name, force_refresh=False):
        self.calls.append({"name": name, "force_refresh": force_refresh})
        return _FakeResult(self.payload)


class AgenticWorkflowTests(unittest.TestCase):
    def test_workflow_success_path_calls_business_lookup(self):
        recorder_tool = _FakeRecorderTool(
            {
                "source_name": "essex_county_recorder",
                "source_url": "https://example.com/recorder",
                "access_date": "2026-03-29",
                "municipality": "NEWARK",
                "block": "701",
                "lot": "1",
                "grouped_records": [],
                "latest_deed": {
                    "document_number": "2025000001",
                    "primary_grantee": "BROAD STREET HOLDINGS LLC",
                    "grantees": ["BROAD STREET HOLDINGS LLC"],
                    "recorded_at": None,
                },
            }
        )
        business_tool = _FakeBusinessTool(
            {
                "source_name": "nj_business_name_search",
                "source_url": "https://example.com/njbgs",
                "access_date": "2026-03-29",
                "owner_name": "BROAD STREET HOLDINGS LLC",
                "match_status": "matched",
                "matched_entity": {"entity_id": "0600999999", "match_score": 1.0},
                "candidates": [],
                "details": {"status": "Active", "registered_agent": "Smith Law", "officers": []},
            }
        )
        context = OwnershipWorkflowContext(
            connection=object(),
            tracker=None,
            recorder_tool=recorder_tool,
            business_tool=business_tool,
        )
        workflow = build_ownership_workflow(context)

        with patch(
            "app.ownership_workflow.load_parcel_context",
            return_value={"id": "parcel-0701-0001", "block": "701", "lot": "1"},
        ), patch(
            "app.ownership_workflow.persist_ownership_result",
            return_value={"parcel_id": "parcel-0701-0001", "enrichment_status": "completed"},
        ) as persist_mock:
            final_state = workflow.invoke(
                {"market_id": "newark", "parcel_id": "parcel-0701-0001", "status": "in_progress"}
            )

        self.assertEqual(final_state["status"], "completed")
        self.assertEqual(len(recorder_tool.calls), 1)
        self.assertEqual(len(business_tool.calls), 1)
        self.assertEqual(business_tool.calls[0]["name"], "BROAD STREET HOLDINGS LLC")
        persist_mock.assert_called_once()
        self.assertIsNotNone(final_state["entity_result"])

    def test_workflow_no_deed_skips_business_lookup(self):
        recorder_tool = _FakeRecorderTool(
            {
                "source_name": "essex_county_recorder",
                "source_url": "https://example.com/recorder",
                "access_date": "2026-03-29",
                "municipality": "NEWARK",
                "block": "701",
                "lot": "1",
                "grouped_records": [],
                "latest_deed": None,
            }
        )
        business_tool = _FakeBusinessTool({})
        context = OwnershipWorkflowContext(
            connection=object(),
            tracker=None,
            recorder_tool=recorder_tool,
            business_tool=business_tool,
        )
        workflow = build_ownership_workflow(context)

        with patch(
            "app.ownership_workflow.load_parcel_context",
            return_value={"id": "parcel-0701-0001", "block": "701", "lot": "1"},
        ), patch(
            "app.ownership_workflow.persist_ownership_result",
            return_value={"parcel_id": "parcel-0701-0001", "enrichment_status": "completed"},
        ) as persist_mock:
            final_state = workflow.invoke(
                {"market_id": "newark", "parcel_id": "parcel-0701-0001", "status": "in_progress"}
            )

        self.assertEqual(final_state["status"], "completed")
        self.assertEqual(len(recorder_tool.calls), 1)
        self.assertEqual(len(business_tool.calls), 0)
        persist_mock.assert_called_once()
        self.assertIsNone(persist_mock.call_args.args[4])
