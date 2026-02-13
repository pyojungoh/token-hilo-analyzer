# 자동배팅기 금액 출처 분석

## 사용자 보고
> "자동배팅기로 오는 값이 계산기표 헤더값 밑 1열이 아니고 2열부터 오고 있어. 아마도 1열에는 배팅중 픽이 들어오기 때문에 저장하는 곳이 다른 곳 같아."

## 계산기 표 구조

| 구분 | 설명 | 배팅금액 출처 |
|------|------|---------------|
| **헤더** | 보유자산 \| 순익 \| **배팅중** | `getCalcResult(id).currentBet` |
| **1열(1행)** | 배팅중 회차 (pending) | `nextRoundBet` 또는 `getBetForRound(id, rn)` |
| **2열(2행)~** | 완료된 회차들 | `roundToBetProfit[rn].betAmount` (마틴 시뮬레이션) |

## 매크로가 받는 값의 흐름

### 1. 클라이언트 POST 경로
- **위치**: `updateCalcStatus(id)` (line ~6935-6938)
- **금액**: `betAmt = getCalcResult(id).currentBet`
- **전송**: `postCurrentPickIfChanged(..., suggested_amount: betAmt)`

### 2. 서버 저장 경로
- **POST 수신**: `suggested_amount`를 DB `current_pick.suggested_amount`에 저장
- **스케줄러**: `_push_current_pick_from_calc` → `_server_calc_effective_pick_and_amount` → `suggested_amount` 저장

### 3. 매크로 GET
- `GET /api/current-pick?calculator=N` → DB의 `suggested_amount` 반환

## 출처 비교

| 구분 | 1열(배팅중 행) | 헤더 "배팅중" | POST → 매크로 |
|------|----------------|---------------|---------------|
| **출처** | `nextRoundBet` 또는 `getBetForRound` | `getCalcResult.currentBet` | `getCalcResult.currentBet` |
| **동일 여부** | 1열과 헤더는 **다를 수 있음** | 헤더 = POST | ✓ |

## 잠재적 불일치

1. **1열 vs 헤더**
   - 1열(대기 행): `isNextRoundAfterCompleted ? nextRoundBet : getBetForRound(id, rn)`
   - 헤더: `getCalcResult(id).currentBet`
   - `nextRoundBet`와 `getCalcResult.currentBet`는 **같은 시뮬레이션**에서 나오므로 이론상 동일
   - `getBetForRound`는 `pending_round`/`pending_bet_amount`가 있으면 **서버 값**을 반환 → 1열만 다른 값이 나올 수 있음

2. **getCalcResult vs getBetForRound**
   - `getCalcResult`: history에서 pending 제외 후 시뮬레이션 → `currentBet`
   - `getBetForRound`: `pending_round === roundNum && pending_bet_amount > 0`이면 **서버 값** 반환, 아니면 시뮬레이션
   - 서버와 클라이언트 history가 어긋나면 두 함수 결과가 달라질 수 있음

3. **표 2열(완료 행)**
   - `roundToBetProfit[rn].betAmount` 사용 (마틴 시뮬레이션)
   - 매크로에는 **사용되지 않음** (매크로는 배팅중 금액만 필요)

## 결론

- **매크로 금액**은 `getCalcResult(id).currentBet` → POST → DB를 거쳐 전달됨.
- **헤더 "배팅중"**도 `getCalcResult(id).currentBet` 사용 → **동일 출처**.
- **1열(배팅중 행)**은 `nextRoundBet` 또는 `getBetForRound` 사용 → 조건에 따라 헤더/POST와 **다를 수 있음**.

따라서 "2열부터 오고 있다"는 현상은:
1. **1열과 헤더가 다른 경우**: 1열은 `getBetForRound`(서버 값), 매크로는 `getCalcResult`(로컬 시뮬레이션) 사용
2. **회차/동기화 지연**: 클라이언트 history가 서버보다 한 단계 늦을 때, `getCalcResult`가 **이전 회차** 배팅금을 반환할 수 있음 (그 금액이 표 2열과 일치)

## 권장 검토 사항

1. POST 시 `getCalcResult.currentBet` 대신 **1열과 동일**하게 `getBetForRound(id, lastPrediction.round)` 사용 검토
2. `pending_bet_amount`가 있을 때 `getCalcResult`도 이 값을 반영하도록 수정 검토
3. 서버/클라이언트 history 동기화 타이밍 재확인
