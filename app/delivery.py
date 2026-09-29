"""送达站点排程。

以生成方案时的输入快照(``plan_input``,与 /api/plan 请求体相同)重新规划,
先 **核对箱号、批次与件数**(服务端只按重新规划得到的全员可运输装箱方案
排程,绝不信任客户端随附的任何明细),再为每个货物批次指定一个送达站点,
在下列规则下穷举全部可行站序:

- 站点数 1~8 个;每个批次恰好归属一站(不允许遗漏);
- 每站填写可签收时间窗 [``window_start``, ``window_end``](出库时刻为 0)、
  卸货耗时 ``unload_hours`` 与有向行车时长:仓库 → 各站(``from_warehouse``)、
  站 i → 站 j(矩阵 ``travel``,行=上一站、列=下一站,有向,不要求对称);
  以上数值均须为非负有限数,窗口结束不得早于开始;
- 抵站过早(早于窗口开始)须等待到窗口开始才开始卸货;
- 卸货必须在窗口结束前完成(完成时刻 ≤ 窗口结束,恰等允许);
- 最后一站卸毕时刻不得超过方案运输总时长(``summary.transport_hours``,
  恰等允许)。

在全部可行站序中取 **最终卸毕时刻最早** 者;并列时取按输入站点序号
(1 起)组成的站序 **字典序最小** 者。响应给出逐站抵达/开始卸货/等待/
卸毕时刻,以及该站对应的箱号、批次、件数明细。不存在任何可行站序时
抛 ValueError(HTTP 400),不返回部分路线。

响应携带输入快照与排程输入合并后的 SHA-256 指纹,前端据此在输入变化后
将旧排程结果标记为已过期。
"""
from __future__ import annotations

import itertools
import math
from typing import Any

from .planner import EPS, _r, input_fingerprint, plan

MAX_STATIONS = 8


class ScheduleError(ValueError):
    """排程业务校验错误(与 plan 的 ValueError 同样映射为 HTTP 400)。"""


def _nonneg_number(raw: Any, label: str) -> float:
    """解析非负有限数值;非法时抛出指明字段名的 ScheduleError。"""
    if isinstance(raw, bool):
        raise ScheduleError(f"{label}须为非负数值,实际为 {raw!r},已拒绝")
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ScheduleError(f"{label}须为非负数值,实际为 {raw!r},已拒绝") from None
    if not math.isfinite(v) or v < 0:
        raise ScheduleError(f"{label}须为非负有限数值,实际为 {raw!r},已拒绝")
    return v


def _station_name(raw: Any, idx: int) -> str:
    name = str(raw or "").strip()
    return name or f"站点 {idx + 1}"


def _parse_delivery_input(payload: dict[str, Any]) -> tuple[
    list[dict[str, Any]], list[list[float]], dict[int, int], int
]:
    """解析并校验站点、有向行车时长矩阵与批次归属。

    返回 (站点列表(按输入序号), 行车时长矩阵, {批号: 站序号(0 起)},
    归属明细行数;紧凑列表形式时该计数为 0,由调用方按批次数补核)。
    """
    raw_stations = payload.get("stations")
    if not isinstance(raw_stations, list) or not raw_stations:
        raise ScheduleError("请至少填写 1 个送达站点")
    n = len(raw_stations)
    if n > MAX_STATIONS:
        raise ScheduleError(f"送达站点数限 1~{MAX_STATIONS} 个,当前为 {n} 个")

    stations: list[dict[str, Any]] = []
    for i, st in enumerate(raw_stations):
        if not isinstance(st, dict):
            raise ScheduleError(f"第 {i + 1} 个站点的输入格式不正确")
        label = f"站点 {i + 1}"
        name = _station_name(st.get("name"), i)
        label = f"站点 {i + 1}「{name}」"
        w_start = _nonneg_number(
            st.get("window_start"), f"{label} 的窗口开始时间"
        )
        w_end = _nonneg_number(st.get("window_end"), f"{label} 的窗口结束时间")
        if w_end + EPS < w_start:
            raise ScheduleError(
                f"{label} 的窗口结束 {_r(w_end)}h 早于开始 {_r(w_start)}h"
            )
        unload = _nonneg_number(st.get("unload_hours"), f"{label} 的卸货耗时")
        from_wh = _nonneg_number(
            st.get("from_warehouse"), f"{label} 的仓库→站点行车时长"
        )
        stations.append(
            {
                "index": i + 1,
                "name": name,
                "window_start": w_start,
                "window_end": w_end,
                "unload_hours": unload,
                "from_warehouse": from_wh,
            }
        )

    # 有向行车时长矩阵:n×n,行=上一站、列=下一站;对角线(自己→自己)不使用
    raw_travel = payload.get("travel")
    if raw_travel is None:
        # 兼容:矩阵也可直接内嵌在站点行的 travel_to 列表中
        raw_travel = [st.get("travel_to") for st in raw_stations]
    if not isinstance(raw_travel, list) or len(raw_travel) != n:
        raise ScheduleError(
            f"站点间有向行车时长矩阵须为 {n}×{n} 的二维数组(行=上一站、列=下一站)"
        )
    travel: list[list[float]] = []
    for i, row in enumerate(raw_travel):
        if not isinstance(row, list) or len(row) != n:
            raise ScheduleError(
                f"站点间有向行车时长矩阵第 {i + 1} 行须含 {n} 个数值"
                f"(行=上一站、列=下一站)"
            )
        travel.append(
            [
                _nonneg_number(
                    v, f"站点 {i + 1} → 站点 {j + 1} 的行车时长"
                )
                for j, v in enumerate(row)
            ]
        )

    # 批次归属:每批恰好一站
    raw_assign = payload.get("assignments")
    assignments: dict[int, int] = {}
    assign_hint = 0
    if isinstance(raw_assign, list) and raw_assign and all(
        isinstance(a, dict) for a in raw_assign
    ):
        # 显式明细形式:[{batch_no: 1, station_no: 2}, ...]
        assign_hint = len(raw_assign)
        for k, a in enumerate(raw_assign):
            bno_raw = a.get("batch_no", k + 1)
            if isinstance(bno_raw, bool):
                raise ScheduleError(
                    f"批次归属第 {k + 1} 行的批号非法:{bno_raw!r}"
                )
            try:
                bno = int(bno_raw)
            except (TypeError, ValueError):
                raise ScheduleError(
                    f"批次归属第 {k + 1} 行的批号非法:{bno_raw!r}"
                ) from None
            sno_raw = a.get("station_no")
            if sno_raw is None:
                raise ScheduleError(f"批次 {bno} 未指定送达站点")
            if isinstance(sno_raw, bool):
                raise ScheduleError(
                    f"批次 {bno} 的送达站号须为 1~{n} 的整数,实际为 {sno_raw!r}"
                )
            try:
                sno = int(sno_raw)
            except (TypeError, ValueError):
                raise ScheduleError(
                    f"批次 {bno} 的送达站号须为 1~{n} 的整数,实际为 {sno_raw!r}"
                ) from None
            if not (1 <= sno <= n):
                raise ScheduleError(
                    f"批次 {bno} 的送达站号 {sno} 超出范围:当前共 {n} 个站点,"
                    f"站号须为 1~{n}"
                )
            if bno in assignments:
                raise ScheduleError(f"批次 {bno} 重复指定了送达站点")
            assignments[bno] = sno - 1
    else:
        # 紧凑形式:按批次输入顺序给出站号列表,如 [2, 1, 2]
        if not isinstance(raw_assign, list):
            raise ScheduleError(
                "请为每个货物批次指定一个送达站点"
                "(assignments:按批次顺序的站号列表,或 {batch_no, station_no} 明细)"
            )
        for k, raw_sno in enumerate(raw_assign):
            if isinstance(raw_sno, bool) or not isinstance(raw_sno, int):
                raise ScheduleError(
                    f"第 {k + 1} 批货物的送达站号须为 1~{n} 的整数,"
                    f"实际为 {raw_sno!r}"
                )
            if not (1 <= raw_sno <= n):
                raise ScheduleError(
                    f"第 {k + 1} 批货物的送达站号 {raw_sno} 超出范围:"
                    f"当前共 {n} 个站点,站号须为 1~{n}"
                )
            assignments[k + 1] = raw_sno - 1

    return stations, travel, assignments, assign_hint


def _evaluate_order(
    order: list[int],
    stations: list[dict[str, Any]],
    travel: list[list[float]],
    total_hours: float,
) -> float | None:
    """快速评估给定站序(0 起下标)的末站卸毕时刻;不可行返回 None。

    时刻约定:出库时刻为 0。首站从仓库出发(行车时长 from_warehouse),
    之后按有向矩阵 travel[上一站][本站] 行驶。到站早于窗口开始则等待;
    卸货完成时刻超过该站窗口结束,或末站卸毕超过方案运输总时长,即不可行。
    """
    t = 0.0
    prev: int | None = None
    last = len(order) - 1
    for pos, idx in enumerate(order):
        st = stations[idx]
        drive = st["from_warehouse"] if prev is None else travel[prev][idx]
        arrival = t + drive
        start = st["window_start"] if arrival < st["window_start"] else arrival
        finish = start + st["unload_hours"]
        if finish > st["window_end"] + EPS:
            return None
        if pos == last and finish > total_hours + EPS:
            return None
        t = finish
        prev = idx
    return t


def _build_stops(
    order: list[int],
    stations: list[dict[str, Any]],
    travel: list[list[float]],
) -> list[dict[str, Any]]:
    """对已判定可行的站序构建逐站时刻明细(抵达/等待/卸货/卸毕)。"""
    rows: list[dict[str, Any]] = []
    t = 0.0
    prev: int | None = None
    for pos, idx in enumerate(order):
        st = stations[idx]
        drive = st["from_warehouse"] if prev is None else travel[prev][idx]
        arrival = _r(t + drive)
        start = max(arrival, st["window_start"])
        rows.append(
            {
                "seq": pos + 1,
                "station_idx": idx,
                "station_no": st["index"],
                "name": st["name"],
                "departure_time": _r(t),
                "drive_hours": _r(drive),
                "arrival_time": arrival,
                "window_start": _r(st["window_start"]),
                "window_end": _r(st["window_end"]),
                "wait_hours": _r(start - arrival),
                "unload_start": _r(start),
                "unload_hours": _r(st["unload_hours"]),
                "unload_finish": _r(start + st["unload_hours"]),
            }
        )
        t = start + st["unload_hours"]
        prev = idx
    return rows


def schedule_delivery(payload: dict[str, Any]) -> dict[str, Any]:
    """送达站点排程入口;输入不合法或无可行站序时抛 ScheduleError。"""
    result = _solve_delivery(payload)
    return result["response"]


def _solve_delivery(payload: dict[str, Any]) -> dict[str, Any]:
    """排程核心求解:重新规划核对快照、穷举最优站序。

    返回内部结构(供排程响应与运费分摊复用):
    ``plan_input`` / ``stations`` / ``travel`` / ``assignments``(规范化后的
    排程输入)、``batch_meta``(每批 name/件数/单件体积)、``optimal_order``
    (0 起下标)、``stops``(最优站序逐站时刻+送达明细)及排程响应 ``response``。
    """
    plan_input = payload.get("plan_input")
    if not isinstance(plan_input, dict):
        raise ScheduleError("缺少生成方案时的输入快照 plan_input")

    stations, travel, assignments, assign_hint = _parse_delivery_input(payload)

    # 按规划输入快照重新规划:箱号/批次/件数一律以重新规划结果为准
    baseline = plan(plan_input)
    boxes = baseline["boxes"]
    summary = baseline["summary"]
    cargo_rows = plan_input.get("cargoes") or []
    batch_count = len(cargo_rows)
    total_hours = float(summary["transport_hours"])

    if not boxes or not summary.get("all_loaded") or not summary.get(
        "all_transportable"
    ):
        raise ScheduleError(
            "当前规划输入不存在全员可运输的装箱方案(有未装载或不可运输货物),"
            "请先生成全员可运输方案后再进行送达排程"
        )

    # 归属覆盖核对:全部批次恰好归属一站(以快照中的批次数为准)
    if assign_hint and assign_hint != batch_count:
        raise ScheduleError(
            f"批次归属明细共 {assign_hint} 行,但规划快照中共有 {batch_count} 批货物;"
            "全部批次须恰好归属一站,请按快照重新核对"
        )
    missing = [b for b in range(1, batch_count + 1) if b not in assignments]
    extra = [b for b in assignments if not (1 <= b <= batch_count)]
    if missing:
        raise ScheduleError(
            "以下批次未指定送达站点(全部批次须恰好归属一站):"
            + "、".join(map(str, missing))
        )
    if extra:
        raise ScheduleError(
            "批次归属中出现了快照不存在的批号:"
            + "、".join(map(str, sorted(extra)))
        )

    # 箱号/批次/件数核对:按重新规划结果汇总每站应送达的明细(按箱号、批号排序)
    batch_names = {i + 1: str(c.get("name") or f"批次 {i + 1}") for i, c in enumerate(cargo_rows)}
    station_items: dict[int, list[dict[str, Any]]] = {i: [] for i in range(len(stations))}
    station_boxes: dict[int, list[int]] = {i: [] for i in range(len(stations))}
    station_units: dict[int, int] = {i: 0 for i in range(len(stations))}
    for box in boxes:
        box_no = int(box["box_no"])
        merged: dict[int, int] = {}
        for it in box["items"]:
            bno = int(it["batch_no"])
            merged[bno] = merged.get(bno, 0) + int(it["units"])
        for bno, units in sorted(merged.items()):
            sidx = assignments[bno]
            station_items[sidx].append(
                {
                    "box_no": box_no,
                    "batch_no": bno,
                    "name": batch_names.get(bno, f"批次 {bno}"),
                    "units": units,
                }
            )
            if box_no not in station_boxes[sidx]:
                station_boxes[sidx].append(box_no)
            station_units[sidx] += units
    for sidx, items in station_items.items():
        items.sort(key=lambda x: (x["box_no"], x["batch_no"]))

    # 穷举全部站序:最早末站卸毕优先,并列取站序号字典序最小
    n = len(stations)
    best_finish: float | None = None
    best_order: tuple[int, ...] | None = None
    evaluated = 0
    feasible_count = 0
    for order in itertools.permutations(range(n)):
        evaluated += 1
        finish = _evaluate_order(list(order), stations, travel, total_hours)
        if finish is None:
            continue
        feasible_count += 1
        if best_finish is None:
            best_finish, best_order = finish, order
        elif finish < best_finish - EPS:
            best_finish, best_order = finish, order
        elif abs(finish - best_finish) <= EPS and order < best_order:
            # 最终卸毕并列:取按输入站点序号组成的站序字典序最小者
            best_order = order

    if best_order is None:
        raise ScheduleError(
            f"不存在可行的站点送达顺序:已穷举 {evaluated} 种站序,"
            "均无法同时满足各站签收时间窗(抵站过早须等待、卸货须在窗口结束前完成)"
            f"且最后一站卸毕不晚于方案运输总时长 {_r(total_hours)}h,"
            "不返回部分路线,请放宽时间窗、调整卸货耗时或行车时长后重试"
        )

    best_rows = _build_stops(list(best_order), stations, travel)
    final_finish = best_rows[-1]["unload_finish"]

    stops: list[dict[str, Any]] = []
    for row in best_rows:
        sidx = row["station_idx"]
        stops.append(
            {
                **{k: v for k, v in row.items() if k != "station_idx"},
                "box_nos": station_boxes[sidx],
                "total_units": station_units[sidx],
                "items": station_items[sidx],
            }
        )

    order_nos = [stations[i]["index"] for i in best_order]
    order_text = " → ".join(
        f"{no}({stations[no - 1]['name']})" for no in order_nos
    )
    delivery_fp_payload = _fingerprint_payload(stations, travel, assignments)
    verdict = (
        f"最优站序 {order_text},共搜索 {evaluated} 种站序、"
        f"{feasible_count} 种可行;最后一站于第 {_r(final_finish)}h 卸毕"
        f"(方案运输总时长 {_r(total_hours)}h,余量 "
        f"{_r(total_hours - final_finish)}h)"
    )
    response = {
        "input_fingerprint": input_fingerprint(
            {"plan_input": plan_input, "delivery": delivery_fp_payload}
        ),
        "plan_fingerprint": input_fingerprint(plan_input),
        "forbidden_mode": bool(summary.get("forbidden_mode")),
        "transport_total_hours": _r(total_hours),
        "station_count": n,
        "orders_evaluated": evaluated,
        "feasible_order_count": feasible_count,
        "optimal_order": order_nos,
        "final_finish": final_finish,
        "time_margin": _r(total_hours - final_finish),
        "stops": stops,
        "stations": [
            {
                "station_no": st["index"],
                "name": st["name"],
                "window_start": _r(st["window_start"]),
                "window_end": _r(st["window_end"]),
                "unload_hours": _r(st["unload_hours"]),
                "from_warehouse": _r(st["from_warehouse"]),
                "box_nos": station_boxes[i],
                "total_units": station_units[i],
                "items": station_items[i],
            }
            for i, st in enumerate(stations)
        ],
        "feasible": True,
        "verdict": verdict,
    }
    # 每批快照信息(运费分摊按「剩余件数 × 单件体积」加权时复用)
    batch_meta: dict[int, dict[str, Any]] = {}
    for i, c in enumerate(cargo_rows):
        bno = i + 1
        batch_meta[bno] = {
            "batch_no": bno,
            "name": batch_names.get(bno, f"批次 {bno}"),
            "units": int(c.get("units")),
            "unit_volume": float(c.get("unit_volume")),
            "station_idx": assignments[bno],
        }
    return {
        "plan_input": plan_input,
        "stations": stations,
        "travel": travel,
        "assignments": assignments,
        "delivery_fp_payload": delivery_fp_payload,
        "batch_meta": batch_meta,
        "optimal_order": list(best_order),
        "stops": stops,
        "response": response,
    }


def _fingerprint_payload(
    stations: list[dict[str, Any]],
    travel: list[list[float]],
    assignments: dict[int, int],
) -> dict[str, Any]:
    """排程输入(不含 plan_input)的规范化结构,用于快照指纹。"""
    return {
        "stations": [
            {
                "name": st["name"],
                "window_start": st["window_start"],
                "window_end": st["window_end"],
                "unload_hours": st["unload_hours"],
                "from_warehouse": st["from_warehouse"],
            }
            for st in stations
        ],
        "travel": travel,
        "assignments": {str(b): s + 1 for b, s in sorted(assignments.items())},
    }
