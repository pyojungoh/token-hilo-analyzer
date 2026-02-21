# -*- coding: utf-8 -*-
"""
에뮬레이터(LDPlayer) 자동배팅 매크로.
- 서버(계산기)에서 회차·배팅중 픽·배팅금액만 받아서 ADB로 배팅. 매크로는 전회차/다음회차 계산 안 함.
- 한 회차당 1번만 배팅. 분석기 웹 계산기1/2/3 중 선택 → 해당 계산기 픽/금액 수신 → 금액 입력 → RED/BLACK 탭 → 정정.
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

# 배팅 동작 간 지연(초). 픽 수신 즉시 사이트로 빠르게 배팅 — 최소화. 입력/확정이 안 먹으면 값을 늘리세요.
BET_DELAY_BEFORE_EXECUTE = 0.15  # 배팅 실행 전 대기(초) — 픽 수신 즉시 배팅
BET_DELAY_AFTER_AMOUNT_TAP = 0.01  # 금액 칸 탭 후 포커스 대기 (자동 클리어됨)
BET_DELAY_AFTER_INPUT = 0.01  # 금액 입력 후 바로 BACK
BET_DELAY_AFTER_BACK = 0.12  # 키보드 닫힌 뒤 바로 레드/블랙 탭
BET_AMOUNT_CONFIRM_COUNT = 1  # 같은 (회차, 픽, 금액) 1회 수신 시 즉시 배팅 (2회는 서버 응답 변동으로 놓치는 경우 있음)
PUSH_PICK_PORT = 8765  # 중간페이지→매크로 푸시 수신 포트. 푸시 시 3회확인 생략·즉시 ADB
PUSH_BET_DELAY = 0.15  # 푸시 수신 시 배팅 전 대기(초) — 배팅시간 확보용 최소화
BET_DEL_COUNT = 8  # 기존 값 삭제용 DEL (8자리: 99999999까지. 탭 후 바로 입력 위해 최소화)
BET_AMOUNT_DOUBLE_INPUT = False  # 금액 1회만 입력 (이중 입력 시 1000010000 중복 발생)
BET_DELAY_AFTER_COLOR_TAP = 0.01
BET_DELAY_BETWEEN_CONFIRM_TAPS = 0.02
BET_DELAY_AFTER_CONFIRM = 0.02
BET_CONFIRM_TAP_COUNT = 1  # 정정 버튼 1번만
BET_RETRY_ATTEMPTS = 1  # 한 회차당 1번만 배팅 (재시도 시 중복 배팅됨)


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


def _run_push_server(port, on_pick_callback):
    """중간페이지→매크로 푸시 수신. POST /push-pick {round, pick_color, suggested_amount} → 즉시 배팅용."""
    from http.server import HTTPServer, BaseHTTPRequestHandler
    pick_data = [None]  # mutable for closure

    class PushHandler(BaseHTTPRequestHandler):
        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_POST(self):
            if self.path != "/push-pick":
                self.send_response(404)
                self.end_headers()
                return
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len).decode("utf-8", errors="replace") if content_len else "{}"
                data = json.loads(body) if body.strip() else {}
                pick_data[0] = {
                    "round": data.get("round"),
                    "pick_color": data.get("pick_color") or data.get("pickColor"),
                    "suggested_amount": data.get("suggested_amount") or data.get("suggestedAmount"),
                }
                if callable(on_pick_callback):
                    on_pick_callback(pick_data[0])
            except Exception:
                pass
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, format, *args):
            pass

    try:
        server = HTTPServer(("", port), PushHandler)
        server.serve_forever()
    except Exception:
        pass


def fetch_current_pick(analyzer_url, calculator_id=1, timeout=5):
    """GET {analyzer_url}/api/current-pick-relay?calculator=1|2|3 (계산기 상단 배팅중 금액 = DB 클라이언트 POST 값)"""
    base = normalize_analyzer_url(analyzer_url)
    if not base:
        return {"pick_color": None, "round": None, "probability": None, "error": "URL 없음"}
    url = base + "/api/current-pick-relay"
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
    """클릭한 점이 속한 창의 클라이언트 영역 (left, top, width, height) 반환.
    GetClientRect + ClientToScreen 사용 — LDPlayer 실제 화면 영역과 일치 (제목줄/테두리 제외)."""
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


def _apply_window_offset(coords, x, y, key=None):
    """창 내 상대 좌표 또는 화면 좌표 → 기기 해상도로 스케일. raw_coords=True면 그대로. key=좌표키면 해당 키만 창상대 여부 적용."""
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
            # 창/기기 미설정 시: 창 상대면 그대로, 아니면 0,0 기준이라 화면좌표가 됨 → 기기 범위로 클램프(밖으로 튕김 방지)
            elif not is_window_relative and (ox == 0 and oy == 0) and (dev_w > 0 and dev_h > 0):
                rx = max(0, min(dev_w - 1, rx))
                ry = max(0, min(dev_h - 1, ry))
        except (TypeError, ValueError):
            pass
        # 기기 범위 초과 시 클램프 (밖으로 튕김 방지)
        dev_w = int(coords.get("device_width") or 0)
        dev_h = int(coords.get("device_height") or 0)
        if dev_w > 0 and dev_h > 0:
            rx = max(0, min(dev_w - 1, rx))
            ry = max(0, min(dev_h - 1, ry))
        return rx, ry
    except (TypeError, ValueError):
        return int(x), int(y)


def _run_adb_shell_cmd(device_id, *args):
    """Windows에서는 CMD와 동일한 환경으로 adb 실행(shell=True). 반환: (returncode, stdout, stderr)."""
    kw = {"capture_output": True, "text": True, "timeout": 10, "encoding": "utf-8", "errors": "replace"}
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


def adb_keyevent_repeat(device_id, keycode, count):
    """keyevent를 count회 한 번의 ADB 호출로 전송 (1초 대기 방지)."""
    if count <= 0:
        return
    if count == 1:
        adb_keyevent(device_id, keycode)
        return
    # 한 번의 shell 호출로 여러 keyevent 전송 (8회 DEL = 1회 왕복)
    loop = ";".join(["input keyevent %s" % keycode] * count)
    if device_id:
        cmd = 'adb -s %s shell "%s"' % (device_id, loop)
    else:
        cmd = 'adb shell "%s"' % loop
    try:
        subprocess.run(cmd, shell=True, capture_output=True, timeout=5, encoding="utf-8", errors="replace")
    except Exception:
        pass


class EmulatorMacroWindow(QMainWindow if HAS_PYQT else object):
    connect_result_ready = pyqtSignal(object, object) if HAS_PYQT else None  # (pick, results) 서브스레드 → 메인 스레드
    test_tap_done = pyqtSignal(object, str, str) if HAS_PYQT else None  # (버튼, 복원텍스트, 로그메시지)
    poll_done = pyqtSignal(object, object) if HAS_PYQT else None  # (pick, results) 폴링 스레드 → 메인 스레드
    push_pick_received = pyqtSignal(object) if HAS_PYQT else None  # 푸시 수신 시 (pick dict) → 즉시 배팅
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
        self._bet_rounds_done = set()  # 배팅 완료 회차 집합 — 마틴 끝 후 동일 금액 재송출 방지 (최대 50개 유지)
        self._pending_bet_rounds = {}  # round_num -> { pick_color, amount } (결과 대기 → 승/패/조커 로그)
        self._pick_history = deque(maxlen=5)  # 최근 5회 (round_num, pick_color) — 회차·픽 안정 시에만 배팅
        self._pick_data = {}
        self._results_data = {}
        self._lock = threading.Lock()
        self._do_bet_lock = threading.Lock()  # _do_bet 동시 실행 방지 — 픽 1회만 탭, 2중배팅 절대 방지
        # 표시 깜빡임 방지: 같은 (회차, 픽, 금액)이 2회 연속 올 때만 카드/회차/금액 갱신 (웹·서버 교차 덮어쓰기로 5천↔1만 깜빡임 방지)
        self._display_stable = None  # (round_num, pick_color, amount)
        self._display_candidate = None
        self._display_confirm_count = 0
        self._display_confirm_needed = 1  # 1회 수신 시 즉시 갱신 (20000 고정 방지, 서버 안정화로 깜빡임 완화)
        # 배팅금액 2~3회만 받고 즉시 배팅 (계산기 표와 동일 금액 확정용)
        self._amount_confirm_round = None
        self._amount_confirm_pick = None
        self._amount_confirm_amounts = []
        self._amount_confirm_want = 3
        # 회차 N회 확인: 같은 (회차, 픽, 금액)이 N회 연속 수신될 때만 배팅 (금액 오탐 방지)
        self._bet_confirm_last = None  # (round_num, pick_color, amount)
        self._bet_confirm_count = 0  # 연속 일치 횟수
        self._last_seen_round = None  # 지금까지 본 최고 회차 — 역행(전회차) 수신 시 거부
        self._display_best_amount = None  # (round_num, amount) — 같은 회차에서 본 최고 금액 (표시용, 5000 고정 방지)
        self._round_prev = None  # 전회차 (배팅 완료)
        self._round_current = None  # 지금회차 (배팅 대기/진행)
        self._round_next = None  # 다음회차 (픽 수신 시 설정)
        self._poll_in_flight = False  # 폴링 중복 방지 — 요청 완료 전 새 요청 시작 금지 (CPU 부하 감소)
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
        if HAS_PYQT and self.push_pick_received is not None:
            self.push_pick_received.connect(self._on_push_pick_received)
        # 중간페이지→매크로 푸시 수신 서버 (localhost:8765)
        def _push_cb(pick):
            if HAS_PYQT and self.push_pick_received is not None:
                self.push_pick_received.emit(pick)
        try:
            t = threading.Thread(target=_run_push_server, args=(PUSH_PICK_PORT, _push_cb), daemon=True)
            t.start()
            self._log("푸시 수신: localhost:%s (중간페이지→매크로 즉시 배팅)" % PUSH_PICK_PORT)
        except Exception:
            pass

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
        self.adb_confirm_btn = QPushButton("정정 1회 탭")
        self.adb_confirm_btn.setMinimumHeight(28)
        self.adb_confirm_btn.setToolTip("정정(금액정정) 버튼을 1회 탭해, 좌표가 맞는지 확인합니다.")
        self.adb_confirm_btn.clicked.connect(self._on_adb_confirm_tap)
        self.adb_devices_btn = QPushButton("ADB 연결 확인")
        self.adb_devices_btn.setMinimumHeight(28)
        self.adb_devices_btn.clicked.connect(self._on_adb_devices)
        adb_btn_row.addWidget(self.adb_test_btn)
        adb_btn_row.addWidget(self.adb_red_btn)
        adb_btn_row.addWidget(self.adb_black_btn)
        adb_btn_row.addWidget(self.adb_confirm_btn)
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
        self.raw_coords_check = QCheckBox("원시 좌표 (보정 없이 저장된 x,y 그대로 전송 — 탭이 밖으로 튕기면 체크)")
        self.raw_coords_check.setChecked(False)
        self.raw_coords_check.setToolTip("체크 시 창/해상도 보정 없이 저장된 좌표를 ADB에 그대로 전송. 탭이 밖으로 튕길 때 시도해 보세요.")
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
        win_hint = QLabel("※ 좌표 찾기로 레드/배팅금액 등을 잡으면 창(클라이언트 영역) 위치가 자동 채워집니다. 탭이 밖으로 튕기면 '기기 해상도 가져오기' 후 창 위치 저장.")
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
        self.rounds_track_label = QLabel("전/지금/다음: - / - / -")
        self.rounds_track_label.setStyleSheet("color: #888; font-size: 11px;")
        disp_layout.addWidget(self.rounds_track_label)

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
            self._log("클릭한 창(콘텐츠 영역) 자동 감지됨. %s = (%s, %s), 크기 %s×%s. 레드 1회 탭으로 확인해 보세요." % (COORD_KEYS.get(key, key), rel_x, rel_y, w, h))
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
            self._log("배팅금액 좌표를 먼저 잡아주세요. (좌표 설정 → 금액 찾기)")
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
                dev_w = int(coords.get("device_width") or 0)
                dev_h = int(coords.get("device_height") or 0)
                if dev_w > 0 and dev_h > 0 and (tx < 0 or ty < 0 or tx >= dev_w or ty >= dev_h):
                    msg = "배팅금액 테스트: 보정 좌표 (%s,%s)가 기기(%s×%s) 밖입니다. '기기 해상도 가져오기' 후 좌표를 다시 잡거나, '원시 좌표' 체크해 보세요." % (tx, ty, dev_w, dev_h)
                    if self.test_tap_done is not None:
                        self.test_tap_done.emit(btn, restore, msg)
                    return
                # 1) 배팅금액 칸 탭 1회 (중복 탭 시 오동작)
                adb_swipe(device, tx, ty, 100)
                time.sleep(0.6)  # 키보드 뜰 때까지 대기
                adb_input_text(device, "5000")
                time.sleep(0.5)
                adb_keyevent(device, 4)  # BACK
                cmd_str = "adb -s %s shell input swipe %s %s %s %s 100" % (device or "", tx, ty, tx, ty) if device else "adb shell input swipe %s %s %s %s 100" % (tx, ty, tx, ty)
                msg = "배팅금액 테스트 완료. 저장(%s,%s)→전송(%s,%s) | 직접: %s" % (bet_xy_copy[0], bet_xy_copy[1], tx, ty, cmd_str)
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

    def _on_adb_confirm_tap(self):
        """정정(금액정정) 좌표 1회 탭 (즉시 반응 + 스레드에서 실행)."""
        label = "정정"
        btn = self.adb_confirm_btn
        restore = "정정 1회 탭"
        self._coords = load_coords()
        xy = self._coords.get("confirm")
        if not xy or len(xy) < 2:
            self._log("정정 좌표를 먼저 잡아주세요. (좌표 설정 → 정정 찾기)")
            return
        btn.setEnabled(False)
        btn.setText("탭 중...")
        self._log("정정 버튼 탭 실행 중...")
        device = self.device_edit.text().strip() or None
        coords = dict(self._coords)
        xy_list = [int(xy[0]), int(xy[1])]
        def run():
            msg = ""
            try:
                tx, ty = _apply_window_offset(coords, xy_list[0], xy_list[1], key="confirm")
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
        # 배팅 중: 80ms 간격 — 픽 수신 즉시 배팅
        self._poll_interval_sec = 0.08
        self._coords = load_coords()
        if not self._coords.get("bet_amount") or not self._coords.get("red") or not self._coords.get("black"):
            self._log("좌표를 먼저 설정하세요. coord_picker.py로 배팅금액/정정/레드/블랙 좌표를 잡으세요.")
            return
        self._running = True
        self._last_round_when_started = None
        self._last_bet_round = None
        self._bet_rounds_done.clear()  # 배팅 완료 회차 초기화
        self._pick_history.clear()  # 전회차 히스토리 초기화 → 2회 연속 일치 시에만 배팅
        self._bet_confirm_last = None  # 회차 N회 확인 상태 초기화
        self._bet_confirm_count = 0
        self._last_seen_round = None  # 회차 역행 방지 초기화
        self._pick_data = {}  # 이전 연결/폴링 잔여 픽 제거 — 계산기 정지 시 매크로만 시작해도 배팅 들어가는 것 방지
        self._display_stable = None  # 표시 깜빡임 방지 상태 초기화 — 새 폴링으로 다시 안정화
        self._display_best_amount = None  # 표시용 최고 금액 초기화
        self._display_candidate = None
        self._display_confirm_count = 0
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._log("시작 — 계산기 픽 바뀌는 즉시 사이트로 전송합니다.")
        self._timer.start(int(self._poll_interval_sec * 1000))
        QTimer.singleShot(0, self._poll)   # 시작 직후 즉시 1회 폴링

    def _on_stop(self):
        self._running = False
        self._poll_in_flight = False  # 정지 시 플래그 초기화
        if not self._connected:
            self._timer.stop()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._log("배팅 중지 (연결 유지 시 픽은 계속 갱신)")

    def _poll(self):
        """타이머에서 호출: 스레드에서 서버 요청 후 결과만 메인 스레드로 전달 (UI 멈춤 방지)."""
        if not self._running and not self._connected:
            return
        if self._poll_in_flight:
            return  # 이전 요청 완료 전 중복 방지 — CPU 부하 감소
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
        self._poll_in_flight = True

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
        try:
            self._on_poll_done_impl(pick, results)
        finally:
            self._poll_in_flight = False  # 다음 폴링 허용 — CPU 부하 감소

    def _on_poll_done_impl(self, pick, results):
        """폴링 결과 처리 (내부)."""
        with self._lock:
            self._pick_data = pick if isinstance(pick, dict) else {}
            self._results_data = results if isinstance(results, dict) else {}
        # 표시: 회차·픽·금액 — 최신 값 즉시 반영 (20000 고정 방지, 서버 relay 0.2초 갱신으로 안정화)
        round_num = self._pick_data.get("round")
        raw_color = self._pick_data.get("pick_color")
        pick_color = _normalize_pick_color(raw_color)
        try:
            amt = self._pick_data.get("suggested_amount") or self._pick_data.get("suggestedAmount")
            amt_val = int(amt) if amt is not None else None
        except (TypeError, ValueError):
            amt_val = None
        key = (round_num, pick_color, amt_val)
        self._display_stable = key
        # 같은 회차에서 본 최고 금액 유지 — 오래된 5000이 다시 표시되는 것 방지
        if round_num is not None and amt_val is not None and amt_val > 0:
            prev_round, prev_amt = self._display_best_amount or (None, 0)
            if prev_round != round_num or amt_val > prev_amt:
                self._display_best_amount = (round_num, amt_val)
        self._update_display()

        # 매크로는 오는 픽만 따라감. 목표금액/중지 판단은 분석기 계산기에서만 함 — running=False 수신해도 여기서 자동 중지하지 않음.

        if not self._running:
            return
        # 계산기가 정지 상태면 픽이 있어도 배팅하지 않음 (서버가 픽을 비워도 레거시/타이밍으로 값이 올 수 있음)
        if self._pick_data.get("running") is False:
            return
        round_num = self._pick_data.get("round")
        raw_color = self._pick_data.get("pick_color")
        # 서버는 RED/BLACK 또는 빨강/검정 올 수 있음 → 항상 RED/BLACK으로 통일
        pick_color = _normalize_pick_color(raw_color)
        amount = self._pick_data.get("suggested_amount") or self._pick_data.get("suggestedAmount")
        # 계산기 멈춤 연동: suggested_amount 없거나 0이면 이 회차 배팅 스킵 (macro.py와 동일)
        try:
            amt_val = int(amount) if amount is not None else 0
        except (TypeError, ValueError):
            amt_val = 0
        if amount is None or amt_val <= 0:
            self._log("[멈춤] 계산기 멈춤 구간 (금액=%s) — 회차 %s 배팅 스킵" % (amount, round_num))
            return
        if round_num is None or pick_color is None:
            self._log("서버 픽 없음 (회차=%s, 픽=%s, 금액=%s) — 분석기에서 해당 계산기 실행·픽 확인" % (round_num, raw_color or "(없음)", amount))
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
            if round_num in self._bet_rounds_done:
                return  # 이미 배팅 완료한 회차 — 마틴 끝 후 동일 금액 재송출 방지
        if self._last_bet_round is not None and round_num <= self._last_bet_round:
            return

        # 회차 역행 방지: 이미 더 높은 회차를 본 적 있으면 전회차 데이터 거부 (마틴 금액 오탐 방지)
        if self._last_seen_round is not None and round_num < self._last_seen_round:
            self._log("전회차 데이터 무시: %s회 (최고 %s회)" % (round_num, self._last_seen_round))
            return
        self._last_seen_round = max(self._last_seen_round or 0, round_num)
        self._round_next = round_num  # 픽 수신 시 다음회차 갱신

        # 회차 N회 확인: 같은 (회차, 픽, 금액)이 N회 연속 수신될 때만 배팅 (금액 오탐 방지)
        key = (round_num, pick_color, amt_val)
        if self._bet_confirm_last != key:
            # 회차가 올라갔는데 이전 회차를 아직 안 쳤으면 즉시 배팅 (픽 놓침 방지)
            old = self._bet_confirm_last
            if old is not None and len(old) >= 3 and (self._bet_confirm_count or 0) >= 1:
                old_round, old_pick, old_amt = old[0], old[1], old[2]
                if round_num > old_round and old_round is not None and old_pick and old_amt and old_amt > 0:
                    with self._lock:
                        skip = old_round in self._bet_rounds_done or old_round in self._pending_bet_rounds
                    if not skip and (self._last_bet_round is None or old_round > self._last_bet_round):
                        self._log("회차 변경 감지 — 이전 %s회 즉시 배팅 (놓침 방지)" % old_round)
                        self._coords = load_coords()
                        self._run_bet(old_round, old_pick, old_amt, from_push=False)
            self._bet_confirm_last = key
            self._bet_confirm_count = 1
            self._log("픽 수신: %s회 %s %s원 (1회 확인 — %s회 연속 일치 시 배팅)" % (round_num, pick_color, amt_val, BET_AMOUNT_CONFIRM_COUNT))
            return
        self._bet_confirm_count = (self._bet_confirm_count or 0) + 1
        if self._bet_confirm_count < BET_AMOUNT_CONFIRM_COUNT:
            self._log("픽 수신: %s회 %s %s원 (%s회 확인 — %s회 연속 일치 시 배팅)" % (round_num, pick_color, amt_val, self._bet_confirm_count, BET_AMOUNT_CONFIRM_COUNT))
            return

        # N회 연속 일치 → 배팅
        self._log("픽 수신: %s회 %s %s원 (%s회 확인 — 배팅)" % (round_num, pick_color, amt_val, BET_AMOUNT_CONFIRM_COUNT))
        self._bet_confirm_last = None
        self._bet_confirm_count = 0
        self._pick_history.append((round_num, pick_color))
        self._coords = load_coords()
        self._run_bet(round_num, pick_color, amt_val, from_push=False)

    def _on_push_pick_received(self, pick):
        """중간페이지 푸시 수신: 회차 검증 후 즉시 ADB 전송 (3회확인 생략, 배팅시간 확보)."""
        if not isinstance(pick, dict):
            return
        round_num = pick.get("round")
        raw_color = pick.get("pick_color")
        pick_color = _normalize_pick_color(raw_color)
        try:
            amt = pick.get("suggested_amount")
            amt_val = int(amt) if amt is not None else 0
        except (TypeError, ValueError):
            amt_val = 0
        if round_num is None or pick_color is None:
            return
        try:
            round_num = int(round_num)
        except (TypeError, ValueError):
            return
        if not self._running:
            return
        if amt_val <= 0:
            self._log("[푸시] 금액 없음 — %s회 %s 스킵" % (round_num, pick_color))
            return
        if not _validate_bet_amount(amt_val):
            return
        with self._lock:
            if round_num in self._pending_bet_rounds:
                return
            if round_num in self._bet_rounds_done:
                return  # 이미 배팅 완료한 회차 — 마틴 끝 후 동일 금액 재송출 방지
            if self._last_bet_round is not None and round_num <= self._last_bet_round:
                return
        # 회차 검증: 전회차/지금회차/다음회차 — 픽 회차가 다음회차(또는 지금회차)와 일치 시 배팅
        self._round_next = round_num
        self._last_seen_round = max(self._last_seen_round or 0, round_num)
        self._log("[푸시] %s회 %s %s원 수신 — 회차 검증 후 즉시 ADB" % (round_num, pick_color, amt_val))
        self._coords = load_coords()
        self._run_bet(round_num, pick_color, amt_val, from_push=True)

    def _on_amount_confirm_timeout(self):
        """금액 2~3회 수집 타임아웃: 1회 분이라도 있으면 그 금액으로 즉시 배팅."""
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
        with self._lock:
            if round_num in self._pending_bet_rounds or round_num in self._bet_rounds_done:
                return
            if self._last_bet_round is not None and round_num <= self._last_bet_round:
                return
        self._log("픽 수신: %s회 %s %s원 (확인 1회분으로 즉시 배팅)" % (round_num, pick_color, final_amt))
        self._coords = load_coords()
        self._run_bet(round_num, pick_color, final_amt)

    def _run_bet(self, round_num, pick_color, amount, from_push=False):
        """배팅 실행. from_push면 PUSH_BET_DELAY, 아니면 BET_DELAY_BEFORE_EXECUTE 후 ADB 전송."""
        # 즉시 pending 등록 — 같은 회차로 _run_bet이 연속 호출되어 두 번 배팅되는 것 방지
        with self._lock:
            if round_num in self._pending_bet_rounds:
                return
            self._pending_bet_rounds[round_num] = {"pick_color": pick_color, "amount": amount}
        delay_sec = PUSH_BET_DELAY if from_push else BET_DELAY_BEFORE_EXECUTE
        delay_ms = int(delay_sec * 1000)
        self._log("배팅 예약: %s회 %s %s원 (%s초 후 실행%s)" % (round_num, pick_color, amount, delay_sec, " [푸시]" if from_push else ""))
        def _execute():
            if not self._running:
                return
            with self._lock:
                if round_num in self._bet_rounds_done:
                    self._pending_bet_rounds.pop(round_num, None)
                    return  # 이미 배팅 완료 — 마틴 끝 후 동일 금액 재송출 방지
            if self._last_bet_round is not None and round_num <= self._last_bet_round:
                with self._lock:
                    self._pending_bet_rounds.pop(round_num, None)
                return
            # 배팅 직전: 전/지금/뒤 회차 확인 후 해당 회차의 최신 금액 재조회
            final_amount = amount
            try:
                url = normalize_analyzer_url((self._analyzer_url or "").strip() or self.analyzer_url_edit.text().strip())
                calc_id = self._calculator_id or (self.calc_combo.currentData() if HAS_PYQT else 1)
                if url and calc_id:
                    pick = fetch_current_pick(url, calculator_id=calc_id, timeout=2)
                    if pick and isinstance(pick, dict) and int(pick.get("round") or 0) == round_num:
                        amt = pick.get("suggested_amount") or pick.get("suggestedAmount")
                        if amt is not None and int(amt) > 0:
                            final_amount = int(amt)
                            self._log("배팅 직전 %s회 금액 재조회: %s원 (전/지금/뒤 확인 후)" % (round_num, final_amount))
            except Exception:
                pass
            self._log("배팅 실행: %s회 %s %s원" % (round_num, pick_color, final_amount))
            ok = self._do_bet(round_num, pick_color, final_amount)
            if ok:
                self._last_bet_round = round_num
                with self._lock:
                    self._bet_rounds_done.add(round_num)
                    # 최대 50개 유지 (오래된 것 제거)
                    if len(self._bet_rounds_done) > 50:
                        for r in sorted(self._bet_rounds_done)[:-50]:
                            self._bet_rounds_done.discard(r)
                self._round_prev = self._round_current
                self._round_current = round_num
                self._round_next = round_num + 1
                if HAS_PYQT:
                    QTimer.singleShot(80, self._poll)
                    QTimer.singleShot(300, self._poll)
        if HAS_PYQT and delay_ms > 0:
            QTimer.singleShot(delay_ms, _execute)
        else:
            _execute()

    def _do_bet(self, round_num, pick_color, amount_from_calc=None):
        """금액 입력 → RED/BLACK 픽대로 레드 또는 블랙 탭 → 마지막에 정정(선택). 성공 시 True, 실패(스킵) 시 False.
        한 회차당 1번만 배팅. 픽(RED/BLACK) 버튼은 절대 1회만 탭 — 2중배팅 방지."""
        with self._do_bet_lock:  # 동시 실행 방지 — 픽 1회만 클릭 보장 (전체 배팅 흐름 직렬화)
            with self._lock:
                if round_num in self._bet_rounds_done:
                    self._pending_bet_rounds.pop(round_num, None)
                    return False  # 이미 배팅 완료한 회차 — 중복 방지
                if self._last_bet_round is not None and round_num <= self._last_bet_round:
                    self._pending_bet_rounds.pop(round_num, None)
                    return False  # 이미 배팅한 회차 — 중복 방지
            try:
                amt = int(amount_from_calc) if amount_from_calc is not None else 0
            except (TypeError, ValueError):
                amt = 0
            if not _validate_bet_amount(amt):
                self._log("배팅금액 오류: %s (1~99,999,999 범위 정수만 허용)" % amount_from_calc)
                with self._lock:
                    self._pending_bet_rounds.pop(round_num, None)
                return False
            # _run_bet에서 이미 _pending_bet_rounds에 등록됨 (두 번 배팅 방지)
            bet_amount = str(amt)
            self._log("[금액확인] %s회 %s원 입력 예정" % (round_num, bet_amount))
            coords = self._coords
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

            def tap_swipe(ax, ay, coord_key=None):
                """tap 대신 swipe(터치 다운·업) — 웹/앱에서 버튼이 tap에 안 먹을 때 사용."""
                tx, ty = _apply_window_offset(coords, ax, ay, key=coord_key)
                adb_swipe(device, tx, ty, 50)

            last_error = None
            for attempt in range(BET_RETRY_ATTEMPTS):
                try:
                    # 1) 배팅금액 칸 탭 → 포커스 대기 → 금액 입력 → 키보드 닫기 (탭 시 자동 클리어됨)
                    def _input_amount_once():
                        tap_swipe(bet_xy[0], bet_xy[1], "bet_amount")
                        time.sleep(BET_DELAY_AFTER_AMOUNT_TAP)
                        adb_input_text(device, bet_amount)
                        time.sleep(BET_DELAY_AFTER_INPUT)
                        adb_keyevent(device, 4)  # BACK
                        time.sleep(BET_DELAY_AFTER_BACK)
                    _input_amount_once()
                    if BET_AMOUNT_DOUBLE_INPUT:
                        time.sleep(0.15)  # 첫 입력 반영 대기
                        _input_amount_once()  # 이중 입력 — 오입력 방지
                    # 2) 픽 RED=레드 / BLACK=블랙 — 1회만 탭 (2중배팅 절대 방지)
                    tap_red_button = pick_color == "RED"
                    color_xy = red_xy if tap_red_button else black_xy
                    color_key = "red" if tap_red_button else "black"
                    button_name = "레드" if tap_red_button else "블랙"
                    cx, cy = _apply_window_offset(coords, color_xy[0], color_xy[1], key=color_key)
                    self._log("ADB: 픽 %s → %s 버튼 탭 (%s,%s)" % (pick_color, button_name, cx, cy))
                    adb_swipe(device, cx, cy, 100)  # 픽 버튼 1회만 — 2중배팅 방지
                    time.sleep(BET_DELAY_AFTER_COLOR_TAP)
                    # 3) 정정 버튼(배팅 확정) — 1번만 탭
                    if confirm_xy and len(confirm_xy) >= 2:
                        tap_swipe(confirm_xy[0], confirm_xy[1], "confirm")
                        time.sleep(BET_DELAY_AFTER_CONFIRM)

                    pred_text = "정" if pick_color == "RED" else "꺽"
                    self._log(f"{round_num}회차 {pred_text} {pick_color} {bet_amount}원 (ADB 완료 — 사이트 반영은 화면에서 확인)")
                    return True
                except Exception as e:
                    last_error = e
                    break  # 한 회차 1번만 — 재시도 안 함
            self._log("배팅 실행 중 오류: %s — 같은 회차 다음 폴링에 재시도됩니다." % (str(last_error)[:80] if last_error else "unknown"))
            with self._lock:
                self._pending_bet_rounds.pop(round_num, None)
            return False

    def _update_display(self):
        with self._lock:
            pick = self._pick_data.copy()
            results = self._results_data.copy()
        # 표시: _display_stable(폴링 시 설정) 또는 pick(연결 시 등)
        if self._display_stable is not None and len(self._display_stable) >= 3:
            stable_round, stable_color, stable_amt = self._display_stable[0], self._display_stable[1], self._display_stable[2]
            round_num, pick_color = stable_round, stable_color
            # 같은 회차에서 본 최고 금액 사용 (5000 고정 방지 — 오래된 값이 다시 표시되는 것 방지)
            if self._display_best_amount is not None and self._display_best_amount[0] == stable_round:
                display_amt = self._display_best_amount[1]
            else:
                display_amt = stable_amt
            amount_str = str(display_amt) if display_amt is not None and display_amt > 0 else "-"
        else:
            round_num = pick.get("round")
            raw_color = pick.get("pick_color")
            pick_color = _normalize_pick_color(raw_color)
            suggested = pick.get("suggested_amount")
            if suggested is not None and int(suggested) > 0:
                amount_str = str(int(suggested))
            else:
                amount_str = "-"
        prob = pick.get("probability")
        icon_ch, _ = get_round_icon(round_num)
        round_str = f"{round_num}회 {icon_ch}" if round_num is not None else "-"
        self.round_label.setText(f"회차: {round_str}")
        pr = self._round_prev
        cr = self._round_current
        nr = self._round_next
        self.rounds_track_label.setText("전/지금/다음: %s / %s / %s" % (pr if pr is not None else "-", cr if cr is not None else "-", nr if nr is not None else "-"))
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
