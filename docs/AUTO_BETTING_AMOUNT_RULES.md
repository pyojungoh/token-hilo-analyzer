# 자동배팅기 배팅금액 규칙 (엄격·변형 금지)

자동배팅기는 **금액을 매크로 내부에서만 계산**한다. 서버 suggested_amount 사용 금지.

---

## 1. 절대 규칙

| 규칙 | 내용 |
|------|------|
| **금액 출처** | 배팅금액은 **매크로 내부** 마틴 계산만 사용. round_actuals + _bet_history 기반. |
| **마틴 후** | 마틴이 끝난 후(승이 난 후)에는 **반드시 초기 금액**으로 배팅. |
| **직전 회차 결과 필수** | 직전 배팅 회차(_last_bet_round)의 결과가 round_actuals/history에 **없으면** 금액 계산·배팅 **절대 금지**. 결과 대기 후 다음 폴링에서 재시도. |

---

## 2. 매크로 금액 계산 (변형 금지)

### 2.1 유일한 출처

- **round_actuals**: `GET /api/macro-data` 또는 WebSocket `round_actuals_update` → 회차별 actual(정/꺽/조커)
- **_bet_history**: 배팅 시 round → predicted 저장
- **마틴 테이블**: 표마틴 9단계 (base × [1,2,3,6,11,21,40,76,120])

매크로는 **이 데이터로만** 계산한 금액을 배팅에 사용한다.

### 2.2 금지 사항

| 금지 | 설명 |
|------|------|
| 서버 suggested_amount 사용 | relay/WebSocket의 suggested_amount로 배팅 금지. |
| `_display_best_amount` 배팅 사용 | `_display_best_amount`는 **화면 표시용**만. |

### 2.3 배팅 실행 경로

- `_on_poll_done` / `_on_ws_pick_received` → `amt_val = _calc_bet_amount(round_num, predicted)` → `_run_bet` / `_do_bet`
- `_calc_bet_amount`: _bet_history + _round_actuals → calc_martingale_step_and_bet → amt

---

## 3. 매크로 내부 마틴 계산

### 3.1 `_calc_bet_amount`

- **_bet_history**: 배팅한 회차 → predicted(정/꺽) 저장
- **_round_actuals**: 회차별 actual(정/꺽/joker)
- **completed**: _bet_history에 있고 _round_actuals에 결과 있는 회차들, round 순 정렬
- **calc_martingale_step_and_bet(completed, martin_table)** → (step, next_bet_amount)

### 3.2 마틴 승 후

- **승**: step = 0 → next_bet = martin_table[0] = 초기 금액
- **패/조커**: step += 1 → next_bet = martin_table[step]

### 3.3 직전 회차 결과 검증 (변형 금지)

- **_last_bet_round**가 있으면, **round_actuals**에 `str(_last_bet_round)` 키가 있고 `actual`이 정/꺽/joker/조커일 때만 금액 계산.
- 조건 미충족 시 `_calc_bet_amount`는 **0 반환** → 배팅 스킵. 로그: `[금액검증] N회 결과 대기 — N+1회 배팅 스킵 (마틴 리셋 보장)`.
- **목적**: 승 후 round_actuals 지연으로 "이전 금액(8만원) 재배팅" 방지. 결과 수신될 때까지 스킵 후 다음 폴링에서 정확한 초기금으로 배팅.

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
| 매크로 `_run_bet` 호출 | `amount` 파라미터가 `_calc_bet_amount`에서만 옴 |
| `_display_best_amount` | `_update_display` 표시용만. `_do_bet`에 전달되지 않음 |
| `_calc_bet_amount` | _bet_history + _round_actuals 기반. suggested_amount 사용 금지 |
| `_bet_history` 저장 | 배팅 성공 시 round → predicted 저장 |
| `_bet_rounds_done` | 같은 회차 재배팅 방지 유지 |

---

*이 문서는 배팅금액·마틴 리셋 규칙의 변형 금지 기준임.*
