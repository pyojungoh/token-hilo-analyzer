# 분석기 "갑자기 느려짐" 원인 진단

## 핵심 원인: calc_sessions 무한 누적 + N+1 쿼리

### 1. calc_sessions 정리 로직 없음

- **calc_sessions** 테이블에 **DELETE/정리 로직이 전혀 없음**
- 예전에는 여러 session_id(다른 기기, 시크릿 모드 등)가 생성됐을 수 있음
- 현재는 'default'만 사용하지만 **DB에는 과거 세션이 그대로 남아 있음**
- 시간이 지날수록 **세션 수(N)가 계속 증가**

### 2. 0.1초마다 N+1 쿼리 폭주

매 **0.1초**마다 스케줄러가 실행될 때:

```
_get_all_calc_session_ids()     → SELECT session_id FROM calc_sessions  (1회)
for session_id in session_ids:
    get_calc_state(session_id)  → 새 DB 연결 + SELECT (세션당 1회)
```

- **_apply_results_to_calcs**: N개 세션 × get_calc_state = **N회 DB 연결+쿼리**
- **_update_relay_cache_for_running_calcs**: 또 N개 세션 × get_calc_state = **N회**
- **총**: 2 + 2N회 DB 연결/쿼리 per 0.1초

| 세션 수(N) | 0.1초당 연결 | 초당 연결 |
|------------|--------------|-----------|
| 5          | 12           | 120       |
| 20         | 42           | 420       |
| 50         | 102          | **1,020** |
| 100        | 202          | **2,020** |

Railway Postgres 기본 connection limit은 보통 **20~100**.  
세션이 20~50개만 쌓여도 **연결 한도 초과** → 새 쿼리 대기 → 전반적인 지연.

### 3. "잘 되다가 갑자기 느려진" 이유

- 처음엔 세션이 1~2개 → 정상
- 사용/테스트가 쌓이면서 calc_sessions 행 수 증가
- **어느 시점에 connection limit에 걸림** → 그때부터 전반적으로 느려짐

---

## 해결 방안

### ① calc_sessions 정리 (즉시 적용)

- **default** 외 세션 중 `updated_at`이 24시간 이상 된 것 삭제
- 또는 running이 아닌 세션 중 오래된 것 삭제
- trim_shape_tables처럼 주기적(예: 5분마다) 실행

### ② get_calc_state N+1 제거 (즉시 적용)

- `_get_all_calc_session_ids()` + N번 `get_calc_state()` 대신
- **한 번에** `SELECT session_id, state_json FROM calc_sessions` 조회
- `get_all_calc_states()` 같은 함수로 일괄 조회 후 메모리에서 사용

### ③ DB 연결 풀 (선택)

- psycopg2 대신 SQLAlchemy 등 connection pool 사용
- 연결 재사용으로 connection 수 감소

---

## 확인 방법

DB에서 calc_sessions 행 수 확인:

```sql
SELECT COUNT(*) FROM calc_sessions;
SELECT session_id, updated_at FROM calc_sessions ORDER BY updated_at DESC LIMIT 20;
```

행 수가 10개를 넘으면 의심, 20개 이상이면 원인일 가능성이 높음.
