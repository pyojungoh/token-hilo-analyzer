# 마틴게일 금액 밀림 원인 분석 (2만원 차례에 1만원 배팅 등)

## 증상

- **2만원** 배팅해야 할 차례에 **1만원** 배팅
- 연패가 이어지면 **4만원** 차례에 **2만원**, **8만원** 차례에 **4만원** 등 한 단계씩 밀림

---

## 원인 1: 클라이언트 히스토리 지연 (가장 유력)

### 흐름

1. **N회차 패** 발생 → 서버 apply가 history에 반영, `pending_bet_amount = 2만원`(마틴 2단계)
2. **클라이언트**는 아직 N회차 actual을 모름 (히스토리에 `pending` 유지)
3. `getBetForRound(N+1)` 호출 시:
   - `hist` = N회차 **미포함** (actual이 pending이면 필터에서 제외)
   - 시뮬레이션: N-1회차까지만 반영 → 1승 0패 → **1만원** 반환
4. 클라이언트가 **1만원** POST → relay 캐시 1만원 → 매크로 1만원 배팅

### 타이밍

| 이벤트 | 주기 |
|--------|------|
| 서버 apply | 0.2초 |
| 클라이언트 loadResults | 200ms |
| 클라이언트 loadCalcStateFromServer | **3000ms** |
| prediction poll → updateCalcStatus | 200ms |

**핵심**: `predictionPollInterval`(200ms)이 `updateCalcStatus` → `postCurrentPickIfChanged`를 호출하지만, **calc state(history)는 loadCalcStateFromServer(3초)에서만** 갱신됨.  
→ prediction poll 시점에 클라이언트 history가 **최대 3초 뒤처질 수 있음**.

### syncCalcHistoryFromServerPrediction

`loadResults` 응답의 `prediction_history`로 로컬 calc history의 `pending` → `actual` 보정.  
하지만:

- `prediction_history`는 서버 apply → `save_prediction_record` 후 DB에 저장
- `/api/results`와 apply가 **비동기**라, 클라이언트가 results를 받을 때 prediction_history에 N회차가 아직 없을 수 있음
- 그 경우 sync가 안 되고, `getBetForRound`는 구식 history로 시뮬레이션 → 1만원

---

## 원인 2: pending_bet_amount 수신 지연

`getBetForRound`는 `pending_round === roundNum`이고 `pending_bet_amount > 0`이면  
`Math.max(simulated, pending_bet_amount)`를 사용 (서버 값 우선).

- `pending_bet_amount`는 **loadCalcStateFromServer**에서만 설정
- loadCalcStateFromServer 주기: **3초**
- prediction poll(200ms)으로 updateCalcStatus가 먼저 실행되면, `pending_bet_amount`가 null이거나 예전 값
- → 로컬 시뮬레이션만 사용 → 1만원

---

## 원인 3: relay POST에 서버 보정 없음

`/api/current-pick-relay` POST:

```python
# 클라이언트(디스플레이) 값 우선 — 상단이 정확하므로 round·픽·금액 그대로 사용
_write_macro_pick_transmit(calculator_id, round_num, pc_stored, amt_int, ...)
_update_current_pick_relay_cache(calculator_id, round_num, pc_stored, amt_int, ...)
```

- 클라이언트가 보낸 금액을 **그대로** relay에 반영
- 서버가 `get_calc_state`로 올바른 금액을 계산해도, **클라이언트 값으로 덮어쓰지 않음**
- `/api/current-pick`(relay 아님)에는 `pr == rn`일 때 서버 금액으로 보정하는 로직이 있으나, relay에는 없음

---

## 원인 4: 스케줄러 0.5초 스킵

```python
if time.time() - _last_post_time_per_calc.get(calc_id, 0) < 0.5:
    return  # 클라이언트 값 유지
```

- 클라이언트 POST 후 **0.5초** 동안 스케줄러가 relay를 갱신하지 않음
- 클라이언트가 1만원(잘못된 값) POST → 0.5초 동안 서버의 2만원이 relay에 반영되지 않음
- 매크로가 그 사이에 GET하면 1만원 수신 → 1만원 배팅

---

## 원인 5: 2회 연속 확인의 역효과

매크로는 `(round, pick_color, amount)`가 **2회 연속 동일**할 때만 배팅.

- 1만원 2회 → 1만원 배팅 (잘못된 값이 먼저 2번 오면 그대로 배팅)
- 2만원이 나중에 와도, 이미 1만원으로 배팅 완료

---

## 요약: 밀림 발생 시나리오

1. N회차 패 → 서버: pending_bet_amount = 2만원
2. 클라이언트: history에 N회차 actual 없음, pending_bet_amount도 3초 주기라 아직 없음
3. getBetForRound(N+1) → 1만원
4. postCurrentPickIfChanged(1만원) → relay 1만원
5. 0.5초 동안 스케줄러 relay 갱신 스킵
6. 매크로: 1만원 2회 수신 → 1만원 배팅
7. 다음 회차부터도 한 단계씩 밀림 유지

---

## 권장 대응 (우선순위)

### 1. relay POST 시 서버 금액 보정 (우선)

- 클라이언트가 round, pick, amount를 보낼 때
- 서버 `get_calc_state`로 `_get_calc_row1_bundle` 호출
- `server_amt`와 `client_amt` 비교
- **client_amt < server_amt** (클라이언트가 한 단계 낮음)이면 **server_amt 사용**
- 마틴 승 후(client > server) 보정은 기존 로직 유지

### 2. loadCalcStateFromServer 주기 단축

- 계산기 실행 중: 3000ms → 1000ms 또는 500ms
- pending_bet_amount를 더 자주 받아 getBetForRound가 서버 값을 쓰도록

### 3. loadResults 시 calc-state 동시 요청

- loadResults 직후 같은 탭에서 `/api/calc-state` 한 번 더 호출
- 또는 results API에 calc state 일부(pending_round, pending_bet_amount) 포함

### 4. 스케줄러 스킵 시간 단축

- 0.5초 → 0.2초
- 클라이언트가 잘못 보내도 서버가 더 빨리 보정 가능

---

## 적용 완료 (2025-02)

- **1. relay POST 시 서버 금액 보정**: `api_current_pick_relay` POST에서 `round_num`이 있을 때 `_get_calc_row1_bundle`로 서버 금액 조회. `server_amt > client_amt`이면 `server_amt` 사용.
- **2. loadCalcStateFromServer 주기 단축**: 3000ms → 1000ms (탭 visible 시).
- **3. 스케줄러 스킵 시간 단축**: 0.5초 → 0.2초

---

## 관련 문서

- `docs/BET_AMOUNT_MACRO_ANALYSIS.md`
- `docs/AMOUNT_VALIDATION_PROPOSAL.md`
- `docs/AUTO_BETTING_AMOUNT_RULES.md`
