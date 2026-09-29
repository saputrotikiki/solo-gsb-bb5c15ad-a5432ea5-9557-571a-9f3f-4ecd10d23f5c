"""多站送达分运费分摊。

排程确定后,运营人员按 **实际行驶顺序**(仓库 → 首站、相邻两站)逐程填写
非负整数分运费。服务端沿用现有规划输入快照与站点归属、时间窗、站序规则
(``delivery._solve_delivery``)重新核对可行路线,再做分摊:

- 每程只让 **尚未在前站卸下** 的批次分担费用——批次在其归属站卸毕后,
  后续程不再参与;
- 权重 = 该批在车 **剩余件数 × 单件体积**(剩余件数取自快照核对后的
  规划件数,单件体积取自规划输入,全程为精确有理值);
- 按精确比例(``fractions.Fraction``)计算各批份额,先用最大余数法
  (Hamilton/Largest-Remainder)分配不足一分的尾差:每批先取份额下取整,
  再把尾差(总额 − 已分配)逐分补给分数余数最大者,**同余数按批号小者
  优先**;
- 逐程分摊金额之和 **严格等于** 该程输入费用,各批合计之和 **严格等于
  全程费用总额**。

费用条数与站序程数不符、费用非法(非非负整数)或排程无解时拒绝,
抛 FreightError(HTTP 400),**不返回任何部分分摊**。

响应同时携带排程结果与指纹:规划输入或排程输入变化后,前端将旧分摊
结果标记为已过期。
"""
from __future__ import annotations

from fractions import Fraction
from typing import Any

from .delivery import _solve_delivery
from .planner import _r, input_fingerprint


class FreightError(ValueError):
    """运费分摊业务校验错误(与 ScheduleError 同样映射为 HTTP 400)。"""


def _parse_freight_fees(payload: dict[str, Any], leg_count: int) -> list[int]:
    """解析逐程费用列表:必须为恰好 leg_count 个非负整数(分)。

    布尔、浮点、非数字、负数或条数不符均拒绝,消息可直接展示。
    """
    raw = payload.get("freight_fees")
    if not isinstance(raw, list):
        raise FreightError(
            "请按实际行驶顺序逐程填写分运费 freight_fees:"
            f"须为 {leg_count} 个非负整数(分)组成的列表"
        )
    if len(raw) != leg_count:
        raise FreightError(
            f"运费条数 {len(raw)} 与站序程数 {leg_count} 不符:"
            f"当前最优站序共 {leg_count} 程(仓库→首站及相邻两站),"
            "请按实际行驶顺序逐程填写费用"
        )
    fees: list[int] = []
    for k, v in enumerate(raw):
        if isinstance(v, bool) or not isinstance(v, int):
            raise FreightError(
                f"第 {k + 1} 程分运费须为非负整数(单位:分),"
                f"实际为 {v!r},已拒绝"
            )
        if v < 0:
            raise FreightError(
                f"第 {k + 1} 程分运费不能为负数,实际为 {v},已拒绝"
            )
        fees.append(v)
    return fees


def _allocate_leg(
    fee: int, weights: list[tuple[int, int, Fraction]], leg_no: int
) -> list[dict[str, Any]]:
    """对一程费用按 (批号, 在车件数, 权重) 做最大余数法分摊。

    返回按批号升序的 [{batch_no, remaining_units, unit_volume, weight,
    exact_share, share_ratio, amount_cents}],各 amount_cents 之和恒为 fee。
    分数余数相等时批号小者优先拿到尾差。
    """
    if not weights:
        # 该程无在车批次(后续站点均无归属批次):正费用无处分摊,拒绝;
        # 费用为 0 时返回空分摊,逐程合计 0 仍与输入严格相等
        if fee > 0:
            raise FreightError(
                f"第 {leg_no} 程行驶时车上已无任何批次(后续站点均无归属批次),"
                f"该程费用 {fee} 分无处分摊,请改为 0 分或调整批次归属后重试"
            )
        return []
    total_weight = sum((w for _, _, w in weights), Fraction(0))
    # 全员可运输方案中每批至少 1 件且单件体积 > 0,故每程总权重必为正
    if total_weight <= 0:
        raise FreightError("在车批次总权重为 0,无法按件数×体积分摊费用")

    exact: dict[int, Fraction] = {
        bno: fee * w / total_weight for bno, _, w in weights
    }
    floors = {bno: int(share.numerator // share.denominator)
              for bno, share in exact.items()}
    remainder = fee - sum(floors.values())
    # 分数余数降序;同余数批号升序优先拿尾差
    order = sorted(
        exact.keys(),
        key=lambda b: (-(exact[b] - floors[b]), b),
    )
    amounts = dict(floors)
    for bno in order[:remainder]:
        amounts[bno] += 1

    rows: list[dict[str, Any]] = []
    for bno, remaining, w in sorted(weights, key=lambda x: x[0]):
        share = exact[bno]
        ratio = w / total_weight
        rows.append(
            {
                "batch_no": bno,
                "remaining_units": remaining,
                "unit_volume": None,  # 由调用方补上(展示用)
                "weight": f"{w.numerator}/{w.denominator}",
                "weight_decimal": _fraction_decimal(w),
                "share_ratio": f"{ratio.numerator}/{ratio.denominator}",
                "exact_share": f"{share.numerator}/{share.denominator}",
                "exact_share_decimal": _fraction_decimal(share),
                "amount_cents": amounts[bno],
            }
        )
    return rows


def _fraction_decimal(value: Fraction, places: int = 6) -> float:
    """把精确分数转展示用有限小数(四舍五入到 places 位)。"""
    factor = 10**places
    rounded = (value * factor + Fraction(1, 2)).numerator // (
        value * factor + Fraction(1, 2)
    ).denominator
    return rounded / factor


def allocate_freight(payload: dict[str, Any]) -> dict[str, Any]:
    """运费分摊入口;输入非法或排程无解时抛 FreightError/ScheduleError。"""
    # 先按排程规则重新核对可行路线(站点/归属/时间窗非法或排程无解时直接
    # 拒绝,不返回部分分摊),再以最优站序程数核对费用条数与取值。
    solved = _solve_delivery(payload)
    stops = solved["stops"]
    leg_count = len(stops)
    fees = _parse_freight_fees(payload, leg_count)

    batch_meta = solved["batch_meta"]
    # 精确权重:件数(整数)× 单件体积(以输入小数字符串构造精确分数)
    volume_frac: dict[int, Fraction] = {}
    for bno, meta in batch_meta.items():
        volume_frac[bno] = Fraction(str(meta["unit_volume"]))
    total_units = {bno: int(meta["units"]) for bno, meta in batch_meta.items()}

    # 按最优站序逐程确定在车批次:归属站未到(站序位置 > 当前程)即在车
    order = solved["optimal_order"]  # 0 起站点下标
    pos_of_station = {sidx: pos for pos, sidx in enumerate(order)}
    legs: list[dict[str, Any]] = []
    batch_totals: dict[int, int] = {bno: 0 for bno in batch_meta}
    grand_total = 0
    for pos, (stop, fee) in enumerate(zip(stops, fees)):
        onboard: list[tuple[int, int, Fraction]] = []
        for bno in sorted(batch_meta.keys()):
            if pos_of_station[batch_meta[bno]["station_idx"]] >= pos:
                # 尚未在前站卸下(本程在车,含本站要卸的批次)
                remaining = total_units[bno]
                onboard.append(
                    (bno, remaining, Fraction(remaining) * volume_frac[bno])
                )
        allocations = _allocate_leg(fee, onboard, pos + 1)
        for row in allocations:
            bno = row["batch_no"]
            row["name"] = batch_meta[bno]["name"]
            row["unit_volume"] = batch_meta[bno]["unit_volume"]
            batch_totals[bno] += row["amount_cents"]
        leg_sum = sum(r["amount_cents"] for r in allocations)
        if leg_sum != fee:  # 兜底:最大余数法保证恒等
            raise FreightError("内部分摊错误:逐程合计与输入费用不一致")
        grand_total += fee
        legs.append(
            {
                "leg_no": pos + 1,
                "from": "仓库" if pos == 0 else f"站点 {stops[pos - 1]['station_no']}",
                "from_station_no": None if pos == 0 else stops[pos - 1]["station_no"],
                "to": f"站点 {stop['station_no']}",
                "to_station_no": stop["station_no"],
                "station_name": stop["name"],
                "drive_hours": stop["drive_hours"],
                "freight_cents": fee,
                "onboard_batch_count": len(allocations),
                "allocated_total_cents": leg_sum,
                "allocations": allocations,
            }
        )

    if sum(batch_totals.values()) != grand_total:
        raise FreightError("内部分摊错误:各批合计与费用总额不一致")

    # 指纹:规划输入 + 排程输入 + 逐程费用;任一变化旧结果即过期
    fee_fp_payload = {
        "plan_input": solved["plan_input"],
        "delivery": solved["delivery_fp_payload"],
        "freight_fees": fees,
    }
    schedule = solved["response"]
    order_text = " → ".join(map(str, schedule["optimal_order"]))
    verdict = (
        f"按最优站序 {order_text} 共 {len(legs)} 程完成运费分摊:"
        f"全程运费合计 {grand_total} 分,"
        "每程按在车批次剩余件数×单件体积精确比例分摊,"
        "不足一分的尾差按最大余数法分配(同余数批号小者优先),"
        "逐程及总额均与输入费用严格相等"
    )
    return {
        "input_fingerprint": input_fingerprint(fee_fp_payload),
        "schedule_fingerprint": schedule["input_fingerprint"],
        "plan_fingerprint": schedule["plan_fingerprint"],
        "forbidden_mode": schedule["forbidden_mode"],
        "optimal_order": schedule["optimal_order"],
        "transport_total_hours": schedule["transport_total_hours"],
        "orders_evaluated": schedule["orders_evaluated"],
        "feasible_order_count": schedule["feasible_order_count"],
        "stops": schedule["stops"],
        "stations": schedule["stations"],
        "schedule_verdict": schedule["verdict"],
        "currency": "CNY-cent",
        "leg_count": len(legs),
        "legs": legs,
        "batch_totals": [
            {
                "batch_no": bno,
                "name": batch_meta[bno]["name"],
                "units": total_units[bno],
                "unit_volume": _r(float(volume_frac[bno])),
                "station_no": batch_meta[bno]["station_idx"] + 1,
                "total_cents": batch_totals[bno],
            }
            for bno in sorted(batch_totals.keys())
        ],
        "total_freight_cents": grand_total,
        "batch_total_sum_cents": sum(batch_totals.values()),
        "feasible": True,
        "verdict": verdict,
    }
