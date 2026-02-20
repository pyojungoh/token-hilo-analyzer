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
| **서버 저장** | 클라이언트 POST 값 그대로 | 서버가 덮어쓰지 않음 |

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
- `suggested_amount`는 **클라이언트가 보낸 값 그대로** DB에 저장
- 서버가 `pending_bet_amount`로 덮어쓰지 않음 (배팅중 금액 정확도 보장)
- relay 캐시는 POST에서 갱신하지 않음 (스케줄러가 서버 calc 기준으로 갱신)

### 3.2 GET `/api/current-pick-relay?calculator=N`

**호출처**: 중간페이지(practice), 매크로(직접 폴링)

**반환 우선순위**:
1. **DB 우선** — 회차가 relay 캐시보다 같거나 높으면 DB(클라이언트 POST) 반환
2. relay 캐시 — DB 비었거나 회차 낮을 때
3. 회차 같으면 DB 금액 우선

**이유**: 분석기에서 픽 나오자마자 POST하므로 DB가 더 빠름. relay는 스케줄러(2~3초 블로킹)로 느림.

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

## 6. 금액 불일치 방지 체크리스트

| 항목 | 규칙 |
|------|------|
| 클라이언트 POST | `getBetForRound(id, curRound)` 사용 (1열과 동일) |
| 서버 POST 수신 | 클라이언트 `suggested_amount` 그대로 저장, 덮어쓰기 금지 |
| GET 반환 | DB 회차 ≥ relay 회차이면 DB 우선 |
| relay 캐시 | 스케줄러만 갱신, POST에서 갱신 안 함 |

---

## 7. 관련 파일

| 파일 | 역할 |
|------|------|
| `app.py` | `api_current_pick_relay`, `_update_current_pick_relay_cache`, `_relay_db_write_background` |
| `templates/results.html` (embedded JS) | `postCurrentPickIfChanged`, `getBetForRound` |
| `templates/practice.html` | relay 폴링, 푸시 |
| `auto-betting-macro/emulator_macro.py` | 폴링/푸시 수신, ADB 배팅 |
