# -*- coding: utf-8 -*-
"""
5단계 2차: app.py에서 페이지용 @app.route 데코레이터 줄만 제거.
함수(def index, results_page 등)는 그대로 두고, 데코레이터만 삭제.
한 줄씩 처리하여 OOM 방지. 실행: python step5_remove_pages_decorators_only.py
"""
import re

# /api/ 가 아닌 페이지 경로만 대상 (한 줄에 @app.route + 경로)
PATTERNS = [
    re.compile(r"^\s*@app\.route\s*\(\s*['\"]\/\s*['\"]"),           # '/'
    re.compile(r"^\s*@app\.route\s*\(\s*['\"]\/results['\"]"),      # '/results'
    re.compile(r"^\s*@app\.route\s*\(\s*['\"]\/betting-helper"),    # '/betting-helper'
    re.compile(r"^\s*@app\.route\s*\(\s*['\"]\/practice"),         # '/practice'
    re.compile(r"^\s*@app\.route\s*\(\s*['\"]\/docs\/tampermonkey"), # tampermonkey
    re.compile(r"^\s*@app\.route\s*\(\s*['\"]\/favicon\.ico"),       # '/favicon.ico'
]

def main():
    app_path = 'app.py'
    out_path = 'app.py.new'
    removed = 0
    with open(app_path, 'r', encoding='utf-8') as f:
        with open(out_path, 'w', encoding='utf-8') as out:
            for line in f:
                if any(p.match(line) for p in PATTERNS):
                    removed += 1
                    continue
                out.write(line)
    import os
    os.replace(out_path, app_path)
    print(f"Done. Removed {removed} page decorator line(s).")

if __name__ == '__main__':
    main()
