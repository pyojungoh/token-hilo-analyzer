# 계산기 DB 및 데이터 흐름

## 사용 DB (PostgreSQL, DATABASE_URL)

| 테이블 | 용도 |
|--------|------|
| **calc_sessions** | 계산기 상태 저장. `session_id`(기본 'default'), `state_json`(calc 1·2·3 전체), `updated_at` |
| **game_results** | 경기 결과 (game_id, result, red, black, joker 등) |
| **prediction_history** | 예측기표 기록 (round_num, predicted, actual, pick_color 등) |
| **round_predictions** | 배팅중 예측 임시 저장 → 결과 나오면 prediction_history로 머지 |
| **current_pick** | 배팅 연동용 현재 픽 (RED/BLACK, 회차, 금액) |

## 결과값 출처

- **경기 결과**: `game_results` + 외부 fetch 병합. `/api/results`에서 `get_recent_results()`(DB) 또는 `_refresh_results_background()`(외부) 사용.
- **계산기 history**: `calc_sessions.state_json` 내 `calcs['1'|'2'|'3'].history`
- **픽/색상**: 클라이언트 `lastBetPickForRound`(배팅 픽) → 서버 `pending_predicted`, `pending_color`로 전달

## 배팅중 vs 1열(픽) 불일치 방지

1. **saveCalcStateToServer**: `pending_predicted`, `pending_color`에 `lastBetPickForRound` 사용 (예측기 픽 X)
2. **결과 반영**: `saved` = `lastBetPickForRound` 우선, 없으면 `savedBetPickByRound`
3. **ensurePendingRowForRunningCalc**: `lastBetPickForRound` 없을 때 최소한 반픽 적용
4. **saveCalcStateToServer 응답 후**: `updateCalcStatus` → `ensurePendingRowForRunningCalc` 순서 (lastBetPickForRound 먼저 설정)
