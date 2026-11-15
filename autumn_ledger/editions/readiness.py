from collections import Counter

from ..errors import require
from ..values import identifier


class PublicationReadiness:
    def __init__(self, database, editions, review):
        self.database = database
        self.editions = editions
        self.review = review

    def inspect(self, identity, channel=None):
        if channel is not None:
            identifier(channel)
        with self.database.transaction() as connection:
            edition = self.editions.get(identity, connection)
            decisions = connection.execute("SELECT * FROM decisions WHERE edition_id=? ORDER BY id", (identity,)).fetchall()
            pointer = None
            if channel is not None:
                row = connection.execute("SELECT * FROM channels WHERE name=?", (channel,)).fetchone()
                pointer = dict(row) if row else {"name": channel, "edition_id": None, "generation": 0}
        checks = []
        checks.append({"code": "current_data", "ok": not edition["stale"],
                       "message": "数据版本与当前账本一致" if not edition["stale"] else "数据已变化，需要新分析版本",
                       "evidence": {"edition_revision": edition["data_revision"], "current_revision": edition["current_data_revision"]}})
        publishable = edition["result"]["quality"]["publishable"]
        checks.append({"code": "quality", "ok": publishable,
                       "message": "质量检查通过" if publishable else "存在阻止发布的质量问题",
                       "evidence": {"errors": edition["result"]["quality"]["errors"]}})
        state_ok = edition["state"] in {"approved", "published"}
        checks.append({"code": "approval", "ok": state_ok,
                       "message": "分析已经审核" if state_ok else "当前状态不能发布",
                       "evidence": {"state": edition["state"], "version": edition["version"]}})
        integrity = edition["result"]["integrity"]["ok"] and edition["result"]["statistical_checks"]["ok"]
        checks.append({"code": "integrity", "ok": integrity, "message": "统计分区与证据一致性检查",
                       "evidence": {"checks": edition["result"]["statistical_checks"]["check_count"]}})
        return {"edition_id": identity, "can_publish": all(item["ok"] for item in checks),
                "expected_version": edition["version"], "channel": pointer,
                "checks": checks, "blockers": [item["code"] for item in checks if not item["ok"]],
                "decisions": [dict(row) for row in decisions],
                "note": "此结果是查询时刻的说明；实际发布仍在同一写事务中重新检查版本与频道代数"}

    def queue(self):
        with self.database.transaction() as connection:
            rows = connection.execute("SELECT id,state,data_revision,version,result FROM editions ORDER BY created_at,id").fetchall()
            current = self.database.revision(connection)
        import json
        groups = {"awaiting_review": [], "ready_to_publish": [], "quality_blocked": [], "stale": [], "closed": []}
        for row in rows:
            quality = json.loads(row["result"])["quality"]
            item = {"id": row["id"], "state": row["state"], "version": row["version"],
                    "data_revision": row["data_revision"], "quality_errors": quality["errors"]}
            if row["state"] in {"rejected", "retired"}:
                group = "closed"
            elif row["data_revision"] != current:
                group = "stale"
            elif not quality["publishable"]:
                group = "quality_blocked"
            elif row["state"] == "draft":
                group = "awaiting_review"
            elif row["state"] == "approved":
                group = "ready_to_publish"
            else:
                group = "closed"
            groups[group].append(item)
        return {"revision": current, "counts": {key: len(items) for key, items in groups.items()}, "groups": groups}

    def published_consistency(self):
        with self.database.transaction() as connection:
            channels = connection.execute("SELECT * FROM channels ORDER BY name").fetchall()
            rows = []
            for channel in channels:
                edition = self.editions.get(channel["edition_id"], connection)
                history = connection.execute("SELECT * FROM publication_history WHERE channel=? AND generation=?",
                                             (channel["name"], channel["generation"])).fetchone()
                matches = history is not None and history["edition_id"] == channel["edition_id"]
                state_valid = edition["state"] == "published"
                rows.append({"channel": channel["name"], "edition_id": channel["edition_id"],
                             "generation": channel["generation"], "history_matches": matches,
                             "state_valid": state_valid, "stale": edition["stale"], "ok": matches and state_valid})
        return {"ok": all(row["ok"] for row in rows), "channels": rows}
