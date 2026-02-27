# 분석기 결과값 지연 정밀 검사

## 1. 데이터 흐름 요약

```
[외부 result.json] --load_results_data(최대 2.5초)--→ [DB game_results]
         ↑                                                    ↓
   _scheduler_fetch (0.05초)                    _scheduler_apply (0.05초)
   _results_refresh_lock 유지                        get_recent_results
         │                                                    ↓
         └────────────────────────────────── ensure_stored_prediction_for_current_round
                                                          ↓
                                            _update_prediction_cache_from_db
                                                          ↓
                                            prediction_cache ← /api/current-prediction (클라이언트 200ms)
```

## 2. 현재 타이밍 값 (10초 게임 8초 내 배팅 규칙)

| 구간 | 항목 | 값 | 비고 |
|------|------|-----|------|
| **서버** | fetch 스케줄 | 0.05초 | 20회/초 트리거 |
| | apply 스케줄 | 0.05초 | 20회/초 |
| | fetch 경로당 타임아웃 | **1초** | `RESULTS_FETCH_TIMEOUT_PER_PATH` (0.6초는 그래프 미갱신 발생 → 완화) |
| | fetch 전체 타임아웃 | **2.5초** | `RESULTS_FETCH_OVERALL_TIMEOUT` |
| | /api/results refresh 스로틀 | **0.2초** | 클라이언트 요청 시 트리거 (지연 완화) |
| | apply 스킵 시 경량 갱신 스로틀 | **0.1초** | `_run_prediction_update_light` (지연 완화) |
| | 스케줄러 시작 지연 | 5초 | 앱 기동 후 |
| **클라이언트** | results 체크 주기 | 200ms | `resultsInterval` |
| | loadResults 실제 간격 | 150~320ms | 계산기 실행 중 150ms |
| | current-prediction 폴링 | 200ms | `predictionPollIntervalId` |

## 3. 병목 지점

### A. 외부 fetch (최대 영향)

- `load_results_data()`: 6개 경로 병렬 시도, **먼저 성공한 것 사용**
- `_results_refresh_lock`: fetch가 끝날 때까지 **새 fetch 시작 불가**
- **최악**: 모든 경로 1초씩 실패 → 2.5초 대기 → 그동안 새 회차 DB 미반영

### B. Lock 직렬화

- fetch 1회 = lock 점유 ~2.5초 (fetch 시간)
- 0.05초마다 새 fetch 시도 → lock 때문에 **스킵**
- **실질 fetch 빈도**: 1회 / 2.5초 (외부 지연 시)

### C. apply vs 경량 갱신

- apply가 `_apply_lock`을 못 잡으면 → `_run_prediction_update_light` (0.1초 스로틀)
- prediction_cache 갱신이 최대 0.1초 지연 가능

### D. DB 쿼리

- `get_recent_results(hours=24)`: LIMIT 2000, statement_timeout 8초
- `calculate_and_save_color_matches` + `get_color_matches_batch` 포함
- 정상 시 수십 ms 수준이지만, DB 부하 시 지연 가능

## 4. 적용된 개선

| 항목 | 이전 | 적용 후 | 비고 |
|------|------|---------|------|
| fetch 경로당/전체 타임아웃 | - | **1초 / 2.5초** | 0.6/1.5초는 외부 응답 전 타임아웃으로 그래프 미갱신 → 원복 |
| 경량 갱신 스로틀 | 0.2초 | **0.1초** | apply 스킵 시 prediction 갱신 2배 빠름 |
| /api/results 스로틀 | 0.3초 | **0.2초** | 클라이언트 활성 시 fetch 트리거 빈도 증가 |

## 5. 수정 시 주의

- 10초 게임 8초 내 배팅: fetch 타임아웃 짧게 유지 (경로당 0.6초, 전체 1.5초)
- `prediction-cache-architecture.mdc`: `_update_prediction_cache_from_db`에서 `load_results_data` 호출 금지
- `get_current_prediction`: prediction_cache 폴백 순서 변경 금지
