# -*- coding: utf-8 -*-
"""
3.2 _merge_calc_histories 추출: app.py는 한 줄씩만 읽고, 수정도 스크립트로만.
- 추출: 541~587행을 calc_handlers.py에 append
- 제거: app.py에서 541~587행 삭제
- 삽입: "from apply_logic import" 다음에 from calc_handlers import _merge_calc_histories 추가
"""
import os

APP = "app.py"
HANDLERS = "calc_handlers.py"
TMP = "app.py.tmp"
START, END = 541, 587   # inclusive
IMPORT_ANCHOR = "from apply_logic import"
NEW_IMPORT = "from calc_handlers import _merge_calc_histories"

def main():
    base = os.path.dirname(os.path.abspath(__file__))
    app_path = os.path.join(base, APP)
    handlers_path = os.path.join(base, HANDLERS)
    tmp_path = os.path.join(base, TMP)

    # 1) 추출: 541~587행을 calc_handlers.py에 append
    with open(app_path, "r", encoding="utf-8") as f:
        lines = []
        for i, line in enumerate(f, 1):
            if START <= i <= END:
                lines.append(line)
    with open(handlers_path, "a", encoding="utf-8") as f:
        f.write("\n")
        f.writelines(lines)
    print("1) Appended lines %d-%d to %s" % (START, END, HANDLERS))

    # 2) app.py에서 541~587 제거 + "from apply_logic import" 다음에 import 한 줄 삽입
    with open(app_path, "r", encoding="utf-8") as fin:
        with open(tmp_path, "w", encoding="utf-8") as fout:
            for i, line in enumerate(fin, 1):
                if START <= i <= END:
                    continue
                fout.write(line)
                if IMPORT_ANCHOR in line and NEW_IMPORT not in line:
                    fout.write(NEW_IMPORT + "\n")
    os.replace(tmp_path, app_path)
    print("2) Removed lines %d-%d from %s and added import" % (START, END, APP))

if __name__ == "__main__":
    main()
