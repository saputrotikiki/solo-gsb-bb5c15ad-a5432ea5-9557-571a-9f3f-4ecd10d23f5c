"""送达站点排程 API 测试(stdlib unittest,无需额外数据库)。

运行:python tests/test_delivery.py
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

from app.delivery import ScheduleError, schedule_delivery  # noqa: E402

# 两批货物、单一温区、运输总时长 12h;两批可同箱
PLAN = {
    "transport_hours": 12,
    "box_capacity": 100,
    "zones": [{"name": "冷藏区", "t_min": 0, "t_max": 8}],
    "cargoes": [
        {"name": "鲜奶", "units": 40, "unit_volume": 0.25, "t_min": 2, "t_max": 6},
        {"name": "三文鱼", "units": 10, "unit_volume": 0.4, "t_min": -2, "t_max": 2},
    ],
}

# 鲜奶会被拆到多箱的小容量方案
SPLIT_PLAN = {
    "transport_hours": 10,
    "box_capacity": 3.0,
    "max_boxes": 10,
    "zones": [{"name": "冷藏区", "t_min": 0, "t_max": 8}],
    "cargoes": [
        {"name": "鲜奶", "units": 40, "unit_volume": 0.25, "t_min": 2, "t_max": 6},
        {"name": "三文鱼", "units": 10, "unit_volume": 0.4, "t_min": -2, "t_max": 2},
    ],
}


def st(name, s, e, u, w):
    return {
        "name": name,
        "window_start": s,
        "window_end": e,
        "unload_hours": u,
        "from_warehouse": w,
    }


def body(stations, travel, assignments, plan=PLAN):
    return {
        "plan_input": plan,
        "stations": stations,
        "travel": travel,
        "assignments": assignments,
    }


class DeliveryDirectTestCase(unittest.TestCase):
    """不依赖 HTTP 层的核心规则测试。"""

    def test_basic_order_and_times(self):
        # 站1:仓库 2h 到,窗口 [2,8],卸 1h → 2~3;站1→站2 2h,到 5,卸到 6
        d = schedule_delivery(
            body(
                [st("东城站", 2, 8, 1, 2), st("西城站", 3, 12, 1, 3)],
                [[0, 2], [2, 0]],
                [1, 2],
            )
        )
        self.assertEqual(d["optimal_order"], [1, 2])
        self.assertEqual(d["final_finish"], 6.0)
        self.assertEqual(d["time_margin"], 6.0)
        self.assertEqual(d["orders_evaluated"], 2)
        self.assertEqual(d["feasible_order_count"], 2)
        s1, s2 = d["stops"]
        self.assertEqual((s1["arrival_time"], s1["wait_hours"],
                          s1["unload_start"], s1["unload_finish"]), (2.0, 0.0, 2.0, 3.0))
        self.assertEqual((s2["arrival_time"], s2["unload_finish"]), (5.0, 6.0))
        # 明细:箱号/批次/件数按快照核对
        self.assertEqual(s1["items"],
                         [{"box_no": 1, "batch_no": 1, "name": "鲜奶", "units": 40}])
        self.assertEqual(s2["items"],
                         [{"box_no": 1, "batch_no": 2, "name": "三文鱼", "units": 10}])

    def test_wait_for_window(self):
        # 站2 窗口 0 点起:先走站2(到 1 卸到 2),再 1h 到站1,等到 4 点卸到 5
        d = schedule_delivery(
            body(
                [st("S1", 4, 10, 1, 1), st("S2", 0, 20, 1, 1)],
                [[0, 1], [1, 0]],
                [1, 2],
            )
        )
        self.assertEqual(d["optimal_order"], [2, 1])
        last = d["stops"][-1]
        self.assertEqual(last["station_no"], 1)
        self.assertEqual(last["arrival_time"], 3.0)
        self.assertEqual(last["wait_hours"], 1.0)
        self.assertEqual(last["unload_start"], 4.0)
        self.assertEqual(last["unload_finish"], 5.0)

    def test_directed_matrix_asymmetric(self):
        # 1→2 需要 10h,2→1 仅 1h:先去站2 末站 4h 完成,明显更优
        d = schedule_delivery(
            body(
                [st("S1", 0, 20, 1, 1), st("S2", 0, 20, 1, 1)],
                [[0, 10], [1, 0]],
                [1, 2],
            )
        )
        self.assertEqual(d["optimal_order"], [2, 1])
        self.assertEqual(d["final_finish"], 4.0)

    def test_tie_prefers_lexicographically_smallest(self):
        d = schedule_delivery(
            body(
                [st("S1", 0, 20, 0, 0), st("S2", 0, 20, 0, 0)],
                [[0, 0], [0, 0]],
                [1, 2],
            )
        )
        self.assertEqual(d["feasible_order_count"], 2)
        self.assertEqual(d["final_finish"], 0.0)
        self.assertEqual(d["optimal_order"], [1, 2])

    def test_boundary_finish_equal_window_end_and_total(self):
        # 恰等窗口结束:窗口 [0,2]、卸货 2h;恰等总时长:transport_hours=2
        d = schedule_delivery(
            body(
                [st("唯一站", 0, 2, 2, 0)],
                [[0]],
                [1, 1],
                plan={**PLAN, "transport_hours": 2},
            )
        )
        self.assertEqual(d["final_finish"], 2.0)
        self.assertEqual(d["time_margin"], 0.0)
        self.assertEqual(d["stops"][0]["total_units"], 50)

    def test_infeasible_window_too_short(self):
        with self.assertRaises(ScheduleError) as cm:
            schedule_delivery(
                body(
                    [st("S1", 0, 0.5, 1, 0), st("S2", 0, 20, 1, 0)],
                    [[0, 1], [1, 0]],
                    [1, 2],
                )
            )
        self.assertIn("不存在可行的站点送达顺序", str(cm.exception))
        self.assertIn("不返回部分路线", str(cm.exception))

    def test_infeasible_last_beyond_total_hours(self):
        with self.assertRaises(ScheduleError) as cm:
            schedule_delivery(
                body(
                    [st("S1", 0, 20, 1, 1), st("S2", 0, 20, 1, 1)],
                    [[0, 1], [1, 0]],
                    [1, 2],
                    plan={**PLAN, "transport_hours": 3},
                )
            )
        self.assertIn("不存在可行的站点送达顺序", str(cm.exception))
        self.assertIn("3.0h", str(cm.exception))

    def test_multi_box_station_aggregation(self):
        d = schedule_delivery(
            body(
                [st("中心站", 0, 10, 1, 0)],
                [[0]],
                [1, 1],
                plan=SPLIT_PLAN,
            )
        )
        st0 = d["stations"][0]
        self.assertEqual(st0["box_nos"], [1, 2, 3, 4, 5])
        self.assertEqual(st0["total_units"], 50)
        self.assertEqual(sum(it["units"] for it in st0["items"]), 50)
        # 同箱同批合并、按箱号批号排序
        self.assertEqual(
            [(it["box_no"], it["batch_no"]) for it in st0["items"]],
            sorted((it["box_no"], it["batch_no"]) for it in st0["items"]),
        )

    def test_detail_form_assignments(self):
        d = schedule_delivery(
            body(
                [st("S1", 0, 20, 1, 1), st("S2", 0, 20, 1, 1)],
                [[0, 1], [1, 0]],
                [{"batch_no": 2, "station_no": 1},
                 {"batch_no": 1, "station_no": 2}],
            )
        )
        self.assertEqual(
            [it["batch_no"] for it in d["stations"][0]["items"]], [2]
        )
        self.assertEqual(
            [it["batch_no"] for it in d["stations"][1]["items"]], [1]
        )

    def test_fingerprint_stable_and_changes(self):
        a = schedule_delivery(body([st("S", 0, 10, 1, 0)], [[0]], [1, 1]))
        b = schedule_delivery(body([st("S", 0, 10, 1, 0)], [[0]], [1, 1]))
        c = schedule_delivery(body([st("S", 1, 10, 1, 0)], [[0]], [1, 1]))
        self.assertEqual(a["input_fingerprint"], b["input_fingerprint"])
        self.assertNotEqual(a["input_fingerprint"], c["input_fingerprint"])
        self.assertEqual(a["plan_fingerprint"], c["plan_fingerprint"])

    # ---- 输入校验 ----

    def _expect_error(self, payload, needle):
        with self.assertRaises(ScheduleError) as cm:
            schedule_delivery(payload)
        self.assertIn(needle, str(cm.exception))

    def test_window_end_before_start(self):
        self._expect_error(
            body([st("S", 5, 4, 1, 1), st("S2", 0, 20, 1, 1)],
                 [[0, 1], [1, 0]], [1, 2]),
            "窗口结束",
        )

    def test_negative_and_nonfinite_numbers(self):
        self._expect_error(
            body([st("S", 0, 4, -1, 1), st("S2", 0, 20, 1, 1)],
                 [[0, 1], [1, 0]], [1, 2]),
            "卸货耗时须为非负",
        )
        self._expect_error(
            body([st("S", float("nan"), 4, 1, 1)], [[0]], [1, 1]),
            "非负有限",
        )
        self._expect_error(
            body([st("S", 0, 4, 1, float("inf"))], [[0]], [1, 1]),
            "非负有限",
        )
        self._expect_error(
            body(
                [st("S", 0, 4, 1, 1), st("S2", 0, 20, 1, 1)],
                [[0, "x"], [1, 0]], [1, 2],
            ),
            "行车时长须为非负",
        )

    def test_station_count_limits(self):
        stations = [st(f"S{i}", 0, 20, 1, 1) for i in range(9)]
        self._expect_error(
            body(stations, [[0] * 9 for _ in range(9)], [1, 2]),
            "1~8",
        )
        self._expect_error(
            {"plan_input": PLAN, "stations": [], "travel": [], "assignments": [1]},
            "至少填写 1 个",
        )

    def test_assignment_coverage(self):
        self._expect_error(
            body(
                [st("S1", 0, 20, 1, 1), st("S2", 0, 20, 1, 1)],
                [[0, 1], [1, 0]],
                [1],
            ),
            "未指定送达站点",
        )
        self._expect_error(
            body(
                [st("S1", 0, 20, 1, 1), st("S2", 0, 20, 1, 1)],
                [[0, 1], [1, 0]],
                [1, 9],
            ),
            "站号须为 1~2",
        )
        self._expect_error(
            body([st("S1", 0, 20, 1, 1)], [[0]],
                 [{"batch_no": 1, "station_no": 1}]),
            "归属明细共 1 行",
        )
        self._expect_error(
            body([st("S1", 0, 20, 1, 1)], [[0]], [True, 1]),
            "站号须为 1~1",
        )

    def test_travel_matrix_shape(self):
        self._expect_error(
            body(
                [st("S1", 0, 20, 1, 1), st("S2", 0, 20, 1, 1)],
                [[0, 1], [0]],
                [1, 2],
            ),
            "矩阵第 2 行须含 2",
        )
        self._expect_error(
            body(
                [st("S1", 0, 20, 1, 1), st("S2", 0, 20, 1, 1)],
                [[0, 1]],
                [1, 2],
            ),
            "2×2",
        )

    def test_non_all_loaded_plan_rejected(self):
        self._expect_error(
            body(
                [st("S", 0, 20, 1, 1)],
                [[0]],
                [1, 1],
                plan={**PLAN, "box_capacity": 1.0, "max_boxes": 1},
            ),
            "全员可运输",
        )

    def test_missing_plan_input(self):
        self._expect_error(
            {"stations": [st("S", 0, 20, 1, 1)], "travel": [[0]],
             "assignments": [1, 1]},
            "plan_input",
        )


@unittest.skipUnless(_APP_AVAILABLE, "未安装 FastAPI,跳过 HTTP 层用例")
class DeliveryApiTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def _post(self, payload):
        return self.client.post("/api/delivery-schedule", json=payload)

    def test_api_ok(self):
        resp = self._post(
            body(
                [st("东城站", 2, 8, 1, 2), st("西城站", 3, 12, 1, 3)],
                [[0, 2], [2, 0]],
                [1, 2],
            )
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["optimal_order"], [1, 2])
        self.assertEqual(data["final_finish"], 6.0)
        self.assertEqual(len(data["stops"]), 2)

    def test_api_infeasible_returns_400_no_partial(self):
        resp = self._post(
            body(
                [st("S1", 0, 0.5, 1, 0), st("S2", 0, 20, 1, 0)],
                [[0, 1], [1, 0]],
                [1, 2],
            )
        )
        self.assertEqual(resp.status_code, 400)
        detail = resp.json()["detail"]
        self.assertIn("不存在可行的站点送达顺序", detail)
        self.assertNotIn("stops", resp.json())

    def test_api_validation_400(self):
        resp = self._post(
            body([st("S", 5, 4, 1, 1), st("S2", 0, 20, 1, 1)],
                 [[0, 1], [1, 0]], [1, 2])
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("窗口结束", resp.json()["detail"])

    def test_api_more_than_8_stations_422(self):
        stations = [st(f"S{i}", 0, 20, 1, 1) for i in range(9)]
        resp = self._post(
            body(stations, [[0] * 9 for _ in range(9)], [1, 2])
        )
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main(verbosity=2)
