# -*- coding: utf-8 -*-
"""
에뮬레이터(LDPlayer) 자동배팅 매크로.
- 분석기 웹의 계산기1/2/3 중 선택 → 그 계산기와 같은 픽 수신.
- 회차(별동그라미세모), 금액, 배팅픽, 정/꺽 카드, 경고/합선/승률 표시.
- 시작 누르면 현재 픽이 아닌 다음 픽부터 배팅: 금액 입력 → 배팅픽(RED/BLACK) 클릭 (ADB).
"""
import json
import os
import re
import subprocess
import threading
import time
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
        QFrame, QScrollArea, QGridLayout,
    )
    from PyQt5.QtCore import Qt, QTimer, pyqtSlot
    from PyQt5.QtGui import QFont
    HAS_PYQT = True
except ImportError:
    HAS_PYQT = False

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COORDS_PATH = os.path.join(SCRIPT_DIR, "emulator_coords.json")

# 좌표 찾기용 키·라벨 (한곳에 통합)
COORD_KEYS = {"bet_amount": "배팅금액", "confirm": "정정", "red": "레드", "black": "블랙"}


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


def fetch_results(analyzer_url, timeout=5):
    """GET /api/results -> blended_win_rate, prediction_history"""
    base = normalize_analyzer_url(analyzer_url)
    if not base:
        return {"blended_win_rate": None, "prediction_history": [], "error": "URL 없음"}
    try:
        r = requests.get(base + "/api/results", timeout=timeout)
        r.raise_for_status()
        data = r.json()
        return {
            "blended_win_rate": data.get("blended_win_rate"),
            "prediction_history": data.get("prediction_history") or [],
            "error": None,
        }
    except Exception as e:
        return {"blended_win_rate": None, "prediction_history": [], "error": str(e)}


def load_coords():
    if not os.path.exists(COORDS_PATH):
        return {}
    try:
        with open(COORDS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def adb_tap(device_id, x, y):
    """adb -s {device} shell input tap x y"""
    if device_id:
        cmd = ["adb", "-s", device_id, "shell", "input", "tap", str(x), str(y)]
    else:
        cmd = ["adb", "shell", "input", "tap", str(x), str(y)]
    try:
        kw = {"capture_output": True, "timeout": 5}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(cmd, **kw)
    except Exception:
        pass


def adb_input_text(device_id, text):
    """adb shell input text "xxx" (공백은 %s로)"""
    escaped = text.replace(" ", "%s")
    if device_id:
        cmd = ["adb", "-s", device_id, "shell", "input", "text", escaped]
    else:
        cmd = ["adb", "shell", "input", "text", escaped]
    try:
        kw = {"capture_output": True, "timeout": 5}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(cmd, **kw)
    except Exception:
        pass


class EmulatorMacroWindow(QMainWindow if HAS_PYQT else object):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("에뮬레이터 자동배팅 (LDPlayer)")
        self.setMinimumSize(420, 520)

        self._analyzer_url = ""
        self._calculator_id = 1
        self._coords = {}
        self._device_id = "127.0.0.1:5555"
        self._poll_interval_sec = 2.0
        self._running = False
        self._last_round_when_started = None  # 시작 시점의 회차 (이 회차는 배팅 안 함)
        self._last_bet_round = None  # 이미 배팅한 회차 (중복 방지)
        self._pending_bet_rounds = {}  # round_num -> { pick_color, amount } (결과 대기 → 승/패/조커 로그)
        self._pick_data = {}
        self._results_data = {}
        self._lock = threading.Lock()
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

    def _build_ui(self):
        cw = QWidget()
        self.setCentralWidget(cw)
        layout = QVBoxLayout(cw)

        # 설정
        g_set = QGroupBox("설정")
        fl = QFormLayout()
        self.analyzer_url_edit = QLineEdit()
        self.analyzer_url_edit.setText("https://web-production-fa2dd.up.railway.app")
        self.analyzer_url_edit.setPlaceholderText("Analyzer 결과 페이지 주소")
        fl.addRow("Analyzer URL:", self.analyzer_url_edit)

        self.calc_combo = QComboBox()
        self.calc_combo.addItem("계산기 1", 1)
        self.calc_combo.addItem("계산기 2", 2)
        self.calc_combo.addItem("계산기 3", 3)
        fl.addRow("계산기 선택:", self.calc_combo)
        connect_row = QHBoxLayout()
        connect_row.setContentsMargins(0, 4, 0, 4)
        self.connect_btn = QPushButton("Analyzer 연결")
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
        self.device_edit.setPlaceholderText("ADB device (예: 127.0.0.1:5555)")
        self.device_edit.setMaximumWidth(180)
        fl.addRow("ADB 기기:", self.device_edit)

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
            btn = QPushButton(f"{label} 좌표 찾기")
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

        self.status_display_label = QLabel("대기 중 (시작 시 다음 픽부터 배팅)")
        self.status_display_label.setStyleSheet("color: #81c784; font-weight: bold;")
        disp_layout.addWidget(self.status_display_label)

        g_display.setLayout(disp_layout)
        layout.addWidget(g_display)

        # 시작 / 중지
        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("시작 (다음 픽부터 배팅)")
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn = QPushButton("중지")
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
            self._set_coord_status("bet_amount", "pynput 설치 필요", "red")
            return
        if self._coord_listener is not None:
            self._set_coord_status(key, "다른 항목 검색 중", "red")
            QTimer.singleShot(2000, lambda: self._set_coord_status(key, ""))
            return
        self._coord_capture_key = key
        self._set_coord_status(key, "검색중", "green")
        self.showMinimized()
        self._coord_listener = pynput_mouse.Listener(on_click=self._on_coord_click)
        self._coord_listener.start()

    def _on_coord_click(self, x, y, button, pressed):
        if not pressed or self._coord_capture_key is None:
            return
        self._pending_coord_click = (self._coord_capture_key, x, y)
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
        self._coords[key] = [x, y]
        save_coords(self._coords)
        self._refresh_coord_labels()
        self._set_coord_status(key, "저장됨", "green")
        QTimer.singleShot(1500, lambda: self._set_coord_status(key, ""))


    def _on_connect_analyzer(self):
        """Analyzer URL/계산기로 1회 조회 후 픽·금액 표시 — 배팅 정보가 자연스럽게 들어오는지 확인용."""
        url = self.analyzer_url_edit.text().strip()
        if not url:
            self._log("Analyzer URL을 입력한 뒤 연결하세요.")
            return
        calc_id = self.calc_combo.currentData()
        self.connect_btn.setEnabled(False)
        self.connect_status_label.setText("연결 중…")
        self.connect_status_label.setStyleSheet("color: #666; font-size: 11px;")

        def do_fetch():
            pick = fetch_current_pick(url, calculator_id=calc_id, timeout=8)
            results = fetch_results(url, timeout=8)
            QTimer.singleShot(0, lambda: self._apply_connect_result(pick, results))

        thread = threading.Thread(target=do_fetch, daemon=True)
        thread.start()

    def _apply_connect_result(self, pick, results):
        self.connect_btn.setEnabled(True)
        with self._lock:
            self._pick_data = pick if isinstance(pick, dict) else {}
            self._results_data = results if isinstance(results, dict) else {}
        self._update_display()
        err = self._pick_data.get("error") or self._results_data.get("error")
        if err:
            self.connect_status_label.setText("연결 실패")
            self.connect_status_label.setStyleSheet("color: #c62828; font-size: 11px;")
            self._log(f"Analyzer 연결 실패: {err}")
        else:
            self.connect_status_label.setText("연결됨")
            self.connect_status_label.setStyleSheet("color: #2e7d32; font-size: 11px;")
            self._log("Analyzer 연결됨 — 픽/금액 확인 후 배팅 시작하세요.")

    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        if hasattr(self.log_text, "append"):
            self.log_text.append(line)
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
        try:
            self._poll_interval_sec = max(1.0, min(10.0, float(2)))
        except ValueError:
            self._poll_interval_sec = 2.0
        self._coords = load_coords()
        if not self._coords.get("bet_amount") or not self._coords.get("red") or not self._coords.get("black"):
            self._log("좌표를 먼저 설정하세요. coord_picker.py로 배팅금액/정정/레드/블랙 좌표를 잡으세요.")
            return
        self._running = True
        self._last_round_when_started = None
        self._last_bet_round = None
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._log("시작 — 다음 픽부터 배팅합니다.")
        self._timer.start(int(self._poll_interval_sec * 1000))

    def _on_stop(self):
        self._running = False
        self._timer.stop()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._log("중지")

    def _poll(self):
        if not self._running:
            return
        url = self._analyzer_url
        calc_id = self._calculator_id
        try:
            pick = fetch_current_pick(url, calculator_id=calc_id, timeout=5)
            results = fetch_results(url, timeout=5)
        except Exception as e:
            with self._lock:
                self._pick_data = {"error": str(e)}
                self._results_data = {}
            self._update_display()
            return
        with self._lock:
            self._pick_data = pick
            self._results_data = results
        self._update_display()
        self._check_pending_results(results.get("prediction_history") or [])

        round_num = pick.get("round")
        pick_color = pick.get("pick_color")
        if round_num is None or pick_color not in ("RED", "BLACK"):
            return
        round_num = int(round_num) if round_num is not None else None
        if round_num is None:
            return
        # 시작 직후: 현재 픽의 회차를 "스킵"용으로 기록
        if self._last_round_when_started is None:
            self._last_round_when_started = round_num
            return
        # 다음 픽(회차가 바뀐 경우)에만 배팅
        if round_num <= self._last_round_when_started:
            return
        if self._last_bet_round is not None and round_num <= self._last_bet_round:
            return
        self._last_bet_round = round_num
        amount = pick.get("suggested_amount")
        self._do_bet(round_num, pick_color, amount)

    def _do_bet(self, round_num, pick_color, amount_from_calc=None):
        """금액 입력 → 정정(선택) → RED 또는 BLACK 탭. 금액은 계산기에서 전달된 값만 사용."""
        if amount_from_calc is None or int(amount_from_calc) <= 0:
            self._log("계산기에서 금액 미전달 — 배팅 생략")
            return
        bet_amount = str(int(amount_from_calc))
        coords = self._coords
        device = self._device_id or None

        bet_xy = coords.get("bet_amount")
        confirm_xy = coords.get("confirm")
        red_xy = coords.get("red")
        black_xy = coords.get("black")
        if not bet_xy or len(bet_xy) < 2:
            self._log("배팅금액 좌표 없음")
            return
        if pick_color == "RED" and (not red_xy or len(red_xy) < 2):
            self._log("레드 좌표 없음")
            return
        if pick_color == "BLACK" and (not black_xy or len(black_xy) < 2):
            self._log("블랙 좌표 없음")
            return

        adb_tap(device, bet_xy[0], bet_xy[1])
        time.sleep(0.2)
        adb_input_text(device, bet_amount)
        time.sleep(0.15)
        if confirm_xy and len(confirm_xy) >= 2:
            adb_tap(device, confirm_xy[0], confirm_xy[1])
            time.sleep(0.15)
        if pick_color == "RED":
            adb_tap(device, red_xy[0], red_xy[1])
        else:
            adb_tap(device, black_xy[0], black_xy[1])
        # 로그: N회차 정/꺽 RED|BLACK 배팅금액 N원
        pred_text = "정" if pick_color == "RED" else "꺽"
        self._log(f"{round_num}회차 {pred_text} {pick_color} {bet_amount}원")
        with self._lock:
            self._pending_bet_rounds[round_num] = {"pick_color": pick_color, "amount": int(bet_amount)}

    def _check_pending_results(self, prediction_history):
        """prediction_history에서 우리가 배팅한 회차 결과가 나왔으면 승/패/조커 로그 후 대기 목록에서 제거."""
        if not prediction_history:
            return
        hist_by_round = {}
        for h in prediction_history:
            if not h or not isinstance(h, dict):
                continue
            r = h.get("round")
            if r is not None:
                hist_by_round[int(r)] = h
        with self._lock:
            pending = list(self._pending_bet_rounds.keys())
        for round_num in pending:
            entry = hist_by_round.get(round_num)
            if not entry:
                continue
            actual = entry.get("actual")
            with self._lock:
                info = self._pending_bet_rounds.get(round_num)
            if not info:
                continue
            pick_color = info.get("pick_color") or ""
            if actual == "joker":
                result_text = "조커"
            else:
                # 승: RED→정, BLACK→꺽 와 actual 일치
                win = (pick_color == "RED" and actual == "정") or (pick_color == "BLACK" and actual == "꺽")
                result_text = "승" if win else "패"
            self._log(f"{round_num}회차 결과: {result_text}")
            with self._lock:
                self._pending_bet_rounds.pop(round_num, None)

    def _update_display(self):
        with self._lock:
            pick = self._pick_data.copy()
            results = self._results_data.copy()
        round_num = pick.get("round")
        pick_color = pick.get("pick_color")
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

        # 정/꺽 + 색깔 카드 (웹과 동일: 정=빨강, 꺽=검정)
        if pick_color == "RED":
            self.pick_card_label.setText("정 · 빨강 (RED)")
            self.pick_card_label.setStyleSheet("font-size: 16px; font-weight: bold; padding: 8px; border-radius: 6px; background: #ffcdd2; color: #b71c1c;")
        elif pick_color == "BLACK":
            self.pick_card_label.setText("꺽 · 검정 (BLACK)")
            self.pick_card_label.setStyleSheet("font-size: 16px; font-weight: bold; padding: 8px; border-radius: 6px; background: #cfd8dc; color: #263238;")
        else:
            self.pick_card_label.setText("보류")
            self.pick_card_label.setStyleSheet("font-size: 16px; font-weight: bold; padding: 8px; border-radius: 6px; background: #eee; color: #666;")

        blended = results.get("blended_win_rate")
        if blended is not None and not isinstance(blended, str):
            try:
                blended_str = f"{float(blended):.1f}%"
            except (TypeError, ValueError):
                blended_str = "-"
        else:
            blended_str = "-"
        self.stats_label.setText(f"실제 경고 합산승률: {blended_str}%")

        if self._running:
            if self._last_round_when_started is None:
                self.status_display_label.setText("다음 픽부터 배팅 예정…")
            else:
                self.status_display_label.setText("배팅 중 (다음 픽마다 자동)")
        else:
            self.status_display_label.setText("대기 중 (시작 시 다음 픽부터 배팅)")


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
