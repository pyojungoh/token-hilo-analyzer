# 계산기표 픽(predicted) vs 색(pickColor) 불일치 원인 분석

배팅중 픽이 "정"인데 색이 정 색깔(15번 카드 기준)이 아닌 검정으로 나오는 등, **픽과 배팅할 색이 가끔 다르게** 표시되는 현상의 원인을 정밀 분석한다.

---

## 1. 규칙 (PREDICTION_AND_RESULT_SPEC.md)

**15번 카드 기준 매핑** — 고정 "정=빨강, 꺽=검정" 사용 금지:

| 15번 카드 | 정 → 색 | 꺽 → 색 |
|----------|---------|---------|
| 빨강 | 빨강 | 검정 |
| 검정 | **검정** | **빨강** |
| 미확인 | 빨강 | 검정 (폴백) |

→ **15번이 검정이면 "정"은 검정, "꺽"은 빨강**이다.

---

## 2. 원인 후보 (발견된 버그)

### 2.1 서버: 줄 추종 시 고정 매핑 (app.py 2188-2190)

```python
if run_length >= 4 and run_last_value is not None:
    pred_for_calc = '정' if run_last_value else '꺽'
    bet_color_for_history = '빨강' if pred_for_calc == '정' else '검정'  # ← 15번 카드 무시!
```

- **문제**: 줄 4 이상일 때 `bet_color_for_history`를 **고정** "정=빨강, 꺽=검정"으로 설정
- **영향**: 15번 카드가 검정인데 정 픽이면 → 잘못된 빨강 저장 → 픽(정)과 색(빨강) 불일치
- **수정**: `_get_card_15_color_for_round(results, pending_round)` 사용해 15번 기준으로 색 계산

---

### 2.2 서버: _server_calc_effective_pick_and_amount (app.py) — ✅ 수정 완료

- **초기 color (pending_color 없을 때)**: `color = '빨강' if pred == '정' else '검정'` 고정 매핑 → `_get_card_15_color_for_round(results_rl, pr)` 사용
- **줄 추종 시**: 이미 15번 카드 기준 적용됨
- **shape_only_latest_next_pick 줄 추종 시**: `color = '빨강' if pred == '정' else '검정'` 고정 매핑 → `_get_card_15_color_for_round(results, pr)` 사용

---

### 2.3 클라이언트: 줄 추종 시 bettingIsRed 오류 (app.py 10036-10041, 10071-10077) — ✅ 수정 완료

```javascript
// 수정 전: bettingIsRed = !!runLenObjCard.last;  // run_last는 정/꺽이지 색이 아님
// 수정 후:
var card15Bet = (typeof allResults !== 'undefined' && allResults && allResults.length >= 15 && typeof parseCardValue === 'function') ? parseCardValue(allResults[14].result || '') : null;
var is15RedBet = card15Bet ? card15Bet.isRed : null;
if (is15RedBet === true || is15RedBet === false) {
    bettingIsRed = (bettingText === '정') ? is15RedBet : !is15RedBet;
} else { bettingIsRed = (bettingText === '정'); }
```

- **문제**: `runLenObjCard.last`는 직전 결과(정=true, 꺽=false)이다. **색(빨강/검정)이 아님**
- **수정**: 15번 카드(displayResults[14]) 기준으로 `bettingIsRed` 계산. 메인 블록·shapeOnly 블록 둘 다 적용

---

### 2.4 클라이언트: 결과 반영 시 줄 추종 betColor (app.py 7956-7958, 8025-8027)

```javascript
if (runLenObj.run >= 4 && runLenObj.last != null) {
    pred = runLenObj.last ? '정' : '꺽';
    betColor = pred === '정' ? '빨강' : '검정';  // ← 15번 카드 무시!
}
```

- **문제**: 고정 매핑
- **영향**: pending→완료 전환 시 잘못된 pickColor가 history에 저장

---

### 2.5 클라이언트: predForRound.color vs 줄 추종

- `predForRound.color`(lastPrediction)는 서버에서 15번 기준으로 계산된 값
- 줄 추종 시 `pred`를 `run_last`로 덮어쓰면서 `betColor`를 **고정** "정=빨강"으로 설정
- predForRound.color를 사용하지 않아 15번 기준이 깨짐

---

### 2.6 merge 시 pickColor

- 완료 행은 이제 서버 predicted/pickColor 유지
- 서버 `bet_color_for_history`가 줄 추종에서 잘못되면, merge 후에도 잘못된 색이 유지됨

---

## 3. 수정 포인트 요약

| 위치 | 현재 | 수정 |
|------|------|------|
| app.py 2190 | `'빨강' if pred_for_calc == '정' else '검정'` | `_get_card_15_color_for_round` 사용 |
| app.py 2573 | `'빨강' if pred == '정' else '검정'` | 15번 카드 기준 색 계산 |
| app.py 7958, 8027 | `pred === '정' ? '빨강' : '검정'` | 15번 카드 기준 (parseCardValue(disp[14])) |
| app.py 10036-10041, 10071-10077 | `bettingIsRed = !!runLenObjCard.last` | 15번 카드 기준 `parseCardValue(allResults[14])` → `(bettingText === '정') ? is15Red : !is15Red` ✅ |

---

## 4. 15번 카드 출처 (클라이언트)

- **최신 회차 15번 카드**: `allResults[14]` 또는 `displayResults[14]` (구조에 따라 상이)
- `parseCardValue(card.result)` → `card.isRed` (true=빨강, false=검정)
- 현재 round의 15번 카드 = 예측 대상 회차의 15번 카드. 최신 게임(results[0])의 15번 = results[14] (서버), 또는 클라이언트 displayResults 구조에 맞게 조회

---

## 5. 관련 코드 위치

| 기능 | 파일 | 행 |
|------|------|-----|
| 서버 bet_color_for_history (줄 추종) | app.py | 2188-2190 |
| 서버 _server_calc_effective_pick_and_amount (줄 추종) | app.py | 2571-2573 |
| 클라이언트 결과 반영 betColor (줄 추종) | app.py | 7956-7958, 8025-8027 |
| 클라이언트 bettingIsRed (줄 추종) | app.py | 10036-10041, 10071-10077 |
| 계산기표 pickClass 렌더 | app.py | 10409-10426 |

---

*이 문서는 픽·색 불일치 원인 분석용이다. 수정 시 PREDICTION_AND_RESULT_SPEC.md, prediction-result-spec-immutable.mdc를 준수할 것.*
