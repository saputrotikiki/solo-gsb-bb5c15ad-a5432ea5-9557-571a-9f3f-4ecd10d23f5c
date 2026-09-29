"""FastAPI 入口:提供规划 API 与静态前端页面。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db, delay, delivery, freight, handover
from .planner import plan
from .resilience import assess_zone_failures
from .review import review
from .schemas import (
    DelayRequest,
    DeliveryScheduleRequest,
    FreightAllocationRequest,
    HandoverOrderCreate,
    HandoverRecordsSubmit,
    PlanRequest,
    ReviewRequest,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动时初始化 SQLite(建目录 + 建表),交接单与记录随卷持久化
    db.init()
    yield


app = FastAPI(
    title="冷链装箱温控规划器 API",
    description="按温区兼容与容量约束生成冷链分箱方案,支持分段运输逐段温区校验、"
    "累计热暴露(度时)预算与锁温区段(相邻同组段共用设定点);"
    "并提供单温区故障容灾评估:将每个运输段的每个可用温区分别模拟为不可用,"
    "复用完整规则重新规划并输出风险矩阵数据;"
    "以及实测温度复核:上传实测温度 CSV 与可选测温仪器绝对误差(℃),按所选箱子"
    "的装载批次核算确定/可能超界区间与实测度时上下界"
    "(真实温度视为实测值 ± 误差,温度线性插值、边界处切段、距离积分 ÷ 3600);"
    "以及到货交接台账:从当前方案创建交接单冻结各箱各批次应交件数,"
    "多行签收/退回整笔入账(净签收量不得小于 0 或超过应交量),"
    "支持撤销记录(重放校验,越界则拒绝并指出冲突记录),"
    "交接单与记录存入 SQLite,重启后可继续办理;"
    "以及运输延误推演:选择一个运输段并填写新增时长(小时,非负),"
    "以生成方案时的输入快照和已装箱明细为基准,箱号/批次/件数不变,"
    "重新计算延误后总时长、逐段设定点与各批累计度时,逐箱对比原值,"
    "列出超出最长运输时长或度时预算的批次并标出度时首次超限段,"
    "没有受影响批次时明确返回可继续运输;另支持禁配装箱模式(forbidden_mode):"
    "货物行可填不可同箱的其他批号(当前列表序号,逗号分隔),单向填写视为双方禁配,"
    "启用时总件数限 1~10,精确搜索箱数最少、并列时箱号序列字典序最小的全员方案,"
    "无解返回 400 不返回部分方案;容灾评估、延误推演与交接单均沿用模式与禁配快照;"
    "以及送达站点排程:为每个货物批次指定一个送达站点(1~8 个,每批恰好归属一站),"
    "填写各站签收时间窗、卸货耗时及仓库/站点间有向行车时长,按快照重新核对"
    "箱号/批次/件数,穷举全部可行站序(抵站过早等待、卸货须在窗口结束前完成、"
    "末站卸毕不超过方案运输总时长),取最终卸毕最早、并列时站序号字典序最小者,"
    "无可行站序返回 400 不返回部分路线;"
    "以及多站送达分运费分摊:按实际行驶顺序逐程填写非负整数分运费(条数与程数一致),"
    "沿用快照与站点/时间窗/站序规则重新核对可行路线,每程仅由尚未在前站卸下的"
    "批次按剩余件数×单件体积精确比例分摊,不足一分用最大余数法分配(同余数批号小者优先),"
    "逐程及总额严格等于输入费用,费用非法或排程无解返回 400 不返回部分分摊",
    version="1.11.0",
    lifespan=lifespan,
)


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """规划器抛出的业务校验错误统一返回 400,消息直接展示给用户。"""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """请求体校验错误返回 422。

    默认错误体会原样回显输入(如 NaN/Infinity),jsonable_encoder 默认拒绝
    非有限浮点导致二次序列化失败(会被兜底成 400);这里用自定义编码器把
    非有限值转成字符串,保证非法输入(含非负有限数校验)稳定返回 422。
    """
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(exc.errors(), custom_encoder={
            float: lambda x: x if x == x and x not in (float("inf"), float("-inf"))
            else ("NaN" if x != x else ("Infinity" if x > 0 else "-Infinity")),
        })},
    )


@app.exception_handler(handover.HandoverNotFound)
async def handover_not_found_handler(
    request: Request, exc: handover.HandoverNotFound
) -> JSONResponse:
    """交接单/记录不存在返回 404。"""
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(handover.VoidConflictError)
async def void_conflict_handler(
    request: Request, exc: handover.VoidConflictError
) -> JSONResponse:
    """撤销会导致后续记录越界:返回 409 并附冲突记录明细,原台账保持不变。"""
    return JSONResponse(
        status_code=409, content={"detail": str(exc), "conflicts": exc.conflicts}
    )


@app.get("/api/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/plan", tags=["plan"])
async def api_plan(req: PlanRequest) -> dict:
    return plan(req.model_dump())


@app.post("/api/zone-failure-assessment", tags=["assessment"])
async def api_zone_failure_assessment(req: PlanRequest) -> dict:
    """单温区故障容灾评估:请求体与 /api/plan 相同,基于同一输入快照,
    将每个运输段的每个可用温区分别模拟为不可用并重新规划。"""
    return assess_zone_failures(req.model_dump())


@app.post("/api/temperature-review", tags=["review"])
async def api_temperature_review(req: ReviewRequest) -> dict:
    """实测温度复核:请求体携带生成方案时的输入快照(plan_input)、箱号、
    实测温度 CSV(time_seconds,temperature 两列)与可选的测温仪器绝对误差
    instrument_error(℃,非负,默认 0)。按快照重新规划定位箱子,再按该箱
    装载批次分别给出确定超界/可能超界区间,以及有首选区间批次的度时
    下界/上界与预算判定。输入变化后旧复核结果过期,需重新生成方案。"""
    return review(req.model_dump())


@app.post("/api/transport-delay", tags=["delay"])
async def api_transport_delay(req: DelayRequest) -> dict:
    """运输延误推演:请求体携带生成方案时的输入快照(plan_input)、运输段序号
    segment_no 与新增时长 extra_hours(小时,非负)。以快照重新规划得到的
    已装箱明细为基准(箱号、批次与件数不变),按规划器选点与预算判定规则
    重算延误后总时长、逐段设定点与各批累计度时,逐箱对比原值,列出超出
    最长运输时长或度时预算的批次并标出度时首次超限段。输入变化后旧推演
    结果过期,需重新生成方案。"""
    return delay.simulate_delay(req.model_dump())


@app.post("/api/delivery-schedule", tags=["delivery"])
async def api_delivery_schedule(req: DeliveryScheduleRequest) -> dict:
    """送达站点排程:请求体携带生成方案时的输入快照(plan_input)、1~8 个站点
    (签收时间窗、卸货耗时、仓库→站点有向行车时长)、站点间有向行车时长矩阵
    与每批货物的归属站号(全部批次恰好归属一站)。按快照重新规划核对
    箱号/批次/件数,穷举全部可行站序,取最终卸毕时刻最早、并列时按输入站点
    序号组成的站序字典序最小者;无可行站序返回 400,不返回部分路线。
    输入变化后旧排程结果过期,需重新生成方案。"""
    return delivery.schedule_delivery(req.model_dump())


@app.post("/api/freight-allocation", tags=["delivery"])
async def api_freight_allocation(req: FreightAllocationRequest) -> dict:
    """多站送达分运费分摊:请求体与 /api/delivery-schedule 相同,额外携带按
    实际行驶顺序(仓库→首站、相邻两站)逐程填写的非负整数分运费
    freight_fees(条数须与站序程数相等)。服务端沿用规划输入快照与站点
    归属、时间窗和站序规则重新核对可行路线,每程仅由尚未在前站卸下的
    批次按剩余件数×单件体积精确比例分摊,不足一分用最大余数法分配
    (同余数批号小者优先);逐程与总额均严格等于输入费用,否则整单
    拒绝(400)不返回部分分摊。输入变化后旧分摊结果过期。"""
    return freight.allocate_freight(req.model_dump())


# ---------------- 到货交接台账 ----------------


@app.post("/api/handover-orders", tags=["handover"], status_code=201)
async def api_handover_create(req: HandoverOrderCreate) -> dict:
    """从规划输入快照创建交接单:按快照重新规划,冻结各箱各批次的应交件数。
    交接单存入 SQLite,此后修改规划输入、重新生成方案均不影响本单。"""
    return handover.create_order(req.plan_input.model_dump())


@app.get("/api/handover-orders", tags=["handover"])
async def api_handover_list() -> dict:
    """交接单列表(重启后仍在),附应交/净签收/待交汇总。"""
    return {"orders": handover.list_orders()}


@app.get("/api/handover-orders/{order_id}", tags=["handover"])
async def api_handover_detail(order_id: int) -> dict:
    """交接单详情:各箱各批次的应交/净签收/待交件数与全部签收/退回记录。"""
    return handover.get_order(order_id)


@app.post("/api/handover-orders/{order_id}/records", tags=["handover"], status_code=201)
async def api_handover_submit(order_id: int, req: HandoverRecordsSubmit) -> dict:
    """一次提交多行签收/退回记录:按提交顺序逐行累计净签收量,
    任一行使某箱某批净签收量小于 0 或超过应交件数时整笔不入账。"""
    return handover.submit_records(order_id, [r.model_dump() for r in req.records])


@app.post(
    "/api/handover-orders/{order_id}/records/{record_id}/void", tags=["handover"]
)
async def api_handover_void(order_id: int, record_id: int) -> dict:
    """撤销一条未撤销的记录:跳过该记录重放其余有效记录,若任何后续记录
    导致净签收量越界则拒绝撤销(409,附冲突记录),原台账保持不变。"""
    return handover.void_record(order_id, record_id)


# 前端静态资源(挂载在最后,避免覆盖 /api 与 /docs)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
