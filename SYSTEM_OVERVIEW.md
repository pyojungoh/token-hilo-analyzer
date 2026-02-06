# 토큰 하이로우 분석기 — 시스템 개요 & 코딩 규칙

이 문서는 전체 시스템을 파악하고 이어서 코딩할 때 참고하기 위한 요약이다.

---

## 1. 프로젝트 구조

| 경로 | 역할 |
|------|------|
| `app.py` | Flask 앱 전부 (라우트, DB, 비즈니스 로직, HTML/CSS/JS 인라인). **단일 대형 파일** (~5700줄). |
| `betting_integration.py` | 배팅 연동: `current_pick` 테이블 조회/저장, `/api/current-pick`에서 사용 |
| `templates/betting_helper.html` | 배팅 연동 안내 페이지 (`/betting-helper`) |
| `auto-betting-macro/` | PyQt5 기반 매크로: 분석기 API 호출 + 배팅 사이트 자동 클릭 |
| `REFACTOR_PLAN.md` | 향후 app.py를 역할별로 분리하는 리팩터링 계획 (v2 구조) |

- **브랜치**: 항상 `main`만 사용. master 등 사용 금지.
- **언어**: 응답/문서는 한국어.

---

## 2. 데이터 흐름 요약

### 2.1 결과 데이터

1. **수집**: `load_results_data()` → 외부(BASE_URL)에서 JSON 로드.
2. **저장**: `save_game_result()` → `game_results` 테이블.
3. **조회**: `get_recent_results(hours=24 또는 72)` → DB에서 **game_id 숫자 기준 DESC** 정렬.
4. **API**: `GET /api/results` → `_build_results_payload_db_only(24)` 우선, 없으면 72h, 없으면 캐시. **응답 직전** `_sort_results_newest_first(payload['results'])` **강제**.
5. **백그라운드**: `_refresh_results_background()` 스레드에서 `_build_results_payload()` 호출 → 캐시 갱신.
6. **스케줄러**: 2초마다 `_scheduler_fetch_results()` → 캐시 갱신, DB 저장, 계산기 회차 반영, **prediction_history 보정**.

### 2.2 예측·히스토리

- **round_predictions**: 회차별 “예측 중” 저장. 결과 나오면 `_merge_round_predictions_into_history()`로 `prediction_history`에 머지.
- **prediction_history**: 시스템 예측 기록 (전체 공용). 저장 경로는 둘 뿐:
  - **클라이언트**: 현재 회차 = 예측했던 회차일 때 `POST /api/save-prediction`.
  - **서버**: 스케줄러에서 `_apply_results_to_calcs()`로 pending_round 결과 반영 시 `save_prediction_record()` + **보정** `_backfill_latest_round_to_prediction_history(results)`.
- **보정**: 최신 회차가 히스토리에 없으면 서버가 `results[0]`, `_get_actual_for_round`, `compute_prediction(results[1:], ph)`로 한 건 저장.

### 2.3 계산기(calc)

- **calc_sessions**: session_id별 계산기 상태 (running, history, pending_round 등).
- **API**: `GET/POST /api/calc-state`. POST 시 running=true이고 started_at 없으면 서버가 started_at 설정.
- **회차 반영**: 스케줄러에서 `_apply_results_to_calcs(results)`로 실행 중인 계산기 pending_round에 대한 실제 결과를 반영하고 다음 예측으로 갱신.

### 2.4 배팅 연동

- **current_pick**: DB 1행(id=1). RED/BLACK, round_num, probability, suggested_amount.
- **API**: `GET/POST /api/current-pick`. 매크로·Tampermonkey 등이 픽 조회/갱신.

---

## 3. 코딩 규칙 (반드시 준수)

### 3.1 회차(round_num / gameID)

- **끝 3자리 사용 금지.** 서로 다른 회차가 같은 3자리로 겹칠 수 있음 (예: 11423052 vs 11424052 → 052).
- **비교·저장·표시 모두 전체 번호(정수 gameID).**
- DB `prediction_history.round_num`, API `round`, 클라이언트 `lastPrediction.round`·`h.round` → **전부 전체 회차 번호**.
- 화면 표시도 `displayRound(round)` 등으로 **전체 회차** 표시.

### 3.2 API 결과 순서 (맨 앞 = 최신 회차)

- **응답 직전** `payload['results']`를 **game_id 기준 내림차순** 한 번 더 정렬 (`_sort_results_newest_first`).
- DB 조회는 **24h(또는 72h 폴백)** 구간 사용.
- **클라이언트**: 서버에서 결과가 1건이라도 오면 **전체 교체**. 예전 데이터와 병합해 상위 N개만 쓰는 로직 사용 금지.
- **맨 왼쪽 카드 = index 0 = 최신 회차.** `displayResults[0]`이 현재 회차. DOM/CSS 역순 그리기 금지.

### 3.3 예측기표 = 메인 예측기 픽 고정 (절대 수정 금지)

- **예측기표에는 무조건 메인 예측기 픽만 고정값으로 들어가야 한다.** 계산기(반픽/승률반픽), 실제 경고 합산승률에 따른 반대픽 등 어떤 보정도 예측기표·prediction_history에 넣지 않는다.
- **prediction_history** DB 및 `save_prediction_record()` 호출 시에는 **항상 예측픽(메인 예측기 픽)**만 저장. 서버 스케줄러는 `pred_for_record = pending_predicted`(원본)로 저장하고, 반픽/승률반픽 적용값(`pred_for_calc`)은 **계산기 history에만** 사용.
- 이 원칙을 삭제·완화하지 말 것. 위반 시 합산승률 50% 이하일 때 예측기표에 반대값이 들어가는 버그가 재발한다.

### 3.4 prediction_history 저장

- 저장 경로: (1) 클라이언트 `/api/save-prediction`, (2) 서버 스케줄러 `save_prediction_record()` + **보정**.
- **보정 로직 `_backfill_latest_round_to_prediction_history` 유지.** 화면 미반영으로 누락된 회차를 서버가 자동 저장.
- 보정 시: `results[0]` 최신 회차, `_get_actual_for_round(results, latest_round)`, `compute_prediction(results[1:], ph)` 후 `save_prediction_record()` 호출. 저장값은 **예측픽만** (3.3 준수).

### 3.5 정렬·필터 요약

| 위치 | 규칙 |
|------|------|
| DB `get_recent_results` | `ORDER BY (game_id 숫자) DESC`, 24h/72h 구간 |
| API `get_results()` | 응답 직전 `_sort_results_newest_first(payload['results'])` |
| 클라이언트 `loadResults` | `allResults = sortResultsNewestFirst(newResults).slice(0, 300)` **전체 교체** |
| 카드 렌더 | `displayResults = allResults.slice(0, 15)`, index 0부터 append → 맨 왼쪽이 최신 |

---

## 4. 주요 API 목록

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/` | `/results`로 리다이렉트 |
| GET | `/results` | 메인 분석기 페이지 (RESULTS_HTML) |
| GET | `/api/results` | 결과 + server_prediction + prediction_history + round_actuals (캐시 no-store) |
| GET/POST | `/api/calc-state` | 계산기 상태 조회/저장 |
| GET | `/api/win-rate-buckets` | 합산승률 구간 |
| POST | `/api/round-prediction` | 회차별 예측 저장 (round_predictions) |
| POST | `/api/prediction-history` | 예측 기록 저장 (save_prediction_record) |
| GET/POST | `/api/current-pick` | 배팅용 현재 픽 (betting_integration) |
| GET | `/api/current-status` | 현재 상태 요약 |
| GET | `/api/streaks` | 스트릭 데이터 |
| POST | `/api/refresh` | 수동 갱신 트리거 |
| GET | `/betting-helper` | 배팅 연동 안내 |
| GET | `/health` | 헬스체크 |

---

## 5. DB 테이블 요약

- **game_results**: game_id, result, hi/lo/red/black/jqka/joker, hash_value, salt_value.
- **color_matches**: (game_id, compare_game_id) → 정/꺽 match_result.
- **prediction_history**: round_num(PK), predicted, actual, probability, pick_color, blended_win_rate, rate_15/30/100.
- **calc_sessions**: session_id(PK), state_json.
- **round_predictions**: round_num(PK), predicted, pick_color, probability.
- **current_pick**: id=1 한 행, pick_color, round_num, probability, suggested_amount.

---

## 6. 규칙 수정·확장 시

- **“맨 왼쪽 = 최신 회차”** 와 **“히스토리 누락 없이 서버 보정”** **"예측기표 = 메인 예측기 픽 고정(절대 수정 금지)"** — 위 세 원칙은 유지한다.
- 상세 규칙: `.cursor/rules/token-hilo-analyzer-conventions.mdc` 참고.

---

*이 문서는 시스템 파악 및 이어서 코딩용 참고 자료이다.*
