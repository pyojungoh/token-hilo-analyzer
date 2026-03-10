# 계산기표 2행(수익·결과) 반영 지연 — 정밀 진단

## 1. 2행이란

- **1행**: 대기(배팅중) — 현재 회차, actual='pending'
- **2행**: 방금 완료된 회차 — actual(승/패/조커), 배팅금액, 수익

표는 `usedHist`를 역순으로 그리므로: `usedHist[length-1]`=1행, `usedHist[length-2]`=2행.

---

## 2. 데이터 흐름

```
[서버] apply (0.1초마다)
  get_recent_results(DB) → pending_round에 actual 있으면
  history에 actual·profit 반영 → save_calc_state → _calc_state_get_cache 갱신

[클라이언트] loadResults (120ms 폴링)
  /api/results → prediction_history, round_actuals 수신
  syncCalcHistoryFromServerPrediction: prediction_history.actual → calc history pending 행에 반영
  ❌ updateAllCalcs() 호출 없음! → 2행 미렌더
  loadCalcStateFromServer 트리거 (100ms 스로틀)
  loadCalcState 응답 → applyCalcsToState → updateAllCalcs() → 2행 렌더
```

---

## 3. 핵심 원인

**syncCalcHistoryFromServerPrediction**에서 prediction_history로 calc history의 pending 행에 actual을 채운 뒤, **updateAllCalcs()를 호출하지 않음.**

- sync 시점에 이미 actual이 있으므로 roundToBetProfit 시뮬레이션으로 수익·결과 계산 가능
- 하지만 화면 갱신은 loadCalcState 응답 후 updateAllCalcs()에서만 수행
- 그사이 100~350ms(loadCalcState 요청+응답) 지연 발생

---

## 4. 수정 방향

syncCalcHistoryFromServerPrediction 내부에서 `changed`일 때 **즉시 updateAllCalcs()** 호출.

- prediction_history에 새 actual이 있으면 sync로 calc history 갱신
- 이 시점에 updateAllCalcs() 호출 → 2행(수익·결과) 즉시 렌더
- loadCalcState는 서버와 최종 동기화용으로 유지

---

## 5. 기타 지연 요인 (참고)

| 요인 | 영향 |
|------|------|
| 외부 fetch (0.5~4초) | 결과가 DB에 들어오기 전까지 apply 대기 |
| apply_lock 경합 | apply가 200ms+ 걸리면 다음 apply 스킵 |
| calc_state 캐시 150ms | DB 대신 캐시 사용으로 응답 가속 |
