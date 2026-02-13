# -*- coding: utf-8 -*-
"""
배팅 연동 모듈
- 현재 예측 픽(RED/BLACK)과 회차·확률을 저장/조회합니다.
- 외부 도구(확장 프로그램 등)가 GET /api/current-pick 으로 조회할 수 있습니다.
- 규칙: 기존 로딩·표시·정/꺽 순서에는 관여하지 않습니다.
"""

# 계산기 1,2,3 각각 id=1, id=2, id=3
CALC_IDS = (1, 2, 3)


def get_current_pick(conn, calculator_id=1):
    """
    DB에서 해당 계산기의 현재 예측 픽 1건 조회.
    conn: psycopg2 연결 (호출자가 열고 닫음)
    calculator_id: 1 | 2 | 3 (계산기 번호)
    반환: dict 또는 None
      { 'pick_color': 'RED'|'BLACK', 'round': int, 'probability': float,
        'suggested_amount': int|None, 'updated_at': str (ISO), 'running': bool }
    """
    if conn is None:
        return None
    calc_id = int(calculator_id) if calculator_id in (1, 2, 3) else 1
    try:
        cur = conn.cursor()
        try:
            cur.execute('''
                SELECT pick_color, round_num, probability, suggested_amount, updated_at, running
                FROM current_pick WHERE id = %s
            ''', (calc_id,))
            has_running = True
        except Exception:
            cur.execute('''
                SELECT pick_color, round_num, probability, suggested_amount, updated_at
                FROM current_pick WHERE id = %s
            ''', (calc_id,))
            has_running = False
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        out = {
            'pick_color': row[0],
            'round': row[1],
            'probability': float(row[2]) if row[2] is not None else None,
            'suggested_amount': row[3],
            'updated_at': row[4].isoformat() if row[4] else None,
        }
        out['running'] = bool(row[5]) if has_running and len(row) > 5 else True
        return out
    except Exception:
        return None


def set_calculator_running(conn, calculator_id, running):
    """해당 계산기의 running 플래그만 갱신 (목표 달성 등으로 계산기 중지 시 매크로 연동용)."""
    if conn is None:
        return False
    calc_id = int(calculator_id) if calculator_id in (1, 2, 3) else 1
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'current_pick' AND column_name = 'running'")
        if cur.fetchone() is None:
            cur.close()
            return False
        cur.execute('UPDATE current_pick SET running = %s WHERE id = %s', (bool(running), calc_id))
        cur.close()
        return True
    except Exception:
        return False


def set_current_pick_pick_only(conn, pick_color=None, round_num=None, probability=None, calculator_id=1):
    """
    픽/회차/확률만 갱신. suggested_amount는 건드리지 않음.
    — 매크로 금액은 오직 클라이언트(계산기 상단 배팅중)에서만 설정. 서버는 덮어쓰지 않음.
    """
    if conn is None:
        return False
    calc_id = int(calculator_id) if calculator_id in (1, 2, 3) else 1
    try:
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO current_pick (id, pick_color, round_num, probability, suggested_amount, updated_at)
            VALUES (%s, %s, %s, %s, NULL, CURRENT_TIMESTAMP)
            ON CONFLICT (id) DO UPDATE SET
                pick_color = EXCLUDED.pick_color,
                round_num = EXCLUDED.round_num,
                probability = EXCLUDED.probability,
                updated_at = CURRENT_TIMESTAMP
        ''', (calc_id, pick_color, int(round_num) if round_num is not None else None,
              float(probability) if probability is not None else None))
        cur.close()
        return True
    except Exception:
        return False


def set_current_pick(conn, pick_color=None, round_num=None, probability=None, suggested_amount=None, calculator_id=1):
    """
    DB에 해당 계산기의 현재 예측 픽 1건 저장 (id=calculator_id 행 upsert).
    conn: psycopg2 연결 (호출자가 commit/close)
    calculator_id: 1 | 2 | 3
    pick_color: 'RED' | 'BLACK' | None (보류 시 None)
    round_num: 다음 회차 번호
    probability: 확률 0~100
    suggested_amount: 권장 배팅 금액 (선택)
    반환: True 성공, False 실패
    """
    if conn is None:
        return False
    calc_id = int(calculator_id) if calculator_id in (1, 2, 3) else 1
    try:
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO current_pick (id, pick_color, round_num, probability, suggested_amount, updated_at)
            VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (id) DO UPDATE SET
                pick_color = EXCLUDED.pick_color,
                round_num = EXCLUDED.round_num,
                probability = EXCLUDED.probability,
                suggested_amount = EXCLUDED.suggested_amount,
                updated_at = CURRENT_TIMESTAMP
        ''', (
            calc_id,
            pick_color,
            int(round_num) if round_num is not None else None,
            float(probability) if probability is not None else None,
            int(suggested_amount) if suggested_amount is not None else None,
        ))
        cur.close()
        return True
    except Exception:
        return False
