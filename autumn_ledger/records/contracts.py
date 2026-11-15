from ..errors import require
from ..values import boolean, fields, identifier, instant, integer, iso_day


EVENT_FIELDS = {"event_id", "order_id", "sequence", "occurred_at", "state", "origin", "destination",
                "travel_day", "family", "adults", "children", "gross_cents", "refund_cents"}
EVENT_REQUIRED = EVENT_FIELDS


def normalize_event(value):
    fields(value, EVENT_FIELDS, EVENT_REQUIRED)
    state = value["state"]
    require(state in {"booked", "cancelled"}, "invalid_state", "订单状态只支持booked/cancelled")
    result = {"event_id": identifier(value["event_id"], "event_id"),
              "order_id": identifier(value["order_id"], "order_id"),
              "sequence": integer(value["sequence"], "sequence", 1, 10**9),
              "occurred_at": instant(value["occurred_at"]), "state": state,
              "origin": identifier(value["origin"], "origin"),
              "destination": identifier(value["destination"], "destination"),
              "travel_day": iso_day(value["travel_day"], "出游日期").isoformat(),
              "family": boolean(value["family"], "family"),
              "adults": integer(value["adults"], "adults", 0, 10000),
              "children": integer(value["children"], "children", 0, 10000),
              "gross_cents": integer(value["gross_cents"], "gross_cents", 0, 10**11),
              "refund_cents": integer(value["refund_cents"], "refund_cents", 0, 10**11)}
    require(result["adults"] + result["children"] > 0, "empty_party", "订单至少有一位旅客")
    require(result["refund_cents"] <= result["gross_cents"], "excess_refund", "累计退款不能超过订单金额")
    require(not result["family"] or result["children"] > 0,
            "invalid_family", "亲子订单需要儿童人数")
    require(result["family"] or result["children"] == 0,
            "invalid_family", "含儿童订单应标记为亲子订单")
    return result


def validate_transition(previous, current):
    if previous is None:
        return
    require(current["sequence"] > previous["sequence"], "stale_event", "修订序号必须递增")
    require(current["occurred_at"] >= previous["occurred_at"],
            "event_time_reversed", "递增修订的事件时间不能倒退")
    require(previous["state"] != "cancelled" or current["state"] == "cancelled",
            "cancelled_terminal", "已取消订单不能重新预订，应使用新订单号")


def contribution(event):
    active = event["state"] == "booked"
    net = event["gross_cents"] - event["refund_cents"]
    return {"orders": int(active), "cancelled_orders": int(not active),
            "travelers": event["adults"] + event["children"] if active else 0,
            "children": event["children"] if active else 0,
            "gross_cents": event["gross_cents"], "refund_cents": event["refund_cents"],
            "net_cents": net, "family_orders": int(active and event["family"])}
