# -*- coding: utf-8 -*-
"""
가벼운 자동배팅 매크로 (최소 PyQt).
- 베이스(emulator_macro)에서 ADB 연결·좌표 잡기만 가져옴.
- 계산기에서 회차·픽만 받고, 금액은 매크로 내부에서 마틴 계산.
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
        QCheckBox, QLineEdit,
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

# 배팅 지연 — 픽 수신 즉시 사이트로 빠르게 배팅 (입력 안 먹으면 늘리세요)
D_BEFORE_EXECUTE = 0.02  # 배팅 실행 전 대기(초) — 최소화해 배팅 시간 확보
D_AMOUNT_TAP = 0.01  # 금액 칸 탭 후 포커스 대기 (자동 클리어됨)
D_INPUT = 0.04  # 금액 입력 후 레드/블랙 탭 전 (키보드 닫기 없음, BACK 절대 금지)
D_BACK = 0.12   # 입력 후 대기 — 레드/블랙 탭 전
D_COLOR = 0.01
D_CONFIRM = 0.01
SWIPE_AMOUNT_MS = 50
SWIPE_COLOR_MS = 50
D_AMOUNT_CONFIRM_COUNT = 1  # 같은 (회차, 픽) 1회 수신 시 즉시 배팅
D_DEL_COUNT = 8  # 기존 값 삭제용 DEL (8자리. 1회 ADB로 전송)
MARTINGALE_SAME_AMOUNT_THRESHOLD = 30000  # 마틴 연속 동일금액 검증: 이 값 초과 금액이 연속 회차에 같으면 오탐
D_AMOUNT_DOUBLE_INPUT = False  # 금액 1회만 입력 (이중 입력 시 1000010000 중복 발생)
# 표마틴 9단계 (1,2,3,6,11,21,40,76,120 × base)
MARTIN_RATIOS_TABLE = [1, 2, 3, 6, 11, 21, 40, 76, 120]


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


def _validate_bet_amount(amt):
    """금액 유효성 검증. 1~99,999,999 범위, 정수만 허용."""
    if amt is None:
        return False
    try:
        v = int(amt)
        return 1 <= v <= 99999999
    except (TypeError, ValueError):
        return False


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


def _pick_color_to_pred(pick_color, card_15_color=None):
    """RED/BLACK → 정/꺽. 15번 카드 색에 따라 (PREDICTION_AND_RESULT_SPEC 3.3)."""
    if _normalize_pick_color(pick_color) is None:
        return None
    n = _normalize_pick_color(pick_color)
    c15 = _normalize_pick_color(card_15_color) if card_15_color else None
    if c15 == "BLACK":
        return "꺽" if n == "RED" else "정"
    return "정" if n == "RED" else "꺽"


def _build_martin_table(base_amount):
    """1단계 배팅 금액으로 표마틴 테이블 생성."""
    try:
        b = int(base_amount)
        if b <= 0:
            b = 5000
    except (TypeError, ValueError):
        b = 5000
    return [b * r for r in MARTIN_RATIOS_TABLE]


def _calc_martingale_step_and_bet(history, martin_table=None):
    """history: [{round, predicted, actual}] 완료된 순서. 반환: (step, next_bet_amount)."""
    martin_table = martin_table or _build_martin_table(5000)
    step = 0
    for h in (history or []):
        act = (h.get("actual") or "").strip()
        pred = (h.get("predicted") or "").strip()
        if pred == "보류":
            continue
        if act not in ("정", "꺽", "joker", "조커"):
            continue
        is_joker = act in ("joker", "조커")
        is_win = not is_joker and pred in ("정", "꺽") and pred == act
        if is_win:
            step = 0
        else:
            step = min(step + 1, len(martin_table) - 1)
    bet = martin_table[min(step, len(martin_table) - 1)]
    return step, bet


def fetch_macro_data(url, calc_id=1, timeout=6):
    """GET /api/macro-data → round_actuals, cards. 금액 계산용."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    try:
        r = requests.get(url + "/api/macro-data", params={"calculator": calc_id}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _run_adb_raw(device_id, *args):
    kw = {"capture_output": True, "timeout": 10, "encoding": "utf-8", "errors": "replace"}
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


def adb_keyevent_repeat(device_id, keycode, count):
    """keyevent를 count회 한 번의 ADB 호출로 전송 (1초 대기 방지)."""
    if count <= 0:
        return
    if count == 1:
        adb_keyevent(device_id, keycode)
        return
    loop = ";".join(["input keyevent %s" % keycode] * count)
    import subprocess
    try:
        cmd = 'adb -s %s shell "%s"' % (device_id, loop) if device_id else 'adb shell "%s"' % loop
        subprocess.run(cmd, shell=True, capture_output=True, timeout=5, encoding="utf-8", errors="replace")
    except Exception:
        pass


def _apply_window_offset(coords, x, y, key=None):
    """창/기기 보정 후 ADB 전송 좌표. 기기 범위 초과 시 클램프 (밖으로 튕김 방지)."""
    try:
        x, y = int(x), int(y)
        if coords.get("raw_coords"):
            dev_w = int(coords.get("device_width") or 0)
            dev_h = int(coords.get("device_height") or 0)
            if dev_w > 0 and dev_h > 0:
                x = max(0, min(dev_w - 1, x))
                y = max(0, min(dev_h - 1, y))
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
            elif not is_window_relative and (ox == 0 and oy == 0) and (dev_w > 0 and dev_h > 0):
                rx = max(0, min(dev_w - 1, rx))
                ry = max(0, min(dev_h - 1, ry))
        except (TypeError, ValueError):
            pass
        dev_w = int(coords.get("device_width") or 0)
        dev_h = int(coords.get("device_height") or 0)
        if dev_w > 0 and dev_h > 0:
            rx = max(0, min(dev_w - 1, rx))
            ry = max(0, min(dev_h - 1, ry))
        return rx, ry
    except (TypeError, ValueError):
        return int(x), int(y)


def get_window_rect_at(screen_x, screen_y):
    """클릭한 점이 속한 창의 클라이언트 영역 (left, top, width, height). 제목줄/테두리 제외."""
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
        crect = RECT()
        if not user32.GetClientRect(root, ctypes.byref(crect)):
            return None
        client_w = crect.right - crect.left
        client_h = crect.bottom - crect.top
        if client_w <= 0 or client_h <= 0:
            return None
        pt_tl = POINT(0, 0)
        if not user32.ClientToScreen(root, ctypes.byref(pt_tl)):
            return None
        return (pt_tl.x, pt_tl.y, client_w, client_h)
    except Exception:
        return None


def fetch_pick(url, calc_id=1, timeout=4):
    """GET /api/current-pick-relay → round, pick_color, pick_pred (금액은 매크로 내부 계산)"""
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
        self._last_bet_amount = None  # 마틴 연속 동일금액 검증용
        self._bet_rounds_done = set()  # 배팅 완료 회차 — 마틴 끝 후 동일 금액 재송출 방지
        self._pending_bet_rounds = {}  # round_num -> {} (두 번 배팅 방지)
        self._bet_confirm_last = None  # 회차 2회 확인용
        self._bet_confirm_count = 0
        self._last_seen_round = None  # 회차 역행 방지
        self._do_bet_lock = threading.Lock()  # 픽 1회만 탭, 2중배팅 절대 방지
        self._round_actuals = {}  # round_id -> {actual, color} — 매크로 내부 금액 계산용
        self._cards = []  # 15번 카드 색(정/꺽 변환용)
        self._bet_history = {}  # round -> predicted (배팅 시 저장)
        self._last_macro_fetch_at = 0  # macro-data 폴링 간격
        self._macro_lock = threading.Lock()
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
        if hasattr(self, "base_bet_edit"):
            b = self._coords.get("macro_base") or 5000
            self.base_bet_edit.setText(str(int(b)))
        if hasattr(self, "no_martin_check"):
            self.no_martin_check.setChecked(bool(self._coords.get("macro_no_martin")))

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

        self.base_bet_edit = QLineEdit()
        self.base_bet_edit.setPlaceholderText("5000")
        self.base_bet_edit.setMaximumWidth(80)
        self.base_bet_edit.setToolTip("배팅 금액 (마틴 끄면 고정 금액)")
        fl.addRow("시작 금액 (원):", self.base_bet_edit)
        self.no_martin_check = QCheckBox("마틴 사용 안함 (고정 금액만)")
        self.no_martin_check.setToolTip("체크 시 매 회차 시작 금액만 배팅. 마틴 단계 없음.")
        fl.addRow("", self.no_martin_check)

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
        try:
            b = int(self.base_bet_edit.text().strip() or 5000)
            self._coords["macro_base"] = b
            self._coords["macro_no_martin"] = getattr(self, "no_martin_check", None) and self.no_martin_check.isChecked()
            save_coords(self._coords)
        except (TypeError, ValueError):
            pass
        if not self._coords.get("bet_amount") or not self._coords.get("red") or not self._coords.get("black"):
            self._log("좌표를 먼저 설정하세요.")
            return
        self._running = True
        self._last_bet_round = None
        self._last_bet_amount = None  # 마틴 연속 동일금액 검증 초기화
        self._bet_rounds_done.clear()  # 배팅 완료 회차 초기화
        self._bet_history.clear()  # 매크로 내부 금액 계산용 — 시작 시 초기화
        self._pending_bet_rounds = {}  # 시작 시 초기화
        self._bet_confirm_last = None  # 회차 3회 확인 상태 초기화
        self._bet_confirm_count = 0
        self._last_seen_round = None  # 회차 역행 방지 초기화
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._timer.start(25)  # 25ms 폴링 — 픽 빠른 수신
        self._log("시작 — 회차·픽·금액 %s회 연속 일치 시 배팅" % D_AMOUNT_CONFIRM_COUNT)
        QTimer.singleShot(20, self._poll)

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
            kw = {"shell": True, "capture_output": True, "text": True, "timeout": 5, "encoding": "utf-8", "errors": "replace"} if os.name == "nt" else {"capture_output": True, "text": True, "timeout": 5}
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
            now = time.time()
            need_macro = now - self._last_macro_fetch_at >= 0.5
            pick = {}
            if need_macro:
                # macro-data와 pick 병렬 fetch — 순차 대기 제거, 배팅 속도 개선
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=2) as ex:
                    f_macro = ex.submit(fetch_macro_data, url, calc_id, 2)
                    f_pick = ex.submit(fetch_pick, url, calc_id, 2)
                    try:
                        data = f_macro.result(timeout=2.5)
                        if data:
                            with self._macro_lock:
                                self._round_actuals = data.get("round_actuals") or {}
                                if data.get("cards"):
                                    self._cards = data.get("cards") or []
                                self._last_macro_fetch_at = now
                    except Exception:
                        pass
                    try:
                        pick = f_pick.result(timeout=2.5) or {}
                    except Exception:
                        pick = {}
            else:
                try:
                    pick = fetch_pick(url, calc_id, timeout=2) or {}
                except Exception:
                    pick = {}
            if self.poll_done:
                self.poll_done.emit(pick)

        threading.Thread(target=do_fetch, daemon=True).start()

    def _calc_bet_amount(self, round_num, predicted):
        """매크로 내부 마틴 계산. _bet_history + _round_actuals 기반. 마틴 끄면 고정 금액만."""
        no_martin = getattr(self, "no_martin_check", None) and self.no_martin_check.isChecked()
        with self._macro_lock:
            ra = dict(self._round_actuals)
            bh = dict(self._bet_history)
        # 마틴 사용 시에만: 직전 배팅 회차 결과가 round_actuals에 없으면 금액 계산 안 함
        if not no_martin and self._last_bet_round is not None:
            lb_str = str(self._last_bet_round)
            if lb_str not in ra:
                if hasattr(self, "_log"):
                    self._log("[금액검증] %s회 결과 대기 — %s회 배팅 스킵 (마틴 리셋 보장)" % (lb_str, round_num))
                return 0
            act = (ra[lb_str].get("actual") or "").strip()
            if act not in ("정", "꺽", "joker", "조커"):
                if hasattr(self, "_log"):
                    self._log("[금액검증] %s회 결과 미완료 — %s회 배팅 스킵" % (lb_str, round_num))
                return 0
        base = int(self._coords.get("macro_base") or 5000)
        if hasattr(self, "base_bet_edit") and self.base_bet_edit:
            txt = (self.base_bet_edit.text() or "").strip()
            if txt:
                try:
                    base = int(txt)
                except (TypeError, ValueError):
                    pass
        if base <= 0:
            base = 5000
        capital = int(self._coords.get("macro_capital") or 1000000)
        martin_table = _build_martin_table(base)
        completed = []
        if not no_martin:
            for rnd, pred in sorted(bh.items(), key=lambda x: x[0]):
                rnd_str = str(rnd)
                if rnd_str not in ra:
                    continue
                act = (ra[rnd_str].get("actual") or "").strip()
                if act not in ("정", "꺽", "joker", "조커"):
                    continue
                completed.append({"round": rnd, "predicted": pred, "actual": act})
            completed.sort(key=lambda x: x["round"])
        _, amt = _calc_martingale_step_and_bet(completed, martin_table)
        return min(amt, capital) if amt > 0 else 0

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
        if round_num is None or pick_color is None:
            return
        try:
            round_num = int(round_num)
        except (TypeError, ValueError):
            return
        pick_pred = (pick.get("pick_pred") or "").strip()
        pick_pred = pick_pred if pick_pred in ("정", "꺽") else None
        card_15 = self._cards[14].get("color") if len(self._cards) > 14 else None
        predicted = pick_pred or _pick_color_to_pred(pick_color, card_15)
        amt_val = self._calc_bet_amount(round_num, predicted)
        if amt_val <= 0 or not _validate_bet_amount(amt_val):
            return
        if self._last_bet_round is not None and round_num <= self._last_bet_round:
            return
        if round_num in self._pending_bet_rounds:
            return
        if round_num in self._bet_rounds_done:
            return  # 이미 배팅 완료 — 마틴 끝 후 동일 금액 재송출 방지

        # 회차 역행 방지: 이미 더 높은 회차를 본 적 있으면 전회차 데이터 거부
        if self._last_seen_round is not None and round_num < self._last_seen_round:
            return
        self._last_seen_round = max(self._last_seen_round or 0, round_num)

        # 회차 N회 확인: 같은 (회차, 픽)이 N회 연속 수신될 때만 배팅 (금액은 매크로 내부 계산)
        key = (round_num, pick_color)
        if self._bet_confirm_last != key:
            self._bet_confirm_last = key
            self._bet_confirm_count = 1
            return
        self._bet_confirm_count = (self._bet_confirm_count or 0) + 1
        if self._bet_confirm_count < D_AMOUNT_CONFIRM_COUNT:
            return

        self._bet_confirm_last = None
        self._bet_confirm_count = 0
        self._pending_bet_rounds[round_num] = {}  # 즉시 등록 — 두 번 배팅 방지
        self._log("%s회 %s %s원 배팅 (매크로 내부 계산, %s초 후 실행)" % (round_num, pick_color, amt_val, D_BEFORE_EXECUTE))
        def _execute():
            if not self._running:
                return
            if round_num in self._bet_rounds_done:
                self._pending_bet_rounds.pop(round_num, None)
                return  # 이미 배팅 완료 — 마틴 끝 후 동일 금액 재송출 방지
            if self._last_bet_round is not None and round_num <= self._last_bet_round:
                self._pending_bet_rounds.pop(round_num, None)
                return
            # 마틴 연속 동일금액 검증: 다음 회차에 같은 금액(3만 초과)은 오탐
            if (self._last_bet_round is not None and self._last_bet_amount is not None
                    and round_num == self._last_bet_round + 1 and amt_val == self._last_bet_amount
                    and amt_val > MARTINGALE_SAME_AMOUNT_THRESHOLD):
                self._log("[금액검증] %s회 %s원 스킵 — 마틴 연속 동일금액 오탐" % (round_num, amt_val))
                self._pending_bet_rounds.pop(round_num, None)
                return
            # 마틴 사용 시에만: 배팅 직전 macro-data 재조회 (마틴 끄면 생략 — 속도 우선)
            no_martin = getattr(self, "no_martin_check", None) and self.no_martin_check.isChecked()
            if not no_martin:
                try:
                    url = self._url or (self.url_combo.currentText() or self.url_combo.currentData() or "").strip().rstrip("/")
                    calc_id = self._calc_id or (self.calc_combo.currentData() if HAS_PYQT else 1)
                    if url and calc_id:
                        data = fetch_macro_data(url, calc_id, timeout=2)
                        if data:
                            with self._macro_lock:
                                self._round_actuals = data.get("round_actuals") or {}
                                if data.get("cards"):
                                    self._cards = data.get("cards") or []
                except Exception:
                    pass
            final_amt = self._calc_bet_amount(round_num, predicted)
            if final_amt <= 0 or not _validate_bet_amount(final_amt):
                self._log("[금액검증] %s회 배팅 스킵 — 직전 회차 결과 미수신" % round_num)
                self._pending_bet_rounds.pop(round_num, None)
                return
            # 회차 재조회 생략 — 픽 수신 직후 배팅, 속도 우선 (마틴 끄면 이미 생략)
            self._log("%s회 %s %s원 실행" % (round_num, pick_color, final_amt))
            ok = self._do_bet(round_num, pick_color, final_amt)
            if ok:
                with self._macro_lock:
                    self._bet_history[round_num] = predicted
                self._last_bet_round = round_num
                self._last_bet_amount = final_amt
                self._bet_rounds_done.add(round_num)
                if len(self._bet_rounds_done) > 50:
                    for r in sorted(self._bet_rounds_done)[:-50]:
                        self._bet_rounds_done.discard(r)
                QTimer.singleShot(30, self._poll)
            else:
                self._pending_bet_rounds.pop(round_num, None)
        delay_ms = int(D_BEFORE_EXECUTE * 1000)
        if HAS_PYQT and delay_ms > 0:
            QTimer.singleShot(delay_ms, _execute)
        else:
            _execute()

    def _do_bet(self, round_num, pick_color, amount):
        """한 회차당 1번만 배팅. 픽(RED/BLACK) 버튼은 절대 1회만 탭 — 2중배팅 방지."""
        with self._do_bet_lock:  # 동시 실행 방지 — 픽 1회만 클릭 보장
            if round_num in self._bet_rounds_done:
                return False  # 이미 배팅 완료 — 중복 방지
            if self._last_bet_round is not None and round_num <= self._last_bet_round:
                return False  # 이미 배팅한 회차 — 중복 방지
            if not _validate_bet_amount(amount):
                self._log("배팅금액 오류: %s (1~99,999,999 범위)" % amount)
                return False
            coords = load_coords()
            device = self._device_id or None
            bet_xy = coords.get("bet_amount")
            red_xy = coords.get("red")
            black_xy = coords.get("black")
            if not bet_xy or len(bet_xy) < 2:
                self._log("배팅금액 좌표 없음")
                return False
            color_xy = red_xy if pick_color == "RED" else black_xy
            if not color_xy or len(color_xy) < 2:
                self._log("%s 좌표 없음" % ("레드" if pick_color == "RED" else "블랙"))
                return False

            bet_amount = str(int(amount))
            self._log("[금액확인] %s회 %s원 입력 예정" % (round_num, bet_amount))
            try:
                def _input_amount_once():
                    tx, ty = _apply_window_offset(coords, bet_xy[0], bet_xy[1], key="bet_amount")
                    adb_swipe(device, tx, ty, SWIPE_AMOUNT_MS)
                    time.sleep(D_AMOUNT_TAP)
                    adb_input_text(device, bet_amount)
                    time.sleep(D_INPUT)
                    # BACK 키 사용 안 함 — 금액 넣고 바로 레드/블랙 탭 (앱 나가기 원인)
                    time.sleep(D_BACK)
                _input_amount_once()

                # 픽 버튼 1회만 탭 — 2중배팅 절대 방지 (정정 버튼 탭 제거 — 밖으로 튕김 방지)
                cx, cy = _apply_window_offset(coords, color_xy[0], color_xy[1], key="red" if pick_color == "RED" else "black")
                adb_swipe(device, cx, cy, SWIPE_COLOR_MS)
                time.sleep(D_COLOR)

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
