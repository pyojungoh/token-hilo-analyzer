# -*- coding: utf-8 -*-
"""
3단계 모듈화: DB 관련 함수
get_db_connection, init_database, save_game_result, get_calc_state, save_calc_state
"""
import json
from config import DATABASE_URL

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    DB_AVAILABLE = True
    print("[✅] psycopg2 라이브러리 로드 성공")
except ImportError as e:
    psycopg2 = None
    RealDictCursor = None
    DB_AVAILABLE = False
    print(f"[❌ 경고] psycopg2가 설치되지 않았습니다: {e}")
    print("[❌ 경고] pip install psycopg2-binary로 설치하세요")


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

        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_game_id ON game_results(game_id)
        ''')
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_created_at ON game_results(created_at)
        ''')

        # color_matches 테이블 생성
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
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_color_matches_game_id ON color_matches(game_id)
        ''')
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_color_matches_compare_game_id ON color_matches(compare_game_id)
        ''')

        # prediction_history
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
        for col, typ in [('probability', 'REAL'), ('pick_color', 'VARCHAR(10)'), ('blended_win_rate', 'REAL'), ('rate_15', 'REAL'), ('rate_30', 'REAL'), ('rate_100', 'REAL'), ('prediction_details', 'JSONB')]:
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

        # calc_sessions
        cur.execute('''
            CREATE TABLE IF NOT EXISTS calc_sessions (
                session_id VARCHAR(64) PRIMARY KEY,
                state_json TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # round_predictions
        cur.execute('''
            CREATE TABLE IF NOT EXISTS round_predictions (
                round_num INTEGER PRIMARY KEY,
                predicted VARCHAR(10) NOT NULL,
                pick_color VARCHAR(10),
                probability REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # current_pick
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

        # shape_win_stats
        cur.execute('''
            CREATE TABLE IF NOT EXISTS shape_win_stats (
                signature TEXT PRIMARY KEY,
                next_jung_count INTEGER DEFAULT 0,
                next_kkeok_count INTEGER DEFAULT 0,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        ''')
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

        # chunk_profile_occurrences
        cur.execute('''
            CREATE TABLE IF NOT EXISTS chunk_profile_occurrences (
                id SERIAL PRIMARY KEY,
                profile_json TEXT NOT NULL,
                next_actual TEXT NOT NULL,
                round_num INTEGER NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        ''')
        try:
            cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'chunk_profile_occurrences' AND column_name = 'segment_type'")
            if cur.fetchone() is None:
                cur.execute("ALTER TABLE chunk_profile_occurrences ADD COLUMN segment_type VARCHAR(20) DEFAULT 'chunk'")
        except Exception:
            pass
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


# DB 없을 때 계산기 상태 in-memory 저장
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


def get_color_matches_batch(conn, pairs):
    """정/꺽 결과 일괄 조회. pairs: [(game_id, compare_game_id), ...]. 반환: {(gid, cgid): match_result}"""
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


def get_recent_results_raw(hours=24):
    """최근 N시간 game_results 조회만 수행. colorMatch 등 후처리는 호출자가 담당."""
    if not DB_AVAILABLE or not DATABASE_URL or not RealDictCursor:
        return []
    conn = get_db_connection(statement_timeout_sec=8)
    if not conn:
        return []
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
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
        cur.close()
        conn.close()
        return results
    except Exception as e:
        print(f"[❌ 오류] 게임 결과 조회 실패: {str(e)[:200]}")
        try:
            conn.close()
        except Exception:
            pass
        return []
