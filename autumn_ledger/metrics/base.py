"""指标口径可以引用的基础度量目录。

基础度量由订单快照直接累加得到（见 records.contracts.contribution 与
analysis.cubes.MEASURES），口径只能按名称引用，不能重新定义，因此老报告的
固定指标行为完全不受指标登记影响。
"""

# 每个基础度量的计量单位。单位用 (种类, 维度) 描述，种类决定能否相加。
# count 类按计数对象区分维度（订单、人、城市日期不可混加）；
# currency 类统一为人民币分；city_day 是暴露分母的计数维度。
BASE_MEASURES = {
    "orders": {"label": "有效订单数", "unit": {"kind": "count", "dimension": "order"},
               "accumulable": True, "description": "state=booked 的订单头数量"},
    "cancelled_orders": {"label": "取消订单数", "unit": {"kind": "count", "dimension": "order"},
                         "accumulable": True, "description": "state=cancelled 的订单头数量"},
    "travelers": {"label": "有效出游人数", "unit": {"kind": "count", "dimension": "traveler"},
                  "accumulable": True, "description": "有效订单的成人加儿童人数"},
    "children": {"label": "儿童出游人数", "unit": {"kind": "count", "dimension": "traveler"},
                 "accumulable": True, "description": "有效订单中的儿童人数"},
    "gross_cents": {"label": "订单总额（分）", "unit": {"kind": "currency", "dimension": "cents"},
                    "accumulable": True, "description": "全部订单头的 gross_cents 之和"},
    "refund_cents": {"label": "累计退款（分）", "unit": {"kind": "currency", "dimension": "cents"},
                     "accumulable": True, "description": "订单头累计退款之和，非本次增量"},
    "net_cents": {"label": "净收入（分）", "unit": {"kind": "currency", "dimension": "cents"},
                  "accumulable": True, "description": "gross_cents 减 refund_cents，取消订单未退部分仍计入"},
    "family_orders": {"label": "亲子订单数", "unit": {"kind": "count", "dimension": "order"},
                      "accumulable": True, "description": "有效且 family=true 的订单数"},
}

# 明确可选的暴露分母。denominator 取值即登记口径时允许使用的键。
# scope=both 表示总计与分组都可计算；每个暴露分母在两种场景下的取值由
# analysis.metrics.runtime 的上下文给出。
EXPOSURE_DENOMINATORS = {
    "city_days": {
        "label": "选定城市日期暴露数", "unit": {"kind": "city_day", "dimension": "city_day"},
        "scope": "both",
        "total": "窗口内选定城市的城市日期格点数（不按有订单的日期计）",
        "group": "分组覆盖城市在窗口内的城市日期格点数；日明细行恒为 1",
    },
    "complete_city_days": {
        "label": "完整覆盖城市日期暴露数", "unit": {"kind": "city_day", "dimension": "city_day"},
        "scope": "both",
        "total": "覆盖状态为 complete 的城市日期格点数",
        "group": "分组内覆盖状态为 complete 的城市日期格点数；日明细行取该格点状态",
    },
    "orders": {
        "label": "有效订单分母", "unit": {"kind": "count", "dimension": "order"},
        "scope": "both",
        "total": "与基础度量 orders 相同的有效订单数",
        "group": "该分组的有效订单数",
    },
    "travelers": {
        "label": "有效出游人数分母", "unit": {"kind": "count", "dimension": "traveler"},
        "scope": "both",
        "total": "与基础度量 travelers 相同的有效出游人数",
        "group": "该分组的有效出游人数",
    },
}


def base_measure(identifier):
    return BASE_MEASURES[identifier]


def exposure(identifier):
    return EXPOSURE_DENOMINATORS[identifier]


def catalog_payload():
    """供冻结证据与接口输出的只读目录快照。"""
    return {"base_measures": BASE_MEASURES, "exposure_denominators": EXPOSURE_DENOMINATORS}
