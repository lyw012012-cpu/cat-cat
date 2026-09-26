#!/usr/bin/env python3
"""
집사 고양이 — 스파이크 #1 : 활성 창 감지 / URL 추출 / 규칙 판정

이 스파이크가 답하려는 질문 세 개
  Q1. 지금 사용자가 보고 있는 창이 무엇인지 알아낼 수 있는가?
  Q2. 그게 "유튜브 쇼츠"인지 구분할 수 있는가?   ← 진짜 난관
  Q3. 그 창을 닫을 수 있는가?                    ← 두 번째 난관

DB도 UI도 고양이도 없다. 콘솔에 찍고 JSONL 파일에 남기는 게 전부다.

--------------------------------------------------------------------------
구조
    [OS 의존 부분]  WindowsProbe / SimulatedProbe   ← 갈아끼울 수 있게 분리
           │  WindowInfo(title, exe, url, hwnd)
           ▼
    [로직 부분]     SessionTracker  (usage_session 테이블과 같은 모양)
                    RuleEngine      (block_rule + rule_condition 과 같은 모양)

    OS 부분은 Windows에서만 돌지만, 로직 부분은 --simulate 로 어디서든 테스트된다.
    설계서의 테이블 구조가 실제 코드에서 그대로 쓰인다는 것도 같이 확인하게 된다.
--------------------------------------------------------------------------

실행
    python watch.py --simulate          # 아무 OS에서나. 로직 검증용
    python watch.py                     # Windows. 실제 감지
    python watch.py --action close      # 실제로 창을 닫아본다 (주의!)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

UTC = timezone.utc


def now_iso() -> str:
    """스키마 규약과 동일하게 UTC ISO-8601로 찍는다."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# =============================================================================
#  OS에서 읽어온 '지금 창'의 모습
# =============================================================================

@dataclass(frozen=True)
class WindowInfo:
    title: str                      # 창 제목
    exe: str                        # 실행 파일명 (chrome.exe)
    url: Optional[str] = None       # 브라우저면 주소, 아니면 None
    hwnd: int = 0                   # 창 핸들 (닫을 때 필요)
    pid: int = 0                    # 프로세스 번호 (고양이 앱 자기 창을 알아보는 데 씀)
    video_kind: Optional[str] = None  # 유튜브 영상 종류: lecture/music/fun/ask, 아직 모르면 None

    @property
    def key(self) -> tuple:
        """
        이 값이 바뀌면 '다른 창으로 전환했다'고 본다.
        제목은 넣지 않는다 — 터미널 스피너·알림 개수처럼 제목만 깜빡이면 세션이
        1초 단위로 쪼개지기 때문. 브라우저는 URL이 바뀔 때만 새 세션이다.
        """
        return (self.exe, self.url)


# =============================================================================
#  OS 의존 부분 1 — Windows
# =============================================================================

class WindowsProbe:
    """
    Win32 API를 ctypes로 직접 호출한다. 외부 패키지 없이 창 제목과 프로세스명까지는
    확실히 얻을 수 있다. URL만 uiautomation 패키지가 있으면 추가로 시도한다.
    """

    WM_CLOSE = 0x0010
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # GetLastInputInfo용 구조체 — 자리 비움(idle) 판정에 쓴다.
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        self._LASTINPUTINFO = LASTINPUTINFO
        self._wintypes = wintypes

        # URL 추출은 선택 사항. 없으면 없는 대로 돌아간다.
        try:
            import uiautomation  # noqa: F401
            self._uia = uiautomation
            self._uia.SetGlobalSearchTimeout(0.5)   # 느려지면 안 되므로 짧게
        except ImportError:
            self._uia = None
            print("[!] uiautomation 미설치 — URL 추출은 건너뜁니다.")
            print("    pip install uiautomation")

    # ---- 창 정보 --------------------------------------------------------

    def probe(self) -> Optional[WindowInfo]:
        hwnd = self.user32.GetForegroundWindow()
        if not hwnd:
            return None

        title = self._window_title(hwnd)
        pid = self._window_pid(hwnd)
        exe = self._process_name(hwnd)
        url = self._browser_url(hwnd, exe)
        return WindowInfo(title=title, exe=exe, url=url, hwnd=hwnd, pid=pid)

    def _window_title(self, hwnd: int) -> str:
        length = self.user32.GetWindowTextLengthW(hwnd)
        buf = self.ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    def _window_pid(self, hwnd: int) -> int:
        pid = self._wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, self.ctypes.byref(pid))
        return pid.value

    def _process_name(self, hwnd: int) -> str:
        pid = self._wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, self.ctypes.byref(pid))
        handle = self.kernel32.OpenProcess(
            self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return "unknown"
        try:
            size = self._wintypes.DWORD(260)
            buf = self.ctypes.create_unicode_buffer(size.value)
            ok = self.kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, self.ctypes.byref(size))
            if not ok:
                return "unknown"
            return buf.value.rsplit("\\", 1)[-1]       # 전체 경로 → 파일명
        finally:
            self.kernel32.CloseHandle(handle)

    # ---- ★ 이 스파이크의 핵심: 브라우저 주소 읽기 -------------------------

    BROWSERS = {"chrome.exe", "msedge.exe", "whale.exe", "brave.exe"}

    def _browser_url(self, hwnd: int, exe: str) -> Optional[str]:
        """
        창 제목만으로는 유튜브 '쇼츠'인지 일반 영상인지 절대 알 수 없다.
        ("영상제목 - YouTube - Chrome" 은 둘 다 똑같이 생겼다)

        그래서 UI Automation으로 주소창(Edit 컨트롤)의 값을 직접 읽는다.
        이 방식의 한계를 반드시 직접 확인할 것:
          · 브라우저가 업데이트되면 트리 구조가 바뀌어 깨질 수 있다
          · 호출이 느리다 (수십~수백 ms). 1초 간격이면 버틴다
          · 주소창이 포커스를 잃으면 스킴(https://)이 생략돼 보인다
          · 여러 탭 중 '활성 탭'의 주소만 얻는다 — 백그라운드 탭은 모른다
        """
        if self._uia is None or exe not in self.BROWSERS:
            return None
        try:
            window = self._uia.ControlFromHandle(hwnd)
            if window is None:
                return None
            edit = window.EditControl(searchDepth=12)   # 주소 표시줄
            if not edit.Exists(0.3, 0.1):
                return None
            value = edit.GetValuePattern().Value
            return value or None
        except Exception as e:                           # noqa: BLE001
            # 스파이크이므로 삼키고 계속 간다. 어떤 예외가 나는지 기록해 둘 것.
            print(f"[url] 추출 실패: {type(e).__name__}: {e}")
            return None

    # ---- 자리 비움 ------------------------------------------------------

    def idle_seconds(self) -> float:
        lii = self._LASTINPUTINFO()
        lii.cbSize = self.ctypes.sizeof(lii)
        if not self.user32.GetLastInputInfo(self.ctypes.byref(lii)):
            return 0.0
        return (self.kernel32.GetTickCount() - lii.dwTime) / 1000.0

    # ---- ★ 두 번째 난관: 닫기 -------------------------------------------

    def close_window(self, hwnd: int) -> bool:
        """
        WM_CLOSE를 보낸다. 반드시 직접 확인할 것:
          · 크롬이면 '탭 하나'가 아니라 '창 전체'가 닫힌다.
            탭만 닫으려면 브라우저 확장 + 네이티브 메시징이 필요하다.
          · 저장 안 된 문서가 있는 앱은 '저장하시겠습니까?' 대화상자를 띄운다.
          · 관리자 권한으로 뜬 창은 일반 권한 앱이 닫을 수 없다(UIPI).
        """
        return bool(self.user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0))

    def mute_app(self, exe: str, mute: bool) -> bool:
        """그 앱(예: chrome.exe)의 소리만 끄거나 켠다 — Windows 볼륨 믹서와 같은 방식. pycaw 필요."""
        try:
            from pycaw.pycaw import AudioUtilities
        except ImportError:
            print("[!] pycaw 미설치 — 소리 끄기를 건너뜁니다.  pip install pycaw")
            return False
        found = False
        for s in AudioUtilities.GetAllSessions():
            if s.Process and s.Process.name().lower() == exe.lower():
                s.SimpleAudioVolume.SetMute(int(mute), None)
                found = True
        return found

    VK_CONTROL, VK_W, KEYEVENTF_KEYUP = 0x11, 0x57, 0x0002

    def close_tab(self, hwnd: int) -> bool:
        """
        브라우저의 '현재 탭'만 닫는다 — Ctrl+W 키 입력을 흉내낸다.
        키 입력은 '맨 앞 창'으로 가므로, 그 사이 사용자가 다른 창으로 옮겼다면
        엉뚱한 탭이 닫힌다. 그래서 보내기 직전에 맨 앞 창이 그대로인지 다시 확인한다.
        """
        if self.user32.GetForegroundWindow() != hwnd:
            return False
        for vk, flag in ((self.VK_CONTROL, 0), (self.VK_W, 0),
                         (self.VK_W, self.KEYEVENTF_KEYUP),
                         (self.VK_CONTROL, self.KEYEVENTF_KEYUP)):
            self.user32.keybd_event(vk, 0, flag, 0)
        return True


# =============================================================================
#  OS 의존 부분 2 — 시뮬레이터 (로직 테스트용, 아무 OS에서나 동작)
# =============================================================================

class SimulatedProbe:
    """가짜 창 전환 시나리오를 재생한다. OS 없이 로직만 검증할 때 쓴다."""

    SCENARIO = [
        # (제목, 실행파일, URL, 몇 틱 동안 유지할지)
        ("main.py - cat-app - Visual Studio Code", "Code.exe", None, 3),
        ("Google - Chrome", "chrome.exe", "https://www.google.com/", 1),
        ("일반 영상 - YouTube - Chrome", "chrome.exe",
         "https://www.youtube.com/watch?v=abc", 2),
        ("웃긴영상 - YouTube - Chrome", "chrome.exe",
         "https://www.youtube.com/shorts/xyz789", 3),   # ← 차단 대상
        ("Instagram - Chrome", "chrome.exe",
         "https://www.instagram.com/reels/", 2),        # ← 차단 대상
        ("main.py - cat-app - Visual Studio Code", "Code.exe", None, 2),
    ]

    def __init__(self) -> None:
        self._frames: list[WindowInfo] = []
        for title, exe, url, ticks in self.SCENARIO:
            self._frames += [WindowInfo(title, exe, url, hwnd=1234)] * ticks
        self._i = 0

    def probe(self) -> Optional[WindowInfo]:
        if self._i >= len(self._frames):
            return None                     # 시나리오 끝 → 루프 종료
        win = self._frames[self._i]
        self._i += 1
        return win

    def idle_seconds(self) -> float:
        return 0.0

    def close_window(self, hwnd: int) -> bool:
        print(f"      (시뮬레이터: hwnd={hwnd} 에 WM_CLOSE 보냈다고 가정)")
        return True

    def close_tab(self, hwnd: int) -> bool:
        print(f"      (시뮬레이터: hwnd={hwnd} 에 Ctrl+W 보냈다고 가정)")
        return True

    def mute_app(self, exe: str, mute: bool) -> bool:
        print(f"      (시뮬레이터: {exe} 소리 {'끔' if mute else '켬'})")
        return True


# =============================================================================
#  로직 부분 1 — 규칙 엔진
#  설계서의 block_rule / rule_condition 테이블과 1:1로 대응한다.
#  같은 group_no 안에서는 OR, 서로 다른 group_no 사이에서는 AND.
# =============================================================================

@dataclass(frozen=True)
class Condition:
    group_no: int
    subject: str        # 'app' | 'url' | 'window_title' | 'video_kind'
    operator: str       # 'eq' | 'contains' | 'regex' | 'not_regex'(이 패턴이 없어야 맞음)
    value: str


@dataclass(frozen=True)
class Rule:
    rule_id: str
    name: str
    action: str         # 고양이가 올라갈 수 있는 최대 단계: 'warn' < 'mute' < 'delay' < 'close'
    priority: int
    conditions: tuple[Condition, ...]
    reaction: str = ""       # {time} 이 있으면 누적 시간("40초", "2분 10초")으로 채운다
    step_sec: int = 0        # 0이면 창을 열자마자, N이면 오늘 누적 N초마다 한 단계씩 발동


def _subject_value(win: WindowInfo, subject: str) -> Optional[str]:
    return {
        "app": win.exe,
        "url": win.url,
        "window_title": win.title,
        "video_kind": win.video_kind,
    }.get(subject)


def _test(cond: Condition, win: WindowInfo) -> bool:
    actual = _subject_value(win, cond.subject)
    if actual is None:
        return False
    if cond.operator == "eq":
        return actual.lower() == cond.value.lower()
    if cond.operator == "contains":
        return cond.value.lower() in actual.lower()
    if cond.operator == "regex":
        return re.search(cond.value, actual, re.IGNORECASE) is not None
    if cond.operator == "not_regex":
        return re.search(cond.value, actual, re.IGNORECASE) is None
    return False


def rule_matches(rule: Rule, win: WindowInfo) -> bool:
    """모든 조건 그룹이 각각 하나 이상 만족되어야 발동."""
    groups: dict[int, list[Condition]] = {}
    for c in rule.conditions:
        groups.setdefault(c.group_no, []).append(c)
    if not groups:
        return False
    return all(any(_test(c, win) for c in conds) for conds in groups.values())


def pick_rule(rules: Iterable[Rule], win: WindowInfo) -> Optional[Rule]:
    """priority가 낮은 것부터 평가해 첫 번째로 맞는 규칙 하나."""
    matched = [r for r in rules if rule_matches(r, win)]
    return min(matched, key=lambda r: r.priority) if matched else None


# 영상 제목에 이 말이 있으면 '공부·음악 용도'로 보고 유튜브 시간에서 뺀다.
STUDY_WORDS = ("강의|수업|공부|인강|lecture|study|tutorial|course|코딩|파이썬|python"
               "|노래|음악|music|lofi|플레이리스트|playlist")

# 기본 규칙 — 나중에 이 리스트가 DB의 block_rule 테이블로 옮겨간다.
DEFAULT_RULES: tuple[Rule, ...] = (
    Rule(
        rule_id="r_shorts", name="유튜브 쇼츠 차단", action="close", priority=10,
        reaction="또 쇼츠야?",
        conditions=(
            Condition(0, "url", "contains", "youtube.com/shorts"),
        ),
    ),
    Rule(
        rule_id="r_reels", name="릴스/틱톡 차단", action="close", priority=10,
        reaction="숏폼은 그만.",
        conditions=(
            # group 0 안에서 OR — 셋 중 아무거나 하나면 된다
            Condition(0, "url", "contains", "instagram.com/reels"),
            Condition(0, "url", "contains", "tiktok.com"),
            Condition(0, "url", "contains", "youtube.com/shorts"),
        ),
    ),
    Rule(
        rule_id="r_yt_warn", name="유튜브 (공부·음악 제외)", action="close", priority=50,
        reaction="딴짓 영상 {time}째야.", step_sec=10,
        conditions=(
            # group 0 AND group 1 AND group 2 — 크롬이면서, 유튜브이고, 딴짓 영상이어야 한다.
            # 영상 종류는 YouTube 카테고리로 자동 분류(강의·노래는 제외, 애매하면 사용자에게 물어봄).
            Condition(0, "app", "eq", "chrome.exe"),
            Condition(1, "url", "contains", "youtube.com"),
            Condition(2, "video_kind", "not_regex", "^(lecture|music|ask)$"),
        ),
    ),
)


# =============================================================================
#  로직 부분 2 — 세션 집계
#  설계서의 usage_session 테이블과 같은 모양의 레코드를 만든다.
# =============================================================================

@dataclass
class Session:
    started_at: str
    exe: str
    title: str
    url: Optional[str]
    duration_sec: float = 0.0
    is_idle: bool = False
    ended_at: Optional[str] = None

    def to_record(self) -> dict:
        return {
            "type": "usage_session",
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exe": self.exe,
            "window_title": self.title,          # ← 민감 정보. 로컬 전용 컬럼
            "url_host": _host(self.url),         # 전체 URL이 아니라 호스트만 남긴다
            "duration_sec": round(self.duration_sec, 1),
            "is_idle": self.is_idle,
        }


def _host(url: Optional[str]) -> Optional[str]:
    """개인정보 최소화: 전체 URL 대신 호스트만 저장한다."""
    if not url:
        return None
    m = re.match(r"^(?:https?://)?([^/]+)", url)
    return m.group(1) if m else None


class SessionTracker:
    def __init__(self, on_close: Callable[[Session], None]) -> None:
        self.current: Optional[Session] = None
        self._on_close = on_close

    def observe(self, win: Optional[WindowInfo], elapsed: float, idle: bool) -> bool:
        """창을 관찰한다. 새 세션이 시작됐으면 True."""
        key = (win.key if win else None, idle)
        cur_key = ((self.current.exe, self.current.url),
                   self.current.is_idle) if self.current else None

        if self.current and key == cur_key:
            self.current.duration_sec += elapsed
            # 제목은 '마지막으로 본 것'으로 갱신한다. 유튜브는 다음 영상으로 넘어가면 주소가 먼저 바뀌고
            # 탭 제목은 몇 초 뒤에 바뀌어서, 처음 제목을 쓰면 이전 영상 제목이 남는다.
            self.current.title = win.title
            return False

        self.flush()
        if win is not None:
            self.current = Session(now_iso(), win.exe, win.title, win.url,
                                   elapsed, idle)
        return win is not None

    def flush(self) -> None:
        if self.current:
            self.current.ended_at = now_iso()
            self._on_close(self.current)
            self.current = None


# =============================================================================
#  메인 루프
# =============================================================================

IDLE_THRESHOLD_SEC = 60


def main() -> int:
    ap = argparse.ArgumentParser(description="집사 고양이 스파이크 #1")
    ap.add_argument("--simulate", action="store_true",
                    help="가짜 시나리오로 로직만 검증 (Windows 불필요)")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="관찰 주기(초). 기본 1.0")
    ap.add_argument("--action", choices=["log", "close"], default="log",
                    help="규칙이 맞았을 때: log=출력만, close=실제로 창 닫기")
    ap.add_argument("--out", default="spike_log.jsonl",
                    help="기록 파일 (JSONL)")
    args = ap.parse_args()

    # --- 프로브 선택 ---
    if args.simulate:
        probe = SimulatedProbe()
        args.interval = min(args.interval, 0.2)     # 시뮬은 빨리 돌린다
        print("=== 시뮬레이션 모드 — 가짜 창 전환 시나리오를 재생합니다 ===\n")
    elif sys.platform != "win32":
        print(f"[x] 이 스파이크의 실제 감지는 Windows 전용입니다 "
              f"(현재: {sys.platform}).")
        print("    로직만 확인하려면:  python watch.py --simulate")
        return 1
    else:
        probe = WindowsProbe()
        print("=== 감시 시작 — Ctrl+C 로 종료 ===\n")

    out = open(args.out, "a", encoding="utf-8")
    sessions: list[dict] = []

    def write(record: dict) -> None:
        out.write(json.dumps(record, ensure_ascii=False) + "\n")
        out.flush()

    def on_session_closed(s: Session) -> None:
        rec = s.to_record()
        sessions.append(rec)
        write(rec)
        mark = " (idle)" if s.is_idle else ""
        print(f"  └ 종료: {s.exe:<16} {rec['duration_sec']:>5.1f}초{mark}")

    tracker = SessionTracker(on_session_closed)
    acted_on: set[str] = set()      # 세션당 규칙은 한 번만 발동 (도배 방지)

    try:
        while True:
            win = probe.probe()
            if win is None and args.simulate:
                break

            idle = probe.idle_seconds() > IDLE_THRESHOLD_SEC
            is_new = tracker.observe(win, args.interval, idle)

            if is_new and win is not None:
                url_disp = win.url or "-"
                if len(url_disp) > 52:
                    url_disp = url_disp[:49] + "..."
                print(f"\n[{now_iso()}] {win.exe}")
                print(f"  제목: {win.title[:60]}")
                print(f"  URL : {url_disp}")
                acted_on.clear()

                # --- 규칙 판정 ---
                rule = pick_rule(DEFAULT_RULES, win)
                if rule and rule.rule_id not in acted_on:
                    acted_on.add(rule.rule_id)
                    print(f"  ★ 규칙 발동: [{rule.name}] action={rule.action}")
                    print(f"    고양이: \"{rule.reaction}\"")
                    write({"type": "block_event", "occurred_at": now_iso(),
                           "rule_id": rule.rule_id, "action": rule.action,
                           "exe": win.exe, "url_host": _host(win.url)})
                    if args.action == "close" and rule.action == "close":
                        ok = probe.close_window(win.hwnd)
                        print(f"    → WM_CLOSE 전송 {'성공' if ok else '실패'}"
                              f" (브라우저는 창 전체가 닫힙니다)")

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n\n중단됨.")
    finally:
        tracker.flush()
        out.close()
        summarize(sessions, args.out)

    return 0


def summarize(sessions: list[dict], path: str) -> None:
    """설계서 Q2 '오늘 가장 많이 쓴 앱 TOP 5'를 SQL 없이 파이썬으로 흉내낸 것."""
    if not sessions:
        print("기록된 세션이 없습니다.")
        return
    totals: dict[str, float] = {}
    for s in sessions:
        if not s["is_idle"]:
            totals[s["exe"]] = totals.get(s["exe"], 0) + s["duration_sec"]

    print("\n" + "─" * 46)
    print("앱별 사용 시간 (이게 나중에 usage_session 테이블이 됩니다)")
    print("─" * 46)
    for exe, sec in sorted(totals.items(), key=lambda x: -x[1])[:5]:
        print(f"  {exe:<24} {sec:>7.1f}초")
    print("─" * 46)
    print(f"세션 {len(sessions)}건 → {path}")


if __name__ == "__main__":
    raise SystemExit(main())
