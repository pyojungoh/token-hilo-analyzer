# -*- coding: utf-8 -*-
"""
에뮬레이터(LDPlayer) 자동배팅용 좌표 찾기 프로그램.
배팅금액, 정정(금액정정), 레드, 블랙 버튼 위치를 화면에서 클릭해 좌표를 저장합니다.
저장된 좌표는 emulator_coords.json 에 저장되며, ADB 매크로에서 사용합니다.
정정 = 금액정정 버튼 좌표.
※ 반드시 LDPlayer 창 안에서만 클릭하세요. 창 좌표가 자동 감지되어 매크로와 호환됩니다.
"""
import json
import os
import sys
import tkinter as tk
from tkinter import ttk, font as tkfont

# 상태 문구 색
STATUS_SEARCHING = "검색중"   # 초록
STATUS_SAVED = "저장됨"       # 초록

# 전역 클릭 캡처용 (화면 어디든 클릭 감지)
try:
    from pynput import mouse
    HAS_PYNPUT = True
except ImportError:
    HAS_PYNPUT = False

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "emulator_coords.json")


def get_window_rect_at(screen_x, screen_y):
    """클릭한 점이 속한 창의 클라이언트 영역 (left, top, width, height) 반환.
    emulator_macro.py와 동일 — GetClientRect + ClientToScreen 사용."""
    if sys.platform != "win32":
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

# 설정 키 → 한글 라벨 (정정 = 금액정정 버튼)
LABELS = {
    "bet_amount": "배팅금액",
    "confirm": "정정",
    "red": "레드",
    "black": "블랙",
}


def load_config():
    """저장된 좌표 설정 불러오기. 기존 window_left, coord_spaces 등은 유지."""
    if not os.path.exists(CONFIG_PATH):
        return {k: None for k in LABELS}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k in LABELS:
            if k not in data:
                data[k] = None
        return data
    except Exception:
        return {k: None for k in LABELS}


def save_config(config):
    """좌표 설정 저장."""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


class CoordPickerApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("에뮬레이터 좌표 찾기")
        self.root.minsize(320, 280)
        self.root.resizable(True, True)

        self.config = load_config()
        self._listener = None
        self._capture_key = None  # 현재 캡처 중인 키
        self._pending_click = None  # (key, x, y) 캡처 결과

        self._build_ui()
        self._refresh_labels()

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, text="LDPlayer 창 안에서 해당 위치를 클릭하세요. (매크로 창과 겹치면 잘못 잡힙니다)", font=("", 9)).pack(anchor=tk.W)

        f = tkfont.Font(size=9)
        self._value_labels = {}
        self._status_labels = {}
        for key, label in LABELS.items():
            row = ttk.Frame(main)
            row.pack(fill=tk.X, pady=4)
            ttk.Button(row, text=f"{label} 좌표 찾기", command=lambda k=key: self._start_capture(k)).pack(side=tk.LEFT, padx=(0, 8))
            lbl = ttk.Label(row, text="(미설정)", font=f)
            lbl.pack(side=tk.LEFT, padx=(0, 8))
            self._value_labels[key] = lbl
            status_lbl = tk.Label(row, text="", font=("", 9), fg="green")
            status_lbl.pack(side=tk.LEFT)
            self._status_labels[key] = status_lbl

        ttk.Separator(main, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8)

        # 기본 배팅금액(입력할 숫자)
        def_row = ttk.Frame(main)
        def_row.pack(fill=tk.X, pady=4)
        ttk.Label(def_row, text="기본 배팅금액(숫자):").pack(side=tk.LEFT, padx=(0, 8))
        self._default_bet_var = tk.StringVar(value=self.config.get("default_bet") or "100")
        ttk.Entry(def_row, textvariable=self._default_bet_var, width=10).pack(side=tk.LEFT)

        save_row = ttk.Frame(main)
        save_row.pack(pady=8)
        ttk.Button(save_row, text="저장", command=self._save_default_bet).pack(side=tk.LEFT, padx=(0, 8))
        self._save_status_label = tk.Label(save_row, text="", font=("", 9), fg="green")
        self._save_status_label.pack(side=tk.LEFT)

        if not HAS_PYNPUT:
            ttk.Label(main, text="경고: pynput 미설치. 'pip install pynput' 후 다시 실행하세요.", foreground="red").pack(anchor=tk.W)

    def _refresh_labels(self):
        for key in LABELS:
            val = self.config.get(key)
            if val is not None and isinstance(val, (list, tuple)) and len(val) >= 2:
                self._value_labels[key].config(text=f"({val[0]}, {val[1]})")
            else:
                self._value_labels[key].config(text="(미설정)")

    def _set_status(self, key, text, color="green"):
        if key in self._status_labels:
            self._status_labels[key].config(text=text, fg=color)

    def _start_capture(self, key):
        if not HAS_PYNPUT:
            self._set_status(next(iter(LABELS)), "pynput 설치 필요: pip install pynput", "red")
            return
        if self._listener is not None:
            self._set_status(key, "다른 항목 검색 중", "red")
            self.root.after(2000, lambda: self._set_status(key, ""))
            return

        self._capture_key = key
        self._set_status(key, "검색중", "green")
        self.root.iconify()
        self._listener = mouse.Listener(on_click=self._on_click)
        self._listener.start()

    def _on_click(self, x, y, button, pressed):
        if not pressed:
            return
        if self._capture_key is None:
            return
        self._pending_click = (self._capture_key, x, y)
        self._capture_key = None
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        self.root.after(0, self._apply_captured_click)

    def _apply_captured_click(self):
        self.root.deiconify()
        self.root.lift()
        if self._pending_click is None:
            return
        key, x, y = self._pending_click
        self._pending_click = None
        # emulator_macro와 동일: 클릭한 창의 클라이언트 영역 자동 감지 → 창 내 상대 좌표 저장
        rect = get_window_rect_at(x, y)
        if rect is not None:
            left, top, w, h = rect
            rel_x, rel_y = x - left, y - top
            self.config[key] = [rel_x, rel_y]
            self.config["window_left"] = left
            self.config["window_top"] = top
            self.config["window_width"] = w
            self.config["window_height"] = h
            if not int(self.config.get("device_width") or 0) or not int(self.config.get("device_height") or 0):
                self.config["device_width"] = w
                self.config["device_height"] = h
            sp = self.config.get("coord_spaces") or {}
            sp[key] = True
            self.config["coord_spaces"] = sp
            self._set_status(key, "저장됨(창 자동)", "green")
        else:
            self.config[key] = [x, y]
            sp = self.config.get("coord_spaces") or {}
            sp[key] = False
            self.config["coord_spaces"] = sp
            self._set_status(key, "저장됨", "green")
        save_config(self.config)
        self._refresh_labels()
        self.root.after(1500, lambda: self._set_status(key, ""))

    def _save_default_bet(self):
        try:
            val = self._default_bet_var.get().strip()
            if val:
                self.config["default_bet"] = val
                save_config(self.config)
                self._save_status_label.config(text="저장됨", fg="green")
                self.root.after(1500, lambda: self._save_status_label.config(text=""))
        except Exception as e:
            self._save_status_label.config(text=f"오류: {e}", fg="red")
            self.root.after(3000, lambda: self._save_status_label.config(text=""))

    def run(self):
        self.root.mainloop()


def main():
    app = CoordPickerApp()
    app.run()


if __name__ == "__main__":
    main()
