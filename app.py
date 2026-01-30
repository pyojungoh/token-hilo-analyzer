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

def fetch_with_retry(url, max_retries=MAX_RETRIES, silent=False):
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
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                # 404는 조용히 처리 (파일이 없을 수 있음)
                return None
            if not silent and attempt == max_retries - 1:
                print(f"HTTP 오류 {e.response.status_code}: {url}")
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            if not silent:
                print(f"요청 오류: {url} - {str(e)[:100]}")
    return None

def load_game_data():
    """게임 데이터 로드 (current_status_frame.json)"""
    try:
        url = f"{BASE_URL}{DATA_PATH}/current_status_frame.json?t={int(time.time() * 1000)}"
        response = fetch_with_retry(url, silent=True)  # 404 에러는 조용히 처리
        
        if not response:
            # 파일이 없으면 기본값 반환 (타이머는 클라이언트 측에서만 계산)
            return {
                'round': 0,
                'elapsed': 0,
                'currentBets': {
                    'red': [],
                    'black': []
                },
                'timestamp': datetime.now().isoformat()
            }
        
        data = response.json()
        
        return {
            'round': data.get('round', 0),
            'elapsed': data.get('elapsed', 0),
            'currentBets': {
                'red': data.get('red', []) if isinstance(data.get('red'), list) else [],
                'black': data.get('black', []) if isinstance(data.get('black'), list) else []
            },
            'timestamp': datetime.now().isoformat()
        }
    except Exception as e:
        # 에러 발생 시 기본값 반환 (서버 크래시 방지)
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
    """경기 결과 데이터 로드 (result.json)"""
    try:
        url = f"{BASE_URL}{DATA_PATH}/result.json?t={int(time.time() * 1000)}"
        response = fetch_with_retry(url, silent=True)
        
        if not response:
            return []
        
        data = response.json()
        
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
            except Exception:
                # 개별 게임 파싱 오류는 무시
                continue
        
        return results
    except Exception:
        # 전체 오류 시 빈 배열 반환
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
            
            // 숫자 변환: A(1), 2~9, 10(J), 11(Q), 12(Q), 13(K)
            if (numInt === 1) return 'A';
            if (numInt === 10) return 'J';
            if (numInt === 11) return 'Q';
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
            if (result.joker) return { text: 'JOKER', class: 'joker' };
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
            
            cardWrapper.appendChild(card);
            
            // 카테고리 표시 (별도 박스, 카드 아래)
            const category = getCategory(result);
            if (category) {
                const categoryDiv = document.createElement('div');
                categoryDiv.className = 'card-category ' + category.class;
                categoryDiv.textContent = category.text;
                cardWrapper.appendChild(categoryDiv);
            }
            
            // 색상 비교 결과 표시 (1번째 카드만, 하이로우 박스 아래)
            if (index === 0 && colorMatchResult !== null) {
                const colorMatchDiv = document.createElement('div');
                colorMatchDiv.className = 'color-match ' + (colorMatchResult ? 'jung' : 'kkuk');
                colorMatchDiv.textContent = colorMatchResult ? '정' : '꺽';
                cardWrapper.appendChild(colorMatchDiv);
            }
            
            return cardWrapper;
        }
        
        // 이전 15번째 카드 색상 저장 (새 결과 발표 전)
        let previous15thCardColor = null;
        
        async function loadResults() {
            try {
                const response = await fetch('/api/results');
                
                if (!response.ok) {
                    const statusElement = document.getElementById('status');
                    if (statusElement) {
                        statusElement.textContent = `서버 오류: HTTP ${response.status}`;
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
                
                const results = data.results || [];
                const statusElement = document.getElementById('status');
                const cardsDiv = document.getElementById('cards');
                
                if (!statusElement || !cardsDiv) {
                    console.error('DOM 요소를 찾을 수 없습니다');
                    return;
                }
                
                statusElement.textContent = `총 ${results.length}개 경기 결과`;
                
                // 최신 결과가 왼쪽에 오도록 (원본 데이터가 최신이 앞에 있음)
                // 최신 15개만 표시 (반응형으로 모두 보이도록)
                const displayResults = results.slice(0, 15);
                
                // 색상 비교 결과 계산
                let colorMatchResult = null;
                if (displayResults.length >= 15 && previous15thCardColor !== null) {
                    // 새로운 1번째 카드 색상
                    const newCard1 = parseCardValue(displayResults[0].result || '');
                    const newColor = newCard1.isRed;
                    
                    // 이전 15번째 카드 색상과 비교
                    colorMatchResult = (previous15thCardColor === newColor);
                }
                
                // 현재 15번째 카드 색상 저장 (다음 비교를 위해)
                if (displayResults.length >= 15) {
                    const card15 = parseCardValue(displayResults[14].result || '');
                    previous15thCardColor = card15.isRed;
                    
                    // 헤더에 기준 색상 표시
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
                        // 1번째 카드만 색상 비교 결과 전달
                        const card = createCard(result, index, index === 0 ? colorMatchResult : null);
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
                console.error('loadResults 오류:', error);
                const statusElement = document.getElementById('status');
                if (statusElement) {
                    if (error.message === 'Failed to fetch' || error.name === 'TypeError') {
                        statusElement.textContent = '연결 오류: 서버에 연결할 수 없습니다';
                    } else {
                        statusElement.textContent = '오류: ' + error.message;
                    }
                }
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
                
                // 0.3초마다 서버에서 데이터 가져오기 (더 빠른 동기화)
                if (now - timerData.lastFetch > 300) {
                    try {
                        const response = await fetch('/api/current-status?t=' + now);
                        if (!response.ok) throw new Error('Network error');
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
                            if (timerData.round !== prevRound || 
                                (prevElapsed > 8 && data.elapsed < 2)) {
                                loadResults();
                                lastResultsUpdate = now;
                            }
                        }
                    } catch (error) {
                        // 에러가 나도 클라이언트 측 계산 계속
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
                
                // 타이머가 거의 0이 되면 경기 결과 새로고침 (라운드 종료 직전)
                if (remaining <= 0.5 && now - lastResultsUpdate > 1000) {
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
        
        // 초기 로드
        loadResults();
        updateTimer();
        
        // 2초마다 결과 새로고침
        setInterval(() => {
            if (Date.now() - lastResultsUpdate > 2000) {
                loadResults();
                lastResultsUpdate = Date.now();
            }
        }, 2000);
        
        // 0.1초마다 타이머 업데이트 (실시간 동기화)
        setInterval(updateTimer, 100);
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
    try:
        global results_cache, last_update_time
        
        current_time = time.time() * 1000
        if results_cache and (current_time - last_update_time) < CACHE_TTL:
            return jsonify(results_cache)
        
        results = load_results_data()
        # 항상 결과 반환 (빈 배열 포함)
        results_cache = {
            'results': results,
            'count': len(results),
            'timestamp': datetime.now().isoformat()
        }
        last_update_time = current_time
        return jsonify(results_cache)
    except Exception:
        # 에러 발생 시 빈 결과 반환
        return jsonify({
            'results': [],
            'count': 0,
            'timestamp': datetime.now().isoformat()
        })

@app.route('/api/current-status', methods=['GET'])
def get_current_status():
    """현재 게임 상태"""
    try:
        data = load_game_data()
        # 항상 데이터 반환 (기본값 포함)
        return jsonify(data)
    except Exception as e:
        # 에러 발생 시 기본값 반환
        return jsonify({
            'round': 0,
            'elapsed': 0,
            'currentBets': {'red': [], 'black': []},
            'timestamp': datetime.now().isoformat()
        })

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
