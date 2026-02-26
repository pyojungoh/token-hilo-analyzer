# 배팅금액 불안정 — 원인 분석 및 해결안

## 현상

- **디스플레이 금액**: 잘못 옴
- **실제 배팅 금액**: 한 단계 낮게 배팅 (예: 1만원이어야 하는데 5천원 배팅)

---

## 원인 분석

### 1. 매크로 `_display_amount_locked`의 역효과

```
[현재 로직] 같은 회차에 "먼저 받은 금액"을 고정
- 5천 → 1만 덮어쓰기 방지를 위해 도입
- 문제: 올바른 값(1만)이 나중에 오면, 이미 5천(잘못된 값)을 lock 해서 1만을 무시
```

**시나리오 (한 단계 낮게 배팅)**  
1. 마틴 1패 후 → 다음 회차 1만원이 맞음  
2. 서버가 아직 결과 반영 전에 5천원 emit (이전 회차 기준)  
3. 매크로: 5천원 lock  
4. 서버가 결과 반영 후 1만원 emit  
5. 매크로: lock 때문에 5천원 유지 → 5천원 배팅 (한 단계 낮음)

### 2. 두 출처의 타이밍 차이

| 출처 | 갱신 시점 |
|------|-----------|
| **1행** (표 1행) | 서버 `_apply_results_to_calcs` → `_push_current_pick_from_calc` (0.3초 주기) |
| **상단** (배팅중) | 클라이언트 `getCalcResult.currentBet` → POST (25ms 주기) |

- 서버가 결과를 먼저 반영하면 1행이 먼저 맞음  
- 클라이언트가 늦게 반영되면 상단이 잘못된 값을 보냄  
- 반대로 클라이언트가 먼저 맞고 서버가 늦을 수도 있음  

### 3. 현재 구조의 한계

- 서버 POST 시 1행만 사용 → 클라이언트 상단 값은 무시
- 매크로는 lock으로 “첫 값”만 사용 → 나중에 오는 올바른 값을 버림
- 1행과 상단이 같은지 검증하는 단계가 없음

---

## 해결안: 이중 검증 (1행 vs 상단)

### 아이디어

**1행과 상단 금액이 같을 때만** relay/배팅에 반영한다.

- 두 값이 같으면 → 동기화된 상태 → 신뢰 가능
- 두 값이 다르면 → 한쪽이 지연/오류 → 배팅하지 않음

### 데이터 흐름

```
[클라이언트]
  - 1행: getBetForRound(id, pending_round)  → amount_row1
  - 상단: getCalcResult(id).currentBet      → amount_header
  - POST 시 둘 다 전송: { amount_row1, amount_header, round, pick }

[서버]
  - 1행: _get_calc_row1_bundle(c)           → amount_row1_srv
  - amount_row1_srv == amount_header (클라이언트 상단)?
  - 또는 amount_row1_srv == amount_row1 (클라이언트 1행)?
  → 둘 중 하나라도 일치하면 검증 통과

[검증 통과 시]
  - relay 캐시에 amount_row1_srv (서버 1행) 반영
  - WebSocket emit

[검증 실패 시]
  - relay 갱신 안 함 (기존 값 유지)
  - 또는 서버 1행만 사용 (서버 우선)
```

### 구현 옵션

#### 옵션 A: 서버에서 이중 검증 (권장)

1. **클라이언트 POST**  
   - `amount_row1`: 표 1행 금액 (`getBetForRound`)  
   - `amount_header`: 상단 금액 (`getCalcResult.currentBet`)  
   - `round`, `pick` 등 기존 필드 유지  

2. **서버**  
   - `amount_row1_srv` = `_get_calc_row1_bundle` 금액  
   - 검증: `amount_row1_srv == amount_header`  
   - 일치 시: relay에 `amount_row1_srv` 반영  
   - 불일치 시: relay 갱신 안 함 (또는 서버 1행만 사용 + 로그)  

3. **매크로**  
   - `_display_amount_locked` 제거  
   - 받은 `suggested_amount`를 그대로 표시·배팅에 사용  

#### 옵션 B: 매크로에서 이중 검증

1. **페이로드 확장**  
   - `suggested_amount` (서버 1행)  
   - `amount_header` (클라이언트 상단)  

2. **매크로**  
   - `suggested_amount == amount_header`일 때만 배팅  
   - 불일치 시 대기  

3. **단점**  
   - WebSocket/GET 응답 구조 변경 필요  
   - 클라이언트가 항상 `amount_header`를 같이 보내야 함  

#### 옵션 C: lock 제거 + 최신값 사용 (단순)

1. **매크로**  
   - `_display_amount_locked` 제거  
   - 매번 들어오는 `suggested_amount`를 그대로 사용  

2. **2회 확인**  
   - 같은 (round, pick, amount)가 2번 연속 올 때만 배팅  

3. **효과**  
   - 5천 → 1만 순서: 5천 2번 → 5천 배팅  
   - 1만 → 1만 순서: 1만 2번 → 1만 배팅  
   - 5천 → 1만 순서: 5천 1번, 1만 1번 → key 변경으로 count 리셋 → 1만 2번 대기 → 1만 배팅  

4. **리스크**  
   - 5천이 2번 먼저 오면 5천 배팅 (잘못된 값 2회)  
   - 서버가 올바른 값을 빨리 보내는지가 중요  

---

## 권장안: 옵션 A + C 조합

1. **서버**: 1행 vs 상단 이중 검증  
   - 클라이언트가 `amount_header` 전송  
   - `amount_row1_srv == amount_header`일 때만 relay 갱신  

2. **매크로**: `_display_amount_locked` 제거  
   - 검증된 값만 relay에 들어가므로 lock 불필요  
   - 받은 값을 그대로 표시·배팅  

3. **폴백**  
   - 검증 실패 시: 서버 1행만 사용 (서버 우선)  
   - 또는: 기존 relay 유지  

---

## 구현 체크리스트 (완료)

- [x] 클라이언트: POST에 `suggested_amount`(상단) 이미 전송 중  
- [x] 서버: `amount_row1_srv == amount_header` 검증 — 불일치 시 relay 갱신 안 함  
- [x] 서버: 상단 0이면 서버 1행 사용 (폴백)  
- [x] 매크로: `_display_amount_locked` 제거  
- [x] 매크로: `suggested_amount` 그대로 사용  
