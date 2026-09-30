import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


def located_item(service, suffix, region="north", strength_dbm=-35, reporter="m"):
    """创建并走完 assess/locate 的事件，返回最新 item。"""
    item = service.create_item({
        "frequency_mhz": 2400.0,
        "bandwidth_mhz": 1.0,
        "station_id": "ST-%s" % suffix,
        "region": region,
        "strength_dbm": strength_dbm,
        "detected_at": "2026-09-30T10:0%s:00+00:00" % suffix,
        "reporter": reporter,
    }, "m", "monitor")
    item = service.act(item["id"], "assess", {}, "a", "analyst", item["version"])
    item = service.act(item["id"], "locate", {"location": "cell-%s" % suffix, "confidence": 0.9},
                       "f", "field_operator", item["version"])
    return service.get_item(item["id"])


class OccupancyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.repo.set_pool_capacity("north", 2, "reg-1")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_apply_holds_seat_and_waits_when_full_with_earliest_release(self):
        a = located_item(self.service, "1")   # critical
        b = located_item(self.service, "2")   # critical
        c = located_item(self.service, "3")   # critical
        occ_a = self.service.apply_occupancy(a["id"], {"authorization_code": "REG-1",
                                                        "expected_release_at": "2026-09-30T12:00:00+00:00"},
                                             "coord", "coordinator", "north")
        occ_b = self.service.apply_occupancy(b["id"], {"authorization_code": "REG-2",
                                                        "expected_release_at": "2026-09-30T11:00:00+00:00"},
                                             "coord", "coordinator", "north")
        self.assertEqual({occ_a["state"], occ_b["state"]}, {"held"})
        occ_c = self.service.apply_occupancy(c["id"], {"authorization_code": "REG-3"},
                                             "coord", "coordinator", "north")
        self.assertEqual(occ_c["state"], "waiting")
        # 候补返回最早释放时间（取占用者中最早的）
        self.assertEqual(occ_c["earliest_release_at"], "2026-09-30T11:00:00+00:00")
        self.assertEqual(occ_c["waiting_ahead"], 0)

    def test_waitlist_ordered_by_urgency_then_submission(self):
        # 占满 2 席：critical、critical
        held1 = located_item(self.service, "1", strength_dbm=-30)
        held2 = located_item(self.service, "2", strength_dbm=-30)
        self.service.apply_occupancy(held1["id"], {"authorization_code": "REG-1"}, "c", "coordinator", "north")
        self.service.apply_occupancy(held2["id"], {"authorization_code": "REG-2"}, "c", "coordinator", "north")
        # 候补：先 medium，后 high；high 应排到 medium 前面
        medium = located_item(self.service, "3", strength_dbm=-85)
        high = located_item(self.service, "4", strength_dbm=-70)
        wait_medium = self.service.apply_occupancy(medium["id"], {"authorization_code": "REG-3"},
                                                   "c", "coordinator", "north")
        wait_high = self.service.apply_occupancy(high["id"], {"authorization_code": "REG-4"},
                                                 "c", "coordinator", "north")
        self.assertEqual(wait_high["waiting_ahead"], 0)
        overview = self.service.seats_overview("north")
        self.assertEqual([w["item_id"] for w in overview["waiting"]],
                         [high["id"], medium["id"]])
        self.assertEqual([w["waiting_ahead"] for w in overview["waiting"]], [0, 1])
        # 余量、阻塞原因可见
        self.assertEqual(overview["remaining"], 0)
        self.assertIn("席位已满", overview["waiting"][0]["blocked_reason"])
        self.assertIn("最早释放时间", overview["waiting"][1]["blocked_reason"])

    def test_promotion_on_release_respects_priority(self):
        held1 = located_item(self.service, "1", strength_dbm=-30)
        held2 = located_item(self.service, "2", strength_dbm=-30)
        self.service.apply_occupancy(held1["id"], {"authorization_code": "REG-1"}, "c", "coordinator", "north")
        self.service.apply_occupancy(held2["id"], {"authorization_code": "REG-2"}, "c", "coordinator", "north")
        medium = located_item(self.service, "3", strength_dbm=-85)
        high = located_item(self.service, "4", strength_dbm=-70)
        self.service.apply_occupancy(medium["id"], {"authorization_code": "REG-3"}, "c", "coordinator", "north")
        self.service.apply_occupancy(high["id"], {"authorization_code": "REG-4"}, "c", "coordinator", "north")
        result = self.service.release_occupancy(held1["id"], {"reason": "提前结束"}, "c", "coordinator", "north")
        self.assertEqual(result["promoted"][0]["item_id"], high["id"])
        high_occ = self.service.get_occupancy(high["id"])
        self.assertEqual(high_occ["state"], "held")
        medium_occ = self.service.get_occupancy(medium["id"])
        self.assertEqual(medium_occ["state"], "waiting")

    def test_confirmation_required_for_authorization_effect(self):
        item = located_item(self.service, "1")
        occ = self.service.apply_occupancy(item["id"], {"authorization_code": "REG-9"}, "c", "coordinator", "north")
        self.assertEqual(occ["state"], "held")
        # 申请后授权未生效：事件仍停在 located
        self.assertEqual(self.service.get_item(item["id"])["status"], "located")
        occ = self.service.confirm_occupancy(item["id"], {}, "c", "coordinator", "north")
        self.assertEqual(occ["state"], "confirmed")
        self.assertEqual(self.service.get_item(item["id"])["status"], "suspended")
        # 不能重复确认
        with self.assertRaises(ConflictError) as again:
            self.service.confirm_occupancy(item["id"], {}, "c", "coordinator", "north")
        self.assertEqual(again.exception.code, "already_confirmed")

    def test_two_simultaneous_confirmations_only_one_wins(self):
        first = located_item(self.service, "1")
        second = located_item(self.service, "2")
        self.service.apply_occupancy(first["id"], {"authorization_code": "REG-1"}, "c", "coordinator", "north")
        self.service.apply_occupancy(second["id"], {"authorization_code": "REG-2"}, "c", "coordinator", "north")
        # 两个 held 事件都尝试确认同一席位 SEAT-1
        errors = []
        results = []
        barrier = threading.Barrier(2)

        def confirm(item_id):
            barrier.wait()
            try:
                results.append(self.service.confirm_occupancy(
                    item_id, {"seat": "SEAT-1"}, "c", "coordinator", "north"))
            except ConflictError as exc:
                errors.append(exc)

        t1 = threading.Thread(target=confirm, args=(first["id"],))
        t2 = threading.Thread(target=confirm, args=(second["id"],))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "seat_taken")
        # 后到者看到最新占用（当时可能仍 held，也可能已 confirmed）
        holder = errors[0].details
        self.assertEqual(holder["seat"], "SEAT-1")
        self.assertIn(holder["state"], ("held", "confirmed"))
        winner = results[0]
        self.assertEqual(winner["seat"], "SEAT-1")
        # 最终账上 SEAT-1 只有一个 confirmed，另一事件保留在自己的 held 席位
        overview = self.service.seats_overview("north")
        seat1 = [a for a in overview["active"] if a["seat"] == "SEAT-1"]
        self.assertEqual(len(seat1), 1)
        self.assertEqual(seat1[0]["state"], "confirmed")
        self.assertEqual(sum(1 for a in overview["active"] if a["state"] == "confirmed"), 1)

    def test_release_only_withdraws_that_events_writes(self):
        first = located_item(self.service, "1")
        second = located_item(self.service, "2")
        occ1 = self.service.apply_occupancy(first["id"], {"authorization_code": "REG-1"},
                                            "c", "coordinator", "north")
        occ2 = self.service.apply_occupancy(second["id"], {"authorization_code": "REG-2"},
                                            "c", "coordinator", "north")
        self.service.confirm_occupancy(first["id"], {}, "c", "coordinator", "north")
        self.service.confirm_occupancy(second["id"], {}, "c", "coordinator", "north")
        result = self.service.release_occupancy(first["id"], {"reason": "误操作"}, "c", "coordinator", "north")
        self.assertTrue(result["released"])
        # 第一个事件回到 located、席位释放；第二个事件继续沿用
        self.assertEqual(self.service.get_item(first["id"])["status"], "located")
        self.assertEqual(self.service.get_occupancy(first["id"])["state"], "unoccupied")
        second_after = self.service.get_occupancy(second["id"])
        self.assertEqual(second_after["state"], "confirmed")
        self.assertEqual(second_after["seat"], occ2["seat"])
        self.assertEqual(self.service.get_item(second["id"])["status"], "suspended")
        # 释放出的席位可再被申请
        reused = located_item(self.service, "9")
        occ3 = self.service.apply_occupancy(reused["id"], {"authorization_code": "REG-3"},
                                            "c", "coordinator", "north")
        self.assertEqual(occ3["state"], "held")
        self.assertIn(occ3["seat"], {occ1["seat"], occ2["seat"]})
        self.assertNotEqual(occ3["seat"], occ2["seat"])

    def test_revoke_overreach_only_affects_target_event(self):
        first = located_item(self.service, "1")
        second = located_item(self.service, "2")
        self.service.apply_occupancy(first["id"], {"authorization_code": "REG-1"}, "c", "coordinator", "north")
        self.service.apply_occupancy(second["id"], {"authorization_code": "REG-2"}, "c", "coordinator", "north")
        self.service.confirm_occupancy(first["id"], {}, "c", "coordinator", "north")
        self.service.confirm_occupancy(second["id"], {}, "c", "coordinator", "north")
        # 监管员事后发现第一个事件越权
        result = self.service.revoke_occupancy(first["id"], {"reason": "跨区越权处置"},
                                               "reg", "regulator", "north")
        self.assertEqual(result["state"], "revoked")
        self.assertEqual(self.service.get_item(first["id"])["status"], "located")
        self.assertEqual(self.service.get_item(second["id"])["status"], "suspended")
        # 协调员不能认定越权
        with self.assertRaises(DomainError) as denied:
            self.service.revoke_occupancy(second["id"], {"reason": "x"}, "c", "coordinator", "north")
        self.assertEqual(denied.exception.status, 403)

    def test_duplicate_authorization_is_rejected(self):
        first = located_item(self.service, "1")
        second = located_item(self.service, "2")
        self.service.apply_occupancy(first["id"], {"authorization_code": "REG-DUP"},
                                     "c", "coordinator", "north")
        with self.assertRaises(ConflictError) as dup:
            self.service.apply_occupancy(second["id"], {"authorization_code": "REG-DUP"},
                                         "c", "coordinator", "north")
        self.assertEqual(dup.exception.code, "authorization_in_use")

    def test_failed_write_keeps_steps_and_retry_does_not_double_occupy(self):
        item = located_item(self.service, "1")
        # 在申请记录提交前制造一次故障
        self.repo.fail_once("apply:record")
        with self.assertRaises(ConflictError) as failed:
            self.service.apply_occupancy(item["id"], {"authorization_code": "REG-FAIL"},
                                         "c", "coordinator", "north")
        self.assertEqual(failed.exception.code, "occupancy_apply_incomplete")
        op = failed.exception.details["operation"]
        self.assertEqual(op["steps"][0]["state"], "pending")
        self.assertEqual(op["steps"][0]["attempts"], 1)
        # 重试：同一操作继续跑，不产生第二条占用记录
        view = self.service.resume_occupancy(op["op_id"], "c", "coordinator", "north")
        self.assertEqual(view["state"], "held")
        rows = self.repo.connect().execute(
            "SELECT COUNT(*) AS total FROM occupancy WHERE item_id=?", (item["id"],)
        ).fetchone()["total"]
        self.assertEqual(rows, 1)
        # 未执行确认，不产生确认审计
        audits = self.repo.audit_trail(item["id"])
        self.assertEqual(len([a for a in audits if a["event_type"] == "occupancy_confirmed"]), 0)

    def test_failure_after_release_commit_resumes_without_double_audit(self):
        first = located_item(self.service, "1")
        second = located_item(self.service, "2")
        third = located_item(self.service, "3")
        occ1 = self.service.apply_occupancy(first["id"], {"authorization_code": "REG-1"}, "c", "coordinator", "north")
        self.service.apply_occupancy(second["id"], {"authorization_code": "REG-2"}, "c", "coordinator", "north")
        self.service.confirm_occupancy(first["id"], {}, "c", "coordinator", "north")
        self.service.confirm_occupancy(second["id"], {}, "c", "coordinator", "north")
        # 容量 2：第三个事件候补，释放后应被提补一次
        wait = self.service.apply_occupancy(third["id"], {"authorization_code": "REG-3"},
                                            "c", "coordinator", "north")
        self.assertEqual(wait["state"], "waiting")
        # release 已提交、步骤状态未落账时崩溃
        self.repo.fail_once("release:after_commit")
        with self.assertRaises(ConflictError) as failed:
            self.service.release_occupancy(first["id"], {"reason": "撤回"}, "c", "coordinator", "north")
        op = failed.exception.details["operation"]
        states = {s["step"]: s["state"] for s in op["steps"]}
        self.assertEqual(states["release"], "pending")
        self.service.resume_occupancy(op["op_id"], "c", "coordinator", "north")
        self.assertEqual(self.service.get_item(first["id"])["status"], "located")
        # 释放与提补在第一次提交时已完成，重试不重复
        release_audits = [a for a in self.repo.audit_trail(first["id"])
                          if a["event_type"] == "occupancy_release"]
        withdraw_audits = [a for a in self.repo.audit_trail(first["id"])
                           if a["event_type"] == "authorization_withdrawn"]
        promoted_audits = [a for a in self.repo.audit_trail(third["id"])
                           if a["event_type"] == "occupancy_promoted"]
        self.assertEqual(len(release_audits), 1)
        self.assertEqual(len(withdraw_audits), 1)
        self.assertEqual(len(promoted_audits), 1)
        self.assertEqual(self.service.get_occupancy(third["id"])["state"], "held")
        self.assertEqual(self.service.get_occupancy(third["id"])["seat"], occ1["seat"])
        # 第二事件不受影响
        self.assertEqual(self.service.get_item(second["id"])["status"], "suspended")

    def test_confirm_crash_after_commit_resumes_without_double_audit(self):
        item = located_item(self.service, "1")
        self.service.apply_occupancy(item["id"], {"authorization_code": "REG-1"}, "c", "coordinator", "north")
        self.repo.fail_once("confirm:after_commit")
        with self.assertRaises(ConflictError) as failed:
            self.service.confirm_occupancy(item["id"], {}, "c", "coordinator", "north")
        self.assertEqual(failed.exception.code, "occupancy_confirm_incomplete")
        op = failed.exception.details["operation"]
        self.assertEqual(op["steps"][0]["state"], "pending")
        # 确认已经提交：事件已 suspended，重试只是沿用结果
        self.assertEqual(self.service.get_item(item["id"])["status"], "suspended")
        view = self.service.resume_occupancy(op["op_id"], "c", "coordinator", "north")
        self.assertEqual(view["state"], "confirmed")
        audits = [a for a in self.repo.audit_trail(item["id"])
                  if a["event_type"] == "occupancy_confirmed"]
        self.assertEqual(len(audits), 1)
        # 全新的确认操作识别为 already_confirmed，而不是再写授权
        with self.assertRaises(ConflictError) as again:
            self.service.confirm_occupancy(item["id"], {}, "c", "coordinator", "north")
        self.assertEqual(again.exception.code, "already_confirmed")

    def test_legacy_item_without_occupancy_reads_as_unoccupied(self):
        item = located_item(self.service, "1")
        view = self.service.get_item(item["id"])
        self.assertEqual(view["occupancy"]["state"], "unoccupied")
        self.assertIsNone(view["occupancy"]["authorization_code"])

    def test_cancel_located_item_releases_held_seat_and_promotes(self):
        held1 = located_item(self.service, "1", strength_dbm=-30)
        held2 = located_item(self.service, "2", strength_dbm=-30)
        waiting = located_item(self.service, "3", strength_dbm=-70)
        self.service.apply_occupancy(held1["id"], {"authorization_code": "REG-1"}, "c", "coordinator", "north")
        self.service.apply_occupancy(held2["id"], {"authorization_code": "REG-2"}, "c", "coordinator", "north")
        self.service.apply_occupancy(waiting["id"], {"authorization_code": "REG-3"}, "c", "coordinator", "north")
        item = self.service.get_item(held1["id"])
        cancelled = self.service.act(held1["id"], "cancel", {"reason": "误报"},
                                     "c", "coordinator", item["version"], "north")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["occupancy"]["state"], "unoccupied")
        self.assertEqual(cancelled["occupancy_release"]["promoted"][0]["item_id"], waiting["id"])


if __name__ == "__main__":
    unittest.main()
