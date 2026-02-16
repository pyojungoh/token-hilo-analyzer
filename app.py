"""
토큰하이로우 분석기 - Railway 서버
필요한 정보만 추출하여 새로 작성
"""

from flask import Flask, jsonify, render_template_string, render_template, request, redirect
from flask_cors import CORS
import requests
import os
from datetime import datetime
import time
import json
import traceback
import threading
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

# .env 파일 로드 (DATABASE_URL 등)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    DB_AVAILABLE = True
    print("[✅] psycopg2 라이브러리 로드 성공")
except ImportError as e:
    DB_AVAILABLE = False
    print(f"[❌ 경고] psycopg2가 설치되지 않았습니다: {e}")
    print("[❌ 경고] pip install psycopg2-binary로 설치하세요")

try:
    import betting_integration as bet_int
except ImportError:
    bet_int = None

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    SCHEDULER_AVAILABLE = True
    import logging
    for _name in ('apscheduler', 'apscheduler.scheduler', 'apscheduler.executors.default'):
        logging.getLogger(_name).setLevel(logging.ERROR)
except ImportError:
    SCHEDULER_AVAILABLE = False

app = Flask(__name__)
CORS(app)

@app.after_request
def add_csp_allow_eval(response):
    """CSP: 'eval' 차단으로 스크립트 오동작 시 script-src에 unsafe-eval 허용."""
    if response.content_type and 'text/html' in response.content_type:
        response.headers['Content-Security-Policy'] = "script-src 'self' 'unsafe-inline' 'unsafe-eval'; object-src 'self'; base-uri 'self'"
    return response

# 환경 변수
BASE_URL = os.getenv('BASE_URL', 'http://tgame365.com')
DATA_PATH = ''
TIMEOUT = int(os.getenv('TIMEOUT', '10'))
MAX_RETRIES = int(os.getenv('MAX_RETRIES', '2'))
DATABASE_URL = os.getenv('DATABASE_URL', None)

# 모양·덩어리 테이블 행 수 상한 (저장량·속도 저하 방지)
SHAPE_MAX_OCCURRENCES = 5000
CHUNK_MAX_OCCURRENCES = 3000

# 반복 로그 억제용 (키 -> 마지막 출력 시각)
_log_throttle_last = {}
# 값이 바뀔 때만 로그 (키 -> 마지막 값)
_log_when_changed_last = {}

def _log_throttle(key, interval_sec, message):
    """같은 key로 interval_sec 초에 한 번만 출력."""
    now = time.time()
    if key not in _log_throttle_last or (now - _log_throttle_last[key]) >= interval_sec:
        _log_throttle_last[key] = now
        print(message)

def _log_when_changed(key, value, message_fn):
    """value가 이전과 다를 때만 출력. value는 비교 가능한 값 (튜플/문자열/숫자)."""
    last = _log_when_changed_last.get(key)
    if last != value:
        _log_when_changed_last[key] = value
        print(message_fn(value))

# 데이터베이스 연결 및 초기화
def init_database():
    """데이터베이스 테이블 생성 및 초기화"""
    if not DB_AVAILABLE or not DATABASE_URL:
        print("[❌ 경고] 데이터베이스 연결 불가 (psycopg2 없음 또는 DATABASE_URL 미설정)")
        return False
    
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        
        # game_results 테이블 생성
        cur.execute('''
            CREATE TABLE IF NOT EXISTS game_results (
                id SERIAL PRIMARY KEY,
                game_id VARCHAR(50) UNIQUE NOT NULL,
                result VARCHAR(10),
                hi BOOLEAN DEFAULT FALSE,
                lo BOOLEAN DEFAULT FALSE,
                red BOOLEAN DEFAULT FALSE,
                black BOOLEAN DEFAULT FALSE,
                jqka BOOLEAN DEFAULT FALSE,
                joker BOOLEAN DEFAULT FALSE,
                hash_value VARCHAR(100),
                salt_value VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # game_id에 인덱스 생성 (조회 성능 향상)
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_game_id ON game_results(game_id)
        ''')
        
        # created_at에 인덱스 생성 (시간 기반 조회 성능 향상)
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_created_at ON game_results(created_at)
        ''')
        
        # color_matches 테이블 생성 (정/꺽 결과 저장)
        cur.execute('''
            CREATE TABLE IF NOT EXISTS color_matches (
                id SERIAL PRIMARY KEY,
                game_id VARCHAR(50) NOT NULL,
                compare_game_id VARCHAR(50) NOT NULL,
                match_result BOOLEAN NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(game_id, compare_game_id)
            )
        ''')
        
        # color_matches 인덱스 생성
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_color_matches_game_id ON color_matches(game_id)
        ''')
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_color_matches_compare_game_id ON color_matches(compare_game_id)
        ''')
        
        # prediction_history: 시스템 예측 기록 (전체 공용, 어디서 접속해도 동일)
        cur.execute('''
            CREATE TABLE IF NOT EXISTS prediction_history (
                round_num INTEGER PRIMARY KEY,
                predicted VARCHAR(10) NOT NULL,
                actual VARCHAR(10) NOT NULL,
                probability REAL,
                pick_color VARCHAR(10),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_prediction_history_created ON prediction_history(created_at DESC)
        ''')
        for col, typ in [('probability', 'REAL'), ('pick_color', 'VARCHAR(10)'), ('blended_win_rate', 'REAL'), ('rate_15', 'REAL'), ('rate_30', 'REAL'), ('rate_100', 'REAL'), ('prediction_details', 'JSONB'), ('shape_predicted', 'VARCHAR(10)')]:
            cur.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'prediction_history' AND column_name = %s",
                (col,)
            )
            if cur.fetchone() is None:
                try:
                    cur.execute('SAVEPOINT add_col_prediction_history')
                    cur.execute('ALTER TABLE prediction_history ADD COLUMN ' + col + ' ' + typ)
                except Exception as alter_err:
                    if 'already exists' in str(alter_err).lower():
                        cur.execute('ROLLBACK TO SAVEPOINT add_col_prediction_history')
                    else:
                        raise
        
        # calc_sessions: 계산기 상태 서버 저장 (새로고침/재접속 후에도 실행중 유지)
        cur.execute('''
            CREATE TABLE IF NOT EXISTS calc_sessions (
                session_id VARCHAR(64) PRIMARY KEY,
                state_json TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # round_predictions: 배팅중(예측) 나올 때마다 회차별로 즉시 저장 → 결과 나오면 prediction_history로 머지
        cur.execute('''
            CREATE TABLE IF NOT EXISTS round_predictions (
                round_num INTEGER PRIMARY KEY,
                predicted VARCHAR(10) NOT NULL,
                pick_color VARCHAR(10),
                probability REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # current_pick: 배팅 연동용 현재 예측 픽 1건 (RED/BLACK, 회차, 확률). 실패해도 서버는 기동
        try:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS current_pick (
                    id INTEGER PRIMARY KEY,
                    pick_color VARCHAR(10),
                    round_num INTEGER,
                    probability REAL,
                    suggested_amount INTEGER,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cur.execute('INSERT INTO current_pick (id) VALUES (1), (2), (3) ON CONFLICT (id) DO NOTHING')
            cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'current_pick' AND column_name = 'running'")
            if cur.fetchone() is None:
                cur.execute('ALTER TABLE current_pick ADD COLUMN running BOOLEAN DEFAULT true')
                cur.execute('UPDATE current_pick SET running = true WHERE running IS NULL')
        except Exception as ex:
            print(f"[경고] current_pick 테이블 생성/초기화 건너뜀 (서버는 계속 기동): {str(ex)[:100]}")
        
        # shape_win_stats: 자주 나온 그래프 모양별 "그 다음 실제 결과" 누적 (정/꺽 예측 무관, 모양→다음 결과만)
        cur.execute('''
            CREATE TABLE IF NOT EXISTS shape_win_stats (
                signature TEXT PRIMARY KEY,
                next_jung_count INTEGER DEFAULT 0,
                next_kkeok_count INTEGER DEFAULT 0,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        ''')
        # shape_win_occurrences: 회차별 모양→다음 결과 기록. 최근 데이터에 더 높은 가중치 적용용
        cur.execute('''
            CREATE TABLE IF NOT EXISTS shape_win_occurrences (
                id SERIAL PRIMARY KEY,
                signature TEXT NOT NULL,
                next_actual TEXT NOT NULL,
                round_num INTEGER NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        ''')
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_shape_occurrences_sig_round
            ON shape_win_occurrences (signature, round_num DESC)
        ''')
        # chunk_profile_occurrences: 덩어리(2개 이상 줄 이어진 구간) 프로필별 다음 결과. 유사 덩어리 가중치용
        cur.execute('''
            CREATE TABLE IF NOT EXISTS chunk_profile_occurrences (
                id SERIAL PRIMARY KEY,
                profile_json TEXT NOT NULL,
                next_actual TEXT NOT NULL,
                round_num INTEGER NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        ''')
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_chunk_profile_round
            ON chunk_profile_occurrences (round_num DESC)
        ''')
        for col, typ in [('next_jung_count', 'INTEGER DEFAULT 0'), ('next_kkeok_count', 'INTEGER DEFAULT 0')]:
            cur.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'shape_win_stats' AND column_name = %s",
                (col,)
            )
            if cur.fetchone() is None:
                try:
                    cur.execute('ALTER TABLE shape_win_stats ADD COLUMN ' + col + ' ' + typ)
                except Exception as alter_err:
                    if 'already exists' not in str(alter_err).lower():
                        print(f"[경고] shape_win_stats 컬럼 추가: {str(alter_err)[:100]}")
        
        conn.commit()
        cur.close()
        conn.close()
        print("[✅] 데이터베이스 테이블 초기화 완료")
        return True
    except Exception as e:
        print(f"[❌ 오류] 데이터베이스 초기화 실패: {str(e)[:200]}")
        return False

def ensure_current_pick_table(conn):
    """current_pick 테이블이 없으면 생성 (POST 실패 시 재시도용)."""
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS current_pick (
                id INTEGER PRIMARY KEY,
                pick_color VARCHAR(10),
                round_num INTEGER,
                probability REAL,
                suggested_amount INTEGER,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cur.execute('INSERT INTO current_pick (id) VALUES (1), (2), (3) ON CONFLICT (id) DO NOTHING')
        cur.close()
        return True
    except Exception as e:
        print(f"[경고] current_pick 테이블 생성 실패: {str(e)[:100]}")
        return False


def get_db_connection(statement_timeout_sec=None):
    """데이터베이스 연결 반환 (connect_timeout으로 먹통 방지). statement_timeout_sec 지정 시 쿼리 실행 시간 제한."""
    if not DB_AVAILABLE or not DATABASE_URL:
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        if statement_timeout_sec is not None and statement_timeout_sec > 0:
            try:
                cur = conn.cursor()
                cur.execute("SET statement_timeout = %s", (str(int(statement_timeout_sec * 1000)),))
                cur.close()
            except Exception:
                pass
        return conn
    except Exception as e:
        print(f"[❌ 오류] 데이터베이스 연결 실패: {str(e)[:200]}")
        return None

def save_game_result(game_data):
    """게임 결과를 데이터베이스에 저장 (중복 체크). statement_timeout으로 먹통 방지."""
    if not DB_AVAILABLE or not DATABASE_URL:
        return False
    
    conn = get_db_connection(statement_timeout_sec=3)
    if not conn:
        return False
    
    try:
        cur = conn.cursor()
        
        # 중복 체크 후 저장
        cur.execute('''
            INSERT INTO game_results 
            (game_id, result, hi, lo, red, black, jqka, joker, hash_value, salt_value)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (game_id) DO NOTHING
        ''', (
            str(game_data.get('gameID', '')),
            game_data.get('result', ''),
            game_data.get('hi', False),
            game_data.get('lo', False),
            game_data.get('red', False),
            game_data.get('black', False),
            game_data.get('jqka', False),
            game_data.get('joker', False),
            game_data.get('hash', ''),
            game_data.get('salt', '')
        ))
        
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"[❌ 오류] 게임 결과 저장 실패: {str(e)[:200]}")
        try:
            conn.close()
        except:
            pass
        return False


def save_prediction_record(round_num, predicted, actual, probability=None, pick_color=None, results=None):
    """시스템 예측 기록 1건 저장. 해당 회차 직전 이력으로 합산승률(blended_win_rate) 계산 후 저장.
    results가 제공되면 shape_signature를 계산하여 prediction_details에 저장."""
    if not DB_AVAILABLE or not DATABASE_URL:
        return False
    conn = get_db_connection(statement_timeout_sec=5)
    if not conn:
        return False
    try:
        history_before = get_prediction_history_before_round(conn, round_num, limit=100)
        blended_val = None
        r15_val = r30_val = r100_val = None
        comp = _blended_win_rate_components(history_before)
        if comp:
            r15_val, r30_val, r100_val, blended_val = comp
        
        # shape_signature·shape_predicted 계산 (results가 제공되고 충분한 길이일 때)
        # shape_predicted = 모양판별 알고리즘(get_shape_prediction_hint) 결과. 모양판별반픽 도구로 사용.
        shape_sig = None
        shape_pred = None
        prediction_details = None
        if results and len(results) >= 16:
            sig = _get_shape_signature(results)
            if sig:
                shape_sig = sig
                prediction_details = json.dumps({'shape_signature': shape_sig})
            try:
                hint = get_shape_prediction_hint(results, history_before)
                shape_pred = hint.get('value') if hint and hint.get('value') in ('정', '꺽') else None
            except Exception:
                shape_pred = None
        
        cur = conn.cursor()
        if prediction_details or shape_pred:
            cur.execute('''
                INSERT INTO prediction_history (round_num, predicted, actual, probability, pick_color, blended_win_rate, rate_15, rate_30, rate_100, prediction_details, shape_predicted)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (round_num) DO UPDATE SET predicted = EXCLUDED.predicted, actual = EXCLUDED.actual,
                    probability = EXCLUDED.probability, pick_color = EXCLUDED.pick_color,
                    blended_win_rate = EXCLUDED.blended_win_rate, rate_15 = EXCLUDED.rate_15, rate_30 = EXCLUDED.rate_30, rate_100 = EXCLUDED.rate_100,
                    prediction_details = COALESCE(EXCLUDED.prediction_details, prediction_history.prediction_details),
                    shape_predicted = COALESCE(EXCLUDED.shape_predicted, prediction_history.shape_predicted),
                    created_at = DEFAULT
            ''', (int(round_num), str(predicted), str(actual), float(probability) if probability is not None else None, str(pick_color) if pick_color else None,
                 round(blended_val, 1) if blended_val is not None else None, round(r15_val, 1) if r15_val is not None else None, round(r30_val, 1) if r30_val is not None else None, round(r100_val, 1) if r100_val is not None else None,
                 prediction_details, str(shape_pred) if shape_pred in ('정', '꺽') else None))
        else:
            cur.execute('''
                INSERT INTO prediction_history (round_num, predicted, actual, probability, pick_color, blended_win_rate, rate_15, rate_30, rate_100)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (round_num) DO UPDATE SET predicted = EXCLUDED.predicted, actual = EXCLUDED.actual,
                    probability = EXCLUDED.probability, pick_color = EXCLUDED.pick_color,
                    blended_win_rate = EXCLUDED.blended_win_rate, rate_15 = EXCLUDED.rate_15, rate_30 = EXCLUDED.rate_30, rate_100 = EXCLUDED.rate_100, created_at = DEFAULT
            ''', (int(round_num), str(predicted), str(actual), float(probability) if probability is not None else None, str(pick_color) if pick_color else None,
                 round(blended_val, 1) if blended_val is not None else None, round(r15_val, 1) if r15_val is not None else None, round(r30_val, 1) if r30_val is not None else None, round(r100_val, 1) if r100_val is not None else None))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"[❌ 오류] 예측 기록 저장 실패: {str(e)[:200]}")
        try:
            conn.close()
        except:
            pass
        return False


def _shape_run_bucket(n):
    """run 길이를 구간으로: 1~2=S, 3~5=M, 6+=L (같은 모양이 더 자주 매칭되도록)."""
    if n <= 2:
        return 'S'
    if n <= 5:
        return 'M'
    return 'L'


def _get_shape_signature(results):
    """
    결과 리스트(최신순)로부터 '그래프 모양' 시그니처 문자열 생성.
    굵은 시그니처: 최근 30개에서 줄/퐁당 run을 앞에서부터 3개만 쓰고, 길이는 S(1~2)/M(3~5)/L(6+)로 구간화.
    예: L6,P1,L2 → L,S,S. 같은 모양 클래스가 자주 쌓여서 모양별 다음 결과 통계가 반영되기 쉽게 함.
    """
    if not results or len(results) < 16:
        return ""
    graph_values = _build_graph_values(results)
    if len(graph_values) < 4:
        return ""
    use = graph_values[:30]
    line_runs, pong_runs = _get_line_pong_runs(use)
    if not line_runs and not pong_runs:
        return ""
    first_is_line = True
    if len(use) >= 2 and (use[0] is True or use[0] is False) and (use[1] is True or use[1] is False):
        first_is_line = (use[0] == use[1])
    parts = []
    li, pi = 0, 0
    for _ in range(3):
        if first_is_line:
            if li < len(line_runs):
                parts.append(_shape_run_bucket(line_runs[li]))
                li += 1
            elif pi < len(pong_runs):
                parts.append(_shape_run_bucket(pong_runs[pi]))
                pi += 1
        else:
            if pi < len(pong_runs):
                parts.append(_shape_run_bucket(pong_runs[pi]))
                pi += 1
            elif li < len(line_runs):
                parts.append(_shape_run_bucket(line_runs[li]))
                li += 1
    return ",".join(parts) if parts else ""


def _reverse_shape_signature(sig):
    """좌우 반전: 시그니처 'L,S,M' → 'M,S,L'. 그래프 좌우 대칭 시 동일 패턴 매칭용."""
    if not sig:
        return ""
    parts = sig.split(",")
    return ",".join(reversed(parts)) if parts else ""


def _reverse_chunk_profile(profile):
    """좌우 반전: 프로필 (h1,h2,h3) → (h3,h2,h1). 덩어리 좌우 대칭 시 동일 패턴 매칭용."""
    if not profile:
        return None
    return tuple(reversed(profile))


def _fetch_shape_win_stats_single(conn, signature, current_round=None):
    """단일 시그니처에 대한 shape win stats. None 또는 {jung_count, kkeok_count}."""
    if not conn or not signature:
        return None
    try:
        cur = conn.cursor()
        if current_round is not None:
            try:
                cur.execute('''
                    SELECT next_actual, round_num FROM shape_win_occurrences
                    WHERE signature = %s
                    ORDER BY round_num DESC
                    LIMIT 100
                ''', (signature,))
                rows = cur.fetchall()
            except Exception:
                rows = []
            finally:
                cur.close()
            if rows:
                SHAPE_DECAY_BASE = 0.95
                SHAPE_DECAY_STEP = 15
                jung_weighted = 0.0
                kkeok_weighted = 0.0
                for next_actual, rnd in rows:
                    age = max(0, current_round - rnd)
                    w = SHAPE_DECAY_BASE ** (age / SHAPE_DECAY_STEP)
                    if next_actual == '정':
                        jung_weighted += w
                    else:
                        kkeok_weighted += w
                total = jung_weighted + kkeok_weighted
                if total >= 10:
                    return {'jung_count': jung_weighted, 'kkeok_count': kkeok_weighted}
        cur = conn.cursor()
        cur.execute('''
            SELECT next_jung_count, next_kkeok_count FROM shape_win_stats WHERE signature = %s
        ''', (signature,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        return {'jung_count': row[0] or 0, 'kkeok_count': row[1] or 0}
    except Exception:
        return None


def get_shape_win_stats(conn, signature, current_round=None):
    """모양 시그니처별 '그 다음 실제 결과' 누적. 원본+좌우반전 시그니처 통계 합산.
    반환: {jung_count, kkeok_count} 또는 None."""
    if not conn or not signature:
        return None
    a = _fetch_shape_win_stats_single(conn, signature, current_round)
    rev_sig = _reverse_shape_signature(signature)
    b = _fetch_shape_win_stats_single(conn, rev_sig, current_round) if rev_sig and rev_sig != signature else None
    if not a and not b:
        return None
    j = (a.get('jung_count') or 0) + (b.get('jung_count') or 0)
    k = (a.get('kkeok_count') or 0) + (b.get('kkeok_count') or 0)
    if j + k < 0.5:
        return None
    return {'jung_count': j, 'kkeok_count': k}


def _get_shape_stats_for_results(results):
    """현재 results에 해당하는 그래프 모양의 '다음 결과' 누적 통계. 예측픽 계산 시 shape_win_stats 인자로 넘길 때 사용.
    current_round를 넘겨 최근 과거 모양에 더 높은 가중치 적용."""
    if not results or len(results) < 16 or not DB_AVAILABLE or not DATABASE_URL:
        return None
    sig = _get_shape_signature(results)
    if not sig:
        return None
    try:
        current_round = int(str(results[0].get('gameID', '0') or '0'), 10)
    except (ValueError, TypeError):
        current_round = None
    conn = get_db_connection(statement_timeout_sec=3)
    if not conn:
        return None
    try:
        return get_shape_win_stats(conn, sig, current_round=current_round)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _get_chunk_profile_from_results(results):
    """results로부터 현재 덩어리 프로필 추출. 없으면 None. 엄격 추출 실패 시 완화 추출 시도."""
    if not results or len(results) < 16:
        return None
    graph_values = _build_graph_values(results)
    if len(graph_values) < 4:
        return None
    use = graph_values[:30]
    line_runs, pong_runs = _get_line_pong_runs(use)
    first_is_line = True
    if len(use) >= 2 and (use[0] is True or use[0] is False) and (use[1] is True or use[1] is False):
        first_is_line = (use[0] == use[1])
    profiles = _extract_chunk_profiles(line_runs, pong_runs, first_is_line)
    if profiles:
        return profiles[0]
    # 완화: line_runs 중 2 이상인 것만 모아 2개 이상이면 프로필로 사용 (덩어리 구간 판별과 유사)
    heights = [r for r in line_runs if r >= 2]
    if len(heights) >= 2:
        return tuple(heights[:8])
    return None


def _get_chunk_stats_for_results(results):
    """현재 results 덩어리 프로필과 유사한 과거 덩어리의 '다음 결과' 가중 통계. 최근·유사할수록 가중치 높게."""
    if not results or len(results) < 16 or not DB_AVAILABLE or not DATABASE_URL:
        return None
    profile = _get_chunk_profile_from_results(results)
    if not profile:
        return None
    try:
        current_round = int(str(results[0].get('gameID', '0') or '0'), 10)
    except (ValueError, TypeError):
        current_round = None
    conn = get_db_connection(statement_timeout_sec=3)
    if not conn:
        return None
    try:
        return get_chunk_profile_stats(conn, profile, current_round=current_round)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _get_latest_next_pick_for_chunk(results, exclude_round=None):
    """현재 results의 덩어리 프로필과 유사한 가장 최근 덩어리의 다음 결과(정/꺽)를 반환. 없으면 모양 시그니처(동일)로 폴백.
    exclude_round: 해당 회차 이상의 데이터는 제외(현재 예측 중인 회차의 actual 누수 방지)."""
    if not results or len(results) < 16 or not DB_AVAILABLE or not DATABASE_URL:
        return None
    profile = _get_chunk_profile_from_results(results)
    sig = _get_shape_signature(results)
    conn = get_db_connection(statement_timeout_sec=3)
    if not conn:
        return None
    try:
        import json
        cur = conn.cursor()
        if profile:
            if exclude_round is not None:
                cur.execute('''
                    SELECT profile_json, next_actual, round_num FROM chunk_profile_occurrences
                    WHERE round_num < %s
                    ORDER BY round_num DESC
                    LIMIT 150
                ''', (int(exclude_round),))
            else:
                cur.execute('''
                    SELECT profile_json, next_actual, round_num FROM chunk_profile_occurrences
                    ORDER BY round_num DESC
                    LIMIT 150
                ''')
            rows = cur.fetchall()
            CHUNK_SIM_THRESHOLD = 0.65
            matches = []
            for profile_json, next_actual, rnd in rows:
                try:
                    other = tuple(json.loads(profile_json))
                except Exception:
                    continue
                if next_actual not in ('정', '꺽'):
                    continue
                sim = _chunk_profile_similarity(profile, other)
                if sim >= CHUNK_SIM_THRESHOLD:
                    matches.append((sim, rnd, next_actual))
                    if len(matches) >= 5:
                        break
            if matches:
                if len(matches) >= 2:
                    jung_cnt = sum(1 for m in matches if m[2] == '정')
                    kkeok_cnt = len(matches) - jung_cnt
                    pick = '정' if jung_cnt > kkeok_cnt else ('꺽' if kkeok_cnt > jung_cnt else matches[0][2])
                else:
                    pick = matches[0][2]
                cur.close()
                return pick
        if sig:
            if exclude_round is not None:
                cur.execute('''
                    SELECT next_actual FROM shape_win_occurrences
                    WHERE signature = %s AND round_num < %s
                    ORDER BY round_num DESC
                    LIMIT 1
                ''', (sig, int(exclude_round)))
            else:
                cur.execute('''
                    SELECT next_actual FROM shape_win_occurrences
                    WHERE signature = %s
                    ORDER BY round_num DESC
                    LIMIT 1
                ''', (sig,))
            row = cur.fetchone()
            cur.close()
            if row and row[0] in ('정', '꺽'):
                return row[0]
        else:
            cur.close()
        return None
    except Exception as e:
        print(f"[경고] 가장 최근 다음 픽(덩어리/모양) 조회 실패: {str(e)[:150]}")
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def update_shape_win_stats(conn, signature, actual, round_num=None):
    """예측 기록 저장 시 호출: 해당 회차 예측 시점의 모양 다음에 실제로 나온 결과(정/꺽)만 누적. 예측값은 사용 안 함.
    round_num이 있으면 shape_win_occurrences에도 저장해 최근 데이터 가중치 적용에 사용."""
    if not conn or not signature:
        return
    is_jung = (actual == '정' or (isinstance(actual, str) and '정' in actual))
    next_actual = '정' if is_jung else '꺽'
    try:
        cur = conn.cursor()
        if is_jung:
            cur.execute('''
                INSERT INTO shape_win_stats (signature, next_jung_count, next_kkeok_count, updated_at)
                VALUES (%s, 1, 0, NOW())
                ON CONFLICT (signature) DO UPDATE SET
                    next_jung_count = shape_win_stats.next_jung_count + 1,
                    updated_at = NOW()
            ''', (signature,))
        else:
            cur.execute('''
                INSERT INTO shape_win_stats (signature, next_jung_count, next_kkeok_count, updated_at)
                VALUES (%s, 0, 1, NOW())
                ON CONFLICT (signature) DO UPDATE SET
                    next_kkeok_count = shape_win_stats.next_kkeok_count + 1,
                    updated_at = NOW()
            ''', (signature,))
        if round_num is not None:
            cur.execute('''
                INSERT INTO shape_win_occurrences (signature, next_actual, round_num)
                VALUES (%s, %s, %s)
            ''', (signature, next_actual, int(round_num)))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"[경고] shape_win_stats 갱신 실패: {str(e)[:150]}")


def get_chunk_profile_stats(conn, profile, current_round=None):
    """
    유사 덩어리 프로필의 '다음 결과' 가중 합계. 원본+좌우반전 프로필 통계 합산.
    profile: (h1, h2, ...) 튜플. 유사도 >= 0.65인 과거 기록만 사용.
    반환: {jung_count, kkeok_count} 또는 None.
    """
    if not conn or not profile:
        return None
    rev_profile = _reverse_chunk_profile(profile)
    try:
        import json
        cur = conn.cursor()
        cur.execute('''
            SELECT profile_json, next_actual, round_num FROM chunk_profile_occurrences
            ORDER BY round_num DESC
            LIMIT 500
        ''')
        rows = cur.fetchall()
        cur.close()
        if not rows:
            return None
        CHUNK_DECAY_BASE = 0.95
        CHUNK_DECAY_STEP = 15
        CHUNK_SIM_THRESHOLD = 0.65
        jung_weighted = 0.0
        kkeok_weighted = 0.0
        for profile_json, next_actual, rnd in rows:
            try:
                other = tuple(json.loads(profile_json))
            except Exception:
                continue
            sim = _chunk_profile_similarity(profile, other)
            if rev_profile:
                sim = max(sim, _chunk_profile_similarity(rev_profile, other))
            if sim < CHUNK_SIM_THRESHOLD:
                continue
            age = max(0, (current_round or 0) - rnd)
            w = (CHUNK_DECAY_BASE ** (age / CHUNK_DECAY_STEP)) * sim
            if next_actual == '정':
                jung_weighted += w
            else:
                kkeok_weighted += w
        total = jung_weighted + kkeok_weighted
        if total < 0.5:
            return None
        return {'jung_count': jung_weighted, 'kkeok_count': kkeok_weighted}
    except Exception:
        return None


def update_chunk_profile_occurrences(conn, profile, actual, round_num=None):
    """덩어리 프로필 다음 결과 저장. 예측 기록 저장 시 호출."""
    if not conn or not profile or round_num is None:
        return
    try:
        import json
        is_jung = (actual == '정' or (isinstance(actual, str) and '정' in actual))
        next_actual = '정' if is_jung else '꺽'
        profile_json = json.dumps(list(profile))
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO chunk_profile_occurrences (profile_json, next_actual, round_num)
            VALUES (%s, %s, %s)
        ''', (profile_json, next_actual, int(round_num)))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"[경고] chunk_profile_occurrences 저장 실패: {str(e)[:100]}")


def _trim_shape_tables(conn):
    """shape_win_occurrences, chunk_profile_occurrences 행 수가 상한 초과 시 가장 오래된 행 삭제. 속도 저하 방지."""
    if not conn or not DB_AVAILABLE:
        return
    try:
        cur = conn.cursor()
        for table, max_rows in [
            ('shape_win_occurrences', SHAPE_MAX_OCCURRENCES),
            ('chunk_profile_occurrences', CHUNK_MAX_OCCURRENCES),
        ]:
            cur.execute(f'SELECT COUNT(*) FROM {table}')
            cnt = cur.fetchone()[0] if cur.rowcount else 0
            if cnt > max_rows:
                to_del = min(cnt - max_rows, 2000)
                cur.execute(f'''
                    DELETE FROM {table} WHERE id IN (
                        SELECT id FROM {table} ORDER BY round_num ASC LIMIT %s
                    )
                ''', (to_del,))
                conn.commit()
                _log_throttle(f'trim_{table}', 60, f"[모양정리] {table} {to_del}행 삭제 (상한 {max_rows})")
        cur.close()
    except Exception as e:
        _log_throttle('trim_err', 60, f"[경고] 모양 테이블 정리 실패: {str(e)[:100]}")


# DB 없을 때 계산기 상태 in-memory 저장 (새로고침 시 유지, 서버 재시작 시 초기화)
_calc_state_memory = {}

def get_calc_state(session_id):
    """계산기 세션 상태 조회. 없으면 None. statement_timeout으로 먹통 방지."""
    if not session_id:
        return None
    sk = str(session_id)[:64]
    if DB_AVAILABLE and DATABASE_URL:
        conn = get_db_connection(statement_timeout_sec=5)
        if conn:
            try:
                cur = conn.cursor()
                cur.execute('SELECT state_json FROM calc_sessions WHERE session_id = %s', (sk,))
                row = cur.fetchone()
                cur.close()
                conn.close()
                if row and row[0]:
                    return json.loads(row[0])
            except Exception as e:
                print(f"[❌ 오류] 계산기 상태 조회 실패: {str(e)[:200]}")
                try:
                    conn.close()
                except:
                    pass
    return _calc_state_memory.get(sk)


def save_calc_state(session_id, state_dict):
    """계산기 세션 상태 저장. statement_timeout으로 먹통 방지."""
    if not session_id:
        return False
    sk = str(session_id)[:64]
    _calc_state_memory[sk] = state_dict
    if DB_AVAILABLE and DATABASE_URL:
        conn = get_db_connection(statement_timeout_sec=5)
        if conn:
            try:
                cur = conn.cursor()
                cur.execute('''
                    INSERT INTO calc_sessions (session_id, state_json, updated_at)
                    VALUES (%s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (session_id) DO UPDATE SET state_json = EXCLUDED.state_json, updated_at = CURRENT_TIMESTAMP
                ''', (sk, json.dumps(state_dict)))
                conn.commit()
                cur.close()
                conn.close()
                return True
            except Exception as e:
                print(f"[❌ 오류] 계산기 상태 저장 실패: {str(e)[:200]}")
                try:
                    conn.close()
                except:
                    pass
    return True


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
            client_pick_by_round[rn] = {'predicted': pred, 'pickColor': h.get('pickColor') or h.get('pick_color')}
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


def _get_actual_for_round(results, round_id):
    """results(최신순)에서 해당 회차의 실제 결과 반환. '정'|'꺽'|'joker'|None(미수신)."""
    if not results or round_id is None:
        return None
    rid = str(round_id)
    for i in range(len(results)):
        if str(results[i].get('gameID')) == rid:
            if results[i].get('joker'):
                return 'joker'
            gv = _build_graph_values(results)
            if i < len(gv) and gv[i] is not None:
                return '정' if gv[i] else '꺽'
            return None
    return None


def _build_round_actuals(results):
    """results(최신순)에서 회차별 실제 결과 추출. 프론트엔드 getCategory와 동일한 색상 로직."""
    out = {}
    if not results or len(results) < 16:
        return out
    gv = _build_graph_values(results)
    for i in range(min(15, len(results) - 15, len(gv))):
        r = results[i]
        r15 = results[i + 15]
        rid = str(r.get('gameID', ''))
        if not rid:
            continue
        if r.get('joker') or r15.get('joker'):
            out[rid] = {'actual': 'joker', 'color': None}
            continue
        if gv[i] is None:
            continue
        actual = '정' if gv[i] else '꺽'
        c = get_card_color_from_result(r)
        if c is None:
            c15 = get_card_color_from_result(r15)
            if c15 is not None:
                c = c15 if gv[i] else (not c15)
        color = 'RED' if c is True else 'BLACK' if c is False else None
        out[rid] = {'actual': actual, 'color': color}
    return out


def _blended_win_rate(prediction_history):
    """예측 이력으로 15/30/100 가중 승률. (0.6*15 + 0.25*30 + 0.15*100).
    프론트엔드와 동일: 위치 기준 마지막 N개에서 조커 제외 후 승률 계산."""
    comp = _blended_win_rate_components(prediction_history)
    return comp[3] if comp else None


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


def _get_main_recent15_win_rate(ph):
    """메인 예측기(예측픽) 기준 최근 15경기 승률(%). 조커 제외. 5회 미만이면 None."""
    if not ph or len(ph) < 5:
        return None
    vh = [h for h in ph if h and h.get('actual') not in ('joker', '조커')]
    last15 = vh[-15:] if len(vh) >= 15 else vh
    if len(last15) < 5:
        return None
    wins = sum(1 for h in last15 if h.get('predicted') == h.get('actual'))
    return 100.0 * wins / len(last15)


def _get_current_result_run_length(ph):
    """실제 결과(actual) 기준 맨 끝에서 연속 같은 결과(정 또는 꺽) 개수. 조커 나오면 끊김."""
    if not ph:
        return 0
    vh = [h for h in ph if h and h.get('actual') is not None and str(h.get('actual', '')).strip()]
    if not vh:
        return 0
    last_actual = vh[-1].get('actual')
    if last_actual in ('joker', '조커'):
        return 0
    run = 0
    for i in range(len(vh) - 1, -1, -1):
        a = vh[i].get('actual')
        if a in ('joker', '조커'):
            break
        if a != last_actual:
            break
        run += 1
    return run


def _get_lose_streak_from_history(history):
    """완료된 history 끝에서부터 연패 개수. 조커/멈춤(no_bet)은 연패 카운트에 포함(패와 동일)."""
    if not history:
        return 0
    completed = [h for h in history if h.get('actual') and h.get('actual') != 'pending']
    if not completed:
        return 0
    n = 0
    for h in reversed(completed):
        actual = h.get('actual')
        pred = h.get('predicted')
        if actual == 'joker':
            n += 1
        elif pred != actual:
            n += 1
        else:
            break
    return n


def _get_shape_50_win_rate():
    """메인 예측기 모양 픽 최근 50회 승률. prediction_history의 shape_predicted vs actual, 조커 제외. 승률반픽 기준."""
    ph = get_prediction_history(60)
    if not ph:
        return None
    last50 = [h for h in ph[-50:] if h.get('actual') in ('정', '꺽')]
    valid = [h for h in last50 if h.get('shape_predicted') in ('정', '꺽')]
    if not valid:
        return None
    wins = sum(1 for h in valid if h.get('shape_predicted') == h.get('actual'))
    return 100.0 * wins / len(valid)


def _get_shape_prediction_win_rate_10(c):
    """모양판별승률: 메인 예측기표 모양판별 픽(shape_predicted) 최신 10개 결과. prediction_history 기준, 조커=패. 모양판별반픽 판단용."""
    ph = get_prediction_history(200)
    with_shape = [h for h in ph if h and h.get('shape_predicted') in ('정', '꺽') and h.get('actual') in ('정', '꺽', 'joker', '조커')]
    if not with_shape:
        return None
    last10 = with_shape[-10:]
    wins, total = 0, 0
    for h in last10:
        sp = h.get('shape_predicted')
        act = h.get('actual')
        if act in ('joker', '조커'):
            total += 1
            continue
        total += 1
        if sp == act:
            wins += 1
    return (100.0 * wins / total) if total > 0 else None


def _get_display_win_rate(history, max_rows=200):
    """계산기 표승률: 최근 max_rows개 중 배팅한 완료 행만, 조커=패. 승률 = 승/(승+패)*100. 표본 없으면 None."""
    if not history:
        return None
    completed = [h for h in history if h.get('actual') and h.get('actual') != 'pending']
    # 배팅한 행만 (멈춤 no_bet 제외. betAmount 없으면 no_bet 아닌 행 포함)
    bet_rows = [h for h in completed if not h.get('no_bet')]
    last_n = bet_rows[-max_rows:] if len(bet_rows) > max_rows else bet_rows
    if not last_n:
        return None
    wins = sum(1 for h in last_n if h.get('actual') != 'joker' and h.get('predicted') == h.get('actual'))
    losses = sum(1 for h in last_n if h.get('actual') == 'joker' or h.get('predicted') != h.get('actual'))
    total = wins + losses
    if total < 1:
        return None
    return 100.0 * wins / total


def _server_recent_15_win_rate(completed_list):
    """완료된 회차 리스트에서 최근 15회 승률(%). 조커=패. 클라이언트 getCalcRecent15WinRate와 동일 로직."""
    if not completed_list:
        return 50.0
    last15 = completed_list[-15:] if len(completed_list) >= 15 else completed_list
    wins = sum(1 for h in last15 if h.get('actual') != 'joker' and h.get('predicted') == h.get('actual'))
    return (wins / len(last15)) * 100.0 if last15 else 50.0


def _server_win_rate_direction_zone(ph):
    """예측 이력(과거→현재)으로 롤링 100회 승률 구간 계산. 'high_falling'|'low_rising'|'mid_flat'|None. 클라이언트 승률방향 패널과 동일 공식.
    최근 10경기 승률 가중: 예측 잘 맞으면(53% 이상) 반대픽 억제, 안 맞으면(50% 이하) 정픽 억제. 승률/연패 반픽은 15경기 53% 이상이면 미적용."""
    if not ph or len(ph) < 100:
        return None
    vh = [h for h in ph if h and h.get('actual') is not None and str(h.get('actual', '')).strip() and str(h.get('actual')) != 'pending']
    if len(vh) < 100:
        return None
    # 메인 예측기 최근 10경기 승률 (조커 제외) — 정픽/반대픽 판정에 가중
    last10 = [h for h in vh[-10:] if h.get('actual') not in ('joker', '조커')]
    rate10_pct = None
    if len(last10) >= 3:
        wins10 = sum(1 for h in last10 if h.get('predicted') == h.get('actual'))
        rate10_pct = 100.0 * wins10 / len(last10)
    derived = []
    for i in range(99, len(vh)):
        w = vh[i - 99:i + 1]
        wins = sum(1 for h in w if h.get('actual') != 'joker' and h.get('predicted') == h.get('actual'))
        loss = sum(1 for h in w if h.get('actual') != 'joker' and h.get('predicted') != h.get('actual'))
        c = wins + loss
        if c > 0:
            derived.append({'round': w[-1].get('round'), 'rate50': 100.0 * wins / c})
    if len(derived) < 6:
        return None
    rates = [x['rate50'] for x in derived]
    high = max(rates)
    low = min(rates)
    current = rates[-1]
    rate5ago = rates[-6]
    delta5 = current - rate5ago
    # 저점 40~43% / 고점 57~60% 반영: ratio를 고정 밴드 기준으로 정의 (실제 승률 구간과 일치)
    WIN_RATE_LOW_BAND = 43.0   # 저점 상한 %
    WIN_RATE_HIGH_BAND = 57.0  # 고점 하한 %
    if current <= WIN_RATE_LOW_BAND:
        ratio_fixed = 0.0  # 저점 부근
    elif current >= WIN_RATE_HIGH_BAND:
        ratio_fixed = 1.0  # 고점 부근
    else:
        ratio_fixed = (current - WIN_RATE_LOW_BAND) / (WIN_RATE_HIGH_BAND - WIN_RATE_LOW_BAND)
    ratio_dynamic = (current - low) / (high - low) if high > low else 0.5
    WIN_RATE_DIR_DELTA4 = 0.45   # 올릴수록 오름세 판정 보수적 — 예측 틀릴 때 정픽 덜 고집 (기존 0.38)
    WIN_RATE_DIR_DELTA5 = 0.44   # 올릴수록 방향 전환 보수적 — 고점하락 더 빨리 (기존 0.52)
    if len(derived) >= 4:
        recent = rates[-1]
        prev4 = rates[-4]
        is_rising = recent > prev4 + WIN_RATE_DIR_DELTA4
        is_falling = recent < prev4 - WIN_RATE_DIR_DELTA4
        # 저점 부근(40~43%) + 오름세 → 정픽(low_rising)
        # 메인 예측 최근 10경기 승률로 반픽/정픽 억제 (53%/50% — 예측 틀릴 때 정픽 더 쉽게 억제)
        if current <= WIN_RATE_LOW_BAND and is_rising:
            if rate10_pct is not None and rate10_pct <= 50:
                return 'mid_flat'  # 최근 10회 승률 낮으면 정픽 고집 완화
            return 'low_rising'
        # 고점 부근(57~60%) + 내림세 → 반대픽(high_falling)
        if current >= WIN_RATE_HIGH_BAND and is_falling:
            if rate10_pct is not None and rate10_pct >= 53:
                return 'mid_flat'  # 최근 10회 예측 잘 맞으면 반대픽 억제
            return 'high_falling'
        # 기존 ratio 기반 판정 (중간 구간) — R_LOW 0.48, R_HIGH 0.57 (내림 시 반대픽 더 빨리)
        if is_rising and ratio_dynamic >= 0.48:
            if rate10_pct is not None and rate10_pct <= 50:
                return 'mid_flat'
            return 'low_rising'
        if is_falling and ratio_dynamic <= 0.57:
            if rate10_pct is not None and rate10_pct >= 53:
                return 'mid_flat'
            return 'high_falling'
    if delta5 < -WIN_RATE_DIR_DELTA5 and (ratio_fixed >= 0.5 or ratio_dynamic >= 0.57):
        if rate10_pct is not None and rate10_pct >= 53:
            return 'mid_flat'
        return 'high_falling'
    if delta5 > WIN_RATE_DIR_DELTA5 and (ratio_fixed <= 0.5 or ratio_dynamic >= 0.48):
        if rate10_pct is not None and rate10_pct <= 50:
            return 'mid_flat'
        return 'low_rising'
    return 'mid_flat'


def _effective_win_rate_direction_zone(ph, c, current_round):
    """히스테리시스 적용된 zone. c에 last_win_rate_zone, last_win_rate_zone_change_round 저장.
    쿨다운 제거 — 내림 감지 시 반대픽 즉시 전환.
    연패 중 방향 고정(lock_direction_on_lose_streak): 배팅이 연패 중일 때 방향을 바꾸지 않고 진행하던 방향 유지."""
    raw_zone = _server_win_rate_direction_zone(ph)
    last_zone = c.get('last_win_rate_zone')
    lock_on_streak = bool(c.get('lock_direction_on_lose_streak', True))
    lose_streak = _get_lose_streak_from_history(c.get('history') or [])

    # 연패 중 방향 고정: 연패 >= 1이면 마지막 승 직후 zone 사용
    if lock_on_streak and lose_streak >= 1:
        zone_on_win = c.get('last_win_rate_zone_on_win')
        if zone_on_win in ('low_rising', 'high_falling', 'mid_flat'):
            return zone_on_win
        # 저장된 값 없으면 last_zone 사용 (연패 직전에 사용하던 방향)
        if last_zone in ('low_rising', 'high_falling', 'mid_flat'):
            return last_zone
        return raw_zone if raw_zone else last_zone

    # 연패 0이면 raw_zone 반영 (승 직후이므로 zone_on_win 갱신)
    if raw_zone == 'low_rising':
        if raw_zone != last_zone:
            c['last_win_rate_zone'] = raw_zone
            c['last_win_rate_zone_change_round'] = current_round
        c['last_win_rate_zone_on_win'] = raw_zone
        return raw_zone
    if raw_zone == 'high_falling':
        if raw_zone != last_zone:
            c['last_win_rate_zone'] = raw_zone
            c['last_win_rate_zone_change_round'] = current_round
        c['last_win_rate_zone_on_win'] = raw_zone
        return raw_zone
    if raw_zone and raw_zone != last_zone:
        c['last_win_rate_zone'] = raw_zone
        c['last_win_rate_zone_change_round'] = current_round
    effective = raw_zone if raw_zone else last_zone
    if effective:
        c['last_win_rate_zone_on_win'] = effective
    # 승 직후 mid_flat이면 last_trend_direction 초기화 — 연패 중 반픽에서 이어지던 'down' 제거, 정픽으로 전환
    if effective == 'mid_flat':
        c['last_trend_direction'] = None
    return effective


def _update_calc_paused_after_round(c):
    """회차 반영 후 서버에서 paused 갱신. 멈춤 기준 = 계산기 표 15회 승률(해당 계산기 배팅 상황과 맞춤). 클라이언트 없이 멈춤 정확 동작."""
    history = c.get('history') or []
    completed = [h for h in history if h.get('actual') and h.get('actual') != 'pending']
    pause_enabled = c.get('pause_low_win_rate_enabled', False)
    thr = max(0, min(100, int(c.get('pause_win_rate_threshold') or 45)))

    # 완료 회차가 없으면 멈춤 해제(이전 세션의 paused=true가 초반에 띄엄띄엄 적용되는 것 방지)
    if len(completed) < 1:
        c['paused'] = False
        return

    # 멈춤은 '계산기 표 15회 승률 ≤ N%' 옵션에만 해당. 마틴만 켜진 경우는 멈춤과 무관.
    # 계산기 15회 승률 = 해당 계산기가 실제 배팅한 최근 15회 승률 → 현재 배팅 상황과 맞춤
    if pause_enabled:
        martingale = c.get('martingale', False)
        last_is_loss = False
        if martingale and len(completed) >= 1:
            last_h = completed[-1]
            last_is_loss = last_h.get('actual') == 'joker' or last_h.get('predicted') != last_h.get('actual')
        if not last_is_loss:
            rate15 = _server_recent_15_win_rate(completed)  # 계산기 완료 회차 최근 15회 승률
            PAUSE_RESUME_HYSTERESIS = 3
            resume_thr = min(100, thr + PAUSE_RESUME_HYSTERESIS)
            if c.get('paused', False):
                c['paused'] = rate15 <= resume_thr  # 멈춤 중: 재개는 15회 승률 > resume_thr 일 때만
            else:
                c['paused'] = rate15 <= thr  # 배팅 중: 15회 승률 기준 이하이면 멈춤
        # last_is_loss이면 이번에는 paused 갱신 안 함(마틴 끝날 때까지 배팅 계속)
    # 옵션 꺼져 있으면 기존 paused 유지(서버가 강제로 False로 바꾸지 않음)


def _round_eq(a, b):
    """회차 비교: int/str 혼용 시에도 동일 회차면 True (표·매크로 금액 일치용)."""
    if a is None or b is None:
        return a == b
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return a == b


def _calculate_calc_profit_server(calc_state, history_entry):
    """서버에서 계산기 수익, 마틴게일 단계, 연승/연패 계산. history_entry에 계산된 값 추가."""
    MARTIN_PYO_RATIOS = [1, 1.5, 2.5, 4, 7, 12, 20, 40, 40]
    
    capital = float(calc_state.get('capital', 1000000))
    base = float(calc_state.get('base', 10000))
    odds = float(calc_state.get('odds', 1.97))
    martingale = bool(calc_state.get('martingale', False))
    martingale_type = calc_state.get('martingale_type', 'pyo')
    
    history = calc_state.get('history', [])
    entry_round = history_entry.get('round')
    # 현재 회차 이전의 완료된 회차만 계산 (회차별 1건만, int/str 혼용 시에도 pending 제외 — 매크로 금액 정확도)
    completed_list = [h for h in history if h.get('actual') and h.get('actual') != 'pending' and not _round_eq(h.get('round'), entry_round)]
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
    
    # 마틴게일 테이블 생성
    martin_table = [round(base * r) for r in MARTIN_PYO_RATIOS]
    if martingale_type == 'pyo_half':
        martin_table = [round(x / 2) for x in martin_table]
    
    # 이전 회차들로 자본금과 마틴게일 단계 계산
    for h in completed_history:
        if h.get('no_bet') or (h.get('betAmount') == 0):
            continue
        
        if martingale and martingale_type in ('pyo', 'pyo_half'):
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
            if martingale and martingale_type in ('pyo', 'pyo_half'):
                martingale_step = min(martingale_step + 1, len(martin_table) - 1)
            else:
                current_bet = min(current_bet * 2, int(cap))
        elif is_win:
            cap += bet * (odds - 1)
            if martingale and martingale_type in ('pyo', 'pyo_half'):
                martingale_step = 0
            else:
                current_bet = base
        else:
            cap -= bet
            if martingale and martingale_type in ('pyo', 'pyo_half'):
                martingale_step = min(martingale_step + 1, len(martin_table) - 1)
            else:
                current_bet = min(current_bet * 2, int(cap))
        
        if cap <= 0:
            break
    
    # 현재 회차의 배팅금액 계산
    if martingale and martingale_type in ('pyo', 'pyo_half'):
        current_bet = martin_table[min(martingale_step, len(martin_table) - 1)]
    else:
        current_bet = min(current_bet, int(cap))
    
    bet_amount = min(current_bet, int(cap)) if not history_entry.get('no_bet') and history_entry.get('betAmount') != 0 else 0
    
    # 현재 회차의 수익 계산
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
    
    # 연승/연패 계산
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
    
    # 계산된 값들을 history_entry에 추가
    history_entry['betAmount'] = bet_amount
    history_entry['profit'] = profit
    history_entry['capital_after'] = max(0, int(cap + profit))
    history_entry['martingale_step'] = martingale_step
    history_entry['max_win_streak'] = max_win_streak
    history_entry['max_lose_streak'] = max_lose_streak
    
    return history_entry


def _apply_results_to_calcs(results):
    """결과 수집 후 실행 중인 계산기 회차 반영: pending_round 결과 있으면 history 반영 후 다음 예측으로 갱신.
    안정화: pending_*는 저장된 예측(round_predictions)만 사용. 저장은 스케줄러 ensure_stored에서만.
    서버에서 계산기 수익, 마틴게일, 연승/연패 계산."""
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
                        # 모양판별 옵션: shape_only와 별도. 덩어리/퐁당 개선 픽 사용 (기존 공식 변경 없음)
                        if c.get('shape_prediction') and not c.get('shape_only_latest_next_pick'):
                            try:
                                sw = max(0, min(3, float(c.get('shape_weight', 1)) or 1))
                                cw = max(0, min(3, float(c.get('chunk_weight', 1)) or 1))
                                pw = max(0, min(3, float(c.get('pong_weight', 1)) or 1))
                                symw = max(0, min(3, float(c.get('symmetry_weight', 1)) or 1))
                                hint = get_shape_prediction_hint(results, get_prediction_history(100), shape_weight=sw, chunk_weight=cw, pong_weight=pw, symmetry_weight=symw)
                                if hint and hint.get('value') in ('정', '꺽'):
                                    c['pending_predicted'] = hint['value']
                                    c['pending_color'] = hint.get('color') or c.get('pending_color')
                                    c['pending_shape_debug'] = hint.get('debug') or {}
                            except Exception:
                                pass
                        eff_pick, amt, eff_pred = _server_calc_effective_pick_and_amount(c)
                        c['pending_bet_amount'] = amt if amt is not None and amt > 0 else None
                        # 모양옵션 체크 시: 실제 배팅 픽(shape 기준)으로 pending_color·pending_predicted 보정 → 1열과 배팅중 색 일치
                        if c.get('shape_only_latest_next_pick') and eff_pick and eff_pred:
                            c['pending_predicted'] = eff_pred
                            c['pending_color'] = '빨강' if eff_pick == 'RED' else ('검정' if eff_pick == 'BLACK' else c.get('pending_color'))
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
                # shape_prediction·shape_only_latest_next_pick 사용 시 pending_predicted는 모양픽이므로, 예측기표용으로는 메인 픽을 별도 계산.
                pred_for_record = pending_predicted
                main_pred_for_record = None
                if (c.get('shape_prediction') or c.get('shape_only_latest_next_pick')) and results and len(results) >= 16:
                    filtered_for_main = [r for r in results if int(str(r.get('gameID') or '0'), 10) < pending_round]
                    if len(filtered_for_main) >= 16:
                        ph_rec = get_prediction_history(100)
                        main_pred_for_record = compute_prediction(filtered_for_main, ph_rec)
                        if main_pred_for_record and main_pred_for_record.get('value') in ('정', '꺽') and main_pred_for_record.get('round') == pending_round:
                            pred_for_record = main_pred_for_record['value']
                pick_color_for_record = _normalize_pick_color_value((main_pred_for_record.get('color') if main_pred_for_record else None) or c.get('pending_color'))
                if pick_color_for_record is None:
                    # pick-color-core-rule: 정/꺽→빨강/검정은 15번 카드 기준. 고정 매핑(정=빨강,꺽=검정) 금지.
                    is_15_red = _get_card_15_color_for_round(results, pending_round)
                    if is_15_red is True:
                        pick_color_for_record = '빨강' if pending_predicted == '정' else '검정'
                    elif is_15_red is False:
                        pick_color_for_record = '검정' if pending_predicted == '정' else '빨강'
                    else:
                        pick_color_for_record = '빨강' if pending_predicted == '정' else '검정'  # 15번 미확인 시 최후 폴백
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
                                chunk_prof = _get_chunk_profile_from_results(results_for_shape)
                                if chunk_prof:
                                    update_chunk_profile_occurrences(conn_shape, chunk_prof, actual, round_num=pending_round)
                            finally:
                                try:
                                    conn_shape.close()
                                except Exception:
                                    pass
                # 계산기 히스토리·표시용: 배팅한 픽(반픽/승률반픽 적용)
                pred_for_calc = pending_predicted
                bet_color_for_history = _normalize_pick_color_value(c.get('pending_color'))
                if bet_color_for_history is None:
                    # pick-color-core-rule: 정/꺽→빨강/검정은 15번 카드 기준. 고정 매핑 금지.
                    is_15_red = _get_card_15_color_for_round(results, pending_round)
                    if is_15_red is True:
                        bet_color_for_history = '빨강' if pending_predicted == '정' else '검정'
                    elif is_15_red is False:
                        bet_color_for_history = '검정' if pending_predicted == '정' else '빨강'
                    else:
                        bet_color_for_history = '빨강' if pending_predicted == '정' else '검정'  # 15번 미확인 시 최후 폴백
                if c.get('reverse'):
                    pred_for_calc = '꺽' if pending_predicted == '정' else '정'
                    bet_color_for_history = _flip_pick_color(bet_color_for_history)
                blended = _blended_win_rate(get_prediction_history(100))
                # 승률반픽: 모양승률이 설정값 이하일 때만 반픽
                shape_wr = _get_shape_50_win_rate()
                wr_thr = max(0, min(100, int(c.get('win_rate_threshold') or 50)))
                if c.get('win_rate_reverse') and shape_wr is not None and shape_wr <= wr_thr:
                    pred_for_calc = '꺽' if pred_for_calc == '정' else '정'
                    bet_color_for_history = _flip_pick_color(bet_color_for_history)
                lose_streak = _get_lose_streak_from_history(c.get('history') or [])
                lose_streak_thr = max(0, min(100, int(c.get('lose_streak_reverse_threshold') or 48)))
                lose_streak_min = max(2, min(15, int(c.get('lose_streak_reverse_min_streak') or 3)))
                if c.get('lose_streak_reverse') and lose_streak >= lose_streak_min and blended is not None and blended <= lose_streak_thr:
                    pred_for_calc = '꺽' if pred_for_calc == '정' else '정'
                    bet_color_for_history = _flip_pick_color(bet_color_for_history)
                # 승률방향 옵션: 저점→고점 정픽, 고점→저점 반대픽, 정체 시 직전 방향 참조
                if c.get('win_rate_direction_reverse'):
                    ph = get_prediction_history(150)
                    zone = _effective_win_rate_direction_zone(ph, c, pending_round)
                    if zone == 'high_falling':
                        pred_for_calc = '꺽' if pred_for_calc == '정' else '정'
                        bet_color_for_history = _flip_pick_color(bet_color_for_history)
                        c['last_trend_direction'] = 'down'
                    elif zone == 'low_rising':
                        c['last_trend_direction'] = 'up'
                    elif zone == 'mid_flat':
                        if c.get('last_trend_direction') == 'down':
                            pred_for_calc = '꺽' if pred_for_calc == '정' else '정'
                            bet_color_for_history = _flip_pick_color(bet_color_for_history)
                if c.get('shape_prediction') and c.get('shape_prediction_reverse'):
                    sp10 = _get_shape_prediction_win_rate_10(c)
                    thr = max(0, min(100, int(c.get('shape_prediction_reverse_threshold') or 50)))
                    if sp10 is not None and sp10 <= thr:
                        pred_for_calc = '꺽' if pred_for_calc == '정' else '정'
                        bet_color_for_history = _flip_pick_color(bet_color_for_history)
                history_entry = {'round': pending_round, 'predicted': pred_for_calc, 'actual': actual}
                if bet_color_for_history:
                    history_entry['pickColor'] = bet_color_for_history
                # 경고 합산승률 저장
                if blended is not None:
                    history_entry['warningWinRate'] = blended
                # 모양: 가장 최근 다음 픽에만 배팅 — 값 없거나 픽 불일치면 no_bet
                if c.get('shape_only_latest_next_pick') and results_for_shape and len(results_for_shape) >= 16:
                    latest_next = _get_latest_next_pick_for_chunk(results_for_shape, exclude_round=pending_round)
                    if not latest_next or latest_next not in ('정', '꺽') or pred_for_calc != latest_next:
                        history_entry['no_bet'] = True
                        history_entry['betAmount'] = 0
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
                # 15번 카드 조커 시 배팅 안 함 → no_bet. 조커 끝나면 마틴 이어감
                if actual == 'joker':
                    is_15_joker_at_pred = len(results) >= 16 and bool(results[15].get('joker'))
                    if is_15_joker_at_pred:
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
                    # 모양판별 옵션: shape_only와 별도
                    if c.get('shape_prediction') and not c.get('shape_only_latest_next_pick'):
                        try:
                            sw = max(0, min(3, float(c.get('shape_weight', 1)) or 1))
                            cw = max(0, min(3, float(c.get('chunk_weight', 1)) or 1))
                            pw = max(0, min(3, float(c.get('pong_weight', 1)) or 1))
                            symw = max(0, min(3, float(c.get('symmetry_weight', 1)) or 1))
                            hint = get_shape_prediction_hint(results, get_prediction_history(100), shape_weight=sw, chunk_weight=cw, pong_weight=pw, symmetry_weight=symw)
                            if hint and hint.get('value') in ('정', '꺽'):
                                c['pending_predicted'] = hint['value']
                                c['pending_color'] = hint.get('color') or c.get('pending_color')
                                c['pending_shape_debug'] = hint.get('debug') or {}
                        except Exception:
                            pass
                    eff_pick, next_amt, eff_pred = _server_calc_effective_pick_and_amount(c)
                    c['pending_bet_amount'] = next_amt if next_amt is not None and next_amt > 0 else None
                    # 모양옵션 체크 시: 실제 배팅 픽(shape 기준)으로 pending_color·pending_predicted 보정 → 1열과 배팅중 색 일치
                    if c.get('shape_only_latest_next_pick') and eff_pick and eff_pred:
                        c['pending_predicted'] = eff_pred
                        c['pending_color'] = '빨강' if eff_pick == 'RED' else ('검정' if eff_pick == 'BLACK' else c.get('pending_color'))
                    updated = True
                    to_push.append((int(cid), c))
            if updated:
                save_calc_state(session_id, state)  # 먼저 저장 → POST /api/current-pick 시 get_calc_state가 새 상태를 읽어 금액 보정 가능
                for calc_id, calc_c in to_push:
                    _push_current_pick_from_calc(calc_id, calc_c)
            # relay 캐시: 실행 중인 계산기는 회차 반영 여부와 관계없이 항상 서버 금액으로 갱신 (웹·서버 교차 덮어쓰기로 5천↔1만 깜빡임 방지)
            for cid in ('1', '2', '3'):
                c = state.get(cid)
                if not c or not isinstance(c, dict) or not c.get('running'):
                    continue
                try:
                    pick_color, suggested_amount, _ = _server_calc_effective_pick_and_amount(c)
                    if pick_color is not None:
                        pr = c.get('pending_round')
                        _update_current_pick_relay_cache(int(cid), pr, pick_color, suggested_amount, c.get('running', True))
                except Exception:
                    pass
    except Exception as e:
        print(f"[스케줄러] 회차 반영 오류: {str(e)[:200]}")


def get_prediction_history_before_round(conn, round_num, limit=100):
    """해당 회차 직전까지의 예측 이력 (round_num < round_num, 과거→현재 순). 합산승률 저장용."""
    if not conn or round_num is None:
        return []
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('''
            SELECT round_num as "round", predicted, actual
            FROM prediction_history
            WHERE round_num < %s
            ORDER BY round_num DESC
            LIMIT %s
        ''', (int(round_num), int(limit)))
        rows = cur.fetchall()
        cur.close()
        out = [{'round': r['round'], 'predicted': r['predicted'], 'actual': r['actual']} for r in reversed(rows)]
        return out
    except Exception:
        return []


def _prediction_history_has_round(round_num):
    """해당 회차가 prediction_history에 이미 있는지 조회."""
    if not DB_AVAILABLE or not DATABASE_URL or round_num is None:
        return False
    conn = get_db_connection(statement_timeout_sec=3)
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute('SELECT 1 FROM prediction_history WHERE round_num = %s LIMIT 1', (int(round_num),))
        found = cur.fetchone() is not None
        cur.close()
        conn.close()
        return found
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return False


def get_stored_round_prediction(round_num):
    """해당 회차에 대해 round_predictions에 저장된 예측이 있으면 반환. 한 출처(서버 저장)로 안정화용."""
    if not DB_AVAILABLE or not DATABASE_URL or round_num is None:
        return None
    conn = get_db_connection(statement_timeout_sec=3)
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            'SELECT predicted, pick_color, probability FROM round_predictions WHERE round_num = %s LIMIT 1',
            (int(round_num),)
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return None
        return {
            'predicted': str(row[0]) if row[0] else None,
            'pick_color': str(row[1]).strip() if row[1] else None,
            'probability': float(row[2]) if row[2] is not None else None,
        }
    except Exception as e:
        print(f"[경고] get_stored_round_prediction 조회 실패: {str(e)[:100]}")
        try:
            conn.close()
        except Exception:
            pass
        return None


def ensure_stored_prediction_for_current_round(results):
    """현재 회차에 대한 예측이 round_predictions에 없으면 한 번만 계산·저장. 스케줄러에서만 호출(저장은 한 곳)."""
    if not results or len(results) < 16 or not DB_AVAILABLE or not DATABASE_URL:
        return
    try:
        latest_gid = results[0].get('gameID')
        predicted_round = int(str(latest_gid or '0'), 10) + 1
        is_15_joker = len(results) >= 15 and bool(results[14].get('joker'))
        if is_15_joker:
            return
        if get_stored_round_prediction(predicted_round):
            return
        ph = get_prediction_history(100)
        pred = compute_prediction(results, ph)
        if pred and pred.get('round') and pred.get('value') is not None:
            save_round_prediction(
                pred['round'], pred['value'],
                pick_color=pred.get('color'), probability=pred.get('prob')
            )
    except Exception as e:
        print(f"[경고] ensure_stored_prediction_for_current_round 실패: {str(e)[:120]}")


def save_round_prediction(round_num, predicted, pick_color=None, probability=None):
    """배팅중(예측) 나올 때마다 회차별로 즉시 저장. 결과 나오면 prediction_history로 머지됨."""
    if not DB_AVAILABLE or not DATABASE_URL or round_num is None or predicted is None:
        return False
    conn = get_db_connection(statement_timeout_sec=3)
    if not conn:
        return False
    try:
        cur = conn.cursor()
        pick_color = _normalize_pick_color_value(pick_color)
        # 안정화: 이미 저장된 회차는 덮어쓰지 않음. 첫 저장(스케줄러)만 유지.
        cur.execute('''
            INSERT INTO round_predictions (round_num, predicted, pick_color, probability)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (round_num) DO NOTHING
        ''', (int(round_num), str(predicted), pick_color, float(probability) if probability is not None else None))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"[경고] round_predictions 저장 실패: {str(e)[:150]}")
        try:
            conn.close()
        except Exception:
            pass
        return False


def _normalize_pick_color_value(color):
    """RED/BLACK 또는 빨강/검정을 일관된 문자열로 통일."""
    if color is None:
        return None
    s = str(color).strip()
    if not s:
        return None
    upper = s.upper()
    if upper in ('RED', '빨강'):
        return '빨강'
    if upper in ('BLACK', '검정'):
        return '검정'
    return s


def _flip_pick_color(color):
    """빨강/검정을 서로 반전. 기타 값은 그대로."""
    if color == '빨강':
        return '검정'
    if color == '검정':
        return '빨강'
    return color


def _get_card_15_color_for_round(results, round_id):
    """해당 회차 게임의 15번째 카드 색상. True=빨강, False=검정, None=미확인.
    pick-color-core-rule: 정/꺽→빨강/검정은 15번 카드 기준. 고정 매핑 금지."""
    if not results or len(results) < 16 or round_id is None:
        return None
    rid = str(round_id)
    for i in range(len(results) - 1):
        if str(results[i].get('gameID')) == rid and i + 1 < len(results):
            return get_card_color_from_result(results[i + 1])
    return None


def _server_calc_effective_pick_and_amount(c):
    """계산기 c의 pending_round 기준으로 배팅 픽(RED/BLACK)과 금액 계산. 매크로 current_pick 반영용.
    반환: (pick_color, amt, pred) — pred는 모양옵션 시 1열·배팅중 일치용."""
    if not c or not c.get('running'):
        return None, 0, None
    pr = c.get('pending_round')
    pred = c.get('pending_predicted')
    if pr is None or pred is None:
        return None, 0, None
    color = _normalize_pick_color_value(c.get('pending_color'))
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
        color = _flip_pick_color(color)
    blended = _blended_win_rate(ph or get_prediction_history(100))
    # 승률반픽: 모양승률이 설정값 이하일 때만 반픽. 모양옵션 블록에서는 메인15 억제 없음
    shape_wr = _get_shape_50_win_rate()
    wr_thr = max(0, min(100, int(c.get('win_rate_threshold') or 50)))
    if c.get('win_rate_reverse') and shape_wr is not None and shape_wr <= wr_thr and not no_reverse_in_streak and (main_rate15 is None or main_rate15 < 53):
        pred = '꺽' if pred == '정' else '정'
        color = _flip_pick_color(color)
    lose_streak = _get_lose_streak_from_history(c.get('history') or [])
    lose_streak_thr = max(0, min(100, int(c.get('lose_streak_reverse_threshold') or 48)))
    lose_streak_min = max(2, min(15, int(c.get('lose_streak_reverse_min_streak') or 3)))
    if c.get('lose_streak_reverse') and lose_streak >= lose_streak_min and blended is not None and blended <= lose_streak_thr and not no_reverse_in_streak and (main_rate15 is None or main_rate15 < 53):
        pred = '꺽' if pred == '정' else '정'
        color = _flip_pick_color(color)
    if c.get('win_rate_direction_reverse') and not no_reverse_in_streak:
        current_round = ph[-1]['round'] if ph else None
        zone = _effective_win_rate_direction_zone(ph, c, current_round)
        if zone == 'high_falling':
            pred = '꺽' if pred == '정' else '정'
            color = _flip_pick_color(color)
            c['last_trend_direction'] = 'down'
        elif zone == 'low_rising':
            c['last_trend_direction'] = 'up'
        elif zone == 'mid_flat' and c.get('last_trend_direction') == 'down':
            pred = '꺽' if pred == '정' else '정'
            color = _flip_pick_color(color)
    if c.get('shape_prediction') and c.get('shape_prediction_reverse'):
        sp10 = _get_shape_prediction_win_rate_10(c)
        thr = max(0, min(100, int(c.get('shape_prediction_reverse_threshold') or 50)))
        if sp10 is not None and sp10 <= thr:
            pred = '꺽' if pred == '정' else '정'
            color = _flip_pick_color(color)
    pick_color = 'RED' if color == '빨강' else ('BLACK' if color == '검정' else None)
    if pick_color is None:
        return None, 0, None
    # 모양: 가장 최근 다음 픽에만 배팅 — 값 있으면 그 픽을 기준으로 반픽/승률반픽 등 적용. 없으면 배팅 안 함
    if c.get('shape_only_latest_next_pick'):
        results = None
        try:
            results = (results_cache or {}).get('results') if results_cache else None
            if not results or len(results) < 16:
                results = get_recent_results(hours=24)
                if results:
                    results = _sort_results_newest_first(results)
        except Exception:
            results = None
        if not results or len(results) < 16:
            return None, 0, None
        latest_next = _get_latest_next_pick_for_chunk(results)
        if not latest_next or latest_next not in ('정', '꺽'):
            return None, 0, None
        pred = latest_next
        is_15_red = get_card_color_from_result(results[14]) if len(results) >= 15 else None
        if is_15_red is True:
            color = '빨강' if pred == '정' else '검정'
        elif is_15_red is False:
            color = '검정' if pred == '정' else '빨강'
        else:
            color = '빨강' if pred == '정' else '검정'
        # 모양 픽에 반픽/승률반픽/연패반픽/승률방향 반픽 적용 (다른 옵션과 중복 사용 가능)
        if c.get('reverse'):
            pred = '꺽' if pred == '정' else '정'
            color = _flip_pick_color(color)
        shape_wr = _get_shape_50_win_rate()
        wr_thr = max(0, min(100, int(c.get('win_rate_threshold') or 50)))
        if c.get('win_rate_reverse') and shape_wr is not None and shape_wr <= wr_thr and not no_reverse_in_streak:
            pred = '꺽' if pred == '정' else '정'
            color = _flip_pick_color(color)
        if c.get('lose_streak_reverse') and lose_streak >= lose_streak_min and blended is not None and blended <= lose_streak_thr and not no_reverse_in_streak and (main_rate15 is None or main_rate15 < 53):
            pred = '꺽' if pred == '정' else '정'
            color = _flip_pick_color(color)
        if c.get('win_rate_direction_reverse') and not no_reverse_in_streak:
            current_round = ph[-1]['round'] if ph else None
            zone = _effective_win_rate_direction_zone(ph, c, current_round)
            if zone == 'high_falling':
                pred = '꺽' if pred == '정' else '정'
                color = _flip_pick_color(color)
                c['last_trend_direction'] = 'down'
            elif zone == 'low_rising':
                c['last_trend_direction'] = 'up'
            elif zone == 'mid_flat' and c.get('last_trend_direction') == 'down':
                pred = '꺽' if pred == '정' else '정'
                color = _flip_pick_color(color)
        pick_color = 'RED' if color == '빨강' else ('BLACK' if color == '검정' else None)
    if c.get('paused'):
        return pick_color, 0, pred
    dummy = {'round': pr, 'actual': 'pending'}
    _calculate_calc_profit_server(c, dummy)
    amt = int(dummy.get('betAmount') or 0)
    return pick_color, amt, pred


def _push_current_pick_from_calc(calculator_id, c):
    """서버에서 계산기 픽을 current_pick에 즉시 반영 — 매크로가 클라이언트를 기다리지 않고 픽 수신."""
    if not bet_int or not DB_AVAILABLE or not DATABASE_URL:
        return
    pick_color, suggested_amount, _ = _server_calc_effective_pick_and_amount(c)
    if pick_color is None:
        return
    conn = get_db_connection(statement_timeout_sec=3)
    if not conn:
        return
    try:
        calc_id = int(calculator_id) if calculator_id in (1, 2, 3) else 1
        pr = c.get('pending_round')
        ok = bet_int.set_current_pick(conn, pick_color=pick_color, round_num=pr,
                                       suggested_amount=suggested_amount, calculator_id=calc_id)
        if ok:
            conn.commit()
            _update_current_pick_relay_cache(calc_id, pr, pick_color, suggested_amount, c.get('running', True))
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


# Relay 캐시: 매크로 폴링 시 DB 없이 즉시 반환. 계산기 POST·스케줄러 푸시 시 갱신.
_current_pick_relay_cache = {1: None, 2: None, 3: None}


def _update_current_pick_relay_cache(calculator_id, round_num, pick_color, suggested_amount, running=True, probability=None):
    """회차·배팅중 픽·금액을 relay 캐시에 즉시 반영. 매크로 GET 시 DB 없이 반환."""
    try:
        cid = int(calculator_id) if calculator_id in (1, 2, 3) else 1
        _current_pick_relay_cache[cid] = {
            'round': round_num,
            'pick_color': pick_color,
            'suggested_amount': suggested_amount,
            'running': running,
            'probability': probability,
        }
    except (TypeError, ValueError):
        pass


# 머지 캐시: 이미 머지한 회차 집합. 새 결과 회차가 생길 때만 머지해서 폴링 시 속도 향상
_merge_rounds_cache = set()


def _merge_round_predictions_into_history(round_actuals, results=None):
    """round_actuals에 있는 회차 중 prediction_history에 없는 것은 round_predictions에서 꺼내 저장 후 삭제.
    results(최신순) 있으면 해당 회차 예측 시점 모양으로 shape_win_stats 갱신.
    새로 결과가 나온 회차가 있을 때만 DB 접근(폴링마다 머지하지 않음)."""
    global _merge_rounds_cache
    if not round_actuals or not DB_AVAILABLE or not DATABASE_URL:
        return
    rounds_with_result = set()
    for rid, ra in round_actuals.items():
        try:
            rnd = int(rid)
        except (TypeError, ValueError):
            continue
        if (ra.get('actual') or '').strip():
            rounds_with_result.add(rnd)
    if not rounds_with_result:
        return
    # 새로 결과가 나온 회차가 없으면 머지 생략 → 폴링 시 응답 속도 향상
    if rounds_with_result <= _merge_rounds_cache:
        return
    conn = get_db_connection(statement_timeout_sec=5)
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute('SELECT round_num FROM prediction_history WHERE round_num = ANY(%s)', (list(rounds_with_result),))
        already = {r[0] for r in cur.fetchall()}
        to_merge = [r for r in rounds_with_result if r not in already]
        for rnd in to_merge:
            cur.execute('SELECT predicted, pick_color, probability FROM round_predictions WHERE round_num = %s LIMIT 1', (rnd,))
            row = cur.fetchone()
            if not row:
                continue
            pred_val, pick_color, prob = row[0], row[1], row[2]
            actual = (round_actuals.get(str(rnd), {}).get('actual') or '').strip()
            if not actual:
                continue
            cur.close()
            conn.close()
            # 예측 시점의 shape_signature를 계산하기 위해 rnd를 제외한 이전 결과 사용
            results_for_shape = None
            if results and len(results) >= 16:
                # rnd 이후의 결과들을 제외하고, rnd 직전까지의 결과만 사용
                filtered_results = [r for r in results if int(str(r.get('gameID', '0')), 10) < rnd]
                if len(filtered_results) >= 16:
                    results_for_shape = filtered_results
            save_prediction_record(rnd, pred_val, actual, probability=prob, pick_color=pick_color, results=results_for_shape)
            if results and len(results) >= 17 and str(results[0].get('gameID')) == str(rnd):
                sig = _get_shape_signature(results[1:])
                if sig:
                    conn2 = get_db_connection(statement_timeout_sec=3)
                    if conn2:
                        try:
                            update_shape_win_stats(conn2, sig, actual, round_num=rnd)
                            chunk_prof = _get_chunk_profile_from_results(results[1:])
                            if chunk_prof:
                                update_chunk_profile_occurrences(conn2, chunk_prof, actual, round_num=rnd)
                        finally:
                            try:
                                conn2.close()
                            except Exception:
                                pass
            conn = get_db_connection(statement_timeout_sec=3)
            if not conn:
                return
            cur = conn.cursor()
            cur.execute('DELETE FROM round_predictions WHERE round_num = %s', (rnd,))
            conn.commit()
        _merge_rounds_cache |= rounds_with_result
    except Exception as e:
        print(f"[경고] round_predictions 머지 실패: {str(e)[:150]}")
    try:
        if conn:
            cur.close()
            conn.close()
    except Exception:
        pass


def _backfill_latest_round_to_prediction_history(results):
    """최신 회차가 prediction_history에 없으면 서버가 예측/실제를 계산해 저장. 화면 미반영으로 누락된 회차 보정."""
    if not results or len(results) < 17:
        return
    try:
        latest_game_id = results[0].get('gameID')
        if not latest_game_id:
            return
        latest_round = int(str(latest_game_id), 10)
        if _prediction_history_has_round(latest_round):
            return
        actual = _get_actual_for_round(results, latest_round)
        if actual is None:
            return
        ph = get_prediction_history(100)
        pred = compute_prediction(results[1:], ph)
        if not pred or pred.get('round') != latest_round or pred.get('value') is None:
            return
        save_prediction_record(
            latest_round, pred['value'], actual,
            probability=pred.get('prob'), pick_color=pred.get('color'),
            results=results[1:] if results and len(results) > 1 else None
        )
        sig = _get_shape_signature(results[1:])
        if sig and DB_AVAILABLE and DATABASE_URL:
            conn = get_db_connection(statement_timeout_sec=3)
            if conn:
                try:
                    update_shape_win_stats(conn, sig, actual, round_num=latest_round)
                    chunk_prof = _get_chunk_profile_from_results(results[1:])
                    if chunk_prof:
                        update_chunk_profile_occurrences(conn, chunk_prof, actual, round_num=latest_round)
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
        print(f"[API] prediction_history 보정 저장: round {latest_round} predicted={pred.get('value')} actual={actual}")
    except Exception as e:
        print(f"[경고] prediction_history 보정 실패: {str(e)[:150]}")


def _update_shape_predicted_in_db(round_num, shape_predicted):
    """해당 회차의 shape_predicted를 DB에 저장. 보정 시 영구 반영용."""
    if not DB_AVAILABLE or not DATABASE_URL or round_num is None or shape_predicted not in ('정', '꺽'):
        return False
    conn = get_db_connection(statement_timeout_sec=3)
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute('UPDATE prediction_history SET shape_predicted = %s WHERE round_num = %s', (str(shape_predicted), int(round_num)))
        conn.commit()
        cur.close()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _backfill_shape_predicted_in_ph(ph, results, max_backfill=0, persist_to_db=False):
    """prediction_history 중 shape_predicted가 null인 회차에 대해 results로 보정.
    기본 비활성화(max_backfill=0): 매 요청마다 50회×(DB 2회+저장 1회)=150회 DB 작업으로 API 지연 발생.
    필요 시 ?backfill=1 로 호출하거나 별도 배치에서 실행."""
    if not ph or not results or len(results) < 16 or max_backfill <= 0:
        return ph
    to_fill = [h for h in ph if h and isinstance(h, dict) and h.get('shape_predicted') not in ('정', '꺽')]
    if not to_fill:
        return ph
    ph_by_round = {h['round']: h for h in ph if h and h.get('round') is not None}
    filled = 0
    for h in to_fill:
        if filled >= max_backfill:
            break
        rnd = h.get('round')
        if rnd is None:
            continue
        filtered = [r for r in results if int(str(r.get('gameID') or '0'), 10) < rnd]
        if len(filtered) < 16:
            continue
        history_before = [ph_by_round[r] for r in sorted(ph_by_round.keys()) if r < rnd][-100:]
        try:
            hint = get_shape_prediction_hint(filtered, history_before)
            if hint and hint.get('value') in ('정', '꺽'):
                h['shape_predicted'] = hint['value']
                if persist_to_db:
                    _update_shape_predicted_in_db(rnd, hint['value'])
                filled += 1
        except Exception:
            pass
    return ph


def get_prediction_history(limit=30):
    """시스템 예측 기록 조회 (최신 N건, round 오름차순 = 과거→현재). statement_timeout으로 먹통 방지."""
    if not DB_AVAILABLE or not DATABASE_URL:
        return []
    conn = get_db_connection(statement_timeout_sec=5)
    if not conn:
        return []
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('''
            SELECT round_num as "round", predicted, actual, probability, pick_color, blended_win_rate, rate_15, rate_30, rate_100, shape_predicted
            FROM prediction_history
            ORDER BY round_num DESC
            LIMIT %s
        ''', (limit,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        # 프론트와 맞추기: 과거→현재 순 (round 오름차순). actualColor = 분석기 승/패 표시와 동일
        out = []
        for r in reversed(rows):
            o = {'round': r['round'], 'predicted': r['predicted'], 'actual': r['actual']}
            if r.get('shape_predicted') in ('정', '꺽'):
                o['shape_predicted'] = r['shape_predicted']
            if r.get('probability') is not None:
                o['probability'] = float(r['probability'])
            if r.get('blended_win_rate') is not None:
                o['blended_win_rate'] = float(r['blended_win_rate'])
            if r.get('rate_15') is not None:
                o['rate_15'] = float(r['rate_15'])
            if r.get('rate_30') is not None:
                o['rate_30'] = float(r['rate_30'])
            if r.get('rate_100') is not None:
                o['rate_100'] = float(r['rate_100'])
            pick_color = str(r.get('pick_color') or '').strip()
            if pick_color:
                # API·프론트 일관성: 항상 빨강/검정으로 반환 (RED/BLACK 혼용 방지)
                o['pickColor'] = '빨강' if pick_color.upper() in ('RED', '빨강') else '검정' if pick_color.upper() in ('BLACK', '검정') else pick_color
                pc = 'RED' if pick_color.upper() in ('RED', '빨강') else 'BLACK' if pick_color.upper() in ('BLACK', '검정') else None
                raw = str(r.get('actual') or '').strip()
                if raw == 'joker':
                    o['actualColor'] = None
                elif raw in ('정', '꺽') and pc:
                    # 상단 예측픽 결과색: 실제 나온 색 표시 (정=예측색과 동일, 꺽=예측색 반대). 반대로 나오던 표시 수정.
                    o['actualColor'] = ('BLACK' if pc == 'RED' else 'RED') if raw == '정' else pc
                else:
                    o['actualColor'] = None
            out.append(o)
        return out
    except Exception as e:
        print(f"[❌ 오류] 예측 기록 조회 실패: {str(e)[:200]}")
        try:
            conn.close()
        except:
            pass
        return []


def parse_card_color(result_str):
    """카드 결과 문자열에서 색상 추출. H,D,♥,♦=빨강 / S,C,♠,♣=검정. 앞뒤 모두 확인."""
    if not result_str:
        return None
    s = str(result_str).upper().strip()
    for c in s:
        if c in ('H', 'D') or c in ('♥', '♦'):
            return True
        if c in ('S', 'C') or c in ('♠', '♣'):
            return False
    if 'RED' in s or 'HEART' in s or 'DIAMOND' in s:
        return True
    if 'BLACK' in s or 'SPADE' in s or 'CLUB' in s:
        return False
    return None


def get_card_color_from_result(r):
    """프론트엔드 getCategory와 동일: result 객체에서 카드 색상 추출. True=RED, False=BLACK, None=미확인.
    red/black 우선(게임 제공값), parse_card_color 보조, 정/꺽+비교카드 유도까지 적용."""
    if not r or r.get('joker'):
        return None
    if r.get('red') and not r.get('black'):
        return True
    if r.get('black') and not r.get('red'):
        return False
    c = parse_card_color(r.get('result', ''))
    return c


def _build_graph_values(results):
    """결과 배열(최신순)에서 그래프용 정/꺽 배열 생성. 인덱스 0이 가장 최신. True=정, False=꺽."""
    if not results or len(results) < 16:
        return []
    out = []
    for i in range(len(results) - 15):
        r0, r15 = results[i], results[i + 15]
        if r0.get('joker') or r15.get('joker'):
            out.append(None)
            continue
        c0 = get_card_color_from_result(r0)
        c15 = get_card_color_from_result(r15)
        if c0 is None or c15 is None:
            out.append(None)
            continue
        out.append(c0 == c15)
    return out


def _calc_transitions(arr):
    """인접 쌍 기준 전이 개수. 정-정(jj), 정-꺽(jk), 꺽-정(kj), 꺽-꺽(kk)."""
    jj = jk = kj = kk = 0
    for i in range(len(arr) - 1):
        a, b = arr[i], arr[i + 1]
        if a is not True and a is not False or b is not True and b is not False:
            continue
        if a is True and b is True:
            jj += 1
        elif a is True and b is False:
            jk += 1
        elif a is False and b is True:
            kj += 1
        else:
            kk += 1
    jung_denom = jj + jk
    kkuk_denom = kk + kj
    return {
        'jj': jj, 'jk': jk, 'kj': kj, 'kk': kk,
        'jungDenom': jung_denom, 'kkukDenom': kkuk_denom,
    }


def _pong_line_pct(arr):
    """퐁당%/줄%. 퐁당=바뀜, 줄=유지."""
    v = [x for x in arr if x is True or x is False]
    if len(v) < 2:
        return 50.0, 50.0
    alt = same = 0
    for i in range(len(v) - 1):
        if v[i] != v[i + 1]:
            alt += 1
        else:
            same += 1
    tot = alt + same
    pong_pct = round(100 * alt / tot, 1) if tot else 50.0
    line_pct = round(100 * same / tot, 1) if tot else 50.0
    return pong_pct, line_pct


def _balance_raw_series(graph_values, window=10):
    """
    구간별 '같은 게 나온 비율' 리스트. docs/BALANCE_SEGMENT_SPEC.md 참고.
    graph_values[i]가 True면 정(빨강), False면 꺽(검정). 줄/퐁당은 연속·교차 패턴(정정/꺽꺽=줄, 정꺽=퐁당).
    반환: [balance_0, balance_1, ...] 각 항목 0.0~1.0 또는 None.
    """
    if not graph_values or len(graph_values) < window:
        return []
    out = []
    for i in range(len(graph_values) - window + 1):
        window_i = graph_values[i : i + window]
        valid = [v for v in window_i if v is True or v is False]
        if not valid:
            out.append(None)
            continue
        out.append(sum(1 for v in valid if v is True) / len(valid))
    return out


def _balance_segment_phase(graph_values, w_balance=10, l_ref=60, p_high=60, p_low=40):
    """
    밸런스(같은 게 나오는 확률) 구간 전환점 캐치. BALANCE_SEGMENT_SPEC.md 참고.
    반환: 'transition_to_low' | 'transition_to_high' | None
    """
    if not graph_values or len(graph_values) < w_balance + 5:
        return None
    balance_raw = _balance_raw_series(graph_values, window=w_balance)
    if len(balance_raw) < 2:
        return None
    ref_values = [balance_raw[i] for i in range(min(l_ref, len(balance_raw))) if balance_raw[i] is not None]
    if len(ref_values) < 20:
        return None
    sorted_ref = sorted(ref_values)
    n = len(sorted_ref)
    idx_high = min(int(n * p_high / 100), n - 1)
    idx_low = min(int(n * p_low / 100), n - 1)
    threshold_high = sorted_ref[idx_high]
    threshold_low = sorted_ref[idx_low]
    if threshold_high <= threshold_low:
        return None

    def _segment(b):
        if b is None:
            return 'mid'
        if b >= threshold_high:
            return 'high'
        if b <= threshold_low:
            return 'low'
        return 'mid'

    current_balance = balance_raw[0]
    prev_balance = balance_raw[1] if len(balance_raw) > 1 else None
    segment = _segment(current_balance)
    prev_segment = _segment(prev_balance)
    if prev_segment == 'high' and segment == 'low':
        return 'transition_to_low'
    if prev_segment == 'low' and segment == 'high':
        return 'transition_to_high'
    return None


def _detect_overall_pong_dominant(graph_values):
    """
    전체 그림 기준: 퐁당이 자주 나오고, 줄이 낮고, 덩어리보다 줄이 많은지 판별.
    이 패턴이면 '올리려고만' 하는 예측(줄 연속)이 연패하므로 퐁당(바뀜) 가중치를 올려야 함.
    반환: bool. True면 전체적으로 퐁당 우세·줄 낮음·덩어리 적음.
    """
    if not graph_values or len(graph_values) < 20:
        return False
    use = graph_values[:50] if len(graph_values) >= 50 else graph_values
    pong_pct, _ = _pong_line_pct(use)
    line_runs, pong_runs = _get_line_pong_runs(use)
    if not line_runs:
        return pong_pct >= 55
    total_line = len(line_runs)
    line_two_plus = sum(1 for l in line_runs if l >= 2)
    avg_line = sum(line_runs) / total_line if total_line else 0
    max_line = max(line_runs) if line_runs else 0
    chunk_ratio = line_two_plus / total_line if total_line else 0
    return (
        pong_pct >= 55 and
        avg_line <= 2.2 and
        max_line <= 4 and
        chunk_ratio <= 0.5
    )


def _get_column_heights(graph_values, max_cols=30):
    """그래프 열 높이(세그먼트 길이) 리스트. 맨 앞=최신. 퐁당(1)/짧은줄(2~3)/장줄(4+) 파악용."""
    if not graph_values or len(graph_values) < 2:
        return []
    filtered = [v for v in graph_values if v is True or v is False]
    if len(filtered) < 2:
        return []
    segments = []
    current, count = filtered[0], 1
    for v in filtered[1:]:
        if v == current:
            count += 1
        else:
            segments.append(count)
            current, count = v, 1
    segments.append(count)
    return segments[:max_cols]


def _get_line_pong_runs(arr):
    """줄(1)/퐁당(0) 쌍으로 run 길이 리스트."""
    pairs = []
    for i in range(len(arr) - 1):
        a, b = arr[i], arr[i + 1]
        if a is not True and a is not False or b is not True and b is not False:
            continue
        pairs.append(1 if a == b else 0)
    line_runs, pong_runs = [], []
    idx = 0
    while idx < len(pairs):
        if pairs[idx] == 1:
            c = 0
            while idx < len(pairs) and pairs[idx] == 1:
                c += 1
                idx += 1
            line_runs.append(c)
        else:
            c = 0
            while idx < len(pairs) and pairs[idx] == 0:
                c += 1
                idx += 1
            pong_runs.append(c)
    return line_runs, pong_runs


def _detect_v_pattern(line_runs, pong_runs, graph_values_head=None):
    """
    V자 패턴 감지: 긴 줄 → 한두 개 퐁당 → 짧은 줄 → 퐁당 → … → 다시 긴 줄로 가는 그래프.
    이 구간에서는 연패가 많아서, 퐁당(바뀜) 쪽 가중치를 올려서 넘기기 쉽게 함.
    graph_values_head: [v0, v1] 최신 2개 (같으면 첫 run이 줄, 다르면 퐁당). 없으면 줄 먼저로 가정.
    반환: (bool) V자 밸런스 구간에 해당하면 True.
    """
    if not line_runs or not pong_runs:
        return False
    first_is_line = True
    if graph_values_head is not None and len(graph_values_head) >= 2:
        a, b = graph_values_head[0], graph_values_head[1]
        if a is True or a is False:
            first_is_line = (a == b)
    # 시간 순서(최신→과거): 첫 run이 줄이면 [line0, pong0, line1, pong1, ...], 퐁당이면 [pong0, line0, pong1, line1, ...]
    if first_is_line:
        long_line = line_runs[0] >= 4
        short_pong_after = len(pong_runs) >= 1 and 1 <= pong_runs[0] <= 2
        short_line_after = len(line_runs) >= 2 and line_runs[1] <= 2
        return long_line and short_pong_after and short_line_after
    else:
        if len(line_runs) < 2 or len(pong_runs) < 2:
            return False
        long_line = line_runs[0] >= 4
        short_pong_after = 1 <= pong_runs[1] <= 2
        short_line_after = line_runs[1] <= 2
        return long_line and short_pong_after and short_line_after


def _detect_u_35_pattern(line_runs):
    """
    U자 + 줄 3~5 구간 감지: 줄 길이가 3~5로 반복되고, 그 전에 짧은 줄(1~2)이 있어 U자 모양인 구간.
    이 구간에서는 줄(유지) 쪽 가중치를 올려서 연패를 줄임.
    반환: (bool) U자·3~5 구간이면 True.
    """
    if not line_runs or len(line_runs) < 3:
        return False
    # 조건 A: 현재 줄 길이가 3~5
    if line_runs[0] not in (3, 4, 5):
        return False
    # 조건 B: 최근에 짧은 줄(1~2)이 있었음 → U자 바닥을 지나 3~5로 올라온 형태
    if line_runs[1] in (1, 2) or line_runs[2] in (1, 2):
        return True
    return False


def _detect_line1_pong1_pattern(line_runs, pong_runs, first_is_line):
    """
    정정꺽꺽정정꺽꺽 같은 덩어리: 줄1·퐁당1·줄1·퐁당1 교차 패턴.
    run 길이가 모두 1이면 (블록이 2개씩 반복) True. 최소 3 run 이상에서 4개가 1,1,1,1이면 인정.
    """
    if not line_runs or not pong_runs:
        return False
    # 최신 순: 첫 run이 줄이면 [line0, pong0, line1, pong1, ...], 퐁당이면 [pong0, line0, ...]
    runs = []
    li, pi = 0, 0
    for i in range(min(8, len(line_runs) + len(pong_runs))):
        if first_is_line:
            if i % 2 == 0 and li < len(line_runs):
                runs.append(line_runs[li])
                li += 1
            elif i % 2 == 1 and pi < len(pong_runs):
                runs.append(pong_runs[pi])
                pi += 1
        else:
            if i % 2 == 0 and pi < len(pong_runs):
                runs.append(pong_runs[pi])
                pi += 1
            elif i % 2 == 1 and li < len(line_runs):
                runs.append(line_runs[li])
                li += 1
    # 정정꺽꺽 한 쌍만 있어도(3 run) 덩어리로 인정; 4 run 이상이면 6개까지 모두 1인지 확인
    if len(runs) < 3:
        return False
    n = min(6, len(runs))
    if all(r == 1 for r in runs[:n]):
        return True
    return False


def _detect_chunk_shape(line_runs, pong_runs, first_is_line):
    """
    덩어리 구간일 때 블록 모양: 321(줄어듦), 123(늘어남), block_repeat(동일 블록 반복).
    line_runs/pong_runs는 최신 순. first_is_line이면 [line0, pong0, line1, pong1, ...]
    """
    if not line_runs or len(line_runs) < 3:
        return None
    # 줄 run 3개로 321 / 123 판별 (최신 순: line_runs[0], [1], [2])
    a, b, c = line_runs[0], line_runs[1], line_runs[2]
    if a >= b >= c and (a > b or b > c):
        return '321'
    if a <= b <= c and (a < b or b < c):
        return '123'
    # 블록 반복: (line, pong) 쌍이 2회 이상 동일. 최신 순 [line0, pong0, line1, pong1, ...]
    pairs = []
    for i in range(min(3, len(line_runs), len(pong_runs))):
        pairs.append((line_runs[i], pong_runs[i]))
    if len(pairs) >= 2 and pairs[0] == pairs[1]:
        return 'block_repeat'
    if len(pairs) >= 3 and pairs[0] == pairs[2]:
        return 'block_repeat'
    return None


def _extract_chunk_profiles(line_runs, pong_runs, first_is_line):
    """
    덩어리 = 2개 이상의 줄들이 이어진 구간. 줄 = 같은 결과 연속(정정/꺽꺽).
    연속 줄 패턴: 2+줄, 1퐁당, 2+줄, 1퐁당. 퐁당 = 1개씩 정꺽정꺽.
    반환: [profile_tuple, ...] - 추출된 덩어리 프로필들. 맨 앞이 최신 덩어리.
    프로필 = (h1, h2, ...) 줄들의 높이(블록 개수). 줄1(꺽꺽정꺽꺽 등) 포함해 (2,1,2)·(1,1) 등 추출.
    """
    if not line_runs and not pong_runs:
        return []
    profiles = []
    chunk_heights = []
    li, pi = 0, 0
    expect_line = first_is_line
    max_iter = len(line_runs) + len(pong_runs)
    for _ in range(max_iter):
        if expect_line and li < len(line_runs):
            run_val = line_runs[li]
            li += 1
            is_line = True
        elif not expect_line and pi < len(pong_runs):
            run_val = pong_runs[pi]
            pi += 1
            is_line = False
        else:
            break
        expect_line = not expect_line

        if is_line:
            if run_val >= 5:
                if len(chunk_heights) >= 2:
                    profiles.append(tuple(chunk_heights[:8]))
                chunk_heights = []
            elif run_val == 1:
                # 줄1 포함: 꺽꺽정꺽꺽 등 (2,1,2)·(1,1) 패턴 추출. 덩어리에 1 추가.
                chunk_heights.append(1)
            elif run_val >= 2:
                chunk_heights.append(run_val)
        else:
            if run_val >= 2:
                if len(chunk_heights) >= 2:
                    profiles.append(tuple(chunk_heights[:8]))
                chunk_heights = []
            elif run_val == 1:
                pass
    if len(chunk_heights) >= 2:
        profiles.append(tuple(chunk_heights[:8]))
    return profiles


def _chunk_profile_similarity(profile_a, profile_b):
    """
    덩어리 프로필 유사도 0~1. 높이 분포 기반.
    같은 길이 + 비슷한 높이 → 높은 유사도. 최근 덩어리와 비슷할 때 가중치 상승용.
    """
    if not profile_a or not profile_b:
        return 0.0
    a, b = list(profile_a), list(profile_b)
    if len(a) != len(b):
        len_penalty = 0.7 ** abs(len(a) - len(b))
    else:
        len_penalty = 1.0
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    diff_sum = sum(abs(a[i] - b[i]) for i in range(n))
    max_diff = n * 5
    height_sim = 1.0 - min(1.0, diff_sum / max(max_diff, 1))
    return height_sim * len_penalty


def _detect_pong_chunk_phase(line_runs, pong_runs, graph_values_head, pong_pct_short, pong_pct_prev):
    """
    퐁당 / 덩어리 / 줄 세 가지 구간 판별. 시각화·예측픽 가중치에 사용.
    - 줄 구간: 한쪽으로 길게 이어짐 (line run >= 5).
    - 퐁당 구간: 2회 이상 바뀜이 이어짐 (pong run >= 2).
    - 덩어리 구간: 꺽줄-정-꺽줄-정 블록 반복, 줄1퐁당1, 또는 줄 2~4. debug에 chunk_shape(321/123/block_repeat) 추가.
    """
    debug = {'first_run_type': None, 'first_run_len': 0, 'pong_pct_short': pong_pct_short, 'pong_pct_prev': pong_pct_prev, 'segment_type': None, 'chunk_shape': None}
    if not line_runs and not pong_runs:
        return None, debug
    first_is_line = True
    if graph_values_head is not None and len(graph_values_head) >= 2:
        a, b = graph_values_head[0], graph_values_head[1]
        if a is True or a is False:
            first_is_line = (a == b)
    if first_is_line and line_runs:
        current_run_len = line_runs[0]
        debug['first_run_type'] = 'line'
        debug['first_run_len'] = current_run_len
    elif not first_is_line and pong_runs:
        current_run_len = pong_runs[0]
        debug['first_run_type'] = 'pong'
        debug['first_run_len'] = current_run_len
    else:
        return None, debug
    # 줄1 퐁당1 줄1 퐁당1 패턴 → 덩어리
    if _detect_line1_pong1_pattern(line_runs, pong_runs, first_is_line):
        if first_is_line:
            debug['segment_type'] = 'chunk'
            debug['chunk_shape'] = _detect_chunk_shape(line_runs, pong_runs, first_is_line)
            return 'chunk_phase', debug
        return None, debug
    # 전환 구간: 직전 15 vs 최근 15 퐁당%
    diff_prev_short = (pong_pct_prev - pong_pct_short) if pong_pct_prev is not None and pong_pct_short is not None else 0
    diff_short_prev = (pong_pct_short - pong_pct_prev) if pong_pct_short is not None and pong_pct_prev is not None else 0
    if diff_prev_short >= 20:
        debug['segment_type'] = 'chunk'
        debug['chunk_shape'] = _detect_chunk_shape(line_runs, pong_runs, True)
        return 'pong_to_chunk', debug
    if diff_short_prev >= 20:
        debug['segment_type'] = 'pong'
        return 'chunk_to_pong', debug
    # 덩어리 직후 퐁당 진입: 맨 앞이 퐁당 run이고, 그 다음(과거)에 줄 run 2 이상 있으면 chunk_to_pong
    if not first_is_line and pong_runs and line_runs and len(pong_runs) >= 1 and line_runs[0] >= 2:
        debug['segment_type'] = 'pong'
        debug['chunk_shape'] = 'chunk_then_pong'
        return 'chunk_to_pong', debug
    # 퐁당 1~2회 직후 긴 줄: 맨 앞이 퐁당 run(1~2), 그 다음(과거)에 줄 run 5 이상 → 줄 쪽 가산(pong_to_chunk)
    if not first_is_line and pong_runs and line_runs and 1 <= current_run_len <= 2 and line_runs[0] >= 5:
        debug['segment_type'] = 'chunk'
        debug['chunk_shape'] = 'pong_then_long_line'
        return 'pong_to_chunk', debug
    if first_is_line:
        # 줄 구간: 긴 줄(5 이상) → 유지(줄) 가중치
        if current_run_len >= 5:
            debug['segment_type'] = 'line'
            return 'line_phase', debug
        # 덩어리: 줄 2~4 또는 1(막 시작)
        debug['segment_type'] = 'chunk'
        debug['chunk_shape'] = _detect_chunk_shape(line_runs, pong_runs, first_is_line)
        if current_run_len >= 2:
            return 'chunk_phase', debug
        return 'chunk_start', debug
    else:
        # 퐁당: 맨 앞이 바뀜(정꺽/꺽정)이면 퐁당 구간. 1회만 있어도 퐁당(덩어리 직후 퐁당 진입 포함)
        if current_run_len >= 1:
            debug['segment_type'] = 'pong'
            return 'pong_phase', debug
        return None, debug


def _compute_blend_data(prediction_history):
    """예측 이력(actual!=joker)으로 15/30/100 구간 반영 확률."""
    valid = [h for h in (prediction_history or []) if h and isinstance(h, dict)]
    outcomes = [ (h.get('actual') == '정') for h in valid if h.get('actual') != 'joker' ]
    outcomes.reverse()
    if len(outcomes) < 2:
        return None
    last_bool = outcomes[0]
    s15 = outcomes[:min(15, len(outcomes))]
    s30 = outcomes[:min(30, len(outcomes))]
    s100 = outcomes[:min(100, len(outcomes))]
    def trans_counts(a):
        jj = jk = kj = kk = 0
        for i in range(len(a) - 1):
            if a[i] is True and a[i+1] is True: jj += 1
            elif a[i] is True and a[i+1] is False: jk += 1
            elif a[i] is False and a[i+1] is True: kj += 1
            else: kk += 1
        return {'jj': jj, 'jk': jk, 'kj': kj, 'kk': kk, 'jungDenom': jj+jk, 'kkukDenom': kk+kj}
    def prob_from_trans(t, last_b):
        if last_b and t['jungDenom'] > 0:
            return t['jj']/t['jungDenom'], t['jk']/t['jungDenom']
        if not last_b and t['kkukDenom'] > 0:
            return t['kk']/t['kkukDenom'], t['kj']/t['kkukDenom']
        return 0.5, 0.5
    t15, t30, t100 = trans_counts(s15), trans_counts(s30), trans_counts(s100)
    r15 = prob_from_trans(t15, last_bool)
    r30 = prob_from_trans(t30, last_bool)
    r100 = prob_from_trans(t100, last_bool)
    p15 = (max(r15[0], r15[1]) * 100) if len(s15) >= 2 else None
    p30 = (max(r30[0], r30[1]) * 100) if len(s30) >= 2 else None
    p100 = (max(r100[0], r100[1]) * 100) if len(s100) >= 2 else None
    w15 = 0.5 if len(s15) >= 2 else 0
    w30 = 0.3 if len(s30) >= 2 else 0
    w100 = 0.2 if len(s100) >= 2 else 0
    denom = w15 + w30 + w100
    new_prob = (w15 * (p15 or 50) + w30 * (p30 or 50) + w100 * (p100 or 50)) / denom if denom > 0 else None
    return {'p15': p15, 'p30': p30, 'p100': p100, 'newProb': new_prob}


def _symmetry_line_for_n(graph_values, n):
    """
    최근 n열만 사용해 좌우 대칭·줄 개수 계산. n=15(8+7), 20(10+10), 30(15+15) 지원.
    반환: dict(symmetryPct, leftLineCount, rightLineCount, avgLeft, avgRight, lineSimilarityPct, maxLeftRunLength, recentRunLength) 또는 None.
    """
    arr = [v for v in graph_values[:n] if v is True or v is False]
    if len(arr) < n:
        return None
    half = n // 2
    pair_count = half  # 15→7, 20→10, 30→15
    left = arr[:half]
    right = arr[half:n]

    def get_run_lengths(a):
        r, cur, c = [], None, 0
        for x in a:
            if x == cur:
                c += 1
            else:
                if cur is not None:
                    r.append(c)
                cur = x
                c = 1
        if cur is not None:
            r.append(c)
        return r

    sym_count = sum(1 for si in range(pair_count) if arr[si] == arr[n - 1 - si])
    left_runs = get_run_lengths(left)
    right_runs = get_run_lengths(right)
    avg_l = sum(left_runs) / len(left_runs) if left_runs else 0
    avg_r = sum(right_runs) / len(right_runs) if right_runs else 0
    line_diff = abs(avg_l - avg_r)
    max_left_run = max(left_runs) if left_runs else 0
    recent_run_len = 1
    for ri in range(1, len(arr)):
        if arr[ri] == arr[0]:
            recent_run_len += 1
        else:
            break
    return {
        'symmetryPct': sym_count / pair_count * 100 if pair_count else 0,
        'avgLeft': avg_l, 'avgRight': avg_r,
        'lineSimilarityPct': max(0, 100 - min(100, line_diff * 25)),
        'leftLineCount': len(left_runs), 'rightLineCount': len(right_runs),
        'maxLeftRunLength': max_left_run, 'recentRunLength': recent_run_len,
    }


def get_shape_prediction_hint(results, prediction_history=None, shape_weight=1.0, chunk_weight=1.0, pong_weight=1.0, symmetry_weight=1.0):
    """모양판별 옵션용: 덩어리 끝 변형·퐁당 가중치 개선된 예측. 기존 compute_prediction 공식 변경 없음.
    shape_weight, chunk_weight, pong_weight, symmetry_weight: 모양판별 계산식 내 각 요소 배율(0~3, 기본 1).
    반환: {'value': '정'|'꺽'|None, 'color': '빨강'|'검정'|None, 'debug': {...}} 또는 None(15번 조커 등)."""
    if not results or len(results) < 16:
        return None
    ph = prediction_history or []
    shape_win_stats = _get_shape_stats_for_results(results)
    chunk_profile_stats = _get_chunk_stats_for_results(results)
    debug = {}
    out = compute_prediction(
        results, ph,
        shape_win_stats=shape_win_stats,
        chunk_profile_stats=chunk_profile_stats,
        use_shape_adjustments=True,
        shape_debug_out=debug,
        shape_weight=shape_weight,
        chunk_weight=chunk_weight,
        pong_weight=pong_weight,
        symmetry_weight=symmetry_weight
    )
    if not out or out.get('value') is None:
        return None
    return {'value': out['value'], 'color': out.get('color', '빨강'), 'debug': debug}


def compute_prediction(results, prediction_history, prev_symmetry_counts=None, shape_win_stats=None, chunk_profile_stats=None, use_shape_adjustments=False, shape_debug_out=None, shape_weight=1.0, chunk_weight=1.0, pong_weight=1.0, symmetry_weight=1.0):
    """
    서버 측 예측 공식. JS와 동일한 입력·출력.
    results: 최신순 결과 리스트, 각 항목 dict(result, joker, gameID 등)
    prediction_history: [{round, predicted, actual}, ...], actual이 'joker'면 제외 후 사용
    prev_symmetry_counts: {left, right} 이전 좌우 줄 개수(선택)
    shape_win_stats: {jung_count, kkeok_count} 저장된 모양별 '그 다음 실제 결과' 누적(선택). 있으면 해당 모양 가중치 반영.
    chunk_profile_stats: {jung_count, kkeok_count} 유사 덩어리 프로필의 '다음 결과' 가중 통계(선택). 최근·유사 덩어리일수록 가중치 높게 반영.
    use_shape_adjustments: True면 모양판별 개선(덩어리 321/줄1퐁당1 퐁당 가산, 덩어리 기본 가중치 축소) 적용.
    shape_debug_out: dict 전달 시 use_shape_adjustments일 때 phase·chunk_shape 등 로그용 정보 채움.
    shape_weight, chunk_weight, pong_weight, symmetry_weight: 모양판별 계산식 내 각 요소 배율(0~3, 기본 1). use_shape_adjustments일 때만 적용.
    반환: {'value': '정'|'꺽'|None, 'round': int, 'prob': float, 'color': '빨강'|'검정'|None}
    15번 카드가 조커면 value=None, color=None (픽 보류).
    좌우 대칭·줄 유사도는 15·20·30열 가중 평균으로 반영(데이터 있으면).
    """
    if not results or len(results) < 16:
        return {'value': None, 'round': 0, 'prob': 0, 'color': None}
    graph_values = _build_graph_values(results)
    if len(graph_values) < 2:
        return {'value': None, 'round': 0, 'prob': 0, 'color': None}
    valid_gv = [v for v in graph_values if v is True or v is False]
    if len(valid_gv) < 2:
        return {'value': None, 'round': 0, 'prob': 0, 'color': None}

    latest_game_id = results[0].get('gameID')
    try:
        current_round_full = int(str(latest_game_id or '0'), 10)
    except (ValueError, TypeError):
        current_round_full = 0
    predicted_round_full = current_round_full + 1

    is_15_joker = len(results) >= 15 and bool(results[14].get('joker'))
    if is_15_joker:
        return {'value': None, 'round': predicted_round_full, 'prob': 0, 'color': None}

    full = _calc_transitions(graph_values)
    recent30 = _calc_transitions(graph_values[:30])
    short15 = _calc_transitions(graph_values[:15]) if len(graph_values) >= 15 else None
    last = graph_values[0]
    pong_pct, line_pct = 50.0, 50.0
    if len([v for v in graph_values[:15] if v is True or v is False]) >= 2:
        pong_pct, line_pct = _pong_line_pct(graph_values[:15])

    use_for_pattern = graph_values[:30]
    line_runs, pong_runs = _get_line_pong_runs(use_for_pattern)
    total_line_runs = len(line_runs)
    total_pong_runs = len(pong_runs)
    line_two_plus = sum(1 for l in line_runs if l >= 2) if total_line_runs else 0
    line_one = sum(1 for l in line_runs if l == 1) if total_line_runs else 0
    line_two = sum(1 for l in line_runs if l == 2) if total_line_runs else 0
    pong_one = sum(1 for p in pong_runs if p == 1) if total_pong_runs else 0
    chunk_idx = line_two_plus / total_line_runs if total_line_runs else 0
    scatter_idx = (line_one / total_line_runs * pong_one / total_pong_runs) if (total_line_runs and total_pong_runs) else 0
    two_one_idx = (line_two / total_line_runs * pong_one / total_pong_runs) if (total_line_runs and total_pong_runs) else 0

    pong_prev15 = 50.0
    if len(graph_values) >= 30:
        pong_prev15, _ = _pong_line_pct(graph_values[15:30])
    line_strong_by_transition = pong_strong_by_transition = False
    if short15:
        long_same = (100 * recent30['jj'] / recent30['jungDenom']) if recent30['jungDenom'] and last is True else (100 * recent30['kk'] / recent30['kkukDenom']) if recent30['kkukDenom'] and last is False else 50
        short_same = (100 * short15['jj'] / short15['jungDenom']) if short15['jungDenom'] and last is True else (100 * short15['kk'] / short15['kkukDenom']) if short15['kkukDenom'] and last is False else 50
        if short_same - long_same >= 15:
            line_strong_by_transition = True
        if long_same - short_same >= 15:
            pong_strong_by_transition = True
    line_strong_by_pong = (pong_prev15 - pong_pct >= 20)
    pong_strong_by_pong = (len(graph_values) >= 30 and pong_pct - pong_prev15 >= 20)
    line_strong = line_strong_by_transition or line_strong_by_pong
    pong_strong = pong_strong_by_transition or pong_strong_by_pong

    surge_unknown = False
    ph = prediction_history or []
    ph_valid = [h for h in ph if h and isinstance(h, dict)]
    if len(ph_valid) >= 5:
        rev_surge = list(reversed(ph_valid))
        i, win_run, lose_run = 0, 0, 0
        while i < len(rev_surge) and rev_surge[i] and rev_surge[i].get('actual') != 'joker':
            is_win = rev_surge[i].get('predicted') == rev_surge[i].get('actual')
            if is_win:
                win_run += 1
                i += 1
            else:
                break
        while i < len(rev_surge) and rev_surge[i] and rev_surge[i].get('actual') != 'joker':
            is_win = rev_surge[i].get('predicted') == rev_surge[i].get('actual')
            if not is_win:
                lose_run += 1
                i += 1
            else:
                break
        if win_run >= 2 and lose_run >= 3:
            surge_unknown = True

    flow_state = ''
    if line_strong:
        flow_state = 'line_strong'
    elif pong_strong:
        flow_state = 'pong_strong'
    elif surge_unknown:
        flow_state = 'surge_unknown'

    # 15·20·30열 각각 계산 후 가중 평균(폭 넓힌 대칭·줄 반영). 데이터 부족 시 사용 가능한 구간만 사용.
    SYM_WINDOWS = (15, 20, 30)
    SYM_WEIGHTS = (0.2, 0.5, 0.3)
    per_n = {}
    for w in SYM_WINDOWS:
        data = _symmetry_line_for_n(graph_values, w)
        if data is not None:
            per_n[w] = data
    symmetry_line_data = None
    symmetry_windows_used = []  # 예측픽에 반영된 구간(15·20·30 중 사용된 열)
    if per_n:
        total_w = 0
        for i, w in enumerate(SYM_WINDOWS):
            if w in per_n:
                total_w += SYM_WEIGHTS[i]
        if total_w > 0:
            symmetry_windows_used = [w for w in SYM_WINDOWS if w in per_n]
            blend = {}
            for key in ('symmetryPct', 'avgLeft', 'avgRight', 'lineSimilarityPct', 'leftLineCount', 'rightLineCount', 'maxLeftRunLength', 'recentRunLength'):
                blend[key] = 0
                for i, w in enumerate(SYM_WINDOWS):
                    if w in per_n:
                        blend[key] += (SYM_WEIGHTS[i] / total_w) * per_n[w][key]
                if key in ('leftLineCount', 'rightLineCount', 'maxLeftRunLength', 'recentRunLength'):
                    blend[key] = round(blend[key])
            symmetry_line_data = blend

    SYM_LINE_PONG_BOOST = 0.15
    SYM_SAME_BOOST = 0.05
    SYM_LOW_MUL = 0.95
    Pjung = Pkkuk = 0.5
    if last is True and recent30['jungDenom'] > 0:
        Pjung = recent30['jj'] / recent30['jungDenom']
        Pkkuk = recent30['jk'] / recent30['jungDenom']
    elif last is False and recent30['kkukDenom'] > 0:
        Pjung = recent30['kj'] / recent30['kkukDenom']
        Pkkuk = recent30['kk'] / recent30['kkukDenom']
    prob_same = Pjung if last is True else Pkkuk
    prob_change = Pkkuk if last is True else Pjung
    line_w = line_pct / 100.0
    pong_w = pong_pct / 100.0
    if flow_state == 'line_strong':
        line_w = min(1.0, line_w + 0.25)
        pong_w = max(0.0, 1.0 - line_w)
    elif flow_state == 'pong_strong':
        pong_w = min(1.0, pong_w + 0.25)
        line_w = max(0.0, 1.0 - pong_w)
    # 전체 그림: 퐁당 자주·줄 낮음·덩어리 적음 → 올리려고만 하면 연패하므로 퐁당 가중치 가산
    overall_pong = _detect_overall_pong_dominant(graph_values)
    if overall_pong:
        pong_w = min(1.0, pong_w + 0.14)
        line_w = max(0.0, 1.0 - pong_w)

    if symmetry_line_data:
        lc = symmetry_line_data['leftLineCount']
        rc = symmetry_line_data['rightLineCount']
        sp = symmetry_line_data['symmetryPct']
        prev_l = (prev_symmetry_counts or {}).get('left')
        prev_r = (prev_symmetry_counts or {}).get('right')
        is_new_segment = (rc >= 5 and lc <= 3)
        is_new_segment_early = (prev_r and prev_r >= 5 and (prev_l is None or prev_l >= 4) and lc <= 3)
        sym_mul = symmetry_weight if use_shape_adjustments else 1.0
        if is_new_segment or is_new_segment_early:
            line_w = min(1.0, line_w + 0.22 * sym_mul)
            pong_w = max(0.0, 1.0 - line_w)
        elif sp >= 70 and rc <= 3:
            line_w = min(1.0, line_w + 0.28 * sym_mul)
            pong_w = max(0.0, 1.0 - line_w)
        else:
            if lc <= 3:
                line_w = min(1.0, line_w + SYM_LINE_PONG_BOOST * sym_mul)
                pong_w = max(0.0, 1.0 - line_w)
            elif lc >= 5:
                max_run = symmetry_line_data.get('maxLeftRunLength', 4)
                recent_run = symmetry_line_data.get('recentRunLength', 0)
                calm_or_run_start = (max_run <= 3) or (recent_run >= 2)
                pong_boost = (0.06 if calm_or_run_start else SYM_LINE_PONG_BOOST) * sym_mul
                pong_w = min(1.0, pong_w + pong_boost)
                line_w = max(0.0, 1.0 - pong_w)
            if sp >= 70:
                line_w = min(1.0, line_w + SYM_SAME_BOOST * sym_mul)
            elif sp <= 30:
                line_w *= (1.0 - (1.0 - SYM_LOW_MUL) * sym_mul)
                pong_w *= (1.0 - (1.0 - SYM_LOW_MUL) * sym_mul)

    line_w += chunk_idx * 0.2 + two_one_idx * 0.1
    pong_w += scatter_idx * 0.2
    # V자 패턴(긴 줄→퐁당 1~2→짧은 줄→…) 구간에서는 연패가 많으므로 퐁당(바뀜) 쪽 가중치 보정
    if _detect_v_pattern(line_runs, pong_runs, use_for_pattern[:2] if len(use_for_pattern) >= 2 else None):
        pong_w += 0.12
        line_w = max(0.0, line_w - 0.06)
    # U자 + 줄 3~5 구간: 연패가 많으므로 줄(유지) 가산·반전(퐁당) 축소. 멈춤 권장.
    u35_detected = _detect_u_35_pattern(line_runs)
    if u35_detected:
        line_w += 0.14
        pong_w = max(0.0, pong_w - 0.07)
    # 줄 길이 보정: 같은 결과가 4개 이상 이어질 때. 줄=유지(정/꺽 구분 없음) → 줄 따라감.
    current_run_len = 1
    for ri in range(1, len(use_for_pattern)):
        v = use_for_pattern[ri]
        if v is True or v is False:
            if v == last:
                current_run_len += 1
            else:
                break
    if current_run_len >= 4:
        line_w += 0.12
        pong_w = max(0.0, pong_w - 0.06)
    # 퐁당 / 덩어리 / 줄 세 가지 구간 보정: phase·chunk_shape에 따라 line_w·pong_w 조정
    pong_chunk_phase = None
    pong_chunk_debug = {}
    phase, pong_chunk_debug = _detect_pong_chunk_phase(
        line_runs, pong_runs,
        use_for_pattern[:2] if len(use_for_pattern) >= 2 else None,
        pong_pct, pong_prev15
    )
    chunk_shape = (pong_chunk_debug or {}).get('chunk_shape')
    # 줄 구간: 한쪽으로 길게 이어짐 → 유지(줄) 가중치 가산
    pong_mul = pong_weight if use_shape_adjustments else 1.0
    if phase == 'line_phase':
        line_w += 0.12
        pong_w = max(0.0, pong_w - 0.06)
        pong_chunk_phase = phase
    # 퐁당 구간: 번갈아 바뀜 → 바뀜 가중치 가산
    elif phase == 'pong_phase' or phase == 'chunk_to_pong':
        pong_w += 0.10 * pong_mul
        line_w = max(0.0, line_w - 0.05 * pong_mul)
        pong_chunk_phase = phase
    # 덩어리 구간: 블록 반복·줄2~4 → 줄 가중치 우선. 321 끝이면 바뀜 소폭 가산
    elif phase in ('chunk_start', 'chunk_phase', 'pong_to_chunk'):
        if use_shape_adjustments:
            line_w += 0.05  # 덩어리 기본 가중치 축소 (위로만 쏠림 완화)
            pong_w = max(0.0, pong_w - 0.025)
            if chunk_shape == '321':
                pong_w += 0.08 * pong_mul  # 321(줄어듦) 구간 퐁당 가산
                line_w = max(0.0, line_w - 0.04 * pong_mul)
            elif _detect_line1_pong1_pattern(line_runs, pong_runs, (use_for_pattern[0] == use_for_pattern[1]) if len(use_for_pattern) >= 2 else True):
                pong_w += 0.05 * pong_mul  # 줄1퐁당1 패턴 퐁당 반영
                line_w = max(0.0, line_w - 0.025 * pong_mul)
        else:
            line_w += 0.10
            pong_w = max(0.0, pong_w - 0.05)
            if chunk_shape == '321':
                pong_w += 0.04 * pong_mul
                line_w = max(0.0, line_w - 0.02 * pong_mul)
        pong_chunk_phase = phase
    # 밸런스 구간 전환점 보정: 서서히 올라갔다 최고점 후 내려가는 등 구간 전환 시 line_w/pong_w 소폭 반영
    balance_phase = _balance_segment_phase(graph_values)
    if balance_phase == 'transition_to_low':
        pong_w += 0.06
        line_w = max(0.0, line_w - 0.03)
    elif balance_phase == 'transition_to_high':
        line_w += 0.06
        pong_w = max(0.0, pong_w - 0.03)
    # 저장된 모양 가중치: 그 모양 다음에 정/꺽이 많았으면 해당 예측 쪽 가산. 줄/퐁당은 last에 따라 결정.
    # 정 많음→정 예측: last 정이면 줄(유지), last 꺽이면 퐁당(바뀜). 꺽 많음→꺽 예측: last 꺽이면 줄, last 정이면 퐁당.
    shape_mul = shape_weight if use_shape_adjustments else 1.0
    if shape_win_stats:
        jc = shape_win_stats.get('jung_count') or 0
        kc = shape_win_stats.get('kkeok_count') or 0
        total = jc + kc
        if total >= 10:
            jr = jc / total if total else 0
            kr = kc / total if total else 0
            if total >= 50:
                w = 0.10 * shape_mul
            elif total >= 20:
                w = 0.07 * shape_mul
            else:
                w = 0.04 * shape_mul
            if jr >= 0.55:
                if last is True:
                    line_w += w
                    pong_w = max(0.0, pong_w - w * 0.5)
                else:
                    pong_w += w
                    line_w = max(0.0, line_w - w * 0.5)
            elif kr >= 0.55:
                if last is False:
                    line_w += w
                    pong_w = max(0.0, pong_w - w * 0.5)
                else:
                    pong_w += w
                    line_w = max(0.0, line_w - w * 0.5)
    # 유사 덩어리 가중치: 정/꺽→줄/퐁당 매핑은 last 반영 (위 shape_win_stats와 동일).
    chunk_mul = chunk_weight if use_shape_adjustments else 1.0
    if chunk_profile_stats:
        jc = chunk_profile_stats.get('jung_count') or 0
        kc = chunk_profile_stats.get('kkeok_count') or 0
        total = jc + kc
        if total >= 1:
            jr = jc / total if total else 0
            kr = kc / total if total else 0
            if total >= 15:
                w = (0.10 if (jr >= 0.6 or kr >= 0.6) else 0.08) * chunk_mul
            elif total >= 5:
                w = 0.07 * chunk_mul
            else:
                w = 0.04 * chunk_mul
            if jr >= 0.55:
                if last is True:
                    line_w += w
                    pong_w = max(0.0, pong_w - w * 0.5)
                else:
                    pong_w += w
                    line_w = max(0.0, line_w - w * 0.5)
            elif kr >= 0.55:
                if last is False:
                    line_w += w
                    pong_w = max(0.0, pong_w - w * 0.5)
                else:
                    pong_w += w
                    line_w = max(0.0, line_w - w * 0.5)
            elif jr >= 0.52 and jr < 0.55:
                w2 = w * 0.4
                if last is True:
                    line_w += w2
                    pong_w = max(0.0, pong_w - w2 * 0.5)
                else:
                    pong_w += w2
                    line_w = max(0.0, line_w - w2 * 0.5)
            elif kr >= 0.52 and kr < 0.55:
                w2 = w * 0.4
                if last is False:
                    line_w += w2
                    pong_w = max(0.0, pong_w - w2 * 0.5)
                else:
                    pong_w += w2
                    line_w = max(0.0, line_w - w2 * 0.5)
    total_w = line_w + pong_w
    if total_w > 0:
        line_w /= total_w
        pong_w /= total_w
    adj_same = prob_same * line_w
    adj_change = prob_change * pong_w
    s = adj_same + adj_change or 1.0
    adj_same_n = adj_same / s
    adj_change_n = adj_change / s
    predict = ('정' if last is True else '꺽') if adj_same_n >= adj_change_n else ('꺽' if last is True else '정')
    pred_prob = (adj_same_n if predict == ('정' if last is True else '꺽') else adj_change_n) * 100
    is_15_red = get_card_color_from_result(results[14]) if len(results) >= 15 else None
    if is_15_red is True:
        color_to_pick = '빨강' if predict == '정' else '검정'
    elif is_15_red is False:
        color_to_pick = '검정' if predict == '정' else '빨강'
    else:
        color_to_pick = '빨강'
    # U+3~5 구간이면 확률 상한 적용(과신 방지)
    if u35_detected and pred_prob > 58:
        pred_prob = 58.0
    if isinstance(pong_chunk_debug, dict):
        pong_chunk_debug = dict(pong_chunk_debug)
        pong_chunk_debug['overall_pong_dominant'] = overall_pong  # 전체 퐁당 우세·줄 낮음·덩어리 적음 (위에서 계산됨)
        col_heights = _get_column_heights(graph_values, 30)
        pong_chunk_debug['column_heights'] = col_heights  # 열 높이 (장줄/짧은줄 파악용)
        long_cols = sum(1 for h in col_heights if h >= 4)
        short_cols = sum(1 for h in col_heights if 2 <= h <= 3)
        pong_cols = sum(1 for h in col_heights if h == 1)
        pong_chunk_debug['long_short_stats'] = {'long': long_cols, 'short': short_cols, 'pong': pong_cols, 'total': len(col_heights)}  # 장줄(4+), 짧은줄(2~3), 퐁당(1)
        pong_chunk_debug['symmetry_windows_used'] = symmetry_windows_used
        pong_chunk_debug['u_shape'] = u35_detected  # U자 구간(연패 많음): 유지 가중치 보정·멈춤 권장
        pong_chunk_debug['balance_phase'] = balance_phase  # 밸런스 구간 전환(transition_to_low/high)
        pong_chunk_debug['shape_signature'] = _get_shape_signature(results)  # 저장·대조용 모양 코드 (S/M/L 구간, 예: L,S,M)
        if shape_win_stats:
            pong_chunk_debug['shape_jung_count'] = shape_win_stats.get('jung_count')
            pong_chunk_debug['shape_kkeok_count'] = shape_win_stats.get('kkeok_count')
        chunk_profile = _get_chunk_profile_from_results(results)
        if chunk_profile:
            pong_chunk_debug['chunk_profile'] = list(chunk_profile)
        if chunk_profile_stats:
            pong_chunk_debug['chunk_profile_jung'] = chunk_profile_stats.get('jung_count')
            pong_chunk_debug['chunk_profile_kkeok'] = chunk_profile_stats.get('kkeok_count')
    if use_shape_adjustments and shape_debug_out is not None and isinstance(shape_debug_out, dict):
        shape_debug_out['phase'] = pong_chunk_phase
        shape_debug_out['chunk_shape'] = chunk_shape
        shape_debug_out['pred'] = predict
        shape_debug_out['prob'] = round(pred_prob, 1)
    return {
        'value': predict, 'round': predicted_round_full, 'prob': round(pred_prob, 1), 'color': color_to_pick,
        'warning_u35': u35_detected,
        'pong_chunk_phase': pong_chunk_phase,
        'pong_chunk_debug': pong_chunk_debug,
    }


def calculate_and_save_color_matches(results):
    """정/꺽 결과 계산 및 저장 (서버 측)"""
    if not DB_AVAILABLE or not DATABASE_URL:
        return
    
    if len(results) < 16:
        return  # 최소 16개 필요
    
    conn = get_db_connection(statement_timeout_sec=10)
    if not conn:
        return
    
    try:
        cur = conn.cursor()
        saved_count = 0
        
        # 1번째~15번째 카드를 16번째~30번째 카드와 비교
        for i in range(min(15, len(results) - 15)):
            current_result = results[i]
            compare_result = results[i + 15]
            
            current_game_id = str(current_result.get('gameID', ''))
            compare_game_id = str(compare_result.get('gameID', ''))
            
            # 조커 카드는 비교 불가
            if current_result.get('joker') or compare_result.get('joker'):
                continue
            
            if not current_game_id or not compare_game_id:
                continue
            
            # 색상 비교
            current_color = get_card_color_from_result(current_result)
            compare_color = get_card_color_from_result(compare_result)
            if current_color is None or compare_color is None:
                continue
            
            match_result = (current_color == compare_color)  # True = 정, False = 꺽
            
            # DB에 저장 (중복 시 업데이트)
            try:
                cur.execute('''
                    INSERT INTO color_matches (game_id, compare_game_id, match_result)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (game_id, compare_game_id) 
                    DO UPDATE SET match_result = EXCLUDED.match_result
                ''', (current_game_id, compare_game_id, match_result))
                saved_count += 1
            except Exception as e:
                print(f"[경고] 정/꺽 결과 저장 실패: {str(e)[:100]}")
        
        conn.commit()
        cur.close()
        conn.close()
        
        if saved_count > 0:
            _log_when_changed('color_matches', saved_count, lambda v: f"[✅] 정/꺽 결과 {v}개 저장 완료")
    except Exception as e:
        print(f"[❌ 오류] 정/꺽 결과 계산 실패: {str(e)[:200]}")
        try:
            conn.close()
        except:
            pass


def get_color_matches_batch(conn, pairs):
    """정/꺽 결과 일괄 조회 (동일 conn 사용, 먹통 방지). pairs: [(game_id, compare_game_id), ...]. 반환: {(gid, cgid): match_result}"""
    if not conn or not pairs:
        return {}
    try:
        cur = conn.cursor()
        conditions = []
        params = []
        for gid, cgid in pairs:
            conditions.append('(game_id = %s AND compare_game_id = %s)')
            params.extend([str(gid), str(cgid)])
        cur.execute(
            'SELECT game_id, compare_game_id, match_result FROM color_matches WHERE ' + ' OR '.join(conditions),
            params
        )
        out = {}
        for row in cur.fetchall():
            out[(str(row[0]), str(row[1]))] = row[2]
        cur.close()
        return out
    except Exception as e:
        print(f"[경고] get_color_matches_batch 오류: {str(e)[:100]}")
        return {}


def get_color_match(game_id, compare_game_id):
    """정/꺽 결과 조회 (단일)"""
    if not DB_AVAILABLE or not DATABASE_URL:
        return None
    
    conn = get_db_connection(statement_timeout_sec=5)
    if not conn:
        return None
    
    try:
        cur = conn.cursor()
        cur.execute('''
            SELECT match_result
            FROM color_matches
            WHERE game_id = %s AND compare_game_id = %s
        ''', (str(game_id), str(compare_game_id)))
        
        row = cur.fetchone()
        cur.close()
        conn.close()
        
        if row:
            return row[0]  # boolean 값 반환
        return None
    except Exception as e:
        print(f"[❌ 오류] 정/꺽 결과 조회 실패: {str(e)[:200]}")
        try:
            conn.close()
        except:
            pass
        return None

def save_color_match(game_id, compare_game_id, match_result):
    """정/꺽 결과 저장 (단일). statement_timeout으로 먹통 방지."""
    if not DB_AVAILABLE or not DATABASE_URL:
        return False
    
    conn = get_db_connection(statement_timeout_sec=3)
    if not conn:
        return False
    
    try:
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO color_matches (game_id, compare_game_id, match_result)
            VALUES (%s, %s, %s)
            ON CONFLICT (game_id, compare_game_id) 
            DO UPDATE SET match_result = EXCLUDED.match_result
        ''', (str(game_id), str(compare_game_id), match_result))
        
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"[❌ 오류] 정/꺽 결과 저장 실패: {str(e)[:200]}")
        try:
            conn.close()
        except:
            pass
        return False

def _sort_results_newest_first(results):
    """결과를 gameID 기준 최신순(높은 ID 먼저)으로 정렬. 그래프/표시 순서 일관성 유지."""
    if not results:
        return results
    def key_fn(r):
        g = str(r.get('gameID') or '')
        nums = re.findall(r'\d+', g)
        n = int(nums[0]) if nums else 0
        return (-n, g)  # 숫자 추출해서 높은 ID가 앞으로
    return sorted(results, key=key_fn)


def get_recent_results(hours=24):
    """최근 N시간 데이터 조회 (정/꺽 결과 포함). 규칙: 24h 구간으로 최신 회차 누락 방지. statement_timeout·LIMIT으로 먹통 방지."""
    if not DB_AVAILABLE or not DATABASE_URL:
        return []
    
    conn = get_db_connection(statement_timeout_sec=8)
    if not conn:
        return []
    
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # 최근 N시간 데이터 조회, LIMIT 2000. 회차(game_id) 숫자 기준 최신순으로 정렬 (화면에 현재 회차 표시 보장)
        cur.execute('''
            SELECT game_id as "gameID", result, hi, lo, red, black, jqka, joker, 
                   hash_value as hash, salt_value as salt
            FROM game_results
            WHERE created_at >= NOW() - (INTERVAL '1 hour' * %s)
            ORDER BY (NULLIF(REGEXP_REPLACE(game_id::text, '[^0-9]', '', 'g'), '')::BIGINT) DESC NULLS LAST, created_at DESC
            LIMIT 2000
        ''', (int(hours),))
        
        results = []
        for row in cur.fetchall():
            results.append({
                'gameID': str(row['gameID']),
                'result': row['result'] or '',
                'hi': row['hi'] or False,
                'lo': row['lo'] or False,
                'red': row['red'] or False,
                'black': row['black'] or False,
                'jqka': row['jqka'] or False,
                'joker': row['joker'] or False,
                'hash': row['hash'] or '',
                'salt': row['salt'] or ''
            })
        
        if len(results) >= 16:
            calculate_and_save_color_matches(results)
        
        # 정/꺽 정보: 동일 conn으로 일괄 조회 (15회 개별 쿼리 제거)
        pairs = []
        pair_to_idx = {}
        for i in range(min(15, len(results))):
            if i + 15 >= len(results):
                break
            if results[i].get('joker') or results[i + 15].get('joker'):
                results[i]['colorMatch'] = None
                continue
            gid = results[i].get('gameID')
            cgid = results[i + 15].get('gameID')
            if not gid or not cgid:
                results[i]['colorMatch'] = None
                continue
            pairs.append((gid, cgid))
            pair_to_idx[(gid, cgid)] = i
        batch = get_color_matches_batch(conn, pairs)
        for (gid, cgid), match_result in batch.items():
            if (gid, cgid) in pair_to_idx:
                results[pair_to_idx[(gid, cgid)]]['colorMatch'] = match_result
        to_save = []
        for (gid, cgid), idx in pair_to_idx.items():
            if 'colorMatch' not in results[idx]:
                current_color = get_card_color_from_result(results[idx])
                compare_color = get_card_color_from_result(results[idx + 15])
                if current_color is not None and compare_color is not None:
                    results[idx]['colorMatch'] = (current_color == compare_color)
                    to_save.append((gid, cgid, results[idx]['colorMatch']))
                else:
                    results[idx]['colorMatch'] = None
        if to_save:
            try:
                for gid, cgid, match_result in to_save:
                    cur.execute('''
                        INSERT INTO color_matches (game_id, compare_game_id, match_result)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (game_id, compare_game_id) DO UPDATE SET match_result = EXCLUDED.match_result
                    ''', (gid, cgid, match_result))
                conn.commit()
            except Exception as e:
                print(f"[경고] 정/꺽 일괄 저장 실패: {str(e)[:100]}")
        
        cur.close()
        conn.close()
        return _sort_results_newest_first(results)
    except Exception as e:
        print(f"[❌ 오류] 게임 결과 조회 실패: {str(e)[:200]}")
        try:
            conn.close()
        except Exception:
            pass
        return []

def cleanup_old_results(hours=5):
    """5시간이 지난 데이터 삭제"""
    if not DB_AVAILABLE or not DATABASE_URL:
        return
    
    conn = get_db_connection()
    if not conn:
        return
    
    try:
        cur = conn.cursor()
        
        # N시간 이전 데이터 삭제
        cur.execute('''
            DELETE FROM game_results
            WHERE created_at < NOW() - (INTERVAL '1 hour' * %s)
        ''', (int(hours),))
        
        deleted_count = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        
        if deleted_count > 0:
            print(f"[🗑️] 오래된 데이터 {deleted_count}개 삭제 완료")
    except Exception as e:
        print(f"[❌ 오류] 오래된 데이터 삭제 실패: {str(e)[:200]}")
        try:
            conn.close()
        except:
            pass

# 캐시
game_data_cache = None
streaks_cache = None
results_cache = None
last_update_time = 0
CACHE_TTL = 1000  # 결과 캐시 유효 시간 (ms). 1초 동안 동일 캐시 반환, 스케줄러가 1초마다 선제 갱신

# 게임 상태 (Socket.IO 제거 후 기본값만 사용)
current_status_data = {
    'round': 0,
    'elapsed': 0,
    'currentBets': {
        'red': [],
        'black': []
    },
    'timestamp': datetime.now().isoformat()
}

def fetch_with_retry(url, max_retries=MAX_RETRIES, silent=False, timeout_sec=None):
    """재시도 로직 포함 fetch. timeout_sec 지정 시 해당 초 단위 타임아웃 사용 (먹통 방지)."""
    timeout = timeout_sec if timeout_sec is not None else TIMEOUT
    for attempt in range(max_retries):
        try:
            response = requests.get(
                url,
                timeout=timeout,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Cache-Control': 'no-cache',
                    'Pragma': 'no-cache',
                    'Accept': 'application/json, text/plain, */*',
                    'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
                    'Referer': f'{BASE_URL}/',
                    'Origin': BASE_URL,
                    'Connection': 'keep-alive',
                    'Sec-Fetch-Dest': 'empty',
                    'Sec-Fetch-Mode': 'cors',
                    'Sec-Fetch-Site': 'same-origin'
                },
                allow_redirects=True  # 리다이렉트 허용
            )
            response.raise_for_status()
            
            # 응답 내용 확인 (디버깅)
            if not silent:
                print(f"[✅ 요청 성공] {url}")
                print(f"   상태: {response.status_code}, 크기: {len(response.content)} bytes")
                print(f"   Content-Type: {response.headers.get('Content-Type', 'unknown')}")
                # JSON인 경우 샘플 출력
                if 'application/json' in response.headers.get('Content-Type', ''):
                    try:
                        sample = response.json()
                        if isinstance(sample, dict):
                            print(f"   JSON 키: {list(sample.keys())[:10]}")
                        elif isinstance(sample, list):
                            print(f"   JSON 배열 길이: {len(sample)}")
                    except:
                        pass
            
            return response
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else 0
            if status_code == 404:
                # 404는 조용히 처리 (파일이 없을 수 있음)
                if not silent:
                    print(f"[❌ 404] 파일 없음: {url}")
                return None
            if not silent and attempt == max_retries - 1:
                print(f"[❌ HTTP 오류] {status_code}: {url}")
                if e.response:
                    print(f"   응답 내용: {e.response.text[:300]}")
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            if not silent:
                print(f"[❌ 요청 오류] {url}")
                print(f"   오류 내용: {str(e)[:200]}")
    return None

# 데이터베이스 초기화 함수 (나중에 호출)
def ensure_database_initialized():
    """데이터베이스 초기화 확인 및 실행"""
    if not DB_AVAILABLE:
        print("[❌ 경고] psycopg2가 설치되지 않았습니다")
        return False
    
    if not DATABASE_URL:
        print("[❌ 경고] DATABASE_URL 환경 변수가 설정되지 않았습니다")
        return False
    
    try:
        result = init_database()
        if result:
            print("[✅] 데이터베이스 초기화 성공")
        else:
            print("[❌ 경고] 데이터베이스 초기화 실패 (init_database()가 False 반환)")
        return result
    except Exception as e:
        import traceback
        print(f"[❌ 오류] 데이터베이스 초기화 실패: {str(e)}")
        print(f"[❌ 오류] 트레이스백:\n{traceback.format_exc()}")
        return False

# 모듈 로드 시 DB 초기화는 백그라운드 스레드에서 (앱 시작 블로킹 방지). 헬스체크 통과 후 실행
def _run_db_init():
    try:
        time.sleep(20)
        ensure_database_initialized()
    except Exception as e:
        print(f"[❌ 오류] DB 초기화 실패: {str(e)}")

print("[🔄] 모듈 로드 시 데이터베이스 초기화는 백그라운드에서 실행됩니다.")
if DB_AVAILABLE and DATABASE_URL:
    _db_init_thread = threading.Thread(target=_run_db_init, daemon=True)
    _db_init_thread.start()
elif not DATABASE_URL:
    print("[❌ 경고] DATABASE_URL이 None입니다. 환경 변수를 확인하세요.")
else:
    print("[❌ 경고] DB_AVAILABLE이 False입니다. psycopg2를 설치하세요.")

def load_game_data():
    """게임 데이터 로드 (Socket.IO 제거 후 기본값만 반환)"""
    global current_status_data
    return {
        'round': current_status_data.get('round', 0),
        'elapsed': current_status_data.get('elapsed', 0),
        'currentBets': current_status_data.get('currentBets', {'red': [], 'black': []}),
        'timestamp': current_status_data.get('timestamp', datetime.now().isoformat())
    }

# 외부 result.json 요청 시 타임아웃 (병렬: 경로당 4초, 전체 6초)
RESULTS_FETCH_TIMEOUT_PER_PATH = 4
RESULTS_FETCH_OVERALL_TIMEOUT = 6
RESULTS_FETCH_MAX_RETRIES = 1


def _parse_results_json(data):
    """response.json() 결과를 파싱해 results 리스트 반환. 실패 시 None."""
    if not isinstance(data, list):
        return None
    results = []
    for game in data:
        try:
            game_id = game.get('gameID', '')
            result = game.get('result', '')
            json_str = game.get('json', '{}')
            if isinstance(json_str, str):
                json_data = json.loads(json_str)
            else:
                json_data = json_str
            red_val = json_data.get('red') or game.get('red', False)
            black_val = json_data.get('black') or game.get('black', False)
            results.append({
                'gameID': str(game_id),
                'result': result,
                'hi': json_data.get('hi', False),
                'lo': json_data.get('lo', False),
                'red': red_val,
                'black': black_val,
                'jqka': json_data.get('jqka', False),
                'joker': json_data.get('joker', False),
                'hash': game.get('hash', ''),
                'salt': game.get('salt', '')
            })
        except Exception:
            continue
    return results if results else None


def _fetch_one_result_path(url_path, timeout_sec):
    """단일 경로 result.json 요청. 반환: response 또는 None."""
    url = f"{url_path}?t={int(time.time() * 1000)}"
    return fetch_with_retry(
        url,
        max_retries=RESULTS_FETCH_MAX_RETRIES,
        silent=True,
        timeout_sec=timeout_sec,
    )


def load_results_data(base_url=None):
    """경기 결과 데이터 로드 (result.json). 여러 경로 병렬 요청해 먼저 성공한 결과 사용 → 회차 갱신."""
    base = (base_url or '').rstrip('/') or BASE_URL
    possible_paths = [
        f"{base}/frame/hilo/result.json",
        f"{base}/result.json",
        f"{base}/hilo/result.json",
        f"{base}/frame/result.json",
        f"{base}/api/result.json",
        f"{base}/game/result.json",
    ]
    executor = ThreadPoolExecutor(max_workers=min(6, len(possible_paths)))
    try:
        future_to_path = {
            executor.submit(_fetch_one_result_path, p, RESULTS_FETCH_TIMEOUT_PER_PATH): p
            for p in possible_paths
        }
        for future in as_completed(future_to_path, timeout=RESULTS_FETCH_OVERALL_TIMEOUT):
            url_path = future_to_path[future]
            try:
                response = future.result()
                if not response:
                    continue
                try:
                    data = response.json()
                except (ValueError, json.JSONDecodeError):
                    continue
                results = _parse_results_json(data)
                if results:
                    _log_when_changed(('result_success', url_path), (url_path, len(results)), lambda v: f"[✅ 결과 데이터 성공] {v[0]} ({v[1]}개)")
                    executor.shutdown(wait=False)
                    if DB_AVAILABLE and DATABASE_URL and base == BASE_URL:
                        saved_count = 0
                        for game_data in results:
                            if save_game_result(game_data):
                                saved_count += 1
                        if saved_count > 0:
                            _log_when_changed('db_save', saved_count, lambda v: f"[💾] 데이터베이스에 {v}개 결과 저장 완료")
                        if len(results) >= 16:
                            calculate_and_save_color_matches(results)
                    return results
            except Exception as e:
                print(f"[결과 데이터 오류] {url_path}: {str(e)[:80]}")
                continue
    except Exception as e:
        print(f"[경고] 결과 병렬 요청 오류: {str(e)[:150]}")
    finally:
        try:
            executor.shutdown(wait=True)
        except Exception:
            pass
    print(f"[경고] 모든 경로에서 결과 데이터를 가져올 수 없음")
    return []


def _update_relay_cache_for_running_calcs():
    """실행 중인 계산기 relay 캐시 갱신. 결과 유무와 관계없이 0.2초마다 호출 → 전회차 금액 송출·20000 고정 방지."""
    if not DB_AVAILABLE or not DATABASE_URL:
        return
    try:
        session_ids = _get_all_calc_session_ids()
        for session_id in session_ids:
            state = get_calc_state(session_id)
            if not state or not isinstance(state, dict):
                continue
            for cid in ('1', '2', '3'):
                c = state.get(cid)
                if not c or not isinstance(c, dict) or not c.get('running'):
                    continue
                try:
                    pick_color, suggested_amount, _ = _server_calc_effective_pick_and_amount(c)
                    if pick_color is not None:
                        pr = c.get('pending_round')
                        _update_current_pick_relay_cache(int(cid), pr, pick_color, suggested_amount, c.get('running', True))
                except Exception:
                    pass
    except Exception:
        pass


def _scheduler_fetch_results():
    """스케줄러에서 호출: results_cache 갱신 + DB 저장 + 현재 회차 예측 1회 저장(한 곳) + 계산기 회차 반영 + prediction_history 누락 보정."""
    try:
        _refresh_results_background()
        if DB_AVAILABLE and DATABASE_URL:
            results = get_recent_results(hours=24)
            if results and len(results) >= 16:
                ensure_stored_prediction_for_current_round(results)
                _apply_results_to_calcs(results)
                _backfill_latest_round_to_prediction_history(results)
            # relay 캐시: 결과 유무와 관계없이 항상 갱신 (전회차 금액 송출·20000 고정 방지)
            _update_relay_cache_for_running_calcs()
    except Exception as e:
        print(f"[스케줄러] 결과 수집/회차 반영 오류: {str(e)[:150]}")


def _scheduler_trim_shape_tables():
    """5분마다 모양·덩어리 테이블 행 수 상한 초과 시 오래된 행 삭제."""
    if not DB_AVAILABLE or not DATABASE_URL:
        return
    conn = get_db_connection(statement_timeout_sec=10)
    if conn:
        try:
            _trim_shape_tables(conn)
        finally:
            try:
                conn.close()
            except Exception:
                pass


if SCHEDULER_AVAILABLE:
    _scheduler = BackgroundScheduler()
    # 배팅시간 확보: 0.2초마다 실행 → 픽/금액 DB 반영을 빠르게 해 매크로가 곧바로 가져가도록
    _scheduler.add_job(_scheduler_fetch_results, 'interval', seconds=0.2, id='fetch_results', max_instances=1)
    _scheduler.add_job(_scheduler_trim_shape_tables, 'interval', seconds=300, id='trim_shape', max_instances=1)
    def _start_scheduler_delayed():
        time.sleep(25)
        _scheduler.start()
        print("[✅] 결과 수집 스케줄러 시작 (0.2초마다, 픽/금액 빠른 반영)")
    threading.Thread(target=_start_scheduler_delayed, daemon=True).start()
    print("[⏳] 스케줄러는 25초 후 시작 (DB init 20초 후)")
else:
    print("[⚠] APScheduler 미설치 - 결과 수집은 브라우저 요청 시에만 동작합니다. pip install APScheduler")

def parse_csv_data(csv_text):
    """CSV 데이터 파싱 (bet_result_log.csv)"""
    valid_games = []
    lines = csv_text.split('\n')
    
    # 헤더 제외하고 파싱
    for i in range(1, len(lines)):
        line = lines[i].strip()
        if not line:
            continue
        
        try:
            parts = line.split(',')
            if len(parts) < 7:
                continue
            
            round_num = int(parts[1])
            account = parts[2].strip() if len(parts) > 2 else None
            category = parts[3].strip().lower() if len(parts) > 3 else None
            result = parts[5].strip().lower() if len(parts) > 5 else None
            
            # 유효성 검증
            if not account or not category or not result:
                continue
            if category not in ['red', 'black', 'hi', 'lo']:
                continue
            if result not in ['win', 'lose']:
                continue
            if round_num <= 0:
                continue
            
            valid_games.append({
                'round': round_num,
                'account': account,
                'category': category,
                'result': result
            })
        except (ValueError, IndexError):
            continue
    
    # 라운드 순으로 정렬
    valid_games.sort(key=lambda x: x['round'])
    return valid_games

def calculate_streaks(valid_games):
    """연승 계산"""
    streaks = {}
    
    for game in valid_games:
        key = f"{game['account']}_{game['category']}"
        
        if key not in streaks:
            streaks[key] = 0
        
        if game['result'] == 'win':
            streaks[key] += 1
        else:
            streaks[key] = 0
    
    # userStreaks 형태로 변환
    user_streaks = {}
    for key, streak_value in streaks.items():
        parts = key.split('_')
        if len(parts) != 2:
            continue
        
        account, category = parts
        if category not in ['red', 'black', 'hi', 'lo']:
            continue
        
        if account not in user_streaks:
            user_streaks[account] = {'red': 0, 'black': 0, 'hi': 0, 'lo': 0}
        
        user_streaks[account][category] = streak_value
    
    return user_streaks

def load_streaks_data():
    """연승 데이터 로드 (타임아웃으로 먹통 방지)"""
    try:
        url = f"{BASE_URL}/bet_result_log.csv?t={int(time.time() * 1000)}"
        response = fetch_with_retry(url, timeout_sec=6)
        
        if not response:
            raise Exception("CSV 데이터 로드 실패")
        
        csv_text = response.text
        if not csv_text or not csv_text.strip():
            raise Exception("CSV 파일이 비어있습니다")
        
        valid_games = parse_csv_data(csv_text)
        user_streaks = calculate_streaks(valid_games)
        
        return {
            'userStreaks': user_streaks,
            'validGames': len(valid_games),
            'timestamp': datetime.now().isoformat()
        }
    except Exception as e:
        print(f"연승 데이터 로드 오류: {e}")
        return None

# HTML 템플릿
RESULTS_HTML = '''
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🎲 토큰하이로우 경기 결과</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            background: #2a2a3e;
            color: #fff;
            font-family: 'Consolas', monospace;
            padding: 10px;
        }
        .container {
            max-width: 100%;
            margin: 0 auto;
            padding: 0 clamp(8px, 2vw, 16px);
        }
        .header-info {
            margin-bottom: 15px;
            padding: 12px;
            background: rgba(255,255,255,0.05);
            border-radius: 5px;
            font-size: clamp(0.8em, 2vw, 0.9em);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .header-info div {
            margin: 0 10px;
        }
        .remaining-time {
            font-weight: bold;
            color: #4caf50;
        }
        .remaining-time.warning {
            color: #ffaa00;
        }
        .remaining-time.danger {
            color: #f44336;
        }
        .cards-container {
            display: flex;
            gap: clamp(2px, 1.2vw, 12px);
            padding: 15px 0;
            flex-wrap: nowrap;
            width: 100%;
            min-width: 0;
        }
        .card-wrapper {
            display: flex;
            flex-direction: column;
            align-items: center;
            flex: 0 0 calc((100% - (14 * clamp(2px, 1.2vw, 12px))) / 15);
            min-width: 0;
        }
        .card-wrapper .card {
            width: 100% !important;
            max-width: clamp(22px, 6.5vw, 54px) !important;
            height: auto !important;
            aspect-ratio: 54 / 48;
            min-height: clamp(20px, 5.8vw, 48px) !important;
        }
        .card {
            width: 100%;
            max-width: clamp(22px, 6.5vw, 54px);
            height: auto;
            aspect-ratio: 54 / 48;
            min-height: clamp(20px, 5.8vw, 48px);
            background: #fff;
            border: clamp(2px, 0.5vw, 3px) solid #000;
            border-radius: clamp(4px, 1.2vw, 10px);
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            padding: clamp(1px, 0.4vw, 4px);
            box-shadow: 0 4px 12px rgba(0,0,0,0.4);
        }
        .card.red {
            background: #d32f2f;
            color: #fff;
        }
        .card.black {
            color: #000;
        }
        .card-suit-icon {
            font-size: clamp(8px, 1.8vw, 14px);
            line-height: 1;
            margin-bottom: clamp(1px, 0.3vw, 2px);
        }
        .card-value {
            font-size: clamp(9px, 2.2vw, 18px);
            font-weight: bold;
            text-align: center;
            line-height: 1;
        }
        .card-category {
            margin-top: clamp(2px, 0.5vw, 5px);
            font-size: clamp(7px, 1.6vw, 16px);
            font-weight: bold;
            padding: clamp(2px, 0.4vw, 4px) clamp(4px, 0.8vw, 8px);
            border-radius: clamp(3px, 0.8vw, 5px);
            white-space: nowrap;
            width: 100%;
            text-align: center;
        }
        .card-category.hi {
            background: #4caf50;
            color: #fff;
        }
        .card-category.lo {
            background: #2196f3;
            color: #fff;
        }
        .card-category.joker {
            background: #2196f3;
            color: #fff;
            font-size: clamp(8px, 1.5vw, 12px);
        }
        .card-category.draw {
            background: #ff9800;
            color: #fff;
        }
        .card-category.red-only {
            background: #f44336;
            color: #fff;
        }
        .card-category.black-only {
            background: #424242;
            color: #fff;
        }
        .color-match {
            margin-top: clamp(2px, 0.5vw, 5px);
            font-size: clamp(7px, 1.6vw, 16px);
            font-weight: bold;
            padding: clamp(2px, 0.4vw, 4px) clamp(4px, 0.8vw, 8px);
            border-radius: clamp(3px, 0.8vw, 5px);
            white-space: nowrap;
            width: 100%;
            text-align: center;
        }
        .color-match.jung {
            background: #4caf50;
            color: #fff;
        }
        .color-match.kkuk {
            background: #f44336;
            color: #fff;
        }
        /* 정/꺽 블록 그래프: 좌=최신, 같은 타입 세로로 쌓기, 반응형(모바일에서 박스·간격 축소) */
        .jung-kkuk-graph {
            margin-top: 8px;
            display: flex;
            flex-direction: row;
            justify-content: flex-start;
            align-items: flex-end;
            gap: clamp(3px, 1.2vw, 6px);
            flex-wrap: nowrap;
            overflow-x: auto;
            overflow-y: hidden;
            max-width: 100%;
            padding-bottom: clamp(2px, 1vw, 4px);
        }
        .jung-kkuk-graph .graph-column {
            display: flex;
            flex-direction: column;
            gap: clamp(2px, 0.6vw, 3px);
            align-items: center;
        }
        .jung-kkuk-graph .graph-block {
            font-size: clamp(9px, 2vw, 14px);
            font-weight: bold;
            padding: clamp(2px, 1vw, 4px) clamp(4px, 2vw, 10px);
            min-width: clamp(20px, 5vw, 36px);
            min-height: clamp(18px, 4vw, 28px);
            box-sizing: border-box;
            border-radius: clamp(3px, 1vw, 5px);
            white-space: nowrap;
            text-align: center;
            color: #fff;
        }
        .jung-kkuk-graph .graph-block.jung {
            background: #4caf50;
        }
        .jung-kkuk-graph .graph-block.kkuk {
            background: #f44336;
        }
        .jung-kkuk-graph .graph-column-num { font-size: 12px; font-weight: 600; color: #bbb; margin-top: 3px; }
        .graph-stats {
            margin-top: 0;
            font-size: clamp(12px, 2vw, 14px);
            color: #fff;
            overflow-x: auto;
        }
        .graph-stats table {
            border-collapse: collapse;
            margin: 0 auto;
            min-width: 260px;
        }
        .graph-stats th, .graph-stats td {
            border: 1px solid #666;
            padding: clamp(6px, 1.5vw, 10px) clamp(8px, 2vw, 12px);
            text-align: center;
            color: #fff;
            font-size: clamp(11px, 2vw, 14px);
        }
        .graph-stats th { background: #444; font-weight: bold; color: #fff; }
        .graph-stats td:first-child { text-align: left; font-weight: bold; color: #fff; }
        .graph-stats .jung-next { color: #81c784; }
        .graph-stats .kkuk-next { color: #e57373; }
        .graph-stats .jung-kkuk { color: #ffb74d; }
        .graph-stats .kkuk-jung { color: #64b5f6; }
        .graph-stats .stat-rate.high { color: #81c784; font-weight: 600; }
        .graph-stats .stat-rate.mid { color: #ffb74d; }
        .graph-stats .stat-rate.low { color: #e57373; font-weight: 500; }
        .graph-stats-note { margin-top: 6px; font-size: 0.85em; color: #aaa; text-align: center; line-height: 1.5; }
        /* 성공/실패 결과: 예측 박스와 완전 분리(아웃) */
        .prediction-result-section {
            width: 100%;
            margin-top: 8px;
            margin-bottom: 6px;
        }
        .prediction-result-bar-wrap {
            width: 100%;
            min-height: 0;
        }
        .prediction-result-bar-wrap .pick-result-bar {
            max-width: none;
            width: 100%;
            box-sizing: border-box;
        }
        .prediction-table-row {
            display: flex;
            align-items: stretch;
            gap: 8px;
            margin-top: 8px;
            flex-wrap: wrap;
        }
        .prediction-table-row #prediction-box {
            flex: 1 1 55%;
            min-width: 0;
        }
        @media (max-width: 768px) {
            .prediction-table-row { flex-direction: column; align-items: stretch; gap: 8px; }
            .prediction-table-row #prediction-pick-container { order: 1; width: 100%; max-width: 100%; flex: 1 1 auto; display: flex; justify-content: center; box-sizing: border-box; }
            .prediction-table-row #prediction-box { order: 2; width: 100%; max-width: 100%; flex: 1 1 auto; box-sizing: border-box; }
            .prediction-table-row #prob-bucket-collapse { order: 3; width: 100%; }
        }
        @media (max-width: 480px) {
            .cards-container { gap: 2px; padding: 8px 0; }
            .card-wrapper { flex: 0 0 calc((100% - 28px) / 15); }
            .card-wrapper .card { max-width: none !important; min-height: 20px !important; }
            .card { max-width: none; min-height: 20px; border-width: 1px; border-radius: 4px; }
            .card-suit-icon { font-size: 7px; }
            .card-value { font-size: 8px; }
            .card-category, .color-match { font-size: 6px; padding: 2px 3px; }
        }
        #prediction-pick-container {
            flex: 1 1 45%;
            min-width: 200px;
            max-width: 100%;
            padding: clamp(8px, 1.5vw, 14px);
            background: rgba(255,255,255,0.04);
            border: 1px solid #444;
            border-radius: 10px;
            display: flex;
            align-items: center;
            justify-content: center;
            box-sizing: border-box;
        }
        #graph-stats {
            flex: 1 1 260px;
            min-width: 200px;
            overflow-x: auto;
        }
        .prob-bucket-collapse {
            margin-top: 8px;
            border: 1px solid #444;
            border-radius: 8px;
            background: rgba(255,255,255,0.03);
            overflow: hidden;
        }
        .prob-bucket-collapse-header {
            padding: 10px 14px;
            font-size: 1em;
            color: #aaa;
            cursor: pointer;
            user-select: none;
        }
        .prob-bucket-collapse-header:hover { background: rgba(255,255,255,0.06); color: #fff; }
        .prob-bucket-collapse.collapsed .prob-bucket-collapse-header::before { content: '▶ '; }
        .prob-bucket-collapse:not(.collapsed) .prob-bucket-collapse-header::before { content: '▼ '; }
        .prob-bucket-collapse-body {
            display: none;
            padding: 14px 18px;
            border-top: 1px solid #333;
        }
        .prob-bucket-collapse:not(.collapsed) .prob-bucket-collapse-body { display: block; }
        /* 모양 판별 등 통합 탭 (가로 탭, 클릭 시 해당 패널만 표시) */
        .analysis-tabs-wrap { margin-top: 12px; border: 1px solid #444; border-radius: 8px; background: rgba(255,255,255,0.03); overflow: hidden; }
        .analysis-tabs { display: flex; flex-wrap: wrap; gap: 0; border-bottom: 1px solid #444; background: #252525; position: relative; }
        .analysis-tab { padding: 10px 14px; cursor: pointer; font-size: 0.95em; color: #aaa; user-select: none; white-space: nowrap; }
        .analysis-tab:hover { background: rgba(255,255,255,0.06); color: #fff; }
        .analysis-tab.active { background: #444; color: #fff; font-weight: 600; }
        .analysis-tabs-collapse-btn { position: absolute; right: 0; top: 0; padding: 10px 14px; cursor: pointer; font-size: 0.95em; color: #aaa; user-select: none; background: #252525; border-left: 1px solid #444; }
        .analysis-tabs-collapse-btn:hover { background: rgba(255,255,255,0.06); color: #fff; }
        .analysis-tabs-wrap.collapsed .analysis-panel { display: none !important; }
        .analysis-panel { display: none; padding: 14px 18px; border-top: none; }
        .analysis-panel.active { display: block; }
        .analysis-panel .prob-bucket-collapse-body { display: block !important; }
        .formula-explanation { font-size: clamp(13px, 1.8vw, 15px); color: #ccc; line-height: 1.55; max-width: 720px; margin: 0 auto; }
        .formula-explanation .formula-intro { margin-bottom: 12px; color: #ddd; }
        .formula-explanation .formula-steps { margin: 0 0 12px 0; padding-left: 1.4em; }
        .formula-explanation .formula-steps li { margin-bottom: 10px; }
        .formula-explanation .formula-steps strong { color: #81c784; }
        .formula-explanation .formula-steps em { color: #ffb74d; font-style: normal; }
        .formula-explanation .formula-note { margin-top: 14px; padding-top: 10px; border-top: 1px solid #444; font-size: 0.92em; color: #999; }
        #prob-bucket-collapse-body .prob-bucket-table {
            border-collapse: collapse;
            margin: 0 auto;
            font-size: clamp(14px, 2.2vw, 18px);
            color: #fff;
            min-width: 280px;
        }
        #prob-bucket-collapse-body .prob-bucket-table th,
        #prob-bucket-collapse-body .prob-bucket-table td {
            border: 1px solid #555;
            padding: 10px 14px;
            text-align: center;
        }
        #prob-bucket-collapse-body .prob-bucket-table th { background: #444; color: #aaa; font-weight: 600; }
        #prob-bucket-collapse-body .prob-bucket-table td:first-child { text-align: left; font-weight: bold; }
        #prob-bucket-collapse-body .prob-bucket-table .stat-rate.high { color: #81c784; font-weight: 600; }
        #prob-bucket-collapse-body .prob-bucket-table .stat-rate.mid { color: #ffb74d; }
        #prob-bucket-collapse-body .prob-bucket-table .stat-rate.low { color: #e57373; }
        /* 좌우대칭 / 줄 유사도 표: 보기 좋게 */
        #symmetry-line-collapse-body .symmetry-line-table {
            border-collapse: collapse;
            width: 100%;
            max-width: 480px;
            margin: 0 auto;
            font-size: clamp(13px, 1.9vw, 15px);
            color: #e0e0e0;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 2px 8px rgba(0,0,0,0.3);
        }
        #symmetry-line-collapse-body .symmetry-line-table thead {
            background: linear-gradient(180deg, #3a3a3a 0%, #2d2d2d 100%);
            color: #fff;
        }
        #symmetry-line-collapse-body .symmetry-line-table th {
            padding: 12px 14px;
            font-weight: 600;
            text-align: left;
            border-bottom: 2px solid #555;
        }
        #symmetry-line-collapse-body .symmetry-line-table th:nth-child(1) { width: 38%; }
        #symmetry-line-collapse-body .symmetry-line-table th:nth-child(2) { width: 18%; text-align: center; }
        #symmetry-line-collapse-body .symmetry-line-table th:nth-child(3) { width: 44%; }
        #symmetry-line-collapse-body .symmetry-line-table tbody tr {
            background: #2a2a2a;
            border-bottom: 1px solid #3a3a3a;
        }
        #symmetry-line-collapse-body .symmetry-line-table tbody tr:nth-child(even) {
            background: #252525;
        }
        #symmetry-line-collapse-body .symmetry-line-table tbody tr:hover {
            background: #333;
        }
        #symmetry-line-collapse-body .symmetry-line-table td {
            padding: 10px 14px;
            border-bottom: 1px solid #333;
        }
        #symmetry-line-collapse-body .symmetry-line-table td:nth-child(1) { font-weight: 500; color: #ccc; }
        #symmetry-line-collapse-body .symmetry-line-table td:nth-child(2) { text-align: center; font-weight: 600; color: #81c784; }
        #symmetry-line-collapse-body .symmetry-line-table td:nth-child(3) { font-size: 0.92em; color: #999; }
        #symmetry-line-collapse-body .symmetry-line-table tbody tr:last-child td { border-bottom: none; }
        #pong-chunk-collapse-body .symmetry-line-table { border-collapse: collapse; width: 100%; max-width: 420px; }
        #pong-chunk-collapse-body .symmetry-line-table th, #pong-chunk-collapse-body .symmetry-line-table td { padding: 8px 12px; border: 1px solid #444; text-align: left; background: #2a2a2a; }
        #pong-chunk-collapse-body .symmetry-line-table td:nth-child(2) { font-weight: 600; color: #81c784; }
        /* 예측픽이 해당 확률 구간에 있을 때 아웃라인 깜빡임 (강승부 구간 강조) */
        .prediction-pick.pick-in-bucket .prediction-card {
            animation: bucketOutlineBlink 1.4s ease-in-out infinite;
        }
        @keyframes bucketOutlineBlink {
            0%, 100% { box-shadow: 0 0 0 2px rgba(129, 199, 132, 0.5), 0 4px 16px rgba(0,0,0,0.5); outline: 2px solid rgba(129, 199, 132, 0.8); outline-offset: 2px; }
            50% { box-shadow: 0 0 0 6px rgba(129, 199, 132, 0.9), 0 4px 20px rgba(129, 199, 132, 0.3); outline: 3px solid #81c784; outline-offset: 3px; }
        }
        .prediction-pick.pick-in-bucket .prediction-card.card-red { animation-name: bucketOutlineBlinkRed; }
        @keyframes bucketOutlineBlinkRed {
            0%, 100% { box-shadow: 0 0 0 2px rgba(198, 40, 40, 0.6), 0 4px 16px rgba(198,40,40,0.5); outline: 2px solid rgba(229, 115, 115, 0.9); outline-offset: 2px; }
            50% { box-shadow: 0 0 0 6px rgba(229, 115, 115, 0.95), 0 4px 20px rgba(198, 40, 40, 0.4); outline: 3px solid #e57373; outline-offset: 3px; }
        }
        .prediction-pick.pick-in-bucket .prediction-card.card-black { animation-name: bucketOutlineBlinkBlack; }
        @keyframes bucketOutlineBlinkBlack {
            0%, 100% { box-shadow: 0 0 0 2px rgba(144, 164, 174, 0.6), 0 4px 16px rgba(0,0,0,0.5); outline: 2px solid rgba(144, 164, 174, 0.8); outline-offset: 2px; }
            50% { box-shadow: 0 0 0 6px rgba(144, 164, 174, 0.9), 0 4px 20px rgba(66, 66, 66, 0.5); outline: 3px solid #90a4ae; outline-offset: 3px; }
        }
        .prediction-pick {
            position: relative;
            display: flex;
            flex-direction: column;
            align-items: center;
            text-align: center;
            width: 100%;
        }
        /* 예측 박스 밖 별도 가로 박스 (몇 회차 성공/실패, 정·꺽 / 빨강·검정) */
        .pick-result-bar {
            padding: 8px 14px;
            border-radius: 6px;
            font-size: clamp(0.85em, 1.9vw, 1em);
            font-weight: 600;
            text-align: center;
            box-sizing: border-box;
        }
        .pick-result-bar.result-win {
            background: rgba(76, 175, 80, 0.25);
            border: 1px solid rgba(76, 175, 80, 0.6);
            color: #a5d6a7;
        }
        .pick-result-bar.result-lose {
            background: rgba(198, 40, 40, 0.2);
            border: 1px solid rgba(239, 83, 80, 0.5);
            color: #ef9a9a;
        }
        .prediction-pick-title {
            font-size: clamp(0.8em, 1.8vw, 0.9em);
            font-weight: bold;
            color: #81c784;
            margin-bottom: clamp(2px, 0.6vw, 5px);
        }
        .prediction-pick-title.prediction-pick-title-betting {
            color: #ffeb3b;
            animation: prediction-blink 1s ease-in-out infinite;
        }
        @keyframes prediction-blink { 50% { opacity: 0.7; } }
        .prediction-pick .pred-round {
            margin-top: 2px;
            font-size: 0.9em;
            font-weight: bold;
            color: #81c784;
        }
        .prediction-card {
            width: clamp(52px, 18vw, 110px);
            height: clamp(52px, 18vw, 110px);
            background: #1a1a1a;
            border: clamp(2px, 0.4vw, 4px) solid #424242;
            border-radius: clamp(10px, 2vw, 14px);
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 4px 16px rgba(0,0,0,0.5);
        }
        .prediction-card.card-red {
            background: #c62828;
            border-color: #e57373;
            box-shadow: 0 4px 16px rgba(198,40,40,0.5);
        }
        .prediction-card.card-black {
            background: #1a1a1a;
            border-color: #424242;
        }
        .prediction-card .pred-value-big {
            font-size: clamp(1.4em, 4.5vw, 2.6em);
            font-weight: 900;
            color: #fff;
            text-shadow: 0 0 10px rgba(255,255,255,0.4);
        }
        .prediction-card.card-red .pred-value-big { color: #fff; text-shadow: 0 0 12px rgba(255,255,255,0.5); }
        .prediction-card.card-black .pred-value-big { color: #e0e0e0; }
        .prediction-prob-under {
            margin-top: 4px;
            font-size: clamp(0.8em, 1.8vw, 0.9em);
            color: #81c784;
            font-weight: bold;
        }
        .prediction-warning-u35 {
            margin-top: 4px;
            padding: 3px 6px;
            font-size: 0.75em;
            font-weight: bold;
            color: #e65100;
            background: rgba(230, 81, 0, 0.15);
            border-radius: 4px;
            border: 1px solid rgba(230, 81, 0, 0.4);
        }
        .prediction-stats-row {
            width: 100%;
            margin-top: 10px;
            padding: 10px 12px;
            border-radius: 8px;
            background: rgba(0,0,0,0.25);
            font-size: clamp(0.9em, 2vw, 1em);
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            justify-content: center;
            gap: 10px 14px;
        }
        .prediction-stats-row .stat-total { color: #b0bec5; }
        .prediction-stats-row .stat-total .num { color: #fff; font-weight: bold; }
        .prediction-stats-row .stat-win { color: #81c784; font-weight: bold; }
        .prediction-stats-row .stat-win .num { color: #a5d6a7; }
        .prediction-stats-row .stat-lose { color: #e57373; font-weight: bold; }
        .prediction-stats-row .stat-lose .num { color: #ef9a9a; }
        .prediction-stats-row .stat-joker { color: #64b5f6; }
        .prediction-stats-row .stat-joker .num { color: #90caf9; }
        .prediction-stats-row .stat-rate { font-weight: 900; }
        .prediction-stats-row .stat-rate.high { color: #81c784; }
        .prediction-stats-row .stat-rate.low { color: #e57373; }
        .prediction-stats-row .stat-rate.mid { color: #ffb74d; }
        .blended-win-rate-wrap {
            margin-bottom: 10px; padding: 10px 12px; background: #2a2a2a; border-radius: 8px; border: 1px solid #444;
            text-align: center;
        }
        .prediction-stats-blended-label { font-size: clamp(0.8em, 2vw, 0.9em); color: #b0bec5; margin-bottom: 4px; }
        .prediction-stats-blended-value { font-size: clamp(1.4em, 4vw, 1.8em); font-weight: 900; color: #fff; }
        .blended-win-rate-low .prediction-stats-blended-value { color: #e57373; }
        @keyframes blended-blink {
            0%, 100% { opacity: 1; background: rgba(229,115,115,0.15); }
            50% { opacity: 0.85; background: rgba(229,115,115,0.35); }
        }
        .blended-win-rate-low { animation: blended-blink 1.2s ease-in-out infinite; }
        .prediction-streak-line { margin-top: 8px; font-size: clamp(0.9em, 2vw, 1em); color: #bbb; text-align: center; }
        .prediction-streak-line .streak-win { color: #ffeb3b; font-weight: bold; }
        .prediction-streak-line .streak-lose { color: #c62828; font-weight: bold; }
        .prediction-streak-line .streak-joker { color: #64b5f6; }
        .main-streak-table { width: 100%; margin-top: 8px; border-collapse: collapse; font-size: clamp(0.65em, 1.5vw, 0.75em); }
        .main-streak-table th, .main-streak-table td { padding: 3px 5px; border: 1px solid #444; text-align: center; background: #2a2a2a; }
        .main-streak-table th { color: #81c784; background: #333; white-space: nowrap; }
        .main-streak-table th:first-child, .main-streak-table td:first-child { background: #333; white-space: nowrap; min-width: 3em; }
        .main-streak-table td.pick-red { background: #b71c1c; color: #fff; }
        .main-streak-table td.pick-black { background: #111; color: #fff; }
        .main-streak-table td.streak-win { color: #ffeb3b; font-weight: 600; }
        .main-streak-table td.streak-lose { color: #c62828; font-weight: 500; }
        .main-streak-table td.streak-joker { color: #64b5f6; }
        .main-streak-table-wrap { overflow-x: auto; max-width: 100%; }
        .prob-bucket-table .stat-rate.high { color: #81c784; font-weight: 600; }
        .prob-bucket-table .stat-rate.mid { color: #ffb74d; }
        .prob-bucket-table .stat-rate.low { color: #e57373; }
        .prediction-notice {
            margin-top: 10px;
            padding: 10px 12px;
            background: rgba(255,193,7,0.12);
            border: 1px solid rgba(255,193,7,0.35);
            border-radius: 8px;
            color: #ffc107;
            font-size: clamp(0.85em, 2vw, 0.95em);
            text-align: center;
            line-height: 1.4;
        }
        .prediction-notice.danger { background: rgba(229,115,115,0.12); border-color: rgba(229,115,115,0.35); color: #e57373; }
        .prediction-box {
            margin-top: 0;
            padding: clamp(8px, 1.5vw, 12px);
            background: #333;
            border-radius: 8px;
            color: #fff;
            font-size: clamp(13px, 2vw, 15px);
            text-align: center;
        }
        .prediction-box .pred-round { font-weight: bold; color: #81c784; }
        .prediction-box .pred-value { font-weight: bold; font-size: 1.1em; }
        .prediction-box .pred-color { font-weight: bold; }
        .prediction-box .pred-color.red { color: #e57373; }
        .prediction-box .pred-color.black { color: #90a4ae; }
        .prediction-box .pred-prob { font-size: 0.95em; color: #aaa; }
        .prediction-box .streak-line { margin-top: 6px; font-size: 0.9em; color: #bbb; }
        .prediction-box .streak-now { font-weight: bold; }
        .prediction-box .flow-type { margin-top: 4px; font-size: 0.85em; color: #aaa; }
        .prediction-box .flow-type .pong { color: #64b5f6; }
        .prediction-box .flow-type .line { color: #ffb74d; }
        .prediction-box .flow-advice { margin-top: 6px; font-size: 0.85em; padding: 4px 6px; border-radius: 4px; background: rgba(255,193,7,0.15); color: #ffc107; border: 1px solid rgba(255,193,7,0.4); }
        .prediction-box .hit-rate { margin-top: 6px; font-size: 0.9em; color: #aaa; }
        .bet-calc {
            margin-top: 8px;
            padding: 10px 12px;
            background: #2a2a2a;
            border-radius: 8px;
            color: #fff;
            font-size: clamp(12px, 2vw, 14px);
        }
        .bet-calc h4 { margin: 0 0 6px 0; font-size: 0.95em; color: #81c784; }
        .bet-calc .bet-inputs { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: center; margin-bottom: 6px; }
        .bet-calc .bet-inputs label { display: flex; align-items: center; gap: 6px; }
        .bet-calc .bet-inputs input { width: 90px; padding: 4px 8px; border-radius: 4px; border: 1px solid #555; background: #1a1a1a; color: #fff; }
        .bet-calc .bet-result { margin-top: 10px; padding-top: 10px; border-top: 1px solid #444; color: #bbb; font-size: 0.9em; }
        .bet-calc .bet-result .profit { font-weight: bold; }
        .bet-calc .bet-result .profit.plus { color: #81c784; }
        .bet-calc .bet-result .profit.minus { color: #e57373; }
        .bet-calc .bet-result .bust { color: #e57373; font-weight: bold; }
        .bet-calc .bet-row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; margin-top: 6px; font-size: 0.8em; color: #bbb; }
        .bet-calc .bet-status { flex: 1; min-width: 180px; }
        .bet-calc .bet-buttons { display: flex; gap: 4px; }
        .bet-calc .bet-buttons button { padding: 3px 8px; border-radius: 4px; border: 1px solid #555; background: #1a1a1a; color: #fff; cursor: pointer; font-size: 0.75em; }
        .bet-calc .bet-buttons button:hover { background: #333; }
        .bet-calc .bet-buttons button.run { background: #2e7d32; border-color: #4caf50; }
        .bet-calc .bet-buttons button.stop { background: #c62828; border-color: #e57373; }
        .bet-calc .bet-buttons button.reset { background: #455a64; border-color: #78909c; }
        .bet-calc .bet-result { margin-top: 8px; padding-top: 8px; }
        .calc-dropdowns { margin-top: 8px; display: flex; flex-direction: column; gap: 6px; }
        .calc-dropdown { width: 100%; border: 1px solid #444; border-radius: 8px; overflow: hidden; }
        .calc-dropdown-header { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; padding: 8px 10px; background: #333; cursor: pointer; }
        .calc-dropdown-header .calc-title { font-weight: bold; color: #81c784; flex-shrink: 0; }
        .calc-dropdown-header .calc-summary { flex: 0 0 auto; font-size: 0.85em; color: #bbb; margin-left: auto; width: max-content; max-width: 100%; }
        .calc-summary-grid { display: grid; grid-template-columns: min-content min-content; gap: 2px 6px; align-items: baseline; width: max-content; }
        .calc-summary-grid .label { color: #888; font-size: 0.9em; white-space: nowrap; width: fit-content; }
        .calc-summary-grid .value { color: #ddd; font-weight: 500; text-align: right; min-width: 0; white-space: nowrap; }
        .calc-summary-grid .value.profit-plus { color: #81c784; }
        .calc-summary-grid .value.profit-minus { color: #e57373; }
        .calc-summary-grid .calc-timer-note { margin-bottom: 2px; }
        .calc-dropdown-header .calc-status { font-size: 0.8em; margin-left: 6px; }
        .calc-dropdown-header .calc-status.running { color: #4caf50; }
        .calc-dropdown-header .calc-status.running::before { content: ''; display: inline-block; width: 6px; height: 6px; background: #4caf50; border-radius: 50%; margin-right: 4px; vertical-align: middle; animation: blink 1s ease-in-out infinite; }
        @keyframes blink { 50% { opacity: 0.6; } }
        .calc-dropdown-header .calc-status.stopped { color: #e57373; }
        .calc-dropdown-header .calc-status.timer-done { color: #64b5f6; font-weight: bold; }
        .calc-dropdown-header .calc-status.idle { color: #888; }
        .calc-cards-wrap { display: inline-flex; align-items: center; gap: 10px; margin-left: 8px; vertical-align: middle; }
        .calc-card-item { display: inline-flex; align-items: center; gap: 4px; font-size: 0.8em; color: #888; }
        .calc-card-label { white-space: nowrap; }
        .calc-card-box { display: inline-flex; flex-direction: column; align-items: center; gap: 2px; }
        .calc-round-line { font-size: 0.95em; font-weight: 600; color: #ddd; min-height: 1.3em; line-height: 1.3; }
        .calc-round-line .calc-icon { font-size: 1.5em; display: inline-block; vertical-align: middle; line-height: 1; }
        .calc-round-line .calc-icon-star { color: #ffeb3b; }
        .calc-round-line .calc-icon-triangle { color: #f44336; }
        .calc-round-line .calc-icon-circle { color: #2196f3; }
        .calc-round-badge { display: inline-block; padding: 2px 8px; border-radius: 6px; font-weight: 600; white-space: nowrap; }
        .calc-round-badge.calc-round-star { background: rgba(255, 235, 59, 0.25); color: #ffeb3b; border: 1px solid rgba(255, 235, 59, 0.5); }
        .calc-round-badge.calc-round-triangle { background: rgba(244, 67, 54, 0.2); color: #ff8a80; border: 1px solid rgba(244, 67, 54, 0.45); }
        .calc-round-badge.calc-round-circle { background: rgba(33, 150, 243, 0.2); color: #82b1ff; border: 1px solid rgba(33, 150, 243, 0.45); }
        .calc-current-card { display: inline-block; text-align: center; vertical-align: middle; border: 1px solid #555; box-sizing: border-box; color: #fff; }
        .calc-current-card.calc-card-betting { width: 44px; height: 28px; line-height: 28px; font-size: 1em; font-weight: bold; }
        .calc-current-card.calc-card-prediction { width: 36px; height: 22px; line-height: 22px; font-size: 0.85em; }
        .calc-current-card.card-jung { background: #b71c1c; }
        .calc-current-card.card-kkuk { background: #111; }
        .calc-current-card.card-hold { background: #555; color: #ccc; }
        .calc-dropdown-header .calc-toggle { font-size: 0.8em; color: #888; }
        .calc-dropdown.collapsed .calc-dropdown-body { display: none !important; }
        .calc-dropdown:not(.collapsed) .calc-dropdown-header .calc-toggle { transform: rotate(180deg); }
        .calc-dropdown-body { padding: 8px 12px; background: #2a2a2a; display: flex; flex-direction: row; flex-wrap: wrap; gap: 12px; align-items: flex-start; min-width: 0; }
        .calc-body-row { display: flex; flex-direction: row; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 0; flex: 1 1 200px; min-width: 0; max-width: 100%; }
        .calc-inputs { display: flex; flex-direction: row; flex-wrap: wrap; gap: 6px 12px; align-items: center; min-width: 0; }
        .calc-inputs label { display: flex; align-items: center; gap: 4px; font-size: 0.9em; flex-shrink: 0; }
        .calc-inputs input[type="number"] { width: 80px; min-width: 0; padding: 4px 6px; border-radius: 4px; border: 1px solid #555; background: #1a1a1a; color: #fff; }
        .calc-inputs select { padding: 4px 6px; border-radius: 4px; border: 1px solid #555; background: #1a1a1a; color: #fff; font-size: 0.9em; }
        .calc-settings-table { width: 100%; max-width: 560px; border: none; border-collapse: collapse; font-size: 0.9em; }
        .calc-settings-table td { padding: 6px 10px 6px 0; vertical-align: middle; border: none; }
        .calc-settings-table tr td:first-child { white-space: nowrap; color: #aaa; width: 1%; min-width: 72px; }
        .calc-settings-table tr td:last-child { line-height: 1.5; }
        .calc-settings-table label { display: inline-flex; align-items: center; gap: 4px; margin-right: 12px; margin-bottom: 2px; }
        .calc-settings-table input[type="number"] { width: 72px; padding: 4px 6px; border-radius: 4px; border: 1px solid #555; background: #1a1a1a; color: #fff; }
        .calc-settings-table input.calc-threshold-input { width: 4em; min-width: 4em; box-sizing: content-box; }
        .calc-settings-table input[type="checkbox"] { margin: 0; }
        .calc-settings-table select { padding: 4px 6px; border-radius: 4px; border: 1px solid #555; background: #1a1a1a; color: #fff; }
        .calc-target-hint { margin-left: 4px; }
        .calc-bet-copy-line { font-size: 0.95em; color: #bbb; }
        .calc-bet-copy-amount { cursor: pointer; padding: 2px 6px; border-radius: 4px; background: #37474f; color: #81c784; font-weight: 600; margin-left: 4px; }
        .calc-bet-copy-amount:hover { background: #455a64; color: #a5d6a7; }
        .calc-bet-copy-amount:active { background: #546e7a; }
        .calc-bet-copy-hint { font-size: 0.85em; color: #78909c; margin-left: 4px; }
        .calc-options-wrap { margin-top: 6px; border: 1px solid #444; border-radius: 6px; overflow: hidden; }
        .calc-options-toggle { display: flex; align-items: center; gap: 6px; padding: 6px 10px; background: #333; cursor: pointer; font-size: 0.9em; color: #aaa; }
        .calc-options-toggle:hover { background: #3a3a3a; color: #ccc; }
        .calc-options-toggle .calc-options-icon { font-size: 0.75em; transition: transform 0.2s; }
        .calc-options-wrap.collapsed .calc-options-toggle .calc-options-icon { transform: rotate(-90deg); }
        .calc-options-body { padding: 8px 10px; background: #252525; }
        .calc-options-wrap.collapsed .calc-options-body { display: none !important; }
        @media (max-width: 520px) {
            .calc-dropdown-body { flex-direction: column; }
            .calc-body-row { flex: 1 1 auto; max-width: none; }
            .calc-detail { flex: 1 1 auto; min-width: 0; width: 100%; }
        }
        .calc-reverse { margin-left: 4px; }
        .calc-buttons { display: flex; flex-wrap: wrap; gap: 4px; }
        .calc-buttons button { padding: 4px 10px; border-radius: 4px; border: 1px solid #555; background: #1a1a1a; color: #fff; cursor: pointer; font-size: 0.85em; }
        .calc-buttons button.calc-run { background: #2e7d32; border-color: #4caf50; }
        .calc-buttons button.calc-stop { background: #c62828; border-color: #e57373; }
        .calc-buttons button.calc-reset { background: #455a64; }
        .calc-buttons button.calc-save { background: #1565c0; border-color: #1976d2; }
        .calc-detail { font-size: 0.85em; color: #bbb; flex: 1 1 280px; min-width: 0; }
        /* 계산기 내 미니 그래프: 최근 25열, 작은 블록, 접기 가능 */
        .calc-mini-graph-collapse { margin-bottom: 6px; border: 1px solid #444; border-radius: 4px; overflow: hidden; background: rgba(255,255,255,0.02); }
        .calc-mini-graph-header { padding: 4px 8px; font-size: 0.8em; color: #888; cursor: pointer; user-select: none; }
        .calc-mini-graph-header:hover { background: rgba(255,255,255,0.06); color: #aaa; }
        .calc-mini-graph-collapse.collapsed .calc-mini-graph-header::before { content: '▶ '; }
        .calc-mini-graph-collapse:not(.collapsed) .calc-mini-graph-header::before { content: '▼ '; }
        .calc-mini-graph-collapse .calc-mini-graph-body { display: block; padding: 4px 8px 6px; border-top: 1px solid #333; }
        .calc-mini-graph-collapse.collapsed .calc-mini-graph-body { display: none; }
        .calc-mini-graph-wrap {
            display: flex; flex-direction: row; justify-content: flex-start; align-items: flex-end;
            gap: 2px; flex-wrap: nowrap; overflow-x: auto; overflow-y: auto;
            max-height: 80px; padding: 0; -webkit-overflow-scrolling: touch;
        }
        .calc-mini-graph-wrap .calc-mini-col {
            display: flex; flex-direction: column; gap: 1px; align-items: center; flex-shrink: 0;
        }
        .calc-mini-graph-wrap .calc-mini-col-num { font-size: 9px; font-weight: 600; color: #aaa; margin-top: 2px; }
        .calc-mini-graph-wrap .calc-mini-block {
            font-size: 8px; font-weight: bold; padding: 1px 3px; min-width: 12px; min-height: 10px;
            box-sizing: border-box; border-radius: 2px; color: #fff; line-height: 1;
        }
        .calc-mini-graph-wrap .calc-mini-block.jung { background: #4caf50; }
        .calc-mini-graph-wrap .calc-mini-block.kkuk { background: #f44336; }
        .calc-round-table-wrap { margin-bottom: 6px; overflow-x: auto; max-height: 32em; overflow-y: auto; }
        .calc-round-table { width: 100%; border-collapse: collapse; font-size: 0.8em; }
        .calc-round-table th, .calc-round-table td { padding: 4px 6px; border: 1px solid #444; text-align: center; }
        .calc-round-table th { background: #333; color: #81c784; }
        .calc-round-table td.pick-jung, .calc-round-table td.pick-red { background: #b71c1c; color: #fff; }
        .calc-round-table td.pick-kkuk, .calc-round-table td.pick-black { background: #111; color: #fff; }
        .calc-round-table td.pick-hold { background: #555; color: #aaa; }
        .calc-round-table .win { color: #ffeb3b; font-weight: 600; }
        .calc-round-table .lose { color: #c62828; font-weight: 500; }
        .calc-round-table .joker { color: #64b5f6; }
        .calc-round-table .skip { color: #666; }
        .calc-round-table .calc-td-bet { text-align: right; white-space: nowrap; }
        .calc-round-table .calc-td-profit { text-align: right; white-space: nowrap; }
        .calc-round-table .profit-plus { color: #81c784; font-weight: 600; }
        .calc-round-table .profit-minus { color: #e57373; font-weight: 500; }
        .calc-round-table td.calc-td-round-star { background: rgba(255, 235, 59, 0.12); color: #ffeb3b; font-weight: 600; }
        .calc-round-table td.calc-td-round-triangle { background: rgba(244, 67, 54, 0.12); color: #ff8a80; font-weight: 600; }
        .calc-round-table td.calc-td-round-circle { background: rgba(33, 150, 243, 0.12); color: #82b1ff; font-weight: 600; }
        .calc-round-table td .calc-icon { font-size: 1.1em; vertical-align: middle; margin-left: 2px; }
        .calc-streak { margin-bottom: 4px; word-break: break-all; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; line-height: 1.35; }
        .calc-streak .w { color: #ffeb3b; }
        .calc-streak .l { color: #c62828; }
        .calc-streak .j { color: #64b5f6; }
        .calc-stats { color: #aaa; }
        .bet-calc-tabs { display: flex; gap: 0; margin-top: 8px; border-bottom: 1px solid #444; }
        .bet-calc-tabs .tab { padding: 8px 16px; cursor: pointer; font-size: 0.9em; color: #888; background: #2a2a2a; border: 1px solid #444; border-bottom: none; border-radius: 6px 6px 0 0; margin-bottom: -1px; }
        .bet-calc-tabs .tab.active { color: #81c784; background: #333; }
        .bet-calc-panel { display: none; padding: 0; }
        .bet-calc-panel.active { display: block; }
        .bet-log-panel { display: none; padding: 10px; background: #1a1a1a; border-radius: 0 6px 6px 6px; border: 1px solid #444; border-top: none; }
        .bet-log-panel.active { display: block; }
        .bet-calc-log { font-size: 0.8em; color: #aaa; max-height: 320px; overflow-y: auto; }
        .bet-calc-log .log-entry { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; padding: 4px 0; border-bottom: 1px solid #333; }
        .bet-calc-log .log-entry .log-text { flex: 1; min-width: 0; word-break: break-all; }
        .bet-calc-log .log-entry .log-actions { flex-shrink: 0; display: flex; gap: 4px; }
        .bet-calc-log .log-entry .log-actions button { padding: 2px 8px; font-size: 0.75em; border-radius: 4px; border: 1px solid #555; background: #2a2a2a; color: #bbb; cursor: pointer; }
        .bet-calc-log .log-entry .log-actions button:hover { background: #333; color: #fff; }
        .bet-calc-log .log-detail { margin-top: 6px; padding: 8px; background: #1a1a1a; border-radius: 4px; overflow-x: auto; display: none; }
        .bet-calc-log .log-detail.open { display: block; }
        .bet-calc-log .log-detail table { width: 100%; border-collapse: collapse; font-size: 0.75em; }
        .bet-calc-log .log-detail th, .bet-calc-log .log-detail td { padding: 3px 6px; border: 1px solid #444; text-align: center; }
        .bet-calc-log .log-detail td.win { color: #ffeb3b; }
        .bet-calc-log .log-detail td.lose { color: #c62828; }
        .bet-log-actions { margin-bottom: 8px; }
        .bet-log-actions button { padding: 4px 10px; font-size: 0.8em; border-radius: 4px; border: 1px solid #555; background: #2a2a2a; color: #bbb; cursor: pointer; }
        .pause-guide-desc { font-size: 0.85em; color: #aaa; margin: 0 0 10px; }
        .pause-guide-table-wrap { overflow-x: auto; margin-top: 8px; }
        .pause-guide-table-wrap table { width: 100%; border-collapse: collapse; font-size: 0.9em; }
        .pause-guide-table-wrap th, .pause-guide-table-wrap td { padding: 6px 10px; text-align: left; border: 1px solid #444; }
        .pause-guide-table-wrap th { background: #2a2a2a; color: #ccc; }
        .pause-guide-table-wrap tr.best-row { background: #1b3d1b; color: #81c784; }
        .status {
            text-align: center;
            margin-top: 15px;
            color: #aaa;
            font-size: clamp(0.8em, 2vw, 0.9em);
        }
        .reference-color {
            font-size: clamp(0.7em, 1.5vw, 0.8em);
            color: #aaa;
            margin-left: 10px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header-info">
            <div id="prev-round">이전회차: --</div>
            <div>
                <span id="remaining-time" class="remaining-time">남은 시간: -- 초</span>
                <span id="reference-color" class="reference-color"></span>
            </div>
        </div>
        <div class="cards-container" id="cards"></div>
        <div id="jung-kkuk-graph" class="jung-kkuk-graph"></div>
        <div class="prediction-result-section">
            <div id="prediction-result-bar" class="prediction-result-bar-wrap"></div>
        </div>
        <div class="prediction-table-row">
            <div id="prediction-pick-container"></div>
            <div id="prediction-box" class="prediction-box"></div>
        </div>
        <div class="analysis-tabs-wrap" id="analysis-tabs-wrap">
            <div class="analysis-tabs" role="tablist">
                <span class="analysis-tab active" role="tab" data-panel="pong-chunk" aria-selected="true">모양 판별</span>
                <span class="analysis-tab" role="tab" data-panel="formula">예측 공식</span>
                <span class="analysis-tab" role="tab" data-panel="graph-stats">승률관리</span>
                <span class="analysis-tab" role="tab" data-panel="prob-bucket">확률 구간</span>
                <span class="analysis-tab" role="tab" data-panel="losing-streaks">연패 구간</span>
                <span class="analysis-tab" role="tab" data-panel="win-rate-direction">승률 방향</span>
                <span class="analysis-tab" role="tab" data-panel="symmetry-line">대칭/줄</span>
                <span class="analysis-tabs-collapse-btn" id="analysis-tabs-collapse-btn" title="접기/펼치기">▼</span>
            </div>
            <div id="panel-pong-chunk" class="analysis-panel active">
                <div id="pong-chunk-collapse-body" class="prob-bucket-collapse-body">
                <div id="shape-visual-summary" class="shape-visual-summary" style="display:none;margin-bottom:12px;padding:12px;background:linear-gradient(135deg,#1e2a1e 0%,#1a1a1a 100%);border-radius:8px;border:1px solid #2d4a2d;">
                    <div class="shape-visual-row" style="display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin-bottom:10px;">
                        <div id="shape-phase-badge" class="shape-phase-badge" style="padding:6px 12px;border-radius:6px;font-weight:bold;font-size:0.95em;">—</div>
                        <div id="shape-latest-pick-card" class="shape-latest-pick-card" style="min-width:60px;height:44px;display:flex;align-items:center;justify-content:center;border-radius:6px;font-weight:bold;font-size:1.1em;">—</div>
                        <div id="shape-signature-bars" class="shape-signature-bars" style="display:flex;align-items:flex-end;gap:4px;height:32px;">—</div>
                    </div>
                    <div class="shape-stats-bars" style="display:flex;flex-wrap:wrap;gap:16px;font-size:0.85em;">
                        <div id="shape-stats-bar" style="flex:1;min-width:120px;"><span style="color:#888;">모양→다음</span><div class="shape-bar-track" style="height:6px;background:#333;border-radius:3px;overflow:hidden;margin-top:4px;"><div class="shape-bar-fill" style="height:100%;background:linear-gradient(90deg,#4caf50,#81c784);width:50%;transition:width 0.3s;"></div></div><span class="shape-bar-labels" style="font-size:0.9em;color:#aaa;">정 — / 꺽 —</span></div>
                        <div id="chunk-stats-bar" style="flex:1;min-width:120px;"><span style="color:#888;">덩어리→다음</span><div class="shape-bar-track" style="height:6px;background:#333;border-radius:3px;overflow:hidden;margin-top:4px;"><div class="shape-bar-fill" style="height:100%;background:linear-gradient(90deg,#2196f3,#64b5f6);width:50%;transition:width 0.3s;"></div></div><span class="shape-bar-labels" style="font-size:0.9em;color:#aaa;">정 — / 꺽 —</span></div>
                    </div>
                    <div id="shape-u-warning" class="shape-u-warning" style="display:none;margin-top:8px;padding:6px 10px;background:rgba(255,152,0,0.15);border:1px solid rgba(255,152,0,0.4);border-radius:4px;color:#ffb74d;font-size:0.9em;">⚠ U자 구간 감지 — 유지 가중치 보정·멈춤 권장</div>
                </div>
                <div id="pong-chunk-section" style="margin-top:0;padding:10px;background:#1a1a1a;border-radius:6px;border:1px solid #444;">
                    <p style="font-size:0.9em;color:#aaa;margin:0 0 8px 0;">최근 그래프에서 <strong>줄(유지)</strong>·<strong>퐁당(바뀜)</strong>·<strong>덩어리(블록 반복)</strong>·<strong>U자 구간</strong>을 판별해 가중치에 반영합니다. U자 구간은 연패가 많아 유지 쪽 보정·멈춤 권장. <strong>유사 덩어리</strong>는 현재 덩어리와 높이 프로필이 비슷한 과거 덩어리의 다음 결과를 가중 합산해 반영하며, <strong>가장 최근 다음 픽</strong>은 유사 덩어리·모양 시그니처 중 가장 최근 것의 다음 결과입니다.</p>
                    <div id="pong-chunk-data" style="font-size:0.9em;color:#ccc;"><table class="symmetry-line-table" style="width:100%;max-width:420px;"><tbody id="pong-chunk-tbody"><tr><td colspan="2" style="color:#888;">데이터 로딩 후 표시</td></tr></tbody></table></div>
                </div>
                </div>
            </div>
            <div id="panel-formula" class="analysis-panel">
                <div id="formula-collapse-body" class="prob-bucket-collapse-body">
                <div class="formula-explanation">
                    <p class="formula-intro">위에 표시되는 <strong>정/꺽</strong> 예측은 아래 단계로 계산됩니다. (서버와 동일 공식)</p>
                    <ol class="formula-steps">
                        <li><strong>그래프값</strong> · 최근 결과에서 카드 i번과 (i+15)번 색상이 같으면 <em>정</em>, 다르면 <em>꺽</em>. 이걸 배열로 만듦 (0번이 가장 최신).</li>
                        <li><strong>전이 확률</strong> · 인접한 두 회차 쌍(정→정, 정→꺽, 꺽→정, 꺽→꺽) 비율을 최근 15회·30회·전체로 계산. 직전이 정이면 «정 유지/정→꺽», 꺽이면 «꺽 유지/꺽→정» 확률 사용.</li>
                        <li><strong>퐁당 / 줄</strong> · 최근 15회에서 «바뀜» 비율 = 퐁당%, «유지» 비율 = 줄%. 퐁당%·줄%로 각각 가중치 초기값 설정.</li>
                        <li><strong>흐름 보정</strong> · 15회 vs 30회 유지 확률 차이가 15%p 이상이면 «줄 강함» 또는 «퐁당 강함»으로 판단. 줄 강함이면 줄 가중치 +0.25, 퐁당 강함이면 퐁당 가중치 +0.25.</li>
                        <li><strong>15·20·30열 대칭·줄</strong> · 15열·20열·30열 각각 좌반/우반 대칭도·줄 개수 계산 후 가중 평균(0.2·0.5·0.3) 반영. 새 구간 감지 시 줄 +0.22, 대칭 70% 이상·우측 줄 적으면 줄 +0.28 등 보정.</li>
                        <li><strong>30회 패턴</strong> · «덩어리»(줄이 2개 이상 이어짐) 비율·«띄엄»(줄 1개씩)·«두줄한개» 비율을 지수로 계산. 덩어리/두줄한개는 줄 가중치에, 띄엄은 퐁당 가중치에 반영.</li>
                        <li><strong>가중치 정규화</strong> · 위에서 나온 줄 가중치(lineW)와 퐁당 가중치(pongW)를 더한 뒤 1이 되도록 나눔.</li>
                        <li><strong>V자 패턴 보정</strong> · 그래프가 «긴 줄 → 퐁당 1~2개 → 짧은 줄 → 퐁당 → …» 형태(V자 밸런스)일 때 연패가 많아서, 퐁당(바뀜) 가중치를 올려 이 구간을 넘기기 쉽게 보정함.</li>
                        <li><strong>U자 구간 보정</strong> · «높은 줄 → 낮은 줄(1~2) → 다시 3~5 길이 줄»(U자 모양)일 때 연패가 많음. 감지 시 줄(유지) 가중치 +0.14, 퐁당(반전) -0.07로 유지 쪽 픽 강화·과한 반전 픽 축소. 58% 상한 적용. 계산기에서는 멈춤 권장.</li>
                        <li><strong>연패 길이 보정</strong> · 맨 왼쪽(최신) 열이 꺽(연패)이고 그 연속 길이가 4 이상이면, 퐁당(바뀜) 가중치를 올려 «다음은 정» 쪽으로 픽을 내도록 보정함. (그래프만 봤을 때 연패 구간에서 승을 끌어올리기 위한 보정)</li>
                        <li><strong>퐁당/덩어리/줄 구간 판별</strong> · 세 구간으로 나눔: <em>줄</em>(한쪽으로 길게 이어짐)→유지 가중치 가산, <em>퐁당</em>(2회 이상 바뀜)→바뀜 가중치 가산, <em>덩어리</em>(블록 반복·줄2~4)→줄 가중치 우선. 덩어리 모양 321(줄어듦)이면 바뀜 소폭 가산.</li>
                        <li><strong>유지 vs 바뀜</strong> · «유지 확률 = 전이에서 구한 유지 확률», «바뀜 확률 = 전이에서 구한 바뀜 확률». 각각 lineW, pongW를 곱해 <em>adjSame</em>, <em>adjChange</em> 계산 후 다시 합으로 나누어 0~1로 만듦.</li>
                        <li><strong>최종 픽</strong> · adjSame ≥ adjChange 이면 직전과 <strong>같은 방향</strong>(직전 정→정, 직전 꺽→꺽), 아니면 <strong>반대</strong>(직전 정→꺽, 직전 꺽→정). 15번 카드가 빨강이면 정=빨강/꺽=검정, 검정이면 정=검정/꺽=빨강으로 <em>배팅 색</em> 결정.</li>
                    </ol>
                    <p class="formula-note">※ 15번 카드가 조커면 예측 픽은 보류(배팅 보류). ※ 반픽·승률반픽은 계산기에서만 적용되며, 위 공식은 «정/꺽» 자체의 계산만 설명합니다.</p>
                </div>
                </div>
            </div>
            <div id="panel-graph-stats" class="analysis-panel">
                <div id="graph-stats-collapse-body" class="prob-bucket-collapse-body">
            <div id="graph-stats" class="graph-stats"></div>
            <div id="win-rate-formula-section" class="win-rate-formula-section" style="margin-top:12px;padding:10px;background:#1a1a1a;border-radius:6px;border:1px solid #444;">
                <div class="win-rate-formula-title" style="font-weight:bold;color:#81c784;margin-bottom:8px;">합산승률 공식</div>
                <p style="font-size:0.9em;color:#aaa;margin:0 0 8px 0;">합산승률 = 15회 승률×<span id="win-rate-w15">0.6</span> + 30회 승률×<span id="win-rate-w30">0.25</span> + 100회 승률×<span id="win-rate-w100">0.15</span></p>
                <p style="font-size:0.85em;color:#888;margin:0 0 10px 0;">위험 구간: 합산승률 ≤ <input type="number" id="win-rate-danger-threshold" min="0" max="100" value="46" style="width:4em;min-width:4em;background:#333;color:#fff;border:1px solid #555;padding:2px 4px;"> % 일 때 패 비율 참고</p>
                <div class="win-rate-formula-title" style="font-weight:bold;color:#81c784;margin:12px 0 6px 0;">합산승률 구간별 승/패 (5% 단위)</div>
                <div id="win-rate-buckets-table-wrap" class="graph-stats" style="margin-top:8px;"><table><thead><tr><th>합산승률 구간</th><th>n</th><th>승</th><th>패</th><th>승률%</th></tr></thead><tbody id="win-rate-buckets-tbody"><tr><td colspan="5" style="color:#888;">로딩 중...</td></tr></tbody></table></div>
                <p id="win-rate-recommendation" style="font-size:0.9em;color:#81c784;margin:8px 0 4px 0;font-weight:bold;"></p>
                <p style="font-size:0.8em;color:#888;margin:0 0 0 0;">※ 위 권장값은 표에서 승률 50% 미만인 구간의 상한으로 계산됩니다.</p>
                <div style="margin-top:14px;padding:8px 10px;background:#2d1f1f;border:1px solid #5d4037;border-radius:6px;">
                    <div style="font-weight:bold;color:#ffab91;margin-bottom:4px;">배팅 자제 구간 (2연패 기준)</div>
                    <p id="dont-bet-ranges-msg" style="font-size:0.95em;color:#ffcc80;margin:0 0 4px 0;font-weight:bold;">로딩 중...</p>
                    <p style="font-size:0.75em;color:#888;margin:0;">※ 2연패가 발생한 회차들의 예측확률 범위입니다.</p>
                </div>
            </div>
                </div>
            </div>
            <div id="panel-prob-bucket" class="analysis-panel">
                <div id="prob-bucket-collapse-body" class="prob-bucket-collapse-body"></div>
            </div>
            <div id="panel-losing-streaks" class="analysis-panel">
                <div id="losing-streaks-collapse-body" class="prob-bucket-collapse-body">
                <div id="losing-streaks-section" style="margin-top:8px;padding:10px;background:#1a1a1a;border-radius:6px;border:1px solid #444;">
                    <div style="margin-bottom:12px;padding:8px 10px;background:#2d1f1f;border:1px solid #5d4037;border-radius:6px;">
                        <div style="font-weight:bold;color:#ffab91;margin-bottom:4px;">배팅 자제 구간 (2연패 기준)</div>
                        <p id="losing-streaks-dont-bet-msg" style="font-size:0.95em;color:#ffcc80;margin:0 0 4px 0;font-weight:bold;">로딩 중...</p>
                        <p style="font-size:0.75em;color:#888;margin:0;">※ 2연패가 발생한 회차들의 예측확률 범위. 계산기 등에서 참고용.</p>
                    </div>
                    <div class="win-rate-formula-title" style="font-weight:bold;color:#e57373;margin-bottom:6px;">3연패 이상 구간 분석</div>
                    <p style="font-size:0.85em;color:#aaa;margin:0 0 8px 0;">연패 구간(3패 이상)에 속한 회차들의 예측확률 분포를 봅니다. 어느 확률대에서 연패가 자주 발생했는지 참고하세요.</p>
                    <div style="font-weight:bold;color:#b0bec5;margin:10px 0 6px 0;">예측확률 구간별 연패 발생 (연패 구간 내 회차 수)</div>
                    <div id="losing-streaks-prob-table-wrap" style="margin-top:6px;"><table><thead><tr><th>예측확률 구간</th><th>연패 구간 내 회차 수</th></tr></thead><tbody id="losing-streaks-prob-tbody"><tr><td colspan="2" style="color:#888;">로딩 중...</td></tr></tbody></table></div>
                    <div style="font-weight:bold;color:#b0bec5;margin:12px 0 6px 0;">최근 연패 구간 목록</div>
                    <div id="losing-streaks-list-wrap" style="margin-top:6px;"><table><thead><tr><th>시작 회차</th><th>종료 회차</th><th>연패 수</th><th>평균 예측확률</th></tr></thead><tbody id="losing-streaks-list-tbody"><tr><td colspan="4" style="color:#888;">로딩 중...</td></tr></tbody></table></div>
                </div>
                </div>
            </div>
            <div id="panel-win-rate-direction" class="analysis-panel">
                <div id="win-rate-direction-collapse-body" class="prob-bucket-collapse-body">
                <div id="win-rate-direction-section" style="margin-top:8px;padding:10px;background:#1a1a1a;border-radius:6px;border:1px solid #444;">
                    <p style="font-size:0.9em;color:#aaa;margin:0 0 8px 0;">메인 예측기 밑 <strong>결과 표</strong> 데이터로 롤링 100회 승률을 계산해, <strong>기록 최고점·최저점·중간·방향</strong>을 바로 표시합니다. (결과 100회 이상이면 즉시 표시)</p>
                    <div id="win-rate-direction-data" style="font-size:0.95em;color:#ccc;">
                        <table class="symmetry-line-table" style="width:100%;max-width:480px;">
                            <tbody id="win-rate-direction-tbody">
                                <tr><td colspan="2" style="color:#888;">데이터 로딩 후 표시</td></tr>
                            </tbody>
                        </table>
                        <p style="font-size:0.8em;color:#888;margin-top:8px 0 0 0;">※ 100회 미만이면 기록되지 않습니다. 조커 제외 승/패만으로 승률 계산.</p>
                    </div>
                </div>
                </div>
            </div>
            <div id="panel-symmetry-line" class="analysis-panel">
                <div id="symmetry-line-collapse-body" class="prob-bucket-collapse-body"></div>
            </div>
        </div>
        <div class="bet-calc">
            <h4>가상 배팅 계산기</h4>
            <div class="bet-calc-tabs">
                <span class="tab active" data-tab="calc">계산기</span>
                <span class="tab" data-tab="log">로그</span>
                <span class="tab" data-tab="pause-guide">멈춤 기준 추천</span>
            </div>
            <div id="bet-calc-panel" class="bet-calc-panel active">
                <div class="calc-dropdowns">
                    <div class="calc-dropdown collapsed" data-calc="1">
                        <div class="calc-dropdown-header">
                            <span class="calc-title">계산기 1</span>
                            <span class="calc-status idle" id="calc-1-status">대기중</span>
                            <span class="calc-cards-wrap" id="calc-1-cards-wrap">
                                <span class="calc-card-item"><span class="calc-card-label">배팅중</span><div class="calc-card-box"><div class="calc-round-line" id="calc-1-current-round"></div><span class="calc-current-card calc-card-betting" id="calc-1-current-card"></span></div></span>
                                <span class="calc-card-item"><span class="calc-card-label">예측픽</span><div class="calc-card-box"><div class="calc-round-line" id="calc-1-prediction-round"></div><span class="calc-current-card calc-card-prediction" id="calc-1-prediction-card"></span></div></span>
                            </span>
                            <div class="calc-summary" id="calc-1-summary">보유자산 - | 순익 - | 배팅중 -</div>
                            <span class="calc-toggle">▼</span>
                </div>
                        <div class="calc-dropdown-body" id="calc-1-body">
                            <div class="calc-body-row">
                                <table class="calc-settings-table">
                                    <tr><td>자본/배팅</td><td><label>자본금 <input type="number" id="calc-1-capital" min="0" value="1000000"></label> <label>배팅금액 <input type="number" id="calc-1-base" min="1" value="10000"></label> <label>배당 <input type="number" id="calc-1-odds" min="1" step="0.01" value="1.97"></label></td></tr>
                                </table>
                                <div class="calc-options-wrap collapsed" data-calc="1">
                                    <div class="calc-options-toggle"><span class="calc-options-label">옵션</span><span class="calc-options-icon">▼</span></div>
                                    <div class="calc-options-body">
                                        <table class="calc-settings-table">
                                            <tr><td>픽/승률</td><td><label class="calc-reverse"><input type="checkbox" id="calc-1-reverse"> 반픽</label> <label><input type="checkbox" id="calc-1-win-rate-reverse"> 승률반픽</label> <label title="모양승률(예측기표 모양픽 최근 50회) 이 값 이하일 때 반픽">모양승률≤<input type="number" id="calc-1-win-rate-threshold" min="0" max="100" value="50" class="calc-threshold-input" title="모양승률 이 값 이하일 때 반픽">% 이하일 때 반픽</label></td></tr>
                                            <tr><td>연패반픽</td><td><label><input type="checkbox" id="calc-1-lose-streak-reverse"> 연패≥<input type="number" id="calc-1-lose-streak-reverse-min" min="2" max="15" value="3" class="calc-threshold-input" title="이 값 이상 연패일 때">이상·합산승률≤<input type="number" id="calc-1-lose-streak-reverse-threshold" min="0" max="100" value="48" class="calc-threshold-input" title="이 값 이하일 때 반대픽">%일 때 반대픽</label></td></tr>
                                            <tr><td>승률방향</td><td><label><input type="checkbox" id="calc-1-win-rate-direction-reverse" title="저점→고점 정픽, 고점→저점 반대픽, 정체 시 직전 방향 참조"> 승률방향 반픽 (저점↑정픽·고점↓반대·정체=직전방향)</label> <label><input type="checkbox" id="calc-1-streak-suppress-reverse" title="5연승 또는 5연패일 때 반픽 억제"> 줄 5 이상 반픽 억제</label> <label><input type="checkbox" id="calc-1-lock-direction-on-lose-streak" title="배팅이 연패 중일 때 방향을 바꾸지 않고 진행하던 방향 유지" checked> 연패 중 방향 고정</label></td></tr>
                                            <tr><td>모양</td><td><label><input type="checkbox" id="calc-1-shape-only-latest-next-pick" title="모양 판별의 가장 최근 다음 픽에 뜬 픽에만 배팅. 값이 없으면 배팅 안 함"> 가장 최근 다음 픽에만 배팅 (값 없으면 배팅 안 함)</label></td></tr>
                                            <tr><td>모양판별</td><td><label><input type="checkbox" id="calc-1-shape-prediction" title="덩어리 끝 변형 허용·퐁당 가중치 등 개선된 모양 판별로 픽. 기존 모양옵션과 별도."> 모양판별 픽 사용</label> <label><input type="checkbox" id="calc-1-shape-prediction-reverse"> 모양판별반픽</label> <label title="계산기표 2~11행 모양판별승률 이 값 이하일 때 반픽">모양판별승률≤<input type="number" id="calc-1-shape-prediction-reverse-threshold" min="0" max="100" value="50" class="calc-threshold-input">%일 때 반픽</label> <label title="모양판별 계산식 내 shape/chunk/퐁당/대칭 배율(0~3, 기본 1)">shape×<input type="number" id="calc-1-shape-weight" min="0" max="3" step="0.1" value="1.5" class="calc-threshold-input" style="width:3em"> chunk×<input type="number" id="calc-1-chunk-weight" min="0" max="3" step="0.1" value="1" class="calc-threshold-input" style="width:3em"> 퐁당×<input type="number" id="calc-1-pong-weight" min="0" max="3" step="0.1" value="1.5" class="calc-threshold-input" style="width:3em"> 대칭×<input type="number" id="calc-1-symmetry-weight" min="0" max="3" step="0.1" value="1" class="calc-threshold-input" style="width:3em"></label></td></tr>
                                            <tr><td>모양판별 로그</td><td><div id="calc-1-shape-prediction-log" class="shape-prediction-log" style="font-size:0.8em;color:#888;max-height:80px;overflow-y:auto;white-space:pre-wrap;">—</div></td></tr>
                                            <tr><td>멈춤</td><td><label><input type="checkbox" id="calc-1-pause-low-win-rate"> 계산기 표 15회승률≤<input type="number" id="calc-1-pause-win-rate-threshold" min="0" max="100" value="45" class="calc-threshold-input" title="해당 계산기 표의 15회 승률이 이 값 이하일 때 배팅멈춤. 표 하단 15회승률과 동일 기준">% 이하일 때 배팅멈춤</label></td></tr>
                                            <tr><td>시간</td><td><label>지속 시간(분) <input type="number" id="calc-1-duration" min="0" value="0" placeholder="0=무제한"></label> <label class="calc-duration-check"><input type="checkbox" id="calc-1-duration-check"> 지정 시간만 실행</label></td></tr>
                                            <tr><td>마틴</td><td><label class="calc-martingale"><input type="checkbox" id="calc-1-martingale"> 마틴 적용</label> <label>마틴 방식 <select id="calc-1-martingale-type"><option value="pyo" selected>표마틴</option><option value="pyo_half">표마틴 반</option></select></label></td></tr>
                                            <tr><td>목표</td><td><label><input type="checkbox" id="calc-1-target-enabled"> 목표금액 설정</label> <label>목표 <input type="number" id="calc-1-target-amount" min="0" value="0" placeholder="0=미사용">원</label> <span class="calc-target-hint" id="calc-1-target-hint" style="color:#888;font-size:0.85em"></span></td></tr>
                                            <tr><td>배팅복사</td><td><span id="calc-1-bet-copy-line" class="calc-bet-copy-line">—</span></td></tr>
                                        </table>
                                    </div>
                                </div>
                                <div class="calc-buttons">
                                    <button type="button" class="calc-run" data-calc="1">실행</button>
                                    <button type="button" class="calc-stop" data-calc="1">정지</button>
                                    <button type="button" class="calc-reset" data-calc="1">리셋</button>
                                    <button type="button" class="calc-save" data-calc="1" style="display:none">저장</button>
            </div>
                            </div>
                            <div class="calc-detail" id="calc-1-detail">
                                <div class="calc-mini-graph-collapse" id="calc-1-mini-graph-collapse" data-calc="1">
                                    <div class="calc-mini-graph-header">그래프</div>
                                    <div class="calc-mini-graph-body"><div class="calc-mini-graph-wrap" id="calc-1-mini-graph" title="최근 정/꺽 그래프 (좌=최신)"></div></div>
                                </div>
                                <div class="calc-round-table-wrap" id="calc-1-round-table-wrap"></div>
                                <div class="calc-export-line" style="margin:6px 0;"><button type="button" class="calc-export-csv" data-calc="1">전체 내보내기 (CSV)</button> <span class="calc-export-hint" style="color:#888;font-size:0.85em">표는 최근 200회차까지 표시 (전체 내보내기로 전체 확인)</span></div>
                                <div class="calc-streak" id="calc-1-streak">경기결과 (최근 30회): -</div>
                                <div class="calc-stats" id="calc-1-stats">최대연승: - | 최대연패: - | 모양승률: - | 표승률: - | 15회승률: - | 모양판별승률: -</div>
                            </div>
                        </div>
                    </div>
                    <div class="calc-dropdown collapsed" data-calc="2">
                        <div class="calc-dropdown-header">
                            <span class="calc-title">계산기 2</span>
                            <span class="calc-status idle" id="calc-2-status">대기중</span>
                            <span class="calc-cards-wrap" id="calc-2-cards-wrap">
                                <span class="calc-card-item"><span class="calc-card-label">배팅중</span><div class="calc-card-box"><div class="calc-round-line" id="calc-2-current-round"></div><span class="calc-current-card calc-card-betting" id="calc-2-current-card"></span></div></span>
                                <span class="calc-card-item"><span class="calc-card-label">예측픽</span><div class="calc-card-box"><div class="calc-round-line" id="calc-2-prediction-round"></div><span class="calc-current-card calc-card-prediction" id="calc-2-prediction-card"></span></div></span>
                            </span>
                            <div class="calc-summary" id="calc-2-summary">보유자산 - | 순익 - | 배팅중 -</div>
                            <span class="calc-toggle">▼</span>
                        </div>
                        <div class="calc-dropdown-body" id="calc-2-body">
                            <div class="calc-body-row">
                                <table class="calc-settings-table">
                                    <tr><td>자본/배팅</td><td><label>자본금 <input type="number" id="calc-2-capital" min="0" value="1000000"></label> <label>배팅금액 <input type="number" id="calc-2-base" min="1" value="10000"></label> <label>배당 <input type="number" id="calc-2-odds" min="1" step="0.01" value="1.97"></label></td></tr>
                                </table>
                                <div class="calc-options-wrap collapsed" data-calc="2">
                                    <div class="calc-options-toggle"><span class="calc-options-label">옵션</span><span class="calc-options-icon">▼</span></div>
                                    <div class="calc-options-body">
                                        <table class="calc-settings-table">
                                            <tr><td>픽/승률</td><td><label class="calc-reverse"><input type="checkbox" id="calc-2-reverse"> 반픽</label> <label><input type="checkbox" id="calc-2-win-rate-reverse"> 승률반픽</label> <label title="모양승률(예측기표 모양픽 최근 50회) 이 값 이하일 때 반픽">모양승률≤<input type="number" id="calc-2-win-rate-threshold" min="0" max="100" value="50" class="calc-threshold-input" title="모양승률 이 값 이하일 때 반픽">% 이하일 때 반픽</label></td></tr>
                                            <tr><td>연패반픽</td><td><label><input type="checkbox" id="calc-2-lose-streak-reverse"> 연패≥<input type="number" id="calc-2-lose-streak-reverse-min" min="2" max="15" value="3" class="calc-threshold-input" title="이 값 이상 연패일 때">이상·합산승률≤<input type="number" id="calc-2-lose-streak-reverse-threshold" min="0" max="100" value="48" class="calc-threshold-input" title="이 값 이하일 때 반대픽">%일 때 반대픽</label></td></tr>
                                            <tr><td>승률방향</td><td><label><input type="checkbox" id="calc-2-win-rate-direction-reverse" title="저점→고점 정픽, 고점→저점 반대픽, 정체 시 직전 방향 참조"> 승률방향 반픽 (저점↑정픽·고점↓반대·정체=직전방향)</label> <label><input type="checkbox" id="calc-2-streak-suppress-reverse" title="5연승 또는 5연패일 때 반픽 억제"> 줄 5 이상 반픽 억제</label> <label><input type="checkbox" id="calc-2-lock-direction-on-lose-streak" title="배팅이 연패 중일 때 방향을 바꾸지 않고 진행하던 방향 유지" checked> 연패 중 방향 고정</label></td></tr>
                                            <tr><td>모양</td><td><label><input type="checkbox" id="calc-2-shape-only-latest-next-pick" title="모양 판별의 가장 최근 다음 픽에 뜬 픽에만 배팅. 값이 없으면 배팅 안 함"> 가장 최근 다음 픽에만 배팅 (값 없으면 배팅 안 함)</label></td></tr>
                                            <tr><td>모양판별</td><td><label><input type="checkbox" id="calc-2-shape-prediction" title="덩어리 끝 변형 허용·퐁당 가중치 등 개선된 모양 판별로 픽. 기존 모양옵션과 별도."> 모양판별 픽 사용</label> <label><input type="checkbox" id="calc-2-shape-prediction-reverse"> 모양판별반픽</label> <label title="계산기표 2~11행 모양판별승률 이 값 이하일 때 반픽">모양판별승률≤<input type="number" id="calc-2-shape-prediction-reverse-threshold" min="0" max="100" value="50" class="calc-threshold-input">%일 때 반픽</label> <label title="모양판별 계산식 내 shape/chunk/퐁당/대칭 배율(0~3, 기본 1)">shape×<input type="number" id="calc-2-shape-weight" min="0" max="3" step="0.1" value="1.5" class="calc-threshold-input" style="width:3em"> chunk×<input type="number" id="calc-2-chunk-weight" min="0" max="3" step="0.1" value="1" class="calc-threshold-input" style="width:3em"> 퐁당×<input type="number" id="calc-2-pong-weight" min="0" max="3" step="0.1" value="1.5" class="calc-threshold-input" style="width:3em"> 대칭×<input type="number" id="calc-2-symmetry-weight" min="0" max="3" step="0.1" value="1" class="calc-threshold-input" style="width:3em"></label></td></tr>
                                            <tr><td>모양판별 로그</td><td><div id="calc-2-shape-prediction-log" class="shape-prediction-log" style="font-size:0.8em;color:#888;max-height:80px;overflow-y:auto;white-space:pre-wrap;">—</div></td></tr>
                                            <tr><td>멈춤</td><td><label><input type="checkbox" id="calc-2-pause-low-win-rate"> 계산기 표 15회승률≤<input type="number" id="calc-2-pause-win-rate-threshold" min="0" max="100" value="45" class="calc-threshold-input" title="해당 계산기 표의 15회 승률이 이 값 이하일 때 배팅멈춤. 표 하단 15회승률과 동일 기준">% 이하일 때 배팅멈춤</label></td></tr>
                                            <tr><td>시간</td><td><label>지속 시간(분) <input type="number" id="calc-2-duration" min="0" value="0" placeholder="0=무제한"></label> <label class="calc-duration-check"><input type="checkbox" id="calc-2-duration-check"> 지정 시간만 실행</label></td></tr>
                                            <tr><td>마틴</td><td><label class="calc-martingale"><input type="checkbox" id="calc-2-martingale"> 마틴 적용</label> <label>마틴 방식 <select id="calc-2-martingale-type"><option value="pyo" selected>표마틴</option><option value="pyo_half">표마틴 반</option></select></label></td></tr>
                                            <tr><td>목표</td><td><label><input type="checkbox" id="calc-2-target-enabled"> 목표금액 설정</label> <label>목표 <input type="number" id="calc-2-target-amount" min="0" value="0" placeholder="0=미사용">원</label> <span class="calc-target-hint" id="calc-2-target-hint" style="color:#888;font-size:0.85em"></span></td></tr>
                                            <tr><td>배팅복사</td><td><span id="calc-2-bet-copy-line" class="calc-bet-copy-line">—</span></td></tr>
                                        </table>
                                    </div>
                                </div>
                                <div class="calc-buttons">
                                    <button type="button" class="calc-run" data-calc="2">실행</button>
                                    <button type="button" class="calc-stop" data-calc="2">정지</button>
                                    <button type="button" class="calc-reset" data-calc="2">리셋</button>
                                    <button type="button" class="calc-save" data-calc="2" style="display:none">저장</button>
                                </div>
                            </div>
                            <div class="calc-detail" id="calc-2-detail">
                                <div class="calc-mini-graph-collapse" id="calc-2-mini-graph-collapse" data-calc="2">
                                    <div class="calc-mini-graph-header">그래프</div>
                                    <div class="calc-mini-graph-body"><div class="calc-mini-graph-wrap" id="calc-2-mini-graph" title="최근 정/꺽 그래프 (좌=최신)"></div></div>
                                </div>
                                <div class="calc-round-table-wrap" id="calc-2-round-table-wrap"></div>
                                <div class="calc-export-line" style="margin:6px 0;"><button type="button" class="calc-export-csv" data-calc="2">전체 내보내기 (CSV)</button> <span class="calc-export-hint" style="color:#888;font-size:0.85em">표는 최근 200회차까지 표시 (전체 내보내기로 전체 확인)</span></div>
                                <div class="calc-streak" id="calc-2-streak">경기결과 (최근 30회): -</div>
                                <div class="calc-stats" id="calc-2-stats">최대연승: - | 최대연패: - | 모양승률: - | 표승률: - | 15회승률: - | 모양판별승률: -</div>
                            </div>
                        </div>
                    </div>
                    <div class="calc-dropdown collapsed" data-calc="3">
                        <div class="calc-dropdown-header">
                            <span class="calc-title">계산기 3</span>
                            <span class="calc-status idle" id="calc-3-status">대기중</span>
                            <span class="calc-cards-wrap" id="calc-3-cards-wrap">
                                <span class="calc-card-item"><span class="calc-card-label">배팅중</span><div class="calc-card-box"><div class="calc-round-line" id="calc-3-current-round"></div><span class="calc-current-card calc-card-betting" id="calc-3-current-card"></span></div></span>
                                <span class="calc-card-item"><span class="calc-card-label">예측픽</span><div class="calc-card-box"><div class="calc-round-line" id="calc-3-prediction-round"></div><span class="calc-current-card calc-card-prediction" id="calc-3-prediction-card"></span></div></span>
                            </span>
                            <div class="calc-summary" id="calc-3-summary">보유자산 - | 순익 - | 배팅중 -</div>
                            <span class="calc-toggle">▼</span>
                        </div>
                        <div class="calc-dropdown-body" id="calc-3-body">
                            <div class="calc-body-row">
                                <table class="calc-settings-table">
                                    <tr><td>자본/배팅</td><td><label>자본금 <input type="number" id="calc-3-capital" min="0" value="1000000"></label> <label>배팅금액 <input type="number" id="calc-3-base" min="1" value="10000"></label> <label>배당 <input type="number" id="calc-3-odds" min="1" step="0.01" value="1.97"></label></td></tr>
                                </table>
                                <div class="calc-options-wrap collapsed" data-calc="3">
                                    <div class="calc-options-toggle"><span class="calc-options-label">옵션</span><span class="calc-options-icon">▼</span></div>
                                    <div class="calc-options-body">
                                        <table class="calc-settings-table">
                                            <tr><td>픽/승률</td><td><label class="calc-reverse"><input type="checkbox" id="calc-3-reverse"> 반픽</label> <label><input type="checkbox" id="calc-3-win-rate-reverse"> 승률반픽</label> <label title="모양승률(예측기표 모양픽 최근 50회) 이 값 이하일 때 반픽">모양승률≤<input type="number" id="calc-3-win-rate-threshold" min="0" max="100" value="50" class="calc-threshold-input" title="모양승률 이 값 이하일 때 반픽">% 이하일 때 반픽</label></td></tr>
                                            <tr><td>연패반픽</td><td><label><input type="checkbox" id="calc-3-lose-streak-reverse"> 연패≥<input type="number" id="calc-3-lose-streak-reverse-min" min="2" max="15" value="3" class="calc-threshold-input" title="이 값 이상 연패일 때">이상·합산승률≤<input type="number" id="calc-3-lose-streak-reverse-threshold" min="0" max="100" value="48" class="calc-threshold-input" title="이 값 이하일 때 반대픽">%일 때 반대픽</label></td></tr>
                                            <tr><td>승률방향</td><td><label><input type="checkbox" id="calc-3-win-rate-direction-reverse" title="저점→고점 정픽, 고점→저점 반대픽, 정체 시 직전 방향 참조"> 승률방향 반픽 (저점↑정픽·고점↓반대·정체=직전방향)</label> <label><input type="checkbox" id="calc-3-streak-suppress-reverse" title="5연승 또는 5연패일 때 반픽 억제"> 줄 5 이상 반픽 억제</label> <label><input type="checkbox" id="calc-3-lock-direction-on-lose-streak" title="배팅이 연패 중일 때 방향을 바꾸지 않고 진행하던 방향 유지" checked> 연패 중 방향 고정</label></td></tr>
                                            <tr><td>모양</td><td><label><input type="checkbox" id="calc-3-shape-only-latest-next-pick" title="모양 판별의 가장 최근 다음 픽에 뜬 픽에만 배팅. 값이 없으면 배팅 안 함"> 가장 최근 다음 픽에만 배팅 (값 없으면 배팅 안 함)</label></td></tr>
                                            <tr><td>모양판별</td><td><label><input type="checkbox" id="calc-3-shape-prediction" title="덩어리 끝 변형 허용·퐁당 가중치 등 개선된 모양 판별로 픽. 기존 모양옵션과 별도."> 모양판별 픽 사용</label> <label><input type="checkbox" id="calc-3-shape-prediction-reverse"> 모양판별반픽</label> <label title="계산기표 2~11행 모양판별승률 이 값 이하일 때 반픽">모양판별승률≤<input type="number" id="calc-3-shape-prediction-reverse-threshold" min="0" max="100" value="50" class="calc-threshold-input">%일 때 반픽</label> <label title="모양판별 계산식 내 shape/chunk/퐁당/대칭 배율(0~3, 기본 1)">shape×<input type="number" id="calc-3-shape-weight" min="0" max="3" step="0.1" value="1.5" class="calc-threshold-input" style="width:3em"> chunk×<input type="number" id="calc-3-chunk-weight" min="0" max="3" step="0.1" value="1" class="calc-threshold-input" style="width:3em"> 퐁당×<input type="number" id="calc-3-pong-weight" min="0" max="3" step="0.1" value="1.5" class="calc-threshold-input" style="width:3em"> 대칭×<input type="number" id="calc-3-symmetry-weight" min="0" max="3" step="0.1" value="1" class="calc-threshold-input" style="width:3em"></label></td></tr>
                                            <tr><td>모양판별 로그</td><td><div id="calc-3-shape-prediction-log" class="shape-prediction-log" style="font-size:0.8em;color:#888;max-height:80px;overflow-y:auto;white-space:pre-wrap;">—</div></td></tr>
                                            <tr><td>멈춤</td><td><label><input type="checkbox" id="calc-3-pause-low-win-rate"> 계산기 표 15회승률≤<input type="number" id="calc-3-pause-win-rate-threshold" min="0" max="100" value="45" class="calc-threshold-input" title="해당 계산기 표의 15회 승률이 이 값 이하일 때 배팅멈춤. 표 하단 15회승률과 동일 기준">% 이하일 때 배팅멈춤</label></td></tr>
                                            <tr><td>시간</td><td><label>지속 시간(분) <input type="number" id="calc-3-duration" min="0" value="0" placeholder="0=무제한"></label> <label class="calc-duration-check"><input type="checkbox" id="calc-3-duration-check"> 지정 시간만 실행</label></td></tr>
                                            <tr><td>마틴</td><td><label class="calc-martingale"><input type="checkbox" id="calc-3-martingale"> 마틴 적용</label> <label>마틴 방식 <select id="calc-3-martingale-type"><option value="pyo" selected>표마틴</option><option value="pyo_half">표마틴 반</option></select></label></td></tr>
                                            <tr><td>목표</td><td><label><input type="checkbox" id="calc-3-target-enabled"> 목표금액 설정</label> <label>목표 <input type="number" id="calc-3-target-amount" min="0" value="0" placeholder="0=미사용">원</label> <span class="calc-target-hint" id="calc-3-target-hint" style="color:#888;font-size:0.85em"></span></td></tr>
                                            <tr><td>배팅복사</td><td><span id="calc-3-bet-copy-line" class="calc-bet-copy-line">—</span></td></tr>
                                        </table>
                                    </div>
                                </div>
                                <div class="calc-buttons">
                                    <button type="button" class="calc-run" data-calc="3">실행</button>
                                    <button type="button" class="calc-stop" data-calc="3">정지</button>
                                    <button type="button" class="calc-reset" data-calc="3">리셋</button>
                                    <button type="button" class="calc-save" data-calc="3" style="display:none">저장</button>
                                </div>
                            </div>
                            <div class="calc-detail" id="calc-3-detail">
                                <div class="calc-mini-graph-collapse" id="calc-3-mini-graph-collapse" data-calc="3">
                                    <div class="calc-mini-graph-header">그래프</div>
                                    <div class="calc-mini-graph-body"><div class="calc-mini-graph-wrap" id="calc-3-mini-graph" title="최근 정/꺽 그래프 (좌=최신)"></div></div>
                                </div>
                                <div class="calc-round-table-wrap" id="calc-3-round-table-wrap"></div>
                                <div class="calc-export-line" style="margin:6px 0;"><button type="button" class="calc-export-csv" data-calc="3">전체 내보내기 (CSV)</button> <span class="calc-export-hint" style="color:#888;font-size:0.85em">표는 최근 200회차까지 표시 (전체 내보내기로 전체 확인)</span></div>
                                <div class="calc-streak" id="calc-3-streak">경기결과 (최근 30회): -</div>
                                <div class="calc-stats" id="calc-3-stats">최대연승: - | 최대연패: - | 모양승률: - | 표승률: - | 15회승률: - | 모양판별승률: -</div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            <div id="bet-log-panel" class="bet-log-panel">
                <div class="bet-log-actions"><button type="button" id="bet-log-clear-all">전체 삭제</button></div>
                <div id="bet-calc-log" class="bet-calc-log"></div>
            </div>
            <div id="bet-pause-guide-panel" class="bet-log-panel">
                <p class="pause-guide-desc">각 기준(15회 승률 ≤ N%일 때 멈춤)으로 시뮬레이션했을 때, <strong>실제로 배팅했을 구간</strong>의 승률입니다. 조커는 패로 반영.</p>
                <div class="bet-log-actions">
                    <label>데이터 <select id="pause-guide-source"><option value="pred">예측기 전체</option><option value="1">계산기 1</option><option value="2">계산기 2</option><option value="3">계산기 3</option></select></label>
                    <button type="button" id="pause-guide-calc">계산</button>
                </div>
                <div id="pause-guide-table-wrap" class="pause-guide-table-wrap"></div>
            </div>
        </div>
        <div class="status" id="status">로딩 중...</div>
    </div>
    <script>
        function convertCardNumber(num) {
            const numStr = String(num).trim();
            const numInt = parseInt(numStr);
            
            if (isNaN(numInt)) return numStr;
            
            // 숫자 변환: A(1), 2~9, 10(J), 11(J), 12(Q), 13(K)
            if (numInt === 1) return 'A';
            if (numInt === 10 || numInt === 11) return 'J';  // 10과 11 모두 J
            if (numInt === 12) return 'Q';
            if (numInt === 13) return 'K';
            
            return numStr;
        }
        
        function parseCardValue(value) {
            if (!value) return { number: '', suit: '♥', isRed: true };
            
            // 문양 매핑: H=하트, D=다이아몬드, S=스페이드, C=클럽
            const suitMap = {
                'H': { icon: '♥', isRed: true },
                'D': { icon: '♦', isRed: true },
                'S': { icon: '♠', isRed: false },
                'C': { icon: '♣', isRed: false }
            };
            
            // 첫 글자가 문양인지 확인
            const firstChar = value.charAt(0).toUpperCase();
            if (suitMap[firstChar]) {
                const numberStr = value.substring(1).trim();
                return {
                    number: convertCardNumber(numberStr),
                    suit: suitMap[firstChar].icon,
                    isRed: suitMap[firstChar].isRed
                };
            }
            
            // 기본값
            return { number: convertCardNumber(value), suit: '♥', isRed: true };
        }
        
        function getCategory(result) {
            if (result.joker) return { text: '조커', class: 'joker' };
            if (result.hi && result.lo) return { text: '비김', class: 'draw' };
            if (result.hi) return { text: 'HI ↑', class: 'hi' };
            if (result.lo) return { text: 'LO ↓', class: 'lo' };
            if (result.red && !result.black) return { text: 'RED', class: 'red-only' };
            if (result.black && !result.red) return { text: 'BLACK', class: 'black-only' };
            return null;
        }
        
        function createCard(result, index, colorMatchResult) {
            const cardWrapper = document.createElement('div');
            cardWrapper.className = 'card-wrapper';
            
            const card = document.createElement('div');
            const isJoker = result.joker;
            
            // 조커 카드는 파란색 배경 (일반 카드와 같은 사이즈, 텍스트로 맞춤)
            if (isJoker) {
                card.className = 'card';
                card.style.background = '#2196f3';
                card.style.color = '#fff';
                
                // 문양 아이콘 자리에 "J" 텍스트 (일반 카드와 같은 구조)
                const jokerIcon = document.createElement('div');
                jokerIcon.className = 'card-suit-icon';
                jokerIcon.textContent = 'J';
                card.appendChild(jokerIcon);
                
                // 숫자 자리에 "K" 텍스트 (일반 카드와 같은 구조)
                const jokerText = document.createElement('div');
                jokerText.className = 'card-value';
                jokerText.textContent = 'K';
                card.appendChild(jokerText);
            } else {
                const cardInfo = parseCardValue(result.result || '');
                card.className = 'card ' + (cardInfo.isRed ? 'red' : 'black');
                
                // 문양 아이콘 (크게)
                const suitIcon = document.createElement('div');
                suitIcon.className = 'card-suit-icon';
                suitIcon.textContent = cardInfo.suit;
                card.appendChild(suitIcon);
                
                // 카드 숫자 (크게)
                const valueDiv = document.createElement('div');
                valueDiv.className = 'card-value';
                valueDiv.textContent = cardInfo.number;
                card.appendChild(valueDiv);
            }
            
            cardWrapper.appendChild(card);
            
            // 카테고리 표시 (별도 박스, 카드 아래)
            const category = getCategory(result);
            if (category) {
                const categoryDiv = document.createElement('div');
                categoryDiv.className = 'card-category ' + category.class;
                categoryDiv.textContent = category.text;
                cardWrapper.appendChild(categoryDiv);
            }
            
            // 색상 비교 결과 표시 (모든 카드, 하이로우 박스 아래)
            // null이나 undefined가 아니고, boolean 값일 때만 표시
            if (colorMatchResult !== null && colorMatchResult !== undefined && typeof colorMatchResult === 'boolean') {
                const colorMatchDiv = document.createElement('div');
                colorMatchDiv.className = 'color-match ' + (colorMatchResult === true ? 'jung' : 'kkuk');
                colorMatchDiv.textContent = colorMatchResult === true ? '정' : '꺽';
                cardWrapper.appendChild(colorMatchDiv);
            }
            
            return cardWrapper;
        }
        
        // 각 카드의 색상 비교 결과 저장 (gameID를 키로, 비교 대상 gameID도 함께 저장)
        const colorMatchCache = {};
        // 최근 150개 결과 저장 (카드 15개, 그래프는 전부 쭉 표시)
        let allResults = [];
        let isLoadingResults = false;  // 중복 요청 방지
        let resultsRequestId = 0;       // 응답 순서: 늦게 도착한 응답은 적용 안 함 (깜빡임 방지)
        // 예측 기록 (최근 30회): { round, predicted, actual } — 새로고침 후에도 유지되도록 localStorage 저장
        const PREDICTION_HISTORY_KEY = 'tokenHiloPredictionHistory';
        let predictionHistory = [];
        // 회차별 실제 결과(카드 기준) — API round_actuals로 예측기표 결과열 표시 보정
        let roundActualsFromServer = {};
        try {
            const saved = localStorage.getItem(PREDICTION_HISTORY_KEY);
            if (saved) {
                const parsed = JSON.parse(saved);
                if (Array.isArray(parsed)) predictionHistory = parsed.slice(-300).filter(function(h) { return h && typeof h === 'object'; });
            }
        } catch (e) { /* 복원 실패 시 빈 배열 유지 */ }
        function savePredictionHistory() {
            try { localStorage.setItem(PREDICTION_HISTORY_KEY, JSON.stringify(predictionHistory)); } catch (e) {}
        }
        function savePredictionHistoryToServer(round, predicted, actual, probability, pickColor) {
            const body = { round: round, predicted: predicted, actual: actual };
            if (probability != null) body.probability = probability;
            if (pickColor) body.pickColor = pickColor;
            fetch('/api/prediction-history', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).catch(function() {});
        }
        // 배팅 색상 통일: RED/빨강 → 빨강, BLACK/검정 → 검정 (표시·저장 일관성)
        function normalizePickColor(pc) {
            if (pc == null || pc === '') return '';
            var s = String(pc).trim();
            if (s.toUpperCase() === 'RED' || s === '빨강') return '빨강';
            if (s.toUpperCase() === 'BLACK' || s === '검정') return '검정';
            return s;
        }
        function pickColorToClass(pc) {
            var n = normalizePickColor(pc);
            return n === '빨강' ? 'pick-red' : (n === '검정' ? 'pick-black' : '');
        }
        // 회차별 순차 아이콘: 별→세모→동그라미 (회차 바뀔 때마다 아이콘 변경으로 구분)
        function getRoundIcon(round) {
            var r = parseInt(round, 10);
            if (isNaN(r) || r < 1) return '★';
            var icons = ['★', '△', '○'];
            return icons[(r - 1) % 3];
        }
        // 계산기 회차줄용: 아이콘에 색상 클래스 넣은 HTML (별=노랑, 세모=빨강, 동그라미=파랑)
        function getRoundIconHtml(round) {
            var r = parseInt(round, 10);
            if (isNaN(r) || r < 1) return '<span class="calc-icon calc-icon-star">★</span>';
            var idx = (r - 1) % 3;
            var classes = ['calc-icon calc-icon-star', 'calc-icon calc-icon-triangle', 'calc-icon calc-icon-circle'];
            var chars = ['★', '△', '○'];
            return '<span class="' + classes[idx] + '">' + chars[idx] + '</span>';
        }
        // 회차별 아이콘 타입 (별/세모/동그라미) — 배지·표 셀 색상용
        function getRoundIconType(round) {
            var r = parseInt(round, 10);
            if (isNaN(r) || r < 1) return 'star';
            var types = ['star', 'triangle', 'circle'];
            return types[(r - 1) % 3];
        }
        // 회차 4자리만 표시 (끝 4자리)
        function roundLast4(round) {
            if (round == null) return '-';
            var s = String(round);
            if (s.length <= 4) return s;
            return s.slice(-4);
        }
        var _lastCalcHistKey = {};  // 계산기별 마지막 history 키 (불필요한 갱신 방지)
        function needCalcUpdate(id) {
            var state = calcState[id];
            if (!state || !state.history) return true;
            var len = state.history.length;
            var last = len > 0 ? state.history[len - 1] : null;
            var key = len + '-' + (last ? (last.round + '_' + (last.actual || '')) : '');
            if (_lastCalcHistKey[id] === key) return false;
            _lastCalcHistKey[id] = key;
            return true;
        }
        let lastPrediction = null;  // { value: '정'|'꺽', round: number }
        var lastServerPrediction = null;  // 서버 예측 (있으면 표시·pending 동기화용)
        var lastIs15Joker = false;  // 15번 카드 조커 여부 (계산기 예측픽에 보류 반영용)
        /** 승률 방향 메뉴: 최근 100회 기준 고점/저점/방향. { round, rate50 } 최대 300개 */
        var winRate50History = [];
        var lastWinRateDirectionRef = null;  // 정체 시 '기존 전략 유지 (오름/반대)' 표시용
        var roundPredictionBuffer = {};   // 회차별 예측 저장 (표 충돌 방지: 결과 반영 시 해당 회차만 조회)
        var ROUND_PREDICTION_BUFFER_MAX = 50;
        var savedBetPickByRound = {};     // 배팅중 카드 그릴 때 걸은 픽 저장 (표에 넣을 때 이 값 사용 → 예측픽/재계산과 충돌 방지)
        var SAVED_BET_PICK_MAX = 50;
        var lastPostedCurrentPick = {};   // 계산기별 마지막으로 POST한 픽 — 같으면 재전송 안 함 (충돌·깜빡임 방지)
        function postCurrentPickIfChanged(id, payload) {
            var key = { round: payload.round ?? null, pickColor: payload.pickColor ?? null, suggested_amount: payload.suggested_amount ?? null, running: payload.running };
            var last = lastPostedCurrentPick[id];
            if (last && last.round === key.round && last.pickColor === key.pickColor && last.suggested_amount === key.suggested_amount && last.running === key.running) return;
            lastPostedCurrentPick[id] = key;
            try { fetch('/api/current-pick-relay', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ calculator: id, pickColor: payload.pickColor ?? null, round: payload.round ?? null, suggested_amount: payload.suggested_amount ?? null, running: payload.running }) }).catch(function() {}); } catch (e) {}
        }
        function setRoundPrediction(round, pred) {
            if (round == null || !pred) return;
            roundPredictionBuffer[String(round)] = { value: pred.value, round: round, prob: pred.prob != null ? pred.prob : 0, color: pred.color || null };
            var keys = Object.keys(roundPredictionBuffer).map(Number).filter(function(k) { return !isNaN(k); }).sort(function(a,b) { return a - b; });
            while (keys.length > ROUND_PREDICTION_BUFFER_MAX) {
                delete roundPredictionBuffer[String(keys.shift())];
            }
        }
        function getRoundPrediction(round) {
            if (round == null) return null;
            return roundPredictionBuffer[String(round)] || null;
        }
        var lastWarningU35 = false;       // U자+줄 3~5 구간 감지 시 서버가 보낸 경고
        var lastPongChunkPhase = null;    // 퐁당/덩어리 구간 판별 결과
        var lastPongChunkDebug = {};      // 퐁당/덩어리 판별 디버그 데이터
        let lastWinEffectRound = null;  // 승리 이펙트를 이미 보여준 회차 (한 번만 표시)
        let lastLoseEffectRound = null;  // 실패 이펙트를 이미 보여준 회차 (한 번만 표시)
        var prevSymmetryCounts = { left: null, right: null };  // 이전 시점 20열 줄 개수 (새 구간 빨리 캐치용)
        const CALC_IDS = [1, 2, 3];
        const CALC_SESSION_KEY = 'tokenHiloCalcSessionId';
        const CALC_STATE_BACKUP_KEY = 'tokenHiloCalcStateBackup';
        const calcState = {};
        // 표마틴: 기준금액(배팅금액)에 맞게 9단계. 비율 [1, 1.5, 2.5, 4, 7, 12, 20, 40, 40]
        var MARTIN_PYO_RATIOS = [1, 1.5, 2.5, 4, 7, 12, 20, 40, 40];
        function getMartinTable(type, baseAmount) {
            var base = (baseAmount != null && !isNaN(Number(baseAmount)) && Number(baseAmount) > 0) ? Number(baseAmount) : 10000;
            var table = MARTIN_PYO_RATIOS.map(function(r) { return Math.round(base * r); });
            return (type === 'pyo_half') ? table.map(function(x) { return Math.floor(x / 2); }) : table;
        }
        CALC_IDS.forEach(id => {
            calcState[id] = {
                running: false,
                started_at: 0,
                history: [],
                elapsed: 0,
                duration_limit: 0,
                use_duration_limit: false,
                reverse: false,
                win_rate_reverse: false,
                win_rate_threshold: 50,
                lose_streak_reverse: false,
                lose_streak_reverse_threshold: 48,
                lose_streak_reverse_min_streak: 3,
                win_rate_direction_reverse: false,
                streak_suppress_reverse: false,
                lock_direction_on_lose_streak: true,
                shape_only_latest_next_pick: false,
                shape_prediction: false,
                shape_weight: 1.5,
                chunk_weight: 1,
                pong_weight: 1.5,
                symmetry_weight: 1,
                last_trend_direction: null,
                martingale: false,
                martingale_type: 'pyo',
                target_enabled: false,
                target_amount: 0,
                timer_completed: false,
                timerId: null,
                maxWinStreakEver: 0,
                maxLoseStreakEver: 0,
                first_bet_round: 0,
                pause_low_win_rate_enabled: false,
                pause_win_rate_threshold: 45,
                paused: false,
                last_win_rate_zone: null,
                last_win_rate_zone_change_round: null
            };
        });
        let lastServerTimeSec = 0;  // /api/current-status 등에서 갱신
        function getServerTimeSec() { return lastServerTimeSec || Math.floor(Date.now() / 1000); }
        function buildCalcPayload() {
            const payload = {};
            CALC_IDS.forEach(id => {
                const durEl = document.getElementById('calc-' + id + '-duration');
                const checkEl = document.getElementById('calc-' + id + '-duration-check');
                const revEl = document.getElementById('calc-' + id + '-reverse');
                const duration_min = (durEl && parseInt(durEl.value, 10)) || 0;
                const duration_limit = duration_min * 60;
                const use_duration_limit = !!(checkEl && checkEl.checked);
                const winRateRevEl = document.getElementById('calc-' + id + '-win-rate-reverse');
                const winRateThrEl = document.getElementById('calc-' + id + '-win-rate-threshold');
                var winRateThr = (winRateThrEl && !isNaN(parseFloat(winRateThrEl.value))) ? Math.max(0, Math.min(100, parseFloat(winRateThrEl.value))) : 50;
                if (typeof winRateThr !== 'number' || isNaN(winRateThr)) winRateThr = 50;
                const martingaleEl = document.getElementById('calc-' + id + '-martingale');
                const martingaleTypeEl = document.getElementById('calc-' + id + '-martingale-type');
                const pauseLowEl = document.getElementById('calc-' + id + '-pause-low-win-rate');
                const pauseThrEl = document.getElementById('calc-' + id + '-pause-win-rate-threshold');
                var pauseThr = (pauseThrEl && !isNaN(parseFloat(pauseThrEl.value))) ? Math.max(0, Math.min(100, parseFloat(pauseThrEl.value))) : 45;
                if (typeof pauseThr !== 'number' || isNaN(pauseThr)) pauseThr = 45;
                const capVal = parseFloat(document.getElementById('calc-' + id + '-capital')?.value);
                const baseVal = parseFloat(document.getElementById('calc-' + id + '-base')?.value);
                const oddsVal = parseFloat(document.getElementById('calc-' + id + '-odds')?.value);
                var histRaw = dedupeCalcHistoryByRound((calcState[id].history || []).slice(-50000));
                var baseAmt = (baseVal != null && !isNaN(baseVal) && baseVal >= 1) ? baseVal : 10000;
                var histNorm = histRaw.map(function(h) {
                    var out = Object.assign({}, h);
                    if (out.no_bet === true) out.betAmount = 0;
                    else if (out.betAmount == null || isNaN(Number(out.betAmount))) out.betAmount = baseAmt;
                    else out.betAmount = Number(out.betAmount);
                    return out;
                });
                payload[String(id)] = {
                    running: calcState[id].running,
                    started_at: calcState[id].started_at || 0,
                    history: histNorm,
                    capital: (capVal != null && !isNaN(capVal) && capVal >= 0) ? capVal : 1000000,
                    base: (baseVal != null && !isNaN(baseVal) && baseVal >= 1) ? baseVal : 10000,
                    odds: (oddsVal != null && !isNaN(oddsVal) && oddsVal >= 1) ? oddsVal : 1.97,
                    duration_limit: duration_limit,
                    use_duration_limit: use_duration_limit,
                    reverse: !!(revEl && revEl.checked),
                    win_rate_reverse: !!(winRateRevEl && winRateRevEl.checked),
                    win_rate_threshold: winRateThr,
                    lose_streak_reverse: !!(document.getElementById('calc-' + id + '-lose-streak-reverse') && document.getElementById('calc-' + id + '-lose-streak-reverse').checked),
                    lose_streak_reverse_threshold: (function() { var el = document.getElementById('calc-' + id + '-lose-streak-reverse-threshold'); var v = el && !isNaN(parseFloat(el.value)) ? Math.max(0, Math.min(100, parseFloat(el.value))) : 48; return typeof v === 'number' && !isNaN(v) ? v : 48; })(),
                    lose_streak_reverse_min_streak: (function() { var el = document.getElementById('calc-' + id + '-lose-streak-reverse-min'); var v = el && !isNaN(parseInt(el.value, 10)) ? Math.max(2, Math.min(15, parseInt(el.value, 10))) : 3; return typeof v === 'number' && !isNaN(v) ? v : 3; })(),
                    win_rate_direction_reverse: !!(document.getElementById('calc-' + id + '-win-rate-direction-reverse') && document.getElementById('calc-' + id + '-win-rate-direction-reverse').checked),
                    streak_suppress_reverse: !!(document.getElementById('calc-' + id + '-streak-suppress-reverse') && document.getElementById('calc-' + id + '-streak-suppress-reverse').checked),
                    lock_direction_on_lose_streak: !!(document.getElementById('calc-' + id + '-lock-direction-on-lose-streak') && document.getElementById('calc-' + id + '-lock-direction-on-lose-streak').checked),
                    shape_only_latest_next_pick: !!(document.getElementById('calc-' + id + '-shape-only-latest-next-pick') && document.getElementById('calc-' + id + '-shape-only-latest-next-pick').checked),
                    shape_prediction: !!(document.getElementById('calc-' + id + '-shape-prediction') && document.getElementById('calc-' + id + '-shape-prediction').checked),
                    shape_prediction_reverse: !!(document.getElementById('calc-' + id + '-shape-prediction-reverse') && document.getElementById('calc-' + id + '-shape-prediction-reverse').checked),
                    shape_prediction_reverse_threshold: (function() { var el = document.getElementById('calc-' + id + '-shape-prediction-reverse-threshold'); return (el && !isNaN(parseFloat(el.value))) ? Math.max(0, Math.min(100, parseFloat(el.value))) : 50; })(),
                    shape_weight: (function() { var el = document.getElementById('calc-' + id + '-shape-weight'); return (el && !isNaN(parseFloat(el.value))) ? Math.max(0, Math.min(3, parseFloat(el.value))) : 1.5; })(),
                    chunk_weight: (function() { var el = document.getElementById('calc-' + id + '-chunk-weight'); return (el && !isNaN(parseFloat(el.value))) ? Math.max(0, Math.min(3, parseFloat(el.value))) : 1; })(),
                    pong_weight: (function() { var el = document.getElementById('calc-' + id + '-pong-weight'); return (el && !isNaN(parseFloat(el.value))) ? Math.max(0, Math.min(3, parseFloat(el.value))) : 1.5; })(),
                    symmetry_weight: (function() { var el = document.getElementById('calc-' + id + '-symmetry-weight'); return (el && !isNaN(parseFloat(el.value))) ? Math.max(0, Math.min(3, parseFloat(el.value))) : 1; })(),
                    last_trend_direction: (calcState[id].last_trend_direction === 'up' || calcState[id].last_trend_direction === 'down') ? calcState[id].last_trend_direction : null,
                    martingale: !!(martingaleEl && martingaleEl.checked),
                    martingale_type: (martingaleTypeEl && martingaleTypeEl.value) || 'pyo',
                    target_enabled: !!(document.getElementById('calc-' + id + '-target-enabled') && document.getElementById('calc-' + id + '-target-enabled').checked),
                    target_amount: Math.max(0, parseInt(document.getElementById('calc-' + id + '-target-amount')?.value, 10) || 0),
                    pause_low_win_rate_enabled: !!(pauseLowEl && pauseLowEl.checked),
                    pause_win_rate_threshold: pauseThr,
                    paused: !!calcState[id].paused,
                    timer_completed: !!calcState[id].timer_completed,
                    max_win_streak_ever: calcState[id].maxWinStreakEver || 0,
                    max_lose_streak_ever: calcState[id].maxLoseStreakEver || 0,
                    first_bet_round: calcState[id].first_bet_round || 0,
                    pending_round: calcState[id].running ? ((lastServerPrediction && lastServerPrediction.round) || calcState[id].pending_round) : null,
                    // 서버 history 저장 시 배팅 픽(반픽/승률반픽 적용) 사용. 예측기 픽(lastServerPrediction) 사용 시 정/꺽·색상 뒤바뀜 버그 발생
                    pending_predicted: (function() {
                        var pr = calcState[id].running ? ((lastServerPrediction && lastServerPrediction.round) || calcState[id].pending_round) : null;
                        var bet = (calcState[id].lastBetPickForRound && Number(calcState[id].lastBetPickForRound.round) === pr && (calcState[id].lastBetPickForRound.value === '정' || calcState[id].lastBetPickForRound.value === '꺽')) ? calcState[id].lastBetPickForRound.value : null;
                        if (bet) return bet;
                        // 모양옵션/모양판별 시: 서버 calc state(실제 배팅 픽) 우선 — lastServerPrediction은 메인 예측기라 1열·배팅중과 다를 수 있음
                        var shapeOn = !!(document.getElementById('calc-' + id + '-shape-only-latest-next-pick') && document.getElementById('calc-' + id + '-shape-only-latest-next-pick').checked);
                        var shapePredOn = !!(document.getElementById('calc-' + id + '-shape-prediction') && document.getElementById('calc-' + id + '-shape-prediction').checked);
                        if ((shapeOn || shapePredOn) && calcState[id].pending_predicted) return calcState[id].pending_predicted;
                        return calcState[id].running ? ((lastServerPrediction && lastServerPrediction.value) || calcState[id].pending_predicted) : null;
                    })(),
                    pending_prob: calcState[id].running ? ((lastServerPrediction && lastServerPrediction.prob != null) ? lastServerPrediction.prob : calcState[id].pending_prob) : null,
                    pending_color: (function() {
                        var pr = calcState[id].running ? ((lastServerPrediction && lastServerPrediction.round) || calcState[id].pending_round) : null;
                        var bet = (calcState[id].lastBetPickForRound && Number(calcState[id].lastBetPickForRound.round) === pr && (calcState[id].lastBetPickForRound.value === '정' || calcState[id].lastBetPickForRound.value === '꺽')) ? calcState[id].lastBetPickForRound : null;
                        if (bet) return bet.isRed ? '빨강' : '검정';
                        // 모양옵션/모양판별 시: 서버 calc state(실제 배팅 픽) 우선 — lastServerPrediction은 메인 예측기라 1열·배팅중과 다를 수 있음
                        var shapeOn = !!(document.getElementById('calc-' + id + '-shape-only-latest-next-pick') && document.getElementById('calc-' + id + '-shape-only-latest-next-pick').checked);
                        var shapePredOn = !!(document.getElementById('calc-' + id + '-shape-prediction') && document.getElementById('calc-' + id + '-shape-prediction').checked);
                        if ((shapeOn || shapePredOn) && calcState[id].pending_color) return calcState[id].pending_color;
                        return calcState[id].running ? ((lastServerPrediction && lastServerPrediction.color) || calcState[id].pending_color) : null;
                    })(),
                    pending_bet_amount: (calcState[id].pending_bet_amount != null && calcState[id].pending_bet_amount > 0) ? calcState[id].pending_bet_amount : null,
                    last_win_rate_zone: (calcState[id].last_win_rate_zone === 'high_falling' || calcState[id].last_win_rate_zone === 'low_rising' || calcState[id].last_win_rate_zone === 'mid_flat') ? calcState[id].last_win_rate_zone : null,
                    last_win_rate_zone_change_round: (calcState[id].last_win_rate_zone_change_round != null && !isNaN(Number(calcState[id].last_win_rate_zone_change_round))) ? Number(calcState[id].last_win_rate_zone_change_round) : null,
                    last_win_rate_zone_on_win: (calcState[id].last_win_rate_zone_on_win === 'high_falling' || calcState[id].last_win_rate_zone_on_win === 'low_rising' || calcState[id].last_win_rate_zone_on_win === 'mid_flat') ? calcState[id].last_win_rate_zone_on_win : null
                };
            });
            return payload;
        }
        function dedupeCalcHistoryByRound(hist) {
            if (!Array.isArray(hist) || hist.length === 0) return hist;
            var byRound = {};
            for (var i = 0; i < hist.length; i++) {
                var h = hist[i];
                if (!h || typeof h.predicted === 'undefined') continue;
                var rn = h.round != null ? Number(h.round) : NaN;
                if (isNaN(rn)) continue;
                var existing = byRound[rn];
                var merged = existing ? Object.assign({}, existing, h) : Object.assign({}, h);
                if ((!merged.pickColor || merged.pickColor === '') && existing && (existing.pickColor || existing.pick_color)) {
                    merged.pickColor = existing.pickColor || existing.pick_color;
                }
                // 순익 뻥튀기 방지: 같은 회차에 실제 결과(정/꺽/조커)가 있으면 pending으로 덮어쓰지 않음
                if (existing && existing.actual && existing.actual !== 'pending' && (!h.actual || h.actual === 'pending')) {
                    merged.actual = existing.actual;
                }
                // 멈춤(no_bet) 복원: 기존 또는 새 데이터에 no_bet이 있으면 배팅금액 0 유지
                if (merged.no_bet === true) merged.betAmount = 0;
                byRound[rn] = merged;
            }
            var rounds = Object.keys(byRound).map(Number).sort(function(a, b) { return a - b; });
            return rounds.map(function(r) { return byRound[r]; });
        }
        function applyCalcsToState(calcs, serverTimeSec, restoreUi, skipIds) {
            skipIds = skipIds || [];
            const st = serverTimeSec || Math.floor(Date.now() / 1000);
            const fullRestore = restoreUi === true;
            var currentPredRound = (typeof lastPrediction !== 'undefined' && lastPrediction && lastPrediction.round != null) ? Number(lastPrediction.round) : null;
            CALC_IDS.forEach(id => {
                if (skipIds.indexOf(id) >= 0) return;
                const c = calcs[String(id)] || {};
                // 실행 중일 때: 일반 폴링에서는 로컬 유지(깜빡임 방지). 서버가 로컬보다 많을 때만 서버 적용(창 내렸다 올렸을 때 계산기표 복구)
                const serverRunning = !!c.running;
                const localRunning = !!(calcState[id] && calcState[id].running);
                if (Array.isArray(c.history)) {
                    var raw = c.history.slice(-50000);
                    raw.forEach(function(h) {
                        if (!h) return;
                        if (h.no_bet === true) h.betAmount = 0;
                        if (h.betAmount === 0) h.no_bet = true;
                    });
                    var serverDeduped = dedupeCalcHistoryByRound(raw);
                    var localHist = (calcState[id].history || []);
                    // 로컬에 이미 저장된 픽(1열 배팅중 때 넣은 값) — 서버로 덮어쓰지 않고 유지
                    var localPickByRound = {};
                    localHist.forEach(function(h) {
                        if (!h || h.round == null) return;
                        if (h.predicted === '정' || h.predicted === '꺽') {
                            var rn = Number(h.round);
                            localPickByRound[rn] = { predicted: h.predicted, pickColor: h.pickColor || h.pick_color };
                        }
                    });
                    if (localRunning && serverRunning) {
                        var byRound = {};
                        serverDeduped.forEach(function(s) {
                            var rn = Number(s.round);
                            var loc = localHist.find(function(h) { return h && Number(h.round) === rn; });
                            if (loc && loc.no_bet === true) byRound[rn] = Object.assign({}, s, { no_bet: true, betAmount: 0 });
                            else if (loc && loc.actual === 'pending' && rn === currentPredRound) byRound[rn] = loc;
                            else if (loc && loc.actual !== 'pending' && loc.actual != null && (s.actual === 'pending' || !s.actual)) byRound[rn] = loc;
                            else byRound[rn] = s;
                        });
                        localHist.forEach(function(loc) {
                            if (!loc || loc.round == null) return;
                            var rn = Number(loc.round);
                            if (!(rn in byRound)) byRound[rn] = loc;
                        });
                        calcState[id].history = Object.keys(byRound).map(Number).sort(function(a, b) { return a - b; }).map(function(r) { return byRound[r]; });
                    } else if (localRunning) {
                        // 로컬만 실행 중(서버가 아직 반영 전): 히스토리를 서버로 통째로 덮어쓰지 않음 → 보유자산/순익/배팅중 깜빡임 방지
                        var byRoundLocal = {};
                        localHist.forEach(function(loc) {
                            if (!loc || loc.round == null) return;
                            byRoundLocal[Number(loc.round)] = loc;
                        });
                        serverDeduped.forEach(function(s) {
                            var rn = Number(s.round);
                            if (!(rn in byRoundLocal)) byRoundLocal[rn] = s;
                        });
                        calcState[id].history = Object.keys(byRoundLocal).map(Number).sort(function(a, b) { return a - b; }).map(function(r) { return byRoundLocal[r]; });
                    } else {
                        calcState[id].history = serverDeduped;
                    }
                    // 1열에 저장된 픽 그대로 유지: 서버 응답으로 predicted/pickColor 덮어쓰지 않음 → 승패만 actual 기준으로 일치
                    calcState[id].history.forEach(function(row) {
                        var rn = row.round != null ? Number(row.round) : NaN;
                        if (!isNaN(rn) && localPickByRound[rn]) {
                            row.predicted = localPickByRound[rn].predicted;
                            if (localPickByRound[rn].pickColor != null) row.pickColor = localPickByRound[rn].pickColor;
                        }
                    });
                } else {
                    if (!localRunning) calcState[id].history = [];
                }
                // 폴링 시: 로컬이 실행 중인데 서버가 아직 저장 전(running false, started_at 0)이면 덮어쓰지 않음 → 깜빡임 방지
                var serverStartedAt = c.started_at || 0;
                var localStartedAt = (calcState[id].started_at || 0);
                var staleServerAfterRun = !fullRestore && localRunning && !serverRunning && !serverStartedAt && (localStartedAt > 0);
                if (!staleServerAfterRun) {
                    calcState[id].running = !!c.running;
                    calcState[id].started_at = serverStartedAt || localStartedAt;
                }
                calcState[id].duration_limit = parseInt(c.duration_limit, 10) || 0;
                calcState[id].use_duration_limit = !!c.use_duration_limit;
                calcState[id].timer_completed = !!c.timer_completed;
                calcState[id].maxWinStreakEver = Math.max(0, parseInt(c.max_win_streak_ever, 10) || 0);
                calcState[id].maxLoseStreakEver = Math.max(0, parseInt(c.max_lose_streak_ever, 10) || 0);
                calcState[id].first_bet_round = Math.max(0, parseInt(c.first_bet_round, 10) || 0);
                calcState[id].elapsed = calcState[id].running && calcState[id].started_at ? Math.max(0, st - calcState[id].started_at) : 0;
                calcState[id].pending_round = c.pending_round != null ? c.pending_round : null;
                calcState[id].pending_predicted = c.pending_predicted != null ? c.pending_predicted : null;
                calcState[id].pending_prob = c.pending_prob != null ? c.pending_prob : null;
                calcState[id].pending_color = c.pending_color || null;
                calcState[id].pending_bet_amount = (c.pending_bet_amount != null && !isNaN(Number(c.pending_bet_amount)) && Number(c.pending_bet_amount) > 0) ? Number(c.pending_bet_amount) : null;
                calcState[id].pending_shape_debug = (c.pending_shape_debug && typeof c.pending_shape_debug === 'object') ? c.pending_shape_debug : null;
                var pauseThrRestore = (typeof c.pause_win_rate_threshold === 'number' && c.pause_win_rate_threshold >= 0 && c.pause_win_rate_threshold <= 100) ? c.pause_win_rate_threshold : 45;
                calcState[id].pause_low_win_rate_enabled = !!c.pause_low_win_rate_enabled;
                calcState[id].pause_win_rate_threshold = pauseThrRestore;
                // 폴링 시 실행 중이면 paused는 클라이언트 유지(서버의 예전 paused=true가 마틴 중 다시 멈춤 걸리지 않도록)
                if (fullRestore || !localRunning) calcState[id].paused = !!c.paused;
                calcState[id].lose_streak_reverse = !!c.lose_streak_reverse;
                var loseStreakThrRestore = (typeof c.lose_streak_reverse_threshold === 'number' && c.lose_streak_reverse_threshold >= 0 && c.lose_streak_reverse_threshold <= 100) ? c.lose_streak_reverse_threshold : 48;
                calcState[id].lose_streak_reverse_threshold = loseStreakThrRestore;
                var minStreakRestore = (typeof c.lose_streak_reverse_min_streak === 'number' && c.lose_streak_reverse_min_streak >= 2 && c.lose_streak_reverse_min_streak <= 15) ? c.lose_streak_reverse_min_streak : 3;
                calcState[id].lose_streak_reverse_min_streak = minStreakRestore;
                calcState[id].win_rate_direction_reverse = !!c.win_rate_direction_reverse;
                calcState[id].streak_suppress_reverse = !!c.streak_suppress_reverse;
                calcState[id].lock_direction_on_lose_streak = c.lock_direction_on_lose_streak !== false;
                calcState[id].shape_only_latest_next_pick = !!c.shape_only_latest_next_pick;
                calcState[id].shape_prediction = !!c.shape_prediction;
                calcState[id].shape_prediction_reverse = !!c.shape_prediction_reverse;
                calcState[id].shape_prediction_reverse_threshold = (typeof c.shape_prediction_reverse_threshold === 'number' && c.shape_prediction_reverse_threshold >= 0 && c.shape_prediction_reverse_threshold <= 100) ? c.shape_prediction_reverse_threshold : 50;
                calcState[id].shape_weight = (typeof c.shape_weight === 'number' && c.shape_weight >= 0 && c.shape_weight <= 3) ? c.shape_weight : 1.5;
                calcState[id].chunk_weight = (typeof c.chunk_weight === 'number' && c.chunk_weight >= 0 && c.chunk_weight <= 3) ? c.chunk_weight : 1;
                calcState[id].pong_weight = (typeof c.pong_weight === 'number' && c.pong_weight >= 0 && c.pong_weight <= 3) ? c.pong_weight : 1.5;
                calcState[id].symmetry_weight = (typeof c.symmetry_weight === 'number' && c.symmetry_weight >= 0 && c.symmetry_weight <= 3) ? c.symmetry_weight : 1;
                calcState[id].last_trend_direction = (c.last_trend_direction === 'up' || c.last_trend_direction === 'down') ? c.last_trend_direction : null;
                calcState[id].last_win_rate_zone = (c.last_win_rate_zone === 'high_falling' || c.last_win_rate_zone === 'low_rising' || c.last_win_rate_zone === 'mid_flat') ? c.last_win_rate_zone : null;
                calcState[id].last_win_rate_zone_change_round = (c.last_win_rate_zone_change_round != null && !isNaN(Number(c.last_win_rate_zone_change_round))) ? Number(c.last_win_rate_zone_change_round) : null;
                calcState[id].last_win_rate_zone_on_win = (c.last_win_rate_zone_on_win === 'high_falling' || c.last_win_rate_zone_on_win === 'low_rising' || c.last_win_rate_zone_on_win === 'mid_flat') ? c.last_win_rate_zone_on_win : null;
                if (!fullRestore) return;
                calcState[id].reverse = !!c.reverse;
                calcState[id].win_rate_reverse = !!c.win_rate_reverse;
                var thr = (typeof c.win_rate_threshold === 'number' && c.win_rate_threshold >= 0 && c.win_rate_threshold <= 100) ? c.win_rate_threshold : 50;
                calcState[id].win_rate_threshold = thr;
                calcState[id].martingale = !!c.martingale;
                calcState[id].martingale_type = (c.martingale_type === 'pyo_half' ? 'pyo_half' : 'pyo');
                calcState[id].target_enabled = !!c.target_enabled;
                calcState[id].target_amount = Math.max(0, parseInt(c.target_amount, 10) || 0);
                const durEl = document.getElementById('calc-' + id + '-duration');
                const checkEl = document.getElementById('calc-' + id + '-duration-check');
                const revEl = document.getElementById('calc-' + id + '-reverse');
                if (durEl) durEl.value = Math.floor((calcState[id].duration_limit || 0) / 60);
                if (checkEl) checkEl.checked = calcState[id].use_duration_limit;
                if (revEl) revEl.checked = !!c.reverse;
                const winRateRevEl = document.getElementById('calc-' + id + '-win-rate-reverse');
                if (winRateRevEl) winRateRevEl.checked = !!c.win_rate_reverse;
                const winRateThrEl = document.getElementById('calc-' + id + '-win-rate-threshold');
                if (winRateThrEl) { winRateThrEl.value = String(Math.round(thr)); }
                const loseStreakRevEl = document.getElementById('calc-' + id + '-lose-streak-reverse');
                const loseStreakThrEl = document.getElementById('calc-' + id + '-lose-streak-reverse-threshold');
                const loseStreakMinEl = document.getElementById('calc-' + id + '-lose-streak-reverse-min');
                if (loseStreakRevEl) loseStreakRevEl.checked = !!calcState[id].lose_streak_reverse;
                if (loseStreakThrEl) loseStreakThrEl.value = String(Math.round(calcState[id].lose_streak_reverse_threshold || 48));
                if (loseStreakMinEl) loseStreakMinEl.value = String(Math.max(2, Math.min(15, calcState[id].lose_streak_reverse_min_streak || 3)));
                const winRateDirRevEl = document.getElementById('calc-' + id + '-win-rate-direction-reverse');
                if (winRateDirRevEl) winRateDirRevEl.checked = !!calcState[id].win_rate_direction_reverse;
                var streakSuppressEl = document.getElementById('calc-' + id + '-streak-suppress-reverse');
                if (streakSuppressEl) streakSuppressEl.checked = !!calcState[id].streak_suppress_reverse;
                var lockDirEl = document.getElementById('calc-' + id + '-lock-direction-on-lose-streak');
                if (lockDirEl) lockDirEl.checked = calcState[id].lock_direction_on_lose_streak !== false;
                var shapeOnlyEl = document.getElementById('calc-' + id + '-shape-only-latest-next-pick');
                if (shapeOnlyEl) shapeOnlyEl.checked = !!calcState[id].shape_only_latest_next_pick;
                var shapePredEl = document.getElementById('calc-' + id + '-shape-prediction');
                if (shapePredEl) shapePredEl.checked = !!calcState[id].shape_prediction;
                var shapePredRevEl = document.getElementById('calc-' + id + '-shape-prediction-reverse');
                var shapePredRevThrEl = document.getElementById('calc-' + id + '-shape-prediction-reverse-threshold');
                if (shapePredRevEl) shapePredRevEl.checked = !!calcState[id].shape_prediction_reverse;
                if (shapePredRevThrEl) shapePredRevThrEl.value = String(Math.round(calcState[id].shape_prediction_reverse_threshold || 50));
                var shapeWeightEl = document.getElementById('calc-' + id + '-shape-weight');
                if (shapeWeightEl) shapeWeightEl.value = String(calcState[id].shape_weight != null ? calcState[id].shape_weight : 1.5);
                var chunkWeightEl = document.getElementById('calc-' + id + '-chunk-weight');
                if (chunkWeightEl) chunkWeightEl.value = String(calcState[id].chunk_weight != null ? calcState[id].chunk_weight : 1);
                var pongWeightEl = document.getElementById('calc-' + id + '-pong-weight');
                if (pongWeightEl) pongWeightEl.value = String(calcState[id].pong_weight != null ? calcState[id].pong_weight : 1.5);
                var symmetryWeightEl = document.getElementById('calc-' + id + '-symmetry-weight');
                if (symmetryWeightEl) symmetryWeightEl.value = String(calcState[id].symmetry_weight != null ? calcState[id].symmetry_weight : 1);
                const martingaleEl = document.getElementById('calc-' + id + '-martingale');
                const martingaleTypeEl = document.getElementById('calc-' + id + '-martingale-type');
                if (martingaleEl) martingaleEl.checked = !!calcState[id].martingale;
                if (martingaleTypeEl) martingaleTypeEl.value = (calcState[id].martingale_type === 'pyo_half' ? 'pyo_half' : 'pyo');
                const targetEnabledEl = document.getElementById('calc-' + id + '-target-enabled');
                const targetAmountEl = document.getElementById('calc-' + id + '-target-amount');
                if (targetEnabledEl) targetEnabledEl.checked = !!calcState[id].target_enabled;
                if (targetAmountEl) targetAmountEl.value = String(calcState[id].target_amount || 0);
                const pauseLowEl = document.getElementById('calc-' + id + '-pause-low-win-rate');
                const pauseThrEl = document.getElementById('calc-' + id + '-pause-win-rate-threshold');
                if (pauseLowEl) pauseLowEl.checked = !!calcState[id].pause_low_win_rate_enabled;
                if (pauseThrEl) pauseThrEl.value = String(Math.round(calcState[id].pause_win_rate_threshold || 45));
                const capitalEl = document.getElementById('calc-' + id + '-capital');
                const baseEl = document.getElementById('calc-' + id + '-base');
                const oddsEl = document.getElementById('calc-' + id + '-odds');
                if (capitalEl && typeof c.capital === 'number' && c.capital >= 0) capitalEl.value = String(c.capital);
                if (baseEl && typeof c.base === 'number' && c.base >= 1) baseEl.value = String(c.base);
                if (oddsEl && typeof c.odds === 'number' && c.odds >= 1) oddsEl.value = String(c.odds);
            });
        }
        /** 서버 적용 후 보유자산·순익·배팅중 깜빡임 방지: 실행 중인 계산기에 현재 회차 pending 행이 없으면 복원.
         * lastBetPickForRound/savedBetPickByRound 우선 사용 → 배팅중 픽과 1열 일치. */
        function ensurePendingRowForRunningCalc(id) {
            try {
                var state = calcState[id];
                if (!state || !state.running) return;
                if (typeof lastPrediction === 'undefined' || !lastPrediction || lastPrediction.round == null) return;
                var roundNum = Number(lastPrediction.round);
                var hist = state.history || [];
                var hasRound = hist.some(function(h) { return h && Number(h.round) === roundNum; });
                if (hasRound) return;
                var saved = (state.lastBetPickForRound && Number(state.lastBetPickForRound.round) === roundNum) ? state.lastBetPickForRound : (typeof savedBetPickByRound !== 'undefined' && savedBetPickByRound[roundNum]) ? savedBetPickByRound[roundNum] : null;
                var bettingText, bettingIsRed;
                var shapeOn = !!(state.shape_only_latest_next_pick);
                if (shapeOn && (state.pending_predicted === '정' || state.pending_predicted === '꺽') && (state.pending_color === '빨강' || state.pending_color === '검정')) {
                    bettingText = state.pending_predicted;
                    bettingIsRed = (state.pending_color === '빨강');
                } else if (saved && (saved.value === '정' || saved.value === '꺽')) {
                    bettingText = saved.value;
                    bettingIsRed = !!saved.isRed;
                } else {
                    bettingText = lastPrediction.value || '정';
                    bettingIsRed = (normalizePickColor(lastPrediction.color) === '빨강');
                    var rev = !!(state.reverse);
                    if (rev) { bettingText = bettingText === '정' ? '꺽' : '정'; bettingIsRed = !bettingIsRed; }
                }
                var isNoBet = (typeof effectivePausedForRound === 'function' && effectivePausedForRound(id)) || (typeof lastIs15Joker !== 'undefined' && lastIs15Joker);
                var betForThisRound = (typeof getBetForRound === 'function') ? getBetForRound(id, roundNum) : 0;
                var amt = isNoBet ? 0 : betForThisRound;
                if (typeof lastIs15Joker !== 'undefined' && lastIs15Joker) { bettingText = '보류'; bettingIsRed = false; amt = 0; }
                state.history = state.history || [];
                state.history.push({ round: roundNum, predicted: bettingText, pickColor: bettingIsRed ? '빨강' : '검정', betAmount: amt, no_bet: isNoBet, actual: 'pending', warningWinRate: null });
                state.history = dedupeCalcHistoryByRound(state.history);
            } catch (e) { console.warn('ensurePendingRowForRunningCalc', id, e); }
        }
        async function loadCalcStateFromServer(restoreUi) {
            if (window.__calcStateLoadInProgress) return;
            window.__calcStateLoadInProgress = true;
            try {
                if (restoreUi === undefined) restoreUi = true;
                // 항상 공용 세션만 사용 → 모바일/다른 PC에서 열어도 같은 진행 중 계산기 표시
                try { localStorage.setItem(CALC_SESSION_KEY, 'default'); } catch (e) {}
                const session_id = 'default';
                const url = '/api/calc-state?session_id=' + encodeURIComponent(session_id);
                const res = await fetch(url, { cache: 'no-cache' });
                const data = await res.json();
                if (data.session_id) localStorage.setItem(CALC_SESSION_KEY, data.session_id);
                lastServerTimeSec = data.server_time || Math.floor(Date.now() / 1000);
                let calcs = data.calcs || {};
                // 가이드 §6: 서버에 실행 중/히스토리 없으면 localStorage 백업으로 복원(새로고침 후 실행 상태 유지)
                const hasRunning = CALC_IDS.some(id => calcs[String(id)] && calcs[String(id)].running);
                const hasHistory = CALC_IDS.some(id => calcs[String(id)] && Array.isArray(calcs[String(id)].history) && calcs[String(id)].history.length > 0);
                if (!hasRunning && !hasHistory) {
                    try {
                        const backup = localStorage.getItem(CALC_STATE_BACKUP_KEY);
                        if (backup) {
                            const parsed = JSON.parse(backup);
                            if (parsed && typeof parsed === 'object') calcs = parsed;
                        }
                    } catch (e) { /* ignore */ }
                }
                applyCalcsToState(calcs, lastServerTimeSec, restoreUi);
                CALC_IDS.forEach(function(id) { if (typeof updateCalcStatus === 'function') updateCalcStatus(id); });
                CALC_IDS.forEach(function(id) { ensurePendingRowForRunningCalc(id); });
                CALC_IDS.forEach(function(id) { if (typeof updateCalcStatus === 'function') updateCalcStatus(id); });
            } catch (e) { console.warn('계산기 상태 로드 실패:', e); }
            finally { window.__calcStateLoadInProgress = false; }
        }
        var lastSaveCalcStateAt = 0;
        var SAVE_CALC_STATE_THROTTLE_MS = 2500;  // ERR_INSUFFICIENT_RESOURCES 방지: 동기화/폴링 유발 저장은 최대 2.5초에 1회
        async function saveCalcStateToServer(opt) {
            opt = opt || {};
            const skipApplyForIds = opt.skipApplyForIds || [];  // 정지/리셋 시 해당 calc는 서버 응답 적용 안 함 (로컬 상태 유지)
            const immediate = !!opt.immediate;  // 실행/정지/리셋 등 사용자 액션은 즉시 저장
            const now = Date.now();
            if (!immediate && (now - lastSaveCalcStateAt) < SAVE_CALC_STATE_THROTTLE_MS) return;
            lastSaveCalcStateAt = now;
            try {
                const session_id = 'default';
                const payload = buildCalcPayload();
                try {
                    localStorage.setItem(CALC_STATE_BACKUP_KEY, JSON.stringify(payload));
                } catch (e) { /* ignore */ }
                const res = await fetch('/api/calc-state', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ session_id: session_id, calcs: payload })
                });
                const data = await res.json().catch(function() { return {}; });
                if (data.calcs && typeof data.calcs === 'object') {
                    applyCalcsToState(data.calcs, data.server_time || lastServerTimeSec, false, skipApplyForIds);
                    // updateCalcStatus 먼저 → lastBetPickForRound 설정 후 ensurePendingRowForRunningCalc (배팅중·1열 일치)
                    CALC_IDS.forEach(function(id) {
                        if (skipApplyForIds.indexOf(id) >= 0) return;
                        if (typeof updateCalcStatus === 'function') updateCalcStatus(id);
                    });
                    CALC_IDS.forEach(function(id) {
                        if (skipApplyForIds.indexOf(id) >= 0) return;
                        ensurePendingRowForRunningCalc(id);
                    });
                    CALC_IDS.forEach(function(id) {
                        if (skipApplyForIds.indexOf(id) >= 0) return;
                        if (typeof updateCalcDetail === 'function') updateCalcDetail(id);
                        if (typeof updateCalcSummary === 'function') updateCalcSummary(id);
                    });
                }
            } catch (e) { console.warn('계산기 상태 저장 실패:', e); }
        }
        const BET_LOG_KEY = 'tokenHiloBetCalcLog';
        let betCalcLog = [];  // [{ line, calcId, history }, ...] 또는 레거시 문자열
        try {
            const saved = localStorage.getItem(BET_LOG_KEY);
            if (saved) {
                const parsed = JSON.parse(saved);
                if (Array.isArray(parsed)) betCalcLog = parsed;
            }
        } catch (e) { /* ignore */ }
        function saveBetCalcLog() {
            try { localStorage.setItem(BET_LOG_KEY, JSON.stringify(betCalcLog)); } catch (e) { /* ignore */ }
        }
        function buildLogDetailTable(hist, calcId) {
            let rows = [];
            for (let i = 0; i < hist.length; i++) {
                const h = hist[i];
                if (!h) continue;
                const pred = h.predicted === '정' ? '정' : (h.predicted === '꺽' ? '꺽' : '-');
                const res = h.actual === 'joker' ? '조' : (h.actual === '정' ? '정' : '꺽');
                const outcome = h.actual === 'joker' ? '조' : (h.predicted === h.actual ? '승' : '패');
                rows.push({ idx: i + 1, pick: pred, result: res, outcome: outcome });
            }
            let html = '<table><thead><tr><th>#</th><th>픽</th><th>결과</th><th>승패</th></tr></thead><tbody>';
            rows.forEach(function(r) {
                const c = r.outcome === '승' ? 'win' : r.outcome === '패' ? 'lose' : r.outcome === '조' ? 'joker' : 'skip';
                html += '<tr><td>' + r.idx + '</td><td>' + r.pick + '</td><td>' + r.result + '</td><td class="' + c + '">' + r.outcome + '</td></tr>';
            });
            html += '</tbody></table>';
            return html;
        }
        function renderBetCalcLog() {
            const logEl = document.getElementById('bet-calc-log');
            if (!logEl) return;
            logEl.innerHTML = '';
            betCalcLog.forEach(function(entry, idx) {
                const isObj = entry && typeof entry === 'object' && !Array.isArray(entry) && Object.prototype.hasOwnProperty.call(entry, 'line');
                const line = isObj ? entry.line : (typeof entry === 'string' ? entry : String(entry || ''));
                const hist = isObj && Array.isArray(entry.history) ? entry.history : [];
                const calcId = isObj ? entry.calcId : null;
                const div = document.createElement('div');
                div.className = 'log-entry';
                div.setAttribute('data-idx', idx);
                div.innerHTML = '<span class="log-text">' + String(line).replace(/</g, '&lt;').replace(/>/g, '&gt;') + '</span><div class="log-actions"><button type="button" class="log-detail-btn">상세보기</button><button type="button" class="log-delete-btn">삭제</button></div>';
                const detailDiv = document.createElement('div');
                detailDiv.className = 'log-detail';
                detailDiv.setAttribute('data-idx', idx);
                if (hist.length > 0) detailDiv.innerHTML = buildLogDetailTable(hist, calcId);
                div.appendChild(detailDiv);
                logEl.appendChild(div);
                div.querySelector('.log-detail-btn').addEventListener('click', function() {
                    detailDiv.classList.toggle('open');
                    this.textContent = detailDiv.classList.contains('open') ? '접기' : '상세보기';
                });
                div.querySelector('.log-delete-btn').addEventListener('click', function() {
                    betCalcLog.splice(idx, 1);
                    saveBetCalcLog();
                    renderBetCalcLog();
                });
            });
        }
        
        async function loadResults() {
            // 한 번에 하나만 요청: 동시 요청이 쌓여 서버 먹통·pending 폭증 방지
            if (isLoadingResults) return;
            const statusEl = document.getElementById('status');
            if (statusEl) statusEl.textContent = '데이터 요청 중...';
            const thisRequestId = ++resultsRequestId;
            
            try {
                isLoadingResults = true;
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 3000);
                
                const response = await fetch('/api/results?t=' + Date.now(), {
                    signal: controller.signal,
                    cache: 'no-cache'
                });
                
                clearTimeout(timeoutId);
                if (thisRequestId !== resultsRequestId) return;
                if (statusEl) statusEl.textContent = '결과 표시 중...';
                
                if (!response.ok) {
                    console.warn('결과 로드 실패:', response.status, response.statusText);
                    if (statusEl) statusEl.textContent = '결과 로드 실패 (' + response.status + ')';
                    return;
                }
                
                const data = await response.json();
                if (thisRequestId !== resultsRequestId) return;
                var hasResults = Array.isArray(data.results) && data.results.length > 0;
                if (data.error && !hasResults) {
                    if (statusEl) statusEl.textContent = '오류: ' + data.error;
                    return;
                }
                // 서버에 저장된 시스템 예측 기록 복원 (어디서 접속해도 동일). 무효 항목 제거해 ReferenceError 방지
                if (Object.prototype.hasOwnProperty.call(data, 'prediction_history') && Array.isArray(data.prediction_history)) {
                    predictionHistory = data.prediction_history.slice(-300).filter(function(h) { return h && typeof h === 'object'; });
                    roundActualsFromServer = (data.round_actuals && typeof data.round_actuals === 'object') ? data.round_actuals : {};
                    savePredictionHistory();
                    if (typeof renderWinRateDirectionPanel === 'function') renderWinRateDirectionPanel();
                    // 서버 prediction_history로 계산기 히스토리 '대기' 보정 — actual(결과)만 서버 값으로 채움. 픽(predicted/pickColor)은 배팅중 픽 유지(덮어쓰지 않음)
                    (function syncCalcHistoryFromServerPrediction() {
                        if (!Array.isArray(predictionHistory) || predictionHistory.length === 0) return;
                        var byRound = {};
                        predictionHistory.forEach(function(p) {
                            if (p && typeof p === 'object' && p.round != null && p.actual != null && p.actual !== '') {
                                byRound[Number(p.round)] = { actual: p.actual };
                            }
                        });
                        var changed = false;
                        CALC_IDS.forEach(function(id) {
                            var hist = calcState[id].history || [];
                            hist.forEach(function(h) {
                                if (!h || h.actual !== 'pending') return;
                                var r = Number(h.round);
                                if (isNaN(r)) return;
                                var fromServer = byRound[r];
                                if (!fromServer) return;
                                h.actual = fromServer.actual;
                                changed = true;
                            });
                        });
                        if (changed) try { saveCalcStateToServer(); } catch (e) {}
                    })();
                }
                // lastServerPrediction/lastPrediction은 아래에서 results 수락 시에만 설정 (깜빡임 방지)
                
                const newResults = data.results || [];
                const statusElement = document.getElementById('status');
                const cardsDiv = document.getElementById('cards');
                if (!statusElement || !cardsDiv) {
                    if (statusEl) statusEl.textContent = '화면 오류 - 새로고침 해 주세요';
                    return;
                }
                
                try {
                // 정/꺽 그래프 순서 일관성: gameID 기준 최신순 정렬 (항상 동일한 순서로 표시)
                function sortResultsNewestFirst(arr) {
                    return [...arr].sort((a, b) => {
                        const ga = String(a.gameID || '');
                        const gb = String(b.gameID || '');
                        const na = parseInt(ga, 10), nb = parseInt(gb, 10);
                        if (!isNaN(na) && !isNaN(nb)) return nb - na;  // 숫자면 높은 ID가 앞
                        return gb.localeCompare(ga);  // 문자열이면 역순
                    });
                }
                // 서버에서 결과가 오면 무조건 전체 교체. 병합 시 과거 데이터가 남아 최신 회차가 안 나오는 문제 방지
                let resultsUpdated = false;
                if (newResults.length > 0) {
                    allResults = sortResultsNewestFirst(newResults).slice(0, 300);
                    resultsUpdated = true;
                } else {
                    if (allResults.length === 0) {
                        allResults = [];
                        resultsUpdated = true;
                    } else {
                        allResults = sortResultsNewestFirst(allResults);
                        resultsUpdated = true;
                    }
                }
                
                // 서버 예측 반영 (서버에서 결과를 받았을 때마다 갱신). 곧바로 카드 갱신해 예측픽이 결과와 같이 보이게
                if (resultsUpdated) {
                    const sp = data.server_prediction;
                    lastServerPrediction = (sp && (sp.value === '정' || sp.value === '꺽')) ? sp : null;
                    lastWarningU35 = !!(lastServerPrediction && sp && sp.warning_u35);
                    lastPongChunkPhase = (sp && (sp.pong_chunk_phase != null && sp.pong_chunk_phase !== '')) ? sp.pong_chunk_phase : null;
                    lastPongChunkDebug = (sp && sp.pong_chunk_debug && typeof sp.pong_chunk_debug === 'object') ? sp.pong_chunk_debug : {};
                    if (sp && sp.round != null) {
                        var newRound = Number(sp.round);
                        var prevRound = (lastPrediction && lastPrediction.round != null) ? Number(lastPrediction.round) : NaN;
                        if (!isNaN(newRound) && (isNaN(prevRound) || newRound >= prevRound)) {
                            var normColor = normalizePickColor(sp.color) || sp.color || null;
                            lastPrediction = { value: sp.value, round: sp.round, prob: sp.prob != null ? sp.prob : 0, color: normColor };
                            setRoundPrediction(sp.round, lastPrediction);
                            if (lastServerPrediction) {
                                fetch('/api/round-prediction', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ round: sp.round, predicted: sp.value, pickColor: normColor || sp.color, probability: sp.prob }) }).catch(function() {});
                                if (typeof renderWinRateDirectionPanel === 'function') renderWinRateDirectionPanel();
                            }
                        }
                    }
                    lastResultsUpdate = Date.now();  // 갱신 완료 시점에 폴링 간격 리셋
                }
                
                // resultsUpdated가 false면 DOM 갱신 생략 (데이터 없을 때만)
                if (!resultsUpdated) {
                    return;
                }
                
                statusElement.textContent = `총 ${allResults.length}개 경기 결과 (표시: ${newResults.length}개)`;
                
                // 맨 왼쪽 = 최신 회차: 서버·클라이언트 모두 gameID 내림차순 정렬 완료. index 0이 최신.
                const displayResults = allResults.slice(0, 15);
                const results = allResults;  // 비교를 위해 전체 결과 사용
                // 픽/보류 깜빡임 방지: lastIs15Joker를 먼저 갱신한 뒤 계산기 카드·POST 갱신 (이전 값으로 보류/픽 뒤바뀌는 것 방지)
                lastIs15Joker = (displayResults.length >= 15 && !!displayResults[14].joker);
                try { CALC_IDS.forEach(function(id) { updateCalcStatus(id); }); } catch (e) {}
                
                // 이전회차·상태를 맨 앞에서 먼저 적용 (아래 예측/그래프 블록에서 예외 나도 화면에 현재 회차 반영)
                if (displayResults.length > 0) {
                    const latest = displayResults[0];
                    const fullGameID = latest.gameID != null && latest.gameID !== '' ? String(latest.gameID) : '--';
                    const prevRoundElement = document.getElementById('prev-round');
                    if (prevRoundElement) prevRoundElement.textContent = '이전회차: ' + fullGameID;
                }
                
                // 모든 카드의 색상 비교 결과 계산 (캐시 사용)
                // 각 카드는 고정된 상대 위치의 카드와 비교 (1번째↔16번째, 2번째↔17번째, ...)
                const colorMatchResults = [];
                
                // 그래프용: 전체 results에서 유효한 모든 위치(i vs i+15)에 대해 정/꺽 계산
                const graphColorMatchResults = [];
                
                // 전체 results 배열이 16개 이상이어야 비교 가능
                if (results.length < 16) {
                    for (let i = 0; i < displayResults.length; i++) {
                        colorMatchResults[i] = null;
                    }
                } else {
                    for (let i = 0; i < displayResults.length; i++) {
                        const currentResult = displayResults[i];
                        const currentGameID = currentResult?.gameID || '';
                        const compareIndex = i + 15;
                        
                        if (currentResult.joker) {
                            colorMatchResults[i] = null;
                            continue;
                        }
                        if (!currentGameID) {
                            colorMatchResults[i] = null;
                            continue;
                        }
                        if (results.length <= compareIndex) {
                            colorMatchResults[i] = null;
                            continue;
                        }
                        if (results[compareIndex]?.joker) {
                            colorMatchResults[i] = null;
                            continue;
                        }
                        
                        const compareGameID = results[compareIndex]?.gameID || '';
                        const cacheKey = `${currentGameID}_${compareGameID}`;
                        if (colorMatchCache[cacheKey] !== undefined) {
                            colorMatchResults[i] = colorMatchCache[cacheKey] === true;
                        } else {
                            const currentCard = parseCardValue(currentResult.result || '');
                            const compareCard = parseCardValue(results[compareIndex].result || '');
                            const matchResult = (currentCard.isRed === compareCard.isRed);
                            colorMatchCache[cacheKey] = matchResult;
                            colorMatchResults[i] = matchResult === true;
                        }
                    }
                    
                    // 그래프용: 0 ~ (results.length - 16) 전부 계산 (쭉 표시)
                    for (let i = 0; i <= results.length - 16; i++) {
                        const cur = results[i];
                        const compareIndex = i + 15;
                        if (cur?.joker || results[compareIndex]?.joker) {
                            graphColorMatchResults.push(null);
                            continue;
                        }
                        const currentGameID = cur?.gameID || '';
                        const compareGameID = results[compareIndex]?.gameID || '';
                        if (!currentGameID || !compareGameID) {
                            graphColorMatchResults.push(null);
                            continue;
                        }
                        const cacheKey = `${currentGameID}_${compareGameID}`;
                        if (colorMatchCache[cacheKey] !== undefined) {
                            graphColorMatchResults.push(colorMatchCache[cacheKey] === true);
                        } else {
                            const currentCard = parseCardValue(cur.result || '');
                            const compareCard = parseCardValue(results[compareIndex].result || '');
                            const matchResult = (currentCard.isRed === compareCard.isRed);
                            colorMatchCache[cacheKey] = matchResult;
                            graphColorMatchResults.push(matchResult === true);
                        }
                    }
                }
                
                // 오래된 캐시 정리 (allResults에 없는 카드만 제거 - 그래프용 데이터 유지)
                const currentGameIDs = new Set(allResults.map(r => String(r.gameID != null && r.gameID !== '' ? r.gameID : '')).filter(id => id !== ''));
                for (const key in colorMatchCache) {
                    const gameID = key.split('_')[0];
                    if (!currentGameIDs.has(gameID)) {
                        delete colorMatchCache[key];
                    }
                }
                
                // 헤더에 기준 색상 표시 (15번째 카드, 조커면 표시)
                if (displayResults.length >= 15) {
                    const refCard = displayResults[14];
                    const referenceColorElement = document.getElementById('reference-color');
                    if (referenceColorElement) {
                        if (refCard.joker) referenceColorElement.textContent = '기준: 조커 (배팅 보류)';
                        else {
                            const card15 = parseCardValue(refCard.result || '');
                            const colorText = card15.isRed ? '🔴 빨간색' : '⚫ 검은색';
                            referenceColorElement.textContent = `기준: ${colorText}`;
                        }
                    }
                } else {
                    // 15개 미만이면 기준 색상 표시 제거
                    const referenceColorElement = document.getElementById('reference-color');
                    if (referenceColorElement) {
                        referenceColorElement.textContent = '';
                    }
                }
                
                cardsDiv.innerHTML = '';
                
                if (displayResults.length === 0) {
                    statusElement.textContent = '경기 결과가 없습니다';
                    return;
                }
                
                // 카드용 정/꺽 (15개)
                const cardMatchValues = [];
                displayResults.forEach((result, index) => {
                    let matchResult = result.colorMatch;
                    if (matchResult === undefined || matchResult === null) {
                        matchResult = colorMatchResults[index];
                    }
                    cardMatchValues.push(matchResult);
                });
                
                // 그래프용 정/꺽 (전체: results.length - 15개, 쭉 표시)
                const graphValues = (results.length >= 16) ? graphColorMatchResults : [];
                
                displayResults.forEach((result, index) => {
                    try {
                        const matchResult = cardMatchValues[index];
                        const card = createCard(result, index, matchResult);
                        cardsDiv.appendChild(card);
                    } catch (error) {
                        console.error('카드 생성 오류:', error, result);
                    }
                });
                
                // 정/꺽 블록 그래프: 조커(null)는 무시하고 같은 타입끼리만 한 열에 쌓기
                const graphDiv = document.getElementById('jung-kkuk-graph');
                if (graphDiv) {
                    graphDiv.innerHTML = '';
                    const filtered = graphValues.filter(v => v === true || v === false);
                    const segments = [];
                    let current = null;
                    let count = 0;
                    filtered.forEach(v => {
                        if (v === current) {
                            count++;
                        } else {
                            if (current !== null) segments.push({ type: current, count: count });
                            current = v;
                            count = 1;
                        }
                    });
                    if (current !== null) segments.push({ type: current, count: count });
                    segments.forEach(seg => {
                        const col = document.createElement('div');
                        col.className = 'graph-column';
                        for (let i = 0; i < seg.count; i++) {
                            const block = document.createElement('div');
                            block.className = 'graph-block ' + (seg.type === true ? 'jung' : 'kkuk');
                            block.textContent = seg.type === true ? '정' : '꺽';
                            col.appendChild(block);
                        }
                        const numSpan = document.createElement('span');
                        numSpan.className = 'graph-column-num';
                        numSpan.textContent = seg.count;
                        numSpan.title = seg.count >= 4 ? '장줄' : (seg.count == 1 ? '퐁당' : '짧은줄');
                        col.appendChild(numSpan);
                        graphDiv.appendChild(col);
                    });
                }
                // 계산기 내 미니 그래프: 최근 25열, 작은 블록 (메인과 동일 데이터)
                [1, 2, 3].forEach(function(id) {
                    var miniEl = document.getElementById('calc-' + id + '-mini-graph');
                    if (miniEl && graphValues && Array.isArray(graphValues) && graphValues.length >= 2) {
                        var filtered = graphValues.filter(function(v) { return v === true || v === false; });
                        var segments = [];
                        var current = null, count = 0;
                        filtered.forEach(function(v) {
                            if (v === current) { count++; } else {
                                if (current !== null) segments.push({ type: current, count: count });
                                current = v; count = 1;
                            }
                        });
                        if (current !== null) segments.push({ type: current, count: count });
                        var take = Math.min(25, segments.length);
                        var show = segments.slice(0, take);
                        miniEl.innerHTML = '';
                        show.forEach(function(seg) {
                            var col = document.createElement('div');
                            col.className = 'calc-mini-col';
                            for (var i = 0; i < seg.count; i++) {
                                var block = document.createElement('div');
                                block.className = 'calc-mini-block ' + (seg.type === true ? 'jung' : 'kkuk');
                                block.textContent = seg.type === true ? '정' : '꺽';
                                col.appendChild(block);
                            }
                            var numSpan = document.createElement('span');
                            numSpan.className = 'calc-mini-col-num';
                            numSpan.textContent = seg.count;
                            col.appendChild(numSpan);
                            miniEl.appendChild(col);
                        });
                    } else if (miniEl) { miniEl.innerHTML = ''; }
                });
                
                // 전이 확률 표: 전체 / 최근 30회 (연속된 비-null 쌍만 사용)
                function calcTransitions(arr) {
                    let jj = 0, jk = 0, kj = 0, kk = 0;
                    for (let i = 0; i < arr.length - 1; i++) {
                        const a = arr[i], b = arr[i + 1];
                        if (a !== true && a !== false || b !== true && b !== false) continue;
                        if (a === true && b === true) jj++;
                        else if (a === true && b === false) jk++;
                        else if (a === false && b === true) kj++;
                        else kk++;
                    }
                    const jungDenom = jj + jk, kkukDenom = kk + kj;
                    return {
                        pJung: jungDenom > 0 ? (100 * jj / jungDenom).toFixed(1) : '-',
                        pKkuk: kkukDenom > 0 ? (100 * kk / kkukDenom).toFixed(1) : '-',
                        pJungToKkuk: jungDenom > 0 ? (100 * jk / jungDenom).toFixed(1) : '-',
                        pKkukToJung: kkukDenom > 0 ? (100 * kj / kkukDenom).toFixed(1) : '-',
                        jj, jk, kj, kk, jungDenom, kkukDenom
                    };
                }
                var blendData = { p15: null, p30: null, p100: null, newProb: null };
                const statsDiv = document.getElementById('graph-stats');
                if (statsDiv && graphValues && Array.isArray(graphValues) && graphValues.length >= 2) {
                    if (!Array.isArray(predictionHistory)) predictionHistory = [];
                    let symmetryLineData = null;
                    let symmetryWindowsUsed = [];
                    const full = calcTransitions(graphValues);
                    const recent30 = calcTransitions(graphValues.slice(0, 30));
                    const short15 = graphValues.length >= 15 ? calcTransitions(graphValues.slice(0, 15)) : null;
                    const fmt = (p, n, d) => d > 0 ? p + '% (' + n + '/' + d + ')' : '-';
                    // 예측 이력으로 15/30/100 구간 반영값 계산 (표 맨 아랫줄 + 확률 30% 반영용)
                    const validHistBlend = Array.isArray(predictionHistory) ? predictionHistory.filter(function(h) { return h && typeof h === 'object'; }) : [];
                    const outcomesNewestFirst = validHistBlend.filter(function(h) { return h.actual !== 'joker'; }).map(function(h) { return h.actual === '정'; }).reverse();
                    if (outcomesNewestFirst.length >= 2) {
                        function transCounts(arr) {
                            var jj = 0, jk = 0, kj = 0, kk = 0;
                            for (var i = 0; i < arr.length - 1; i++) {
                                var a = arr[i], b = arr[i + 1];
                                if (a === true && b === true) jj++; else if (a === true && b === false) jk++; else if (a === false && b === true) kj++; else if (a === false && b === false) kk++;
                            }
                            return { jj: jj, jk: jk, kj: kj, kk: kk, jungDenom: jj + jk, kkukDenom: kk + kj };
                        }
                        function probFromTrans(t, lastBool) {
                            if (lastBool === true && t.jungDenom > 0) { var sameP = t.jj / t.jungDenom, changeP = t.jk / t.jungDenom; return { sameP: sameP, changeP: changeP }; }
                            if (lastBool === false && t.kkukDenom > 0) { var sameP = t.kk / t.kkukDenom, changeP = t.kj / t.kkukDenom; return { sameP: sameP, changeP: changeP }; }
                            return { sameP: 0.5, changeP: 0.5 };
                        }
                        var lastBool = outcomesNewestFirst[0];
                        var s15 = outcomesNewestFirst.slice(0, Math.min(15, outcomesNewestFirst.length));
                        var s30 = outcomesNewestFirst.slice(0, Math.min(30, outcomesNewestFirst.length));
                        var s100 = outcomesNewestFirst.slice(0, Math.min(100, outcomesNewestFirst.length));
                        var t15 = transCounts(s15), t30 = transCounts(s30), t100 = transCounts(s100);
                        var r15 = probFromTrans(t15, lastBool), r30 = probFromTrans(t30, lastBool), r100 = probFromTrans(t100, lastBool);
                        blendData.p15 = s15.length >= 2 ? (r15.sameP >= r15.changeP ? r15.sameP : r15.changeP) * 100 : null;
                        blendData.p30 = s30.length >= 2 ? (r30.sameP >= r30.changeP ? r30.sameP : r30.changeP) * 100 : null;
                        blendData.p100 = s100.length >= 2 ? (r100.sameP >= r100.changeP ? r100.sameP : r100.changeP) * 100 : null;
                        var w15 = s15.length >= 2 ? 0.5 : 0, w30 = s30.length >= 2 ? 0.3 : 0, w100 = s100.length >= 2 ? 0.2 : 0;
                        var denom = w15 + w30 + w100;
                        if (denom > 0) blendData.newProb = (w15 * (blendData.p15 || 50) + w30 * (blendData.p30 || 50) + w100 * (blendData.p100 || 50)) / denom;
                    }
                    var rowBlend15 = blendData.p15 != null ? Number(blendData.p15).toFixed(1) + '%' : '-';
                    var rowBlend30 = blendData.p30 != null ? Number(blendData.p30).toFixed(1) + '%' : '-';
                    var rowBlend100 = blendData.p100 != null ? Number(blendData.p100).toFixed(1) + '%' : '-';
                    statsDiv.innerHTML = '<table><thead><tr><th></th><th>최근 15회</th><th>최근 30회</th><th>전체</th></tr></thead><tbody>' +
                        '<tr><td><span class="jung-next">정 ↑</span></td><td>' + (short15 ? fmt(short15.pJung, short15.jj, short15.jungDenom) : '-') + '</td><td>' + fmt(recent30.pJung, recent30.jj, recent30.jungDenom) + '</td><td>' + fmt(full.pJung, full.jj, full.jungDenom) + '</td></tr>' +
                        '<tr><td><span class="kkuk-next">꺽 ↑</span></td><td>' + (short15 ? fmt(short15.pKkuk, short15.kk, short15.kkukDenom) : '-') + '</td><td>' + fmt(recent30.pKkuk, recent30.kk, recent30.kkukDenom) + '</td><td>' + fmt(full.pKkuk, full.kk, full.kkukDenom) + '</td></tr>' +
                        '<tr><td><span class="jung-kkuk">← 꺽</span></td><td>' + (short15 ? fmt(short15.pJungToKkuk, short15.jk, short15.jungDenom) : '-') + '</td><td>' + fmt(recent30.pJungToKkuk, recent30.jk, recent30.jungDenom) + '</td><td>' + fmt(full.pJungToKkuk, full.jk, full.jungDenom) + '</td></tr>' +
                        '<tr><td><span class="kkuk-jung">← 정</span></td><td>' + (short15 ? fmt(short15.pKkukToJung, short15.kj, short15.kkukDenom) : '-') + '</td><td>' + fmt(recent30.pKkukToJung, recent30.kj, recent30.kkukDenom) + '</td><td>' + fmt(full.pKkukToJung, full.kj, full.kkukDenom) + '</td></tr>' +
                        '<tr><td><span style="color:#888">구간반영</span></td><td>' + rowBlend15 + '</td><td>' + rowBlend30 + '</td><td>' + rowBlend100 + '</td></tr>' +
                        '</tbody></table><p class="graph-stats-note">※ 단기(15회) vs 장기(30회) 비교로 흐름 전환 감지<br>· 아랫줄=구간반영(예측이력 15/30/100회, 30% 적용)<br>· % 높을수록 예측 픽(정/꺽)에 대한 확신↑</p>';
                    
                    // 회차: 비교·저장·표시 모두 전체 gameID(11416052 등) 사용. 끝 3자리만 쓰면 11423052/11424052가 둘 다 052로 겹침 → 충돌 방지를 위해 전체 표시
                    function fullRoundFromGameID(g) {
                        var s = String(g != null && g !== '' ? g : '0');
                        var n = parseInt(s, 10);
                        return isNaN(n) ? 0 : n;
                    }
                    function displayRound(r) { return r != null ? String(r) : '-'; }
                    const latestGameID = displayResults[0]?.gameID;
                    const currentRoundFull = fullRoundFromGameID(latestGameID);
                    const predictedRoundFull = currentRoundFull + 1;
                    try { window.__latestGameIDForCalc = latestGameID; } catch (e) {}
                    const is15Joker = displayResults.length >= 15 && !!displayResults[14].joker;  // 15번 카드 조커면 픽/배팅 보류
                    lastIs15Joker = is15Joker;  // 계산기 예측픽에 보류 반영
                    
                    // 직전 예측의 실제 결과 반영. 예측기 밑 표/합산승률은 무조건 예측픽만 사용 — 계산기(승률반픽 등)와 독립.
                    const currentRoundNum = Number(currentRoundFull);
                    const alreadyRecordedRound = predictionHistory.some(function(h) { return h && Number(h.round) === currentRoundNum; });
                    // predForRound: 계산기 루프에서 반픽/승률반픽 적용할 때 쓸 기준 (기존 로직 유지)
                    var predForRound = (predictionHistory && predictionHistory.find(function(p) { return p && Number(p.round) === currentRoundNum; })) || getRoundPrediction(currentRoundFull) || (lastPrediction && Number(lastPrediction.round) === currentRoundNum ? lastPrediction : null);
                    if (predForRound && predForRound.actual !== undefined) predForRound = { round: predForRound.round, value: predForRound.predicted, prob: predForRound.probability, color: predForRound.pickColor || predForRound.pick_color };
                    // 예측기표에 넣을 값은 서버·버퍼에서만 취함. predictionHistory.find는 배팅픽 오염 가능으로 사용 안 함.
                    var predForRecord = getRoundPrediction(currentRoundFull) || (lastPrediction && Number(lastPrediction.round) === currentRoundNum ? lastPrediction : null);
                    var lowWinRateForRecord = false;
                    var blended = 50, c15 = 0, c30 = 0, c100 = 0;
                    try {
                        var vh = Array.isArray(predictionHistory) ? predictionHistory.filter(function(h) { return h && typeof h === 'object'; }) : [];
                        var v15 = vh.slice(-15), v30 = vh.slice(-30), v100 = vh.slice(-100);
                        var hit15r = v15.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
                        var loss15 = v15.filter(function(h) { return h.actual !== 'joker' && h.predicted !== h.actual; }).length;
                        c15 = hit15r + loss15;
                        var r15 = c15 > 0 ? 100 * hit15r / c15 : 50;
                        var hit30r = v30.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
                        var loss30 = v30.filter(function(h) { return h.actual !== 'joker' && h.predicted !== h.actual; }).length;
                        c30 = hit30r + loss30;
                        var r30 = c30 > 0 ? 100 * hit30r / c30 : 50;
                        var hit100r = v100.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
                        var loss100 = v100.filter(function(h) { return h.actual !== 'joker' && h.predicted !== h.actual; }).length;
                        c100 = hit100r + loss100;
                        var r100 = c100 > 0 ? 100 * hit100r / c100 : 50;
                        blended = 0.65 * r15 + 0.25 * r30 + 0.10 * r100;
                        lowWinRateForRecord = (c15 > 0 || c30 > 0 || c100 > 0) && blended <= 50;
                    } catch (e) {}
                    var runLen = getCurrentResultRunLength(typeof predictionHistory !== 'undefined' && Array.isArray(predictionHistory) ? predictionHistory : []);
                    if (!alreadyRecordedRound && predForRound) {
                        const isActualJoker = displayResults.length > 0 && !!displayResults[0].joker;
                        if (isActualJoker) {
                            if (predForRecord) {
                                predictionHistory.push({ round: currentRoundFull, predicted: predForRecord.value, actual: 'joker', probability: predForRecord.prob != null ? predForRecord.prob : null, pickColor: predForRecord.color || null });
                            }
                            var betPredForServer = null, betColorForServer = null;
                            CALC_IDS.forEach(id => {
                                if (!calcState[id].running) return;
                                const firstBetJoker = calcState[id].first_bet_round || 0;
                                if (firstBetJoker > 0 && currentRoundNum < firstBetJoker) return;
                                var pred, betColor;
                                var saved = (calcState[id].lastBetPickForRound && Number(calcState[id].lastBetPickForRound.round) === currentRoundNum && (calcState[id].lastBetPickForRound.value === '정' || calcState[id].lastBetPickForRound.value === '꺽')) ? calcState[id].lastBetPickForRound : savedBetPickByRound[Number(currentRoundNum)];
                                if (saved && (saved.value === '정' || saved.value === '꺽')) {
                                    pred = saved.value;
                                    betColor = saved.isRed ? '빨강' : '검정';
                                } else {
                                    const rev = !!(calcState[id] && calcState[id].reverse);
                                    pred = rev ? (predForRound.value === '정' ? '꺽' : '정') : predForRound.value;
                                    const useWinRateRev = !!(calcState[id] && calcState[id].win_rate_reverse);
                                    var shapeWr = (typeof getShape50WinRate === 'function') ? getShape50WinRate() : null;
                                    var wrThrEl = document.getElementById('calc-' + id + '-win-rate-threshold');
                                    var wrThr = (wrThrEl && !isNaN(parseFloat(wrThrEl.value))) ? Math.max(0, Math.min(100, parseFloat(wrThrEl.value))) : 50;
                                    var streakSuppress = !!(calcState[id] && calcState[id].streak_suppress_reverse);
                                    var noRevByMain15 = (r15 == null || r15 < 53);
                                    var noRevByStreak5 = !(streakSuppress && runLen >= 5);
                                    if (useWinRateRev && shapeWr != null && shapeWr <= wrThr && noRevByMain15 && noRevByStreak5) pred = pred === '정' ? '꺽' : '정';
                                    var useLoseStreakRev = !!(calcState[id] && calcState[id].lose_streak_reverse);
                                    var loseStreakThrEl = document.getElementById('calc-' + id + '-lose-streak-reverse-threshold');
                                    var loseStreakThr = (loseStreakThrEl && !isNaN(parseFloat(loseStreakThrEl.value))) ? Math.max(0, Math.min(100, parseFloat(loseStreakThrEl.value))) : (calcState[id] != null && typeof calcState[id].lose_streak_reverse_threshold === 'number' ? calcState[id].lose_streak_reverse_threshold : 48);
                                    if (useLoseStreakRev && getLoseStreak(id) >= getLoseStreakMin(id) && typeof blended === 'number' && blended <= loseStreakThr && noRevByMain15 && noRevByStreak5) pred = pred === '정' ? '꺽' : '정';
                                    betColor = normalizePickColor(predForRound.color);
                                    if (rev) betColor = betColor === '빨강' ? '검정' : '빨강';
                                    if (useWinRateRev && shapeWr != null && shapeWr <= wrThr && noRevByMain15 && noRevByStreak5) betColor = betColor === '빨강' ? '검정' : '빨강';
                                    if (useLoseStreakRev && getLoseStreak(id) >= getLoseStreakMin(id) && typeof blended === 'number' && blended <= loseStreakThr && noRevByMain15 && noRevByStreak5) betColor = betColor === '빨강' ? '검정' : '빨강';
                                    var winRateDirRevEl = document.getElementById('calc-' + id + '-win-rate-direction-reverse');
                                    var useWinRateDirRev = !!(winRateDirRevEl && winRateDirRevEl.checked) || !!(calcState[id] && calcState[id].win_rate_direction_reverse);
                                    if (useWinRateDirRev && noRevByStreak5 && typeof getEffectiveWinRateDirectionZone === 'function') {
                                        var phForZone = (typeof predictionHistory !== 'undefined' && Array.isArray(predictionHistory)) ? predictionHistory : [];
                                        var zone = getEffectiveWinRateDirectionZone(phForZone, id, currentRoundNum);
                                        if (zone === 'high_falling') { pred = pred === '정' ? '꺽' : '정'; betColor = betColor === '빨강' ? '검정' : '빨강'; calcState[id].last_trend_direction = 'down'; }
                                        else if (zone === 'low_rising') { calcState[id].last_trend_direction = 'up'; }
                                        else if (zone === 'mid_flat' && calcState[id].last_trend_direction === 'down') { pred = pred === '정' ? '꺽' : '정'; betColor = betColor === '빨강' ? '검정' : '빨강'; }
                                    }
                                }
                                if (betPredForServer == null) { betPredForServer = pred; betColorForServer = betColor || null; }
                                var pendingIdx = calcState[id].history.findIndex(function(h) { return h && Number(h.round) === currentRoundNum && h.actual === 'pending'; });
                                if (pendingIdx >= 0) {
                                    var rowJ = calcState[id].history[pendingIdx];
                                    rowJ.actual = 'joker';
                                    if (saved && (saved.value === '정' || saved.value === '꺽')) { rowJ.predicted = saved.value; rowJ.pickColor = saved.isRed ? '빨강' : '검정'; }
                                    else if ((rowJ.predicted !== '정' && rowJ.predicted !== '꺽') || rowJ.pickColor == null || rowJ.pickColor === '') { rowJ.predicted = pred; rowJ.pickColor = betColor || null; }
                                    var isNoBetJ = !!(is15Joker || effectivePausedForRound(id) || (rowJ.no_bet && !isMartingaleLossStreak(id)));
                                    rowJ.no_bet = isNoBetJ;
                                    rowJ.betAmount = isNoBetJ ? 0 : (rowJ.betAmount != null ? rowJ.betAmount : undefined);
                                    if (rowJ.warningWinRate == null && typeof blended === 'number') rowJ.warningWinRate = blended;
                                    if (typeof getCalcRecent15WinRate === 'function') rowJ.rate15 = getCalcRecent15WinRate(id);
                                } else {
                                    var noBetJoker = !!(is15Joker || effectivePausedForRound(id));
                                    calcState[id].history.push({ predicted: pred, actual: 'joker', round: currentRoundFull, pickColor: betColor || null, betAmount: noBetJoker ? 0 : undefined, no_bet: noBetJoker, warningWinRate: typeof blended === 'number' ? blended : null });
                                }
                                calcState[id].history = dedupeCalcHistoryByRound(calcState[id].history);
                                var entryJ = calcState[id].history.find(function(h) { return h && Number(h.round) === currentRoundNum; });
                                if (entryJ && entryJ.actual && entryJ.actual !== 'pending' && typeof getCalcRecent15WinRate === 'function' && (entryJ.rate15 == null || entryJ.rate15 === undefined)) entryJ.rate15 = getCalcRecent15WinRate(id);
                                _lastCalcHistKey[id] = (calcState[id].history.length) + '-joker';
                            });
                            saveCalcStateToServer();
                            if (predForRecord) { savePredictionHistoryToServer(currentRoundFull, predForRecord.value, 'joker', predForRecord.prob, predForRecord.color || null); }
                        } else if (graphValues.length > 0 && (graphValues[0] === true || graphValues[0] === false)) {
                            const actual = graphValues[0] ? '정' : '꺽';
                            if (predForRecord) {
                                predictionHistory.push({ round: currentRoundFull, predicted: predForRecord.value, actual: actual, probability: predForRecord.prob != null ? predForRecord.prob : null, pickColor: predForRecord.color || null });
                            }
                            var betPredForServerActual = null, betColorForServerActual = null;
                            CALC_IDS.forEach(id => {
                                if (!calcState[id].running) return;
                                const firstBetActual = calcState[id].first_bet_round || 0;
                                if (firstBetActual > 0 && currentRoundNum < firstBetActual) return;
                                var pred, betColorActual;
                                var saved = (calcState[id].lastBetPickForRound && Number(calcState[id].lastBetPickForRound.round) === currentRoundNum && (calcState[id].lastBetPickForRound.value === '정' || calcState[id].lastBetPickForRound.value === '꺽')) ? calcState[id].lastBetPickForRound : savedBetPickByRound[Number(currentRoundNum)];
                                if (saved && (saved.value === '정' || saved.value === '꺽')) {
                                    pred = saved.value;
                                    betColorActual = saved.isRed ? '빨강' : '검정';
                                } else {
                                    const rev = !!(calcState[id] && calcState[id].reverse);
                                    pred = rev ? (predForRound.value === '정' ? '꺽' : '정') : predForRound.value;
                                    const useWinRateRevActual = !!(calcState[id] && calcState[id].win_rate_reverse);
                                    var shapeWrActual = (typeof getShape50WinRate === 'function') ? getShape50WinRate() : null;
                                    var wrThrElA = document.getElementById('calc-' + id + '-win-rate-threshold');
                                    var wrThrA = (wrThrElA && !isNaN(parseFloat(wrThrElA.value))) ? Math.max(0, Math.min(100, parseFloat(wrThrElA.value))) : 50;
                                    var streakSuppressA = !!(calcState[id] && calcState[id].streak_suppress_reverse);
                                    var noRevByMain15A = (r15 == null || r15 < 53);
                                    var noRevByStreak5A = !(streakSuppressA && runLen >= 5);
                                    if (useWinRateRevActual && shapeWrActual != null && shapeWrActual <= wrThrA && noRevByMain15A && noRevByStreak5A) pred = pred === '정' ? '꺽' : '정';
                                    var useLoseStreakRevActual = !!(calcState[id] && calcState[id].lose_streak_reverse);
                                    var loseStreakThrElActual = document.getElementById('calc-' + id + '-lose-streak-reverse-threshold');
                                    var loseStreakThrActual = (loseStreakThrElActual && !isNaN(parseFloat(loseStreakThrElActual.value))) ? Math.max(0, Math.min(100, parseFloat(loseStreakThrElActual.value))) : (calcState[id] != null && typeof calcState[id].lose_streak_reverse_threshold === 'number' ? calcState[id].lose_streak_reverse_threshold : 48);
                                    if (useLoseStreakRevActual && getLoseStreak(id) >= getLoseStreakMin(id) && typeof blended === 'number' && blended <= loseStreakThrActual && noRevByMain15A && noRevByStreak5A) pred = pred === '정' ? '꺽' : '정';
                                    betColorActual = normalizePickColor(predForRound.color);
                                    if (rev) betColorActual = betColorActual === '빨강' ? '검정' : '빨강';
                                    if (useWinRateRevActual && shapeWrActual != null && shapeWrActual <= wrThrA && noRevByMain15A && noRevByStreak5A) betColorActual = betColorActual === '빨강' ? '검정' : '빨강';
                                    if (useLoseStreakRevActual && getLoseStreak(id) >= getLoseStreakMin(id) && typeof blended === 'number' && blended <= loseStreakThrActual && noRevByMain15A && noRevByStreak5A) betColorActual = betColorActual === '빨강' ? '검정' : '빨강';
                                    var winRateDirRevElA = document.getElementById('calc-' + id + '-win-rate-direction-reverse');
                                    var useWinRateDirRevActual = !!(winRateDirRevElA && winRateDirRevElA.checked) || !!(calcState[id] && calcState[id].win_rate_direction_reverse);
                                    if (useWinRateDirRevActual && noRevByStreak5A && typeof getEffectiveWinRateDirectionZone === 'function') {
                                        var phForZoneA = (typeof predictionHistory !== 'undefined' && Array.isArray(predictionHistory)) ? predictionHistory : [];
                                        var zoneA = getEffectiveWinRateDirectionZone(phForZoneA, id, currentRoundNum);
                                        if (zoneA === 'high_falling') { pred = pred === '정' ? '꺽' : '정'; betColorActual = betColorActual === '빨강' ? '검정' : '빨강'; calcState[id].last_trend_direction = 'down'; }
                                        else if (zoneA === 'low_rising') { calcState[id].last_trend_direction = 'up'; }
                                        else if (zoneA === 'mid_flat' && calcState[id].last_trend_direction === 'down') { pred = pred === '정' ? '꺽' : '정'; betColorActual = betColorActual === '빨강' ? '검정' : '빨강'; }
                                    }
                                }
                                if (betPredForServerActual == null) { betPredForServerActual = pred; betColorForServerActual = betColorActual || null; }
                                var pendingIdxActual = calcState[id].history.findIndex(function(h) { return h && Number(h.round) === currentRoundNum && h.actual === 'pending'; });
                                if (pendingIdxActual >= 0) {
                                    var row = calcState[id].history[pendingIdxActual];
                                    row.actual = actual;
                                    // 배팅중 때 넣은 픽/색 유지(결과 시점 재계산이 반대색으로 바뀌어 승패·금액 꼬이는 것 방지)
                                    if (saved && (saved.value === '정' || saved.value === '꺽')) {
                                        row.predicted = saved.value;
                                        row.pickColor = saved.isRed ? '빨강' : '검정';
                                    } else if ((row.predicted !== '정' && row.predicted !== '꺽') || row.pickColor == null || row.pickColor === '') {
                                        row.predicted = pred;
                                        row.pickColor = betColorActual || null;
                                    }
                                    var isNoBet = !!(effectivePausedForRound(id) || (row.no_bet && !isMartingaleLossStreak(id)));
                                    row.no_bet = isNoBet;
                                    row.betAmount = isNoBet ? 0 : (row.betAmount != null ? row.betAmount : undefined);
                                    if (row.warningWinRate == null && typeof blended === 'number') row.warningWinRate = blended;
                                    if (typeof getCalcRecent15WinRate === 'function') row.rate15 = getCalcRecent15WinRate(id);
                                } else {
                                    var noBetPush = !!effectivePausedForRound(id);
                                    calcState[id].history.push({ predicted: pred, actual: actual, round: currentRoundFull, pickColor: betColorActual || null, betAmount: noBetPush ? 0 : undefined, no_bet: noBetPush, warningWinRate: typeof blended === 'number' ? blended : null });
                                }
                                calcState[id].history = dedupeCalcHistoryByRound(calcState[id].history);
                                var entryA = calcState[id].history.find(function(h) { return h && Number(h.round) === currentRoundNum; });
                                if (entryA && entryA.actual && entryA.actual !== 'pending' && typeof getCalcRecent15WinRate === 'function' && (entryA.rate15 == null || entryA.rate15 === undefined)) entryA.rate15 = getCalcRecent15WinRate(id);
                                _lastCalcHistKey[id] = (calcState[id].history.length) + '-' + currentRoundFull + '_' + actual;
                                if (pred === actual) checkPauseAfterWin(id);
                                updateCalcSummary(id);
                                updateCalcDetail(id);
                                var targetEnabledEl = document.getElementById('calc-' + id + '-target-enabled');
                                var targetAmountEl = document.getElementById('calc-' + id + '-target-amount');
                                var targetEnabled = !!(targetEnabledEl && targetEnabledEl.checked);
                                var targetAmount = Math.max(0, parseInt(targetAmountEl && targetAmountEl.value, 10) || 0);
                                if (targetEnabled && targetAmount > 0) {
                                    var res = getCalcResult(id);
                                    if (res.profit >= targetAmount) {
                                        calcState[id].running = false;
                                        calcState[id].timer_completed = true;
                                        updateCalcSummary(id);
                                        updateCalcStatus(id);
                                        saveCalcStateToServer();
                                        // 목표 달성 즉시 current_pick 픽 비움 — 서버 저장 전에 매크로가 픽 받아 배팅하는 것 방지
                                        postCurrentPickIfChanged(id, { pickColor: null, round: null, probability: null, suggested_amount: null });
                                    }
                                }
                            });
                            saveCalcStateToServer();
                            if (predForRecord) { savePredictionHistoryToServer(currentRoundFull, predForRecord.value, actual, predForRecord.prob, predForRecord.color || null); }
                        }
                        predictionHistory = predictionHistory.slice(-300);
                        savePredictionHistory();  // localStorage 백업
                    } else if (alreadyRecordedRound && predForRound) {
                        // 서버가 이미 prediction_history에 머지한 회차 → calc에만 반영 (한 회차 건너뛰기 방지). 기록 회차는 화면 기준 currentRoundFull로 통일
                        const isActualJoker2 = displayResults.length > 0 && !!displayResults[0].joker;
                        if (isActualJoker2) {
                            CALC_IDS.forEach(id => {
                                if (!calcState[id].running) return;
                                const firstBetJoker = calcState[id].first_bet_round || 0;
                                if (firstBetJoker > 0 && currentRoundNum < firstBetJoker) return;
                                var pred, betColor;
                                var saved = (calcState[id].lastBetPickForRound && Number(calcState[id].lastBetPickForRound.round) === currentRoundNum && (calcState[id].lastBetPickForRound.value === '정' || calcState[id].lastBetPickForRound.value === '꺽')) ? calcState[id].lastBetPickForRound : savedBetPickByRound[Number(currentRoundNum)];
                                if (saved && (saved.value === '정' || saved.value === '꺽')) {
                                    pred = saved.value;
                                    betColor = saved.isRed ? '빨강' : '검정';
                                } else {
                                    const rev = !!(calcState[id] && calcState[id].reverse);
                                    pred = rev ? (predForRound.value === '정' ? '꺽' : '정') : predForRound.value;
                                    const useWinRateRev = !!(calcState[id] && calcState[id].win_rate_reverse);
                                    var shapeWr2 = (typeof getShape50WinRate === 'function') ? getShape50WinRate() : null;
                                    var wrThrEl2 = document.getElementById('calc-' + id + '-win-rate-threshold');
                                    var wrThr2 = (wrThrEl2 && !isNaN(parseFloat(wrThrEl2.value))) ? Math.max(0, Math.min(100, parseFloat(wrThrEl2.value))) : 50;
                                    var streakSuppress2 = !!(calcState[id] && calcState[id].streak_suppress_reverse);
                                    var noRevByMain152 = (r15 == null || r15 < 53);
                                    var noRevByStreak52 = !(streakSuppress2 && runLen >= 5);
                                    if (useWinRateRev && shapeWr2 != null && shapeWr2 <= wrThr2 && noRevByMain152 && noRevByStreak52) pred = pred === '정' ? '꺽' : '정';
                                    var useLoseStreakRev2 = !!(calcState[id] && calcState[id].lose_streak_reverse);
                                    var loseStreakThrEl2 = document.getElementById('calc-' + id + '-lose-streak-reverse-threshold');
                                    var loseStreakThr2 = (loseStreakThrEl2 && !isNaN(parseFloat(loseStreakThrEl2.value))) ? Math.max(0, Math.min(100, parseFloat(loseStreakThrEl2.value))) : (calcState[id] != null && typeof calcState[id].lose_streak_reverse_threshold === 'number' ? calcState[id].lose_streak_reverse_threshold : 48);
                                    if (useLoseStreakRev2 && getLoseStreak(id) >= getLoseStreakMin(id) && typeof blended === 'number' && blended <= loseStreakThr2 && noRevByMain152 && noRevByStreak52) pred = pred === '정' ? '꺽' : '정';
                                    betColor = normalizePickColor(predForRound.color);
                                    if (rev) betColor = betColor === '빨강' ? '검정' : '빨강';
                                    if (useWinRateRev && shapeWr2 != null && shapeWr2 <= wrThr2 && noRevByMain152 && noRevByStreak52) betColor = betColor === '빨강' ? '검정' : '빨강';
                                    if (useLoseStreakRev2 && getLoseStreak(id) >= getLoseStreakMin(id) && typeof blended === 'number' && blended <= loseStreakThr2 && noRevByMain152 && noRevByStreak52) betColor = betColor === '빨강' ? '검정' : '빨강';
                                    var winRateDirRevEl2 = document.getElementById('calc-' + id + '-win-rate-direction-reverse');
                                    var useWinRateDirRev2 = !!(winRateDirRevEl2 && winRateDirRevEl2.checked) || !!(calcState[id] && calcState[id].win_rate_direction_reverse);
                                    if (useWinRateDirRev2 && noRevByStreak52 && typeof getEffectiveWinRateDirectionZone === 'function') {
                                        var phForZone2 = (typeof predictionHistory !== 'undefined' && Array.isArray(predictionHistory)) ? predictionHistory : [];
                                        var zone2 = getEffectiveWinRateDirectionZone(phForZone2, id, currentRoundNum);
                                        if (zone2 === 'high_falling') { pred = pred === '정' ? '꺽' : '정'; betColor = betColor === '빨강' ? '검정' : '빨강'; calcState[id].last_trend_direction = 'down'; }
                                        else if (zone2 === 'low_rising') { calcState[id].last_trend_direction = 'up'; }
                                        else if (zone2 === 'mid_flat' && calcState[id].last_trend_direction === 'down') { pred = pred === '정' ? '꺽' : '정'; betColor = betColor === '빨강' ? '검정' : '빨강'; }
                                    }
                                }
                                var pendingIdx2 = calcState[id].history.findIndex(function(h) { return h && Number(h.round) === currentRoundNum && h.actual === 'pending'; });
                                if (pendingIdx2 >= 0) {
                                    var rowJ2 = calcState[id].history[pendingIdx2];
                                    rowJ2.actual = 'joker';
                                    rowJ2.predicted = pred;
                                    rowJ2.pickColor = betColor || null;
                                    var isNoBetJ2 = !!(is15Joker || effectivePausedForRound(id) || (rowJ2.no_bet && !isMartingaleLossStreak(id)));
                                    rowJ2.no_bet = isNoBetJ2;
                                    rowJ2.betAmount = isNoBetJ2 ? 0 : (rowJ2.betAmount != null ? rowJ2.betAmount : undefined);
                                    if (rowJ2.warningWinRate == null && typeof blended === 'number') rowJ2.warningWinRate = blended;
                                    if (typeof getCalcRecent15WinRate === 'function') rowJ2.rate15 = getCalcRecent15WinRate(id);
                                } else if (!calcState[id].history.some(function(h) { return h && Number(h.round) === currentRoundNum; })) {
                                    var noBetJoker2 = !!(is15Joker || effectivePausedForRound(id));
                                    calcState[id].history.push({ predicted: pred, actual: 'joker', round: currentRoundFull, pickColor: betColor || null, betAmount: noBetJoker2 ? 0 : undefined, no_bet: noBetJoker2, warningWinRate: typeof blended === 'number' ? blended : null });
                                }
                                calcState[id].history = dedupeCalcHistoryByRound(calcState[id].history);
                                var entryJ2 = calcState[id].history.find(function(h) { return h && Number(h.round) === currentRoundNum; });
                                if (entryJ2 && entryJ2.actual && entryJ2.actual !== 'pending' && typeof getCalcRecent15WinRate === 'function' && (entryJ2.rate15 == null || entryJ2.rate15 === undefined)) entryJ2.rate15 = getCalcRecent15WinRate(id);
                                _lastCalcHistKey[id] = (calcState[id].history.length) + '-joker';
                                updateCalcSummary(id);
                                updateCalcDetail(id);
                            });
                        } else if (graphValues.length > 0 && (graphValues[0] === true || graphValues[0] === false)) {
                            const actual = graphValues[0] ? '정' : '꺽';
                            CALC_IDS.forEach(id => {
                                if (!calcState[id].running) return;
                                const firstBetActual = calcState[id].first_bet_round || 0;
                                if (firstBetActual > 0 && currentRoundNum < firstBetActual) return;
                                var pred, betColorActual;
                                var saved = (calcState[id].lastBetPickForRound && Number(calcState[id].lastBetPickForRound.round) === currentRoundNum && (calcState[id].lastBetPickForRound.value === '정' || calcState[id].lastBetPickForRound.value === '꺽')) ? calcState[id].lastBetPickForRound : savedBetPickByRound[Number(currentRoundNum)];
                                if (saved && (saved.value === '정' || saved.value === '꺽')) {
                                    pred = saved.value;
                                    betColorActual = saved.isRed ? '빨강' : '검정';
                                } else {
                                    const rev = !!(calcState[id] && calcState[id].reverse);
                                    pred = rev ? (predForRound.value === '정' ? '꺽' : '정') : predForRound.value;
                                    const useWinRateRevActual = !!(calcState[id] && calcState[id].win_rate_reverse);
                                    var shapeWr3 = (typeof getShape50WinRate === 'function') ? getShape50WinRate() : null;
                                    var wrThrEl3 = document.getElementById('calc-' + id + '-win-rate-threshold');
                                    var wrThr3 = (wrThrEl3 && !isNaN(parseFloat(wrThrEl3.value))) ? Math.max(0, Math.min(100, parseFloat(wrThrEl3.value))) : 50;
                                    var streakSuppress3 = !!(calcState[id] && calcState[id].streak_suppress_reverse);
                                    var noRevByMain153 = (r15 == null || r15 < 53);
                                    var noRevByStreak53 = !(streakSuppress3 && runLen >= 5);
                                    if (useWinRateRevActual && shapeWr3 != null && shapeWr3 <= wrThr3 && noRevByMain153 && noRevByStreak53) pred = pred === '정' ? '꺽' : '정';
                                    var useLoseStreakRev3 = !!(calcState[id] && calcState[id].lose_streak_reverse);
                                    var loseStreakThrEl3 = document.getElementById('calc-' + id + '-lose-streak-reverse-threshold');
                                    var loseStreakThr3 = (loseStreakThrEl3 && !isNaN(parseFloat(loseStreakThrEl3.value))) ? Math.max(0, Math.min(100, parseFloat(loseStreakThrEl3.value))) : (calcState[id] != null && typeof calcState[id].lose_streak_reverse_threshold === 'number' ? calcState[id].lose_streak_reverse_threshold : 48);
                                    if (useLoseStreakRev3 && getLoseStreak(id) >= getLoseStreakMin(id) && typeof blended === 'number' && blended <= loseStreakThr3 && noRevByMain153 && noRevByStreak53) pred = pred === '정' ? '꺽' : '정';
                                    betColorActual = normalizePickColor(predForRound.color);
                                    if (rev) betColorActual = betColorActual === '빨강' ? '검정' : '빨강';
                                    if (useWinRateRevActual && shapeWr3 != null && shapeWr3 <= wrThr3 && noRevByMain153 && noRevByStreak53) betColorActual = betColorActual === '빨강' ? '검정' : '빨강';
                                    if (useLoseStreakRev3 && getLoseStreak(id) >= getLoseStreakMin(id) && typeof blended === 'number' && blended <= loseStreakThr3 && noRevByMain153 && noRevByStreak53) betColorActual = betColorActual === '빨강' ? '검정' : '빨강';
                                    var winRateDirRevEl3 = document.getElementById('calc-' + id + '-win-rate-direction-reverse');
                                    var useWinRateDirRev3 = !!(winRateDirRevEl3 && winRateDirRevEl3.checked) || !!(calcState[id] && calcState[id].win_rate_direction_reverse);
                                    if (useWinRateDirRev3 && noRevByStreak53 && typeof getEffectiveWinRateDirectionZone === 'function') {
                                        var phForZone3 = (typeof predictionHistory !== 'undefined' && Array.isArray(predictionHistory)) ? predictionHistory : [];
                                        var zone3 = getEffectiveWinRateDirectionZone(phForZone3, id, currentRoundNum);
                                        if (zone3 === 'high_falling') { pred = pred === '정' ? '꺽' : '정'; betColorActual = betColorActual === '빨강' ? '검정' : '빨강'; calcState[id].last_trend_direction = 'down'; }
                                        else if (zone3 === 'low_rising') { calcState[id].last_trend_direction = 'up'; }
                                        else if (zone3 === 'mid_flat' && calcState[id].last_trend_direction === 'down') { pred = pred === '정' ? '꺽' : '정'; betColorActual = betColorActual === '빨강' ? '검정' : '빨강'; }
                                    }
                                }
                                var pendingIdx3 = calcState[id].history.findIndex(function(h) { return h && Number(h.round) === currentRoundNum && h.actual === 'pending'; });
                                if (pendingIdx3 >= 0) {
                                    var row3 = calcState[id].history[pendingIdx3];
                                    row3.actual = actual;
                                    if (saved && (saved.value === '정' || saved.value === '꺽')) { row3.predicted = saved.value; row3.pickColor = saved.isRed ? '빨강' : '검정'; }
                                    else if ((row3.predicted !== '정' && row3.predicted !== '꺽') || row3.pickColor == null || row3.pickColor === '') { row3.predicted = pred; row3.pickColor = betColorActual || null; }
                                    var isNoBet3 = !!(effectivePausedForRound(id) || (row3.no_bet && !isMartingaleLossStreak(id)));
                                    row3.no_bet = isNoBet3;
                                    row3.betAmount = isNoBet3 ? 0 : (row3.betAmount != null ? row3.betAmount : undefined);
                                    if (row3.warningWinRate == null && typeof blended === 'number') row3.warningWinRate = blended;
                                    if (typeof getCalcRecent15WinRate === 'function') row3.rate15 = getCalcRecent15WinRate(id);
                                } else if (!calcState[id].history.some(function(h) { return h && Number(h.round) === currentRoundNum; })) {
                                    var noBetPush3 = !!effectivePausedForRound(id);
                                    calcState[id].history.push({ predicted: pred, actual: actual, round: currentRoundFull, pickColor: betColorActual || null, betAmount: noBetPush3 ? 0 : undefined, no_bet: noBetPush3, warningWinRate: typeof blended === 'number' ? blended : null });
                                }
                                calcState[id].history = dedupeCalcHistoryByRound(calcState[id].history);
                                var entryA3 = calcState[id].history.find(function(h) { return h && Number(h.round) === currentRoundNum; });
                                if (entryA3 && entryA3.actual && entryA3.actual !== 'pending' && typeof getCalcRecent15WinRate === 'function' && (entryA3.rate15 == null || entryA3.rate15 === undefined)) entryA3.rate15 = getCalcRecent15WinRate(id);
                                _lastCalcHistKey[id] = (calcState[id].history.length) + '-' + currentRoundFull + '_' + actual;
                                if (pred === actual) checkPauseAfterWin(id);
                                updateCalcSummary(id);
                                updateCalcDetail(id);
                            });
                        }
                        saveCalcStateToServer();
                    }
                    
                    // 최근 15회 정/꺽 흐름으로 퐁당·줄 계산 (승패 아님)
                    function pongLinePct(arr) {
                        const v = arr.filter(x => x === true || x === false);
                        if (v.length < 2) return { pongPct: 50, linePct: 50 };
                        let alt = 0, same = 0;
                        for (let i = 0; i < v.length - 1; i++) {
                            if (v[i] !== v[i + 1]) alt++; else same++;
                        }
                        const tot = alt + same;
                        return { pongPct: tot ? parseFloat((100 * alt / tot).toFixed(1)) : 50, linePct: tot ? parseFloat((100 * same / tot).toFixed(1)) : 50 };
                    }
                    const last15JungKkuk = graphValues.slice(0, 15).filter(v => v === true || v === false);
                    let pongPct = 50, linePct = 50;
                    if (last15JungKkuk.length >= 2) {
                        const pl = pongLinePct(graphValues.slice(0, 15));
                        pongPct = pl.pongPct; linePct = pl.linePct;
                    }
                    const flowStr = '최근 15회(정꺽): <span class="pong">퐁당 ' + pongPct + '%</span> / <span class="line">줄 ' + linePct + '%</span>';
                    const last = graphValues[0];  // 직전 정/꺽 (아래 단기vs장기·전이 확률에서 사용)
                    
                    // 줄 패턴 (최근 30회 기준): 덩어리/띄엄띄엄/두줄한개 지수 수치화 → 예측 픽에 반영
                    function getLinePongRuns(arr) {
                        const pairs = [];
                        for (let i = 0; i < arr.length - 1; i++) {
                            const a = arr[i], b = arr[i + 1];
                            if (a !== true && a !== false || b !== true && b !== false) continue;
                            pairs.push(a === b ? 1 : 0);  // 1=줄, 0=퐁당
                        }
                        const lineRuns = [], pongRuns = [];
                        let idx = 0;
                        while (idx < pairs.length) {
                            if (pairs[idx] === 1) {
                                let c = 0;
                                while (idx < pairs.length && pairs[idx] === 1) { c++; idx++; }
                                lineRuns.push(c);
                            } else {
                                let c = 0;
                                while (idx < pairs.length && pairs[idx] === 0) { c++; idx++; }
                                pongRuns.push(c);
                            }
                        }
                        return { lineRuns, pongRuns };
                    }
                    const useForPattern = graphValues.slice(0, 30);  // 최근 30회 = 30개 값 → 29쌍
                    const { lineRuns, pongRuns } = getLinePongRuns(useForPattern);
                    function detectVPattern(lineRuns, pongRuns, head) {
                        if (!lineRuns || !lineRuns.length || !pongRuns || !pongRuns.length) return false;
                        const firstIsLine = !head || head.length < 2 ? true : (head[0] === true || head[0] === false) && (head[0] === head[1]);
                        if (firstIsLine) {
                            return lineRuns[0] >= 4 && pongRuns[0] >= 1 && pongRuns[0] <= 2 && lineRuns.length >= 2 && lineRuns[1] <= 2;
                        }
                        return lineRuns.length >= 2 && pongRuns.length >= 2 && lineRuns[0] >= 4 && pongRuns[1] >= 1 && pongRuns[1] <= 2 && lineRuns[1] <= 2;
                    }
                    const totalLineRuns = lineRuns.length;
                    const totalPongRuns = pongRuns.length;
                    const lineTwoPlus = totalLineRuns > 0 ? lineRuns.filter(l => l >= 2).length : 0;
                    const lineOne = totalLineRuns > 0 ? lineRuns.filter(l => l === 1).length : 0;
                    const lineTwo = totalLineRuns > 0 ? lineRuns.filter(l => l === 2).length : 0;
                    const pongOne = totalPongRuns > 0 ? pongRuns.filter(p => p === 1).length : 0;
                    // 지수 0~1: 덩어리(유지 가산), 띄엄띄엄(바뀜 가산), 두줄한개(유지 소폭 가산)
                    const chunkIdx = totalLineRuns > 0 ? lineTwoPlus / totalLineRuns : 0;
                    const scatterIdx = (totalLineRuns > 0 && totalPongRuns > 0) ? (lineOne / totalLineRuns) * (pongOne / totalPongRuns) : 0;
                    const twoOneIdx = (totalLineRuns > 0 && totalPongRuns > 0) ? (lineTwo / totalLineRuns) * (pongOne / totalPongRuns) : 0;
                    let linePatternStr = '';
                    if (totalLineRuns >= 1 || totalPongRuns >= 1) {
                        if (totalLineRuns >= 2 && chunkIdx >= 0.5) {
                            linePatternStr = '줄 패턴(30회): <span class="line">덩어리</span> 지수 ' + (chunkIdx * 100).toFixed(0) + '%';
                        } else if (totalLineRuns >= 2 && lineOne / totalLineRuns >= 0.7 && totalPongRuns >= 1 && pongOne / totalPongRuns >= 0.7) {
                            linePatternStr = '줄 패턴(30회): <span class="pong">띄엄띄엄</span> 지수 ' + (scatterIdx * 100).toFixed(0) + '%';
                        } else if (totalLineRuns >= 2 && lineTwo >= Math.ceil(totalLineRuns / 2) && totalPongRuns >= 1 && pongOne / totalPongRuns >= 0.6) {
                            linePatternStr = '줄 패턴(30회): <span class="line">두줄한개</span> 지수 ' + (twoOneIdx * 100).toFixed(0) + '%';
                        } else {
                            linePatternStr = '줄 패턴(30회): 혼합 덩' + (chunkIdx * 100).toFixed(0) + '% 띄' + (scatterIdx * 100).toFixed(0) + '% 2-1' + (twoOneIdx * 100).toFixed(0) + '%';
                        }
                    }
                    
                    // 이전 15회 퐁당% (흐름 전환 감지용)
                    let pongPrev15 = 50;
                    if (graphValues.length >= 30) {
                        const plPrev = pongLinePct(graphValues.slice(15, 30));
                        pongPrev15 = plPrev.pongPct;
                    }
                    // 단기(15회) vs 장기(30회) 유지 확률 비교: 15~20%p 이상 차이면 "줄이 강해졌다"
                    let lineStrongByTransition = false, pongStrongByTransition = false;
                    if (short15) {
                        const longSamePct = last === true
                            ? (recent30.jungDenom > 0 ? 100 * recent30.jj / recent30.jungDenom : 50)
                            : (recent30.kkukDenom > 0 ? 100 * recent30.kk / recent30.kkukDenom : 50);
                        const shortSamePct = last === true
                            ? (short15.jungDenom > 0 ? 100 * short15.jj / short15.jungDenom : 50)
                            : (short15.kkukDenom > 0 ? 100 * short15.kk / short15.kkukDenom : 50);
                        if (shortSamePct - longSamePct >= 15) lineStrongByTransition = true;
                        if (longSamePct - shortSamePct >= 15) pongStrongByTransition = true;
                    }
                    // 퐁당% 추이: 이전 15회 대비 최근 15회 퐁당이 크게 떨어지면 줄 강함, 크게 올라가면 퐁당 강함
                    const lineStrongByPong = (pongPrev15 - pongPct >= 20);
                    const pongStrongByPong = (graphValues.length >= 30 && pongPct - pongPrev15 >= 20);
                    const lineStrong = lineStrongByTransition || lineStrongByPong;
                    const pongStrong = pongStrongByTransition || pongStrongByPong;
                    
                    // 연패 후 연승 2~3회: "확률 급상승" 구간 (방향 불명 → 보수적 배팅 권장)
                    let surgeUnknown = false;
                    if (predictionHistory.length >= 5) {
                        const revSurge = predictionHistory.slice().reverse().filter(function(h) { return h && typeof h === 'object'; });
                        let i = 0, winRun = 0, loseRun = 0;
                        while (i < revSurge.length && revSurge[i] && (revSurge[i].predicted === revSurge[i].actual ? '승' : '패') === '승') { winRun++; i++; }
                        while (i < revSurge.length && revSurge[i] && (revSurge[i].predicted === revSurge[i].actual ? '승' : '패') === '패') { loseRun++; i++; }
                        if (winRun >= 2 && loseRun >= 3) surgeUnknown = true;
                    }
                    
                    // 흐름 상태 및 배팅 전환 안내
                    let flowState = ''; let flowAdvice = '';
                    if (lineStrong) {
                        flowState = 'line_strong';
                        flowAdvice = '줄 강함 → 유지 예측 비중↑, 동일금/마틴 줄이기 권장';
                    } else if (pongStrong) {
                        flowState = 'pong_strong';
                        flowAdvice = '퐁당 강함 → 바뀜 예측 비중↑, 기존 전략 유지';
                    } else if (surgeUnknown) {
                        flowState = 'surge_unknown';
                        flowAdvice = '확률 급상승 구간(방향 불명) → 보수적 배팅 권장';
                    }
                    
                    // 15·20·30열 각각 계산 후 가중 평균 (서버와 동일). 예측픽 보정 + 아래 표에 사용.
                    try {
                        function getRunLengths(a) {
                            var r = [], cur = null, c = 0, i;
                            for (i = 0; i < a.length; i++) {
                                if (a[i] === cur) c++;
                                else { if (cur !== null) r.push(c); cur = a[i]; c = 1; }
                            }
                            if (cur !== null) r.push(c);
                            return r;
                        }
                        function symmetryForN(gv, n) {
                            var arr = (gv && gv.filter(function(v) { return v === true || v === false; }).slice(0, n)) || [];
                            if (arr.length < n) return null;
                            var half = Math.floor(n / 2), pairCount = half;
                            var left = arr.slice(0, half), right = arr.slice(half, n);
                            var symCount = 0;
                            for (var si = 0; si < pairCount; si++) { if (arr[si] === arr[n - 1 - si]) symCount++; }
                            var leftRuns = getRunLengths(left), rightRuns = getRunLengths(right);
                            var avgL = leftRuns.length ? leftRuns.reduce(function(s, x) { return s + x; }, 0) / leftRuns.length : 0;
                            var avgR = rightRuns.length ? rightRuns.reduce(function(s, x) { return s + x; }, 0) / rightRuns.length : 0;
                            var lineDiff = Math.abs(avgL - avgR);
                            var maxLeftRun = (leftRuns && leftRuns.length) ? Math.max.apply(null, leftRuns) : 0;
                            var recentRunLen = 1;
                            for (var ri = 1; ri < arr.length; ri++) { if (arr[ri] === arr[0]) recentRunLen++; else break; }
                            return {
                                symmetryPct: pairCount ? symCount / pairCount * 100 : 0,
                                avgLeft: avgL, avgRight: avgR,
                                lineSimilarityPct: Math.max(0, 100 - Math.min(100, lineDiff * 25)),
                                leftLineCount: leftRuns.length,
                                rightLineCount: rightRuns.length,
                                maxLeftRunLength: maxLeftRun,
                                recentRunLength: recentRunLen
                            };
                        }
                        var SYM_WINDOWS = [15, 20, 30], SYM_WEIGHTS = [0.2, 0.5, 0.3];
                        var perN = {};
                        for (var wi = 0; wi < SYM_WINDOWS.length; wi++) {
                            var d = symmetryForN(graphValues, SYM_WINDOWS[wi]);
                            if (d) perN[SYM_WINDOWS[wi]] = d;
                        }
                        if (Object.keys(perN).length > 0) {
                            var totalW = 0;
                            for (var wi = 0; wi < SYM_WINDOWS.length; wi++) {
                                if (perN[SYM_WINDOWS[wi]]) totalW += SYM_WEIGHTS[wi];
                            }
                            if (totalW > 0) {
                                var blend = { symmetryPct: 0, avgLeft: 0, avgRight: 0, lineSimilarityPct: 0, leftLineCount: 0, rightLineCount: 0, maxLeftRunLength: 0, recentRunLength: 0 };
                                for (var wi = 0; wi < SYM_WINDOWS.length; wi++) {
                                    var w = SYM_WINDOWS[wi];
                                    if (!perN[w]) continue;
                                    var frac = SYM_WEIGHTS[wi] / totalW;
                                    blend.symmetryPct += frac * perN[w].symmetryPct;
                                    blend.avgLeft += frac * perN[w].avgLeft;
                                    blend.avgRight += frac * perN[w].avgRight;
                                    blend.lineSimilarityPct += frac * perN[w].lineSimilarityPct;
                                    blend.leftLineCount += frac * perN[w].leftLineCount;
                                    blend.rightLineCount += frac * perN[w].rightLineCount;
                                    blend.maxLeftRunLength += frac * perN[w].maxLeftRunLength;
                                    blend.recentRunLength += frac * perN[w].recentRunLength;
                                }
                                blend.leftLineCount = Math.round(blend.leftLineCount);
                                blend.rightLineCount = Math.round(blend.rightLineCount);
                                blend.maxLeftRunLength = Math.round(blend.maxLeftRunLength);
                                blend.recentRunLength = Math.round(blend.recentRunLength);
                                symmetryLineData = blend;
                                symmetryWindowsUsed = Object.keys(perN).map(Number).sort(function(a, b) { return a - b; });
                            }
                        }
                    } catch (symErr) { symmetryLineData = null; symmetryWindowsUsed = []; console.warn('15·20·30열 symmetry/line calc:', symErr); }
                    var symmetryBoostNotice = false;  // 15·20·30열 보정 반영 시 경고문구용
                    var newSegmentNotice = false;    // 새 구간 구성 중 경고문구용
                    
                    // 예측픽 합산 공식: (유지확률×줄가중치) vs (바뀜확률×퐁당가중치) → 정규화 후 큰 쪽이 예측.
                    // 가중치: ①최근15회 줄/퐁당% ②흐름전환 ±0.25 ③15·20·30열 가중평균(줄개수·대칭도) ④30회패턴 → 합산 후 정규화.
                    var SYM_LINE_PONG_BOOST = 0.15;   // 20열 줄개수: 적으면 lineW, 많으면 pongW에 더하는 값 (0~0.2 권장)
                    var SYM_SAME_BOOST = 0.05;        // 20열 대칭도>=70%일 때 lineW에 더하는 값
                    var SYM_LOW_MUL = 0.95;           // 20열 대칭도<=30%일 때 보수적: lineW,pongW 둘 다 곱하는 값
                    let predict = '-', predProb = 0, colorToPick = '-', colorClass = 'black';
                    if (!is15Joker) {
                        let Pjung = 0.5, Pkkuk = 0.5;
                        if (last === true && recent30.jungDenom > 0) {
                            Pjung = recent30.jj / recent30.jungDenom;
                            Pkkuk = recent30.jk / recent30.jungDenom;
                        } else if (last === false && recent30.kkukDenom > 0) {
                            Pjung = recent30.kj / recent30.kkukDenom;
                            Pkkuk = recent30.kk / recent30.kkukDenom;
                        }
                        const probSame = last === true ? Pjung : Pkkuk;
                        const probChange = last === true ? Pkkuk : Pjung;
                        let lineW = linePct / 100, pongW = pongPct / 100;
                        if (flowState === 'line_strong') { lineW = Math.min(1, lineW + 0.25); pongW = Math.max(0, 1 - lineW); }
                        else if (flowState === 'pong_strong') { pongW = Math.min(1, pongW + 0.25); lineW = Math.max(0, 1 - pongW); }
                        if (symmetryLineData) {
                            var lc = symmetryLineData.leftLineCount;
                            var rc = symmetryLineData.rightLineCount;
                            var sp = symmetryLineData.symmetryPct;
                            var prevL = (prevSymmetryCounts && typeof prevSymmetryCounts.left !== 'undefined') ? prevSymmetryCounts.left : null;
                            var prevR = (prevSymmetryCounts && typeof prevSymmetryCounts.right !== 'undefined') ? prevSymmetryCounts.right : null;
                            // 우측 줄 없음(rc>=5) + 좌측 줄 생김(lc<=3) = 새 구간 시작. 오른쪽 쫒지 말고 왼쪽 추세(줄 유지) 반영.
                            var isNewSegment = (rc >= 5 && lc <= 3);
                            var isNewSegmentEarly = (prevR >= 5 && (prevL === null || prevL >= 4) && lc <= 3);
                            if (isNewSegment || isNewSegmentEarly) {
                                lineW = Math.min(1, lineW + 0.22);
                                pongW = Math.max(0, 1 - lineW);
                                newSegmentNotice = true;
                            } else if (sp >= 70 && rc <= 3) {
                                lineW = Math.min(1, lineW + 0.28);
                                pongW = Math.max(0, 1 - lineW);
                                symmetryBoostNotice = true;
                            } else {
                                if (lc <= 3) { lineW = Math.min(1, lineW + SYM_LINE_PONG_BOOST); pongW = Math.max(0, 1 - lineW); symmetryBoostNotice = true; }
                                else if (lc >= 5) {
                                    var maxRun = (symmetryLineData && typeof symmetryLineData.maxLeftRunLength === 'number') ? symmetryLineData.maxLeftRunLength : 4;
                                    var recentRun = (symmetryLineData && typeof symmetryLineData.recentRunLength === 'number') ? symmetryLineData.recentRunLength : 0;
                                    var calmOrRunStart = (maxRun <= 3) || (recentRun >= 2);
                                    var pongBoost = calmOrRunStart ? 0.06 : SYM_LINE_PONG_BOOST;
                                    pongW = Math.min(1, pongW + pongBoost);
                                    lineW = Math.max(0, 1 - pongW);
                                    symmetryBoostNotice = true;
                                }
                                if (sp >= 70) { lineW = Math.min(1, lineW + SYM_SAME_BOOST); symmetryBoostNotice = true; }
                                else if (sp <= 30) { lineW *= SYM_LOW_MUL; pongW *= SYM_LOW_MUL; }
                            }
                            if (prevSymmetryCounts && typeof prevSymmetryCounts === 'object') {
                                prevSymmetryCounts.left = lc;
                                prevSymmetryCounts.right = rc;
                            }
                        }
                        lineW += chunkIdx * 0.2 + twoOneIdx * 0.1;
                        pongW += scatterIdx * 0.2;
                        if (detectVPattern(lineRuns, pongRuns, useForPattern.slice(0, 2))) {
                            pongW += 0.12;
                            lineW = Math.max(0, lineW - 0.06);
                        }
                        const totalW = lineW + pongW;
                        if (totalW > 0) { lineW = lineW / totalW; pongW = pongW / totalW; }
                        const adjSame = probSame * lineW;
                        const adjChange = probChange * pongW;
                        const sum = adjSame + adjChange || 1;
                        const adjSameN = adjSame / sum;
                        const adjChangeN = adjChange / sum;
                        predict = adjSameN >= adjChangeN ? (last === true ? '정' : '꺽') : (last === true ? '꺽' : '정');
                        predProb = (predict === (last === true ? '정' : '꺽') ? adjSameN : adjChangeN) * 100;
                        const card15 = displayResults.length >= 15 ? parseCardValue(displayResults[14].result || '') : null;
                        const is15Red = card15 ? card15.isRed : false;
                        colorToPick = predict === '정' ? (is15Red ? '빨강' : '검정') : (is15Red ? '검정' : '빨강');
                        // 한 출처: lastPrediction은 서버(loadResults 응답)에서만 설정. 클라이언트 계산으로 덮어쓰지 않음 (깜빡임 방지).
                        colorClass = colorToPick === '빨강' ? 'red' : 'black';
                    }
                    // 표시용 픽/색은 서버 출처(lastPrediction)만 사용. 없으면 보류.
                    if (lastPrediction && (lastPrediction.value === '정' || lastPrediction.value === '꺽')) {
                        predict = lastPrediction.value;
                        predProb = (lastPrediction.prob != null && !isNaN(lastPrediction.prob)) ? lastPrediction.prob : predProb;
                        var serverColor = normalizePickColor(lastPrediction.color);
                        colorToPick = (serverColor === '빨강' || serverColor === '검정') ? serverColor : (lastPrediction.value === '정' ? '빨강' : '검정');
                        colorClass = colorToPick === '빨강' ? 'red' : 'black';
                    } else {
                        predict = '보류';
                        colorToPick = '-';
                        colorClass = 'black';
                        predProb = 0;
                    }
                    
                    // 연승/연패: 표 형식. 최신 회차가 가장 왼쪽 (reverse). 무효 항목 제외해 먹통 방지
                    const rev = predictionHistory.slice(-30).slice().reverse().filter(function(h) { return h && typeof h === 'object'; });
                    let streakCount = 0;
                    let streakType = '';
                    for (let i = predictionHistory.length - 1; i >= 0; i--) {
                        const p = predictionHistory[i];
                        if (!p || typeof p !== 'object') break;
                        if (p.actual === 'joker') break;
                        const s = p.predicted === p.actual ? '승' : '패';
                        if (i === predictionHistory.length - 1) { streakType = s; streakCount = 1; }
                        else if (s === streakType) streakCount++;
                        else break;
                    }
                    const streakNow = streakCount > 0 ? '현재 ' + streakCount + '연' + streakType : '';
                    // 최근 100회 기준: 현재 연승/연패, 최대 연승, 최대 연패 (가독성)
                    var currStreak100 = 0, currStreakType100 = '', maxWin100 = 0, maxLose100 = 0;
                    (function() {
                        var v100 = predictionHistory.slice(-100).filter(function(h) { return h && typeof h === 'object'; });
                        var i, run = 0, runType = '';
                        for (i = v100.length - 1; i >= 0; i--) {
                            var h = v100[i];
                            if (h.actual === 'joker') break;
                            var s = h.predicted === h.actual ? '승' : '패';
                            if (i === v100.length - 1) { currStreakType100 = s; currStreak100 = 1; }
                            else if (s === currStreakType100) currStreak100++;
                            else break;
                        }
                        for (i = 0; i < v100.length; i++) {
                            var h = v100[i];
                            if (h.actual === 'joker') { run = 0; runType = ''; continue; }
                            var s = h.predicted === h.actual ? '승' : '패';
                            if (s === runType) run++;
                            else { run = 1; runType = s; }
                            if (runType === '승') maxWin100 = Math.max(maxWin100, run);
                            else maxLose100 = Math.max(maxLose100, run);
                        }
                    })();
                    const streakLine100 = '현재 ' + (currStreak100 > 0 ? currStreak100 + '연' + currStreakType100 : '-') + ' | 최대 연승 ' + (maxWin100 || '-') + ' | 최대 연패 ' + (maxLose100 || '-');
                    
                    // [예측픽 전용] 메인 예측기(위) + 예측기표(아래). 계산기(calcState)와 독립 — 반드시 predictionHistory(예측픽)만 사용.
                    const resultBarContainer = document.getElementById('prediction-result-bar');
                    const pickContainer = document.getElementById('prediction-pick-container');  // 메인 예측기: 배팅중 RED, 정/꺽, 예측 확률
                    const predDiv = document.getElementById('prediction-box');               // 예측기표: 실제 경고 합산승률, 최근 50회, 회차별 표
                    const validHist = predictionHistory.filter(function(h) { return h && typeof h === 'object'; });
                    const hit = validHist.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
                    const losses = validHist.filter(function(h) { return h.actual !== 'joker' && h.predicted !== h.actual; }).length;
                    const jokerCount = validHist.filter(function(h) { return h.actual === 'joker'; }).length;
                    const total = validHist.length;
                    const countForPct = hit + losses;
                    const hitPctNum = countForPct > 0 ? 100 * hit / countForPct : 0;
                    const hitPct = countForPct > 0 ? hitPctNum.toFixed(1) : '-';
                    // 승률 낮음·배팅 주의: 15회 65% + 30회 25% + 100회 10%
                    const validHist15 = validHist.slice(-15);
                    const validHist30 = validHist.slice(-30);
                    const validHist100 = validHist.slice(-100);
                    const hit15 = validHist15.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
                    const losses15 = validHist15.filter(function(h) { return h.actual !== 'joker' && h.predicted !== h.actual; }).length;
                    const count15 = hit15 + losses15;
                    const hit30 = validHist30.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
                    const losses30 = validHist30.filter(function(h) { return h.actual !== 'joker' && h.predicted !== h.actual; }).length;
                    const count30 = hit30 + losses30;
                    const hit100 = validHist100.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
                    const losses100 = validHist100.filter(function(h) { return h.actual !== 'joker' && h.predicted !== h.actual; }).length;
                    const count100 = hit100 + losses100;
                    const rate15 = count15 > 0 ? 100 * hit15 / count15 : 50;
                    const hitPctNum30 = count30 > 0 ? 100 * hit30 / count30 : 50;
                    const rate100 = count100 > 0 ? 100 * hit100 / count100 : 50;
                    const blendedWinRate = 0.65 * rate15 + 0.25 * hitPctNum30 + 0.10 * rate100;
                    const lowWinRate = (count15 > 0 || count30 > 0 || count100 > 0) && blendedWinRate <= 50;
                    // 표시용: 최근 50회 결과 (승/패/조커/합산승률)
                    const validHist50 = validHist.slice(-50);
                    const hit50 = validHist50.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
                    const losses50 = validHist50.filter(function(h) { return h.actual !== 'joker' && h.predicted !== h.actual; }).length;
                    const joker50 = validHist50.filter(function(h) { return h.actual === 'joker'; }).length;
                    const count50 = hit50 + losses50;
                    const rate50 = count50 > 0 ? 100 * hit50 / count50 : 0;
                    const rate50Str = count50 > 0 ? rate50.toFixed(1) : '-';
                    // 승률 방향: 100회 승률 기준 (고점/저점/오름·내림 지표용) — 위에서 이미 validHist100/count100/rate100 계산됨
                    if (count100 >= 100 && validHist.length > 0) {
                        var _lastEntry = validHist[validHist.length - 1];
                        var _lastRound = _lastEntry && _lastEntry.round;
                        if (_lastRound != null && (winRate50History.length === 0 || Number(winRate50History[winRate50History.length - 1].round) !== Number(_lastRound))) {
                            winRate50History.push({ round: _lastRound, rate50: rate100 });
                            if (winRate50History.length > 300) winRate50History.shift();
                            if (document.getElementById('panel-win-rate-direction') && document.getElementById('panel-win-rate-direction').classList.contains('active') && typeof renderWinRateDirectionPanel === 'function') renderWinRateDirectionPanel();
                        }
                    }
                    // 확률 구간별 승률 (joker 제외, probability 있는 것만)
                    const nonJokerWithProb = validHist.filter(function(h) { return h && h.actual !== 'joker' && h.probability != null; });
                    const BUCKETS = [{ min: 50, max: 55 }, { min: 55, max: 60 }, { min: 60, max: 65 }, { min: 65, max: 70 }, { min: 70, max: 75 }, { min: 75, max: 80 }, { min: 80, max: 85 }, { min: 85, max: 90 }, { min: 90, max: 101 }];
                    const bucketStats = BUCKETS.map(function(b) {
                        const inBucket = nonJokerWithProb.filter(function(h) { var p = Number(h.probability); return p >= b.min && p < b.max; });
                        const wins = inBucket.filter(function(h) { return h.predicted === h.actual; }).length;
                        const total = inBucket.length;
                        return { label: b.min + '~' + (b.max === 101 ? '100' : b.max) + '%', total: total, wins: wins, pct: total > 0 ? (100 * wins / total).toFixed(1) : '-', min: b.min, max: b.max };
                    }).filter(function(s) { return s.total > 0; });
                    // 기존 확률에 30% 반영 (blendData는 전이 확률 표에서 계산됨). 한 출처: 서버 픽 표시 중일 때는 서버 확률 유지.
                    var usingServerPick = lastPrediction && (lastPrediction.value === '정' || lastPrediction.value === '꺽');
                    if (blendData && blendData.newProb != null && !is15Joker && !usingServerPick) predProb = 0.7 * predProb + 0.3 * blendData.newProb;
                    // 깜빡임: 예측픽 확률이 "승률 상위 2개 구간" 안에 있을 때만 (나올 확률 높은 게 아니라, 그 구간이 실제로 많이 이긴 구간일 때만)
                    var pickInBucket = false;
                    if (!is15Joker && predProb != null && bucketStats.length > 0) {
                        var sortedByRate = bucketStats.slice().sort(function(a, b) { return (parseFloat(b.pct) || 0) - (parseFloat(a.pct) || 0); });
                        var top2 = sortedByRate.slice(0, 2);
                        for (var ti = 0; ti < top2.length; ti++) {
                            var t = top2[ti];
                            if (predProb >= t.min && predProb < t.max) { pickInBucket = true; break; }
                        }
                    }
                    const lastEntry = validHist.length > 0 ? validHist[validHist.length - 1] : null;
                    const lastIsWin = lastEntry && lastEntry.actual !== 'joker' && lastEntry.predicted === lastEntry.actual;
                    const lastIsLose = lastEntry && lastEntry.actual !== 'joker' && lastEntry.predicted !== lastEntry.actual;
                    const shouldShowWinEffect = lastIsWin && lastEntry && lastWinEffectRound !== lastEntry.round;
                    const shouldShowLoseEffect = lastIsLose && lastEntry && lastLoseEffectRound !== lastEntry.round;
                    if (shouldShowWinEffect) lastWinEffectRound = lastEntry.round;
                    if (shouldShowLoseEffect) lastLoseEffectRound = lastEntry.round;
                    var resultBarHtml = '';
                    if (lastEntry && lastEntry.actual !== 'joker') {
                        var lastPickColor = normalizePickColor(lastEntry.pickColor || lastEntry.pick_color) || (lastEntry.predicted === '정' ? '빨강' : lastEntry.predicted === '꺽' ? '검정' : '') || '-';
                        var resultBarClass = lastIsWin ? 'pick-result-bar result-win' : 'pick-result-bar result-lose';
                        var resultBarText = displayRound(lastEntry.round) + '회 ' + (lastIsWin ? '성공' : '실패') + ' (' + (lastEntry.predicted || '-') + ' / ' + lastPickColor + ')';
                        resultBarHtml = '<div class="' + resultBarClass + '">' + resultBarText + '</div>';
                    }
                    const pickWrapClass = 'prediction-pick' + (pickInBucket ? ' pick-in-bucket' : '');
                    if (resultBarContainer) resultBarContainer.innerHTML = resultBarHtml;
                    const u35WarningBlock = lastWarningU35 ? ('<div class="prediction-warning-u35">⚠ U자+줄 3~5 구간 · 줄(유지) 보정 적용</div>') : '';
                    const displayRoundNum = (lastPrediction && lastPrediction.round) ? lastPrediction.round : predictedRoundFull;
                    const roundIconMain = getRoundIcon(displayRoundNum);
                    const showHold = is15Joker || predict === '보류';
                    const leftBlock = showHold ? ('<div class="prediction-pick">' +
                        '<div class="prediction-pick-title">예측 픽</div>' +
                        '<div class="prediction-card" style="background:#455a64;border-color:#78909c">' +
                        '<span class="pred-value-big" style="color:#fff;font-size:1.2em">보류</span>' +
                        '</div>' +
                        '<div class="prediction-prob-under" style="color:#ffb74d">' + (is15Joker ? '15번 카드 조커 · 배팅하지 마세요' : '서버 예측 대기 중') + '</div>' +
                        '<div class="pred-round">' + displayRound(displayRoundNum) + '회 ' + roundIconMain + '</div>' +
                        '</div>') : ('<div class="' + pickWrapClass + '">' +
                        '<div class="prediction-pick-title prediction-pick-title-betting">배팅중<br>' + (colorToPick === '빨강' ? 'RED' : 'BLACK') + '</div>' +
                        '<div class="prediction-card card-' + colorClass + '">' +
                        '<span class="pred-value-big">' + predict + '</span>' +
                        '</div>' +
                        '<div class="prediction-prob-under">예측 확률 ' + predProb.toFixed(1) + '%</div>' +
                        '<div class="pred-round">' + displayRound(displayRoundNum) + '회 ' + roundIconMain + '</div>' +
                        u35WarningBlock +
                        '</div>');
                    if (pickContainer) { pickContainer.innerHTML = leftBlock; pickContainer.setAttribute('data-section', '메인 예측기'); }
                    // 배팅 연동: 계산기별 픽은 updateCalcStatus(id) 내에서 POST (GET /api/current-pick?calculator=1|2|3 으로 조회).
                    if (predDiv) {
                        predDiv.setAttribute('data-section', '예측기표');
                        const rateClass50 = count50 > 0 ? (rate50 >= 60 ? 'high' : rate50 >= 50 ? 'mid' : 'low') : '';
                        const blendedStr = (typeof blendedWinRate === 'number' && !isNaN(blendedWinRate)) ? blendedWinRate.toFixed(1) : '-';
                        const blendedLow = (typeof blendedWinRate === 'number' && !isNaN(blendedWinRate) && blendedWinRate <= 50);
                        const blendedWrapClass = 'blended-win-rate-wrap' + (blendedLow ? ' blended-win-rate-low' : '');
                        const statsBlock = '<div class="' + blendedWrapClass + '">' +
                            '<div class="prediction-stats-blended-label">실제 경고 합산승률</div>' +
                            '<div class="prediction-stats-blended-value">' + blendedStr + '%</div>' +
                            '</div>' +
                            '<div class="prediction-stats-row">' +
                            '<span class="stat-total">최근 50회 결과</span>' +
                            '<span class="stat-win">승 - <span class="num">' + hit50 + '</span>회</span>' +
                            '<span class="stat-lose">패 - <span class="num">' + losses50 + '</span>회</span>' +
                            '<span class="stat-joker">조커 - <span class="num">' + joker50 + '</span>회</span>' +
                            (count50 > 0 ? '<span class="stat-rate ' + rateClass50 + '">승률 : ' + rate50Str + '%</span>' : '') +
                            '</div>' +
                            '<div class="prediction-stats-note" style="font-size:0.8em;color:#888;margin-top:2px">※ 예측픽 기준(계산기와 독립) · 합산승률=15·30·100 반영(65·25·10)</div>';
                        // 예측기표: 실제 경고 합산승률 + 최근 50회 결과 + 회차별(정/꺽/승·패·조커) 표 — 예측픽만 사용
                        let streakTableBlock = '';
                        try {
                        if (rev.length === 0) {
                            streakTableBlock = '<div class="prediction-streak-line">최근 100회 기준 · <span class="streak-now">' + streakLine100 + '</span></div>';
                        } else {
                            const headerCells = '<th>구분</th>' + rev.map(function(h) { return '<th>' + displayRound(h.round) + '</th>'; }).join('');
                            const rowProb = '<td>메인</td>' + rev.map(function(h) { return '<td>' + (h.probability != null ? Number(h.probability).toFixed(1) + '%' : '-') + '</td>'; }).join('');
                            const rowPick = '<td>메인</td>' + rev.map(function(h) {
                                const c = pickColorToClass(h.pickColor || h.pick_color);
                                return '<td class="' + c + '">' + (h.predicted != null ? h.predicted : '-') + '</td>';
                            }).join('');
                            const rowOutcome = '<td>메인</td>' + rev.map(function(h) {
                                var actualForDisplay = (roundActualsFromServer[String(h.round)] && roundActualsFromServer[String(h.round)].actual) ? roundActualsFromServer[String(h.round)].actual : h.actual;
                                var isJoker = (actualForDisplay === 'joker' || actualForDisplay === '조커');
                                const out = isJoker ? '조커' : (h.predicted === actualForDisplay ? '승' : '패');
                                const c = out === '승' ? 'streak-win' : out === '패' ? 'streak-lose' : 'streak-joker';
                                return '<td class="' + c + '">' + out + '</td>';
                            }).join('');
                            const rowShapePick = '<td>모양판별</td>' + rev.map(function(h) {
                                const sp = h.shape_predicted;
                                var c = '';
                                if (sp === '정' || sp === '꺽') {
                                    var mainColor = (h.pickColor || h.pick_color);
                                    if (mainColor === '빨강' || mainColor === '검정') {
                                        if (sp === (h.predicted || '')) c = pickColorToClass(mainColor);
                                        else c = pickColorToClass(mainColor === '빨강' ? '검정' : '빨강');
                                    } else {
                                        c = pickColorToClass(sp === '정' ? '빨강' : '검정');
                                    }
                                }
                                return '<td class="' + (c || '') + '">' + (sp || '-') + '</td>';
                            }).join('');
                            const rowShapeOutcome = '<td>모양판별</td>' + rev.map(function(h) {
                                var actualForDisplay = (roundActualsFromServer[String(h.round)] && roundActualsFromServer[String(h.round)].actual) ? roundActualsFromServer[String(h.round)].actual : h.actual;
                                if (typeof actualForDisplay === 'string') actualForDisplay = actualForDisplay.trim();
                                var isJoker = (actualForDisplay === 'joker' || actualForDisplay === '조커');
                                const sp = (h.shape_predicted && typeof h.shape_predicted === 'string') ? h.shape_predicted.trim() : null;
                                const out = isJoker ? '조커' : (sp && (sp === '정' || sp === '꺽') && (actualForDisplay === '정' || actualForDisplay === '꺽') && sp === actualForDisplay ? '승' : (sp && (sp === '정' || sp === '꺽') && (actualForDisplay === '정' || actualForDisplay === '꺽') ? '패' : '-'));
                                const c = out === '승' ? 'streak-win' : out === '패' ? 'streak-lose' : out === '조커' ? 'streak-joker' : '';
                                return '<td class="' + c + '">' + (out || '-') + '</td>';
                            }).join('');
                            streakTableBlock = '<div class="main-streak-table-wrap" data-section="예측기표"><table class="main-streak-table" aria-label="예측기표">' +
                                '<thead><tr>' + headerCells + '</tr></thead><tbody>' +
                                '<tr>' + rowProb + '</tr>' +
                                '<tr>' + rowPick + '</tr>' +
                                '<tr>' + rowOutcome + '</tr>' +
                                '<tr>' + rowShapePick + '</tr>' +
                                '<tr>' + rowShapeOutcome + '</tr>' +
                                '</tbody></table></div><div class="prediction-streak-line" style="margin-top:6px">최근 100회 기준 · <span class="streak-now">' + streakLine100 + '</span></div>';
                        }
                        } catch (streakErr) {
                            console.warn('연승/연패 표 구성 오류:', streakErr);
                            streakTableBlock = '<div class="prediction-streak-line">최근 100회 기준 · <span class="streak-now">' + streakLine100 + '</span></div>';
                        }
                        const probBucketBody = document.getElementById('prob-bucket-collapse-body');
                        const probBucketCollapse = document.getElementById('prob-bucket-collapse');
                        if (probBucketBody && probBucketCollapse) {
                            if (bucketStats.length > 0) {
                                const bucketRows = bucketStats.map(function(s) {
                                    const pctNum = s.pct !== '-' ? parseFloat(s.pct) : 0;
                                    const rowClass = pctNum >= 60 ? 'high' : pctNum >= 50 ? 'mid' : 'low';
                                    return '<tr><td>' + s.label + '</td><td>' + s.total + '</td><td>' + s.wins + '</td><td class="stat-rate ' + rowClass + '">' + s.pct + '%</td></tr>';
                                }).join('');
                                probBucketBody.innerHTML = '<table class="prob-bucket-table"><thead><tr><th>구간</th><th>n</th><th>승</th><th>%</th></tr></thead><tbody>' + bucketRows + '</tbody></table>';
                            } else {
                                probBucketBody.innerHTML = '';
                            }
                        }
                        var analysisTabsWrap = document.getElementById('analysis-tabs-wrap');
                        if (analysisTabsWrap) analysisTabsWrap.style.display = '';
                        var collapseHeader = document.getElementById('prob-bucket-collapse-header');
                        if (collapseHeader && !collapseHeader.getAttribute('data-bound')) {
                            collapseHeader.setAttribute('data-bound', '1');
                            collapseHeader.addEventListener('click', function() {
                                var el = document.getElementById('prob-bucket-collapse');
                                if (el) el.classList.toggle('collapsed');
                            });
                        }
                        var symmetryLineBody = document.getElementById('symmetry-line-collapse-body');
                        if (symmetryLineBody) {
                            if (symmetryLineData) {
                                var s = symmetryLineData;
                                var windowsLabel = (lastPongChunkDebug && Array.isArray(lastPongChunkDebug.symmetry_windows_used) && lastPongChunkDebug.symmetry_windows_used.length)
                                    ? lastPongChunkDebug.symmetry_windows_used.join('·') + '열'
                                    : (symmetryWindowsUsed && symmetryWindowsUsed.length) ? symmetryWindowsUsed.join('·') + '열' : '15·20·30열';
                                symmetryLineBody.innerHTML = '<table class="symmetry-line-table" cellspacing="0" cellpadding="0"><thead><tr><th>항목</th><th>값</th><th>비고</th></tr></thead><tbody>' +
                                    '<tr><td>반영 구간</td><td>' + windowsLabel + '</td><td>가중 평균(서버 예측픽 동일)</td></tr>' +
                                    '<tr><td>좌우 대칭도</td><td>' + s.symmetryPct.toFixed(1) + '%</td><td>좌반·우반 대칭 매칭</td></tr>' +
                                    '<tr><td>왼쪽 절반 줄 개수</td><td>' + s.leftLineCount + '</td><td>적을수록 긴 줄(추세), 많을수록 퐁당</td></tr>' +
                                    '<tr><td>오른쪽 절반 줄 개수</td><td>' + s.rightLineCount + '</td><td>적을수록 긴 줄(추세), 많을수록 퐁당</td></tr>' +
                                    '<tr><td>왼쪽 평균 줄길이</td><td>' + s.avgLeft.toFixed(2) + '</td><td>연속 정/꺽 평균</td></tr>' +
                                    '<tr><td>오른쪽 평균 줄길이</td><td>' + s.avgRight.toFixed(2) + '</td><td>연속 정/꺽 평균</td></tr>' +
                                    '<tr><td>줄 유사도</td><td>' + s.lineSimilarityPct.toFixed(1) + '%</td><td>양쪽 평균 줄길이 차이 반영</td></tr></tbody></table>';
                            } else {
                                symmetryLineBody.innerHTML = '<p style="color:#888;font-size:0.9em">최근 15열 이상(정/꺽) 데이터가 부족합니다.</p>';
                            }
                        }
                        var symmetryLineHeader = document.getElementById('symmetry-line-collapse-header');
                        if (symmetryLineHeader && !symmetryLineHeader.getAttribute('data-bound')) {
                            symmetryLineHeader.setAttribute('data-bound', '1');
                            symmetryLineHeader.addEventListener('click', function() {
                                var el = document.getElementById('symmetry-line-collapse');
                                if (el) el.classList.toggle('collapsed');
                            });
                        }
                        var pongChunkTbody = document.getElementById('pong-chunk-tbody');
                        if (pongChunkTbody) {
                            var phaseLabels = { 'line_phase': '줄 구간', 'pong_phase': '퐁당 구간', 'chunk_start': '덩어리 막 시작', 'chunk_phase': '덩어리 만드는 중', 'pong_to_chunk': '퐁당→덩어리 전환', 'chunk_to_pong': '덩어리→퐁당 전환' };
                            var phaseLabel = (lastPongChunkPhase && phaseLabels[lastPongChunkPhase]) ? phaseLabels[lastPongChunkPhase] : (lastPongChunkPhase || '—');
                            var segmentLabels = { 'line': '줄', 'pong': '퐁당', 'chunk': '덩어리' };
                            var chunkShapeLabels = { '321': '321 (줄어듦)', '123': '123 (늘어남)', 'block_repeat': '블록 반복' };
                            var d = lastPongChunkDebug || {};
                            var segmentLabel = (d.segment_type && segmentLabels[d.segment_type]) ? segmentLabels[d.segment_type] : (d.segment_type || '—');
                            var chunkShapeLabel = (d.chunk_shape && chunkShapeLabels[d.chunk_shape]) ? chunkShapeLabels[d.chunk_shape] : (d.chunk_shape || '—');
                            var uShapeLabel = (d.u_shape === true) ? '감지됨 (유지 가중치↑, 멈춤 권장)' : '—';
                            var chunkProfileStatsLabel = '—';
                            if (d.chunk_profile_jung != null && d.chunk_profile_kkeok != null) {
                                var cj = Number(d.chunk_profile_jung) || 0, ck = Number(d.chunk_profile_kkeok) || 0;
                                if (cj + ck >= 2) {
                                    chunkProfileStatsLabel = '다음 정 ' + cj.toFixed(1) + ', 꺽 ' + ck.toFixed(1) + ' (유사 덩어리 가중)';
                                }
                            }
                            if (chunkProfileStatsLabel === '—' && d.chunk_profile && Array.isArray(d.chunk_profile) && d.chunk_profile.length >= 2) {
                                chunkProfileStatsLabel = '수집 중 (덩어리: ' + d.chunk_profile.join(',') + ')';
                            }
                            if (chunkProfileStatsLabel === '—') {
                                chunkProfileStatsLabel = '덩어리 구간 아님';
                            }
                            var latestNextPickLabel = '—';
                            if (d.latest_next_pick && (d.latest_next_pick === '정' || d.latest_next_pick === '꺽')) {
                                latestNextPickLabel = d.latest_next_pick;
                            }
                            var overallPongLabel = (d.overall_pong_dominant === true) ? '감지됨 (퐁당 우세·줄 낮음·덩어리 적음 → 퐁당 가중치↑)' : '—';
                            var colHeightsLabel = '—';
                            if (d.column_heights && Array.isArray(d.column_heights) && d.column_heights.length) {
                                var ch = d.column_heights.slice(0, 15);
                                colHeightsLabel = ch.join(',') + (d.column_heights.length > 15 ? '…' : '');
                            }
                            var longShortLabel = '—';
                            if (d.long_short_stats && d.long_short_stats.total > 0) {
                                var ls = d.long_short_stats;
                                longShortLabel = '장줄(4+): ' + (ls.long || 0) + '개, 짧은줄(2~3): ' + (ls.short || 0) + '개, 퐁당(1): ' + (ls.pong || 0) + '개 / ' + ls.total + '열';
                            }
                            var rows = '<tr><td>판별 구간</td><td>' + phaseLabel + '</td></tr>' +
                                '<tr><td>구간 유형</td><td>' + segmentLabel + '</td></tr>' +
                                '<tr><td>덩어리 모양</td><td>' + chunkShapeLabel + '</td></tr>' +
                                '<tr><td>U자 구간</td><td>' + uShapeLabel + '</td></tr>' +
                                '<tr><td>전체 퐁당 우세</td><td>' + overallPongLabel + '</td></tr>' +
                                '<tr><td>열 높이 (최근)</td><td>' + colHeightsLabel + '</td></tr>' +
                                '<tr><td>장줄/짧은줄/퐁당</td><td>' + longShortLabel + '</td></tr>' +
                                '<tr><td>유사 덩어리 다음 결과</td><td>' + chunkProfileStatsLabel + '</td></tr>' +
                                '<tr><td>가장 최근 다음 픽</td><td>' + latestNextPickLabel + '</td></tr>' +
                                '<tr><td>모양 시그니처</td><td>' + (d.shape_signature || '—') + '</td></tr>' +
                                '<tr><td>맨 앞 run 타입</td><td>' + (d.first_run_type || '—') + '</td></tr>' +
                                '<tr><td>맨 앞 run 길이</td><td>' + (d.first_run_len != null ? d.first_run_len : '—') + '</td></tr>' +
                                '<tr><td>최근 15개 퐁당%</td><td>' + (d.pong_pct_short != null ? d.pong_pct_short.toFixed(1) + '%' : '—') + '</td></tr>' +
                                '<tr><td>직전 15개 퐁당%</td><td>' + (d.pong_pct_prev != null ? d.pong_pct_prev.toFixed(1) + '%' : '—') + '</td></tr>';
                            pongChunkTbody.innerHTML = rows;
                        }
                        var shapeVisual = document.getElementById('shape-visual-summary');
                        if (shapeVisual) {
                            var d2 = lastPongChunkDebug || {};
                            var hasData = lastPongChunkPhase || d2.shape_signature || d2.latest_next_pick || (d2.shape_jung_count != null) || (d2.chunk_profile_jung != null) || d2.overall_pong_dominant || (d2.column_heights && d2.column_heights.length);
                            if (hasData) {
                                shapeVisual.style.display = 'block';
                                var phaseLabelsMap = { 'line_phase': '줄 구간', 'pong_phase': '퐁당 구간', 'chunk_start': '덩어리 막 시작', 'chunk_phase': '덩어리 만드는 중', 'pong_to_chunk': '퐁당→덩어리 전환', 'chunk_to_pong': '덩어리→퐁당 전환' };
                                var phaseBadge = document.getElementById('shape-phase-badge');
                                var phaseColors = { 'line_phase': 'background:#2d4a2d;color:#81c784', 'pong_phase': 'background:#1e3a4a;color:#64b5f6', 'chunk_start': 'background:#4a3d1e;color:#ffb74d', 'chunk_phase': 'background:#4a3d1e;color:#ffb74d', 'pong_to_chunk': 'background:#3d2e4a;color:#b39ddb', 'chunk_to_pong': 'background:#3d2e4a;color:#b39ddb' };
                                var pLabel = (lastPongChunkPhase && phaseLabelsMap[lastPongChunkPhase]) ? phaseLabelsMap[lastPongChunkPhase] : (lastPongChunkPhase || '—');
                                if (phaseBadge) { phaseBadge.textContent = pLabel; phaseBadge.style.cssText = phaseColors[lastPongChunkPhase] || 'background:#333;color:#aaa'; }
                                var latestCard = document.getElementById('shape-latest-pick-card');
                                if (latestCard) {
                                    var lp = d2.latest_next_pick;
                                    if (lp === '정') { latestCard.textContent = '정'; latestCard.style.background = 'linear-gradient(135deg,#2e7d32,#4caf50)'; latestCard.style.color = '#fff'; }
                                    else if (lp === '꺽') { latestCard.textContent = '꺽'; latestCard.style.background = 'linear-gradient(135deg,#c62828,#e57373)'; latestCard.style.color = '#fff'; }
                                    else { latestCard.textContent = '—'; latestCard.style.background = '#333'; latestCard.style.color = '#888'; }
                                }
                                var sigBars = document.getElementById('shape-signature-bars');
                                if (sigBars && d2.shape_signature) {
                                    var parts = String(d2.shape_signature).split(',').filter(Boolean);
                                    var heights = { 'S': 10, 'M': 20, 'L': 28 };
                                    sigBars.innerHTML = parts.map(function(p) { var h = heights[p.trim()] || 16; return '<div style="width:12px;height:' + h + 'px;background:linear-gradient(180deg,#4caf50,#2e7d32);border-radius:2px;" title="' + (p.trim()) + '"></div>'; }).join('');
                                } else if (sigBars) { sigBars.innerHTML = '<span style="color:#666;font-size:0.9em">—</span>'; }
                                var shapeStatsBar = document.getElementById('shape-stats-bar');
                                if (shapeStatsBar) {
                                    var sj = Number(d2.shape_jung_count) || 0, sk = Number(d2.shape_kkeok_count) || 0;
                                    var stotal = sj + sk;
                                    var spct = stotal > 0 ? (sj / stotal * 100) : 50;
                                    shapeStatsBar.querySelector('.shape-bar-fill').style.width = spct + '%';
                                    shapeStatsBar.querySelector('.shape-bar-labels').textContent = stotal > 0 ? ('정 ' + sj.toFixed(1) + ' / 꺽 ' + sk.toFixed(1)) : '정 — / 꺽 —';
                                }
                                var chunkStatsBar = document.getElementById('chunk-stats-bar');
                                if (chunkStatsBar) {
                                    var cj = Number(d2.chunk_profile_jung) || 0, ck = Number(d2.chunk_profile_kkeok) || 0;
                                    var ctotal = cj + ck;
                                    var cpct = ctotal > 0 ? (cj / ctotal * 100) : 50;
                                    chunkStatsBar.querySelector('.shape-bar-fill').style.width = cpct + '%';
                                    chunkStatsBar.querySelector('.shape-bar-labels').textContent = ctotal > 0 ? ('정 ' + cj.toFixed(1) + ' / 꺽 ' + ck.toFixed(1)) : '정 — / 꺽 —';
                                }
                                var uWarn = document.getElementById('shape-u-warning');
                                if (uWarn) uWarn.style.display = (d2.u_shape === true) ? 'block' : 'none';
                            } else {
                                shapeVisual.style.display = 'none';
                            }
                        }
                        var pongChunkHeader = document.getElementById('pong-chunk-collapse-header');
                        if (pongChunkHeader && !pongChunkHeader.getAttribute('data-bound')) {
                            pongChunkHeader.setAttribute('data-bound', '1');
                            pongChunkHeader.addEventListener('click', function() {
                                var el = document.getElementById('pong-chunk-collapse');
                                if (el) el.classList.toggle('collapsed');
                            });
                        }
                        (function loadWinRateBuckets() {
                            var tbody = document.getElementById('win-rate-buckets-tbody');
                            if (!tbody) return;
                            fetch('/api/win-rate-buckets').then(function(r) { return r.json(); }).then(function(data) {
                                var buckets = data.buckets || [];
                                var recEl = document.getElementById('win-rate-recommendation');
                                if (recEl) {
                                    var rec = data.recommended_threshold;
                                    if (rec != null && typeof rec === 'number') {
                                        recEl.textContent = '반픽승률을 ' + rec + '%로 설정하시는 걸 추천드립니다.';
                                        recEl.style.display = '';
                                    } else {
                                        recEl.textContent = '';
                                        recEl.style.display = 'none';
                                    }
                                }
                                if (buckets.length === 0) {
                                    tbody.innerHTML = '<tr><td colspan="5" style="color:#888;">합산승률 데이터 없음 (회차 기록 후 저장되는 값)</td></tr>';
                                    return;
                                }
                                var rows = buckets.map(function(b) {
                                    var label = b.bucket_min + '~' + b.bucket_max + '%';
                                    var pct = b.win_pct != null ? b.win_pct.toFixed(1) : '-';
                                    var rowClass = b.win_pct != null && b.win_pct >= 55 ? 'high' : b.win_pct != null && b.win_pct >= 45 ? 'mid' : 'low';
                                    return '<tr><td>' + label + '</td><td>' + b.total + '</td><td>' + b.wins + '</td><td>' + b.losses + '</td><td class="stat-rate ' + rowClass + '">' + pct + '%</td></tr>';
                                }).join('');
                                tbody.innerHTML = rows;
                            }).catch(function() {
                                if (tbody) tbody.innerHTML = '<tr><td colspan="5" style="color:#888;">로드 실패</td></tr>';
                                var recEl = document.getElementById('win-rate-recommendation');
                                if (recEl) { recEl.textContent = ''; recEl.style.display = 'none'; }
                            });
                        })();
                        (function loadDontBetRanges() {
                            var msgEl = document.getElementById('dont-bet-ranges-msg');
                            if (!msgEl) return;
                            fetch('/api/dont-bet-ranges?limit=1000').then(function(r) { return r.json(); }).then(function(data) {
                                var ranges = data.dont_bet_ranges || [];
                                var text = '';
                                var color = '#ffcc80';
                                if (ranges.length === 0) {
                                    text = '2연패 데이터가 없습니다. (2연패 발생 시 예측확률 범위 표시)';
                                    color = '#9e9e9e';
                                } else {
                                    var r0 = ranges[0];
                                    text = '예측확률 ' + r0.min + '%부터 ' + r0.max + '%까지 2연패 했다면 배팅하지 마세요.';
                                }
                                if (msgEl) { msgEl.textContent = text; msgEl.style.color = color; }
                                var msgEl2 = document.getElementById('losing-streaks-dont-bet-msg');
                                if (msgEl2) { msgEl2.textContent = text; msgEl2.style.color = color; }
                            }).catch(function() {
                                var failText = '로드 실패';
                                if (msgEl) { msgEl.textContent = failText; msgEl.style.color = '#888'; }
                                var msgEl2 = document.getElementById('losing-streaks-dont-bet-msg');
                                if (msgEl2) { msgEl2.textContent = failText; msgEl2.style.color = '#888'; }
                            });
                        })();
                        var graphStatsCollapseHeader = document.getElementById('graph-stats-collapse-header');
                        if (graphStatsCollapseHeader && !graphStatsCollapseHeader.getAttribute('data-bound')) {
                            graphStatsCollapseHeader.setAttribute('data-bound', '1');
                            graphStatsCollapseHeader.addEventListener('click', function() {
                                var el = document.getElementById('graph-stats-collapse');
                                if (el) el.classList.toggle('collapsed');
                            });
                        }
                        var formulaCollapseHeader = document.getElementById('formula-collapse-header');
                        if (formulaCollapseHeader && !formulaCollapseHeader.getAttribute('data-bound')) {
                            formulaCollapseHeader.setAttribute('data-bound', '1');
                            formulaCollapseHeader.addEventListener('click', function() {
                                var el = document.getElementById('formula-collapse');
                                if (el) el.classList.toggle('collapsed');
                            });
                        }
                        (function loadLosingStreaks() {
                            var tbodyProb = document.getElementById('losing-streaks-prob-tbody');
                            var tbodyList = document.getElementById('losing-streaks-list-tbody');
                            if (!tbodyProb && !tbodyList) return;
                            fetch('/api/losing-streaks?limit=500').then(function(r) { return r.json(); }).then(function(data) {
                                var buckets = data.prob_buckets || [];
                                var streaks = data.streaks || [];
                                if (tbodyProb) {
                                    if (buckets.length === 0 && (data.total_streak_rounds || 0) === 0) {
                                        tbodyProb.innerHTML = '<tr><td colspan="2" style="color:#888;">3연패 이상 구간이 없거나 데이터가 부족합니다.</td></tr>';
                                    } else {
                                        var rows = buckets.map(function(b) {
                                            var label = b.bucket_min + '~' + b.bucket_max + '%';
                                            return '<tr><td>' + label + '</td><td>' + (b.count || 0) + '</td></tr>';
                                        }).join('');
                                        tbodyProb.innerHTML = rows;
                                    }
                                }
                                if (tbodyList) {
                                    if (streaks.length === 0) {
                                        tbodyList.innerHTML = '<tr><td colspan="4" style="color:#888;">3연패 이상 구간이 없습니다.</td></tr>';
                                    } else {
                                        var listRows = streaks.map(function(s) {
                                            var startR = s.start_round != null ? s.start_round : '-';
                                            var endR = s.end_round != null ? s.end_round : '-';
                                            var len = s.length != null ? s.length : '-';
                                            var avgP = s.avg_probability != null ? s.avg_probability + '%' : '-';
                                            return '<tr><td>' + startR + '</td><td>' + endR + '</td><td>' + len + '</td><td>' + avgP + '</td></tr>';
                                        }).join('');
                                        tbodyList.innerHTML = listRows;
                                    }
                                }
                            }).catch(function() {
                                if (tbodyProb) tbodyProb.innerHTML = '<tr><td colspan="2" style="color:#888;">로드 실패</td></tr>';
                                if (tbodyList) tbodyList.innerHTML = '<tr><td colspan="4" style="color:#888;">로드 실패</td></tr>';
                            });
                        })();
                        var losingStreaksHeader = document.getElementById('losing-streaks-collapse-header');
                        if (losingStreaksHeader && !losingStreaksHeader.getAttribute('data-bound')) {
                            losingStreaksHeader.setAttribute('data-bound', '1');
                            losingStreaksHeader.addEventListener('click', function() {
                                var el = document.getElementById('losing-streaks-collapse');
                                if (el) el.classList.toggle('collapsed');
                            });
                        }
                        let noticeBlock = '';
                        if (flowAdvice || lowWinRate || symmetryBoostNotice || newSegmentNotice) {
                            const notices = [];
                            if (flowAdvice) notices.push(flowAdvice);
                            if (lowWinRate) notices.push('⚠ 승률이 낮으니 배팅 주의 (합산승률: ' + blendedWinRate.toFixed(1) + '%)');
                            if (newSegmentNotice) notices.push('새로운 구간 구성 중, 왼쪽 추세 반영');
                            if (symmetryBoostNotice) notices.push('좌우대칭이 확인되어 보정이 반영됩니다');
                            noticeBlock = '<div class="prediction-notice' + (lowWinRate && !flowAdvice ? ' danger' : '') + '">' + notices.join(' &nbsp; · &nbsp; ') + '</div>';
                        }
                        const extraLine = '<div class="flow-type" style="margin-top:6px;font-size:clamp(0.75em,1.8vw,0.85em)">' + flowStr + (linePatternStr ? ' &nbsp;|&nbsp; ' + linePatternStr : '') + '</div>';
                        var wrapEl = predDiv.querySelector('.main-streak-table-wrap');
                        var savedScroll = wrapEl ? wrapEl.scrollLeft : 0;
                        predDiv.innerHTML = noticeBlock + statsBlock + streakTableBlock + extraLine;
                        var newWrap = predDiv.querySelector('.main-streak-table-wrap');
                        if (newWrap && savedScroll > 0) newWrap.scrollLeft = savedScroll;
                    }
                    
                    // 가상 배팅 계산기: history 변경된 것만 갱신 (배팅픽 표시 속도 개선)
                    try {
                        CALC_IDS.forEach(id => {
                            if (needCalcUpdate(id)) {
                                updateCalcSummary(id);
                                updateCalcDetail(id);
                            }
                        });
                    } catch (calcErr) {
                        console.warn('계산기 갱신 오류:', calcErr);
                    }
                } else if (statsDiv) {
                    statsDiv.innerHTML = '';
                    const resultBarEmpty = document.getElementById('prediction-result-bar');
                    const pickEmpty = document.getElementById('prediction-pick-container');
                    const predDivEmpty = document.getElementById('prediction-box');
                    const probBucketBodyEmpty = document.getElementById('prob-bucket-collapse-body');
                    const symmetryLineBodyEmpty = document.getElementById('symmetry-line-collapse-body');
                    if (resultBarEmpty) resultBarEmpty.innerHTML = '';
                    if (pickEmpty) pickEmpty.innerHTML = '';
                    if (predDivEmpty) predDivEmpty.innerHTML = '';
                    if (probBucketBodyEmpty) probBucketBodyEmpty.innerHTML = '';
                    if (symmetryLineBodyEmpty) symmetryLineBodyEmpty.innerHTML = '';
                }
                } catch (renderErr) {
                    if (statusEl) statusEl.textContent = '표시 오류 - 새로고침 해 주세요';
                    console.error('표시 오류:', renderErr);
                }
            } catch (error) {
                const statusEl = document.getElementById('status');
                // AbortError는 조용히 처리 (타임아웃은 정상적인 상황)
                if (error.name === 'AbortError') {
                    if (statusEl) statusEl.textContent = allResults.length === 0 ? '5초 내 응답 없음 - 다시 시도 중...' : '갱신 대기 중...';
                    if (allResults.length === 0) setTimeout(() => loadResults(), 1200);
                    return;
                }
                
                // Failed to fetch는 네트워크 오류이므로 조용히 처리 (기존 결과 유지)
                if (error.message === 'Failed to fetch' || error.name === 'TypeError') {
                    if (statusEl && allResults.length === 0) statusEl.textContent = '연결 실패 - 1.2초 후 재시도...';
                    if (allResults.length === 0) setTimeout(() => loadResults(), 1200);
                    return;
                }
                
                // 기타 오류만 로그
                console.error('loadResults 오류:', error);
                if (statusEl) {
                    statusEl.textContent = '결과 로드 오류: ' + error.message;
                }
            } finally {
                isLoadingResults = false;  // 로딩 완료
            }
        }
        
        function formatMmSs(sec) {
            const h = Math.floor(sec / 3600);
            const m = Math.floor((sec % 3600) / 60);
            const s = Math.floor(sec % 60);
            return h + '시 ' + m + '분 ' + s + '초';
        }
        function getCalcResult(id) {
            try {
            if (!calcState[id]) return { cap: 0, profit: 0, currentBet: 0, wins: 0, losses: 0, bust: false, maxWinStreak: 0, maxLoseStreak: 0, winRate: '-', processedCount: 0 };
            const capIn = parseFloat(document.getElementById('calc-' + id + '-capital')?.value) || 1000000;
            const baseIn = parseFloat(document.getElementById('calc-' + id + '-base')?.value) || 10000;
            const oddsIn = parseFloat(document.getElementById('calc-' + id + '-odds')?.value) || 1.97;
            const martingaleEl = document.getElementById('calc-' + id + '-martingale');
            const martingaleTypeEl = document.getElementById('calc-' + id + '-martingale-type');
            const useMartingale = !!(martingaleEl && martingaleEl.checked);
            const martingaleType = (martingaleTypeEl && martingaleTypeEl.value) || 'pyo';
            const hist = dedupeCalcHistoryByRound(calcState[id].history || []);
            
            // [변경 금지] 순익·보유자산은 CALCULATOR_GUIDE에 따라 마틴게일 시뮬레이션으로만 계산.
            // 서버 h.profit/h.betAmount는 DB 병합 타이밍으로 어긋날 수 있으므로 사용하지 않음. 표와 동일 출처 보장.
            let cap = capIn, currentBet = baseIn, bust = false;
            let martingaleStep = 0;
            let wins = 0, losses = 0, maxWinStreak = 0, maxLoseStreak = 0, curWin = 0, curLose = 0;
            let processedCount = 0;
            var martinTable = getMartinTable(martingaleType, baseIn);
            for (let i = 0; i < hist.length; i++) {
                const h = hist[i];
                if (!h || typeof h.predicted === 'undefined' || typeof h.actual === 'undefined') continue;
                if (h.actual === 'pending') continue;  // 미결 회차는 배팅금·수익 계산에서 제외
                if (h.no_bet === true || (h.betAmount != null && h.betAmount === 0)) continue;  // 멈춤 회차(배팅 안 함)는 순익/자본 계산에서 제외
                if (useMartingale && (martingaleType === 'pyo' || martingaleType === 'pyo_half')) {
                    currentBet = martinTable[Math.min(martingaleStep, martinTable.length - 1)];
                }
                var bet = Math.min(currentBet, Math.floor(cap));  // 완료 행은 시뮬레이션 기준 배팅금만 사용 (h.betAmount 미사용)
                if (cap < bet || cap <= 0) { bust = true; processedCount = i; break; }
                const isJoker = h.actual === 'joker';
                const isWin = !isJoker && h.predicted === h.actual;
                if (isJoker) {
                    cap -= bet;
                    if (useMartingale && (martingaleType === 'pyo' || martingaleType === 'pyo_half')) martingaleStep = Math.min(martingaleStep + 1, martinTable.length - 1);
                    else currentBet = Math.min(currentBet * 2, Math.floor(cap));
                    curWin = 0;
                    curLose = 0;
                } else if (isWin) {
                    cap += bet * (oddsIn - 1);
                    if (useMartingale && (martingaleType === 'pyo' || martingaleType === 'pyo_half')) martingaleStep = 0;
                    else currentBet = baseIn;
                    wins++;
                    curWin++;
                    curLose = 0;
                    if (curWin > maxWinStreak) maxWinStreak = curWin;
                } else {
                    cap -= bet;
                    if (useMartingale && (martingaleType === 'pyo' || martingaleType === 'pyo_half')) martingaleStep = Math.min(martingaleStep + 1, martinTable.length - 1);
                    else currentBet = Math.min(currentBet * 2, Math.floor(cap));
                    losses++;
                    curLose++;
                    curWin = 0;
                    if (curLose > maxLoseStreak) maxLoseStreak = curLose;
                }
                processedCount = i + 1;
                if (cap <= 0) { bust = true; break; }
            }
            var martinTableFinal = getMartinTable(martingaleType, baseIn);
            if (useMartingale && (martingaleType === 'pyo' || martingaleType === 'pyo_half')) {
                currentBet = bust ? 0 : martinTableFinal[Math.min(martingaleStep, martinTableFinal.length - 1)];
            }
            if (calcState[id]) {
                calcState[id].maxWinStreakEver = Math.max(calcState[id].maxWinStreakEver || 0, maxWinStreak);
                calcState[id].maxLoseStreakEver = Math.max(calcState[id].maxLoseStreakEver || 0, maxLoseStreak);
            }
            const profit = cap - capIn;
            const total = wins + losses;
            const winRate = total > 0 ? (100 * wins / total).toFixed(1) : '-';
            const displayMaxWin = (calcState[id] && calcState[id].maxWinStreakEver != null) ? calcState[id].maxWinStreakEver : maxWinStreak;
            const displayMaxLose = (calcState[id] && calcState[id].maxLoseStreakEver != null) ? calcState[id].maxLoseStreakEver : maxLoseStreak;
            return { cap: Math.max(0, Math.floor(cap)), profit, currentBet: bust ? 0 : currentBet, wins, losses, bust, maxWinStreak: displayMaxWin, maxLoseStreak: displayMaxLose, winRate, processedCount: bust ? processedCount : hist.length };
            } catch (e) { console.warn('getCalcResult', id, e); return { cap: 0, profit: 0, currentBet: 0, wins: 0, losses: 0, bust: false, maxWinStreak: 0, maxLoseStreak: 0, winRate: '-', processedCount: 0 }; }
        }
        function getBetForRound(id, roundNum) {
            try {
                if (!calcState[id] || roundNum == null) return 0;
                // 서버가 저장한 pending 회차 금액이 있으면 그대로 사용(금액 왔다갔다 방지)
                var pr = calcState[id].pending_round;
                var pba = calcState[id].pending_bet_amount;
                if (pr != null && Number(pr) === Number(roundNum) && pba != null && pba > 0) return Math.floor(pba);
                const capIn = parseFloat(document.getElementById('calc-' + id + '-capital')?.value) || 1000000;
                const baseIn = parseFloat(document.getElementById('calc-' + id + '-base')?.value) || 10000;
                const oddsIn = parseFloat(document.getElementById('calc-' + id + '-odds')?.value) || 1.97;
                const martingaleEl = document.getElementById('calc-' + id + '-martingale');
                const martingaleTypeEl = document.getElementById('calc-' + id + '-martingale-type');
                const useMartingale = !!(martingaleEl && martingaleEl.checked);
                const martingaleType = (martingaleTypeEl && martingaleTypeEl.value) || 'pyo';
                const hist = (calcState[id].history || []).filter(function(h) { return h && h.round != null && Number(h.round) < roundNum && h.actual !== 'pending' && h.actual != null && typeof h.actual !== 'undefined'; });
                const sorted = dedupeCalcHistoryByRound(hist).sort(function(a, b) { return Number(a.round) - Number(b.round); });
                let cap = capIn, currentBet = baseIn, martingaleStep = 0;
                var martinTable = getMartinTable(martingaleType, baseIn);
                for (var i = 0; i < sorted.length; i++) {
                    var h = sorted[i];
                    if (!h || typeof h.predicted === 'undefined' || typeof h.actual === 'undefined') continue;
                    if (h.no_bet === true || (h.betAmount != null && h.betAmount === 0)) continue;  // 멈춤 회차는 배팅 없음 → 자본/마틴 단계 변화 없음
                    if (useMartingale && (martingaleType === 'pyo' || martingaleType === 'pyo_half')) currentBet = martinTable[Math.min(martingaleStep, martinTable.length - 1)];
                    var bet = Math.min(currentBet, Math.floor(cap));
                    if (cap < bet || cap <= 0) return 0;
                    var isJoker = h.actual === 'joker';
                    var isWin = !isJoker && h.predicted === h.actual;
                    if (isJoker) { cap -= bet; if (useMartingale && (martingaleType === 'pyo' || martingaleType === 'pyo_half')) martingaleStep = Math.min(martingaleStep + 1, martinTable.length - 1); else currentBet = Math.min(currentBet * 2, Math.floor(cap)); }
                    else if (isWin) { cap += bet * (oddsIn - 1); if (useMartingale && (martingaleType === 'pyo' || martingaleType === 'pyo_half')) martingaleStep = 0; else currentBet = baseIn; }
                    else { cap -= bet; if (useMartingale && (martingaleType === 'pyo' || martingaleType === 'pyo_half')) martingaleStep = Math.min(martingaleStep + 1, martinTable.length - 1); else currentBet = Math.min(currentBet * 2, Math.floor(cap)); }
                    if (cap <= 0) return 0;
                }
                // [변경] 직전 회차가 pending일 때 패배 가정 제거 — 마틴 한 단계 더 가는 버그 방지 (계산기표 vs 자동배팅 금액 불일치 원인)
                if (useMartingale && (martingaleType === 'pyo' || martingaleType === 'pyo_half')) currentBet = martinTable[Math.min(martingaleStep, martinTable.length - 1)];
                return Math.min(currentBet, Math.floor(cap));
            } catch (e) { return 0; }
        }
        function getCalcRecent15WinRate(id) {
            var hist = calcState[id] && calcState[id].history;
            if (!Array.isArray(hist) || hist.length === 0) return 50;
            // 서버 prediction_history로 동기화 (15회 승률 계산 전에 강제 동기화)
            if (Array.isArray(predictionHistory) && predictionHistory.length > 0) {
                var byRound = {};
                predictionHistory.forEach(function(p) {
                    if (p && typeof p === 'object' && p.round != null && p.actual != null && p.actual !== '') {
                        byRound[Number(p.round)] = { actual: p.actual };
                    }
                });
                hist.forEach(function(h) {
                    if (!h) return;
                    if (h.actual === 'pending' || !h.actual || h.actual === '') {
                        var r = Number(h.round);
                        if (!isNaN(r)) {
                            var fromServer = byRound[r];
                            if (fromServer) h.actual = fromServer.actual;
                        }
                    }
                });
            }
            var completed = hist.filter(function(h) { return h && h.actual && h.actual !== 'pending' && h.actual !== ''; });
            var last15 = completed.slice(-15);
            if (last15.length < 1) return 50;
            // 조커는 패로 간주: 승 = 실제 정/꺽이고 예측 적중한 경우만
            var wins = last15.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; });
            return (wins.length / last15.length) * 100;
        }
        /** 해당 회차(upToRound) 완료 시점의 15회 승률. 표 15회승률 열 저장값 보정용 — 완료 행에 rate15 없을 때 한 번만 채움. */
        function getCalcRecent15WinRateAtRound(id, upToRound) {
            var hist = calcState[id] && calcState[id].history;
            if (!Array.isArray(hist) || upToRound == null) return null;
            var completed = hist.filter(function(h) { return h && h.actual && h.actual !== 'pending' && h.actual !== '' && h.round != null; });
            completed.sort(function(a, b) { return (Number(a.round) || 0) - (Number(b.round) || 0); });
            var upTo = completed.filter(function(h) { return Number(h.round) <= Number(upToRound); });
            var last15 = upTo.slice(-15);
            if (last15.length < 1) return null;
            var wins = last15.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; });
            return Math.round((wins.length / last15.length) * 1000) / 10;
        }
        /** 표승률: 계산기 최근 200회 중 배팅한 완료 행만, 조커=패. 승률반픽 기준. */
        function getDisplayWinRate(id, maxRows) {
            var hist = calcState[id] && calcState[id].history;
            if (!Array.isArray(hist)) return null;
            var completed = hist.filter(function(h) { return h && h.actual && h.actual !== 'pending' && h.actual !== ''; });
            var betRows = completed.filter(function(h) { return !h.no_bet && (h.betAmount || 0) > 0; });
            var lastN = (maxRows || 200) >= betRows.length ? betRows : betRows.slice(-(maxRows || 200));
            if (!lastN || lastN.length < 1) return null;
            var wins = lastN.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
            var losses = lastN.filter(function(h) { return h.actual === 'joker' || h.predicted !== h.actual; }).length;
            var total = wins + losses;
            return total < 1 ? null : 100 * wins / total;
        }
        /** 메인 예측기 모양 픽 최근 50회 승률. prediction_history의 shape_predicted vs actual, 조커 제외. */
        function getShape50WinRate() {
            var vh = Array.isArray(predictionHistory) ? predictionHistory.filter(function(h) { return h && typeof h === 'object'; }) : [];
            var last50 = vh.slice(-50).filter(function(h) { return h.actual === '정' || h.actual === '꺽'; });
            if (last50.length < 1) return null;
            var sp = last50.filter(function(h) { return (h.shape_predicted === '정' || h.shape_predicted === '꺽') && h.shape_predicted === h.actual; }).length;
            var total = last50.filter(function(h) { return h.shape_predicted === '정' || h.shape_predicted === '꺽'; }).length;
            return total < 1 ? null : 100 * sp / total;
        }
        /** 모양판별승률: 메인 예측기표 모양판별 픽(shape_predicted) 최신 10개 결과. prediction_history 기준, 조커=패. 모양판별반픽 판단용. */
        function getShapePredictionWinRate10(id) {
            var vh = Array.isArray(predictionHistory) ? predictionHistory.filter(function(h) { return h && typeof h === 'object'; }) : [];
            var withShape = vh.filter(function(h) { return (h.shape_predicted === '정' || h.shape_predicted === '꺽') && (h.actual === '정' || h.actual === '꺽' || h.actual === 'joker' || h.actual === '조커'); });
            if (withShape.length < 1) return null;
            var last10 = withShape.slice(-10);
            var wins = 0, total = 0;
            last10.forEach(function(h) {
                var sp = h.shape_predicted;
                var act = h.actual;
                if (act === 'joker' || act === '조커') { total++; return; }
                total++;
                if (sp === act) wins++;
            });
            return total < 1 ? null : 100 * wins / total;
        }
        /** 경고 표와 동일한 합산승률(blended). 멈춤은 계산기 표 15회 승률 기준으로 변경됨. */
        function getBlendedWinRate() {
            var vh = Array.isArray(predictionHistory) ? predictionHistory.filter(function(h) { return h && typeof h === 'object' && h.actual !== 'joker'; }) : [];
            if (vh.length < 1) return null;
            var v15 = vh.slice(-15), v30 = vh.slice(-30), v100 = vh.slice(-100);
            var r15 = v15.length > 0 ? 100 * v15.filter(function(h) { return h.predicted === h.actual; }).length / v15.length : 50;
            var r30 = v30.length > 0 ? 100 * v30.filter(function(h) { return h.predicted === h.actual; }).length / v30.length : 50;
            var r100 = v100.length > 0 ? 100 * v100.filter(function(h) { return h.predicted === h.actual; }).length / v100.length : 50;
            return 0.65 * r15 + 0.25 * r30 + 0.10 * r100;
        }
        function getLoseStreak(id) {
            var hist = calcState[id] && calcState[id].history;
            if (!Array.isArray(hist)) return 0;
            var completed = hist.filter(function(h) { return h && h.actual && h.actual !== 'pending'; });
            if (completed.length === 0) return 0;
            var n = 0;
            for (var i = completed.length - 1; i >= 0; i--) {
                var h = completed[i];
                if (h.actual === 'joker' || h.predicted !== h.actual) n++;
                else break;
            }
            return n;
        }
        function getLoseStreakMin(id) {
            var el = document.getElementById('calc-' + id + '-lose-streak-reverse-min');
            var v = (el && !isNaN(parseInt(el.value, 10))) ? Math.max(2, Math.min(15, parseInt(el.value, 10))) : (calcState[id] != null && typeof calcState[id].lose_streak_reverse_min_streak === 'number' ? calcState[id].lose_streak_reverse_min_streak : 3);
            return typeof v === 'number' && !isNaN(v) ? v : 4;
        }
        /** 마틴 사용 중 연패 구간이면 멈춤(paused) 적용 안 함 — 마틴을 마친 다음(연패 후 승)에만 멈춤. */
        function effectivePausedForRound(id) {
            if (!calcState[id]) return false;
            var martingaleEl = document.getElementById('calc-' + id + '-martingale');
            if (!(martingaleEl && martingaleEl.checked)) return !!calcState[id].paused;
            var hist = calcState[id].history || [];
            var completed = hist.filter(function(h) { return h.actual && h.actual !== 'pending'; });
            if (completed.length === 0) return !!calcState[id].paused;
            var last = completed[completed.length - 1];
            var lastIsLoss = last.actual === 'joker' || last.predicted !== last.actual;
            if (lastIsLoss) return false;
            return !!calcState[id].paused;
        }
        /** 마틴 켜져 있고 직전 완료 회차가 패/조커면 true. pending 반영 시 예전 no_bet 덮어쓸지 판단용. */
        function isMartingaleLossStreak(id) {
            if (!calcState[id]) return false;
            var martingaleEl = document.getElementById('calc-' + id + '-martingale');
            if (!(martingaleEl && martingaleEl.checked)) return false;
            var hist = calcState[id].history || [];
            var completed = hist.filter(function(h) { return h.actual && h.actual !== 'pending'; });
            if (completed.length === 0) return false;
            var last = completed[completed.length - 1];
            return last.actual === 'joker' || last.predicted !== last.actual;
        }
        function checkPauseAfterWin(id) {
            var pauseLowEl = document.getElementById('calc-' + id + '-pause-low-win-rate');
            var pauseThrEl = document.getElementById('calc-' + id + '-pause-win-rate-threshold');
            if (calcState[id]) {
                calcState[id].pause_low_win_rate_enabled = !!(pauseLowEl && pauseLowEl.checked);
                var thrNum = (pauseThrEl && !isNaN(parseFloat(pauseThrEl.value))) ? Math.max(0, Math.min(100, parseFloat(pauseThrEl.value))) : 45;
                calcState[id].pause_win_rate_threshold = thrNum;
            }
            var hist = calcState[id].history || [];
            var completed = hist.filter(function(h) { return h.actual && h.actual !== 'pending'; });
            // 멈춤은 '승률≤N% 이하·연패 시 배팅멈춤' 옵션에만 해당. 마틴만 체크한 경우는 멈춤과 무관(연패 시 마틴대로 계속 진행)
            if (!pauseLowEl || !pauseLowEl.checked) return;
            var martingaleEl = document.getElementById('calc-' + id + '-martingale');
            var useMartingale = !!(martingaleEl && martingaleEl.checked);
            // 멈춤 옵션 체크 시: 이미 마틴 중(직전 완료가 패/조커)이면 마틴을 끝낸 뒤(승 한 번 나온 뒤) 멈춤 → 지금은 paused 세우지 않음
            if (useMartingale && completed.length >= 1) {
                var last = completed[completed.length - 1];
                var lastIsLoss = last.actual === 'joker' || last.predicted !== last.actual;
                if (lastIsLoss) return;  // 연패 중이면 아직 멈추지 않음
            }
            // 멈춤 기준 = 계산기 표의 15회 승률 (해당 계산기 배팅 상황과 맞춤). 데이터 없으면 합산승률 폴백
            var rate15 = getCalcRecent15WinRate(id);
            var rateForPause = (completed.length >= 1) ? rate15 : (getBlendedWinRate() != null ? getBlendedWinRate() : rate15);
            var thr = (pauseThrEl && !isNaN(parseFloat(pauseThrEl.value))) ? Math.max(0, Math.min(100, parseFloat(pauseThrEl.value))) : 45;
            if (rateForPause <= thr) {
                calcState[id].paused = true;
                for (var j = 0; j < hist.length; j++) {
                    if (hist[j] && hist[j].actual === 'pending') { hist[j].betAmount = 0; hist[j].no_bet = true; }
                }
                calcState[id].history = dedupeCalcHistoryByRound(hist);
                saveCalcStateToServer();
                updateCalcDetail(id);
                postCurrentPickIfChanged(id, { pickColor: null, round: null, probability: null, suggested_amount: null });
            }
        }
        function getPauseGuideList(source) {
            var raw = [];
            if (source === 'pred' && typeof predictionHistory !== 'undefined' && Array.isArray(predictionHistory)) {
                raw = predictionHistory.filter(function(h) {
                    if (!h) return false;
                    var a = h.actual;
                    return a === '정' || a === '꺽' || a === 'joker' || a === true || a === false;
                }).map(function(h) {
                    var pred = (h.predicted != null ? h.predicted : (h.value != null ? h.value : ''));
                    var a = h.actual;
                    if (a === true) a = '정'; else if (a === false) a = '꺽';
                    return { round: h.round, predicted: pred, actual: a };
                });
            } else if (source === '1' || source === '2' || source === '3') {
                var id = parseInt(source, 10);
                var hist = calcState[id] && calcState[id].history;
                if (!Array.isArray(hist)) return [];
                raw = hist.filter(function(h) { return h && h.actual && h.actual !== 'pending'; }).map(function(h) { return { round: h.round, predicted: h.predicted, actual: h.actual }; });
            }
            raw.sort(function(a, b) { return Number(a.round) - Number(b.round); });
            return raw;
        }
        function computePauseGuide(list) {
            var thresholds = [35, 40, 45, 50, 55, 60];
            var out = [];
            for (var t = 0; t < thresholds.length; t++) {
                var thr = thresholds[t];
                var wins = 0, bets = 0;
                for (var i = 0; i < list.length; i++) {
                    var window = list.slice(Math.max(0, i - 15), i);
                    var rate15 = 50;
                    if (window.length >= 1) {
                        var wWins = window.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
                        rate15 = (wWins / window.length) * 100;
                    }
                    if (rate15 <= thr) continue;
                    bets++;
                    if (list[i].actual !== 'joker' && list[i].predicted === list[i].actual) wins++;
                }
                var actualWinRate = bets > 0 ? (wins / bets * 100) : null;
                out.push({ threshold: thr, bets: bets, wins: wins, actualWinRate: actualWinRate });
            }
            return out;
        }
        function renderPauseGuideTable() {
            var wrap = document.getElementById('pause-guide-table-wrap');
            var sel = document.getElementById('pause-guide-source');
            if (!wrap || !sel) return;
            var source = sel.value || 'pred';
            var list = getPauseGuideList(source);
            if (list.length < 15) {
                wrap.innerHTML = '<p class="pause-guide-desc" style="color:#888;">완료된 회차가 15회 미만입니다. 예측기/계산기 데이터를 더 쌓은 뒤 다시 계산하세요.</p>';
                return;
            }
            var rows = computePauseGuide(list);
            var bestRate = -1;
            for (var r = 0; r < rows.length; r++) { if (rows[r].actualWinRate != null && rows[r].actualWinRate > bestRate) bestRate = rows[r].actualWinRate; }
            var tbl = '<table class="pause-guide-table"><thead><tr><th>멈춤 기준 (15회 승률 ≤)</th><th>배팅한 회차 수</th><th>실제 승률</th></tr></thead><tbody>';
            for (var i = 0; i < rows.length; i++) {
                var row = rows[i];
                var rateStr = row.actualWinRate != null ? row.actualWinRate.toFixed(1) + '%' : '-';
                var trClass = (row.actualWinRate != null && row.actualWinRate === bestRate) ? ' class="best-row"' : '';
                tbl += '<tr' + trClass + '><td>' + row.threshold + '%</td><td>' + row.bets + '</td><td>' + rateStr + '</td></tr>';
            }
            tbl += '</tbody></table>';
            wrap.innerHTML = tbl;
        }
        /** 실제 결과(actual) 기준 맨 끝에서 연속 같은 결과(정 또는 꺽) 개수. 조커 나오면 끊김. 5연승/5연패 판별용 */
        function getCurrentResultRunLength(ph) {
            if (!ph || !Array.isArray(ph) || ph.length === 0) return 0;
            var vh = ph.filter(function(h) { return h && h.actual != null && String(h.actual).trim() !== ''; });
            if (vh.length === 0) return 0;
            var lastActual = vh[vh.length - 1].actual;
            if (lastActual === 'joker' || lastActual === '조커') return 0;
            var run = 0;
            for (var i = vh.length - 1; i >= 0; i--) {
                var a = vh[i].actual;
                if (a === 'joker' || a === '조커') break;
                if (a !== lastActual) break;
                run++;
            }
            return run;
        }
        var WIN_RATE_DIRECTION_WINDOW = 100;
        function getWinRateDirectionZone(ph) {
            if (!Array.isArray(ph) || ph.length < WIN_RATE_DIRECTION_WINDOW) return null;
            var vh = ph.filter(function(h) { return h && typeof h === 'object' && h.actual != null && h.actual !== ''; });
            if (vh.length > 600) vh = vh.slice(-600);
            var last10 = vh.slice(-10).filter(function(h) { return h.actual !== 'joker' && h.actual !== '조커'; });
            var rate10Pct = null;
            if (last10.length >= 3) {
                var wins10 = last10.filter(function(h) { return h.predicted === h.actual; }).length;
                rate10Pct = 100 * wins10 / last10.length;
            }
            var derivedSeries = [];
            for (var i = WIN_RATE_DIRECTION_WINDOW - 1; i < vh.length; i++) {
                var w = vh.slice(i - (WIN_RATE_DIRECTION_WINDOW - 1), i + 1);
                var wins = w.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
                var losses = w.filter(function(h) { return h.actual !== 'joker' && h.predicted !== h.actual; }).length;
                var c = wins + losses;
                if (c > 0) derivedSeries.push({ round: vh[i].round, rate50: 100 * wins / c });
            }
            if (derivedSeries.length < 6) return null;
            var rates = derivedSeries.map(function(x) { return x.rate50; });
            var high = rates.reduce(function(a, b) { return a > b ? a : b; }, -Infinity);
            var low = rates.reduce(function(a, b) { return a < b ? a : b; }, Infinity);
            if (high <= low) return null;
            var current = derivedSeries[derivedSeries.length - 1].rate50;
            var rate5Ago = derivedSeries[derivedSeries.length - 6].rate50;
            var delta5 = current - rate5Ago;
            var ratioDynamic = (current - low) / (high - low);
            var WIN_RATE_LOW_BAND = 43, WIN_RATE_HIGH_BAND = 57;
            var ratioFixed = current <= WIN_RATE_LOW_BAND ? 0 : (current >= WIN_RATE_HIGH_BAND ? 1 : (current - WIN_RATE_LOW_BAND) / (WIN_RATE_HIGH_BAND - WIN_RATE_LOW_BAND));
            var D4 = 0.45, D5 = 0.44, R_LOW = 0.48, R_HIGH = 0.57;
            if (derivedSeries.length >= 4) {
                var recent = derivedSeries[derivedSeries.length - 1].rate50;
                var prev4 = derivedSeries[derivedSeries.length - 4].rate50;
                var isRising = recent > prev4 + D4;
                var isFalling = recent < prev4 - D4;
                // 메인 예측 최근 10경기 승률로 반픽/정픽 억제 (53%/50% — 예측 틀릴 때 정픽 더 쉽게 억제)
                if (current <= WIN_RATE_LOW_BAND && isRising) {
                    if (rate10Pct != null && rate10Pct <= 50) return 'mid_flat';
                    return 'low_rising';
                }
                if (current >= WIN_RATE_HIGH_BAND && isFalling) {
                    if (rate10Pct != null && rate10Pct >= 53) return 'mid_flat';
                    return 'high_falling';
                }
                if (isRising && ratioDynamic >= R_LOW) {
                    if (rate10Pct != null && rate10Pct <= 50) return 'mid_flat';
                    return 'low_rising';
                }
                if (isFalling && ratioDynamic <= R_HIGH) {
                    if (rate10Pct != null && rate10Pct >= 53) return 'mid_flat';
                    return 'high_falling';
                }
            }
            if (delta5 < -D5 && (ratioFixed >= 0.5 || ratioDynamic >= R_HIGH)) {
                if (rate10Pct != null && rate10Pct >= 53) return 'mid_flat';
                return 'high_falling';
            }
            if (delta5 > D5 && (ratioFixed <= 0.5 || ratioDynamic >= R_LOW)) {
                if (rate10Pct != null && rate10Pct <= 50) return 'mid_flat';
                return 'low_rising';
            }
            return 'mid_flat';
        }
        function getEffectiveWinRateDirectionZone(ph, id, currentRound) {
            var rawZone = getWinRateDirectionZone(ph);
            var lastZone = calcState[id] && calcState[id].last_win_rate_zone;
            var lockOnStreak = !!(calcState[id] && calcState[id].lock_direction_on_lose_streak);
            var loseStreak = typeof getLoseStreak === 'function' ? getLoseStreak(id) : 0;
            // 연패 중 방향 고정: 연패 >= 1이면 마지막 승 직후 zone 사용
            if (lockOnStreak && loseStreak >= 1) {
                var zoneOnWin = calcState[id] && calcState[id].last_win_rate_zone_on_win;
                if (zoneOnWin === 'low_rising' || zoneOnWin === 'high_falling' || zoneOnWin === 'mid_flat') return zoneOnWin;
                if (lastZone === 'low_rising' || lastZone === 'high_falling' || lastZone === 'mid_flat') return lastZone;
                return rawZone || lastZone;
            }
            // 쿨다운 제거 — 내림 감지 시 반대픽 즉시 전환
            if (rawZone === 'low_rising') {
                if (rawZone !== lastZone) {
                    calcState[id].last_win_rate_zone = rawZone;
                    calcState[id].last_win_rate_zone_change_round = currentRound;
                }
                calcState[id].last_win_rate_zone_on_win = rawZone;
                return rawZone;
            }
            if (rawZone === 'high_falling') {
                if (rawZone !== lastZone) {
                    calcState[id].last_win_rate_zone = rawZone;
                    calcState[id].last_win_rate_zone_change_round = currentRound;
                }
                calcState[id].last_win_rate_zone_on_win = rawZone;
                return rawZone;
            }
            if (rawZone && rawZone !== lastZone) {
                calcState[id].last_win_rate_zone = rawZone;
                calcState[id].last_win_rate_zone_change_round = currentRound;
            }
            var effective = rawZone || lastZone;
            if (effective) calcState[id].last_win_rate_zone_on_win = effective;
            // 승 직후 mid_flat이면 last_trend_direction 초기화 — 연패 중 반픽에서 이어지던 'down' 제거, 정픽으로 전환
            if (effective === 'mid_flat' && calcState[id]) calcState[id].last_trend_direction = null;
            return effective;
        }
        function renderWinRateDirectionPanel() {
            var tbody = document.getElementById('win-rate-direction-tbody');
            var wrap = document.getElementById('win-rate-direction-data');
            if (!tbody) return;
            var winRateDirWindow = (typeof WIN_RATE_DIRECTION_WINDOW !== 'undefined') ? WIN_RATE_DIRECTION_WINDOW : 100;
            // 메인 예측기 밑 결과 표와 동일한 데이터(predictionHistory)에서 롤링 100회 승률 계산 → 고점/저점/중간/방향 즉시 표시
            var vh = (typeof predictionHistory !== 'undefined' && Array.isArray(predictionHistory)) ? predictionHistory.filter(function(h) { return h && typeof h === 'object' && h.actual != null && h.actual !== ''; }) : [];
            if (vh.length > 600) vh = vh.slice(-600);
            var last10Panel = vh.slice(-10).filter(function(h) { return h.actual !== 'joker' && h.actual !== '조커'; });
            var rate10PctPanel = null;
            if (last10Panel.length >= 3) {
                var wins10Panel = last10Panel.filter(function(h) { return h.predicted === h.actual; }).length;
                rate10PctPanel = 100 * wins10Panel / last10Panel.length;
            }
            var derivedSeries = [];
            for (var i = winRateDirWindow - 1; i < vh.length; i++) {
                var w = vh.slice(i - (winRateDirWindow - 1), i + 1);
                var wins = w.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
                var losses = w.filter(function(h) { return h.actual !== 'joker' && h.predicted !== h.actual; }).length;
                var c = wins + losses;
                if (c > 0) derivedSeries.push({ round: vh[i].round, rate50: 100 * wins / c });
            }
            var high, low, mid, direction, directionClass, lastRound, current;
            var trendZoneLabel = '-';
            var delta5Text = '-';
            var vsHighText = '-';
            var vsLowText = '-';
            var refPickText = '기존 전략 유지';
            var refPickClass = 'color:#b0bec5;';
            if (derivedSeries.length > 0) {
                var rates = derivedSeries.map(function(x) { return x.rate50; });
                high = rates.reduce(function(a, b) { return a > b ? a : b; }, -Infinity);
                low = rates.reduce(function(a, b) { return a < b ? a : b; }, Infinity);
                mid = (high + low) / 2;
                direction = '정체';
                directionClass = '';
                if (derivedSeries.length >= 4) {
                    var recent = derivedSeries[derivedSeries.length - 1].rate50;
                    var prev = derivedSeries[derivedSeries.length - 4].rate50;
                    var d4 = 0.45;  // WIN_RATE_DIR_DELTA4와 동일 — 오름세 판정 보수적
                    if (recent > prev + d4) { direction = '오름'; directionClass = 'color:#81c784;'; }
                    else if (recent < prev - d4) { direction = '내림'; directionClass = 'color:#e57373;'; }
                }
                lastRound = derivedSeries[derivedSeries.length - 1].round;
                current = derivedSeries[derivedSeries.length - 1].rate50;
                // 5구간 전 대비 변화(%p)
                if (derivedSeries.length >= 6) {
                    var rate5Ago = derivedSeries[derivedSeries.length - 6].rate50;
                    var delta5 = current - rate5Ago;
                    delta5Text = (delta5 >= 0 ? '+' : '') + delta5.toFixed(1) + '%p';
                }
                // 고점/저점 대비 현재 위치
                if (high != null && low != null && high > low) {
                    var vsHigh = current - high;
                    var vsLow = current - low;
                    vsHighText = (vsHigh <= 0 ? '' : '+') + vsHigh.toFixed(1) + '%p';
                    vsLowText = (vsLow >= 0 ? '+' : '') + vsLow.toFixed(1) + '%p';
                }
                // 추세 구간: 저점 40~43% / 고점 57~60% 반영. 서버 getWinRateDirectionZone과 동일 로직
                if (derivedSeries.length >= 6 && high != null && low != null && high > low) {
                    var rate5Ago = derivedSeries[derivedSeries.length - 6].rate50;
                    var delta5 = current - rate5Ago;
                    var ratio = (current - low) / (high - low);
                    var d4 = 0.45, d5 = 0.44;
                    var WIN_RATE_LOW_BAND = 43, WIN_RATE_HIGH_BAND = 57;
                    var isRising = direction === '오름';
                    var isFalling = direction === '내림';
                    if (current <= WIN_RATE_LOW_BAND && isRising) {
                        trendZoneLabel = '저점 부근·오름';
                        refPickText = '정픽 참고';
                        refPickClass = 'color:#81c784;';
                        lastWinRateDirectionRef = '오름';
                    } else if (current >= WIN_RATE_HIGH_BAND && isFalling) {
                        trendZoneLabel = '고점 부근·내림';
                        refPickText = '반대픽 참고';
                        refPickClass = 'color:#e57373;';
                        lastWinRateDirectionRef = '내림';
                    } else if (isRising && ratio >= 0.48) {
                        trendZoneLabel = '오름·상위 구간';
                        refPickText = '정픽 참고';
                        refPickClass = 'color:#81c784;';
                        lastWinRateDirectionRef = '오름';
                    } else if (isFalling && ratio <= 0.57) {
                        trendZoneLabel = '내림·하위 구간';
                        refPickText = '반대픽 참고';
                        refPickClass = 'color:#e57373;';
                        lastWinRateDirectionRef = '내림';
                    } else if (delta5 < -d5 && ratio >= 0.57) {
                        trendZoneLabel = '고점 하락 구간';
                        refPickText = '반대픽 참고';
                        refPickClass = 'color:#e57373;';
                        lastWinRateDirectionRef = '내림';
                    } else if (delta5 > d5 && (current <= WIN_RATE_LOW_BAND || ratio < 0.48)) {
                        trendZoneLabel = '저점 상승 구간';
                        refPickText = '정픽 참고';
                        refPickClass = 'color:#81c784;';
                        lastWinRateDirectionRef = '오름';
                    } else {
                        trendZoneLabel = '중간·횡보';
                        // 방향별 일치: 내림→(내림 참고), 오름→(오름 참고), 정체→전회차 방향(오름/내림) 참고
                        var suffix = direction === '내림' ? ' (내림 참고)' : direction === '오름' ? ' (오름 참고)' : (lastWinRateDirectionRef === '오름' ? ' (오름 참고)' : lastWinRateDirectionRef === '내림' ? ' (내림 참고)' : '');
                        refPickText = '기존 전략 유지' + suffix;
                        refPickClass = 'color:#b0bec5;';
                    }
                    if (refPickText === '반대픽 참고' && rate10PctPanel != null && rate10PctPanel >= 53) { refPickText = '기존 전략 유지 (최근 10회 예측 양호)'; refPickClass = 'color:#b0bec5;'; }
                    if (refPickText === '정픽 참고' && rate10PctPanel != null && rate10PctPanel <= 50) { refPickText = '기존 전략 유지 (최근 10회 예측 저조)'; refPickClass = 'color:#b0bec5;'; }
                }
            } else {
                var v100cur = vh.slice(-winRateDirWindow);
                var hit100cur = v100cur.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
                var loss100cur = v100cur.filter(function(h) { return h.actual !== 'joker' && h.predicted !== h.actual; }).length;
                var count100cur = hit100cur + loss100cur;
                current = count100cur > 0 ? 100 * hit100cur / count100cur : null;
                high = low = mid = null;
                direction = '-';
                directionClass = '';
                lastRound = null;
            }
            var lowVal = low != null ? low : 0;
            var highVal = high != null ? high : 100;
            var range = Math.max(highVal - lowVal, 1);
            var pctCurrent = current != null ? Math.max(0, Math.min(100, (current - lowVal) / range * 100)) : 50;
            var barHtml = '';
            if (current != null) {
                var segW = (current - lowVal) / range * 100;
                barHtml = '<div style="margin:10px 0;padding:4px 0;">' +
                    '<div style="font-size:0.8em;color:#888;margin-bottom:4px;">0% — 저점 ' + (lowVal.toFixed(0)) + '% — 현재 — 고점 ' + (highVal.toFixed(0)) + '% — 100%</div>' +
                    '<div style="height:24px;background:#333;border-radius:4px;position:relative;overflow:hidden;">' +
                    '<div style="position:absolute;left:0;top:0;bottom:0;width:' + (pctCurrent) + '%;background:linear-gradient(90deg,#37474f 0%,#546e7a 100%);border-radius:4px 0 0 4px;"></div>' +
                    '<div style="position:absolute;left:' + (pctCurrent) + '%;top:0;bottom:0;width:4px;background:#fff;border-radius:2px;box-shadow:0 0 4px #000;"></div>' +
                    '</div></div>';
            }
            tbody.innerHTML =
                '<tr><td style="color:#b0bec5;">현재 100회 승률</td><td><strong>' + (current != null ? current.toFixed(1) + '%' : '-') + '</strong>' + (derivedSeries.length === 0 && current != null ? ' <span style="color:#888;font-size:0.85em">(100회 미만)</span>' : '') + '</td></tr>' +
                '<tr><td style="color:#b0bec5;">기록 최고점</td><td style="color:#81c784;">' + (high != null ? high.toFixed(1) + '%' : '-') + '</td></tr>' +
                '<tr><td style="color:#b0bec5;">기록 최저점</td><td style="color:#e57373;">' + (low != null ? low.toFixed(1) + '%' : '-') + '</td></tr>' +
                '<tr><td style="color:#b0bec5;">중간 (고·저)</td><td>' + (mid != null ? mid.toFixed(1) + '%' : '-') + '</td></tr>' +
                '<tr><td style="color:#b0bec5;">방향</td><td style="' + directionClass + ' font-weight:bold;">' + direction + '</td></tr>' +
                '<tr><td style="color:#b0bec5;">추세 구간</td><td style="font-weight:bold;">' + trendZoneLabel + '</td></tr>' +
                '<tr><td style="color:#b0bec5;">5구간 전 대비</td><td>' + delta5Text + '</td></tr>' +
                '<tr><td style="color:#b0bec5;">고점 대비</td><td>' + vsHighText + '</td></tr>' +
                '<tr><td style="color:#b0bec5;">저점 대비</td><td>' + vsLowText + '</td></tr>' +
                '<tr><td style="color:#b0bec5;">참고 픽</td><td style="' + refPickClass + ' font-weight:bold;">' + refPickText + '</td></tr>' +
                '<tr><td style="color:#888;font-size:0.9em;">기준 회차</td><td style="color:#888;">' + (lastRound != null ? String(lastRound) : '-') + '</td></tr>';
            if (wrap && barHtml) {
                var oldBar = wrap.querySelector('.win-rate-direction-bar');
                if (oldBar) oldBar.remove();
                var barEl = document.createElement('div');
                barEl.className = 'win-rate-direction-bar';
                barEl.innerHTML = barHtml;
                wrap.appendChild(barEl);
            }
        }
        document.getElementById('pause-guide-calc')?.addEventListener('click', function() { renderPauseGuideTable(); });
        function updateCalcStatus(id) {
            try {
            const statusId = 'calc-' + id + '-status';
            const el = document.getElementById(statusId);
            if (!el) return;
            const state = calcState[id];
            if (!state) return;
            var pauseLowEl = document.getElementById('calc-' + id + '-pause-low-win-rate');
            var pauseThrEl = document.getElementById('calc-' + id + '-pause-win-rate-threshold');
            var pauseEnabled = !!(pauseLowEl && pauseLowEl.checked);
            var thrPause = (pauseThrEl && !isNaN(parseFloat(pauseThrEl.value))) ? Math.max(0, Math.min(100, parseFloat(pauseThrEl.value))) : 45;
            if (calcState[id]) { calcState[id].pause_low_win_rate_enabled = pauseEnabled; calcState[id].pause_win_rate_threshold = thrPause; }
            if (state.paused && pauseEnabled) {
                var rate15 = getCalcRecent15WinRate(id);
                var resumeThr = Math.min(100, thrPause + 3);  // 이력: 멈춤 해제는 기준+3% 초과일 때만
                if (rate15 > resumeThr) state.paused = false;
            }
            el.className = 'calc-status';
            if (state.running) {
                el.classList.add('running');
                var statusTxt = '실행중';
                if (!!(state.reverse)) statusTxt += ' · 반픽';
                if (!!(state.win_rate_reverse)) statusTxt += ' · 승률반픽';
                if (!!(state.lose_streak_reverse)) statusTxt += ' · 연패반픽';
                if (!!(state.streak_suppress_reverse)) statusTxt += ' · 줄5억제';
                if (!!(state.shape_prediction) && !!(state.shape_prediction_reverse)) statusTxt += ' · 반픽중';
                el.textContent = statusTxt;
            } else if (state.timer_completed) {
                el.classList.add('timer-done');
                el.textContent = '타이머 완료';
            } else if (state.history && state.history.length > 0) {
                el.classList.add('stopped');
                el.textContent = '정지중';
            } else {
                el.classList.add('idle');
                el.textContent = '대기중';
            }
            try {
                    const bettingRoundEl = document.getElementById('calc-' + id + '-current-round');
                    const predictionRoundEl = document.getElementById('calc-' + id + '-prediction-round');
                    const bettingCardEl = document.getElementById('calc-' + id + '-current-card');
                    const predictionCardEl = document.getElementById('calc-' + id + '-prediction-card');
                    if (!bettingCardEl || !predictionCardEl) return;
                    if (state.running && lastPrediction && lastPrediction.round != null) {
                        var roundFull = lastPrediction.round != null ? String(lastPrediction.round) : '-';
                        var iconType = getRoundIconType(lastPrediction.round);
                        var roundLineHtml = '<span class="calc-round-badge calc-round-' + iconType + '">' + roundFull + '회 ' + getRoundIconHtml(lastPrediction.round) + '</span>';
                        if (bettingRoundEl) { bettingRoundEl.innerHTML = roundLineHtml; bettingRoundEl.className = 'calc-round-line'; }
                        if (predictionRoundEl) { predictionRoundEl.innerHTML = roundLineHtml; predictionRoundEl.className = 'calc-round-line'; }
                        if (lastIs15Joker || !lastPrediction.value || lastPrediction.value === '') {
                            predictionCardEl.textContent = '보류';
                            predictionCardEl.className = 'calc-current-card calc-card-prediction card-hold';
                            predictionCardEl.title = lastIs15Joker ? '15번 카드 조커 · 배팅하지 마세요' : '예측 대기 중';
                            bettingCardEl.textContent = '보류';
                            bettingCardEl.className = 'calc-current-card calc-card-betting card-hold';
                            bettingCardEl.title = lastIs15Joker ? '15번 카드 조커 · 배팅하지 마세요' : '예측 대기 중';
                            var betAmt = lastIs15Joker ? 0 : ((lastPrediction && lastPrediction.round != null && typeof getBetForRound === 'function') ? getBetForRound(id, lastPrediction.round) : 0);
                            postCurrentPickIfChanged(parseInt(id, 10) || 1, { pickColor: null, round: lastPrediction && lastPrediction.round != null ? lastPrediction.round : null, probability: null, suggested_amount: (lastIs15Joker || betAmt <= 0) ? null : betAmt });
                        } else {
                        // 배팅중인 회차는 이미 정한 계산기 픽만 유지 — lastPrediction이 잠깐 예측기로 바뀌어도 저장된 픽으로 POST/표시해 예측기 픽으로 배팅 나가는 것 방지
                        var curRound = lastPrediction && lastPrediction.round != null ? Number(lastPrediction.round) : null;
                        var saved = (calcState[id].lastBetPickForRound && Number(calcState[id].lastBetPickForRound.round) === curRound) ? calcState[id].lastBetPickForRound : null;
                        // 반픽/승률반픽 등 판정용 — 모양 옵션과 중복 사용 시 shapeOnly 블록에서 필요
                        var r15Card = null, blendedCard = null;
                        try {
                            var vh = (typeof predictionHistory !== 'undefined' && Array.isArray(predictionHistory)) ? predictionHistory.filter(function(h) { return h && typeof h === 'object'; }) : [];
                            var v15 = vh.slice(-15), v30 = vh.slice(-30), v100 = vh.slice(-100);
                            var hit15r = v15.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
                            var loss15 = v15.filter(function(h) { return h.actual !== 'joker' && h.predicted !== h.actual; }).length;
                            var c15 = hit15r + loss15; r15Card = c15 > 0 ? 100 * hit15r / c15 : 50;
                            var hit30r = v30.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
                            var loss30 = v30.filter(function(h) { return h.actual !== 'joker' && h.predicted !== h.actual; }).length;
                            var c30 = hit30r + loss30; var r30 = c30 > 0 ? 100 * hit30r / c30 : 50;
                            var hit100r = v100.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
                            var loss100 = v100.filter(function(h) { return h.actual !== 'joker' && h.predicted !== h.actual; }).length;
                            var c100 = hit100r + loss100; var r100 = c100 > 0 ? 100 * hit100r / c100 : 50;
                            blendedCard = 0.65 * r15Card + 0.25 * r30 + 0.10 * r100;
                        } catch (e2) {}
                        var runLenCard = getCurrentResultRunLength(typeof predictionHistory !== 'undefined' && Array.isArray(predictionHistory) ? predictionHistory : []);
                        var noRevByMain15Card = (r15Card == null || r15Card < 53);
                        var noRevByStreak5Card = !(calcState[id].streak_suppress_reverse && runLenCard >= 5);
                        var shapePredOn = !!(document.getElementById('calc-' + id + '-shape-prediction') && document.getElementById('calc-' + id + '-shape-prediction').checked);
                        // 상단 예측픽: lastPrediction.value 사용
                        var predictionText = lastPrediction.value;
                        var predColorNorm = normalizePickColor(lastPrediction.color);
                        var predictionIsRed = (predColorNorm === '빨강' || predColorNorm === '검정') ? (predColorNorm === '빨강') : (predictionText === '정');
                        var bettingText, bettingIsRed;
                        if (saved && (saved.value === '정' || saved.value === '꺽')) {
                            bettingText = saved.value;
                            bettingIsRed = !!saved.isRed;
                        } else {
                            bettingText = predictionText;
                            bettingIsRed = predictionIsRed;
                            const rev = !!(calcState[id] && calcState[id].reverse);
                            if (rev) { bettingText = bettingText === '정' ? '꺽' : '정'; bettingIsRed = !bettingIsRed; }
                            var blended = blendedCard;
                            const useWinRateRevCard = !!(calcState[id] && calcState[id].win_rate_reverse);
                            var shapeWrCard = (typeof getShape50WinRate === 'function') ? getShape50WinRate() : null;
                            var wrThrCardEl = document.getElementById('calc-' + id + '-win-rate-threshold');
                            var wrThrCard = (wrThrCardEl && !isNaN(parseFloat(wrThrCardEl.value))) ? Math.max(0, Math.min(100, parseFloat(wrThrCardEl.value))) : 50;
                            if (useWinRateRevCard && shapeWrCard != null && shapeWrCard <= wrThrCard && noRevByMain15Card && noRevByStreak5Card) { bettingText = bettingText === '정' ? '꺽' : '정'; bettingIsRed = !bettingIsRed; }
                            var useLoseStreakRevCard = !!(calcState[id] && calcState[id].lose_streak_reverse);
                            var loseStreakThrCardEl = document.getElementById('calc-' + id + '-lose-streak-reverse-threshold');
                            var loseStreakThrCard = (loseStreakThrCardEl && !isNaN(parseFloat(loseStreakThrCardEl.value))) ? Math.max(0, Math.min(100, parseFloat(loseStreakThrCardEl.value))) : (calcState[id] != null && typeof calcState[id].lose_streak_reverse_threshold === 'number' ? calcState[id].lose_streak_reverse_threshold : 48);
                            if (useLoseStreakRevCard && getLoseStreak(id) >= getLoseStreakMin(id) && typeof blendedCard === 'number' && blendedCard <= loseStreakThrCard && noRevByMain15Card && noRevByStreak5Card) { bettingText = bettingText === '정' ? '꺽' : '정'; bettingIsRed = !bettingIsRed; }
                            var winRateDirRevCardEl = document.getElementById('calc-' + id + '-win-rate-direction-reverse');
                            var useWinRateDirRevCard = !!(winRateDirRevCardEl && winRateDirRevCardEl.checked) || !!(calcState[id] && calcState[id].win_rate_direction_reverse);
                            if (useWinRateDirRevCard && noRevByStreak5Card && typeof getEffectiveWinRateDirectionZone === 'function') {
                                var phCard = (typeof predictionHistory !== 'undefined' && Array.isArray(predictionHistory)) ? predictionHistory : [];
                                var zoneCard = getEffectiveWinRateDirectionZone(phCard, id, curRound);
                                if (zoneCard === 'high_falling') { bettingText = bettingText === '정' ? '꺽' : '정'; bettingIsRed = !bettingIsRed; calcState[id].last_trend_direction = 'down'; }
                                else if (zoneCard === 'low_rising') { calcState[id].last_trend_direction = 'up'; }
                                else if (zoneCard === 'mid_flat' && calcState[id].last_trend_direction === 'down') { bettingText = bettingText === '정' ? '꺽' : '정'; bettingIsRed = !bettingIsRed; }
                            }
                            var shapePredRev = !!(document.getElementById('calc-' + id + '-shape-prediction-reverse') && document.getElementById('calc-' + id + '-shape-prediction-reverse').checked);
                            var shapePredRevThrEl = document.getElementById('calc-' + id + '-shape-prediction-reverse-threshold');
                            var shapePredRevThr = (shapePredRevThrEl && !isNaN(parseFloat(shapePredRevThrEl.value))) ? Math.max(0, Math.min(100, parseFloat(shapePredRevThrEl.value))) : 50;
                            if (shapePredOn && shapePredRev && (typeof getShapePredictionWinRate10 === 'function')) {
                                var sp10 = getShapePredictionWinRate10(id);
                                if (sp10 != null && sp10 <= shapePredRevThr) { bettingText = bettingText === '정' ? '꺽' : '정'; bettingIsRed = !bettingIsRed; }
                            }
                            if (curRound != null) { calcState[id].lastBetPickForRound = { round: curRound, value: bettingText, isRed: bettingIsRed }; }
                        }
                        // 모양: 가장 최근 다음 픽에만 배팅 — 값 있으면 그 픽을 기준으로, 반픽/승률반픽 등 적용 후 표시. 없으면 보류
                        var predBeforeShapeOnly = bettingText;
                        var predBeforeShapeOnlyIsRed = bettingIsRed;
                        var shapeOnly = !!(calcState[id] && calcState[id].shape_only_latest_next_pick);
                        var latestNext = (typeof lastPongChunkDebug !== 'undefined' && lastPongChunkDebug && (lastPongChunkDebug.latest_next_pick === '정' || lastPongChunkDebug.latest_next_pick === '꺽')) ? lastPongChunkDebug.latest_next_pick : null;
                        if (shapeOnly) {
                            if (latestNext) {
                                bettingText = latestNext;
                                var card15 = (allResults && allResults.length >= 15 && typeof parseCardValue === 'function') ? parseCardValue(allResults[14].result || '') : null;
                                var is15Red = card15 ? !!card15.isRed : false;
                                bettingIsRed = (latestNext === '정') ? is15Red : !is15Red;
                                // 모양 픽에 반픽/승률반픽/연패반픽/승률방향 반픽 적용 (다른 옵션과 중복 사용 가능)
                                const rev = !!(calcState[id] && calcState[id].reverse);
                                if (rev) { bettingText = bettingText === '정' ? '꺽' : '정'; bettingIsRed = !bettingIsRed; }
                                const useWinRateRevCard = !!(calcState[id] && calcState[id].win_rate_reverse);
                                var shapeWrCard = (typeof getShape50WinRate === 'function') ? getShape50WinRate() : null;
                                var wrThrCardEl = document.getElementById('calc-' + id + '-win-rate-threshold');
                                var wrThrCard = (wrThrCardEl && !isNaN(parseFloat(wrThrCardEl.value))) ? Math.max(0, Math.min(100, parseFloat(wrThrCardEl.value))) : 50;
                                if (useWinRateRevCard && shapeWrCard != null && shapeWrCard <= wrThrCard && noRevByStreak5Card) { bettingText = bettingText === '정' ? '꺽' : '정'; bettingIsRed = !bettingIsRed; }
                                var useLoseStreakRevCard = !!(calcState[id] && calcState[id].lose_streak_reverse);
                                var loseStreakThrCardEl = document.getElementById('calc-' + id + '-lose-streak-reverse-threshold');
                                var loseStreakThrCard = (loseStreakThrCardEl && !isNaN(parseFloat(loseStreakThrCardEl.value))) ? Math.max(0, Math.min(100, parseFloat(loseStreakThrCardEl.value))) : (calcState[id] != null && typeof calcState[id].lose_streak_reverse_threshold === 'number' ? calcState[id].lose_streak_reverse_threshold : 48);
                                if (useLoseStreakRevCard && getLoseStreak(id) >= getLoseStreakMin(id) && typeof blendedCard === 'number' && blendedCard <= loseStreakThrCard && noRevByMain15Card && noRevByStreak5Card) { bettingText = bettingText === '정' ? '꺽' : '정'; bettingIsRed = !bettingIsRed; }
                                var useWinRateDirRevCard = !!(document.getElementById('calc-' + id + '-win-rate-direction-reverse') && document.getElementById('calc-' + id + '-win-rate-direction-reverse').checked) || !!(calcState[id] && calcState[id].win_rate_direction_reverse);
                                if (useWinRateDirRevCard && noRevByStreak5Card && typeof getEffectiveWinRateDirectionZone === 'function') {
                                    var phCard = (typeof predictionHistory !== 'undefined' && Array.isArray(predictionHistory)) ? predictionHistory : [];
                                    var zoneCard = getEffectiveWinRateDirectionZone(phCard, id, curRound);
                                    if (zoneCard === 'high_falling') { bettingText = bettingText === '정' ? '꺽' : '정'; bettingIsRed = !bettingIsRed; calcState[id].last_trend_direction = 'down'; }
                                    else if (zoneCard === 'low_rising') { calcState[id].last_trend_direction = 'up'; }
                                    else if (zoneCard === 'mid_flat' && calcState[id].last_trend_direction === 'down') { bettingText = bettingText === '정' ? '꺽' : '정'; bettingIsRed = !bettingIsRed; }
                                }
                                if (curRound != null) { calcState[id].lastBetPickForRound = { round: curRound, value: bettingText, isRed: bettingIsRed }; }
                            } else {
                                bettingText = '보류';
                                bettingIsRed = false;
                                if (curRound != null && calcState[id].lastBetPickForRound && Number(calcState[id].lastBetPickForRound.round) === curRound) {
                                    calcState[id].lastBetPickForRound = { round: curRound, value: '보류', isRed: false };
                                }
                            }
                        }
                        predictionCardEl.textContent = predictionText;
                        predictionCardEl.className = 'calc-current-card calc-card-prediction card-' + (predictionIsRed ? 'jung' : 'kkuk');
                        predictionCardEl.title = '';
                        bettingCardEl.textContent = bettingText;
                        bettingCardEl.className = 'calc-current-card calc-card-betting' + (bettingText === '보류' ? ' card-hold' : ' card-' + (bettingIsRed ? 'jung' : 'kkuk'));
                        bettingCardEl.title = bettingText === '보류' ? '모양 옵션: 픽 불일치 또는 값 없음' : '';
                        // 매크로: 1행(배팅중 행)과 동일한 출처 — getBetForRound 사용 (getCalcResult 대신). 15번 카드 조커 시 금액 0
                        var betAmt = (effectivePausedForRound(id) || (shapeOnly && bettingText === '보류') || (typeof lastIs15Joker !== 'undefined' && lastIs15Joker)) ? 0 : (curRound != null && typeof getBetForRound === 'function' ? getBetForRound(id, curRound) : 0);
                        var suggestedAmt = (bettingText === '보류' && shapeOnly) || (typeof lastIs15Joker !== 'undefined' && lastIs15Joker) ? null : (betAmt > 0 ? betAmt : null);
                        var postPickColor = (bettingText === '보류' && shapeOnly) ? null : (bettingIsRed ? 'RED' : 'BLACK');
                        postCurrentPickIfChanged(parseInt(id, 10) || 1, { pickColor: postPickColor, round: lastPrediction && lastPrediction.round != null ? lastPrediction.round : null, probability: typeof predProb === 'number' && !isNaN(predProb) ? predProb : null, suggested_amount: suggestedAmt });
                        if (lastPrediction && lastPrediction.round != null) {
                            savedBetPickByRound[Number(lastPrediction.round)] = { value: bettingText, isRed: bettingIsRed };
                            var sbKeys = Object.keys(savedBetPickByRound).map(Number).filter(function(k) { return !isNaN(k); }).sort(function(a,b) { return a - b; });
                            var curRoundNum = lastPrediction && lastPrediction.round != null ? Number(lastPrediction.round) : null;
                            var pendingRounds = {};
                            CALC_IDS.forEach(function(cid) {
                                (calcState[cid].history || []).forEach(function(h) { if (h && h.actual === 'pending' && h.round != null) pendingRounds[Number(h.round)] = true; });
                            });
                            var evictable = sbKeys.filter(function(k) { return k !== curRoundNum && !pendingRounds[k]; });
                            var evictCount = Math.max(0, sbKeys.length - SAVED_BET_PICK_MAX);
                            for (var ei = 0; ei < evictCount && ei < evictable.length; ei++) {
                                delete savedBetPickByRound[evictable[ei]];
                            }
                            // 표 1행(배팅중) 픽·no_bet 보정: ensurePendingRow 등에서 예측픽으로 채워진 행이 있으면 배팅중 픽으로 덮어씀
                            var pendingRow = (calcState[id].history || []).find(function(h) { return h && Number(h.round) === Number(lastPrediction.round) && h.actual === 'pending'; });
                            if (pendingRow && (bettingText === '정' || bettingText === '꺽') && !(typeof lastIs15Joker !== 'undefined' && lastIs15Joker)) {
                                var targetColor = bettingIsRed ? '빨강' : '검정';
                                if (pendingRow.predicted !== bettingText || (pendingRow.pickColor || pendingRow.pick_color) !== targetColor) {
                                    pendingRow.predicted = bettingText;
                                    pendingRow.pickColor = targetColor;
                                }
                                if (pendingRow.no_bet === true && !effectivePausedForRound(id)) { pendingRow.no_bet = false; pendingRow.betAmount = (typeof getBetForRound === 'function' ? getBetForRound(id, Number(lastPrediction.round)) : 0); }
                            }
                            // 배팅중 뜨자마자 표에 픽+배팅금액 행 추가 (결과 대기)
                            var firstBet = calcState[id].first_bet_round || 0;
                            var roundNum = Number(lastPrediction.round);
                            if (firstBet > 0 && roundNum < firstBet) { /* 첫배팅 회차 전이면 스킵 */ } else {
                            var r = getCalcResult(id);
                            var hasRound = calcState[id].history.some(function(h) { return h && Number(h.round) === roundNum; });
                            var betForThisRound = getBetForRound(id, roundNum);
                            var shapeOnlyNoBet = !!(shapeOnly && bettingText === '보류');
                            var joker15NoBet = !!(typeof lastIs15Joker !== 'undefined' && lastIs15Joker);
                            if (!hasRound && (betForThisRound > 0 || effectivePausedForRound(id) || shapeOnlyNoBet || joker15NoBet)) {
                                var isNoBet = !!effectivePausedForRound(id) || shapeOnlyNoBet || joker15NoBet;
                                var amt = isNoBet ? 0 : betForThisRound;
                                var predForHistory = joker15NoBet ? '보류' : ((shapeOnlyNoBet && (predBeforeShapeOnly === '정' || predBeforeShapeOnly === '꺽')) ? predBeforeShapeOnly : bettingText);
                                var pickColorForHistory = joker15NoBet ? null : ((shapeOnlyNoBet && (predBeforeShapeOnly === '정' || predBeforeShapeOnly === '꺽')) ? (predBeforeShapeOnlyIsRed ? '빨강' : '검정') : (bettingIsRed ? '빨강' : '검정'));
                                calcState[id].history.push({ round: roundNum, predicted: predForHistory, pickColor: pickColorForHistory, betAmount: amt, no_bet: isNoBet, actual: 'pending', warningWinRate: typeof blended === 'number' ? blended : null });
                                calcState[id].history = dedupeCalcHistoryByRound(calcState[id].history);
                                saveCalcStateToServer();
                                updateCalcDetail(id);
                            }
                            }
                        }
                        }
                    } else {
                        if (bettingRoundEl) bettingRoundEl.textContent = '';
                        if (predictionRoundEl) predictionRoundEl.textContent = '';
                        bettingCardEl.textContent = '';
                        bettingCardEl.className = 'calc-current-card calc-card-betting';
                        predictionCardEl.textContent = '';
                        predictionCardEl.className = 'calc-current-card calc-card-prediction';
                        calcState[id].lastBetPickForRound = null;
                    }
                } catch (cardErr) { console.warn('updateCalcStatus card', id, cardErr); }
            var logEl = document.getElementById('calc-' + id + '-shape-prediction-log');
            if (logEl && calcState[id] && calcState[id].shape_prediction) {
                var dbg = calcState[id].pending_shape_debug;
                if (dbg && typeof dbg === 'object') {
                    var parts = ['phase: ' + (dbg.phase || '-'), 'chunk_shape: ' + (dbg.chunk_shape || '-'), 'pred: ' + (dbg.pred || '-'), 'prob: ' + (dbg.prob != null ? dbg.prob : '-')];
                    logEl.textContent = parts.join(' | ');
                } else {
                    logEl.textContent = '—';
                }
            } else if (logEl) {
                logEl.textContent = '—';
            }
            } catch (e) { console.warn('updateCalcStatus', id, e); }
        }
        function updateCalcSummary(id) {
            try {
            const summaryId = 'calc-' + id + '-summary';
            const el = document.getElementById(summaryId);
            if (!el) return;
            const state = calcState[id];
            if (!state) return;
            const hist = state.history || [];
            const elapsedStr = state.running && typeof formatMmSs === 'function' ? formatMmSs(state.elapsed || 0) : '-';
            const timerNote = state.timer_completed ? '<span class="calc-timer-note" style="color:#64b5f6;font-weight:bold;grid-column:1/-1">타이머 완료</span>' : '';
            if (hist.length === 0) {
                try { var __c = window.__calcSummaryCache; if (__c && __c[id]) delete __c[id]; } catch (e) {}
                var targetNoteEmpty = '';
                const targetEnabledEl = document.getElementById('calc-' + id + '-target-enabled');
                const targetAmountEl = document.getElementById('calc-' + id + '-target-amount');
                const targetEnabled = !!(targetEnabledEl && targetEnabledEl.checked);
                const targetAmount = Math.max(0, parseInt(targetAmountEl?.value, 10) || 0);
                if (targetEnabled && targetAmount > 0) targetNoteEmpty = '<span class="calc-timer-note" style="grid-column:1/-1">목표금액: ' + targetAmount.toLocaleString() + '원 / 목표까지: ' + targetAmount.toLocaleString() + '원 남음</span>';
                el.innerHTML = '<div class="calc-summary-grid">' + timerNote + targetNoteEmpty +
                    '<span class="label">보유자산</span><span class="value">-</span>' +
                    '<span class="label">순익</span><span class="value">-</span>' +
                    '<span class="label">배팅중</span><span class="value">-</span>' +
                    '<span class="label">경과</span><span class="value">' + elapsedStr + '</span></div>';
                updateCalcBetCopyLine(id);
                updateCalcStatus(id);
                return;
            }
            const r = getCalcResult(id);
            const profitStr = (r.profit >= 0 ? '+' : '') + r.profit.toLocaleString() + '원';
            const profitClass = r.profit > 0 ? 'profit-plus' : (r.profit < 0 ? 'profit-minus' : '');
            var betDisplay = (effectivePausedForRound(id) || (typeof lastIs15Joker !== 'undefined' && lastIs15Joker) ? '-' : (r.currentBet.toLocaleString() + '원'));
            // 보유자산·순익·배팅중이 그대로면 그리드 전체를 다시 쓰지 않고 경과만 갱신 (깜빡임 방지)
            try {
                var cache = window.__calcSummaryCache = window.__calcSummaryCache || {};
                if (cache[id] && cache[id].cap === r.cap && cache[id].profit === r.profit && cache[id].betDisplay === betDisplay) {
                    var grid = el.querySelector('.calc-summary-grid');
                    if (grid) {
                        var valueSpans = grid.querySelectorAll('span.value');
                        if (valueSpans.length >= 4) valueSpans[3].textContent = elapsedStr;
                    }
                    updateCalcStatus(id);
                    return;
                }
                cache[id] = { cap: r.cap, profit: r.profit, betDisplay: betDisplay };
            } catch (skipErr) {}
            var targetNote = '';
            const targetEnabledEl = document.getElementById('calc-' + id + '-target-enabled');
            const targetAmountEl = document.getElementById('calc-' + id + '-target-amount');
            const targetEnabled = !!(targetEnabledEl && targetEnabledEl.checked);
            const targetAmount = Math.max(0, parseInt(targetAmountEl?.value, 10) || 0);
            if (targetEnabled && targetAmount > 0) {
                const remain = targetAmount - r.profit;
                if (remain <= 0) targetNote = '<span class="calc-timer-note" style="color:#81c784;font-weight:bold;grid-column:1/-1">목표금액: ' + targetAmount.toLocaleString() + '원 / 달성</span>';
                else targetNote = '<span class="calc-timer-note" style="grid-column:1/-1">목표금액: ' + targetAmount.toLocaleString() + '원 / 목표까지: ' + remain.toLocaleString() + '원 남음</span>';
            }
            el.innerHTML = '<div class="calc-summary-grid">' + timerNote + targetNote +
                '<span class="label">보유자산</span><span class="value">' + r.cap.toLocaleString() + '원</span>' +
                '<span class="label">순익</span><span class="value ' + profitClass + '">' + profitStr + '</span>' +
                '<span class="label">배팅중</span><span class="value">' + betDisplay + '</span>' +
                '<span class="label">경과</span><span class="value">' + elapsedStr + '</span></div>';
            updateCalcBetCopyLine(id, (effectivePausedForRound(id) || (typeof lastIs15Joker !== 'undefined' && lastIs15Joker)) ? 0 : r.currentBet);
            updateCalcStatus(id);
            } catch (e) { console.warn('updateCalcSummary', id, e); }
        }
        function updateCalcBetCopyLine(id, currentBetVal) {
            try {
                var el = document.getElementById('calc-' + id + '-bet-copy-line');
                if (!el) return;
                var state = calcState[id];
                var round = (state && state.pending_round) ? state.pending_round : (typeof lastPrediction !== 'undefined' && lastPrediction && lastPrediction.round ? lastPrediction.round : null);
                var amount = (currentBetVal !== undefined && currentBetVal > 0) ? currentBetVal : (state && state.running ? (getCalcResult(id).currentBet || 0) : 0);
                if (round == null || amount <= 0) {
                    el.innerHTML = '—';
                    return;
                }
                var roundStr = String(round) + '회 ';
                var iconHtml = getRoundIconHtml(round);
                var amountPlain = String(amount);
                var amountDisplay = amount.toLocaleString() + '원';
                el.innerHTML = roundStr + iconHtml + ' <span class="calc-bet-copy-amount" data-amount="' + amountPlain + '" title="클릭하면 금액 복사">' + amountDisplay + '</span> <span class="calc-bet-copy-hint">[클릭 복사]</span>';
            } catch (e) { console.warn('updateCalcBetCopyLine', id, e); }
        }
        function appendCalcLog(id) {
            const state = calcState[id];
            if (!state || !state.history || state.history.length === 0) return;
            const now = new Date();
            const dateStr = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0') + '-' + String(now.getDate()).padStart(2, '0') + '_' + String(now.getHours()).padStart(2, '0') + String(now.getMinutes()).padStart(2, '0');
            const r = getCalcResult(id);
            const rev = document.getElementById('calc-' + id + '-reverse')?.checked;
            const baseIn = parseFloat(document.getElementById('calc-' + id + '-base')?.value) || 10000;
            const pickType = rev ? '반픽' : '정픽';
            const logLine = dateStr + '_계산기' + id + '_' + pickType + '_배팅' + baseIn + '원_순익' + (r.profit >= 0 ? '+' : '') + r.profit + '원_승' + r.wins + '패' + r.losses + '_승률' + r.winRate + '%';
            const histCopy = JSON.parse(JSON.stringify(state.history || []));
            betCalcLog.unshift({ line: logLine, calcId: String(id), history: histCopy });
            saveBetCalcLog();
            renderBetCalcLog();
        }
        function updateCalcDetail(id) {
            try {
            // 서버 prediction_history로 계산기 히스토리 동기화 (updateCalcDetail 실행 전에 강제 동기화)
            (function syncCalcHistoryFromServerPredictionBeforeRender() {
                if (!Array.isArray(predictionHistory) || predictionHistory.length === 0) return;
                var byRound = {};
                predictionHistory.forEach(function(p) {
                    if (p && typeof p === 'object' && p.round != null && p.actual != null && p.actual !== '') {
                        byRound[Number(p.round)] = { actual: p.actual };
                    }
                });
                var currentPredRound = (typeof lastPrediction !== 'undefined' && lastPrediction && lastPrediction.round != null) ? Number(lastPrediction.round) : null;
                var running = !!(calcState[id] && calcState[id].running);
                var changed = false;
                var hist = calcState[id].history || [];
                hist.forEach(function(h) {
                    if (!h) return;
                    if (h.actual !== 'pending' && h.actual != null && h.actual !== '') return;
                    var r = Number(h.round);
                    if (isNaN(r)) return;
                    if (running && r === currentPredRound) return;
                    var fromServer = byRound[r];
                    if (!fromServer) return;
                    h.actual = fromServer.actual;
                    changed = true;
                });
                if (changed) {
                    calcState[id].history = dedupeCalcHistoryByRound(hist);
                    try { saveCalcStateToServer(); } catch (e) {}
                }
            })();
            const streakId = 'calc-' + id + '-streak';
            const statsId = 'calc-' + id + '-stats';
            const tableWrapId = 'calc-' + id + '-round-table-wrap';
            const streakEl = document.getElementById(streakId);
            const statsEl = document.getElementById(statsId);
            const tableWrap = document.getElementById(tableWrapId);
            if (!streakEl || !statsEl) return;
            const state = calcState[id];
            if (!state) return;
            const hist = state.history || [];
            if (hist.length === 0) {
                streakEl.textContent = '경기결과 (최근 30회): -';
                statsEl.textContent = '최대연승: - | 최대연패: - | 모양승률: - | 표승률: - | 15회승률: - | 모양판별승률: -';
                if (tableWrap) tableWrap.innerHTML = '';
                return;
            }
            const r = getCalcResult(id);
            const usedHist = dedupeCalcHistoryByRound(hist);
            // completedHist: pending이 아니고 actual이 있는 것만 (서버 동기화 후에는 실제 결과가 있음)
            const completedHist = usedHist.filter(function(h) { return h && h.actual !== 'pending' && h.actual != null && h.actual !== '' && typeof h.predicted !== 'undefined'; });
            const oddsIn = parseFloat(document.getElementById('calc-' + id + '-odds')?.value) || 1.97;
            var roundToBetProfit = {};
            const capIn = parseFloat(document.getElementById('calc-' + id + '-capital')?.value) || 1000000;
                const baseIn = parseFloat(document.getElementById('calc-' + id + '-base')?.value) || 10000;
                const martingaleEl = document.getElementById('calc-' + id + '-martingale');
                const martingaleTypeEl = document.getElementById('calc-' + id + '-martingale-type');
                const useMartingale = !!(martingaleEl && martingaleEl.checked);
                const martingaleType = (martingaleTypeEl && martingaleTypeEl.value) || 'pyo';
                
                // 서버에서 계산된 값이 있으면 우선 사용
                for (let i = 0; i < completedHist.length; i++) {
                    const h = completedHist[i];
                    if (!h || typeof h.predicted === 'undefined' || typeof h.actual === 'undefined') continue;
                    const rn = h.round != null ? Number(h.round) : NaN;
                    if (isNaN(rn)) continue;
                    
                    var wasPaused = (h.no_bet === true || (h.betAmount != null && h.betAmount === 0));
                    if (wasPaused) {
                        // 멈춤 상태여도 실제 결과가 나왔으면 승패 기록 (15회 승률 계산용)
                        const isJokerPaused = h.actual === 'joker';
                        const isWinPaused = !isJokerPaused && h.predicted === h.actual;
                        roundToBetProfit[rn] = { betAmount: 0, profit: 0, isWin: isWinPaused, isJoker: isJokerPaused };
                        continue;
                    }
                    
                    // [변경 금지] 완료 행 배팅금액은 아래 마틴 시뮬레이션으로만 채움. 서버 h.betAmount는 완료 행 표시에 사용하지 않음. CALCULATOR_GUIDE.md "계산기 표 데이터 출처" 참고.
                }
                
                // [변경 금지] 완료 행 전부 마틴게일 시뮬레이션으로 배팅금액·수익 채움. 표시는 roundToBetProfit[rn]만 사용.
                var martinTableDetail = getMartinTable(martingaleType, baseIn);
                let cap = capIn, currentBet = baseIn, martingaleStep = 0;
                for (let i = 0; i < completedHist.length; i++) {
                    const h = completedHist[i];
                    if (!h || typeof h.predicted === 'undefined' || typeof h.actual === 'undefined') continue;
                    const rn = h.round != null ? Number(h.round) : NaN;
                    if (isNaN(rn)) continue;
                    
                    var wasPaused = (h.no_bet === true || (h.betAmount != null && h.betAmount === 0));
                    if (wasPaused) {
                        const isJokerPaused = h.actual === 'joker';
                        const isWinPaused = !isJokerPaused && h.predicted === h.actual;
                        if (!roundToBetProfit[rn]) roundToBetProfit[rn] = { betAmount: 0, profit: 0, isWin: isWinPaused, isJoker: isJokerPaused };
                        continue;
                    }
                    
                    if (roundToBetProfit[rn]) {
                        // 이미 서버/첫 루프에서 값 있음 → 배팅금액은 그대로 두고, 자본·마틴 단계만 갱신 (다음 행 폴백 계산용)
                        const bet = roundToBetProfit[rn].betAmount || 0;
                        const isJoker = roundToBetProfit[rn].isJoker;
                        const isWin = roundToBetProfit[rn].isWin;
                        if (isJoker) { cap -= bet; if (useMartingale && (martingaleType === 'pyo' || martingaleType === 'pyo_half')) martingaleStep = Math.min(martingaleStep + 1, martinTableDetail.length - 1); else currentBet = Math.min(currentBet * 2, Math.floor(cap)); }
                        else if (isWin) { cap += bet * (oddsIn - 1); if (useMartingale && (martingaleType === 'pyo' || martingaleType === 'pyo_half')) martingaleStep = 0; else currentBet = baseIn; }
                        else { cap -= bet; if (useMartingale && (martingaleType === 'pyo' || martingaleType === 'pyo_half')) martingaleStep = Math.min(martingaleStep + 1, martinTableDetail.length - 1); else currentBet = Math.min(currentBet * 2, Math.floor(cap)); }
                        continue;
                    }
                    
                    if (useMartingale && (martingaleType === 'pyo' || martingaleType === 'pyo_half')) currentBet = martinTableDetail[Math.min(martingaleStep, martinTableDetail.length - 1)];
                    const bet = Math.min(currentBet, Math.floor(cap));
                    if (cap < bet || cap <= 0) break;
                    const isJoker = h.actual === 'joker';
                    const isWin = !isJoker && h.predicted === h.actual;
                    roundToBetProfit[rn] = { betAmount: bet, profit: isJoker ? -bet : (isWin ? Math.floor(bet * (oddsIn - 1)) : -bet), isWin: isWin, isJoker: isJoker };
                    if (isJoker) { cap -= bet; if (useMartingale && (martingaleType === 'pyo' || martingaleType === 'pyo_half')) martingaleStep = Math.min(martingaleStep + 1, martinTableDetail.length - 1); else currentBet = Math.min(currentBet * 2, Math.floor(cap)); }
                    else if (isWin) { cap += bet * (oddsIn - 1); if (useMartingale && (martingaleType === 'pyo' || martingaleType === 'pyo_half')) martingaleStep = 0; else currentBet = baseIn; }
                    else { cap -= bet; if (useMartingale && (martingaleType === 'pyo' || martingaleType === 'pyo_half')) martingaleStep = Math.min(martingaleStep + 1, martinTableDetail.length - 1); else currentBet = Math.min(currentBet * 2, Math.floor(cap)); }
                }
            // 1열(대기) 배팅금액: 위 시뮬레이션 직후 currentBet 사용 → 2열(완료)과 동일 기준, getBetForRound 타이밍 꼬임 방지
            var lastCompletedRound = completedHist.length ? Math.max.apply(null, completedHist.map(function(he) { return Number(he.round) || 0; })) : null;
            var nextRoundBet = (lastCompletedRound != null && cap > 0) ? Math.min(currentBet, Math.floor(cap)) : 0;
            /** pending 회차가 lastCompletedRound+1보다 클 때(회차 간격): getBetForRound는 완료 회차만 시뮬레이션해 직전 회차 금액을 반환함.
             * 1행에 2행과 같은 금액이 표시되는 버그 방지: 간격 구간을 패배 가정으로 시뮬레이션해 해당 회차의 정확한 마틴금액 계산 */
            function getBetForPendingRoundWithGap(rn) {
                if (lastCompletedRound == null || cap <= 0 || isNaN(rn) || rn <= lastCompletedRound + 1) return nextRoundBet;
                var gap = rn - lastCompletedRound - 1;
                var simCap = cap, simCurrentBet = currentBet, simStep = martingaleStep;
                for (var g = 0; g < gap && simCap > 0; g++) {
                    var bet = Math.min(simCurrentBet, Math.floor(simCap));
                    if (simCap < bet || simCap <= 0) break;
                    simCap -= bet;
                    if (useMartingale && (martingaleType === 'pyo' || martingaleType === 'pyo_half')) {
                        simStep = Math.min(simStep + 1, martinTableDetail.length - 1);
                        simCurrentBet = martinTableDetail[simStep];
                    } else {
                        simCurrentBet = Math.min(simCurrentBet * 2, Math.floor(simCap));
                    }
                }
                return (simCap > 0) ? Math.min(simCurrentBet, Math.floor(simCap)) : 0;
            }
            // 회차별 픽/결과/승패/배팅금액/수익 행 목록 (pending=대기, completed=결과·수익)
            let rows = [];
            var seenRoundNums = {};
            for (let i = usedHist.length - 1; i >= 0; i--) {
                const h = usedHist[i];
                if (!h || typeof h.predicted === 'undefined') continue;
                const rn = h.round != null ? Number(h.round) : NaN;
                if (!isNaN(rn) && seenRoundNums[rn]) continue;
                if (!isNaN(rn)) seenRoundNums[rn] = true;
                const roundStr = h.round != null ? String(h.round) : '-';
                var curRound = (typeof lastPrediction !== 'undefined' && lastPrediction && lastPrediction.round != null) ? Number(lastPrediction.round) : null;
                var isPendingRow = !isNaN(rn) && curRound != null && rn === curRound && (h.actual === 'pending' || !h.actual || h.actual === '');
                const pickVal = (isPendingRow && typeof lastIs15Joker !== 'undefined' && lastIs15Joker) ? '보류' : ((h.predicted === '정' || h.predicted === '꺽') ? h.predicted : '보류');
                // pick-color-core-rule: 정/꺽→빨강/검정은 15번 카드 기준. 고정 매핑(정=빨강,꺽=검정) 금지.
                var pickClass;
                var pendingColorFromState = (isPendingRow && state && (state.pending_color === '빨강' || state.pending_color === '검정')) ? state.pending_color : null;
                if (!pendingColorFromState && isPendingRow && state && state.lastBetPickForRound && Number(state.lastBetPickForRound.round) === rn && (state.lastBetPickForRound.value === '정' || state.lastBetPickForRound.value === '꺽')) {
                    pendingColorFromState = state.lastBetPickForRound.isRed ? '빨강' : '검정';
                }
                if (pickVal === '보류') {
                    pickClass = 'pick-hold';
                } else if ((h.pickColor || h.pick_color) === '빨강') {
                    pickClass = 'pick-jung';
                } else if ((h.pickColor || h.pick_color) === '검정') {
                    pickClass = 'pick-kkuk';
                } else if (pendingColorFromState) {
                    pickClass = pendingColorFromState === '빨강' ? 'pick-jung' : 'pick-kkuk';
                } else {
                    // pickColor 없을 때: 현재 회차 + 15번 카드 있으면 계산, 없으면 보류 스타일(잘못된 색상 표시 방지)
                    var disp = (typeof allResults !== 'undefined' && Array.isArray(allResults) && allResults.length >= 15) ? allResults.slice(0, 15) : [];
                    var card15 = (disp.length >= 15 && !isNaN(rn) && curRound != null && rn === curRound && typeof parseCardValue === 'function') ? parseCardValue(disp[14].result || '') : null;
                    var is15Red = card15 ? card15.isRed : null;
                    if (is15Red === true || is15Red === false) {
                        var colorFrom15 = (pickVal === '정') ? (is15Red ? '빨강' : '검정') : (is15Red ? '검정' : '빨강');
                        pickClass = colorFrom15 === '빨강' ? 'pick-jung' : 'pick-kkuk';
                    } else {
                        pickClass = 'pick-hold';  // 15번 카드 미확인 시 회색(잘못된 색상보다 안전)
                    }
                }
                const warningWinRateVal = (typeof h.warningWinRate === 'number' && !isNaN(h.warningWinRate)) ? h.warningWinRate.toFixed(1) + '%' : '-';
                // 15회 승률: CALCULATOR_GUIDE — 표에는 회차별 저장값(rate15) 표시. 없으면 완료 행은 해당 시점 15회 승률 계산 후 저장(한 번만).
                var rate15Val;
                if (typeof h.rate15 === 'number' && !isNaN(h.rate15)) {
                    rate15Val = h.rate15.toFixed(1) + '%';
                } else if (h.actual === 'pending' || !h.actual || h.actual === '') {
                    if (typeof getCalcRecent15WinRate === 'function') {
                        var r15 = getCalcRecent15WinRate(id);
                        rate15Val = (typeof r15 === 'number' && !isNaN(r15)) ? r15.toFixed(1) + '%' : '-';
                    } else rate15Val = '-';
                } else {
                    // 완료 행인데 rate15 없음 → 해당 회차 시점 15회 승률 계산 후 h.rate15에 저장(저장되면 다음부터 표시 유지)
                    if (typeof getCalcRecent15WinRateAtRound === 'function' && !isNaN(rn)) {
                        var atRound = getCalcRecent15WinRateAtRound(id, rn);
                        if (atRound != null && !isNaN(atRound)) {
                            h.rate15 = atRound;
                            rate15Val = atRound.toFixed(1) + '%';
                        } else rate15Val = '-';
                    } else rate15Val = '-';
                }
                var betStr, profitStr, res, outcome, resultClass, outClass;
                // 계산기 표는 한 행 기준 통일: 픽·배팅금액·수익·승패 모두 이 행(h)의 predicted/actual만 사용 (예측기표 actual 혼합 시 행 내 불일치 방지)
                var effectiveActual = h.actual;
                if (effectiveActual === 'pending' || !effectiveActual || effectiveActual === '') {
                    // 1열(배팅중) 배팅금액: 완료 행과 동일 시뮬레이션 출처 사용. getCalcResult.currentBet = 헤더 "배팅중"과 동일.
                    // nextRoundBet/pending_bet_amount 대신 getCalcResult 사용 → 1행에 2행과 같은 금액 표시 버그 방지
                    // 15번 카드 조커 시 배팅 안 함 → 금액 표시 안 함
                    var amt;
                    if (h.no_bet === true || (typeof effectivePausedForRound === 'function' && effectivePausedForRound(id)) || (typeof lastIs15Joker !== 'undefined' && lastIs15Joker)) {
                        amt = 0;
                    } else if (lastCompletedRound != null && !isNaN(rn) && rn > lastCompletedRound + 1) {
                        // 회차 간격: 간격 구간 패배 가정 시뮬레이션
                        amt = getBetForPendingRoundWithGap(rn);
                    } else {
                        var simResult = (typeof getCalcResult === 'function') ? getCalcResult(id) : null;
                        amt = (simResult && simResult.currentBet != null && simResult.currentBet > 0) ? Math.min(simResult.currentBet, Math.floor(simResult.cap || capIn)) : (lastCompletedRound != null && cap > 0 ? nextRoundBet : (typeof getBetForRound === 'function' ? getBetForRound(id, rn) : (h.betAmount > 0 ? h.betAmount : 0)));
                    }
                    if (h && typeof amt === 'number') h.betAmount = amt;
                    betStr = amt > 0 ? Number(amt).toLocaleString() : '-';
                    profitStr = '-';
                    res = '-';
                    outcome = (h.no_bet === true || amt === 0) ? '멈춤' : '대기';
                    resultClass = '';
                    outClass = amt > 0 ? 'skip' : 'skip';
                } else {
                    const bp = roundToBetProfit[rn];
                    betStr = (bp && bp.betAmount != null && bp.betAmount > 0) ? bp.betAmount.toLocaleString() : '-';
                    const profitVal = (bp && bp.profit != null) ? bp.profit : '-';
                    profitStr = profitVal === '-' ? '-' : (profitVal >= 0 ? '+' : '') + Number(profitVal).toLocaleString();
                    // effectiveActual 사용 (서버 동기화된 값 우선)
                    res = effectiveActual === 'joker' ? '조' : (effectiveActual === '정' ? '정' : '꺽');
                    // 멈춤 상태에서도 실제 결과가 나왔으면 승패 기록 (15회 승률 계산용)
                    if (h.no_bet === true || (h.betAmount != null && h.betAmount === 0)) {
                        // 멈춤 상태: 수익은 0이지만 승패는 기록
                        if (effectiveActual === 'joker') {
                            outcome = '조';
                        } else {
                            outcome = h.predicted === effectiveActual ? '승' : '패';
                        }
                    } else {
                        outcome = effectiveActual === 'joker' ? '조' : (h.predicted === effectiveActual ? '승' : '패');
                    }
                    resultClass = res === '조' ? 'result-joker' : (res === '정' ? 'result-jung' : 'result-kkuk');
                    outClass = outcome === '승' ? 'win' : outcome === '패' ? 'lose' : outcome === '조' ? 'joker' : 'skip';
                }
                rows.push({ roundStr: roundStr, roundNum: !isNaN(rn) ? rn : null, pick: pickVal, pickClass: pickClass, warningWinRate: warningWinRateVal, rate15: rate15Val, result: res, resultClass: resultClass, outcome: outcome, betAmount: betStr, profit: profitStr, outClass: outClass });
            }
            try { window.__calcDetailRows = window.__calcDetailRows || {}; window.__calcDetailRows[id] = rows; } catch (e) {}
            const CALC_TABLE_DISPLAY_MAX = 200;
            const displayRows = rows.slice(0, CALC_TABLE_DISPLAY_MAX);
            if (tableWrap) {
                if (displayRows.length === 0) {
                    tableWrap.innerHTML = '';
                } else {
                    let tbl = '<table class="calc-round-table"><thead><tr><th>회차</th><th>픽</th><th>경고 승률</th><th>15회승률</th><th>배팅금액</th><th>수익</th><th>승패</th></tr></thead><tbody>';
                    displayRows.forEach(function(row) {
                        const outClass = row.outClass || (row.outcome === '승' ? 'win' : row.outcome === '패' ? 'lose' : row.outcome === '조' ? 'joker' : 'skip');
                        const profitClass = (typeof row.profit === 'number' && row.profit > 0) || (typeof row.profit === 'string' && row.profit.indexOf('+') === 0) ? 'profit-plus' : (typeof row.profit === 'number' && row.profit < 0) || (typeof row.profit === 'string' && row.profit.indexOf('-') === 0 && row.profit !== '-') ? 'profit-minus' : '';
                        var roundTdClass = (row.roundNum != null) ? 'calc-td-round-' + getRoundIconType(row.roundNum) : '';
                        var roundCellHtml = (row.roundNum != null) ? (String(row.roundNum) + getRoundIconHtml(row.roundNum)) : row.roundStr;
                        tbl += '<tr><td class="' + roundTdClass + '">' + roundCellHtml + '</td><td class="' + row.pickClass + '">' + row.pick + '</td><td class="calc-td-warning-rate">' + (row.warningWinRate || '-') + '</td><td class="calc-td-rate15">' + (row.rate15 || '-') + '</td><td class="calc-td-bet">' + row.betAmount + '</td><td class="calc-td-profit ' + profitClass + '">' + row.profit + '</td><td class="' + outClass + '">' + row.outcome + '</td></tr>';
                    });
                    tbl += '</tbody></table>';
                    tableWrap.innerHTML = tbl;
                }
            }
            // 경기결과는 완료된 회차만, 최근 30회 표시 (서버 동기화된 actual 사용)
            let arr = [];
            for (const h of completedHist) {
                if (!h || typeof h.predicted === 'undefined' || typeof h.actual === 'undefined' || h.actual === 'pending' || h.actual === '') continue;
                // 계산기 표와 동일 출처: 이 행의 actual만 사용 (행 내 픽·승패 일치)
                var effectiveActualForStreak = h.actual;
                if (effectiveActualForStreak === 'pending' || !effectiveActualForStreak || effectiveActualForStreak === '') continue;
                arr.push(effectiveActualForStreak === 'joker' ? 'j' : (h.predicted === effectiveActualForStreak ? 'w' : 'l'));
            }
            const arrRev = arr.slice().reverse();
            const showMax = 30;
            const arrShow = arrRev.slice(0, showMax);
            const streakStr = arrShow.map(a => {
                return '<span class="' + (a === 'w' ? 'w' : a === 'l' ? 'l' : 'j') + '">' + (a === 'w' ? '승' : a === 'l' ? '패' : '조') + '</span>';
            }).join(' ');
            streakEl.innerHTML = '경기결과 (최근 30회←): ' + streakStr;
            var rate15 = getCalcRecent15WinRate(id);
            var rate15Str = (completedHist.length < 1) ? '-' : (rate15.toFixed(1) + '%');
            // 표시된 내역(최근 200회) 승률: 배팅한 완료 행만, 조커=패 (멈춤 행 제외)
            var dispWins = 0, dispLosses = 0;
            displayRows.forEach(function(row) {
                if (row.betAmount === '-' || row.outcome === '멈춤' || row.outcome === '대기') return;
                if (row.outcome === '승') dispWins++;
                else if (row.outcome === '패' || row.outcome === '조') dispLosses++;
            });
            var dispTotal = dispWins + dispLosses;
            var dispRateStr = (dispTotal < 1) ? '-' : (dispWins / dispTotal * 100).toFixed(1) + '%';
            var shape50 = (typeof getShape50WinRate === 'function') ? getShape50WinRate() : null;
            var shape50Str = (shape50 != null && !isNaN(shape50)) ? shape50.toFixed(1) + '%' : '-';
            var shapePred10 = (typeof getShapePredictionWinRate10 === 'function') ? getShapePredictionWinRate10(id) : null;
            var shapePred10Str = (shapePred10 != null && !isNaN(shapePred10)) ? shapePred10.toFixed(1) + '%' : '-';
            statsEl.textContent = '최대연승: ' + r.maxWinStreak + ' | 최대연패: ' + r.maxLoseStreak + ' | 모양승률: ' + shape50Str + ' | 표승률: ' + dispRateStr + ' | 15회승률: ' + rate15Str + ' | 모양판별승률: ' + shapePred10Str;
            } catch (e) { console.warn('updateCalcDetail', id, e); }
        }
        document.querySelectorAll('.calc-dropdown-header').forEach(h => {
            h.addEventListener('click', function() {
                const dd = this.closest('.calc-dropdown');
                if (dd) dd.classList.toggle('collapsed');
            });
        });
        document.querySelectorAll('.calc-options-toggle').forEach(t => {
            t.addEventListener('click', function(e) {
                e.stopPropagation();
                const wrap = this.closest('.calc-options-wrap');
                if (wrap) wrap.classList.toggle('collapsed');
            });
        });
        document.querySelectorAll('.calc-mini-graph-header').forEach(h => {
            h.addEventListener('click', function() {
                const wrap = this.closest('.calc-mini-graph-collapse');
                if (wrap) wrap.classList.toggle('collapsed');
            });
        });
        document.querySelectorAll('.bet-calc-tabs .tab').forEach(tab => {
            tab.addEventListener('click', function() {
                const t = this.getAttribute('data-tab');
                document.querySelectorAll('.bet-calc-tabs .tab').forEach(x => x.classList.remove('active'));
                this.classList.add('active');
                const calcPanel = document.getElementById('bet-calc-panel');
                const logPanel = document.getElementById('bet-log-panel');
                const pauseGuidePanel = document.getElementById('bet-pause-guide-panel');
                if (calcPanel) calcPanel.classList.toggle('active', t === 'calc');
                if (logPanel) logPanel.classList.toggle('active', t === 'log');
                if (pauseGuidePanel) pauseGuidePanel.classList.toggle('active', t === 'pause-guide');
                if (t === 'pause-guide' && typeof renderPauseGuideTable === 'function') renderPauseGuideTable();
            });
        });
        document.addEventListener('click', function(e) {
            var tab = e.target && e.target.closest('#analysis-tabs-wrap .analysis-tab');
            if (!tab) return;
            var panelId = tab.getAttribute('data-panel');
            if (!panelId) return;
            document.querySelectorAll('#analysis-tabs-wrap .analysis-tab').forEach(function(x) { x.classList.remove('active'); x.removeAttribute('aria-selected'); });
            document.querySelectorAll('#analysis-tabs-wrap .analysis-panel').forEach(function(p) { p.classList.remove('active'); });
            tab.classList.add('active');
            tab.setAttribute('aria-selected', 'true');
            var panel = document.getElementById('panel-' + panelId);
            if (panel) panel.classList.add('active');
            if (panelId === 'win-rate-direction' && typeof renderWinRateDirectionPanel === 'function') renderWinRateDirectionPanel();
        });
        var collapseBtn = document.getElementById('analysis-tabs-collapse-btn');
        if (collapseBtn && !collapseBtn.getAttribute('data-bound')) {
            collapseBtn.setAttribute('data-bound', '1');
            collapseBtn.addEventListener('click', function() {
                var wrap = document.getElementById('analysis-tabs-wrap');
                if (wrap) {
                    wrap.classList.toggle('collapsed');
                    this.textContent = wrap.classList.contains('collapsed') ? '▶' : '▼';
                }
            });
        }
        document.getElementById('bet-log-clear-all')?.addEventListener('click', function() {
            if (betCalcLog.length === 0) return;
            if (typeof confirm !== 'undefined' && !confirm('로그를 모두 삭제할까요?')) return;
            betCalcLog = [];
            saveBetCalcLog();
            renderBetCalcLog();
        });
        renderBetCalcLog();
        setInterval(function() {
            const st = getServerTimeSec();
            CALC_IDS.forEach(id => {
                if (!calcState[id].running) return;
                const started = calcState[id].started_at || 0;
                calcState[id].elapsed = started ? Math.max(0, st - started) : 0;
                updateCalcSummary(id);
                if (calcState[id].use_duration_limit && calcState[id].duration_limit > 0 && calcState[id].elapsed >= calcState[id].duration_limit) {
                    calcState[id].running = false;
                    calcState[id].timer_completed = true;
                    if (calcState[id].history.length > 0) appendCalcLog(id);
                    saveCalcStateToServer({ immediate: true });
                    updateCalcSummary(id);
                    updateCalcStatus(id);
                    const saveBtn = document.querySelector('.calc-save[data-calc="' + id + '"]');
                    if (saveBtn) saveBtn.style.display = 'none';
                } else {
                    var targetEnabledEl = document.getElementById('calc-' + id + '-target-enabled');
                    var targetAmountEl = document.getElementById('calc-' + id + '-target-amount');
                    var targetEnabled = !!(targetEnabledEl && targetEnabledEl.checked);
                    var targetAmount = Math.max(0, parseInt(targetAmountEl && targetAmountEl.value, 10) || 0);
                    if (targetEnabled && targetAmount > 0) {
                        var r = getCalcResult(id);
                        if (r.profit >= targetAmount) {
                            calcState[id].running = false;
                            calcState[id].timer_completed = true;
                            saveCalcStateToServer({ immediate: true });
                            updateCalcSummary(id);
                            updateCalcStatus(id);
                            postCurrentPickIfChanged(id, { pickColor: null, round: null, probability: null, suggested_amount: null });
                            const saveBtn = document.querySelector('.calc-save[data-calc="' + id + '"]');
                            if (saveBtn) saveBtn.style.display = 'none';
                        }
                    }
                }
            });
            }, 1000);
        function updateAllCalcs() {
            CALC_IDS.forEach(id => { updateCalcStatus(id); updateCalcDetail(id); updateCalcSummary(id); });
        }
        try { updateAllCalcs(); } catch (e) { console.warn('초기 계산기 상태:', e); }
        document.querySelectorAll('.calc-run').forEach(btn => {
            btn.addEventListener('click', async function() {
                const rawId = this.getAttribute('data-calc');
                const id = parseInt(rawId, 10);
                if (!CALC_IDS.includes(id)) return;
                const state = calcState[id];
                if (!state) return;
                try {
                if (!localStorage.getItem(CALC_SESSION_KEY)) {
                    try { await loadCalcStateFromServer(); } catch (e) { try { localStorage.setItem(CALC_SESSION_KEY, 'default'); } catch (e2) {} }
                }
                if (state.timerId) { clearInterval(state.timerId); state.timerId = null; }
                const durEl = document.getElementById('calc-' + id + '-duration');
                const checkEl = document.getElementById('calc-' + id + '-duration-check');
                const durationMin = (durEl && parseInt(durEl.value, 10)) || 0;
                calcState[id].duration_limit = durationMin * 60;
                calcState[id].use_duration_limit = !!(checkEl && checkEl.checked);
                const revRun = document.getElementById('calc-' + id + '-reverse');
                calcState[id].reverse = !!(revRun && revRun.checked);
                const winRateRevRun = document.getElementById('calc-' + id + '-win-rate-reverse');
                calcState[id].win_rate_reverse = !!(winRateRevRun && winRateRevRun.checked);
                const winRateThrRun = document.getElementById('calc-' + id + '-win-rate-threshold');
                var thrRun = (winRateThrRun && parseFloat(winRateThrRun.value) != null && !isNaN(parseFloat(winRateThrRun.value))) ? Math.max(0, Math.min(100, parseFloat(winRateThrRun.value))) : 46;
                calcState[id].win_rate_threshold = thrRun;
                const loseStreakRevRun = document.getElementById('calc-' + id + '-lose-streak-reverse');
                calcState[id].lose_streak_reverse = !!(loseStreakRevRun && loseStreakRevRun.checked);
                const loseStreakThrRunEl = document.getElementById('calc-' + id + '-lose-streak-reverse-threshold');
                calcState[id].lose_streak_reverse_threshold = (loseStreakThrRunEl && !isNaN(parseFloat(loseStreakThrRunEl.value))) ? Math.max(0, Math.min(100, parseFloat(loseStreakThrRunEl.value))) : 48;
                const loseStreakMinRunEl = document.getElementById('calc-' + id + '-lose-streak-reverse-min');
                calcState[id].lose_streak_reverse_min_streak = (loseStreakMinRunEl && !isNaN(parseInt(loseStreakMinRunEl.value, 10))) ? Math.max(2, Math.min(15, parseInt(loseStreakMinRunEl.value, 10))) : 3;
                const winRateDirRevRun = document.getElementById('calc-' + id + '-win-rate-direction-reverse');
                calcState[id].win_rate_direction_reverse = !!(winRateDirRevRun && winRateDirRevRun.checked);
                var streakSuppressRun = document.getElementById('calc-' + id + '-streak-suppress-reverse');
                calcState[id].streak_suppress_reverse = !!(streakSuppressRun && streakSuppressRun.checked);
                var lockDirRun = document.getElementById('calc-' + id + '-lock-direction-on-lose-streak');
                calcState[id].lock_direction_on_lose_streak = !(lockDirRun && !lockDirRun.checked);
                var shapeOnlyRun = document.getElementById('calc-' + id + '-shape-only-latest-next-pick');
                calcState[id].shape_only_latest_next_pick = !!(shapeOnlyRun && shapeOnlyRun.checked);
                calcState[id].last_trend_direction = null;
                const pauseLowRun = document.getElementById('calc-' + id + '-pause-low-win-rate');
                const pauseThrRunEl = document.getElementById('calc-' + id + '-pause-win-rate-threshold');
                calcState[id].pause_low_win_rate_enabled = !!(pauseLowRun && pauseLowRun.checked);
                calcState[id].pause_win_rate_threshold = (pauseThrRunEl && !isNaN(parseFloat(pauseThrRunEl.value))) ? Math.max(0, Math.min(100, parseFloat(pauseThrRunEl.value))) : 45;
                calcState[id].paused = false;
                calcState[id].timer_completed = false;
                calcState[id].running = true;
                calcState[id].history = [];
                calcState[id].started_at = 0;
                calcState[id].elapsed = 0;
                calcState[id].maxWinStreakEver = 0;
                calcState[id].maxLoseStreakEver = 0;
                var latestG = null;
                try { latestG = window.__latestGameIDForCalc; } catch (e) {}
                var nextRound = 0;
                if (latestG != null && latestG !== '') { var n = parseInt(String(latestG), 10); if (!isNaN(n)) nextRound = n + 1; }
                calcState[id].first_bet_round = nextRound;
                try {
                    const payload = buildCalcPayload();
                    payload[String(id)].running = true;
                    payload[String(id)].history = [];
                    payload[String(id)].first_bet_round = calcState[id].first_bet_round;
                    const session_id = localStorage.getItem(CALC_SESSION_KEY) || 'default';
                    const res = await fetch('/api/calc-state', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: session_id, calcs: payload }) });
                    const data = await res.json().catch(function() { return {}; });
                    if (data.session_id) localStorage.setItem(CALC_SESSION_KEY, data.session_id);
                    if (data.calcs && data.calcs[String(id)]) {
                        calcState[id].started_at = data.calcs[String(id)].started_at || 0;
                        lastServerTimeSec = data.server_time || lastServerTimeSec;
                    } else if (calcState[id].running && !calcState[id].started_at) {
                        // 서버가 calcs를 비워서 보냈을 때도 로컬에서 실행 유지 (started_at 클라이언트 기준)
                        calcState[id].started_at = Math.floor(Date.now() / 1000);
                        if (data.error) console.warn('계산기 실행 저장 경고:', data.error);
                    }
                    // 가이드 §6: 새로고침 후 실행 상태 복원 — 실행 직후 백업 저장 (서버 상태 유실 시 백업으로 복원)
                    try { localStorage.setItem(CALC_STATE_BACKUP_KEY, JSON.stringify(buildCalcPayload())); } catch (e2) { /* ignore */ }
                } catch (e) { console.warn('계산기 실행 저장 실패:', e); if (calcState[id].running && !calcState[id].started_at) calcState[id].started_at = Math.floor(Date.now() / 1000); }
                lastResetOrRunAt = Date.now();
                updateCalcSummary(id);
                updateCalcStatus(id);
                updateCalcDetail(id);
                // 시작 시 픽/금액은 배팅중 표시될 때 타이머가 전달. running=true로 DB 반영해 다음 픽 POST 시 매크로가 픽 수신
                postCurrentPickIfChanged(id, { pickColor: null, round: null, probability: null, suggested_amount: null, running: true });
                var saveBtnEl = document.querySelector('.calc-save[data-calc="' + id + '"]');
                if (saveBtnEl) saveBtnEl.style.display = 'none';
                } catch (err) {
                    console.warn('계산기 실행 중 오류:', id, err);
                    if (calcState[id]) {
                        calcState[id].running = true;
                        if (!calcState[id].started_at) calcState[id].started_at = Math.floor(Date.now() / 1000);
                    }
                    lastResetOrRunAt = Date.now();
                    updateCalcSummary(id);
                    updateCalcStatus(id);
                    updateCalcDetail(id);
                }
            });
        });
        document.querySelectorAll('.calc-stop').forEach(btn => {
            btn.addEventListener('click', async function() {
                const rawId = this.getAttribute('data-calc');
                const id = parseInt(rawId, 10);
                if (!CALC_IDS.includes(id)) return;
                const state = calcState[id];
                if (!state) return;
                state.running = false;
                state.timer_completed = false;
                if (state.timerId) { clearInterval(state.timerId); state.timerId = null; }
                state.history = [];
                state.pending_round = null;
                state.pending_predicted = null;
                state.pending_prob = null;
                state.pending_color = null;
                state.pending_bet_amount = null;
                lastResetOrRunAt = Date.now();
                updateCalcSummary(id);
                updateCalcStatus(id);
                updateCalcDetail(id);
                postCurrentPickIfChanged(id, { pickColor: null, round: null, probability: null, suggested_amount: null, running: false });
                await saveCalcStateToServer({ skipApplyForIds: [id], immediate: true });
            });
        });
        document.querySelectorAll('.calc-reset').forEach(btn => {
            btn.addEventListener('click', async function() {
                const rawId = this.getAttribute('data-calc');
                const id = parseInt(rawId, 10);
                if (!CALC_IDS.includes(id)) return;
                const state = calcState[id];
                if (!state) return;
                state.running = false;
                state.timer_completed = false;
                if (state.timerId) { clearInterval(state.timerId); state.timerId = null; }
                state.history = [];
                state.started_at = 0;
                state.elapsed = 0;
                state.first_bet_round = 0;
                state.maxWinStreakEver = 0;
                state.maxLoseStreakEver = 0;
                state.pending_round = null;
                state.pending_predicted = null;
                state.pending_prob = null;
                state.pending_color = null;
                state.pending_bet_amount = null;
                state.paused = false;
                lastResetOrRunAt = Date.now();
                updateCalcSummary(id);
                updateCalcStatus(id);
                updateCalcDetail(id);
                updateCalcBetCopyLine(id);
                const saveBtn = document.querySelector('.calc-save[data-calc="' + id + '"]');
                if (saveBtn) saveBtn.style.display = 'none';
                await saveCalcStateToServer({ skipApplyForIds: [id], immediate: true });
            });
        });
        function exportCalcHistoryToCsv(id) {
            try {
                var rows = (window.__calcDetailRows && window.__calcDetailRows[id]) ? window.__calcDetailRows[id] : [];
                if (!rows || rows.length === 0) { alert('내보낼 내역이 없습니다. 표를 한 번 갱신한 뒤 시도해 주세요.'); return; }
                var esc = function(s) { var t = String(s == null ? '' : s); if (t.indexOf(',') >= 0 || t.indexOf('"') >= 0 || t.indexOf('\\n') >= 0) return '"' + t.replace(/"/g, '""') + '"'; return t; };
                var header = '회차,픽,경고승률,15회승률,배팅금액,수익,승패';
                var lines = [header].concat(rows.map(function(r) { return esc(r.roundStr) + ',' + esc(r.pick) + ',' + esc(r.warningWinRate) + ',' + esc(r.rate15 || '-') + ',' + esc(r.betAmount) + ',' + esc(r.profit) + ',' + esc(r.outcome); }));
                var csv = lines.join('\\n');
                var blob = new Blob(['\\ufeff' + csv], { type: 'text/csv;charset=utf-8' });
                var url = URL.createObjectURL(blob);
                var a = document.createElement('a');
                a.href = url;
                a.download = 'calc-' + id + '-history-' + (new Date().toISOString().slice(0, 10)) + '.csv';
                a.click();
                URL.revokeObjectURL(url);
            } catch (err) { console.warn('exportCalcHistoryToCsv', err); alert('내보내기 실패'); }
        }
        document.querySelectorAll('.calc-export-csv').forEach(btn => {
            btn.addEventListener('click', function() { var id = parseInt(this.getAttribute('data-calc'), 10); if (CALC_IDS.includes(id)) exportCalcHistoryToCsv(id); });
        });
        document.querySelectorAll('.calc-save').forEach(btn => {
            btn.addEventListener('click', function() {
                const id = parseInt(this.getAttribute('data-calc'), 10);
                if (!CALC_IDS.includes(id) || !calcState[id]) return;
                if (calcState[id].history.length === 0) return;
                appendCalcLog(id);
                this.style.display = 'none';
            });
        });
        /** 게임 중 옵션 변경 시 calcState 동기화 — 배팅중 픽 즉시 반영 */
        function syncCalcOptionsFromUI(id) {
            if (!calcState[id]) return;
            const revEl = document.getElementById('calc-' + id + '-reverse');
            calcState[id].reverse = !!(revEl && revEl.checked);
            const winRateRevEl = document.getElementById('calc-' + id + '-win-rate-reverse');
            calcState[id].win_rate_reverse = !!(winRateRevEl && winRateRevEl.checked);
            const winRateThrEl = document.getElementById('calc-' + id + '-win-rate-threshold');
            calcState[id].win_rate_threshold = (winRateThrEl && !isNaN(parseFloat(winRateThrEl.value))) ? Math.max(0, Math.min(100, parseFloat(winRateThrEl.value))) : 50;
            const loseStreakRevEl = document.getElementById('calc-' + id + '-lose-streak-reverse');
            calcState[id].lose_streak_reverse = !!(loseStreakRevEl && loseStreakRevEl.checked);
            const loseStreakThrEl = document.getElementById('calc-' + id + '-lose-streak-reverse-threshold');
            calcState[id].lose_streak_reverse_threshold = (loseStreakThrEl && !isNaN(parseFloat(loseStreakThrEl.value))) ? Math.max(0, Math.min(100, parseFloat(loseStreakThrEl.value))) : 48;
            const loseStreakMinEl = document.getElementById('calc-' + id + '-lose-streak-reverse-min');
            calcState[id].lose_streak_reverse_min_streak = (loseStreakMinEl && !isNaN(parseInt(loseStreakMinEl.value, 10))) ? Math.max(2, Math.min(15, parseInt(loseStreakMinEl.value, 10))) : 3;
            const winRateDirRevEl = document.getElementById('calc-' + id + '-win-rate-direction-reverse');
            calcState[id].win_rate_direction_reverse = !!(winRateDirRevEl && winRateDirRevEl.checked);
            const streakSuppressEl = document.getElementById('calc-' + id + '-streak-suppress-reverse');
            calcState[id].streak_suppress_reverse = !!(streakSuppressEl && streakSuppressEl.checked);
            const lockDirEl = document.getElementById('calc-' + id + '-lock-direction-on-lose-streak');
            calcState[id].lock_direction_on_lose_streak = !(lockDirEl && !lockDirEl.checked);
            const shapeOnlyEl = document.getElementById('calc-' + id + '-shape-only-latest-next-pick');
            calcState[id].shape_only_latest_next_pick = !!(shapeOnlyEl && shapeOnlyEl.checked);
            const shapePredEl = document.getElementById('calc-' + id + '-shape-prediction');
            calcState[id].shape_prediction = !!(shapePredEl && shapePredEl.checked);
            const shapePredRevEl = document.getElementById('calc-' + id + '-shape-prediction-reverse');
            calcState[id].shape_prediction_reverse = !!(shapePredRevEl && shapePredRevEl.checked);
            const shapePredRevThrEl = document.getElementById('calc-' + id + '-shape-prediction-reverse-threshold');
            calcState[id].shape_prediction_reverse_threshold = (shapePredRevThrEl && !isNaN(parseFloat(shapePredRevThrEl.value))) ? Math.max(0, Math.min(100, parseFloat(shapePredRevThrEl.value))) : 50;
            const shapeWeightEl = document.getElementById('calc-' + id + '-shape-weight');
            calcState[id].shape_weight = (shapeWeightEl && !isNaN(parseFloat(shapeWeightEl.value))) ? Math.max(0, Math.min(3, parseFloat(shapeWeightEl.value))) : 1.5;
            const chunkWeightEl = document.getElementById('calc-' + id + '-chunk-weight');
            calcState[id].chunk_weight = (chunkWeightEl && !isNaN(parseFloat(chunkWeightEl.value))) ? Math.max(0, Math.min(3, parseFloat(chunkWeightEl.value))) : 1;
            const pongWeightEl = document.getElementById('calc-' + id + '-pong-weight');
            calcState[id].pong_weight = (pongWeightEl && !isNaN(parseFloat(pongWeightEl.value))) ? Math.max(0, Math.min(3, parseFloat(pongWeightEl.value))) : 1.5;
            const symmetryWeightEl = document.getElementById('calc-' + id + '-symmetry-weight');
            calcState[id].symmetry_weight = (symmetryWeightEl && !isNaN(parseFloat(symmetryWeightEl.value))) ? Math.max(0, Math.min(3, parseFloat(symmetryWeightEl.value))) : 1;
            const pauseLowEl = document.getElementById('calc-' + id + '-pause-low-win-rate');
            calcState[id].pause_low_win_rate_enabled = !!(pauseLowEl && pauseLowEl.checked);
            const pauseThrEl = document.getElementById('calc-' + id + '-pause-win-rate-threshold');
            calcState[id].pause_win_rate_threshold = (pauseThrEl && !isNaN(parseFloat(pauseThrEl.value))) ? Math.max(0, Math.min(100, parseFloat(pauseThrEl.value))) : 45;
        }
        function onCalcOptionChange(id) {
            syncCalcOptionsFromUI(id);
            calcState[id].lastBetPickForRound = null;  // 옵션 변경 시 저장된 픽 무효화 → 재계산
            var curRound = (typeof lastPrediction !== 'undefined' && lastPrediction && lastPrediction.round != null) ? Number(lastPrediction.round) : null;
            if (curRound != null && typeof savedBetPickByRound !== 'undefined') delete savedBetPickByRound[curRound];  // 캐시 무효화
            updateCalcStatus(id);
            ensurePendingRowForRunningCalc(id);  // 1열 pending 행 보정
            updateCalcDetail(id);
            updateCalcSummary(id);
            try { saveCalcStateToServer({ immediate: true, skipApplyForIds: [id] }); } catch (e) {}  // 서버 응답으로 로컬 덮어쓰기 방지
        }
        CALC_IDS.forEach(id => {
            ['capital', 'base', 'odds', 'target-amount'].forEach(f => {
                const el = document.getElementById('calc-' + id + '-' + f);
                if (el) {
                    el.addEventListener('input', () => { updateCalcSummary(id); updateCalcDetail(id); });
                    // 자본/배팅금액/배당 변경 시 서버에 저장 — 매크로가 GET으로 받는 금액이 표와 동일하도록
                    if (f === 'capital' || f === 'base' || f === 'odds') {
                        el.addEventListener('change', function() { try { saveCalcStateToServer({ immediate: true }); } catch (e) {} });
                    }
                }
            });
            const targetEnabledEl = document.getElementById('calc-' + id + '-target-enabled');
            if (targetEnabledEl) targetEnabledEl.addEventListener('change', () => { updateCalcSummary(id); });
            // 게임 중 옵션(반픽/승률반픽/연패반픽 등) 변경 시 즉시 반영
            ['reverse', 'win-rate-reverse', 'win-rate-threshold', 'lose-streak-reverse', 'lose-streak-reverse-threshold', 'lose-streak-reverse-min', 'win-rate-direction-reverse', 'streak-suppress-reverse', 'lock-direction-on-lose-streak', 'shape-only-latest-next-pick', 'shape-prediction', 'shape-prediction-reverse', 'shape-prediction-reverse-threshold', 'shape-weight', 'chunk-weight', 'pong-weight', 'symmetry-weight', 'pause-low-win-rate', 'pause-win-rate-threshold'].forEach(f => {
                const el = document.getElementById('calc-' + id + '-' + f);
                if (el) el.addEventListener('change', function() { onCalcOptionChange(id); });
            });
        });
        document.addEventListener('click', function(e) {
            var t = e.target && e.target.closest('.calc-bet-copy-amount');
            if (!t) return;
            var amount = t.getAttribute('data-amount') || t.textContent.replace(/[^0-9]/g, '');
            if (!amount) return;
            try {
                navigator.clipboard.writeText(amount).then(function() {
                    var orig = t.textContent;
                    t.textContent = '복사됨';
                    t.style.color = '#ffb74d';
                    setTimeout(function() { t.textContent = orig; t.style.color = ''; }, 600);
                });
            } catch (err) {
                try {
                    var ta = document.createElement('textarea');
                    ta.value = amount;
                    document.body.appendChild(ta);
                    ta.select();
                    document.execCommand('copy');
                    document.body.removeChild(ta);
                    var orig = t.textContent;
                    t.textContent = '복사됨';
                    setTimeout(function() { t.textContent = orig; }, 600);
                } catch (e2) {}
            }
        });
        
        let timerData = { elapsed: 0, lastFetch: 0, round: 0, serverTime: 0 };
        let lastResultsUpdate = 0;
        let lastTimerUpdate = Date.now();
        var remainingSecForPoll = 10;  // 10초 경기용: 라운드 종료 직전/직후 폴링 간격 조절
        async function updateTimer() {
            try {
                const now = Date.now();
                const timeElement = document.getElementById('remaining-time');
                
                if (!timeElement) {
                    return;
                }
                // 클라이언트 측 남은 시간 (폴링 간격·결과 새로고침 판단용). 10초 경기 기준
                const timeDiff = (now - timerData.serverTime) / 1000;
                const currentElapsed = Math.max(0, timerData.elapsed + timeDiff);
                const remaining = Math.max(0, 10 - currentElapsed);
                remainingSecForPoll = remaining;
                // 라운드 종료 직전/직후에는 더 자주 폴링 (다음 픽을 빨리 보여주기)
                const nearEnd = remaining < 3;
                const fetchInterval = nearEnd ? 150 : 300;
                if (now - timerData.lastFetch > fetchInterval) {
                    try {
                    // 10초 경기 룰: 8초 타임아웃
                    const controller = new AbortController();
                    const timeoutId = setTimeout(() => controller.abort(), 8000);
                    
                    const response = await fetch('/api/current-status?t=' + now, {
                        signal: controller.signal,
                        cache: 'no-cache'
                    });
                    
                    clearTimeout(timeoutId);
                    
                    if (!response.ok) {
                        throw new Error('Network error: ' + response.status);
                    }
                    const data = await response.json();
                        
                        if (data.server_time !== undefined) lastServerTimeSec = data.server_time;
                        if (!data.error && data.elapsed !== undefined) {
                            const prevElapsed = timerData.elapsed;
                            const prevRound = timerData.round;
                            
                            // elapsed 값 업데이트 (항상 서버 값 사용)
                            timerData.elapsed = data.elapsed;
                            timerData.round = data.round || 0;
                            timerData.serverTime = now;  // 서버에서 데이터를 가져온 시점
                            lastTimerUpdate = now;
                            timerData.lastFetch = now;
                            
                            // 라운드가 변경되거나 elapsed가 리셋되면 경기 결과 즉시 새로고침
                            const roundChanged = timerData.round !== prevRound;
                            const roundEnded = prevElapsed > 8 && data.elapsed < 2;
                            const roundStarted = prevElapsed < 1 && data.elapsed > 9;
                            
                            if (roundChanged || roundEnded || roundStarted) {
                                if (now - lastResultsUpdate > 500) { loadResults(); lastResultsUpdate = now; }
                            }
                            // updateBettingInfo는 별도로 실행하므로 여기서 제거
                        }
                    } catch (error) {
                        // 네트워크 오류는 조용히 처리 (클라이언트 측 계산 계속)
                        // AbortError, Failed to fetch 등은 조용히 처리
                    }
                }
                
                // 항상 시간 표시 (실시간 카운팅)
                timeElement.textContent = `남은 시간: ${remaining.toFixed(2)} 초`;
                
                // 타이머 색상
                timeElement.className = 'remaining-time';
                if (remaining <= 1) {
                    timeElement.classList.add('danger');
                } else if (remaining <= 3) {
                    timeElement.classList.add('warning');
                }
                
                // 타이머가 거의 0이 되면 결과 요청 (10초 경기: 새 결과·예측픽 빨리 표시)
                if (remaining <= 1.5 && now - lastResultsUpdate > 500) {
                    loadResults();
                    lastResultsUpdate = now;
                }
                if (remaining <= 0 && now - lastResultsUpdate > 500) {
                    loadResults();
                    lastResultsUpdate = now;
                }
            } catch (error) {
                console.error('타이머 업데이트 오류:', error);
                const timeElement = document.getElementById('remaining-time');
                if (timeElement) {
                    timeElement.textContent = '남은 시간: -- 초';
                }
            }
        }
        
        // 초기 로드: 서버에서 계산기 상태 복원 후 결과 로드 (실행중 상태 유지)
        async function initialLoad() {
            try {
                await loadCalcStateFromServer();
                updateAllCalcs();
            } catch (e) { console.warn('계산기 상태 로드:', e); }
            try {
                await loadResults().catch(e => console.warn('초기 결과 로드 실패:', e));
            } catch (e) {
                console.warn('초기 로드 오류:', e);
            }
            updateTimer();
        }
        
        initialLoad();
        
        // 탭 가시성 추적: 백그라운드일 때 폴링 간격 조정
        var isTabVisible = !document.hidden;
        var resultsPollIntervalId = null;
        var calcStatusPollIntervalId = null;
        var calcStatePollIntervalId = null;
        var timerUpdateIntervalId = null;
        var predictionPollIntervalId = null;
        // 리셋/실행 직후에는 서버 폴링 스킵 (저장 반영 전에 예전 상태로 덮어쓰는 것 방지)
        var lastResetOrRunAt = 0;
        
        function refreshPredictionPickOnly() {
            var pickContainer = document.getElementById('prediction-pick-container');
            if (!pickContainer) return;
            function dr(r) { return r != null ? String(r) : '-'; }
            var disp = allResults.slice(0, 15);
            var is15Joker = disp.length >= 15 && !!disp[14].joker;
            var predict = '보류', predProb = 0, colorToPick = '-', colorClass = 'black';
            if (lastPrediction && (lastPrediction.value === '정' || lastPrediction.value === '꺽')) {
                predict = lastPrediction.value;
                predProb = (lastPrediction.prob != null && !isNaN(lastPrediction.prob)) ? lastPrediction.prob : 0;
                var sc = normalizePickColor(lastPrediction.color);
                colorToPick = (sc === '빨강' || sc === '검정') ? sc : (lastPrediction.value === '정' ? '빨강' : '검정');
                colorClass = colorToPick === '빨강' ? 'red' : 'black';
            }
            var showHold = is15Joker || predict === '보류';
            var displayRoundNum = (lastPrediction && lastPrediction.round) ? lastPrediction.round : (allResults.length > 0 && allResults[0].gameID != null ? Number(allResults[0].gameID) + 1 : 0);
            var roundIconMain = getRoundIcon(displayRoundNum);
            var u35WarningBlock = lastWarningU35 ? ('<div class="prediction-warning-u35">⚠ U자+줄 3~5 구간 · 줄(유지) 보정 적용</div>') : '';
            var leftBlock = showHold ? ('<div class="prediction-pick"><div class="prediction-pick-title">예측 픽</div><div class="prediction-card" style="background:#455a64;border-color:#78909c"><span class="pred-value-big" style="color:#fff;font-size:1.2em">보류</span></div><div class="prediction-prob-under" style="color:#ffb74d">' + (is15Joker ? '15번 카드 조커 · 배팅하지 마세요' : '서버 예측 대기 중') + '</div><div class="pred-round">' + dr(displayRoundNum) + '회 ' + roundIconMain + '</div></div>') : ('<div class="prediction-pick"><div class="prediction-pick-title prediction-pick-title-betting">배팅중<br>' + (colorToPick === '빨강' ? 'RED' : 'BLACK') + '</div><div class="prediction-card card-' + colorClass + '"><span class="pred-value-big">' + predict + '</span></div><div class="prediction-prob-under">예측 확률 ' + (typeof predProb === 'number' ? predProb.toFixed(1) : '0') + '%</div><div class="pred-round">' + dr(displayRoundNum) + '회 ' + roundIconMain + '</div>' + u35WarningBlock + '</div>');
            pickContainer.innerHTML = leftBlock;
            pickContainer.setAttribute('data-section', '메인 예측기');
        }
        
        function setupIntervals() {
            // 기존 interval 정리
            if (resultsPollIntervalId) clearInterval(resultsPollIntervalId);
            if (calcStatusPollIntervalId) clearInterval(calcStatusPollIntervalId);
            if (calcStatePollIntervalId) clearInterval(calcStatePollIntervalId);
            if (timerUpdateIntervalId) clearInterval(timerUpdateIntervalId);
            if (predictionPollIntervalId) clearInterval(predictionPollIntervalId);
            
            // 탭 가시성에 따라 간격 조정. 과도한 폴링 시 ERR_INSUFFICIENT_RESOURCES 방지를 위해 완만한 간격 사용
            var resultsInterval = isTabVisible ? 280 : 1200;
            var calcStatusInterval = isTabVisible ? 350 : 1200;  // 픽 서버 전달(매크로용). 350ms로 요청 수 완화
            var calcStateInterval = isTabVisible ? 2200 : 4000;  // 계산기 상태 GET 간격 완화(리소스 절약)
            var timerInterval = isTabVisible ? 200 : 1000;
            
            // 결과 폴링: 분당 4게임(15초 사이클) 기준. 계산기 실행 중이면 빠르게 해서 회차 놓침 방지
            // 백그라운드일 때는 브라우저 제한(최소 1초)을 고려해 간격 조정
            resultsPollIntervalId = setInterval(() => {
                const anyRunning = CALC_IDS.some(id => calcState[id] && calcState[id].running);
                const r = typeof remainingSecForPoll === 'number' ? remainingSecForPoll : 10;
                const criticalPhase = r <= 3 || r >= 8;
                // 백그라운드일 때는 최소 1초 간격, 보일 때는 더 빠른 간격 (예측픽 빨리 나오도록 단축)
                const baseInterval = allResults.length === 0 ? 200 : (anyRunning ? 100 : (criticalPhase ? 150 : 200));
                const interval = isTabVisible ? baseInterval : Math.max(1000, baseInterval);
                if (Date.now() - lastResultsUpdate > interval) {
                    loadResults().catch(e => console.warn('결과 새로고침 실패:', e));
                }
            }, resultsInterval);
            
            // 계산기 실행 중: 픽을 서버로 빠르게 전달해 매크로가 곧바로 받도록 150ms 간격 (배팅 연동 속도)
            calcStatusPollIntervalId = setInterval(() => {
                const anyRunning = CALC_IDS.some(id => calcState[id] && calcState[id].running);
                if (anyRunning) CALC_IDS.forEach(id => { updateCalcStatus(id); });
            }, calcStatusInterval);
            
            // 계산기 실행 중일 때 서버 상태 주기적으로 가져와 UI 실시간 반영 (멈춰 보이는 현상 방지)
            // 백그라운드일 때는 간격을 늘림 (브라우저 제한 고려)
            calcStatePollIntervalId = setInterval(() => {
                if (Date.now() - lastResetOrRunAt < 6000) return;
                const anyRunning = CALC_IDS.some(id => calcState[id] && calcState[id].running);
                if (anyRunning) {
                    loadCalcStateFromServer(false).then(function() { updateAllCalcs(); }).catch(function(e) { console.warn('계산기 상태 폴링:', e); });
                }
            }, calcStateInterval);
            
            // 0.2초마다 타이머 업데이트 (UI만 업데이트, 서버 요청은 1초마다)
            // 백그라운드일 때는 1초 간격으로 조정 (브라우저 제한)
            timerUpdateIntervalId = setInterval(updateTimer, timerInterval);
            
            // 예측픽만 경량 폴링: 캐시 기반으로 0.1초마다 받아서 카드만 먼저 갱신 (예측픽이 늦게 나오는 현상 완화)
            if (isTabVisible) {
                predictionPollIntervalId = setInterval(function() {
                    fetch('/api/current-prediction?t=' + Date.now(), { cache: 'no-cache' }).then(function(r) { return r.json(); }).then(function(data) {
                        var sp = data && data.server_prediction;
                        if (!sp || (sp.value !== '정' && sp.value !== '꺽')) return;
                        var newRound = sp.round != null ? Number(sp.round) : NaN;
                        var prevRound = (lastPrediction && lastPrediction.round != null) ? Number(lastPrediction.round) : NaN;
                        if (isNaN(newRound) || (!isNaN(prevRound) && newRound < prevRound)) return;
                        var normColor = normalizePickColor(sp.color) || sp.color || null;
                        lastPrediction = { value: sp.value, round: sp.round, prob: sp.prob != null ? sp.prob : 0, color: normColor };
                        lastWarningU35 = !!(sp.warning_u35);
                        refreshPredictionPickOnly();
                    }).catch(function() {});
                }, 100);
            }
        }
        
        // 초기 설정
        setupIntervals();
        
        // 탭 가시성 변경 시 interval 재설정
        document.addEventListener('visibilitychange', function() {
            var wasVisible = isTabVisible;
            isTabVisible = !document.hidden;
            
            if (isTabVisible && !wasVisible) {
                // 탭이 다시 보일 때 즉시 동기화 및 interval 재설정
                lastResultsUpdate = 0;
                setupIntervals();  // 빠른 간격으로 재설정
                loadResults().then(function() {
                    return loadCalcStateFromServer(false);
                }).then(function() {
                    if (typeof updateAllCalcs === 'function') updateAllCalcs();
                }).catch(function(e) { console.warn('visibilitychange 동기화:', e); });
            } else if (!isTabVisible && wasVisible) {
                // 백그라운드로 갈 때 느린 간격으로 재설정
                setupIntervals();
            }
        });
        
        // 창을 다시 올렸을 때(탭 전환 없이 최소화/다른 창 위로만 했을 때) 동기화 — visibilitychange는 탭 전환 시에만 발생
        var lastFocusSyncAt = 0;
        window.addEventListener('focus', function() {
            if (document.hidden) return;
            var now = Date.now();
            if (now - lastFocusSyncAt < 2500) return;
            lastFocusSyncAt = now;
            lastResultsUpdate = 0;
            setupIntervals();
            loadResults().then(function() {
                return loadCalcStateFromServer(false);
            }).then(function() {
                if (typeof updateAllCalcs === 'function') updateAllCalcs();
            }).catch(function(e) { console.warn('focus 동기화:', e); });
        });
    </script>
</body>
</html>
'''

@app.route('/results', methods=['GET'])
def results_page():
    """경기 결과 웹페이지"""
    return render_template_string(RESULTS_HTML)

def _build_results_payload_db_only(hours=24, backfill=False):
    """DB만으로 페이로드 생성 (네트워크 없음). 규칙: 24h 구간. 캐시 비어 있을 때 첫 화면 빠르게 표시용."""
    try:
        if not DB_AVAILABLE or not DATABASE_URL:
            return None
        results = get_recent_results(hours=hours)
        results = _sort_results_newest_first(results)
        results_full = results  # 모양판별 보정용 전체 (슬라이스 전)
        # 응답 크기·처리 시간 제한 (성능 최적화: 100건으로 축소)
        RESULTS_PAYLOAD_LIMIT = 100
        if len(results) > RESULTS_PAYLOAD_LIMIT:
            results = results[:RESULTS_PAYLOAD_LIMIT]
        round_actuals = _build_round_actuals(results)
        _merge_round_predictions_into_history(round_actuals, results=results)
        # 100회 승률방향용: 클라이언트에 수백 회 내려줘야 함 (DB는 수백 회 저장됨)
        ph = get_prediction_history(300)
        # 안정화: 서버에 저장된 예측만 불러옴. 계산/저장은 스케줄러에서만(ensure_stored_prediction_for_current_round).
        server_pred = None
        if len(results) >= 16:
            try:
                latest_gid = results[0].get('gameID')
                predicted_round = int(str(latest_gid or '0'), 10) + 1
                is_15_joker = len(results) >= 15 and bool(results[14].get('joker'))
                if not is_15_joker:
                    stored = get_stored_round_prediction(predicted_round)
                    if stored and stored.get('predicted'):
                        server_pred = {
                            'value': stored['predicted'], 'round': predicted_round,
                            'prob': stored.get('probability') or 0, 'color': stored.get('pick_color'),
                            'warning_u35': False, 'pong_chunk_phase': None, 'pong_chunk_debug': {},
                        }
                        # 퐁당/덩어리 판별 메뉴용: 저장 픽은 유지하되 phase/debug만 계산. API 응답 속도 위해 shape/chunk DB 조회 생략.
                        try:
                            computed = compute_prediction(results, ph, shape_win_stats=None, chunk_profile_stats=None)
                            if computed:
                                server_pred['pong_chunk_phase'] = computed.get('pong_chunk_phase')
                                server_pred['pong_chunk_debug'] = computed.get('pong_chunk_debug') or {}
                                # 가장 최근 다음 픽 추가
                                latest_next_pick = _get_latest_next_pick_for_chunk(results)
                                if latest_next_pick:
                                    server_pred['pong_chunk_debug']['latest_next_pick'] = latest_next_pick
                        except Exception:
                            pass
            except Exception as e:
                print(f"[API] server_pred 조회 오류: {str(e)[:100]}")
        if server_pred is None:
            server_pred = {'value': None, 'round': int(str(results[0].get('gameID') or '0'), 10) + 1 if results else 0, 'prob': 0, 'color': None, 'warning_u35': False, 'pong_chunk_phase': None, 'pong_chunk_debug': {}}
        # 모양 옵션: server_pred가 기본값이어도 latest_next_pick 항상 포함 — 계산기 최상단 보류만 표시 버그 방지
        if len(results) >= 16 and (not server_pred.get('pong_chunk_debug') or 'latest_next_pick' not in (server_pred.get('pong_chunk_debug') or {})):
            try:
                latest_next_pick = _get_latest_next_pick_for_chunk(results)
                if latest_next_pick:
                    if not server_pred.get('pong_chunk_debug'):
                        server_pred['pong_chunk_debug'] = {}
                    server_pred['pong_chunk_debug']['latest_next_pick'] = latest_next_pick
            except Exception:
                pass
        blended = _blended_win_rate(ph)
        if backfill:
            ph = _backfill_shape_predicted_in_ph(ph, results_full, max_backfill=10, persist_to_db=True)
        return {
            'results': results,
            'count': len(results),
            'timestamp': datetime.now().isoformat(),
            'source': 'database',
            'prediction_history': ph,
            'server_prediction': server_pred,
            'blended_win_rate': round(blended, 1) if blended is not None else None,
            'round_actuals': round_actuals
        }
    except Exception as e:
        print(f"[API] DB 전용 페이로드 오류: {str(e)[:150]}")
        return None


def _build_results_payload():
    """경기 결과 페이로드 생성 (스레드에서 호출, 먹통 시 None 반환)."""
    try:
        latest_results = load_results_data()
        if latest_results is None:
            latest_results = []
        _log_when_changed('api_latest', len(latest_results), lambda v: f"[API] 최신 데이터 로드: {v}개")
        if DB_AVAILABLE and DATABASE_URL:
            # 데이터베이스에서 최근 3시간 데이터 조회 (성능 최적화: 최신 회차만 필요)
            db_results = get_recent_results(hours=3)
            _log_when_changed('api_db', len(db_results), lambda v: f"[API] DB 데이터 조회: {v}개")
            
            # 최신 데이터 저장 (백그라운드)
            if latest_results:
                try:
                    saved_count = 0
                    for game_data in latest_results:
                        if save_game_result(game_data):
                            saved_count += 1
                    if saved_count > 0:
                        _log_when_changed('latest_save', saved_count, lambda v: f"[💾] 최신 데이터 {v}개 저장 완료")
                except Exception as e:
                    print(f"[경고] 최신 데이터 저장 실패: {str(e)[:100]}")
            
            # 최신 데이터와 DB 데이터 병합 (최신 데이터 우선)
            if latest_results:
                # 최신 데이터의 gameID들
                latest_game_ids = {str(r.get('gameID', '')) for r in latest_results if r.get('gameID')}
                
                # DB 결과에서 최신 데이터에 없는 것만 유지
                db_results_filtered = [r for r in db_results if str(r.get('gameID', '')) not in latest_game_ids]
                
                # 최신 데이터 + DB 데이터 (최신순) → gameID 기준 정렬로 순서 고정 (그래프 일관성)
                results = latest_results + db_results_filtered
                results = _sort_results_newest_first(results)
                results_full = results  # 모양판별 보정용 전체 (슬라이스 전)
                # 성능 최적화: 응답 크기 제한 (100건으로 더 축소)
                if len(results) > 100:
                    results = results[:100]
                _log_when_changed('api_merge', (len(latest_results), len(db_results_filtered), len(results)), lambda v: f"[API] 병합 결과: 최신 {v[0]}개 + DB {v[1]}개 = 총 {v[2]}개")
                
                # 병합된 전체 결과에 대해 정/꺽 결과 계산 및 추가
                if len(results) >= 16:
                    # 정/꺽 결과 계산 및 저장
                    calculate_and_save_color_matches(results)
                    
                    # 각 결과에 정/꺽 정보 추가 (최신 15개만) - 일괄 조회로 최적화
                    pairs_to_lookup = []
                    pairs_index_map = {}
                    
                    for i in range(min(15, len(results))):
                        if i + 15 < len(results):
                            current_game_id = str(results[i].get('gameID', ''))
                            compare_game_id = str(results[i + 15].get('gameID', ''))
                            
                            if not current_game_id or not compare_game_id:
                                results[i]['colorMatch'] = None
                                continue
                            
                            # 조커 카드는 비교 불가
                            if results[i].get('joker') or results[i + 15].get('joker'):
                                results[i]['colorMatch'] = None
                                continue
                            
                            pairs_to_lookup.append((current_game_id, compare_game_id))
                            pairs_index_map[(current_game_id, compare_game_id)] = i
                    
                    # 일괄 조회 (성능 최적화, statement_timeout으로 먹통 방지)
                    batch_results = {}
                    if pairs_to_lookup and DB_AVAILABLE and DATABASE_URL:
                        try:
                            conn = get_db_connection(statement_timeout_sec=5)
                            if conn:
                                cur = conn.cursor()
                                # PostgreSQL에서 튜플 비교는 여러 방법이 있지만, 간단하게 OR 조건 사용
                                conditions = []
                                params = []
                                for gid, cgid in pairs_to_lookup:
                                    conditions.append('(game_id = %s AND compare_game_id = %s)')
                                    params.extend([gid, cgid])
                                
                                query = f'''
                                    SELECT game_id, compare_game_id, match_result
                                    FROM color_matches
                                    WHERE {' OR '.join(conditions)}
                                '''
                                cur.execute(query, params)
                                
                                for row in cur.fetchall():
                                    key = (str(row[0]), str(row[1]))
                                    batch_results[key] = row[2]
                                
                                cur.close()
                                conn.close()
                        except Exception as e:
                            # 일괄 조회 실패 시 개별 조회로 전환
                            print(f"[경고] 일괄 조회 실패, 개별 조회로 전환: {str(e)[:100]}")
                            try:
                                conn.close()
                            except:
                                pass
                    
                    # 조회 결과 적용 및 없는 것 계산
                    for current_game_id, compare_game_id in pairs_to_lookup:
                        i = pairs_index_map[(current_game_id, compare_game_id)]
                        match_result = batch_results.get((current_game_id, compare_game_id))
                        
                        if match_result is None:
                            # DB에 없으면 즉시 계산
                            current_color = parse_card_color(results[i].get('result', ''))
                            compare_color = parse_card_color(results[i + 15].get('result', ''))
                            
                            if current_color is not None and compare_color is not None:
                                match_result = (current_color == compare_color)
                                # 계산 결과를 DB에 저장
                                save_color_match(current_game_id, compare_game_id, match_result)
                            else:
                                match_result = None
                        
                        # 결과에 추가 (항상 추가, None이어도)
                        results[i]['colorMatch'] = match_result
            else:
                # 최신 데이터가 없으면 DB 데이터만 사용
                results = db_results
                results_full = results
                print(f"[API] 최신 데이터 없음, DB 데이터만 사용: {len(results)}개")
            
            # 그래프/표시 순서 일관성: 항상 gameID 기준 최신순으로 정렬
            results = _sort_results_newest_first(results)
            if not latest_results:
                results_full = results
            round_actuals = _build_round_actuals(results)
            _merge_round_predictions_into_history(round_actuals, results=results)
            ph = get_prediction_history(300)
            # 안정화: 서버에 저장된 예측만 불러옴. 계산/저장은 스케줄러에서만.
            server_pred = None
            if len(results) >= 16:
                try:
                    latest_gid = results[0].get('gameID')
                    predicted_round = int(str(latest_gid or '0'), 10) + 1
                    is_15_joker = len(results) >= 15 and bool(results[14].get('joker'))
                    if not is_15_joker:
                        stored = get_stored_round_prediction(predicted_round)
                        if stored and stored.get('predicted'):
                            server_pred = {
                                'value': stored['predicted'], 'round': predicted_round,
                                'prob': stored.get('probability') or 0, 'color': stored.get('pick_color'),
                                'warning_u35': False, 'pong_chunk_phase': None, 'pong_chunk_debug': {},
                            }
                            try:
                                computed = compute_prediction(results, ph, shape_win_stats=None, chunk_profile_stats=None)
                                if computed:
                                    server_pred['pong_chunk_phase'] = computed.get('pong_chunk_phase')
                                    server_pred['pong_chunk_debug'] = computed.get('pong_chunk_debug') or {}
                                    latest_next_pick = _get_latest_next_pick_for_chunk(results)
                                    if latest_next_pick:
                                        server_pred['pong_chunk_debug']['latest_next_pick'] = latest_next_pick
                            except Exception:
                                pass
                except Exception as e:
                    print(f"[API] server_pred 조회 오류: {str(e)[:100]}")
            if server_pred is None:
                server_pred = {'value': None, 'round': int(str(results[0].get('gameID') or '0'), 10) + 1 if results else 0, 'prob': 0, 'color': None, 'warning_u35': False, 'pong_chunk_phase': None, 'pong_chunk_debug': {}}
            # 모양 옵션: latest_next_pick 항상 포함 (계산기 최상단 보류만 표시 버그 방지)
            if len(results) >= 16 and (not server_pred.get('pong_chunk_debug') or 'latest_next_pick' not in (server_pred.get('pong_chunk_debug') or {})):
                try:
                    latest_next_pick = _get_latest_next_pick_for_chunk(results)
                    if latest_next_pick:
                        if not server_pred.get('pong_chunk_debug'):
                            server_pred['pong_chunk_debug'] = {}
                        server_pred['pong_chunk_debug']['latest_next_pick'] = latest_next_pick
                except Exception:
                    pass
            blended = _blended_win_rate(ph)
            ph = _backfill_shape_predicted_in_ph(ph, results_full)
            return {
                'results': results,
                'count': len(results),
                'timestamp': datetime.now().isoformat(),
                'source': 'database+json',
                'prediction_history': ph,
                'server_prediction': server_pred,
                'blended_win_rate': round(blended, 1) if blended is not None else None,
                'round_actuals': round_actuals
            }
        else:
            # 데이터베이스가 없으면 기존 방식 (result.json에서 가져오기)
            results = latest_results if latest_results else []
            results = _sort_results_newest_first(results)
            print(f"[API] DB 없음, 최신 데이터만 사용: {len(results)}개")
            ph = get_prediction_history(300)
            
            # DB가 없어도 정/꺽 결과 계산 (클라이언트 측 계산을 위해)
            if len(results) >= 16:
                # 각 결과에 정/꺽 정보 추가 (최신 15개만)
                for i in range(min(15, len(results))):
                    if i + 15 < len(results):
                        current_game_id = str(results[i].get('gameID', ''))
                        compare_game_id = str(results[i + 15].get('gameID', ''))
                        
                        if not current_game_id or not compare_game_id:
                            results[i]['colorMatch'] = None
                            continue
                        
                        # 조커 카드는 비교 불가
                        if results[i].get('joker') or results[i + 15].get('joker'):
                            results[i]['colorMatch'] = None
                            continue
                        
                        # 즉시 계산 (DB 없음)
                        current_color = parse_card_color(results[i].get('result', ''))
                        compare_color = parse_card_color(results[i + 15].get('result', ''))
                        
                        if current_color is not None and compare_color is not None:
                            match_result = (current_color == compare_color)
                            results[i]['colorMatch'] = match_result
                            print(f"[API] 정/꺽 결과 계산 (DB 없음): 카드 {i+1} ({current_game_id}) = {match_result}")
                        else:
                            results[i]['colorMatch'] = None
            
            # ph는 위에서 get_prediction_history(300) 이미 설정됨
            # 한 출처: 해당 회차에 저장된 예측이 있으면 그대로 사용 (DB 없을 때는 매번 계산)
            server_pred = None
            if len(results) >= 16 and DB_AVAILABLE and DATABASE_URL:
                try:
                    latest_gid = results[0].get('gameID')
                    predicted_round = int(str(latest_gid or '0'), 10) + 1
                    is_15_joker = len(results) >= 15 and bool(results[14].get('joker'))
                    if not is_15_joker:
                        stored = get_stored_round_prediction(predicted_round)
                        if stored and stored.get('predicted'):
                            server_pred = {
                                'value': stored['predicted'], 'round': predicted_round,
                                'prob': stored.get('probability') or 0, 'color': stored.get('pick_color'),
                                'warning_u35': False, 'pong_chunk_phase': None, 'pong_chunk_debug': {},
                            }
                            try:
                                computed = compute_prediction(results, ph, shape_win_stats=None, chunk_profile_stats=None)
                                if computed:
                                    server_pred['pong_chunk_phase'] = computed.get('pong_chunk_phase')
                                    server_pred['pong_chunk_debug'] = computed.get('pong_chunk_debug') or {}
                                    latest_next_pick = _get_latest_next_pick_for_chunk(results)
                                    if latest_next_pick:
                                        server_pred['pong_chunk_debug']['latest_next_pick'] = latest_next_pick
                            except Exception:
                                pass
                except Exception as e:
                    print(f"[API] server_pred 구성 오류: {str(e)[:100]}")
            if server_pred is None:
                shape_stats = _get_shape_stats_for_results(results) if len(results) >= 16 else None
                chunk_stats = _get_chunk_stats_for_results(results) if len(results) >= 16 else None
                server_pred = compute_prediction(results, ph, shape_win_stats=shape_stats, chunk_profile_stats=chunk_stats) if len(results) >= 16 else {'value': None, 'round': 0, 'prob': 0, 'color': None, 'warning_u35': False, 'pong_chunk_phase': None, 'pong_chunk_debug': {}}
            # 모양 옵션: latest_next_pick 항상 포함 (계산기 최상단 보류만 표시 버그 방지)
            if len(results) >= 16 and (not server_pred.get('pong_chunk_debug') or 'latest_next_pick' not in (server_pred.get('pong_chunk_debug') or {})):
                try:
                    latest_next_pick = _get_latest_next_pick_for_chunk(results)
                    if latest_next_pick:
                        if not server_pred.get('pong_chunk_debug'):
                            server_pred['pong_chunk_debug'] = {}
                        server_pred['pong_chunk_debug']['latest_next_pick'] = latest_next_pick
                except Exception:
                    pass
            blended = _blended_win_rate(ph)
            round_actuals = _build_round_actuals(results)
            ph = _backfill_shape_predicted_in_ph(ph, results)
            return {
                'results': results,
                'count': len(results),
                'timestamp': datetime.now().isoformat(),
                'source': 'json',
                'prediction_history': ph,
                'server_prediction': server_pred,
                'blended_win_rate': round(blended, 1) if blended is not None else None,
                'round_actuals': round_actuals
            }
    except Exception as e:
        print(f"[❌ 오류] _build_results_payload 실패: {str(e)[:200]}")
        return None


_results_refresh_lock = threading.Lock()
_results_refreshing = False

def _refresh_results_background():
    """백그라운드에서 캐시 갱신. 서버가 항상 최신 결과를 송출하려면 유효한 페이로드가 오면 캐시를 덮어쓴다."""
    global results_cache, last_update_time, _results_refreshing
    if not _results_refresh_lock.acquire(blocking=False):
        return
    _results_refreshing = True
    try:
        payload = _build_results_payload()
        if payload is not None and payload.get('results'):
            results_cache = payload
            last_update_time = time.time() * 1000
    except Exception as e:
        print(f"[API] 백그라운드 갱신 오류: {str(e)[:150]}")
    finally:
        _results_refreshing = False
        try:
            _results_refresh_lock.release()
        except Exception:
            pass

@app.route('/api/results', methods=['GET'])
def get_results():
    """경기 결과 API. 화면 송출 보장: 매 요청마다 DB에서 결과 생성(워커/캐시 무관)."""
    try:
        global results_cache, last_update_time
        result_source = request.args.get('result_source', '').strip()
        do_backfill = request.args.get('backfill') == '1'

        # 매 요청마다 DB에서 응답 생성. 24h 구간으로 타임존/커밋 타이밍에 따른 최신 회차 누락 방지 (규칙 준수)
        payload = _build_results_payload_db_only(hours=24, backfill=do_backfill)
        if payload and payload.get('results'):
            results_cache = payload
            last_update_time = time.time() * 1000
        if not payload or not payload.get('results'):
            payload = _build_results_payload_db_only(hours=72, backfill=do_backfill) or payload
            if payload and payload.get('results'):
                results_cache = payload
                last_update_time = time.time() * 1000
        if not payload or not payload.get('results'):
            if results_cache and results_cache.get('results'):
                payload = results_cache.copy()
            else:
                payload = {
                    'results': [], 'count': 0, 'timestamp': datetime.now().isoformat(),
                    'error': 'loading', 'prediction_history': [], 'server_prediction': {'value': None, 'round': 0, 'prob': 0, 'color': None, 'warning_u35': False, 'pong_chunk_phase': None, 'pong_chunk_debug': {}},
                    'blended_win_rate': None, 'round_actuals': {}
                }
        if not _results_refreshing:
            threading.Thread(target=_refresh_results_background, daemon=True).start()
        
        # result_source 지정 시: 베팅 사이트와 동일한 결과 소스에서 round_actuals 재조회
        if result_source:
            try:
                from urllib.parse import urlparse
                parsed = urlparse(result_source)
                base = f"{parsed.scheme or 'https'}://{parsed.netloc}" if parsed.netloc else result_source.rstrip('/')
                results_from_source = load_results_data(base_url=base)
                if results_from_source and len(results_from_source) >= 16:
                    ra = _build_round_actuals(_sort_results_newest_first(results_from_source))
                    payload = dict(payload)
                    payload['round_actuals'] = ra
                    payload['result_source_used'] = base
                    print(f"[API] result_source 적용: {base} → round_actuals {len(ra)}건")
            except Exception as e:
                print(f"[API] result_source 조회 실패: {result_source} - {str(e)[:100]}")
        
        # 화면 맨 왼쪽 = 최신 회차 보장: 응답 직전에 game_id 기준 내림차순 강제 정렬 (캐시/병합 출처와 무관)
        if payload and payload.get('results'):
            payload = dict(payload)
            payload['results'] = _sort_results_newest_first(list(payload['results']))
            first_id = (payload['results'][0].get('gameID') if payload['results'] else None)
            print(f"[API] 응답 결과 수: {len(payload['results'])}개, 맨 앞(최신) gameID: {first_id}")
        
        resp = jsonify(payload)
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        resp.headers['Pragma'] = 'no-cache'
        return resp
    except Exception as e:
        import traceback
        error_msg = str(e)[:200]
        print(f"[❌ 오류] 결과 로드 실패: {error_msg}")
        print(traceback.format_exc()[:500])
        err_resp = jsonify({
            'results': [],
            'count': 0,
            'timestamp': datetime.now().isoformat(),
            'error': error_msg,
            'prediction_history': [],
            'server_prediction': {'value': None, 'round': 0, 'prob': 0, 'color': None, 'warning_u35': False, 'pong_chunk_phase': None, 'pong_chunk_debug': {}},
            'blended_win_rate': None,
            'round_actuals': {}
        })
        err_resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        return err_resp, 200


@app.route('/api/current-prediction', methods=['GET'])
def get_current_prediction():
    """예측픽만 경량 반환(캐시 기반). 화면에서 예측픽을 빨리 표시하기 위해 짧은 간격 폴링용."""
    global results_cache
    sp = None
    if results_cache and results_cache.get('server_prediction'):
        sp = results_cache['server_prediction']
    return jsonify({'server_prediction': sp})


@app.route('/api/calc-state', methods=['GET', 'POST'])
def api_calc_state():
    """GET: 계산기 상태 조회. session_id 없으면 새로 생성. POST: 계산기 상태 저장. running=true이고 started_at 없으면 서버가 started_at 설정."""
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
            _default = {'running': False, 'started_at': 0, 'history': [], 'capital': 1000000, 'base': 10000, 'odds': 1.97, 'duration_limit': 0, 'use_duration_limit': False, 'reverse': False, 'timer_completed': False, 'win_rate_reverse': False, 'win_rate_threshold': 50, 'lose_streak_reverse': False, 'lose_streak_reverse_threshold': 48, 'lose_streak_reverse_min_streak': 3, 'win_rate_direction_reverse': False, 'streak_suppress_reverse': False, 'shape_only_latest_next_pick': False, 'shape_prediction': False, 'last_trend_direction': None, 'martingale': False, 'martingale_type': 'pyo', 'target_enabled': False, 'target_amount': 0, 'pause_low_win_rate_enabled': False, 'pause_win_rate_threshold': 45, 'paused': False, 'max_win_streak_ever': 0, 'max_lose_streak_ever': 0, 'first_bet_round': 0, 'pending_round': None, 'pending_predicted': None, 'pending_prob': None, 'pending_color': None, 'pending_bet_amount': None}
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
                    'win_rate_threshold': max(0, min(100, int(c.get('win_rate_threshold') or 50))),
                    'lose_streak_reverse': bool(c.get('lose_streak_reverse')),
                    'lose_streak_reverse_threshold': max(0, min(100, int(c.get('lose_streak_reverse_threshold') or 48))),
                    'lose_streak_reverse_min_streak': max(2, min(15, int(c.get('lose_streak_reverse_min_streak') or 3))),
                    'win_rate_direction_reverse': bool(c.get('win_rate_direction_reverse')),
                    'streak_suppress_reverse': bool(c.get('streak_suppress_reverse')),
                    'lock_direction_on_lose_streak': bool(c.get('lock_direction_on_lose_streak', True)),
                    'shape_only_latest_next_pick': bool(c.get('shape_only_latest_next_pick')),
                    'shape_prediction': bool(c.get('shape_prediction')),
                    'shape_prediction_reverse': bool(c.get('shape_prediction_reverse')),
                    'shape_prediction_reverse_threshold': max(0, min(100, int(c.get('shape_prediction_reverse_threshold') or 50))),
                    'shape_weight': max(0, min(3, float(c.get('shape_weight', 1.5) or 1.5))),
                    'chunk_weight': max(0, min(3, float(c.get('chunk_weight', 1) or 1))),
                    'pong_weight': max(0, min(3, float(c.get('pong_weight', 1.5) or 1.5))),
                    'symmetry_weight': max(0, min(3, float(c.get('symmetry_weight', 1) or 1))),
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
                if current_c.get('pending_shape_debug') and c.get('pending_round') == current_c.get('pending_round'):
                    out[cid]['pending_shape_debug'] = current_c['pending_shape_debug']
            else:
                out[cid] = {'running': False, 'started_at': 0, 'history': [], 'capital': 1000000, 'base': 10000, 'odds': 1.97, 'duration_limit': 0, 'use_duration_limit': False, 'reverse': False, 'timer_completed': False, 'win_rate_reverse': False, 'win_rate_threshold': 50, 'lose_streak_reverse': False, 'lose_streak_reverse_threshold': 48, 'lose_streak_reverse_min_streak': 3, 'win_rate_direction_reverse': False, 'streak_suppress_reverse': False, 'lock_direction_on_lose_streak': True, 'shape_only_latest_next_pick': False, 'shape_prediction': False, 'shape_weight': 1.5, 'chunk_weight': 1, 'pong_weight': 1.5, 'symmetry_weight': 1, 'last_trend_direction': None, 'martingale': False, 'martingale_type': 'pyo', 'target_enabled': False, 'target_amount': 0, 'pause_low_win_rate_enabled': False, 'pause_win_rate_threshold': 45, 'paused': False, 'max_win_streak_ever': 0, 'max_lose_streak_ever': 0, 'first_bet_round': 0, 'pending_round': None, 'pending_predicted': None, 'pending_prob': None, 'pending_color': None, 'pending_bet_amount': None, 'last_win_rate_zone': None, 'last_win_rate_zone_change_round': None, 'last_win_rate_zone_on_win': None}
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
        _default = {'running': False, 'started_at': 0, 'history': [], 'capital': 1000000, 'base': 10000, 'odds': 1.97, 'duration_limit': 0, 'use_duration_limit': False, 'reverse': False, 'timer_completed': False, 'win_rate_reverse': False, 'win_rate_threshold': 50, 'lose_streak_reverse': False, 'lose_streak_reverse_threshold': 48, 'lose_streak_reverse_min_streak': 3, 'win_rate_direction_reverse': False, 'streak_suppress_reverse': False, 'shape_only_latest_next_pick': False, 'shape_prediction': False, 'last_trend_direction': None, 'martingale': False, 'martingale_type': 'pyo', 'target_enabled': False, 'target_amount': 0, 'pause_low_win_rate_enabled': False, 'pause_win_rate_threshold': 45, 'paused': False, 'max_win_streak_ever': 0, 'max_lose_streak_ever': 0, 'first_bet_round': 0, 'pending_round': None, 'pending_predicted': None, 'pending_prob': None, 'pending_color': None, 'pending_bet_amount': None, 'last_win_rate_zone': None, 'last_win_rate_zone_change_round': None}
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


def _backfill_blended_win_rate(conn):
    """기존 prediction_history 행 중 blended_win_rate가 null인 행을 과거 이력으로 채움."""
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute('SELECT round_num FROM prediction_history WHERE blended_win_rate IS NULL ORDER BY round_num ASC')
        null_rounds = [r[0] for r in cur.fetchall()]
        cur.close()
        for rn in null_rounds:
            hist = get_prediction_history_before_round(conn, rn, limit=100)
            comp = _blended_win_rate_components(hist)
            if not comp:
                continue
            r15, r30, r100, blended = comp
            cur2 = conn.cursor()
            cur2.execute('''
                UPDATE prediction_history SET blended_win_rate = %s, rate_15 = %s, rate_30 = %s, rate_100 = %s WHERE round_num = %s
            ''', (round(blended, 1), round(r15, 1), round(r30, 1), round(r100, 1), rn))
            cur2.close()
        conn.commit()
    except Exception as e:
        print(f"[경고] blended_win_rate backfill 실패: {str(e)[:150]}")


@app.route('/api/win-rate-buckets', methods=['GET'])
def api_win_rate_buckets():
    """합산승률 구간별 승/패 집계. prediction_history의 blended_win_rate 기준 5% 단위 구간(승률반픽 % 설정 참고용). ?backfill=1 시 null 행 보정."""
    if not DB_AVAILABLE or not DATABASE_URL:
        return jsonify({'buckets': []}), 200
    try:
        conn = get_db_connection(statement_timeout_sec=10)
        if not conn:
            return jsonify({'buckets': []}), 200
        if request.args.get('backfill') == '1':
            _backfill_blended_win_rate(conn)
        cur = conn.cursor()
        cur.execute('''
            SELECT round_num, predicted, actual, blended_win_rate
            FROM prediction_history
            WHERE blended_win_rate IS NOT NULL AND actual != 'joker'
            ORDER BY round_num ASC
        ''')
        rows = cur.fetchall()
        cur.close()
        conn.close()
        # 5% 단위 20개 구간 (0~5, 5~10, ..., 95~100) — 승률반픽 % 설정 시 참고
        buckets = {i: {'bucket_min': i * 5, 'bucket_max': i * 5 + 5, 'wins': 0, 'losses': 0} for i in range(20)}
        for r in rows:
            b = float(r[3]) if r[3] is not None else None
            if b is None:
                continue
            idx = min(19, max(0, int(b // 5)))
            win = 1 if r[1] == r[2] else 0
            buckets[idx]['wins'] += win
            buckets[idx]['losses'] += (1 - win)
        out = []
        recommended_upper = None  # 승률 50% 미만인 구간의 상한(맨 위 %)
        for i in range(20):
            d = buckets[i]
            total = d['wins'] + d['losses']
            win_pct = round(100 * d['wins'] / total, 1) if total > 0 else None
            out.append({
                'bucket_min': d['bucket_min'],
                'bucket_max': d['bucket_max'],
                'wins': d['wins'],
                'losses': d['losses'],
                'total': total,
                'win_pct': win_pct
            })
            # 권장값: 승률 50% 미만이고 표본이 충분한 구간(상한) 중 최대
            if total >= 5 and win_pct is not None and win_pct < 50:
                upper = d['bucket_max']
                if recommended_upper is None or upper > recommended_upper:
                    recommended_upper = upper
        return jsonify({'buckets': out, 'recommended_threshold': recommended_upper}), 200
    except Exception as e:
        print(f"[❌ 오류] win-rate-buckets 실패: {str(e)[:200]}")
        return jsonify({'buckets': [], 'error': str(e)[:200]}), 200


@app.route('/api/dont-bet-ranges', methods=['GET'])
def api_dont_bet_ranges():
    """2연패가 발생한 회차들의 예측확률 범위(최소~최대)를 구해, '몇%부터 몇%까지 2연패 했다면 배팅하지 마세요' 반환."""
    if not DB_AVAILABLE or not DATABASE_URL:
        return jsonify({'dont_bet_ranges': [], 'two_streak_count': 0}), 200
    try:
        limit = min(2000, max(300, int(request.args.get('limit', 1000))))
        history = get_prediction_history(limit)
        # 2연패 찾기: 연속 두 회차 모두 패(조커 제외)
        probs_in_2streak = []
        n = len(history or [])
        for i in range(n - 1):
            h0 = history[i] if i < len(history) else None
            h1 = history[i + 1] if i + 1 < len(history) else None
            if not h0 or not h1:
                continue
            if h0.get('actual') == 'joker' or h1.get('actual') == 'joker':
                continue
            loss0 = (h0.get('predicted') or '') != (h0.get('actual') or '')
            loss1 = (h1.get('predicted') or '') != (h1.get('actual') or '')
            if not (loss0 and loss1):
                continue
            for h in (h0, h1):
                prob = h.get('probability')
                if prob is not None:
                    try:
                        probs_in_2streak.append(float(prob))
                    except (TypeError, ValueError):
                        pass
        dont_bet_ranges = []
        if probs_in_2streak:
            min_p = min(probs_in_2streak)
            max_p = max(probs_in_2streak)
            dont_bet_ranges = [{'min': round(min_p), 'max': round(max_p)}]
        two_streak_count = sum(1 for i in range(n - 1) if _is_2streak_at(history, i))
        return jsonify({
            'dont_bet_ranges': dont_bet_ranges,
            'two_streak_count': two_streak_count,
        }), 200
    except Exception as e:
        print(f"[❌ 오류] dont-bet-ranges 실패: {str(e)[:200]}")
        return jsonify({'dont_bet_ranges': [], 'two_streak_count': 0, 'error': str(e)[:200]}), 200


def _is_2streak_at(history, i):
    """history에서 i, i+1이 둘 다 패(조커 제외)인지."""
    if not history or i + 1 >= len(history):
        return False
    h0, h1 = history[i], history[i + 1]
    if not h0 or not h1 or h0.get('actual') == 'joker' or h1.get('actual') == 'joker':
        return False
    return (h0.get('predicted') or '') != (h0.get('actual') or '') and (h1.get('predicted') or '') != (h1.get('actual') or '')


def _compute_losing_streaks(history, min_streak=3):
    """예측 이력에서 min_streak(기본 3)연패 이상 구간 추출. 조커 제외, round 오름차순 가정."""
    streaks = []
    current = []
    for h in (history or []):
        if not h or not isinstance(h, dict):
            continue
        actual = (h.get('actual') or '').strip()
        if actual == 'joker':
            if len(current) >= min_streak:
                streaks.append(list(current))
            current = []
            continue
        predicted = (h.get('predicted') or '').strip()
        is_loss = (predicted != actual)
        if is_loss:
            current.append({
                'round': h.get('round'),
                'probability': h.get('probability'),
            })
        else:
            if len(current) >= min_streak:
                streaks.append(list(current))
            current = []
    if len(current) >= min_streak:
        streaks.append(list(current))
    return streaks


@app.route('/api/losing-streaks', methods=['GET'])
def api_losing_streaks():
    """3연패 이상 구간 감지 후, 해당 구간의 예측확률 구간별 집계. 연패 구간 메뉴용."""
    if not DB_AVAILABLE or not DATABASE_URL:
        return jsonify({'prob_buckets': [], 'streaks': [], 'total_streak_rounds': 0}), 200
    try:
        limit = min(2000, max(300, int(request.args.get('limit', 500))))
        history = get_prediction_history(limit)
        streaks = _compute_losing_streaks(history, min_streak=3)
        # 예측확률 10% 단위 구간별: 연패 구간에 속한 회차 수
        prob_buckets = {i: {'bucket_min': i * 10, 'bucket_max': i * 10 + 10, 'count': 0} for i in range(10)}
        total_streak_rounds = 0
        for s in streaks:
            for r in s:
                total_streak_rounds += 1
                prob = r.get('probability')
                if prob is not None:
                    p = float(prob)
                    idx = min(9, max(0, int(p // 10)))
                    prob_buckets[idx]['count'] += 1
        out_buckets = []
        for i in range(10):
            d = prob_buckets[i]
            out_buckets.append({
                'bucket_min': d['bucket_min'],
                'bucket_max': d['bucket_max'],
                'count': d['count'],
            })
        # 최근 연패 구간 목록 (최대 20개): start_round, end_round, length, avg_prob
        streak_list = []
        for s in streaks[-20:]:
            if not s:
                continue
            rounds = [x.get('round') for x in s if x.get('round') is not None]
            probs = [float(x['probability']) for x in s if x.get('probability') is not None]
            start_r = min(rounds) if rounds else None
            end_r = max(rounds) if rounds else None
            avg_p = round(sum(probs) / len(probs), 1) if probs else None
            streak_list.append({
                'start_round': start_r,
                'end_round': end_r,
                'length': len(s),
                'avg_probability': avg_p,
            })
        streak_list.reverse()
        return jsonify({
            'prob_buckets': out_buckets,
            'streaks': streak_list,
            'total_streak_rounds': total_streak_rounds,
            'total_streaks': len(streaks),
        }), 200
    except Exception as e:
        print(f"[❌ 오류] losing-streaks 실패: {str(e)[:200]}")
        return jsonify({'prob_buckets': [], 'streaks': [], 'total_streak_rounds': 0, 'error': str(e)[:200]}), 200


@app.route('/api/round-prediction', methods=['POST'])
def api_save_round_prediction():
    """배팅중(예측) 나올 때마다 회차별로 즉시 저장. round, predicted 필수. pick_color, probability 선택."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        round_num = data.get('round')
        predicted = data.get('predicted')
        if round_num is None or predicted is None:
            return jsonify({'ok': False, 'error': 'round, predicted required'}), 400
        pick_color = data.get('pickColor') or data.get('pick_color')
        probability = data.get('probability')
        ok = save_round_prediction(int(round_num), str(predicted), pick_color=pick_color, probability=probability)
        return jsonify({'ok': ok}), 200
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:200]}), 200


@app.route('/api/prediction-history', methods=['POST'])
def api_save_prediction_history():
    """시스템 예측 기록 1건 저장 (round, predicted, actual, probability, pick_color). 어디서 접속해도 동일 기록 유지."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        round_num = data.get('round')
        predicted = data.get('predicted')
        actual = data.get('actual')
        if round_num is None or predicted is None or actual is None:
            return jsonify({'ok': False, 'error': 'round, predicted, actual required'}), 400
        probability = data.get('probability')
        pick_color = data.get('pickColor') or data.get('pick_color')
        if pick_color:
            s = str(pick_color).strip().upper()
            if s in ('RED', '빨강'): pick_color = '빨강'
            elif s in ('BLACK', '검정'): pick_color = '검정'
        ok = save_prediction_record(int(round_num), str(predicted), str(actual), probability=probability, pick_color=pick_color)
        return jsonify({'ok': ok}), 200
    except Exception as e:
        print(f"[❌ 오류] 예측 기록 API 실패: {str(e)[:200]}")
        return jsonify({'ok': False, 'error': str(e)[:200]}), 500


@app.route('/api/current-pick', methods=['GET', 'POST'])
def api_current_pick():
    """GET: 배팅 연동 현재 예측 픽 조회 (계산기별). POST: 프론트엔드가 픽 갱신 시 저장 (계산기별)."""
    empty_pick = {'pick_color': None, 'round': None, 'probability': None, 'suggested_amount': None, 'updated_at': None, 'running': True}
    try:
        if not bet_int or not DB_AVAILABLE or not DATABASE_URL:
            return jsonify(empty_pick if request.method == 'GET' else {'ok': False}), 200
        if request.method == 'GET':
            calculator_id = request.args.get('calculator', '1').strip()
            try:
                calculator_id = int(calculator_id) if calculator_id in ('1', '2', '3') else 1
            except (TypeError, ValueError):
                calculator_id = 1
            conn = get_db_connection(statement_timeout_sec=5)
            if not conn:
                return jsonify(empty_pick), 200
            ensure_current_pick_table(conn)
            conn.commit()
            out = bet_int.get_current_pick(conn, calculator_id=calculator_id)
            conn.close()
            # 목표 달성 등으로 계산기 중지(running=false)면 에뮬레이터에 픽을 보내지 않음 — 픽/회차 비움
            if out and out.get('running') is False:
                out = dict(out)
                out['pick_color'] = None
                out['round'] = None
                out['suggested_amount'] = None
            # GET 시: 계산기 상단 "배팅중" 금액만 사용 (클라이언트 POST 값 = DB 그대로 반환, 서버 재계산으로 덮어쓰지 않음)
            try:
                state = get_calc_state('default') or {}
                c = state.get(str(calculator_id)) if isinstance(state.get(str(calculator_id)), dict) else None
                if c and c.get('paused') and out and isinstance(out, dict):
                    out = dict(out)
                    out['suggested_amount'] = None
                # DB에 클라이언트(계산기 상단)가 POST한 값이 있으면 그대로 사용 — 충돌 방지
            except Exception:
                pass
            return jsonify(out if out else empty_pick), 200
        # POST: 테이블 없으면 생성 후 저장 (계산기별)
        data = request.get_json(force=True, silent=True) or {}
        calculator_id = data.get('calculator', 1)
        try:
            calculator_id = int(calculator_id) if calculator_id in (1, 2, 3) else 1
        except (TypeError, ValueError):
            calculator_id = 1
        pick_color = data.get('pickColor') or data.get('pick_color')
        round_num = data.get('round')
        probability = data.get('probability')
        suggested_amount = data.get('suggestedAmount') or data.get('suggested_amount')
        running = data.get('running')  # True/False 또는 없음 — 정지 시 클라이언트가 running: false 보내면 DB에 반영해 GET 시 픽 미반환
        # 픽/회차는 서버 calc 상태로 맞추고, 금액은 선택한 계산기 상단 "배팅중"에서 보낸 값(suggested_amount) 그대로 사용 — 매크로가 그 금액을 GET으로 받음
        try:
            state = get_calc_state('default') or {}
            c = state.get(str(calculator_id)) if isinstance(state.get(str(calculator_id)), dict) else None
            if c is not None and c.get('running') and c.get('pending_round') is not None:
                try:
                    srv_pick, server_amt, _ = _server_calc_effective_pick_and_amount(c)
                    pr = c.get('pending_round')
                    # 클라이언트 회차가 서버보다 크면 클라이언트 값 사용 — 1회차 느림 방지 (결과 반영 직후 클라이언트가 먼저 POST할 때)
                    try:
                        pr_int = int(pr) if pr is not None else None
                        cr_int = int(round_num) if round_num is not None else None
                        use_client_round = cr_int is not None and (pr_int is None or cr_int > pr_int)
                    except (TypeError, ValueError):
                        use_client_round = False
                    if use_client_round:
                        round_num = cr_int
                        # 픽/금액도 클라이언트 사용 (서버는 아직 새 회차 반영 전)
                        if pick_color:
                            pass  # 클라이언트 pick_color 그대로
                        try:
                            client_amt = int(suggested_amount) if suggested_amount is not None else 0
                            suggested_amount = client_amt if client_amt > 0 else (int(server_amt) if server_amt is not None else 0)
                        except (TypeError, ValueError):
                            suggested_amount = int(server_amt) if server_amt is not None else 0
                    else:
                        round_num = pr
                        if srv_pick is not None:
                            pick_color = srv_pick
                        try:
                            client_amt = int(suggested_amount) if suggested_amount is not None else 0
                            if client_amt > 0:
                                suggested_amount = client_amt
                            else:
                                suggested_amount = int(server_amt) if server_amt is not None else 0
                        except (TypeError, ValueError):
                            suggested_amount = int(server_amt) if server_amt is not None else 0
                except (TypeError, ValueError):
                    pass
            elif c is not None and c.get('paused'):
                suggested_amount = 0  # 서버가 멈춤이면 무조건 0 — 에뮬레이터 배팅 스킵
        except Exception:
            pass
        conn = get_db_connection(statement_timeout_sec=5)
        if not conn:
            return jsonify({'ok': False}), 200
        ensure_current_pick_table(conn)
        conn.commit()
        ok = bet_int.set_current_pick(conn, pick_color=pick_color, round_num=round_num, probability=probability, suggested_amount=suggested_amount, calculator_id=calculator_id)
        if ok:
            conn.commit()
            _update_current_pick_relay_cache(calculator_id, round_num, pick_color, suggested_amount, running if running is not None else True, probability)
            _log_when_changed('current_pick', (calculator_id, pick_color, round_num), lambda v: f"[배팅연동] 계산기{v[0]} 픽 저장: {v[1]} round {v[2]}")
        if running is not None:
            if bet_int.set_calculator_running(conn, calculator_id, bool(running)):
                conn.commit()
                c = _current_pick_relay_cache.get(calculator_id)
                if c and isinstance(c, dict):
                    c['running'] = bool(running)
        elif pick_color is not None:
            # 픽 저장 시 자동으로 running=True — 실행 중인 계산기로 복원
            if bet_int.set_calculator_running(conn, calculator_id, True):
                conn.commit()
        conn.close()
        return jsonify({'ok': ok}), 200
    except Exception as e:
        print(f"[❌ 오류] current-pick 실패: {str(e)[:200]}")
        return jsonify(empty_pick if request.method == 'GET' else {'ok': False}), 200


def _relay_db_write_background(calculator_id, pick_color, round_num, suggested_amount, running):
    """relay POST 시 DB 쓰기를 백그라운드로 수행. 응답 지연 없음."""
    if not bet_int or not DB_AVAILABLE or not DATABASE_URL:
        return
    try:
        conn = get_db_connection(statement_timeout_sec=3)
        if conn:
            try:
                ensure_current_pick_table(conn)
                bet_int.set_current_pick(conn, pick_color=pick_color, round_num=round_num,
                                         suggested_amount=suggested_amount, calculator_id=calculator_id)
                if running is not None:
                    bet_int.set_calculator_running(conn, calculator_id, bool(running))
                conn.commit()
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
    except Exception as e:
        print(f"[경고] relay DB 백그라운드 저장 실패: {str(e)[:80]}")


@app.route('/api/current-pick-relay', methods=['GET', 'POST'])
def api_current_pick_relay():
    """매크로 전용: DB 없이 메모리 캐시에서 즉시 반환. POST: DB에만 저장(relay 캐시는 스케줄러가 서버 금액으로 갱신)."""
    empty_pick = {'pick_color': None, 'round': None, 'probability': None, 'suggested_amount': None, 'running': True}
    try:
        if request.method == 'POST':
            data = request.get_json(silent=True) or {}
            calculator_id = data.get('calculator', 1)
            try:
                calculator_id = int(calculator_id) if calculator_id in (1, 2, 3) else 1
            except (TypeError, ValueError):
                calculator_id = 1
            pick_color = data.get('pickColor') or data.get('pick_color')
            round_num = data.get('round')
            suggested_amount = data.get('suggested_amount')
            running = data.get('running')
            # relay 캐시는 POST에서 갱신하지 않음 — 스케줄러가 0.2초마다 서버 금액으로 덮어써서 5천↔1만 깜빡임 방지
            # DB(current_pick)만 저장. 매크로 GET 시 relay 캐시(스케줄러가 채움) 또는 DB 폴백 사용
            # 금액 깜빡임 방지: 서버 calc 상태의 pending_bet_amount 우선 (웹·스케줄러 교차 시 5천↔1만 방지)
            try:
                state = get_calc_state('default') or {}
                c = state.get(str(calculator_id)) if isinstance(state.get(str(calculator_id)), dict) else None
                if c and c.get('running') and c.get('pending_round') == round_num and (c.get('pending_bet_amount') or 0) > 0:
                    suggested_amount = int(c.get('pending_bet_amount'))
            except Exception:
                pass
            threading.Thread(target=_relay_db_write_background, daemon=True,
                             args=(calculator_id, pick_color, round_num, suggested_amount, running)).start()
            return jsonify({'ok': True}), 200
        calculator_id = request.args.get('calculator', '1').strip()
        try:
            calculator_id = int(calculator_id) if calculator_id in ('1', '2', '3') else 1
        except (TypeError, ValueError):
            calculator_id = 1
        # 계산기 상단 "배팅중" 금액만 사용 — DB(클라이언트 POST) 우선, 캐시는 DB 없을 때만
        if bet_int and DB_AVAILABLE and DATABASE_URL:
            conn = get_db_connection(statement_timeout_sec=3)
            if conn:
                try:
                    ensure_current_pick_table(conn)
                    conn.commit()
                    out = bet_int.get_current_pick(conn, calculator_id=calculator_id)
                    if out:
                        # DB = 클라이언트(계산기 상단) POST 값 그대로 사용
                        _update_current_pick_relay_cache(calculator_id, out.get('round'), out.get('pick_color'),
                                                         out.get('suggested_amount'), out.get('running', True), out.get('probability'))
                        if out.get('running') is False:
                            out = dict(out)
                            out['pick_color'] = out['round'] = out['suggested_amount'] = None
                        return jsonify(out if out else empty_pick), 200
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
        cached = _current_pick_relay_cache.get(calculator_id)
        if cached and isinstance(cached, dict):
            out = dict(empty_pick)
            for k in ('round', 'pick_color', 'probability', 'suggested_amount', 'running'):
                if k in cached:
                    out[k] = cached[k]
            if out.get('running') is False:
                out['pick_color'] = None
                out['round'] = None
                out['suggested_amount'] = None
            return jsonify(out), 200
        return jsonify(empty_pick), 200
    except Exception as e:
        print(f"[경고] current-pick-relay 실패: {str(e)[:100]}")
        return jsonify(empty_pick), 200


# 배팅 사이트 URL (토큰하이로우). 필요 시 환경변수로 오버라이드 가능
BETTING_SITE_URL = os.getenv('BETTING_SITE_URL', 'https://nhs900.com')



@app.route('/betting-helper', methods=['GET'])
def betting_helper_page():
    """배팅 연동 페이지. 왼쪽 설정, 오른쪽 배팅 사이트 iframe. Tampermonkey 스크립트가 postMessage 수신."""
    return render_template(
        'betting_helper.html',
        betting_site_url=BETTING_SITE_URL,
        betting_site_url_json=json.dumps(BETTING_SITE_URL)
    )


@app.route('/practice', methods=['GET'])
def practice_page():
    """자동배팅 매크로 연습용 페이지. 에뮬레이터 브라우저에서 열고 좌표 잡은 뒤 매크로로 탭·금액 테스트. 마틴 로그 확인용."""
    return render_template('practice.html')


@app.route('/docs/tampermonkey-auto-bet.user.js', methods=['GET'])
def serve_tampermonkey_script():
    """Tampermonkey 자동배팅 스크립트 제공 (배팅 사이트에서 우리 API 픽으로 자동 입력·클릭)."""
    path = os.path.join(os.path.dirname(__file__), 'docs', 'tampermonkey-auto-bet.user.js')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            body = f.read()
        from flask import Response
        return Response(body, mimetype='application/javascript')
    except FileNotFoundError:
        return Response('// Script file not found', status=404, mimetype='application/javascript')


@app.route('/api/server-time', methods=['GET'])
def api_server_time():
    """매크로 네이버 시계 동기화용. 분당 4게임(15초 주기) 배팅 타이밍 계산에 사용."""
    return jsonify({'server_time': int(time.time())}), 200


@app.route('/api/current-status', methods=['GET'])
def get_current_status():
    """현재 게임 상태"""
    try:
        data = load_game_data()
        # 디버깅: 반환 데이터 확인
        red_count = len(data.get('currentBets', {}).get('red', []))
        black_count = len(data.get('currentBets', {}).get('black', []))
        _log_when_changed('current_status', (red_count, black_count), lambda v: f"[API 응답] RED: {v[0]}명, BLACK: {v[1]}명 | 구조: {list(data.keys())}")
        data['server_time'] = int(time.time())  # 계산기 경과시간용
        return jsonify(data), 200
    except Exception as e:
        # 에러 발생 시 기본값 반환 (서버 크래시 방지)
        print(f"게임 상태 로드 오류: {str(e)[:200]}")
        try:
            print(traceback.format_exc())
        except:
            pass
        return jsonify({
            'round': 0,
            'elapsed': 0,
            'currentBets': {'red': [], 'black': []},
            'timestamp': datetime.now().isoformat(),
            'server_time': int(time.time())
        }), 200

@app.route('/api/streaks', methods=['GET'])
def get_streaks():
    """연승 데이터"""
    try:
        data = load_streaks_data()
        if data:
            return jsonify(data), 200
        else:
            return jsonify({
                'userStreaks': {},
                'validGames': 0,
                'timestamp': datetime.now().isoformat()
            }), 200
    except Exception as e:
        print(f"연승 데이터 로드 오류: {str(e)[:200]}")
        return jsonify({
            'userStreaks': {},
            'validGames': 0,
            'timestamp': datetime.now().isoformat()
        }), 200

@app.route('/api/streaks/<user_id>', methods=['GET'])
def get_user_streak(user_id):
    """특정 유저 연승"""
    streaks_data = load_streaks_data()
    if not streaks_data:
        return jsonify({'error': '연승 데이터 로드 실패'}), 500
    
    user_streaks = streaks_data.get('userStreaks', {})
    user_data = user_streaks.get(user_id, {'red': 0, 'black': 0, 'hi': 0, 'lo': 0})
    
    max_streak = max(user_data.values())
    max_category = None
    for category, value in user_data.items():
        if value == max_streak and max_streak > 0:
            max_category = category
            break
    
    return jsonify({
        'userId': user_id,
        'streaks': user_data,
        'maxStreak': max_streak,
        'maxCategory': max_category,
        'isExpert': max_streak >= 3
    })

@app.route('/api/refresh', methods=['POST'])
def refresh_data():
    """데이터 갱신 (스레드+타임아웃으로 먹통 방지)"""
    global game_data_cache, streaks_cache, results_cache, last_update_time
    
    ref = [None, None, None]
    def _do_refresh():
        try:
            ref[0] = load_game_data()
            ref[1] = load_streaks_data()
            ref[2] = load_results_data()
        except Exception as e:
            print(f"[api/refresh] 오류: {str(e)[:150]}")
    t = threading.Thread(target=_do_refresh, daemon=True)
    t.start()
    t.join(timeout=15)
    
    game_data, streaks_data, results_data = ref[0], ref[1], ref[2]
    if game_data is not None:
        game_data_cache = game_data
    if streaks_data is not None:
        streaks_cache = streaks_data
    if results_data is not None:
        # 전체 구조(blended_win_rate 등) 포함해 캐시 갱신
        payload = _build_results_payload()
        if payload is not None:
            results_cache = payload
        else:
            # 폴백: 최소 구조 + blended_win_rate + round_actuals (100회 승률방향용 300건)
            ph = get_prediction_history(300)
            ph = _backfill_shape_predicted_in_ph(ph, results_data or [])
            blended = _blended_win_rate(ph)
            round_actuals = _build_round_actuals(results_data) if results_data else {}
            results_cache = {
                'results': results_data,
                'count': len(results_data),
                'timestamp': datetime.now().isoformat(),
                'prediction_history': ph,
                'server_prediction': {'value': None, 'round': 0, 'prob': 0, 'color': None, 'warning_u35': False, 'pong_chunk_phase': None, 'pong_chunk_debug': {}},
                'blended_win_rate': round(blended, 1) if blended is not None else None,
                'round_actuals': round_actuals
            }
    if game_data is not None or streaks_data is not None or results_data is not None:
        last_update_time = time.time() * 1000

    return jsonify({
        'success': True,
        'gameData': game_data is not None,
        'streaksData': streaks_data is not None,
        'resultsData': results_data is not None,
        'timestamp': datetime.now().isoformat()
    })

@app.route('/health', methods=['GET'])
def health_check():
    """헬스 체크 - Railway 헬스체크용 (외부 API 호출 없음)"""
    # Railway 헬스체크를 위해 즉시 응답 (외부 API 호출 없음)
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now().isoformat()
    }), 200

@app.route('/', methods=['GET'])
def index():
    """루트 - 분석기 페이지로 이동 (항상 내용이 보이도록)"""
    return redirect('/results', code=302)

@app.route('/api/test-betting', methods=['GET'])
def test_betting():
    """베팅 데이터 테스트 엔드포인트 (디버깅용)"""
    try:
        data = load_game_data()
        return jsonify({
            'success': True,
            'data': data,
            'red_count': len(data.get('currentBets', {}).get('red', [])),
            'black_count': len(data.get('currentBets', {}).get('black', [])),
            'red_sample': data.get('currentBets', {}).get('red', [])[:3] if len(data.get('currentBets', {}).get('red', [])) > 0 else [],
            'black_sample': data.get('currentBets', {}).get('black', [])[:3] if len(data.get('currentBets', {}).get('black', [])) > 0 else []
        }), 200
    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 500

@app.route('/api/debug/db-status', methods=['GET'])
def debug_db_status():
    """데이터베이스 상태 확인 (디버깅용)"""
    try:
        status = {
            'db_available': DB_AVAILABLE,
            'database_url_set': bool(DATABASE_URL),
            'database_url_length': len(DATABASE_URL) if DATABASE_URL else 0
        }
        
        if not DB_AVAILABLE or not DATABASE_URL:
            return jsonify(status), 200
        
        # 데이터베이스 연결 테스트
        conn = get_db_connection()
        if not conn:
            status['connection'] = 'failed'
            return jsonify(status), 200
        
        try:
            cur = conn.cursor()
            
            # game_results 테이블 확인
            cur.execute('''
                SELECT COUNT(*) as count,
                       COUNT(DISTINCT game_id) as unique_count,
                       MIN(created_at) as oldest,
                       MAX(created_at) as newest
                FROM game_results
            ''')
            game_results_row = cur.fetchone()
            
            # color_matches 테이블 확인
            cur.execute('''
                SELECT COUNT(*) as count,
                       COUNT(DISTINCT (game_id, compare_game_id)) as unique_count
                FROM color_matches
            ''')
            color_matches_row = cur.fetchone()
            
            # 최근 15개 게임 결과 샘플
            cur.execute('''
                SELECT game_id, result, created_at
                FROM game_results
                ORDER BY created_at DESC
                LIMIT 15
            ''')
            recent_games = [{'game_id': row[0], 'result': row[1], 'created_at': str(row[2])} 
                           for row in cur.fetchall()]
            
            # 최근 15개 정/꺽 결과 샘플
            cur.execute('''
                SELECT game_id, compare_game_id, match_result, created_at
                FROM color_matches
                ORDER BY created_at DESC
                LIMIT 15
            ''')
            recent_matches = [{'game_id': row[0], 'compare_game_id': row[1], 
                              'match_result': row[2], 'created_at': str(row[3])} 
                             for row in cur.fetchall()]
            
            status.update({
                'connection': 'success',
                'game_results': {
                    'total_count': game_results_row[0],
                    'unique_count': game_results_row[1],
                    'oldest': str(game_results_row[2]) if game_results_row[2] else None,
                    'newest': str(game_results_row[3]) if game_results_row[3] else None,
                    'recent_samples': recent_games
                },
                'color_matches': {
                    'total_count': color_matches_row[0],
                    'unique_count': color_matches_row[1],
                    'recent_samples': recent_matches
                }
            })
            
            cur.close()
            conn.close()
        except Exception as e:
            status['error'] = str(e)[:200]
            try:
                conn.close()
            except:
                pass
        
        return jsonify(status), 200
    except Exception as e:
        import traceback
        return jsonify({
            'error': str(e),
            'traceback': traceback.format_exc()[:500]
        }), 500

@app.route('/api/debug/init-db', methods=['POST'])
def debug_init_db():
    """데이터베이스 테이블 수동 생성 (디버깅용)"""
    try:
        result = ensure_database_initialized()
        return jsonify({
            'success': result,
            'message': '데이터베이스 초기화 완료' if result else '데이터베이스 초기화 실패',
            'db_available': DB_AVAILABLE,
            'database_url_set': bool(DATABASE_URL)
        }), 200 if result else 500
    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc()[:500]
        }), 500

@app.route('/api/debug/results-check', methods=['GET'])
def debug_results_check():
    """결과 데이터 점검 (디버깅용)"""
    try:
        # 최신 데이터 로드
        latest_results = load_results_data()
        
        # DB 데이터 조회
        db_results = []
        if DB_AVAILABLE and DATABASE_URL:
            db_results = get_recent_results(hours=24)
        
        # 병합
        if latest_results:
            latest_game_ids = {str(r.get('gameID', '')) for r in latest_results if r.get('gameID')}
            db_results_filtered = [r for r in db_results if str(r.get('gameID', '')) not in latest_game_ids]
            merged_results = latest_results + db_results_filtered
        else:
            merged_results = db_results
        
        # colorMatch 확인
        color_match_info = []
        for i in range(min(15, len(merged_results))):
            if i + 15 < len(merged_results):
                current = merged_results[i]
                compare = merged_results[i + 15]
                color_match_info.append({
                    'index': i + 1,
                    'current_game_id': current.get('gameID'),
                    'current_result': current.get('result'),
                    'compare_game_id': compare.get('gameID'),
                    'compare_result': compare.get('result'),
                    'has_color_match': 'colorMatch' in current,
                    'color_match_value': current.get('colorMatch'),
                    'current_joker': current.get('joker'),
                    'compare_joker': compare.get('joker')
                })
        
        return jsonify({
            'latest_results_count': len(latest_results) if latest_results else 0,
            'db_results_count': len(db_results),
            'merged_results_count': len(merged_results),
            'color_match_info': color_match_info,
            'sample_latest': latest_results[:3] if latest_results else [],
            'sample_db': db_results[:3] if db_results else [],
            'sample_merged': merged_results[:3] if merged_results else []
        }), 200
    except Exception as e:
        import traceback
        return jsonify({
            'error': str(e),
            'traceback': traceback.format_exc()[:500]
        }), 500

@app.route('/favicon.ico', methods=['GET'])
def favicon():
    """favicon 404 에러 방지"""
    return '', 204  # No Content

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    print(f"[✅ 정보] Flask 서버 시작: http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port, debug=False)
