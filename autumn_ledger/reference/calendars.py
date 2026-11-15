import json

from ..errors import conflict, missing, require
from ..values import canonical, date_range, fields, identifier, integer, iso_day, text


class Calendars:
    def __init__(self, database, catalog):
        self.database = database
        self.catalog = catalog

    def put(self, identity, value, expected_version=0, actor="operator"):
        identifier(identity)
        integer(expected_version, "expected_version")
        fields(value, {"city", "start", "end", "label", "status", "school_days"},
               {"city", "start", "end", "label"})
        city = identifier(value["city"], "city")
        days = date_range(value["start"], value["end"], 62)
        status = value.get("status", "confirmed")
        require(status in {"confirmed", "withdrawn"}, "invalid_status", "校历状态无效")
        overrides = value.get("school_days", [])
        require(isinstance(overrides, list) and len(overrides) <= 62,
                "invalid_school_days", "调休上课日列表无效")
        overrides = [iso_day(day).isoformat() for day in overrides]
        require(len(overrides) == len(set(overrides)), "duplicate_day", "调休日重复")
        require(all(days[0].isoformat() <= day <= days[-1].isoformat() for day in overrides),
                "invalid_school_days", "调休日必须位于校历窗口内")
        payload = {"id": identity, "city": city, "start": days[0].isoformat(),
                   "end": days[-1].isoformat(), "label": text(value["label"], "校历名称"),
                   "status": status, "school_days": sorted(overrides)}
        with self.database.transaction(write=True) as connection:
            self.catalog.get("city", city, connection=connection)
            current = self.get(identity, connection=connection, required=False)
            version = current["version"] if current else 0
            if version != expected_version:
                raise conflict("校历版本已变化", {"current_version": version})
            if current:
                require(current["city"] == city, "calendar_city_change", "校历不能更换所属城市")
            if status == "confirmed":
                for other in self.list(city, connection=connection):
                    if other["id"] == identity or other["status"] != "confirmed":
                        continue
                    overlap = max(other["start"], payload["start"]) <= min(other["end"], payload["end"])
                    require(not overlap, "calendar_overlap", "同一城市的有效校历窗口不能重叠")
            if current and all(current.get(key) == val for key, val in payload.items()):
                return current
            revision = self.database.advance(connection, "calendar.put", identity, actor, payload)
            connection.execute("INSERT INTO calendars VALUES (?,?,?,?,?,?,?)",
                               (identity, version + 1, revision, city, payload["start"], payload["end"], canonical(payload)))
            return {**payload, "version": version + 1, "revision": revision}

    def get(self, identity, revision=None, connection=None, required=True):
        identifier(identity)
        if connection is None:
            with self.database.transaction() as opened:
                return self.get(identity, revision, opened, required)
        revision = self.database.snapshot_revision(connection, revision)
        row = connection.execute(
            "SELECT * FROM calendars WHERE id=? AND revision<=? ORDER BY version DESC LIMIT 1",
            (identity, revision),
        ).fetchone()
        if row is None:
            if required:
                raise missing("校历", identity)
            return None
        return {**json.loads(row["payload"]), "version": row["version"], "revision": row["revision"]}

    def list(self, city=None, revision=None, connection=None):
        if connection is None:
            with self.database.transaction() as opened:
                return self.list(city, revision, opened)
        revision = self.database.snapshot_revision(connection, revision)
        if city is not None:
            identifier(city, "city")
        rows = connection.execute(
            "SELECT c.* FROM calendars c JOIN (SELECT id,MAX(version) v FROM calendars WHERE revision<=? GROUP BY id) h ON c.id=h.id AND c.version=h.v ORDER BY c.city,c.start_day,c.id",
            (revision,),
        ).fetchall()
        result = [{**json.loads(row["payload"]), "version": row["version"], "revision": row["revision"]} for row in rows]
        return [item for item in result if city is None or item["city"] == city]

    def classify(self, city, day, revision=None, connection=None):
        parsed = iso_day(day)
        candidates = self.list(city, revision, connection)
        for item in candidates:
            if item["status"] == "confirmed" and item["start"] <= day <= item["end"]:
                return {"calendar_id": item["id"], "calendar_version": item["version"],
                        "holiday": day not in item["school_days"], "school_day": day in item["school_days"],
                        "weekend": parsed.weekday() >= 5, "offset": (parsed - iso_day(item["start"])).days}
        return {"calendar_id": None, "calendar_version": None, "holiday": False,
                "school_day": parsed.weekday() < 5, "weekend": parsed.weekday() >= 5, "offset": None}

    def exposure(self, cities, start, end, revision=None, connection=None):
        days = date_range(start, end)
        result = []
        for city in sorted(cities):
            for day in days:
                result.append({"city": city, "day": day.isoformat(),
                               **self.classify(city, day.isoformat(), revision, connection)})
        return result
