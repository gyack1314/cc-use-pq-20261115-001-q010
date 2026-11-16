"""指标运行时：在已冻结的指标集上做三态求值。

关键规则：
1. 比例（ratio 节点）的总计不是各组比例的平均，而是用汇总后的分子、分母重新计算。
2. 缺报(missing)、零分母(zero_denominator)、不适用(not_applicable)是三种不同状态，
   分别保留原因，绝不互相折算成 0。
3. 求值只走 dsl.evaluate 的白名单节点分派，不存在任何动态表达式执行。
4. 一切输入来自冻结集，运行期不回读注册表，因此停用/重命名口径不影响历史重放。
"""

from . import dsl
from .base import EXPOSURE_DENOMINATORS


class FrozenPlan:
    """冻结指标集的可求值计划：表达式已链接、单位已推导。"""

    def __init__(self, freeze_set):
        self.entries = {entry["key"]: entry for entry in freeze_set["entries"]}
        self.digest = freeze_set["digest"]
        self.linked = {}
        self._link()

    def _link(self):
        import copy
        latest = {key: {entry["version"]: entry["expression"]}
                  for key, entry in self.entries.items()}

        def resolver(key, version):
            return latest[key][version]

        for key, entry in self.entries.items():
            expression = copy.deepcopy(entry["expression"])
            dsl.resolve_units(expression, resolver)
            self.linked[key] = expression

    def keys(self):
        return sorted(self.entries)

    def entry(self, key):
        return self.entries[key]


def _coverage_lookup(period):
    """(city, day) -> 是否所有来源都 complete。"""
    grouped = {}
    for cell in period.get("coverage", []):
        grouped.setdefault((cell["city"], cell["day"]), []).append(cell["status"])
    return {key: all(status == "complete" for status in statuses)
            for key, statuses in grouped.items()}


def _group_cities(dimensions, row_dimensions):
    """根据分组维度推断该组覆盖的城市集合；无法定位城市时返回 None。"""
    if "city" in row_dimensions:
        return [row_dimensions["city"]]
    if "origin" in row_dimensions:
        return [row_dimensions["origin"]]
    return None  # 目的地/来源维度与出发城市没有一一对应，城市日期暴露不适用


class MetricContext:
    """单个期间的求值上下文，负责为每个作用域（总计/分组/日行）构造 env。"""

    def __init__(self, period):
        self.period = period
        self.grid = period["exposure"]
        self.coverage_complete = _coverage_lookup(period)
        self.exposure_summary = period["exposure_summary"]
        # 缺报城市日期集合：覆盖状态为 missing 的格点。partial 不算 missing，
        # 但会让 complete_city_days 不计入——两种弱覆盖在分母上有不同后果。
        self.missing_city_days = {(cell["city"], cell["day"]) for cell in period.get("coverage", [])
                                  if cell["status"] == "missing"}

    def _is_missing_scope(self, name, scope_kind, dimensions):
        """判断某个暴露分母在该作用域是否落在缺报格点上。

        city_days 是窗口格点计数，缺报格点依然是一个暴露日，故不标记 missing；
        complete_city_days 依赖覆盖确认，缺报格点无法确认完整性，标记 missing。
        """
        if name != "complete_city_days":
            return False
        if scope_kind == "daily":
            return (dimensions["city"], dimensions["day"]) in self.missing_city_days
        if scope_kind == "group":
            cities = _group_cities(None, dimensions)
            if cities is None:
                return False
            keys = {(city, row["day"]) for city in cities for row in self.grid if row["city"] == city}
            return any(key in self.missing_city_days for key in keys)
        # 总计：存在任一缺报格点即标记，提示该暴露分母下的率不能当作全样本结论
        return bool(self.missing_city_days)

    # -- 暴露分母 ---------------------------------------------------------
    def _exposure_value(self, name, scope_kind, dimensions=None):
        if name in ("orders", "travelers"):
            # 计数型分母由 measure env 提供，这里转成同一份取值
            return dsl.ok(self._measure(name, dimensions),
                          EXPOSURE_DENOMINATORS[name]["unit"])
        if self._is_missing_scope(name, scope_kind, dimensions):
            return dsl.undefined("missing", "该城市日期存在缺报，暴露分母不可解释为零",
                                 EXPOSURE_DENOMINATORS[name]["unit"])
        if scope_kind == "total":
            if name == "city_days":
                return dsl.ok(self.exposure_summary["city_days"],
                              EXPOSURE_DENOMINATORS[name]["unit"])
            complete = sum(1 for row in self.grid
                           if self.coverage_complete.get((row["city"], row["day"]), False))
            return dsl.ok(complete, EXPOSURE_DENOMINATORS[name]["unit"])
        if scope_kind == "daily":
            city, day = dimensions["city"], dimensions["day"]
            if name == "city_days":
                return dsl.ok(1, EXPOSURE_DENOMINATORS[name]["unit"])
            is_complete = self.coverage_complete.get((city, day), False)
            return dsl.ok(1 if is_complete else 0, EXPOSURE_DENOMINATORS[name]["unit"])
        # 分组
        cities = _group_cities(None, dimensions)
        if cities is None:
            return dsl.undefined("not_applicable",
                                 "该分组维度无法定位城市日期暴露，城市日期分母不适用",
                                 EXPOSURE_DENOMINATORS[name]["unit"])
        rows = [row for row in self.grid if row["city"] in cities]
        if name == "city_days":
            return dsl.ok(len(rows), EXPOSURE_DENOMINATORS[name]["unit"])
        complete = sum(1 for row in rows
                       if self.coverage_complete.get((row["city"], row["day"]), False))
        return dsl.ok(complete, EXPOSURE_DENOMINATORS[name]["unit"])

    # -- 基础度量 ---------------------------------------------------------
    def _measure(self, name, dimensions=None):
        if dimensions is None:
            return self.period["cube"]["totals"][name]
        return dimensions["_measures"][name]

    # -- 环境构造 ---------------------------------------------------------
    def make_env(self, plan, scope_kind, dimensions=None, metric_cache=None):
        def measure(name):
            return self._measure(name, dimensions)

        def exposure(name):
            return self._exposure_value(name, scope_kind, dimensions)

        def metric(key, version):
            cache_key = (key, version)
            if metric_cache is not None and cache_key in metric_cache:
                return metric_cache[cache_key]
            from ..errors import LedgerError
            if key not in plan.entries:
                raise LedgerError("freeze_mismatch",
                                  f"冻结集缺少被引用指标：{key}@v{version}", 500)
            require_entry = plan.entries[key]
            if require_entry["version"] != version:
                # 冻结集内引用版本必须与计划一致，否则说明冻结集不自洽
                raise LedgerError("freeze_mismatch",
                                  f"冻结集版本不一致：{key} 需要v{version}，冻结为v{require_entry['version']}", 500)
            value = dsl.evaluate(plan.linked[key],
                                 self.make_env(plan, scope_kind, dimensions, metric_cache))
            if metric_cache is not None:
                metric_cache[cache_key] = value
            return value

        return {"measure": measure, "exposure": exposure, "metric": metric}

    # -- 求值入口 ---------------------------------------------------------
    def evaluate_scope(self, plan, scope_kind, dimensions=None):
        """返回 {metric_key: 三态值}。metric_cache 保证同一作用域内指标只算一次。"""
        cache = {}
        env = self.make_env(plan, scope_kind, dimensions, cache)
        return {key: dsl.evaluate(plan.linked[key], env) for key in plan.keys()}


# --------------------------------------------------------------------------
# 把指标结果挂到期间结构上
# --------------------------------------------------------------------------

def _attach(scope, results, plan):
    scope["custom_metrics"] = {
        key: {"value": result["value"] if result["status"] == "ok" else None,
              "status": result["status"],
              "reason": result.get("reason"),
              "unit": result.get("unit"),
              "name": plan.entry(key)["name"],
              "definition_digest": plan.entry(key)["definition_digest"]}
        for key, result in results.items()}


def apply_metrics(period, freeze_set):
    """在一个期间的总计、各明细表分组、日明细上计算全部冻结指标。"""
    if not freeze_set.get("entries"):
        period["metric_freeze"] = None
        return
    plan = FrozenPlan(freeze_set)
    context = MetricContext(period)

    # 总计：比例在此处用汇总分子分母重算（dsl.evaluate 的 ratio 分支天然如此，
    # 因为总计 env 提供的就是汇总后的度量）
    total_results = context.evaluate_scope(plan, "total")
    _attach(period["cube"]["totals"], total_results, plan)

    # 明细表分组
    for table_name, rows in period["cube"]["tables"].items():
        daily = table_name == "daily"
        for row in rows:
            dims = dict(row.get("dimensions", row))
            dims["_measures"] = row["measures"]
            scope_kind = "daily" if daily else "group"
            results = context.evaluate_scope(plan, scope_kind, dims)
            _attach(row["measures"], results, plan)

    period["metric_freeze"] = {"digest": plan.digest, "keys": plan.keys(),
                               "schema": freeze_set["schema"]}
    return plan
