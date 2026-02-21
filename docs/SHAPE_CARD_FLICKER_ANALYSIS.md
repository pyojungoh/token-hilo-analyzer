# 예측기픽 모양 카드 정확한 픽 즉시 표시

## 목표
- **처음부터 정확한** 모양 픽이 나와야 함 (나중에 바뀌는 게 아님)

---

## 원인 (적용 전)

### prediction_cache가 최신 results보다 먼저 갱신됨

- `_update_prediction_cache_from_db`가 **별도 job**으로 0.2초마다 독립 실행
- `_scheduler_fetch_results`는 `_refresh_results_background` (외부 fetch 2~4초) 후 DB에 저장
- **순서**: prediction_cache job이 먼저 돌면 **아직 저장 안 된 DB**로 shape_pick 계산 → 잘못된 값
- 나중에 fetch 완료 후 DB 저장 → 다음 prediction_cache 갱신 시 정확한 값
- **결과**: 잘못된 픽 → 정확한 픽으로 바뀜 (패 발생)

---

## 적용된 해결

### 1. prediction_cache를 스케줄러 fetch 직후에만 갱신

1. **별도 prediction_cache job 제거**
2. **`_scheduler_fetch_results` 끝에서** `_update_prediction_cache_from_db()` 호출
3. `_refresh_results_background` 완료 → DB 저장 → `get_recent_results`로 최신 데이터 → `_update_prediction_cache_from_db`로 갱신

**효과**: prediction_cache는 **항상 최신 results 반영 직후**에만 갱신되므로, 처음 표시되는 shape_pick이 정확함

### 2. _shape_pick_cache를 모든 페이로드 경로에 적용

- **원인**: `_build_results_payload`(fetch 경로)는 `_shape_pick_cache`를 사용하지 않아, 스케줄러가 `_refresh_results_background`로 results_cache를 덮어쓸 때 shape_pick이 매번 재계산됨. `/api/current-prediction`이 results_cache를 폴백으로 사용하면 캐시되지 않은 값이 반환되어 모양 카드가 바뀜.
- **조치**: `_build_results_payload` 내 shape_pick 계산 블록 2곳(DB+json 경로, DB 없음 경로) 모두에 `_shape_pick_cache` 로직 추가. `_build_results_payload_db_only`와 동일하게 회차별 캐시 사용.
