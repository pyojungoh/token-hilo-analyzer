# 예측기픽 메뉴 깜빡임 원인 분석

## 1. 예측기픽 메뉴 구조

| 요소 | DOM ID | 갱신 함수 |
|------|--------|-----------|
| 4카드 컨테이너 | `#prediction-picks-cards` | `updatePredictionPicksCards(sp)` |
| 메인 카드 | `[data-type="main"]` | value, color, 15회 승률 |
| 메인반픽 카드 | `[data-type="main_reverse"]` | value, color, 15회 승률 |
| 모양 카드 | `[data-type="shape"]` | value, color, 15회 승률 |
| 퐁당 카드 | `[data-type="pong"]` | value, color, 15회 승률 |
| 강조 카드 | `.pred-pick-card-calc-best` | `calc_best_type` 기준 |

---

## 2. 갱신 경로 (3개)

| 경로 | 간격 | API | sp 출처 |
|------|------|-----|---------|
| **A. loadResults** | 200~320ms | `/api/results` | `results_cache['server_prediction']` (전체) |
| **B. prediction 폴링** | 200ms | `/api/current-prediction` | `prediction_cache` 우선 → `results_cache` |
| **C. shape 폴링** | 400ms | `/api/shape-pick` | `lastSpForCards` + `lastShapePickFromApi` 병합 |

---

## 3. 핵심 원인: prediction_cache = 경량(light) 전용

### prediction_cache vs results_cache

| 캐시 | 갱신 경로 | server_prediction 필드 |
|------|-----------|------------------------|
| **prediction_cache** | `_update_prediction_cache_from_db` → `_build_server_prediction_light` | `value`, `round`, `prob`, `color`, `warning_u35` **만** |
| **results_cache** | `_refresh_results_background` → `_build_results_payload_db_only` | **전체**: `main_reverse`, `shape_pick`, `pong_pick`, `calc_best_type` 등 |

### /api/current-prediction 우선순위

```
prediction_cache (우선) → results_cache['server_prediction'] (폴백)
```

- `prediction_cache`가 있으면 **항상** light 버전 반환
- `prediction_cache`는 0.2초마다 `_update_prediction_cache_from_db`로 갱신
- `_build_server_prediction_light`는 **calc_best·shape_pick·main_reverse·pong_pick 생략** (속도용)

---

## 4. 깜빡임 발생 시나리오

1. **loadResults** 완료 → `sp` = 전체 → `updatePredictionPicksCards(full)` → 4카드 모두 표시, 강조 적용
2. **약 0~100ms 후** prediction 폴링 → `sp` = light → `updatePredictionPicksCards(light)` 호출
3. light `sp`에는 `main_reverse`, `pong_pick`, `calc_best_type` 없음
   - `mainReverseVal` = null → 메인반픽 "—"
   - `pongVal` = null → 퐁당 "—"
   - `bestType` = null → 강조 해제
4. **약 100~200ms 후** loadResults 다시 완료 → `sp` = 전체 → 4카드 복원
5. → **light ↔ full 교차**로 인한 깜빡임

---

## 5. 부가 원인

### 5.1 calc_best_type 변동

- `_get_prediction_picks_best`가 4개 승률 비교 후 `calc_best_type` 결정
- 승률이 비슷하면 (예: 52.1 vs 52.3) `ph`·반올림 차이로 `main` ↔ `main_reverse` 등 전환
- 3회 연속 디바운스로 완화했으나, light `sp`가 들어오면 `calc_best_type`이 없어 강조가 해제됨

### 5.2 메인·메인반픽 서버 불일치

- 서버 `prediction_cache` / `results_cache` 갱신 시점 차이
- `main_reverse` 등이 요청마다 달라질 수 있음
- (이전 42f48a7 수정: 메인에서 메인반픽 파생·회차별 고정 → 롤백됨)

### 5.3 updateCard 스타일 변경

- `updateCard`에서 `textContent`가 같아도 `style.borderColor`, `style.background` 등 매번 적용
- DOM 재계산·repaint 발생 → 시각적 깜빡임 가능

---

## 6. 룰 규칙 준수 여부

| 규칙 | 파일 | 준수 |
|------|------|------|
| **prediction_cache 우선** | `.cursor/rules/prediction-cache-architecture.mdc` | ✅ `/api/current-prediction`에서 prediction_cache 우선 |
| **예측기표 = 메인 예측기 픽 고정** | `.cursor/rules/token-hilo-analyzer-conventions.mdc` | ✅ 예측기픽 메뉴와 별개 |
| **_update_prediction_cache_from_db에서 load_results_data 금지** | prediction-cache-architecture | ✅ `_build_server_prediction_light`만 사용 |

---

## 7. 결론 및 권장 수정

### 근본 원인

**예측 폴링(200ms)이 light `prediction_cache`를 받아 `updatePredictionPicksCards`를 호출하면서, 이전에 full `sp`로 그려둔 4카드를 light 기준으로 덮어쓰는 것.**

### 권장 수정

1. **prediction 폴링 시 light sp 병합**
   - `sp`에 `main_reverse` 또는 `calc_best_type`이 없으면, `lastSpForCards`와 병합
   - `mergeSp = { ...lastSpForCards, ...sp }` → value/round/prob/color는 최신, 나머지는 full 유지

2. **또는 prediction 폴링에서 updatePredictionPicksCards 조건부 호출**
   - `sp`가 full이면 (main_reverse 또는 calc_best_type 존재) 호출
   - light이면 `updatePredictionPicksCards` 호출 생략

3. **(선택) 메인·메인반픽 회차별 고정**
   - 42f48a7과 유사하게, 메인 픽이 있으면 메인반픽을 클라이언트에서 파생
   - `lastMainByRound`로 회차별 고정

---

## 8. 참고 코드 위치

| 항목 | 위치 |
|------|------|
| `updatePredictionPicksCards` | app.py 10569~10660 |
| prediction 폴링 | app.py 10776~10800 |
| shape 폴링 | app.py 10803~10810 |
| `_build_server_prediction_light` | app.py 10878~10921 |
| `_build_results_payload_db_only` server_pred | app.py 10947~11080 |
| `/api/current-prediction` | app.py 11832~11845 |
