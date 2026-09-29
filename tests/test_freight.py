"""多站送达分运费分摊测试(stdlib unittest,无需额外数据库)。

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

from app.delivery import ScheduleError  # noqa: E402
from app.freight import allocate_freight  # noqa: E402

# 两批货物、单一温区、运输总时长 12h;批1 40 件×0.25=10,批2 10 件×0.4=4
PLAN = {
    "transport_hours": 12,
    "box_capacity": 100,
    "zones": [{"name": "冷藏区", "t_min": 0, "t_max": 8}],
    "cargoes": [
        {"name": "鲜奶", "units": 40, "unit_volume": 0.25, "t_min": 2, "t_max": 6},
        {"name": "三文鱼", "units": 10, "unit_volume": 0.4, "t_min": -2, "t_max": 2},
    ],
}


def st(name, s=0, e=24, u=1, w=0):
    return {
        "name": name,
        "window_start": s,
        "window_end": e,
        "unload_hours": u,
        "from_warehouse": w,
    }


def body(stations, travel, assignments, fees, plan=PLAN):
    return {
        "plan_input": plan,
        "stations": stations,
        "travel": travel,
        "assignments": assignments,
        "fees": fees,
    }


def alloc_by_batch(leg):
    return {o["batch_no"]: o["allocated_cents"] for o in leg["onboard"]}


class FreightDirectTestCase(unittest.TestCase):
    """不依赖 HTTP 层的核心规则测试。"""

    def test_basic_proportion_and_largest_remainder(self):
        # 程1 在车两批权重 10:4,费 1000 分 → 714.2857... / 285.7142...,
        # 整数 714/285,余 1 分按余数补给批 2 → 714/286
        # 程2 仅批 2 在车,400 分全部归批 2
        r = allocate_freight(
            body(
                [st("东城站", 2, 8, w=2), st("西城站", 3, 12, w=3)],
                [[0, 2], [2, 0]],
                [1, 2],
                [1000, 400],
            )
        )
        self.assertEqual(r["optimal_order"], [1, 2])
        leg1, leg2 = r["legs"]
        self.assertEqual(alloc_by_batch(leg1), {1: 714, 2: 286})
        self.assertEqual(alloc_by_batch(leg2), {2: 400})
        # 每程合计严格等于输入费用
        self.assertEqual(leg1["allocated_total_cents"], 1000)
        self.assertEqual(leg2["allocated_total_cents"], 400)
        # 各批合计与总额
        totals = {b["batch_no"]: b["total_allocated_cents"]
                  for b in r["batch_totals"]}
        self.assertEqual(totals, {1: 714, 2: 686})
        self.assertEqual(r["total_fee_cents"], 1400)
        self.assertEqual(r["allocated_total_cents"], sum(r["fees_cents"]))
        self.assertTrue(r["balanced"])
        # 精确份额以分子/分母给出
        sh1 = next(o for o in leg1["onboard"] if o["batch_no"] == 1)
        self.assertEqual(
            (sh1["exact_share_numerator"], sh1["exact_share_denominator"]),
            (5000, 7),
        )
        self.assertEqual(sh1["remaining_units"], 40)
        self.assertEqual(sh1["weight"], 10.0)

    def test_each_leg_and_grand_total_strictly_equal_for_many_fees(self):
        # 三批等权重 6,各归一站;多种费用下逐程、总额恒等
        plan = {
            "transport_hours": 24,
            "box_capacity": 100,
            "zones": [{"name": "z", "t_min": 0, "t_max": 8}],
            "cargoes": [
                {"name": n, "units": 6, "unit_volume": 1, "t_min": 0, "t_max": 8}
                for n in ("A", "B", "C")
            ],
        }
        stations = [st("S1", u=0), st("S2", u=0), st("S3", u=0)]
        travel = [[0] * 3 for _ in range(3)]
        for fees in ([0, 0, 0], [1, 1, 1], [7, 11, 13], [100, 0, 5],
                     [999999, 2, 1]):
            r = allocate_freight(body(stations, travel, [1, 2, 3], fees, plan=plan))
            for leg, fee in zip(r["legs"], fees):
                self.assertEqual(
                    leg["allocated_total_cents"], fee,
                    f"程合计不等:fees={fees}, seq={leg['seq']}",
                )
                self.assertEqual(
                    sum(o["allocated_cents"] for o in leg["onboard"]), fee
                )
            self.assertEqual(r["total_fee_cents"], sum(fees))
            self.assertEqual(
                sum(b["total_allocated_cents"] for b in r["batch_totals"]),
                sum(fees),
            )

    def test_tie_same_remainder_smaller_batch_first(self):
        # 三批等权重,费 10 分:各 10/3=3 余 1/3,余 1 分归批号最小者
        plan = {
            "transport_hours": 24,
            "box_capacity": 100,
            "zones": [{"name": "z", "t_min": 0, "t_max": 8}],
            "cargoes": [
                {"name": n, "units": 6, "unit_volume": 1, "t_min": 0, "t_max": 8}
                for n in ("A", "B", "C")
            ],
        }
        r = allocate_freight(
            body([st("S", u=0)], [[0]], [1, 1, 1], [10], plan=plan)
        )
        self.assertEqual(alloc_by_batch(r["legs"][0]), {1: 4, 2: 3, 3: 3})

    def test_decimal_weights_equal_across_expressions(self):
        # 批1: 4 件×0.1=0.4;批2: 1 件×0.4=0.4 —— 十进制下权重相等,
        # 费 3 分:各 1.5,余 2 分同余数,批号 1、2 各补 1 → 均 1.5→ 1+1=2 分起
        plan = {
            "transport_hours": 24,
            "box_capacity": 100,
            "zones": [{"name": "z", "t_min": 0, "t_max": 8}],
            "cargoes": [
                {"name": "A", "units": 4, "unit_volume": 0.1, "t_min": 0, "t_max": 8},
                {"name": "B", "units": 1, "unit_volume": 0.4, "t_min": 0, "t_max": 8},
            ],
        }
        r = allocate_freight(
            body([st("S", u=0)], [[0]], [1, 1], [3], plan=plan)
        )
        got = alloc_by_batch(r["legs"][0])
        self.assertEqual(sum(got.values()), 3)
        # 同余数:1 分、2 分各补一次(批号 1 先),floor 同为 1
        self.assertEqual(got, {1: 2, 2: 1})

    def test_largest_remainder_beats_smaller_batch(self):
        # 权重 2:1,费 2 分:批1 4/3(余1/3),批2 2/3(余2/3) → 余分归批2
        plan = {
            "transport_hours": 24,
            "box_capacity": 100,
            "zones": [{"name": "z", "t_min": 0, "t_max": 8}],
            "cargoes": [
                {"name": "A", "units": 2, "unit_volume": 1, "t_min": 0, "t_max": 8},
                {"name": "B", "units": 1, "unit_volume": 1, "t_min": 0, "t_max": 8},
            ],
        }
        r = allocate_freight(
            body([st("S", u=0)], [[0]], [1, 1], [2], plan=plan)
        )
        self.assertEqual(alloc_by_batch(r["legs"][0]), {1: 1, 2: 1})

    def test_batch_unloaded_at_earlier_stop_excluded_later(self):
        # 站序 1→2;批1 归站1、批2 归站2:第 2 程仅批2 在车
        r = allocate_freight(
            body(
                [st("东城站", 2, 8, w=2), st("西城站", 3, 12, w=3)],
                [[0, 2], [2, 0]],
                [1, 2],
                [700, 7],
            )
        )
        self.assertEqual([o["batch_no"] for o in r["legs"][1]["onboard"]], [2])
        self.assertEqual(
            sorted(o["batch_no"] for o in r["legs"][0]["onboard"]), [1, 2]
        )

    def test_split_boxes_merge_units(self):
        # 容量 1.5:批1(2×1)拆两箱;与批2(1×1)首程按权重 2:1 分担 9 分
        split_plan = {**PLAN, "box_capacity": 1.5, "max_boxes": 10}
        r = allocate_freight(
            body(
                [st("S1", u=0), st("S2", u=0)],
                [[0, 0], [0, 0]],
                [1, 2],
                [9, 5],
                plan=split_plan,
            )
        )
        self.assertEqual(alloc_by_batch(r["legs"][0]), {1: 6, 2: 3})
        self.assertEqual(alloc_by_batch(r["legs"][1]), {2: 5})
        totals = {b["batch_no"]: b["total_allocated_cents"]
                  for b in r["batch_totals"]}
        self.assertEqual(totals, {1: 6, 2: 8})

    def test_zero_fees_all_zero(self):
        r = allocate_freight(
            body(
                [st("S1"), st("S2")],
                [[0, 0], [0, 0]],
                [1, 2],
                [0, 0],
            )
        )
        for leg in r["legs"]:
            self.assertTrue(all(o["allocated_cents"] == 0 for o in leg["onboard"]))
        self.assertEqual(r["total_fee_cents"], 0)

    def test_empty_station_leg_requires_zero_fee(self):
        # 三批但只归站1/2(批3 也归站1),站3 无归属;最优站序 1,2,3,
        # 第 3 程无在车批次
        plan = {
            "transport_hours": 24,
            "box_capacity": 100,
            "zones": [{"name": "z", "t_min": 0, "t_max": 8}],
            "cargoes": [
                {"name": n, "units": 2, "unit_volume": 1, "t_min": 0, "t_max": 8}
                for n in ("A", "B", "C")
            ],
        }
        stations = [st("S1", u=0), st("S2", u=0), st("S3", u=0)]
        travel = [[0] * 3 for _ in range(3)]
        with self.assertRaises(ScheduleError) as cm:
            allocate_freight(
                body(stations, travel, [1, 2, 1], [10, 10, 1], plan=plan)
            )
        self.assertIn("没有任何在车批次", str(cm.exception))
        r = allocate_freight(
            body(stations, travel, [1, 2, 1], [10, 10, 0], plan=plan)
        )
        self.assertEqual(r["legs"][2]["onboard"], [])
        self.assertEqual(r["legs"][2]["allocated_total_cents"], 0)

    # ---- 拒绝场景:不返回部分分摊 ----

    def _expect_error(self, payload, needle):
        with self.assertRaises(ScheduleError) as cm:
            allocate_freight(payload)
        self.assertIn(needle, str(cm.exception))

    def test_fee_count_mismatch(self):
        stations = [st("S1"), st("S2")]
        travel = [[0, 0], [0, 0]]
        self._expect_error(
            body(stations, travel, [1, 2], [100]), "费用条数须与站序"
        )
        self._expect_error(
            body(stations, travel, [1, 2], [100, 100, 100]), "费用条数须与站序"
        )

    def test_fee_must_be_list(self):
        self._expect_error(
            {
                "plan_input": PLAN,
                "stations": [st("S1")],
                "travel": [[0]],
                "assignments": [1, 1],
            },
            "逐程填写分运费",
        )

    def test_illegal_fees(self):
        stations = [st("S1"), st("S2")]
        travel = [[0, 0], [0, 0]]
        for bad in (-1, 1.5, "10", True, None, float("nan")):
            self._expect_error(
                body(stations, travel, [1, 2], [bad, 10]),
                "分运费须为非负整数",
            )

    def test_infeasible_schedule_rejected_without_partial(self):
        # 站1 窗口 [0,0.5] 但卸货 1h,两种站序均不可行
        self._expect_error(
            body(
                [st("S1", 0, 0.5, u=1), st("S2")],
                [[0, 1], [1, 0]],
                [1, 2],
                [100, 100],
            ),
            "不存在可行的站点送达顺序",
        )

    def test_bad_plan_input_rejected(self):
        self._expect_error(
            body(
                [st("S1")], [[0]], [1, 1], [10],
                plan={**PLAN, "box_capacity": 1.0, "max_boxes": 1},
            ),
            "全员可运输",
        )

    def test_fingerprint_changes_with_fees(self):
        payload = body(
            [st("S1", u=0)], [[0]], [1, 1], [10],
        )
        a = allocate_freight(payload)
        b = allocate_freight({**payload, "fees": [11]})
        self.assertNotEqual(a["input_fingerprint"], b["input_fingerprint"])
        # 费用不变指纹稳定
        self.assertEqual(
            a["input_fingerprint"], allocate_freight(payload)["input_fingerprint"]
        )
        self.assertEqual(a["plan_fingerprint"], b["plan_fingerprint"])
        self.assertNotEqual(a["schedule_fingerprint"], a["input_fingerprint"])


@unittest.skipUnless(_APP_AVAILABLE, "未安装 FastAPI,跳过 HTTP 层用例")
class FreightApiTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def _post(self, payload):
        return self.client.post("/api/freight-allocation", json=payload)

    def test_api_ok(self):
        resp = self._post(
            body(
                [st("东城站", 2, 8, w=2), st("西城站", 3, 12, w=3)],
                [[0, 2], [2, 0]],
                [1, 2],
                [1000, 400],
            )
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["total_fee_cents"], 1400)
        self.assertEqual(data["legs"][0]["allocated_total_cents"], 1000)
        self.assertEqual(data["legs"][1]["allocated_total_cents"], 400)

    def test_api_count_mismatch_400_no_partial(self):
        resp = self._post(
            body(
                [st("S1"), st("S2")],
                [[0, 0], [0, 0]],
                [1, 2],
                [100],
            )
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertIn("费用条数", data["detail"])
        self.assertNotIn("legs", data)

    def test_api_illegal_fee_400(self):
        resp = self._post(
            body(
                [st("S1"), st("S2")],
                [[0, 0], [0, 0]],
                [1, 2],
                [-5, 10],
            )
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("非负整数", resp.json()["detail"])

    def test_api_infeasible_400(self):
        resp = self._post(
            body(
                [st("S1", 0, 0.5, u=1), st("S2")],
                [[0, 1], [1, 0]],
                [1, 2],
                [100, 100],
            )
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不存在可行的站点送达顺序", resp.json()["detail"])

    def test_api_float_fee_rejected(self):
        # 1.0 这类「整数值浮点」也不接受:费用必须是非负整数
        resp = self._post(
            body([st("S1")], [[0]], [1, 1], [1.0])
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("非负整数", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
