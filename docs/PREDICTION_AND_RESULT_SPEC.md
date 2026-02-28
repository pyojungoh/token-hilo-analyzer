# 예측픽·예측기픽·결과처리·승패 규격 (변형 금지)

이 문서는 **현재 정상 동작 중인** 예측픽, 예측기픽, 결과 처리, 승/패 표시 방식을 정의한다.  
**절대 변형하지 말 것.** 수정 시 예측 색상 반전·승패 오표시 등 버그가 재발한다.

---

## 1. 데이터 구조 전제

- **results**: 경기 결과 배열. **최신순** (index 0 = 가장 최신 회차).
- **회차**: `gameID` 또는 `round_num` — **전체 번호**만 사용. 끝 3자리 사용 금지.
- **맨 왼쪽 = index 0 = 최신 회차.** DOM/CSS 역순 그리기 금지.

---

## 2. 정/꺽 계산 (graph_values)

### 2.1 정의

- **비교**: `results[i]` vs `results[i+15]` — i번째 회차 카드와 (i+15)번째 회차 카드 색상 비교.
- **정**: 두 카드 색상 **같음** → `graph_values[i] = True`
- **꺽**: 두 카드 색상 **다름** → `graph_values[i] = False`
- **조커**: `results[i].joker` 또는 `results[i+15].joker` → `graph_values[i] = None`

### 2.2 코드

```python
def _build_graph_values(results):
    # results[i] vs results[i+15] 색상 비교
    out.append(c0 == c15)  # True=정, False=꺽
```

### 2.3 color_matches DB

- `game_id`(현재) vs `compare_game_id`(15회차 전) 색상 비교.
- `match_result = (current_color == compare_color)` — True=정, False=꺽.

---

## 3. 15번 카드 (정/꺽 → 빨강/검정 매핑 기준)

### 3.1 정의

- **15번 카드**: 정/꺽 픽을 RED/BLACK(빨강/검정)으로 변환할 때 사용하는 **기준 카드**.
- **고정 매핑 금지**: "정=빨강, 꺽=검정" 같은 고정 규칙 사용 금지. 15번 카드 색에 따라 매핑이 달라진다.

### 3.2 현재 구현 (변형 금지)

| 함수 | 15번 카드 출처 | 용도 |
|------|----------------|------|
| `_get_card_15_color_for_latest_round(results)` | `results[14]` | 예측기픽 메뉴·메인 예측픽 색상 |
| `compute_prediction` 내부 | `results[14]` | 메인 예측 color_to_pick |
| `_get_card_15_color_for_round(results, round_id)` | `results[i+14]` (gameID==round_id인 i의 15번째 카드) | 특정 회차 배팅 색 |

### 3.3 색상 매핑 규칙

```
is_15_red = get_card_color_from_result(results[14])  # True=빨강, False=검정

if is_15_red is True:
    정 → 빨강,  꺽 → 검정
elif is_15_red is False:
    정 → 검정,  꺽 → 빨강
else:
    정 → 빨강,  꺽 → 검정  # 폴백
```

---

## 4. 메인 예측픽 (compute_prediction)

### 4.1 출력

- `value`: '정' | '꺽' | None
- `round`: 예측 대상 회차 (results[0].gameID + 1)
- `prob`: 확률 0~100
- `color`: '빨강' | '검정' (15번 카드 기준 매핑)

### 4.2 15번 카드 조커

- `results[0].joker` 이면 예측 보류 → `value=None`, `color=None`.

### 4.3 색상 계산

- `is_15_red = get_card_color_from_result(results[14])`
- 위 3.3 매핑 규칙 적용.

---

## 5. 예측기픽 메뉴 (4카드: 메인, 메인반픽, 모양, 퐁당)

### 5.1 픽 출처

| 카드 | 픽 | 색상 |
|------|-----|------|
| 메인 | `server_prediction.value` | `server_prediction.color` (compute_prediction) |
| 메인반픽 | 정↔꺽 반전 | `is_15_red` 기준 매핑 |
| 모양 | `shape_pick` = _get_latest_next_pick_for_chunk | `shape_color` |
| 퐁당 | `pong_pick` = _get_pong_pick_for_round | `pong_color` |

### 5.2 색상 계산 (서버)

```python
is_15_red = _get_card_15_color_for_latest_round(results)  # results[14]

if is_15_red is True:
    shape_color = '빨강' if sp=='정' else '검정'
    pong_color = '빨강' if pong_pick=='정' else '검정'
    main_reverse_color = '빨강' if main_reverse=='정' else '검정'
elif is_15_red is False:
    shape_color = '검정' if sp=='정' else '빨강'
    pong_color = '검정' if pong_pick=='정' else '빨강'
    main_reverse_color = '검정' if main_reverse=='정' else '빨강'
```

---

## 6. 결과 처리

### 6.1 실제 결과(actual) 추출

- **함수**: `_get_actual_for_round(results, round_id)`
- **로직**: `graph_values[i]` 사용. `gv[i] is True` → '정', `gv[i] is False` → '꺽', 조커 → 'joker'.

### 6.2 round_actuals (회차별 실제 결과)

- **함수**: `_build_round_actuals(results)`
- **출력**: `{ round_id: { actual: '정'|'꺽'|'joker', color: 'RED'|'BLACK'|None } }`
- **actual**: `graph_values[i]` 기준.
- **color**: `results[i]` 또는 `results[i+15]` 색상. 없으면 `gv[i]`와 `c15`로 유도.

### 6.3 prediction_history 저장

- **저장 경로**: (1) 클라이언트 `/api/save-prediction`, (2) 서버 스케줄러 `save_prediction_record()`.
- **예측기표 = 메인 예측기 픽 고정**: prediction_history에는 **메인 예측기 픽(예측픽)**만 저장. 계산기 반픽/승률반픽 값 사용 금지.
- **보정**: `_backfill_latest_round_to_prediction_history` — 최신 회차 누락 시 서버가 자동 저장.

---

## 7. 승/패 판정

### 7.1 기준

- **승**: `predicted == actual` (둘 다 '정' 또는 '꺽')
- **패**: `predicted != actual` (정 vs 꺽)
- **조커**: `actual == 'joker'` → **패로 간주** (승률 계산 시 제외 또는 패 처리)

### 7.2 합산승률

- **blended_win_rate**: 15회×0.65 + 30회×0.25 + 100회×0.10
- 조커 제외 후 `predicted == actual` 비율로 계산.

### 7.3 표시

- 예측기표·계산기 표: 승=초록/노랑, 패=빨강, 조커=파랑.
- **예측기표 픽**: `getRoundPrediction(round)` 또는 `lastPrediction`에서만 취함. `predictionHistory.find` 기록용 사용 금지.

---

## 8. 카드 색상 추출 (get_card_color_from_result)

- **우선순위**: `r.red`/`r.black` (게임 제공) > `parse_card_color(r.result)`
- **반환**: True=빨강, False=검정, None=미확인
- **parse_card_color**: H,D,♥,♦=빨강 / S,C,♠,♣=검정

---

## 9. 수정 시 금지 사항

| 항목 | 금지 |
|------|------|
| 15번 카드 | `results[14]` 대신 `results[0]`/`results[15]` 등 다른 인덱스로 임의 변경 |
| 정/꺽 매핑 | "정=빨강, 꺽=검정" 고정 규칙 사용 |
| graph_values | `results[i]` vs `results[i+15]` 비교 로직 변경 |
| 예측기표 | 계산기(반픽/승률반픽) 픽 사용 |
| prediction_history | 예측픽 외 다른 픽 저장 |
| 결과 순서 | 맨 왼쪽 ≠ 최신 회차로 렌더 |
| 회차 | 끝 3자리만 사용 |

---

## 10. 코드 위치 참고

| 기능 | 파일 | 함수/위치 |
|------|------|-----------|
| graph_values | app.py | `_build_graph_values` |
| 15번 카드 | app.py | `_get_card_15_color_for_latest_round`, `_get_card_15_color_for_round` |
| 메인 예측 | app.py | `compute_prediction` |
| 예측기픽 색상 | app.py | `_build_results_payload_db_only` 내 server_pred |
| 실제 결과 | app.py | `_get_actual_for_round`, `_build_round_actuals` |
| 예측 저장 | app.py | `save_prediction_record` |
| 승률 | app.py | `_blended_win_rate_components`, `_get_main_recent15_win_rate` |

---

*이 문서는 da354ad (2026-02-20 17:43) 시점 동작 기준으로 작성됨. 변형 금지.*
