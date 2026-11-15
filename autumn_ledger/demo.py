import csv
import io
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from .api.server import create_app
from .errors import LedgerError


def synthetic_event(order_id, day="2026-11-16", origin="sz", destination="park", sequence=1,
                    source="alpha", gross=50000, refund=0, state="booked"):
    return {"event_id": f"{order_id}-{sequence}", "order_id": order_id, "sequence": sequence,
            "occurred_at": f"2026-11-{10 + sequence:02d}T10:00:00+08:00", "state": state,
            "origin": origin, "destination": destination, "travel_day": day,
            "family": True, "adults": 2, "children": 1, "gross_cents": gross, "refund_cents": refund}


def request_data(**changes):
    result = {"name": "合成秋假周分析", "start": "2026-11-16", "end": "2026-11-22",
              "baseline_start": "2026-11-09", "baseline_end": "2026-11-15",
              "cities": ["sz", "nj"], "sources": ["alpha"], "family_only": True}
    result.update(changes)
    return result


def seed(ledger, complete=True):
    for identity, name in (("sz", "合成深圳"), ("nj", "合成南京"), ("sh", "合成上海")):
        ledger.catalog.put("city", identity, {"name": name, "region": "合成区域"})
    ledger.catalog.put("destination", "park", {"name": "合成乐园", "city": "sh", "category": "乐园"})
    ledger.catalog.put("destination", "museum", {"name": "合成博物馆", "city": "nj", "category": "研学"})
    ledger.catalog.put("source", "alpha", {"name": "合成来源甲"})
    ledger.catalog.put("source", "beta", {"name": "合成来源乙"})
    ledger.calendars.put("sz-autumn", {"city": "sz", "start": "2026-11-16", "end": "2026-11-20", "label": "合成校历"})
    ledger.calendars.put("nj-autumn", {"city": "nj", "start": "2026-11-19", "end": "2026-11-22", "label": "合成校历"})
    from .values import date_range
    if complete:
        for city in ("sz", "nj"):
            for day in date_range("2026-11-09", "2026-11-22"):
                ledger.coverage.put({"source": "alpha", "city": city, "day": day.isoformat(), "status": "complete"})
    events = [synthetic_event("a1"), synthetic_event("a2", "2026-11-17"),
              synthetic_event("a3", "2026-11-21", "nj", "museum", gross=20000),
              synthetic_event("b1", "2026-11-09", gross=30000),
              synthetic_event("b2", "2026-11-14", "nj", "museum", gross=10000)]
    ledger.intake.ingest("alpha", "synthetic-initial", events)
    return events


def run_demo():
    with tempfile.TemporaryDirectory(prefix="autumn-demo-") as directory:
        app = create_app(Path(directory) / "ledger.sqlite3")
        ledger = app.state.ledger
        events = seed(ledger)
        with TestClient(app) as client:
            response = client.post("/editions", json={"request_key": "demo-1", "request": request_data()})
            assert response.status_code == 200, response.text
            draft = response.json()
            identity = draft["id"]
            assert draft["summary"]["orders"] == 3
            assert draft["summary"]["net_cents"] == 120000
            approved = client.post(f"/editions/{identity}/decision", json={"action": "approve", "expected_version": 1,
                                  "actor": "analyst", "note": "合成来源完整，金额与订单已对账"})
            assert approved.status_code == 200, approved.text
            published = client.post(f"/editions/{identity}/publish", json={"channel": "weekly", "expected_version": 2,
                                   "expected_generation": 0, "actor": "publisher"})
            assert published.status_code == 200, published.text
            bundle = client.get(f"/editions/{identity}/export.zip")
            assert bundle.status_code == 200
            assert ledger.export.verify_bundle(bundle.content)["ok"]
            revision = ledger.database.revision()
            update = synthetic_event("a1", sequence=2, refund=10000)
            correction = client.post("/sources/alpha/batches", json={"batch_id": "correction", "events": [update]})
            assert correction.status_code == 200, correction.text
            assert client.get("/public/weekly").json()["stale"]
            stale = client.post(f"/editions/{identity}/publish", json={"channel": "other", "expected_version": 3,
                               "expected_generation": 0, "actor": "publisher"})
            assert stale.status_code == 409
            assert stale.json()["error"]["code"] == "stale_edition"
            assert client.post(f"/editions/{identity}/verify").json()["ok"]
            replacement = client.post("/editions", json={"request_key": "demo-2", "request": request_data()}).json()
            assert replacement["summary"]["net_cents"] == 110000
            assert ledger.database.integrity()["ok"]
            return {"ok": True, "orders": 3, "original_net_cents": 120000, "corrected_net_cents": 110000,
                    "stale_publish_blocked": True, "old_snapshot_reproduced": True,
                    "bundle_verified": True, "revision_before_correction": revision,
                    "final_revision": ledger.database.revision()}
