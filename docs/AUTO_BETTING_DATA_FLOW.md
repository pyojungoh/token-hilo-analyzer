# 자동배팅기 정보 전송 방식

분석기 → 자동배팅기로 픽·회차·금액이 전달되는 전체 흐름과 출처 규칙.

---

## 1. 전체 구조

```
[분석기 결과 페이지]                    [자동배팅기]
       │                                      │
       │ postCurrentPickIfChanged             │
       │ POST /api/current-pick-relay         │
       ├─────────────────────────────────────►│
       │   { round, pickColor, suggested_amount }  │
       │                                      │
       │                    [중간페이지 /practice] │
       │                    GET current-pick-relay │
       │                    (100ms 폴링)       │
       │                    ──► 푸시 URL 있으면  │
       │                    POST /push-pick ──► 매크로
       │                                      │
       │                    [또는 매크로 직접 폴링] │
       │                    GET current-pick-relay │
       │                    (폴링)             │
```

---

## 2. 배팅중 금액 출처 (절대 규칙)

**배팅중 금액 = 계산기 표 1열(배팅중 행)과 동일한 값만 사용.**

| 구분 | 출처 | 설명 |
|------|------|------|
| **계산기 표 1열** | `getBetForRound(id, pending_round)` | pending_bet_amount 있으면 서버 값, 없으면 마틴 시뮬레이션 |
| **헤더 "배팅중"** | `getCalcResult(id).currentBet` | 마틴 시뮬레이션 |
| **POST suggested_amount** | `getBetForRound(id, curRound)` | 1열과 동일 출처 (코드상 이미 적용) |
| **서버 저장** | 클라이언트 값. 마틴 끝 후 직전 회차 결과 있으면 서버 `pending_bet_amount` 우선 | 잘못된 금액 방지 |

---

## 3. API 상세

### 3.1 POST `/api/current-pick-relay`

**호출처**: 분석기 결과 페이지 `postCurrentPickIfChanged`

**페이로드**:
```json
{
  "calculator": 1,
  "pickColor": "RED",
  "round": 11423052,
  "suggested_amount": 10000,
  "running": true
}
```

**규칙**:
- `suggested_amount`: 클라이언트 값 사용. **단, 마틴 끝 후**: 서버에 직전 회차 결과가 있으면 `pending_bet_amount` 우선 (클라이언트가 결과 반영 전에 보낸 잘못된 금액 방지)
- relay 캐시: POST 시 즉시 갱신 (매크로가 DB 쓰기 대기 없이 바로 픽 수신)

### 3.2 GET `/api/current-pick-relay?calculator=N`

**호출처**: 중간페이지(practice), 매크로(직접 폴링)

**반환 우선순위**:
1. **캐시 최우선** — POST가 방금 갱신했으면 즉시 반환 (DB 쓰기 대기 없음)
2. **DB 우선** — 회차가 서버보다 같거나 높으면 DB(클라이언트 POST) 반환
3. relay 캐시 — DB 비었거나 회차 낮을 때
4. 회차 같으면 서버 금액(마틴 보정) 참고

**이유**: 분석기에서 픽 나오자마자 POST → 캐시 즉시 갱신 → 매크로가 50ms 내 수신·배팅.

---

## 4. 중간페이지 (practice.html)

- **경로**: `/practice` (에뮬레이터 브라우저에서 열기)
- **폴링**: `GET /api/current-pick-relay?calculator=N` 100ms마다
- **푸시**: 푸시 URL 입력 시 `POST {pushUrl}/push-pick` 로 `{ round, pick_color, suggested_amount }` 전송
- **조건**: `round`, `pick_color`, `suggested_amount > 0`, `running !== false` 일 때만 푸시

---

## 5. 매크로 수신

### 5.1 폴링 방식
- `GET /api/current-pick-relay?calculator=N` 주기적 호출
- 같은 (회차, 픽, 금액) 3회 연속 수신 시 배팅 실행

### 5.2 푸시 방식 (중간페이지 사용 시)
- `POST /push-pick` 수신 (포트 8765)
- 수신 즉시 배팅 실행 (3회 확인 생략)

---

## 6. 픽 놓침·마틴 금액·2중배팅 방지

| 현상 | 대응 |
|------|------|
| **픽 놓침** | 2회 연속 확인 시 배팅 (3회→2회). 회차가 올라갔는데 이전 회차 미배팅 시 즉시 배팅 |
| **마틴 끝 후 전회차 금액** | 서버에 직전 회차 결과 있으면 `pending_bet_amount` 우선 사용 |
| **2중배팅** | `_do_bet_lock`으로 배팅 흐름 직렬화. 픽(RED/BLACK) 버튼 1회만 탭. `_bet_rounds_done`·`_pending_bet_rounds`로 회차당 1번만 실행 |

## 7. 금액 불일치 방지 체크리스트

| 항목 | 규칙 |
|------|------|
| 클라이언트 POST | `getBetForRound(id, curRound)` 사용 (1열과 동일) |
| 서버 POST 수신 | 클라이언트 값 사용. 마틴 끝 후 직전 회차 결과 있으면 서버 금액 우선 |
| GET 반환 | DB 회차 ≥ relay 회차이면 DB 우선 |
| relay 캐시 | POST 시 항상 즉시 갱신 (배팅중 픽 즉시 전달). 스케줄러 0.2초마다 보조 갱신 |

---

## 8. 관련 파일

| 파일 | 역할 |
|------|------|
| `app.py` | `api_current_pick_relay`, `_update_current_pick_relay_cache`, `_relay_db_write_background` |
| `templates/results.html` (embedded JS) | `postCurrentPickIfChanged`, `getBetForRound` |
| `templates/practice.html` | relay 폴링, 푸시 |
| `auto-betting-macro/emulator_macro.py` | 폴링/푸시 수신, ADB 배팅 |
