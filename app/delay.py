"""运输延误推演。

以生成方案时的输入快照(plan_input,与 /api/plan 请求体相同)重新规划得到
基准方案,在 **箱号、批次与各批件数完全不变** 的前提下,把指定运输段的时长
增加 extra_hours(小时,非负),重新计算:

- 延误后的总运输时长(仅该段时长增加,其余段与温区、锁温组均不变);
- 每个箱子 **逐段设定点**:沿用规划器规则按决策单元(相邻同锁温组的连续段
  共用一个设定点)重新贪心选点——单元候选取共享可行温度段端点与落在其中的
  首选区间端点,结合单元开始前各批累计度时,选取使单元结束后箱内最大预算
  使用率最低的点,平局取较低温度;无预算批次时取最宽可行区间中点;
- 箱内各批 **累计度时**(℃·h)与首次超限段。

逐箱对比原值与延误后值,汇总两类受影响的已装箱批次(按 箱×批 展开,
同批分拆到多箱时分别列出各箱件数):

- ``max_transport_hours``:延误后总时长超过该批「最长运输时长」;
- ``degree_hours``:延误后累计度时超过该批预算,并标出 **度时首次超限段**。

没有任何受影响批次时 ``can_continue`` 为 true(可继续运输)。推演不重新
装箱:未装载批次不在运输途中,不参与判定。响应携带输入快照指纹
(SHA-256),输入变化后前端据此将旧推演标记为已过期。
"""
from __future__ import annotations

import copy
import math
from typing import Any

from .planner import (
    EPS,
    _Batch,
    _Iv,
    _Segment,
    _Zone,
    _lock_units,
    _r,
    _simulate_heat,
    _usage_ratio,
    input_fingerprint,
    plan,
)
# 受影响原因标签(同时作为响应中的稳定标识)
REASON_MAX_HOURS = "max_transport_hours"
REASON_DEGREE_HOURS = "degree_hours"


def _build_segments(seg_rows: list[dict[str, Any]]) -> list[_Segment]:
    """从 plan() 已规范化输出的 segments 重建规划器内部运输段对象。"""
    segments: list[_Segment] = []
    for row in seg_rows:
        segments.append(
            _Segment(
                no=int(row["segment_no"]),
                name=str(row["name"]),
                hours=float(row["hours"]),
                zones=[
                    _Zone(
                        name=str(z["name"]),
                        iv=_Iv(float(z["t_min"]), float(z["t_max"])),
                    )
                    for z in row["zones"]
                ],
                lock_group=row.get("lock_group"),
            )
        )
    return segments


def _build_batch(no: int, c: dict[str, Any]) -> _Batch:
    """从快照中的货物条目重建规划器内部批次对象(校验已由 Pydantic 完成)。"""
    lo, hi = float(c["t_min"]), float(c["t_max"])
    p_lo_raw, p_hi_raw = c.get("preferred_t_min"), c.get("preferred_t_max")
    pref: _Iv | None = None
    if p_lo_raw not in (None, "") and p_hi_raw not in (None, ""):
        pref = _Iv(float(p_lo_raw), float(p_hi_raw))
    mb = c.get("max_degree_hours")
    budget = None if mb in (None, "") else float(mb)
    mh = c.get("max_transport_hours")
    max_hours = None if mh in (None, "", 0, "0") else float(mh)
    units = int(c["units"])
    unit_volume = float(c["unit_volume"])
    return _Batch(
        no=no,
        name=str(c.get("name") or f"批次 {no}"),
        units=units,
        unit_volume=unit_volume,
        iv=_Iv(lo, hi),
        max_hours=max_hours,
        pref=pref,
        budget=budget,
        total_volume=_r(units * unit_volume),
    )


def _parse_extra_hours(raw: Any) -> float:
    """解析新增时长(小时,非负有限值);非法抛 ValueError。"""
    try:
        extra = float(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"新增时长须为非负数值(小时),实际为 {raw!r},已拒绝"
        ) from None
    if not math.isfinite(extra) or extra < 0:
        raise ValueError(
            f"新增时长须为非负有限值(小时),实际为 {raw!r},已拒绝"
        )
    return extra


def simulate_delay(payload: dict[str, Any]) -> dict[str, Any]:
    """运输延误推演入口;输入不合法时抛 ValueError(与 plan 一致)。"""
    plan_input = payload.get("plan_input") or {}
    raw_seg_no = payload.get("segment_no")
    if raw_seg_no is None:
        raise ValueError("请选择要推演延误的运输段")
    try:
        seg_no = int(raw_seg_no)
    except (TypeError, ValueError):
        raise ValueError(f"运输段序号须为正整数,实际为 {raw_seg_no!r}") from None
    extra = _parse_extra_hours(payload.get("extra_hours"))

    # 推演绑定生成方案时的输入快照:按快照重新规划,以已装箱明细为基准
    baseline = plan(plan_input)
    seg_rows = baseline["segments"]
    if not (1 <= seg_no <= len(seg_rows)):
        raise ValueError(
            f"运输段序号 {seg_no} 无效:当前方案共 {len(seg_rows)} 个运输段,"
            f"请重新生成方案后选择有效段号"
        )
    if not baseline["boxes"]:
        raise ValueError("当前方案没有任何已装箱货物,无法进行运输延误推演")

    # 禁配装箱模式:规划失败(如禁配无解)已在 plan() 处抛错;这里沿用模式与
    # 禁配快照,确保延误推演与生成方案时的装箱约束完全一致(不重新装箱)。
    forbidden_mode = bool(baseline["summary"].get("forbidden_mode"))
    forbidden_pairs = baseline.get("forbidden_pairs") or []

    base_segments = _build_segments(seg_rows)
    delayed_segments = copy.deepcopy(base_segments)
    delayed_segments[seg_no - 1].hours = _r(
        delayed_segments[seg_no - 1].hours + extra
    )
    total_original = _r(sum(s.hours for s in base_segments))
    total_delayed = _r(total_original + extra)

    cargo_rows = plan_input.get("cargoes") or []
    batches_by_no = {
        i + 1: _build_batch(i + 1, c) for i, c in enumerate(cargo_rows)
    }

    # 段下标 -> (锁温组名, 组内段号列表),延误段所属锁温组的总时长随之增加
    lock_members: dict[int, tuple[str, list[int]]] = {}
    for group, idxs in _lock_units(delayed_segments):
        if group is None:
            continue
        nos = [delayed_segments[i].no for i in idxs]
        for i in idxs:
            lock_members[i] = (group, nos)

    out_boxes: list[dict[str, Any]] = []
    affected: list[dict[str, Any]] = []
    affected_boxes: set[int] = set()
    affected_batches: set[int] = set()
    max_hours_violations = 0
    degree_hours_violations = 0
    seg_names = {s.no: s.name for s in delayed_segments}

    for box in baseline["boxes"]:
        box_no = int(box["box_no"])
        # 箱内批次(按批号去重,保持装载明细顺序)与各批在本箱的件数
        units_by_no: dict[int, int] = {}
        for it in box["items"]:
            no = int(it["batch_no"])
            units_by_no[no] = units_by_no.get(no, 0) + int(it["units"])
        lot_nos = list(units_by_no)
        lots = [batches_by_no[no] for no in lot_nos]

        # 箱内货物允许温区交集(与规划完成时的 box.feasible 一致)
        feasible: _Iv | None = None
        for lot in lots:
            feasible = lot.iv if feasible is None else feasible.intersect(lot.iv)
        assert feasible is not None

        # 原值与延误后值都由同一规划器规则计算,保证逐箱对比口径一致
        sim_base = _simulate_heat(base_segments, feasible, lots)
        sim_delay = _simulate_heat(delayed_segments, feasible, lots)

        segment_setpoints: list[dict[str, Any]] = []
        for idx, seg in enumerate(delayed_segments):
            base_seg = base_segments[idx]
            sp_base = sim_base["setpoints"][idx]
            sp_delay = sim_delay["setpoints"][idx]
            lock_info = lock_members.get(idx)
            segment_setpoints.append(
                {
                    "segment_no": seg.no,
                    "name": seg.name,
                    "hours_original": _r(base_seg.hours),
                    "hours_delayed": _r(seg.hours),
                    "is_delayed": seg.no == seg_no,
                    "setpoint_original": (
                        _r(sp_base) if sp_base is not None else None
                    ),
                    "setpoint_delayed": (
                        _r(sp_delay) if sp_delay is not None else None
                    ),
                    "setpoint_changed": (
                        sp_base is not None
                        and sp_delay is not None
                        and abs(sp_base - sp_delay) > EPS
                    ),
                    "lock_group": lock_info[0] if lock_info else None,
                    "lock_group_segments": lock_info[1] if lock_info else None,
                }
            )

        heat_rows: list[dict[str, Any]] = []
        for no in lot_nos:
            lot = batches_by_no[no]
            exp_base = sim_base["exposures"].get(no)
            exp_delay = sim_delay["exposures"].get(no)
            first_no_base = sim_base["first_exceeded"].get(no)
            first_no_delay = sim_delay["first_exceeded"].get(no)

            reasons: list[str] = []
            if (
                lot.max_hours is not None
                and total_delayed > lot.max_hours + EPS
            ):
                reasons.append(REASON_MAX_HOURS)
            if (
                lot.budget is not None
                and first_no_delay is not None
            ):
                reasons.append(REASON_DEGREE_HOURS)

            row = {
                "batch_no": no,
                "name": lot.name,
                "units": units_by_no[no],
                "preferred_t_min": _r(lot.pref.lo) if lot.pref else None,
                "preferred_t_max": _r(lot.pref.hi) if lot.pref else None,
                "max_degree_hours": (
                    _r(lot.budget) if lot.budget is not None else None
                ),
                "max_transport_hours": (
                    _r(lot.max_hours) if lot.max_hours is not None else None
                ),
                "exposure_original": _r(exp_base) if exp_base is not None else None,
                "exposure_delayed": _r(exp_delay) if exp_delay is not None else None,
                "remaining_original": (
                    _r(lot.budget - exp_base)
                    if lot.budget is not None and exp_base is not None
                    else None
                ),
                "remaining_delayed": (
                    _r(lot.budget - exp_delay)
                    if lot.budget is not None and exp_delay is not None
                    else None
                ),
                "usage_ratio_original": (
                    _r(_usage_ratio(exp_base, lot.budget))
                    if lot.budget is not None and exp_base is not None
                    else None
                ),
                "usage_ratio_delayed": (
                    _r(_usage_ratio(exp_delay, lot.budget))
                    if lot.budget is not None and exp_delay is not None
                    else None
                ),
                "exceeded_original": first_no_base is not None,
                "exceeded_delayed": first_no_delay is not None,
                "first_exceeded_segment_no": first_no_delay,
                "first_exceeded_segment_name": (
                    seg_names.get(first_no_delay) if first_no_delay else None
                ),
                "reasons": reasons,
            }
            heat_rows.append(row)

            if reasons:
                affected_boxes.add(box_no)
                affected_batches.add(no)
                if REASON_MAX_HOURS in reasons:
                    max_hours_violations += 1
                if REASON_DEGREE_HOURS in reasons:
                    degree_hours_violations += 1
                affected.append(
                    {
                        "box_no": box_no,
                        "batch_no": no,
                        "name": lot.name,
                        "units": units_by_no[no],
                        "reasons": reasons,
                        "max_transport_hours": (
                            _r(lot.max_hours) if lot.max_hours is not None else None
                        ),
                        "total_hours_original": total_original,
                        "total_hours_delayed": total_delayed,
                        "max_degree_hours": (
                            _r(lot.budget) if lot.budget is not None else None
                        ),
                        "exposure_original": (
                            _r(exp_base) if exp_base is not None else None
                        ),
                        "exposure_delayed": (
                            _r(exp_delay) if exp_delay is not None else None
                        ),
                        "first_exceeded_segment_no": first_no_delay,
                        "first_exceeded_segment_name": (
                            seg_names.get(first_no_delay)
                            if first_no_delay
                            else None
                        ),
                    }
                )

        out_boxes.append(
            {
                "box_no": box_no,
                "items": copy.deepcopy(box["items"]),
                "segment_setpoints": segment_setpoints,
                "heat_exposure": heat_rows,
                "heat_exposure_ok_original": sim_base["ok"],
                "heat_exposure_ok_delayed": sim_delay["ok"],
                "affected_batch_count": sum(
                    1 for r in heat_rows if r["reasons"]
                ),
            }
        )

    can_continue = not affected
    if can_continue:
        verdict = (
            f"第 {seg_no} 段「{seg_rows[seg_no - 1]['name']}」延误 {_r(extra)}h 后"
            f"总时长 {total_delayed}h,没有批次超出最长运输时长或度时预算,"
            f"可继续运输"
        )
    else:
        verdict = (
            f"第 {seg_no} 段「{seg_rows[seg_no - 1]['name']}」延误 {_r(extra)}h 后"
            f"总时长 {total_original}h → {total_delayed}h,共 "
            f"{len(affected_batches)} 个批次"
            f"({sum(a['units'] for a in affected)} 件)超出最长运输时长或度时预算,"
            f"不可继续运输,请重新规划或调整运输方案"
        )

    return {
        "input_fingerprint": input_fingerprint(plan_input),
        "forbidden_mode": forbidden_mode,
        "forbidden_pairs": forbidden_pairs,
        "forbidden_pair_count": len(forbidden_pairs),
        "delay": {
            "segment_no": seg_no,
            "segment_name": str(seg_rows[seg_no - 1]["name"]),
            "extra_hours": _r(extra),
        },
        "total_hours": {
            "original": total_original,
            "delayed": total_delayed,
            "delta": _r(extra),
        },
        "segments": [
            {
                "segment_no": int(row["segment_no"]),
                "name": str(row["name"]),
                "hours_original": _r(base_segments[i].hours),
                "hours_delayed": _r(delayed_segments[i].hours),
                "is_delayed": int(row["segment_no"]) == seg_no,
                "lock_group": row.get("lock_group"),
            }
            for i, row in enumerate(seg_rows)
        ],
        "boxes": out_boxes,
        "affected_batches": affected,
        "summary": {
            "box_count": len(out_boxes),
            "affected_item_count": len(affected),
            "affected_box_count": len(affected_boxes),
            "affected_batch_count": len(affected_batches),
            "affected_units": sum(a["units"] for a in affected),
            "max_transport_hours_violation_count": max_hours_violations,
            "degree_hours_violation_count": degree_hours_violations,
            "can_continue": can_continue,
            "verdict": verdict,
        },
        "can_continue": can_continue,
        "verdict": verdict,
    }
