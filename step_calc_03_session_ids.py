# -*- coding: utf-8 -*-
"""
3.3 _get_all_calc_session_ids 추출.
- 추출: 542~563행을 calc_handlers.py에 append
- 제거: app.py에서 542~563행 삭제
- 삽입: "from calc_handlers import _merge_calc_histories" 다음에 _get_all_calc_session_ids 추가
"""
import os

APP = "app.py"
HANDLERS = "calc_handlers.py"
TMP = "app.py.tmp"
START, END = 542, 563   # inclusive
IMPORT_ANCHOR = "from calc_handlers import _merge_calc_histories"
NEW_IMPORT = "from calc_handlers import _merge_calc_histories, _get_all_calc_session_ids"

def main():
    base = os.path.dirname(os.path.abspath(__file__))
    app_path = os.path.join(base, APP)
    handlers_path = os.path.join(base, HANDLERS)
    tmp_path = os.path.join(base, TMP)

    # 1) 추출: 542~563행을 calc_handlers.py에 append
    with open(app_path, "r", encoding="utf-8") as f:
        lines = []
        for i, line in enumerate(f, 1):
            if START <= i <= END:
                lines.append(line)
    with open(handlers_path, "a", encoding="utf-8") as f:
        f.write("\n")
        f.writelines(lines)
    print("1) Appended lines %d-%d to %s" % (START, END, HANDLERS))

    # 2) app.py에서 542~563 제거 + import 줄을 _merge_calc_histories 만 -> _merge_calc_histories, _get_all_calc_session_ids 로 교체
    with open(app_path, "r", encoding="utf-8") as fin:
        with open(tmp_path, "w", encoding="utf-8") as fout:
            for i, line in enumerate(fin, 1):
                if START <= i <= END:
                    continue
                if IMPORT_ANCHOR in line and " _get_all_calc_session_ids" not in line:
                    line = NEW_IMPORT + "\n"
                fout.write(line)
    os.replace(tmp_path, app_path)
    print("2) Removed lines %d-%d from %s and updated import" % (START, END, APP))

if __name__ == "__main__":
    main()
