import os
import tempfile
import unittest
from urllib.parse import urlsplit, urlunsplit
import uuid

import psycopg
from psycopg import OperationalError

from app.config import get_settings
from app.db import get_connection
from app.ingest import ingest_newark
from app.ownership import enrich_newark_ownership
from app.repository import get_parcel_detail, get_parcel_ownership, get_parcels, get_summary


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "newark")
OWNERSHIP_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "ownership")
LEAD_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "leads")
LEAD_SOURCE_CONFIG_PATH = os.path.join(LEAD_FIXTURES_DIR, "source-config.json")


class PostgisIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_database_url = os.environ.get("DATABASE_URL")
        cls.original_data_dir = os.environ.get("DATA_DIR")
        cls.original_celery_always_eager = os.environ.get("CELERY_TASK_ALWAYS_EAGER")
        cls.original_ownership_fixtures_dir = os.environ.get("OWNERSHIP_FIXTURES_DIR")
        cls.original_lead_fixtures_dir = os.environ.get("LEAD_FIXTURES_DIR")
        cls.original_lead_source_config_path = os.environ.get("LEAD_SOURCE_CONFIG_PATH")
        cls.temp_dir = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.temp_dir.name
        os.environ["CELERY_TASK_ALWAYS_EAGER"] = "1"
        os.environ["OWNERSHIP_FIXTURES_DIR"] = OWNERSHIP_FIXTURES_DIR
        os.environ["LEAD_FIXTURES_DIR"] = LEAD_FIXTURES_DIR
        os.environ["LEAD_SOURCE_CONFIG_PATH"] = LEAD_SOURCE_CONFIG_PATH
        default_database_url = cls.original_database_url or get_settings().database_url
        split = urlsplit(default_database_url)
        cls.test_database_name = "market_intel_test_%s" % uuid.uuid4().hex[:8]
        admin_url = urlunsplit((split.scheme, split.netloc, "/postgres", split.query, split.fragment))
        cls.test_database_url = urlunsplit(
            (split.scheme, split.netloc, "/%s" % cls.test_database_name, split.query, split.fragment)
        )
        try:
            admin_connection = psycopg.connect(admin_url, autocommit=True)
            with admin_connection.cursor() as cursor:
                cursor.execute('CREATE DATABASE "%s"' % cls.test_database_name)
            admin_connection.close()
        except OperationalError as exc:
            raise unittest.SkipTest("PostGIS admin database is not available for integration tests") from exc
        os.environ["DATABASE_URL"] = cls.test_database_url
        try:
            connection = get_connection(get_settings().database_url)
            connection.close()
        except OperationalError as exc:
            raise unittest.SkipTest("PostGIS is not available for integration tests") from exc

    @classmethod
    def tearDownClass(cls):
        split = urlsplit(cls.original_database_url or get_settings().database_url)
        admin_url = urlunsplit((split.scheme, split.netloc, "/postgres", split.query, split.fragment))
        try:
            admin_connection = psycopg.connect(admin_url, autocommit=True)
            with admin_connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                    (cls.test_database_name,),
                )
                cursor.execute('DROP DATABASE IF EXISTS "%s"' % cls.test_database_name)
            admin_connection.close()
        except OperationalError:
            pass

        if cls.original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = cls.original_database_url

        if cls.original_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = cls.original_data_dir
        if cls.original_celery_always_eager is None:
            os.environ.pop("CELERY_TASK_ALWAYS_EAGER", None)
        else:
            os.environ["CELERY_TASK_ALWAYS_EAGER"] = cls.original_celery_always_eager
        if cls.original_ownership_fixtures_dir is None:
            os.environ.pop("OWNERSHIP_FIXTURES_DIR", None)
        else:
            os.environ["OWNERSHIP_FIXTURES_DIR"] = cls.original_ownership_fixtures_dir
        if cls.original_lead_fixtures_dir is None:
            os.environ.pop("LEAD_FIXTURES_DIR", None)
        else:
            os.environ["LEAD_FIXTURES_DIR"] = cls.original_lead_fixtures_dir
        if cls.original_lead_source_config_path is None:
            os.environ.pop("LEAD_SOURCE_CONFIG_PATH", None)
        else:
            os.environ["LEAD_SOURCE_CONFIG_PATH"] = cls.original_lead_source_config_path
        cls.temp_dir.cleanup()

    def test_fixture_ingest_is_idempotent(self):
        result_one = ingest_newark(fixtures_dir=FIXTURES_DIR)
        result_two = ingest_newark(fixtures_dir=FIXTURES_DIR)
        self.assertEqual(result_one["parcels_loaded"], 2)
        self.assertEqual(result_two["parcels_loaded"], 2)

        connection = get_connection(get_settings().database_url)
        try:
            summary = get_summary(connection, "newark")
            self.assertEqual(summary["property_count"], 2)
            parcels = get_parcels(connection, "newark")
            self.assertEqual(len(parcels["features"]), 2)
            detail = get_parcel_detail(connection, "newark", "parcel-0701-0001")
            self.assertIsNotNone(detail)
            self.assertEqual(detail["parcel"]["building_count"], 1)
            self.assertTrue(detail["sources"])
        finally:
            connection.close()

    def test_fixture_ownership_enrichment_and_readthrough(self):
        ingest_newark(fixtures_dir=FIXTURES_DIR)
        result = enrich_newark_ownership(
            top_n=1,
            parcel_id="parcel-0701-0001",
            fixtures_dir=OWNERSHIP_FIXTURES_DIR,
        )
        self.assertEqual(result["status_counts"]["completed"], 1)

        connection = get_connection(get_settings().database_url)
        try:
            ownership = get_parcel_ownership(connection, "newark", "parcel-0701-0001")
            self.assertIsNotNone(ownership)
            self.assertEqual(ownership["enrichment_status"], "completed")
            self.assertEqual(ownership["deed"]["document_number"], "2025000001")
            self.assertEqual(ownership["organization"]["nj_entity_id"], "0600999999")
            self.assertEqual(len(ownership["deed_history"]), 2)
            self.assertEqual(len(ownership["financing_claims"]), 2)
            self.assertEqual(ownership["financing_claims"][0]["claim_type"], "assignment_of_mortgage")
            self.assertEqual(ownership["financing_claims"][0]["lender_name"], "HARBOR NOTE FUND I LLC")
            self.assertEqual(len(ownership["related_filings"]), 2)
            self.assertIsNotNone(ownership["recorder_history_last_enriched_at"])
            self.assertTrue(ownership["source_provenance"])
        finally:
            connection.close()

        from fastapi.testclient import TestClient
        from app.main import app

        response = TestClient(app).get("/api/newark/parcels/parcel-0701-0001/ownership")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deed"]["document_number"], "2025000001")

    def test_ownership_get_is_read_only_until_enqueued(self):
        ingest_newark(fixtures_dir=FIXTURES_DIR)

        from fastapi.testclient import TestClient
        from app.main import app

        response = TestClient(app).get("/api/newark/parcels/parcel-0701-0001/ownership")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["enrichment_status"], "not_started")
        self.assertIsNone(payload["deed"])

    def test_ownership_enqueue_endpoint_and_agent_run_status(self):
        ingest_newark(fixtures_dir=FIXTURES_DIR)

        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        enqueue_response = client.post("/api/newark/parcels/parcel-0701-0001/ownership/enrich")
        self.assertEqual(enqueue_response.status_code, 200)
        enqueue_payload = enqueue_response.json()
        self.assertIn("agent_run_id", enqueue_payload)
        self.assertTrue(enqueue_payload["run"]["events"])

        status_response = client.get("/api/agent-runs/%s" % enqueue_payload["agent_run_id"])
        self.assertEqual(status_response.status_code, 200)
        run_payload = status_response.json()
        self.assertEqual(run_payload["status"], "completed")
        self.assertTrue(any(event["step_name"] == "fetch_recorder" for event in run_payload["events"]))
        self.assertTrue(any(event["step_name"] == "completed" for event in run_payload["events"]))

        ownership_response = client.get("/api/newark/parcels/parcel-0701-0001/ownership")
        self.assertEqual(ownership_response.status_code, 200)
        ownership_payload = ownership_response.json()
        self.assertEqual(ownership_payload["enrichment_status"], "completed")
        self.assertEqual(ownership_payload["deed"]["document_number"], "2025000001")
        self.assertEqual(len(ownership_payload["deed_history"]), 2)
        self.assertEqual(len(ownership_payload["financing_claims"]), 2)

    def test_fixture_ownership_ambiguous_match_sets_review_status(self):
        ingest_newark(fixtures_dir=FIXTURES_DIR)
        result = enrich_newark_ownership(
            top_n=1,
            parcel_id="parcel-0701-0002",
            fixtures_dir=OWNERSHIP_FIXTURES_DIR,
        )
        self.assertEqual(result["status_counts"]["needs_review"], 1)

        connection = get_connection(get_settings().database_url)
        try:
            ownership = get_parcel_ownership(connection, "newark", "parcel-0701-0002")
            self.assertEqual(ownership["enrichment_status"], "needs_review")
            self.assertEqual(len(ownership["match_candidates"]), 2)
            self.assertEqual(ownership["organization"]["matched_to_nj_entity"], False)
        finally:
            connection.close()

    def test_agent_ops_endpoints_and_review_approval(self):
        ingest_newark(fixtures_dir=FIXTURES_DIR)

        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        enqueue_response = client.post("/api/newark/parcels/parcel-0701-0002/ownership/enrich")
        self.assertEqual(enqueue_response.status_code, 200)
        run_id = enqueue_response.json()["agent_run_id"]

        list_response = client.get("/api/agent-runs?workflow_type=newark_parcel_ownership_v1&review_state=pending")
        self.assertEqual(list_response.status_code, 200)
        self.assertTrue(any(run["id"] == run_id for run in list_response.json()))

        tasks_response = client.get(f"/api/agent-runs/{run_id}/tasks")
        artifacts_response = client.get(f"/api/agent-runs/{run_id}/artifacts")
        proposals_response = client.get(f"/api/agent-runs/{run_id}/proposals")
        self.assertEqual(tasks_response.status_code, 200)
        self.assertEqual(artifacts_response.status_code, 200)
        self.assertEqual(proposals_response.status_code, 200)
        self.assertTrue(tasks_response.json())
        self.assertTrue(artifacts_response.json())
        proposals = proposals_response.json()
        bundle = next(
            proposal for proposal in proposals if proposal["proposal_type"] == "ownership_promotion_bundle"
        )
        self.assertEqual(bundle["review_state"], "pending")

        approve_response = client.post(f"/api/agent-reviews/{bundle['id']}/approve")
        self.assertEqual(approve_response.status_code, 200)
        approve_payload = approve_response.json()
        self.assertEqual(approve_payload["review"]["status"], "approved")

        status_response = client.get(f"/api/agent-runs/{run_id}")
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["review_state"], "approved")

    def test_lead_enrichment_builds_ranked_newark_leads(self):
        ingest_newark(fixtures_dir=FIXTURES_DIR)
        enrich_newark_ownership(
            top_n=1,
            parcel_id="parcel-0701-0001",
            fixtures_dir=OWNERSHIP_FIXTURES_DIR,
        )

        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        enqueue_response = client.post("/api/newark/leads/enrich?top_n=5")
        self.assertEqual(enqueue_response.status_code, 200)
        run_id = enqueue_response.json()["agent_run_id"]

        status_response = client.get(f"/api/agent-runs/{run_id}")
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["status"], "completed")

        leads_response = client.get("/api/newark/leads")
        self.assertEqual(leads_response.status_code, 200)
        payload = leads_response.json()
        self.assertTrue(payload["items"])
        lead = payload["items"][0]
        self.assertIn(lead["persona"], {"developer", "planner", "architect"})
        self.assertTrue(lead["contacts"])

        detail_response = client.get(f"/api/newark/leads/{lead['id']}")
        self.assertEqual(detail_response.status_code, 200)
        detail = detail_response.json()
        self.assertTrue(detail["why_person_matters"])
        self.assertTrue(detail["source_provenance"])

        ownership_response = client.get("/api/newark/parcels/parcel-0701-0001/ownership")
        self.assertEqual(ownership_response.status_code, 200)
        self.assertTrue(ownership_response.json()["related_leads"])
