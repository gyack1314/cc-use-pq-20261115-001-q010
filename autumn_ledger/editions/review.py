from ..errors import conflict, missing, require
from ..values import identifier, integer, text, utc_now


class Review:
    def __init__(self, database, editions):
        self.database = database
        self.editions = editions

    def check_version(self, edition, expected_version):
        integer(expected_version, "expected_version", 1)
        if edition["version"] != expected_version:
            raise conflict("分析状态版本已变化", {"current_version": edition["version"]})

    def check_current(self, edition):
        require(not edition["stale"], "stale_edition", "数据已变化，请重新计算后审核发布", 409,
                {"data_revision": edition["data_revision"], "current_revision": edition["current_data_revision"]})

    def decide(self, identity, action, expected_version, actor, note):
        identifier(identity)
        actor = text(actor, "审核人", 100)
        note = text(note, "审核说明", 1000)
        require(action in {"approve", "reject", "reopen"}, "invalid_action", "不支持的审核操作")
        with self.database.transaction(write=True) as connection:
            edition = self.editions.get(identity, connection)
            self.check_version(edition, expected_version)
            if action == "approve":
                require(edition["state"] == "draft", "invalid_transition", "仅草稿可以审核通过", 409)
                self.check_current(edition)
                require(edition["result"]["quality"]["publishable"], "quality_blocked", "质量检查阻止审核通过", 409,
                        edition["result"]["quality"])
                target = "approved"
            elif action == "reject":
                require(edition["state"] in {"draft", "approved"}, "invalid_transition", "当前状态不能驳回", 409)
                target = "rejected"
            else:
                require(edition["state"] == "rejected", "invalid_transition", "仅驳回版本可以重新送审", 409)
                target = "draft"
            version = expected_version + 1
            updated = connection.execute("UPDATE editions SET state=?,version=? WHERE id=? AND version=?",
                                         (target, version, identity, expected_version)).rowcount
            require(updated == 1, "review_race", "状态竞争，请重新读取", 409)
            connection.execute("INSERT INTO decisions(edition_id,edition_version,action,actor,note,created_at) VALUES (?,?,?,?,?,?)",
                               (identity, version, action, actor, note, utc_now()))
            return self.editions.get(identity, connection, False)

    def publish(self, identity, channel, expected_version, expected_generation, actor):
        identifier(identity)
        identifier(channel, "channel")
        integer(expected_generation, "expected_generation")
        actor = text(actor, "发布人", 100)
        with self.database.transaction(write=True) as connection:
            edition = self.editions.get(identity, connection)
            self.check_version(edition, expected_version)
            current = connection.execute("SELECT * FROM channels WHERE name=?", (channel,)).fetchone()
            generation = current["generation"] if current else 0
            if generation != expected_generation:
                raise conflict("发布频道已被更新", {"current_generation": generation})
            require(edition["state"] in {"approved", "published"}, "not_approved", "分析尚未审核通过", 409)
            self.check_current(edition)
            require(edition["result"]["quality"]["publishable"], "quality_blocked", "质量检查未通过", 409)
            if current and current["edition_id"] == identity:
                return {**dict(current), "state": edition["state"], "version": edition["version"], "replayed": True}
            generation += 1
            if current:
                connection.execute("UPDATE channels SET edition_id=?,generation=? WHERE name=?",
                                   (identity, generation, channel))
            else:
                connection.execute("INSERT INTO channels VALUES (?,?,?)", (channel, identity, generation))
            version = expected_version + 1
            connection.execute("UPDATE editions SET state='published',version=? WHERE id=?", (version, identity))
            connection.execute("INSERT INTO publication_history(channel,generation,edition_id,actor,created_at) VALUES (?,?,?,?,?)",
                               (channel, generation, identity, actor, utc_now()))
            connection.execute("INSERT INTO decisions(edition_id,edition_version,action,actor,note,created_at) VALUES (?,?,?,?,?,?)",
                               (identity, version, "publish", actor, channel, utc_now()))
            return {"name": channel, "edition_id": identity, "generation": generation,
                    "state": "published", "version": version, "replayed": False}

    def retire(self, identity, expected_version, actor, note):
        actor = text(actor, "操作人", 100)
        note = text(note, "撤下说明", 1000)
        with self.database.transaction(write=True) as connection:
            edition = self.editions.get(identity, connection)
            self.check_version(edition, expected_version)
            require(edition["state"] == "published", "invalid_transition", "仅已发布分析可以撤下", 409)
            channels = connection.execute("SELECT name FROM channels WHERE edition_id=?", (identity,)).fetchall()
            require(not channels, "edition_in_use", "分析仍被频道引用，请先发布替代版本", 409,
                    {"channels": [row["name"] for row in channels]})
            connection.execute("UPDATE editions SET state='retired',version=version+1 WHERE id=?", (identity,))
            connection.execute("INSERT INTO decisions(edition_id,edition_version,action,actor,note,created_at) VALUES (?,?,?,?,?,?)",
                               (identity, expected_version + 1, "retire", actor, note, utc_now()))
            return self.editions.get(identity, connection, False)

    def channel(self, name):
        identifier(name, "channel")
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM channels WHERE name=?", (name,)).fetchone()
            if row is None:
                raise missing("发布频道", name)
            edition = self.editions.get(row["edition_id"], connection, False)
            return {**dict(row), "edition": edition}

    def channels(self):
        with self.database.transaction() as connection:
            rows = connection.execute("SELECT * FROM channels ORDER BY name").fetchall()
            return [{**dict(row), "stale": self.editions.get(row["edition_id"], connection, False)["stale"]} for row in rows]

    def history(self, identity):
        self.editions.get(identity, include_result=False)
        with self.database.transaction() as connection:
            rows = connection.execute("SELECT * FROM decisions WHERE edition_id=? ORDER BY id", (identity,)).fetchall()
        return [dict(row) for row in rows]

    def publication_history(self, channel):
        identifier(channel)
        with self.database.transaction() as connection:
            rows = connection.execute("SELECT * FROM publication_history WHERE channel=? ORDER BY generation", (channel,)).fetchall()
        return [dict(row) for row in rows]
