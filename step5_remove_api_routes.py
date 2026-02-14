# -*- coding: utf-8 -*-
"""
5단계: app.py에서 기존 /api/* 라우트 블록 제거 (Blueprint로 이전했으므로).
app.py 전체를 메모리에 올리지 않고 한 줄씩 처리.
실행: python step5_remove_api_routes.py
"""
# 제거할 라인 범위 (1-based, 포함). @app.route + 해당 def 블록 전체.
# (get_results ~ debug_results_check. results_page, betting_helper, index, favicon 등은 유지)
SKIP_RANGES = [
    (8230, 8308),   # @app.route('/api/results') + get_results
    (8309, 8318),   # /api/current-prediction + get_current_prediction
    (8319, 8509),   # /api/calc-state + api_calc_state
    (8510, 8565),   # /api/win-rate-buckets + api_win_rate_buckets
    (8566, 8648),   # /api/dont-bet-ranges + api_dont_bet_ranges
    (8649, 8704),   # /api/losing-streaks + api_losing_streaks
    (8705, 8721),   # /api/round-prediction + api_save_round_prediction
    (8722, 8744),   # /api/prediction-history + api_save_prediction_history
    (8745, 8852),   # /api/current-pick + api_current_pick
    (8882, 8887),   # /api/server-time + api_server_time
    (8888, 8913),   # /api/current-status + get_current_status
    (8914, 8934),   # /api/streaks + get_streaks
    (8935, 8959),   # /api/streaks/<user_id> + get_user_streak
    (8960, 9011),   # /api/refresh + refresh_data
    (9026, 9046),   # /api/test-betting + test_betting
    (9047, 9140),   # /api/debug/db-status + debug_db_status
    (9141, 9159),   # /api/debug/init-db + debug_init_db
    (9160, 9213),   # /api/debug/results-check + debug_results_check
]

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
                out.write(line)

    import os
    os.replace(out_path, app_path)
    print('Done. API route blocks removed from app.py.')

if __name__ == '__main__':
    main()
