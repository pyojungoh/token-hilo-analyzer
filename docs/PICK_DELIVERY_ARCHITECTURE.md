# 분석기 → 매크로 픽 전달 구조 (왜 느린가)

## 1. 전체 데이터 흐름 (다단계 폭포)

```
[게임 사이트]                    [분석기 서버]                    [매크로]
  tgame365 등                        Railway                          PC
      │                                  │                               │
      │  result.json                     │                               │
      │  (15장 나오면 다음회차 예측 가능)   │                               │
      │                                  │                               │
      │  ◄─── load_results_data() ─────  │   ← 0.1초마다 시도             │
      │       HTTP GET (병렬 6경로)       │   ← 경로당 2초 타임아웃        │
      │       전체 4초 타임아웃           │   ← 실제 0.5~4초 소요         │
      │                                  │                               │
      │  ──── 응답 ───────────────────►  │                               │
      │                                  │ DB 저장                        │
      │                                  │ apply (0.05초마다 또는 즉시)   │
      │                                  │   → calc_state 갱신            │
      │                                  │   → pending_round, 픽, 금액    │
      │                                  │                               │
      │                                  │  ◄── GET /api/current-pick-relay
      │                                  │       (매크로 50ms 폴링)        │
      │                                  │                               │
      │                                  │  ──── JSON 응답 ────────────► │
      │                                  │       { round, pick_color,     │
      │                                  │         suggested_amount }     │
      │                                  │                               │
      │                                  │                    1회 확인 후 배팅
```

---

## 2. 지연 원인 (병목 구간)

| 구간 | 소요 시간 | 설명 |
|------|-----------|------|
| **① 외부 API 응답** | **0.5 ~ 4초** | 게임 사이트 result.json을 우리가 **가져옴(PULL)**. 푸시 아님. 네트워크·서버 부하에 따라 변동 |
| **② fetch 시도 주기** | 0 ~ 0.1초 | 0.1초마다 fetch 시도. 직전 fetch가 끝나야 다음 시도 |
| **③ apply** | 0 ~ 0.05초 | fetch 완료 시 즉시 또는 0.05초 내 실행 |
| **④ 매크로 폴링** | 0 ~ 0.05초 | 50ms마다 GET. 서버에 데이터 있으면 다음 폴에서 수신 |
| **⑤ 네트워크 왕복** | 수십~백 ms | 매크로(PC) ↔ 분석기(Railway) HTTP 왕복 |

**총 지연 = ① + ② + ③ + ④ + ⑤**  
→ **가장 큰 병목은 ① (외부 API 0.5~4초)**

---

## 3. 구조적 한계

### 3.1 Pull 기반
- 게임 사이트가 우리에게 **푸시하지 않음**
- 우리가 **주기적으로 GET** 해야 함
- 새 결과가 나와도, 우리가 요청하기 전까지 알 수 없음

### 3.2 다단계 전달
1. 게임 사이트 → 분석기 (fetch)
2. 분석기 DB → calc_state (apply)
3. 분석기 → 매크로 (GET 폴링)

각 단계마다 주기·지연이 쌓임.

### 3.3 웹 클라이언트 경로 (선택)
- **웹 열어둠**: 클라이언트가 50ms마다 `updateCalcStatus` → POST relay → relay 캐시 갱신
- **GET은 캐시 안 씀**: 현재 GET은 `get_calc_state` + `_server_calc_effective_pick_and_amount`로 **매번 계산**
- 따라서 웹이 POST해도, 매크로 GET은 **서버 calc_state** 기준. 웹 POST가 relay 캐시를 덮어써도 GET은 계산값 사용.

---

## 4. 푸시 방식 (practice 페이지)

```
[분석기 결과 페이지]     [practice 페이지]        [매크로]
       │                       │                     │
       │  GET relay 100ms      │                     │
       │  ──────────────────► │                     │
       │  픽/회차/금액         │  POST /push-pick    │
       │                       │  (localhost:8765)  │
       │                       │  ─────────────────►│
       │                       │                     │ 즉시 배팅
```

- practice 페이지가 **같은 도메인**에서 relay를 폴링하고, 푸시 URL이 있으면 매크로로 **직접 POST**
- 이 경우: 분석기 → practice → 매크로. **한 홉 추가**이지만, practice가 relay를 100ms 폴링하므로 분석기 서버에 픽이 있으면 곧바로 푸시
- **단점**: practice 페이지를 **에뮬레이터 브라우저**에서 열어둬야 함

---

## 5. 개선 가능 방향

| 방향 | 설명 | 난이도 |
|------|------|--------|
| **외부 API 폴링 간격 축소** | 0.1초 → 0.05초 등. 단, 요청 과다 시 차단·지연 가능 | 낮음 |
| **외부 API 타임아웃 축소** | 2초 → 1초. 실패 시 빠르게 재시도 | 중간 (실패율 상승) |
| **게임 사이트 WebSocket** | 사이트가 푸시 제공 시 즉시 수신. **사이트 지원 필요** | 높음 |
| **매크로와 분석기 동일 네트워크** | 로컬 배포 시 왕복 지연 감소 | 환경 의존 |
| **푸시 URL + practice** | practice에서 매크로로 푸시. 3회 확인 생략, 즉시 배팅 | 이미 구현됨 |

---

## 6. 관련 코드 위치

| 역할 | 파일 | 함수/위치 |
|------|------|-----------|
| 외부 fetch | `app.py` | `load_results_data`, `_build_results_payload` |
| fetch 스케줄 | `app.py` | `_scheduler_fetch_results` (0.1초) |
| apply | `app.py` | `_scheduler_apply_results` (0.05초), fetch 완료 시 즉시 |
| relay GET | `app.py` | `api_current_pick_relay` GET |
| 매크로 폴링 | `emulator_macro.py` | `_poll`, `fetch_current_pick` (50ms) |
| practice 푸시 | `templates/practice.html` | `pollAnalyzerPick` 100ms, `POST /push-pick` |
