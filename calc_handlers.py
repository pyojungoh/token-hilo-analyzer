# -*- coding: utf-8 -*-
"""
계산기 전용 핸들러. app.py에서 단계적으로 옮김 (OOM 방지).

Public: api_calc_state (GET/POST /api/calc-state).
Internal (다른 모듈에서 import): _merge_calc_histories, _get_all_calc_session_ids.
"""
import time
from flask import request, jsonify
from config import DATABASE_URL
from db import get_calc_state, save_calc_state, get_db_connection, _calc_state_memory, DB_AVAILABLE

def _merge_calc_histories(client_hist, server_hist):
    """회차별 병합: 서버 행 기준. 단 클라이언트가 해당 회차 픽(정/꺽)을 보냈으면 1열 저장값으로 간주해 픽만 클라이언트 유지 → 승패만 actual 기준."""
    by_round = {}
    for h in (server_hist or []):
        if not isinstance(h, dict):
            continue
        rn = h.get('round')
        if rn is not None:
            by_round[rn] = dict(h)
            if (by_round[rn].get('no_bet') or (by_round[rn].get('betAmount') is not None and by_round[rn].get('betAmount') == 0)):
                by_round[rn]['no_bet'] = True
                by_round[rn]['betAmount'] = 0
    client_pick_by_round = {}
    for h in (client_hist or []):
        if not isinstance(h, dict) or h.get('round') is None:
            continue
        pred = h.get('predicted')
        if pred in ('정', '꺽'):
            rn = h.get('round')
            cb = h.get('betAmount')
            client_pick_by_round[rn] = {'predicted': pred, 'pickColor': h.get('pickColor') or h.get('pick_color'), 'no_bet': h.get('no_bet'), 'betAmount': cb}
    for h in (client_hist or []):
        if not isinstance(h, dict):
            continue
        rn = h.get('round')
        if rn is not None and rn not in by_round:
            by_round[rn] = dict(h)
            if by_round[rn].get('no_bet') or (by_round[rn].get('betAmount') is not None and by_round[rn].get('betAmount') == 0):
                by_round[rn]['no_bet'] = True
                by_round[rn]['betAmount'] = 0
    for rn, pick in client_pick_by_round.items():
        if rn in by_round and pick.get('predicted') in ('정', '꺽'):
            by_round[rn]['predicted'] = pick['predicted']
            if pick.get('pickColor') is not None:
                by_round[rn]['pickColor'] = pick['pickColor']
            # shape_only: 클라이언트가 배팅했다고 판단(정/꺽 픽 + no_bet false 또는 betAmount>0)이면 서버 no_bet 덮어씀
            cb = pick.get('betAmount')
            cb_positive = cb is not None and (isinstance(cb, (int, float)) and cb > 0)
            if pick.get('no_bet') is False or cb_positive:
                by_round[rn]['no_bet'] = False
    try:
        rounds = sorted(by_round.keys(), key=lambda x: (x if isinstance(x, (int, float)) else 0))
    except (TypeError, ValueError):
        rounds = sorted(by_round.keys())
    return [by_round[r] for r in rounds]



def _get_all_calc_session_ids():
    """실행 중인 계산기 세션 ID 목록 (회차 반영용). DB 사용 시 calc_sessions 전체, 미사용 시 메모리 키."""
    if DB_AVAILABLE and DATABASE_URL:
        conn = get_db_connection(statement_timeout_sec=5)
        if not conn:
            return list(_calc_state_memory.keys())
        try:
            cur = conn.cursor()
            cur.execute('SELECT session_id FROM calc_sessions')
            rows = cur.fetchall()
            cur.close()
            conn.close()
            return [r[0] for r in rows] if rows else []
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            return list(_calc_state_memory.keys())
    return list(_calc_state_memory.keys())



def api_calc_state():
    """GET: 계산기 상태 조회. session_id 없으면 새로 생성. POST: 계산기 상태 저장. running=true이고 started_at 없으면 서버가 started_at 설정."""
    try:
        import betting_integration as bet_int
    except Exception:
        bet_int = None
    try:
        server_time = int(time.time())
        if request.method == 'GET':
            # session_id 없으면 공용 세션 'default' 사용 → 모바일/PC/다른 기기에서 열어도 같은 진행 중 계산기 상태 표시
            session_id = request.args.get('session_id', '').strip() or None
            if not session_id:
                session_id = 'default'
            state = get_calc_state(session_id)
            if state is None and session_id == 'default':
                save_calc_state(session_id, {})
                state = {}
            if state is None:
                state = {}
            # 계산기 1,2,3만 반환 (레거시 defense 제거 후 클라이언트 호환)
            _default = {'running': False, 'started_at': 0, 'history': [], 'capital': 1000000, 'base': 10000, 'odds': 1.97, 'duration_limit': 0, 'use_duration_limit': False, 'reverse': False, 'timer_completed': False, 'win_rate_reverse': False, 'win_rate_threshold': 46, 'lose_streak_reverse': False, 'lose_streak_reverse_threshold': 48, 'lose_streak_reverse_min_streak': 3, 'win_rate_direction_reverse': False, 'streak_suppress_reverse': False, 'shape_only_latest_next_pick': False, 'last_trend_direction': None, 'martingale': False, 'martingale_type': 'pyo', 'target_enabled': False, 'target_amount': 0, 'pause_low_win_rate_enabled': False, 'pause_win_rate_threshold': 45, 'paused': False, 'max_win_streak_ever': 0, 'max_lose_streak_ever': 0, 'first_bet_round': 0, 'pending_round': None, 'pending_predicted': None, 'pending_prob': None, 'pending_color': None, 'pending_bet_amount': None}
            calcs = {}
            for cid in ('1', '2', '3'):
                calcs[cid] = state[cid] if (cid in state and isinstance(state.get(cid), dict)) else dict(_default)
            # no_bet ↔ betAmount 0 한 쌍 유지. betAmount 없으면 no_bet 덮어쓰지 않음(배팅했던 회차가 멈춤으로 복원되는 버그 방지)
            for cid in ('1', '2', '3'):
                hist = calcs[cid].get('history') if isinstance(calcs[cid].get('history'), list) else []
                for ent in hist:
                    if not isinstance(ent, dict):
                        continue
                    if ent.get('no_bet') is True:
                        ent['betAmount'] = 0
                    elif ent.get('betAmount') is not None and ent.get('betAmount') == 0:
                        ent['no_bet'] = True
            return jsonify({'session_id': session_id, 'server_time': server_time, 'calcs': calcs}), 200
        # POST
        data = request.get_json(force=True, silent=True) or {}
        session_id = (data.get('session_id') or '').strip()
        if not session_id:
            session_id = 'default'
        calcs = data.get('calcs') or {}
        # 순익계산기 안정화: 서버에 저장된 history가 더 길면 유지 (클라이언트 덮어쓰기로 누락 방지)
        current_state = get_calc_state(session_id) or {}
        out = {}
        for cid in ('1', '2', '3'):
            c = calcs.get(cid) or {}
            if isinstance(c, dict):
                running = c.get('running', False)
                started_at = c.get('started_at') or 0
                if running and not started_at:
                    started_at = server_time
                client_history = c.get('history') if isinstance(c.get('history'), list) else []
                current_c = current_state.get(cid) if isinstance(current_state.get(cid), dict) else {}
                current_history = current_c.get('history') if isinstance(current_c.get('history'), list) else []
                # 계산기 정지 시 클라이언트가 history=[]로 보내면 기록 전체 삭제 — 이때는 병합하지 않고 빈 배열 저장
                if len(client_history) == 0:
                    use_history = []
                else:
                    # 회차별 병합: 클라이언트 행 우선(no_bet/betAmount 유지). 새로고침 후에도 멈춤·배팅 구간 정확히 복원
                    use_history = _merge_calc_histories(client_history, current_history)
                    use_history = use_history[-50000:] if len(use_history) > 50000 else use_history
                for ent in use_history:
                    if not isinstance(ent, dict):
                        continue
                    if ent.get('no_bet') is True:
                        ent['betAmount'] = 0
                    elif ent.get('betAmount') is not None and ent.get('betAmount') == 0:
                        ent['no_bet'] = True
                try:
                    cap = int(float(c.get('capital', 1000000))) if c.get('capital') is not None else 1000000
                except (TypeError, ValueError):
                    cap = 1000000
                cap = 1000000 if cap < 0 else cap
                try:
                    base = int(float(c.get('base', 10000))) if c.get('base') is not None else 10000
                except (TypeError, ValueError):
                    base = 10000
                base = 10000 if base < 1 else base
                try:
                    odds_val = float(c.get('odds', 1.97)) if c.get('odds') is not None else 1.97
                except (TypeError, ValueError):
                    odds_val = 1.97
                odds_val = 1.97 if odds_val < 1 else odds_val
                out[cid] = {
                    'running': running,
                    'started_at': started_at,
                    'history': use_history,
                    'capital': cap,
                    'base': base,
                    'odds': odds_val,
                    'duration_limit': int(c.get('duration_limit') or 0),
                    'use_duration_limit': bool(c.get('use_duration_limit')),
                    'reverse': bool(c.get('reverse')),
                    'timer_completed': bool(c.get('timer_completed')),
                    'win_rate_reverse': bool(c.get('win_rate_reverse')),
                    'win_rate_threshold': max(0, min(100, int(c.get('win_rate_threshold') or 46))),
                    'lose_streak_reverse': bool(c.get('lose_streak_reverse')),
                    'lose_streak_reverse_threshold': max(0, min(100, int(c.get('lose_streak_reverse_threshold') or 48))),
                    'lose_streak_reverse_min_streak': max(2, min(15, int(c.get('lose_streak_reverse_min_streak') or 3))),
                    'win_rate_direction_reverse': bool(c.get('win_rate_direction_reverse')),
                    'streak_suppress_reverse': bool(c.get('streak_suppress_reverse')),
                    'lock_direction_on_lose_streak': bool(c.get('lock_direction_on_lose_streak', True)),
                    'shape_only_latest_next_pick': bool(c.get('shape_only_latest_next_pick')),
                    'shape_only_loss_streak_skip': bool(c.get('shape_only_loss_streak_skip', True)),
                    'last_trend_direction': c.get('last_trend_direction') if c.get('last_trend_direction') in ('up', 'down') else None,
                    'martingale': bool(c.get('martingale')),
                    'martingale_type': str(c.get('martingale_type') or 'pyo'),
                    'target_enabled': bool(c.get('target_enabled')),
                    'target_amount': max(0, int(c.get('target_amount') or 0)),
                    'pause_low_win_rate_enabled': bool(c.get('pause_low_win_rate_enabled')),
                    'pause_win_rate_threshold': max(0, min(100, int(c.get('pause_win_rate_threshold') or 45))),
                    'paused': bool(c.get('paused')),
                    'max_win_streak_ever': int(c.get('max_win_streak_ever') or 0),
                    'max_lose_streak_ever': int(c.get('max_lose_streak_ever') or 0),
                    'first_bet_round': max(0, int(c.get('first_bet_round') or 0)),
                    'pending_round': c.get('pending_round'),
                    'pending_predicted': c.get('pending_predicted'),
                    'pending_prob': c.get('pending_prob'),
                    'pending_color': c.get('pending_color'),
                    'pending_bet_amount': current_c.get('pending_bet_amount') if current_c.get('pending_bet_amount') is not None else c.get('pending_bet_amount'),
                    'last_win_rate_zone': c.get('last_win_rate_zone') or current_c.get('last_win_rate_zone'),
                    'last_win_rate_zone_change_round': c.get('last_win_rate_zone_change_round') if c.get('last_win_rate_zone_change_round') is not None else current_c.get('last_win_rate_zone_change_round'),
                    'last_win_rate_zone_on_win': c.get('last_win_rate_zone_on_win') or current_c.get('last_win_rate_zone_on_win'),
                }
            else:
                out[cid] = {'running': False, 'started_at': 0, 'history': [], 'capital': 1000000, 'base': 10000, 'odds': 1.97, 'duration_limit': 0, 'use_duration_limit': False, 'reverse': False, 'timer_completed': False, 'win_rate_reverse': False, 'win_rate_threshold': 46, 'lose_streak_reverse': False, 'lose_streak_reverse_threshold': 48, 'lose_streak_reverse_min_streak': 3, 'win_rate_direction_reverse': False, 'streak_suppress_reverse': False, 'lock_direction_on_lose_streak': True, 'shape_only_latest_next_pick': False, 'last_trend_direction': None, 'martingale': False, 'martingale_type': 'pyo', 'target_enabled': False, 'target_amount': 0, 'pause_low_win_rate_enabled': False, 'pause_win_rate_threshold': 45, 'paused': False, 'max_win_streak_ever': 0, 'max_lose_streak_ever': 0, 'first_bet_round': 0, 'pending_round': None, 'pending_predicted': None, 'pending_prob': None, 'pending_color': None, 'pending_bet_amount': None, 'last_win_rate_zone': None, 'last_win_rate_zone_change_round': None, 'last_win_rate_zone_on_win': None}
        save_calc_state(session_id, out)
        # 계산기 running 상태를 current_pick에 반영 → 에뮬레이터 매크로가 목표 달성 시 자동 중지
        if bet_int:
            conn = get_db_connection(statement_timeout_sec=3)
            if conn:
                try:
                    for cid in ('1', '2', '3'):
                        if cid in out and isinstance(out[cid], dict):
                            bet_int.set_calculator_running(conn, int(cid), out[cid].get('running', True))
                    conn.commit()
                except Exception:
                    conn.rollback()
                finally:
                    conn.close()
        return jsonify({'session_id': session_id, 'server_time': server_time, 'calcs': out}), 200
    except Exception as e:
        # 계산기 실행이 서버 오류로 실패해도 클라이언트가 실행 상태 유지할 수 있도록 요청 body 기준으로 fallback 응답 반환
        server_time = int(time.time())
        session_id = ((request.get_json(force=True, silent=True) or {}).get('session_id') or '').strip() or 'default'
        calcs = (request.get_json(force=True, silent=True) or {}).get('calcs') or {}
        out_fallback = {}
        _default = {'running': False, 'started_at': 0, 'history': [], 'capital': 1000000, 'base': 10000, 'odds': 1.97, 'duration_limit': 0, 'use_duration_limit': False, 'reverse': False, 'timer_completed': False, 'win_rate_reverse': False, 'win_rate_threshold': 46, 'lose_streak_reverse': False, 'lose_streak_reverse_threshold': 48, 'lose_streak_reverse_min_streak': 3, 'win_rate_direction_reverse': False, 'streak_suppress_reverse': False, 'shape_only_latest_next_pick': False, 'last_trend_direction': None, 'martingale': False, 'martingale_type': 'pyo', 'target_enabled': False, 'target_amount': 0, 'pause_low_win_rate_enabled': False, 'pause_win_rate_threshold': 45, 'paused': False, 'max_win_streak_ever': 0, 'max_lose_streak_ever': 0, 'first_bet_round': 0, 'pending_round': None, 'pending_predicted': None, 'pending_prob': None, 'pending_color': None, 'pending_bet_amount': None, 'last_win_rate_zone': None, 'last_win_rate_zone_change_round': None}
        for cid in ('1', '2', '3'):
            c = calcs.get(cid) or {}
            if isinstance(c, dict):
                running = c.get('running', False)
                started_at = c.get('started_at') or 0
                if running and not started_at:
                    started_at = server_time
                out_fallback[cid] = dict(_default)
                out_fallback[cid].update({k: v for k, v in c.items() if v is not None})
                out_fallback[cid]['running'] = running
                out_fallback[cid]['started_at'] = started_at
            else:
                out_fallback[cid] = dict(_default)
        try:
            save_calc_state(session_id, out_fallback)
        except Exception:
            pass
        print(f"[❌ 오류] 계산기 상태 POST 예외: {str(e)[:200]}")
        return jsonify({'error': str(e)[:200], 'session_id': session_id, 'server_time': server_time, 'calcs': out_fallback}), 200


