# 분석기 성능 프로파일링 가이드

## 사용 방법

환경변수 `PERF_PROFILE=1`을 설정한 뒤 앱을 실행하면, 주요 구간별 소요 시간이 **5초마다** 콘솔에 출력됩니다.

```bash
# Windows (PowerShell)
$env:PERF_PROFILE="1"; python app.py

# Linux/Mac
PERF_PROFILE=1 python app.py
```

출력 예시:
```
[⏱ 병목] scheduler_fetch: 450ms, refresh_results_background: 380ms, get_recent_results: 120ms, apply_results_to_calcs: 85ms, get_shape_prediction_hint: 45ms, compute_prediction: 25ms, ...
```

평균 소요 시간이 가장 긴 순서대로 상위 10개만 표시됩니다.

## 프로파일된 구간

| 구간 | 설명 |
|------|------|
| `scheduler_fetch` | 스케줄러 전체 (0.15초마다 실행) |
| `refresh_results_background` | 외부 fetch + DB 저장 (스레드) |
| `build_results_payload_db_only` | API `/api/results` DB 전용 페이로드 생성 |
| `get_recent_results` | DB에서 최근 24h 결과 조회 (최대 2000행) |
| `apply_results_to_calcs` | 실행 중인 계산기 회차 반영 |
| `update_relay_cache` | relay 캐시 갱신 |
| `ensure_stored_prediction` | 현재 회차 예측 저장 |
| `get_shape_prediction_hint` | 모양판별 예측 (DB + compute_prediction) |
| `compute_prediction` | 메인 예측 공식 |
| `backfill_shape_predicted` | shape_predicted 보정 (?backfill=1 시) |

## 의심 병목 지점 (이전 분석)

1. **스케줄러 0.15초 주기**: 과도하게 짧을 수 있음
2. **get_recent_results**: 2000행 LIMIT + color_matches + calculate_and_save_color_matches
3. **apply_results_to_calcs**: 세션별 get_shape_prediction_hint, compute_prediction 반복
4. **get_shape_prediction_hint**: _get_shape_stats_for_results(DB), _get_chunk_stats_for_results(DB), compute_prediction
5. **compute_prediction**: 긴 예측 로직 (약 3000행대)
6. **build_results_payload_db_only**: get_shape_prediction_hint 2~3회, compute_prediction, _backfill_shape_predicted_in_ph(backfill=1 시)

## 최적화 권장

1. 프로파일 실행 후 **가장 높은 ms** 구간부터 확인
2. `get_shape_prediction_hint`·`compute_prediction`이 비중이 크면 → 캐싱/호출 축소 검토
3. `get_recent_results`가 느리면 → DB 인덱스, LIMIT 축소, color_matches 배치 최적화
4. `apply_results_to_calcs`가 느리면 → 세션 수·반복 호출 줄이기

---

## 적용된 최적화 (배포 환경 프로파일 기반)

| 변경 | 효과 |
|------|------|
| **refresh 비블로킹** | 스케줄러가 외부 fetch(~2.5초) 대기 안 함 → scheduler_fetch ~4초 → ~1.5초 |
| **스케줄러 주기 0.15초 → 2초** | 4초 걸리는 작업에 0.15초 간격은 무의미. 2초로 여유 확보 |
| **get_shape_prediction_hint 캐싱** | apply_results_to_calcs 내 동일 results·가중치 시 재사용 → 세션 N개 시 ~120ms×(N-1) 절약 |
