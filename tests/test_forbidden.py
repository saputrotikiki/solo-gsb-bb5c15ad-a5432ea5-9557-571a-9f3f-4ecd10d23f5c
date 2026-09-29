"""禁配装箱模式测试(stdlib unittest;API 用例需要 fastapi,缺失时自动跳过)。

运行:python tests/test_forbidden.py
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

from app.planner import plan  # noqa: E402

ZONE = [{"name": "通用", "t_min": -30, "t_max": 30}]


def cargo(name, units, vol, lo=0, hi=8, **kw):
    return {
        "name": name,
        "units": units,
        "unit_volume": vol,
        "t_min": lo,
        "t_max": hi,
        **kw,
    }


def payload(cargoes, cap=10, max_boxes=10, **kw):
    return {
        "box_capacity": cap,
        "max_boxes": max_boxes,
        "zones": ZONE,
        "forbidden_mode": True,
        "cargoes": cargoes,
        **kw,
    }


class ForbiddenBasicTestCase(unittest.TestCase):
    def test_forbidden_pair_never_share_box(self):
        r = plan(payload([
            {**cargo("A", 3, 2), "forbidden_with": "2"},
            cargo("B", 3, 2),
            cargo("C", 3, 2),
        ]))
        self.assertEqual(r["summary"]["boxes_used"], 2)
        for b in r["boxes"]:
            nos = {i["batch_no"] for i in b["items"]}
            self.assertFalse({1, 2} <= nos)
        self.assertEqual(r["summary"]["all_loaded"], True)
        self.assertEqual(r["summary"]["loaded_units"], 9)

    def test_unilateral_declaration_is_symmetric(self):
        # 只有批号 2 填写了禁配 1、3,仍视为双方禁配
        r = plan(payload([
            cargo("A", 1, 1),
            {**cargo("B", 1, 1), "forbidden_with": "1，3"},
            cargo("C", 1, 1),
        ]))
        self.assertEqual(
            [(p["a"], p["b"]) for p in r["forbidden_pairs"]],
            [(1, 2), (2, 3)],
        )

    def test_same_batch_split_across_boxes(self):
        r = plan(payload([
            cargo("A", 4, 1),
            {**cargo("B", 2, 1), "forbidden_with": "1"},
        ], cap=3))
        for b in r["boxes"]:
            nos = {i["batch_no"] for i in b["items"]}
            self.assertFalse({1, 2} <= nos)
        self.assertEqual(r["summary"]["loaded_units"], 6)

    def test_temperature_incompatibility_forces_separate_box(self):
        r = plan(payload([
            cargo("A", 2, 1, -2, 2),
            cargo("B", 2, 1, 4, 8),
        ], max_boxes=5,
            zones=[{"name": "冷", "t_min": -5, "t_max": 2},
                   {"name": "凉", "t_min": 4, "t_max": 10}]))
        self.assertEqual(r["summary"]["boxes_used"], 2)
        for b in r["boxes"]:
            self.assertEqual(len({i["batch_no"] for i in b["items"]}), 1)

    def test_budget_incompatibility_forces_separate_box(self):
        # A、B 首选区间相差大且预算紧:独占暴露均为 0,拼箱必致一方超预算
        r = plan(payload([
            cargo("A", 1, 1, 0, 8,
                  preferred_t_min=-1, preferred_t_max=1, max_degree_hours=10),
            cargo("B", 1, 1, 0, 8,
                  preferred_t_min=5, preferred_t_max=7, max_degree_hours=10),
        ], transport_hours=10))
        self.assertEqual(r["summary"]["boxes_used"], 2)

    def test_segments_and_lock_group_still_apply(self):
        r = plan(payload([
            cargo("A", 1, 1, 0, 8),
            cargo("B", 1, 1, 2, 6),
        ], segments=[
            {"name": "s1", "hours": 5, "lock_group": "G",
             "zones": [{"name": "z", "t_min": 0, "t_max": 8}]},
            {"name": "s2", "hours": 5, "lock_group": "G",
             "zones": [{"name": "z", "t_min": 0, "t_max": 8}]},
        ]))
        self.assertEqual(r["summary"]["boxes_used"], 1)
        self.assertTrue(all(b["transportable"] for b in r["boxes"]))


class ForbiddenOptimalityTestCase(unittest.TestCase):
    def test_uses_minimum_boxes(self):
        # 互禁 3 批,容量够大:必须 3 箱
        r = plan(payload([
            {**cargo("A", 1, 1), "forbidden_with": "2,3"},
            {**cargo("B", 1, 1), "forbidden_with": "3"},
            cargo("C", 1, 1),
        ]))
        self.assertEqual(r["summary"]["boxes_used"], 3)
        self.assertEqual(r["forbidden_box_sequence"], [1, 2, 3])

    def test_lexicographically_smallest_sequence_on_tie(self):
        # A=2 件×4 禁配 B=2 件×2;C=1 件×2。容量 10:
        # box1=A,A,C(10) 与 box1=A,A / box2=B,B,C 都是 2 箱并列,
        # 字典序最小要求 C(逐件序列第 5 件)放进编号最小的可行箱 -> 1。
        r = plan(payload([
            {**cargo("A", 2, 4), "forbidden_with": "2"},
            cargo("B", 2, 2),
            cargo("C", 1, 2),
        ]))
        self.assertEqual(r["summary"]["boxes_used"], 2)
        self.assertEqual(r["forbidden_box_sequence"], [1, 1, 2, 2, 1])

    def test_no_solution_within_max_boxes_raises_with_hint(self):
        body = payload([
            {**cargo("A", 1, 1), "forbidden_with": "2,3"},
            {**cargo("B", 1, 1), "forbidden_with": "3"},
            cargo("C", 1, 1),
        ], max_boxes=2)
        with self.assertRaises(ValueError) as ctx:
            plan(body)
        self.assertIn("禁配装箱模式无解", str(ctx.exception))
        self.assertIn("至少 3 箱", str(ctx.exception))
        # 放宽到 3 箱即有解,且不返回部分方案的保证体现在直接抛错
        body["max_boxes"] = 3
        self.assertEqual(plan(body)["summary"]["boxes_used"], 3)

    def test_no_partial_plan_key_error_shape(self):
        # 无解时是抛异常而非返回 unloaded 部分方案
        body = payload([cargo("A", 10, 1)], max_boxes=1, cap=1)
        with self.assertRaises(ValueError):
            plan(body)


class ForbiddenValidationTestCase(unittest.TestCase):
    def _expect(self, body, fragment):
        with self.assertRaises(ValueError) as ctx:
            plan(body)
        self.assertIn(fragment, str(ctx.exception))

    def test_self_reference_rejected(self):
        self._expect(
            payload([{**cargo("A", 1, 1), "forbidden_with": "1"}]),
            "不得将自身序号",
        )

    def test_out_of_range_rejected(self):
        self._expect(
            payload([{**cargo("A", 1, 1), "forbidden_with": "5"}]),
            "超出范围",
        )

    def test_non_numeric_rejected(self):
        self._expect(
            payload([{**cargo("A", 1, 1), "forbidden_with": "x"}]),
            "非法",
        )

    def test_total_units_limited_1_to_10(self):
        self._expect(payload([cargo("A", 11, 1)]), "总件数")
        self._expect(payload([cargo("A", 0, 1)]), "件数必须大于 0")

    def test_solo_infeasible_rejected_as_no_solution(self):
        # 单件体积超过容量
        self._expect(
            payload([cargo("A", 1, 20)]),
            "禁配装箱模式无解",
        )

    def test_disabled_mode_ignores_forbidden_with(self):
        body = {
            "box_capacity": 10,
            "max_boxes": 10,
            "zones": ZONE,
            "cargoes": [
                {**cargo("A", 2, 1), "forbidden_with": "2"},
                cargo("B", 2, 1),
            ],
        }
        r = plan(body)
        self.assertFalse(r["forbidden_mode"])
        self.assertEqual(r["forbidden_pairs"], [])
        self.assertIsNone(r["forbidden_box_sequence"])
        self.assertEqual(r["summary"]["boxes_used"], 1)

    def test_disabled_mode_behaves_like_ffd(self):
        body = {
            "box_capacity": 1,
            "max_boxes": 1,
            "zones": ZONE,
            "cargoes": [cargo("A", 3, 1)],
        }
        r = plan(body)
        self.assertEqual(r["summary"]["unloaded_units"], 2)
        self.assertEqual(r["summary"]["boxes_used"], 1)


class SnapshotPropagationTestCase(unittest.TestCase):
    """容灾评估、延误推演与交接单沿用模式与禁配快照。"""

    FORBIDDEN_BODY = payload([
        {**cargo("A", 2, 1), "forbidden_with": "2"},
        cargo("B", 2, 1),
    ])

    def test_delay_carries_forbidden_snapshot(self):
        from app.delay import simulate_delay

        r = plan(self.FORBIDDEN_BODY)
        d = simulate_delay({
            "plan_input": self.FORBIDDEN_BODY,
            "segment_no": 1,
            "extra_hours": 0,
        })
        self.assertTrue(d["forbidden_mode"])
        self.assertEqual(d["forbidden_pair_count"], 1)
        # 箱号/批次/件数与基准一致(不重新装箱)
        self.assertEqual(
            [[(i["batch_no"], i["units"]) for i in b["items"]] for b in d["boxes"]],
            [[(i["batch_no"], i["units"]) for i in b["items"]] for b in r["boxes"]],
        )

    def test_assessment_carries_forbidden_snapshot(self):
        from app.resilience import assess_zone_failures

        a = assess_zone_failures(self.FORBIDDEN_BODY)
        self.assertTrue(a["forbidden_mode"])
        self.assertEqual(a["forbidden_pair_count"], 1)
        self.assertTrue(a["baseline"]["forbidden_mode"])
        # 唯一温区失效 -> 全员无法运输,场景仍返回结构化结果而非崩溃
        self.assertFalse(a["scenarios"][0]["transportable"])
        self.assertEqual(a["scenarios"][0]["newly_unloaded_units"], 4)


@unittest.skipUnless(_APP_AVAILABLE, "未安装 fastapi,跳过 API 用例")
class ForbiddenApiTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_plan_api_forbidden_mode(self):
        resp = self.client.post("/api/plan", json=payload([
            {**cargo("A", 1, 1), "forbidden_with": "2"},
            cargo("B", 1, 1),
        ]))
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["summary"]["boxes_used"], 2)
        self.assertTrue(data["forbidden_mode"])

    def test_plan_api_no_solution_returns_400(self):
        resp = self.client.post("/api/plan", json=payload([
            {**cargo("A", 1, 1), "forbidden_with": "2"},
            cargo("B", 1, 1),
        ], max_boxes=1))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("禁配装箱模式无解", resp.json()["detail"])

    def test_plan_api_invalid_batch_ref_returns_400(self):
        resp = self.client.post("/api/plan", json=payload([
            {**cargo("A", 1, 1), "forbidden_with": "9"},
        ]))
        self.assertEqual(resp.status_code, 400)

    def test_handover_order_freezes_forbidden_snapshot(self):
        import tempfile

        from app import db, handover

        db_path = os.path.join(tempfile.mkdtemp(), "h.db")
        os.environ["HANDOVER_DB_PATH"] = db_path
        db.reset()
        body = payload([
            {**cargo("A", 2, 1), "forbidden_with": "2"},
            cargo("B", 2, 1),
        ])
        order = handover.create_order(body)
        self.assertTrue(order["forbidden_mode"])
        self.assertEqual(order["forbidden_pair_count"], 1)
        orders = handover.list_orders()
        self.assertTrue(orders[-1]["forbidden_mode"])
        db.reset()
        os.environ.pop("HANDOVER_DB_PATH", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
