"""多站送达分运费分摊 API 测试(stdlib unittest,无需额外数据库)。

运行:python tests/test_freight.py
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:  # 未安装 Web 依赖时仍可运行核心规则用例,API 用例自动跳过
    from fastapi.testclient import TestClient  # noqa: E402
    from app.main import app  # noqa: E402

    _APP_AVAILABLE = True
except ImportError:  # pragma: no cover - 仅极简环境触发
    TestClient = None  # type: ignore[assignment,misc]
    app = None  # type: ignore[assignment]
    _APP_AVAILABLE = False

from app.freight import FreightError, allocate_freight  # noqa: E402

# 两批货物(鲜奶 40 件×0.25=10 体积权重;三文鱼 10 件×0.4=4),总时长 12h
PLAN = {
    "transport_hours": 12,
    "box_capacity": 100,
    "zones": [{"name": "冷藏区", "t_min": 0, "t_max": 8}],
    "cargoes": [
        {"name": "鲜奶", "units": 40, "unit_volume": 0.25, "t_min": 2, "t_max": 6},
        {"name": "三文鱼", "units": 10, "unit_volume": 0.4, "t_min": -2, "t_max": 2},
    ],
}


def st(name, s=0, e=20, u=0, w=0):
    return {"name": name, "window_start": s, "window_end": e,
            "unload_hours": u, "from_warehouse": w}


def body(fees, stations, travel, assignments=(1, 2), plan=PLAN):
    return {
        "plan_input": plan,
        "stations": stations,
        "travel": travel,
        "assignments": list(assignments),
        "freight_fees": list(fees),
    }


def basic(fees=(100, 33), assignments=(1, 2)):
    return body(
        fees,
        [st("东城站", 2, 8, 1, 2), st("西城站", 3, 12, 1, 3)],
        [[0, 2], [2, 0]],
        assignments,
    )


class FreightDirectTestCase(unittest.TestCase):
    """不依赖 HTTP 层的核心规则测试。"""

    def test_proportional_shares_and_remainder(self):
        # 程1:权重 10:4,100 分 → 鲜奶 100*5/7=71.43 → 71;三文鱼 28.57 → 29
        r = allocate_freight(basic((100, 33)))
        self.assertEqual(r["optimal_order"], [1, 2])
        leg1, leg2 = r["legs"]
        self.assertEqual(leg1["from"], "仓库")
        self.assertEqual(leg1["to"], "站点 1")
        self.assertEqual(
            [(a["batch_no"], a["amount_cents"]) for a in leg1["allocations"]],
            [(1, 71), (2, 29)],
        )
        self.assertEqual(leg1["allocated_total_cents"], 100)
        # 程2:鲜奶已在站1卸下,仅三文鱼在车,全额 33 分
        self.assertEqual(leg2["from_station_no"], 1)
        self.assertEqual(
            [(a["batch_no"], a["amount_cents"]) for a in leg2["allocations"]],
            [(2, 33)],
        )
        self.assertEqual(leg2["allocated_total_cents"], 33)
        # 各批合计与总额
        self.assertEqual(
            [(b["batch_no"], b["total_cents"]) for b in r["batch_totals"]],
            [(1, 71), (2, 62)],
        )
        self.assertEqual(r["total_freight_cents"], 133)
        self.assertEqual(r["batch_total_sum_cents"], 133)

    def test_exact_ratio_fields(self):
        r = allocate_freight(basic((7, 0)))
        a = r["legs"][0]["allocations"]
        # 精确比例 5/7、2/7;7 分 → 5、2,无尾差
        self.assertEqual(a[0]["share_ratio"], "5/7")
        self.assertEqual(a[1]["share_ratio"], "2/7")
        self.assertEqual(a[0]["exact_share"], "5/1")
        self.assertEqual(a[1]["exact_share"], "2/1")
        self.assertEqual([x["amount_cents"] for x in a], [5, 2])
        self.assertEqual(a[0]["weight"], "10/1")
        self.assertEqual(a[0]["remaining_units"], 40)
        self.assertEqual(a[1]["remaining_units"], 10)

    def test_tie_remainder_smaller_batch_no_first(self):
        # 三批同权重(各 1 件×体积 1),10 分:精确 10/3 余数相等,尾差归批号 1
        p = body(
            (10, 0),
            [st("S1"), st("S2")],
            [[0, 0], [0, 0]],
            assignments=(1, 2, 2),
            plan={
                "transport_hours": 12, "box_capacity": 100,
                "zones": [{"name": "z", "t_min": 0, "t_max": 8}],
                "cargoes": [
                    {"name": "A", "units": 1, "unit_volume": 1, "t_min": 0, "t_max": 8},
                    {"name": "B", "units": 1, "unit_volume": 1, "t_min": 0, "t_max": 8},
                    {"name": "C", "units": 1, "unit_volume": 1, "t_min": 0, "t_max": 8},
                ],
            },
        )
        r = allocate_freight(p)
        self.assertEqual(
            [a["amount_cents"] for a in r["legs"][0]["allocations"]],
            [4, 3, 3],
        )
        # 程2:B、C 在车,0 分
        self.assertEqual(
            [(a["batch_no"], a["amount_cents"])
             for a in r["legs"][1]["allocations"]],
            [(2, 0), (3, 0)],
        )

    def test_zero_fees(self):
        r = allocate_freight(basic((0, 0)))
        self.assertEqual(r["total_freight_cents"], 0)
        for leg in r["legs"]:
            self.assertEqual(leg["allocated_total_cents"], 0)
            self.assertTrue(all(a["amount_cents"] == 0 for a in leg["allocations"]))
        self.assertTrue(all(b["total_cents"] == 0 for b in r["batch_totals"]))

    def test_single_station_single_leg(self):
        p = body(
            (200,),
            [st("唯一站", 0, 20, 0, 0)],
            [[0]],
            assignments=(1, 1),
        )
        r = allocate_freight(p)
        self.assertEqual(r["leg_count"], 1)
        self.assertEqual(r["legs"][0]["from"], "仓库")
        self.assertEqual(r["legs"][0]["to"], "站点 1")
        # 权重 10:4,200 = 1400/7 整除 → 143? 200*5/7=142.857
        self.assertEqual(
            [(a["batch_no"], a["amount_cents"])
             for a in r["legs"][0]["allocations"]],
            [(1, 143), (2, 57)],
        )
        self.assertEqual(r["total_freight_cents"], 200)

    def test_unloaded_at_first_station_absent_from_next_legs(self):
        # 鲜奶归站1、三文鱼归站2:第 2 程在车批次只有三文鱼
        r = allocate_freight(basic((500, 500)))
        self.assertEqual(
            {a["batch_no"] for a in r["legs"][1]["allocations"]}, {2}
        )
        # 三文鱼全程在车(2 程),鲜奶仅第 1 程
        t1, t2 = {b["batch_no"]: b["total_cents"] for b in r["batch_totals"]}[1], \
                 {b["batch_no"]: b["total_cents"] for b in r["batch_totals"]}[2]
        self.assertEqual(t1 + t2, 1000)

    def test_empty_leg_zero_fee_allowed_positive_rejected(self):
        # 仅 1 批归属站1;站2 无任何批次,第 2 程空车
        p = basic((100, 0), assignments=(1, 1))
        r = allocate_freight(p)
        self.assertEqual(r["legs"][1]["allocations"], [])
        self.assertEqual(r["legs"][1]["allocated_total_cents"], 0)
        self.assertEqual(r["total_freight_cents"], 100)
        p2 = basic((100, 5), assignments=(1, 1))
        with self.assertRaises(FreightError) as cm:
            allocate_freight(p2)
        self.assertIn("车上已无任何批次", str(cm.exception))

    # ---- 拒绝:费用条数/非法 ----

    def _expect_error(self, payload, needle):
        with self.assertRaises((FreightError, ValueError)) as cm:
            allocate_freight(payload)
        self.assertIn(needle, str(cm.exception))

    def test_fee_count_mismatch(self):
        self._expect_error(basic((100,)), "运费条数 1 与站序程数 2 不符")
        self._expect_error(basic((100, 0, 0)), "运费条数 3 与站序程数 2 不符")
        self._expect_error(basic(()), "运费条数 0 与站序程数 2 不符")
        self._expect_error({**basic((100, 0)), "freight_fees": None},
                           "须为 2 个非负整数")

    def test_illegal_fee_values(self):
        self._expect_error(basic((100, 1.0)), "非负整数")
        self._expect_error(basic((True, 0)), "非负整数")
        self._expect_error(basic((-1, 0)), "不能为负数")
        self._expect_error(basic((100, "0")), "非负整数")
        self._expect_error({**basic((100, 0)), "freight_fees": "100,0"},
                           "非负整数")
        self._expect_error({**basic((100, 0)), "freight_fees": None},
                           "非负整数")

    # ---- 拒绝:排程规则沿用,无解不返回部分分摊 ----

    def test_infeasible_schedule_rejected_no_partial(self):
        p = body(
            (100, 0),
            [st("S1", 0, 0.5, 1, 0), st("S2", 0, 20, 1, 0)],
            [[0, 1], [1, 0]],
        )
        with self.assertRaises(ValueError) as cm:
            allocate_freight(p)
        self.assertIn("不存在可行的站点送达顺序", str(cm.exception))

    def test_non_all_loaded_plan_rejected(self):
        p = body(
            (100, 0),
            [st("S"), st("S2")],
            [[0, 0], [0, 0]],
            plan={**PLAN, "box_capacity": 1.0, "max_boxes": 1},
        )
        self._expect_error(p, "全员可运输")

    def test_invalid_station_input_rejected(self):
        p = body(
            (100, 0),
            [st("S", 5, 4, 1, 1), st("S2", 0, 20, 1, 1)],
            [[0, 1], [1, 0]],
        )
        self._expect_error(p, "窗口结束")

    def test_no_partial_allocation_in_response(self):
        # 任何拒绝路径都不产生分摊结果
        try:
            allocate_freight(basic((100,)))
        except ValueError:
            pass
        else:  # pragma: no cover
            self.fail("应当拒绝")

    # ---- 指纹与过期 ----

    def test_fingerprints_change_with_fees_and_inputs(self):
        a = allocate_freight(basic((100, 0)))
        b = allocate_freight(basic((101, 0)))
        c = allocate_freight(body(
            (100, 0),
            [st("东城站", 2, 9, 1, 2), st("西城站", 3, 12, 1, 3)],
            [[0, 2], [2, 0]],
        ))
        self.assertNotEqual(a["input_fingerprint"], b["input_fingerprint"])
        self.assertEqual(a["plan_fingerprint"], b["plan_fingerprint"])
        self.assertEqual(a["schedule_fingerprint"], b["schedule_fingerprint"])
        # 仅排程输入变化:plan 指纹不变,合并指纹变化
        self.assertEqual(a["plan_fingerprint"], c["plan_fingerprint"])
        self.assertNotEqual(a["input_fingerprint"], c["input_fingerprint"])
        self.assertNotEqual(a["schedule_fingerprint"], c["schedule_fingerprint"])

    def test_response_contains_schedule_recheck(self):
        r = allocate_freight(basic())
        # 排程结果同样经过快照核对:逐站明细可查
        self.assertEqual(len(r["stops"]), 2)
        self.assertEqual(r["stops"][0]["items"][0]["units"], 40)
        self.assertEqual(r["orders_evaluated"], 2)
        self.assertTrue(r["feasible"])


@unittest.skipUnless(_APP_AVAILABLE, "未安装 FastAPI,跳过 HTTP 层用例")
class FreightApiTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_api_ok(self):
        resp = self.client.post("/api/freight-allocation", json=basic())
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["total_freight_cents"], 133)
        self.assertEqual(data["legs"][0]["allocated_total_cents"], 100)
        self.assertEqual(data["batch_total_sum_cents"], 133)

    def test_api_count_mismatch_400(self):
        resp = self.client.post("/api/freight-allocation", json=basic((100,)))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("运费条数", resp.json()["detail"])

    def test_api_illegal_fee_400(self):
        for bad in ((-1, 0), (1.5, 0), (True, 0)):
            resp = self.client.post(
                "/api/freight-allocation", json=basic(bad)
            )
            self.assertEqual(resp.status_code, 400, bad)

    def test_api_infeasible_400(self):
        p = body(
            (100, 0),
            [st("S1", 0, 0.5, 1, 0), st("S2", 0, 20, 1, 0)],
            [[0, 1], [1, 0]],
        )
        resp = self.client.post("/api/freight-allocation", json=p)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不存在可行的站点送达顺序", resp.json()["detail"])
        self.assertNotIn("legs", resp.json())


if __name__ == "__main__":
    unittest.main(verbosity=2)
