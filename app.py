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
except ImportError:
    SCHEDULER_AVAILABLE = False

app = Flask(__name__)
CORS(app)

# 환경 변수
BASE_URL = os.getenv('BASE_URL', 'http://tgame365.com')
DATA_PATH = ''
TIMEOUT = int(os.getenv('TIMEOUT', '10'))
MAX_RETRIES = int(os.getenv('MAX_RETRIES', '2'))
DATABASE_URL = os.getenv('DATABASE_URL', None)

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
        for col, typ in [('probability', 'REAL'), ('pick_color', 'VARCHAR(10)')]:
            try:
                cur.execute('ALTER TABLE prediction_history ADD COLUMN ' + col + ' ' + typ)
                conn.commit()
            except Exception:
                pass
        
        # calc_sessions: 계산기 상태 서버 저장 (새로고침/재접속 후에도 실행중 유지)
        cur.execute('''
            CREATE TABLE IF NOT EXISTS calc_sessions (
                session_id VARCHAR(64) PRIMARY KEY,
                state_json TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            cur.execute('INSERT INTO current_pick (id) VALUES (1) ON CONFLICT (id) DO NOTHING')
        except Exception as ex:
            print(f"[경고] current_pick 테이블 생성/초기화 건너뜀 (서버는 계속 기동): {str(ex)[:100]}")
        
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
        cur.execute('INSERT INTO current_pick (id) VALUES (1) ON CONFLICT (id) DO NOTHING')
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


def save_prediction_record(round_num, predicted, actual, probability=None, pick_color=None):
    """시스템 예측 기록 1건 저장. statement_timeout으로 먹통 방지."""
    if not DB_AVAILABLE or not DATABASE_URL:
        return False
    conn = get_db_connection(statement_timeout_sec=5)
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO prediction_history (round_num, predicted, actual, probability, pick_color)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (round_num) DO UPDATE SET predicted = EXCLUDED.predicted, actual = EXCLUDED.actual,
                probability = EXCLUDED.probability, pick_color = EXCLUDED.pick_color, created_at = DEFAULT
        ''', (int(round_num), str(predicted), str(actual), float(probability) if probability is not None else None, str(pick_color) if pick_color else None))
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
            SELECT round_num as "round", predicted, actual, probability, pick_color
            FROM prediction_history
            ORDER BY round_num DESC
            LIMIT %s
        ''', (limit,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        # 프론트와 맞추기: 과거→현재 순 (round 오름차순)
        out = []
        for r in reversed(rows):
            o = {'round': r['round'], 'predicted': r['predicted'], 'actual': r['actual']}
            if r.get('probability') is not None:
                o['probability'] = float(r['probability'])
            if r.get('pick_color'):
                o['pickColor'] = str(r['pick_color'])
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
    """카드 결과 문자열에서 색상 추출 (빨강/검정)"""
    if not result_str:
        return None
    
    # 첫 글자가 문양인지 확인
    first_char = result_str[0].upper()
    if first_char in ['H', 'D']:  # 하트, 다이아몬드 = 빨강
        return True
    elif first_char in ['S', 'C']:  # 스페이드, 클럽 = 검정
        return False
    return None

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
            current_color = parse_card_color(current_result.get('result', ''))
            compare_color = parse_card_color(compare_result.get('result', ''))
            
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
            print(f"[✅] 정/꺽 결과 {saved_count}개 저장 완료")
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
        try:
            return (-int(g), '')  # 숫자면 높은 ID가 앞으로
        except ValueError:
            return (0, g)  # 문자열이면 그대로
    return sorted(results, key=key_fn)


def get_recent_results(hours=5):
    """최근 N시간 데이터 조회 (정/꺽 결과 포함). statement_timeout·LIMIT으로 먹통 방지."""
    if not DB_AVAILABLE or not DATABASE_URL:
        return []
    
    conn = get_db_connection(statement_timeout_sec=8)
    if not conn:
        return []
    
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # 최근 N시간 데이터 조회, LIMIT 2000으로 과부하 방지
        cur.execute('''
            SELECT game_id as "gameID", result, hi, lo, red, black, jqka, joker,
                   hash_value as hash, salt_value as salt
            FROM game_results
            WHERE created_at >= NOW() - (INTERVAL '1 hour' * %s)
            ORDER BY created_at DESC
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
                current_color = parse_card_color(results[idx].get('result', ''))
                compare_color = parse_card_color(results[idx + 15].get('result', ''))
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
CACHE_TTL = 2000

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

# 모듈 로드 시 DB 초기화는 백그라운드 스레드에서 (앱 시작 블로킹 방지)
def _run_db_init():
    try:
        time.sleep(1)
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

# 10초 게임: 결과를 빠르게 가져오고 예측 타이밍을 맞추기 위한 설정
GAME_ROUND_SECONDS = 10          # 한 라운드 약 10초
SCHEDULER_INTERVAL_SECONDS = 5   # 5초마다 수집 → 한 라운드에 2번 체크로 결과 빠른 반영
# 외부 result.json 요청 시 타임아웃 (먹통 방지, 초 단위)
RESULTS_FETCH_TIMEOUT = 3
RESULTS_FETCH_MAX_RETRIES = 1

def load_results_data():
    """경기 결과 데이터 로드 (result.json) - 짧은 타임아웃으로 먹통 방지"""
    possible_paths = [
        f"{BASE_URL}/frame/hilo/result.json",
        f"{BASE_URL}/result.json",
        f"{BASE_URL}/hilo/result.json",
        f"{BASE_URL}/frame/result.json",
    ]
    for url_path in possible_paths:
        try:
            url = f"{url_path}?t={int(time.time() * 1000)}"
            print(f"[결과 데이터 요청 시도] {url}")
            response = fetch_with_retry(
                url,
                max_retries=RESULTS_FETCH_MAX_RETRIES,
                silent=True,
                timeout_sec=RESULTS_FETCH_TIMEOUT,
            )
            
            if response:
                print(f"[✅ 결과 데이터 성공] {url}")
                try:
                    data = response.json()
                    print(f"[결과 데이터 파싱] 받은 데이터 개수: {len(data) if isinstance(data, list) else '리스트 아님'}")
                    
                    # 결과 파싱
                    results = []
                    for game in data:
                        try:
                            game_id = game.get('gameID', '')
                            result = game.get('result', '')
                            json_str = game.get('json', '{}')
                            
                            # JSON 파싱
                            if isinstance(json_str, str):
                                json_data = json.loads(json_str)
                            else:
                                json_data = json_str
                            
                            # 실제 데이터 구조에 맞게 파싱 (boolean 값)
                            results.append({
                                'gameID': str(game_id),  # 문자열로 변환
                                'result': result,
                                'hi': json_data.get('hi', False),
                                'lo': json_data.get('lo', False),
                                'red': json_data.get('red', False),
                                'black': json_data.get('black', False),
                                'jqka': json_data.get('jqka', False),
                                'joker': json_data.get('joker', False),
                                'hash': game.get('hash', ''),
                                'salt': game.get('salt', '')
                            })
                        except Exception as e:
                            # 개별 게임 파싱 오류는 무시
                            print(f"[결과 파싱 오류] {str(e)[:100]}")
                            continue
                    
                    print(f"[결과 데이터 최종] {len(results)}개 게임 결과 파싱 완료")
                    
                    # 데이터베이스에 저장 (비동기로 처리하지 않음 - 순차적으로 저장)
                    if DB_AVAILABLE and DATABASE_URL:
                        saved_count = 0
                        for game_data in results:
                            if save_game_result(game_data):
                                saved_count += 1
                        if saved_count > 0:
                            print(f"[💾] 데이터베이스에 {saved_count}개 결과 저장 완료")
                        
                        # 정/꺽 결과 계산 및 저장 (30개 이상일 때만)
                        if len(results) >= 16:
                            calculate_and_save_color_matches(results)
                    
                    return results
                except (ValueError, json.JSONDecodeError) as e:
                    print(f"[결과 JSON 파싱 오류] {str(e)[:200]}")
                    continue  # 다음 경로 시도
            else:
                print(f"[❌ 결과 데이터 실패] {url} - 다음 경로 시도")
                continue  # 다음 경로 시도
        except Exception as e:
            print(f"[결과 데이터 오류] {url_path}: {str(e)[:100]}")
            continue  # 다음 경로 시도
    
    # 모든 경로 실패
    print(f"[경고] 모든 경로에서 결과 데이터를 가져올 수 없음")
    return []


# ----- 서버용 예측 공식 (10초 게임 타이밍에 맞춰 스케줄러에서 실행) -----
def _server_graph_values(results):
    """results(최신순)에서 정/꺽 배열. i번째 = results[i] vs results[i+15] 색상 일치 여부."""
    if not results or len(results) < 16:
        return []
    out = []
    for i in range(len(results) - 15):
        cur, cmp = results[i], results[i + 15]
        if cur.get('joker') or cmp.get('joker'):
            out.append(None)
            continue
        cm = cur.get('colorMatch')
        if cm is not None:
            out.append(bool(cm))
            continue
        c1 = parse_card_color(cur.get('result', ''))
        c2 = parse_card_color(cmp.get('result', ''))
        out.append(c1 is not None and c2 is not None and c1 == c2)
    return out


def _server_calc_transitions(arr):
    """정/꺽 배열에서 전이 확률 (jj,jk,kj,kk, jungDenom, kkukDenom)."""
    jj = jk = kj = kk = 0
    for i in range(len(arr) - 1):
        a, b = arr[i], arr[i + 1]
        if a is None or b is None:
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
    return {'jj': jj, 'jk': jk, 'kj': kj, 'kk': kk, 'jungDenom': jung_denom, 'kkukDenom': kkuk_denom}


def _server_pong_line_pct(arr):
    """최근 15개 정/꺽에서 퐁당%/줄%."""
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
    pong_pct = 100.0 * alt / tot if tot else 50.0
    line_pct = 100.0 * same / tot if tot else 50.0
    return pong_pct, line_pct


def _server_line_pong_runs(arr):
    """1=줄, 0=퐁당 쌍으로 run 길이 리스트."""
    pairs = []
    for i in range(len(arr) - 1):
        a, b = arr[i], arr[i + 1]
        if a not in (True, False) or b not in (True, False):
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


def _server_symmetry_20(arr20):
    """20개 정/꺽에서 좌/우 줄 개수, 대칭%, maxLeftRun, recentRunLength."""
    if not arr20 or len(arr20) < 20:
        return None
    valid = [x for x in arr20[:20] if x is True or x is False]
    if len(valid) < 20:
        return None
    sym_count = sum(1 for si in range(10) if valid[si] == valid[19 - si])
    left10, right10 = valid[:10], valid[10:20]

    def run_lengths(a):
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

    left_runs = run_lengths(left10)
    right_runs = run_lengths(right10)
    avg_l = sum(left_runs) / len(left_runs) if left_runs else 0
    avg_r = sum(right_runs) / len(right_runs) if right_runs else 0
    max_left_run = max(left_runs) if left_runs else 0
    recent_run = 1
    for ri in range(1, len(valid)):
        if valid[ri] == valid[0]:
            recent_run += 1
        else:
            break
    return {
        'symmetryPct': sym_count / 10.0 * 100,
        'leftLineCount': len(left_runs),
        'rightLineCount': len(right_runs),
        'avgLeft': avg_l,
        'avgRight': avg_r,
        'maxLeftRunLength': max_left_run,
        'recentRunLength': recent_run,
    }


def compute_server_prediction(results, prediction_history):
    """서버에서 다음 회차 예측 (정/꺽). results=최신순, prediction_history=과거 예측 기록 리스트. 반환: (predict, pred_prob, is15joker, predicted_round)."""
    try:
        if not results or len(results) < 16:
            return None
        graph_values = _server_graph_values(results)
        if not graph_values:
            return None
        # 15번 카드 조커면 보류
        is15joker = len(results) >= 15 and bool(results[14].get('joker'))
        if is15joker:
            try:
                latest_gid = results[0].get('gameID') or '0'
                predicted_round = int(latest_gid) + 1
            except (TypeError, ValueError):
                predicted_round = 0
            return ('정', 50.0, True, predicted_round)

        full = _server_calc_transitions(graph_values)
        recent30 = _server_calc_transitions(graph_values[:30]) if len(graph_values) >= 30 else full
        short15 = _server_calc_transitions(graph_values[:15]) if len(graph_values) >= 15 else None
        last = graph_values[0]
        pong_pct, line_pct = _server_pong_line_pct(graph_values[:15])
        line_pct = min(100, max(0, line_pct))
        pong_pct = min(100, max(0, pong_pct))

        # flowState: 단기 vs 장기 유지 확률 차이
        flow_state = ''
        if short15 and recent30:
            if last is True:
                long_same = 100 * recent30['jj'] / recent30['jungDenom'] if recent30['jungDenom'] else 50
                short_same = 100 * short15['jj'] / short15['jungDenom'] if short15['jungDenom'] else 50
            else:
                long_same = 100 * recent30['kk'] / recent30['kkukDenom'] if recent30['kkukDenom'] else 50
                short_same = 100 * short15['kk'] / short15['kkukDenom'] if short15['kkukDenom'] else 50
            if short_same - long_same >= 15:
                flow_state = 'line_strong'
            elif long_same - short_same >= 15:
                flow_state = 'pong_strong'

        # 30회 패턴 지수
        use30 = graph_values[:30] if len(graph_values) >= 30 else graph_values
        line_runs, pong_runs = _server_line_pong_runs(use30)
        total_line = len(line_runs)
        total_pong = len(pong_runs)
        line_two_plus = sum(1 for l in line_runs if l >= 2)
        line_one = sum(1 for l in line_runs if l == 1)
        line_two = sum(1 for l in line_runs if l == 2)
        pong_one = sum(1 for p in pong_runs if p == 1)
        chunk_idx = line_two_plus / total_line if total_line else 0
        scatter_idx = (line_one / total_line * pong_one / total_pong) if total_line and total_pong else 0
        two_one_idx = (line_two / total_line * pong_one / total_pong) if total_line and total_pong else 0

        # 전이 확률 (직전이 정이면 정 유지/꺽 전이)
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
            line_w = min(1, line_w + 0.25)
            pong_w = max(0, 1 - line_w)
        elif flow_state == 'pong_strong':
            pong_w = min(1, pong_w + 0.25)
            line_w = max(0, 1 - pong_w)

        sym = _server_symmetry_20(graph_values[:20]) if len(graph_values) >= 20 else None
        if sym:
            lc, rc, sp = sym['leftLineCount'], sym['rightLineCount'], sym['symmetryPct']
            if (rc >= 5 and lc <= 3):
                line_w = min(1, line_w + 0.22)
                pong_w = max(0, 1 - line_w)
            elif sp >= 70 and rc <= 3:
                line_w = min(1, line_w + 0.28)
                pong_w = max(0, 1 - line_w)
            else:
                if lc <= 3:
                    line_w = min(1, line_w + 0.15)
                    pong_w = max(0, 1 - line_w)
                elif lc >= 5:
                    max_run = sym.get('maxLeftRunLength', 4)
                    recent_run = sym.get('recentRunLength', 0)
                    boost = 0.06 if (max_run <= 3 or recent_run >= 2) else 0.15
                    pong_w = min(1, pong_w + boost)
                    line_w = max(0, 1 - pong_w)
                if sp >= 70:
                    line_w = min(1, line_w + 0.05)
                elif sp <= 30:
                    line_w *= 0.95
                    pong_w *= 0.95

        line_w += chunk_idx * 0.2 + two_one_idx * 0.1
        pong_w += scatter_idx * 0.2
        total_w = line_w + pong_w
        if total_w > 0:
            line_w /= total_w
            pong_w /= total_w
        adj_same = prob_same * line_w
        adj_change = prob_change * pong_w
        s = adj_same + adj_change or 1
        adj_same_n = adj_same / s
        adj_change_n = adj_change / s
        predict = ('정' if last is True else '꺽') if adj_same_n >= adj_change_n else ('꺽' if last is True else '정')
        pred_prob = (adj_same_n if predict == ('정' if last is True else '꺽') else adj_change_n) * 100

        try:
            latest_gid = results[0].get('gameID') or '0'
            predicted_round = int(latest_gid) + 1
        except (TypeError, ValueError):
            predicted_round = 0
        return (predict, min(100, max(0, pred_prob)), False, predicted_round)
    except Exception as e:
        print(f"[서버예측] 오류: {str(e)[:150]}")
        return None


def _process_calc_rounds_after_results():
    """결과 수집 후 실행: 실행 중인 계산기의 pending 회차 반영 + 다음 예측 계산·저장."""
    try:
        results = get_recent_results(hours=1)
        if not results or len(results) < 16:
            return
        prediction_history = get_prediction_history(100)
        pred_out = compute_server_prediction(results, prediction_history)
        if not pred_out:
            return
        predict, pred_prob, is15joker, predicted_round = pred_out
        latest_round = int(results[0].get('gameID') or 0) if results else 0

        # 모든 세션의 계산기 상태 조회 (DB 또는 메모리)
        sessions_to_check = []
        if DB_AVAILABLE and DATABASE_URL:
            conn = get_db_connection(statement_timeout_sec=5)
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute('SELECT session_id, state_json FROM calc_sessions')
                    for row in cur.fetchall():
                        if row[0] and row[1]:
                            try:
                                sessions_to_check.append((row[0], json.loads(row[1])))
                            except (TypeError, ValueError):
                                pass
                    cur.close()
                    conn.close()
                except Exception as e:
                    print(f"[서버계산기] DB 조회 오류: {str(e)[:100]}")
                    try:
                        conn.close()
                    except Exception:
                        pass
        for sk, state in _calc_state_memory.items():
            sessions_to_check.append((sk, state))

        for session_id, state in sessions_to_check:
            if not isinstance(state, dict):
                continue
            updated = False
            for cid in ('1', '2', '3'):
                c = state.get(cid)
                if not c or not c.get('running'):
                    continue
                hist = c.get('history') or []
                pending_round = c.get('pending_round')
                pending_predicted = c.get('pending_predicted')

                if pending_round is not None and pending_predicted and latest_round == pending_round:
                    # 이번 결과가 pending 회차 → 기록
                    is_joker = results[0].get('joker') if results else False
                    actual = 'joker' if is_joker else ('정' if results[0].get('colorMatch') else '꺽')
                    rev = bool(c.get('reverse'))
                    pred = ('꺽' if pending_predicted == '정' else '정') if rev else pending_predicted
                    thr = max(0, min(100, int(c.get('win_rate_threshold') or 50)))
                    use_wr = bool(c.get('win_rate_reverse'))
                    if use_wr and prediction_history:
                        vh = [h for h in prediction_history if h and isinstance(h, dict)]
                        v15 = vh[-15:]
                        hit = sum(1 for h in v15 if h.get('actual') != 'joker' and h.get('predicted') == h.get('actual'))
                        loss = sum(1 for h in v15 if h.get('actual') != 'joker' and h.get('predicted') != h.get('actual'))
                        r15 = 100 * hit / (hit + loss) if (hit + loss) else 50
                        blended = r15
                        if blended <= thr:
                            pred = '꺽' if pred == '정' else '정'
                    hist = list(hist) + [{'predicted': pred, 'actual': actual, 'round': pending_round}]
                    c['history'] = hist[-500:]
                    save_prediction_record(pending_round, pending_predicted, actual, probability=pred_prob if not is_joker else None, pick_color=None)
                    updated = True

                if not is15joker:
                    c['pending_round'] = predicted_round
                    c['pending_predicted'] = predict
                else:
                    c['pending_round'] = predicted_round
                    c['pending_predicted'] = '정'
                updated = True

            if updated:
                save_calc_state(session_id, state)
    except Exception as e:
        print(f"[서버계산기] 회차 반영 오류: {str(e)[:200]}")


def _scheduler_fetch_results():
    """스케줄러에서 호출: 외부 결과 수집·DB 저장 후 즉시 예측·pending 반영 (10초 게임 타이밍)"""
    try:
        load_results_data()
        _process_calc_rounds_after_results()
    except Exception as e:
        print(f"[스케줄러] 결과 수집/예측 반영 오류: {str(e)[:150]}")


if SCHEDULER_AVAILABLE:
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _scheduler_fetch_results,
        'interval',
        seconds=SCHEDULER_INTERVAL_SECONDS,
        id='fetch_results',
        max_instances=1,
    )
    _scheduler.start()
    print(f"[✅] 10초 게임 결과 수집 스케줄러 시작 ({SCHEDULER_INTERVAL_SECONDS}초마다, 결과·예측 타이밍 반영)")
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
            padding: clamp(12px, 2.5vw, 20px);
            background: rgba(255,255,255,0.04);
            border: 1px solid #444;
            border-radius: 12px;
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
            font-size: clamp(0.85em, 2vw, 0.95em);
            font-weight: bold;
            color: #81c784;
            margin-bottom: clamp(6px, 1.5vw, 10px);
        }
        .prediction-pick-title.prediction-pick-title-betting {
            color: #ffeb3b;
            animation: prediction-blink 1s ease-in-out infinite;
        }
        @keyframes prediction-blink { 50% { opacity: 0.7; } }
        .prediction-pick .pred-round {
            margin-top: 4px;
            font-size: 0.95em;
            font-weight: bold;
            color: #81c784;
        }
        .prediction-card {
            width: clamp(64px, 22vw, 140px);
            height: clamp(64px, 22vw, 140px);
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
            font-size: clamp(1.6em, 5.5vw, 3.2em);
            font-weight: 900;
            color: #fff;
            text-shadow: 0 0 12px rgba(255,255,255,0.4);
        }
        .prediction-card.card-red .pred-value-big { color: #fff; text-shadow: 0 0 12px rgba(255,255,255,0.5); }
        .prediction-card.card-black .pred-value-big { color: #e0e0e0; }
        .prediction-prob-under {
            margin-top: 8px;
            font-size: clamp(0.85em, 2vw, 0.95em);
            color: #81c784;
            font-weight: bold;
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
        .prediction-streak-line { margin-top: 8px; font-size: clamp(0.9em, 2vw, 1em); color: #bbb; text-align: center; }
        .prediction-streak-line .streak-win { color: #ffeb3b; font-weight: bold; }
        .prediction-streak-line .streak-lose { color: #c62828; font-weight: bold; }
        .prediction-streak-line .streak-joker { color: #64b5f6; }
        .main-streak-table { width: 100%; margin-top: 8px; border-collapse: collapse; font-size: clamp(0.65em, 1.5vw, 0.75em); }
        .main-streak-table th, .main-streak-table td { padding: 3px 5px; border: 1px solid #444; text-align: center; background: #2a2a2a; }
        .main-streak-table th { color: #81c784; background: #333; white-space: nowrap; }
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
        .calc-current-card { display: inline-block; text-align: center; vertical-align: middle; border: 1px solid #555; box-sizing: border-box; color: #fff; }
        .calc-current-card.calc-card-betting { width: 44px; height: 36px; line-height: 36px; font-size: 1em; font-weight: bold; }
        .calc-current-card.calc-card-prediction { width: 22px; height: 18px; line-height: 18px; font-size: 0.75em; }
        .calc-current-card.card-jung { background: #b71c1c; }
        .calc-current-card.card-kkuk { background: #111; }
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
        .calc-settings-table input[type="checkbox"] { margin: 0; }
        .calc-settings-table select { padding: 4px 6px; border-radius: 4px; border: 1px solid #555; background: #1a1a1a; color: #fff; }
        .calc-target-hint { margin-left: 4px; }
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
        .calc-round-table-wrap { margin-bottom: 6px; overflow-x: auto; }
        .calc-round-table { width: 100%; border-collapse: collapse; font-size: 0.8em; }
        .calc-round-table th, .calc-round-table td { padding: 4px 6px; border: 1px solid #444; text-align: center; }
        .calc-round-table th { background: #333; color: #81c784; }
        .calc-round-table td.pick-jung { background: #b71c1c; color: #fff; }
        .calc-round-table td.pick-kkuk { background: #111; color: #fff; }
        .calc-round-table .win { color: #ffeb3b; font-weight: 600; }
        .calc-round-table .lose { color: #c62828; font-weight: 500; }
        .calc-round-table .joker { color: #64b5f6; }
        .calc-round-table .skip { color: #666; }
        .calc-round-table .calc-td-bet { text-align: right; white-space: nowrap; }
        .calc-round-table .calc-td-profit { text-align: right; white-space: nowrap; }
        .calc-round-table .profit-plus { color: #81c784; font-weight: 600; }
        .calc-round-table .profit-minus { color: #e57373; font-weight: 500; }
        .calc-streak { margin-bottom: 4px; word-break: break-all; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; line-height: 1.35; }
        .calc-streak .w { color: #ffeb3b; }
        .calc-streak .l { color: #c62828; }
        .calc-streak .j { color: #64b5f6; }
        .calc-streak .defense-skip { color: #666; }
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
        <div id="graph-stats-collapse" class="prob-bucket-collapse collapsed">
            <div class="prob-bucket-collapse-header" id="graph-stats-collapse-header" role="button" tabindex="0">최근 15회/30회/전체 정꺽 승률</div>
            <div class="prob-bucket-collapse-body" id="graph-stats-collapse-body">
                <div id="graph-stats" class="graph-stats"></div>
            </div>
        </div>
        <div id="prob-bucket-collapse" class="prob-bucket-collapse collapsed">
            <div class="prob-bucket-collapse-header" id="prob-bucket-collapse-header" role="button" tabindex="0">예측 확률 구간별 승률</div>
            <div class="prob-bucket-collapse-body" id="prob-bucket-collapse-body"></div>
        </div>
        <div id="symmetry-line-collapse" class="prob-bucket-collapse collapsed">
            <div class="prob-bucket-collapse-header" id="symmetry-line-collapse-header" role="button" tabindex="0">좌우 대칭 / 줄 유사도 (20열 기준)</div>
            <div class="prob-bucket-collapse-body" id="symmetry-line-collapse-body"></div>
        </div>
        <div class="bet-calc">
            <h4>가상 배팅 계산기</h4>
            <div class="bet-calc-tabs">
                <span class="tab active" data-tab="calc">계산기</span>
                <span class="tab" data-tab="log">로그</span>
            </div>
            <div id="bet-calc-panel" class="bet-calc-panel active">
                <div class="calc-dropdowns">
                    <div class="calc-dropdown collapsed" data-calc="1">
                        <div class="calc-dropdown-header">
                            <span class="calc-title">계산기 1</span>
                            <span class="calc-status idle" id="calc-1-status">대기중</span>
                            <span class="calc-cards-wrap" id="calc-1-cards-wrap">
                                <span class="calc-card-item"><span class="calc-card-label">배팅중</span><span class="calc-current-card calc-card-betting" id="calc-1-current-card"></span></span>
                                <span class="calc-card-item"><span class="calc-card-label">예측픽</span><span class="calc-current-card calc-card-prediction" id="calc-1-prediction-card"></span></span>
                            </span>
                            <div class="calc-summary" id="calc-1-summary">보유자산 - | 순익 - | 배팅중 -</div>
                            <span class="calc-toggle">▼</span>
                </div>
                        <div class="calc-dropdown-body" id="calc-1-body">
                            <div class="calc-body-row">
                                <table class="calc-settings-table">
                                    <tr><td>자본/배팅</td><td><label>자본금 <input type="number" id="calc-1-capital" min="0" value="1000000"></label> <label>배팅금액 <input type="number" id="calc-1-base" min="1" value="10000"></label> <label>배당 <input type="number" id="calc-1-odds" min="1" step="0.01" value="1.97"></label></td></tr>
                                    <tr><td>픽/승률</td><td><label class="calc-reverse"><input type="checkbox" id="calc-1-reverse"> 반픽</label> <label><input type="checkbox" id="calc-1-win-rate-reverse"> 승률반픽</label> <label>합산승률≤<input type="number" id="calc-1-win-rate-threshold" min="0" max="100" value="50" style="width:3em" title="이 값 이하일 때 승률반픽 발동">%일 때</label></td></tr>
                                    <tr><td>시간</td><td><label>지속 시간(분) <input type="number" id="calc-1-duration" min="0" value="0" placeholder="0=무제한"></label> <label class="calc-duration-check"><input type="checkbox" id="calc-1-duration-check"> 지정 시간만 실행</label></td></tr>
                                    <tr><td>마틴</td><td><label class="calc-martingale"><input type="checkbox" id="calc-1-martingale"> 마틴 적용</label> <label>마틴 방식 <select id="calc-1-martingale-type"><option value="pyo" selected>표마틴</option></select></label></td></tr>
                                    <tr><td>목표</td><td><label><input type="checkbox" id="calc-1-target-enabled"> 목표금액 설정</label> <label>목표 <input type="number" id="calc-1-target-amount" min="0" value="0" placeholder="0=미사용">원</label> <span class="calc-target-hint" id="calc-1-target-hint" style="color:#888;font-size:0.85em"></span></td></tr>
                                </table>
                                <div class="calc-buttons">
                                    <button type="button" class="calc-run" data-calc="1">실행</button>
                                    <button type="button" class="calc-stop" data-calc="1">정지</button>
                                    <button type="button" class="calc-reset" data-calc="1">리셋</button>
                                    <button type="button" class="calc-save" data-calc="1" style="display:none">저장</button>
                                </div>
                            </div>
                            <div class="calc-detail" id="calc-1-detail">
                                <div class="calc-round-table-wrap" id="calc-1-round-table-wrap"></div>
                                <div class="calc-streak" id="calc-1-streak">경기결과 (최근 30회): -</div>
                                <div class="calc-stats" id="calc-1-stats">최대연승: - | 최대연패: - | 승률: -</div>
                            </div>
                        </div>
                    </div>
                    <div class="calc-dropdown collapsed" data-calc="2">
                        <div class="calc-dropdown-header">
                            <span class="calc-title">계산기 2</span>
                            <span class="calc-status idle" id="calc-2-status">대기중</span>
                            <span class="calc-cards-wrap" id="calc-2-cards-wrap">
                                <span class="calc-card-item"><span class="calc-card-label">배팅중</span><span class="calc-current-card calc-card-betting" id="calc-2-current-card"></span></span>
                                <span class="calc-card-item"><span class="calc-card-label">예측픽</span><span class="calc-current-card calc-card-prediction" id="calc-2-prediction-card"></span></span>
                            </span>
                            <div class="calc-summary" id="calc-2-summary">보유자산 - | 순익 - | 배팅중 -</div>
                            <span class="calc-toggle">▼</span>
                        </div>
                        <div class="calc-dropdown-body" id="calc-2-body">
                            <div class="calc-body-row">
                                <table class="calc-settings-table">
                                    <tr><td>자본/배팅</td><td><label>자본금 <input type="number" id="calc-2-capital" min="0" value="1000000"></label> <label>배팅금액 <input type="number" id="calc-2-base" min="1" value="10000"></label> <label>배당 <input type="number" id="calc-2-odds" min="1" step="0.01" value="1.97"></label></td></tr>
                                    <tr><td>픽/승률</td><td><label class="calc-reverse"><input type="checkbox" id="calc-2-reverse"> 반픽</label> <label><input type="checkbox" id="calc-2-win-rate-reverse"> 승률반픽</label> <label>합산승률≤<input type="number" id="calc-2-win-rate-threshold" min="0" max="100" value="50" style="width:3em" title="이 값 이하일 때 승률반픽 발동">%일 때</label></td></tr>
                                    <tr><td>시간</td><td><label>지속 시간(분) <input type="number" id="calc-2-duration" min="0" value="0" placeholder="0=무제한"></label> <label class="calc-duration-check"><input type="checkbox" id="calc-2-duration-check"> 지정 시간만 실행</label></td></tr>
                                    <tr><td>마틴</td><td><label class="calc-martingale"><input type="checkbox" id="calc-2-martingale"> 마틴 적용</label> <label>마틴 방식 <select id="calc-2-martingale-type"><option value="pyo" selected>표마틴</option></select></label></td></tr>
                                    <tr><td>목표</td><td><label><input type="checkbox" id="calc-2-target-enabled"> 목표금액 설정</label> <label>목표 <input type="number" id="calc-2-target-amount" min="0" value="0" placeholder="0=미사용">원</label> <span class="calc-target-hint" id="calc-2-target-hint" style="color:#888;font-size:0.85em"></span></td></tr>
                                </table>
                                <div class="calc-buttons">
                                    <button type="button" class="calc-run" data-calc="2">실행</button>
                                    <button type="button" class="calc-stop" data-calc="2">정지</button>
                                    <button type="button" class="calc-reset" data-calc="2">리셋</button>
                                    <button type="button" class="calc-save" data-calc="2" style="display:none">저장</button>
                                </div>
                            </div>
                            <div class="calc-detail" id="calc-2-detail">
                                <div class="calc-round-table-wrap" id="calc-2-round-table-wrap"></div>
                                <div class="calc-streak" id="calc-2-streak">경기결과 (최근 30회): -</div>
                                <div class="calc-stats" id="calc-2-stats">최대연승: - | 최대연패: - | 승률: -</div>
                            </div>
                        </div>
                    </div>
                    <div class="calc-dropdown collapsed" data-calc="3">
                        <div class="calc-dropdown-header">
                            <span class="calc-title">계산기 3</span>
                            <span class="calc-status idle" id="calc-3-status">대기중</span>
                            <span class="calc-cards-wrap" id="calc-3-cards-wrap">
                                <span class="calc-card-item"><span class="calc-card-label">배팅중</span><span class="calc-current-card calc-card-betting" id="calc-3-current-card"></span></span>
                                <span class="calc-card-item"><span class="calc-card-label">예측픽</span><span class="calc-current-card calc-card-prediction" id="calc-3-prediction-card"></span></span>
                            </span>
                            <div class="calc-summary" id="calc-3-summary">보유자산 - | 순익 - | 배팅중 -</div>
                            <span class="calc-toggle">▼</span>
                        </div>
                        <div class="calc-dropdown-body" id="calc-3-body">
                            <div class="calc-body-row">
                                <table class="calc-settings-table">
                                    <tr><td>자본/배팅</td><td><label>자본금 <input type="number" id="calc-3-capital" min="0" value="1000000"></label> <label>배팅금액 <input type="number" id="calc-3-base" min="1" value="10000"></label> <label>배당 <input type="number" id="calc-3-odds" min="1" step="0.01" value="1.97"></label></td></tr>
                                    <tr><td>픽/승률</td><td><label class="calc-reverse"><input type="checkbox" id="calc-3-reverse"> 반픽</label> <label><input type="checkbox" id="calc-3-win-rate-reverse"> 승률반픽</label> <label>합산승률≤<input type="number" id="calc-3-win-rate-threshold" min="0" max="100" value="50" style="width:3em" title="이 값 이하일 때 승률반픽 발동">%일 때</label></td></tr>
                                    <tr><td>시간</td><td><label>지속 시간(분) <input type="number" id="calc-3-duration" min="0" value="0" placeholder="0=무제한"></label> <label class="calc-duration-check"><input type="checkbox" id="calc-3-duration-check"> 지정 시간만 실행</label></td></tr>
                                    <tr><td>마틴</td><td><label class="calc-martingale"><input type="checkbox" id="calc-3-martingale"> 마틴 적용</label> <label>마틴 방식 <select id="calc-3-martingale-type"><option value="pyo" selected>표마틴</option></select></label></td></tr>
                                    <tr><td>목표</td><td><label><input type="checkbox" id="calc-3-target-enabled"> 목표금액 설정</label> <label>목표 <input type="number" id="calc-3-target-amount" min="0" value="0" placeholder="0=미사용">원</label> <span class="calc-target-hint" id="calc-3-target-hint" style="color:#888;font-size:0.85em"></span></td></tr>
                                </table>
                                <div class="calc-buttons">
                                    <button type="button" class="calc-run" data-calc="3">실행</button>
                                    <button type="button" class="calc-stop" data-calc="3">정지</button>
                                    <button type="button" class="calc-reset" data-calc="3">리셋</button>
                                    <button type="button" class="calc-save" data-calc="3" style="display:none">저장</button>
                                </div>
                            </div>
                            <div class="calc-detail" id="calc-3-detail">
                                <div class="calc-round-table-wrap" id="calc-3-round-table-wrap"></div>
                                <div class="calc-streak" id="calc-3-streak">경기결과 (최근 30회): -</div>
                                <div class="calc-stats" id="calc-3-stats">최대연승: - | 최대연패: - | 승률: -</div>
                            </div>
                        </div>
                    </div>
                    <div class="calc-dropdown collapsed" data-calc="defense">
                        <div class="calc-dropdown-header">
                            <span class="calc-title">방어 계산기</span>
                            <span class="calc-status idle" id="calc-defense-status">대기중</span>
                            <div class="calc-summary" id="calc-defense-summary">보유자산 - | 순익 - | 배팅중 -</div>
                            <span class="calc-toggle">▼</span>
                        </div>
                        <div class="calc-dropdown-body" id="calc-defense-body">
                            <div class="calc-body-row">
                                <div class="calc-inputs">
                                    <label>연결 계산기 <select id="calc-defense-linked"><option value="1">계산기 1</option><option value="2">계산기 2</option><option value="3">계산기 3</option></select></label>
                                    <label>자본금 <input type="number" id="calc-defense-capital" min="0" value="1000000"></label>
                                    <label>배당 <input type="number" id="calc-defense-odds" min="1" step="0.01" value="1.97"></label>
                                    <label title="마틴 1~N회까지 연결과 동일 금액">동일금액 <input type="number" id="calc-defense-full-steps" min="0" value="3" style="width:40px">회까지</label>
                                    <label title="N회부터 연결금액의 1/X로 감액">감액 <input type="number" id="calc-defense-reduce-from" min="1" value="4" style="width:40px">회부터 1/<input type="number" id="calc-defense-reduce-div" min="2" value="4" style="width:40px"> 금액</label>
                                    <label title="방어 N연승 달성 시 다음 회차부터 배팅 안 함, 0=해제">배팅중지 <input type="number" id="calc-defense-stop-streak" min="0" value="5" style="width:40px">연승부터 (0=해제)</label>
                                    <label>지속 시간(분) <input type="number" id="calc-defense-duration" min="0" value="0" placeholder="0=무제한"></label>
                                    <label class="calc-duration-check"><input type="checkbox" id="calc-defense-duration-check"> 지정 시간만 실행</label>
                                </div>
                                <div class="calc-buttons">
                                    <button type="button" class="calc-run" data-calc="defense">실행</button>
                                    <button type="button" class="calc-stop" data-calc="defense">정지</button>
                                    <button type="button" class="calc-reset" data-calc="defense">리셋</button>
                                </div>
                            </div>
                            <div class="calc-detail" id="calc-defense-detail">
                                <div class="calc-round-table-wrap" id="calc-defense-round-table-wrap"></div>
                                <div class="calc-streak" id="calc-defense-streak">경기결과 (최근 30회): - (연결 반픽·설정에 따라 동일/감액/미배팅)</div>
                                <div class="calc-stats" id="calc-defense-stats">최대연승: - | 최대연패: - | 승률: -</div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            <div id="bet-log-panel" class="bet-log-panel">
                <div class="bet-log-actions"><button type="button" id="bet-log-clear-all">전체 삭제</button></div>
                <div id="bet-calc-log" class="bet-calc-log"></div>
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
        // 예측 기록 (최근 30회): { round, predicted, actual } — 새로고침 후에도 유지되도록 localStorage 저장
        const PREDICTION_HISTORY_KEY = 'tokenHiloPredictionHistory';
        let predictionHistory = [];
        try {
            const saved = localStorage.getItem(PREDICTION_HISTORY_KEY);
            if (saved) {
                const parsed = JSON.parse(saved);
                if (Array.isArray(parsed)) predictionHistory = parsed.slice(-100).filter(function(h) { return h && typeof h === 'object'; });
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
        let lastPrediction = null;  // { value: '정'|'꺽', round: number }
        let lastWinEffectRound = null;  // 승리 이펙트를 이미 보여준 회차 (한 번만 표시)
        let lastLoseEffectRound = null;  // 실패 이펙트를 이미 보여준 회차 (한 번만 표시)
        var prevSymmetryCounts = { left: null, right: null };  // 이전 시점 20열 줄 개수 (새 구간 빨리 캐치용)
        const CALC_IDS = [1, 2, 3];
        const CALC_SESSION_KEY = 'tokenHiloCalcSessionId';
        const CALC_STATE_BACKUP_KEY = 'tokenHiloCalcStateBackup';
        const calcState = {};
        var TABLE_MARTIN_PYO = [10000, 15000, 25000, 40000, 70000, 120000, 200000, 400000, 120000];
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
                martingale: false,
                martingale_type: 'pyo',
                target_enabled: false,
                target_amount: 0,
                timer_completed: false,
                timerId: null,
                maxWinStreakEver: 0,
                maxLoseStreakEver: 0,
                first_bet_round: 0
            };
        });
        calcState.defense = {
            running: false,
            started_at: 0,
            history: [],
            elapsed: 0,
            duration_limit: 0,
            use_duration_limit: false,
            timer_completed: false,
            linked_calc_id: 1,
            timerId: null,
            maxWinStreakEver: 0,
            maxLoseStreakEver: 0,
            first_bet_round: 0
        };
        const DEFENSE_ID = 'defense';
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
                payload[String(id)] = {
                    running: calcState[id].running,
                    started_at: calcState[id].started_at || 0,
                    history: (calcState[id].history || []).slice(-500),
                    duration_limit: duration_limit,
                    use_duration_limit: use_duration_limit,
                    reverse: !!(revEl && revEl.checked),
                    win_rate_reverse: !!(winRateRevEl && winRateRevEl.checked),
                    win_rate_threshold: winRateThr,
                    martingale: !!(martingaleEl && martingaleEl.checked),
                    martingale_type: (martingaleTypeEl && martingaleTypeEl.value) || 'pyo',
                    target_enabled: !!(document.getElementById('calc-' + id + '-target-enabled') && document.getElementById('calc-' + id + '-target-enabled').checked),
                    target_amount: Math.max(0, parseInt(document.getElementById('calc-' + id + '-target-amount')?.value, 10) || 0),
                    timer_completed: !!calcState[id].timer_completed,
                    max_win_streak_ever: calcState[id].maxWinStreakEver || 0,
                    max_lose_streak_ever: calcState[id].maxLoseStreakEver || 0,
                    first_bet_round: calcState[id].first_bet_round || 0
                };
            });
            const d = calcState.defense;
            const defDurEl = document.getElementById('calc-defense-duration');
            const defCheckEl = document.getElementById('calc-defense-duration-check');
            const defLinkEl = document.getElementById('calc-defense-linked');
            const defFullSteps = document.getElementById('calc-defense-full-steps');
            const defReduceFrom = document.getElementById('calc-defense-reduce-from');
            const defReduceDiv = document.getElementById('calc-defense-reduce-div');
            const defStopStreak = document.getElementById('calc-defense-stop-streak');
            const defDurationMin = (defDurEl && parseInt(defDurEl.value, 10)) || 0;
            payload[DEFENSE_ID] = {
                running: !!d.running,
                started_at: d.started_at || 0,
                history: (d.history || []).slice(-500),
                duration_limit: defDurationMin * 60,
                use_duration_limit: !!(defCheckEl && defCheckEl.checked),
                timer_completed: !!d.timer_completed,
                linked_calc_id: (defLinkEl && parseInt(defLinkEl.value, 10)) || 1,
                full_steps: (defFullSteps && parseInt(defFullSteps.value, 10)) || 3,
                reduce_from: (defReduceFrom && parseInt(defReduceFrom.value, 10)) || 4,
                reduce_div: (defReduceDiv && parseInt(defReduceDiv.value, 10)) || 4,
                stop_streak: (defStopStreak && parseInt(defStopStreak.value, 10)) || 0,
                max_win_streak_ever: (d.maxWinStreakEver || 0),
                max_lose_streak_ever: (d.maxLoseStreakEver || 0),
                first_bet_round: d.first_bet_round || 0
            };
            return payload;
        }
        function applyCalcsToState(calcs, serverTimeSec) {
            const st = serverTimeSec || Math.floor(Date.now() / 1000);
            CALC_IDS.forEach(id => {
                const c = calcs[String(id)] || {};
                if (Array.isArray(c.history)) calcState[id].history = c.history.slice(-500);
                else calcState[id].history = [];
                calcState[id].running = !!c.running;
                calcState[id].started_at = c.started_at || 0;
                calcState[id].duration_limit = parseInt(c.duration_limit, 10) || 0;
                calcState[id].use_duration_limit = !!c.use_duration_limit;
                calcState[id].timer_completed = !!c.timer_completed;
                calcState[id].maxWinStreakEver = Math.max(0, parseInt(c.max_win_streak_ever, 10) || 0);
                calcState[id].maxLoseStreakEver = Math.max(0, parseInt(c.max_lose_streak_ever, 10) || 0);
                calcState[id].first_bet_round = Math.max(0, parseInt(c.first_bet_round, 10) || 0);
                calcState[id].elapsed = calcState[id].running && calcState[id].started_at ? Math.max(0, st - calcState[id].started_at) : 0;
                const durEl = document.getElementById('calc-' + id + '-duration');
                const checkEl = document.getElementById('calc-' + id + '-duration-check');
                const revEl = document.getElementById('calc-' + id + '-reverse');
                if (durEl) durEl.value = Math.floor((calcState[id].duration_limit || 0) / 60);
                if (checkEl) checkEl.checked = calcState[id].use_duration_limit;
                if (revEl) revEl.checked = !!c.reverse;
                calcState[id].reverse = !!c.reverse;
                calcState[id].win_rate_reverse = !!c.win_rate_reverse;
                var thr = (typeof c.win_rate_threshold === 'number' && c.win_rate_threshold >= 0 && c.win_rate_threshold <= 100) ? c.win_rate_threshold : 50;
                calcState[id].win_rate_threshold = thr;
                calcState[id].martingale = !!c.martingale;
                calcState[id].martingale_type = (c.martingale_type === 'pyo' ? 'pyo' : 'pyo');
                const winRateRevEl = document.getElementById('calc-' + id + '-win-rate-reverse');
                if (winRateRevEl) winRateRevEl.checked = !!c.win_rate_reverse;
                const winRateThrEl = document.getElementById('calc-' + id + '-win-rate-threshold');
                if (winRateThrEl) { winRateThrEl.value = String(Math.round(thr)); }
                const martingaleEl = document.getElementById('calc-' + id + '-martingale');
                const martingaleTypeEl = document.getElementById('calc-' + id + '-martingale-type');
                if (martingaleEl) martingaleEl.checked = !!calcState[id].martingale;
                if (martingaleTypeEl) martingaleTypeEl.value = calcState[id].martingale_type || 'pyo';
                calcState[id].target_enabled = !!c.target_enabled;
                calcState[id].target_amount = Math.max(0, parseInt(c.target_amount, 10) || 0);
                const targetEnabledEl = document.getElementById('calc-' + id + '-target-enabled');
                const targetAmountEl = document.getElementById('calc-' + id + '-target-amount');
                if (targetEnabledEl) targetEnabledEl.checked = !!calcState[id].target_enabled;
                if (targetAmountEl) targetAmountEl.value = String(calcState[id].target_amount || 0);
            });
            const dc = calcs[DEFENSE_ID] || {};
            if (Array.isArray(dc.history)) calcState.defense.history = dc.history.slice(-500);
            else calcState.defense.history = [];
            calcState.defense.running = !!dc.running;
            calcState.defense.started_at = dc.started_at || 0;
            calcState.defense.duration_limit = parseInt(dc.duration_limit, 10) || 0;
            calcState.defense.use_duration_limit = !!dc.use_duration_limit;
            calcState.defense.timer_completed = !!dc.timer_completed;
            calcState.defense.linked_calc_id = parseInt(dc.linked_calc_id, 10) || 1;
            calcState.defense.maxWinStreakEver = Math.max(0, parseInt(dc.max_win_streak_ever, 10) || 0);
            calcState.defense.maxLoseStreakEver = Math.max(0, parseInt(dc.max_lose_streak_ever, 10) || 0);
            calcState.defense.first_bet_round = Math.max(0, parseInt(dc.first_bet_round, 10) || 0);
            calcState.defense.elapsed = calcState.defense.running && calcState.defense.started_at ? Math.max(0, st - calcState.defense.started_at) : 0;
            const defDurEl = document.getElementById('calc-defense-duration');
            const defCheckEl = document.getElementById('calc-defense-duration-check');
            const defLinkEl = document.getElementById('calc-defense-linked');
            const defFullSteps = document.getElementById('calc-defense-full-steps');
            const defReduceFrom = document.getElementById('calc-defense-reduce-from');
            const defReduceDiv = document.getElementById('calc-defense-reduce-div');
            const defStopStreak = document.getElementById('calc-defense-stop-streak');
            if (defDurEl) defDurEl.value = Math.floor((calcState.defense.duration_limit || 0) / 60);
            if (defCheckEl) defCheckEl.checked = calcState.defense.use_duration_limit;
            if (defLinkEl) defLinkEl.value = String(calcState.defense.linked_calc_id);
            if (defFullSteps) defFullSteps.value = dc.full_steps !== undefined ? dc.full_steps : 3;
            if (defReduceFrom) defReduceFrom.value = dc.reduce_from !== undefined ? dc.reduce_from : 4;
            if (defReduceDiv) defReduceDiv.value = dc.reduce_div !== undefined ? dc.reduce_div : 4;
            if (defStopStreak) defStopStreak.value = dc.stop_streak !== undefined ? dc.stop_streak : 5;
        }
        async function loadCalcStateFromServer() {
            try {
                const session_id = localStorage.getItem(CALC_SESSION_KEY);
                const url = session_id ? '/api/calc-state?session_id=' + encodeURIComponent(session_id) : '/api/calc-state';
                const res = await fetch(url, { cache: 'no-cache' });
                const data = await res.json();
                if (data.session_id) localStorage.setItem(CALC_SESSION_KEY, data.session_id);
                lastServerTimeSec = data.server_time || Math.floor(Date.now() / 1000);
                let calcs = data.calcs || {};
                const hasRunning = CALC_IDS.some(id => calcs[String(id)] && calcs[String(id)].running) || (calcs[DEFENSE_ID] && calcs[DEFENSE_ID].running);
                const hasHistory = CALC_IDS.some(id => calcs[String(id)] && Array.isArray(calcs[String(id)].history) && calcs[String(id)].history.length > 0) || (calcs[DEFENSE_ID] && Array.isArray(calcs[DEFENSE_ID].history) && calcs[DEFENSE_ID].history.length > 0);
                if (!hasRunning && !hasHistory) {
                    try {
                        const backup = localStorage.getItem(CALC_STATE_BACKUP_KEY);
                        if (backup) {
                            const parsed = JSON.parse(backup);
                            if (parsed && typeof parsed === 'object') calcs = parsed;
                        }
                    } catch (e) { /* ignore */ }
                }
                applyCalcsToState(calcs, lastServerTimeSec);
            } catch (e) { console.warn('계산기 상태 로드 실패:', e); }
        }
        async function saveCalcStateToServer() {
            try {
                let session_id = localStorage.getItem(CALC_SESSION_KEY);
                if (!session_id) {
                    const res = await fetch('/api/calc-state', { cache: 'no-cache' });
                    const data = await res.json();
                    if (data.session_id) {
                        localStorage.setItem(CALC_SESSION_KEY, data.session_id);
                        session_id = data.session_id;
                    }
                }
                if (!session_id) return;
                const payload = buildCalcPayload();
                try {
                    localStorage.setItem(CALC_STATE_BACKUP_KEY, JSON.stringify(payload));
                } catch (e) { /* ignore */ }
                await fetch('/api/calc-state', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ session_id: session_id, calcs: payload })
                });
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
            const isDefense = calcId === 'defense';
            let rows = [];
            for (let i = 0; i < hist.length; i++) {
                const h = hist[i];
                if (!h) continue;
                if (isDefense) {
                    const bet = (typeof h.betAmount === 'number' ? h.betAmount : 0) || parseInt(h.betAmount, 10) || 0;
                    if (bet <= 0) { rows.push({ idx: i + 1, pick: '-', result: '-', outcome: '－' }); continue; }
                }
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
            if (isLoadingResults) return;
            const statusEl = document.getElementById('status');
            if (statusEl) statusEl.textContent = '데이터 요청 중...';
            
            try {
                isLoadingResults = true;
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 5000);
                
                const response = await fetch('/api/results?t=' + Date.now(), {
                    signal: controller.signal,
                    cache: 'no-cache'
                });
                
                clearTimeout(timeoutId);
                if (statusEl) statusEl.textContent = '결과 표시 중...';
                
                if (!response.ok) {
                    console.warn('결과 로드 실패:', response.status, response.statusText);
                    if (statusEl) statusEl.textContent = '결과 로드 실패 (' + response.status + ')';
                    return;
                }
                
                const data = await response.json();
                if (data.error) {
                    if (statusEl) statusEl.textContent = '오류: ' + data.error;
                    return;
                }
                // 서버에 저장된 시스템 예측 기록 복원 (어디서 접속해도 동일). 무효 항목 제거해 ReferenceError 방지
                if (Object.prototype.hasOwnProperty.call(data, 'prediction_history') && Array.isArray(data.prediction_history)) {
                    predictionHistory = data.prediction_history.slice(-100).filter(function(h) { return h && typeof h === 'object'; });
                    savePredictionHistory();
                }
                
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
                // 새로운 결과를 기존 결과와 병합 (중복 제거, 최신 150개 유지 - 그래프 쭉 표시용)
                if (newResults.length > 0) {
                    // 새로운 결과의 gameID들
                    const newGameIDs = new Set(newResults.map(r => r.gameID).filter(id => id));
                    
                    // 기존 결과에서 새로운 결과에 없는 것만 유지
                    const oldResults = allResults.filter(r => !newGameIDs.has(r.gameID));
                    
                    // 새로운 결과 + 기존 결과 (최신 150개) → gameID 기준 정렬로 그래프 순서 고정
                    allResults = sortResultsNewestFirst([...newResults, ...oldResults].slice(0, 150));
                } else {
                    // 새로운 결과가 없으면 기존 결과 유지 (순서만 정렬)
                    if (allResults.length === 0) {
                        allResults = sortResultsNewestFirst(newResults);
                    } else {
                        allResults = sortResultsNewestFirst(allResults);
                    }
                }
                
                statusElement.textContent = `총 ${allResults.length}개 경기 결과 (표시: ${newResults.length}개)`;
                
                // 최신 결과가 왼쪽에 오도록 (원본 데이터가 최신이 앞에 있음)
                // 최신 15개만 표시 (반응형으로 모두 보이도록)
                const displayResults = allResults.slice(0, 15);
                const results = allResults;  // 비교를 위해 전체 결과 사용
                
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
                const currentGameIDs = new Set(allResults.map(r => r.gameID).filter(id => id));
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
                        graphDiv.appendChild(col);
                    });
                }
                
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
                    
                    // 회차: 비교·저장은 전체 gameID(11416052 등), 표시만 뒤 3자리(052). 숫자 높을수록 최신이므로 전체로 비교해야 035가 999보다 최신으로 인식됨
                    function fullRoundFromGameID(g) {
                        var s = String(g != null && g !== '' ? g : '0');
                        var n = parseInt(s, 10);
                        return isNaN(n) ? 0 : n;
                    }
                    function displayRound3(r) { return r != null ? String(r).slice(-3) : '-'; }
                    const latestGameID = displayResults[0]?.gameID;
                    const currentRoundFull = fullRoundFromGameID(latestGameID);
                    const predictedRoundFull = currentRoundFull + 1;
                    try { window.__latestGameIDForCalc = latestGameID; } catch (e) {}
                    const is15Joker = displayResults.length >= 15 && !!displayResults[14].joker;  // 15번 카드 조커면 픽/배팅 보류
                    
                    // 직전 예측의 실제 결과 반영: 예측했던 회차(전체 ID)가 지금 나왔으면 기록
                    const alreadyRecordedRound = lastPrediction ? predictionHistory.some(function(h) { return h && h.round === lastPrediction.round; }) : true;
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
                        blended = 0.5 * r15 + 0.3 * r30 + 0.2 * r100;
                        lowWinRateForRecord = (c15 > 0 || c30 > 0 || c100 > 0) && blended <= 50;
                    } catch (e) {}
                    if (lastPrediction && currentRoundFull === lastPrediction.round && !alreadyRecordedRound) {
                        const isActualJoker = displayResults.length > 0 && !!displayResults[0].joker;
                        if (isActualJoker) {
                            predictionHistory.push({ round: lastPrediction.round, predicted: lastPrediction.value, actual: 'joker', probability: lastPrediction.prob != null ? lastPrediction.prob : null, pickColor: lastPrediction.color || null });
                            CALC_IDS.forEach(id => {
                                if (!calcState[id].running) return;
                                const firstBetJoker = calcState[id].first_bet_round || 0;
                                if (firstBetJoker > 0 && lastPrediction.round < firstBetJoker) return;
                                const hasRound = calcState[id].history.some(function(h) { return h && h.round === lastPrediction.round; });
                                if (hasRound) return;
                                const rev = !!(calcState[id] && calcState[id].reverse);
                                var pred = rev ? (lastPrediction.value === '정' ? '꺽' : '정') : lastPrediction.value;
                                const useWinRateRev = !!(calcState[id] && calcState[id].win_rate_reverse);
                                var thrEl = document.getElementById('calc-' + id + '-win-rate-threshold');
                                var thr = (thrEl && !isNaN(parseFloat(thrEl.value))) ? Math.max(0, Math.min(100, parseFloat(thrEl.value))) : (calcState[id] != null && typeof calcState[id].win_rate_threshold === 'number' ? calcState[id].win_rate_threshold : 50);
                                if (typeof thr !== 'number' || isNaN(thr)) thr = 50;
                                if (useWinRateRev && (c15 > 0 || c30 > 0 || c100 > 0) && typeof blended === 'number' && blended <= thr) pred = pred === '정' ? '꺽' : '정';
                                // 방어 배팅금: 연결에 이번 회차 푸시하기 *전*에 계산 (이번 회차에 실제로 건 금액)
                                let defenseBet = 0;
                                if (calcState.defense.running && calcState.defense.linked_calc_id === id) defenseBet = getDefenseBetAmount(id);
                                calcState[id].history.push({ predicted: pred, actual: 'joker', round: lastPrediction.round });
                                if (calcState.defense.running && calcState.defense.linked_calc_id === id) {
                                    const defFirstJ = calcState.defense.first_bet_round || 0;
                                    if (defFirstJ === 0 || lastPrediction.round >= defFirstJ) {
                                        calcState.defense.history.push({ predicted: pred === '정' ? '꺽' : '정', actual: 'joker', betAmount: defenseBet, round: lastPrediction.round });
                                        updateCalcSummary(DEFENSE_ID);
                                        updateCalcDetail(DEFENSE_ID);
                                    }
                                }
                            });
                            saveCalcStateToServer();
                            savePredictionHistoryToServer(lastPrediction.round, lastPrediction.value, 'joker', lastPrediction.prob, lastPrediction.color);
                        } else if (graphValues.length > 0 && (graphValues[0] === true || graphValues[0] === false)) {
                            const actual = graphValues[0] ? '정' : '꺽';
                            predictionHistory.push({ round: lastPrediction.round, predicted: lastPrediction.value, actual: actual, probability: lastPrediction.prob != null ? lastPrediction.prob : null, pickColor: lastPrediction.color || null });
                            CALC_IDS.forEach(id => {
                                if (!calcState[id].running) return;
                                const firstBetActual = calcState[id].first_bet_round || 0;
                                if (firstBetActual > 0 && lastPrediction.round < firstBetActual) return;
                                const hasRound = calcState[id].history.some(function(h) { return h && h.round === lastPrediction.round; });
                                if (hasRound) return;
                                const rev = !!(calcState[id] && calcState[id].reverse);
                                var pred = rev ? (lastPrediction.value === '정' ? '꺽' : '정') : lastPrediction.value;
                                const useWinRateRevActual = !!(calcState[id] && calcState[id].win_rate_reverse);
                                var thrElActual = document.getElementById('calc-' + id + '-win-rate-threshold');
                                var thrActual = (thrElActual && !isNaN(parseFloat(thrElActual.value))) ? Math.max(0, Math.min(100, parseFloat(thrElActual.value))) : (calcState[id] != null && typeof calcState[id].win_rate_threshold === 'number' ? calcState[id].win_rate_threshold : 50);
                                if (typeof thrActual !== 'number' || isNaN(thrActual)) thrActual = 50;
                                if (useWinRateRevActual && (c15 > 0 || c30 > 0 || c100 > 0) && typeof blended === 'number' && blended <= thrActual) pred = pred === '정' ? '꺽' : '정';
                                // 방어 배팅금: 연결에 이번 회차 푸시하기 *전*에 계산 (이번 회차에 실제로 건 금액)
                                let defenseBet = 0;
                                if (calcState.defense.running && calcState.defense.linked_calc_id === id) defenseBet = getDefenseBetAmount(id);
                                calcState[id].history.push({ predicted: pred, actual: actual, round: lastPrediction.round });
                                if (calcState.defense.running && calcState.defense.linked_calc_id === id) {
                                    const defFirstA = calcState.defense.first_bet_round || 0;
                                    if (defFirstA === 0 || lastPrediction.round >= defFirstA) {
                                        calcState.defense.history.push({ predicted: pred === '정' ? '꺽' : '정', actual: actual, betAmount: defenseBet, round: lastPrediction.round });
                                        updateCalcSummary(DEFENSE_ID);
                                        updateCalcDetail(DEFENSE_ID);
                                    }
                                }
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
                                    }
                                }
                            });
                            saveCalcStateToServer();
                            savePredictionHistoryToServer(lastPrediction.round, lastPrediction.value, actual, lastPrediction.prob, lastPrediction.color);
                        }
                        predictionHistory = predictionHistory.slice(-100);
                        savePredictionHistory();  // localStorage 백업
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
                    
                    // 20열 기준 좌우대칭·줄 데이터 (예측픽 보정 + 아래 표에 사용). 오류 시 symmetryLineData=null로 무시.
                    try {
                        var arr20 = (graphValues && graphValues.filter(function(v) { return v === true || v === false; }).slice(0, 20)) || [];
                        if (arr20 && arr20.length >= 20) {
                            function getRunLengths(a) {
                                var r = [], cur = null, c = 0, i;
                                for (i = 0; i < a.length; i++) {
                                    if (a[i] === cur) c++;
                                    else { if (cur !== null) r.push(c); cur = a[i]; c = 1; }
                                }
                                if (cur !== null) r.push(c);
                                return r;
                            }
                            var symCount = 0;
                            for (var si = 0; si < 10; si++) { if (arr20[si] === arr20[19 - si]) symCount++; }
                            var left10 = arr20.slice(0, 10), right10 = arr20.slice(10, 20);
                            var leftRuns = getRunLengths(left10), rightRuns = getRunLengths(right10);
                            var avgL = leftRuns.length ? leftRuns.reduce(function(s, x) { return s + x; }, 0) / leftRuns.length : 0;
                            var avgR = rightRuns.length ? rightRuns.reduce(function(s, x) { return s + x; }, 0) / rightRuns.length : 0;
                            var lineDiff = Math.abs(avgL - avgR);
                            var maxLeftRun = (leftRuns && leftRuns.length) ? Math.max.apply(null, leftRuns) : 0;
                            var recentRunLen = 1;
                            if (arr20 && arr20.length >= 2) {
                                var v0 = arr20[0];
                                for (var ri = 1; ri < arr20.length; ri++) { if (arr20[ri] === v0) recentRunLen++; else break; }
                            }
                            symmetryLineData = {
                                symmetryPct: symCount / 10 * 100,
                                avgLeft: avgL, avgRight: avgR,
                                lineSimilarityPct: Math.max(0, 100 - Math.min(100, lineDiff * 25)),
                                leftLineCount: leftRuns.length,
                                rightLineCount: rightRuns.length,
                                maxLeftRunLength: maxLeftRun,
                                recentRunLength: recentRunLen
                            };
                        }
                    } catch (symErr) { symmetryLineData = null; console.warn('20열 symmetry/line calc:', symErr); }
                    var symmetryBoostNotice = false;  // 20열 보정 반영 시 경고문구용
                    var newSegmentNotice = false;    // 새 구간 구성 중 경고문구용
                    
                    // 예측픽 합산 공식: (유지확률×줄가중치) vs (바뀜확률×퐁당가중치) → 정규화 후 큰 쪽이 예측.
                    // 가중치(lineW,pongW) 구성: ①최근15회 줄/퐁당% ②흐름전환(줄강/퐁당강) ±0.25 ③20열(줄개수 ±0.15, 대칭도 +0.05 또는 ×0.95) ④30회패턴(chunk/scatter/twoOne) → 합산 후 정규화.
                    // 20열 반영 비중(조절 가능): 줄개수 보정 +0.15, 대칭도 높을 때 +0.05, 낮을 때 ×0.95.
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
                        lastPrediction = { value: predict, round: predictedRoundFull, prob: predProb, color: colorToPick };
                        colorClass = colorToPick === '빨강' ? 'red' : 'black';
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
                    
                    // 예측 픽(표 왼쪽 박스, 가운데 정렬) · 적중률·연승연패·주의 사항(아래 회색 박스)
                    const resultBarContainer = document.getElementById('prediction-result-bar');
                    const pickContainer = document.getElementById('prediction-pick-container');
                    const predDiv = document.getElementById('prediction-box');
                    const validHist = predictionHistory.filter(function(h) { return h && typeof h === 'object'; });
                    const hit = validHist.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
                    const losses = validHist.filter(function(h) { return h.actual !== 'joker' && h.predicted !== h.actual; }).length;
                    const jokerCount = validHist.filter(function(h) { return h.actual === 'joker'; }).length;
                    const total = validHist.length;
                    const countForPct = hit + losses;
                    const hitPctNum = countForPct > 0 ? 100 * hit / countForPct : 0;
                    const hitPct = countForPct > 0 ? hitPctNum.toFixed(1) : '-';
                    // 승률 낮음·배팅 주의: 15회 50% + 30회 30% + 100회 20% 반영 (룰)
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
                    const blendedWinRate = 0.5 * rate15 + 0.3 * hitPctNum30 + 0.2 * rate100;
                    const lowWinRate = (count15 > 0 || count30 > 0 || count100 > 0) && blendedWinRate <= 50;
                    // 표시용: 최근 50회 결과 (승/패/조커/합산승률)
                    const validHist50 = validHist.slice(-50);
                    const hit50 = validHist50.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
                    const losses50 = validHist50.filter(function(h) { return h.actual !== 'joker' && h.predicted !== h.actual; }).length;
                    const joker50 = validHist50.filter(function(h) { return h.actual === 'joker'; }).length;
                    const count50 = hit50 + losses50;
                    const rate50 = count50 > 0 ? 100 * hit50 / count50 : 0;
                    const rate50Str = count50 > 0 ? rate50.toFixed(1) : '-';
                    // 확률 구간별 승률 (joker 제외, probability 있는 것만)
                    const nonJokerWithProb = validHist.filter(function(h) { return h && h.actual !== 'joker' && h.probability != null; });
                    const BUCKETS = [{ min: 50, max: 55 }, { min: 55, max: 60 }, { min: 60, max: 65 }, { min: 65, max: 70 }, { min: 70, max: 75 }, { min: 75, max: 80 }, { min: 80, max: 85 }, { min: 85, max: 90 }, { min: 90, max: 101 }];
                    const bucketStats = BUCKETS.map(function(b) {
                        const inBucket = nonJokerWithProb.filter(function(h) { var p = Number(h.probability); return p >= b.min && p < b.max; });
                        const wins = inBucket.filter(function(h) { return h.predicted === h.actual; }).length;
                        const total = inBucket.length;
                        return { label: b.min + '~' + (b.max === 101 ? '100' : b.max) + '%', total: total, wins: wins, pct: total > 0 ? (100 * wins / total).toFixed(1) : '-', min: b.min, max: b.max };
                    }).filter(function(s) { return s.total > 0; });
                    // 기존 확률에 30% 반영 (blendData는 전이 확률 표에서 계산됨)
                    if (blendData && blendData.newProb != null && !is15Joker) predProb = 0.7 * predProb + 0.3 * blendData.newProb;
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
                        var lastPickColor = (lastEntry.pickColor || lastEntry.pick_color || '').toString();
                        if (lastPickColor === 'RED') lastPickColor = '빨강';
                        else if (lastPickColor === 'BLACK') lastPickColor = '검정';
                        else if (!lastPickColor && lastEntry.predicted) lastPickColor = lastEntry.predicted === '정' ? '빨강' : '검정';
                        else lastPickColor = lastPickColor || '-';
                        var resultBarClass = lastIsWin ? 'pick-result-bar result-win' : 'pick-result-bar result-lose';
                        var resultBarText = displayRound3(lastEntry.round) + '회 ' + (lastIsWin ? '성공' : '실패') + ' (' + (lastEntry.predicted || '-') + ' / ' + lastPickColor + ')';
                        resultBarHtml = '<div class="' + resultBarClass + '">' + resultBarText + '</div>';
                    }
                    const pickWrapClass = 'prediction-pick' + (pickInBucket ? ' pick-in-bucket' : '');
                    if (resultBarContainer) resultBarContainer.innerHTML = resultBarHtml;
                    const leftBlock = is15Joker ? ('<div class="prediction-pick">' +
                        '<div class="prediction-pick-title">예측 픽</div>' +
                        '<div class="prediction-card" style="background:#455a64;border-color:#78909c">' +
                        '<span class="pred-value-big" style="color:#fff;font-size:1.2em">보류</span>' +
                        '</div>' +
                        '<div class="prediction-prob-under" style="color:#ffb74d">15번 카드 조커 · 배팅하지 마세요</div>' +
                        '<div class="pred-round">' + displayRound3(predictedRoundFull) + '회</div>' +
                        '</div>') : ('<div class="' + pickWrapClass + '">' +
                        '<div class="prediction-pick-title prediction-pick-title-betting">배팅중<br>' + (colorToPick === '빨강' ? 'RED' : 'BLACK') + '</div>' +
                        '<div class="prediction-card card-' + colorClass + '">' +
                        '<span class="pred-value-big">' + predict + '</span>' +
                        '</div>' +
                        '<div class="prediction-prob-under">예측 확률 ' + predProb.toFixed(1) + '%</div>' +
                        '<div class="pred-round">' + displayRound3(predictedRoundFull) + '회</div>' +
                        '</div>');
                    if (pickContainer) pickContainer.innerHTML = leftBlock;
                    // 배팅 연동: 현재 픽을 서버에 저장 (GET /api/current-pick 으로 외부 조회 가능)
                    try {
                        if (is15Joker) {
                            fetch('/api/current-pick', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pickColor: null, round: predictedRoundFull, probability: null }) }).catch(function() {});
                        } else if (lastPrediction && (colorToPick === '빨강' || colorToPick === '검정')) {
                            fetch('/api/current-pick', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({
                                    pickColor: colorToPick === '빨강' ? 'RED' : 'BLACK',
                                    round: predictedRoundFull,
                                    probability: predProb
                                })
                            }).catch(function() {});
                        }
                    } catch (e) {}
                    if (predDiv) {
                        const rateClass50 = count50 > 0 ? (rate50 >= 60 ? 'high' : rate50 >= 50 ? 'mid' : 'low') : '';
                        const blendedStr = (typeof blendedWinRate === 'number' && !isNaN(blendedWinRate)) ? blendedWinRate.toFixed(1) : '-';
                        const statsBlock = '<div class="prediction-stats-row">' +
                            '<span class="stat-total">최근 50회 결과</span>' +
                            '<span class="stat-win">승 - <span class="num">' + hit50 + '</span>회</span>' +
                            '<span class="stat-lose">패 - <span class="num">' + losses50 + '</span>회</span>' +
                            '<span class="stat-joker">조커 - <span class="num">' + joker50 + '</span>회</span>' +
                            (count50 > 0 ? '<span class="stat-rate ' + rateClass50 + '">승률 : ' + rate50Str + '%</span>' : '') +
                            '<span class="stat-rate" style="color:#888;font-size:0.9em">실제 경고 합산승률 : ' + blendedStr + '%</span>' +
                            '</div>' +
                            '<div class="prediction-stats-note" style="font-size:0.8em;color:#888;margin-top:2px">※ 메인=서버 최근 100회 · 승률/경고=15·30·100 반영(50·30·20)</div>';
                        let streakTableBlock = '';
                        try {
                        if (rev.length === 0) {
                            streakTableBlock = '<div class="prediction-streak-line">최근 100회 기준 · <span class="streak-now">' + streakLine100 + '</span></div>';
                        } else {
                            const headerCells = rev.map(function(h) { return '<th>' + displayRound3(h.round) + '</th>'; }).join('');
                            const rowProb = rev.map(function(h) { return '<td>' + (h.probability != null ? Number(h.probability).toFixed(1) + '%' : '-') + '</td>'; }).join('');
                            const rowPick = rev.map(function(h) {
                                const pickColor = h.pickColor || h.pick_color;
                                const c = pickColor === '빨강' ? 'pick-red' : (pickColor === '검정' ? 'pick-black' : '');
                                return '<td class="' + c + '">' + (h.predicted != null ? h.predicted : '-') + '</td>';
                            }).join('');
                            const rowOutcome = rev.map(function(h) {
                                const out = h.actual === 'joker' ? '조커' : (h.predicted === h.actual ? '승' : '패');
                                const c = out === '승' ? 'streak-win' : out === '패' ? 'streak-lose' : 'streak-joker';
                                return '<td class="' + c + '">' + out + '</td>';
                            }).join('');
                            streakTableBlock = '<div class="main-streak-table-wrap"><table class="main-streak-table">' +
                                '<thead><tr>' + headerCells + '</tr></thead><tbody>' +
                                '<tr>' + rowProb + '</tr>' +
                                '<tr>' + rowPick + '</tr>' +
                                '<tr>' + rowOutcome + '</tr>' +
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
                                probBucketCollapse.style.display = '';
                            } else {
                                probBucketBody.innerHTML = '';
                                probBucketCollapse.style.display = 'none';
                            }
                        }
                        var collapseHeader = document.getElementById('prob-bucket-collapse-header');
                        if (collapseHeader && !collapseHeader.getAttribute('data-bound')) {
                            collapseHeader.setAttribute('data-bound', '1');
                            collapseHeader.addEventListener('click', function() {
                                var el = document.getElementById('prob-bucket-collapse');
                                if (el) el.classList.toggle('collapsed');
                            });
                        }
                        var symmetryLineBody = document.getElementById('symmetry-line-collapse-body');
                        var symmetryLineCollapse = document.getElementById('symmetry-line-collapse');
                        if (symmetryLineBody && symmetryLineCollapse) {
                            if (symmetryLineData) {
                                var s = symmetryLineData;
                                symmetryLineBody.innerHTML = '<table class="symmetry-line-table" cellspacing="0" cellpadding="0"><thead><tr><th>항목</th><th>값</th><th>비고</th></tr></thead><tbody>' +
                                    '<tr><td>좌우 대칭도</td><td>' + s.symmetryPct.toFixed(1) + '%</td><td>1~10열 vs 11~20열 대칭 매칭(10쌍)</td></tr>' +
                                    '<tr><td>왼쪽(1~10열) 줄 개수</td><td>' + s.leftLineCount + '</td><td>적을수록 긴 줄(추세), 많을수록 퐁당</td></tr>' +
                                    '<tr><td>오른쪽(11~20열) 줄 개수</td><td>' + s.rightLineCount + '</td><td>적을수록 긴 줄(추세), 많을수록 퐁당</td></tr>' +
                                    '<tr><td>왼쪽(1~10열) 평균 줄길이</td><td>' + s.avgLeft.toFixed(2) + '</td><td>연속 정/꺽 평균</td></tr>' +
                                    '<tr><td>오른쪽(11~20열) 평균 줄길이</td><td>' + s.avgRight.toFixed(2) + '</td><td>연속 정/꺽 평균</td></tr>' +
                                    '<tr><td>줄 유사도</td><td>' + s.lineSimilarityPct.toFixed(1) + '%</td><td>양쪽 평균 줄길이 차이 반영</td></tr></tbody></table>';
                                symmetryLineCollapse.style.display = '';
                            } else {
                                symmetryLineBody.innerHTML = '<p style="color:#888;font-size:0.9em">최근 20열(정/꺽) 데이터가 부족합니다.</p>';
                                symmetryLineCollapse.style.display = '';
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
                        var graphStatsCollapse = document.getElementById('graph-stats-collapse');
                        if (graphStatsCollapse) graphStatsCollapse.style.display = '';
                        var graphStatsCollapseHeader = document.getElementById('graph-stats-collapse-header');
                        if (graphStatsCollapseHeader && !graphStatsCollapseHeader.getAttribute('data-bound')) {
                            graphStatsCollapseHeader.setAttribute('data-bound', '1');
                            graphStatsCollapseHeader.addEventListener('click', function() {
                                var el = document.getElementById('graph-stats-collapse');
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
                        predDiv.innerHTML = noticeBlock + statsBlock + streakTableBlock + extraLine;
                    }
                    
                    // 가상 배팅 계산기 1,2,3 요약·상세 갱신 (오류 시에도 메인 화면은 유지)
                    try {
                        CALC_IDS.forEach(id => updateCalcSummary(id));
                        CALC_IDS.forEach(id => updateCalcDetail(id));
                    } catch (calcErr) {
                        console.warn('계산기 갱신 오류:', calcErr);
                    }
                } else if (statsDiv) {
                    statsDiv.innerHTML = '';
                    const resultBarEmpty = document.getElementById('prediction-result-bar');
                    const pickEmpty = document.getElementById('prediction-pick-container');
                    const predDivEmpty = document.getElementById('prediction-box');
                    const probBucketBodyEmpty = document.getElementById('prob-bucket-collapse-body');
                    const probBucketCollapseEmpty = document.getElementById('prob-bucket-collapse');
                    const symmetryLineBodyEmpty = document.getElementById('symmetry-line-collapse-body');
                    const symmetryLineCollapseEmpty = document.getElementById('symmetry-line-collapse');
                    const graphStatsCollapseEmpty = document.getElementById('graph-stats-collapse');
                    if (resultBarEmpty) resultBarEmpty.innerHTML = '';
                    if (pickEmpty) pickEmpty.innerHTML = '';
                    if (predDivEmpty) predDivEmpty.innerHTML = '';
                    if (probBucketBodyEmpty) probBucketBodyEmpty.innerHTML = '';
                    if (probBucketCollapseEmpty) probBucketCollapseEmpty.style.display = 'none';
                    if (symmetryLineBodyEmpty) symmetryLineBodyEmpty.innerHTML = '';
                    if (symmetryLineCollapseEmpty) symmetryLineCollapseEmpty.style.display = 'none';
                    if (graphStatsCollapseEmpty) graphStatsCollapseEmpty.style.display = 'none';
                }
                
                // 헤더: 상단에는 회차 전체 숫자 표시 (비교용), 표에는 뒤 3자리만
                if (displayResults.length > 0) {
                    const latest = displayResults[0];
                    const fullGameID = latest.gameID != null && latest.gameID !== '' ? String(latest.gameID) : '--';
                    const prevRoundElement = document.getElementById('prev-round');
                    if (prevRoundElement) {
                        prevRoundElement.textContent = '이전회차: ' + fullGameID;
                    }
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
            const hist = calcState[id].history || [];
            let cap = capIn, currentBet = baseIn, bust = false;
            let martingaleStep = 0;
            let wins = 0, losses = 0, maxWinStreak = 0, maxLoseStreak = 0, curWin = 0, curLose = 0;
            let processedCount = 0;
            for (let i = 0; i < hist.length; i++) {
                const h = hist[i];
                if (!h || typeof h.predicted === 'undefined' || typeof h.actual === 'undefined') continue;
                if (useMartingale && martingaleType === 'pyo') {
                    currentBet = TABLE_MARTIN_PYO[Math.min(martingaleStep, TABLE_MARTIN_PYO.length - 1)];
                }
                const bet = Math.min(currentBet, Math.floor(cap));
                if (cap < bet || cap <= 0) { bust = true; processedCount = i; break; }
                const isJoker = h.actual === 'joker';
                const isWin = !isJoker && h.predicted === h.actual;
                if (isJoker) {
                    cap -= bet;
                    if (useMartingale && martingaleType === 'pyo') martingaleStep = Math.min(martingaleStep + 1, TABLE_MARTIN_PYO.length - 1);
                    else currentBet = Math.min(currentBet * 2, Math.floor(cap));
                    curWin = 0;
                    curLose = 0;
                } else if (isWin) {
                    cap += bet * (oddsIn - 1);
                    if (useMartingale && martingaleType === 'pyo') martingaleStep = 0;
                    else currentBet = baseIn;
                    wins++;
                    curWin++;
                    curLose = 0;
                    if (curWin > maxWinStreak) maxWinStreak = curWin;
                } else {
                    cap -= bet;
                    if (useMartingale && martingaleType === 'pyo') martingaleStep = Math.min(martingaleStep + 1, TABLE_MARTIN_PYO.length - 1);
                    else currentBet = Math.min(currentBet * 2, Math.floor(cap));
                    losses++;
                    curLose++;
                    curWin = 0;
                    if (curLose > maxLoseStreak) maxLoseStreak = curLose;
                }
                processedCount = i + 1;
                if (cap <= 0) { bust = true; break; }
            }
            if (useMartingale && martingaleType === 'pyo') {
                currentBet = bust ? 0 : TABLE_MARTIN_PYO[Math.min(martingaleStep, TABLE_MARTIN_PYO.length - 1)];
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
        function getDefenseBetAmount(linkedId) {
            const linkedBet = getCalcResult(linkedId).currentBet;
            const linkedBase = parseFloat(document.getElementById('calc-' + linkedId + '-base')?.value) || 10000;
            if (!linkedBet || linkedBet <= linkedBase) return 0;
            const fullSteps = Math.max(0, parseInt(document.getElementById('calc-defense-full-steps')?.value, 10) || 3);
            const reduceFrom = Math.max(1, parseInt(document.getElementById('calc-defense-reduce-from')?.value, 10) || 4);
            const reduceDiv = Math.max(2, parseInt(document.getElementById('calc-defense-reduce-div')?.value, 10) || 4);
            const stopStreak = parseInt(document.getElementById('calc-defense-stop-streak')?.value, 10) || 0;
            const hist = (calcState.defense && calcState.defense.history) || [];
            let consecutiveWins = 0;
            for (let i = hist.length - 1; i >= 0; i--) {
                const h = hist[i];
                if ((h.betAmount || 0) <= 0) break;
                if (h.actual === 'joker') break;
                if (h.predicted === h.actual) consecutiveWins++; else break;
            }
            if (stopStreak > 0 && consecutiveWins >= stopStreak) return 0;
            const ratio = linkedBet / linkedBase;
            const step = ratio <= 1 ? 0 : Math.round(Math.log2(ratio));
            if (step <= fullSteps) return linkedBet;
            if (step >= reduceFrom) return Math.floor(linkedBet / reduceDiv);
            return linkedBet;
        }
        function getDefenseCalcResult() {
            try {
            const d = calcState.defense;
            if (!d || !d.history || d.history.length === 0) return { cap: 0, profit: 0, currentBet: 0, wins: 0, losses: 0, bust: false, maxWinStreak: 0, maxLoseStreak: 0, winRate: '-' };
            const capIn = parseFloat(document.getElementById('calc-defense-capital')?.value) || 1000000;
            const oddsIn = parseFloat(document.getElementById('calc-defense-odds')?.value) || 1.97;
            const hist = d.history || [];
            let cap = capIn, bust = false;
            let wins = 0, losses = 0, maxWinStreak = 0, maxLoseStreak = 0, curWin = 0, curLose = 0;
            for (let i = 0; i < hist.length; i++) {
                const h = hist[i];
                const betAmount = (h && typeof h.betAmount === 'number' ? h.betAmount : 0) || (h && parseInt(h.betAmount, 10)) || 0;
                if (!h || (typeof h.predicted === 'undefined' && typeof h.actual === 'undefined')) continue;
                if (betAmount <= 0) continue;
                const isJoker = h.actual === 'joker';
                const isWin = !isJoker && h.predicted === h.actual;
                if (cap < betAmount || cap <= 0) { bust = true; break; }
                if (isJoker) {
                    cap -= betAmount;
                    curWin = 0;
                    curLose = 0;
                } else if (isWin) {
                    cap += betAmount * (oddsIn - 1);
                    wins++;
                    curWin++;
                    curLose = 0;
                    if (curWin > maxWinStreak) maxWinStreak = curWin;
                } else {
                    cap -= betAmount;
                    losses++;
                    curLose++;
                    curWin = 0;
                    if (curLose > maxLoseStreak) maxLoseStreak = curLose;
                }
                if (cap <= 0) { bust = true; break; }
            }
            if (calcState.defense) {
                calcState.defense.maxWinStreakEver = Math.max(calcState.defense.maxWinStreakEver || 0, maxWinStreak);
                calcState.defense.maxLoseStreakEver = Math.max(calcState.defense.maxLoseStreakEver || 0, maxLoseStreak);
            }
            const linkedId = d.linked_calc_id || 1;
            const currentBet = (d.running && calcState[linkedId] && calcState[linkedId].running) ? getDefenseBetAmount(linkedId) : 0;
            const profit = cap - capIn;
            const total = wins + losses;
            const winRate = total > 0 ? (100 * wins / total).toFixed(1) : '-';
            const displayMaxWin = (calcState.defense && calcState.defense.maxWinStreakEver != null) ? calcState.defense.maxWinStreakEver : maxWinStreak;
            const displayMaxLose = (calcState.defense && calcState.defense.maxLoseStreakEver != null) ? calcState.defense.maxLoseStreakEver : maxLoseStreak;
            return { cap: Math.max(0, Math.floor(cap)), profit, currentBet, wins, losses, bust, maxWinStreak: displayMaxWin, maxLoseStreak: displayMaxLose, winRate };
            } catch (e) { console.warn('getDefenseCalcResult', e); return { cap: 0, profit: 0, currentBet: 0, wins: 0, losses: 0, bust: false, maxWinStreak: 0, maxLoseStreak: 0, winRate: '-' }; }
        }
        function updateCalcStatus(id) {
            try {
            const statusId = id === DEFENSE_ID ? 'calc-defense-status' : ('calc-' + id + '-status');
            const el = document.getElementById(statusId);
            if (!el) return;
            const state = id === DEFENSE_ID ? calcState.defense : calcState[id];
            if (!state) return;
            el.className = 'calc-status';
            if (state.running) {
                el.classList.add('running');
                var statusTxt = '실행중';
                if (id !== DEFENSE_ID) {
                    if (!!(state.reverse)) statusTxt += ' · 반픽';
                    if (!!(state.win_rate_reverse)) statusTxt += ' · 승률반픽';
                }
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
            // 계산기 1,2,3: 예측픽 = 메인과 동일( lastPrediction.value + lastPrediction.color로 색 보장 ). 반픽/승률반픽이면 배팅만 반대로.
            if (id !== DEFENSE_ID) {
                try {
                    const bettingCardEl = document.getElementById('calc-' + id + '-current-card');
                    const predictionCardEl = document.getElementById('calc-' + id + '-prediction-card');
                    if (!bettingCardEl || !predictionCardEl) return;
                    if (state.running && lastPrediction && (lastPrediction.value === '정' || lastPrediction.value === '꺽')) {
                        var predictionText = lastPrediction.value;
                        var predictionIsRed = (lastPrediction.color === '빨강' || lastPrediction.color === '검정') ? (lastPrediction.color === '빨강') : (predictionText === '정');
                        var bettingText = predictionText;
                        var bettingIsRed = predictionIsRed;
                        const rev = !!(calcState[id] && calcState[id].reverse);
                        if (rev) { bettingText = bettingText === '정' ? '꺽' : '정'; bettingIsRed = !bettingIsRed; }
                        var lowWinRate = false;
                        try {
                            var vh = (typeof predictionHistory !== 'undefined' && Array.isArray(predictionHistory)) ? predictionHistory.filter(function(h) { return h && typeof h === 'object'; }) : [];
                            var v15 = vh.slice(-15), v30 = vh.slice(-30), v100 = vh.slice(-100);
                            var hit15r = v15.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
                            var loss15 = v15.filter(function(h) { return h.actual !== 'joker' && h.predicted !== h.actual; }).length;
                            var c15 = hit15r + loss15, r15 = c15 > 0 ? 100 * hit15r / c15 : 50;
                            var hit30r = v30.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
                            var loss30 = v30.filter(function(h) { return h.actual !== 'joker' && h.predicted !== h.actual; }).length;
                            var c30 = hit30r + loss30, r30 = c30 > 0 ? 100 * hit30r / c30 : 50;
                            var hit100r = v100.filter(function(h) { return h.actual !== 'joker' && h.predicted === h.actual; }).length;
                            var loss100 = v100.filter(function(h) { return h.actual !== 'joker' && h.predicted !== h.actual; }).length;
                            var c100 = hit100r + loss100, r100 = c100 > 0 ? 100 * hit100r / c100 : 50;
                            var blended = 0.5 * r15 + 0.3 * r30 + 0.2 * r100;
                            var thrCardEl = document.getElementById('calc-' + id + '-win-rate-threshold');
                            var thrCardNum = (thrCardEl && !isNaN(parseFloat(thrCardEl.value))) ? Math.max(0, Math.min(100, parseFloat(thrCardEl.value))) : (calcState[id] != null && typeof calcState[id].win_rate_threshold === 'number' ? calcState[id].win_rate_threshold : 50);
                            if (typeof thrCardNum !== 'number' || isNaN(thrCardNum)) thrCardNum = 50;
                            lowWinRate = (c15 > 0 || c30 > 0 || c100 > 0) && typeof blended === 'number' && blended <= thrCardNum;
                        } catch (e2) {}
                        const useWinRateRevCard = !!(calcState[id] && calcState[id].win_rate_reverse);
                        if (useWinRateRevCard && lowWinRate) { bettingText = bettingText === '정' ? '꺽' : '정'; bettingIsRed = !bettingIsRed; }
                        predictionCardEl.textContent = predictionText;
                        predictionCardEl.className = 'calc-current-card calc-card-prediction card-' + (predictionIsRed ? 'jung' : 'kkuk');
                        bettingCardEl.textContent = bettingText;
                        bettingCardEl.className = 'calc-current-card calc-card-betting card-' + (bettingIsRed ? 'jung' : 'kkuk');
                    } else {
                        bettingCardEl.textContent = '';
                        bettingCardEl.className = 'calc-current-card calc-card-betting';
                        predictionCardEl.textContent = '';
                        predictionCardEl.className = 'calc-current-card calc-card-prediction';
                    }
                } catch (cardErr) { console.warn('updateCalcStatus card', id, cardErr); }
            }
            } catch (e) { console.warn('updateCalcStatus', id, e); }
        }
        function updateCalcSummary(id) {
            try {
            const summaryId = id === DEFENSE_ID ? 'calc-defense-summary' : ('calc-' + id + '-summary');
            const el = document.getElementById(summaryId);
            if (!el) return;
            const state = id === DEFENSE_ID ? calcState.defense : calcState[id];
            const hist = state.history || [];
            const elapsedStr = state.running && typeof formatMmSs === 'function' ? formatMmSs(state.elapsed || 0) : '-';
            const timerNote = state.timer_completed ? '<span class="calc-timer-note" style="color:#64b5f6;font-weight:bold;grid-column:1/-1">타이머 완료</span>' : '';
            if (hist.length === 0) {
                var targetNoteEmpty = '';
                if (id !== DEFENSE_ID) {
                    const targetEnabledEl = document.getElementById('calc-' + id + '-target-enabled');
                    const targetAmountEl = document.getElementById('calc-' + id + '-target-amount');
                    const targetEnabled = !!(targetEnabledEl && targetEnabledEl.checked);
                    const targetAmount = Math.max(0, parseInt(targetAmountEl?.value, 10) || 0);
                    if (targetEnabled && targetAmount > 0) targetNoteEmpty = '<span class="calc-timer-note" style="grid-column:1/-1">목표금액: ' + targetAmount.toLocaleString() + '원 / 목표까지: ' + targetAmount.toLocaleString() + '원 남음</span>';
                }
                el.innerHTML = '<div class="calc-summary-grid">' + timerNote + targetNoteEmpty +
                    '<span class="label">보유자산</span><span class="value">-</span>' +
                    '<span class="label">순익</span><span class="value">-</span>' +
                    '<span class="label">배팅중</span><span class="value">-</span>' +
                    '<span class="label">경과</span><span class="value">' + elapsedStr + '</span></div>';
                updateCalcStatus(id);
                return;
            }
            const r = id === DEFENSE_ID ? getDefenseCalcResult() : getCalcResult(id);
            const profitStr = (r.profit >= 0 ? '+' : '') + r.profit.toLocaleString() + '원';
            const profitClass = r.profit > 0 ? 'profit-plus' : (r.profit < 0 ? 'profit-minus' : '');
            var targetNote = '';
            if (id !== DEFENSE_ID) {
                const targetEnabledEl = document.getElementById('calc-' + id + '-target-enabled');
                const targetAmountEl = document.getElementById('calc-' + id + '-target-amount');
                const targetEnabled = !!(targetEnabledEl && targetEnabledEl.checked);
                const targetAmount = Math.max(0, parseInt(targetAmountEl?.value, 10) || 0);
                if (targetEnabled && targetAmount > 0) {
                    const remain = targetAmount - r.profit;
                    if (remain <= 0) targetNote = '<span class="calc-timer-note" style="color:#81c784;font-weight:bold;grid-column:1/-1">목표금액: ' + targetAmount.toLocaleString() + '원 / 달성</span>';
                    else targetNote = '<span class="calc-timer-note" style="grid-column:1/-1">목표금액: ' + targetAmount.toLocaleString() + '원 / 목표까지: ' + remain.toLocaleString() + '원 남음</span>';
                }
            }
            el.innerHTML = '<div class="calc-summary-grid">' + timerNote + targetNote +
                '<span class="label">보유자산</span><span class="value">' + r.cap.toLocaleString() + '원</span>' +
                '<span class="label">순익</span><span class="value ' + profitClass + '">' + profitStr + '</span>' +
                '<span class="label">배팅중</span><span class="value">' + r.currentBet.toLocaleString() + '원</span>' +
                '<span class="label">경과</span><span class="value">' + elapsedStr + '</span></div>';
            updateCalcStatus(id);
            } catch (e) { console.warn('updateCalcSummary', id, e); }
        }
        function appendCalcLog(id) {
            const state = id === DEFENSE_ID ? calcState.defense : calcState[id];
            if (!state || !state.history || state.history.length === 0) return;
            const now = new Date();
            const dateStr = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0') + '-' + String(now.getDate()).padStart(2, '0') + '_' + String(now.getHours()).padStart(2, '0') + String(now.getMinutes()).padStart(2, '0');
            const r = id === DEFENSE_ID ? getDefenseCalcResult() : getCalcResult(id);
            let logLine;
            if (id === DEFENSE_ID) {
                logLine = dateStr + '_방어_순익' + (r.profit >= 0 ? '+' : '') + r.profit + '원_승' + r.wins + '패' + r.losses + '_승률' + r.winRate + '%_최대연승' + r.maxWinStreak + '_최대연패' + r.maxLoseStreak;
            } else {
                const rev = document.getElementById('calc-' + id + '-reverse')?.checked;
                const baseIn = parseFloat(document.getElementById('calc-' + id + '-base')?.value) || 10000;
                const pickType = rev ? '반픽' : '정픽';
                logLine = dateStr + '_계산기' + id + '_' + pickType + '_배팅' + baseIn + '원_순익' + (r.profit >= 0 ? '+' : '') + r.profit + '원_승' + r.wins + '패' + r.losses + '_승률' + r.winRate + '%';
            }
            const histCopy = JSON.parse(JSON.stringify(state.history || []));
            betCalcLog.unshift({ line: logLine, calcId: id === DEFENSE_ID ? 'defense' : String(id), history: histCopy });
            saveBetCalcLog();
            renderBetCalcLog();
        }
        function updateCalcDetail(id) {
            try {
            const streakId = id === DEFENSE_ID ? 'calc-defense-streak' : ('calc-' + id + '-streak');
            const statsId = id === DEFENSE_ID ? 'calc-defense-stats' : ('calc-' + id + '-stats');
            const tableWrapId = id === DEFENSE_ID ? 'calc-defense-round-table-wrap' : ('calc-' + id + '-round-table-wrap');
            const streakEl = document.getElementById(streakId);
            const statsEl = document.getElementById(statsId);
            const tableWrap = document.getElementById(tableWrapId);
            if (!streakEl || !statsEl) return;
            const state = id === DEFENSE_ID ? calcState.defense : calcState[id];
            if (!state) return;
            const hist = state.history || [];
            if (hist.length === 0) {
                streakEl.textContent = id === DEFENSE_ID ? '경기결과 (최근 30회): - (연결 계산기의 반픽·동일 배팅금)' : '경기결과 (최근 30회): -';
                statsEl.textContent = '최대연승: - | 최대연패: - | 승률: -';
                if (tableWrap) tableWrap.innerHTML = '';
                return;
            }
            const r = id === DEFENSE_ID ? getDefenseCalcResult() : getCalcResult(id);
            const usedLen = (r.processedCount !== undefined && r.processedCount >= 0) ? r.processedCount : hist.length;
            const usedHist = hist.slice(0, usedLen);
            const oddsIn = parseFloat(document.getElementById(id === DEFENSE_ID ? 'calc-defense-odds' : ('calc-' + id + '-odds'))?.value) || 1.97;
            var betAmounts = [], profits = [];
            if (id === DEFENSE_ID) {
                for (let i = 0; i < usedHist.length; i++) {
                    const h = usedHist[i];
                    const bet = (h && typeof h.betAmount === 'number' ? h.betAmount : 0) || (h && parseInt(h.betAmount, 10)) || 0;
                    if (!h || (typeof h.predicted === 'undefined' && typeof h.actual === 'undefined')) { betAmounts[i] = null; profits[i] = null; continue; }
                    if (bet <= 0) { betAmounts[i] = null; profits[i] = null; continue; }
                    const isJoker = h.actual === 'joker';
                    const isWin = !isJoker && h.predicted === h.actual;
                    betAmounts[i] = bet;
                    profits[i] = isJoker ? -bet : (isWin ? Math.floor(bet * (oddsIn - 1)) : -bet);
                }
            } else {
                const capIn = parseFloat(document.getElementById('calc-' + id + '-capital')?.value) || 1000000;
                const baseIn = parseFloat(document.getElementById('calc-' + id + '-base')?.value) || 10000;
                const martingaleEl = document.getElementById('calc-' + id + '-martingale');
                const martingaleTypeEl = document.getElementById('calc-' + id + '-martingale-type');
                const useMartingale = !!(martingaleEl && martingaleEl.checked);
                const martingaleType = (martingaleTypeEl && martingaleTypeEl.value) || 'pyo';
                let cap = capIn, currentBet = baseIn, martingaleStep = 0;
                for (let i = 0; i < usedHist.length; i++) {
                    const h = usedHist[i];
                    if (!h || typeof h.predicted === 'undefined' || typeof h.actual === 'undefined') { betAmounts[i] = null; profits[i] = null; continue; }
                    if (useMartingale && martingaleType === 'pyo') currentBet = TABLE_MARTIN_PYO[Math.min(martingaleStep, TABLE_MARTIN_PYO.length - 1)];
                    const bet = Math.min(currentBet, Math.floor(cap));
                    if (cap < bet || cap <= 0) { betAmounts[i] = null; profits[i] = null; break; }
                    const isJoker = h.actual === 'joker';
                    const isWin = !isJoker && h.predicted === h.actual;
                    betAmounts[i] = bet;
                    profits[i] = isJoker ? -bet : (isWin ? Math.floor(bet * (oddsIn - 1)) : -bet);
                    if (isJoker) { cap -= bet; if (useMartingale && martingaleType === 'pyo') martingaleStep = Math.min(martingaleStep + 1, TABLE_MARTIN_PYO.length - 1); else currentBet = Math.min(currentBet * 2, Math.floor(cap)); }
                    else if (isWin) { cap += bet * (oddsIn - 1); if (useMartingale && martingaleType === 'pyo') martingaleStep = 0; else currentBet = baseIn; }
                    else { cap -= bet; if (useMartingale && martingaleType === 'pyo') martingaleStep = Math.min(martingaleStep + 1, TABLE_MARTIN_PYO.length - 1); else currentBet = Math.min(currentBet * 2, Math.floor(cap)); }
                }
            }
            // 회차별 픽/결과/승패/배팅금액/수익 행 목록 (유효 항목만, 최신순 = 뒤에서부터)
            let rows = [];
            if (id === DEFENSE_ID) {
                for (let i = usedHist.length - 1; i >= 0; i--) {
                    const h = usedHist[i];
                    const bet = (h && typeof h.betAmount === 'number' ? h.betAmount : 0) || (h && parseInt(h.betAmount, 10)) || 0;
                    if (!h || (typeof h.predicted === 'undefined' && typeof h.actual === 'undefined')) continue;
                    const roundStr = h.round != null ? String(h.round).slice(-3) : '-';
                    if (bet <= 0) { rows.push({ roundStr: roundStr, pick: '-', pickClass: '', result: '-', resultClass: '', outcome: '－', betAmount: '-', profit: '-' }); continue; }
                    const res = h.actual === 'joker' ? '조' : (h.actual === '정' ? '정' : '꺽');
                    const outcome = h.actual === 'joker' ? '조' : (h.predicted === h.actual ? '승' : '패');
                    const pickVal = h.predicted === '정' ? '정' : '꺽';
                    const pickClass = pickVal === '정' ? 'pick-jung' : 'pick-kkuk';
                    const resultClass = res === '조' ? 'result-joker' : (res === '정' ? 'result-jung' : 'result-kkuk');
                    const profitVal = profits[i] != null ? profits[i] : '-';
                    const profitStr = profitVal === '-' ? '-' : (profitVal >= 0 ? '+' : '') + Number(profitVal).toLocaleString();
                    rows.push({ roundStr: roundStr, pick: pickVal, pickClass: pickClass, result: res, resultClass: resultClass, outcome: outcome, betAmount: bet.toLocaleString(), profit: profitStr });
                }
            } else {
                for (let i = usedHist.length - 1; i >= 0; i--) {
                    const h = usedHist[i];
                    if (!h || typeof h.predicted === 'undefined' || typeof h.actual === 'undefined') continue;
                    const roundStr = h.round != null ? String(h.round).slice(-3) : '-';
                    const res = h.actual === 'joker' ? '조' : (h.actual === '정' ? '정' : '꺽');
                    const outcome = h.actual === 'joker' ? '조' : (h.predicted === h.actual ? '승' : '패');
                    const pickVal = h.predicted === '정' ? '정' : '꺽';
                    const pickClass = pickVal === '정' ? 'pick-jung' : 'pick-kkuk';
                    const resultClass = res === '조' ? 'result-joker' : (res === '정' ? 'result-jung' : 'result-kkuk');
                    const betStr = betAmounts[i] != null ? betAmounts[i].toLocaleString() : '-';
                    const profitVal = profits[i] != null ? profits[i] : '-';
                    const profitStr = profitVal === '-' ? '-' : (profitVal >= 0 ? '+' : '') + Number(profitVal).toLocaleString();
                    rows.push({ roundStr: roundStr, pick: pickVal, pickClass: pickClass, result: res, resultClass: resultClass, outcome: outcome, betAmount: betStr, profit: profitStr });
                }
            }
            const displayRows = rows.slice(0, 15);
            if (tableWrap) {
                if (displayRows.length === 0) {
                    tableWrap.innerHTML = '';
                } else {
                    let tbl = '<table class="calc-round-table"><thead><tr><th>회차</th><th>픽(걸은 것)</th><th>배팅금액</th><th>수익</th><th>승패</th></tr></thead><tbody>';
                    displayRows.forEach(function(row) {
                        const outClass = row.outcome === '승' ? 'win' : row.outcome === '패' ? 'lose' : row.outcome === '조' ? 'joker' : 'skip';
                        const profitClass = (typeof row.profit === 'number' && row.profit > 0) || (typeof row.profit === 'string' && row.profit.indexOf('+') === 0) ? 'profit-plus' : (typeof row.profit === 'number' && row.profit < 0) || (typeof row.profit === 'string' && row.profit.indexOf('-') === 0 && row.profit !== '-') ? 'profit-minus' : '';
                        tbl += '<tr><td>' + row.roundStr + '</td><td class="' + row.pickClass + '">' + row.pick + '</td><td class="calc-td-bet">' + row.betAmount + '</td><td class="calc-td-profit ' + profitClass + '">' + row.profit + '</td><td class="' + outClass + '">' + row.outcome + '</td></tr>';
                    });
                    tbl += '</tbody></table>';
                    tableWrap.innerHTML = tbl;
                }
            }
            // getCalcResult와 동일 기준: 무효 항목은 표시에서 제외. 경기결과는 최근 30회만 표시(저장은 전부 유지)
            let arr = [];
            if (id === DEFENSE_ID) {
                arr = usedHist.map(h => ((h.betAmount || 0) <= 0 ? '-' : (h.actual === 'joker' ? 'j' : (h.predicted === h.actual ? 'w' : 'l'))));
            } else {
                for (const h of usedHist) {
                    if (!h || typeof h.predicted === 'undefined' || typeof h.actual === 'undefined') continue;
                    arr.push(h.actual === 'joker' ? 'j' : (h.predicted === h.actual ? 'w' : 'l'));
                }
            }
            const arrRev = arr.slice().reverse();
            const showMax = 30;
            const arrShow = arrRev.slice(0, showMax);
            const streakStr = arrShow.map(a => {
                if (a === '-') return '<span class="defense-skip">－</span>';
                return '<span class="' + (a === 'w' ? 'w' : a === 'l' ? 'l' : 'j') + '">' + (a === 'w' ? '승' : a === 'l' ? '패' : '조') + '</span>';
            }).join(' ');
            streakEl.innerHTML = '경기결과 (최근 30회←): ' + streakStr + (id === DEFENSE_ID ? ' <span class="defense-skip">※－=미배팅</span>' : '');
            statsEl.textContent = '최대연승: ' + r.maxWinStreak + ' | 최대연패: ' + r.maxLoseStreak + ' | 승률: ' + r.winRate + '%';
            } catch (e) { console.warn('updateCalcDetail', id, e); }
        }
        document.querySelectorAll('.calc-dropdown-header').forEach(h => {
            h.addEventListener('click', function() {
                const dd = this.closest('.calc-dropdown');
                if (dd) dd.classList.toggle('collapsed');
            });
        });
        document.querySelectorAll('.bet-calc-tabs .tab').forEach(tab => {
            tab.addEventListener('click', function() {
                const t = this.getAttribute('data-tab');
                document.querySelectorAll('.bet-calc-tabs .tab').forEach(x => x.classList.remove('active'));
                this.classList.add('active');
                const calcPanel = document.getElementById('bet-calc-panel');
                const logPanel = document.getElementById('bet-log-panel');
                if (calcPanel) calcPanel.classList.toggle('active', t === 'calc');
                if (logPanel) logPanel.classList.toggle('active', t === 'log');
            });
        });
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
                    saveCalcStateToServer();
                    updateCalcSummary(id);
                    updateCalcStatus(id);
                    const saveBtn = document.querySelector('.calc-save[data-calc="' + id + '"]');
                    if (saveBtn) saveBtn.style.display = 'none';
                }
            });
            if (calcState.defense.running) {
                const started = calcState.defense.started_at || 0;
                calcState.defense.elapsed = started ? Math.max(0, st - started) : 0;
                updateCalcSummary(DEFENSE_ID);
                if (calcState.defense.use_duration_limit && calcState.defense.duration_limit > 0 && calcState.defense.elapsed >= calcState.defense.duration_limit) {
                    calcState.defense.running = false;
                    calcState.defense.timer_completed = true;
                    if (calcState.defense.history.length > 0) appendCalcLog(DEFENSE_ID);
                    saveCalcStateToServer();
                    updateCalcSummary(DEFENSE_ID);
                    updateCalcStatus(DEFENSE_ID);
                }
            }
            }, 1000);
        function updateAllCalcs() {
            CALC_IDS.forEach(id => { updateCalcSummary(id); updateCalcDetail(id); updateCalcStatus(id); });
            updateCalcSummary(DEFENSE_ID); updateCalcDetail(DEFENSE_ID); updateCalcStatus(DEFENSE_ID);
        }
        try { updateAllCalcs(); } catch (e) { console.warn('초기 계산기 상태:', e); }
        document.querySelectorAll('.calc-run').forEach(btn => {
            btn.addEventListener('click', async function() {
                const rawId = this.getAttribute('data-calc');
                const id = rawId === 'defense' ? DEFENSE_ID : parseInt(rawId, 10);
                const state = id === DEFENSE_ID ? calcState.defense : calcState[id];
                if (!state || state.running) return;
                if (!localStorage.getItem(CALC_SESSION_KEY)) {
                    await loadCalcStateFromServer();
                }
                if (id === DEFENSE_ID) {
                    const defLinkEl = document.getElementById('calc-defense-linked');
                    const defDurEl = document.getElementById('calc-defense-duration');
                    const defCheckEl = document.getElementById('calc-defense-duration-check');
                    calcState.defense.linked_calc_id = (defLinkEl && parseInt(defLinkEl.value, 10)) || 1;
                    calcState.defense.duration_limit = ((defDurEl && parseInt(defDurEl.value, 10)) || 0) * 60;
                    calcState.defense.use_duration_limit = !!(defCheckEl && defCheckEl.checked);
                    calcState.defense.timer_completed = false;
                    calcState.defense.running = true;
                    calcState.defense.history = [];
                    calcState.defense.started_at = 0;
                    calcState.defense.elapsed = 0;
                    calcState.defense.maxWinStreakEver = 0;
                    calcState.defense.maxLoseStreakEver = 0;
                    var defLatestG = null;
                    try { defLatestG = window.__latestGameIDForCalc; } catch (e) {}
                    var defNextRound = 0;
                    if (defLatestG != null && defLatestG !== '') { var dn = parseInt(String(defLatestG), 10); if (!isNaN(dn)) defNextRound = dn + 1; }
                    calcState.defense.first_bet_round = defNextRound;
                    try {
                        const defPayload = buildCalcPayload();
                        defPayload[DEFENSE_ID].first_bet_round = calcState.defense.first_bet_round;
                        const res = await fetch('/api/calc-state', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: localStorage.getItem(CALC_SESSION_KEY), calcs: defPayload }) });
                        const data = await res.json();
                        if (data.calcs && data.calcs[DEFENSE_ID]) calcState.defense.started_at = data.calcs[DEFENSE_ID].started_at || 0;
                        if (data.server_time) lastServerTimeSec = data.server_time;
                    } catch (e) { console.warn('방어 계산기 실행 저장 실패:', e); }
                    updateCalcSummary(DEFENSE_ID);
                    updateCalcDetail(DEFENSE_ID);
                    updateCalcStatus(DEFENSE_ID);
                    return;
                }
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
                var thrRun = (winRateThrRun && parseFloat(winRateThrRun.value) != null && !isNaN(parseFloat(winRateThrRun.value))) ? Math.max(0, Math.min(100, parseFloat(winRateThrRun.value))) : 50;
                calcState[id].win_rate_threshold = thrRun;
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
                    const session_id = localStorage.getItem(CALC_SESSION_KEY);
                    const res = await fetch('/api/calc-state', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: session_id, calcs: payload }) });
                    const data = await res.json();
                    if (data.calcs && data.calcs[String(id)]) {
                        calcState[id].started_at = data.calcs[String(id)].started_at || 0;
                        lastServerTimeSec = data.server_time || lastServerTimeSec;
                    }
                } catch (e) { console.warn('계산기 실행 저장 실패:', e); }
                updateCalcSummary(id);
                updateCalcDetail(id);
                updateCalcStatus(id);
                document.querySelector('.calc-save[data-calc="' + id + '"]').style.display = 'none';
            });
        });
        document.querySelectorAll('.calc-stop').forEach(btn => {
            btn.addEventListener('click', function() {
                const rawId = this.getAttribute('data-calc');
                const id = rawId === 'defense' ? DEFENSE_ID : parseInt(rawId, 10);
                const state = id === DEFENSE_ID ? calcState.defense : calcState[id];
                if (!state) return;
                state.running = false;
                state.timer_completed = false;
                if (state.timerId) { clearInterval(state.timerId); state.timerId = null; }
                saveCalcStateToServer();
                updateCalcSummary(id);
                updateCalcDetail(id);
                updateCalcStatus(id);
                if (id === DEFENSE_ID && state.history.length > 0) {
                    appendCalcLog(DEFENSE_ID);
                } else if (id !== DEFENSE_ID && state.history.length > 0) {
                    const saveBtn = document.querySelector('.calc-save[data-calc="' + id + '"]');
                    if (saveBtn) saveBtn.style.display = 'inline-block';
                }
            });
        });
        document.querySelectorAll('.calc-reset').forEach(btn => {
            btn.addEventListener('click', function() {
                const rawId = this.getAttribute('data-calc');
                const id = rawId === 'defense' ? DEFENSE_ID : parseInt(rawId, 10);
                const state = id === DEFENSE_ID ? calcState.defense : calcState[id];
                if (!state) return;
                state.running = false;
                state.timer_completed = false;
                if (state.timerId) { clearInterval(state.timerId); state.timerId = null; }
                state.history = [];
                state.elapsed = 0;
                saveCalcStateToServer();
                updateCalcSummary(id);
                updateCalcDetail(id);
                updateCalcStatus(id);
                if (id !== DEFENSE_ID) {
                    const saveBtn = document.querySelector('.calc-save[data-calc="' + id + '"]');
                    if (saveBtn) saveBtn.style.display = 'none';
                }
            });
        });
        document.querySelectorAll('.calc-save').forEach(btn => {
            btn.addEventListener('click', function() {
                const id = parseInt(this.getAttribute('data-calc'), 10);
                if (calcState[id].history.length === 0) return;
                appendCalcLog(id);
                this.style.display = 'none';
            });
        });
        CALC_IDS.forEach(id => {
            ['capital', 'base', 'odds', 'target-amount'].forEach(f => {
                const el = document.getElementById('calc-' + id + '-' + f);
                if (el) el.addEventListener('input', () => { updateCalcSummary(id); updateCalcDetail(id); });
            });
            const targetEnabledEl = document.getElementById('calc-' + id + '-target-enabled');
            if (targetEnabledEl) targetEnabledEl.addEventListener('change', () => { updateCalcSummary(id); });
        });
        
        let timerData = { elapsed: 0, lastFetch: 0, round: 0, serverTime: 0 };
        let lastResultsUpdate = 0;
        let lastTimerUpdate = Date.now();
        async function updateTimer() {
            try {
                const now = Date.now();
                const timeElement = document.getElementById('remaining-time');
                
                if (!timeElement) {
                    return;
                }
                // 클라이언트 측 남은 시간 (폴링 간격·결과 새로고침 판단용)
                const timeDiff = (now - timerData.serverTime) / 1000;
                const currentElapsed = Math.max(0, timerData.elapsed + timeDiff);
                const remaining = Math.max(0, 10 - currentElapsed);
                // 라운드 종료 직전/직후에는 더 자주 폴링 (다음 픽을 빨리 보여주기)
                const nearEnd = remaining < 3;
                const fetchInterval = nearEnd ? 300 : 500;
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
                                console.log('라운드 변경 감지:', { roundChanged, roundEnded, roundStarted, prevRound, newRound: timerData.round, prevElapsed, newElapsed: data.elapsed });
                                // 즉시 결과 로드 (승리/실패 결과 빨리 표시)
                                loadResults();
                                lastResultsUpdate = Date.now();
                                [80, 200, 350, 550, 800, 1100].forEach(function(ms) {
                                    setTimeout(function() { loadResults(); lastResultsUpdate = Date.now(); }, ms);
                                });
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
                
                // 타이머가 거의 0이 되면 경기 결과 즉시·반복 새로고침 (승리/실패 결과 빨리 표시)
                if (remaining <= 1.5 && now - lastResultsUpdate > 100) {
                    loadResults();
                    lastResultsUpdate = now;
                }
                if (remaining <= 0 && now - lastResultsUpdate > 50) {
                    loadResults();
                    lastResultsUpdate = now;
                    [100, 200, 350, 500, 700, 950, 1200].forEach(function(ms) {
                        setTimeout(function() { loadResults(); lastResultsUpdate = Date.now(); }, ms);
                    });
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
        
        // 데이터 없을 때 0.5초마다, 있으면 1초마다 (10초 경기 안에 결과 보기)
        setInterval(() => {
            const interval = allResults.length === 0 ? 500 : 1000;
            if (Date.now() - lastResultsUpdate > interval) {
                loadResults().catch(e => console.warn('결과 새로고침 실패:', e));
                lastResultsUpdate = Date.now();
            }
        }, 500);
        
        // 0.2초마다 타이머 업데이트 (UI만 업데이트, 서버 요청은 1초마다)
        setInterval(updateTimer, 200);
    </script>
</body>
</html>
'''

@app.route('/results', methods=['GET'])
def results_page():
    """경기 결과 웹페이지"""
    return render_template_string(RESULTS_HTML)

def _build_results_payload():
    """경기 결과 페이로드 생성 (스레드에서 호출, 먹통 시 None 반환)."""
    try:
        latest_results = load_results_data()
        if latest_results is None:
            latest_results = []
        print(f"[API] 최신 데이터 로드: {len(latest_results)}개")
        if DB_AVAILABLE and DATABASE_URL:
            # 데이터베이스에서 최근 5시간 데이터 조회
            db_results = get_recent_results(hours=5)
            print(f"[API] DB 데이터 조회: {len(db_results)}개")
            
            # 최신 데이터 저장 (백그라운드)
            if latest_results:
                try:
                    saved_count = 0
                    for game_data in latest_results:
                        if save_game_result(game_data):
                            saved_count += 1
                    if saved_count > 0:
                        print(f"[💾] 최신 데이터 {saved_count}개 저장 완료")
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
                print(f"[API] 병합 결과: 최신 {len(latest_results)}개 + DB {len(db_results_filtered)}개 = 총 {len(results)}개")
                
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
                print(f"[API] 최신 데이터 없음, DB 데이터만 사용: {len(results)}개")
            
            # 그래프/표시 순서 일관성: 항상 gameID 기준 최신순으로 정렬
            results = _sort_results_newest_first(results)
            return {
                'results': results,
                'count': len(results),
                'timestamp': datetime.now().isoformat(),
                'source': 'database+json',
                'prediction_history': get_prediction_history(100)
            }
        else:
            # 데이터베이스가 없으면 기존 방식 (result.json에서 가져오기)
            results = latest_results if latest_results else []
            results = _sort_results_newest_first(results)
            print(f"[API] DB 없음, 최신 데이터만 사용: {len(results)}개")
            
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
            
            return {
                'results': results,
                'count': len(results),
                'timestamp': datetime.now().isoformat(),
                'source': 'json',
                'prediction_history': get_prediction_history(100)
            }
    except Exception as e:
        print(f"[❌ 오류] _build_results_payload 실패: {str(e)[:200]}")
        return None


_results_refresh_lock = threading.Lock()
_results_refreshing = False

def _refresh_results_background():
    """백그라운드에서 캐시 갱신 (요청 스레드 블로킹 없음)"""
    global results_cache, last_update_time, _results_refreshing
    if not _results_refresh_lock.acquire(blocking=False):
        return
    _results_refreshing = True
    try:
        payload = _build_results_payload()
        if payload is not None:
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
    """경기 결과 API - 요청 스레드는 절대 블로킹 안 함. 캐시 있으면 즉시 반환, 없으면 이전 캐시/빈값 반환 후 백그라운드 갱신."""
    try:
        global results_cache, last_update_time
        current_time = time.time() * 1000
        if results_cache and (current_time - last_update_time) < CACHE_TTL:
            return jsonify(results_cache)
        # 캐시 만료 시: 즉시 응답(이전 캐시 또는 빈값), 갱신은 백그라운드에서만 (join 없음)
        if results_cache:
            if not _results_refreshing:
                threading.Thread(target=_refresh_results_background, daemon=True).start()
            return jsonify(results_cache)
        # 캐시가 한 번도 없으면 갱신 중이 아닐 때만 백그라운드 갱신 후 빈값 즉시 반환
        if not _results_refreshing:
            threading.Thread(target=_refresh_results_background, daemon=True).start()
        return jsonify({
            'results': [],
            'count': 0,
            'timestamp': datetime.now().isoformat(),
            'error': 'loading',
            'prediction_history': []
        }), 200
    except Exception as e:
        import traceback
        error_msg = str(e)[:200]
        print(f"[❌ 오류] 결과 로드 실패: {error_msg}")
        print(traceback.format_exc()[:500])
        return jsonify({
            'results': [],
            'count': 0,
            'timestamp': datetime.now().isoformat(),
            'error': error_msg,
            'prediction_history': []
        }), 200


@app.route('/api/calc-state', methods=['GET', 'POST'])
def api_calc_state():
    """GET: 계산기 상태 조회. session_id 없으면 새로 생성. POST: 계산기 상태 저장. running=true이고 started_at 없으면 서버가 started_at 설정."""
    try:
        server_time = int(time.time())
        if request.method == 'GET':
            session_id = request.args.get('session_id', '').strip() or None
            if not session_id:
                session_id = uuid.uuid4().hex
                save_calc_state(session_id, {})
            state = get_calc_state(session_id)
            if state is None:
                state = {}
            return jsonify({'session_id': session_id, 'server_time': server_time, 'calcs': state}), 200
        # POST
        data = request.get_json(force=True, silent=True) or {}
        session_id = (data.get('session_id') or '').strip()
        if not session_id:
            session_id = uuid.uuid4().hex
        calcs = data.get('calcs') or {}
        out = {}
        for cid in ('1', '2', '3'):
            c = calcs.get(cid) or {}
            if isinstance(c, dict):
                running = c.get('running', False)
                started_at = c.get('started_at') or 0
                if running and not started_at:
                    started_at = server_time
                out[cid] = {
                    'running': running,
                    'started_at': started_at,
                    'history': c.get('history') if isinstance(c.get('history'), list) else [],
                    'duration_limit': int(c.get('duration_limit') or 0),
                    'use_duration_limit': bool(c.get('use_duration_limit')),
                    'reverse': bool(c.get('reverse')),
                    'timer_completed': bool(c.get('timer_completed')),
                    'win_rate_reverse': bool(c.get('win_rate_reverse')),
                    'win_rate_threshold': max(0, min(100, int(c.get('win_rate_threshold') or 50))),
                    'martingale': bool(c.get('martingale')),
                    'martingale_type': str(c.get('martingale_type') or 'pyo'),
                    'target_enabled': bool(c.get('target_enabled')),
                    'target_amount': max(0, int(c.get('target_amount') or 0)),
                    'max_win_streak_ever': int(c.get('max_win_streak_ever') or 0),
                    'max_lose_streak_ever': int(c.get('max_lose_streak_ever') or 0),
                    'first_bet_round': max(0, int(c.get('first_bet_round') or 0))
                }
            else:
                out[cid] = {'running': False, 'started_at': 0, 'history': [], 'duration_limit': 0, 'use_duration_limit': False, 'reverse': False, 'timer_completed': False, 'win_rate_reverse': False, 'win_rate_threshold': 50, 'martingale': False, 'martingale_type': 'pyo', 'target_enabled': False, 'target_amount': 0, 'max_win_streak_ever': 0, 'max_lose_streak_ever': 0, 'first_bet_round': 0}
        c = calcs.get('defense') or {}
        if isinstance(c, dict):
            running = c.get('running', False)
            started_at = c.get('started_at') or 0
            if running and not started_at:
                started_at = server_time
            out['defense'] = {
                'running': running,
                'started_at': started_at,
                'history': c.get('history') if isinstance(c.get('history'), list) else [],
                'duration_limit': int(c.get('duration_limit') or 0),
                'use_duration_limit': bool(c.get('use_duration_limit')),
                'timer_completed': bool(c.get('timer_completed')),
                'linked_calc_id': int(c.get('linked_calc_id') or 1),
                'full_steps': int(c.get('full_steps') or 3),
                'reduce_from': int(c.get('reduce_from') or 4),
                'reduce_div': int(c.get('reduce_div') or 4),
                'stop_streak': int(c.get('stop_streak') or 0),
                'max_win_streak_ever': int(c.get('max_win_streak_ever') or 0),
                'max_lose_streak_ever': int(c.get('max_lose_streak_ever') or 0),
                'first_bet_round': max(0, int(c.get('first_bet_round') or 0))
            }
        else:
            out['defense'] = {'running': False, 'started_at': 0, 'history': [], 'duration_limit': 0, 'use_duration_limit': False, 'timer_completed': False, 'linked_calc_id': 1, 'full_steps': 3, 'reduce_from': 4, 'reduce_div': 4, 'stop_streak': 0, 'max_win_streak_ever': 0, 'max_lose_streak_ever': 0, 'first_bet_round': 0}
        save_calc_state(session_id, out)
        return jsonify({'session_id': session_id, 'server_time': server_time, 'calcs': out}), 200
    except Exception as e:
        return jsonify({'error': str(e)[:200], 'session_id': None, 'server_time': int(time.time()), 'calcs': {}}), 200


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
        ok = save_prediction_record(int(round_num), str(predicted), str(actual), probability=probability, pick_color=pick_color)
        return jsonify({'ok': ok}), 200
    except Exception as e:
        print(f"[❌ 오류] 예측 기록 API 실패: {str(e)[:200]}")
        return jsonify({'ok': False, 'error': str(e)[:200]}), 500


@app.route('/api/current-pick', methods=['GET', 'POST'])
def api_current_pick():
    """GET: 배팅 연동 현재 예측 픽 조회. POST: 프론트엔드가 픽 갱신 시 저장."""
    empty_pick = {'pick_color': None, 'round': None, 'probability': None, 'suggested_amount': None, 'updated_at': None}
    try:
        if not bet_int or not DB_AVAILABLE or not DATABASE_URL:
            return jsonify(empty_pick if request.method == 'GET' else {'ok': False}), 200
        if request.method == 'GET':
            conn = get_db_connection(statement_timeout_sec=5)
            if not conn:
                return jsonify(empty_pick), 200
            out = bet_int.get_current_pick(conn)
            conn.close()
            return jsonify(out if out else empty_pick), 200
        # POST: 테이블 없으면 생성 후 저장 (데이터 충분한데 픽 안 올 때 대비)
        data = request.get_json(force=True, silent=True) or {}
        pick_color = data.get('pickColor') or data.get('pick_color')
        round_num = data.get('round')
        probability = data.get('probability')
        suggested_amount = data.get('suggestedAmount') or data.get('suggested_amount')
        conn = get_db_connection(statement_timeout_sec=5)
        if not conn:
            return jsonify({'ok': False}), 200
        ensure_current_pick_table(conn)
        conn.commit()
        ok = bet_int.set_current_pick(conn, pick_color=pick_color, round_num=round_num, probability=probability, suggested_amount=suggested_amount)
        if ok:
            conn.commit()
            print(f"[배팅연동] 픽 저장: {pick_color} round {round_num}")
        conn.close()
        return jsonify({'ok': ok}), 200
    except Exception as e:
        print(f"[❌ 오류] current-pick 실패: {str(e)[:200]}")
        return jsonify(empty_pick if request.method == 'GET' else {'ok': False}), 200


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


@app.route('/api/current-status', methods=['GET'])
def get_current_status():
    """현재 게임 상태"""
    try:
        data = load_game_data()
        # 디버깅: 반환 데이터 확인
        red_count = len(data.get('currentBets', {}).get('red', []))
        black_count = len(data.get('currentBets', {}).get('black', []))
        print(f"[API 응답] RED: {red_count}명, BLACK: {black_count}명")
        print(f"[API 응답] 전체 데이터 구조: {list(data.keys())}")
        print(f"[API 응답] currentBets 키: {list(data.get('currentBets', {}).keys())}")
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
        results_cache = {
            'results': results_data,
            'count': len(results_data),
            'timestamp': datetime.now().isoformat()
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
            db_results = get_recent_results(hours=5)
        
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
