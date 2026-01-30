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
results_cache = None
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

def load_results_data():
    """경기 결과 데이터 로드 (result.json)"""
    try:
        url = f"{BASE_URL}{DATA_PATH}/result.json?t={int(time.time() * 1000)}"
        response = fetch_with_retry(url)
        
        if not response:
            raise Exception("결과 데이터 로드 실패")
        
        data = response.json()
        
        # 결과 파싱
        results = []
        for game in data:
            try:
                game_id = game.get('gameID', '')
                result = game.get('result', '')
                json_data = json.loads(game.get('json', '{}'))
                
                results.append({
                    'gameID': game_id,
                    'result': result,
                    'hi': json_data.get('hi', ''),
                    'lo': json_data.get('lo', ''),
                    'red': json_data.get('red', ''),
                    'black': json_data.get('black', ''),
                    'jqka': json_data.get('jqka', ''),
                    'joker': json_data.get('joker', '')
                })
            except:
                continue
        
        return results
    except Exception as e:
        print(f"결과 데이터 로드 오류: {e}")
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
        }
        .header-info div {
            margin: 3px 0;
        }
        .cards-container {
            display: flex;
            overflow-x: auto;
            gap: clamp(8px, 2vw, 15px);
            padding: 15px 0;
            -webkit-overflow-scrolling: touch;
        }
        .cards-container::-webkit-scrollbar {
            height: 6px;
        }
        .cards-container::-webkit-scrollbar-track {
            background: rgba(255,255,255,0.1);
            border-radius: 3px;
        }
        .cards-container::-webkit-scrollbar-thumb {
            background: rgba(255,255,255,0.3);
            border-radius: 3px;
        }
        .card {
            position: relative;
            width: clamp(70px, 12vw, 120px);
            height: clamp(100px, 18vw, 180px);
            background: #fff;
            border: 3px solid #000;
            border-radius: 10px;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            padding: clamp(8px, 1.5vw, 15px);
            flex-shrink: 0;
            box-shadow: 0 4px 12px rgba(0,0,0,0.4);
        }
        .card.red {
            color: #d32f2f;
        }
        .card.black {
            color: #000;
        }
        .card-suit-icon {
            font-size: clamp(40px, 8vw, 80px);
            line-height: 1;
            margin-bottom: 5px;
        }
        .card-value {
            font-size: clamp(32px, 6vw, 64px);
            font-weight: bold;
            text-align: center;
            line-height: 1;
            margin: 5px 0;
        }
        .card-category {
            position: absolute;
            bottom: 5px;
            left: 50%;
            transform: translateX(-50%);
            font-size: clamp(14px, 2.5vw, 20px);
            font-weight: bold;
            padding: 3px 8px;
            border-radius: 5px;
            white-space: nowrap;
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
            background: #9c27b0;
            color: #fff;
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
        .status {
            text-align: center;
            margin-top: 15px;
            color: #aaa;
            font-size: clamp(0.8em, 2vw, 0.9em);
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header-info">
            <div id="prev-round">이전회차: --</div>
            <div id="hash">Hash: --</div>
            <div id="remaining-time">남은 시간: -- 초</div>
        </div>
        <div class="cards-container" id="cards"></div>
        <div class="status" id="status">로딩 중...</div>
    </div>
    <script>
        function parseCardValue(value) {
            if (!value) return { number: '', suit: '♥', isRed: true };
            
            // 문양 추출
            const suits = {
                '♥': { icon: '♥', isRed: true },
                '♦': { icon: '♦', isRed: true },
                '♠': { icon: '♠', isRed: false },
                '♣': { icon: '♣', isRed: false }
            };
            
            // result 값에서 문양 찾기
            let suit = '♥';
            let number = value;
            let isRed = true;
            
            for (const [suitChar, suitInfo] of Object.entries(suits)) {
                if (value.includes(suitChar)) {
                    suit = suitChar;
                    number = value.replace(suitChar, '').trim();
                    isRed = suitInfo.isRed;
                    break;
                }
            }
            
            // 문양이 없으면 기본값
            if (number === value) {
                suit = '♥';
                isRed = true;
            }
            
            return {
                number: number,
                suit: suits[suit].icon,
                isRed: isRed
            };
        }
        
        function getCategory(result) {
            if (result.joker) return { text: 'JOKER', class: 'joker' };
            if (result.hi && result.lo) return { text: '비김', class: 'draw' };
            if (result.hi) return { text: 'HI ↑', class: 'hi' };
            if (result.lo) return { text: 'LO ↓', class: 'lo' };
            if (result.red && !result.black) return { text: 'RED', class: 'red-only' };
            if (result.black && !result.red) return { text: 'BLACK', class: 'black-only' };
            return null;
        }
        
        function createCard(result, index) {
            const card = document.createElement('div');
            const cardInfo = parseCardValue(result.result || '');
            const category = getCategory(result);
            
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
            
            // 카테고리 표시 (하단)
            if (category) {
                const categoryDiv = document.createElement('div');
                categoryDiv.className = 'card-category ' + category.class;
                categoryDiv.textContent = category.text;
                card.appendChild(categoryDiv);
            }
            
            return card;
        }
        
        async function loadResults() {
            try {
                const response = await fetch('/api/results');
                const data = await response.json();
                
                if (data.error) {
                    document.getElementById('status').textContent = '오류: ' + data.error;
                    return;
                }
                
                const results = data.results || [];
                document.getElementById('status').textContent = `총 ${results.length}개 경기 결과`;
                
                // 최신 결과가 왼쪽에 오도록 (원본 데이터가 최신이 앞에 있음)
                // 최신 50개만 표시
                const displayResults = results.slice(0, 50);
                
                const cardsDiv = document.getElementById('cards');
                cardsDiv.innerHTML = '';
                
                displayResults.forEach((result, index) => {
                    const card = createCard(result, index);
                    cardsDiv.appendChild(card);
                });
                
                // 헤더 정보 업데이트
                if (displayResults.length > 0) {
                    const latest = displayResults[0];
                    document.getElementById('prev-round').textContent = `이전회차: ${latest.gameID || '--'}`;
                    document.getElementById('hash').textContent = `Hash: ${latest.gameID ? latest.gameID.slice(-8) : '--'}`;
                }
            } catch (error) {
                document.getElementById('status').textContent = '오류: ' + error.message;
            }
        }
        
        // 초기 로드
        loadResults();
        
        // 5초마다 자동 새로고침
        setInterval(loadResults, 5000);
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
    """경기 결과 API"""
    global results_cache, last_update_time
    
    current_time = time.time() * 1000
    if results_cache and (current_time - last_update_time) < CACHE_TTL:
        return jsonify(results_cache)
    
    results = load_results_data()
    if results:
        results_cache = {
            'results': results,
            'count': len(results),
            'timestamp': datetime.now().isoformat()
        }
        last_update_time = current_time
        return jsonify(results_cache)
    else:
        return jsonify({'error': '결과 데이터 로드 실패'}), 500

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
            'GET /results': '경기 결과 웹페이지',
            'GET /api/results': '경기 결과 API',
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
