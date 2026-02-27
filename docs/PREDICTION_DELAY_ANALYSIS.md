# 예측픽 지연 원인 분석

> 상세: **docs/RESULTS_DELAY_ANALYSIS.md** (정밀 검사·타이밍 값)

## 예측픽이 "늦게 나오기 시작"하는 충돌 지점

### 1. **예측픽 갱신 경로**

| 경로 | 주기 | Lock | 역할 |
|------|------|------|------|
| `_scheduler_apply_results` | 0.05초 | `_apply_lock` | DB 결과 → ensure_stored → prediction_cache |
| `_refresh_results_background` | 0.05초마다 트리거 | `_results_refresh_lock` | 외부 fetch → DB 저장 → apply |

### 2. **핵심 병목: 외부 fetch 의존**

예측픽의 **원본 데이터**는 `round_predictions` 테이블이다.
- `round_predictions`는 `ensure_stored_prediction_for_current_round()`에서만 채워짐
- 이 함수는 **DB에 이미 있는** `results`를 기준으로 `compute_prediction` 후 저장

**새 회차 결과가 DB에 들어오는 시점:**
- `_refresh_results_background` → `_build_results_payload()` → `load_results_data()` (외부 fetch)
- fetch 성공 시 `save_game_result()`로 DB 저장
- **fetch가 1.5초(최대) 걸리면**, 그동안 DB에는 새 회차가 없음 (경로당 0.6초, 전체 1.5초 타임아웃)

### 3. **충돌 지점**

| 충돌 | 설명 |
|------|------|
| **A. fetch 지연** | `load_results_data()`가 `result.json` 등 6개 경로 병렬 시도. 경로당 0.6초·전체 1.5초 타임아웃 |
| **B. _results_refresh_lock** | fetch가 끝날 때까지 lock 유지. 그동안 새 fetch 시작 불가 |
| **C. _apply_lock 경쟁** | apply 스킵 시 `_run_prediction_update_light` (0.1초 스로틀)로 예측픽만 경량 갱신 |
| **D. DB → round_predictions 순서** | 새 결과가 DB에 들어온 뒤에야 `ensure_stored_prediction_for_current_round`가 새 회차 예측을 계산·저장 |

### 4. **데이터 흐름 요약**

```
[외부 result.json] --load_results_data(최대 1.5초)--→ [DB game_results]
                                                          ↓
[0.05초마다] _scheduler_apply_results: get_recent_results ←─┘
                    ↓
         ensure_stored_prediction_for_current_round
                    ↓
         round_predictions 테이블 저장
                    ↓
         _update_prediction_cache_from_db
                    ↓
         prediction_cache ← /api/current-prediction (클라이언트 200ms 폴링)
```

**지연 발생 구간:** `load_results_data()` 완료 시점까지.

### 5. **추가 가능 원인**

- **스케줄러 5초 지연**: 앱 기동 후 5초 뒤에 스케줄러 시작
- **fetch 실패**: `load_results_data()`가 None 반환 시 DB에 새 결과 미저장
- **get_results() 트리거**: `/api/results` 호출 시 0.2초 스로틀로 `_refresh_results_background` 시작
