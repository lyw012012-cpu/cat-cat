#!/usr/bin/env python3
"""
규칙 엔진 테스트 — python3 test_rules.py

여기서 검증하는 것은 "코드가 돌아가는가"가 아니라
**설계서의 rule_condition 테이블 구조가 실제로 쓸 만한가** 이다.

  · 같은 group_no  → OR
  · 다른 group_no  → AND
  · 창 제목만으로는 쇼츠를 구분할 수 없다 (이 스파이크의 존재 이유)

Windows 없이 어디서나 돌아간다.
"""

import unittest

from watch import (
    Condition,
    Rule,
    WindowInfo,
    _host,
    pick_rule,
    rule_matches,
    DEFAULT_RULES,
)


SHORTS = WindowInfo(
    title="웃긴영상 - YouTube - Chrome",
    exe="chrome.exe",
    url="https://www.youtube.com/shorts/xyz789",
)
NORMAL_VIDEO = WindowInfo(
    title="강의영상 - YouTube - Chrome",      # 제목이 쇼츠와 구분되지 않는다
    exe="chrome.exe",
    url="https://www.youtube.com/watch?v=abc",
)
VSCODE = WindowInfo(
    title="main.py - cat-app - Visual Studio Code",
    exe="Code.exe",
    url=None,
)


class TestGroupSemantics(unittest.TestCase):
    """AND / OR 조합이 의도대로 동작하는가"""

    def test_or_within_same_group(self):
        rule = Rule("r", "숏폼", "close", 10, (
            Condition(0, "url", "contains", "tiktok.com"),
            Condition(0, "url", "contains", "youtube.com/shorts"),
        ))
        # 둘 중 하나만 맞아도 발동해야 한다
        self.assertTrue(rule_matches(rule, SHORTS))
        self.assertFalse(rule_matches(rule, NORMAL_VIDEO))

    def test_and_across_groups(self):
        rule = Rule("r", "크롬에서 유튜브", "warn", 50, (
            Condition(0, "app", "eq", "chrome.exe"),
            Condition(1, "url", "contains", "youtube.com"),
        ))
        self.assertTrue(rule_matches(rule, NORMAL_VIDEO))

        # 앱 조건이 어긋나면 URL이 맞아도 발동하면 안 된다
        firefox = WindowInfo("YouTube - Firefox", "firefox.exe",
                             "https://www.youtube.com/watch?v=abc")
        self.assertFalse(rule_matches(rule, firefox))

    def test_missing_subject_is_false(self):
        """URL이 없는 창(메모장 등)에 url 조건을 걸면 그냥 안 맞아야 한다."""
        rule = Rule("r", "유튜브", "close", 10, (
            Condition(0, "url", "contains", "youtube.com"),
        ))
        self.assertFalse(rule_matches(rule, VSCODE))


class TestWhyThisSpikeExists(unittest.TestCase):
    """★ 창 제목만으로는 쇼츠를 구분할 수 없다는 사실의 증명"""

    def test_title_cannot_distinguish_shorts(self):
        title_rule = Rule("r", "제목으로 쇼츠 찾기", "close", 10, (
            Condition(0, "window_title", "contains", "YouTube"),
        ))
        # 제목 기준이면 일반 영상까지 같이 걸린다 → 오탐
        self.assertTrue(rule_matches(title_rule, SHORTS))
        self.assertTrue(rule_matches(title_rule, NORMAL_VIDEO))

    def test_url_can_distinguish_shorts(self):
        url_rule = Rule("r", "URL로 쇼츠 찾기", "close", 10, (
            Condition(0, "url", "contains", "youtube.com/shorts"),
        ))
        # URL이 있어야만 정확히 쇼츠만 잡힌다
        self.assertTrue(rule_matches(url_rule, SHORTS))
        self.assertFalse(rule_matches(url_rule, NORMAL_VIDEO))


class TestPriority(unittest.TestCase):
    def test_lowest_priority_number_wins(self):
        """쇼츠는 close(10)와 warn(50) 양쪽에 걸린다. close가 이겨야 한다."""
        picked = pick_rule(DEFAULT_RULES, SHORTS)
        self.assertIsNotNone(picked)
        self.assertEqual(picked.action, "close")

    def test_lecture_video_is_not_caught(self):
        """제목에 '강의'가 있으면 공부 용도 → 유튜브 시간 규칙에서 빠진다 (not_regex)."""
        self.assertIsNone(pick_rule(DEFAULT_RULES, NORMAL_VIDEO))

    def test_fun_video_is_caught_by_youtube_rule(self):
        fun = WindowInfo("웃긴 고양이 모음 - YouTube - Chrome", "chrome.exe",
                         "https://www.youtube.com/watch?v=zzz")
        self.assertEqual(pick_rule(DEFAULT_RULES, fun).rule_id, "r_yt_warn")
        music = WindowInfo("공부할 때 듣는 lofi 플레이리스트 - YouTube - Chrome", "chrome.exe",
                           "https://www.youtube.com/watch?v=lofi")
        self.assertIsNone(pick_rule(DEFAULT_RULES, music))

    def test_no_rule_for_editor(self):
        self.assertIsNone(pick_rule(DEFAULT_RULES, VSCODE))


class TestPrivacy(unittest.TestCase):
    """개인정보 최소화 — 전체 URL이 아니라 호스트만 남기는가"""

    def test_host_only(self):
        self.assertEqual(
            _host("https://www.youtube.com/shorts/xyz789?t=42"),
            "www.youtube.com")
        self.assertEqual(_host("youtube.com/watch"), "youtube.com")
        self.assertIsNone(_host(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
