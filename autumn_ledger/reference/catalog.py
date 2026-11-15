import json

from ..errors import conflict, missing, require
from ..values import boolean, canonical, fields, identifier, integer, text, timezone_name


KINDS = {"city", "destination", "source"}


class Catalog:
    def __init__(self, database):
        self.database = database

    def validate(self, kind, identity, value):
        require(kind in KINDS, "invalid_kind", "不支持的目录类型")
        identifier(identity)
        allowed = {"name", "active"}
        if kind == "city":
            allowed |= {"region", "timezone"}
        if kind == "destination":
            allowed |= {"city", "category"}
        if kind == "source":
            allowed |= {"description"}
        fields(value, allowed, {"name"})
        result = {"id": identity, "name": text(value["name"]),
                  "active": boolean(value.get("active", True), "active")}
        if kind == "city":
            result["region"] = text(value.get("region", "未分区"), "区域")
            result["timezone"] = timezone_name(value.get("timezone", "Asia/Shanghai"))
        if kind == "destination":
            result["city"] = identifier(value.get("city"), "city")
            result["category"] = text(value.get("category", "综合"), "品类", 80)
        if kind == "source":
            result["description"] = text(value.get("description", "本地订单来源"), "来源说明")
        return result

    def put(self, kind, identity, value, expected_version=0, actor="operator"):
        payload = self.validate(kind, identity, value)
        integer(expected_version, "expected_version")
        with self.database.transaction(write=True) as connection:
            current = self.get(kind, identity, connection=connection, required=False)
            version = current["version"] if current else 0
            if version != expected_version:
                raise conflict("目录版本已变化", {"current_version": version})
            if kind == "destination":
                self.get("city", payload["city"], connection=connection)
            if current and all(current.get(key) == val for key, val in payload.items()):
                return current
            revision = self.database.advance(connection, "catalog.put", f"{kind}:{identity}", actor, payload)
            connection.execute("INSERT INTO catalog VALUES (?,?,?,?,?)",
                               (kind, identity, version + 1, revision, canonical(payload)))
            return {**payload, "version": version + 1, "revision": revision}

    def get(self, kind, identity, revision=None, connection=None, required=True):
        require(kind in KINDS, "invalid_kind", "不支持的目录类型")
        identifier(identity)
        if connection is None:
            with self.database.transaction() as opened:
                return self.get(kind, identity, revision, opened, required)
        revision = self.database.snapshot_revision(connection, revision)
        row = connection.execute(
            "SELECT * FROM catalog WHERE kind=? AND id=? AND revision<=? ORDER BY version DESC LIMIT 1",
            (kind, identity, revision),
        ).fetchone()
        if row is None:
            if required:
                raise missing(kind, identity)
            return None
        return {**json.loads(row["payload"]), "version": row["version"], "revision": row["revision"]}

    def list(self, kind, revision=None, connection=None, active_only=False):
        require(kind in KINDS, "invalid_kind", "不支持的目录类型")
        if connection is None:
            with self.database.transaction() as opened:
                return self.list(kind, revision, opened, active_only)
        revision = self.database.snapshot_revision(connection, revision)
        rows = connection.execute(
            "SELECT c.* FROM catalog c JOIN (SELECT id,MAX(version) AS v FROM catalog WHERE kind=? AND revision<=? GROUP BY id) h ON c.id=h.id AND c.version=h.v WHERE c.kind=? ORDER BY c.id",
            (kind, revision, kind),
        ).fetchall()
        result = [{**json.loads(row["payload"]), "version": row["version"], "revision": row["revision"]} for row in rows]
        return [item for item in result if not active_only or item["active"]]

    def require_active(self, kind, identity, connection, revision=None):
        item = self.get(kind, identity, revision, connection)
        require(item["active"], "inactive_reference", f"{kind}:{identity}已停用")
        return item

    def snapshot(self, revision, connection):
        return {kind: self.list(kind, revision, connection) for kind in sorted(KINDS)}
