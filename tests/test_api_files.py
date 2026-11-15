import csv
import io
import json
from pathlib import Path

from fastapi.testclient import TestClient

from autumn_ledger.api.server import create_app
from autumn_ledger.demo import request_data, synthetic_event
from autumn_ledger.records.csv_input import CSV_HEADERS, parse_csv
from tests.support import LedgerCase


def csv_events(events):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_HEADERS)
    writer.writeheader()
    for event in events:
        writer.writerow({**event, "family": "true" if event["family"] else "false"})
    return stream.getvalue()


class ApiAndFilesTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.path)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_health_and_openapi_have_business_routes(self):
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.json()["ok"])
        self.assertEqual(health.json()["events"], 5)
        routes = self.client.get("/openapi.json").json()["paths"]
        self.assertIn("/editions/{identity}/publish", routes)
        self.assertIn("/sources/{source}/batches", routes)
        self.assertIn("/analysis/preview", routes)

    def test_http_complete_business_lifecycle(self):
        create = self.client.post("/editions", json={"request_key": "http", "request": request_data()})
        self.assertEqual(create.status_code, 200, create.text)
        identity = create.json()["id"]
        approve = self.client.post(f"/editions/{identity}/decision", json={"action": "approve", "expected_version": 1,
                                   "actor": "analyst", "note": "来源和金额已核对"})
        self.assertEqual(approve.status_code, 200, approve.text)
        publish = self.client.post(f"/editions/{identity}/publish", json={"channel": "weekly", "expected_version": 2,
                                   "expected_generation": 0, "actor": "publisher"})
        self.assertEqual(publish.status_code, 200, publish.text)
        current = self.client.get("/public/weekly").json()
        self.assertEqual(current["edition_id"], identity)
        self.assertEqual(current["generation"], 1)
        self.assertFalse(current["stale"])
        self.assertEqual(current["result"]["periods"]["current"]["cube"]["totals"]["orders"], 3)

    def test_http_transport_does_not_coerce_event_or_request_values(self):
        event = synthetic_event("bad")
        event["gross_cents"] = "50000"
        result = self.client.post("/sources/alpha/batches", json={"batch_id": "bad", "events": [event]})
        self.assertEqual(result.status_code, 422)
        self.assertEqual(result.json()["error"]["code"], "invalid_integer")
        request = request_data(family_only="true")
        result = self.client.post("/analysis/preview", json=request)
        self.assertEqual(result.status_code, 422)
        result = self.client.post("/analysis/preview", json={**request_data(), "unexpected": 2})
        self.assertEqual(result.status_code, 422)

    def test_http_domain_conflict_is_structured(self):
        event = dict(self.events[0], gross_cents=333)
        response = self.client.post("/sources/alpha/batches", json={"batch_id": "collision", "events": [event]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "conflict")
        self.assertIn("message", response.json()["error"])
        missing = self.client.get("/editions/not-existing")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "not_found")

    def test_http_csv_import_and_replay(self):
        content = csv_events([synthetic_event("csv")])
        first = self.client.post("/sources/alpha/csv", json={"batch_id": "csv", "content": content})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["accepted"], 1)
        second = self.client.post("/sources/alpha/csv", json={"batch_id": "csv", "content": content})
        self.assertTrue(second.json()["replayed"])
        history = self.client.get("/sources/alpha/orders/csv").json()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["gross_cents"], 50000)

    def test_csv_bom_and_header_reordering(self):
        event = synthetic_event("bom")
        content = "\ufeff" + csv_events([event])
        parsed = parse_csv(content)
        self.assertEqual(parsed[0]["order_id"], "bom")
        stream = io.StringIO(newline="")
        headers = list(reversed(CSV_HEADERS))
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        writer.writerow({**event, "family": "true"})
        self.assertEqual(parse_csv(stream.getvalue()), parsed)

    def test_csv_unknown_duplicate_and_missing_headers(self):
        self.assertError("invalid_csv", parse_csv, "order_id,order_id\na,b\n")
        self.assertError("invalid_csv", parse_csv, "order_id,extra\na,b\n")
        self.assertError("invalid_csv", parse_csv, "")
        header = ",".join(CSV_HEADERS) + "\n"
        self.assertError("empty_csv", parse_csv, header)
        self.assertError("invalid_csv", parse_csv, header + "short,row\n")

    def test_csv_strict_integer_and_boolean_grammar(self):
        original = csv_events([synthetic_event("grammar")])
        for bad in ("1.0", "-1", "+1", " 1", "１"):
            event = synthetic_event("grammar")
            event["sequence"] = bad
            with self.subTest(value=bad):
                self.assertError("invalid_csv_integer", parse_csv, csv_events([event]))
        invalid = original.replace(",true,", ",TRUE,")
        self.assertError("invalid_csv_boolean", parse_csv, invalid)
        self.assertError("invalid_csv", parse_csv, original + "\x00")

    def test_csv_business_error_includes_line(self):
        event = synthetic_event("invalid-refund", refund=999999)
        error = self.assertError("excess_refund", parse_csv, csv_events([event]))
        self.assertEqual(error.details["line"], 2)
        self.assertEqual(error.status, 422)
        before = self.ledger.database.revision()
        self.assertError("excess_refund", self.ledger.files.ingest_csv, "alpha", "bad-file", csv_events([event]))
        self.assertEqual(self.ledger.database.revision(), before)

    def test_csv_inspection_does_not_write(self):
        before = self.ledger.database.revision()
        event = synthetic_event("inspect")
        result = self.ledger.files.inspect(csv_events([event, event]))
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["orders"], 1)
        self.assertEqual(result["duplicate_event_ids"], ["inspect-1"])
        self.assertEqual(result["duplicate_sequences"], [["inspect", 1]])
        self.assertEqual(self.ledger.database.revision(), before)
        self.assertError("not_found", self.ledger.intake.history, "alpha", "inspect")

    def test_jsonl_parsing_and_atomic_validation(self):
        content = "\n" + json.dumps(synthetic_event("jsonl")) + "\n\n"
        receipt = self.ledger.files.ingest_jsonl("alpha", "jsonl", content)
        self.assertEqual(receipt["accepted"], 1)
        self.assertError("invalid_jsonl", self.ledger.files.ingest_jsonl, "alpha", "bad", content + "not-json")
        self.assertError("invalid_batch", self.ledger.files.ingest_jsonl, "alpha", "empty", "\n")
        self.assertEqual(len(self.ledger.intake.history("alpha", "jsonl")), 1)

    def test_http_preview_profiles_and_anomalies(self):
        preview = self.client.post("/analysis/preview", json=request_data())
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["current"]["orders"], 3)
        profile = self.client.post("/analysis/profile", json={"request": request_data(), "dimension": "origin"})
        self.assertEqual(profile.status_code, 200, profile.text)
        self.assertEqual(profile.json()["profiles"]["current"]["dimension"], "origin")
        anomaly = self.client.post("/analysis/anomalies", json={"request": request_data()})
        self.assertEqual(anomaly.status_code, 200, anomaly.text)
        self.assertEqual(anomaly.json()["flags"], [])

    def test_http_edition_investigation_routes(self):
        edition = self.edition()
        identity = edition["id"]
        for endpoint in ("overview", "explanation", "narrative", "lineage", "event-reconciliation",
                         "reference-drift", "coverage-disputes", "coverage-query", "issues", "drilldown"):
            with self.subTest(endpoint=endpoint):
                response = self.client.get(f"/editions/{identity}/{endpoint}")
                self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.client.post(f"/editions/{identity}/verify").json()["ok"])

    def test_http_exports_have_correct_media_and_content(self):
        identity = self.edition()["id"]
        csv_response = self.client.get(f"/editions/{identity}/tables/origins.csv")
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("text/csv", csv_response.headers["content-type"])
        self.assertIn("attachment", csv_response.headers["content-disposition"])
        self.assertEqual(len(list(csv.DictReader(io.StringIO(csv_response.text)))), 2)
        archive = self.client.get(f"/editions/{identity}/export.zip")
        self.assertEqual(archive.headers["content-type"], "application/zip")
        self.assertTrue(self.ledger.export.verify_bundle(archive.content)["ok"])
        document = self.client.get(f"/editions/{identity}/export.json").json()
        self.assertEqual(document["edition_id"], identity)

    def test_receipt_reconciliation_and_contribution_changes(self):
        before = self.ledger.database.revision()
        self.ledger.intake.ingest("alpha", "move", [synthetic_event("a1", origin="nj", sequence=2, refund=10000)])
        receipt = self.ledger.receipts.inspect("alpha")
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["events"], 6)
        self.assertEqual(receipt["heads"], 5)
        change = self.ledger.receipts.contributions("alpha", "a1")["revisions"][-1]
        self.assertTrue(change["group_changed"])
        self.assertEqual(change["total_change"]["net_cents"], -10000)
        self.assertEqual(change["total_change"]["orders"], 0)
        delta = self.ledger.receipts.between_snapshots("alpha", before, self.ledger.database.revision())
        self.assertEqual(delta["changed"][0]["order_id"], "a1")
        self.assertIn("origin", delta["changed"][0]["changed_fields"])

    def test_audit_is_contiguous_and_does_not_record_failed_write(self):
        before = self.ledger.database.revision()
        self.assertError("not_found", self.ledger.intake.ingest, "missing", "failed", [synthetic_event("failed")])
        audit = self.ledger.database.audit(limit=500)
        self.assertEqual(len(audit), before)
        self.assertEqual([row["revision"] for row in audit], list(range(1, before + 1)))
        self.assertEqual(audit[-1]["action"], "batch.ingest")
        self.assertEqual(audit[-1]["payload"]["accepted"], 5)
        self.assertTrue(self.ledger.database.integrity()["ok"])

    def test_restart_preserves_edition_and_channel(self):
        edition = self.edition()
        self.publish(edition)
        from autumn_ledger.application import Ledger
        restarted = Ledger(self.path)
        self.assertEqual(restarted.review.channel("weekly")["edition_id"], edition["id"])
        self.assertEqual(restarted.editions.get(edition["id"])["state"], "published")
        self.assertTrue(restarted.editions.verify(edition["id"])["ok"])
        self.assertTrue(restarted.database.integrity()["ok"])
