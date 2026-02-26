# -*- coding: utf-8 -*-
"""
매크로 독립형 — 분석기에서 픽+결과만 받고, 계산·배팅은 매크로에서 직접 수행.
- API: GET /api/macro-data?calculator=N → pick, round_actuals, graph_values, cards
- emulator_macro와 동일: 계산기 선택, 좌표 설정, ADB 연결/테스트
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

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
        QLabel, QLineEdit, QPushButton, QGroupBox, QFormLayout, QFrame,
        QScrollArea, QGridLayout, QTableWidget, QTableWidgetItem, QHeaderView,
        QCheckBox, QComboBox, QSizePolicy, QFileDialog,
    )
    from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QPointF
    from PyQt5.QtGui import QFont, QColor, QBrush, QPainter, QPen, QPolygonF
    HAS_PYQT = True
except ImportError:
    HAS_PYQT = False

COORD_KEYS = {"bet_amount": "배팅금액", "confirm": "정정", "red": "레드", "black": "블랙"}
COORD_BTN_SHORT = {"bet_amount": "금액", "confirm": "정정", "red": "레드", "black": "블랙"}

# 일반마틴 9단계 (2배: 1,2,4,8,16,32,64,128,256 × base)
MARTIN_RATIOS_NORMAL = [1, 2, 4, 8, 16, 32, 64, 128, 256]
# 표마틴 9단계 (비율: 1,2,3,6,11,21,40,76,120 × base)
MARTIN_RATIOS_TABLE = [1, 2, 3, 6, 11, 21, 40, 76, 120]
ODDS = 1.97

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COORDS_PATH = os.path.join(SCRIPT_DIR, "emulator_coords.json")
MACRO_HISTORY_PATH = os.path.join(SCRIPT_DIR, "macro_calc_history.json")


def _emulator_script_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return SCRIPT_DIR


def normalize_analyzer_url(s):
    s = (s or "").strip().rstrip("/")
    if not s:
        return ""
    try:
        from urllib.parse import urlparse
        p = urlparse(s if "://" in s else "https://" + s)
        base = (p.scheme or "https") + "://" + (p.netloc or p.path.split("/")[0])
        return base.rstrip("/")
    except Exception:
        return s.split("/")[0] if s else ""


def _ws_url_from_analyzer(base_url, calculator_id=1):
    """분석기 base URL → WebSocket 연결 URL (wss/https, ?calculator=N)."""
    if not base_url or not base_url.strip():
        return ""
    s = base_url.strip().rstrip("/")
    if "://" not in s:
        s = "https://" + s
    if s.startswith("https://"):
        ws_base = "https://" + s[8:]
    elif s.startswith("http://"):
        ws_base = "http://" + s[7:]
    else:
        ws_base = s
    return ws_base + "?calculator=" + str(int(calculator_id) if calculator_id in (1, 2, 3) else 1)


def fetch_current_pick(analyzer_url, calculator_id=1, timeout=5):
    """GET /api/current-pick-relay?calculator=N — 기존 emulator_macro와 동일. 계산기 배팅중 픽 직접 수신."""
    base = normalize_analyzer_url(analyzer_url)
    if not base:
        return {"pick_color": None, "round": None}, "URL 없음"
    url = base + "/api/current-pick-relay"
    params = {"calculator": int(calculator_id) if calculator_id in (1, 2, 3) else 1}
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json(), None
    except requests.exceptions.Timeout:
        return {"pick_color": None, "round": None}, "타임아웃"
    except requests.exceptions.ConnectionError as e:
        return {"pick_color": None, "round": None}, str(e)[:80] if e else "연결 실패"
    except Exception as e:
        return {"pick_color": None, "round": None}, str(e)[:100]


def fetch_macro_data(analyzer_url, calculator_id=1, timeout=10):
    """GET /api/macro-data?calculator=N → round_actuals, graph_values, cards (픽 제외). 픽은 current-pick-relay 사용."""
    base = normalize_analyzer_url(analyzer_url)
    if not base:
        return None, "URL 없음"
    url = base + "/api/macro-data"
    params = {"calculator": int(calculator_id) if calculator_id in (1, 2, 3) else 1}
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json(), None
    except requests.exceptions.Timeout:
        return None, "타임아웃 (%ss)" % timeout
    except requests.exceptions.ConnectionError as e:
        return None, "연결 실패: %s" % (str(e)[:80] if e else "네트워크 확인")
    except requests.exceptions.HTTPError as e:
        return None, "HTTP %s" % (str(e)[:80] if e else "오류")
    except Exception as e:
        return None, str(e)[:100]


def load_coords():
    if not os.path.exists(COORDS_PATH):
        return {}
    try:
        with open(COORDS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_coords(data):
    try:
        with open(COORDS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_macro_history():
    """매크로 계산기 history·first_bet_round·last_bet_round 로드."""
    path = getattr(sys, "frozen", False) and os.path.join(_emulator_script_dir(), "macro_calc_history.json") or MACRO_HISTORY_PATH
    if not os.path.exists(path):
        return [], None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        hist = data.get("history") or []
        first = data.get("first_bet_round")
        last = data.get("last_bet_round")
        return hist, first, last
    except Exception:
        return [], None, None


def save_macro_history(history, first_bet_round, last_bet_round):
    """매크로 계산기 history·first_bet_round·last_bet_round 저장."""
    path = getattr(sys, "frozen", False) and os.path.join(_emulator_script_dir(), "macro_calc_history.json") or MACRO_HISTORY_PATH
    try:
        data = {"history": history, "first_bet_round": first_bet_round, "last_bet_round": last_bet_round}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_window_rect_at(screen_x, screen_y):
    """클릭한 점이 속한 창의 클라이언트 영역 (left, top, width, height)."""
    if os.name != "nt":
        return None
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
        root = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
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


def get_device_size_via_adb(device_id=None):
    """adb shell wm size 로 기기 해상도 (width, height)."""
    try:
        rc, out, err = _run_adb_shell_cmd(device_id, "shell", "wm", "size")
        combined = (out or "") + (err or "")
        m = re.search(r"(\d+)\s*x\s*(\d+)", combined, re.IGNORECASE)
        if m:
            return (int(m.group(1)), int(m.group(2)))
    except Exception:
        pass
    return (0, 0)


def _run_adb_shell_cmd(device_id, *args):
    """Windows CMD 호환. 반환: (returncode, stdout, stderr)."""
    kw = {"capture_output": True, "text": True, "timeout": 10, "encoding": "utf-8", "errors": "replace"}
    if os.name == "nt":
        cmd = "adb -s %s %s" % (device_id, " ".join(str(a) for a in args)) if device_id else "adb " + " ".join(str(a) for a in args)
        kw["shell"] = True
    else:
        cmd = ["adb"] + (["-s", device_id] if device_id else []) + list(args)
    try:
        r = subprocess.run(cmd, **kw)
        out = (r.stdout or "").replace("\r\n", "\n").strip()
        err = (r.stderr or "").replace("\r\n", "\n").strip()
        return (r.returncode, out, err)
    except Exception as e:
        return (-1, "", str(e))


def _run_adb_raw(device_id, *args):
    kw = {"capture_output": True, "timeout": 10, "encoding": "utf-8", "errors": "replace"}
    if os.name == "nt":
        cmd = "adb -s %s %s" % (device_id, " ".join(str(a) for a in args)) if device_id else "adb " + " ".join(str(a) for a in args)
        kw["shell"] = True
    else:
        cmd = ["adb"] + (["-s", device_id] if device_id else []) + list(args)
    try:
        subprocess.run(cmd, **kw)
    except Exception:
        pass


def adb_swipe(device_id, x, y, duration_ms=80):
    x, y = int(x), int(y)
    _run_adb_raw(device_id, "shell", "input", "swipe", str(x), str(y), str(x), str(y), str(duration_ms))


def adb_input_text(device_id, text):
    escaped = text.replace(" ", "%s")
    _run_adb_raw(device_id, "shell", "input", "text", escaped)


def adb_keyevent(device_id, keycode):
    _run_adb_raw(device_id, "shell", "input", "keyevent", str(keycode))


def _validate_bet_amount(amt):
    if amt is None:
        return False
    try:
        v = int(amt)
        return 1 <= v <= 99999999
    except (TypeError, ValueError):
        return False


def _apply_window_offset(coords, x, y, key=None):
    """좌표 보정. emulator_macro와 동일."""
    try:
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return x, y
    if coords.get("raw_coords"):
        dev_w = int(coords.get("device_width") or 0)
        dev_h = int(coords.get("device_height") or 0)
        if dev_w > 0 and dev_h > 0:
            x, y = max(0, min(dev_w - 1, x)), max(0, min(dev_h - 1, y))
        return x, y
    spaces = coords.get("coord_spaces") or {}
    is_window_relative = spaces.get(key, coords.get("coords_are_window_relative")) if key else coords.get("coords_are_window_relative")
    if is_window_relative:
        rx, ry = x, y
    else:
        ox = int(coords.get("window_left") or 0)
        oy = int(coords.get("window_top") or 0)
        rx, ry = x - ox, y - oy
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


def build_martin_table(base_amount, martin_type="table"):
    """1단계 배팅 금액으로 마틴 테이블 생성. martin_type: 'normal'(일반마틴) | 'table'(표마틴)."""
    try:
        b = int(base_amount)
        if b <= 0:
            b = 5000
    except (TypeError, ValueError):
        b = 5000
    ratios = MARTIN_RATIOS_NORMAL if martin_type == "normal" else MARTIN_RATIOS_TABLE
    return [b * r for r in ratios]


def calc_martingale_step_and_bet(history, martin_table=None):
    """
    history: [{round, predicted, actual, ...}] 완료된 순서.
    predicted: 정/꺽 (RED→정, BLACK→꺽) 또는 보류(배팅 안 함, 스텝 변화 없음)
    actual: 정/꺽/joker
    반환: (martingale_step, next_bet_amount)
    """
    martin_table = martin_table or build_martin_table(5000, "table")
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


def _normalize_pick_color(pc):
    """RED/BLACK/빨강/검정 → RED 또는 BLACK. None/미인식 시 None."""
    s = (pc or "").strip().upper()
    if s in ("RED", "빨강"):
        return "RED"
    if s in ("BLACK", "검정"):
        return "BLACK"
    return None


def pick_color_to_pred(pick_color, card_15_color=None):
    """pick_color(RED/BLACK) → 정/꺽. 15번 카드 색에 따라 매핑 반대.
    - 15번 빨강 또는 미확인: RED→정, BLACK→꺽
    - 15번 검정: RED→꺽, BLACK→정 (PREDICTION_AND_RESULT_SPEC 3.3)"""
    n = _normalize_pick_color(pick_color)
    if n is None:
        return None
    c15 = _normalize_pick_color(card_15_color) if card_15_color else None
    if c15 == "BLACK":
        return "꺽" if n == "RED" else "정"
    return "정" if n == "RED" else "꺽"


def pred_to_pick_color(pred, card_15_color=None):
    """정/꺽 → 배팅 색(RED/BLACK). 15번 카드에 따라 매핑.
    - 15번 빨강 또는 미확인: 정→RED, 꺽→BLACK
    - 15번 검정: 정→BLACK, 꺽→RED"""
    if pred not in ("정", "꺽"):
        return None
    c15 = _normalize_pick_color(card_15_color) if card_15_color else None
    if c15 == "BLACK":
        return "BLACK" if pred == "정" else "RED"
    return "RED" if pred == "정" else "BLACK"


class MacroStandaloneWindow(QMainWindow if HAS_PYQT else object):
    _test_done_signal = pyqtSignal(str, str) if HAS_PYQT else None  # (which, msg) — API/ADB 테스트 완료
    _adb_device_suggested = pyqtSignal(str) if HAS_PYQT else None  # 연결된 기기 ID 자동 채움
    _ws_pick_received = pyqtSignal(object) if HAS_PYQT else None  # WebSocket 픽 수신 → 즉시 배팅
    _poll_result_signal = pyqtSignal(object) if HAS_PYQT else None  # 폴 결과 (백그라운드) → 메인 스레드 UI 업데이트
    _round_actuals_signal = pyqtSignal(object) if HAS_PYQT else None  # WebSocket round_actuals 수신

    def __init__(self):
        super().__init__()
        self.setWindowTitle("매크로 독립형 — 픽+결과 수신, 직접 계산")
        self.setMinimumSize(500, 900)
        self.resize(540, 950)

        self._analyzer_url = ""
        self._device_id = "127.0.0.1:5555"
        self._running = False
        self._coords = {}
        self._last_bet_round = None
        self._first_bet_round = None  # 첫 ADB 배팅 회차 — 이 회차부터만 금액·수익 표시
        self._waiting_for_first_win = False  # True: 첫 승 나올 때까지 배팅 대기
        self._pending_bet_rounds = {}
        self._lock = threading.Lock()

        # 매크로 내부 상태
        self._pick = {"round": None, "pick_color": None}
        self._round_actuals = {}
        self._cards = []
        self._history = []  # [{round, predicted, actual, result, betAmount, profit}]
        self._poll_timer = None
        self._coord_listener = None
        self._coord_capture_key = None
        self._pending_coord_click = None
        self._ws_client = None
        self._ws_thread = None
        self._ws_connected = False
        self._flip_pick = False
        self._last_history_save_at = 0
        self._last_ui_sig = None  # UI 스킵용: 데이터 변경 시에만 갱신
        self._session_start_at = None  # 시작 버튼 누른 시각 — 배팅 통계용

        self._build_ui()
        self._load_coords()
        # 과거 기록 불러오지 않음 — 연결 시점부터 PC에 새로 저장
        if HAS_PYQT and self._test_done_signal is not None:
            self._test_done_signal.connect(self._on_test_done)
        if HAS_PYQT and self._adb_device_suggested is not None:
            self._adb_device_suggested.connect(self._on_adb_device_suggested)
        if HAS_PYQT and self._ws_pick_received is not None:
            self._ws_pick_received.connect(self._on_ws_pick_received)
        if HAS_PYQT and self._poll_result_signal is not None:
            self._poll_result_signal.connect(self._on_poll_result)
        if HAS_PYQT and self._round_actuals_signal is not None:
            self._round_actuals_signal.connect(self._on_round_actuals_received)

    def _get_macro_settings(self):
        """매크로 설정: 시작 금액, 마틴 테이블, 배당."""
        try:
            cap = int(self.capital_edit.text().strip() or 0)
            if cap <= 0:
                cap = 1000000
        except (TypeError, ValueError):
            cap = 1000000
        try:
            base = int(self.base_bet_edit.text().strip() or 0)
            if base <= 0:
                base = 5000
        except (TypeError, ValueError):
            base = 5000
        try:
            odds = float(self.odds_edit.text().strip() or 1.97)
            if odds < 1.0 or odds > 10.0:
                odds = 1.97
        except (TypeError, ValueError):
            odds = 1.97
        martin_type = "normal" if (hasattr(self, "martin_combo") and self.martin_combo and self.martin_combo.currentData() == "normal") else "table"
        return cap, build_martin_table(base, martin_type), odds

    def _load_macro_history(self):
        """저장된 history·first_bet_round·last_bet_round 복원."""
        hist, first, last = load_macro_history()
        if hist:
            self._history = hist
            if first is not None:
                self._first_bet_round = int(first) if isinstance(first, (int, float)) else first
            if last is not None:
                self._last_bet_round = int(last) if isinstance(last, (int, float)) else last

    def _save_macro_history(self, force=False):
        """history·first_bet_round·last_bet_round 저장. force=True 또는 3초 경과 시 저장. I/O는 스레드에서(메인 블로킹 방지)."""
        now = time.time()
        if not force and now - self._last_history_save_at < 3:
            return
        self._last_history_save_at = now
        with self._lock:
            hist = list(self._history)
            first = self._first_bet_round
            last = self._last_bet_round

        def _do_save():
            save_macro_history(hist, first, last)

        threading.Thread(target=_do_save, daemon=True).start()

    def _load_coords(self):
        self._coords = load_coords()
        self._refresh_coord_labels()
        if hasattr(self, "capital_edit") and self.capital_edit:
            self.capital_edit.setText(str(self._coords.get("macro_capital") or 1000000))
        if hasattr(self, "base_bet_edit") and self.base_bet_edit:
            self.base_bet_edit.setText(str(self._coords.get("macro_base") or 5000))
        if hasattr(self, "odds_edit") and self.odds_edit:
            self.odds_edit.setText(str(self._coords.get("macro_odds") or 1.97))
        self._flip_pick = bool(self._coords.get("macro_flip_pick", False))
        if hasattr(self, "flip_pick_check") and self.flip_pick_check:
            self.flip_pick_check.setChecked(self._flip_pick)
        if hasattr(self, "martin_combo") and self.martin_combo:
            mt = self._coords.get("macro_martin_type", "table")
            idx = self.martin_combo.findData(mt)
            if idx >= 0:
                self.martin_combo.setCurrentIndex(idx)
        if hasattr(self, "window_left_edit") and self.window_left_edit:
            self.window_left_edit.setText(str(self._coords.get("window_left") or ""))
            self.window_top_edit.setText(str(self._coords.get("window_top") or ""))
            self.window_width_edit.setText(str(self._coords.get("window_width") or ""))
            self.window_height_edit.setText(str(self._coords.get("window_height") or ""))
            self.device_width_edit.setText(str(self._coords.get("device_width") or ""))
            self.device_height_edit.setText(str(self._coords.get("device_height") or ""))
            self.raw_coords_check.setChecked(bool(self._coords.get("raw_coords", False)))

    def _refresh_coord_labels(self):
        for key in COORD_KEYS:
            val = self._coords.get(key)
            lb = getattr(self, "_coord_value_labels", {}).get(key)
            if lb:
                lb.setText("(%s,%s)" % (val[0], val[1]) if val and len(val) >= 2 else "(미설정)")

    def _set_coord_status(self, key, text, color="green"):
        if key in getattr(self, "_coord_status_labels", {}):
            self._coord_status_labels[key].setText(text)
            self._coord_status_labels[key].setStyleSheet("color: %s; font-size: 11px;" % color)

    def _on_flip_pick_changed(self, state):
        self._flip_pick = bool(state)
        self._coords = load_coords()
        self._coords["macro_flip_pick"] = self._flip_pick
        save_coords(self._coords)

    def _on_martin_type_changed(self):
        if hasattr(self, "martin_combo") and self.martin_combo:
            mt = self.martin_combo.currentData()
            if mt:
                self._coords = load_coords()
                self._coords["macro_martin_type"] = mt
                save_coords(self._coords)

    def _get_effective_pick_color(self, raw_pc):
        """정규화 후 픽 반대로 옵션 적용. RED/BLACK 반환 또는 None."""
        n = _normalize_pick_color(raw_pc)
        if n is None:
            return None
        if self._flip_pick:
            return "BLACK" if n == "RED" else "RED"
        return n

    def _get_card_15_color(self):
        """15번 카드 색 (RED/BLACK). cards[14] = 15번 카드. 정/꺽→색 매핑에 사용."""
        with self._lock:
            cards = list(self._cards)
        if len(cards) > 14:
            c = cards[14].get("color")
            return _normalize_pick_color(c) if c else None
        return None

    def _on_analyzer_nick_changed(self, nick):
        if nick and nick in self._analyzer_nick_urls:
            self.analyzer_url_edit.setText(self._analyzer_nick_urls[nick])

    def _on_test_done(self, which, msg):
        """시그널로 메인 스레드에서 호출 — API/ADB 테스트 완료."""
        if which == "api":
            self.api_test_btn.setEnabled(True)
            self.api_test_btn.setText("API 연결 확인")
            if "API 연결됨" in (msg or ""):
                QTimer.singleShot(0, self._poll_once)
                QTimer.singleShot(400, self._start_ws_client)  # 폴링 먼저, WS는 400ms 후 — 동시 시작 방지
                self._poll_timer = True
                self._poll_loop()
                self._log("픽/카드/그래프 수신 중. 시작 버튼으로 배팅 활성화.")
        elif which == "ws":
            self._log(msg or "")
        elif which == "adb":
            self.adb_devices_btn.setEnabled(True)
            self.adb_devices_btn.setText("ADB 연결 확인")
        elif which == "adb_bet":
            self.adb_bet_btn.setEnabled(True)
            self.adb_bet_btn.setText("배팅금액 테스트 (5000원)")
        self._log(msg)

    def _on_api_test(self):
        url = self.analyzer_url_edit.text().strip()
        calc_id = self.calc_combo.currentData()
        self.api_test_btn.setEnabled(False)
        self.api_test_btn.setText("확인 중...")

        def run():
            try:
                data, err = fetch_macro_data(url, calculator_id=calc_id)
                msg = "API 연결됨 (계산기 %s)" % calc_id if not err and data else ("API 실패: %s" % (err or "응답 없음"))
            except Exception as e:
                msg = "API 오류: %s" % str(e)[:80]
            if self._test_done_signal:
                self._test_done_signal.emit("api", msg)

        threading.Thread(target=run, daemon=True).start()

    def _on_adb_device_suggested(self, device_id):
        """ADB 연결 확인 시 동작하는 기기 ID로 ADB 기기 칸 자동 채움 (emulator_macro와 동일)."""
        if device_id and hasattr(self, "device_edit"):
            self.device_edit.setText(device_id)
            self._log("ADB 기기 칸을 [%s] 로 자동 채움." % device_id)

    def _on_adb_devices(self):
        """CMD와 동일한 방식으로 adb 실행 후 기기 목록·실제 연결 테스트 (emulator_macro와 동일)."""
        user_device = (self.device_edit.text().strip() or "").strip() or "127.0.0.1:5555"
        self.adb_devices_btn.setEnabled(False)
        self.adb_devices_btn.setText("확인 중...")
        self._log("ADB 연결 확인 중... (CMD와 동일한 방식으로 실행)")

        def run():
            msg = ""
            try:
                _run_adb_shell_cmd(None, "start-server")
                time.sleep(0.3)
                code, out, err = _run_adb_shell_cmd(None, "devices")
                out = out or ""
                err = err or ""
                msg = "ADB devices (원본):\n" + (out if out else "(stdout 없음)")
                if err:
                    msg += "\n[stderr] " + err
                raw_lines = out.replace("\r", "").split("\n")
                device_ids = []
                for line in raw_lines:
                    line = line.strip()
                    if "\t" in line and line.endswith("device"):
                        device_ids.append(line.split("\t")[0].strip())

                def test_connection(dev):
                    rc, o, e = _run_adb_shell_cmd(dev, "shell", "echo", "ok")
                    return rc == 0

                ok_user = test_connection(user_device)
                if ok_user:
                    msg += "\n→ 연결됨. [%s] 로 실제 명령 전송 확인됨." % user_device
                elif not device_ids:
                    common_ports = [5554, 5555, 5556, 5557, 62001]
                    tried = []
                    connected_device = None
                    for port in common_ports:
                        addr = "127.0.0.1:%s" % port
                        tried.append(addr)
                        _run_adb_shell_cmd(None, "connect", addr)
                        time.sleep(0.2)
                        if test_connection(addr):
                            connected_device = addr
                            break
                    if connected_device:
                        msg += "\n→ 기기 목록 비었으나 포트 자동 시도 → [%s] 연결됨. ADB 기기 칸 채움." % connected_device
                        if self._adb_device_suggested:
                            self._adb_device_suggested.emit(connected_device)
                    else:
                        msg += "\n→ 기기 목록 비어 있음. 시도한 주소: " + ", ".join(tried)
                        msg += "\n   LDPlayer가 실행 중인지, 설정 → 기타 설정 → ADB 디버깅 켜기 확인."
                        if err:
                            msg += "\n   [원인 추정] stderr: " + err[:200]
                else:
                    working = []
                    for did in device_ids:
                        if test_connection(did):
                            working.append(did)
                    if working:
                        msg += "\n→ [%s] 로는 실패. [%s] 로 연결됨 → ADB 기기 칸 자동 채움." % (user_device, working[0])
                        if self._adb_device_suggested:
                            self._adb_device_suggested.emit(working[0])
                    else:
                        msg += "\n→ 기기는 보이지만 shell 명령 실패. stderr 확인."
            except Exception as e:
                msg += "\n예외: " + str(e)
            if self._test_done_signal:
                self._test_done_signal.emit("adb", msg)

        threading.Thread(target=run, daemon=True).start()

    def _on_adb_bet_test(self):
        self._coords = load_coords()
        bet_xy = self._coords.get("bet_amount")
        if not bet_xy or len(bet_xy) < 2:
            self._log("배팅금액 좌표 없음 — 좌표 설정 열기에서 먼저 잡으세요")
            return
        self.adb_bet_btn.setEnabled(False)
        self.adb_bet_btn.setText("테스트 중...")
        device = self.device_edit.text().strip() or None
        coords = dict(self._coords)
        def run():
            try:
                tx, ty = _apply_window_offset(coords, bet_xy[0], bet_xy[1], key="bet_amount")
                adb_swipe(device, tx, ty, 100)
                time.sleep(0.6)
                adb_input_text(device, "5000")
                time.sleep(0.5)
                adb_keyevent(device, 4)
                msg = "배팅금액 테스트 완료 (5000 입력)"
            except Exception as e:
                msg = "배팅금액 테스트 실패: %s" % str(e)[:80]
            if self._test_done_signal:
                self._test_done_signal.emit("adb_bet", msg)
        threading.Thread(target=run, daemon=True).start()

    def _on_adb_color_tap(self, key):
        self._coords = load_coords()
        if key == "confirm":
            xy = self._coords.get("confirm")
        else:
            xy = self._coords.get("red") if key == "red" else self._coords.get("black")
        if not xy or len(xy) < 2:
            self._log("%s 좌표 없음 — 좌표 설정 열기에서 먼저 잡으세요" % (COORD_KEYS.get(key, key)))
            return
        device = self.device_edit.text().strip() or None
        coords = dict(self._coords)
        coord_key = "confirm" if key == "confirm" else ("red" if key == "red" else "black")
        tx, ty = _apply_window_offset(coords, xy[0], xy[1], key=coord_key)
        adb_swipe(device, tx, ty, 100)
        self._log("%s 탭 완료" % COORD_KEYS.get(coord_key, coord_key))

    def _start_coord_capture(self, key):
        if not HAS_PYNPUT:
            self._log("pynput 설치 필요: pip install pynput")
            return
        if self._coord_listener is not None:
            self._log("다른 좌표 잡는 중입니다. 잠시 후 다시 시도하세요.")
            return
        self._coord_capture_key = key
        if key == "window_topleft":
            self._log("창 왼쪽 위 잡기: 이 창 최소화 후 LDPlayer 창 왼쪽 위 모서리 클릭")
        else:
            self._log("좌표 찾기: 이 창 최소화 후 LDPlayer 창 안에서만 클릭")
            self._set_coord_status(key, "검색중", "green")
        self.showMinimized()
        self._coord_listener = pynput_mouse.Listener(on_click=self._on_coord_click)
        self._coord_listener.start()

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
        self.activateWindow()
        if self._pending_coord_click is None:
            return
        key, x, y = self._pending_coord_click
        self._pending_coord_click = None
        if key == "window_topleft":
            self._coords["window_left"] = int(x)
            self._coords["window_top"] = int(y)
            self.window_left_edit.setText(str(x))
            self.window_top_edit.setText(str(y))
            save_coords(self._coords)
            self._log("창 왼쪽 위 저장: X=%s, Y=%s" % (x, y))
            return
        rect = get_window_rect_at(x, y)
        if rect is not None:
            left, top, w, h = rect
            rel_x, rel_y = x - left, y - top
            self._coords[key] = [rel_x, rel_y]
            self._coords["window_left"] = left
            self._coords["window_top"] = top
            self._coords["window_width"] = w
            self._coords["window_height"] = h
            if not int(self._coords.get("device_width") or 0) or not int(self._coords.get("device_height") or 0):
                self._coords["device_width"] = w
                self._coords["device_height"] = h
                self.device_width_edit.setText(str(w))
                self.device_height_edit.setText(str(h))
            sp = self._coords.get("coord_spaces") or {}
            sp[key] = True
            self._coords["coord_spaces"] = sp
            self.window_left_edit.setText(str(left))
            self.window_top_edit.setText(str(top))
            self.window_width_edit.setText(str(w))
            self.window_height_edit.setText(str(h))
            save_coords(self._coords)
            self._refresh_coord_labels()
            self._set_coord_status(key, "저장됨", "green")
            self._log("%s = (%s,%s) 저장" % (COORD_KEYS.get(key, key), rel_x, rel_y))
        else:
            self._coords[key] = [x, y]
            sp = self._coords.get("coord_spaces") or {}
            sp[key] = False
            self._coords["coord_spaces"] = sp
            self._coords["window_left"] = int(self.window_left_edit.text().strip() or 0)
            self._coords["window_top"] = int(self.window_top_edit.text().strip() or 0)
            self._coords["raw_coords"] = self.raw_coords_check.isChecked()
            save_coords(self._coords)
            self._refresh_coord_labels()
            self._set_coord_status(key, "저장됨", "green")
            self._log("%s = (%s,%s) 저장 (창 자동 감지 실패)" % (COORD_KEYS.get(key, key), x, y))
        QTimer.singleShot(1500, lambda: self._set_coord_status(key, ""))

    def _save_window_offset(self):
        try:
            self._coords["window_left"] = int(self.window_left_edit.text().strip() or 0)
            self._coords["window_top"] = int(self.window_top_edit.text().strip() or 0)
            self._coords["raw_coords"] = self.raw_coords_check.isChecked()
            for k, edit in [("window_width", self.window_width_edit), ("window_height", self.window_height_edit),
                            ("device_width", self.device_width_edit), ("device_height", self.device_height_edit)]:
                v = int(edit.text().strip() or 0)
                self._coords[k] = v if v > 0 else 0
            save_coords(self._coords)
            self._log("창 위치·해상도 저장됨")
        except (TypeError, ValueError):
            self._log("숫자만 입력하세요.")

    def _on_fetch_device_size(self):
        device = self.device_edit.text().strip() or None
        self.device_size_fetch_btn.setEnabled(False)
        self.device_size_fetch_btn.setText("가져오는 중...")
        def run():
            w, h = get_device_size_via_adb(device)
            QTimer.singleShot(0, lambda: self._device_size_done(w, h))
        threading.Thread(target=run, daemon=True).start()

    def _device_size_done(self, w, h):
        self.device_size_fetch_btn.setEnabled(True)
        self.device_size_fetch_btn.setText("기기 해상도 가져오기")
        if w > 0 and h > 0:
            self.device_width_edit.setText(str(w))
            self.device_height_edit.setText(str(h))
            self._coords["device_width"] = w
            self._coords["device_height"] = h
            save_coords(self._coords)
            self._log("기기 해상도 %s×%s 가져옴" % (w, h))
        else:
            self._log("기기 해상도 가져오기 실패. ADB 연결 확인.")

    def _build_ui(self):
        cw = QWidget()
        layout = QVBoxLayout()

        # 설정
        g_set = QGroupBox("설정")
        fl = QFormLayout()
        self._analyzer_nick_urls = {
            "표마왕": "https://web-production-3f4f0.up.railway.app",
            "규지니": "https://web-production-28c2.up.railway.app",
        }
        self.analyzer_nick_combo = QComboBox()
        self.analyzer_nick_combo.addItem("표마왕")
        self.analyzer_nick_combo.addItem("규지니")
        self.analyzer_nick_combo.currentTextChanged.connect(self._on_analyzer_nick_changed)
        fl.addRow("분석기(닉네임):", self.analyzer_nick_combo)
        self.analyzer_url_edit = QLineEdit()
        self.analyzer_url_edit.setPlaceholderText("분석기 URL 루트")
        self.analyzer_url_edit.setText(self._analyzer_nick_urls.get("표마왕", ""))
        fl.addRow("Analyzer URL:", self.analyzer_url_edit)

        self.calc_combo = QComboBox()
        self.calc_combo.addItem("계산기 1", 1)
        self.calc_combo.addItem("계산기 2", 2)
        self.calc_combo.addItem("계산기 3", 3)
        self.calc_combo.currentIndexChanged.connect(self._on_calc_combo_changed)
        fl.addRow("계산기 선택:", self.calc_combo)

        self.capital_edit = QLineEdit()
        self.capital_edit.setPlaceholderText("1000000")
        self.capital_edit.setText("1000000")
        self.capital_edit.setMaximumWidth(120)
        self.capital_edit.setToolTip("시작 자본금 (마틴 상한)")
        fl.addRow("시작 금액 (원):", self.capital_edit)

        self.base_bet_edit = QLineEdit()
        self.base_bet_edit.setPlaceholderText("5000")
        self.base_bet_edit.setText("5000")
        self.base_bet_edit.setMaximumWidth(120)
        self.base_bet_edit.setToolTip("1단계 배팅 금액")
        fl.addRow("1단계 배팅 (원):", self.base_bet_edit)

        self.martin_combo = QComboBox()
        self.martin_combo.addItem("표마틴 (1,2,3,6,11,21,40,76,120)", "table")
        self.martin_combo.addItem("일반마틴 (2배: 1,2,4,8,16,32,64,128,256)", "normal")
        self.martin_combo.setToolTip("마틴 규칙 선택")
        self.martin_combo.currentIndexChanged.connect(self._on_martin_type_changed)
        fl.addRow("마틴 규칙:", self.martin_combo)

        self.odds_edit = QLineEdit()
        self.odds_edit.setPlaceholderText("1.97")
        self.odds_edit.setText("1.97")
        self.odds_edit.setMaximumWidth(80)
        self.odds_edit.setToolTip("승리 시 배당 (예: 1.97 = 97% 수익)")
        fl.addRow("배당:", self.odds_edit)

        self.flip_pick_check = QCheckBox("픽 반대로 (RED↔BLACK)")
        self.flip_pick_check.setToolTip("분석기와 픽이 반대로 들어올 때 체크. 레드/블랙 좌표가 바뀐 경우 등.")
        self.flip_pick_check.stateChanged.connect(self._on_flip_pick_changed)
        fl.addRow("", self.flip_pick_check)

        api_row = QHBoxLayout()
        self.api_test_btn = QPushButton("API 연결 확인")
        self.api_test_btn.setMinimumHeight(28)
        self.api_test_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.api_test_btn.clicked.connect(self._on_api_test)
        api_row.addWidget(self.api_test_btn)
        fl.addRow("", api_row)

        self.device_edit = QLineEdit()
        self.device_edit.setPlaceholderText("127.0.0.1:5555")
        self.device_edit.setText("127.0.0.1:5555")
        self.device_edit.setMaximumWidth(180)
        fl.addRow("ADB 기기:", self.device_edit)
        adb_row = QHBoxLayout()
        self.adb_devices_btn = QPushButton("ADB 연결 확인")
        self.adb_devices_btn.setMinimumHeight(28)
        self.adb_devices_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.adb_devices_btn.clicked.connect(self._on_adb_devices)
        self.adb_bet_btn = QPushButton("배팅금액 테스트 (5000원)")
        self.adb_bet_btn.setMinimumHeight(28)
        self.adb_bet_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.adb_bet_btn.clicked.connect(self._on_adb_bet_test)
        self.adb_red_btn = QPushButton("레드 1회 탭")
        self.adb_red_btn.setMinimumHeight(28)
        self.adb_red_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.adb_red_btn.clicked.connect(lambda: self._on_adb_color_tap("red"))
        self.adb_black_btn = QPushButton("블랙 1회 탭")
        self.adb_black_btn.setMinimumHeight(28)
        self.adb_black_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.adb_black_btn.clicked.connect(lambda: self._on_adb_color_tap("black"))
        self.adb_confirm_btn = QPushButton("정정 1회 탭")
        self.adb_confirm_btn.setMinimumHeight(28)
        self.adb_confirm_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.adb_confirm_btn.clicked.connect(lambda: self._on_adb_color_tap("confirm"))
        adb_row.addWidget(self.adb_devices_btn)
        adb_row.addWidget(self.adb_bet_btn)
        adb_row.addWidget(self.adb_red_btn)
        adb_row.addWidget(self.adb_black_btn)
        adb_row.addWidget(self.adb_confirm_btn)
        fl.addRow("", adb_row)
        g_set.setLayout(fl)
        layout.addWidget(g_set)

        # 좌표 설정 (LDPlayer에서 클릭해 잡기)
        g_coord = QGroupBox("좌표 설정 (LDPlayer에서 해당 위치 클릭)")
        fl_coord = QFormLayout()
        self._coord_value_labels = {}
        self._coord_status_labels = {}
        coord_btns_row = QHBoxLayout()
        coord_btns_row.setContentsMargins(0, 2, 0, 2)
        for key, label in COORD_KEYS.items():
            short = COORD_BTN_SHORT.get(key, label)
            btn = QPushButton(f"{short} 찾기")
            btn.setMinimumHeight(28)
            btn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            btn.clicked.connect(lambda checked=False, k=key: self._start_coord_capture(k))
            coord_btns_row.addWidget(btn)
            val_lbl = QLabel("(미설정)")
            val_lbl.setMinimumWidth(70)
            coord_btns_row.addWidget(val_lbl)
            status_lbl = QLabel("")
            status_lbl.setStyleSheet("color: green; font-size: 11px;")
            coord_btns_row.addWidget(status_lbl)
            self._coord_value_labels[key] = val_lbl
            self._coord_status_labels[key] = status_lbl
        coord_btns_row.addStretch(1)
        coord_btns_w = QWidget()
        coord_btns_w.setLayout(coord_btns_row)
        fl_coord.addRow(coord_btns_w)
        self.raw_coords_check = QCheckBox("원시 좌표 (보정 없이 저장된 x,y 그대로 전송)")
        self.raw_coords_check.setChecked(False)
        self.raw_coords_check.setStyleSheet("color: #888; font-size: 11px;")
        fl_coord.addRow("", self.raw_coords_check)
        win_row = QHBoxLayout()
        win_row.addWidget(QLabel("LDPlayer 창 왼쪽 위:"))
        self.window_left_edit = QLineEdit()
        self.window_left_edit.setPlaceholderText("X")
        self.window_left_edit.setMaximumWidth(60)
        self.window_top_edit = QLineEdit()
        self.window_top_edit.setPlaceholderText("Y")
        self.window_top_edit.setMaximumWidth(60)
        win_row.addWidget(self.window_left_edit)
        win_row.addWidget(self.window_top_edit)
        self.window_capture_btn = QPushButton("창 왼쪽 위 잡기")
        self.window_capture_btn.setMinimumHeight(28)
        self.window_capture_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.window_capture_btn.clicked.connect(lambda: self._start_coord_capture("window_topleft"))
        win_row.addWidget(self.window_capture_btn)
        self.window_save_btn = QPushButton("창 위치 저장")
        self.window_save_btn.setMinimumHeight(28)
        self.window_save_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.window_save_btn.clicked.connect(self._save_window_offset)
        win_row.addWidget(self.window_save_btn)
        win_row.addStretch(1)
        fl_coord.addRow(win_row)
        res_row = QHBoxLayout()
        res_row.addWidget(QLabel("해상도 보정: 창 W/H"))
        self.window_width_edit = QLineEdit()
        self.window_width_edit.setPlaceholderText("창가로")
        self.window_width_edit.setMaximumWidth(50)
        self.window_height_edit = QLineEdit()
        self.window_height_edit.setPlaceholderText("창세로")
        self.window_height_edit.setMaximumWidth(50)
        res_row.addWidget(self.window_width_edit)
        res_row.addWidget(self.window_height_edit)
        res_row.addWidget(QLabel("기기 W/H"))
        self.device_width_edit = QLineEdit()
        self.device_width_edit.setPlaceholderText("기기가로")
        self.device_width_edit.setMaximumWidth(50)
        self.device_height_edit = QLineEdit()
        self.device_height_edit.setPlaceholderText("기기세로")
        self.device_height_edit.setMaximumWidth(50)
        res_row.addWidget(self.device_width_edit)
        res_row.addWidget(self.device_height_edit)
        self.device_size_fetch_btn = QPushButton("기기 해상도 가져오기")
        self.device_size_fetch_btn.setMinimumHeight(28)
        self.device_size_fetch_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.device_size_fetch_btn.clicked.connect(self._on_fetch_device_size)
        res_row.addWidget(self.device_size_fetch_btn)
        res_row.addStretch(1)
        fl_coord.addRow(res_row)
        if not HAS_PYNPUT:
            fl_coord.addRow("", QLabel("pynput 미설치: pip install pynput"))
        g_coord.setLayout(fl_coord)
        layout.addWidget(g_coord)

        # 계산기 표 (최소 5행 보이도록, 칸 축소·가운데 정렬)
        g_calc = QGroupBox("계산기 표")
        self.calc_table = QTableWidget()
        self.calc_table.setColumnCount(6)
        self.calc_table.setHorizontalHeaderLabels(["회차", "픽", "결과", "승패", "금액", "수익"])
        hh = self.calc_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)  # 회차
        hh.setSectionResizeMode(1, QHeaderView.Fixed)
        self.calc_table.setColumnWidth(1, 36)  # 픽 가로폭 축소
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)  # 결과
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)  # 승패
        hh.setSectionResizeMode(4, QHeaderView.Stretch)  # 금액 — 남은 공간 채움
        hh.setSectionResizeMode(5, QHeaderView.Stretch)  # 수익 — 남은 공간 채움
        row_h = 24
        header_h = 24
        self.calc_table.verticalHeader().setDefaultSectionSize(row_h)
        self.calc_table.setMinimumHeight(row_h * 5 + header_h)
        self.calc_table.setStyleSheet("QTableWidget { font-size: 11px; } QHeaderView::section { text-align: center; }")
        self.calc_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        g_calc_layout = QVBoxLayout()
        g_calc_layout.addWidget(self.calc_table)
        g_calc.setLayout(g_calc_layout)
        layout.addWidget(g_calc)

        # 배팅중 (분석기 계산기 상단 배팅픽과 동일 출처 — macro_pick_transmit)
        g_bet = QGroupBox("배팅중")
        g_bet.setToolTip("분석기 계산기 상단 배팅픽과 동일. 픽 반대로 옵션 적용 시 RED↔BLACK 반전.")
        bet_row = QHBoxLayout()
        self.pick_label = QLabel("—")
        self.pick_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        self.pick_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.amount_label = QLabel("—")
        self.amount_label.setStyleSheet("font-size: 14px;")
        self.amount_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        bet_row.addWidget(self.pick_label)
        bet_row.addWidget(self.amount_label)
        g_bet.setLayout(bet_row)
        layout.addWidget(g_bet)

        # 버튼
        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("시작")
        self.start_btn.clicked.connect(self._on_start)
        self.start_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.stop_btn = QPushButton("정지")
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.refresh_btn = QPushButton("새로고침")
        self.refresh_btn.clicked.connect(self._on_refresh)
        self.refresh_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.save_report_btn = QPushButton("저장하기")
        self.save_report_btn.clicked.connect(self._on_save_report)
        self.save_report_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.save_report_btn.setToolTip("총 배팅내역·금액·순익을 파일로 저장")
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(self.refresh_btn)
        btn_row.addWidget(self.save_report_btn)
        layout.addLayout(btn_row)

        # 로그
        self.log_text = QLabel("")
        self.log_text.setWordWrap(True)
        self.log_text.setStyleSheet("font-size: 11px; color: #666; max-height: 80px;")
        layout.addWidget(self.log_text)

        layout.addStretch()
        cw.setLayout(layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(cw)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setCentralWidget(scroll)

    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        try:
            self.log_text.setText(line)
        except Exception:
            pass

    def _on_save_report(self):
        """총 배팅내역·금액·순익을 보기 좋게 파일로 저장"""
        with self._lock:
            hist = list(self._history)
        first = self._first_bet_round
        if first is None:
            self._log("배팅 내역이 없습니다. 시작 후 배팅한 기록이 있어야 합니다.")
            return
        capital, martin_table, odds = self._get_macro_settings()
        completed = [h for h in hist if (h.get("actual") or "").strip() not in ("pending", "") and (h.get("round") or 0) >= first]
        completed_sorted = sorted(completed, key=lambda x: x.get("round") or 0)
        if not completed_sorted:
            self._log("완료된 배팅 내역이 없습니다.")
            return
        step, next_bet = calc_martingale_step_and_bet(completed_sorted, martin_table=martin_table)
        cap = capital
        round_to_bet_profit = {}
        for h in completed_sorted:
            rn = h["round"]
            act = (h.get("actual") or "").strip()
            pred = (h.get("predicted") or "").strip()
            if pred == "보류":
                round_to_bet_profit[rn] = {"bet": 0, "profit": 0}
                continue
            is_joker = act in ("joker", "조커")
            is_win = not is_joker and pred in ("정", "꺽") and pred == act
            bet = min(next_bet, int(cap))
            if is_joker:
                profit = -bet
                step = min(step + 1, len(martin_table) - 1)
                next_bet = martin_table[step]
            elif is_win:
                profit = int(bet * (odds - 1))
                step = 0
                next_bet = martin_table[0]
            else:
                profit = -bet
                step = min(step + 1, len(martin_table) - 1)
                next_bet = martin_table[step]
            cap += profit
            round_to_bet_profit[rn] = {"bet": bet, "profit": profit}
        total_bet = sum(rp["bet"] for rp in round_to_bet_profit.values())
        bet_count = len([h for h in completed_sorted if (h.get("predicted") or "").strip() in ("정", "꺽")])
        total_profit = sum(rp["profit"] for rp in round_to_bet_profit.values())
        total_won = sum(rp["profit"] for rp in round_to_bet_profit.values() if rp["profit"] > 0)
        total_lost = sum(-rp["profit"] for rp in round_to_bet_profit.values() if rp["profit"] < 0)
        now = datetime.now()
        session_start = self._session_start_at or now
        session_end = now
        duration_sec = (session_end - session_start).total_seconds()
        duration_str = "%d분 %d초" % (int(duration_sec // 60), int(duration_sec % 60)) if duration_sec >= 60 else "%d초" % int(duration_sec)
        lines = [
            "═" * 52,
            "          매크로 배팅 내역 보고서",
            "═" * 52,
            "",
            "날짜: %s" % now.strftime("%Y-%m-%d"),
            "세션 시작: %s" % session_start.strftime("%H:%M:%S"),
            "세션 종료: %s" % session_end.strftime("%H:%M:%S"),
            "배팅 시간: 약 %s" % duration_str,
            "",
            "─" * 52,
            "                    요약",
            "─" * 52,
            "총 배팅 횟수:    %d회" % bet_count,
            "총 배팅 금액:    %s원" % format(total_bet, ","),
            "총 승리 금액:    %s원" % format(total_won, ","),
            "총 패배 금액:    %s원" % format(total_lost, ","),
            "순익:            %s원" % (format(total_profit, "+,d") if total_profit != 0 else "0"),
            "",
            "─" * 52,
            "                    배팅 내역",
            "─" * 52,
            "%-8s %-4s %-4s %-4s %10s %10s" % ("회차", "픽", "결과", "승패", "금액", "수익"),
            "─" * 52,
        ]
        for h in completed_sorted:
            rn = h.get("round")
            rp = round_to_bet_profit.get(rn, {})
            pred = (h.get("predicted") or "").strip()
            act = (h.get("actual") or "").strip()
            res = (h.get("result") or "").strip()
            bet_val = rp.get("bet", 0)
            prof_val = rp.get("profit", 0)
            bet_str = format(bet_val, ",") if bet_val else "-"
            prof_str = ("%+d" % prof_val) if prof_val != 0 else "-"
            lines.append("%-8s %-4s %-4s %-4s %10s %10s" % (
                str(rn) if rn else "",
                pred or "-",
                act or "-",
                res or "-",
                bet_str,
                prof_str,
            ))
        lines.append("═" * 52)
        text = "\n".join(lines)
        default_name = "매크로_배팅내역_%s.txt" % now.strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(self, "배팅 내역 저장", default_name, "텍스트 파일 (*.txt);;모든 파일 (*)")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                self._log("저장 완료: %s" % path)
            except Exception as e:
                self._log("저장 실패: %s" % str(e)[:60])

    def _on_refresh(self):
        """수동 새로고침 — 정지 후 표 초기화, 좌표·API 재조회"""
        self._running = False
        self._first_bet_round = None
        self._last_bet_round = None
        self._waiting_for_first_win = False
        self._history = []
        self._session_start_at = None
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._save_macro_history(force=True)
        self._load_coords()
        self._poll_once()
        self._update_ui()
        self._log("새로고침 — 표 초기화됨")

    def _poll_once(self):
        """백그라운드 스레드에서 HTTP 요청 후, 결과를 시그널로 메인 스레드에 전달. UI 블로킹 방지."""
        url = self.analyzer_url_edit.text().strip()
        if not url:
            return
        calc_id = self.calc_combo.currentData() or 1

        def fetch_in_thread():
            # 폴링 필요 이유: round_actuals(실제 결과)를 가져오기 위해. 픽은 WebSocket으로 실시간 수신되지만,
            # 결과(정/꺽/조커)는 서버 푸시가 없어 주기 조회 필요. WebSocket 연결 시 relay 생략(1 API만).
            data, err = fetch_macro_data(url, calculator_id=calc_id)
            if err:
                if self._poll_result_signal:
                    self._poll_result_signal.emit({"err": err})
                return
            if not data:
                if self._poll_result_signal:
                    self._poll_result_signal.emit({"err": "API 응답 없음"})
                return
            if self._ws_connected:
                with self._lock:
                    pick = dict(self._pick)
                    pick["calculator"] = calc_id  # WebSocket에서 이미 수신 중 — relay 생략
            else:
                pick_relay, pick_err = fetch_current_pick(url, calculator_id=calc_id, timeout=5)
                if not pick_err and pick_relay and pick_relay.get("running") is not False:
                    pc_raw = pick_relay.get("pick_color")
                    pc = _normalize_pick_color(pc_raw) if pc_raw else None
                    pick = {"round": pick_relay.get("round"), "pick_color": pc or pc_raw, "calculator": calc_id}
                else:
                    pick = {"round": None, "pick_color": None}
            if self._poll_result_signal:
                self._poll_result_signal.emit({
                    "data": data,
                    "pick": pick,
                })

        threading.Thread(target=fetch_in_thread, daemon=True).start()

    def _on_poll_result(self, result):
        """폴 결과 수신 (메인 스레드) — UI 업데이트만 수행."""
        if not isinstance(result, dict):
            return
        if "err" in result:
            self._log("API 조회 실패: %s" % result["err"])
            return
        data = result.get("data")
        pick = result.get("pick")
        if not data:
            return
        cur_calc = (self.calc_combo.currentData() or 1) if hasattr(self, "calc_combo") else 1
        with self._lock:
            # 폴링 시점과 현재 선택 계산기가 다르면 _pick 덮어쓰지 않음 (콤보 변경 후 지연 응답 방지)
            if pick and pick.get("calculator") == cur_calc:
                self._pick = pick or {}
            elif not pick or pick.get("calculator") != cur_calc:
                pass  # _pick 유지
            self._round_actuals = data.get("round_actuals") or {}
            self._cards = data.get("cards") or []  # 15번 카드 색(정/꺽 표시용)
        self._sync_pick_to_history()
        self._merge_results_into_history()
        self._save_macro_history()
        with self._lock:
            pick_t = (self._pick.get("round"), self._pick.get("pick_color"))
            hist_t = tuple((h.get("round"), h.get("predicted"), h.get("actual"), h.get("result")) for h in self._history[-20:])
        sig = (pick_t, hist_t)
        if sig != self._last_ui_sig:
            self._last_ui_sig = sig
            self._update_ui()
        if self._running:
            self._try_bet_if_needed()

    def _merge_results_into_history(self):
        """round_actuals로 기존 history의 actual만 갱신. 픽이 있었던 회차만 추적."""
        with self._lock:
            ra = dict(self._round_actuals)
            for h in self._history:
                rnd = h.get("round")
                if rnd is None:
                    continue
                rnd_str = str(rnd)
                if rnd_str not in ra:
                    continue
                act = (ra[rnd_str].get("actual") or "").strip()
                if act not in ("정", "꺽", "joker", "조커"):
                    continue
                if (h.get("actual") or "").strip() in ("정", "꺽", "joker", "조커"):
                    continue
                h["actual"] = act
                pred = h.get("predicted")
                if pred == "보류":
                    h["result"] = "보류"
                elif pred in ("정", "꺽"):
                    h["result"] = "승" if pred == act else "패"
                else:
                    h["result"] = "조커"

    def _sync_pick_to_history(self):
        """현재 픽을 pending 회차로 history에 반영. 보류(15번 조커)도 기록."""
        pick = self._pick
        rnd = pick.get("round")
        if rnd is None:
            return
        eff = self._get_effective_pick_color(pick.get("pick_color"))
        pred = pick_color_to_pred(eff, self._get_card_15_color())
        if pred is None:
            pred = "보류"
        with self._lock:
            hist_by_round = {h["round"]: h for h in self._history if h.get("round") is not None}
            if rnd in hist_by_round:
                h = hist_by_round[rnd]
                if (h.get("actual") or "").strip() == "pending":
                    h["predicted"] = pred
                return
            self._history.append({
                "round": rnd, "predicted": pred, "actual": "pending", "result": None,
                "betAmount": None, "profit": None,
            })
            self._history.sort(key=lambda x: x["round"])

    def _update_ui(self):
        with self._lock:
            pick = dict(self._pick)
            cards = list(self._cards)
            hist = list(self._history)

        # 마틴 시뮬레이션: 시작 후 첫 ADB 배팅 회차(_first_bet_round)부터만 금액·수익 표시. API 연결만 시 전부 "-"
        round_to_bet_profit = {}
        total_profit = None
        if self._first_bet_round is not None:
            capital, martin_table, odds = self._get_macro_settings()
            first = self._first_bet_round
            completed = [h for h in hist if (h.get("actual") or "").strip() not in ("pending", "") and (h.get("round") or 0) >= first]
            completed_sorted = sorted(completed, key=lambda x: x.get("round") or 0)
            step, next_bet = calc_martingale_step_and_bet(completed_sorted, martin_table=martin_table)
            cap = capital
            for h in completed_sorted:
                rn = h["round"]
                act = (h.get("actual") or "").strip()
                pred = (h.get("predicted") or "").strip()
                if pred == "보류":
                    round_to_bet_profit[rn] = {"bet": 0, "profit": 0}
                    continue
                is_joker = act in ("joker", "조커")
                is_win = not is_joker and pred in ("정", "꺽") and pred == act
                bet = min(next_bet, int(cap))
                if is_joker:
                    profit = -bet
                    step = min(step + 1, len(martin_table) - 1)
                    next_bet = martin_table[step]
                elif is_win:
                    profit = int(bet * (odds - 1))
                    step = 0
                    next_bet = martin_table[0]
                else:
                    profit = -bet
                    step = min(step + 1, len(martin_table) - 1)
                    next_bet = martin_table[step]
                cap += profit
                round_to_bet_profit[rn] = {"bet": bet, "profit": profit}
            total_profit = sum(rp.get("profit", 0) or 0 for rp in round_to_bet_profit.values() if rp.get("profit") is not None)
            for h in hist:
                rn = h.get("round")
                if rn is not None and rn not in round_to_bet_profit:
                    act = (h.get("actual") or "").strip()
                    pred = (h.get("predicted") or "").strip()
                    if act in ("pending", ""):
                        if pred == "보류":
                            round_to_bet_profit[rn] = {"bet": 0, "profit": None}
                        else:
                            comp = sorted([x for x in hist if (x.get("actual") or "").strip() not in ("pending", "") and (x.get("round") or 0) >= first and (x.get("round") or 0) < rn], key=lambda x: x.get("round") or 0)
                            _, nb = calc_martingale_step_and_bet(comp, martin_table=martin_table)
                            round_to_bet_profit[rn] = {"bet": nb, "profit": None}

        # 계산기 표 — 최신회차가 제일 위에, 승패열만 글씨색 (승: 노란 굵게, 패: 빨강)
        card_15 = _normalize_pick_color(cards[14].get("color")) if len(cards) > 14 else None
        sorted_hist = sorted(hist, key=lambda x: (x.get("round") or 0))
        display_hist = list(reversed(sorted_hist))[:80]
        self.calc_table.setUpdatesEnabled(False)
        self.calc_table.setRowCount(len(display_hist))
        for row, h in enumerate(display_hist):
            rn = h.get("round")
            rp = round_to_bet_profit.get(rn, {})
            res = (h.get("result") or "").strip()
            for col in range(6):
                if col == 0:
                    it = QTableWidgetItem(str(rn) if rn is not None else "")
                elif col == 1:
                    pred = (h.get("predicted") or "").strip()
                    it = QTableWidgetItem(pred or "")
                    pick_color = pred_to_pick_color(pred, card_15) if pred in ("정", "꺽") else None
                    if pick_color == "RED":
                        it.setBackground(QBrush(QColor("#ffcdd2")))
                    elif pick_color == "BLACK":
                        it.setBackground(QBrush(QColor("#cfd8dc")))
                elif col == 2:
                    it = QTableWidgetItem((h.get("actual") or "").strip())
                elif col == 3:
                    it = QTableWidgetItem(res)
                    if res == "승":
                        it.setForeground(QColor("#e6b800"))
                        it.setFont(QFont(it.font().family(), it.font().pointSize(), QFont.Bold))
                    elif res == "패":
                        it.setForeground(QColor("red"))
                    elif res == "보류":
                        it.setForeground(QColor("#888"))
                elif col == 4:
                    bet_val = rp.get("bet")
                    it = QTableWidgetItem(str(bet_val) if bet_val is not None and bet_val > 0 else "-")
                else:
                    prof_val = rp.get("profit")
                    it = QTableWidgetItem(str(prof_val) if prof_val is not None else "-")
                    if prof_val is not None:
                        it.setForeground(QColor("green") if prof_val > 0 else QColor("red"))
                it.setTextAlignment(Qt.AlignCenter)
                self.calc_table.setItem(row, col, it)
        self.calc_table.setUpdatesEnabled(True)

        # 배팅중 (분석기 계산기 상단 배팅픽과 동일 출처. 픽 반대로 옵션 적용)
        rnd = pick.get("round")
        pc = self._get_effective_pick_color(pick.get("pick_color"))
        pending_rnd = rnd
        calc_id = self.calc_combo.currentData() if hasattr(self, "calc_combo") else 1
        if pending_rnd is not None and pc:
            pred = pick_color_to_pred(pc, self._get_card_15_color())
            self.pick_label.setText(f"{pending_rnd}회 {pc} ({pred}) [계산기 {calc_id}]")
            if self._running:
                _, martin_table, _ = self._get_macro_settings()
                first = self._first_bet_round
                completed = [h for h in hist if (h.get("actual") or "").strip() not in ("pending", "") and first is not None and (h.get("round") or 0) >= first]
                completed_sorted = sorted(completed, key=lambda x: x.get("round") or 0)
                step, amt = calc_martingale_step_and_bet(completed_sorted, martin_table=martin_table)
                sp = f"{total_profit:+,d}원" if total_profit is not None else "—"
                self.amount_label.setText(f"다음: {amt:,}원 | 순익: {sp}")
            else:
                sp = f"{total_profit:+,d}원" if total_profit is not None else "—"
                self.amount_label.setText(f"순익: {sp}")
        elif pending_rnd is not None:
            self.pick_label.setText(f"{pending_rnd}회 보류 (15번 조커) [계산기 {calc_id}]")
            sp = f"{total_profit:+,d}원" if total_profit is not None else "—"
            self.amount_label.setText(f"순익: {sp}")
        else:
            self.pick_label.setText("보류")
            sp = f"{total_profit:+,d}원" if total_profit is not None else "—"
            self.amount_label.setText(f"순익: {sp}")

    def _poll_loop(self):
        if not self._ws_connected:
            self._poll_once()  # WebSocket 미연결 시에만 폴링 (픽·결과 모두 WS로 전달 시 생략)
        if self._poll_timer:
            QTimer.singleShot(2000, self._poll_loop)

    def _on_calc_combo_changed(self):
        """계산기 선택 변경 시 WebSocket 재연결 — 선택한 계산기 픽만 수신하도록."""
        if getattr(self, "_poll_timer", False):
            self._start_ws_client()

    def _on_start(self):
        url = self.analyzer_url_edit.text().strip()
        if not url:
            self._log("Analyzer URL을 입력하세요.")
            return
        self._analyzer_url = url
        self._device_id = self.device_edit.text().strip() or "127.0.0.1:5555"
        self._coords = load_coords()
        try:
            self._coords["macro_capital"] = int(self.capital_edit.text().strip() or 1000000)
            self._coords["macro_base"] = int(self.base_bet_edit.text().strip() or 5000)
            try:
                self._coords["macro_odds"] = float(self.odds_edit.text().strip() or 1.97)
            except (TypeError, ValueError):
                self._coords["macro_odds"] = 1.97
            save_coords(self._coords)
        except (TypeError, ValueError):
            pass
        if not self._poll_timer:
            self._start_ws_client()
            self._poll_timer = True
            self._poll_loop()
        self._running = True
        self._waiting_for_first_win = True  # 첫 승 나온 뒤부터 배팅 시작
        self._session_start_at = datetime.now()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._log("배팅 대기 — 첫 승 나온 뒤부터 배팅 시작")

    def _on_stop(self):
        self._running = False
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._log("배팅 정지 (픽/카드/그래프 수신은 계속)")

    def _start_ws_client(self):
        """WebSocket 클라이언트 시작 — 픽 실시간 수신 (emulator_macro와 동일)."""
        self._stop_ws_client()
        self._ws_connected = False
        url = self.analyzer_url_edit.text().strip()
        calc_id = (self.calc_combo.currentData() or 1) if hasattr(self, "calc_combo") else 1
        ws_url = _ws_url_from_analyzer(url, calc_id)
        if not ws_url:
            return

        def run():
            try:
                import socketio
                sio = socketio.Client(reconnection=True, reconnection_attempts=10, reconnection_delay=2)
                self._ws_client = sio

                @sio.on("round_actuals_update")
                def on_round_actuals(data):
                    if isinstance(data, dict) and data.get("round_actuals"):
                        if HAS_PYQT and self._round_actuals_signal is not None:
                            self._round_actuals_signal.emit(data)

                @sio.on("pick_update")
                def on_pick(data):
                    if not isinstance(data, dict):
                        return
                    if data.get("running") is False:
                        return
                    rnd = data.get("round")
                    pc_raw = data.get("pick_color")
                    pc = _normalize_pick_color(pc_raw) if pc_raw else None
                    pick = {
                        "round": rnd,
                        "pick_color": pc or pc_raw,
                        "suggested_amount": data.get("suggested_amount"),
                        "calculator": data.get("calculator"),
                    }
                    if pick.get("round") is None:
                        return
                    if HAS_PYQT and self._ws_pick_received is not None:
                        self._ws_pick_received.emit(pick)

                @sio.on("connect")
                def on_connect():
                    self._ws_connected = True

                @sio.on("disconnect")
                def on_disconnect():
                    self._ws_connected = False

                @sio.on("connect_error")
                def on_error(data):
                    self._ws_connected = False

                sio.connect(ws_url, auth={"calculator": calc_id}, transports=["websocket"], wait_timeout=8)
                self._ws_connected = True
                if self._test_done_signal:
                    self._test_done_signal.emit("ws", "[WebSocket] 연결됨 — 픽·결과 실시간 수신")
                sio.wait()
            except Exception as e:
                self._ws_connected = False
                if self._test_done_signal:
                    self._test_done_signal.emit("ws", "[WebSocket] 연결 실패: %s" % str(e)[:80])
            finally:
                self._ws_client = None
                self._ws_connected = False

        self._ws_thread = threading.Thread(target=run, daemon=True)
        self._ws_thread.start()

    def _stop_ws_client(self):
        """WebSocket 클라이언트 종료."""
        self._ws_connected = False
        if self._ws_client is not None:
            try:
                self._ws_client.disconnect()
            except Exception:
                pass
            self._ws_client = None
        self._ws_thread = None

    def _on_round_actuals_received(self, data):
        """WebSocket round_actuals 수신: 결과 반영·폴링 대체."""
        if not isinstance(data, dict):
            return
        ra = data.get("round_actuals") or {}
        cards = data.get("cards") or []
        with self._lock:
            self._round_actuals = ra
            if cards:
                self._cards = cards
        self._merge_results_into_history()
        self._save_macro_history()
        with self._lock:
            pick_t = (self._pick.get("round"), self._pick.get("pick_color"))
            hist_t = tuple((h.get("round"), h.get("predicted"), h.get("actual"), h.get("result")) for h in self._history[-20:])
        sig = (pick_t, hist_t)
        if sig != self._last_ui_sig:
            self._last_ui_sig = sig
            self._update_ui()

    def _on_ws_pick_received(self, pick):
        """WebSocket 픽 수신: 배팅중 갱신. 보류(15번 조커) 시 round만 있고 pick_color=None."""
        if not isinstance(pick, dict):
            return
        rnd = pick.get("round")
        pc = pick.get("pick_color")
        if rnd is None:
            return
        cur_calc = (self.calc_combo.currentData() or 1) if hasattr(self, "calc_combo") else 1
        if pick.get("calculator") is not None and pick.get("calculator") != cur_calc:
            return  # 다른 계산기 픽 무시 (충돌 방지)
        try:
            rnd = int(rnd)
        except (TypeError, ValueError):
            return
        pc_norm = _normalize_pick_color(pc) if pc else None
        with self._lock:
            self._pick = {"round": rnd, "pick_color": pc_norm or pc, "calculator": cur_calc}
        self._sync_pick_to_history()
        self._save_macro_history()
        self._update_ui()
        if self._running:
            self._try_bet_if_needed()

    def _do_bet(self, round_num, pick_color, amount):
        """ADB 배팅 실행"""
        if not _validate_bet_amount(amount):
            self._log("금액 오류: %s" % amount)
            return False
        coords = self._coords
        device = self._device_id or None
        bet_xy = coords.get("bet_amount")
        red_xy = coords.get("red")
        black_xy = coords.get("black")
        confirm_xy = coords.get("confirm")
        if not bet_xy or not red_xy or not black_xy:
            self._log("좌표 없음 — coord_picker로 설정")
            return False
        try:
            tx, ty = _apply_window_offset(coords, bet_xy[0], bet_xy[1], key="bet_amount")
            adb_swipe(device, tx, ty, 100)
            time.sleep(0.06)
            adb_input_text(device, str(int(amount)))
            time.sleep(0.01)
            adb_keyevent(device, 4)
            time.sleep(0.06)
            color_xy = red_xy if pick_color == "RED" else black_xy
            cx, cy = _apply_window_offset(coords, color_xy[0], color_xy[1], key="red" if pick_color == "RED" else "black")
            adb_swipe(device, cx, cy, 100)
            time.sleep(0.01)
            if confirm_xy:
                cx2, cy2 = _apply_window_offset(coords, confirm_xy[0], confirm_xy[1], key="confirm")
                adb_swipe(device, cx2, cy2, 80)
            self._log("%s회 %s %s원 ADB 완료" % (round_num, pick_color, amount))
            return True
        except Exception as e:
            self._log("ADB 오류: %s" % str(e)[:80])
            return False

    def _try_bet_if_needed(self):
        """픽이 있고 아직 배팅 안 한 회차면 배팅 시도. 보류 회차 건너뛰어도 다음 유효 픽에서 재개."""
        with self._lock:
            pick = dict(self._pick)
            hist = list(self._history)
        rnd = pick.get("round")
        pc = self._get_effective_pick_color(pick.get("pick_color"))
        if rnd is None or pc is None:
            return
        if self._last_bet_round is not None and rnd <= self._last_bet_round:
            return
        # 첫 승 나올 때까지 배팅 대기
        if getattr(self, "_waiting_for_first_win", False):
            has_win = any((h.get("result") or "").strip() == "승" for h in hist)
            if not has_win:
                return
            self._waiting_for_first_win = False
            self._first_bet_round = rnd
            self._log("첫 승 확인 — %s회부터 배팅 시작" % rnd)
        capital, martin_table, _ = self._get_macro_settings()
        first = self._first_bet_round
        completed = [h for h in hist if (h.get("actual") or "").strip() not in ("pending", "") and first is not None and (h.get("round") or 0) >= first]
        completed_sorted = sorted(completed, key=lambda x: x.get("round") or 0)
        step, amt = calc_martingale_step_and_bet(completed_sorted, martin_table=martin_table)
        amt = min(amt, int(capital)) if amt > 0 else 0
        if amt <= 0:
            return
        if self._do_bet(rnd, pc, amt):
            self._last_bet_round = rnd
            if self._first_bet_round is None:
                self._first_bet_round = rnd
            self._save_macro_history(force=True)


def main():
    if not HAS_PYQT:
        print("PyQt5 필요: pip install PyQt5")
        return
    app = QApplication([])
    w = MacroStandaloneWindow()
    w.show()
    app.exec_()


if __name__ == "__main__":
    main()
