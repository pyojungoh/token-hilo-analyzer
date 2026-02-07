# 멈춤(승률) + 마틴 가이드

계산기에서 **「승률 이하·연패 시 배팅멈춤」** 옵션과 **마틴**을 같이 쓸 때의 동작 규칙이다. 수정·추가 시 이 규칙을 유지할 것.

**기준**: 멈춤/재개 판단은 모두 **최근 15회 승률** 기준이다. (`getCalcRecent15WinRate` = 완료된 회차 중 마지막 15개 승률.)

---

## 1. 멈춤 옵션 기본 동작

- **조건**: 최근 15게임 승률이 사용자가 지정한 값 **이하**가 되면 발동.
- **동작**: 배팅을 멈추고, 베팅금액을 넣지 않음(픽만 유지, 금액 0 / `no_bet`).
- **재개**: 15게임 승률이 다시 지정값 **초과**가 되면 `paused = false`로 풀리고, 그다음부터 베팅금액을 넣어서 배팅 재개.

---

## 2. 마틴 켜져 있을 때

- **원칙**: 승률이 이미 지정값보다 낮아도, **마틴 중**(연패 중)이면 멈춤 검사를 하지 않는다. 마틴이 끝날 때까지 배팅 유지.
- **멈춤 검사 시점**: **연패 중 승**이 나온 순간 = 마틴 한 사이클이 끝난 직후(승을 하자마자)에만 멈춤 조건을 검사한다.
  - 연패 중 승 = 직전 회차는 **패**, 이번 회차는 **승** (조커 제외 기준).
  - 이때만 `checkPauseAfterWin`에서 15게임 승률을 보고, 지정값 이하이면 그 시점부터 멈춤(베팅금액 미전송).

---

## 3. 정리

| 상황 | 멈춤 검사 |
|------|-----------|
| 마틴 OFF | 승 나올 때마다 15게임 승률 검사 → 이하이면 멈춤 |
| 마틴 ON, 연승 중 승 | 검사 안 함 (멈춤 미적용) |
| 마틴 ON, 연패 중 승 | **이 순간만** 15게임 승률 검사 → 이하이면 멈춤 |

재개는 마틴 여부와 관계없이 동일: `updateCalcStatus`에서 `paused`이고 멈춤 옵션 켜져 있으면 `rate15 > thrPause`일 때 `paused = false`.

---

## 4. 멈춤 중 히스토리(손익 일치)

- **원칙**: 베팅금액이 안 들어간 시점(멈춤) 이후로 들어오는 모든 회차는 히스토리에도 `no_bet: true`, `betAmount: 0`으로 넣어야 손익 계산이 맞다.
- **적용**: 결과를 반영할 때( pending → actual 또는 새 행 push ) 항상 **현재 `paused`** 를 반영한다.
  - `paused`이거나 해당 행이 이미 `no_bet`이면 → `no_bet: true`, `betAmount: 0` 으로 저장.
  - 정/꺽 결과 반영 두 경로 + 조커 반영 두 경로, 총 네 곳에서 동일하게 적용.

---

## 5. 코드 위치 (app.py)

- **멈춤 검사**: `checkPauseAfterWin(id)`
  - 멈춤 옵션 꺼져 있으면 return.
  - 마틴 ON이면 `completed`에서 마지막 2개로 연패중승 판별 (`lastIsWin && prevWasLoss`), 아니면 return.
  - `getCalcRecent15WinRate(id)`, `pause_win_rate_threshold`로 비교 후 `rate15 <= thr`이면 `paused = true`, pending에 `no_bet`/금액 0 처리.
- **재개**: `updateCalcStatus(id)` 내부
  - `state.paused && state.pause_low_win_rate_enabled`일 때 `rate15 > thrPause`면 `state.paused = false`.
- **15게임 승률**: `getCalcRecent15WinRate(id)` — `calcState[id].history`의 완료된 회차 중 마지막 15개로 승률 계산.
- **멈춤 중 no_bet 유지**: 결과 반영 시 (정/꺽 두 경로, 조커 두 경로) pending 행을 actual로 바꿀 때와 새 행을 push할 때, `calcState[id].paused` 또는 해당 행의 기존 `no_bet`이면 `no_bet: true`, `betAmount: 0` 으로 설정해 손익이 맞도록 함.

---

## 6. 새로고침 후에도 멈춤 유지 (계산기 1, 2, 3 동일)

- **문제**: 새로고침하면 서버/로컬에서 불러온 히스토리에서 `no_bet`이 빠지거나 `betAmount`가 채워져, 멈춤이었던 회차가 배팅한 것처럼 보이면 손익 계산이 틀어진다.
- **원칙**: 로드·저장·API 응답 모두에서 **no_bet ↔ betAmount 0** 를 한 쌍으로 유지한다.
  - `no_bet === true` 이면 반드시 `betAmount === 0`.
  - `betAmount === 0` 이거나 없으면 `no_bet === true` 로 간주하고 저장/표시 시에도 그렇게 맞춘다.
- **적용 (계산기 1, 2, 3 동일)**  
  - **클라이언트 로드** (`applyCalcsToState`): 서버에서 받은 `c.history`를 쓸 때, 각 항목에 대해 `no_bet === true` → `betAmount = 0`, `betAmount === 0`/undefined/null → `no_bet = true` 로 정규화한 뒤 `calcState[id].history`에 넣는다.  
  - **서버 GET** (`/api/calc-state`): 응답 내 `calcs['1'|'2'|'3'].history` 각 항목을 같은 규칙으로 정규화한 뒤 반환.  
  - **서버 POST** (`/api/calc-state`): 저장할 `use_history` 각 항목을 같은 규칙으로 정규화한 뒤 저장.  
  이렇게 하면 새로고침 후에도 멈춤 회차는 계속 `no_bet`/금액 0으로만 표시·계산된다.

이 가이드를 참고하면 멈춤/마틴 로직을 수정할 때 일관되게 유지할 수 있다.
