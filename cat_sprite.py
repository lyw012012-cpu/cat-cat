"""
움직이는 고양이 — 바탕화면 위(작업 표시줄 바로 위)를 돌아다니는 투명 창.

고양이 디자인: 흰냥이 (손그림 원화를 바탕으로 코드로 다시 그림).
그림 파일 없이 tkinter Canvas 도형으로 그린다 — 손그림 낙서 스타일(굵은 진회색 선, 흰 몸, 분홍 귀·볼, 점 눈).
무늬는 SKIN 으로 고른다: 'calico'(크림 무늬, 기본) / 'white'.
자세(pose): idle 앉기 · walk 걷기 · happy 좋아함 · angry 화남 · eat 먹기 · swipe 앞발 · speaker 스피커 위
            block 막아서기 · look_far 먼 곳 보기 · sleep 잠
조작: 드래그 = 옮기기 · 클릭 = 쓰다듬기 · 더블클릭 = 간식 주기 · 오른쪽 클릭 = 메뉴
"""

from __future__ import annotations

import math
import random
import time
import tkinter as tk

KEY = "#00ff01"            # 이 색은 투명하게 처리된다 (고양이 그림엔 쓰지 않는다)
FONT = "맑은 고딕"

# 손그림 낙서 스타일 (참고 그림: 굵고 부드러운 진회색 선, 흰 몸, 분홍 귀·볼, 점 눈)
INK, INK_W = "#3d3d3d", 4.5        # 선 색·굵기
EAR_PINK, BLUSH = "#f9d5da", "#fbd3d8"
SKINS = {
    "calico": {"fur": "#ffffff", "patch": "#f8e6c3"},   # 삼색 (크림 무늬) — 기본
    "white":  {"fur": "#ffffff", "patch": None},        # 흰 고양이
}
SKIN = "calico"


def draw_cat(cv: tk.Canvas, cx: float, base: float, pose: str, t: int, s: float = 1.0, facing: int = 1) -> None:
    """(cx, base) = 고양이 발밑 가운데. t = 애니메이션 틱, s = 크기, facing = 1 오른쪽 / -1 왼쪽."""
    skin = SKINS[SKIN]
    fur, patch = skin["fur"], skin["patch"]
    w = max(2, INK_W * s)

    bob = 2 * math.sin(t / 2) if pose == "walk" else (1.5 * math.sin(t / 6) if pose in ("happy", "eat") else 0)
    lift = -34 if pose == "speaker" else 0
    oy = lift - bob                                   # 몸 전체를 위아래로

    def P(x, y):
        return cx + x * s * facing, base + (y + oy) * s

    def flat(pts):
        return [v for p in pts for v in P(*p)]

    def blob(pts, fill, outline="", width=0):         # 부드러운 채운 도형
        cv.create_polygon(*flat(pts), fill=fill, outline=outline, width=width, smooth=True, splinesteps=24)

    def stroke(pts, width=None, color=INK):           # 부드러운 선
        cv.create_line(*flat(pts), fill=color, width=width or w, smooth=True, splinesteps=24,
                       capstyle="round", joinstyle="round")

    def dot(x, y, r, color=INK):
        (a, b), (c, d) = P(x - r, y - r), P(x + r, y + r)
        cv.create_oval(min(a, c), b, max(a, c), d, fill=color, outline="")

    def text(x, y, txt, size, color=INK):
        cv.create_text(*P(x, y), text=txt, font=(FONT, int(size * s), "bold"), fill=color)

    if pose == "speaker":                             # 🔊 깔고 앉은 스피커
        (a, b), (c, d) = P(-36, 36), P(36, 2)
        cv.create_rectangle(min(a, c), d, max(a, c), b, fill="#e8e8e8", outline=INK, width=w)
        dot(0, 20, 10, "#9a9a9a")
        text(52, 18, "🔇", 15)

    # ---- 꼬리 (속이 흰 관 모양) — 살랑살랑, 화나면 빳빳 ----
    sway = 8 * math.sin(t / 5) if pose not in ("angry", "block") else 0
    tail = ([(36, -8), (58, -30), (62, -70)] if pose in ("angry", "block")
            else [(36, -6), (60, -18), (70 + sway, -44), (60 + sway, -58)])
    stroke(tail, width=w * 2.6)
    stroke(tail, width=w * 1.2, color=patch or fur)

    # ---- 몸통 ----
    body = [(-30, -62), (-44, -34), (-40, -6), (-26, 0), (26, 0), (40, -6), (44, -34), (30, -62)]
    blob(body, fur)
    if patch:                                         # 오른쪽 아래 크림 무늬
        blob([(30, -42), (43, -30), (40, -8), (30, -3), (24, -22)], patch)
    stroke([(-30, -58), (-44, -32), (-41, -9), (-33, -1), (-22, -2)])       # 왼쪽 옆구리와 둥근 발
    stroke([(30, -58), (44, -32), (41, -9), (33, -1), (22, -2)])            # 오른쪽
    if pose == "walk":                                # 🚶 걸을 때: 앞다리가 번갈아 들리며 앞으로 나간다
        for i, x in enumerate((-12, 12)):
            phase = math.sin(t / 2 + i * math.pi)
            lift, reach = max(0.0, 7 * phase), 6 * phase           # 드는 다리는 앞으로, 딛는 다리는 뒤로
            top, foot = (x, -30), (x + reach, -5 - lift)
            stroke([top, foot], width=w * 3.2)                     # 속이 흰 관 모양 다리
            stroke([top, foot], width=w * 1.5, color=fur)
            (a, b), (c, d) = P(foot[0] - 8, foot[1] - 5), P(foot[0] + 8, foot[1] + 5)
            cv.create_oval(min(a, c), b, max(a, c), d, fill=fur, outline=INK, width=w * 0.8)   # 앞발
            hx = -34 if i == 0 else 34                             # 뒷발은 반대 박자로
            hlift = max(0.0, -4 * phase)
            (a, b), (c, d) = P(hx - 7, -6 - hlift), P(hx + 7, 2 - hlift)
            cv.create_oval(min(a, c), b, max(a, c), d, fill=fur, outline=INK, width=w * 0.8)
    elif pose not in ("block",):                      # 앉아 있을 때 앞다리 두 줄 (발끝이 살짝 말린다)
        stroke([(-10, -24), (-10, -4), (-6, -1)])
        stroke([(10, -24), (10, -4), (6, -1)])

    # ---- 머리 (아래 테두리 없이 몸과 이어진다) ----
    flatten = 10 if pose == "angry" else 0            # 화나면 귀가 옆으로 눕는다
    head = [(-40, -56), (-47, -72), (-44, -94), (-42 - flatten, -124 + flatten), (-42 - flatten, -124 + flatten),
            (-15, -107), (0, -105), (15, -107), (42 + flatten, -124 + flatten), (42 + flatten, -124 + flatten),
            (44, -94), (47, -72), (40, -56)]
    blob(head + [(20, -50), (-20, -50)], fur)
    if patch:                                         # 머리 위쪽 크림 무늬 (두 귀 포함)
        blob([(-42, -92), (-41 - flatten, -120 + flatten), (-15, -104), (0, -103), (15, -104),
              (41 + flatten, -120 + flatten), (44, -90), (45, -76), (30, -86), (10, -92), (-12, -92), (-32, -88)], patch)
    for sx in (-1, 1):                                # 분홍 귀 안쪽
        blob([(sx * (38 + flatten), -116 + flatten), (sx * 34, -100), (sx * 22, -106)], EAR_PINK)
    stroke(head)

    # ---- 얼굴 ----
    for sx in (-1, 1):                                # 분홍 볼
        (a, b), (c, d) = P(sx * 22, -76), P(sx * 34, -66)
        cv.create_oval(min(a, c), b, max(a, c), d, fill=BLUSH, outline="")
    blink = t % 50 in (0, 1)
    if pose in ("happy", "eat"):                      # ^ ^
        for ex in (-15, 15):
            stroke([(ex - 5, -79), (ex, -84), (ex + 5, -79)], width=w * 0.7)
    elif pose == "angry":                             # > <
        for sx in (-1, 1):
            stroke([(sx * 20, -86), (sx * 12, -81), (sx * 20, -77)], width=w * 0.7)
    elif pose == "sleep" or blink:
        for ex in (-15, 15):
            stroke([(ex - 5, -81), (ex + 5, -81)], width=w * 0.6)
    else:
        look = 3 if pose == "look_far" else 0
        for ex in (-15, 15):
            dot(ex + look, -81, 3.6)
    dot(0, -74, 2.2)                                  # 코
    if pose == "eat":
        (a, b), (c, d) = P(-4, -71), P(4, -64)
        cv.create_oval(min(a, c), b, max(a, c), d, fill="#c9665f", outline=INK, width=max(1, w * 0.4))
    for sx in (-1, 1):                                # 수염 셋 (머리 테두리를 넘어간다)
        stroke([(sx * 38, -78), (sx * 58, -82)], width=w * 0.55)
        stroke([(sx * 38, -73), (sx * 59, -73)], width=w * 0.55)
        stroke([(sx * 38, -68), (sx * 56, -63)], width=w * 0.55)

    # ---- 자세별 덧붙임 ----
    def paw(x, y):
        (a, b), (c, d) = P(x - 9, y - 9), P(x + 9, y + 9)
        cv.create_oval(min(a, c), b, max(a, c), d, fill=fur, outline=INK, width=w)

    if pose == "happy":
        for k in range(2):
            hh = (t * 2 + k * 15) % 40
            text(-32 + 58 * k, -128 - hh, "♥", 13, "#f28fa0")
    elif pose == "angry":
        text(46, -132, "💢", 16, "#e05a5a")
    elif pose == "eat":                               # 🐟
        cv.create_polygon(*flat([(8, -66), (34, -74), (34, -58)]), fill="#bcd9ef", outline=INK, width=max(1, w * 0.6))
        (a, b), (c, d) = P(30, -76), P(56, -56)
        cv.create_oval(min(a, c), b, max(a, c), d, fill="#bcd9ef", outline=INK, width=max(1, w * 0.6))
    elif pose == "sleep":
        text(44, -120 - (t % 20), "z", 12, "#777")
    elif pose == "swipe":                             # 🐾 앞발 휘두르기 (탭 닫기)
        sw = 7 * math.sin(t)
        stroke([(22, -46), (46 + sw, -92)], width=w * 3.4)
        stroke([(22, -46), (46 + sw, -92)], width=w * 1.6, color=fur)
        paw(48 + sw, -96)
        for k in range(3):
            stroke([(62 + sw, -104 + k * 9), (78 + sw, -110 + k * 9)], width=w * 0.5, color="#e05a5a")
    elif pose == "block":                             # 🚪 두 앞발 벌려 막기
        for sx in (-1, 1):
            stroke([(sx * 30, -44), (sx * 60, -62)], width=w * 3.4)
            stroke([(sx * 30, -44), (sx * 60, -62)], width=w * 1.6, color=fur)
            paw(sx * 64, -64)
    elif pose == "look_far":                          # 👀 먼 곳을 가리키는 앞발
        stroke([(30, -46), (60, -74)], width=w * 3.4)
        stroke([(30, -46), (60, -74)], width=w * 1.6, color=fur)
        paw(64, -78)
        cv.create_line(*flat([(76, -80), (110, -88)]), fill="#aaaaaa", dash=(4, 4), width=2)


class DesktopCat:
    """바탕화면을 돌아다니는 고양이 창. callbacks: pet(), feed(), menu=[(라벨, 함수) | None]."""

    W, H = 300, 230

    def __init__(self, root: tk.Tk, pet, feed, menu: list) -> None:
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-transparentcolor", KEY)
        self.win.configure(bg=KEY)
        self.cv = tk.Canvas(self.win, width=self.W, height=self.H, bg=KEY, highlightthickness=0)
        self.cv.pack()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.min_x, self.max_x = 0, sw - self.W
        self.x, self.y = sw - self.W - 40, sh - self.H - 44       # 작업 표시줄 바로 위
        self.win.geometry(f"+{self.x}+{self.y}")

        self.t, self.facing = 0, -1
        self.base_pose, self.pose, self.pose_until = "idle", "idle", 0.0
        self.walk_ticks = 0
        self.bubble, self.bubble_until = "", 0.0
        self.pet, self.feed = pet, feed
        self._press = None

        self.menu = tk.Menu(self.win, tearoff=0, font=(FONT, 9))
        for item in menu:
            if item is None:
                self.menu.add_separator()
            else:
                self.menu.add_command(label=item[0], command=item[1])
        self.cv.bind("<ButtonPress-1>", self._on_press)
        self.cv.bind("<B1-Motion>", self._on_drag)
        self.cv.bind("<ButtonRelease-1>", self._on_release)
        self.cv.bind("<Double-Button-1>", lambda _: self.feed())
        self.cv.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        self._tick()

    # ---- 바깥에서 부르는 것 ------------------------------------------------
    def say(self, text: str, seconds: float = 7) -> None:
        self.bubble, self.bubble_until = text, time.time() + seconds

    def act(self, pose: str, seconds: float = 3) -> None:
        self.pose, self.pose_until, self.walk_ticks = pose, time.time() + seconds, 0

    def set_mood(self, name: str) -> None:
        self.base_pose = "angry" if name == "화남" else "idle"

    def anchor(self) -> tuple[int, int]:
        """말풍선·질문 창을 고양이 옆에 띄우려고 쓰는 위치."""
        return self.win.winfo_x(), self.win.winfo_y()

    # ---- 마우스 ------------------------------------------------------------
    def _on_press(self, e) -> None:
        self._press = (e.x_root, e.y_root, self.win.winfo_x(), self.win.winfo_y(), False)

    def _on_drag(self, e) -> None:
        if not self._press:
            return
        px, py, wx, wy, _ = self._press
        if abs(e.x_root - px) + abs(e.y_root - py) > 4:
            self._press = (px, py, wx, wy, True)
            self.x, self.y = wx + e.x_root - px, wy + e.y_root - py
            self.win.geometry(f"+{self.x}+{self.y}")

    def _on_release(self, _) -> None:
        moved = self._press and self._press[4]
        self._press = None
        if not moved:
            self.pet()

    # ---- 애니메이션 (0.1초마다) ---------------------------------------------
    def _tick(self) -> None:
        self.t += 1
        now = time.time()
        if now >= self.pose_until:
            self.pose = self.base_pose
            if self.base_pose == "idle" and self.walk_ticks <= 0 and random.random() < 0.01:
                self.walk_ticks, self.facing = random.randint(30, 90), random.choice((-1, 1))
        if self.walk_ticks > 0 and self.pose == "idle" and not self._press:
            self.walk_ticks -= 1
            self.x = min(self.max_x, max(self.min_x, self.x + 3 * self.facing))
            if self.x in (self.min_x, self.max_x):
                self.facing *= -1
            self.win.geometry(f"+{self.x}+{self.y}")
        pose = "walk" if self.walk_ticks > 0 and self.pose == "idle" else self.pose

        self.cv.delete("all")
        if now < self.bubble_until and self.bubble:
            tid = self.cv.create_text(self.W / 2, 12, text=self.bubble, width=self.W - 30, anchor="n",
                                      font=(FONT, 9), fill="#222")
            x1, y1, x2, y2 = self.cv.bbox(tid)
            self.cv.create_rectangle(x1 - 8, y1 - 6, x2 + 8, y2 + 6, fill="white", outline="#888", width=1)
            self.cv.create_polygon(self.W / 2 - 8, y2 + 6, self.W / 2 + 8, y2 + 6, self.W / 2, y2 + 16,
                                   fill="white", outline="#888")
            self.cv.tag_raise(tid)
        draw_cat(self.cv, self.W / 2, self.H - 6, pose, self.t, 0.9, self.facing)
        self.win.after(100, self._tick)
