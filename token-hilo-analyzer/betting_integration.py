# -*- coding: utf-8 -*-
"""
배팅 연동 모듈
- 현재 예측 픽(RED/BLACK)과 회차·확률을 저장/조회합니다.
- 외부 도구(확장 프로그램 등)가 GET /api/current-pick 으로 조회할 수 있습니다.
- 규칙: 기존 로딩·표시·정/꺽 순서에는 관여하지 않습니다.
"""

CURRENT_PICK_ROW_ID = 1


def get_current_pick(conn):
    """
    DB에서 현재 예측 픽 1건 조회.
    conn: psycopg2 연결 (호출자가 열고 닫음)
    반환: dict 또는 None
      { 'pick_color': 'RED'|'BLACK', 'round': int, 'probability': float,
        'suggested_amount': int|None, 'updated_at': str (ISO) }
    """
    if conn is None:
        return None
    try:
        cur = conn.cursor()
        cur.execute('''
            SELECT pick_color, round_num, probability, suggested_amount, updated_at
            FROM current_pick WHERE id = %s
        ''', (CURRENT_PICK_ROW_ID,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        return {
            'pick_color': row[0],
            'round': row[1],
            'probability': float(row[2]) if row[2] is not None else None,
            'suggested_amount': row[3],
            'updated_at': row[4].isoformat() if row[4] else None,
        }
    except Exception:
        return None


def set_current_pick(conn, pick_color=None, round_num=None, probability=None, suggested_amount=None):
    """
    DB에 현재 예측 픽 1건 저장 (id=1 행 upsert).
    conn: psycopg2 연결 (호출자가 commit/close)
    pick_color: 'RED' | 'BLACK' | None (보류 시 None)
    round_num: 다음 회차 번호
    probability: 확률 0~100
    suggested_amount: 권장 배팅 금액 (선택)
    반환: True 성공, False 실패
    """
    if conn is None:
        return False
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
            CURRENT_PICK_ROW_ID,
            pick_color,
            int(round_num) if round_num is not None else None,
            float(probability) if probability is not None else None,
            int(suggested_amount) if suggested_amount is not None else None,
        ))
        cur.close()
        return True
    except Exception:
        return False
