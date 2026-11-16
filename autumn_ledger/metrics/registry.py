"""指标口径注册表：登记、版本化、试算与审核启用。

与 catalog/calendars 一致，每个指标 key 保留全部历史版本行，不可原地改写；
启用（activate）是一次性动作，已启用版本冻结。修改指标 = 登记新版本，
新版本必须重新通过结构校验与下游影响检查。
"""

from ..errors import conflict, missing, require
from ..values import canonical, fingerprint, identifier, integer, page_size, text, utc_now
from .base import BASE_MEASURES, EXPOSURE_DENOMINATORS
from . import dsl

DRAFT, ACTIVE, DISABLED = "draft", "active", "disabled"
STATES = {DRAFT, ACTIVE, DISABLED}


class MetricRegistry:
    def __init__(self, database):
        self.database = database

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def _row(self, connection, key, version):
        row = connection.execute("SELECT * FROM metric_definitions WHERE key=? AND version=?",
                                 (key, version)).fetchone()
        if row is None:
            raise missing("指标定义版本", f"{key}@v{version}")
        return row

    def get(self, key, version=None, connection=None):
        identifier(key)
        if connection is None:
            with self.database.transaction() as opened:
                return self.get(key, version, opened)
        if version is None:
            row = connection.execute(
                "SELECT * FROM metric_definitions WHERE key=? ORDER BY version DESC LIMIT 1",
                (key,)).fetchone()
        else:
            integer(version, "version", 1)
            row = self._row(connection, key, version)
        if row is None:
            raise missing("指标", key)
        return self.decode(row)

    def active(self, connection=None):
        """当前全部已启用版本，按 key 排序。"""
        if connection is None:
            with self.database.transaction() as opened:
                return self.active(opened)
        rows = connection.execute(
            "SELECT * FROM metric_definitions WHERE state='active' ORDER BY key, version").fetchall()
        return [self.decode(row) for row in rows]

    def list(self, state=None, limit=100, offset=0):
        page_size(limit, offset)
        require(state is None or state in STATES, "invalid_state", "指标状态无效")
        clause = " WHERE state=?" if state else ""
        params = [state] if state else []
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM metric_definitions" + clause + " ORDER BY key,version LIMIT ? OFFSET ?",
                params + [limit, offset]).fetchall()
        return [self.decode(row) for row in rows]

    def history(self, key):
        identifier(key)
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM metric_definitions WHERE key=? ORDER BY version", (key,)).fetchall()
        require(rows, "not_found", f"找不到指标：{key}", 404)
        return [self.decode(row, include_expression=False) for row in rows]

    @staticmethod
    def decode(row, include_expression=True):
        import json
        result = {"key": row["key"], "version": row["version"], "state": row["state"],
                  "name": row["name"], "description": row["description"],
                  "revision": row["revision"], "definition_digest": row["definition_digest"],
                  "created_at": row["created_at"], "activated_at": row["activated_at"]}
        if include_expression:
            result["expression"] = json.loads(row["expression"])
        return result

    # ------------------------------------------------------------------
    # 登记新版本（draft）
    # ------------------------------------------------------------------
    def register(self, value, actor="operator"):
        definition = dsl.normalize_definition(value)
        actor = text(actor, "操作人", 100)
        with self.database.transaction(write=True) as connection:
            current = connection.execute(
                "SELECT version,state,name FROM metric_definitions WHERE key=? ORDER BY version DESC LIMIT 1",
                (definition["key"],)).fetchone()
            version = (current["version"] + 1) if current else 1
            # 同名展示允许沿用，但 key 的中文名变更需要显式给出
            digest = fingerprint(definition)
            if current:
                duplicate = connection.execute(
                    "SELECT version FROM metric_definitions WHERE key=? AND definition_digest=?",
                    (definition["key"], digest)).fetchone()
                if duplicate:
                    return self.get(definition["key"], duplicate["version"], connection)
            expression = canonical(definition["expression"])
            revision = self.database.advance(connection, "metric.register",
                                             f"{definition['key']}:v{version}", actor,
                                             {"key": definition["key"], "version": version,
                                              "name": definition["name"]})
            connection.execute(
                "INSERT INTO metric_definitions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (definition["key"], version, "draft", definition["name"],
                 definition["description"], expression, digest, revision, utc_now(), None))
            self._audit(connection, definition["key"], version, "register", actor, revision)
            return self.get(definition["key"], version, connection)

    # ------------------------------------------------------------------
    # 启用前校验：引用存在性、循环依赖、单位冲突（带具体路径）
    # ------------------------------------------------------------------
    def _all_expressions(self, connection, pending_key=None, pending_expression=None):
        """构造用于链接的表达式映射；pending 为待启用的新版本覆盖项。"""
        rows = connection.execute("SELECT key,version,expression FROM metric_definitions").fetchall()
        import json
        latest = {}
        for row in rows:
            latest.setdefault(row["key"], []).append((row["version"], json.loads(row["expression"])))
        definitions = {key: versions[-1][1] for key, versions in latest.items()}
        if pending_key is not None:
            definitions[pending_key] = pending_expression
        return definitions, latest

    def validate_activation(self, connection, key, version):
        row = self._row(connection, key, version)
        require(row["state"] == DRAFT, "invalid_transition", "仅草稿版本可以启用", 409)
        import json
        expression = json.loads(row["expression"])
        definitions, latest = self._all_expressions(connection, key, expression)

        # 1) 引用的指标版本必须存在
        missing_refs = []
        for node in dsl.walk(expression):
            if node["type"] == "metric":
                versions = dict(latest.get(node["key"], []))
                if node["version"] not in versions:
                    missing_refs.append({"key": node["key"], "version": node["version"]})
        require(not missing_refs, "missing_metric_version",
                "引用了不存在的指标定义版本", details={"references": missing_refs})

        # 2) 被引用指标必须已启用（不能建立在未审核定义之上）
        inactive_refs = []
        active_rows = {(r["key"], r["version"]) for r in
                       connection.execute("SELECT key,version FROM metric_definitions WHERE state='active'")}
        for ref_key, ref_version, in dsl.references(expression)[0]:
            if (ref_key, ref_version) not in active_rows:
                inactive_refs.append({"key": ref_key, "version": ref_version})
        require(not inactive_refs, "inactive_reference",
                "只能引用已审核启用的指标版本", details={"references": inactive_refs})

        # 3) 循环依赖检测，给出闭合路径
        cycle = dsl.detect_cycle(definitions)
        require(cycle is None, "cyclic_dependency", "指标定义存在循环依赖",
                details={"path": cycle})

        # 4) 单位推导与冲突（resolve_units 报错时 details 已带依赖路径）
        def resolver(ref_key, ref_version):
            versions = dict(latest[ref_key])
            return versions[ref_version]

        dsl.resolve_units(expression, resolver)
        return expression, latest

    def dependents(self, key, connection=None):
        """返回直接或间接引用了该 key 任一已启用版本的指标（下游影响面）。"""
        def _dependents(conn):
            rows = conn.execute("SELECT key,version,state,expression FROM metric_definitions").fetchall()
            import json
            graph = {}
            for row in rows:
                refs = {ref_key for ref_key, _ in dsl.references(json.loads(row["expression"]))[0]}
                graph.setdefault(row["key"], set()).update(refs)
            reverse = {}
            for owner, refs in graph.items():
                for ref in refs:
                    reverse.setdefault(ref, set()).add(owner)
            seen, stack = set(), list(reverse.get(key, ()))
            while stack:
                item = stack.pop()
                if item in seen:
                    continue
                seen.add(item)
                stack.extend(reverse.get(item, ()))
            active_keys = {r["key"] for r in conn.execute(
                "SELECT DISTINCT key FROM metric_definitions WHERE state='active'")}
            return sorted(seen & active_keys)
        if connection is None:
            with self.database.transaction() as opened:
                return _dependents(opened)
        return _dependents(connection)

    def activate(self, key, version, actor, note):
        identifier(key)
        integer(version, "version", 1)
        actor = text(actor, "审核人", 100)
        note = text(note, "启用说明", 1000)
        with self.database.transaction(write=True) as connection:
            self.validate_activation(connection, key, version)
            downstream = self.dependents(key, connection)
            connection.execute(
                "UPDATE metric_definitions SET state='active', activated_at=? WHERE key=? AND version=?",
                (utc_now(), key, version))
            # 同一 key 同时只允许一个启用版本：旧启用版本转停用（保留行，历史可重放）
            connection.execute(
                "UPDATE metric_definitions SET state='disabled' WHERE key=? AND version<>? AND state='active'",
                (key, version))
            revision = self.database.advance(connection, "metric.activate", f"{key}:v{version}", actor,
                                             {"key": key, "version": version, "downstream": downstream,
                                              "note": note})
            self._audit(connection, key, version, "activate", actor, revision, note)
            return {"metric": self.get(key, version, connection), "downstream_checked": downstream}

    def disable(self, key, expected_version, actor, note):
        actor = text(actor, "操作人", 100)
        note = text(note, "停用说明", 1000)
        with self.database.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM metric_definitions WHERE key=? ORDER BY version DESC LIMIT 1", (key,)).fetchone()
            require(row is not None, "not_found", f"找不到指标：{key}", 404)
            require(row["version"] == expected_version, "conflict", "指标版本已变化", 409,
                    {"current_version": row["version"]})
            require(row["state"] == ACTIVE, "invalid_transition", "仅已启用指标可以停用", 409)
            downstream = self.dependents(key, connection)
            require(not downstream, "metric_in_use",
                    "该指标仍被其他已启用口径引用，不能停用；请先处理下游口径", 409,
                    {"dependents": downstream})
            connection.execute("UPDATE metric_definitions SET state='disabled' WHERE key=? AND version=?",
                               (key, expected_version))
            revision = self.database.advance(connection, "metric.disable", f"{key}:v{expected_version}",
                                             actor, {"key": key, "note": note})
            self._audit(connection, key, expected_version, "disable", actor, revision, note)
            return self.get(key, expected_version, connection)

    def rename(self, key, new_name, actor):
        """重命名只改展示名并产生新版本草稿，不动任何已冻结定义，历史重放不受影响。"""
        actor = text(actor, "操作人", 100)
        new_name = text(new_name, "指标名称", 120)
        with self.database.transaction(write=True) as connection:
            latest = self.get(key, connection=connection)
            import json
            value = {"key": key, "name": new_name,
                     "expression": latest["expression"], "description": latest["description"]}
        return self.register(value, actor)

    def _audit(self, connection, key, version, action, actor, revision, note=""):
        connection.execute(
            "INSERT INTO metric_decisions(metric_key,metric_version,action,actor,note,revision,created_at)"
            " VALUES (?,?,?,?,?,?,?)", (key, version, action, actor, note, revision, utc_now()))

    def decisions(self, key=None, limit=100, offset=0):
        page_size(limit, offset)
        with self.database.transaction() as connection:
            if key is None:
                rows = connection.execute(
                    "SELECT * FROM metric_decisions ORDER BY id DESC LIMIT ? OFFSET ?",
                    (limit, offset)).fetchall()
            else:
                identifier(key)
                rows = connection.execute(
                    "SELECT * FROM metric_decisions WHERE metric_key=? ORDER BY id DESC LIMIT ? OFFSET ?",
                    (key, limit, offset)).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 冻结集：报告创建时快照整套依赖版本
    # ------------------------------------------------------------------
    def freeze_set(self, connection, selected_keys=None):
        """返回创建报告时实际启用的整套指标定义（含传递依赖），用于冻结。"""
        actives = self.active(connection)
        by_key = {item["key"]: item for item in actives}
        if selected_keys is not None:
            selected_keys = sorted(set(selected_keys))
            for key in selected_keys:
                require(key in by_key, "metric_not_active", f"指标未启用或不存在：{key}")
            chosen = {key: by_key[key] for key in selected_keys}
        else:
            chosen = by_key
        # 展开传递依赖，保证冻结集自洽
        closure = dict(chosen)
        stack = list(chosen.values())
        while stack:
            item = stack.pop()
            for ref_key, ref_version in dsl.references(item["expression"])[0]:
                if ref_key not in closure:
                    ref = self.get(ref_key, ref_version, connection)
                    require(ref["state"] == ACTIVE, "inactive_reference",
                            "冻结集中包含未启用依赖", details={"key": ref_key})
                    closure[ref_key] = ref
                    stack.append(ref)
        entries = [{"key": item["key"], "version": item["version"], "state": item["state"],
                    "name": item["name"], "description": item["description"],
                    "expression": item["expression"], "definition_digest": item["definition_digest"]}
                   for item in sorted(closure.values(), key=lambda x: x["key"])]
        return {"schema": "autumn-metric-freeze-v1", "entries": entries,
                "digest": fingerprint(entries), "frozen_at": utc_now(),
                "base_catalog": {"base_measures": sorted(BASE_MEASURES),
                                 "exposure_denominators": sorted(EXPOSURE_DENOMINATORS)}}
