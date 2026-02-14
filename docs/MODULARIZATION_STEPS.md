# app.py 모듈화 단계 가이드

OOM 방지 및 유지보수성을 위해 app.py(약 1만 줄)를 단계적으로 모듈화하는 순서입니다.

---

## 1단계: 상수·설정 (가장 안전) ✅ 완료

**대상**: `WIN_RATE_*`, `BET_DELAY_*`, `CACHE_TTL` 등 숫자/문자열 상수

**완료 내용**: `config.py` 생성, 다음 상수 분리 완료
- BASE_URL, DATA_PATH, TIMEOUT, MAX_RETRIES, DATABASE_URL
- BETTING_SITE_URL
- CACHE_TTL
- RESULTS_FETCH_TIMEOUT_PER_PATH, RESULTS_FETCH_OVERALL_TIMEOUT, RESULTS_FETCH_MAX_RETRIES

**이유**:
- 로직 변경 없음
- `from config import WIN_RATE_LOW_BAND` 형태로만 교체
- 의존성 거의 없음

**위험도**: 거의 없음

---

## 2단계: 순수 유틸 함수

**대상**: Flask/request를 쓰지 않는 함수들  
예: `_normalize_pick_color`, `_sort_results_newest_first`, 날짜/숫자 포맷 함수 등

**이유**:
- 입력 → 출력만 하는 함수
- Flask, DB, 전역 변수에 의존하지 않음
- 테스트·이동이 쉬움

**위험도**: 낮음

---

## 3단계: DB 관련 함수

**대상**: `get_db_connection`, `get_recent_results`, `get_calc_state` 등 DB 접근 함수

**이유**:
- DB만 다루는 함수들
- `betting_integration.py`처럼 이미 분리된 패턴이 있음
- 라우트/비즈니스 로직과 경계가 비교적 명확

**위험도**: 낮음

---

## 4단계: 계산·예측 로직

**대상**: `_server_calc_effective_pick_and_amount`, `_apply_results_to_calcs` 등  
예측·승률·마틴 계산 관련 함수

**이유**:
- 비즈니스 로직이 모여 있음
- Flask `request`를 직접 쓰지 않는 부분 위주
- 3단계 DB 함수를 먼저 분리해 두면 의존성이 단순해짐

**위험도**: 중간

---

## 5단계: 라우트·블루프린트 분리

**대상**: `/api/current-pick`, `/api/results`, `/results` 등 라우트 그룹

**이유**:
- Flask Blueprint로 `/api`, `/results` 등을 나눌 수 있음
- 파일이 많이 쪼개지고 구조가 바뀜
- 1~4단계를 먼저 해두면 이 단계가 수월해짐

**위험도**: 높음

---

## 요약

| 순서 | 대상 | 위험도 |
|------|------|--------|
| 1 | 상수·설정 | 거의 없음 |
| 2 | 순수 유틸 함수 | 낮음 |
| 3 | DB 관련 함수 | 낮음 |
| 4 | 계산·예측 로직 | 중간 |
| 5 | 라우트·블루프린트 | 높음 |

**추천**: 1단계(상수)부터 시작하고, 문제 없으면 2단계(유틸)로 넘어가는 방식이 가장 안전합니다.
