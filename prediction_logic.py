# -*- coding: utf-8 -*-
"""
4단계 모듈화: 계산·예측 로직 (Flask/request/DB 의존 없음).
"""
# 완료: _blended_win_rate, _blended_win_rate_components, _server_recent_15_win_rate, _update_calc_paused_after_round, _calculate_calc_profit_server
from utils import round_eq


def _server_recent_15_win_rate(completed_list):
    """완료된 회차 리스트에서 최근 15회 승률(%). 조커=패."""
    if not completed_list:
        return 50.0
    last15 = completed_list[-15:] if len(completed_list) >= 15 else completed_list
    wins = sum(1 for h in last15 if h.get('actual') != 'joker' and h.get('predicted') == h.get('actual'))
    return (wins / len(last15)) * 100.0 if last15 else 50.0


def _update_calc_paused_after_round(c):
    """회차 반영 후 서버에서 paused 갱신. 멈춤 기준 = 계산기 표 15회 승률."""
    history = c.get('history') or []
    completed = [h for h in history if h.get('actual') and h.get('actual') != 'pending']
    pause_enabled = c.get('pause_low_win_rate_enabled', False)
    thr = max(0, min(100, int(c.get('pause_win_rate_threshold') or 45)))
    if len(completed) < 1:
        c['paused'] = False
        return
    if pause_enabled:
        martingale = c.get('martingale', False)
        last_is_loss = False
        if martingale and len(completed) >= 1:
            last_h = completed[-1]
            last_is_loss = last_h.get('actual') == 'joker' or last_h.get('predicted') != last_h.get('actual')
        if not last_is_loss:
            rate15 = _server_recent_15_win_rate(completed)
            PAUSE_RESUME_HYSTERESIS = 3
            resume_thr = min(100, thr + PAUSE_RESUME_HYSTERESIS)
            if c.get('paused', False):
                c['paused'] = rate15 <= resume_thr
            else:
                c['paused'] = rate15 <= thr
    return


def _calculate_calc_profit_server(calc_state, history_entry):
    """서버에서 계산기 수익, 마틴게일 단계, 연승/연패 계산. history_entry에 계산된 값 추가."""
    MARTIN_PYO_RATIOS = [1, 1.5, 2.5, 4, 7, 12, 20, 40, 40]
    MARTIN_PYO2_RATIOS = [1, 2, 3, 5, 8, 14, 24, 40, 90, 160, 200]
    capital = float(calc_state.get('capital', 1000000))
    base = float(calc_state.get('base', 10000))
    odds = float(calc_state.get('odds', 1.97))
    martingale = bool(calc_state.get('martingale', False))
    martingale_type = calc_state.get('martingale_type', 'pyo')
    history = calc_state.get('history', [])
    entry_round = history_entry.get('round')
    completed_list = [h for h in history if h.get('actual') and h.get('actual') != 'pending' and not round_eq(h.get('round'), entry_round)]
    by_round = {}
    for h in completed_list:
        rn = h.get('round')
        if rn is not None:
            try:
                by_round[int(rn)] = h
            except (TypeError, ValueError):
                by_round[rn] = h
    def _round_sort_key(x):
        r = x.get('round')
        try:
            return (0, int(r)) if r is not None else (1, 0)
        except (TypeError, ValueError):
            return (1, 0)
    completed_history = sorted(by_round.values(), key=_round_sort_key)
    martingale_step = 0
    cap = capital
    current_bet = base
    ratios = MARTIN_PYO2_RATIOS if martingale_type == 'pyo2' else MARTIN_PYO_RATIOS
    martin_table = [round(base * r) for r in ratios]
    if martingale_type == 'pyo_half':
        martin_table = [round(x / 2) for x in martin_table]
    for h in completed_history:
        if h.get('no_bet') or (h.get('betAmount') == 0):
            continue
        if martingale and martingale_type in ('pyo', 'pyo_half', 'pyo2'):
            current_bet = martin_table[min(martingale_step, len(martin_table) - 1)]
        else:
            current_bet = min(current_bet, int(cap))
        bet = min(current_bet, int(cap))
        if cap < bet or cap <= 0:
            break
        actual = h.get('actual')
        predicted = h.get('predicted')
        is_joker = actual == 'joker'
        is_win = not is_joker and predicted == actual
        if is_joker:
            cap -= bet
            if martingale and martingale_type in ('pyo', 'pyo_half', 'pyo2'):
                martingale_step = min(martingale_step + 1, len(martin_table) - 1)
            else:
                current_bet = min(current_bet * 2, int(cap))
        elif is_win:
            cap += bet * (odds - 1)
            if martingale and martingale_type in ('pyo', 'pyo_half', 'pyo2'):
                martingale_step = 0
            else:
                current_bet = base
        else:
            cap -= bet
            if martingale and martingale_type in ('pyo', 'pyo_half', 'pyo2'):
                martingale_step = min(martingale_step + 1, len(martin_table) - 1)
            else:
                current_bet = min(current_bet * 2, int(cap))
        if cap <= 0:
            break
    if martingale and martingale_type in ('pyo', 'pyo_half', 'pyo2'):
        current_bet = martin_table[min(martingale_step, len(martin_table) - 1)]
    else:
        current_bet = min(current_bet, int(cap))
    bet_amount = min(current_bet, int(cap)) if not history_entry.get('no_bet') and history_entry.get('betAmount') != 0 else 0
    actual = history_entry.get('actual')
    predicted = history_entry.get('predicted')
    is_joker = actual == 'joker'
    is_win = not is_joker and predicted == actual
    if history_entry.get('no_bet') or bet_amount == 0:
        profit = 0
    elif is_joker:
        profit = -bet_amount
    elif is_win:
        profit = int(bet_amount * (odds - 1))
    else:
        profit = -bet_amount
    max_win_streak = 0
    max_lose_streak = 0
    cur_win = 0
    cur_lose = 0
    all_completed = completed_history + [history_entry]
    for h in all_completed:
        if h.get('no_bet') or (h.get('betAmount') == 0):
            continue
        a = h.get('actual')
        p = h.get('predicted')
        if a == 'joker':
            cur_win = 0
            cur_lose = 0
        elif p == a:
            cur_win += 1
            cur_lose = 0
            max_win_streak = max(max_win_streak, cur_win)
        else:
            cur_lose += 1
            cur_win = 0
            max_lose_streak = max(max_lose_streak, cur_lose)
    history_entry['betAmount'] = bet_amount
    history_entry['profit'] = profit
    history_entry['capital_after'] = max(0, int(cap + profit))
    history_entry['martingale_step'] = martingale_step
    history_entry['max_win_streak'] = max_win_streak
    history_entry['max_lose_streak'] = max_lose_streak
    return history_entry


def _blended_win_rate_components(prediction_history):
    """예측 이력으로 15/30/100 승률 및 합산. (r15, r30, r100, blended). 가중치: 15회 65%, 30회 25%, 100회 10%."""
    valid_hist = [h for h in (prediction_history or []) if h and isinstance(h, dict)]
    if not valid_hist:
        return None
    v15 = [h for h in valid_hist[-15:] if h.get('actual') != 'joker']
    v30 = [h for h in valid_hist[-30:] if h.get('actual') != 'joker']
    v100 = [h for h in valid_hist[-100:] if h.get('actual') != 'joker']
    def rate(arr):
        hit = sum(1 for h in arr if h.get('predicted') == h.get('actual'))
        return 100 * hit / len(arr) if arr else 50
    r15 = rate(v15)
    r30 = rate(v30)
    r100 = rate(v100)
    blended = 0.65 * r15 + 0.25 * r30 + 0.10 * r100
    return (r15, r30, r100, blended)


def _blended_win_rate(prediction_history):
    """예측 이력으로 15/30/100 가중 승률. (0.6*15 + 0.25*30 + 0.15*100).
    프론트엔드와 동일: 위치 기준 마지막 N개에서 조커 제외 후 승률 계산."""
    comp = _blended_win_rate_components(prediction_history)
    return comp[3] if comp else None
