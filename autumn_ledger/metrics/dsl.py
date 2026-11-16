"""受限指标定义 DSL：只解释受白名单限制的算术节点，绝不执行任意 Python 表达式。

定义是一棵 JSON 节点树，节点类型固定：
  const    常量（仅数字，含单位声明，默认 dimensionless）
  measure  引用 BASE_MEASURES 中的基础度量
  metric   引用另一个已定义指标的指定版本（key + version）
  exposure 明确选择的暴露分母（EXPOSURE_DENOMINATORS）
  neg      一元取负
  add/sub/mul/div  二元算术
  ratio    比例：分子/分母，求值规则与 div 不同——总计从汇总后的分子分母
           重算，分组缺失/零分母保留独立状态，绝不平均各组比例。

每个节点带固定单位。add/sub 要求两侧单位一致；mul/div 按维度推导单位；
ratio 的结果恒为 dimensionless。单位冲突在启用前报错。
"""

from ..errors import require
from ..values import fields, identifier, text
from .base import BASE_MEASURES, EXPOSURE_DENOMINATORS

NODE_TYPES = {"const", "measure", "metric", "exposure", "neg",
              "add", "sub", "mul", "div", "ratio"}
ARITHMETIC = {"add", "sub", "mul", "div", "ratio"}
VALUE_STATUS = {"ok", "zero_denominator", "not_applicable", "missing"}


# --------------------------------------------------------------------------
# 结构校验与规范化
# --------------------------------------------------------------------------

def _unit(value, field="unit"):
    fields(value, {"kind", "dimension"}, {"kind", "dimension"})
    require(value["kind"] in {"count", "currency", "city_day", "dimensionless"},
            "invalid_unit", f"{field}的计量单位种类无效")
    require(isinstance(value["dimension"], str) and 0 < len(value["dimension"]) <= 40,
            "invalid_unit", f"{field}的计量维度无效")
    return {"kind": value["kind"], "dimension": value["dimension"]}


def normalize_node(value):
    """递归校验节点，返回规范化后的深拷贝；任何越权结构都在登记阶段被拒绝。"""
    fields(value, {"type", "value", "unit", "name", "key", "version", "left", "right",
                   "numerator", "denominator", "label"}, {"type"})
    kind = value["type"]
    require(kind in NODE_TYPES, "invalid_node", f"不支持的定义节点：{kind}")
    if kind == "const":
        number = value.get("value")
        require(type(number) in (int, float), "invalid_constant", "常量必须是数字")
        require(number == number and number not in (float("inf"), float("-inf")),
                "invalid_constant", "常量不能是NaN或无穷")
        unit = _unit(value.get("unit", {"kind": "dimensionless", "dimension": "one"}))
        return {"type": "const", "value": number, "unit": unit}
    if kind == "measure":
        name = value.get("name")
        require(name in BASE_MEASURES, "unknown_measure", "引用了不存在的基础度量",
                details={"name": name})
        return {"type": "measure", "name": name, "unit": dict(BASE_MEASURES[name]["unit"])}
    if kind == "exposure":
        name = value.get("name")
        require(name in EXPOSURE_DENOMINATORS, "unknown_exposure", "引用了未开放的暴露分母",
                details={"name": name})
        return {"type": "exposure", "name": name, "unit": dict(EXPOSURE_DENOMINATORS[name]["unit"])}
    if kind == "metric":
        key = identifier(value.get("key"), "metric.key")
        version = value.get("version")
        require(type(version) is int and version >= 1, "invalid_metric_ref",
                "指标引用必须指定明确的定义版本号")
        return {"type": "metric", "key": key, "version": version,
                "unit": None}  # 单位在链接阶段由被引用指标补全
    if kind == "neg":
        require("operand" in value, "invalid_node", "neg 节点缺少 operand")
        return {"type": "neg", "operand": normalize_node(value["operand"]), "unit": None}
    require(kind in ARITHMETIC, "invalid_node", "节点结构无效")
    if kind == "ratio":
        require("numerator" in value and "denominator" in value, "invalid_node",
                "ratio 节点需要 numerator 和 denominator")
        return {"type": "ratio", "numerator": normalize_node(value["numerator"]),
                "denominator": normalize_node(value["denominator"]),
                "unit": {"kind": "dimensionless", "dimension": "ratio"}}
    require("left" in value and "right" in value, "invalid_node",
            f"{kind} 节点需要 left 和 right")
    left = normalize_node(value["left"])
    right = normalize_node(value["right"])
    return {"type": kind, "left": left, "right": right, "unit": None}


def normalize_definition(value):
    """登记口径的入口校验：表达式 + 展示元数据。"""
    fields(value, {"key", "name", "expression", "description"}, {"key", "name", "expression"})
    key = identifier(value["key"], "指标key")
    name = text(value["name"], "指标名称", 120)
    expression = normalize_node(value["expression"])
    description = value.get("description", "")
    require(isinstance(description, str) and len(description) <= 1000,
            "invalid_text", "指标说明长度无效")
    return {"key": key, "name": name, "expression": expression,
            "description": description.strip()}


# --------------------------------------------------------------------------
# 依赖收集与单位推导
# --------------------------------------------------------------------------

def walk(node):
    """深度优先遍历节点。"""
    yield node
    if node["type"] == "neg":
        yield from walk(node["operand"])
    elif node["type"] in ARITHMETIC:
        if node["type"] == "ratio":
            yield from walk(node["numerator"])
            yield from walk(node["denominator"])
        else:
            yield from walk(node["left"])
            yield from walk(node["right"])


def references(node):
    """返回 (metric 引用列表, measure 名称集合, exposure 名称集合)。"""
    metric_refs, measures, exposures = [], set(), set()
    for item in walk(node):
        if item["type"] == "metric":
            metric_refs.append((item["key"], item["version"]))
        elif item["type"] == "measure":
            measures.add(item["name"])
        elif item["type"] == "exposure":
            exposures.add(item["name"])
    return metric_refs, measures, exposures


def _same_unit(left, right):
    return left["kind"] == right["kind"] and left["dimension"] == right["dimension"]


def resolve_units(node, resolver, path=()):
    """链接阶段推导每个节点单位；resolver(key, version) 返回被引用指标的已推导表达式。

    单位冲突、引用不存在版本在此抛出，并在调用方包装具体依赖路径。
    """
    kind = node["type"]
    if kind in ("const", "measure", "exposure"):
        return node["unit"]
    if kind == "metric":
        referenced = resolver(node["key"], node["version"])  # 找不到会抛 missing_version
        node["unit"] = resolve_units(referenced, resolver, path + ((node["key"], node["version"]),))
        return node["unit"]
    if kind == "neg":
        node["unit"] = resolve_units(node["operand"], resolver, path)
        return node["unit"]
    if kind in ("add", "sub"):
        left = resolve_units(node["left"], resolver, path)
        right = resolve_units(node["right"], resolver, path)
        require(_same_unit(left, right), "unit_conflict",
                "加减两侧单位不一致，禁止混合计量维度",
                details={"path": list(path), "operator": kind, "left": left, "right": right})
        node["unit"] = dict(left)
        return node["unit"]
    if kind == "mul":
        left = resolve_units(node["left"], resolver, path)
        right = resolve_units(node["right"], resolver, path)
        if left["kind"] == "dimensionless":
            node["unit"] = dict(right)
        elif right["kind"] == "dimensionless":
            node["unit"] = dict(left)
        else:
            require(False, "unit_conflict", "只允许无量纲常量与有量纲值相乘",
                    details={"path": list(path), "left": left, "right": right})
        return node["unit"]
    if kind == "div":
        left = resolve_units(node["left"], resolver, path)
        right = resolve_units(node["right"], resolver, path)
        if _same_unit(left, right):
            node["unit"] = {"kind": "dimensionless", "dimension": "ratio"}
        elif right["kind"] == "dimensionless":
            node["unit"] = dict(left)
        else:
            require(False, "unit_conflict",
                    "除法只支持同单位相除或除以无量纲常量",
                    details={"path": list(path), "left": left, "right": right})
        return node["unit"]
    # ratio：比例，分子分母可以是不同计数维度（如 分/人），结果恒为无量纲率
    resolve_units(node["numerator"], resolver, path)
    resolve_units(node["denominator"], resolver, path)
    node["unit"] = {"kind": "dimensionless", "dimension": "ratio"}
    return node["unit"]


# --------------------------------------------------------------------------
# 循环依赖检测（报告具体路径）
# --------------------------------------------------------------------------

def detect_cycle(definitions):
    """definitions: {key: expression}。发现环时返回包含闭合路径的列表，否则 None。"""
    stack = []
    seen = set()

    def visit(key):
        if key in stack:
            return stack[stack.index(key):] + [key]
        if key in seen or key not in definitions:
            return None
        stack.append(key)
        for ref_key, _version in references(definitions[key])[0]:
            found = visit(ref_key)
            if found:
                return found
        stack.pop()
        seen.add(key)
        return None

    for key in sorted(definitions):
        found = visit(key)
        if found:
            return found
    return None


# --------------------------------------------------------------------------
# 受限求值：白名单节点分派，三态传播
# --------------------------------------------------------------------------

def is_missing(value):
    return isinstance(value, dict) and value.get("status") == "missing"


def is_undefined(value):
    """非 ok 状态（零分母/不适用/缺报）都不能继续参与算术。"""
    return isinstance(value, dict) and "status" in value and value["status"] != "ok"


def ok(number, unit=None):
    return {"status": "ok", "value": number, "unit": unit}


def undefined(status, reason, unit=None, **extra):
    require(status in VALUE_STATUS and status != "ok", "invalid_status", "指标状态无效")
    return {"status": status, "reason": reason, "unit": unit, **extra}


def _binary_status(left, right):
    """算术运算中任一侧不可解释时，选择最该暴露的状态。"""
    order = {"missing": 0, "not_applicable": 1, "zero_denominator": 2}
    chosen = min((left, right), key=lambda item: order[item["status"]])
    return chosen["status"], chosen.get("reason", "")


def evaluate(node, env):
    """在给定环境 env 上求一棵已链接的表达式。

    env 提供：measure(name)->int、metric(key,version)->value 字典、
    exposure(name)->value 字典、const 直接取值。返回带 status 的值字典。
    返回结构：{"status": ..., "value"?: 数字, "reason"?: 说明, "unit": ...}
    """
    kind = node["type"]
    unit = node.get("unit")
    if kind == "const":
        return ok(node["value"], node["unit"])
    if kind == "measure":
        return ok(env["measure"](node["name"]), unit)
    if kind == "exposure":
        return env["exposure"](node["name"])
    if kind == "metric":
        return env["metric"](node["key"], node["version"])
    if kind == "neg":
        operand = evaluate(node["operand"], env)
        if is_undefined(operand):
            return undefined(operand["status"], operand.get("reason", ""), unit)
        return ok(-operand["value"], unit)
    if kind in ("add", "sub", "mul", "div"):
        left = evaluate(node["left"], env)
        right = evaluate(node["right"], env)
        if is_undefined(left) or is_undefined(right):
            status, reason = _binary_status(left, right)
            return undefined(status, reason or "依赖项不可解释", unit)
        a, b = left["value"], right["value"]
        if kind == "add":
            return ok(a + b, unit)
        if kind == "sub":
            return ok(a - b, unit)
        if kind == "mul":
            return ok(a * b, unit)
        if b == 0:
            return undefined("zero_denominator", "除法分母为零", unit)
        return ok(a / b, unit)
    # ratio：保留独立的零分母/不适用状态，不与普通除法混淆
    numerator = evaluate(node["numerator"], env)
    denominator = evaluate(node["denominator"], env)
    if is_undefined(denominator):
        return undefined(denominator["status"],
                         denominator.get("reason", "比例分母不可解释"), unit,
                         numerator_status=numerator.get("status"))
    if is_undefined(numerator):
        return undefined(numerator["status"], numerator.get("reason", "比例分子不可解释"), unit)
    if denominator["value"] == 0:
        return undefined("zero_denominator", "比例分母为零", unit,
                         numerator_value=numerator["value"])
    return ok(numerator["value"] / denominator["value"], unit)


def explain(node, indent=0):
    """生成可读的定义证据文本（导出用），不暴露任何可执行代码。"""
    pad = "  " * indent
    kind = node["type"]
    if kind == "const":
        return f"{pad}常量 {node['value']}（单位 {node['unit']['dimension']}）"
    if kind == "measure":
        return f"{pad}基础度量 {node['name']}（{BASE_MEASURES[node['name']]['label']}）"
    if kind == "exposure":
        return f"{pad}暴露分母 {node['name']}（{EXPOSURE_DENOMINATORS[node['name']]['label']}）"
    if kind == "metric":
        return f"{pad}指标引用 {node['key']}@v{node['version']}"
    if kind == "neg":
        return f"{pad}取负\n" + explain(node["operand"], indent + 1)
    labels = {"add": "加", "sub": "减", "mul": "乘", "div": "除", "ratio": "比例（汇总分子/汇总分母）"}
    if kind == "ratio":
        return (f"{pad}{labels[kind]}\n{pad}  分子:\n" + explain(node["numerator"], indent + 2)
                + f"\n{pad}  分母:\n" + explain(node["denominator"], indent + 2))
    return (f"{pad}{labels[kind]}\n{pad}  左:\n" + explain(node["left"], indent + 2)
            + f"\n{pad}  右:\n" + explain(node["right"], indent + 2))
