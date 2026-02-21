# 자동배팅기 배팅금액 규칙 (엄격·변형 금지)

자동배팅기는 **계산기 상단 배팅중 금액만** 사용한다. 다른 금액으로 덮어쓰거나, 마틴 승 후에도 마틴 금액이 배팅되면 안 된다.

---

## 1. 절대 규칙

| 규칙 | 내용 |
|------|------|
| **금액 출처** | 배팅금액은 **오직** 계산기 상단 배팅중(1열)과 동일한 값만 사용. 다른 출처 금액으로 덮어쓰기 금지. |
| **마틴 후** | 마틴이 끝난 후(승이 난 후)에는 **반드시 초기 금액**으로 배팅. 마틴 금액이 이어져서 배팅되면 안 됨. |

---

## 2. 매크로 금액 수신 (변형 금지)

### 2.1 유일한 출처

- **API**: `GET /api/current-pick-relay?calculator=N` → `suggested_amount`
- **푸시**: `POST /push-pick` → `suggested_amount`

매크로는 **이 두 경로에서 받은 `suggested_amount`만** 배팅에 사용한다.

### 2.2 금지 사항

| 금지 | 설명 |
|------|------|
| `_display_best_amount` 배팅 사용 | `_display_best_amount`는 **화면 표시용**만. `_run_bet`·`_do_bet`에 전달하는 금액으로 사용 금지. |
| 로컬 금액 계산 | 매크로는 금액을 직접 계산하지 않음. 서버/클라이언트에서 전달한 값만 사용. |
| 다른 API 필드 | `suggested_amount` 외 다른 필드(예: `probability`, `round` 등)로 금액 유도 금지. |

### 2.3 배팅 실행 경로

- `_on_poll_done` → `amt_val = suggested_amount` → `_run_bet(round_num, pick_color, amt_val)`
- `_on_push_pick_received` → `amt_val = suggested_amount` → `_run_bet(round_num, pick_color, amt_val)`
- `_do_bet(round_num, pick_color, amount_from_calc)` → `amount_from_calc`는 위에서 받은 `amt_val`만 전달.

---

## 3. 서버 금액 계산 (마틴 승 후 리셋)

### 3.1 `_calculate_calc_profit_server`

- **승(정/꺽 적중)**: `martingale_step = 0` → 다음 회차 `current_bet = martin_table[0]` = 초기 금액
- **패/조커**: `martingale_step += 1` → 마틴 다음 단계

### 3.2 `_server_calc_effective_pick_and_amount`

- `dummy = {'round': pr, 'actual': 'pending'}`
- `_calculate_calc_profit_server(c, dummy)` → `dummy['betAmount']` = 마틴 시뮬레이션 결과
- 반환 `amt` = `dummy['betAmount']` → `suggested_amount`로 relay/DB 전달

### 3.3 마틴 승 후 보정 (POST 수신 시)

`api_current_pick_relay` POST 핸들러:

- **조건**: 서버에 직전 회차 결과가 있고, `pending_bet_amount` > 0
- **동작**: 클라이언트가 보낸 `suggested_amount`를 **서버 `pending_bet_amount`로 덮어씀**
- **목적**: 클라이언트가 결과 반영 전에 마틴 금액을 보낸 경우, 서버가 초기 금액으로 보정

---

## 4. 마틴 끝 후 동일 금액 재송출 방지

### 4.1 `_bet_rounds_done`

- 배팅 완료한 회차를 저장
- **같은 회차**로 픽이 다시 오면(마틴 끝 후 서버가 같은 회차·초기금 재전송) **배팅 스킵**
- 목적: 마틴 끝 후 이전 회차에 대해 "초기 금액"이 다시 와도, 이미 배팅했으면 중복 배팅 방지

### 4.2 회차 역행 방지

- `_last_seen_round`: 이미 본 최고 회차
- `round_num < _last_seen_round` → 전회차 데이터 무시 (마틴 금액 오탐 방지)

---

## 5. 수정 시 체크리스트

| 항목 | 확인 |
|------|------|
| 매크로 `_run_bet` 호출 | `amount` 파라미터가 `suggested_amount`에서만 옴 |
| `_display_best_amount` | `_update_display` 표시용만. `_do_bet`에 전달되지 않음 |
| 서버 `_calculate_calc_profit_server` | 승 시 `martingale_step = 0` 유지 |
| POST `suggested_amount` 보정 | `has_prev_result` + `pending_bet_amount` 시 서버 금액 우선 |
| `_bet_rounds_done` | 같은 회차 재배팅 방지 유지 |

---

*이 문서는 배팅금액·마틴 리셋 규칙의 변형 금지 기준임.*
