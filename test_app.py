#!/usr/bin/env python3
"""cat_app DB 계층 테스트 — python test_app.py (Windows 없이 동작)"""

import sqlite3
import unittest

from cat_app import connect, load_rules, save_session, top_apps_today
from watch import Session, WindowInfo, now_iso, pick_rule

SHORTS = WindowInfo("웃긴영상 - YouTube - Chrome", "chrome.exe", "https://www.youtube.com/shorts/x")
NORMAL = WindowInfo("강의 - YouTube - Chrome", "chrome.exe", "https://www.youtube.com/watch?v=a")


class TestDb(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")

    def test_rules_survive_db_round_trip(self):
        """DB에서 읽은 규칙이 스파이크 기본 규칙과 똑같이 판정해야 한다 (AND/OR 그룹 포함)."""
        rules = load_rules(self.db)
        self.assertEqual(pick_rule(rules, SHORTS).action, "close")
        self.assertEqual(pick_rule(rules, NORMAL).action, "warn")

    def test_default_rules_seeded(self):
        n = self.db.execute("SELECT COUNT(*) FROM block_rule").fetchone()[0]
        self.assertEqual(n, 3)

    def test_disabled_rule_is_ignored(self):
        self.db.execute("UPDATE block_rule SET enabled = 0 WHERE action = 'close'")
        self.assertEqual(pick_rule(load_rules(self.db), SHORTS).action, "warn")

    def test_bad_action_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO block_rule (rule_id, name, action) VALUES ('x', 'x', 'explode')")

    def test_top_apps_skips_idle_and_stores_host_only(self):
        t = now_iso()
        save_session(self.db, Session(t, "Code.exe", "main.py", None, 30.0, False, t))
        save_session(self.db, Session(t, "chrome.exe", "yt", "https://youtube.com/shorts/x?t=1", 10.0, False, t))
        save_session(self.db, Session(t, "chrome.exe", "yt", None, 999.0, True, t))   # 자리 비움
        self.assertEqual(top_apps_today(self.db), [("Code.exe", 30.0), ("chrome.exe", 10.0)])
        hosts = [h for (h,) in self.db.execute("SELECT url_host FROM usage_session WHERE url_host IS NOT NULL")]
        self.assertEqual(hosts, ["youtube.com"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
