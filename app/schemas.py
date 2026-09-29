"""Pydantic 请求模型(同时用于 /docs 中的接口文档)。"""
from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ZoneIn(BaseModel):
    name: str = Field(..., description="温区名称,如:冷冻区")
    t_min: float = Field(..., description="温区下限 ℃")
    t_max: float = Field(..., description="温区上限 ℃")


class SegmentIn(BaseModel):
    name: str | None = Field(
        None, description="运输段名称,如:干线运输;留空自动按顺序命名"
    )
    hours: float = Field(..., gt=0, description="该段运输时长(小时)")
    zones: list[ZoneIn] = Field(..., min_length=1, description="该段可用温区列表")
    lock_group: str | None = Field(
        None,
        description="锁温组名称(可选);相邻且同组的运输段必须共用一个设定点,"
        "共享设定点取自各段可行温度段交集的真实区间,并使整组结束时箱内最大"
        "度时预算使用率最低;留空表示该段不参与锁温",
    )


class CargoIn(BaseModel):
    name: str = Field(..., description="货物批次名称")
    units: int = Field(..., gt=0, description="件数(拆分的最小单位)")
    unit_volume: float = Field(..., gt=0, description="单件占用体积")
    t_min: float = Field(..., description="允许温度下限 ℃(硬边界)")
    t_max: float = Field(..., description="允许温度上限 ℃(硬边界)")
    preferred_t_min: float | None = Field(
        None, description="首选温度下限 ℃,需与首选上限同时填写;留空表示无偏好"
    )
    preferred_t_max: float | None = Field(
        None, description="首选温度上限 ℃,需与首选下限同时填写;留空表示无偏好"
    )
    max_degree_hours: float | None = Field(
        None,
        ge=0,
        description="最大累计热暴露(度时)预算 ℃·h,需配合首选温度区间使用;"
        "留空表示不限,该批按原规则规划",
    )
    max_transport_hours: float | None = Field(
        None, ge=0, description="该批货物可承受的最长运输时长(小时),留空表示不限"
    )
    forbidden_with: str | None = Field(
        None,
        description="禁配批号(仅在 forbidden_mode=true 启用禁配装箱模式时生效):"
        "当前列表序号(1 起),逗号分隔,如 2,5;单向填写即视为双方禁配,"
        "不得填写自身序号或不存在的批号;启用模式时全部货物总件数限 1~10",
    )


class PlanRequest(BaseModel):
    transport_hours: float | None = Field(
        None,
        ge=0,
        description="运输时长(小时);分段模式下可省略,默认取各段时长之和,"
        "若填写则必须与各段时长之和一致",
    )
    box_capacity: float = Field(..., gt=0, description="单个箱体容量(体积单位)")
    max_boxes: int = Field(100, gt=0, description="最多使用的箱子数量")
    volume_unit: str = Field("m³", description="体积单位名称,仅用于展示")
    zones: list[ZoneIn] = Field(
        default_factory=list,
        description="全程单一可用温区列表;未提供 segments 时至少需要一个",
    )
    segments: list[SegmentIn] = Field(
        default_factory=list,
        description="按顺序维护的运输段(名称/时长/可用温区);"
        "提供后启用分段温区校验,并忽略 zones",
    )
    cargoes: list[CargoIn] = Field(..., min_length=1, description="货物批次列表")
    forbidden_mode: bool = Field(
        False,
        description="是否启用禁配装箱模式:启用后全部货物总件数限 1~10,"
        "同批允许逐件拆箱,任何箱不得同时装入禁配双方,并须通过现有容量、"
        "逐段温区、锁温组及度时预算校验;在最大箱数内存在全员装载方案时"
        "必须返回箱数最少且(并列时)按输入批次逐件顺序组成的箱号序列"
        "字典序最小的方案;无解时返回 400,不返回部分方案。"
        "未启用(false)时保留原 First-Fit-Decreasing 规划",
    )


class ReviewRequest(BaseModel):
    plan_input: PlanRequest = Field(
        ...,
        description="生成方案时的输入快照(请求体与 /api/plan 相同);"
        "复核基于该快照重新规划并定位箱子,输入变化后需重新生成方案再复核",
    )
    box_no: int = Field(
        ..., gt=0, description="要复核的箱号(对应方案 boxes[].box_no)"
    )
    csv: str = Field(
        ...,
        min_length=1,
        description="实测温度 CSV 文本:首行表头须含 time_seconds,temperature 两列;"
        "时间从 0 严格递增至全程秒数(运输时长 × 3600),至少两条记录,"
        "数值须为有限值,非法记录整单拒绝",
    )
    instrument_error: float = Field(
        0.0,
        ge=0,
        description="测温仪器绝对误差(℃,非负,默认 0):每时刻真实温度视为"
        "实测值 ± 误差,据此分别给出确定超界/可能超界区间与度时下界/上界;"
        "为 0 时与不考虑仪器误差的原复核结果一致",
    )


class DelayRequest(BaseModel):
    plan_input: PlanRequest = Field(
        ...,
        description="生成方案时的输入快照(请求体与 /api/plan 相同);"
        "推演基于该快照重新规划,以已装箱明细(箱号/批次/件数)为基准,"
        "输入变化后需重新生成方案再推演",
    )
    segment_no: int = Field(
        ...,
        gt=0,
        description="要推演延误的运输段序号(对应方案 segments[].segment_no);"
        "单一温区模式下全程视为第 1 段",
    )
    extra_hours: float = Field(
        ...,
        ge=0,
        description="该运输段的新增时长(小时,非负);其余段时长、温区与锁温组不变",
    )


class DeliveryStationIn(BaseModel):
    name: str | None = Field(None, description="站点名称;留空自动按「站点 N」命名")
    window_start: float = Field(
        ..., ge=0, allow_inf_nan=False,
        description="可签收时间窗开始(小时,出库时刻为 0;非负有限值)",
    )
    window_end: float = Field(
        ..., ge=0, allow_inf_nan=False,
        description="可签收时间窗结束(小时,非负有限值;不得早于窗口开始)",
    )
    unload_hours: float = Field(
        ..., ge=0, allow_inf_nan=False, description="本站卸货耗时(小时,非负有限值)"
    )
    from_warehouse: float = Field(
        ..., ge=0, allow_inf_nan=False,
        description="从仓库行驶到本站的有向行车时长(小时,非负有限值)",
    )
    travel_to: list[float] | None = Field(
        None,
        description="可选内嵌方式:从本站到各站(按站点输入顺序,含本站占位)的"
        "有向行车时长;也可统一使用请求体顶层 travel 矩阵",
    )


class DeliveryAssignmentIn(BaseModel):
    batch_no: int | None = Field(
        None,
        description="批号(按货物输入顺序,1 起);明细按批次顺序给出时可省略,"
        "省略时取行序号",
    )
    station_no: int = Field(
        ...,
        gt=0,
        description="该批次送达的站点输入序号(1 起);全部批次须恰好归属一站",
    )


class DeliveryScheduleRequest(BaseModel):
    plan_input: PlanRequest = Field(
        ...,
        description="生成方案时的输入快照(请求体与 /api/plan 相同);"
        "服务端按快照重新规划核对箱号/批次/件数,仅接受全员可运输方案,"
        "输入变化后旧排程结果过期",
    )
    stations: list[DeliveryStationIn] = Field(
        ...,
        min_length=1,
        max_length=8,
        description="送达站点(1~8 个):名称、可签收时间窗、卸货耗时与"
        "仓库→本站有向行车时长;时间窗、耗时均为非负有限数,结束不早于开始",
    )
    travel: list[list[float]] | None = Field(
        None,
        description="站点间有向行车时长矩阵(小时),n×n,行=上一站、列=下一站,"
        "有向不要求对称;对角线(自己→自己)不使用;省略时取各站 travel_to;"
        "元素均须为非负有限数(形状与非负由业务层校验,非法返回 400)",
    )
    assignments: list = Field(
        ...,
        min_length=1,
        description="批次归属(全部批次恰好归属一站):紧凑形式为按批次输入顺序的"
        "站号整数列表(如 [2,1,2]);明细形式为 "
        "[{batch_no:1, station_no:2}, ...]",
    )

    @field_validator("travel")
    @classmethod
    def _travel_finite(cls, v):
        if v is None:
            return v
        for i, row in enumerate(v):
            for j, x in enumerate(row):
                if isinstance(x, bool):
                    raise ValueError(
                        f"行车时长矩阵第 {i + 1} 行第 {j + 1} 列须为数值"
                    )
                try:
                    fx = float(x)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"行车时长矩阵第 {i + 1} 行第 {j + 1} 列须为数值"
                    ) from None
                if not math.isfinite(fx):
                    raise ValueError(
                        f"站点 {i + 1} → 站点 {j + 1} 的行车时长须为有限数值"
                    )
                if fx < 0:
                    raise ValueError(
                        f"站点 {i + 1} → 站点 {j + 1} 的行车时长不能为负数"
                    )
        return v


class HandoverOrderCreate(BaseModel):
    plan_input: PlanRequest = Field(
        ...,
        description="创建交接单时的规划输入快照(请求体与 /api/plan 相同);"
        "服务端按快照重新规划,把各箱各批次的已装件数冻结为应交件数,"
        "此后修改规划输入不影响已创建的交接单",
    )


class HandoverRecordIn(BaseModel):
    kind: Literal["receive", "return"] = Field(
        ..., description="记录类型:receive=签收(净签收量增加),return=退回(减少)"
    )
    box_no: int = Field(..., gt=0, description="箱号(对应交接单 items[].box_no)")
    batch_no: int = Field(..., gt=0, description="批号(对应交接单 items[].batch_no)")
    units: int = Field(..., gt=0, description="件数(正整数)")


class HandoverRecordsSubmit(BaseModel):
    records: list[HandoverRecordIn] = Field(
        ...,
        min_length=1,
        description="一次提交多行签收/退回记录:按提交顺序逐行累计净签收量,"
        "任一行使某箱某批净签收量小于 0 或超过应交件数时,整笔不入账",
    )
