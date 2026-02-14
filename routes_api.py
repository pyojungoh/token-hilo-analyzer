# -*- coding: utf-8 -*-
"""
5단계: /api/* 라우트 전용 Blueprint.
순환 import 방지: 핸들러는 register_api_routes(app) 호출 시점에 app에서 import.
"""
from flask import Blueprint

api_bp = Blueprint('api', __name__, url_prefix='/api')


def register_api_routes(app):
    """app 로드 완료 후 호출. app의 API 뷰 함수를 이 블루프린트에 등록."""
    from app import (
        get_results,
        get_current_prediction,
        api_calc_state,
        api_win_rate_buckets,
        api_dont_bet_ranges,
        api_losing_streaks,
        api_save_round_prediction,
        api_save_prediction_history,
        api_current_pick,
        api_server_time,
        get_current_status,
        get_streaks,
        get_user_streak,
        refresh_data,
        test_betting,
        debug_db_status,
        debug_init_db,
        debug_results_check,
    )
    api_bp.add_url_rule('/results', 'get_results', get_results, methods=['GET'])
    api_bp.add_url_rule('/current-prediction', 'get_current_prediction', get_current_prediction, methods=['GET'])
    api_bp.add_url_rule('/calc-state', 'api_calc_state', api_calc_state, methods=['GET', 'POST'])
    api_bp.add_url_rule('/win-rate-buckets', 'api_win_rate_buckets', api_win_rate_buckets, methods=['GET'])
    api_bp.add_url_rule('/dont-bet-ranges', 'api_dont_bet_ranges', api_dont_bet_ranges, methods=['GET'])
    api_bp.add_url_rule('/losing-streaks', 'api_losing_streaks', api_losing_streaks, methods=['GET'])
    api_bp.add_url_rule('/round-prediction', 'api_save_round_prediction', api_save_round_prediction, methods=['POST'])
    api_bp.add_url_rule('/prediction-history', 'api_save_prediction_history', api_save_prediction_history, methods=['POST'])
    api_bp.add_url_rule('/current-pick', 'api_current_pick', api_current_pick, methods=['GET', 'POST'])
    api_bp.add_url_rule('/server-time', 'api_server_time', api_server_time, methods=['GET'])
    api_bp.add_url_rule('/current-status', 'get_current_status', get_current_status, methods=['GET'])
    api_bp.add_url_rule('/streaks', 'get_streaks', get_streaks, methods=['GET'])
    api_bp.add_url_rule('/streaks/<user_id>', 'get_user_streak', get_user_streak, methods=['GET'])
    api_bp.add_url_rule('/refresh', 'refresh_data', refresh_data, methods=['POST'])
    api_bp.add_url_rule('/test-betting', 'test_betting', test_betting, methods=['GET'])
    api_bp.add_url_rule('/debug/db-status', 'debug_db_status', debug_db_status, methods=['GET'])
    api_bp.add_url_rule('/debug/init-db', 'debug_init_db', debug_init_db, methods=['POST'])
    api_bp.add_url_rule('/debug/results-check', 'debug_results_check', debug_results_check, methods=['GET'])
