# -*- coding: utf-8 -*-
"""
만수루프로젝트 (배팅 사이트 내장 자동 배팅)
- Analyzer 연결, 예측픽(N회 정/꺽 색), 배팅 기록 맨 위, 순익/경과시간, 로그 저장
"""
import json
import queue
import os
import re
import threading
import time
from datetime import datetime

import requests

# PyQt5 + QWebEngineView (프로그램 안에 사이트 표시)
try:
    from PyQt5.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QLineEdit, QPushButton, QTextEdit, QGroupBox, QFormLayout,
        QMessageBox, QFrame, QTableWidget, QTableWidgetItem, QScrollArea,
        QCheckBox, QHeaderView, QComboBox, QSpinBox,
        QMenuBar, QMenu, QAction, QFileDialog, QDialog, QPlainTextEdit,
    )
    from PyQt5.QtCore import QUrl, Qt, pyqtSlot, QEvent, QTimer, pyqtSignal
    from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEnginePage
    from PyQt5.QtGui import QFont, QColor, QBrush
    HAS_PYQT = True
except ImportError:
    HAS_PYQT = False


def parse_selector_or_xy(value):
    """입력값이 'x,y' 형태면 (x,y) 정수 튜플, 아니면 CSS 셀렉터 문자열로 반환."""
    if not value or not str(value).strip():
        return None
    s = str(value).strip()
    m = re.match(r"^\s*(\d+)\s*,\s*(\d+)\s*$", s)
    if m:
        return ("xy", int(m.group(1)), int(m.group(2)))
    return ("selector", s)


def fetch_current_pick(analyzer_url, timeout=5):
    """GET {analyzer_url}/api/current-pick -> { pick_color, round, ... }"""
    url = analyzer_url.rstrip("/") + "/api/current-pick"
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"pick_color": None, "round": None, "error": str(e)}


def normalize_analyzer_url(analyzer_url):
    """입력한 아날라이저 주소에서 도메인만 추출 (/results 넣어도 됨)."""
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


def _blended_win_rate_from_ph(ph):
    """API에서 blended_win_rate 없을 때 폴백 (분석기 프론트엔드와 동일: 위치 기준 마지막 N개, 조커 제외)."""
    valid_hist = [h for h in (ph or []) if h and isinstance(h, dict)]
    if not valid_hist:
        return None
    v15 = [h for h in valid_hist[-15:] if h.get("actual") != "joker"]
    v30 = [h for h in valid_hist[-30:] if h.get("actual") != "joker"]
    v100 = [h for h in valid_hist[-100:] if h.get("actual") != "joker"]
    def rate(arr):
        hit = sum(1 for h in arr if (h.get("predicted") or "") == (h.get("actual") or ""))
        return 100 * hit / len(arr) if arr else 50
    return 0.5 * rate(v15) + 0.3 * rate(v30) + 0.2 * rate(v100)


def fetch_results(analyzer_url, timeout=5, result_source=None):
    """GET {analyzer_url}/api/results. result_source 있으면 해당 URL에서 결과 조회(베팅 사이트와 동일 소스)."""
    base = normalize_analyzer_url(analyzer_url)
    url = (base or analyzer_url.rstrip("/")) + "/api/results"
    if result_source:
        from urllib.parse import urlencode, quote
        url = url + "?" + urlencode({"result_source": result_source})
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        return {
            "ok": True,
            "server_prediction": data.get("server_prediction") or {},
            "prediction_history": data.get("prediction_history") or [],
            "blended_win_rate": data.get("blended_win_rate"),
            "round_actuals": data.get("round_actuals") or {},
        }
    except Exception as e:
        return {"ok": False, "server_prediction": {}, "prediction_history": [], "blended_win_rate": None, "round_actuals": {}, "error": str(e)}


# 클릭용 JavaScript (값은 runJavaScript 호출 시 문자열에 삽입)
def js_click_selector(sel):
    s = json.dumps(sel)
    return "(function(sel){ var el = document.querySelector(sel); if(el){ el.click(); return true; } return false; })(" + s + ");"

def js_click_xy(x, y, intended_color=None):
    """(x,y) 뷰포트 좌표로 클릭. intended_color가 RED/BLACK이면 #btn-red/#btn-black 우선 사용(좌표가 LABEL 등에 걸려도 올바른 버튼 클릭)."""
    color_arg = ("'" + intended_color.upper() + "'" if intended_color and str(intended_color).upper() in ("RED", "BLACK") else "null")
    return (
        "(function(x,y,color){"
        "var el=document.elementFromPoint(x,y);"
        "if(!el)return false;"
        "var target=null;"
        "var t=el;while(t&&t!==document.body){if(t.tagName==='BUTTON'||(t.tagName==='INPUT'&&(t.type==='button'||t.type==='submit'))){target=t;break;}t=t.parentElement;}"
        "var btns=document.querySelectorAll('button,input[type=button],input[type=submit]');"
        "if(!target){for(var i=0;i<btns.length;i++){var r=btns[i].getBoundingClientRect();if(x>=r.left&&x<=r.right&&y>=r.top&&y<=r.bottom){target=btns[i];break;}}}"
        "if(!target&&color){var byId=document.getElementById('btn-'+color.toLowerCase());if(byId)target=byId;}"
        "if(!target){var best=null,bestD=1/0;for(var i=0;i<btns.length;i++){var r=btns[i].getBoundingClientRect();var cx=(r.left+r.right)/2,cy=(r.top+r.bottom)/2;var d=(x-cx)*(x-cx)+(y-cy)*(y-cy);if(d<bestD){bestD=d;best=btns[i];}}if(best)target=best;}"
        "if(!target)target=el;"
        "var opts={bubbles:true,cancelable:true,view:window,clientX:x,clientY:y,buttons:1};"
        "target.dispatchEvent(new MouseEvent('mousedown',opts));"
        "target.dispatchEvent(new MouseEvent('mouseup',opts));"
        "target.dispatchEvent(new MouseEvent('click',opts));"
        "return true;"
        "})(" + str(x) + "," + str(y) + "," + color_arg + ");"
    )

def _js_set_value_inner():
    """React/Vue 제어 컴포넌트 대응: 클리어→네이티브 setter→input/change→paste 폴백."""
    return (
        "function setInputValue(el, v){"
        "if(!el)return false;"
        "var s=String(v);"
        "el.scrollIntoView&&el.scrollIntoView({block:'center'});"
        "el.focus();"
        "el.click&&el.click();"
        "if(el.isContentEditable){"
        "el.textContent=s;el.innerHTML=s;"
        "el.dispatchEvent(new Event('input',{bubbles:true}));"
        "return true;"
        "}"
        "el.select&&el.select();"
        "try{"
        "var desc=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value');"
        "if(desc&&desc.set){desc.set.call(el,'');desc.set.call(el,s);}"
        "else{el.value='';el.value=s;}"
        "}catch(e){el.value='';el.value=s;}"
        "el.dispatchEvent(new Event('input',{bubbles:true}));"
        "el.dispatchEvent(new Event('change',{bubbles:true}));"
        "el.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true,key:'0'}));"
        "return true;"
        "}"
    )


def js_set_value_at_xy(x, y, value_str):
    """(x,y) 위치의 input에 값 넣기. label/contenteditable 포함. React/Vue 대응."""
    v = str(value_str) if value_str is not None else ""
    return (
        "(function(x,y,v){"
        + _js_set_value_inner() +
        "var el=document.elementFromPoint(x,y);"
        "if(!el)return;"
        "if(el.tagName==='LABEL'&&el.htmlFor)el=document.getElementById(el.htmlFor)||el;"
        "else if(el.tagName!=='INPUT'&&el.tagName!=='TEXTAREA'&&!el.isContentEditable){"
        "var inp=el.querySelector('input,textarea,[contenteditable=true]');if(inp)el=inp;"
        "}"
        "if(el&&(el.tagName==='INPUT'||el.tagName==='TEXTAREA'||el.isContentEditable)){setInputValue(el,v);}"
        "})(%s,%s,%s);"
    ) % (x, y, json.dumps(v))

def js_set_value_by_selector(sel, value_str):
    """셀렉터로 input 찾아서 값 넣기. React/Vue 대응."""
    v = str(value_str) if value_str is not None else ""
    return (
        "(function(s,v){"
        + _js_set_value_inner() +
        "var el=document.querySelector(s);"
        "if(el){setInputValue(el,v);}"
        "})(%s,%s);"
    ) % (json.dumps(sel), json.dumps(v))

def js_set_value_then_click(amount_x, amount_y, amount_str, pick_x, pick_y):
    """금액 칸(ax,ay)에 값 넣고 280ms 후 (px,py) 좌표로 클릭. 클릭은 js_click_xy와 동일하게 버튼/input 찾아서 클릭."""
    # 클릭 로직: elementFromPoint 후 부모 올라가며 BUTTON/INPUT 찾기 (테스트 페이지·실제 페이지 공통)
    return (
        "(function(ax,ay,amt,px,py){"
        "var el=document.elementFromPoint(ax,ay);"
        "if(el){if(el.tagName==='LABEL'&&el.htmlFor)el=document.getElementById(el.htmlFor)||el;"
        "else if(el.tagName!=='INPUT'&&el.tagName!=='TEXTAREA'){var i=el.querySelector('input,textarea');if(i)el=i;}"
        "if(el&&(el.tagName==='INPUT'||el.tagName==='TEXTAREA')){el.focus();el.value=amt;el.dispatchEvent(new Event('input',{bubbles:true}));}}"
        "setTimeout(function(){"
        "var el=document.elementFromPoint(px,py);if(!el)return;"
        "var target=null;var t=el;while(t&&t!==document.body){if(t.tagName==='BUTTON'||(t.tagName==='INPUT'&&(t.type==='button'||t.type==='submit'))){target=t;break;}t=t.parentElement;}"
        "var list=document.querySelectorAll('button,input[type=button],input[type=submit]');"
        "if(!target){for(var i=0;i<list.length;i++){var r=list[i].getBoundingClientRect();if(px>=r.left&&px<=r.right&&py>=r.top&&py<=r.bottom){target=list[i];break;}}}"
        "if(!target){var best=null,bestD=1/0;for(var i=0;i<list.length;i++){var r=list[i].getBoundingClientRect();var cx=(r.left+r.right)/2,cy=(r.top+r.bottom)/2;var d=(px-cx)*(px-cx)+(py-cy)*(py-cy);if(d<bestD){bestD=d;best=list[i];}}if(best)target=best;}"
        "if(!target)target=el;"
        "var opts={bubbles:true,cancelable:true,view:window,clientX:px,clientY:py,buttons:1};"
        "target.dispatchEvent(new MouseEvent('mousedown',opts));target.dispatchEvent(new MouseEvent('mouseup',opts));target.dispatchEvent(new MouseEvent('click',opts));"
        "},280);"
        "})(%s,%s,%s,%s,%s);"
    ) % (amount_x, amount_y, json.dumps(str(amount_str)), pick_x, pick_y)

# 표마틴 단계별 금액 (원 단위 기준), 표마틴 반 = 절반
TABLE_MARTIN_PYO = [10000, 15000, 25000, 40000, 70000, 120000, 200000, 400000, 120000]
DEFAULT_ANALYZER_URL = "https://web-production-fa2dd.up.railway.app/results"


def _fmt_amount(val):
    """금액에 천단위 쉼표 적용."""
    if val is None or val == "":
        return ""
    try:
        return "{:,}".format(int(val))
    except (ValueError, TypeError):
        return str(val)


def _fmt_round(val):
    """회차 총 뒤 4자리만 표시."""
    if val is None or val == "":
        return ""
    s = str(val)
    return s[-4:] if len(s) >= 4 else s


def _cell_item(text, bg_color=None, align_center=True):
    """QTableWidgetItem 생성: 가운데정렬, 선택적 배경색(흰색 폰트)."""
    item = QTableWidgetItem(str(text))
    if align_center:
        item.setTextAlignment(Qt.AlignCenter)
    if bg_color:
        item.setBackground(QBrush(QColor(bg_color)))
        item.setForeground(QColor("#ffffff"))
    return item


# "보유 금액 없음" 등 사이트 알림 문구 패턴 (포함 여부로 판단)
INSUFFICIENT_FUNDS_KEYWORDS = ("보유", "금액", "없습니다")


def _is_insufficient_funds_alert(message):
    """알림 메시지가 '보유 금액 없음' 유형인지 판별."""
    if not message or not isinstance(message, str):
        return False
    msg = message.strip()
    return all(kw in msg for kw in INSUFFICIENT_FUNDS_KEYWORDS)


class MacroWebEnginePage(QWebEnginePage):
    """JavaScript alert/confirm 가로채기: 보유 금액 없음 시 배팅 중지 신호."""
    insufficient_funds = pyqtSignal()

    def javaScriptAlert(self, securityOrigin, msg):
        if _is_insufficient_funds_alert(msg):
            self.insufficient_funds.emit()
        # 기본 다이얼로그 띄우지 않음 → 알림 수락(닫기) 처리

    def javaScriptConfirm(self, securityOrigin, msg):
        if _is_insufficient_funds_alert(msg):
            self.insufficient_funds.emit()
        # Confirm은 True 반환 = OK 처리
        return True

    def javaScriptPrompt(self, securityOrigin, msg, defaultValue):
        if _is_insufficient_funds_alert(msg):
            self.insufficient_funds.emit()
        return True, defaultValue


class MacroWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("만수루프로젝트")
        self.setMinimumSize(900, 600)
        self.resize(1100, 700)

        self.running = False
        self.poll_thread = None
        self.last_clicked_round = None
        self.last_pick = None
        self.capture_mode = None
        self.update_queue = queue.Queue()
        self.bet_log = []  # [{ round, predicted, pick_color, amount, actual, result, cumulative }]
        self._last_server_prediction = {}
        self._connected = False
        self._start_time = None  # 경과시간 계산용

        # 중앙 위젯
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)

        # 메뉴: 로그 보기 / 로그 저장
        menubar = self.menuBar()
        log_menu = menubar.addMenu("로그")
        self._action_log_view = QAction("로그 보기", self)
        self._action_log_view.triggered.connect(self._on_log_view)
        log_menu.addAction(self._action_log_view)
        self._action_log_save = QAction("로그 저장", self)
        self._action_log_save.triggered.connect(self._on_log_save)
        log_menu.addAction(self._action_log_save)

        # 왼쪽: 스크롤 가능 설정 패널
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumWidth(400)
        left = QFrame()
        left.setFrameStyle(QFrame.StyledPanel)
        left_layout = QVBoxLayout(left)

        # ===== 맨 위: 배팅 기록 (순익, 시드/현재금액, 경과시간, 표)
        g_bet = QGroupBox("배팅 기록 (실시간)")
        bet_top = QHBoxLayout()
        self.net_profit_label = QLabel("순익: 0")
        self.net_profit_label.setStyleSheet("font-weight: bold; font-size: 12px;")
        bet_top.addWidget(self.net_profit_label)
        self.seed_current_label = QLabel("시드: 0 | 현재: 0")
        self.seed_current_label.setStyleSheet("color: #555; font-size: 11px;")
        bet_top.addWidget(self.seed_current_label)
        bet_top.addStretch(1)
        self.elapsed_label = QLabel("경과: 00:00:00")
        self.elapsed_label.setStyleSheet("color: #666; font-size: 11px;")
        bet_top.addWidget(self.elapsed_label)
        left_layout.addWidget(g_bet)
        bet_inner = QVBoxLayout(g_bet)
        bet_inner.addLayout(bet_top)
        self.bet_table = QTableWidget()
        self.bet_table.setColumnCount(6)
        self.bet_table.setHorizontalHeaderLabels(["회차", "PICK", "결과", "배팅금액", "승/패", "누적"])
        self.bet_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.bet_table.setMaximumHeight(140)
        bet_inner.addWidget(self.bet_table)

        # Analyzer 연결 상태 + 예측픽 (몇회 정/꺽 + 색깔)
        g0 = QGroupBox("Analyzer 연결 / 현재 예측픽")
        fl0 = QFormLayout()
        self.connection_label = QLabel("미연결")
        self.connection_label.setStyleSheet("font-weight: bold; color: #888;")
        fl0.addRow("연결 상태:", self.connection_label)
        # 예측픽: [카드색 아이콘] N회 정/꺽 RED/BLACK
        pick_row = QWidget()
        pick_row_layout = QHBoxLayout(pick_row)
        pick_row_layout.setContentsMargins(0, 0, 0, 0)
        self.pick_card_icon = QFrame()
        self.pick_card_icon.setFixedSize(18, 18)
        self.pick_card_icon.setStyleSheet("background: #888; border-radius: 3px;")
        pick_row_layout.addWidget(self.pick_card_icon)
        self.pick_display_label = QLabel("픽 없음")
        self.pick_display_label.setStyleSheet("font-size: 13px; font-weight: bold;")
        pick_row_layout.addWidget(self.pick_display_label)
        pick_row_layout.addStretch(1)
        fl0.addRow("예측픽:", pick_row)
        self.win_rate_label = QLabel("-")
        self.win_rate_label.setStyleSheet("font-size: 12px; color: #555;")
        fl0.addRow("합산승률:", self.win_rate_label)
        g0.setLayout(fl0)
        left_layout.addWidget(g0)

        # Analyzer URL + 연결 버튼
        g1 = QGroupBox("Analyzer")
        fl1 = QFormLayout()
        self.analyzer_url_edit = QLineEdit()
        self.analyzer_url_edit.setPlaceholderText("비우면 기본 Railway 사용")
        self.analyzer_url_edit.setText(DEFAULT_ANALYZER_URL)
        fl1.addRow("Analyzer URL:", self.analyzer_url_edit)
        self.connect_btn = QPushButton("연결")
        self.connect_btn.clicked.connect(self._on_connect)
        fl1.addRow("", self.connect_btn)
        conn_hint = QLabel("※ '연결' 누르면 아날라이저에 연결합니다. 언제든 연결/해제 가능.")
        conn_hint.setStyleSheet("color: #888; font-size: 11px;")
        conn_hint.setWordWrap(True)
        fl1.addRow("", conn_hint)
        g1.setLayout(fl1)
        left_layout.addWidget(g1)

        # 배팅 사이트 (내장 브라우저에 로드)
        g2 = QGroupBox("배팅 사이트")
        fl2 = QFormLayout()
        self.betting_url_edit = QLineEdit()
        self.betting_url_edit.setPlaceholderText("https://nhs900.com 또는 테스트 페이지")
        self.betting_url_edit.setText("https://nhs900.com")
        fl2.addRow("URL:", self.betting_url_edit)
        self.result_source_edit = QLineEdit()
        self.result_source_edit.setPlaceholderText("비우면 Analyzer 기본 사용 (같은 게임)")
        self.result_source_edit.setText("")
        fl2.addRow("결과 소스 URL:", self.result_source_edit)
        self.go_btn = QPushButton("사이트 열기(이동)")
        self.go_btn.clicked.connect(self._on_go_site)
        fl2.addRow(self.go_btn)
        self.test_page_btn = QPushButton("테스트 페이지 열기")
        self.test_page_btn.clicked.connect(self._on_open_test_page)
        fl2.addRow(self.test_page_btn)
        g2.setLayout(fl2)
        left_layout.addWidget(g2)

        # 금액 입력 칸 + RED / BLACK 버튼 (클릭해서 좌표 잡기)
        g3 = QGroupBox("금액 칸 / RED·BLACK 버튼")
        fl3 = QFormLayout()
        self.amount_edit = QLineEdit()
        self.amount_edit.setPlaceholderText("x,y 좌표 또는 #amount (테스트 페이지)")
        self.amount_edit.setText("")
        fl3.addRow("금액 입력 칸:", self.amount_edit)
        self.amount_capture_btn = QPushButton("금액 칸 클릭해서 좌표 잡기")
        self.amount_capture_btn.clicked.connect(lambda: self._start_capture("AMOUNT"))
        fl3.addRow(self.amount_capture_btn)
        coord_hint = QLabel("※ 좌표는 오른쪽 웹뷰 기준이라 창을 움직여도 그대로 유효합니다.")
        coord_hint.setStyleSheet("color: #666; font-size: 10px;")
        coord_hint.setWordWrap(True)
        fl3.addRow("", coord_hint)
        self.red_edit = QLineEdit()
        self.red_edit.setPlaceholderText("좌표 x,y 또는 #btn-red (테스트 페이지)")
        self.red_edit.setText("")
        self.black_edit = QLineEdit()
        self.black_edit.setPlaceholderText("좌표 x,y 또는 #btn-black (테스트 페이지)")
        self.black_edit.setText("")
        fl3.addRow("RED:", self.red_edit)
        self.red_capture_btn = QPushButton("RED 클릭해서 좌표 잡기")
        self.red_capture_btn.clicked.connect(lambda: self._start_capture("RED"))
        fl3.addRow(self.red_capture_btn)
        fl3.addRow("BLACK:", self.black_edit)
        self.black_capture_btn = QPushButton("BLACK 클릭해서 좌표 잡기")
        self.black_capture_btn.clicked.connect(lambda: self._start_capture("BLACK"))
        fl3.addRow(self.black_capture_btn)
        self.poll_interval_edit = QLineEdit()
        self.poll_interval_edit.setText("0.3")
        self.poll_interval_edit.setMaximumWidth(60)
        fl3.addRow("픽 확인 주기(초):", self.poll_interval_edit)
        poll_hint = QLabel("= Analyzer 픽/결과 조회 간격. 0.2~0.3초 권장(예측 빨리 반영)")
        poll_hint.setStyleSheet("color: #888; font-size: 11px;")
        poll_hint.setWordWrap(True)
        fl3.addRow(poll_hint)
        seq_hint = QLabel("※ 시퀀스: 금액 칸 좌표 있으면 → 금액 입력 → 약 0.3초 후 예측픽(RED/BLACK) 클릭. 실제 브라우저 사용·간격으로 자연스럽게 동작.")
        seq_hint.setStyleSheet("color: #666; font-size: 10px;")
        seq_hint.setWordWrap(True)
        fl3.addRow("", seq_hint)
        g3.setLayout(fl3)
        left_layout.addWidget(g3)

        # 계산기 설정 (시드머니, 배팅금액, 마틴 형식, 승률반픽, 시간제한 등)
        g_calc = QGroupBox("배팅 설정 (계산기)")
        fl_calc = QFormLayout()
        self.seed_money_edit = QLineEdit()
        self.seed_money_edit.setPlaceholderText("현재 보유 금액")
        self.seed_money_edit.setText("")
        self.seed_money_edit.setMaximumWidth(120)
        fl_calc.addRow("시드머니(현재금액):", self.seed_money_edit)
        seed_hint = QLabel("※ 입력하면 순익 옆에 시드/현재금액 표시, 배팅금액은 현재금액을 넘지 않음")
        seed_hint.setStyleSheet("color: #888; font-size: 10px;")
        seed_hint.setWordWrap(True)
        fl_calc.addRow("", seed_hint)
        self.base_bet_edit = QLineEdit()
        self.base_bet_edit.setText("1000")
        self.base_bet_edit.setMaximumWidth(100)
        fl_calc.addRow("초기 배팅 금액:", self.base_bet_edit)
        self.odds_edit = QLineEdit()
        self.odds_edit.setText("1.97")
        self.odds_edit.setPlaceholderText("배당 (순익 계산용)")
        self.odds_edit.setMaximumWidth(80)
        fl_calc.addRow("배당:", self.odds_edit)
        odds_hint = QLabel("※ 승리 시 수익 = 배팅금액×(배당-1), 패배 시 = -배팅금액")
        odds_hint.setStyleSheet("color: #888; font-size: 10px;")
        fl_calc.addRow("", odds_hint)
        self.martingale_check = QCheckBox("마틴 적용")
        self.martingale_check.setChecked(False)
        fl_calc.addRow(self.martingale_check)
        self.martingale_type_combo = QComboBox()
        self.martingale_type_combo.addItem("2배 (패배 시 2배)", "double")
        self.martingale_type_combo.addItem("표마틴", "pyo")
        self.martingale_type_combo.addItem("표마틴 반", "pyo_half")
        fl_calc.addRow("마틴 방식:", self.martingale_type_combo)
        self.duration_edit = QLineEdit()
        self.duration_edit.setPlaceholderText("0 = 무제한")
        self.duration_edit.setText("0")
        self.duration_edit.setMaximumWidth(60)
        fl_calc.addRow("목표시간 (분):", self.duration_edit)
        self.reverse_check = QCheckBox("반픽 (반대로 배팅)")
        self.reverse_check.setChecked(False)
        fl_calc.addRow(self.reverse_check)
        self.win_rate_reverse_check = QCheckBox("승률반픽")
        self.win_rate_reverse_check.setChecked(False)
        fl_calc.addRow(self.win_rate_reverse_check)
        self.win_rate_threshold_spin = QSpinBox()
        self.win_rate_threshold_spin.setRange(0, 100)
        self.win_rate_threshold_spin.setValue(50)
        self.win_rate_threshold_spin.setMaximumWidth(60)
        wr_widget = QWidget()
        wr_row = QHBoxLayout(wr_widget)
        wr_row.setContentsMargins(0, 0, 0, 0)
        wr_row.addWidget(self.win_rate_threshold_spin)
        wr_row.addWidget(QLabel("% 이하일 때 승률반픽"))
        wr_row.addStretch(1)
        fl_calc.addRow("합산승률≤", wr_widget)
        self.target_enabled_check = QCheckBox("목표금액 사용 (도달 시 배팅 중지)")
        self.target_enabled_check.setChecked(False)
        fl_calc.addRow(self.target_enabled_check)
        self.target_edit = QLineEdit()
        self.target_edit.setText("0")
        self.target_edit.setMaximumWidth(100)
        fl_calc.addRow("목표 금액:", self.target_edit)
        g_calc.setLayout(fl_calc)
        left_layout.addWidget(g_calc)
        self.seed_money_edit.textChanged.connect(self._update_seed_current_label)

        # 세팅 잠금용 위젯 목록 (시작 시 비활성화, 정지 시 활성화)
        self._settings_widgets = [
            self.analyzer_url_edit, self.connect_btn,
            self.betting_url_edit, self.result_source_edit, self.go_btn, self.test_page_btn,
            self.amount_edit, self.amount_capture_btn,
            self.red_edit, self.red_capture_btn, self.black_edit, self.black_capture_btn,
            self.poll_interval_edit,
            self.seed_money_edit, self.base_bet_edit, self.odds_edit,
            self.martingale_check, self.martingale_type_combo,
            self.duration_edit, self.reverse_check, self.win_rate_reverse_check,
            self.win_rate_threshold_spin, self.target_enabled_check, self.target_edit,
        ]

        # 시작 / 중지
        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("시작 (폴링 + 자동 클릭)")
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn = QPushButton("중지")
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.setEnabled(False)
        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.stop_btn)
        left_layout.addLayout(btn_layout)

        # 상태
        self.status_label = QLabel("대기 중")
        self.status_label.setStyleSheet("color: #81c784; font-weight: bold;")
        left_layout.addWidget(self.status_label)

        # 로그
        left_layout.addWidget(QLabel("로그:"))
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 9))
        self.log_text.setMaximumHeight(120)
        left_layout.addWidget(self.log_text)

        scroll.setWidget(left)
        layout.addWidget(scroll)

        # 오른쪽: 내장 브라우저 (배팅 사이트) — 창 크기에 맞게 표시
        self.web = QWebEngineView()
        self.web.setUrl(QUrl("about:blank"))
        self.web.setMinimumSize(200, 200)
        self.web.installEventFilter(self)
        self.web.loadFinished.connect(self._on_page_loaded_mute)
        # JavaScript 알림(보유 금액 없음 등) 가로채기 → 배팅 중지
        _profile = self.web.page().profile() if self.web.page() else None
        self._web_page = MacroWebEnginePage(_profile, self.web)
        self._web_page.insufficient_funds.connect(self._on_insufficient_funds)
        self.web.setPage(self._web_page)
        layout.addWidget(self.web, stretch=1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._process_queue)
        self._timer.start(400)

        # 드롭다운 휠 무시: 앱 전체 휠 이벤트에서 콤보일 때 흡수
        if HAS_PYQT:
            app = QApplication.instance()
            if app:
                app.installEventFilter(self)

    def _set_settings_locked(self, locked):
        """시작 시 True(잠금), 정지 시 False(해제). 세팅 위젯 활성/비활성."""
        for w in self._settings_widgets:
            w.setEnabled(not locked)

    def _process_queue(self):
        """스레드에서 넣은 연결/픽/히스토리/테이블 갱신 처리."""
        try:
            while True:
                msg = self.update_queue.get_nowait()
                if msg[0] == "connection":
                    ok = msg[1]
                    self._connected = ok
                    if ok:
                        self.connection_label.setText("연결됨")
                        self.connection_label.setStyleSheet("font-weight: bold; color: #81c784;")
                    else:
                        self.connection_label.setText("미연결")
                        self.connection_label.setStyleSheet("font-weight: bold; color: #888;")
                elif msg[0] == "pick":
                    sp = msg[1]
                    self._last_server_prediction = sp
                    r = sp.get("round") or 0
                    v = sp.get("value") or ""
                    c = sp.get("color") or ""
                    if c in ("빨강", "RED"):
                        color_txt = "RED"
                    elif c in ("검정", "BLACK"):
                        color_txt = "BLACK"
                    else:
                        color_txt = str(c) if c else "-"
                    if r and (v or color_txt):
                        self.pick_display_label.setText("%s회 %s %s" % (r, v or "-", color_txt))
                        if color_txt == "RED":
                            self.pick_display_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #e53935;")
                            self.pick_card_icon.setStyleSheet("background: #e53935; border-radius: 3px;")
                        elif color_txt == "BLACK":
                            self.pick_display_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #212121;")
                            self.pick_card_icon.setStyleSheet("background: #212121; border-radius: 3px;")
                        else:
                            self.pick_display_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #888;")
                            self.pick_card_icon.setStyleSheet("background: #888; border-radius: 3px;")
                    else:
                        if self._connected:
                            self.pick_display_label.setText("픽 수신 중")
                        else:
                            self.pick_display_label.setText("픽 없음")
                        self.pick_display_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #888;")
                        self.pick_card_icon.setStyleSheet("background: #888; border-radius: 3px;")
                elif msg[0] == "history":
                    ph = msg[1]
                    blended = msg[2] if len(msg) > 2 else None
                    round_actuals = msg[3] if len(msg) > 3 else {}
                    for b in self.bet_log:
                        if b.get("actual") is not None:
                            continue
                        rid = str(int(b.get("round") or 0))
                        our_pick = (b.get("pick_color") or "").strip().upper()
                        matched = False
                        for h in (ph or []):
                            if not isinstance(h, dict) or int(h.get("round") or 0) != int(b.get("round") or 0) or h.get("actual") in (None, "joker"):
                                continue
                            raw = (h.get("actual") or "").strip()
                            b["actual"] = raw
                            ac = (h.get("actualColor") or h.get("actual_color") or "").strip().upper()
                            actual_color = "RED" if ac in ("RED", "빨강") else "BLACK" if ac in ("BLACK", "검정") else None
                            if actual_color:
                                b["actual_color"] = actual_color
                                b["result"] = our_pick == actual_color
                                matched = True
                                break
                            h_pick = (h.get("pickColor") or h.get("pick_color") or "").strip().upper()
                            h_pick = "RED" if h_pick in ("RED", "빨강") else "BLACK" if h_pick in ("BLACK", "검정") else None
                            if raw.upper() in ("RED", "빨강", "BLACK", "검정"):
                                actual_color = "RED" if raw.upper() in ("RED", "빨강") else "BLACK"
                            elif raw in ("정", "꺽") and h_pick:
                                actual_color = h_pick if raw == "정" else ("BLACK" if h_pick == "RED" else "RED")
                            else:
                                actual_color = None
                            b["actual_color"] = actual_color
                            b["result"] = our_pick == actual_color if actual_color else ((b.get("predicted") or "").strip() == raw)
                            matched = True
                            break
                        if matched:
                            continue
                        ra = (round_actuals or {}).get(rid) if isinstance(round_actuals, dict) else None
                        if ra:
                            raw = ra.get("actual") or ""
                            ac = (ra.get("color") or "").strip().upper()
                            actual_color = "RED" if ac in ("RED", "빨강") else "BLACK" if ac in ("BLACK", "검정") else None
                            b["actual"] = raw
                            b["actual_color"] = actual_color
                            if raw == "joker":
                                b["result"] = None
                            elif actual_color:
                                b["result"] = our_pick == actual_color
                            else:
                                b["result"] = None
                    try:
                        odds = max(1.0, float(self.odds_edit.text().strip().replace(",", ".") or 1.97))
                    except (ValueError, TypeError):
                        odds = 1.97
                    cum = 0
                    for b in self.bet_log:
                        if b.get("result") is not None:
                            amt = b.get("amount") or 0
                            cum += amt * (odds - 1) if b["result"] else -amt
                        b["cumulative"] = cum
                    self._refresh_bet_table()
                    # 합산승률 표시 (API에서 받은 동일 값, 50% 이하 빨강)
                    if blended is not None:
                        self.win_rate_label.setText("%.1f%%" % blended)
                        if blended <= 50:
                            self.win_rate_label.setStyleSheet("font-size: 12px; font-weight: bold; color: #c62828;")
                        else:
                            self.win_rate_label.setStyleSheet("font-size: 12px; color: #2e7d32;")
                    else:
                        self.win_rate_label.setText("-")
                        self.win_rate_label.setStyleSheet("font-size: 12px; color: #555;")
                elif msg[0] == "bet":
                    self.bet_log.append(msg[1])
                    self._refresh_bet_table()
                elif msg[0] == "log":
                    self.log(msg[1])
                elif msg[0] == "auto_stop":
                    if getattr(self, "_elapsed_timer", None):
                        self._elapsed_timer.stop()
                    self.start_btn.setEnabled(True)
                    self.stop_btn.setEnabled(False)
                    self._set_settings_locked(False)
                    self.set_status(msg[1] + "으로 중지")
                elif msg[0] == "conn_done":
                    self.connect_btn.setEnabled(True)
                    self.set_status("대기 중")
                elif msg[0] == "do_click":
                    self._do_bet_sequence(msg[1], msg[2])
                # 목표 금액 달성 시 자동 중지 (목표금액 사용 체크 시에만)
                if msg[0] in ("history", "bet"):
                    if self.target_enabled_check.isChecked():
                        try:
                            t = int(self.target_edit.text().strip() or 0)
                        except Exception:
                            t = 0
                        if t > 0 and self.bet_log:
                            last_cum = self.bet_log[-1].get("cumulative") or 0
                            if last_cum >= t:
                                self.running = False
                                if getattr(self, "_elapsed_timer", None):
                                    self._elapsed_timer.stop()
                                self.start_btn.setEnabled(True)
                                self.stop_btn.setEnabled(False)
                                self._set_settings_locked(False)
                                self.set_status("목표 달성으로 중지")
                                self.log("[목표 달성] 누적 %s >= 목표 %s → 자동 중지" % (last_cum, t))
                                self.update_queue.put(("auto_save_log", None))
                elif msg[0] == "auto_save_log":
                    self._do_save_log(auto=True)
        except queue.Empty:
            pass

    def _refresh_bet_table(self):
        def _actual_to_color(actual_val, pick_color):
            """결과(실제 나온 색)를 셀 배경색으로 변환. 정/꺽은 pick_color로 유추."""
            if not actual_val:
                return None
            v = str(actual_val).strip().upper()
            if v in ("RED", "빨강"):
                return "#e53935"
            if v in ("BLACK", "검정"):
                return "#212121"
            if v == "정" and pick_color:
                # 정 = 예측 맞음 → 실제 = pick_color
                return "#e53935" if pick_color == "RED" else "#212121" if pick_color == "BLACK" else None
            if v == "꺽" and pick_color:
                # 꺽 = 예측 틀림 → 실제 = pick_color 반대
                return "#212121" if pick_color == "RED" else "#e53935" if pick_color == "BLACK" else None
            return None

        self.bet_table.setRowCount(len(self.bet_log))
        for i, b in enumerate(reversed(self.bet_log)):
            # 회차: 뒤 4자리만
            rnd = b.get("round", "")
            self.bet_table.setItem(i, 0, _cell_item(_fmt_round(rnd)))

            # PICK: 배팅한 색상, 셀배경+흰색폰트
            pick_color = (b.get("pick_color") or "").strip().upper()
            pick_bg = "#e53935" if pick_color == "RED" else "#212121" if pick_color == "BLACK" else None
            self.bet_table.setItem(i, 1, _cell_item(pick_color or "-", pick_bg))

            # 결과: 실제로 나온 카드 색으로 셀배경. actual_color 있으면 그대로 사용(반픽 시에도 정확)
            actual_val = b.get("actual", "-") or "-"
            actual_color = b.get("actual_color")
            actual_bg = "#e53935" if actual_color == "RED" else "#212121" if actual_color == "BLACK" else _actual_to_color(actual_val, pick_color)
            self.bet_table.setItem(i, 2, _cell_item(actual_val, actual_bg))

            # 배팅금액, 누적: 천단위 쉼표
            self.bet_table.setItem(i, 3, _cell_item(_fmt_amount(b.get("amount", ""))))

            # 승/패
            res = b.get("result")
            if res is True:
                item4 = _cell_item("승")
                item4.setForeground(QColor(129, 199, 132))
                self.bet_table.setItem(i, 4, item4)
            elif res is False:
                item4 = _cell_item("패")
                item4.setForeground(QColor(229, 115, 115))
                self.bet_table.setItem(i, 4, item4)
            else:
                self.bet_table.setItem(i, 4, _cell_item("-"))

            self.bet_table.setItem(i, 5, _cell_item(_fmt_amount(b.get("cumulative", ""))))
        # 순익 표시 (천단위 쉼표)
        last_cum = self.bet_log[-1].get("cumulative", 0) if self.bet_log else 0
        self.net_profit_label.setText("순익: %s" % _fmt_amount(last_cum))
        if last_cum > 0:
            self.net_profit_label.setStyleSheet("font-weight: bold; font-size: 12px; color: #2e7d32;")
        elif last_cum < 0:
            self.net_profit_label.setStyleSheet("font-weight: bold; font-size: 12px; color: #c62828;")
        else:
            self.net_profit_label.setStyleSheet("font-weight: bold; font-size: 12px;")
        # 시드머니(현재금액) 표시: 현재 = 시드 + 순익
        try:
            seed = max(0, int(self.seed_money_edit.text().strip() or 0))
        except (ValueError, TypeError):
            seed = 0
        current = seed + last_cum
        self.seed_current_label.setText("시드: %s | 현재: %s" % (_fmt_amount(seed), _fmt_amount(current)))

    def _update_seed_current_label(self):
        """시드머니 입력 변경 시 순익 옆 시드/현재금액 라벨 갱신."""
        try:
            seed = max(0, int(self.seed_money_edit.text().strip() or 0))
        except (ValueError, TypeError):
            seed = 0
        last_cum = self.bet_log[-1].get("cumulative", 0) if self.bet_log else 0
        current = seed + last_cum
        self.seed_current_label.setText("시드: %s | 현재: %s" % (_fmt_amount(seed), _fmt_amount(current)))

    def _update_elapsed(self):
        """경과시간 라벨 갱신 (시:분:초)."""
        if not self.running or self._start_time is None:
            return
        secs = int(time.time() - self._start_time)
        h, rest = divmod(secs, 3600)
        m, s = divmod(rest, 60)
        self.elapsed_label.setText("경과: %02d:%02d:%02d" % (h, m, s))

    def _on_log_view(self):
        """로그 보기: 상세 로그를 다이얼로그로 표시."""
        d = QDialog(self)
        d.setWindowTitle("로그 보기")
        d.setMinimumSize(500, 400)
        layout = QVBoxLayout(d)
        te = QPlainTextEdit(d)
        te.setPlainText(self.log_text.toPlainText())
        te.setReadOnly(True)
        te.setFont(QFont("Consolas", 9))
        layout.addWidget(te)
        close_btn = QPushButton("닫기")
        close_btn.clicked.connect(d.accept)
        layout.addWidget(close_btn)
        d.exec_()

    def _on_log_save(self):
        """로그 저장: 파일 선택 후 저장."""
        self._do_save_log(auto=False)

    def _do_save_log(self, auto=False):
        """로그를 파일로 저장. auto=True면 기본 경로(날짜시간).txt."""
        text = self.log_text.toPlainText()
        if not text.strip():
            if not auto:
                QMessageBox.information(self, "저장", "저장할 로그가 없습니다.")
            return
        if auto:
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
            try:
                os.makedirs(log_dir, exist_ok=True)
            except Exception:
                log_dir = os.path.dirname(os.path.abspath(__file__))
            fname = os.path.join(log_dir, "매크로_%s.txt" % datetime.now().strftime("%Y%m%d_%H%M%S"))
            try:
                with open(fname, "w", encoding="utf-8") as f:
                    f.write(text)
                self.log("[자동 저장] %s" % fname)
            except Exception as e:
                self.log("[자동 저장 실패] %s" % e)
        else:
            fname, _ = QFileDialog.getSaveFileName(self, "로그 저장", "", "텍스트 (*.txt);;모든 파일 (*)")
            if not fname:
                return
            try:
                with open(fname, "w", encoding="utf-8") as f:
                    f.write(text)
                QMessageBox.information(self, "저장", "저장했습니다: %s" % fname)
            except Exception as e:
                QMessageBox.warning(self, "저장 실패", str(e))

    def log(self, msg):
        self.log_text.append(msg)
        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())

    def set_status(self, msg):
        self.status_label.setText(msg)

    def eventFilter(self, obj, event):
        """드롭다운 휠 무시(클릭으로만 변경) / 좌표 확인 중일 때 오른쪽 화면 클릭 잡기."""
        if event.type() == QEvent.Wheel:
            o = obj
            while o:
                if isinstance(o, QComboBox):
                    return True
                try:
                    o = o.parent()
                except Exception:
                    o = None
        if self.capture_mode and event.type() == QEvent.MouseButtonPress:
            try:
                from PyQt5.QtGui import QMouseEvent
                if isinstance(event, QMouseEvent):
                    pos_global = event.globalPos()
                    pos_in_web = self.web.mapFromGlobal(pos_global)
                    if self.web.rect().contains(pos_in_web):
                        x, y = pos_in_web.x(), pos_in_web.y()
                        mode = self.capture_mode
                        if mode == "RED":
                            self.red_edit.setText("%d,%d" % (x, y))
                            self.log("[좌표] RED = %d, %d" % (x, y))
                        elif mode == "BLACK":
                            self.black_edit.setText("%d,%d" % (x, y))
                            self.log("[좌표] BLACK = %d, %d" % (x, y))
                        elif mode == "AMOUNT":
                            self.amount_edit.setText("%d,%d" % (x, y))
                            self.log("[좌표] 금액 칸 = %d, %d" % (x, y))
                        self._end_capture()
                        self.set_status("%s 좌표 저장됨: %d, %d" % (mode, x, y))
                        return True
            except Exception:
                pass
        return super().eventFilter(obj, event)

    def _on_connect(self):
        """연결: 아날라이저에 한 번 요청해서 연결됨/미연결 표시."""
        url = (self.analyzer_url_edit.text().strip() or DEFAULT_ANALYZER_URL)
        self.connect_btn.setEnabled(False)
        self.set_status("연결 중...")

        def do_connect():
            data = fetch_results(url)
            self.update_queue.put(("connection", data.get("ok", False)))
            if not data.get("ok"):
                self.update_queue.put(("log", "[연결 실패] %s" % (data.get("error") or "알 수 없음")))
            else:
                self.update_queue.put(("log", "[연결 성공] 아날라이저 응답 정상."))
            self.update_queue.put(("conn_done", None))

        threading.Thread(target=do_connect, daemon=True).start()

    def _start_capture(self, color):
        """좌표 확인 모드: 버튼을 '좌표 확인중'으로 바꾸고, 오른쪽 화면 클릭 시 좌표가 칸에 들어가게."""
        self.capture_mode = color
        self.red_capture_btn.setText("RED 클릭해서 좌표 잡기")
        self.black_capture_btn.setText("BLACK 클릭해서 좌표 잡기")
        self.amount_capture_btn.setText("금액 칸 클릭해서 좌표 잡기")
        self.red_capture_btn.setStyleSheet("")
        self.black_capture_btn.setStyleSheet("")
        self.amount_capture_btn.setStyleSheet("")
        if color == "RED":
            self.red_capture_btn.setText("▶ 좌표 확인중... (오른쪽 화면 클릭)")
            self.red_capture_btn.setStyleSheet("background: #ffcdd2; font-weight: bold;")
        elif color == "BLACK":
            self.black_capture_btn.setText("▶ 좌표 확인중... (오른쪽 화면 클릭)")
            self.black_capture_btn.setStyleSheet("background: #ffcdd2; font-weight: bold;")
        elif color == "AMOUNT":
            self.amount_capture_btn.setText("▶ 좌표 확인중... (오른쪽 화면 클릭)")
            self.amount_capture_btn.setStyleSheet("background: #c8e6c9; font-weight: bold;")
        self.set_status("오른쪽 배팅 화면에서 %s 버튼 위치를 클릭하세요." % color)
        self.log("[좌표 잡기] %s 버튼 위치를 오른쪽 화면에서 클릭하세요." % color)
        app = QApplication.instance()
        if app:
            app.installEventFilter(self)
            self.web.setFocus(Qt.OtherFocusReason)

    def _end_capture(self):
        """좌표 확인 모드 해제, 버튼 문구/스타일 복구."""
        self.capture_mode = None
        self.red_capture_btn.setText("RED 클릭해서 좌표 잡기")
        self.black_capture_btn.setText("BLACK 클릭해서 좌표 잡기")
        self.amount_capture_btn.setText("금액 칸 클릭해서 좌표 잡기")
        self.red_capture_btn.setStyleSheet("")
        self.black_capture_btn.setStyleSheet("")
        self.amount_capture_btn.setStyleSheet("")
        app = QApplication.instance()
        if app:
            app.removeEventFilter(self)

    @pyqtSlot(bool)
    def _on_page_loaded_mute(self, ok):
        """페이지 로드 후 효과음 완전 음소거 + 창 크기에 맞게 줌."""
        if not ok:
            return
        def run_mute():
            js = """
            (function(){
                function muteAll(){
                    document.querySelectorAll('audio,video').forEach(function(el){
                        el.muted = true;
                        el.volume = 0;
                        el.pause && el.pause();
                    });
                }
                muteAll();
                if (window.AudioContext || window.webkitAudioContext) {
                    var Ctx = window.AudioContext || window.webkitAudioContext;
                    var orig = Ctx.prototype.createGain;
                    if (orig && !Ctx.prototype._gainMuted) {
                        Ctx.prototype._gainMuted = true;
                        Ctx.prototype.createGain = function(){
                            var g = orig.apply(this, arguments);
                            g.gain.value = 0;
                            return g;
                        };
                    }
                }
                var obs = new MutationObserver(function(){ muteAll(); });
                if (document.body) obs.observe(document.body, { childList: true, subtree: true });
                var op = HTMLMediaElement.prototype.play;
                if (op && !HTMLMediaElement.prototype._mutePlay) {
                    HTMLMediaElement.prototype._mutePlay = true;
                    HTMLMediaElement.prototype.play = function(){
                        this.muted = true;
                        this.volume = 0;
                        return op.apply(this, arguments);
                    };
                }
            })();
            """
            self.web.page().runJavaScript(js)
        run_mute()
        QTimer.singleShot(300, run_mute)
        QTimer.singleShot(1000, run_mute)
        # 사이트가 프로그램 창 크기에 맞게 줌
        try:
            w = self.web.width()
            zoom = max(0.5, min(1.0, w / 1200.0)) if w > 0 else 0.8
            self.web.setZoomFactor(zoom)
        except Exception:
            pass

    @pyqtSlot()
    def _on_go_site(self):
        url = self.betting_url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "입력", "배팅 사이트 URL을 입력하세요.")
            return
        if not url.startswith("http"):
            url = "https://" + url
        self.web.setUrl(QUrl(url))
        self.log(f"[이동] {url}")

    @pyqtSlot()
    def _on_open_test_page(self):
        """같은 폴더의 test_betting_page.html 로드 (좌표 테스트용)."""
        base = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(base, "test_betting_page.html")
        if not os.path.isfile(path):
            self.log("[테스트] test_betting_page.html 없음: %s" % path)
            QMessageBox.warning(self, "테스트", "test_betting_page.html 파일이 없습니다.\n%s" % path)
            return
        self.web.setUrl(QUrl.fromLocalFile(path))
        self.betting_url_edit.setText(path)
        self.log("[테스트] 테스트 페이지 로드. 금액 칸 / RED / BLACK 좌표 잡고 시작하면 동작 확인 가능.")

    @pyqtSlot()
    def _on_start(self):
        analyzer_url = (self.analyzer_url_edit.text().strip() or DEFAULT_ANALYZER_URL)
        betting_url = self.betting_url_edit.text().strip()
        if not betting_url:
            QMessageBox.warning(self, "입력", "배팅 사이트 URL을 입력하세요.")
            return
        try:
            interval = float(self.poll_interval_edit.text().strip())
            if interval < 0.2 or interval > 60:
                raise ValueError("0.2 ~ 60 사이로 입력하세요.")
        except ValueError as e:
            QMessageBox.warning(self, "입력", f"폴링 간격: {e}")
            return

        self.running = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._set_settings_locked(True)
        self.last_clicked_round = None
        self.last_pick = None
        self._start_round = None  # 이 회차는 스킵, 다음 회차부터 배팅
        try:
            self._base_bet = max(0, int(self.base_bet_edit.text().strip() or 0)) or 1000
        except ValueError:
            self._base_bet = 1000
        try:
            self._seed_money = max(0, int(self.seed_money_edit.text().strip() or 0))
        except (ValueError, TypeError):
            self._seed_money = 0
        self._martingale = self.martingale_check.isChecked()
        self._martingale_type = self.martingale_type_combo.currentData() or "double"
        self._reverse = self.reverse_check.isChecked()
        self._win_rate_reverse = self.win_rate_reverse_check.isChecked()
        try:
            self._win_rate_threshold = max(0, min(100, self.win_rate_threshold_spin.value()))
        except Exception:
            self._win_rate_threshold = 50
        try:
            self._duration_min = max(0, float(self.duration_edit.text().strip() or 0))
        except ValueError:
            self._duration_min = 0
        try:
            self._odds = max(1.0, float(self.odds_edit.text().strip().replace(",", ".") or 1.97))
        except (ValueError, TypeError):
            self._odds = 1.97
        self._start_time = time.time()
        self._target_enabled = self.target_enabled_check.isChecked()
        self.set_status("픽 폴링 중...")
        self.log("[시작] 폴링 시작. (오른쪽에서 배팅 사이트가 보이면 그곳에 클릭됩니다.)")
        # 경과시간 1초마다 갱신
        if getattr(self, "_elapsed_timer", None) is None:
            self._elapsed_timer = QTimer(self)
            self._elapsed_timer.timeout.connect(self._update_elapsed)
        self._elapsed_timer.start(1000)

        def poll_loop():
            while self.running:
                try:
                    # 시간 제한
                    if self._duration_min > 0 and (time.time() - self._start_time) >= self._duration_min * 60:
                        self.running = False
                        self.update_queue.put(("auto_stop", "목표시간"))
                        self.update_queue.put(("log", "[목표시간] %s분 경과 → 자동 중지" % self._duration_min))
                        self.update_queue.put(("auto_save_log", None))
                        break
                    result_src = (self.result_source_edit.text() or "").strip() or None
                    data = fetch_results(analyzer_url, result_source=result_src)
                    ok = data.get("ok", False)
                    self.update_queue.put(("connection", ok))
                    if not ok:
                        err = data.get("error", "연결 실패")
                        self.update_queue.put(("log", "[연결 실패] %s" % err))
                    sp = data.get("server_prediction") or {}
                    ph = data.get("prediction_history") or []
                    blended = data.get("blended_win_rate")
                    if blended is None and ph:
                        blended = _blended_win_rate_from_ph(ph)
                    round_actuals = data.get("round_actuals") or {}
                    self.update_queue.put(("pick", sp))
                    self.update_queue.put(("history", ph, blended, round_actuals))

                    pick_color = None
                    if sp.get("color") in ("빨강", "RED"):
                        pick_color = "RED"
                    elif sp.get("color") in ("검정", "BLACK"):
                        pick_color = "BLACK"
                    round_num = sp.get("round")
                    if round_num is not None:
                        round_num = int(round_num)
                    if pick_color not in ("RED", "BLACK") or round_num is None:
                        if not getattr(self, "_wait_log_count", 0) % 10:
                            self.update_queue.put(("log", "[대기] 픽 없음 - Analyzer 결과 페이지 열어두고 픽 나오는지 확인"))
                        self._wait_log_count = getattr(self, "_wait_log_count", 0) + 1
                        time.sleep(min(interval, 0.2))  # 픽 대기 시 0.2초마다 폴링
                        continue
                    if self.last_clicked_round == round_num:
                        time.sleep(interval)
                        continue
                    # 시작 시 현재 회차는 스킵, 다음 회차부터 배팅
                    if self._start_round is None:
                        self._start_round = round_num
                        time.sleep(interval)
                        continue
                    if round_num <= self._start_round:
                        time.sleep(interval)
                        continue
                    round_actuals = data.get("round_actuals") or {}
                    # 결과 대기: 직전 배팅(last_clicked_round) 결과가 round_actuals 또는 ph에 있어야 마틴/승률반픽 정상 동작
                    if self.last_clicked_round is not None:
                        rid = str(int(self.last_clicked_round))
                        has_prev_result = (
                            (rid in round_actuals and round_actuals.get(rid, {}).get("actual") not in (None, "joker"))
                            or any(
                                isinstance(h, dict) and int(h.get("round") or 0) == int(self.last_clicked_round or 0)
                                and h.get("actual") not in (None, "joker")
                                for h in (ph or [])
                            )
                        )
                        if not has_prev_result:
                            if not getattr(self, "_wait_result_log_count", 0) % 5:
                                self.update_queue.put(("log", "[대기] 직전 회차(%s) 결과 수신 대기 중..." % self.last_clicked_round))
                            self._wait_result_log_count = getattr(self, "_wait_result_log_count", 0) + 1
                            time.sleep(min(interval, 0.15))  # 결과 대기 시 0.15초마다 폴링 (결과 열 빠른 반영)
                            continue
                    # 반픽: 픽과 반대 색으로 클릭
                    if self._reverse:
                        pick_color = "BLACK" if pick_color == "RED" else "RED"
                    # results_from_ph: ph(API)에서 실제 결과 추출 (마틴·승률반픽 모두 이 값 사용)
                    amount = self._base_bet
                    consecutive_losses = 0
                    results_from_ph = []
                    if self.bet_log:
                        for b in self.bet_log:
                            rid = str(int(b.get("round") or 0))
                            our_pick = (b.get("pick_color") or "").strip().upper()
                            res_val = None
                            for h in (ph or []):
                                if not isinstance(h, dict) or int(h.get("round") or 0) != int(b.get("round") or 0) or h.get("actual") in (None, "joker"):
                                    continue
                                raw = (h.get("actual") or "").strip()
                                ac = (h.get("actualColor") or h.get("actual_color") or "").strip().upper()
                                actual_color = "RED" if ac in ("RED", "빨강") else "BLACK" if ac in ("BLACK", "검정") else None
                                if actual_color:
                                    res_val = our_pick == actual_color
                                    break
                                hp = (h.get("pickColor") or h.get("pick_color") or "").strip().upper()
                                h_pick = "RED" if hp in ("RED", "빨강") else "BLACK" if hp in ("BLACK", "검정") else None
                                if raw.upper() in ("RED", "빨강", "BLACK", "검정"):
                                    actual_color = "RED" if raw.upper() in ("RED", "빨강") else "BLACK"
                                elif raw in ("정", "꺽") and h_pick:
                                    actual_color = h_pick if raw == "정" else ("BLACK" if h_pick == "RED" else "RED")
                                if actual_color:
                                    res_val = our_pick == actual_color
                                else:
                                    res_val = (b.get("predicted") or "").strip() == raw
                                break
                            if res_val is not None:
                                results_from_ph.append(res_val)
                                continue
                            ra = round_actuals.get(rid) if isinstance(round_actuals, dict) else None
                            if ra:
                                if ra.get("actual") == "joker":
                                    results_from_ph.append(None)
                                else:
                                    ac = (ra.get("color") or "").strip().upper()
                                    ac_n = "RED" if ac in ("RED", "빨강") else "BLACK" if ac in ("BLACK", "검정") else None
                                    results_from_ph.append(our_pick == ac_n if ac_n else b.get("result"))
                            else:
                                results_from_ph.append(b.get("result"))
                    # 승률반픽: 합산승률(API 또는 ph 폴백)이 기준 % 이하일 때 반대로 배팅
                    if self._win_rate_reverse and blended is not None and blended <= self._win_rate_threshold:
                        orig = pick_color
                        pick_color = "BLACK" if pick_color == "RED" else "RED"
                        self.update_queue.put(("log", "[승률반픽] 합산 %.1f%% ≤ %s%% → %s 대신 %s 배팅" % (blended, self._win_rate_threshold, orig, pick_color)))
                    if self._martingale:
                        for res in reversed(results_from_ph):
                            if res is None:
                                break
                            if res is False:
                                consecutive_losses += 1
                            else:
                                break
                        if self._martingale_type == "double":
                            if consecutive_losses > 0 and self.bet_log:
                                last_amt = self.bet_log[-1].get("amount") or self._base_bet
                                amount = last_amt * 2
                        else:
                            # 표마틴/표마틴 반: 초기배팅 무시, 무조건 표 금액 (첫 배팅=1단계 10000원)
                            step = min(consecutive_losses, len(TABLE_MARTIN_PYO) - 1)
                            amount = TABLE_MARTIN_PYO[step]
                            if self._martingale_type == "pyo_half":
                                amount = amount // 2

                    # 시드머니 기준 현재금액으로 배팅금액 상한 (배당 반영 순익)
                    seed = getattr(self, "_seed_money", 0) or 0
                    odds = getattr(self, "_odds", 1.97) or 1.97
                    cum = 0
                    if self.bet_log and results_from_ph and len(results_from_ph) == len(self.bet_log):
                        for b, res in zip(self.bet_log, results_from_ph):
                            if res is not None:
                                amt = b.get("amount") or 0
                                cum += amt * (odds - 1) if res else -amt
                    current_balance = seed + cum
                    if current_balance < amount and current_balance >= 0:
                        amount = current_balance
                    elif current_balance < 0:
                        amount = 0

                    pred_val = sp.get("value") or ""
                    row = {"round": round_num, "predicted": pred_val, "pick_color": pick_color, "amount": amount, "actual": None, "result": None, "cumulative": 0}
                    self.update_queue.put(("bet", row))
                    self.last_pick = (pick_color, round_num)
                    # 클릭은 메인 스레드에서 실행 (QWebEngine runJavaScript는 메인 스레드에서만 동작)
                    self.update_queue.put(("do_click", amount, pick_color))
                    self.last_clicked_round = round_num
                except Exception as e:
                    self.update_queue.put(("connection", False))
                    self.update_queue.put(("log", "[폴링] %s" % e))
                time.sleep(interval)

        self.poll_thread = threading.Thread(target=poll_loop, daemon=True)
        self.poll_thread.start()

    def _do_bet_sequence(self, amount, pick_color):
        """배팅 시퀀스: 금액 칸에 금액 입력 → 잠시 대기 → 예측픽(RED/BLACK) 클릭."""
        page = self.web.page()
        if not page:
            self.log("[배팅] 페이지 없음.")
            return
        amount_val = self.amount_edit.text().strip()
        amount_parsed = parse_selector_or_xy(amount_val) if amount_val else None
        red_val = self.red_edit.text().strip()
        black_val = self.black_edit.text().strip()
        if pick_color == "RED":
            pick_parsed = parse_selector_or_xy(red_val)
        else:
            pick_parsed = parse_selector_or_xy(black_val)
        if not pick_parsed:
            self.log("[배팅] %s 버튼 좌표/셀렉터 없음." % pick_color)
            return
        if not amount_parsed:
            self.log("[배팅] 금액 칸 좌표/셀렉터 없음. '금액 칸 클릭해서 좌표 잡기'로 설정하세요.")
            return
        self.log("[배팅] 실행: 금액=%s, %s" % (amount, pick_color))
        try:
            # 1) 금액 입력 (셀렉터 또는 좌표) — 반드시 문자열로 전달
            amount_str = str(int(amount)) if amount is not None else "0"
            if amount_parsed[0] == "selector":
                page.runJavaScript(js_set_value_by_selector(amount_parsed[1], amount_str))
            else:
                ax, ay = amount_parsed[1], amount_parsed[2]
                page.runJavaScript(js_set_value_at_xy(ax, ay, amount_str))
            # 2) 0.7초 대기 후 RED/BLACK 클릭 (금액 반영 시간 확보)
            QTimer.singleShot(700, lambda: self._do_click_in_page(pick_color))
            self.set_status("마지막: %s 회차" % pick_color)
        except Exception as e:
            self.log("[배팅 실패] %s" % e)

    def _do_click_in_page(self, pick_color):
        """내장 QWebEngineView 페이지에서 RED 또는 BLACK 버튼만 클릭."""
        page = self.web.page()
        if not page:
            return
        red_val = self.red_edit.text().strip()
        black_val = self.black_edit.text().strip()
        if pick_color == "RED":
            parsed = parse_selector_or_xy(red_val)
        else:
            parsed = parse_selector_or_xy(black_val)
        if not parsed:
            self.log(f"[클릭] {pick_color} 버튼 설정 없음.")
            return
        try:
            if parsed[0] == "xy":
                x, y = parsed[1], parsed[2]
                page.runJavaScript(js_click_xy(x, y, pick_color))
                self.log(f"[클릭] {pick_color} 좌표 ({x},{y})")
                # 디버그: 해당 좌표의 요소가 뭔지 로그 (클릭 직후라 다른 요소가 나올 수 있음)
                page.runJavaScript(
                    "(function(x,y){var e=document.elementFromPoint(x,y);return e?e.tagName+(e.id?'#'+e.id:''):'null';})(%s,%s)" % (x, y),
                    lambda v: self.log("[좌표 %s,%s] 요소: %s" % (x, y, v if v is not None else "?"))
                )
            else:
                sel = parsed[1]
                page.runJavaScript(js_click_selector(sel))
                self.log(f"[클릭] {pick_color} 셀렉터 {sel}")
            self.set_status(f"마지막: {pick_color} 회차")
        except Exception as e:
            self.log(f"[클릭 실패] {pick_color} - {e}")

    @pyqtSlot()
    def _on_insufficient_funds(self):
        """사이트에서 '보유 금액 없음' 알림이 뜬 경우: 알림은 이미 닫힘, 배팅 중지."""
        if not self.running:
            return
        self.running = False
        if getattr(self, "_elapsed_timer", None):
            self._elapsed_timer.stop()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._set_settings_locked(False)
        self.set_status("보유 금액 없음으로 중지")
        self.log("[자동 중지] 카지노/슬롯 '보유 금액 없음' 알림 감지 → 배팅 중지. 잔액 확인 후 다시 시작하세요.")
        self.update_queue.put(("auto_save_log", None))

    @pyqtSlot()
    def _on_stop(self):
        self.running = False
        if getattr(self, "_elapsed_timer", None):
            self._elapsed_timer.stop()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._set_settings_locked(False)
        # 배팅 초기화 (이어하기 아님, 새로 시작)
        self.bet_log = []
        self.last_clicked_round = None
        self.last_pick = None
        self._start_round = None
        self._refresh_bet_table()
        self.set_status("중지됨")
        self.log("[중지] 매크로 중지. 배팅 기록 초기화됨.")

    def closeEvent(self, event):
        if self.running:
            if QMessageBox.question(
                self, "종료", "매크로가 실행 중입니다. 종료할까요?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            ) == QMessageBox.Yes:
                self._on_stop()
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


def main_pyqt():
    import sys
    app = QApplication(sys.argv)
    app.setApplicationName("만수루프로젝트")
    w = MacroWindow()
    w.show()
    sys.exit(app.exec_())


def main_fallback():
    """PyQt5 없을 때 안내."""
    print("PyQt5와 PyQtWebEngine이 필요합니다.")
    print("설치: pip install PyQt5 PyQtWebEngine")
    print("설치 후 다시 python macro.py 를 실행하세요.")


if __name__ == "__main__":
    if HAS_PYQT:
        main_pyqt()
    else:
        main_fallback()
