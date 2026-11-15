import json
from collections import Counter, defaultdict

from ..errors import require
from ..values import fingerprint, identifier, integer, safe_ratio
from .contracts import contribution


class Receipts:
    def __init__(self, database, intake, catalog):
        self.database = database
        self.intake = intake
        self.catalog = catalog

    def inspect(self, source, revision=None):
        identifier(source)
        with self.database.transaction() as connection:
            revision = self.database.snapshot_revision(connection, revision)
            reference = self.catalog.get("source", source, revision, connection)
            batches = connection.execute(
                "SELECT * FROM batches WHERE source=? AND revision<=? ORDER BY revision", (source, revision)
            ).fetchall()
            events = connection.execute(
                "SELECT * FROM events WHERE source=? AND revision<=? ORDER BY order_id,sequence", (source, revision)
            ).fetchall()
            heads = self.intake.heads(revision, [source], connection)
        batches = [dict(row) for row in batches]
        grouped = defaultdict(list)
        failures = []
        for row in events:
            payload = json.loads(row["payload"])
            if fingerprint(payload) != row["digest"]:
                failures.append({"code": "event_digest", "event_id": row["event_id"]})
            if payload["event_id"] != row["event_id"] or payload["order_id"] != row["order_id"]:
                failures.append({"code": "event_identity", "event_id": row["event_id"]})
            grouped[row["order_id"]].append({"revision": row["revision"], **payload})
        by_revision = Counter(row["revision"] for row in events)
        for batch in batches:
            actual = by_revision[batch["revision"]]
            if actual != batch["accepted"]:
                failures.append({"code": "batch_count", "batch_id": batch["id"], "expected": batch["accepted"], "actual": actual})
        revisions = {batch["revision"] for batch in batches}
        orphan_revisions = sorted(set(by_revision) - revisions)
        if orphan_revisions:
            failures.append({"code": "orphan_event_revision", "revisions": orphan_revisions})
        orders = []
        for order_id, history in sorted(grouped.items()):
            sequences = [row["sequence"] for row in history]
            head = history[-1]
            gaps = []
            previous = 0
            for sequence in sequences:
                if sequence > previous + 1:
                    gaps.append({"after": previous, "before": sequence})
                previous = sequence
            orders.append({"order_id": order_id, "events": len(history), "head_sequence": head["sequence"],
                           "head_event_id": head["event_id"], "head_revision": head["revision"],
                           "state": head["state"], "sequence_gaps": gaps,
                           "first_received_revision": min(row["revision"] for row in history),
                           "last_received_revision": max(row["revision"] for row in history)})
        return {"source": reference, "revision": revision, "ok": not failures, "failures": failures,
                "batches": batches, "batch_count": len(batches), "events": len(events),
                "accepted": sum(row["accepted"] for row in batches),
                "duplicate_receipts": sum(row["duplicates"] for row in batches),
                "heads": len(heads), "orders": orders,
                "note": "序号可以跳跃；缺号只提示上游传输范围，不认定漏收或自动补造事件"}

    def contributions(self, source, order_id):
        history = self.intake.history(source, order_id)
        result = []
        previous = None
        for event in history:
            current = contribution(event)
            before = contribution(previous) if previous else {key: 0 for key in current}
            changes = {key: current[key] - before[key] for key in current}
            previous_group = None if previous is None else {
                "origin": previous["origin"], "destination": previous["destination"], "travel_day": previous["travel_day"]}
            current_group = {"origin": event["origin"], "destination": event["destination"], "travel_day": event["travel_day"]}
            result.append({"sequence": event["sequence"], "event_id": event["event_id"], "revision": event["revision"],
                           "current_contribution": current, "previous_contribution": before,
                           "total_change": changes, "previous_group": previous_group,
                           "current_group": current_group, "group_changed": previous_group is not None and previous_group != current_group})
            previous = event
        return {"source": source, "order_id": order_id, "revisions": result,
                "note": "仅展示单订单修订贡献；报告仍使用固定版本全量重算"}

    def activity(self, source, after=0, through=None):
        identifier(source)
        integer(after, "after")
        with self.database.transaction() as connection:
            through = self.database.snapshot_revision(connection, through)
            require(after <= through, "invalid_range", "起始版本不能晚于结束版本")
            self.catalog.get("source", source, through, connection)
            rows = connection.execute(
                "SELECT revision,COUNT(*) events,COUNT(DISTINCT order_id) orders FROM events WHERE source=? AND revision>? AND revision<=? GROUP BY revision ORDER BY revision",
                (source, after, through),
            ).fetchall()
            batches = connection.execute(
                "SELECT * FROM batches WHERE source=? AND revision>? AND revision<=? ORDER BY revision",
                (source, after, through),
            ).fetchall()
        indexed = {row["revision"]: dict(row) for row in rows}
        result = []
        for batch in batches:
            item = indexed.get(batch["revision"], {"revision": batch["revision"], "events": 0, "orders": 0})
            result.append({**item, "batch_id": batch["id"], "duplicates": batch["duplicates"],
                           "duplicate_fraction": safe_ratio(batch["duplicates"], batch["duplicates"] + batch["accepted"])})
        return {"source": source, "after": after, "through": through, "activity": result}

    def between_snapshots(self, source, before, after):
        identifier(source)
        integer(before, "before")
        integer(after, "after")
        require(before <= after, "invalid_range", "起始版本不能晚于结束版本")
        with self.database.transaction() as connection:
            self.database.snapshot_revision(connection, after)
            old = {(row["source"], row["order_id"]): row for row in self.intake.heads(before, [source], connection)}
            new = {(row["source"], row["order_id"]): row for row in self.intake.heads(after, [source], connection)}
        added = []
        changed = []
        for key, row in sorted(new.items()):
            if key not in old:
                added.append({"order_id": key[1], "sequence": row["sequence"]})
            elif old[key]["sequence"] != row["sequence"]:
                fields = [field for field in ("origin", "destination", "travel_day", "state", "gross_cents", "refund_cents", "adults", "children")
                          if old[key][field] != row[field]]
                changed.append({"order_id": key[1], "before_sequence": old[key]["sequence"], "after_sequence": row["sequence"],
                                "changed_fields": fields})
        return {"source": source, "before": before, "after": after, "added": added, "changed": changed}
