# -*- coding: utf-8 -*-
"""
4단계: 계산 반영 로직. app 의존은 함수 내 lazy import로 순환 방지.
"""
from utils import normalize_pick_color_value, flip_pick_color, round_eq
from prediction_logic import _blended_win_rate

def _apply_results_to_calcs(results):
    """결과 수집 후 실행 중인 계산기 회차 반영: pending_round 결과 있으면 history 반영 후 다음 예측으로 갱신.
    안정화: pending_*는 저장된 예측(round_predictions)만 사용. 저장은 스케줄러 ensure_stored에서만.
    서버에서 계산기 수익, 마틴게일, 연승/연패 계산."""
    from calc_handlers import _get_all_calc_session_ids
    from app import (
        get_calc_state, save_calc_state, get_stored_round_prediction,
        _get_actual_for_round,
        ensure_stored_prediction_for_current_round, compute_prediction,
        save_prediction_record, _calculate_calc_profit_server,
        _update_calc_paused_after_round, _merge_calc_histories,
        _push_current_pick_from_calc, results_cache,
    )
    if not results or len(results) < 16:
        return
    try:
        latest_gid = results[0].get('gameID')
        predicted_round = int(str(latest_gid or '0'), 10) + 1
        stored_for_round = get_stored_round_prediction(predicted_round) if predicted_round else None

        session_ids = _get_all_calc_session_ids()
        for session_id in session_ids:
            state = get_calc_state(session_id)
            if not state or not isinstance(state, dict):
                continue
            updated = False
            to_push = []  # (calculator_id, c) — save_calc_state 후에 푸시해 POST 시 서버 보정이 동작하도록
            for cid in ('1', '2', '3'):
                c = state.get(cid)
                if not c or not isinstance(c, dict) or not c.get('running'):
                    continue
                pending_round = c.get('pending_round')
                pending_predicted = c.get('pending_predicted')
                if pending_round is None or pending_predicted is None:
                    if stored_for_round and stored_for_round.get('predicted'):
                        c['pending_round'] = predicted_round
                        c['pending_predicted'] = stored_for_round['predicted']
                        c['pending_prob'] = stored_for_round.get('probability')
                        c['pending_color'] = stored_for_round.get('pick_color')
                        _, amt = _server_calc_effective_pick_and_amount(c)
                        c['pending_bet_amount'] = amt if amt is not None and amt > 0 else None
                        updated = True
                        to_push.append((int(cid), c))
                    continue
                actual = _get_actual_for_round(results, pending_round)
                if actual is None:
                    continue
                first_bet = c.get('first_bet_round') or 0
                if first_bet > 0 and pending_round < first_bet:
                    continue
                # prediction_history(예측기표)에는 항상 예측기 픽만 저장. 계산기(반픽/승률반픽)는 calc history에만 반영.
                pred_for_record = pending_predicted
                pick_color_for_record = normalize_pick_color_value(c.get('pending_color'))
                if pick_color_for_record is None:
                    if pending_predicted == '정':
                        pick_color_for_record = '빨강'
                    elif pending_predicted == '꺽':
                        pick_color_for_record = '검정'
                # 예측 시점의 shape_signature를 계산하기 위해 pending_round를 제외한 이전 결과 사용
                results_for_shape = None
                if results and len(results) >= 16:
                    # pending_round 이후의 결과들을 제외하고, pending_round 직전까지의 결과만 사용
                    filtered_results = [r for r in results if int(str(r.get('gameID', '0')), 10) < pending_round]
                    if len(filtered_results) >= 16:
                        results_for_shape = filtered_results
                save_prediction_record(
                    pending_round, pred_for_record, actual,
                    probability=c.get('pending_prob'), pick_color=pick_color_for_record or c.get('pending_color'),
                    results=results_for_shape
                )
                if results_for_shape and len(results_for_shape) >= 16 and DB_AVAILABLE and DATABASE_URL:
                    sig = _get_shape_signature(results_for_shape)
                    if sig:
                        conn_shape = get_db_connection(statement_timeout_sec=3)
                        if conn_shape:
                            try:
                                update_shape_win_stats(conn_shape, sig, actual, round_num=pending_round)
                                chunk_prof, chunk_seg = _get_chunk_profile_from_results(results_for_shape)
                                if chunk_prof:
                                    update_chunk_profile_occurrences(conn_shape, chunk_prof, actual, round_num=pending_round, segment_type=chunk_seg or 'chunk')
                            finally:
                                try:
                                    conn_shape.close()
                                except Exception:
                                    pass
                # 계산기 히스토리·표시용: 배팅한 픽(반픽/승률반픽 적용)
                pred_for_calc = pending_predicted
                bet_color_for_history = normalize_pick_color_value(c.get('pending_color'))
                if bet_color_for_history is None:
                    if pending_predicted == '정':
                        bet_color_for_history = '빨강'
                    elif pending_predicted == '꺽':
                        bet_color_for_history = '검정'
                if c.get('reverse'):
                    pred_for_calc = '꺽' if pending_predicted == '정' else '정'
                    bet_color_for_history = flip_pick_color(bet_color_for_history)
                blended = _blended_win_rate(get_prediction_history(100))
                thr = c.get('win_rate_threshold', 46)
                if c.get('win_rate_reverse') and blended is not None and blended <= thr:
                    pred_for_calc = '꺽' if pred_for_calc == '정' else '정'
                    bet_color_for_history = flip_pick_color(bet_color_for_history)
                lose_streak = _get_lose_streak_from_history(c.get('history') or [])
                lose_streak_thr = max(0, min(100, int(c.get('lose_streak_reverse_threshold') or 48)))
                lose_streak_min = max(2, min(15, int(c.get('lose_streak_reverse_min_streak') or 3)))
                if c.get('lose_streak_reverse') and lose_streak >= lose_streak_min and blended is not None and blended <= lose_streak_thr:
                    pred_for_calc = '꺽' if pred_for_calc == '정' else '정'
                    bet_color_for_history = flip_pick_color(bet_color_for_history)
                # 승률방향 옵션: 저점→고점 정픽, 고점→저점 반대픽, 정체 시 직전 방향 참조
                if c.get('win_rate_direction_reverse'):
                    ph = get_prediction_history(150)
                    zone = _effective_win_rate_direction_zone(ph, c, pending_round)
                    if zone == 'high_falling':
                        pred_for_calc = '꺽' if pred_for_calc == '정' else '정'
                        bet_color_for_history = flip_pick_color(bet_color_for_history)
                        c['last_trend_direction'] = 'down'
                    elif zone == 'low_rising':
                        c['last_trend_direction'] = 'up'
                    elif zone == 'mid_flat':
                        if c.get('last_trend_direction') == 'down':
                            pred_for_calc = '꺽' if pred_for_calc == '정' else '정'
                            bet_color_for_history = flip_pick_color(bet_color_for_history)
                history_entry = {'round': pending_round, 'predicted': pred_for_calc, 'actual': actual}
                if bet_color_for_history:
                    history_entry['pickColor'] = bet_color_for_history
                # 경고 합산승률 저장
                if blended is not None:
                    history_entry['warningWinRate'] = blended
                # 모양: 유사 덩어리 다음 결과 높은값에만 배팅 — 값 있으면 그 픽으로 배팅, 없으면 no_bet
                if c.get('shape_only_latest_next_pick') and results_for_shape and len(results_for_shape) >= 16:
                    latest_next = _get_chunk_pick_by_higher_stats(results_for_shape)
                    # 연패 방어: shape_only 연패 N회 이상 시 1회 휴식 후 재개 (억지 매칭 연패 방지)
                    skip_due_to_loss_streak = False
                    if latest_next and latest_next in ('정', '꺽') and c.get('shape_only_loss_streak_skip', True):
                        completed = [h for h in (c.get('history') or []) if h.get('actual') and h.get('actual') != 'pending']
                        loss_streak = 0
                        for h in reversed(completed):
                            if h.get('no_bet') or (h.get('betAmount') is not None and h.get('betAmount') == 0):
                                break  # no_bet = 휴식. 연패 끊김 → 다음 회차부터 배팅 재개
                            is_loss = h.get('actual') == 'joker' or h.get('predicted') != h.get('actual')
                            if is_loss:
                                loss_streak += 1
                            else:
                                break
                        if loss_streak >= 2:
                            skip_due_to_loss_streak = True
                    if not latest_next or latest_next not in ('정', '꺽') or skip_due_to_loss_streak:
                        history_entry['no_bet'] = True
                        history_entry['betAmount'] = 0
                    else:
                        history_entry['predicted'] = latest_next
                        is_15_red = get_card_color_from_result(results_for_shape[14]) if len(results_for_shape) >= 15 else None
                        if is_15_red is True:
                            history_entry['pickColor'] = '빨강' if latest_next == '정' else '검정'
                        elif is_15_red is False:
                            history_entry['pickColor'] = '검정' if latest_next == '정' else '빨강'
                        else:
                            history_entry['pickColor'] = '빨강' if latest_next == '정' else '검정'
                # 멈춤 상태 확인 — 마틴 사용 중 연패 구간이면 멈춤 적용 안 함(연패 후 승 다음에만 멈춤)
                paused = c.get('paused', False)
                if paused and c.get('martingale'):
                    completed = [h for h in (c.get('history') or []) if h.get('actual') and h.get('actual') != 'pending']
                    if completed:
                        last = completed[-1]
                        last_is_loss = last.get('actual') == 'joker' or last.get('predicted') != last.get('actual')
                        if last_is_loss:
                            paused = False
                if paused:
                    history_entry['no_bet'] = True
                    history_entry['betAmount'] = 0
                # 같은 회차가 이미 히스토리에 있으면 추가하지 않음 (스케줄러 중복 실행 시 마틴 단계·금액 꼬임 방지)
                existing_rounds = {h.get('round') for h in (c.get('history') or []) if h.get('round') is not None}
                if pending_round in existing_rounds:
                    continue
                # 서버에서 계산기 수익, 마틴게일, 연승/연패 계산
                history_entry = _calculate_calc_profit_server(c, history_entry)
                # 금액 고정: pending_round 정할 때 저장해 둔 금액 사용(DB history 지연으로 마틴 단계 어긋남 방지)
                stored_amt = c.get('pending_bet_amount')
                if stored_amt is not None and not history_entry.get('no_bet') and history_entry.get('actual') and history_entry.get('actual') != 'pending':
                    try:
                        amt = int(stored_amt)
                        if amt >= 0:
                            history_entry['betAmount'] = amt
                            odds_val = float(c.get('odds', 1.97))
                            act = history_entry.get('actual')
                            pred = history_entry.get('predicted')
                            if act == 'joker':
                                history_entry['profit'] = -amt
                            elif pred == act:
                                history_entry['profit'] = int(amt * (odds_val - 1))
                            else:
                                history_entry['profit'] = -amt
                    except (TypeError, ValueError):
                        pass
                c['history'] = (c.get('history') or []) + [history_entry]
                # 해당 회차 완료 시점의 계산기 15회 승률 저장 (표 15회승률 열용)
                completed_new = [x for x in c['history'] if x.get('actual') and x.get('actual') != 'pending']
                history_entry['rate15'] = round(_server_recent_15_win_rate(completed_new), 1)
                # 최대 연승/연패 업데이트
                max_win = history_entry.get('max_win_streak', 0)
                max_lose = history_entry.get('max_lose_streak', 0)
                c['max_win_streak_ever'] = max(c.get('max_win_streak_ever', 0), max_win)
                c['max_lose_streak_ever'] = max(c.get('max_lose_streak_ever', 0), max_lose)
                # 서버에서 멈춤 상태 갱신(15회 승률·연패후승). 클라이언트 꺼져 있어도 멈춤 정확 동작
                _update_calc_paused_after_round(c)
                if stored_for_round and stored_for_round.get('predicted'):
                    c['pending_round'] = predicted_round
                    c['pending_predicted'] = stored_for_round['predicted']
                    c['pending_prob'] = stored_for_round.get('probability')
                    c['pending_color'] = stored_for_round.get('pick_color')
                    _, next_amt = _server_calc_effective_pick_and_amount(c)
                    c['pending_bet_amount'] = next_amt if next_amt is not None and next_amt > 0 else None
                    updated = True
                    to_push.append((int(cid), c))
            if updated:
                save_calc_state(session_id, state)  # 먼저 저장 → POST /api/current-pick 시 get_calc_state가 새 상태를 읽어 금액 보정 가능
                for calc_id, calc_c in to_push:
                    _push_current_pick_from_calc(calc_id, calc_c)
    except Exception as e:
        print(f"[스케줄러] 회차 반영 오류: {str(e)[:200]}")


def _server_calc_effective_pick_and_amount(c):
    """계산기 c의 pending_round 기준으로 배팅 픽(RED/BLACK)과 금액 계산. 매크로 current_pick 반영용."""
    from app import (
        get_prediction_history, _get_main_recent15_win_rate,
        _get_current_result_run_length, _get_lose_streak_from_history,
        _effective_win_rate_direction_zone, results_cache,
    )
    if not c or not c.get('running'):
        return None, 0
    pr = c.get('pending_round')
    pred = c.get('pending_predicted')
    if pr is None or pred is None:
        return None, 0
    color = normalize_pick_color_value(c.get('pending_color'))
    if color is None:
        color = '빨강' if pred == '정' else '검정'
    ph = get_prediction_history(150)
    main_rate15 = _get_main_recent15_win_rate(ph)
    run_length = _get_current_result_run_length(ph)
    streak_suppress = bool(c.get('streak_suppress_reverse', False))
    # 줄 5 이상(5연승/5연패)일 때 반픽 억제
    no_reverse_in_streak = streak_suppress and run_length >= 5
    # 반픽/승률반픽/연패반픽/승률방향 반픽 적용 (클라이언트와 동일). 메인 15경기 승률 좋으면(53% 이상) 반픽 억제.
    if c.get('reverse'):
        pred = '꺽' if pred == '정' else '정'
        color = flip_pick_color(color)
    blended = _blended_win_rate(ph or get_prediction_history(100))
    thr = c.get('win_rate_threshold', 46)
    if c.get('win_rate_reverse') and blended is not None and blended <= thr and not no_reverse_in_streak and (main_rate15 is None or main_rate15 < 53):
        pred = '꺽' if pred == '정' else '정'
        color = flip_pick_color(color)
    lose_streak = _get_lose_streak_from_history(c.get('history') or [])
    lose_streak_thr = max(0, min(100, int(c.get('lose_streak_reverse_threshold') or 48)))
    lose_streak_min = max(2, min(15, int(c.get('lose_streak_reverse_min_streak') or 3)))
    if c.get('lose_streak_reverse') and lose_streak >= lose_streak_min and blended is not None and blended <= lose_streak_thr and not no_reverse_in_streak and (main_rate15 is None or main_rate15 < 53):
        pred = '꺽' if pred == '정' else '정'
        color = flip_pick_color(color)
    if c.get('win_rate_direction_reverse') and not no_reverse_in_streak:
        current_round = ph[-1]['round'] if ph else None
        zone = _effective_win_rate_direction_zone(ph, c, current_round)
        if zone == 'high_falling':
            pred = '꺽' if pred == '정' else '정'
            color = flip_pick_color(color)
            c['last_trend_direction'] = 'down'
        elif zone == 'low_rising':
            c['last_trend_direction'] = 'up'
        elif zone == 'mid_flat' and c.get('last_trend_direction') == 'down':
            pred = '꺽' if pred == '정' else '정'
            color = flip_pick_color(color)
    pick_color = 'RED' if color == '빨강' else ('BLACK' if color == '검정' else None)
    if pick_color is None:
        return None, 0
    # 모양: 유사 덩어리 다음 결과 높은값에만 배팅. 값 있으면 그 픽으로 배팅, 없으면 배팅 안 함
    if c.get('shape_only_latest_next_pick'):
        results = None
        try:
            results = (results_cache or {}).get('results') if results_cache else None
            if not results or len(results) < 16:
                results = get_recent_results(hours=24)
                if results:
                    results = sort_results_newest_first(results)
        except Exception:
            results = None
        if not results or len(results) < 16:
            return None, 0
        latest_next = _get_chunk_pick_by_higher_stats(results)
        # 연패 방어: shape_only 연패 2회 이상 시 1회 휴식 후 재개
        if latest_next and latest_next in ('정', '꺽') and c.get('shape_only_loss_streak_skip', True):
            completed = [h for h in (c.get('history') or []) if h.get('actual') and h.get('actual') != 'pending']
            loss_streak = 0
            for h in reversed(completed):
                if h.get('no_bet') or (h.get('betAmount') is not None and h.get('betAmount') == 0):
                    break  # no_bet = 휴식. 연패 끊김 → 다음 회차부터 배팅 재개
                if h.get('actual') == 'joker' or h.get('predicted') != h.get('actual'):
                    loss_streak += 1
                else:
                    break
            if loss_streak >= 2:
                latest_next = None
        if not latest_next or latest_next not in ('정', '꺽'):
            return None, 0
        pred = latest_next
        is_15_red = get_card_color_from_result(results[14]) if len(results) >= 15 else None
        if is_15_red is True:
            color, pick_color = ('빨강', 'RED') if latest_next == '정' else ('검정', 'BLACK')
        elif is_15_red is False:
            color, pick_color = ('검정', 'BLACK') if latest_next == '정' else ('빨강', 'RED')
        else:
            color, pick_color = ('빨강', 'RED') if latest_next == '정' else ('검정', 'BLACK')
    if c.get('paused'):
        return pick_color, 0
    dummy = {'round': pr, 'actual': 'pending'}
    _calculate_calc_profit_server(c, dummy)
    amt = int(dummy.get('betAmount') or 0)
    return pick_color, amt


