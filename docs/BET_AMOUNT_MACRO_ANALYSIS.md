# 분석기 → 매크로 배팅금액 잘못 전달 원인 분석

## 흐름 요약

1. **금액이 쓰이는 곳**: DB `current_pick` 테이블의 `suggested_amount` (계산기별 id=1,2,3).
2. **매크로**: `GET /api/current-pick?calculator=N`으로 폴링해 `suggested_amount` 사용.
3. **금액을 쓰는 두 경로**:
   - **서버**: 스케줄러 `_apply_results_to_calcs` → `_push_current_pick_from_calc` → `_server_calc_effective_pick_and_amount(c)` → `bet_int.set_current_pick(..., suggested_amount=...)`
   - **클라이언트**: `updateCalcStatus` 등에서 `getBetForRound(id, round)` → `postCurrentPickIfChanged(..., suggested_amount)` → `POST /api/current-pick`

---

## 원인 1: 스케줄러와 POST의 타이밍(레이스)

**순서:**

1. 스케줄러가 회차 반영 후 **메모리상** `c`를 갱신 (pending_round, pending_bet_amount, history 등).
2. `_push_current_pick_from_calc(c)` 호출 → **올바른 금액**으로 `current_pick` DB에 즉시 기록.
3. 루프 끝에서 `save_calc_state(session_id, state)` 호출 → **그 다음**에 `calc_sessions` DB에 반영.

**문제:** 2와 3 사이에 클라이언트가 `POST /api/current-pick`을 보내면, 서버는 `get_calc_state('default')`로 **아직 갱신 전** `calc_sessions`를 읽는다.  
→ `c.pending_round`는 **이전 회차** 그대로.  
→ 클라이언트가 보낸 `round_num`(새 회차)과 `pr != rn` 이라서, POST 핸들러가 “회차 일치 시 서버 금액으로 덮어쓰기”를 **하지 않음**.  
→ 클라이언트가 보낸 (틀릴 수 있는) 금액이 그대로 저장되고, 매크로가 잘못된 금액을 받음.

**발생 조건:** 결과 반영 직후, 클라이언트가 곧바로 POST하는 짧은 구간에서만 발생 → “가끔” 잘못 보내는 현상과 일치.

---

## 원인 2: 클라이언트 금액 계산이 서버와 어긋남

**클라이언트 `getBetForRound(id, roundNum)`:**

- `pending_round === roundNum` 이고 `pending_bet_amount > 0` 이면 → **서버에서 받은 값** 그대로 사용 (의도적으로 일치 유지).
- 그렇지 않으면 → **로컬** history 기준으로 마틴게일 시뮬레이션해 “다음 회차 배팅금” 계산.

**문제:**  
서버가 이미 다음 회차로 넘어가서 `pending_round`/`pending_bet_amount`를 갱신했는데, 클라이언트는 아직 폴링/적용이 안 됐으면 `pending_round`는 예전 회차다.  
→ `roundNum`(새 회차)과 불일치 → 로컬 시뮬레이션 사용.  
→ 클라이언트 history가 서버보다 한 회차 늦거나, 병합/순서가 다르면 **다른 마틴 단계**가 나와 잘못된 금액을 POST할 수 있음.

---

## 원인 3: POST 시 서버 보정이 “회차 일치”에만 의존

**현재 로직 (app.py POST /api/current-pick):**

```python
if c.get('paused'):
    suggested_amount = 0
elif round_num is not None and c.get('pending_round') is not None:
    if pr == rn:  # 서버 pending_round와 클라이언트 round_num 일치할 때만
        _, server_amt = _server_calc_effective_pick_and_amount(c)
        suggested_amount = server_amt
```

- `pr != rn` 이면 클라이언트가 보낸 금액을 **무조건 신뢰**.
- 원인 1 때문에 그 구간에선 서버 쪽 `c`가 아직 이전 회차라 `pr != rn`이 되고, 보정이 빠짐.

---

## 원인 4: 다중 탭/기기

- `current_pick` POST는 **session_id를 보내지 않음** → 서버는 항상 `get_calc_state('default')` 사용.
- 여러 탭이 같은 `default` 세션을 쓰면, **마지막에 저장한 탭**의 calc 상태가 서버에 있음.
- **마지막에 POST한 탭**의 금액이 `current_pick`에 저장됨.
- 스테이트는 탭 A, POST는 탭 B가 나중에 보내면 → 탭 B 금액이 들어가고, 탭 B가 구식이면 잘못된 금액이 매크로에 전달됨.

---

## 권장 대응

1. **스케줄러 순서 변경 (우선 권장)**  
   `save_calc_state(session_id, state)`를 **해당 세션의 모든 계산기 회차 반영이 끝난 직후**, 그리고 **`_push_current_pick_from_calc` 호출 이전**으로 옮기거나,  
   최소한 **`_push_current_pick_from_calc`를 호출하기 전에** 그 세션에 대해 `save_calc_state`를 한 번 호출해 두기.  
   → POST가 들어와도 `get_calc_state('default')`가 이미 새 pending_round/금액을 갖고 있어서 `pr == rn` 보정이 동작하게 함.

2. **POST 시 회차 불일치 시에도 서버 금액 우선 (선택)**  
   `pr != rn` 이더라도, “같은 계산기이고 서버에 pending_round가 설정되어 있으면” 서버 금액으로 덮어쓰는 정책을 검토.  
   (같은 회차가 아닌데 덮어쓰면 논리적으로 맞지 않을 수 있어, “서버 pending_round 기준 금액만 저장” 등 조건을 넣어야 함.)

3. **클라이언트**  
   - calc 폴링/저장 응답에서 `pending_round`/`pending_bet_amount`를 최대한 빨리 반영하고,  
   - `getBetForRound`에서 `pending_round === roundNum`일 때만 금액을 보내는 식으로 “서버와 일치할 때만 POST 금액 사용”을 강화하는 방안 검토.

4. **다중 탭**  
   - 가능하면 계산기/배팅 연동은 한 탭만 사용하도록 안내하거나,  
   - POST body에 `session_id`를 넣어 서버가 해당 세션 기준으로만 보정하도록 할 수 있음.

위 1번(스케줄러에서 save 순서 조정)만 적용해도 “가끔 잘못 보내는” 현상 상당 부분이 줄어들 가능성이 큼.
