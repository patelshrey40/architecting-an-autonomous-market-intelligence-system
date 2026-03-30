import unittest
from pathlib import Path

from app.ownership import (
    _classifier_document_category,
    choose_best_njbgs_match,
    extract_financing_claims,
    normalize_org_name,
    group_recorder_records,
    parse_essex_search_results,
    parse_njbgs_entity_details,
    parse_njbgs_search_results,
    select_latest_deed,
)


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "ownership"


class OwnershipParserTests(unittest.TestCase):
    def test_group_recorder_records_deduplicates_parties(self):
        records = [
            {
                "document_type": "DEED",
                "direct_party": "OWNER A, LLC",
                "indirect_party": "OWNER ENTITY LLC",
                "document_number": "2024000101",
                "recorded_at": __import__("datetime").date(2024, 1, 5),
                "municipality": "NEWARK",
                "block": "701",
                "lot": "1",
                "book": "100",
                "page": "5",
                "consideration": 125000.00,
            },
            {
                "document_type": "DEED",
                "direct_party": "OWNER A, LLC",
                "indirect_party": "OWNER ENTITY LLC",
                "document_number": "2024000101",
                "recorded_at": __import__("datetime").date(2024, 1, 5),
                "municipality": "NEWARK",
                "block": "701",
                "lot": "1",
                "book": None,
                "page": None,
                "consideration": 124900.00,
            },
            {
                "document_type": "MORTGAGE",
                "direct_party": "BANK A",
                "indirect_party": "OWNER ENTITY LLC",
                "document_number": "300001",
                "recorded_at": __import__("datetime").date(2024, 2, 1),
                "municipality": "NEWARK",
                "block": "701",
                "lot": "1",
                "book": None,
                "page": None,
                "consideration": 2000000.00,
            },
        ]
        grouped_records = group_recorder_records(records)
        self.assertEqual(len(grouped_records), 2)
        deed_record = next(
            grouped_record
            for grouped_record in grouped_records
            if grouped_record["document_type"] == "DEED"
        )
        self.assertEqual(len(deed_record["grantors"]), 1)
        self.assertEqual(deed_record["grantors"][0], "OWNER A, LLC")
        self.assertEqual(len(deed_record["grantees"]), 1)
        self.assertEqual(deed_record["grantees"][0], "OWNER ENTITY LLC")
        self.assertEqual(deed_record["consideration"], 125000.0)

        latest = select_latest_deed(records)
        self.assertEqual(latest["document_type"], "DEED")
        self.assertEqual(latest["document_number"], "2024000101")

    def test_normalize_org_name(self):
        self.assertEqual(normalize_org_name("Broad Street Holdings LLC"), "broad street holdings")
        self.assertEqual(normalize_org_name("Broad Street Holdings, Inc."), "broad street holdings")

    def test_parse_essex_results_and_choose_latest_deed(self):
        html = (FIXTURES_DIR / "essex-block-701-lot-1.html").read_text()
        rows = parse_essex_search_results(html)
        self.assertEqual(len(rows), 5)
        latest = select_latest_deed(rows)
        self.assertEqual(latest["document_number"], "2025000001")
        self.assertEqual(latest["primary_grantee"], "BROAD STREET HOLDINGS LLC")
        self.assertEqual(len(latest["grantors"]), 2)
        financing_claims = extract_financing_claims(rows)
        self.assertEqual(len(financing_claims), 2)
        self.assertEqual(financing_claims[0]["document_category"], "assignment_of_mortgage")
        self.assertEqual(financing_claims[0]["lender_name"], "HARBOR NOTE FUND I LLC")
        self.assertEqual(financing_claims[1]["document_category"], "mortgage")
        self.assertEqual(financing_claims[1]["lender_name"], "HUDSON CAPITAL BANK")

    def test_financing_categories_cover_liens_and_ucc(self):
        html = (FIXTURES_DIR / "essex-block-702-lot-1.html").read_text()
        rows = parse_essex_search_results(html)
        financing_claims = extract_financing_claims(rows)
        self.assertEqual(len(financing_claims), 2)
        self.assertEqual(financing_claims[0]["document_category"], "lis_pendens")
        self.assertEqual(financing_claims[1]["document_category"], "lien")
        self.assertIsNone(financing_claims[0]["consideration"])
        self.assertEqual(_classifier_document_category("UCC LIEN"), "ucc")

    def test_parse_essex_results_with_consideration(self):
        html = """
        <html>
          <body>
            <table id=\"ctl00_ContentPlaceHolder1_dgdDeedMort\">
              <tr>
                <th>Type</th>
                <th>Direct Party</th>
                <th>Indirect Party</th>
                <th>Instrument #</th>
                <th>Recorded</th>
                <th>Town Name</th>
                <th>Block</th>
                <th>Lot</th>
                <th>Book</th>
                <th>Page</th>
                <th>Consideration</th>
              </tr>
              <tr>
                <td>DEED</td>
                <td>NEW OWNER LLC</td>
                <td>OWNER LLC</td>
                <td>2026000101</td>
                <td>5/18/2026</td>
                <td>NEWARK</td>
                <td>701</td>
                <td>1</td>
                <td>110</td>
                <td>20</td>
                <td>$1,250,000</td>
              </tr>
            </table>
          </body>
        </html>
        """
        rows = parse_essex_search_results(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["consideration"], 1250000.0)
        latest = select_latest_deed(rows)
        self.assertEqual(latest["document_number"], "2026000101")
        self.assertEqual(latest["consideration"], 1250000.0)

    def test_parse_essex_no_results(self):
        html = "<html><body><span>No results found</span></body></html>"
        self.assertEqual(parse_essex_search_results(html), [])

    def test_parse_njbgs_results_and_choose_exact_match(self):
        html = (FIXTURES_DIR / "njbgs-broad-street-holdings-llc.html").read_text()
        results = parse_njbgs_search_results(html)
        match = choose_best_njbgs_match("Broad Street Holdings LLC", results)
        self.assertEqual(len(results), 1)
        self.assertEqual(match["status"], "matched")
        self.assertEqual(match["match"]["entity_id"], "0600999999")

    def test_choose_ambiguous_match(self):
        html = (FIXTURES_DIR / "njbgs-broad-development.html").read_text()
        results = parse_njbgs_search_results(html)
        match = choose_best_njbgs_match("Broad Development", results)
        self.assertEqual(match["status"], "needs_review")
        self.assertEqual(len(match["candidates"]), 2)

    def test_parse_optional_entity_details(self):
        html = """
        <html>
          <table>
            <tr><th>Status</th><td>Active</td></tr>
            <tr><th>Registered Agent</th><td>Smith Law Firm</td></tr>
          </table>
          <table>
            <tr><th>Name</th><th>Title</th></tr>
            <tr><td>Jane Doe</td><td>Managing Member</td></tr>
            <tr><td>John Roe</td><td>Officer</td></tr>
          </table>
        </html>
        """
        details = parse_njbgs_entity_details(html)
        self.assertEqual(details["status"], "Active")
        self.assertEqual(details["registered_agent"], "Smith Law Firm")
        self.assertEqual(len(details["officers"]), 2)
