# -*- coding: utf-8 -*-
"""
4단계 남은 2함수 추출: app.py에서 해당 라인 구간만 읽어 apply_logic.py 생성.
한 줄씩 처리. 실행: python step4_extract_apply_functions.py
"""
RANGE1 = (825, 1036)   # _apply_results_to_calcs
RANGE2 = (1164, 1255)  # _server_calc_effective_pick_and_amount
INSERT_IMPORT_AFTER_LINE = {829: None, 1166: None}  # line no -> None (use default import)

IMPORT_LINE_1 = """    from app import (
        get_calc_state, save_calc_state, get_stored_round_prediction,
        _get_all_calc_session_ids, _get_actual_for_round,
        ensure_stored_prediction_for_current_round, compute_prediction,
        save_prediction_record, _calculate_calc_profit_server,
        _update_calc_paused_after_round, _merge_calc_histories,
        _push_current_pick_from_calc, results_cache,
    )
"""
IMPORT_LINE_2 = """    from app import (
        get_prediction_history, _get_main_recent15_win_rate,
        _get_current_result_run_length, _get_lose_streak_from_history,
        _effective_win_rate_direction_zone, results_cache,
    )
"""

HEADER = '''# -*- coding: utf-8 -*-
"""
4단계: 계산 반영 로직. app 의존은 함수 내 lazy import로 순환 방지.
"""
from utils import normalize_pick_color_value, flip_pick_color, round_eq
from prediction_logic import _blended_win_rate

'''

def main():
    app_path = 'app.py'
    out_path = 'apply_logic.py'
    out_lines = [HEADER]
    with open(app_path, 'r', encoding='utf-8') as f:
        for i, line in enumerate(f, start=1):
            if RANGE1[0] <= i <= RANGE1[1]:
                if i == 829:
                    out_lines.append(IMPORT_LINE_1)
                out_lines.append(line)
            elif RANGE2[0] <= i <= RANGE2[1]:
                if i == 1166:
                    out_lines.append(IMPORT_LINE_2)
                out_lines.append(line)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.writelines(out_lines)
    print("Done. apply_logic.py created.")

if __name__ == '__main__':
    main()
