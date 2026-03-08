# 분석기 성능 점검 보고서 (2025-03)

## ⚠️ "갑자기 느려짐" 핵심 원인 (추가)

**calc_sessions 무한 누적 + N+1 쿼리** → DB 연결 한도 초과. 상세: `docs/SLOW_CAUSE_DIAGNOSIS.md`

**적용 완료**: calc_sessions 24h 정리, get_calc_state → _get_all_calc_states 일괄 조회.

---

## 요약

분석기가 느린 주요 원인:

1. **스케줄러 0.05초 주기** — 초당 20회 fetch + 20회 apply → DB/CPU 과부하
2. **get_recent_results ORDER BY REGEXP_REPLACE** — 매 행마다 정규식 계산 → 느린 쿼리
3. **_server_calc_effective_pick_and_amount** — 세션당 get_recent_results 2~3회 중복 호출
4. **API 응답마다 print** — 매 요청마다 I/O → 불필요한 지연
5. **_build_results_payload_db_only** — compute_prediction, _get_prediction_picks_best 2회, shape_pick 등 여러 무거운 호출
6. **클라이언트 폴링** — loadResults, prediction, shape, calcStatus 4개 동시 폴링

---

## 1. 스케줄러 주기 (0.05초)

**현재**: fetch 0.05초, apply 0.05초 → **초당 20회 fetch 시도 + 20회 apply**

| 구간 | 현재 | 권장 | 이유 |
|------|------|------|------|
| fetch | 0.05초 | 0.2초 | 외부 fetch 0.5~4초 소요. 0.05초마다 시도해도 lock으로 스킵되지만 스레드 생성·스케줄러 오버헤드 |
| apply | 0.05초 | 0.1초 | 10초 게임 8초 내 배팅이면 0.1초도 충분. DB 조회·compute_prediction 반복 감소 |

**규칙**: calculator-guide.mdc에 "0.05초마다" 명시. 10초 게임 8초 내 배팅 목표 유지하려면 0.1초도 충분.

---

## 2. get_recent_results - ORDER BY REGEXP_REPLACE

```sql
ORDER BY (NULLIF(REGEXP_REPLACE(game_id::text, '[^0-9]', '', 'g'), '')::BIGINT) DESC NULLS LAST, created_at DESC
```

**문제**: LIMIT 2000 전에 **전체 스캔** 후 정규식 계산. game_id가 숫자형이면 인덱스 활용 불가.

**권장**:
- `created_at DESC`만 사용 → 인덱스 `idx_created_at` 활용
- Python에서 `_sort_results_newest_first()`로 game_id 숫자 기준 재정렬 (이미 2000건 이하)

또는 `game_id`가 숫자만 포함하면 `game_id::BIGINT` 등으로 변환 가능한 컬럼 추가 검토.

---

## 3. _server_calc_effective_pick_and_amount - get_recent_results 중복

**호출 위치**:
- 2588행: `results_rl = get_recent_results(hours=24)` — run_length
- 2655행: `results_sr = get_recent_results(hours=24)` — _suppress_smart_reverse_by_phase
- 2679행: `results = get_recent_results(hours=24)` — shape_only_latest_next_pick

**세션당**: calc 1~3개 × 각 calc당 1~3회 = **최대 9회** get_recent_results 중복.

**권장**: results를 인자로 받아서 한 번만 조회하고 재사용.

---

## 4. API 응답마다 print

**현재** (12651행):
```python
print(f"[API] 응답 결과 수: {len(payload['results'])}개, 맨 앞(최신) gameID: {first_id}")
```

**문제**: 매 `/api/results` 요청마다 print → 150~200ms 폴링 시 초당 5~7회 I/O.

**권장**: `_log_when_changed` 또는 `_log_throttle` 사용 (10초에 1회 등).

---

## 5. _build_results_payload_db_only - 무거운 호출

| 호출 | 비고 |
|------|------|
| compute_prediction | 1회 |
| _get_prediction_picks_best | 2회 (일반 + shape_pong_only) |
| _get_latest_next_pick_for_chunk | 2~3회 |
| _get_shape_15_win_rate_weighted 등 | 여러 회 |
| _backfill_shape_predicted_in_ph | backfill=1일 때만 (max_backfill=0이면 스킵) |

**캐시**: `_shape_pick_cache` 350ms TTL 사용 중. `prediction_cache`와 `_build_results_payload_db_only`가 별도라 중복 계산 가능.

**권장**: `/api/current-prediction` 경량 경로와 `_build_results_payload_db_only`가 prediction_cache를 공유하도록 정리. 이미 `prediction_cache`는 apply 경로에서만 갱신.

---

## 6. 클라이언트 폴링

| API | 간격 | 비고 |
|-----|------|------|
| loadResults | 150~300ms | 계산기 실행 중 150ms |
| prediction | 300ms | /api/current-prediction |
| shape | 400ms | /api/shape-pick |
| calcStatus | 200ms | updateCalcStatus (POST) |
| calcState | 1200ms | GET |

**문제**: loadResults + prediction + shape = 동시 3개 API. loadResults가 server_prediction 포함하므로 prediction 폴링과 중복 가능.

**권장**: loadResults 응답에 server_prediction 포함 시 prediction 폴링 간격 축소(500ms) 또는 loadResults와 동기화.

---

## 7. DB 인덱스

**현재**:
- game_results: idx_game_id, idx_created_at
- get_recent_results: `WHERE created_at >= NOW() - ...` → idx_created_at 활용
- ORDER BY REGEXP_REPLACE → 인덱스 미활용

**추가 검토**: `(created_at DESC)` 복합 인덱스로 커버링 인덱스 가능 여부.

---

## 8. 즉시 적용 가능 개선

| 항목 | 우선순위 | 효과 | 위험 |
|------|----------|------|------|
| **① API print 제거/스로틀** | 높음 | I/O 감소 | 없음 |
| **② 스케줄러 0.05→0.1초** | 높음 | CPU/DB 부하 50% 감소 | 10초 게임 8초 내 배팅 유지 |
| **③ get_recent_results ORDER BY 단순화** | 중간 | 쿼리 속도 개선 | game_id 형식 확인 필요 |
| **④ _server_calc_effective_pick_and_amount results 인자 전달** | 중간 | get_recent_results 호출 감소 | 리팩터 필요 |

---

## 9. 프로파일링

```bash
PERF_PROFILE=1 python app.py
```

5초마다 상위 10개 병목 구간 출력. 실제 배포 환경에서 `scheduler_apply`, `get_recent_results`, `build_results_payload_db_only` ms 확인 후 우선순위 결정.

---

## 10. 참고 문서

- `docs/PREDICTION_DELAY_AND_CONFLICT_ANALYSIS.md` — 병목·lock 경합
- `docs/PERFORMANCE_PROFILING.md` — 프로파일 사용법
- `.cursor/rules/calculator-guide.mdc` — 스케줄러 주기 규칙
