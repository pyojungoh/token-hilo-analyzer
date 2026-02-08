# 퐁당 ↔ 덩어리 구간 캐치 구현 가이드

"퐁당 주다 덩어리 만들다" 패턴을 잡아서, **지금이 퐁당 구간인지 덩어리(줄) 구간인지** 판별하고, 그에 맞게 **줄/퐁당 가중치(line_w, pong_w)**를 조정하는 방법입니다.

---

## 1. 이미 있는 재료 (app.py 기준)

- **`_get_line_pong_runs(use_for_pattern)`**  
  - `use_for_pattern = graph_values[:30]`  
  - 반환: `line_runs`, `pong_runs`  
  - **맨 앞 run**: 시간순으로 최신이 먼저이므로, `graph_values[0]`과 `graph_values[1]`이 같으면 첫 run은 **줄**, 다르면 **퐁당**.
- **첫 run 길이**
  - 첫 run이 줄이면: `current_line_run = line_runs[0]`
  - 첫 run이 퐁당이면: `current_pong_run = pong_runs[0]`
- **비율**
  - `_pong_line_pct(graph_values[:15])` → 최근 15개 퐁당%/줄%
  - `_pong_line_pct(graph_values[15:30])` → 직전 15개 퐁당%/줄%
- **현재 같은 결과 연속 길이**
  - 이미 `current_run_len`으로 계산 중 (연패 길이 보정에서 사용).

이걸 조합해서 "지금 구간"을 퐁당/덩어리로 나누고, 가중치만 조정하면 됩니다.

---

## 2. 구현 단계

### 2-1. 구간 판별 함수 추가

**역할**: "지금이 퐁당 구간인지, 덩어리 구간인지, 전환 중인지"를 판별해 반환.

**입력**

- `line_runs`, `pong_runs` (최근 30개 기준, `_get_line_pong_runs` 결과)
- `graph_values_head` = `graph_values[:2]` (최신 2개 → 첫 run이 줄인지 퐁당인지)
- `pong_pct_short` = 최근 15개 퐁당%, `pong_pct_prev` = 직전 15개(15~30) 퐁당%

**판별 기준 예시**

| 상황 | 판별 | 의미 |
|------|------|------|
| 첫 run이 **퐁당**이고 길이 1~2 | `pong_phase` | 지금 퐁당 구간. 다음에도 바뀔 가능성 있음 → 퐁당 가중치 올리기 |
| 첫 run이 **줄**이고 길이 1 | `chunk_start` | 덩어리 막 시작. 한 번 더 같은 쪽 나올 수 있음 → 줄 가중치 살짝 올리기 |
| 첫 run이 **줄**이고 길이 2~4 | `chunk_phase` | 덩어리 만드는 중. 유지 쪽 보정 |
| 첫 run이 **줄**이고 길이 5+ | `chunk_long` | 긴 덩어리. 곧 끊길 수 있음 → 퐁당 가중치 살짝 올리기(기존 V자/U자와 유사) |
| 직전 15개는 퐁당 쪽(퐁당% 높음), 최근 15개는 줄 쪽(줄% 높음) | `pong_to_chunk` | 퐁당에서 덩어리로 전환 중 → 줄 가중치 올리기 |
| 직전 15개는 줄 쪽, 최근 15개는 퐁당 쪽 | `chunk_to_pong` | 덩어리에서 퐁당으로 전환 중 → 퐁당 가중치 올리기 |

**반환**

- 예: `'pong_phase' | 'chunk_start' | 'chunk_phase' | 'chunk_long' | 'pong_to_chunk' | 'chunk_to_pong' | None`
- `None`이면 기존 공식만 사용(추가 보정 없음).

**위치**

- `_detect_v_pattern`, `_detect_u_35_pattern` 근처에  
  `_detect_pong_chunk_phase(line_runs, pong_runs, graph_values_head, pong_pct_short, pong_pct_prev)` 같은 함수로 두면 됨.

---

### 2-2. 전환 구간 판별 (직전 vs 최근)

- `pong_pct_prev` = `graph_values[15:30]` 기준 퐁당%
- `pong_pct_short` = `graph_values[:15]` 기준 퐁당%
- **퐁당 → 덩어리**: `pong_pct_prev - pong_pct_short >= 20` 정도면 “최근에 줄이 많아졌다” → `pong_to_chunk`
- **덩어리 → 퐁당**: `pong_pct_short - pong_pct_prev >= 20` 정도면 “최근에 퐁당이 많아졌다” → `chunk_to_pong`

임계값(20)은 나중에 15~25 사이로 튜닝 가능.

---

### 2-3. `compute_prediction` 안에서 가중치 반영

지금 **V자 / U자+3~5 / 연패 길이 보정** 적용한 뒤, **정규화(total_w) 전**에 다음만 추가:

1. `phase = _detect_pong_chunk_phase(...)` 호출.
2. `phase`에 따라:
   - `pong_phase` 또는 `chunk_to_pong` → `pong_w += 0.08` ~ `0.12`, `line_w = max(0, line_w - 0.04)` 정도.
   - `chunk_start` 또는 `chunk_phase` 또는 `pong_to_chunk` → `line_w += 0.06` ~ `0.10`, `pong_w = max(0, pong_w - 0.03)` 정도.
   - `chunk_long` → 기존 V자와 비슷하게 퐁당 쪽 살짝 올리기 (예: `pong_w += 0.06`).
   - `None` → 보정 없음.

3. 그 다음 기존처럼 `total_w = line_w + pong_w`로 정규화.

이렇게 하면 “퐁당 주다가 덩어리 만들다”를 **구간별로 캐치**해서, 퐁당 구간에서는 바뀜을, 덩어리 구간에서는 유지를 더 믿게 됩니다.

---

## 3. 기존 보정과의 관계

- **V자 / U자+3~5 / 연패 길이 보정**은 그대로 두고, **그 다음**에 퐁당/덩어리 phase 보정을 **한 번 더** 적용.
- phase 보정량은 기존 보정보다 작게(예: ±0.06~0.10) 두어서, 기존 로직을 덮어쓰지 않고 보조만 하게 하는 게 안전함.
- 나중에 통계 보면서 `chunk_start`만 쓰거나, `pong_to_chunk`/`chunk_to_pong`만 쓰는 식으로 단순화해도 됨.

---

## 4. 요약

| 단계 | 내용 |
|------|------|
| 1 | `_detect_pong_chunk_phase(...)` 추가: 첫 run 타입/길이 + 직전 15 vs 최근 15 퐁당%로 phase 판별 |
| 2 | `compute_prediction`에서 phase 결과에 따라 정규화 전에 `line_w`/`pong_w`만 소폭 가감 |
| 3 | 기존 V자/U자/연패 보정 유지, phase 보정은 마지막에 한 번만 적용 |

이렇게 구현하면 “퐁당 주다 덩어리 만들다” 반복을 **구간 단위로 캐치**해서, 퐁당일 때는 바뀜을, 덩어리일 때는 유지를 더 믿도록 가중치를 바꿀 수 있습니다.
