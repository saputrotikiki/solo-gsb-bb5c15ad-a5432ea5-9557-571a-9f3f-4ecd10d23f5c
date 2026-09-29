"""多站送达分运费按在车批次精确分摊。

排程确定后,运营人员按 **实际行驶顺序**(仓库 → 首站、其后相邻两站)逐程
填写 **非负整数分运费**(``fees``,单位:分,条数必须等于站数)。服务端
沿用排程的规划输入快照与站点归属、时间窗、站序规则
(:mod:`app.delivery`)重新核对可行路线:输入非法或无可行站序时直接拒绝,
**不返回任何部分分摊**。

分摊规则:

- 每程只让 **尚未在前站卸下** 的批次分担费用。每个批次恰好归属一站,
  故其在「归属站及之前」各程在车,该站卸毕后不再参与后续各程;
- 权重 = **剩余件数 × 单件体积**(同批拆多箱时按重新规划结果合并件数);
- 份额按 **精确比例** 计算:份额 = 程运费 × 本批权重 ÷ 在车权重合计
  (用有理数精确计算,不做中间舍入);
- 不足一分的尾差用 **最大余数法** 分配:各批先取份额的整数部分(向下取整),
  剩余的若干分按余数从大到小各补 1 分;**余数相同按批号小者优先**;
- 每程分摊合计与该程输入费用严格相等,各批合计之和与费用总额严格相等。

响应同样携带输入快照(规划输入 + 排程输入 + 逐程费用合并后)的 SHA-256
指纹,输入变动后旧分摊结果由前端标记为过期。
"""
from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Any

from .delivery import (
    ScheduleError,
    _build_schedule_response,
    _fingerprint_payload,
    compute_schedule_basis,
)
from .planner import _r, input_fingerprint


def _parse_fees(payload: dict[str, Any], n: int) -> list[int]:
    """解析逐程费用:必须恰好 n 条,每条为非负整数(分);否则拒绝。"""
    raw = payload.get("fees")
    if not isinstance(raw, list):
        raise ScheduleError(
            "请按实际行驶顺序逐程填写分运费(fees:非负整数列表),"
            f"当前共 {n} 程(站点数),费用条数须与站序一致"
        )
    if len(raw) != n:
        raise ScheduleError(
            f"分运费共 {len(raw)} 条,但当前排程共 {n} 程(站点数);"
            "费用条数须与站序严格一致(仓库→首站及相邻两站各一程),"
            "已拒绝,不返回部分分摊"
        )
    fees: list[int] = []
    for k, v in enumerate(raw):
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ScheduleError(
                f"第 {k + 1} 程分运费须为非负整数(单位:分),实际为 {v!r},"
                "不接受小数、负数或非数值,已拒绝,不返回部分分摊"
            )
        fees.append(int(v))
    return fees


def _exact_weight(unit_volume: Any) -> Fraction:
    """把单件体积转为精确有理数权重系数。

    规划快照中的单件体积是 JSON 数值(未参与过浮点运算),先取其最短十进制
    表示(str(float) 可往返还原用户输入)再转 Fraction,使 4×0.1 与 1×0.4
    这类在十进制下本来相等的权重仍被判为相等(同余数按批号小者优先)。
    """
    return Fraction(Decimal(str(unit_volume)))


def allocate_freight(payload: dict[str, Any]) -> dict[str, Any]:
    """分运费分摊入口;输入非法或排程无解时抛 ScheduleError,不返回部分结果。"""
    # 1) 复用排程全部校验(快照、站点、矩阵、归属、全员可运输、可行站序)
    (
        plan_input,
        stations,
        travel,
        assignments,
        baseline,
        batch_names,
        station_items,
    ) = compute_schedule_basis(payload)

    n = len(stations)
    fees = _parse_fees(payload, n)

    # 2) 重新穷举得到最优站序(与送达排程结果页同一结果;无解在此抛错)
    schedule = _build_schedule_response(
        plan_input, stations, travel, assignments,
        baseline, batch_names, station_items,
    )
    order = [int(x) - 1 for x in schedule["optimal_order"]]
    stops = schedule["stops"]

    # 3) 每批总件数(同批拆多箱合并)与单件体积,一律以重新规划快照为准
    cargo_rows = plan_input.get("cargoes") or []
    total_units: dict[int, int] = {}
    for sidx in range(n):
        for it in station_items[sidx]:
            total_units[int(it["batch_no"])] = (
                total_units.get(int(it["batch_no"]), 0) + int(it["units"])
            )
    unit_volume: dict[int, Any] = {}
    for i, c in enumerate(cargo_rows):
        bno = i + 1
        if bno in total_units:
            unit_volume[bno] = c["unit_volume"]
    weight_coef = {bno: _exact_weight(unit_volume[bno]) for bno in total_units}
    dest_station = dict(assignments)  # {批号: 站序号(0 起)}

    # 4) 逐程分摊:在车批次 = 归属站在最优站序中位于本程及之后的批次
    #    (尚未在前站卸下);份额按精确比例,尾差用最大余数法
    station_position = {sidx: pos for pos, sidx in enumerate(order)}
    batch_totals: dict[int, int] = {bno: 0 for bno in total_units}
    legs: list[dict[str, Any]] = []
    for pos, sidx in enumerate(order):
        fee = fees[pos]
        onboard = sorted(
            bno for bno, s in dest_station.items()
            if station_position[s] >= pos
        )
        weights = {
            bno: weight_coef[bno] * total_units[bno] for bno in onboard
        }
        weight_sum = sum(weights.values(), Fraction(0, 1))
        if weight_sum == 0:
            # 该程到站没有任何归属批次(空站),车辆上本程不应再分担费用
            if fee != 0:
                raise ScheduleError(
                    f"第 {pos + 1} 程(站点 {sidx + 1}「{stations[sidx]['name']}」)"
                    "没有任何在车批次(无批次归属该站,前站已全部卸完),"
                    f"该程费用必须为 0 分,实际填写 {fee} 分,已拒绝,不返回部分分摊"
                )
            stop = stops[pos]
            legs.append(
                {
                    "seq": pos + 1,
                    "station_no": sidx + 1,
                    "name": stations[sidx]["name"],
                    "from": "仓库" if pos == 0
                    else f"站点 {order[pos - 1] + 1}"
                         f"「{stations[order[pos - 1]]['name']}」",
                    "drive_hours": stop["drive_hours"],
                    "fee_cents": 0,
                    "onboard_count": 0,
                    "onboard": [],
                    "allocated_total_cents": 0,
                }
            )
            continue

        exact_share: dict[int, Fraction] = {}
        floors: dict[int, int] = {}
        remainders: dict[int, Fraction] = {}
        for bno in onboard:
            share = Fraction(fee, 1) * weights[bno] / weight_sum
            exact_share[bno] = share
            fl = share.numerator // share.denominator  # 份额非负,即向下取整
            floors[bno] = fl
            remainders[bno] = share - fl

        leftover = fee - sum(floors.values())
        # 最大余数法:余数大者优先补 1 分;同余数批号小者优先
        ranked = sorted(onboard, key=lambda b: (-remainders[b], b))
        allocated = dict(floors)
        for bno in ranked[:leftover]:
            allocated[bno] += 1

        stop = stops[pos]
        onboard_rows = []
        for bno in onboard:
            share = exact_share[bno]
            rem = remainders[bno]
            onboard_rows.append(
                {
                    "batch_no": bno,
                    "name": batch_names.get(bno, f"批次 {bno}"),
                    "remaining_units": total_units[bno],
                    "unit_volume": _r(unit_volume[bno]),
                    "weight": _r(float(weights[bno])),
                    "exact_share_numerator": share.numerator,
                    "exact_share_denominator": share.denominator,
                    "floor_cents": floors[bno],
                    "remainder_numerator": rem.numerator,
                    "remainder_denominator": rem.denominator,
                    "allocated_cents": allocated[bno],
                }
            )
        leg_total = sum(allocated.values())
        assert leg_total == fee, "每程分摊合计必须与输入费用严格相等"
        legs.append(
            {
                "seq": pos + 1,
                "station_no": sidx + 1,
                "name": stations[sidx]["name"],
                "from": "仓库" if pos == 0
                else f"站点 {order[pos - 1] + 1}"
                     f"「{stations[order[pos - 1]]['name']}」",
                "drive_hours": stop["drive_hours"],
                "fee_cents": fee,
                "onboard_count": len(onboard),
                "onboard": onboard_rows,
                "allocated_total_cents": leg_total,
            }
        )
        for bno, cents in allocated.items():
            batch_totals[bno] += cents

    grand_total = sum(batch_totals.values())
    assert grand_total == sum(fees), "各批合计必须与费用总额严格相等"

    batch_rows = [
        {
            "batch_no": bno,
            "name": batch_names.get(bno, f"批次 {bno}"),
            "total_units": total_units[bno],
            "unit_volume": _r(unit_volume[bno]),
            "station_no": dest_station[bno] + 1,
            "station_name": stations[dest_station[bno]]["name"],
            "total_allocated_cents": batch_totals[bno],
        }
        for bno in sorted(batch_totals)
    ]

    fp_payload = {
        "plan_input": plan_input,
        "delivery": _fingerprint_payload(stations, travel, assignments),
        "freight_fees": fees,
    }
    return {
        "input_fingerprint": input_fingerprint(fp_payload),
        "plan_fingerprint": input_fingerprint(plan_input),
        "schedule_fingerprint": input_fingerprint(
            {"plan_input": plan_input,
             "delivery": _fingerprint_payload(stations, travel, assignments)}
        ),
        "forbidden_mode": bool(baseline["summary"].get("forbidden_mode")),
        "transport_total_hours": schedule["transport_total_hours"],
        "optimal_order": schedule["optimal_order"],
        "final_finish": schedule["final_finish"],
        "station_count": n,
        "leg_count": len(legs),
        "fees_cents": fees,
        "legs": legs,
        "batch_totals": batch_rows,
        "total_fee_cents": grand_total,
        "allocated_total_cents": grand_total,
        "balanced": True,
        "verdict": (
            f"按最优站序 {' → '.join(str(x) for x in schedule['optimal_order'])} "
            f"逐程分摊 {len(legs)} 程费用,共 {grand_total} 分;"
            "各程分摊合计与输入费用严格相等,各批合计之和与费用总额严格相等"
            "(精确比例 + 最大余数法,同余数批号小者优先)"
        ),
    }
