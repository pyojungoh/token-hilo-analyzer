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

## 2단계: 순수 유틸 함수 ✅ 완료

**완료 내용**: `utils.py` 생성, 분리 완료  
- sort_results_newest_first, normalize_pick_color_value, flip_pick_color, round_eq, parse_card_color, get_card_color_from_result

**대상**: Flask/request를 쓰지 않는 함수들  
**위험도**: 낮음

---

## 3단계: DB 관련 함수 ✅ 완료

**완료 내용**: `db.py` 생성, app.py에서 중복 제거 완료  
- init_database, get_db_connection, ensure_current_pick_table, save_game_result, get_calc_state, save_calc_state, _calc_state_memory, get_color_matches_batch, ensure_database_initialized, get_recent_results_raw, RealDictCursor

**위험도**: 낮음

---

## 4단계: 계산·예측 로직 (일부 완료 → 여기서 끊김)

**완료**: `prediction_logic.py` 생성  
- _blended_win_rate, _blended_win_rate_components → 분리·import 완료

**남은 작업 (아직 app.py에만 있음)**:
| 함수 | app.py 라인 |
|------|-------------|
| _update_calc_paused_after_round | 902 |
| _calculate_calc_profit_server | 934 |
| _apply_results_to_calcs | 1074 |
| _server_calc_effective_pick_and_amount | 1413 |

**위험도**: 중간 (OOM 주의: 제거 시 패치 파일 또는 한 함수씩 최소 편집)

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
