from collections import Counter

from ..errors import conflict, require
from ..values import date_range, fields, identifier, integer, iso_day, text


STATUSES = {"complete", "partial", "missing"}


class Coverage:
    def __init__(self, database, catalog):
        self.database = database
        self.catalog = catalog

    def put(self, value, expected_version=0, actor="operator"):
        fields(value, {"source", "city", "day", "status", "note"},
               {"source", "city", "day", "status"})
        source = identifier(value["source"], "source")
        city = identifier(value["city"], "city")
        day = iso_day(value["day"]).isoformat()
        status = value["status"]
        require(status in STATUSES, "invalid_status", "覆盖状态必须是complete/partial/missing")
        note = value.get("note", "")
        require(isinstance(note, str) and len(note) <= 500, "invalid_note", "覆盖说明过长")
        require(status == "complete" or note.strip(), "missing_note", "缺报或部分覆盖需要说明")
        integer(expected_version, "expected_version")
        with self.database.transaction(write=True) as connection:
            self.catalog.get("source", source, connection=connection)
            self.catalog.get("city", city, connection=connection)
            current = self.get(source, city, day, connection=connection)
            if current["version"] != expected_version:
                raise conflict("覆盖记录已变化", {"current_version": current["version"]})
            if current["status"] == status and current["note"] == note:
                return current
            payload = {"source": source, "city": city, "day": day, "status": status, "note": note}
            revision = self.database.advance(connection, "coverage.put", f"{source}:{city}:{day}", actor, payload)
            connection.execute("INSERT INTO coverage VALUES (?,?,?,?,?,?,?)",
                               (source, city, day, expected_version + 1, revision, status, note))
            return {**payload, "version": expected_version + 1, "revision": revision}

    def get(self, source, city, day, revision=None, connection=None):
        identifier(source)
        identifier(city)
        iso_day(day)
        if connection is None:
            with self.database.transaction() as opened:
                return self.get(source, city, day, revision, opened)
        revision = self.database.snapshot_revision(connection, revision)
        row = connection.execute(
            "SELECT * FROM coverage WHERE source=? AND city=? AND day=? AND revision<=? ORDER BY version DESC LIMIT 1",
            (source, city, day, revision),
        ).fetchone()
        if row:
            return dict(row)
        return {"source": source, "city": city, "day": day, "version": 0,
                "revision": 0, "status": "missing", "note": "尚未登记覆盖"}

    def matrix(self, sources, cities, start, end, revision=None, connection=None):
        if connection is None:
            with self.database.transaction() as opened:
                return self.matrix(sources, cities, start, end, revision, opened)
        revision = self.database.snapshot_revision(connection, revision)
        days = date_range(start, end)
        result = []
        for source in sorted(sources):
            for city in sorted(cities):
                for day in days:
                    result.append(self.get(source, city, day.isoformat(), revision, connection))
        return result

    def summary(self, matrix):
        counts = Counter(item["status"] for item in matrix)
        total = len(matrix)
        complete = counts["complete"]
        return {"expected_cells": total, "complete_cells": complete,
                "partial_cells": counts["partial"], "missing_cells": counts["missing"],
                "complete_fraction": complete / total if total else None,
                "fully_observed": bool(total) and complete == total}

    def missing_groups(self, matrix):
        groups = {}
        for item in matrix:
            if item["status"] == "complete":
                continue
            key = (item["source"], item["city"])
            groups.setdefault(key, []).append(item)
        return [{"source": key[0], "city": key[1],
                 "days": [{"day": item["day"], "status": item["status"], "note": item["note"]}
                          for item in sorted(items, key=lambda x: x["day"])]}
                for key, items in sorted(groups.items())]
