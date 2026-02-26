# 예측픽 지연·병목·충돌 지점 분석

분석기 예측픽이 늦게 나오는 원인과 충돌 지점을 정리한 문서.

---

## 1. 데이터 흐름 요약

```
[게임 사이트 result.json]
        │
        │ load_results_data() — 0.5~4초 (최대 병목)
        ▼
[DB game_results 저장]
        │
        │ get_recent_results(24h)
        ▼
ensure_stored_prediction_for_current_round → round_predictions
        │
        ▼
_update_prediction_cache_from_db → prediction_cache
        │
        │ GET /api/current-prediction (클라이언트 300ms 폴링)
        ▼
[클라이언트 lastPrediction] → updateCalcStatus → postCurrentPickIfChanged
        │
        ▼
[relay 캐시] ← POST /api/current-pick-relay
        │
        │ GET /api/current-pick-relay (매크로 25~50ms 폴링)
        ▼
[매크로 배팅]
```

---

## 2. 병목 구간 (지연 원인)

| 구간 | 소요 | 설명 |
|------|------|------|
| **① 외부 fetch** | **0.5~4초** | `load_results_data()` — 병렬 6경로, 경로당 2초·전체 4초 타임아웃. **가장 큰 병목** |
| **② fetch 시도 주기** | 0.2초 | `_scheduler_fetch_results` 0.2초마다. 직전 fetch 완료 전까지 `_results_refresh_lock`으로 새 fetch 스킵 |
| **③ apply** | 0~300ms | `_scheduler_apply_results` 0.3초마다. `_apply_lock` 경합 시 스킵 |
| **④ prediction_cache 갱신** | apply 내부 | apply 완료 시에만 `_update_prediction_cache_from_db` 호출. **fetch 완료 전까지 구 회차 유지** |
| **⑤ 클라이언트 폴링** | 0~300ms | `/api/current-prediction` 300ms마다. 최악 시 300ms 추가 지연 |
| **⑥ 스케줄러 지연** | 25초 | 앱 기동 후 25초 뒤 스케줄러 시작 → 초기 25초 예측픽 갱신 없음 |

**총 지연 = ① + ②~⑤**  
→ **핵심: ① 외부 fetch(0.5~4초)가 예측픽 지연의 주원인**

---

## 3. 충돌 지점

### 3.1 Lock 경합

| Lock | 사용처 | 충돌 |
|------|--------|------|
| `_results_refresh_lock` | `_refresh_results_background` | fetch 2~4초 동안 보유 → 그동안 새 fetch 스킵. `/api/results` 호출 시 트리거된 refresh도 스킵 |
| `_apply_lock` | `_scheduler_apply_results`, `_refresh_results_background`(fetch 완료 시) | apply 실행 중이면 다른 쪽 스킵. apply가 0.3초 이상 걸리면 다음 apply도 스킵 |

### 3.2 예측픽 갱신 경로 (단일화됨)

- **prediction_cache**는 `_update_prediction_cache_from_db`에서만 갱신
- 호출처: `_scheduler_apply_results`, `_refresh_results_background`(fetch 완료 시)
- **별도 prediction_cache job 없음** (과거 제거됨) — fetch/apply 직후에만 갱신

### 3.3 apply 내부 순서 (의도적)

```
1. get_recent_results(24h)
2. ensure_stored_prediction_for_current_round(results)
3. _apply_results_to_calcs(results)  ← 무거움 (세션별 get_shape_prediction_hint)
4. _backfill_latest_round_to_prediction_history(results)
5. _update_prediction_cache_from_db(results=results)  ← results 전달로 get_recent_results 중복 방지
6. _update_relay_cache_for_running_calcs()
```

- `_update_prediction_cache_from_db(results=results)` 시 `_build_results_payload_db_only`가 `get_recent_results` 재호출 안 함 (중복 방지)

### 3.4 fetch vs apply 타이밍

- **fetch 완료 시**: `_refresh_results_background`가 `_apply_lock` 획득 → apply 즉시 실행 → prediction_cache 갱신
- **fetch 미완료 시**: `_scheduler_apply_results`(0.3초)만 apply 실행. DB에 새 결과 없으면 구 회차 예측 유지
- **결론**: 새 회차 예측픽은 **fetch 완료 시점**에 결정. fetch가 느리면 예측픽도 늦음

---

## 4. 추가 충돌·주의점

### 4.1 results_cache vs prediction_cache

- `/api/current-prediction`: `prediction_cache` 우선 → `results_cache['server_prediction']` 폴백
- `results_cache`는 `_refresh_results_background`(fetch 경로) 또는 `/api/results`(DB 경로)에서 갱신
- **prediction_cache**가 비어 있으면 `results_cache` 사용. fetch 대기 중이면 `results_cache`도 구 데이터일 수 있음

### 4.2 첫 요청 시 캐시 비어 있음

- `/api/current-prediction` 첫 요청 시 캐시 비어 있으면 `_update_prediction_cache_from_db()` 1회 호출
- 이때 `results=None`이므로 `_build_results_payload_db_only`가 `get_recent_results` 호출 → DB 부하

### 4.3 /api/results 트리거

- `/api/results` 호출 시 `!_results_refreshing`이면 `_refresh_results_background` 스레드 시작
- fetch가 이미 실행 중이면 새 스레드는 lock 획득 실패로 즉시 반환
- **클라이언트가 results를 자주 안 부르면** fetch 빈도 감소 가능 (스케줄러 0.2초는 유지)

### 4.4 apply 스킵 시 예측픽 지연

- `_apply_lock.acquire(blocking=False)` 실패 → apply 스킵
- apply가 0.3초 이상 걸리면(DB·계산 부하) 다음 0.3초 주기 apply도 스킵 가능
- **연쇄 스킵** 시 prediction_cache 갱신 지연

### 4.5 15번 카드 조커

- `results[14].joker == True` → `compute_prediction` value=None → 저장/조회 스킵 → "보류"
- 정상 동작. 배팅 보류가 맞음

---

## 5. 클라이언트 측

| 항목 | 값 | 비고 |
|------|-----|------|
| prediction 폴링 | 300ms | `predictionPollIntervalId` |
| shape-pick 폴링 | 400ms | 모양 카드용 |
| calcState 폴링 | 1초 (탭 보일 때) | 계산기 상태 |
| postCurrentPickIfChanged | 즉시 (디바운스 없음) | round/pickColor/suggested_amount 같으면 재전송 스킵 |

---

## 6. 개선 가능 방향

| 방향 | 효과 | 난이도 |
|------|------|--------|
| 외부 fetch 타임아웃 축소 | 4초→2초. 실패 시 빠른 재시도 | 중 (실패율 상승) |
| fetch 폴링 간격 축소 | 0.2초→0.1초. 요청 과다 시 차단 가능 | 낮음 |
| prediction 폴링 간격 축소 | 300ms→150ms. 서버 부하 소폭 증가 | 낮음 |
| apply 결과 재사용 | 이미 적용됨 (results 전달) | - |
| get_shape_prediction_hint 캐싱 | 이미 적용됨 (apply 내 _shape_hint_cache) | - |
| 스케줄러 25초 지연 | 앱 기동 직후 fetch 가능하도록 단축 검토 | 중 |

---

## 7. 진단 체크리스트

| 항목 | 확인 방법 |
|------|-----------|
| fetch 소요 시간 | `PERF_PROFILE=1` 실행 시 `refresh_results_background` ms |
| apply 소요 시간 | `_perf_log('scheduler_apply', ...)` |
| apply 스킵 | `_apply_lock.acquire(False)` 실패 시 로그 |
| prediction_cache 보류 | value=None 시 60초마다 `[진단] 보류: round=...` 로그 |
| round_predictions 저장 | `SELECT * FROM round_predictions ORDER BY round_num DESC LIMIT 5` |

---

## 8. 관련 코드 위치

| 역할 | 파일 | 함수/위치 |
|------|------|-----------|
| 외부 fetch | app.py | `load_results_data`, `_build_results_payload` |
| fetch 스케줄 | app.py | `_scheduler_fetch_results` (0.2초) |
| apply | app.py | `_scheduler_apply_results` (0.3초), `_refresh_results_background` 내 |
| prediction_cache | app.py | `_update_prediction_cache_from_db` |
| current-prediction API | app.py | `get_current_prediction` |
| 클라이언트 폴링 | app.py (인라인 JS) | `predictionPollIntervalId` 300ms |
| relay POST | app.py (인라인 JS) | `postCurrentPickIfChanged` |
