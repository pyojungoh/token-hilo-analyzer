# -*- coding: utf-8 -*-
"""
5단계: app.py에서 @app.route('/api/...') 데코레이터 줄만 제거.
함수(def get_results 등)는 그대로 두고, 데코레이터만 삭제 → Blueprint에서 등록하므로 중복 제거.
한 줄씩 처리하여 OOM 방지.
"""
import re

def main():
    app_path = 'app.py'
    out_path = 'app.py.new'
    # /api/ 로 시작하는 @app.route 줄만 제거 (공백 포함)
    pattern = re.compile(r"^\s*@app\.route\s*\(\s*['\"]\/api\/")
    with open(app_path, 'r', encoding='utf-8') as f:
        with open(out_path, 'w', encoding='utf-8') as out:
            for line in f:
                if pattern.match(line):
                    continue
                out.write(line)
    import os
    os.replace(out_path, app_path)
    print('Done. @app.route("/api/...") lines only removed.')

if __name__ == '__main__':
    main()
