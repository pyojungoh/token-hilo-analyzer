# 픽 정/꺽·매크로 표시 버그 분석 (2026-02-27)

## 1. 문제 요약

- **증상**: 분석기에서 "정 블랙" 픽이 매크로에 "블랙 꺽"으로 표시되어 결과가 틀려짐
- **원인**: 매크로가 `pick_color`(RED/BLACK)만 보고 **RED=정, BLACK=꺽**으로 고정 매핑
- **실제 규칙** (PREDICTION_AND_RESULT_SPEC.md): 15번 카드에 따라 **정/꺽→빨강/검정** 매핑이 달라짐
  - 15번 빨강: 정→빨강, 꺽→검정
  - 15번 검정: 정→검정, 꺽→빨강

## 2. 데이터 흐름 (현재)

| 구간 | pick_color | pick_pred (정/꺽) |
|------|------------|-------------------|
| 서버 relay 캐시 | RED/BLACK | ✅ 있음 |
| WebSocket emit | RED/BLACK | ✅ 있음 |
| GET /api/current-pick-relay | RED/BLACK | ✅ 있음 |
| POST (클라이언트) | RED/BLACK | ✅ predicted 전송 |
| macro_pick_transmit DB | RED/BLACK | ❌ 없음 |
| 매크로 WebSocket 수신 | - | ❌ pick_pred 미복사 |
| 매크로 _pick_data | - | ❌ pick_pred 없음 |
| 매크로 _update_display | RED→정, BLACK→꺽 | ❌ 하드코딩 버그 |

## 3. 해결 방향 (사용자 제안)

- **픽**: 색상(pick_color) 유지
- **1열 추가**: 정/꺽(pick_pred) 명시 전달
- **결과 매칭**: 픽의 **컬러값**으로 승패 판정 (배팅한 색 = 결과 색 → 승)

## 4. 수정 대상

### 4.1 매크로 (emulator_macro.py)

1. **WebSocket on_pick**: `data.get('pick_pred')`를 pick에 포함
2. **_on_ws_pick_received**: `_pick_data`에 `pick_pred` 저장
3. **_update_display**: `pick_pred` 있으면 그대로 표시, 없으면 "정/꺽 미확인" 등
4. **_do_bet 로그**: `pick_pred` 있으면 사용, 없으면 pick_color만

### 4.2 푸시 (practice.html → 매크로)

- 이미 `predicted` 전송 중. 매크로 push handler에서 `pick_pred` 수신·저장 추가

### 4.3 폴링 (fetch_current_pick)

- API가 이미 `pick_pred` 반환. 매크로가 `_pick_data`에 저장하도록 수정

## 5. 금액 간헐적 불일치 점검·수정

### 5.1 적용한 수정

- **POST suggested_amount**: `getCalcResult.currentBet` → `getBetForRound(id, curRound)` 로 변경
- 규칙(betting-in-display-to-macro): 1열과 동일 출처만 사용
- `getBetForRound`는 `pending_bet_amount`(서버) 우선 사용 → 마틴 승 후 초기금 등 서버·클라이언트 동기화

### 5.2 점검 포인트 (유지)

- `getBetForRound`: pending_bet_amount vs 마틴 시뮬레이션 — 더 큰 값 사용
- POST 시 서버 마틴 보정: `has_prev_result` + `pending_bet_amount` 시 서버 금액 우선
- 매크로: `suggested_amount`만 사용, `_display_best_amount` 배팅에 사용 금지
- 2회 연속 동일 (round, pick_color, amount) 확인 후 배팅 — 타이밍 이슈 가능
