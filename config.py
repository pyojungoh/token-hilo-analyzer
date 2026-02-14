# -*- coding: utf-8 -*-
"""
1단계 모듈화: 상수·설정
app.py에서 분리. 로직 변경 없음.
"""
import os

# .env 로드 (config가 먼저 import될 수 있으므로)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# 환경 변수 기반
BASE_URL = os.getenv('BASE_URL', 'http://tgame365.com')
DATA_PATH = ''
TIMEOUT = int(os.getenv('TIMEOUT', '10'))
MAX_RETRIES = int(os.getenv('MAX_RETRIES', '2'))
DATABASE_URL = os.getenv('DATABASE_URL', None)
BETTING_SITE_URL = os.getenv('BETTING_SITE_URL', 'https://nhs900.com')

# 캐시
CACHE_TTL = 1000  # 결과 캐시 유효 시간 (ms)

# 결과 수집
RESULTS_FETCH_TIMEOUT_PER_PATH = 4
RESULTS_FETCH_OVERALL_TIMEOUT = 6
RESULTS_FETCH_MAX_RETRIES = 1
