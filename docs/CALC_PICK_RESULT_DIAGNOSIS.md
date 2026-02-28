# 계산기표 픽·결과값 간헐적 오등록 원인 분석

계산기표의 **픽(predicted)**과 **결과값(actual)**이 간헐적으로 잘못 등록되는 현상의 원인 후보를 정리한다.

---

## 1. 데이터 흐름 요약

| 단계 | 픽(predicted) 출처 | 결과(actual) 출처 |
|------|-------------------|-------------------|
| **서버** `_apply_results_to_calcs` | `pred_for_calc` (pending_predicted + 반픽/스마트반픽) | `_get_actual_for_round(results, pending_round)` |
| **클라이언트** apply 로직 | `predForRound` + reverse/smart_reverse | `graphValues[0]` (displayResults[0] 기준) |
| **병합** `_merge_calc_histories` | **클라이언트 predicted 우선** | 서버/클라이언트 중 더 완전한 쪽 |

---

## 2. 원인 후보

### 2.1 서버·클라이언트 픽(predicted) 불일치

**상황**: `_merge_calc_histories`에서 **클라이언트 predicted가 서버 pred_for_calc를 덮어쓴다**.

```python
# app.py 1232-1236행
for rn, pick in client_pick_by_round.items():
    if rn in by_round and pick.get('predicted') in ('정', '꺽'):
        by_round[rn]['predicted'] = pick['predicted']  # 클라이언트 우선
```

- 서버: `pred_for_calc` = `pending_predicted` + 반픽/스마트반픽
- 클라이언트: `predForRound`(예측픽) + reverse/smart_reverse

**불일치 가능성**:
- 승률방향(zone) 판정 차이: 서버 `_effective_win_rate_direction_zone` vs 클라이언트 `getEffectiveWinRateDirectionZone` — 데이터·타이밍 차이로 zone이 다르면 반픽 적용 여부가 달라짐
- 연패·15경기 승률 등: 서버는 `c['history']`, 클라이언트는 `predictionHistory`/로컬 — 동기화 지연 시 판정 차이
- `predForRound` 출처: `predictionHistory.find` / `getRoundPrediction` / `lastPrediction` — 어느 시점의 값이 쓰였는지에 따라 예측픽이 달라질 수 있음

**권장**: 완료 행(actual이 정/꺽/조커)은 **서버 pred_for_calc를 우선**하도록 merge 로직 수정 검토.

---

### 2.2 타이밍 레이스 (서버 apply vs 클라이언트 apply)

| 이벤트 | 주기 |
|--------|------|
| 서버 `_apply_results_to_calcs` | 0.05초 |
| 클라이언트 loadResults | 200ms |
| 클라이언트 loadCalcStateFromServer | 1~3초 |
| syncCalcHistoryFromServerPrediction | loadResults 시 |

**시나리오**:
1. T0: N회차 결과 수신 → 서버 apply → history에 (pred_for_calc, actual) 추가 → save_calc_state
2. T1: 클라이언트 loadResults → `syncCalcHistoryFromServerPrediction`으로 pending의 actual만 채움 (픽은 그대로)
3. T2: 클라이언트 apply(결과 카드 기준) → pending 행을 actual로 업데이트, pred는 `saved` 또는 재계산
4. T3: 클라이언트 saveCalcStateToServer → POST
5. T4: 서버 merge → **클라이언트 predicted로 덮어씀**

클라이언트가 T2에서 사용한 `pred`가 서버 `pred_for_calc`와 다르면, merge 후 잘못된 픽이 저장된다.

---

### 2.3 dedupeCalcHistoryByRound 병합 순서

```javascript
// app.py 7075-7076행
var merged = existing ? Object.assign({}, existing, h) : Object.assign({}, h);
// h가 나중에 오면 h의 predicted가 우선
```

- 같은 회차가 여러 번 들어오면 **마지막 항목**이 predicted를 덮어씀
- 배열 순서가 잘못되면(예: 서버 history + 클라이언트 push가 섞일 때) 잘못된 픽이 남을 수 있음

---

### 2.4 syncCalcHistoryFromServerPrediction과 prediction_history 지연

- `syncCalcHistoryFromServerPrediction`: `prediction_history`의 actual만 사용해 pending → 완료로 채움
- `prediction_history`는 서버 apply → `save_prediction_record` 후 DB에 저장
- `/api/results`와 apply가 비동기이므로, 클라이언트가 results를 받을 때 해당 회차가 `prediction_history`에 없을 수 있음
- 그 경우 sync가 되지 않고, 이후 `loadCalcStateFromServer`에서 서버 history를 받을 때까지 actual이 pending으로 남을 수 있음

---

### 2.5 결과(actual) 매칭 오류

- `graphValues[0]` = `displayResults[0]` 기준 정/꺽
- `displayResults` = `allResults.slice(0, 15)` — `allResults` 정렬이 잘못되면 `displayResults[0]`이 최신 회차가 아님
- 규칙: API 직전 `_sort_results_newest_first`, 클라이언트 `sortResultsNewestFirst`로 **맨 앞 = 최신** 보장 필요

---

### 2.6 서버 pending_predicted 출처

- `c.get('pending_predicted')`: calc_state에 저장된 값 (이전 apply 또는 클라이언트 POST)
- 없을 때: `get_stored_round_prediction`(round_predictions) — **예측픽**
- `round_predictions`는 배팅중 시점 POST. 클라이언트가 POST하지 않았거나 지연되면, 서버는 예측픽만 가지고 반픽/스마트반픽 적용

---

## 3. 점검 포인트

| 항목 | 확인 방법 |
|------|----------|
| merge 시 완료 행 픽 우선순위 | `_merge_calc_histories`에서 actual 완료 행은 서버 predicted 유지 옵션 검토 |
| 서버·클라이언트 스마트반픽 동기화 | `getSmartReverseDoRev` vs `_server_calc_effective_pick_and_amount` 입력값(blended, r15, zone 등) 일치 여부 |
| results 정렬 | `_sort_results_newest_first`, `sortResultsNewestFirst` 호출 위치·타이밍 |
| prediction_history 반영 시점 | loadResults 시 prediction_history에 해당 회차 포함 여부 |

---

## 4. 권장 대응 (우선순위)

### 4.1 merge 시 완료 행은 서버 픽 우선 ✅ 적용됨

- `_merge_calc_histories`에서 `actual`이 정/꺽/조커인 **완료 행**은 `client_pick_by_round`로 덮어쓰지 않고 서버 `predicted` 유지
- 클라이언트 픽 우선은 **pending 행**에만 적용

### 4.2 클라이언트 apply 시 saved(lastBetPickForRound) 강화

- pending → 완료 전환 시 `saved`(배팅 시점 픽)가 있으면 반드시 사용
- `saved` 없을 때만 재계산 — 재계산 로직이 서버와 동일한지 검증

### 4.3 디버그 로깅 추가

- merge 전후 `predicted` 불일치 시 로그
- 서버 `pred_for_calc` vs 클라이언트 POST `predicted` 비교 로그 (회차·계산기별)

---

## 5. 관련 코드 위치

| 기능 | 파일 | 행 |
|------|------|-----|
| `_merge_calc_histories` | app.py | 1192-1241 |
| `_apply_results_to_calcs` | app.py | 2009-2350 |
| 클라이언트 apply (결과 반영) | app.py | 7925-8180 |
| `dedupeCalcHistoryByRound` | app.py | 7067-7089 |
| `applyCalcsToState` | app.py | 7091-7160 |
| `syncCalcHistoryFromServerPrediction` | app.py | 7449-7472 |

---

*이 문서는 간헐적 픽·결과 오등록 원인 분석용이다. 수정 시 CALCULATOR_GUIDE.md, token-hilo-analyzer-conventions.mdc와 충돌하지 않도록 할 것.*
