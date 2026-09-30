import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError, SEAT_CAPACITY


class OccupancyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _event_payload(self, urgency):
        if urgency == "critical":
            strength, bandwidth = -35, 20.0
        elif urgency == "high":
            strength, bandwidth = -50, 20.0
        elif urgency == "medium":
            strength, bandwidth = -60, 20.0
        else:
            strength, bandwidth = -90, 0.1
        return {
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": bandwidth,
            "station_id": "ST-%s" % urgency,
            "region": "north",
            "strength_dbm": strength,
            "detected_at": "2026-09-27T10:00:00+00:00",
            "reporter": "mon-1",
        }

    def _make_event(self, urgency="low", station=None):
        payload = self._event_payload(urgency)
        if station:
            payload["station_id"] = station
        item = self.service.create_item(payload, "analyst-1", "analyst")
        item = self.service.act(item["id"], "assess", {}, "analyst-1", "analyst", item["version"])
        item = self.service.act(
            item["id"], "locate", {"location": "cell-1", "confidence": 0.9},
            "field-1", "field_operator", item["version"],
        )
        return item

    def _suspend(self, item, code="REG-NORTH-1"):
        return self.service.act(
            item["id"], "suspend", {"authorization_code": code},
            "coord-1", "coordinator", item["version"], "north",
        )

    def _coordinate(self, item):
        return self.service.act(
            item["id"], "coordinate", {"coordination_agreement": "AGC-%s" % item["id"]},
            "coord-1", "coordinator", item["version"], "north",
        )

    def _resolve(self, item_id):
        item = self.service.get_item(item_id)
        item = self._coordinate(item)
        return self.service.act(
            item_id, "resolve", {"measurement_cleared": True, "evidence": "scan"},
            "coord-1", "coordinator", item["version"], "north",
        )

    def test_old_event_without_occupancy_is_unoccupied(self):
        item = self._make_event()
        seat = self.service.seat_occupancy(item["id"])
        self.assertEqual(seat["status"], "unoccupied")
        self.assertFalse(seat["authorization_effective"])
        state = self.service.seat_state()
        self.assertEqual(state["capacity"], SEAT_CAPACITY)

    def test_suspend_holds_seat_but_authorization_not_effective_until_confirm(self):
        item = self._make_event()
        result = self._suspend(item)
        self.assertEqual(result["seat"]["status"], "held")
        self.assertIsNotNone(result["seat"]["seat_no"])
        self.assertFalse(result["seat"]["authorization_effective"])
        # 确认后授权才生效
        confirmed = self.service.confirm_seat(item["id"], "coord-1", "coordinator")
        self.assertTrue(confirmed["authorization_effective"])
        self.assertIsNotNone(confirmed["confirmed_at"])
        # 页面读到的占用也变为已生效
        seat = self.service.seat_occupancy(item["id"])
        self.assertTrue(seat["authorization_effective"])

    def test_confirm_requires_coordinator_role(self):
        item = self._make_event()
        self._suspend(item)
        with self.assertRaises(DomainError) as ctx:
            self.service.confirm_seat(item["id"], "analyst-1", "analyst")
        self.assertEqual(ctx.exception.status, 403)

    def test_full_capacity_waitlists_with_earliest_release_and_position(self):
        items = [self._make_event("low", "ST-%d" % i) for i in range(SEAT_CAPACITY)]
        for it in items:
            r = self._suspend(it)
            self.assertEqual(r["seat"]["status"], "held")
        state = self.service.seat_state()
        self.assertEqual(state["held"], SEAT_CAPACITY)
        self.assertEqual(state["remaining"], 0)
        # 第 4 个事件候补
        extra = self._make_event("low", "ST-EXTRA")
        r = self._suspend(extra)
        self.assertEqual(r["seat"]["status"], "waitlisted")
        self.assertEqual(r["seat"]["position"], 1)
        self.assertIsNotNone(r["seat"]["earliest_release_at"])
        state = self.service.seat_state()
        self.assertEqual(state["waitlisted"], 1)
        self.assertEqual(state["remaining"], 0)

    def test_waitlist_priority_urgency_then_submission_order(self):
        # 3 个低等级占满
        low_items = [self._make_event("low", "ST-L%d" % i) for i in range(SEAT_CAPACITY)]
        for it in low_items:
            self._suspend(it)
        # 高等级候补
        high = self._make_event("high", "ST-HIGH")
        self._suspend(high)
        # 低等级候补（更晚提交）
        low_late = self._make_event("low", "ST-LOWLATE")
        self._suspend(low_late)
        ledger = self.service.seat_ledger()
        wait = [r for r in ledger if r["status"] == "waitlisted"]
        # 高等级排第 1 位
        self.assertEqual(wait[0]["item_id"], high["id"])
        self.assertEqual(wait[0]["position"], 1)
        # 释放一个席位后，高等级优先获得占位
        self._resolve(low_items[0]["id"])
        high_seat = self.service.seat_occupancy(high["id"])
        self.assertEqual(high_seat["status"], "held")
        self.assertIsNotNone(high_seat["seat_no"])
        # 晚到的低等级仍候补
        self.assertEqual(self.service.seat_occupancy(low_late["id"])["status"], "waitlisted")

    def test_same_urgency_fifo(self):
        low_items = [self._make_event("low", "ST-L%d" % i) for i in range(SEAT_CAPACITY)]
        for it in low_items:
            self._suspend(it)
        w1 = self._make_event("low", "ST-W1")
        w2 = self._make_event("low", "ST-W2")
        self._suspend(w1)
        self._suspend(w2)
        wait = [r for r in self.service.seat_ledger() if r["status"] == "waitlisted"]
        self.assertEqual([w["item_id"] for w in wait], [w1["id"], w2["id"]])

    def test_concurrent_confirm_only_one_succeeds(self):
        item = self._make_event()
        self._suspend(item)
        first = self.service.confirm_seat(item["id"], "coord-1", "coordinator")
        self.assertTrue(first["confirmed"])
        with self.assertRaises(ConflictError) as ctx:
            self.service.confirm_seat(item["id"], "coord-2", "coordinator")
        # 后到者看到最新占用
        self.assertIsNotNone(ctx.exception.latest)
        self.assertTrue(ctx.exception.latest["authorization_effective"])

    def test_release_only_withdraws_that_event_and_promotes_waitlist(self):
        holders = [self._make_event("low", "ST-H%d" % i) for i in range(SEAT_CAPACITY)]
        for it in holders:
            self._suspend(it)
        waiter = self._make_event("low", "ST-W")
        self._suspend(waiter)
        # 释放前：holder[1] 仍占用
        before = self.service.seat_occupancy(holders[1]["id"])
        self.assertEqual(before["status"], "held")
        # resolve holder[0] -> 只释放它的席位，其余继续沿用，候补被提升
        self._resolve(holders[0]["id"])
        self.assertEqual(self.service.seat_occupancy(holders[0]["id"])["status"], "released")
        # 其余事件继续沿用
        self.assertEqual(self.service.seat_occupancy(holders[1]["id"])["status"], "held")
        self.assertEqual(self.service.seat_occupancy(holders[2]["id"])["status"], "held")
        # 候补被提升
        self.assertEqual(self.service.seat_occupancy(waiter["id"])["status"], "held")

    def test_cancel_withdraws_that_event(self):
        holders = [self._make_event("low", "ST-H%d" % i) for i in range(SEAT_CAPACITY)]
        for it in holders:
            self._suspend(it)
        waiter = self._make_event("low", "ST-W")
        self._suspend(waiter)
        fresh = self.service.get_item(holders[0]["id"])
        self.service.act(
            fresh["id"], "cancel", {"reason": "误报"},
            "coord-1", "coordinator", fresh["version"], "north",
        )
        self.assertEqual(self.service.seat_occupancy(holders[0]["id"])["status"], "withdrawn")
        self.assertEqual(self.service.seat_occupancy(waiter["id"])["status"], "held")

    def test_unauthorized_withdraw_scoped_to_event(self):
        holders = [self._make_event("low", "ST-H%d" % i) for i in range(SEAT_CAPACITY)]
        for it in holders:
            self._suspend(it)
        waiter = self._make_event("low", "ST-W")
        self._suspend(waiter)
        self.service.withdraw_unauthorized(holders[0]["id"], {"reason": "越权占位"}, "reg-1", "regulator")
        self.assertEqual(self.service.seat_occupancy(holders[0]["id"])["status"], "withdrawn")
        # 其余事件继续沿用
        self.assertEqual(self.service.seat_occupancy(holders[1]["id"])["status"], "held")
        self.assertEqual(self.service.seat_occupancy(holders[2]["id"])["status"], "held")
        self.assertEqual(self.service.seat_occupancy(waiter["id"])["status"], "held")

    def test_waitlisted_event_blocked_from_coordinate_and_resolve(self):
        holders = [self._make_event("low", "ST-H%d" % i) for i in range(SEAT_CAPACITY)]
        for it in holders:
            self._suspend(it)
        waiter = self._make_event("low", "ST-W")
        suspended = self._suspend(waiter)
        self.assertEqual(suspended["seat"]["status"], "waitlisted")
        with self.assertRaises(DomainError) as ctx:
            self.service.act(
                waiter["id"], "coordinate", {"coordination_agreement": "AGC-1"},
                "coord-1", "coordinator", waiter["version"], "north",
            )
        self.assertEqual(ctx.exception.code, "seat_waitlisted")
        # 页面给出阻塞原因
        item = self.service.get_item(waiter["id"])
        self.assertIsNotNone(item["blocking_reason"])

    def test_apply_seat_is_idempotent_no_duplicate_occupancy_or_audit(self):
        item = self._make_event()
        self._suspend(item)
        # 重试同一申请
        again = self.service.apply_seat(item["id"], {"authorization_code": "REG-NORTH-1"}, "coord-1", "coordinator")
        self.assertEqual(again["status"], "held")
        occ_rows = self.repo.connect().execute(
            "SELECT COUNT(*) AS c FROM occupancy WHERE item_id=? AND status IN ('held','waitlisted')",
            (item["id"],),
        ).fetchone()["c"]
        self.assertEqual(occ_rows, 1)
        audit = self.repo.audit_trail(item["id"])
        seat_audits = [e for e in audit if e["event_type"] == "seat_held"]
        self.assertEqual(len(seat_audits), 1)

    def test_operation_steps_record_failure_and_retry_count(self):
        item = self._make_event()
        self._suspend(item)
        # 第一次确认成功；另一位确认人提交会因已确认而冲突（失败步骤被记录）
        self.service.confirm_seat(item["id"], "coord-1", "coordinator")
        with self.assertRaises(ConflictError):
            self.service.confirm_seat(item["id"], "coord-2", "coordinator")
        conn = self.repo.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM operation_steps WHERE operation_key=? ORDER BY id",
                ("seat_confirm:%s:coord-2" % item["id"],),
            ).fetchall()
        finally:
            conn.close()
        # 第二位确认人的步骤失败，且保留了重试次数
        statuses = [(r["step"], r["status"], r["attempts"]) for r in rows]
        self.assertTrue(any(s == "failed" and a >= 1 for _, s, a in statuses))
        # 同一确认人重试第一次确认：幂等成功，不重复占位/审计
        again = self.service.confirm_seat(item["id"], "coord-1", "coordinator")
        self.assertTrue(again["confirmed"])

    def test_state_shows_remaining_waitlist_and_blocking(self):
        holders = [self._make_event("low", "ST-H%d" % i) for i in range(SEAT_CAPACITY)]
        for it in holders:
            self._suspend(it)
        waiter = self._make_event("low", "ST-W")
        self._suspend(waiter)
        state = self.service.state()
        self.assertIn("seats", state)
        self.assertEqual(state["seats"]["remaining"], 0)
        self.assertEqual(state["seats"]["waitlisted"], 1)
        # 事件列表带阻塞原因
        items = self.service.list_items()
        blocked = [i for i in items if i["blocking_reason"]]
        self.assertEqual(len(blocked), 1)


if __name__ == "__main__":
    unittest.main()
