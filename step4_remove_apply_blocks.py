# -*- coding: utf-8 -*-
"""
app.py에서 _apply_results_to_calcs, _server_calc_effective_pick_and_amount 블록 제거.
step4_add_apply_import.py 실행 후 라인 번호 (import 한 줄 추가되어 +1).
한 줄씩 처리. 실행: python step4_remove_apply_blocks.py
"""
# import 추가 후 기준: 기존 825~1036 -> 826~1037, 기존 1164~1255 -> 1165~1256
SKIP_RANGES = [(826, 1037), (1165, 1256)]

def main():
    app_path = 'app.py'
    out_path = 'app.py.new'
    skip = set()
    for a, b in SKIP_RANGES:
        for i in range(a, b + 1):
            skip.add(i)
    with open(app_path, 'r', encoding='utf-8') as f:
        with open(out_path, 'w', encoding='utf-8') as out:
            for i, line in enumerate(f, start=1):
                if i in skip:
                    continue
                out.write(line)
    import os
    os.replace(out_path, app_path)
    print("Done. Two function blocks removed from app.py.")

if __name__ == '__main__':
    main()
