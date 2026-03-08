# 분석기 "또 느려짐" 원인 분석 (코딩 없이)

## 요약

**이전 수정(calc_sessions N+1 제거)은 연결 수는 줄였지만, 다른 병목이 그대로 남아 있거나 오히려 악화됐을 수 있음.**

---

## 1. get_recent_results 중복 호출 (미해결)

`_server_calc_effective_pick_and_amount(c)`는 **계산기당** 호출되며, **results를 인자로 받지 않고** 내부에서 직접 조회함.

### 호출 흐름 (0.1초마다 apply 1회 기준)

```
apply 시작
├─ get_recent_results(24) ........................ 1회
├─ _get_all_calc_states() ........................ 1회
└─ for each session, for each running calc:
   └─ _server_calc_effective_pick_and_amount(c):
      ├─ get_recent_results(24) .................. calc당 1회 (2621행)
      ├─ get_recent_results(24) .................. do_reverse 시 1회 (2688행)
      └─ get_recent_results(24) .................. shape_only 시 1회 (2712행)
```

**계산기 3개 실행 중 + 스마트반픽 켜진 경우:**
- get_recent_results: **1 + 3 + (0~3) + (0~3) = 4~10회** per 0.1초
- **초당 40~100회** get_recent_results

각 호출마다:
- 새 DB 연결
- game_results 2000행 조회
- color_matches 배치 조회
- calculate_and_save_color_matches
- Python 정렬

→ **가장 큰 병목**

---

## 2. get_prediction_history 중복 호출

`get_prediction_history`는 **매번 새 DB 연결 + SELECT** 수행. 코드 전역에 **33회** 호출.

### apply 1회당 호출 예시 (1세션, 3 calc 실행 중)

| 위치 | 호출 수 |
|------|---------|
| streak_wait_enabled | calc당 1 |
| prediction_picks_best | calc당 1 |
| shape_prediction | calc당 1 |
| get_shape_prediction_hint 내부 | calc당 1 |
| _server_calc_effective_pick_and_amount 내부 | calc당 2~3 |

→ **calc 3개 기준 12~15회** get_prediction_history per 0.1초  
→ **초당 120~150회** DB 연결 + prediction_history 조회

---

## 3. _get_all_calc_states의 state_json 부담

```sql
SELECT session_id, state_json FROM calc_sessions
```

- **LIMIT 없음** → 모든 세션의 state_json 전부 조회
- `state_json` = calc 1,2,3 상태 + **history (최대 50,000행)**
- history 1만 건 × 3 calc ≈ **수 MB** JSON per 세션

**영향:**
- DB → 앱으로 대용량 전송
- `json.loads()` 반복 → CPU 부하
- 0.1초마다 실행 → 파싱 비용이 누적

세션 1개만 있어도 history가 크면 부담이 큼.

---

## 4. /api/results와 스케줄러 경합

- 클라이언트: loadResults **150~300ms** 간격
- 캐시 TTL: **100ms**
- 캐시 만료 시: `_build_results_payload_db_only` 호출
  - get_recent_results
  - get_prediction_history(300)
  - compute_prediction
  - _get_prediction_picks_best 2회
  - 기타 무거운 연산

동시에 스케줄러 apply(0.1초)도 get_recent_results, get_prediction_history 호출  
→ **동시 DB 접근 증가** → lock/connection 대기

---

## 5. _build_results_payload_db_only 무게

캐시 miss 시 한 번 호출되는데, 내부에서:

- get_recent_results (또는 results 인자)
- get_prediction_history(300)
- compute_prediction
- _get_prediction_picks_best 2회
- _get_latest_next_pick_for_chunk 2~3회
- _get_shape_15_win_rate_weighted 등 다수

→ 단일 요청 처리 시간이 길어짐.

---

## 6. 원인 우선순위 (추정)

| 순위 | 원인 | 근거 |
|------|------|------|
| 1 | **_server_calc_effective_pick_and_amount 내 get_recent_results 중복** | calc당 1~3회, 0.1초마다 → 초당 40~100회 |
| 2 | **get_prediction_history 비캐시 반복 호출** | 33회 사용, 매번 새 연결 + 쿼리 |
| 3 | **_get_all_calc_states의 대용량 state_json** | history 많을수록 전송/파싱 부담 |
| 4 | **_build_results_payload_db_only 복잡도** | 캐시 miss 시 응답 지연 |
| 5 | **스케줄러 0.1초 주기** | 위 호출들이 0.1초마다 반복 |

---

## 7. 확인 방법

```bash
PERF_PROFILE=1 python app.py
```

5초마다 출력되는 상위 병목 구간 확인:
- `get_recent_results` ms
- `scheduler_apply` ms
- `apply_results_to_calcs` ms
- `build_results_payload_db_only` ms

---

## 8. 수정 시 우선순위 (참고)

1. ~~**_server_calc_effective_pick_and_amount에 results 인자 전달**~~ **적용 완료**
   - apply에서 이미 조회한 results 재사용
   - get_recent_results 호출 수 대폭 감소

2. ~~**get_prediction_history 캐시**~~ **적용 완료**
   - apply 1회 내 _ph_for_apply 1회 조회 후 calc당 재사용
   - relay 경로에도 ph 전달

3. **_get_all_calc_states 경량화** (미적용)
   - relay/apply용으로는 history 불필요할 수 있음
   - session_id + running 여부만 조회하는 경량 경로 검토

4. **스케줄러 주기 완화** (미적용)
   - 0.1초 유지 — 10초 게임 8초 내 배팅 규칙 준수
