import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_complete_interference_workflow(self):
        item = self.service.create_item({
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 20.0,
            "station_id": "ST-01",
            "region": "north",
            "strength_dbm": -35,
            "detected_at": "2026-09-27T10:00:00+00:00",
            "reporter": "monitor-1",
        }, "analyst-1", "analyst")
        item = self.service.act(item["id"], "assess", {}, "analyst-1", "analyst", item["version"])
        self.assertEqual(item["payload"]["assessment"]["level"], "critical")
        item = self.service.act(item["id"], "locate", {"location": "cell-7", "confidence": 0.9}, "field-1", "field_operator", item["version"])
        occupancy = self.service.apply_occupancy(
            item["id"], {"authorization_code": "REG-NORTH-1"}, "coord-1", "coordinator", "north"
        )
        self.assertEqual(occupancy["state"], "held")
        occupancy = self.service.confirm_occupancy(
            item["id"], {}, "coord-1", "coordinator", "north"
        )
        self.assertEqual(occupancy["state"], "confirmed")
        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "suspended")
        item = self.service.act(item["id"], "coordinate", {"coordination_agreement": "AGC-7"}, "coord-1", "coordinator", item["version"], "north")
        item = self.service.act(item["id"], "resolve", {"measurement_cleared": True, "evidence": "scan-7"}, "coord-1", "coordinator", item["version"], "north")
        self.assertEqual(item["status"], "resolved")
        self.assertGreaterEqual(len(item["audit"]), 7)
        # 结案后该事件席位已释放，旧授权不再占位
        self.assertEqual(item["occupancy"]["state"], "unoccupied")


if __name__ == "__main__":
    unittest.main()
