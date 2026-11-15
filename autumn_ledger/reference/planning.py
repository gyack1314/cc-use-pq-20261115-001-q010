from collections import Counter, defaultdict

from ..values import date_range, identifier


class ReferencePlanning:
    def __init__(self, database, catalog, calendars, coverage):
        self.database = database
        self.catalog = catalog
        self.calendars = calendars
        self.coverage = coverage

    def city_calendar(self, city, start, end, sources, revision=None):
        identifier(city)
        days = date_range(start, end)
        with self.database.transaction() as connection:
            revision = self.database.snapshot_revision(connection, revision)
            reference = self.catalog.get("city", city, revision, connection)
            for source in sources:
                self.catalog.get("source", source, revision, connection)
            result = []
            for day in days:
                value = day.isoformat()
                classification = self.calendars.classify(city, value, revision, connection)
                cells = [self.coverage.get(source, city, value, revision, connection) for source in sorted(sources)]
                complete = bool(cells) and all(cell["status"] == "complete" for cell in cells)
                result.append({"day": value, **classification, "coverage": cells, "fully_observed": complete})
        return {"city": reference, "revision": revision, "days": result,
                "complete_days": sum(row["fully_observed"] for row in result),
                "holiday_days": sum(row["holiday"] for row in result)}

    def readiness(self, cities, sources, start, end, revision=None):
        from ..values import unique_ids
        cities = unique_ids(cities, "cities", maximum=50)
        sources = unique_ids(sources, "sources", maximum=30)
        days = date_range(start, end, 93)
        from ..errors import require
        require(len(cities) * len(sources) * len(days) <= 50000,
                "analysis_too_large", "覆盖检查范围过大")
        with self.database.transaction() as connection:
            revision = self.database.snapshot_revision(connection, revision)
            for city in cities:
                self.catalog.get("city", city, revision, connection)
            for source in sources:
                self.catalog.get("source", source, revision, connection)
            matrix = self.coverage.matrix(sources, cities, start, end, revision, connection)
            summary = self.coverage.summary(matrix)
            calendars = self.calendars.list(revision=revision, connection=connection)
            calendars = [row for row in calendars if row["city"] in cities and row["status"] == "confirmed"
                         and row["start"] <= end and row["end"] >= start]
        grouped = defaultdict(Counter)
        for cell in matrix:
            grouped[cell["city"]][cell["status"]] += 1
        city_rows = []
        for city in cities:
            counts = grouped[city]
            matched = [row for row in calendars if row["city"] == city]
            city_rows.append({"city": city, "coverage_counts": dict(counts),
                              "fully_observed": counts["complete"] == len(sources) * len(days),
                              "calendars": [{"id": row["id"], "version": row["version"], "start": row["start"], "end": row["end"]}
                                            for row in matched]})
        return {"revision": revision, "start": start, "end": end, "coverage": summary, "cities": city_rows,
                "gaps": self.coverage.missing_groups(matrix), "ready": summary["fully_observed"]}

    def calendar_changes(self, identity):
        identifier(identity)
        import json
        with self.database.transaction() as connection:
            self.calendars.get(identity, connection=connection)
            rows = connection.execute("SELECT * FROM calendars WHERE id=? ORDER BY version", (identity,)).fetchall()
        previous = None
        changes = []
        for row in rows:
            payload = json.loads(row["payload"])
            altered = []
            if previous:
                altered = [key for key in payload if payload[key] != previous.get(key)]
            changes.append({"version": row["version"], "revision": row["revision"], "value": payload,
                            "changed_fields": sorted(altered), "initial": previous is None})
            previous = payload
        return {"id": identity, "history": changes}
