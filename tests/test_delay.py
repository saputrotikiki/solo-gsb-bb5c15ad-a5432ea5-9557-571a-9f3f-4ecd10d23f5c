"""运输延误推演 API 测试(stdlib unittest,无需额外数据库)。

运行:python tests/test_delay.py
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

from app.delay import simulate_delay  # noqa: E402

# 三段运输:干线/支线同锁温组;三文鱼有预算与最长运输时长,鲜奶仅预算,牛排无预算
PLAN = {
    "box_capacity": 100,
    "max_boxes": 20,
    "segments": [
        {"name": "干线冷链", "hours": 14, "lock_group": "干线锁温",
         "zones": [{"name": "冷冻区", "t_min": -18, "t_max": -2},
                   {"name": "冷藏区", "t_min": 0, "t_max": 8}]},
        {"name": "支线转运", "hours": 6, "lock_group": "干线锁温",
         "zones": [{"name": "冷冻区", "t_min": -18, "t_max": -2},
                   {"name": "冷藏区", "t_min": 0, "t_max": 8}]},
        {"name": "末端配送", "hours": 4,
         "zones": [{"name": "冷藏区", "t_min": 0, "t_max": 8},
                   {"name": "冷冻区", "t_min": -18, "t_max": -2}]},
    ],
    "cargoes": [
        {"name": "三文鱼", "units": 10, "unit_volume": 0.4, "t_min": -2, "t_max": 2,
         "preferred_t_min": -1, "preferred_t_max": 1, "max_degree_hours": 30,
         "max_transport_hours": 26},
        {"name": "巴氏鲜奶", "units": 10, "unit_volume": 0.25, "t_min": 2, "t_max": 6,
         "preferred_t_min": 3, "preferred_t_max": 5, "max_degree_hours": 20},
        {"name": "冷冻牛排", "units": 5, "unit_volume": 0.8, "t_min": -22, "t_max": -15},
    ],
}

# 单段(单一温区)模式:延误只作用于第 1 段(全程)
SINGLE = {
    "transport_hours": 10,
    "box_capacity": 100,
    "zones": [{"name": "冷藏区", "t_min": 0, "t_max": 8}],
    "cargoes": [
        {"name": "鲜奶", "units": 10, "unit_volume": 0.25, "t_min": 2, "t_max": 6,
         "preferred_t_min": 3, "preferred_t_max": 5, "max_degree_hours": 20,
         "max_transport_hours": 15},
    ],
}


def _post(client: TestClient, body: dict):
    return client.post("/api/transport-delay", json=body)


class DelayDirectTestCase(unittest.TestCase):
    """不依赖 HTTP 层的核心规则测试。"""

    def test_zero_extra_reproduces_baseline(self):
        d = simulate_delay({"plan_input": PLAN, "segment_no": 2, "extra_hours": 0})
        self.assertTrue(d["can_continue"])
        self.assertEqual(d["total_hours"]["original"], 24.0)
        self.assertEqual(d["total_hours"]["delayed"], 24.0)
        self.assertEqual(d["affected_batches"], [])
        self.assertIn("可继续运输", d["verdict"])
        # 零延误时逐段时长与设定点均与原值相同
        for box in d["boxes"]:
            for s in box["segment_setpoints"]:
                self.assertEqual(s["hours_original"], s["hours_delayed"])
                self.assertFalse(s["setpoint_changed"])
                self.assertEqual(s["setpoint_original"], s["setpoint_delayed"])
            for h in box["heat_exposure"]:
                self.assertEqual(h["exposure_original"], h["exposure_delayed"])
                self.assertFalse(h["exceeded_original"])
                self.assertFalse(h["exceeded_delayed"])
                self.assertEqual(h["reasons"], [])

    def test_max_transport_hours_violation(self):
        # 末端 +8h:总时长 24 -> 32,仅三文鱼(max=26)超最长运输时长
        d = simulate_delay({"plan_input": PLAN, "segment_no": 3, "extra_hours": 8})
        self.assertFalse(d["can_continue"])
        self.assertEqual(d["total_hours"]["delayed"], 32.0)
        affected = d["affected_batches"]
        self.assertEqual(len(affected), 1)
        a = affected[0]
        self.assertEqual(a["batch_no"], 1)
        self.assertEqual(a["units"], 10)
        self.assertEqual(a["reasons"], ["max_transport_hours"])
        self.assertIsNone(a["first_exceeded_segment_no"])
        self.assertEqual(
            d["summary"]["max_transport_hours_violation_count"], 1
        )
        self.assertEqual(d["summary"]["degree_hours_violation_count"], 0)
        self.assertEqual(d["summary"]["affected_batch_count"], 1)
        self.assertEqual(d["summary"]["affected_units"], 10)
        self.assertIn("不可继续运输", d["verdict"])

    def test_small_delay_within_limits(self):
        # 干线 +2h:总时长 24 -> 26,恰好不超三文鱼最长运输时长(26,严格大于才算超)
        d = simulate_delay({"plan_input": PLAN, "segment_no": 1, "extra_hours": 2})
        self.assertTrue(d["can_continue"])
        self.assertEqual(d["affected_batches"], [])
        seg = d["segments"][0]
        self.assertTrue(seg["is_delayed"])
        self.assertEqual(seg["hours_original"], 14.0)
        self.assertEqual(seg["hours_delayed"], 16.0)

    def test_only_target_segment_hours_changes(self):
        d = simulate_delay({"plan_input": PLAN, "segment_no": 2, "extra_hours": 5})
        flags = [s["is_delayed"] for s in d["segments"]]
        self.assertEqual(flags, [False, True, False])
        hours = [(s["hours_original"], s["hours_delayed"]) for s in d["segments"]]
        self.assertEqual(hours, [(14.0, 14.0), (6.0, 11.0), (4.0, 4.0)])

    def test_single_zone_mode_segment_one(self):
        d = simulate_delay({"plan_input": SINGLE, "segment_no": 1, "extra_hours": 4})
        # 10 -> 14h,未超最长 15h,设定点 4℃ 在首选区间内度时为 0
        self.assertTrue(d["can_continue"])
        self.assertEqual(d["total_hours"]["delayed"], 14.0)
        d2 = simulate_delay({"plan_input": SINGLE, "segment_no": 1, "extra_hours": 10})
        self.assertFalse(d2["can_continue"])
        (a,) = d2["affected_batches"]
        self.assertIn("max_transport_hours", a["reasons"])

    def test_degree_hours_first_exceeded_segment(self):
        # 两段温区都强制设定点不低于 2℃(到首选区间 [0,0] 距离 2):
        # 基准 10×2 + 5×2 = 30(预算 30,严格大于才算超限,恰好不超);
        # 第 2 段 +10h 后累计 50 > 30,首次超限段即延误段(第 2 段)
        plan = {
            "box_capacity": 100,
            "segments": [
                {"name": "前段", "hours": 10,
                 "zones": [{"name": "高区", "t_min": 2, "t_max": 8}]},
                {"name": "延误段", "hours": 5,
                 "zones": [{"name": "高区", "t_min": 2, "t_max": 8}]},
            ],
            "cargoes": [
                {"name": "货", "units": 2, "unit_volume": 0.4, "t_min": -2, "t_max": 8,
                 "preferred_t_min": 0, "preferred_t_max": 0, "max_degree_hours": 30},
            ],
        }
        d = simulate_delay({"plan_input": plan, "segment_no": 2, "extra_hours": 10})
        self.assertFalse(d["can_continue"])
        (a,) = d["affected_batches"]
        self.assertEqual(a["reasons"], ["degree_hours"])
        self.assertEqual(a["first_exceeded_segment_no"], 2)
        self.assertEqual(a["first_exceeded_segment_name"], "延误段")
        # 基准:10h×2 + 5h×2 = 30(恰好不超);延误:(10+15)h×2 = 50
        self.assertAlmostEqual(a["exposure_original"], 30.0)
        self.assertAlmostEqual(a["exposure_delayed"], 50.0)

    def test_setpoint_recomputed_after_delay(self):
        # 延误使后续单元的最优设定点翻转:第 2 段设定点 4 -> 0
        plan = {
            "box_capacity": 100,
            "segments": [
                {"name": "延误段", "hours": 5,
                 "zones": [{"name": "宽区", "t_min": 0, "t_max": 8}]},
                {"name": "后段", "hours": 5,
                 "zones": [{"name": "宽区", "t_min": 0, "t_max": 8}]},
            ],
            "cargoes": [
                {"name": "A", "units": 2, "unit_volume": 0.4, "t_min": 0, "t_max": 8,
                 "preferred_t_min": 0, "preferred_t_max": 0, "max_degree_hours": 100},
                {"name": "B", "units": 2, "unit_volume": 0.4, "t_min": 0, "t_max": 8,
                 "preferred_t_min": 4, "preferred_t_max": 4, "max_degree_hours": 20},
            ],
        }
        d = simulate_delay({"plan_input": plan, "segment_no": 1, "extra_hours": 30})
        (box,) = d["boxes"]
        sp = {s["segment_no"]: s for s in box["segment_setpoints"]}
        self.assertEqual(sp[1]["setpoint_delayed"], 4.0)
        self.assertEqual(sp[2]["setpoint_original"], 4.0)
        self.assertEqual(sp[2]["setpoint_delayed"], 0.0)
        self.assertTrue(sp[2]["setpoint_changed"])

    def test_lock_group_shared_setpoint(self):
        # 相邻同锁温组段在延误后仍共用一个设定点
        d = simulate_delay({"plan_input": PLAN, "segment_no": 1, "extra_hours": 1})
        for box in d["boxes"]:
            sp = box["segment_setpoints"]
            if sp[0]["lock_group"] == "干线锁温":
                self.assertEqual(sp[0]["lock_group_segments"], [1, 2])
                self.assertEqual(sp[1]["lock_group_segments"], [1, 2])
                self.assertEqual(
                    sp[0]["setpoint_delayed"], sp[1]["setpoint_delayed"]
                )

    def test_box_and_units_unchanged(self):
        d = simulate_delay({"plan_input": PLAN, "segment_no": 3, "extra_hours": 8})
        # 与基准方案的箱号、批次、件数完全一致
        from app.planner import plan

        base = plan(PLAN)
        self.assertEqual(
            [b["box_no"] for b in d["boxes"]],
            [b["box_no"] for b in base["boxes"]],
        )
        for db, bb in zip(d["boxes"], base["boxes"]):
            self.assertEqual(db["items"], bb["items"])

    def test_invalid_inputs(self):
        with self.assertRaisesRegex(ValueError, "运输段"):
            simulate_delay({"plan_input": PLAN, "segment_no": 9, "extra_hours": 1})
        with self.assertRaisesRegex(ValueError, "非负"):
            simulate_delay({"plan_input": PLAN, "segment_no": 1, "extra_hours": -1})
        with self.assertRaisesRegex(ValueError, "非负"):
            simulate_delay({"plan_input": PLAN, "segment_no": 1, "extra_hours": "abc"})
        with self.assertRaisesRegex(ValueError, "运输段"):
            simulate_delay({"plan_input": PLAN, "extra_hours": 1})

    def test_fingerprint_matches_snapshot(self):
        from app.planner import input_fingerprint

        d = simulate_delay({"plan_input": PLAN, "segment_no": 1, "extra_hours": 1})
        self.assertEqual(d["input_fingerprint"], input_fingerprint(PLAN))


@unittest.skipUnless(_APP_AVAILABLE, "未安装 FastAPI,跳过 HTTP 用例")
class DelayApiTestCase(unittest.TestCase):
    """HTTP 层:状态码、校验与响应结构。"""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_api_ok_and_continue(self):
        resp = _post(self.client, {
            "plan_input": PLAN, "segment_no": 1, "extra_hours": 2
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertTrue(data["can_continue"])
        self.assertIn("segments", data)
        self.assertEqual(len(data["segments"]), 3)

    def test_api_violation(self):
        resp = _post(self.client, {
            "plan_input": PLAN, "segment_no": 3, "extra_hours": 8
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertFalse(data["can_continue"])
        self.assertEqual(data["summary"]["affected_batch_count"], 1)

    def test_api_bad_segment_400(self):
        resp = _post(self.client, {
            "plan_input": PLAN, "segment_no": 99, "extra_hours": 1
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("运输段序号", resp.json()["detail"])

    def test_api_negative_extra_422(self):
        resp = _post(self.client, {
            "plan_input": PLAN, "segment_no": 1, "extra_hours": -3
        })
        self.assertEqual(resp.status_code, 422)

    def test_api_bad_plan_input_400(self):
        resp = _post(self.client, {
            "plan_input": {"box_capacity": 0},
            "segment_no": 1,
            "extra_hours": 1,
        })
        self.assertIn(resp.status_code, (400, 422))


if __name__ == "__main__":
    unittest.main(verbosity=2)
