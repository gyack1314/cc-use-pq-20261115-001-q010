import json
import uuid

from ..errors import conflict, missing, require
from ..values import canonical, fingerprint, identifier, integer, page_size, utc_now
from ..analysis.windows import normalize_request


class Editions:
    def __init__(self, database, engine):
        self.database = database
        self.engine = engine

    def create(self, request_key, request):
        identifier(request_key, "request_key")
        normalized = normalize_request(request)
        digest = fingerprint(normalized)
        with self.database.transaction(write=True) as connection:
            existing = connection.execute("SELECT * FROM editions WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                if existing["request_digest"] != digest:
                    raise conflict("分析请求号已用于不同请求")
                return self.decode(existing, include_result=False)
            result = self.engine.run(normalized, connection)
            identity = "ed-" + uuid.uuid4().hex
            result_digest = fingerprint(result)
            created = utc_now()
            connection.execute(
                "INSERT INTO editions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (identity, request_key, digest, normalized["name"], result["data_revision"],
                 canonical(normalized), "draft", 1, canonical(result), result_digest, created),
            )
            return self.get(identity, connection=connection, include_result=False)

    def decode(self, row, include_result=True):
        result = dict(row)
        result["request"] = json.loads(result["request"])
        if include_result:
            result["result"] = json.loads(result["result"])
            require(fingerprint(result["result"]) == result["result_digest"],
                    "edition_corrupt", "分析内容校验失败", 500)
        else:
            stored = json.loads(result.pop("result"))
            result["quality"] = stored["quality"]
            result["summary"] = stored["periods"]["current"]["cube"]["totals"]
        return result

    def get(self, identity, connection=None, include_result=True):
        identifier(identity)
        if connection is None:
            with self.database.transaction() as opened:
                return self.get(identity, opened, include_result)
        row = connection.execute("SELECT * FROM editions WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise missing("分析版本", identity)
        result = self.decode(row, include_result)
        result["current_data_revision"] = self.database.revision(connection)
        result["stale"] = result["data_revision"] != result["current_data_revision"]
        return result

    def list(self, state=None, limit=100, offset=0):
        page_size(limit, offset)
        parameters = []
        clause = ""
        if state is not None:
            require(state in {"draft", "approved", "rejected", "published", "retired"}, "invalid_state", "分析状态无效")
            clause = " WHERE state=?"
            parameters.append(state)
        with self.database.transaction() as connection:
            rows = connection.execute("SELECT * FROM editions" + clause + " ORDER BY created_at,id LIMIT ? OFFSET ?",
                                      parameters + [limit, offset]).fetchall()
            current = self.database.revision(connection)
            result = []
            for row in rows:
                item = self.decode(row, False)
                item["stale"] = item["data_revision"] != current
                result.append(item)
        return result

    def lineage(self, identity, source=None, limit=100, offset=0):
        page_size(limit, offset)
        if source is not None:
            identifier(source)
        edition = self.get(identity)
        rows = edition["result"]["lineage"]
        if source is not None:
            rows = [row for row in rows if row["source"] == source]
        return {"edition_id": identity, "data_revision": edition["data_revision"],
                "lineage_digest": edition["result"]["lineage_digest"], "total": len(rows),
                "offset": offset, "items": rows[offset:offset + limit]}

    def explain(self, identity):
        edition = self.get(identity)
        result = edition["result"]
        periods = {}
        for name, period in result["periods"].items():
            periods[name] = {"window": period["window"], "exposure": period["exposure_summary"],
                             "coverage": period["coverage_summary"], "gaps": period["coverage_gaps"],
                             "totals": period["cube"]["totals"], "reconciliation": period["cube"]["reconciliation"]}
        return {"edition_id": identity, "state": edition["state"], "stale": edition["stale"],
                "data_revision": edition["data_revision"], "result_digest": edition["result_digest"],
                "periods": periods, "quality": result["quality"], "semantics": result["semantics"]}

    def verify(self, identity):
        edition = self.get(identity)
        request = {**edition["request"], "revision": edition["data_revision"]}
        fresh = self.engine.run(request)
        stored = edition["result"]
        checks = {"lineage": stored["lineage_digest"] == fresh["lineage_digest"],
                  "periods": stored["periods"] == fresh["periods"],
                  "references": stored["references"] == fresh["references"],
                  "quality": stored["quality"] == fresh["quality"],
                  "comparison": stored["comparison"] == fresh["comparison"]}
        return {"edition_id": identity, "ok": all(checks.values()), "checks": checks,
                "data_revision": edition["data_revision"], "stale": edition["stale"]}

    def compare_editions(self, left_id, right_id):
        left = self.get(left_id)
        right = self.get(right_id)
        left_keys = {(r["source"], r["order_id"], r["sequence"]) for r in left["result"]["lineage"]}
        right_keys = {(r["source"], r["order_id"], r["sequence"]) for r in right["result"]["lineage"]}
        metrics = {}
        ltotal = left["result"]["periods"]["current"]["cube"]["totals"]
        rtotal = right["result"]["periods"]["current"]["cube"]["totals"]
        for key in ("orders", "travelers", "gross_cents", "refund_cents", "net_cents"):
            metrics[key] = {"left": ltotal[key], "right": rtotal[key], "change": rtotal[key] - ltotal[key]}
        return {"left": left_id, "right": right_id, "left_revision": left["data_revision"],
                "right_revision": right["data_revision"], "same_request": left["request"] == right["request"],
                "added_heads": [list(x) for x in sorted(right_keys - left_keys)],
                "removed_heads": [list(x) for x in sorted(left_keys - right_keys)], "metrics": metrics}
