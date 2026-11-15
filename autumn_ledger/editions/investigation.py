from collections import Counter, defaultdict

from ..errors import require
from ..values import identifier, integer, page_size, safe_ratio
from ..analysis.cubes import totals


class Investigation:
    def __init__(self, database, editions, intake, catalog):
        self.database = database
        self.editions = editions
        self.intake = intake
        self.catalog = catalog

    def drilldown(self, edition_id, period="current", origin=None, destination=None, source=None,
                  day=None, state=None, limit=100, offset=0):
        page_size(limit, offset)
        edition = self.editions.get(edition_id)
        require(period in edition["result"]["periods"], "unknown_period", "分析没有该期间")
        filters = {"origin": origin, "destination": destination, "source": source,
                   "travel_day": day, "state": state}
        for field in ("origin", "destination", "source"):
            if filters[field] is not None:
                identifier(filters[field], field)
        if state is not None:
            require(state in {"booked", "cancelled"}, "invalid_state", "订单状态无效")
        if day is not None:
            from ..values import iso_day
            iso_day(day)
        records = edition["result"]["periods"][period]["orders"]
        selected = [row for row in records if all(value is None or row[field] == value for field, value in filters.items())]
        return {"edition_id": edition_id, "data_revision": edition["data_revision"], "period": period,
                "filters": {key: value for key, value in filters.items() if value is not None},
                "total": len(selected), "totals": totals(selected), "offset": offset,
                "items": selected[offset:offset + limit]}

    def order_trace(self, edition_id, source, order_id):
        identifier(source)
        identifier(order_id)
        edition = self.editions.get(edition_id)
        history = self.intake.history(source, order_id)
        eligible = [row for row in history if row["revision"] <= edition["data_revision"]]
        selected = max(eligible, key=lambda row: row["sequence"]) if eligible else None
        occurrences = []
        for period, content in edition["result"]["periods"].items():
            for row in content["orders"]:
                if row["source"] == source and row["order_id"] == order_id:
                    occurrences.append({"period": period, "sequence": row["sequence"], "event_id": row["event_id"]})
        return {"edition_id": edition_id, "data_revision": edition["data_revision"],
                "source": source, "order_id": order_id, "selected_at_snapshot": selected,
                "included_in_periods": occurrences,
                "history": [{**row, "visible_at_snapshot": row["revision"] <= edition["data_revision"]} for row in history],
                "has_later_changes": any(row["revision"] > edition["data_revision"] for row in history)}

    def reference_drift(self, edition_id):
        edition = self.editions.get(edition_id)
        saved = edition["result"]["references"]
        changes = []
        kind_map = {"cities": "city", "destinations": "destination", "sources": "source"}
        with self.database.transaction() as connection:
            current_revision = self.database.revision(connection)
            for group, kind in kind_map.items():
                for before in saved[group]:
                    after = self.catalog.get(kind, before["id"], connection=connection, required=False)
                    if before != after:
                        fields = sorted(key for key in set(before) | set(after or {}) if before.get(key) != (after or {}).get(key))
                        changes.append({"kind": kind, "id": before["id"], "changed_fields": fields,
                                        "snapshot": before, "current": after})
        return {"edition_id": edition_id, "data_revision": edition["data_revision"],
                "current_revision": current_revision, "changes": changes}

    def event_reconciliation(self, edition_id):
        edition = self.editions.get(edition_id)
        result = edition["result"]
        all_periods = []
        identities = set()
        for period, content in result["periods"].items():
            for row in content["orders"]:
                identity = (row["source"], row["order_id"])
                require(identity not in identities, "duplicate_head", "订单头在不重叠期间中重复", 500)
                identities.add(identity)
                all_periods.append({"period": period, **row})
        lineage_keys = {(row["period"], row["source"], row["order_id"], row["sequence"]) for row in result["lineage"]}
        order_keys = {(row["period"], row["source"], row["order_id"], row["sequence"]) for row in all_periods}
        return {"edition_id": edition_id, "ok": lineage_keys == order_keys,
                "order_heads": len(order_keys), "lineage_entries": len(result["lineage"]),
                "missing_lineage": [list(x) for x in sorted(order_keys - lineage_keys)],
                "orphan_lineage": [list(x) for x in sorted(lineage_keys - order_keys)]}

    def publication_impact(self, after_revision):
        integer(after_revision, "after_revision")
        with self.database.transaction() as connection:
            current = self.database.snapshot_revision(connection)
            require(after_revision <= current, "future_revision", "版本尚不存在")
            changed_events = connection.execute(
                "SELECT source,order_id,MIN(revision) first_revision,MAX(revision) last_revision,COUNT(*) changes FROM events WHERE revision>? GROUP BY source,order_id ORDER BY source,order_id",
                (after_revision,),
            ).fetchall()
            published = connection.execute(
                "SELECT c.name,c.generation,e.id,e.data_revision,e.result FROM channels c JOIN editions e ON c.edition_id=e.id ORDER BY c.name"
            ).fetchall()
        changed_keys = {(row["source"], row["order_id"]) for row in changed_events}
        affected = []
        import json
        for row in published:
            result = json.loads(row["result"])
            selected = {(item["source"], item["order_id"]) for item in result["lineage"]}
            overlap = sorted(selected & changed_keys)
            affected.append({"channel": row["name"], "edition_id": row["id"], "generation": row["generation"],
                             "data_revision": row["data_revision"], "stale": row["data_revision"] != current,
                             "changed_existing_orders": [list(key) for key in overlap],
                             "note": "新增订单和参考数据变化也可能影响统计，列表只列已进入该版本的更正订单"})
        return {"after_revision": after_revision, "current_revision": current,
                "changed_orders": [dict(row) for row in changed_events], "channels": affected}

    def coverage_disputes(self, edition_id, period="current"):
        edition = self.editions.get(edition_id)
        require(period in edition["result"]["periods"], "unknown_period", "分析没有该期间")
        content = edition["result"]["periods"][period]
        grouped = defaultdict(list)
        for order in content["orders"]:
            grouped[(order["source"], order["origin"], order["travel_day"])].append(order)
        rows = []
        for cell in content["coverage"]:
            key = (cell["source"], cell["city"], cell["day"])
            records = grouped[key]
            if cell["status"] != "complete" or not records:
                rows.append({"source": key[0], "city": key[1], "day": key[2], "status": cell["status"],
                             "note": cell["note"], "heads": len(records), "totals": totals(records),
                             "interpretation": "已确认零记录" if cell["status"] == "complete" else "未确认完整，不能视为零"})
        return {"edition_id": edition_id, "period": period, "cells": rows}
