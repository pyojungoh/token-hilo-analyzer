# 자동배팅기 금액 오류 원인 분석

## 요약

자동배팅기가 계산기 상단 "배팅중" 금액과 다른 금액을 받는 이유는 **여러 경로가 relay 캐시를 덮어쓰고**, **클라이언트 회차가 서버보다 앞설 때** 서버 보정이 적용되지 않기 때문입니다.

---

## 1. 금액이 흐르는 경로

| 경로 | 갱신 주기 | 금액 출처 |
|------|-----------|-----------|
| **클라이언트 POST** | 200ms (updateCalcStatus) | `getBetForRound(id, curRound)` |
| **스케줄러** `_update_relay_cache_for_running_calcs` | 0.2초 | `_server_calc_effective_pick_and_amount(c)` |
| **스케줄러** `_apply_results_to_calcs` | 0.2초 (결과 반영 시) | `_server_calc_effective_pick_and_amount(c)` |

세 경로 모두 `_update_current_pick_relay_cache()`를 호출해 **같은 캐시를 덮어씁니다.**

---

## 2. 충돌 시나리오

### 2.1 클라이언트 회차가 서버보다 앞설 때

**상황**: 새 회차 124가 나왔는데, 서버 calc에는 아직 `pending_round = 123`만 있음.

**원인**:
- `saveCalcStateToServer`가 **2.5초 throttle**이라 서버 DB에 최신 상태가 늦게 반영됨
- 서버 `pending_round`는 `get_calc_state()` → DB `calc_sessions`에서 읽음
- 클라이언트 `round`는 `lastPrediction.round` (예측 결과)

**흐름**:
1. 클라이언트: `lastPrediction.round = 124` → POST `round: 124, suggested_amount: 10000`
2. 서버 relay POST: `c.pending_round (123) != round_num (124)` → **서버 보정 조건 불충족**
3. 서버는 클라이언트 `suggested_amount`(10000)를 그대로 사용
4. 캐시에 `(124, 10000)` 저장됨

**결과**: 서버가 마틴 승 후 5000으로 리셋했는데, 클라이언트가 10000을 보내면 그대로 반영됨.

---

### 2.2 클라이언트 `getBetForRound`가 잘못된 금액을 계산할 때

**상황**: 클라이언트 history가 서버보다 뒤처져 있음.

**원인**:
- `getBetForRound`는 `pending_round === roundNum && pending_bet_amount > 0`이면 서버 값을 반환
- 그 외에는 **로컬 history로 마틴 시뮬레이션**
- 결과 반영 직후 서버는 history가 갱신된 상태인데, 클라이언트는 아직 응답을 받지 못함

**예시**:
- 서버: 직전 회차 승 → 마틴 리셋 → 5000
- 클라이언트: `saveCalcStateToServer` 응답 전 → history에 승 아직 없음 → 시뮬레이션 결과 10000 (연패 가정)

---

### 2.3 GET 시 캐시 우선으로 잘못된 금액을 반환할 때

**조건** (app.py 11586–11594):
```python
if cached_out and rv_cached >= rv_srv and rv_cached >= rv_db:
    amt = cached_out.get('suggested_amount')
    if server_out and rv_srv == rv_cached and server_out.get('suggested_amount') > 0:
        amt = server_out.get('suggested_amount')  # 서버 금액으로 override
```

**문제**: `rv_cached > rv_srv`일 때

- `rv_cached = 124` (클라이언트 POST로 캐시에 124)
- `rv_srv = 123` (서버 calc 아직 123)
- `rv_srv != rv_cached` → 서버 금액 override **미적용**
- `amt = cached_out.suggested_amount` → **캐시에 있는 잘못된 금액 사용**

---

### 2.4 스케줄러가 캐시를 덮어쓰는 타이밍

- `_scheduler_fetch_results`가 **0.2초마다** 실행
- `_apply_results_to_calcs` → relay 캐시 갱신 (서버 금액)
- `_update_relay_cache_for_running_calcs` → relay 캐시 갱신 (서버 금액)

**클라이언트 POST 직후**:
- 매크로가 0.2초 안에 GET을 하면, 클라이언트가 넣은 금액을 받을 수 있음
- 그 다음 스케줄러 실행에서 서버 금액으로 덮어쓰기

**덮어쓰기 순서**:
```
T=0.00: 클라이언트 POST (124, 10000) → 캐시 (124, 10000)
T=0.05: 매크로 GET → (124, 10000) → 잘못된 금액 배팅
T=0.20: 스케줄러 → 캐시 (124, 5000) → 이미 배팅은 끝남
```

---

## 3. 정리

| 원인 | 설명 |
|------|------|
| **회차 불일치** | 클라이언트 `round`는 `lastPrediction`, 서버 `pending_round`는 DB. 서버가 늦으면 124 vs 123으로 불일치. |
| **서버 보정 조건** | `c.pending_round == round_num`일 때만 서버 금액 사용. 불일치 시 클라이언트 금액이 그대로 사용됨. |
| **캐시 우선** | GET 시 `rv_cached >= rv_srv`이면 캐시 사용. `rv_cached > rv_srv`일 때는 서버 금액 override가 적용되지 않음. |
| **getBetForRound** | 클라이언트 history가 서버보다 뒤처지면 잘못된 금액 계산. |
| **saveCalcStateToServer 2.5초** | 서버 DB가 최대 2.5초 늦게 갱신되므로, 그 사이 클라이언트가 새 회차로 먼저 POST할 수 있음. |

---

## 4. 적용된 해결책 (절대 똑같이 + 속도·부하 최적화)

1. **GET 캐시 우선**: 캐시에 데이터가 있으면 **즉시 반환** (DB/계산 없음)
2. **캐시 없을 때만**: `get_calc_state` + `_server_calc_effective_pick_and_amount` 직접 계산 (스케줄러 미실행·최초 요청)
3. **POST relay 캐시 항상 갱신**: 배팅중 픽 들어오자마자 매크로에 전달 (규칙: `.cursor/rules/betting-in-display-to-macro-rule.mdc`)
   - 클라이언트 회차 > 서버 회차여도 캐시 갱신 (이전: 스킵 → 매크로가 새 회차 픽을 받지 못함)
   - 금액: 서버 `pending_round == round_num` and `srv_amt` 있으면 서버 값(마틴 보정), 아니면 클라이언트 `suggested_amount`
4. **정지 시 캐시 clear**: 스케줄러가 `running=False`인 calc의 캐시를 비워 오래된 값 반환 방지
