# 분석기 픽 보류·멈춤 증상 정밀 진단

## 1. 보류 표시 원인 (value=None → "보류")

### 1.1 15번 카드 조커
- **위치**: `_build_results_payload_db_only` L10705-10706, `ensure_stored_prediction_for_current_round` L2298-2300
- **조건**: `results[14].joker == True`
- **동작**: 저장·조회 모두 스킵 → server_pred.value=None → 화면 "보류"
- **정상**: 15번이 조커면 배팅 보류가 맞음

### 1.2 round_predictions 미저장
- **위치**: `get_stored_round_prediction` → `round_predictions` 테이블
- **원인**:
  - `ensure_stored_prediction_for_current_round`가 아직 해당 회차를 저장하지 못함
  - `compute_prediction`이 value=None 반환 → 저장 조건 `pred.get('value') is not None` 미충족
  - DB 오류로 `save_round_prediction` 실패
- **확인**: DB에서 `SELECT * FROM round_predictions ORDER BY round_num DESC LIMIT 5;`

### 1.3 조커 주의 구간 (skip_bet)
- **위치**: `_build_results_payload_db_only` L10732-10736
- **조건**: `joker_stats.skip_bet == True` (15카드 내 조커 2개 이상 등)
- **동작**: server_pred.value, color를 None으로 덮어씀 → "보류"

### 1.4 apply 스케줄러 스킵
- **위치**: `_scheduler_apply_results` L4748
- **조건**: `_apply_lock.acquire(blocking=False)` 실패 (이전 apply가 아직 실행 중)
- **결과**: ensure_stored·prediction_cache 갱신이 이번 사이클에서 실행되지 않음
- **연쇄**: apply가 0.3초 이상 걸리면 다음 apply도 스킵 → 보류 지속 가능

---

## 2. 멈춤(프리즈) 원인

### 2.1 _apply_lock 경합
- **흐름**: `_scheduler_apply_results`(0.3초) + `_refresh_results_background`(fetch 완료 시) 둘 다 _apply_lock 사용
- **문제**: apply 내부가 느리면(DB·계산) lock 보유 시간 증가 → 다음 apply 스킵
- **확인**: `_perf_log('scheduler_apply', ...)` 출력으로 apply 소요 시간 확인

### 2.2 _build_results_payload_db_only 비용
- **호출**: `_update_prediction_cache_from_db` → 매 0.3초
- **내용**: get_recent_results(24h) + get_prediction_history(300) + compute_prediction + 모양판별 등
- **중복**: `_scheduler_apply_results`에서 이미 get_recent_results 호출 후, _update_prediction_cache_from_db에서 다시 _build_results_payload_db_only → get_recent_results 재호출
- **병목**: DB 부하·계산 부하로 apply 전체 지연

### 2.3 get_recent_results 지연
- **위치**: `get_recent_results` — statement_timeout 8초
- **원인**: DB 커넥션 풀 고갈, 느린 쿼리, 네트워크 지연

### 2.4 _results_refresh_lock
- **위치**: `_refresh_results_background` — load_results_data(외부 fetch 2~4초)
- **동작**: lock 보유 중 다른 refresh 스킵
- **영향**: 페이지 로드 시 매번 refresh 스레드 시작 → lock 대기 누적 가능

---

## 3. 진단 체크리스트

| 항목 | 확인 방법 |
|------|-----------|
| 15번 카드 조커 | results[14].joker 확인 |
| round_predictions 저장 | `SELECT * FROM round_predictions WHERE round_num = (최신회차+1);` |
| apply 소요 시간 | 서버 로그 `_perf_log` 또는 print 추가 |
| apply 스킵 빈도 | `_apply_lock.acquire(False)` 실패 시 카운터 |
| joker_stats | `_compute_joker_stats` 반환값 skip_bet |
| DB 연결 | Railway/로컬 DB 상태, statement_timeout 초과 여부 |

---

## 4. 권장 수정

1. **apply 결과 재사용**: `_update_prediction_cache_from_db`에 results 전달해 get_recent_results 중복 호출 제거
2. **apply 타임아웃**: apply 내부에 전체 timeout(예: 2초) 적용, 초과 시 lock 조기 해제
3. **보류 시 로그**: prediction_cache가 value=None일 때 원인(15joker/stored없음/skip_bet) 로그
4. **진단 API**: `/api/debug-prediction` — 현재 results 일부, stored 여부, apply 상태 반환
