# 조커 통계 및 배팅 보류

## 개요

15카드 내 조커 개수·간격을 분석하고, 위험 구간에서는 **배팅을 자동 보류**합니다.

---

## 1. 조커 통계 (joker_stats)

| 항목 | 설명 |
|------|------|
| **count_in_15** | 화면에 보이는 15개 카드 내 조커 개수 |
| **count_in_30** | 최근 30카드 내 조커 개수 |
| **intervals** | 조커 간 간격(회차 수) 목록 (최근 10개) |
| **avg_interval** | 조커 간 평균 간격(회차) |
| **last_joker_rounds_ago** | 마지막 조커가 몇 회차 전인지 (0 = 최신 회차가 조커) |
| **warning** | 경고 여부 |
| **warning_reason** | 경고 사유 |
| **skip_bet** | 배팅 보류 여부 |

---

## 2. 배팅 보류 조건 (skip_bet = true)

| 조건 | 설명 |
|------|------|
| **15카드 내 조커 2개 이상** | 조커 다발 구간 — 배팅 보류 |
| **30카드 내 조커 3개 이상** | 조커 빈발 구간 — 배팅 보류 |

※ 15번 카드 조커는 기존대로 예측 픽 보류(compute_prediction에서 처리).

---

## 3. 경고만 표시 (skip_bet = false)

| 조건 | 설명 |
|------|------|
| **15카드 내 조커 1개** | 주의 표시, 배팅은 가능 |
| **조커 임박 가능성** | 평균 간격 ≤15회, 마지막 조커 5회 이내 — 참고용 경고 |

---

## 4. API·UI

- **API**: `/api/results` 응답에 `joker_stats` 포함
- **UI**: 예측기표 하단에 "15카드 조커 N개 · 30카드 N개 · 평균간격 N회 · 마지막 N회 전" 표시
- **배팅 보류 시**: 예측 픽 영역에 "조커 주의 · [warning_reason]" 표시, 계산기·매크로 배팅 0

---

## 5. 구현 위치

| 위치 | 역할 |
|------|------|
| `_compute_joker_stats(results)` | 조커 통계·경고·skip_bet 계산 |
| `_build_results_payload_db_only` | joker_stats 추가, skip_bet 시 server_pred value/color None |
| 클라이언트 `lastIs15Joker` | 15번 조커 또는 joker_stats.skip_bet 시 true |
| 클라이언트 `lastJokerStats` | 조커 통계 표시용 |
