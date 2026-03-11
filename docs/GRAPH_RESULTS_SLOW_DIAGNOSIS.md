# 분석기 그래프 결과 적용 느림 — 정밀 원인 분석

## 1. "그래프에 적용되는 결과가 느리다"의 의미

- **새 경기 결과**가 나온 뒤 → **그래프(정/꺽 블록)**에 반영되기까지의 지연
- 데이터 흐름: `서버 DB/외부 fetch` → `results_cache` → `/api/results` → `loadResults()` → `graphColorMatchResults` → `jung-kkuk-graph` DOM

---

## 2. 전체 지연 구간 (우선순위순)

### 2.1 서버: /api/results 응답 지연 (가장 큰 영향)

| 구간 | 조건 | 예상 지연 |
|------|------|-----------|
| **캐시 히트** | `(now_ms - last_update_time) < 150ms` | ~5ms (메모리 복사) |
| **캐시 미스** | TTL 150ms 초과 시 | **100~800ms** (`_build_results_payload_db_only`) |

**캐시 미스 발생 조건**:
- `RESULTS_RESPONSE_CACHE_TTL_MS = 150`
- 클라이언트 폴링: **계산기 실행 중 120ms**, 그 외 200~300ms
- apply가 0.1초마다 `results_cache` 갱신 → `last_update_time` 갱신
- **120ms 폴링**이면 150ms TTL을 자주 넘김 → 캐시 미스 빈번

**_build_results_payload_db_only 무거운 작업** (캐시 미스 시):
- `get_recent_results(24)` — DB 2000행 + color_matches
- `get_prediction_history(300)` — ph 미전달 시
- `compute_prediction` — CPU 집약
- `_get_latest_next_pick_for_chunk` 2~3회
- `_get_prediction_picks_best` 2회 (일반 + shape_pong)
- `_get_shape_15_win_rate_weighted`, `_get_main_recent15_win_rate_weighted` 등
- `_backfill_shape_predicted_in_ph` (backfill=0이면 스킵)

→ **단일 요청 100~800ms** 가능 (DB·CPU 부하에 따라)

---

### 2.2 서버: apply ↔ /api/results 경합

- **apply** (0.1초마다): `_apply_lock` → `get_recent_results` → `_build_results_payload_db_only(results, ph)` → `results_cache` 갱신
- **/api/results** (캐시 미스 시): `_build_results_payload_db_only()` **독립 호출** — results, ph 없음 → `get_recent_results`, `get_prediction_history` 매번 실행
- apply가 `_apply_lock`으로 블로킹 중이면 다음 apply 스킵 → `results_cache` 갱신 지연 → 캐시 미스 확률 증가

---

### 2.3 클라이언트: loadResults 내부 처리 (동기 블로킹)

loadResults 응답 수신 후 **한 번에** 실행되는 작업 (메인 스레드 블로킹):

| 단계 | 작업 | 비고 |
|------|------|------|
| 1 | `syncCalcHistoryFromServerPrediction` | 변경 시 `saveCalcStateToServer` + `updateAllCalcs` |
| 2 | `updatePredictionPicksCards`, `updateCalcStatus` | 3개 계산기 |
| 3 | `loadCalcStateFromServer` (100ms 스로틀) | 계산기 실행 중일 때 — **추가 fetch** |
| 4 | **colorMatchResults** | 15개 × (캐시 미스 시 parseCardValue 2회) |
| 5 | **graphColorMatchResults** | **(results.length - 16)회** 루프. results=100이면 **84회**. 캐시 미스 시 parseCardValue 2회 |
| 6 | colorMatchCache 정리 | `currentGameIDs` Set + key 순회 |
| 7 | 계산기 15개 미니 카드 | 3개 × 15개 DOM 생성 |
| 8 | **cardsDiv** | 15개 카드 createCard + appendChild |
| 9 | **jung-kkuk-graph** | segments → heights → getDynamicLineThreshold → 각 segment마다 column+block DOM |
| 10 | **graph-stats** | calcTransitions(전체/30/15), blendData, predictionHistory filter/map |
| 11 | **예측 카드/배팅중 블록** | predForRound, predForRecord, CALC_IDS.forEach 3회, getSmartReverseDoRev, getShapePredictionWinRate15 등 |
| 12 | calc 미니 그래프 | 3개 × graphValues 기반 DOM |

**graphColorMatchResults 병목**:
- results 100개 → 84번 루프
- **새 결과 1건 추가** 시: 새 (gameID_i, gameID_i+15) 조합 → 캐시 미스 → parseCardValue 2회
- 첫 로드 또는 results 대량 갱신 시: 캐시 미스 다수 → parseCardValue **168회** (84×2) 가능

---

### 2.4 클라이언트: 폴링 간격 vs 체감 지연

| 조건 | loadResults 호출 간격 | 새 결과 반영 최대 지연 |
|------|------------------------|------------------------|
| 계산기 실행 중 | 120ms | 120ms (폴링) + API 응답 + DOM 처리 |
| 그 외 | 200~300ms | 200~300ms + API 응답 + DOM 처리 |

**캐시 미스 시**: API 100~800ms + DOM 20~100ms → **총 120~900ms** 가능

---

## 3. 원인 우선순위 (정밀)

| 순위 | 원인 | 영향 | 근거 |
|------|------|------|------|
| **1** | **캐시 미스 시 _build_results_payload_db_only** | 100~800ms | get_recent_results, get_prediction_history, compute_prediction 등 |
| **2** | **캐시 TTL 150ms vs 폴링 120ms** | 캐시 미스 빈번 | 120ms마다 요청 → 150ms 내 apply 갱신 타이밍과 어긋나기 쉬움 |
| **3** | **graphColorMatchResults 루프** | 5~50ms | 84회 루프, 캐시 미스 시 parseCardValue 168회 |
| **4** | **graph-stats + 예측 카드 블록** | 10~80ms | calcTransitions, blendData, predictionHistory 순회, CALC_IDS 3회 복잡 로직 |
| **5** | **loadCalcStateFromServer 직후** | 추가 50~200ms | 계산기 실행 중 100ms 스로틀로 fetch → updateAllCalcs 재실행 |

---

## 4. 확인 방법

### 4.1 서버 측 (PERF_PROFILE)

```bash
PERF_PROFILE=1 python app.py
```

5초마다 출력:
- `build_results_payload_db_only` ms — **자주·높으면** 캐시 미스 빈번
- `get_recent_results` ms
- `scheduler_apply` ms

### 4.2 클라이언트 측 (개발자 도구)

1. **Network**: `/api/results` 응답 시간 (캐시 미스 시 100ms 이상)
2. **Performance**: loadResults 콜백 실행 시간 (스크립트 블로킹)
3. **Console**: `console.time('loadResults-cb');` … `console.timeEnd('loadResults-cb');` — DOM 처리 구간 측정

### 4.3 캐시 히트율 추정

- `last_update_time` 갱신 주기: apply 0.1초
- 요청 주기: 120ms (계산기 실행 중)
- 150ms TTL → 요청 시점이 apply 직후 30ms 이내여야 히트. **실제로는 미스가 많을 가능성 높음**

---

## 5. 권장 수정 방향 (우선순위)

### 5.1 캐시 TTL 연장 ✅ 적용됨

- `RESULTS_RESPONSE_CACHE_TTL_MS`: 150 → **250**
- 120ms 폴링에서 apply 0.1초 갱신 시, 250ms면 2~3회 연속 캐시 히트 가능

### 5.2 apply에서 results_cache 갱신 강화 (이미 적용됨)

- 현재: apply가 `_build_results_payload_db_only(results, ph)`로 results_cache 갱신

### 5.3 graphColorMatchResults 최적화 ✅ 적용됨

- **서버에서 graph_values 전달**: `_build_results_payload_db_only`, `_build_results_payload` 반환에 `graph_values` 포함
- 클라이언트: `data.graph_values` 있으면 로컬 parseCardValue 루프 생략, 서버 값 사용
- **효과**: parseCardValue 84~168회 제거, 클라이언트 DOM 처리 속도 개선

### 5.4 graph-stats / 예측 카드 블록 분리 (미적용)

- `requestAnimationFrame` 등으로 분리 시 첫 화면 반영 체감 개선 가능

### 5.5 /api/results 캐시 미스 시 경량 경로 (미적용)

- results만 조회 후 병합하는 경량 경로 검토 가능

---

## 6. 요약

| 구간 | 예상 지연 | 비고 |
|------|-----------|------|
| 서버 캐시 히트 | ~5ms | 즉시 반영 |
| 서버 캐시 미스 | 100~800ms | **주요 병목** |
| 클라이언트 DOM | 20~100ms | graphColorMatchResults, graph-stats |
| 폴링 대기 | 120~300ms | 조건별 |

**체감 "그래프 느림"** = 서버 캐시 미스(100~800ms) + 폴링 대기(120ms) + DOM(20~100ms) → **최대 약 1초** 가능.

우선 **캐시 TTL 250ms**와 **서버 graph_values 전달**을 적용하면 체감 개선이 클 것으로 예상됨.
