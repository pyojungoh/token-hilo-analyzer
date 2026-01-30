"""
토큰하이로우 분석기 - Railway 서버
필요한 정보만 추출하여 새로 작성
"""

from flask import Flask, jsonify, render_template_string
from flask_cors import CORS
import requests
import os
from datetime import datetime
import time
import json
import traceback
import threading
import re
try:
    import socketio
    SOCKETIO_AVAILABLE = True
    print("[✅] python-socketio 라이브러리 로드 성공")
except ImportError as e:
    SOCKETIO_AVAILABLE = False
    print(f"[❌ 경고] python-socketio가 설치되지 않았습니다: {e}")
    print("[❌ 경고] pip install python-socketio로 설치하세요")

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    DB_AVAILABLE = True
    print("[✅] psycopg2 라이브러리 로드 성공")
except ImportError as e:
    DB_AVAILABLE = False
    print(f"[❌ 경고] psycopg2가 설치되지 않았습니다: {e}")
    print("[❌ 경고] pip install psycopg2-binary로 설치하세요")

app = Flask(__name__)
CORS(app)

# 환경 변수 (init_socketio() 호출 전에 정의되어야 함)
BASE_URL = os.getenv('BASE_URL', 'http://tgame365.com')
# 기존 파일 예제를 보면 루트 경로에 파일이 있음
DATA_PATH = ''  # 데이터 파일 경로 (루트)
TIMEOUT = int(os.getenv('TIMEOUT', '10'))  # 타임아웃을 10초로 단축
MAX_RETRIES = int(os.getenv('MAX_RETRIES', '2'))  # 재시도 횟수 감소
SOCKETIO_URL = os.getenv('SOCKETIO_URL', 'https://game.cmx258.com:8080')  # Socket.IO 서버 URL (실제 서버)

# Socket.IO 초기화 플래그
socketio_initialized = False

# 데이터베이스 연결 및 초기화
def init_database():
    """데이터베이스 테이블 생성 및 초기화"""
    if not DB_AVAILABLE or not DATABASE_URL:
        print("[❌ 경고] 데이터베이스 연결 불가 (psycopg2 없음 또는 DATABASE_URL 미설정)")
        return False
    
    try:
        conn = psycopg2.connect(DATABASE_URL)
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
        
        conn.commit()
        cur.close()
        conn.close()
        print("[✅] 데이터베이스 테이블 초기화 완료")
        return True
    except Exception as e:
        print(f"[❌ 오류] 데이터베이스 초기화 실패: {str(e)[:200]}")
        return False

def get_db_connection():
    """데이터베이스 연결 반환"""
    if not DB_AVAILABLE or not DATABASE_URL:
        return None
    try:
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        print(f"[❌ 오류] 데이터베이스 연결 실패: {str(e)[:200]}")
        return None

def save_game_result(game_data):
    """게임 결과를 데이터베이스에 저장 (중복 체크)"""
    if not DB_AVAILABLE or not DATABASE_URL:
        return False
    
    conn = get_db_connection()
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

def get_recent_results(hours=5):
    """최근 N시간 데이터 조회"""
    if not DB_AVAILABLE or not DATABASE_URL:
        return []
    
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # 최근 5시간 데이터 조회 (최신순)
        cur.execute('''
            SELECT game_id as "gameID", result, hi, lo, red, black, jqka, joker, 
                   hash_value as hash, salt_value as salt
            FROM game_results
            WHERE created_at >= NOW() - INTERVAL '%s hours'
            ORDER BY created_at DESC
        ''', (hours,))
        
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
        except:
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
        
        # 5시간 이전 데이터 삭제
        cur.execute('''
            DELETE FROM game_results
            WHERE created_at < NOW() - INTERVAL '%s hours'
        ''', (hours,))
        
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

# init_socketio() 함수는 start_socketio_client() 함수 정의 후에 정의됨 (아래 참조)

# 캐시
game_data_cache = None
streaks_cache = None
results_cache = None
last_update_time = 0
CACHE_TTL = 1000  # 1초 (10초 게임에 맞춰 빠른 업데이트)

# Socket.IO 관련
socketio_client = None
socketio_thread = None
socketio_connected = False
current_status_data = {
    'round': 0,
    'elapsed': 0,
    'currentBets': {
        'red': [],
        'black': []
    },
    'timestamp': datetime.now().isoformat()
}

def fetch_with_retry(url, max_retries=MAX_RETRIES, silent=False):
    """재시도 로직 포함 fetch (기존 파일과 동일한 방식)"""
    for attempt in range(max_retries):
        try:
            # 기존 railway_server_example.py와 동일한 헤더 사용
            # 하지만 더 완전한 브라우저 헤더 추가
            response = requests.get(
                url,
                timeout=TIMEOUT,
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

# Socket.IO 이벤트 핸들러
def on_socketio_connect():
    """Socket.IO 연결 성공"""
    global socketio_connected
    socketio_connected = True
    print("🔵 [Socket.IO] ✅ 연결됨!")

def on_socketio_disconnect():
    """Socket.IO 연결 종료"""
    global socketio_connected
    socketio_connected = False
    print("🔵 [Socket.IO] ❌ 연결 종료됨")

def on_socketio_total(data):
    """total 이벤트 수신 (베팅 데이터) - 배열의 첫 번째 요소 사용"""
    global current_status_data
    
    try:
        # 데이터가 배열로 전달되므로 첫 번째 요소 추출
        if isinstance(data, list) and len(data) > 0:
            data = data[0]
        
        if isinstance(data, dict):
            # 베팅 데이터 업데이트
            red_bets = data.get('red', [])
            black_bets = data.get('black', [])
            
            if not isinstance(red_bets, list):
                red_bets = []
            if not isinstance(black_bets, list):
                black_bets = []
            
            current_status_data['currentBets'] = {
                'red': red_bets,
                'black': black_bets
            }
            current_status_data['timestamp'] = datetime.now().isoformat()
            
            print(f"🔵 [Socket.IO total] RED {len(red_bets)}명, BLACK {len(black_bets)}명")
        else:
            print(f"[Socket.IO] total 이벤트 데이터 형식 오류: {type(data)}")
    except Exception as e:
        print(f"[Socket.IO total 이벤트 처리 오류] {str(e)[:200]}")

def on_socketio_status(data):
    """status 이벤트 수신 (경기 상태) - 배열의 첫 번째 요소 사용"""
    global current_status_data
    
    try:
        # 데이터가 배열로 전달되므로 첫 번째 요소 추출
        if isinstance(data, list) and len(data) > 0:
            data = data[0]
        
        if isinstance(data, dict):
            if data.get("round") is not None:
                current_status_data['round'] = data.get("round")
            current_status_data['elapsed'] = data.get('elapsed', 0)
            current_status_data['timestamp'] = datetime.now().isoformat()
            
            status_type = data.get('status', 'unknown')
            print(f"[Socket.IO] status 이벤트: {status_type}, round={data.get('round')}, elapsed={data.get('elapsed')}")
        else:
            print(f"[Socket.IO] status 이벤트 데이터 형식 오류: {type(data)}")
    except Exception as e:
        print(f"[Socket.IO status 이벤트 처리 오류] {str(e)[:200]}")

def on_socketio_betting(data):
    """betting 이벤트 수신 (베팅 정보) - 배열의 첫 번째 요소 사용"""
    global current_status_data
    
    try:
        # 데이터가 배열로 전달되므로 첫 번째 요소 추출
        if isinstance(data, list) and len(data) > 0:
            data = data[0]
        
        if isinstance(data, dict):
            # betting 이벤트도 베팅 데이터를 포함할 수 있음
            red_bets = data.get('red', [])
            black_bets = data.get('black', [])
            
            if isinstance(red_bets, list) and isinstance(black_bets, list):
                current_status_data['currentBets'] = {
                    'red': red_bets,
                    'black': black_bets
                }
                current_status_data['timestamp'] = datetime.now().isoformat()
                print(f"🔵 [Socket.IO betting] RED {len(red_bets)}명, BLACK {len(black_bets)}명")
    except Exception as e:
        print(f"[Socket.IO betting 이벤트 처리 오류] {str(e)[:200]}")

def on_socketio_result(data):
    """result 이벤트 수신 (경기 결과) - 배열의 첫 번째 요소 사용"""
    try:
        # 데이터가 배열로 전달되므로 첫 번째 요소 추출
        if isinstance(data, list) and len(data) > 0:
            data = data[0]
        
        if isinstance(data, dict):
            print(f"[Socket.IO] result 이벤트: round={data.get('round')}, result={data.get('result')}, number={data.get('number')}")
        else:
            print(f"[Socket.IO] result 이벤트 데이터 형식: {type(data)}")
    except Exception as e:
        print(f"[Socket.IO result 이벤트 처리 오류] {str(e)[:200]}")

def start_socketio_client():
    """Socket.IO 클라이언트 시작 (별도 스레드에서 실행)"""
    global socketio_client, socketio_thread, socketio_connected
    
    if not SOCKETIO_AVAILABLE:
        print("[경고] python-socketio가 설치되지 않아 Socket.IO 연결을 사용할 수 없습니다")
        return
    
    if socketio_client and socketio_connected:
        print("[경고] Socket.IO 클라이언트가 이미 실행 중입니다")
        return
    
    def socketio_worker():
        global socketio_client, socketio_connected
        
        while True:
            try:
                print(f"🔵 [Socket.IO] 연결 시도: {SOCKETIO_URL}")
                
                # 기존 파일 방식: engineio.Client를 먼저 생성하고 ssl_verify=False 설정
                import engineio
                eio_client = engineio.Client(ssl_verify=False, logger=False)
                
                # Socket.IO 클라이언트 생성 (engineio_client 전달)
                socketio_client = socketio.Client(
                    engineio_logger=False,
                    logger=False,
                    engineio_client=eio_client
                )
                
                # 이벤트 핸들러 등록 (실제 이벤트 이름 사용)
                socketio_client.on('connect', on_socketio_connect)
                socketio_client.on('disconnect', on_socketio_disconnect)
                socketio_client.on('total', on_socketio_total)
                socketio_client.on('status', on_socketio_status)
                socketio_client.on('betting', on_socketio_betting)
                socketio_client.on('result', on_socketio_result)
                
                # 연결 시도 (기존 파일 방식 사용)
                print(f"🔵 [연결 정보] URL: {SOCKETIO_URL}")
                
                # 기존 파일과 동일한 방식으로 연결
                socketio_client.connect(
                    SOCKETIO_URL,
                    transports=['polling', 'websocket'],
                    socketio_path='/socket.io/',
                    headers={
                        "Origin": "http://tgame365.com",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
                    }
                )
                
                print(f"🔵 [연결 성공] connect() 메서드 완료")
                
                # 연결 유지
                socketio_client.wait()
                
            except Exception as e:
                error_msg = str(e)
                print(f"🔵 [Socket.IO 연결 오류] {error_msg[:300]}")
                print(f"🔵 [오류 상세] {type(e).__name__}: {error_msg}")
                import traceback
                print(f"🔵 [오류 스택] {traceback.format_exc()[:500]}")
                socketio_connected = False
                if socketio_client:
                    try:
                        socketio_client.disconnect()
                    except:
                        pass
                time.sleep(5)  # 5초 후 재연결 시도
    
    socketio_thread = threading.Thread(target=socketio_worker, daemon=True)
    socketio_thread.start()
    print("🔵 [✅ Socket.IO] 클라이언트 스레드 시작됨")

# Socket.IO 초기화 함수 (start_socketio_client() 함수 정의 후에 정의)
def init_socketio():
    """Socket.IO 연결 초기화"""
    print("\n" + "=" * 50)
    print("🔵 [SOCKET.IO 초기화 시작]")
    print("=" * 50)
    print(f"🔵 SOCKETIO_URL: {SOCKETIO_URL}")
    print(f"🔵 BASE_URL: {BASE_URL}")
    print(f"🔵 python-socketio 사용 가능: {SOCKETIO_AVAILABLE}")

    # Socket.IO 클라이언트 시작
    if SOCKETIO_AVAILABLE:
        if SOCKETIO_URL:
            print(f"🔵 [✅] Socket.IO 연결 시작: {SOCKETIO_URL}")
            start_socketio_client()
        else:
            print("🔵 [❌] SOCKETIO_URL 환경 변수가 설정되지 않았습니다")
            print("🔵 [❌] Railway 환경 변수에 SOCKETIO_URL을 설정하세요")
            print("🔵 [❌] 예: SOCKETIO_URL=https://game.cmx258.com:8080")
    else:
        print("🔵 [❌] python-socketio가 설치되지 않아 Socket.IO 연결을 사용하지 않습니다")
        print("🔵 [❌] pip install python-socketio로 설치하세요")
    print("=" * 50 + "\n")

# Socket.IO 초기화를 지연 실행 (서버 시작 후 별도 스레드에서 실행)
def delayed_socketio_init():
    """Socket.IO 초기화를 지연 실행 (서버 시작을 막지 않음)"""
    global socketio_initialized
    if socketio_initialized:
        return
    
    # 서버가 완전히 시작될 때까지 약간 대기
    import time
    time.sleep(2)
    
    try:
        init_socketio()
        socketio_initialized = True
    except Exception as e:
        print(f"🔵 [❌ 오류] Socket.IO 초기화 실패: {e}")
        try:
            import traceback
            traceback.print_exc()
        except:
            pass

# 별도 스레드에서 Socket.IO 초기화 시작 (서버 시작을 막지 않음)
init_thread = threading.Thread(target=delayed_socketio_init, daemon=True)
init_thread.start()

def load_game_data():
    """게임 데이터 로드 - Socket.IO 데이터 우선 사용"""
    global current_status_data
    
    # Socket.IO가 연결되어 있으면 Socket.IO 데이터 사용 (HTTP 요청 불필요)
    if socketio_connected:
        # Socket.IO 데이터가 있으면 사용
        if current_status_data.get('currentBets', {}).get('red') is not None:
            return current_status_data
        # Socket.IO는 연결되었지만 아직 데이터가 없으면 기본값 반환
        return current_status_data
    
    # Socket.IO가 연결되지 않았으면 빈 데이터 반환 (HTTP 요청 제거 - 공개 URL 없음)
    # HTTP 요청은 실패하므로 불필요한 로그 스팸 방지
    return {
        'round': 0,
        'elapsed': 0,
        'currentBets': {
            'red': [],
            'black': []
        },
        'timestamp': datetime.now().isoformat()
    }

def load_results_data():
    """경기 결과 데이터 로드 (result.json) - 실제 URL 사용"""
    # 실제 확인된 URL 경로
    possible_paths = [
        f"{BASE_URL}/frame/hilo/result.json",  # 실제 확인된 경로
        f"{BASE_URL}/result.json",
        f"{BASE_URL}/hilo/result.json",
        f"{BASE_URL}/frame/result.json",
    ]
    
    for url_path in possible_paths:
        try:
            url = f"{url_path}?t={int(time.time() * 1000)}"
            print(f"[결과 데이터 요청 시도] {url}")
            response = fetch_with_retry(url, silent=True)
            
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
    """연승 데이터 로드"""
    try:
        url = f"{BASE_URL}/bet_result_log.csv?t={int(time.time() * 1000)}"
        response = fetch_with_retry(url)
        
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
            gap: clamp(5px, 1.5vw, 12px);
            padding: 15px 0;
            flex-wrap: nowrap;
            width: 100%;
        }
        .card-wrapper {
            display: flex;
            flex-direction: column;
            align-items: center;
            flex: 0 0 calc((100% - (14 * clamp(5px, 1.5vw, 12px))) / 15);
            min-width: 0;
        }
        .card-wrapper .card {
            width: 100% !important;
            aspect-ratio: 2 / 3 !important;
        }
        .card {
            width: 100%;
            aspect-ratio: 2 / 3;
            background: #fff;
            border: 3px solid #000;
            border-radius: 10px;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            padding: clamp(5px, 1vw, 10px);
            box-shadow: 0 4px 12px rgba(0,0,0,0.4);
        }
        .card.red {
            color: #d32f2f;
        }
        .card.black {
            color: #000;
        }
        .card-suit-icon {
            font-size: clamp(30px, 6vw, 60px);
            line-height: 1;
            margin-bottom: 5px;
        }
        .card-value {
            font-size: clamp(24px, 5vw, 48px);
            font-weight: bold;
            text-align: center;
            line-height: 1;
        }
        .card-category {
            margin-top: 5px;
            font-size: clamp(10px, 2vw, 16px);
            font-weight: bold;
            padding: 4px 8px;
            border-radius: 5px;
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
            margin-top: 5px;
            font-size: clamp(10px, 2vw, 16px);
            font-weight: bold;
            padding: 4px 8px;
            border-radius: 5px;
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
            } else {
                // 디버깅: 왜 표시되지 않는지 확인
                console.log(`카드 ${index + 1} 정/꺽 미표시: colorMatchResult =`, colorMatchResult, typeof colorMatchResult);
            }
            
            return cardWrapper;
        }
        
        // 각 카드의 색상 비교 결과 저장 (gameID를 키로, 비교 대상 gameID도 함께 저장)
        const colorMatchCache = {};
        // 최근 30개 결과 저장 (비교를 위해)
        let allResults = [];
        let isLoadingResults = false;  // 중복 요청 방지
        
        async function loadResults() {
            // 이미 로딩 중이면 스킵
            if (isLoadingResults) {
                return;
            }
            
            try {
                isLoadingResults = true;
                
                // 타임아웃 설정 (5초로 단축 - 빠른 실패)
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 5000);
                
                const response = await fetch('/api/results?t=' + Date.now(), {
                    signal: controller.signal,
                    cache: 'no-cache'
                });
                
                clearTimeout(timeoutId);
                
                if (!response.ok) {
                    console.warn('결과 로드 실패:', response.status, response.statusText);
                    const statusElement = document.getElementById('status');
                    if (statusElement) {
                        statusElement.textContent = `결과 로드 실패 (${response.status})`;
                    }
                    return;
                }
                
                const data = await response.json();
                
                if (data.error) {
                    const statusElement = document.getElementById('status');
                    if (statusElement) {
                        statusElement.textContent = '오류: ' + data.error;
                    }
                    return;
                }
                
                const newResults = data.results || [];
                const statusElement = document.getElementById('status');
                const cardsDiv = document.getElementById('cards');
                
                if (!statusElement || !cardsDiv) {
                    console.error('DOM 요소를 찾을 수 없습니다');
                    return;
                }
                
                // 새로운 결과를 기존 결과와 병합 (중복 제거, 최신 30개 유지)
                if (newResults.length > 0) {
                    // 새로운 결과의 gameID들
                    const newGameIDs = new Set(newResults.map(r => r.gameID).filter(id => id));
                    
                    // 기존 결과에서 새로운 결과에 없는 것만 유지
                    const oldResults = allResults.filter(r => !newGameIDs.has(r.gameID));
                    
                    // 새로운 결과 + 기존 결과 (최신 30개만)
                    allResults = [...newResults, ...oldResults].slice(0, 30);
                } else {
                    // 새로운 결과가 없으면 기존 결과 유지
                    if (allResults.length === 0) {
                        allResults = newResults;
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
                
                console.log('=== 색상 비교 시작 ===');
                console.log('전체 결과 개수:', results.length);
                console.log('표시할 결과 개수:', displayResults.length);
                
                // 전체 results 배열이 16개 이상이어야 비교 가능
                if (results.length < 16) {
                    console.log(`경고: 전체 결과가 ${results.length}개밖에 없어 비교 불가능 (최소 16개 필요)`);
                    // 모든 카드에 null 할당
                    for (let i = 0; i < displayResults.length; i++) {
                        colorMatchResults[i] = null;
                    }
                } else {
                    for (let i = 0; i < displayResults.length; i++) {
                        const currentResult = displayResults[i];
                        const currentGameID = currentResult?.gameID || '';
                        const compareIndex = i + 15;  // 1번째는 16번째와, 2번째는 17번째와 비교
                        
                        // 조커 카드는 색상 비교 불가
                        if (currentResult.joker) {
                            colorMatchResults[i] = null;
                            console.log(`카드 ${i + 1}: 조커 카드 - 비교 불가`);
                            continue;
                        }
                        
                        if (!currentGameID) {
                            colorMatchResults[i] = null;
                            console.log(`카드 ${i + 1}: gameID 없음`);
                            continue;
                        }
                        
                        // 16번째 이후 카드가 있어야 비교 가능
                        if (results.length <= compareIndex) {
                            colorMatchResults[i] = null;
                            console.log(`카드 ${i + 1}: 비교 대상 없음 (전체 ${results.length}개, 필요 ${compareIndex + 1}개)`);
                            continue;
                        }
                        
                        // 비교 대상도 조커가 아닌지 확인
                        if (results[compareIndex]?.joker) {
                            colorMatchResults[i] = null;
                            console.log(`카드 ${i + 1}: 비교 대상이 조커`);
                            continue;
                        }
                        
                        // 캐시 키 생성
                        const compareGameID = results[compareIndex]?.gameID || '';
                        const cacheKey = `${currentGameID}_${compareGameID}`;
                        
                        // 캐시에 이미 있는지 확인
                        if (colorMatchCache[cacheKey] !== undefined) {
                            const cachedResult = colorMatchCache[cacheKey];
                            colorMatchResults[i] = cachedResult === true;  // 명확히 boolean으로 변환
                            console.log(`카드 ${i + 1} (${currentGameID}): 캐시에서 가져옴 - ${cachedResult ? '정' : '꺽'}`);
                        } else {
                            // 새로운 비교 결과 계산
                            const currentCard = parseCardValue(currentResult.result || '');
                            const compareCard = parseCardValue(results[compareIndex].result || '');
                            const matchResult = (currentCard.isRed === compareCard.isRed);
                            colorMatchCache[cacheKey] = matchResult;
                            colorMatchResults[i] = matchResult === true;  // 명확히 boolean으로 변환
                            console.log(`카드 ${i + 1} (${currentGameID}): 새로 계산 - 현재(${currentCard.isRed ? '빨강' : '검정'}) vs 비교(${compareCard.isRed ? '빨강' : '검정'}) = ${matchResult ? '정' : '꺽'}`);
                        }
                    }
                }
                
                console.log('=== 색상 비교 완료 ===');
                console.log('결과 배열:', colorMatchResults);
                console.log('결과 타입 확인:', colorMatchResults.map((r, idx) => `${idx + 1}: ${r} (${typeof r})`));
                
                // 오래된 캐시 정리 (현재 표시되지 않는 카드 제거)
                const currentGameIDs = new Set(displayResults.map(r => r.gameID).filter(id => id));
                for (const key in colorMatchCache) {
                    const gameID = key.split('_')[0];
                    if (!currentGameIDs.has(gameID)) {
                        delete colorMatchCache[key];
                    }
                }
                
                // 헤더에 기준 색상 표시 (15번째 카드)
                if (displayResults.length >= 15) {
                    const card15 = parseCardValue(displayResults[14].result || '');
                    const referenceColorElement = document.getElementById('reference-color');
                    if (referenceColorElement) {
                        const colorText = card15.isRed ? '🔴 빨간색' : '⚫ 검은색';
                        referenceColorElement.textContent = `기준: ${colorText}`;
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
                
                displayResults.forEach((result, index) => {
                    try {
                        // 서버에서 받은 colorMatch 값 우선 사용, 없으면 클라이언트 계산 결과 사용
                        let matchResult = result.colorMatch;
                        if (matchResult === undefined || matchResult === null) {
                            matchResult = colorMatchResults[index];
                        }
                        console.log(`카드 ${index + 1} (${result.gameID}) 생성: matchResult =`, matchResult, typeof matchResult, 'isBoolean:', typeof matchResult === 'boolean');
                        const card = createCard(result, index, matchResult);
                        cardsDiv.appendChild(card);
                    } catch (error) {
                        console.error('카드 생성 오류:', error, result);
                    }
                });
                
                // 헤더 정보 업데이트
                if (displayResults.length > 0) {
                    const latest = displayResults[0];
                    const gameID = latest.gameID || '';
                    const prevRoundElement = document.getElementById('prev-round');
                    if (prevRoundElement) {
                        prevRoundElement.textContent = `이전회차: ${gameID}`;
                    }
                }
            } catch (error) {
                // AbortError는 조용히 처리 (타임아웃은 정상적인 상황)
                if (error.name === 'AbortError') {
                    // 타임아웃은 조용히 처리, 기존 결과 유지
                    return;
                }
                
                // Failed to fetch는 네트워크 오류이므로 조용히 처리 (기존 결과 유지)
                if (error.message === 'Failed to fetch' || error.name === 'TypeError') {
                    // 네트워크 오류는 조용히 처리, 기존 결과 유지
                    return;
                }
                
                // 기타 오류만 로그
                console.error('loadResults 오류:', error);
                const statusElement = document.getElementById('status');
                if (statusElement) {
                    statusElement.textContent = '결과 로드 오류: ' + error.message;
                }
            } finally {
                isLoadingResults = false;  // 로딩 완료
            }
        }
        
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
                
                // 0.5초마다 서버에서 데이터 가져오기 (10초 게임에 맞춰 빠른 업데이트)
                if (now - timerData.lastFetch > 500) {
                    try {
                    // 타임아웃 설정 (5초로 단축 - 빠른 실패)
                    const controller = new AbortController();
                    const timeoutId = setTimeout(() => controller.abort(), 5000);
                    
                    const response = await fetch('/api/current-status?t=' + now, {
                        signal: controller.signal,
                        cache: 'no-cache'
                    });
                    
                    clearTimeout(timeoutId);
                    
                    if (!response.ok) {
                        throw new Error('Network error: ' + response.status);
                    }
                    const data = await response.json();
                        
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
                                // 즉시 결과 로드 (10초 게임에 맞춰 빠른 반응)
                                setTimeout(() => {
                                    loadResults();
                                    lastResultsUpdate = Date.now();
                                }, 200);
                            }
                            // updateBettingInfo는 별도로 실행하므로 여기서 제거
                        }
                    } catch (error) {
                        // 네트워크 오류는 조용히 처리 (클라이언트 측 계산 계속)
                        // AbortError, Failed to fetch 등은 조용히 처리
                    }
                }
                
                // 클라이언트 측에서 시간 계산 (서버 elapsed + 경과 시간)
                const timeDiff = (now - timerData.serverTime) / 1000;
                const currentElapsed = Math.max(0, timerData.elapsed + timeDiff);
                const remaining = Math.max(0, 10 - currentElapsed);
                
                // 항상 시간 표시 (실시간 카운팅)
                timeElement.textContent = `남은 시간: ${remaining.toFixed(2)} 초`;
                
                // 타이머 색상
                timeElement.className = 'remaining-time';
                if (remaining <= 1) {
                    timeElement.classList.add('danger');
                } else if (remaining <= 3) {
                    timeElement.classList.add('warning');
                }
                
                // 타이머가 거의 0이 되면 경기 결과 새로고침 (라운드 종료 직전, 10초 게임에 맞춰 빠른 반응)
                if (remaining <= 0.5 && now - lastResultsUpdate > 200) {
                    loadResults();
                    lastResultsUpdate = now;
                }
                
                // 타이머가 0이 되면 즉시 결과 새로고침 (10초 게임에 맞춰 빠른 반응)
                if (remaining <= 0 && now - lastResultsUpdate > 100) {
                    setTimeout(() => {
                        loadResults();
                        lastResultsUpdate = Date.now();
                    }, 100);
                }
            } catch (error) {
                console.error('타이머 업데이트 오류:', error);
                const timeElement = document.getElementById('remaining-time');
                if (timeElement) {
                    timeElement.textContent = '남은 시간: -- 초';
                }
            }
        }
        
        // 초기 로드 (에러 발생 시에도 계속 시도)
        async function initialLoad() {
            try {
                await loadResults().catch(e => console.warn('초기 결과 로드 실패:', e));
            } catch (e) {
                console.warn('초기 로드 오류:', e);
            }
            updateTimer();
        }
        
        initialLoad();
        
        // 1초마다 결과 새로고침 (10초 게임에 맞춰 빠른 업데이트)
        setInterval(() => {
            if (Date.now() - lastResultsUpdate > 1000) {
                loadResults().catch(e => console.warn('결과 새로고침 실패:', e));
                lastResultsUpdate = Date.now();
            }
        }, 1000);
        
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

@app.route('/api/results', methods=['GET'])
def get_results():
    """경기 결과 API - 데이터베이스에서 최근 5시간 데이터 조회"""
    try:
        global results_cache, last_update_time
        
        current_time = time.time() * 1000
        
        # 데이터베이스가 있으면 DB에서 조회, 없으면 기존 방식 사용
        if DB_AVAILABLE and DATABASE_URL:
            # 캐시 사용 (1초)
            if results_cache and (current_time - last_update_time) < CACHE_TTL:
                return jsonify(results_cache)
            
            # 데이터베이스에서 최근 5시간 데이터 조회
            results = get_recent_results(hours=5)
            
            # 최신 데이터도 가져와서 저장 (백그라운드)
            try:
                latest_results = load_results_data()
                if latest_results:
                    saved_count = 0
                    for game_data in latest_results:
                        if save_game_result(game_data):
                            saved_count += 1
                    if saved_count > 0:
                        print(f"[💾] 최신 데이터 {saved_count}개 저장 완료")
            except Exception as e:
                print(f"[경고] 최신 데이터 저장 실패: {str(e)[:100]}")
            
            results_cache = {
                'results': results,
                'count': len(results),
                'timestamp': datetime.now().isoformat(),
                'source': 'database'
            }
            last_update_time = current_time
            return jsonify(results_cache)
        else:
            # 데이터베이스가 없으면 기존 방식 (result.json에서 가져오기)
            if results_cache and (current_time - last_update_time) < CACHE_TTL:
                return jsonify(results_cache)
            
            results = load_results_data()
            results_cache = {
                'results': results,
                'count': len(results),
                'timestamp': datetime.now().isoformat(),
                'source': 'json'
            }
            last_update_time = current_time
            return jsonify(results_cache)
    except Exception as e:
        # 에러 발생 시 빈 결과 반환 (서버 크래시 방지)
        print(f"결과 로드 오류: {str(e)[:200]}")
        return jsonify({
            'results': [],
            'count': 0,
            'timestamp': datetime.now().isoformat()
        }), 200

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
        # 항상 데이터 반환 (기본값 포함)
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
            'timestamp': datetime.now().isoformat()
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
    """데이터 갱신"""
    global game_data_cache, streaks_cache, results_cache, last_update_time
    
    game_data = load_game_data()
    streaks_data = load_streaks_data()
    results_data = load_results_data()
    
    if game_data:
        game_data_cache = game_data
    if streaks_data:
        streaks_cache = streaks_data
    if results_data:
        results_cache = {
            'results': results_data,
            'count': len(results_data),
            'timestamp': datetime.now().isoformat()
        }
    
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
    """루트 - 빠른 헬스체크용 (외부 API 호출 없음)"""
    # Railway 헬스체크를 위해 즉시 응답
    return jsonify({
        'status': 'ok',
        'message': '토큰하이로우 분석기 API',
        'version': '1.0.0'
    }), 200

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

@app.route('/favicon.ico', methods=['GET'])
def favicon():
    """favicon 404 에러 방지"""
    return '', 204  # No Content

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    print(f"[✅ 정보] Flask 서버 시작: http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port, debug=False)
