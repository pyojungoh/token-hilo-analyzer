# 각 계산기 상단 배팅중 픽 색상 차이 — 원인 분석

## 1. 배팅중 픽 표시 구조

각 계산기 상단에는 다음이 있다:
- **배팅중 카드** (`calc-{id}-current-card`): `updateCalcStatus(id)`에서 설정
- **계산기표 1열(pending 행)**: `ensurePendingRowForRunningCalc` 및 테이블 렌더에서 사용

---

## 2. 배팅중 카드 색상 결정 흐름 (`updateCalcStatus` 10178~10295행)

```
curRound = lastPrediction.round (모든 계산기 공통)

1) serverPendingMatch && serverColorOk && serverPredOk
   → bettingText = pending_predicted, bettingIsRed = (pending_color === '빨강')
   → calcState[id].lastBetPickForRound 저장

2) saved (lastBetPickForRound, round 일치)
   → bettingText = saved.value, bettingIsRed = saved.isRed

3) else (재계산)
   → predictionText + reverse/smart_reverse/shape_prediction_reverse 적용
   → calcState[id].lastBetPickForRound 저장
```

---

## 3. 색상 차이 가능 원인

### 3.1 옵션 차이 (정상)

- 계산기마다 **반픽**, **스마트 반픽**, **모양 반픽** 등이 다르면 픽(정/꺽)이 달라지고, 따라서 색도 달라진다.
- 이 경우는 의도된 동작이다.

### 3.2 `savedBetPickByRound` 공유·덮어쓰기 (버그)

```javascript
// 10309~10310행: updateCalcStatus 내부
savedBetPickByRound[Number(lastPrediction.round)] = { value: bettingText, isRed: bettingIsRed };
```

- `savedBetPickByRound`는 **회차(round)만** 키로 사용하는 **전역 객체**다.
- `CALC_IDS.forEach(updateCalcStatus)` 순서대로 실행되면:
  - calc 1 → `savedBetPickByRound[round]` = calc 1 픽
  - calc 2 → **덮어씀** = calc 2 픽
  - calc 3 → **덮어씀** = calc 3 픽
- 최종적으로 `savedBetPickByRound[round]`에는 **마지막 계산기(calc 3)** 픽만 남는다.

**배팅중 카드**는 `lastBetPickForRound`만 사용하므로 이 영향은 받지 않는다.

하지만 **테이블 1열(pending 행)** 및 **결과 반영 시**에는:

```javascript
// 8061, 8131, 8227, 8284행 등
var saved = (calcState[id].lastBetPickForRound && ...) ? calcState[id].lastBetPickForRound 
            : savedBetPickByRound[Number(currentRoundNum)];
```

- `lastBetPickForRound`가 없거나 round 불일치면 `savedBetPickByRound`로 폴백한다.
- 이때 `savedBetPickByRound`는 항상 **마지막 계산기 픽**이므로, **calc 1, 2의 1열·결과 처리**가 calc 3 픽으로 잘못 표시될 수 있다.

### 3.3 `skipApplyForIds`로 인한 상태 불일치

- `saveCalcStateToServer({ skipApplyForIds: [2] })`처럼 특정 calc를 스킵하면, 해당 calc는 서버 응답을 적용하지 않는다.
- 스킵된 calc는 `pending_round`/`pending_color`가 오래된 값이거나 null일 수 있다.
- 그 결과:
  - calc 1: `serverPendingMatch` → 서버 픽 사용
  - calc 2: `serverPendingMatch` 실패 → `saved` 또는 `else` 경로 → 다른 색상

### 3.4 타이밍/동기화

- `loadCalcStateFromServer`: 약 600ms 주기
- `updateCalcStatus`: 약 200ms 주기
- 서버 `pending_round`/`pending_color`가 calc별로 아직 반영되지 않았을 때:
  - calc 1: 서버 값 반영됨 → `serverPendingMatch` 사용
  - calc 2: 아직 반영 안 됨 → `saved` 또는 `else` 사용 → 재계산 결과가 달라질 수 있음

---

## 4. 수정 제안

### 4.1 `savedBetPickByRound` 구조 변경 (권장)

- `savedBetPickByRound`를 **calc id별**로 분리:

```javascript
// 기존
var savedBetPickByRound = {};  // round → { value, isRed }

// 변경
var savedBetPickByRound = {};  // id → { round → { value, isRed } }
// 예: savedBetPickByRound[1][round], savedBetPickByRound[2][round], ...
```

- `updateCalcStatus`에서:
  - `savedBetPickByRound[id] = savedBetPickByRound[id] || {};`
  - `savedBetPickByRound[id][round] = { value, isRed };`
- 사용처(ensurePendingRowForRunningCalc, 테이블 렌더, 결과 반영)에서:
  - `savedBetPickByRound[id]?.[round]` 사용

---

## 5. 요약

| 원인 | 영향 | 대응 |
|------|------|------|
| 옵션 차이(반픽 등) | 정상 | 없음 |
| `savedBetPickByRound` 공유 | 1열·결과 처리 시 잘못된 calc 픽 사용 | calc id별 구조로 분리 |
| `skipApplyForIds` | 일부 calc만 서버 상태 미반영 | 스킵 시에도 pending 관련 필드는 적용 검토 |
| 타이밍 | 서버 반영 전 재계산 경로 사용 | 폴링/동기화 로직 검토 |

가장 확실한 버그는 **`savedBetPickByRound`가 calc id 없이 round만으로 공유**되어, 마지막 계산기 픽이 다른 계산기에 적용되는 것이다.
