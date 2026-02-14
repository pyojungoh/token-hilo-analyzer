# -*- coding: utf-8 -*-
"""
4단계 제거: app.py에서 3개 함수 블록만 제거하고 import 한 줄 수정.
app.py 전체를 메모리에 올리지 않고 한 줄씩 처리합니다.
실행: python step4_remove_by_lines.py
"""
# 제거할 라인 범위 (1-based, 포함). 현재 app.py 기준 — 필요 시 수정.
SKIP_RANGES = [
    (769, 778),   # _server_recent_15_win_rate
    (900, 931),   # _update_calc_paused_after_round
    (932, 1073),  # _calculate_calc_profit_server
]
IMPORT_LINE_NUM = 41  # 수정할 import 라인
NEW_IMPORT = 'from prediction_logic import _blended_win_rate, _blended_win_rate_components, _server_recent_15_win_rate, _update_calc_paused_after_round, _calculate_calc_profit_server'

def main():
    app_path = 'app.py'
    out_path = 'app.py.new'
    skip_set = set()
    for a, b in SKIP_RANGES:
        for i in range(a, b + 1):
            skip_set.add(i)

    with open(app_path, 'r', encoding='utf-8') as f:
        with open(out_path, 'w', encoding='utf-8') as out:
            for i, line in enumerate(f, start=1):
                if i in skip_set:
                    continue
                if i == IMPORT_LINE_NUM:
                    out.write(NEW_IMPORT + '\n')
                    continue
                out.write(line)

    import os
    os.replace(out_path, app_path)
    print('Done. app.py updated.')

if __name__ == '__main__':
    main()
