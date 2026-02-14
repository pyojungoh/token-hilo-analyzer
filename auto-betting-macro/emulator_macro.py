# -*- coding: utf-8 -*-
"""
에뮬레이터(LDPlayer) 자동배팅 매크로.
- 계산기에서 회차·배팅중 픽·배팅금액만 가져와서 ADB로 배팅. 계산/승패는 분석기 가상배팅 계산기가 담당.
- 분석기 웹 계산기1/2/3 중 선택 → 해당 계산기 픽/금액 수신 → 금액 입력 → RED/BLACK 탭 → 정정.
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
        QLabel, QLineEdit, QPushButton, QGroupBox, QFormLayout, QComboBox,
        QFrame, QScrollArea, QGridLayout, QCheckBox,
    )
    from PyQt5.QtCore import Qt, QTimer, pyqtSlot, pyqtSignal
    from PyQt5.QtGui import QFont
    HAS_PYQT = True
except ImportError:
    HAS_PYQT = False

def _emulator_script_dir():
    """EXE 실행 시 exe 위치, 아니면 스크립트 위치 (emulator_coords.json 등)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

SCRIPT_DIR = _emulator_script_dir()
COORDS_PATH = os.path.join(SCRIPT_DIR, "emulator_coords.json")

# 좌표 찾기용 키·라벨. COORD_BTN_SHORT = 버튼 글자 줄여서 잘리지 않게
COORD_KEYS = {"bet_amount": "배팅금액", "confirm": "정정", "red": "레드", "black": "블랙"}
COORD_BTN_SHORT = {"bet_amount": "금액", "confirm": "정정", "red": "레드", "black": "블랙"}

# 배팅 동작 간 지연(초). 픽 나오면 최대한 빠르게 쏘도록 짧게 둠. 입력/확정이 안 먹으면 값을 늘리세요.
BET_DELAY_AFTER_AMOUNT_TAP = 0.025  # 배팅금 탭 후 키보드 포커스 대기. 금액 늦게 입력되면 0.04~0.06으로
BET_DELAY_AFTER_INPUT = 0.04
BET_DELAY_AFTER_BACK = 0.04
BET_DELAY_AFTER_COLOR_TAP = 0.04
BET_DELAY_BETWEEN_CONFIRM_TAPS = 0.04
BET_DELAY_AFTER_CONFIRM = 0.04
BET_AMOUNT_SWIPE_MS = 120   # 배팅금 칸 터치 지속(ms). 터치 안 먹으면 150~180으로
BET_COLOR_SWIPE_MS = 100    # 레드/블랙/정정 터치 지속. 좌표 정확히 안 눌리면 120~150으로
BET_CONFIRM_TAP_COUNT = 1  # 정정 버튼 1번만
BET_RETRY_ATTEMPTS = 2  # 실패 시 재시도 횟수
BET_RETRY_DELAY = 0.8   # 재시도 전 대기(초)
KEYCODE_DEL = 67  # Android KEYCODE_DEL (한 글자 삭제)


def _normalize_pick_color(raw):
    """서버 픽 값(RED/BLACK, 빨강/검정 등)을 항상 'RED' 또는 'BLACK'으로 통일. 모르면 None."""
    s = (raw or "").strip()
    if not s:
        return None
    u = s.upper()
    if u == "RED" or s == "빨강":
        return "RED"
    if u == "BLACK" or s == "검정":
        return "BLACK"
    return None


def save_coords(data):
    with open(COORDS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# 회차 아이콘 (웹과 동일: (round-1)%3)
ROUND_ICONS = ['★', '△', '○']
ROUND_TYPES = ['star', 'triangle', 'circle']


def get_round_icon(round_num):
    if round_num is None:
        return '★', 'star'
    try:
        r = int(round_num)
        if r < 1:
            return '★', 'star'
        idx = (r - 1) % 3
        return ROUND_ICONS[idx], ROUND_TYPES[idx]
    except (TypeError, ValueError):
        return '★', 'star'


def normalize_analyzer_url(analyzer_url):
    s = (analyzer_url or "").strip().rstrip("/")
    if not s:
        return ""
    try:
        from urllib.parse import urlparse
        p = urlparse(s if "://" in s else "https://" + s)
        base = (p.scheme or "https") + "://" + (p.netloc or p.path.split("/")[0])
        return base.rstrip("/")
    except Exception:
        return s.split("/")[0] if s else ""


def fetch_current_pick(analyzer_url, calculator_id=1, timeout=5):
    """GET {analyzer_url}/api/current-pick?calculator=1|2|3"""
    base = normalize_analyzer_url(analyzer_url)
    if not base:
        return {"pick_color": None, "round": None, "probability": None, "error": "URL 없음"}
    url = base + "/api/current-pick"
    params = {"calculator": calculator_id}
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"pick_color": None, "round": None, "probability": None, "error": str(e)}


def load_coords():
    if not os.path.exists(COORDS_PATH):
        return {}
    try:
        with open(COORDS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_device_size_via_adb(device_id=None):
    """adb shell wm size 로 기기 해상도 (width, height) 가져오기. Windows는 CMD와 동일 방식."""
    try:
        rc, out, err = _run_adb_shell_cmd(device_id, "shell", "wm", "size")
        combined = (out or "") + (err or "")
        m = re.search(r"(\d+)\s*x\s*(\d+)", combined, re.IGNORECASE)
        if m:
            return (int(m.group(1)), int(m.group(2)))
    except Exception:
        pass
    return (0, 0)


def get_window_rect_at(screen_x, screen_y):
    """클릭한 점(screen_x, screen_y)이 속한 최상위 창의 (left, top, width, height) 반환. Windows 전용."""
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


def _apply_window_offset(coords, x, y, key=None):
    """창 내 상대 좌표 또는 화면 좌표 → 기기 해상도로 스케일. raw_coords=True면 그대로. key=좌표키면 해당 키만 창상대 여부 적용."""
    try:
        x, y = int(x), int(y)
        if coords.get("raw_coords"):
            return x, y
        # 해당 키가 창 상대로 저장됐는지 확인(키 없으면 예전 호환: coords_are_window_relative)
        spaces = coords.get("coord_spaces") or {}
        is_window_relative = (spaces.get(key, coords.get("coords_are_window_relative")) if key
                              else coords.get("coords_are_window_relative"))
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


def _run_adb_shell_cmd(device_id, *args):
    """Windows에서는 CMD와 동일한 환경으로 adb 실행(shell=True). 반환: (returncode, stdout, stderr)."""
    kw = {"capture_output": True, "text": True, "timeout": 10}
    if os.name == "nt":
        if device_id:
            cmd = "adb -s %s %s" % (device_id, " ".join(str(a) for a in args))
        else:
            cmd = "adb " + " ".join(str(a) for a in args)
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
    """adb 명령 실행. Windows는 shell=True로 CMD와 동일하게 동작하도록 함."""
    kw = {"capture_output": True, "timeout": 10}
    if os.name == "nt":
        cmd = "adb -s %s %s" % (device_id, " ".join(str(a) for a in args)) if device_id else "adb " + " ".join(str(a) for a in args)
        kw["shell"] = True
    else:
        cmd = ["adb"] + (["-s", device_id] if device_id else []) + list(args)
    try:
        subprocess.run(cmd, **kw)
    except Exception:
        pass


def adb_tap(device_id, x, y):
    """adb -s {device} shell input tap x y"""
    _run_adb_raw(device_id, "shell", "input", "tap", str(x), str(y))


def adb_tap_and_return_cmd(device_id, x, y):
    """ADB 탭 실행 후 터미널에서 직접 쓸 수 있는 명령 문자열 반환."""
    if device_id:
        cmd_str = "adb -s %s shell input tap %s %s" % (device_id, x, y)
    else:
        cmd_str = "adb shell input tap %s %s" % (x, y)
    adb_tap(device_id, x, y)
    return cmd_str


def adb_input_text(device_id, text):
    """adb shell input text \"xxx\" (공백은 %s로)"""
    escaped = text.replace(" ", "%s")
    _run_adb_raw(device_id, "shell", "input", "text", escaped)


def adb_swipe(device_id, x, y, duration_ms=80):
    """같은 좌표로 swipe → 터치 다운·업으로 클릭."""
    x, y = int(x), int(y)
    _run_adb_raw(device_id, "shell", "input", "swipe", str(x), str(y), str(x), str(y), str(duration_ms))


def adb_keyevent(device_id, keycode):
    """adb shell input keyevent KEYCODE (4=BACK, 66=ENTER)."""
    _run_adb_raw(device_id, "shell", "input", "keyevent", str(keycode))


class EmulatorMacroWindow(QMainWindow if HAS_PYQT else object):
    connect_result_ready = pyqtSignal(object, object) if HAS_PYQT else None  # (pick, results) 서브스레드 → 메인 스레드
    test_tap_done = pyqtSignal(object, str, str) if HAS_PYQT else None  # (버튼, 복원텍스트, 로그메시지)
    poll_done = pyqtSignal(object, object) if HAS_PYQT else None  # (pick, results) 폴링 스레드 → 메인 스레드
    device_size_fetched = pyqtSignal(int, int) if HAS_PYQT else None  # (width, height) 기기 해상도 가져오기 완료
    adb_device_suggested = pyqtSignal(str) if HAS_PYQT else None  # 연결된 기기 ID로 ADB 칸 자동 채움

    def __init__(self):
        super().__init__()
        self.setWindowTitle("에뮬레이터 자동배팅 (LDPlayer)")
        self.setMinimumSize(420, 860)
        self.resize(440, 900)

        self._analyzer_url = ""
        self._calculator_id = 1
        self._coords = {}
        self._device_id = "127.0.0.1:5555"
        self._poll_interval_sec = 2.0
        self._running = False
        self._connected = False  # 연결 버튼으로 연결됨 → 계속 폴링해 픽/회차 갱신
        self._last_round_when_started = None  # 미사용 (바로 시작 시 현재 회차부터 배팅)
        self._last_bet_round = None  # 이미 배팅한 회차 (중복 방지)
        self._pending_bet_rounds = {}  # round_num -> { pick_color, amount } (결과 대기 → 승/패/조커 로그)
        self._pick_history = deque(maxlen=5)  # 최근 5회 (round_num, pick_color) — 회차·픽 안정 시에만 배팅
        self._pick_data = {}
        self._results_data = {}
        self._lock = threading.Lock()
        # 표시 깜빡임 방지: 같은 (회차, 픽)이 2회 연속 올 때만 카드/회차 문구 갱신
        self._display_stable = None  # (round_num, pick_color) 표시용
        self._display_candidate = None
        self._display_confirm_count = 0
        self._display_confirm_needed = 2
        # 배팅금액 2~3회만 받고 즉시 배팅 (계산기 표와 동일 금액 확정용)
        self._amount_confirm_round = None
        self._amount_confirm_pick = None
        self._amount_confirm_amounts = []
        self._amount_confirm_want = 3
        # 좌표 찾기 (한곳에 통합)
        self._coord_listener = None
        self._coord_capture_key = None
        self._pending_coord_click = None
        self._coord_value_labels = {}
        self._coord_status_labels = {}

        self._build_ui()
        self._load_coords()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        if HAS_PYQT and self.connect_result_ready is not None:
            self.connect_result_ready.connect(self._apply_connect_result)
        if HAS_PYQT and self.test_tap_done is not None:
            self.test_tap_done.connect(self._on_test_tap_done)
            if self.device_size_fetched is not None:
                self.device_size_fetched.connect(self._on_device_size_fetched)
            if self.adb_device_suggested is not None:
                self.adb_device_suggested.connect(self._on_adb_device_suggested)
        if HAS_PYQT and self.poll_done is not None:
            self.poll_done.connect(self._on_poll_done)

    def _on_test_tap_done(self, btn, restore_text, message):
        """테스트 탭/연결확인 완료 시 버튼 복원 + 로그 (메인 스레드)."""
        try:
            if btn:
                btn.setEnabled(True)
                btn.setText(restore_text)
        except Exception:
            pass
        self._log(message)

    def _on_adb_device_suggested(self, device_id):
        """연결 확인 시 동작하는 기기 ID로 ADB 기기 칸 자동 채움."""
        try:
            self.device_edit.setText(device_id)
            self._log("ADB 기기 칸을 [%s] 로 자동 채움." % device_id)
        except Exception:
            pass

    def _build_ui(self):
        cw = QWidget()
        self.setCentralWidget(cw)
        layout = QVBoxLayout(cw)

        # 설정
        g_set = QGroupBox("설정")
        fl = QFormLayout()
        # 닉네임 선택 시 해당 분석기 주소 자동 설정 (API는 루트 주소 사용)
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
        self.analyzer_url_edit.setText(self._analyzer_nick_urls.get("표마왕", ""))
        self.analyzer_url_edit.setPlaceholderText("닉네임 선택 또는 직접 입력")
        fl.addRow("Analyzer URL:", self.analyzer_url_edit)

        self.calc_combo = QComboBox()
        self.calc_combo.addItem("계산기 1", 1)
        self.calc_combo.addItem("계산기 2", 2)
        self.calc_combo.addItem("계산기 3", 3)
        fl.addRow("계산기 선택:", self.calc_combo)
        connect_row = QHBoxLayout()
        connect_row.setContentsMargins(0, 4, 0, 4)
        self.connect_btn = QPushButton("Analyzer 연결")
        self.connect_btn.setMinimumHeight(28)
        self.connect_btn.clicked.connect(self._on_connect_analyzer)
        self.connect_status_label = QLabel("")
        self.connect_status_label.setStyleSheet("color: #666; font-size: 11px;")
        connect_row.addWidget(self.connect_btn)
        connect_row.addWidget(self.connect_status_label)
        connect_row.addStretch(1)
        fl.addRow("", connect_row)
        fl.addRow("", QLabel("※ 연결 후 픽/금액이 보이면 정상. 그다음 배팅 시작하세요."))

        self.device_edit = QLineEdit()
        self.device_edit.setText("127.0.0.1:5555")
        self.device_edit.setPlaceholderText("예: 127.0.0.1:5555 또는 emulator-5554")
        self.device_edit.setMaximumWidth(180)
        fl.addRow("ADB 기기:", self.device_edit)
        adb_btn_row = QHBoxLayout()
        self.adb_test_btn = QPushButton("배팅금액 테스트 (5000원)")
        self.adb_test_btn.setMinimumHeight(28)
        self.adb_test_btn.setToolTip("배팅금액 칸을 탭한 뒤 5000을 입력해, 금액이 제대로 들어가는지 확인합니다.")
        self.adb_test_btn.clicked.connect(self._on_adb_test_tap)
        self.adb_red_btn = QPushButton("레드 1회 탭")
        self.adb_red_btn.setMinimumHeight(28)
        self.adb_red_btn.clicked.connect(lambda: self._on_adb_color_tap("red"))
        self.adb_black_btn = QPushButton("블랙 1회 탭")
        self.adb_black_btn.setMinimumHeight(28)
        self.adb_black_btn.clicked.connect(lambda: self._on_adb_color_tap("black"))
        self.adb_devices_btn = QPushButton("ADB 연결 확인")
        self.adb_devices_btn.setMinimumHeight(28)
        self.adb_devices_btn.clicked.connect(self._on_adb_devices)
        adb_btn_row.addWidget(self.adb_test_btn)
        adb_btn_row.addWidget(self.adb_red_btn)
        adb_btn_row.addWidget(self.adb_black_btn)
        adb_btn_row.addWidget(self.adb_devices_btn)
        fl.addRow("", adb_btn_row)
        g_set.setLayout(fl)
        layout.addWidget(g_set)

        # 좌표 설정 (한곳에 통합 — LDPlayer 화면에서 클릭해 잡기)
        g_coord = QGroupBox("좌표 설정 (LDPlayer에서 해당 위치 클릭)")
        fl_coord = QFormLayout()
        for key, label in COORD_KEYS.items():
            row_w = QWidget()
            row = QHBoxLayout()
            row.setContentsMargins(0, 2, 0, 2)
            row_w.setLayout(row)
            short = COORD_BTN_SHORT.get(key, label)
            btn = QPushButton(f"{short} 찾기")
            btn.setToolTip(f"{label} 좌표 찾기")
            btn.setMinimumHeight(28)
            btn.clicked.connect(lambda checked=False, k=key: self._start_coord_capture(k))
            row.addWidget(btn)
            val_lbl = QLabel("(미설정)")
            val_lbl.setMinimumWidth(80)
            row.addWidget(val_lbl)
            status_lbl = QLabel("")
            status_lbl.setStyleSheet("color: green; font-size: 11px;")
            row.addWidget(status_lbl)
            row.addStretch(1)
            self._coord_value_labels[key] = val_lbl
            self._coord_status_labels[key] = status_lbl
            fl_coord.addRow(row_w)
        self.raw_coords_check = QCheckBox("원시 좌표 (창/해상도 보정 안 함 — 저장된 x,y 그대로 전송)")
        self.raw_coords_check.setChecked(False)
        self.raw_coords_check.setStyleSheet("color: #888; font-size: 11px;")
        fl_coord.addRow("", self.raw_coords_check)
        win_row = QHBoxLayout()
        win_row.addWidget(QLabel("LDPlayer 창 왼쪽 위 (탭 좌표 보정):"))
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
        self.window_capture_btn.setToolTip("클릭 후 LDPlayer 창의 왼쪽 위 모서리를 한 번 클릭하면 X, Y가 자동 저장됩니다.")
        self.window_capture_btn.clicked.connect(lambda: self._start_coord_capture("window_topleft"))
        win_row.addWidget(self.window_capture_btn)
        self.window_save_btn = QPushButton("창 위치 저장")
        self.window_save_btn.setMinimumHeight(28)
        self.window_save_btn.clicked.connect(self._save_window_offset)
        win_row.addWidget(self.window_save_btn)
        win_row.addWidget(QLabel("(0,0이면 비움)"))
        win_row.addStretch(1)
        fl_coord.addRow(win_row)
        win_hint = QLabel("※ 좌표 찾기로 레드/배팅금액 등을 잡으면 창 위치는 자동으로 채워져서, 위 칸은 건드리지 않아도 됩니다.")
        win_hint.setStyleSheet("color: #888; font-size: 11px;")
        fl_coord.addRow("", win_hint)
        res_row = QHBoxLayout()
        res_row.addWidget(QLabel("해상도 보정(선택): 창 W/H"))
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
        self.device_size_fetch_btn.setToolTip("ADB로 연결된 에뮬레이터의 실제 해상도를 가져와 기기 W/H에 채웁니다. 탭이 다른 곳에 눌리면 이 버튼으로 보정하세요.")
        self.device_size_fetch_btn.clicked.connect(self._on_fetch_device_size)
        res_row.addWidget(self.device_size_fetch_btn)
        res_row.addWidget(QLabel("(창과 기기 크기 다르면 입력)"))
        res_row.addStretch(1)
        fl_coord.addRow(res_row)
        if not HAS_PYNPUT:
            fl_coord.addRow("", QLabel("pynput 미설치: pip install pynput"))
        g_coord.setLayout(fl_coord)
        layout.addWidget(g_coord)

        # 표시 영역: 회차(별동그라미세모), 금액, 배팅픽, 정/꺽 카드, 경고/합선/승률
        g_display = QGroupBox("현재 픽 (선택한 계산기와 동일)")
        disp_layout = QVBoxLayout()
        self.round_label = QLabel("회차: -")
        self.round_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        disp_layout.addWidget(self.round_label)

        self.amount_label = QLabel("금액: -")
        disp_layout.addWidget(self.amount_label)

        self.pick_card_label = QLabel("배팅픽: -")
        self.pick_card_label.setStyleSheet("font-size: 16px; font-weight: bold; padding: 8px; border-radius: 6px;")
        self.pick_card_label.setMinimumHeight(44)
        self.pick_card_label.setAlignment(Qt.AlignCenter)
        disp_layout.addWidget(self.pick_card_label)

        self.stats_label = QLabel("경고/합선/승률: -")
        self.stats_label.setStyleSheet("color: #666; font-size: 12px;")
        self.stats_label.setWordWrap(True)
        disp_layout.addWidget(self.stats_label)

        self.status_display_label = QLabel("대기 중 (시작 누르면 바로 배팅)")
        self.status_display_label.setStyleSheet("color: #81c784; font-weight: bold;")
        disp_layout.addWidget(self.status_display_label)
        self.pick_history_label = QLabel("최근 회차·픽: -")
        self.pick_history_label.setStyleSheet("color: #666; font-size: 11px;")
        self.pick_history_label.setWordWrap(True)
        disp_layout.addWidget(self.pick_history_label)

        g_display.setLayout(disp_layout)
        layout.addWidget(g_display)

        # 시작 / 중지
        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("시작 (바로 배팅)")
        self.start_btn.setMinimumHeight(32)
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn = QPushButton("중지")
        self.stop_btn.setMinimumHeight(32)
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.setEnabled(False)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        layout.addLayout(btn_row)

        # 로그
        layout.addWidget(QLabel("로그:"))
        try:
            from PyQt5.QtWidgets import QTextEdit
            self.log_text = QTextEdit()
            self.log_text.setReadOnly(True)
            self.log_text.setMaximumHeight(80)
            self.log_text.setPlaceholderText("동작 로그")
        except Exception:
            self.log_text = QLineEdit()
            self.log_text.setReadOnly(True)
            self.log_text.setPlaceholderText("동작 로그")
        layout.addWidget(self.log_text)

        layout.addStretch(1)

    def _load_coords(self):
        self._coords = load_coords()
        self._refresh_coord_labels()
        try:
            self.window_left_edit.setText(str(self._coords.get("window_left", "") or ""))
            self.window_top_edit.setText(str(self._coords.get("window_top", "") or ""))
            self.window_width_edit.setText(str(self._coords.get("window_width", "") or ""))
            self.window_height_edit.setText(str(self._coords.get("window_height", "") or ""))
            self.device_width_edit.setText(str(self._coords.get("device_width", "") or ""))
            self.device_height_edit.setText(str(self._coords.get("device_height", "") or ""))
            self.raw_coords_check.setChecked(bool(self._coords.get("raw_coords")))
        except Exception:
            pass

    def _refresh_coord_labels(self):
        for key in COORD_KEYS:
            val = self._coords.get(key)
            if val is not None and isinstance(val, (list, tuple)) and len(val) >= 2:
                self._coord_value_labels[key].setText(f"({val[0]}, {val[1]})")
            else:
                self._coord_value_labels[key].setText("(미설정)")

    def _set_coord_status(self, key, text, color="green"):
        if key in self._coord_status_labels:
            self._coord_status_labels[key].setText(text)
            self._coord_status_labels[key].setStyleSheet(f"color: {color}; font-size: 11px;")

    def _start_coord_capture(self, key):
        if not HAS_PYNPUT:
            self._log("pynput 설치 필요: pip install pynput")
            return
        if self._coord_listener is not None:
            self._log("다른 좌표 잡는 중입니다. 잠시 후 다시 시도하세요.")
            return
        self._coord_capture_key = key
        if key == "window_topleft":
            self._log("창 왼쪽 위 잡기: 이 창이 최소화되면 LDPlayer 창의 왼쪽 위 모서리만 클릭하세요.")
        else:
            self._log("좌표 찾기: 이 창이 최소화된 뒤 반드시 LDPlayer 창 안에서만 클릭하세요. (매크로 창과 겹치면 잘못 잡힙니다)")
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
            self._log("LDPlayer 창 왼쪽 위 저장됨: X=%s, Y=%s — 배팅 좌표가 이 창 기준으로 보정됩니다." % (x, y))
            return
        # 레드/블랙/배팅금액 등: 클릭한 점이 속한 창을 자동 감지 → 창 내 상대 좌표로 저장 (창 왼쪽 위 따로 찍을 필요 없음)
        rect = get_window_rect_at(x, y)
        if rect is not None:
            left, top, w, h = rect
            rel_x, rel_y = x - left, y - top
            self._coords[key] = [rel_x, rel_y]
            self._coords["window_left"] = left
            self._coords["window_top"] = top
            self._coords["window_width"] = w
            self._coords["window_height"] = h
            # 기기 W/H가 비어 있으면 창과 동일하게 채움(1:1). 에뮬이 다르면 '기기 해상도 가져오기'로 덮어쓰기
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
            self._set_coord_status(key, "저장됨(창 자동)", "green")
            self._log("클릭한 창 자동 감지됨. %s = 창 내 (%s, %s), 창 크기 %s×%s. 레드 1회 탭으로 확인해 보세요." % (COORD_KEYS.get(key, key), rel_x, rel_y, w, h))
        else:
            self._coords[key] = [x, y]
            sp = self._coords.get("coord_spaces") or {}
            sp[key] = False
            self._coords["coord_spaces"] = sp
            try:
                self._coords["window_left"] = int(self.window_left_edit.text().strip() or 0)
                self._coords["window_top"] = int(self.window_top_edit.text().strip() or 0)
                self._coords["raw_coords"] = self.raw_coords_check.isChecked()
            except (TypeError, ValueError):
                self._coords["window_left"] = 0
                self._coords["window_top"] = 0
            save_coords(self._coords)
            self._refresh_coord_labels()
            self._set_coord_status(key, "저장됨", "green")
            self._log("창 자동 감지 실패(비-Windows 등). 창 왼쪽 위 잡기로 기준점을 잡아주세요.")
        QTimer.singleShot(1500, lambda: self._set_coord_status(key, ""))

    def _save_window_offset(self):
        try:
            self._coords["window_left"] = int(self.window_left_edit.text().strip() or 0)
            self._coords["window_top"] = int(self.window_top_edit.text().strip() or 0)
            self._coords["raw_coords"] = self.raw_coords_check.isChecked()
            for key, edit in [("window_width", self.window_width_edit), ("window_height", self.window_height_edit),
                              ("device_width", self.device_width_edit), ("device_height", self.device_height_edit)]:
                try:
                    v = int(edit.text().strip() or 0)
                    self._coords[key] = v if v > 0 else 0
                except (TypeError, ValueError):
                    self._coords[key] = 0
            save_coords(self._coords)
            self._log("LDPlayer 창 위치·해상도 저장됨 (탭 좌표 보정에 사용)")
        except (TypeError, ValueError):
            self._log("창 X, Y에는 숫자만 입력하세요.")

    def _on_fetch_device_size(self):
        """ADB로 에뮬레이터 기기 해상도를 가져와 기기 W/H에 채움. 탭이 다른 곳에 눌릴 때 사용."""
        device = self.device_edit.text().strip() or None
        self.device_size_fetch_btn.setEnabled(False)
        self.device_size_fetch_btn.setText("가져오는 중...")

        def run():
            w, h = get_device_size_via_adb(device)
            if self.device_size_fetched is not None:
                self.device_size_fetched.emit(w, h)
            else:
                self.device_size_fetch_btn.setEnabled(True)
                self.device_size_fetch_btn.setText("기기 해상도 가져오기")

        threading.Thread(target=run, daemon=True).start()

    def _on_device_size_fetched(self, w, h):
        """기기 해상도 가져오기 완료 (메인 스레드)."""
        self.device_size_fetch_btn.setEnabled(True)
        self.device_size_fetch_btn.setText("기기 해상도 가져오기")
        if w > 0 and h > 0:
            self.device_width_edit.setText(str(w))
            self.device_height_edit.setText(str(h))
            self._coords["device_width"] = w
            self._coords["device_height"] = h
            save_coords(self._coords)
            self._log("기기 해상도 %s×%s 가져옴. 이제 레드 1회 탭으로 확인해 보세요." % (w, h))
        else:
            self._log("기기 해상도 가져오기 실패. ADB 연결 확인 후 다시 시도하세요.")

    def _on_adb_devices(self):
        """CMD와 동일한 방식으로 adb 실행 후 기기 목록·실제 연결 테스트."""
        btn = self.adb_devices_btn
        restore = "ADB 연결 확인"
        btn.setEnabled(False)
        btn.setText("확인 중...")
        self._log("ADB 연결 확인 중... (CMD와 동일한 방식으로 실행)")
        user_device = (self.device_edit.text().strip() or "").strip() or "127.0.0.1:5555"
        def run():
            msg = ""
            try:
                # 1) adb start-server 먼저 (CMD에서 첫 adb 명령과 동일)
                _run_adb_shell_cmd(None, "start-server")
                time.sleep(0.3)
                # 2) adb devices — Windows는 shell=True로 CMD와 동일 환경
                code, out, err = _run_adb_shell_cmd(None, "devices")
                out = out or ""
                err = err or ""
                msg = "ADB devices (원본):\n" + (out if out else "(stdout 없음)")
                if err:
                    msg += "\n[stderr] " + err
                # \r 제거 후 파싱
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
                    # 진단 정보 없이: LDPlayer/에뮬 자주 쓰는 포트 자동 시도
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
                        if self.adb_device_suggested is not None:
                            self.adb_device_suggested.emit(connected_device)
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
                        if self.adb_device_suggested is not None:
                            self.adb_device_suggested.emit(working[0])
                    else:
                        msg += "\n→ 기기는 보이지만 shell 명령 실패. stderr 확인."
            except Exception as e:
                msg += "\n예외: " + str(e)
            if self.test_tap_done is not None:
                self.test_tap_done.emit(btn, restore, msg)
        threading.Thread(target=run, daemon=True).start()

    def _on_adb_test_tap(self):
        """배팅금액 좌표 탭 후 5000원 입력 (테스트로 금액이 들어가는지 확인)."""
        btn = self.adb_test_btn
        restore = "배팅금액 테스트 (5000원)"
        self._coords = load_coords()
        bet_xy = self._coords.get("bet_amount")
        if not bet_xy or len(bet_xy) < 2:
            self._log("배팅금액 좌표를 먼저 잡아주세요.")
            return
        device = self.device_edit.text().strip() or None
        btn.setEnabled(False)
        btn.setText("테스트 중...")
        self._log("배팅금액 테스트 중... (탭 → 5000 입력 → BACK)")
        coords = dict(self._coords)
        bet_xy_copy = [int(bet_xy[0]), int(bet_xy[1])]

        def run():
            msg = ""
            try:
                # ADB 연결 확인 (Windows에서 CMD와 동일한 방식)
                rc, _, _ = _run_adb_shell_cmd(device, "shell", "echo", "ok")
                if rc != 0:
                    msg = "ADB 연결 실패. ADB 기기 칸 확인 후 'ADB 연결 확인' 버튼으로 테스트하세요."
                    if self.test_tap_done is not None:
                        self.test_tap_done.emit(btn, restore, msg)
                    return
                tx, ty = _apply_window_offset(coords, bet_xy_copy[0], bet_xy_copy[1], key="bet_amount")
                adb_swipe(device, tx, ty, 100)
                time.sleep(0.6)
                adb_input_text(device, "5000")
                time.sleep(0.4)
                adb_keyevent(device, 4)  # BACK
                cmd_str = "adb -s %s shell input swipe %s %s %s %s 100" % (device or "", tx, ty, tx, ty) if device else "adb shell input swipe %s %s %s %s 100" % (tx, ty, tx, ty)
                msg = "배팅금액 테스트 완료. 보정 (%s,%s) | 직접 테스트: %s" % (tx, ty, cmd_str)
            except Exception as e:
                msg = "배팅금액 테스트 실패: " + str(e)
            if self.test_tap_done is not None:
                self.test_tap_done.emit(btn, restore, msg)
        threading.Thread(target=run, daemon=True).start()

    def _on_adb_color_tap(self, color_key):
        """레드 또는 블랙 좌표 1회 탭 (즉시 반응 + 스레드에서 실행)."""
        label = "레드" if color_key == "red" else "블랙"
        btn = self.adb_red_btn if color_key == "red" else self.adb_black_btn
        restore = "레드 1회 탭" if color_key == "red" else "블랙 1회 탭"
        self._coords = load_coords()
        xy = self._coords.get(color_key)
        if not xy or len(xy) < 2:
            self._log("%s 좌표를 먼저 잡아주세요." % label)
            return
        # 해당 키가 창 상대가 아니고, 창 왼쪽 위도 비어 있으면 탭 위치가 어긋날 수 있음
        spaces = self._coords.get("coord_spaces") or {}
        if not self._coords.get("raw_coords") and not spaces.get(color_key) and not self._coords.get("coords_are_window_relative"):
            wleft = int(self._coords.get("window_left") or 0)
            wtop = int(self._coords.get("window_top") or 0)
            if wleft == 0 and wtop == 0:
                self._log("참고: 레드/블랙 좌표 찾기로 버튼을 클릭해 저장하면 창이 자동 감지됩니다. 그후 탭 테스트해 보세요.")
        btn.setEnabled(False)
        btn.setText("탭 중...")
        self._log("%s 버튼 탭 실행 중..." % label)
        device = self.device_edit.text().strip() or None
        coords = dict(self._coords)
        xy_list = [int(xy[0]), int(xy[1])]
        def run():
            msg = ""
            try:
                tx, ty = _apply_window_offset(coords, xy_list[0], xy_list[1], key=color_key)
                adb_swipe(device, tx, ty, 100)
                cmd_str = "adb -s %s shell input swipe %s %s %s %s 100" % (device or "", tx, ty, tx, ty) if device else "adb shell input swipe %s %s %s %s 100" % (tx, ty, tx, ty)
                msg = "%s 테스트 완료. 보정 (%s,%s) | 직접: %s" % (label, tx, ty, cmd_str)
            except Exception as e:
                msg = "%s 테스트 실패: %s" % (label, e)
            if self.test_tap_done is not None:
                self.test_tap_done.emit(btn, restore, msg)
        threading.Thread(target=run, daemon=True).start()

    def _on_analyzer_nick_changed(self, nick):
        """닉네임 선택 시 Analyzer URL 입력란에 해당 주소 설정 (API용 루트 주소)."""
        if nick and nick in self._analyzer_nick_urls:
            self.analyzer_url_edit.setText(self._analyzer_nick_urls[nick])

    def _on_connect_analyzer(self):
        """Analyzer URL/계산기로 1회 조회 후 픽·금액 표시 — 배팅 정보가 자연스럽게 들어오는지 확인용."""
        url = normalize_analyzer_url(self.analyzer_url_edit.text().strip())
        if not url:
            self._log("분석기(닉네임)를 선택하거나 Analyzer URL을 입력한 뒤 연결하세요.")
            return
        calc_id = self.calc_combo.currentData()
        self.connect_btn.setEnabled(False)
        self.connect_status_label.setText("연결 중…")
        self.connect_status_label.setStyleSheet("color: #666; font-size: 11px;")

        def do_fetch():
            pick = {"pick_color": None, "round": None, "probability": None, "error": None}
            try:
                pick = fetch_current_pick(url, calculator_id=calc_id, timeout=8)
            except Exception as e:
                pick["error"] = str(e)
            if self.connect_result_ready is not None:
                self.connect_result_ready.emit(pick, {})

        thread = threading.Thread(target=do_fetch, daemon=True)
        thread.start()

    def _apply_connect_result(self, pick, results):
        self.connect_btn.setEnabled(True)
        with self._lock:
            self._pick_data = pick if isinstance(pick, dict) else {}
            self._results_data = results if isinstance(results, dict) else {}
        self._update_display()
        err = self._pick_data.get("error")
        if err:
            self.connect_status_label.setText("연결 실패")
            self.connect_status_label.setStyleSheet("color: #c62828; font-size: 11px;")
            self._log(f"Analyzer 연결 실패: {err}")
        else:
            self.connect_status_label.setText("연결됨")
            self.connect_status_label.setStyleSheet("color: #2e7d32; font-size: 11px;")
            self._connected = True
            self._analyzer_url = self.analyzer_url_edit.text().strip()
            self._calculator_id = self.calc_combo.currentData()
            if not self._timer.isActive():
                self._timer.start(int(self._poll_interval_sec * 1000))
            self._log("Analyzer 연결됨 — 픽/회차가 계속 갱신됩니다. 확인 후 시작하세요.")

    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        if hasattr(self.log_text, "append"):
            self.log_text.append(line)
            try:
                sb = self.log_text.verticalScrollBar()
                if sb:
                    sb.setValue(sb.maximum())
            except Exception:
                pass
        else:
            self.log_text.setText(line)

    def _on_start(self):
        url = self.analyzer_url_edit.text().strip()
        if not url:
            self._log("Analyzer URL을 입력하세요.")
            return
        self._analyzer_url = url
        self._calculator_id = self.calc_combo.currentData()
        self._device_id = self.device_edit.text().strip() or "127.0.0.1:5555"
        # 배팅 중: 0.25초 간격으로 픽 조회 (픽→LDPlayer 배팅 지연 최소화. PC 부하 있으면 0.4~0.5로 늘리세요)
        self._poll_interval_sec = 0.25
        self._coords = load_coords()
        if not self._coords.get("bet_amount") or not self._coords.get("red") or not self._coords.get("black"):
            self._log("좌표를 먼저 설정하세요. coord_picker.py로 배팅금액/정정/레드/블랙 좌표를 잡으세요.")
            return
        self._running = True
        self._last_round_when_started = None
        self._last_bet_round = None
        self._pick_history.clear()  # 전회차 히스토리 초기화 → 2회 연속 일치 시에만 배팅
        self._pick_data = {}  # 이전 연결/폴링 잔여 픽 제거 — 계산기 정지 시 매크로만 시작해도 배팅 들어가는 것 방지
        self._display_stable = None  # 표시 깜빡임 방지 상태 초기화 — 새 폴링으로 다시 안정화
        self._display_candidate = None
        self._display_confirm_count = 0
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._log("시작 — 계산기 픽 바뀌는 즉시 사이트로 전송합니다.")
        self._timer.start(int(self._poll_interval_sec * 1000))
        QTimer.singleShot(50, self._poll)   # 시작 직후 50ms 뒤 1회 폴링 (빠른 픽 확보)

    def _on_stop(self):
        self._running = False
        if not self._connected:
            self._timer.stop()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._log("배팅 중지 (연결 유지 시 픽은 계속 갱신)")

    def _poll(self):
        """타이머에서 호출: 스레드에서 서버 요청 후 결과만 메인 스레드로 전달 (UI 멈춤 방지)."""
        if not self._running and not self._connected:
            return
        url = (self._analyzer_url or "").strip() or self.analyzer_url_edit.text().strip()
        if not url:
            return
        self._analyzer_url = url
        calc_id = self._calculator_id
        if calc_id is None:
            calc_id = self.calc_combo.currentData()
            self._calculator_id = calc_id
        url_snap = url
        calc_snap = calc_id

        def do_fetch():
            """계산기에서 회차·배팅중 픽·배팅금액만 가져옴. 계산/승패는 분석기 가상배팅 계산기가 담당."""
            try:
                pick = fetch_current_pick(url_snap, calculator_id=calc_snap, timeout=4)
            except Exception as e:
                pick = {"error": str(e)}
            if pick is None:
                pick = {"error": "픽 조회 지연"}
            if self.poll_done is not None:
                self.poll_done.emit(pick, {})

        threading.Thread(target=do_fetch, daemon=True).start()

    def _on_poll_done(self, pick, results):
        """폴링 결과 수신: 회차·배팅중 픽·금액만 반영하고 배팅. 계산/승패는 분석기에서."""
        with self._lock:
            self._pick_data = pick if isinstance(pick, dict) else {}
            self._results_data = results if isinstance(results, dict) else {}
        # 표시 깜빡임 방지: 같은 (회차, 픽)이 2회 연속 올 때만 카드 문구 갱신
        round_num = self._pick_data.get("round")
        raw_color = self._pick_data.get("pick_color")
        pick_color = _normalize_pick_color(raw_color)
        key = (round_num, pick_color)
        if self._display_stable is None:
            self._display_stable = key
            self._display_candidate = key
            self._display_confirm_count = 1
        elif key == self._display_candidate:
            self._display_confirm_count += 1
            if self._display_confirm_count >= self._display_confirm_needed:
                self._display_stable = key
        else:
            self._display_candidate = key
            self._display_confirm_count = 1
            # _display_stable 유지 — 새 값이 2회 연속 올 때만 바꿈
        self._update_display()

        # 매크로는 오는 픽만 따라감. 목표금액/중지 판단은 분석기 계산기에서만 함 — running=False 수신해도 여기서 자동 중지하지 않음.

        if not self._running:
            self._amount_confirm_round = None
            self._amount_confirm_pick = None
            self._amount_confirm_amounts = []
            return
        # 계산기가 정지 상태면 픽이 있어도 배팅하지 않음 (서버가 픽을 비워도 레거시/타이밍으로 값이 올 수 있음)
        if self._pick_data.get("running") is False:
            self._amount_confirm_round = None
            self._amount_confirm_pick = None
            self._amount_confirm_amounts = []
            return
        round_num = self._pick_data.get("round")
        raw_color = self._pick_data.get("pick_color")
        # 서버는 RED/BLACK 또는 빨강/검정 올 수 있음 → 항상 RED/BLACK으로 통일
        pick_color = _normalize_pick_color(raw_color)
        amount = self._pick_data.get("suggested_amount")
        # 계산기 멈춤 연동: suggested_amount 없거나 0이면 이 회차 배팅 스킵 (macro.py와 동일)
        try:
            amt_val = int(amount) if amount is not None else 0
        except (TypeError, ValueError):
            amt_val = 0
        if amount is None or amt_val <= 0:
            self._log("[멈춤] 계산기 멈춤 구간 (금액=%s) — 회차 %s 배팅 스킵" % (amount, round_num))
            self._amount_confirm_round = None
            self._amount_confirm_pick = None
            self._amount_confirm_amounts = []
            return
        if round_num is None or pick_color is None:
            self._log("[보류] 서버 픽 없음 (회차=%s, 픽=%s) — 배팅 스킵" % (round_num, raw_color or "(없음)"))
            self._amount_confirm_round = None
            self._amount_confirm_pick = None
            self._amount_confirm_amounts = []
            return
        try:
            round_num = int(round_num)
        except (TypeError, ValueError):
            return
        # 이미 이 회차로 배팅했으면 스킵 (중복 배팅 방지). 오래된 회차는 목록에서 제거
        with self._lock:
            for old_r in list(self._pending_bet_rounds.keys()):
                if old_r < round_num - 20:
                    self._pending_bet_rounds.pop(old_r, None)
            if round_num in self._pending_bet_rounds:
                return
        if self._last_bet_round is not None and round_num <= self._last_bet_round:
            return

        # 배팅금액 2~3회만 받고 즉시 배팅: 같은 회차 금액을 2~3번만 수집한 뒤 마지막(최신) 금액으로 재빨리 배팅
        if self._amount_confirm_round is not None and self._amount_confirm_round != round_num:
            self._amount_confirm_round = None
            self._amount_confirm_pick = None
            self._amount_confirm_amounts = []
        if self._amount_confirm_round == round_num:
            # 이미 이 회차로 금액 수집 중 → 추가 수집 (최대 3개)
            if len(self._amount_confirm_amounts) < self._amount_confirm_want:
                self._amount_confirm_amounts.append(amt_val)
            if len(self._amount_confirm_amounts) >= 2:
                final_amt = self._amount_confirm_amounts[-1]
                self._log("픽 수신: %s회 %s %s원 (2~3회 확인 후 배팅)" % (round_num, pick_color, final_amt))
                self._pick_history.append((round_num, pick_color))
                self._amount_confirm_round = None
                self._amount_confirm_pick = None
                self._amount_confirm_amounts = []
                self._coords = load_coords()
                self._run_bet(round_num, pick_color, final_amt)
            return
        # 이 회차 첫 수신 → 금액 2~3회만 더 받고 배팅
        self._amount_confirm_round = round_num
        self._amount_confirm_pick = pick_color
        self._amount_confirm_amounts = [amt_val]
        self._log("픽 수신: %s회 %s %s원 (금액 2~3회 확인 후 즉시 배팅)" % (round_num, pick_color, amount))
        self._pick_history.append((round_num, pick_color))
        if HAS_PYQT:
            QTimer.singleShot(40, self._poll)
            QTimer.singleShot(100, self._poll)
            # 2~3회 못 받아도 180ms 후 1회 분 금액으로 배팅 (멈춤 방지)
            QTimer.singleShot(180, self._on_amount_confirm_timeout)
        # 2번째·3번째 응답에서 위 분기로 들어와 2회 이상 모이면 _run_bet 호출됨
        return

    def _on_amount_confirm_timeout(self):
        """금액 2~3회 수집 타임아웃: 1회 분이라도 있으면 그 금액으로 즉시 배팅. 보류/멈춤이면 스킵."""
        if not self._running or self._amount_confirm_round is None:
            return
        if not self._amount_confirm_amounts:
            self._amount_confirm_round = None
            self._amount_confirm_pick = None
            return
        round_num = self._amount_confirm_round
        pick_color = self._amount_confirm_pick
        final_amt = self._amount_confirm_amounts[-1]
        self._amount_confirm_round = None
        self._amount_confirm_pick = None
        self._amount_confirm_amounts = []
        # 최신 픽 재검증: 보류/멈춤이면 사이트에 배팅 보내지 않음 (규칙 emulator-macro.mdc §1)
        with self._lock:
            pd = dict(self._pick_data)
        if pd.get("running") is False:
            self._log("[보류] 계산기 정지 — 배팅 스킵")
            return
        if _normalize_pick_color(pd.get("pick_color")) is None:
            self._log("[보류] 현재 픽 없음 — 배팅 스킵")
            return
        try:
            amt = int(pd.get("suggested_amount") or 0)
        except (TypeError, ValueError):
            amt = 0
        if amt <= 0:
            self._log("[멈춤] 현재 금액 없음 — 배팅 스킵")
            return
        with self._lock:
            if round_num in self._pending_bet_rounds or (self._last_bet_round is not None and round_num <= self._last_bet_round):
                return
        self._log("픽 수신: %s회 %s %s원 (확인 1회분으로 즉시 배팅)" % (round_num, pick_color, final_amt))
        self._coords = load_coords()
        self._run_bet(round_num, pick_color, final_amt)

    def _run_bet(self, round_num, pick_color, amount):
        """실제 배팅 실행. 성공 시 _last_bet_round 갱신."""
        self._log("배팅 실행: %s회 %s %s원" % (round_num, pick_color, amount))
        ok = self._do_bet(round_num, pick_color, amount)
        if ok:
            self._last_bet_round = round_num
            if HAS_PYQT:
                QTimer.singleShot(200, self._poll)
                QTimer.singleShot(500, self._poll)
        else:
            pass

    def _do_bet(self, round_num, pick_color, amount_from_calc=None):
        """금액 입력 → RED/BLACK 픽대로 레드 또는 블랙 탭 → 마지막에 정정(선택). 성공 시 True, 실패(스킵) 시 False."""
        try:
            amt = int(amount_from_calc) if amount_from_calc is not None else 0
        except (TypeError, ValueError):
            amt = 0
        if amt <= 0:
            self._log("배팅금액이 없습니다. 분석기 웹에서 해당 계산기 배팅금액 입력 후 저장하세요.")
            return False
        # 배팅 시작 전에 이 회차를 대기 목록에 넣어, 동시에 같은 회차로 또 배팅하는 것 방지
        with self._lock:
            if round_num in self._pending_bet_rounds:
                return False
            self._pending_bet_rounds[round_num] = {"pick_color": pick_color, "amount": amt}
        bet_amount = str(amt)
        # 창 이동 대응: 배팅 직전 최신 좌표·창 위치 로드 (emulator-macro.mdc §2)
        coords = load_coords()
        device = self._device_id or None

        bet_xy = coords.get("bet_amount")
        confirm_xy = coords.get("confirm")
        red_xy = coords.get("red")
        black_xy = coords.get("black")
        if not bet_xy or len(bet_xy) < 2:
            self._log("배팅금액 좌표 없음 — 좌표 찾기로 배팅금액 위치를 잡아주세요.")
            with self._lock:
                self._pending_bet_rounds.pop(round_num, None)
            return False
        if pick_color == "RED" and (not red_xy or len(red_xy) < 2):
            self._log("레드 좌표 없음 — 좌표 찾기로 레드 버튼 위치를 잡아주세요.")
            with self._lock:
                self._pending_bet_rounds.pop(round_num, None)
            return False
        if pick_color == "BLACK" and (not black_xy or len(black_xy) < 2):
            self._log("블랙 좌표 없음 — 좌표 찾기로 블랙 버튼 위치를 잡아주세요.")
            with self._lock:
                self._pending_bet_rounds.pop(round_num, None)
            return False

        def tap_swipe(ax, ay, coord_key=None, duration_ms=None):
            """tap 대신 swipe(터치 다운·업) — 웹/앱에서 버튼이 tap에 안 먹을 때 사용."""
            tx, ty = _apply_window_offset(coords, ax, ay, key=coord_key)
            ms = duration_ms if duration_ms is not None else BET_COLOR_SWIPE_MS
            adb_swipe(device, tx, ty, ms)

        last_error = None
        for attempt in range(BET_RETRY_ATTEMPTS):
            try:
                # 1) 배팅금액 칸 탭(지속 시간 길게 → 터치 확실) → 포커스 대기 → 기존 값 삭제(DEL) → 금액 입력 → 키보드 닫기
                tx, ty = _apply_window_offset(coords, bet_xy[0], bet_xy[1], key="bet_amount")
                adb_swipe(device, tx, ty, BET_AMOUNT_SWIPE_MS)
                time.sleep(BET_DELAY_AFTER_AMOUNT_TAP)
                for _ in range(15):
                    adb_keyevent(device, KEYCODE_DEL)
                    time.sleep(0.002)
                time.sleep(0.015)
                adb_input_text(device, bet_amount)
                time.sleep(BET_DELAY_AFTER_INPUT)
                adb_keyevent(device, 4)  # BACK
                time.sleep(BET_DELAY_AFTER_BACK)
                # 2) 픽 RED=레드 / BLACK=블랙 (한 번만 탭)
                tap_red_button = pick_color == "RED"
                color_xy = red_xy if tap_red_button else black_xy
                color_key = "red" if tap_red_button else "black"
                button_name = "레드" if tap_red_button else "블랙"
                cx, cy = _apply_window_offset(coords, color_xy[0], color_xy[1], key=color_key)
                self._log("ADB: 픽 %s → %s 버튼 탭 (%s,%s)" % (pick_color, button_name, cx, cy))
                tap_swipe(color_xy[0], color_xy[1], color_key, BET_COLOR_SWIPE_MS)
                time.sleep(BET_DELAY_AFTER_COLOR_TAP)
                # 3) 정정 버튼(배팅 확정) — 1번만 탭 (여러 번 누르면 버벅거림)
                if confirm_xy and len(confirm_xy) >= 2:
                    tap_swipe(confirm_xy[0], confirm_xy[1], "confirm", BET_COLOR_SWIPE_MS)
                    time.sleep(BET_DELAY_AFTER_CONFIRM)

                pred_text = "정" if pick_color == "RED" else "꺽"
                self._log(f"{round_num}회차 {pred_text} {pick_color} {bet_amount}원 (ADB 완료 — 사이트 반영은 화면에서 확인)")
                return True
            except Exception as e:
                last_error = e
                if attempt < BET_RETRY_ATTEMPTS - 1:
                    self._log("배팅 시도 실패 → %s초 후 재시도 (%s)" % (BET_RETRY_DELAY, str(e)[:60]))
                    time.sleep(BET_RETRY_DELAY)
                else:
                    break
        self._log("배팅 실행 중 오류: %s — 같은 회차 다음 폴링에 재시도됩니다." % (str(last_error)[:80] if last_error else "unknown"))
        with self._lock:
            self._pending_bet_rounds.pop(round_num, None)
        return False

    def _update_display(self):
        with self._lock:
            pick = self._pick_data.copy()
            results = self._results_data.copy()
        # 깜빡임 방지: 2회 연속 같은 값일 때만 바뀐 _display_stable 사용, 아니면 이전 표시 유지
        if self._display_stable is not None:
            stable_round, stable_color = self._display_stable
            round_num = stable_round
            pick_color = stable_color
        else:
            round_num = pick.get("round")
            raw_color = pick.get("pick_color")
            pick_color = _normalize_pick_color(raw_color)
        prob = pick.get("probability")
        icon_ch, _ = get_round_icon(round_num)
        round_str = f"{round_num}회 {icon_ch}" if round_num is not None else "-"
        self.round_label.setText(f"회차: {round_str}")
        suggested = pick.get("suggested_amount")
        if suggested is not None and int(suggested) > 0:
            amount_str = str(int(suggested))
        else:
            amount_str = "-"
        self.amount_label.setText(f"금액: {amount_str} (계산기에서 전달)")

        # 정/꺽 + 색깔 카드 (RED=정·빨강, BLACK=꺽·검정)
        if pick_color == "RED":
            self.pick_card_label.setText("정 · 빨강 (RED)")
            self.pick_card_label.setStyleSheet("font-size: 16px; font-weight: bold; padding: 8px; border-radius: 6px; background: #ffcdd2; color: #b71c1c;")
        elif pick_color == "BLACK":
            self.pick_card_label.setText("꺽 · 검정 (BLACK)")
            self.pick_card_label.setStyleSheet("font-size: 16px; font-weight: bold; padding: 8px; border-radius: 6px; background: #cfd8dc; color: #263238;")
        else:
            self.pick_card_label.setText("보류")
            self.pick_card_label.setStyleSheet("font-size: 16px; font-weight: bold; padding: 8px; border-radius: 6px; background: #eee; color: #666;")

        # 계산/승패/합산승률은 분석기 가상배팅 계산기에서 처리
        self.stats_label.setText("")

        # 전회차 최대 5회 표시 (회차·픽 확인용) (회차매칭 확인용). 현재 픽 포함해 최근 5개
        recent = list(self._pick_history)
        if round_num is not None and pick_color:
            recent = recent + [(round_num, pick_color)]
        recent = recent[-5:]
        if recent:
            hist_str = ", ".join("%s회 %s" % (r, c) for r, c in recent)
            self.pick_history_label.setText("최근 회차·픽: " + hist_str)
        else:
            self.pick_history_label.setText("최근 회차·픽: -")

        if self._running:
            self.status_display_label.setText("배팅 중 (픽마다 자동)")
        else:
            self.status_display_label.setText("대기 중 (시작 누르면 바로 배팅)")


def main():
    if not HAS_PYQT:
        print("PyQt5 필요: pip install PyQt5")
        return
    app = QApplication([])
    w = EmulatorMacroWindow()
    w.show()
    app.exec_()


if __name__ == "__main__":
    main()
