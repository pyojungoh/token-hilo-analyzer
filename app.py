"""
토큰하이로우 분석기 - Railway 서버
필요한 정보만 추출하여 새로 작성
"""

from flask import Flask, jsonify
from flask_cors import CORS
import requests
import os
from datetime import datetime
import time

app = Flask(__name__)
CORS(app)

# 환경 변수
BASE_URL = os.getenv('BASE_URL', 'http://tgame365.com')
DATA_PATH = '/frame/hilo'  # 데이터 파일 경로
TIMEOUT = int(os.getenv('TIMEOUT', '30'))
MAX_RETRIES = int(os.getenv('MAX_RETRIES', '3'))

# 캐시
game_data_cache = None
streaks_cache = None
last_update_time = 0
CACHE_TTL = 5000  # 5초

def fetch_with_retry(url, max_retries=MAX_RETRIES):
    """재시도 로직 포함 fetch"""
    for attempt in range(max_retries):
        try:
            response = requests.get(
                url,
                timeout=TIMEOUT,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Cache-Control': 'no-cache'
                }
            )
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            raise e
    return None

def load_game_data():
    """게임 데이터 로드 (current_status_frame.json)"""
    try:
        url = f"{BASE_URL}{DATA_PATH}/current_status_frame.json?t={int(time.time() * 1000)}"
        response = fetch_with_retry(url)
        
        if not response:
            raise Exception("데이터 로드 실패")
        
        data = response.json()
        
        return {
            'round': data.get('round', 0),
            'currentBets': {
                'red': data.get('red', []) if isinstance(data.get('red'), list) else [],
                'black': data.get('black', []) if isinstance(data.get('black'), list) else []
            },
            'timestamp': datetime.now().isoformat()
        }
    except Exception as e:
        print(f"게임 데이터 로드 오류: {e}")
        return None

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
        url = f"{BASE_URL}{DATA_PATH}/bet_result_log.csv?t={int(time.time() * 1000)}"
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

@app.route('/api/current-status', methods=['GET'])
def get_current_status():
    """현재 게임 상태"""
    global game_data_cache, last_update_time
    
    current_time = time.time() * 1000
    if game_data_cache and (current_time - last_update_time) < CACHE_TTL:
        return jsonify(game_data_cache)
    
    data = load_game_data()
    if data:
        game_data_cache = data
        last_update_time = current_time
        return jsonify(data)
    else:
        return jsonify({'error': '데이터 로드 실패'}), 500

@app.route('/api/streaks', methods=['GET'])
def get_streaks():
    """연승 데이터"""
    data = load_streaks_data()
    if data:
        return jsonify(data)
    else:
        return jsonify({'error': '연승 데이터 로드 실패'}), 500

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
    global game_data_cache, streaks_cache, last_update_time
    
    game_data = load_game_data()
    streaks_data = load_streaks_data()
    
    if game_data:
        game_data_cache = game_data
        last_update_time = time.time() * 1000
    
    if streaks_data:
        streaks_cache = streaks_data
    
    return jsonify({
        'success': True,
        'gameData': game_data is not None,
        'streaksData': streaks_data is not None,
        'timestamp': datetime.now().isoformat()
    })

@app.route('/health', methods=['GET'])
def health_check():
    """헬스 체크"""
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now().isoformat()
    })

@app.route('/', methods=['GET'])
def index():
    """루트"""
    return jsonify({
        'message': '토큰하이로우 분석기 API',
        'version': '1.0.0',
        'endpoints': {
            'GET /api/current-status': '현재 게임 상태',
            'GET /api/streaks': '연승 데이터',
            'GET /api/streaks/<user_id>': '특정 유저 연승',
            'POST /api/refresh': '데이터 갱신',
            'GET /health': '헬스 체크'
        }
    })

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
