# -*- coding: utf-8 -*-
"""
가벼운 자동배팅 매크로 (최소 PyQt).
- 베이스(emulator_macro)에서 ADB 연결·좌표 잡기만 가져옴.
- 계산기에서 회차·픽·금액만 가져와서 바로 배팅. (금액 2회 확인·API 재조회 없음)
"""
import json
import os
import sys
import threading
import time

import requests

try:
    from pynput import mouse as pynput_mouse
    HAS_PYNPUT = True
except ImportError:
    HAS_PYNPUT = False
    pynput_mouse = None

try:
    from PyQt5.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QGroupBox, QFormLayout, QComboBox, QPlainTextEdit,
    )
    from PyQt5.QtCore import QTimer, pyqtSignal
    HAS_PYQT = True
except ImportError:
    HAS_PYQT = False

# 베이스와 동일 경로
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COORDS_PATH = os.path.join(SCRIPT_DIR, "emulator_coords.json")
COORD_KEYS = {"bet_amount": "배팅금액", "confirm": "정정", "red": "레드", "black": "블랙"}
COORD_BTN_SHORT = {"bet_amount": "금액", "confirm": "정정", "red": "레드", "black": "블랙"}

# 배팅 지연 — 픽 수신 즉시 ADB 전송용 최소화 (입력 안 먹으면 늘리세요)
D_BEFORE_EXECUTE = 1.0  # 배팅 실행 전 대기(초) — 픽이 화면에 반영될 시간 확보
D_AMOUNT_TAP = 0.01
D_INPUT = 0.015
D_BACK = 0.015
D_COLOR = 0.01
D_CONFIRM = 0.01
SWIPE_AMOUNT_MS = 50
SWIPE_COLOR_MS = 50


def load_coords():
    if not os.path.exists(COORDS_PATH):
        return {}
    try:
        with open(COORDS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_coords(data):
    with open(COORDS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _normalize_pick_color(raw):
    s = (raw or "").strip()
    if not s:
        return None
    u = s.upper()
    if u == "RED" or s == "빨강":
        return "RED"
    if u == "BLACK" or s == "검정":
        return "BLACK"
    return None


def _run_adb_raw(device_id, *args):
    kw = {"capture_output": True, "timeout": 10}
    if os.name == "nt":
        cmd = "adb -s %s %s" % (device_id, " ".join(str(a) for a in args)) if device_id else "adb " + " ".join(str(a) for a in args)
        kw["shell"] = True
    else:
        cmd = ["adb"] + (["-s", device_id] if device_id else []) + list(args)
    try:
        import subprocess
        subprocess.run(cmd, **kw)
    except Exception:
        pass


def adb_swipe(device_id, x, y, duration_ms=70):
    x, y = int(x), int(y)
    _run_adb_raw(device_id, "shell", "input", "swipe", str(x), str(y), str(x), str(y), str(duration_ms))


def adb_input_text(device_id, text):
    escaped = text.replace(" ", "%s")
    _run_adb_raw(device_id, "shell", "input", "text", escaped)


def adb_keyevent(device_id, keycode):
    _run_adb_raw(device_id, "shell", "input", "keyevent", str(keycode))


def _apply_window_offset(coords, x, y, key=None):
    try:
        x, y = int(x), int(y)
        if coords.get("raw_coords"):
            return x, y
        spaces = coords.get("coord_spaces") or {}
        is_window_relative = spaces.get(key, coords.get("coords_are_window_relative")) if key else coords.get("coords_are_window_relative")
        if is_window_relative:
            rx, ry = x, y
        else:
            ox = int(coords.get("window_left") or 0)
            oy = int(coords.get("window_top") or 0)
            rx = x - ox
            ry = y - oy
        try:
            win_w = int(coords.get("window_width") or 0)
            win_h = int(coords.get("window_height") or 0)
            dev_w = int(coords.get("device_width") or 0)
            dev_h = int(coords.get("device_height") or 0)
            if win_w > 0 and win_h > 0 and dev_w > 0 and dev_h > 0:
                rx = int(rx * dev_w / win_w)
                ry = int(ry * dev_h / win_h)
        except (TypeError, ValueError):
            pass
        return rx, ry
    except (TypeError, ValueError):
        return int(x), int(y)


def get_window_rect_at(screen_x, screen_y):
    """클릭한 점이 속한 창의 rect (left, top, width, height)."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        GA_ROOT = 2
        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]
        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]
        pt = POINT(int(screen_x), int(screen_y))
        hwnd = user32.WindowFromPoint(pt)
        if not hwnd:
            return None
        root = user32.GetAncestor(hwnd, GA_ROOT)
        if not root:
            root = hwnd
        rect = RECT()
        if not user32.GetWindowRect(root, ctypes.byref(rect)):
            return None
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return None
        return (rect.left, rect.top, w, h)
    except Exception:
        return None


def fetch_pick(url, calc_id=1, timeout=4):
    """GET /api/current-pick-relay → round, pick_color, suggested_amount (DB 없이 캐시에서 즉시 반환)"""
    url = (url or "").strip().rstrip("/")
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    try:
        r = requests.get(url + "/api/current-pick-relay", params={"calculator": calc_id}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


class LightMacroWindow(QMainWindow if HAS_PYQT else object):
    poll_done = pyqtSignal(dict) if HAS_PYQT else None

    def __init__(self):
        super().__init__()
        self.setWindowTitle("자동배팅 (가벼운 버전)")
        self.setMinimumSize(380, 520)
        self.resize(400, 560)

        self._url = ""
        self._calc_id = 1
        self._device_id = "127.0.0.1:5555"
        self._coords = {}
        self._running = False
        self._last_bet_round = None
        self._bet_confirm_last = None  # 회차 3회 확인용
        self._bet_confirm_count = 0
        self._last_seen_round = None  # 회차 역행 방지
        self._coord_listener = None
        self._coord_capture_key = None
        self._pending_coord_click = None
        self._coord_labels = {}

        self._build_ui()
        self._load_coords()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        if HAS_PYQT and self.poll_done:
            self.poll_done.connect(self._on_poll_done)

    def _log(self, msg):
        try:
            self.log_area.appendPlainText(msg)
            self.log_area.verticalScrollBar().setValue(self.log_area.verticalScrollBar().maximum())
        except Exception:
            pass

    def _load_coords(self):
        self._coords = load_coords()
        self._refresh_coord_labels()

    def _refresh_coord_labels(self):
        for key in COORD_KEYS:
            val = self._coords.get(key)
            if isinstance(val, (list, tuple)) and len(val) >= 2:
                self._coord_labels[key].setText("%s,%s" % (val[0], val[1]))
            else:
                self._coord_labels[key].setText("(미설정)")

    def _build_ui(self):
        cw = QWidget()
        self.setCentralWidget(cw)
        layout = QVBoxLayout()
        cw.setLayout(layout)

        g = QGroupBox("설정")
        fl = QFormLayout()
        self.url_combo = QComboBox()
        self.url_combo.setEditable(True)
        self.url_combo.setMinimumWidth(260)
        for nick, url in [("규지니", "https://web-production-28c2.up.railway.app"), ("로컬", "http://localhost:5000")]:
            self.url_combo.addItem(nick, url)
        self.url_combo.setCurrentIndex(0)
        fl.addRow("Analyzer URL:", self.url_combo)

        self.calc_combo = QComboBox()
        self.calc_combo.addItem("계산기 1", 1)
        self.calc_combo.addItem("계산기 2", 2)
        self.calc_combo.addItem("계산기 3", 3)
        fl.addRow("계산기:", self.calc_combo)

        self.device_combo = QComboBox()
        self.device_combo.setEditable(True)
        self.device_combo.addItem("127.0.0.1:5555", "127.0.0.1:5555")
        self.device_combo.addItem("127.0.0.1:5554", "127.0.0.1:5554")
        fl.addRow("ADB 기기:", self.device_combo)

        self.adb_btn = QPushButton("ADB 연결 확인")
        self.adb_btn.clicked.connect(self._on_adb_devices)
        fl.addRow("", self.adb_btn)

        self.start_btn = QPushButton("시작")
        self.start_btn.setMinimumHeight(32)
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn = QPushButton("정지")
        self.stop_btn.setMinimumHeight(32)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        fl.addRow("", btn_row)
        g.setLayout(fl)
        layout.addWidget(g)

        g2 = QGroupBox("좌표 (LDPlayer에서 클릭)")
        fl2 = QFormLayout()
        for key, label in COORD_KEYS.items():
            row = QHBoxLayout()
            btn = QPushButton(COORD_BTN_SHORT.get(key, label) + " 찾기")
            btn.clicked.connect(lambda c=False, k=key: self._start_coord_capture(k))
            lbl = QLabel("(미설정)")
            lbl.setMinimumWidth(70)
            self._coord_labels[key] = lbl
            row.addWidget(btn)
            row.addWidget(lbl)
            row.addStretch(1)
            fl2.addRow(row)
        g2.setLayout(fl2)
        layout.addWidget(g2)

        self.log_area = QPlainTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMaximumHeight(120)
        self.log_area.setPlaceholderText("로그")
        layout.addWidget(QLabel("로그"))
        layout.addWidget(self.log_area)

        layout.addStretch(1)

    def _start_coord_capture(self, key):
        if not HAS_PYNPUT or not pynput_mouse:
            self._log("pynput 필요: pip install pynput")
            return
        self._coord_capture_key = key
        self.showMinimized()
        self._coord_listener = pynput_mouse.Listener(on_click=self._on_coord_click)
        self._coord_listener.start()
        self._log("LDPlayer 창에서 클릭하세요.")

    def _on_coord_click(self, x, y, button, pressed):
        if not pressed or self._coord_capture_key is None:
            return
        key = self._coord_capture_key
        self._pending_coord_click = (key, x, y)
        self._coord_capture_key = None
        if self._coord_listener:
            try:
                self._coord_listener.stop()
            except Exception:
                pass
            self._coord_listener = None
        QTimer.singleShot(0, self._apply_coord_captured)

    def _apply_coord_captured(self):
        self.showNormal()
        self.raise_()
        if self._pending_coord_click is None:
            return
        key, x, y = self._pending_coord_click
        self._pending_coord_click = None
        rect = get_window_rect_at(x, y)
        if rect:
            left, top, w, h = rect
            self._coords[key] = [x - left, y - top]
            self._coords["window_left"] = left
            self._coords["window_top"] = top
            self._coords["window_width"] = w
            self._coords["window_height"] = h
            if not int(self._coords.get("device_width") or 0):
                self._coords["device_width"] = w
                self._coords["device_height"] = h
            sp = self._coords.get("coord_spaces") or {}
            sp[key] = True
            self._coords["coord_spaces"] = sp
            self._log("%s 저장 (창 자동)" % COORD_KEYS.get(key, key))
        else:
            self._coords[key] = [x, y]
            sp = self._coords.get("coord_spaces") or {}
            sp[key] = False
            self._coords["coord_spaces"] = sp
            self._log("%s 저장" % COORD_KEYS.get(key, key))
        save_coords(self._coords)
        self._refresh_coord_labels()

    def _on_start(self):
        url = (self.url_combo.currentText() or self.url_combo.currentData() or "").strip().rstrip("/")
        if not url:
            self._log("URL 입력하세요.")
            return
        self._url = url
        self._calc_id = self.calc_combo.currentData()
        self._device_id = (self.device_combo.currentText() or self.device_combo.currentData() or "127.0.0.1:5555").strip()
        self._coords = load_coords()
        if not self._coords.get("bet_amount") or not self._coords.get("red") or not self._coords.get("black"):
            self._log("좌표를 먼저 설정하세요.")
            return
        self._running = True
        self._last_bet_round = None
        self._bet_confirm_last = None  # 회차 3회 확인 상태 초기화
        self._bet_confirm_count = 0
        self._last_seen_round = None  # 회차 역행 방지 초기화
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._timer.start(80)
        self._log("시작 — 회차·픽·금액 2회 연속 일치 시 배팅")
        QTimer.singleShot(50, self._poll)

    def _on_stop(self):
        self._running = False
        self._timer.stop()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._log("정지")

    def _on_adb_devices(self):
        device = (self.device_combo.currentText() or self.device_combo.currentData() or "127.0.0.1:5555").strip()
        try:
            import subprocess
            cmd = "adb devices" if os.name == "nt" else ["adb", "devices"]
            kw = {"shell": True, "capture_output": True, "text": True, "timeout": 5} if os.name == "nt" else {"capture_output": True, "text": True, "timeout": 5}
            r = subprocess.run(cmd, **kw)
            out = (r.stdout or "").strip()
            self._log(out[:400] if out else "adb devices 완료")
        except Exception as e:
            self._log("ADB 오류: %s" % str(e)[:80])

    def _poll(self):
        if not self._running:
            return
        url = self._url
        calc_id = self._calc_id
        if not url:
            return

        def do_fetch():
            pick = fetch_pick(url, calc_id, timeout=3)
            if self.poll_done:
                self.poll_done.emit(pick or {})

        threading.Thread(target=do_fetch, daemon=True).start()

    def _on_poll_done(self, pick):
        if not self._running or not pick:
            return
        if pick.get("error"):
            return
        if pick.get("running") is False:
            return
        round_num = pick.get("round")
        raw_color = pick.get("pick_color")
        pick_color = _normalize_pick_color(raw_color)
        amt = pick.get("suggested_amount") or pick.get("suggestedAmount")
        try:
            amt_val = int(amt) if amt is not None else 0
        except (TypeError, ValueError):
            amt_val = 0
        if round_num is None or pick_color is None or amt_val <= 0:
            return
        try:
            round_num = int(round_num)
        except (TypeError, ValueError):
            return
        if self._last_bet_round is not None and round_num <= self._last_bet_round:
            return

        # 회차 역행 방지: 이미 더 높은 회차를 본 적 있으면 전회차 데이터 거부
        if self._last_seen_round is not None and round_num < self._last_seen_round:
            return
        self._last_seen_round = max(self._last_seen_round or 0, round_num)

        # 회차 3회 확인: 같은 (회차, 픽, 금액)이 3회 연속 수신될 때만 배팅
        key = (round_num, pick_color, amt_val)
        if self._bet_confirm_last != key:
            self._bet_confirm_last = key
            self._bet_confirm_count = 1
            return
        self._bet_confirm_count = (self._bet_confirm_count or 0) + 1
        if self._bet_confirm_count < 3:
            return

        self._bet_confirm_last = None
        self._bet_confirm_count = 0
        self._log("%s회 %s %s원 배팅 (3회 확인, %s초 후 실행)" % (round_num, pick_color, amt_val, D_BEFORE_EXECUTE))
        def _execute():
            if not self._running:
                return
            if self._last_bet_round is not None and round_num <= self._last_bet_round:
                return
            self._log("%s회 %s %s원 실행" % (round_num, pick_color, amt_val))
            ok = self._do_bet(round_num, pick_color, amt_val)
            if ok:
                self._last_bet_round = round_num
                QTimer.singleShot(60, self._poll)
        delay_ms = int(D_BEFORE_EXECUTE * 1000)
        if HAS_PYQT and delay_ms > 0:
            QTimer.singleShot(delay_ms, _execute)
        else:
            _execute()

    def _do_bet(self, round_num, pick_color, amount):
        coords = load_coords()
        device = self._device_id or None
        bet_xy = coords.get("bet_amount")
        confirm_xy = coords.get("confirm")
        red_xy = coords.get("red")
        black_xy = coords.get("black")
        if not bet_xy or len(bet_xy) < 2:
            self._log("배팅금액 좌표 없음")
            return False
        color_xy = red_xy if pick_color == "RED" else black_xy
        if not color_xy or len(color_xy) < 2:
            self._log("%s 좌표 없음" % ("레드" if pick_color == "RED" else "블랙"))
            return False

        bet_amount = str(amount)
        try:
            tx, ty = _apply_window_offset(coords, bet_xy[0], bet_xy[1], key="bet_amount")
            adb_swipe(device, tx, ty, SWIPE_AMOUNT_MS)
            time.sleep(D_AMOUNT_TAP)
            adb_input_text(device, bet_amount)
            time.sleep(D_INPUT)
            adb_keyevent(device, 4)
            time.sleep(D_BACK)

            cx, cy = _apply_window_offset(coords, color_xy[0], color_xy[1], key="red" if pick_color == "RED" else "black")
            adb_swipe(device, cx, cy, SWIPE_COLOR_MS)
            time.sleep(D_COLOR)

            if confirm_xy and len(confirm_xy) >= 2:
                cx2, cy2 = _apply_window_offset(coords, confirm_xy[0], confirm_xy[1], key="confirm")
                adb_swipe(device, cx2, cy2, SWIPE_COLOR_MS)
                time.sleep(D_CONFIRM)

            self._log("%s회 %s %s원 완료" % (round_num, pick_color, bet_amount))
            return True
        except Exception as e:
            self._log("배팅 오류: %s" % str(e)[:80])
            return False


def main():
    if not HAS_PYQT:
        print("PyQt5 필요: pip install PyQt5")
        return
    app = QApplication(sys.argv)
    w = LightMacroWindow()
    w.show()
    app.exec_()


if __name__ == "__main__":
    main()
