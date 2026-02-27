# 예측픽 구조 변경안

현재 `compute_prediction` 로직을 **올리는 방향 / 직진 방향 / 연패 시 구간 전환** 관점으로 재구성하는 방안.

---

## 1. 현재 구조 (요약)

```
[입력] graph_values, prediction_history
    ↓
line_pct, pong_pct (최근 15회)
    ↓
flow_state (line_strong / pong_strong) → line_w, pong_w
    ↓
overall_pong → pong_w 또는 line_w
    ↓
symmetry_line_data → line_w 또는 pong_w
    ↓
phase (line_phase, chunk_*, pong_phase, chunk_to_pong, None) → line_w, pong_w
    ↓
shape_win_stats, chunk_profile_stats, pattern_match, ngram → line_w, pong_w
    ↓
정규화 → adj_same, adj_change → predict (정/꺽)
```

**문제점**: line_w/pong_w 가산이 여러 곳에 흩어져 있어, "올리는 방향" vs "직진 방향" 의도가 코드에서 드러나지 않음.

---

## 2. 제안 구조

### 2-1. 4단계 파이프라인

```
[1단계] 밸런스·구간 판단
    - pong_pct, line_pct (최근 15회)
    - phase (줄/덩어리/퐁당/전환)
    - 덩어리 많음? 퐁당 많음?

[2단계] 방향 가산 (올리는 vs 직진)
    - 덩어리·줄 많음 → 올리는 방향(line_w) 가산
    - 퐁당 많음 → 직진 방향(pong_w) 가산

[3단계] 픽 결정
    - line_w vs pong_w → 유지 vs 바뀜
    - last 반영 → 정 또는 꺽

[4단계] 연패 시 구간 전환
    - 연패 중 + 구간 잘못 봤을 가능성 → 방향 소폭 전환
```

### 2-2. 코드 구조 변경 (의사코드)

```python
def compute_prediction(results, prediction_history, ...):
    # === 1단계: 밸런스·구간 판단 ===
    pong_pct, line_pct = _pong_line_pct(graph_values[:15])
    phase, chunk_shape = _detect_pong_chunk_phase(...)
    덩어리_많음 = (phase in ('line_phase', 'chunk_start', 'chunk_phase', 'pong_to_chunk') 
                   and line_pct > pong_pct) or (col_heights에서 장줄·덩어리 많음)
    퐁당_많음 = (phase in ('pong_phase', 'chunk_to_pong') 
                 and pong_pct > line_pct) or overall_pong

    # === 2단계: 방향 가산 (올리는 vs 직진) ===
    line_w = line_pct / 100.0   # 올리는 방향 (같은 픽 유지)
    pong_w = pong_pct / 100.0   # 직진 방향 (번갈아 픽)

    if 덩어리_많음:
        line_w = _apply_올리는_방향_가산(line_w, pong_w, phase, chunk_shape, ...)
    if 퐁당_많음:
        pong_w = _apply_직진_방향_가산(line_w, pong_w, phase, ...)

    # symmetry, shape_win_stats, pattern_match 등 기존 보조 가산
    line_w, pong_w = _apply_보조_가산(line_w, pong_w, ...)

    # === 3단계: 픽 결정 ===
    total_w = line_w + pong_w
    line_w, pong_w = line_w / total_w, pong_w / total_w
    adj_same = prob_same * line_w
    adj_change = prob_change * pong_w
    predict = ('정' if last else '꺽') if adj_same >= adj_change else ('꺽' if last else '정')

    # === 4단계: 연패 시 구간 전환 (예측픽 단계 반영) ===
    lose_streak = _get_lose_streak_from_history(prediction_history)
    if lose_streak >= 2 and prediction_history:
        # 구간 잘못 봤을 가능성 → 반대 방향으로 소폭 기울임
        blended_wr = _blended_win_rate(prediction_history)
        if blended_wr and blended_wr < 48:
            predict = reverse_pred(predict)  # 또는 adj_same_n, adj_change_n 비율로 소폭 전환
```

---

## 3. 구체적 변경 포인트

### 3-1. 함수 분리 (가독성)

| 현재 | 변경 후 |
|------|---------|
| compute_prediction 내 400줄+ | `_apply_line_boost()` (올리는 방향) |
| phase별 if/elif 산재 | `_apply_pong_boost()` (직진 방향) |
| | `_apply_phase_adjustments()` (phase→line_w/pong_w) |

### 3-2. 주석 통일

기존 `line_w += 0.12` 등에 아래 주석 패턴 적용:

```python
# [올리는 방향] 줄 구간: 같은 픽 유지
line_w += 0.12

# [직진 방향] 퐁당 구간: 번갈아 픽
pong_w += 0.10
```

### 3-3. 4단계 연패 반영 (선택)

**현재**: 스마트반픽은 계산기(배팅) 단계에서만 적용. 예측픽 자체는 연패를 보지 않음.

**변경안**: `compute_prediction`에 `prediction_history`가 있으면, 연패 2회 이상 + 경고승률 48% 미만일 때 **예측픽 자체를 반대로** 전환.

```python
# compute_prediction 마지막, predict 결정 직후
if prediction_history and len(prediction_history) >= 5:
    lose_streak = _get_lose_streak_from_history(prediction_history)
    wr = _blended_win_rate(prediction_history)
    if lose_streak >= 2 and wr is not None and wr < 48:
        predict = reverse_pred(predict)
        # 또는: adj_same_n, adj_change_n 비율 뒤집어서 소폭만 전환
```

**주의**: 예측기표 = 메인 예측기 픽 고정 규칙과 충돌 가능. 연패 반영은 **계산기 배팅 픽**에만 두고, **예측픽(예측기표)에는 넣지 않는 것**이 규칙상 안전함.

---

## 4. 적용 순서 제안

1. **1차**: 주석 통일 (`[올리는 방향]` / `[직진 방향]`) — ✅ 적용 완료
2. **2차**: `_apply_phase_line_pong_adjustments()` 함수 분리 — ✅ 적용 완료
3. **3차**: 4단계 연패 반영 — ❌ 스킵 (예측기표 = 메인 픽 고정 원칙. 연패 반영은 스마트반픽(계산기)에서만)

---

## 6. 좌우 줄 평균(avgLeft_line, avgRight_line) → 올리는 방향 가산

줄의 평균 높이(좌우 run 길이 평균)로 "어디까지 올릴지" 규칙. **줄만(길이≥2)** 평균 사용 — 덩어리·줄 구간에서 퐁당(1) 제외해 정확한 줄 높이 반영.

| avg_mean_line = (avgLeft_line+avgRight_line)/2 | lineSimilarityPct | line_w 가산 | 의미 |
|---------------------------------|-------------------|-------------|------|
| ≥ 3.5 | - | +0.05 | 줄이 김 → 올리는 방향 |
| ≥ 2.8, < 3.5 | - | +0.03 | 중간 줄 |
| ≥ 2.2, < 2.8 | ≥ 70 | +0.02 | 좌우 비슷 + 줄 있음 |
| < 2.2 | - | 0 | 퐁당 쪽에 가까움 |

- avgLeft/avgRight: 전체 run(줄+퐁당) 평균. 표시·lineSimilarityPct 폴백용.
- avgLeft_line/avgRight_line: 줄만(길이≥2) 평균. 예측픽 line_w 가산용.
- 상한: 기존 symmetry·phase 가산과 합쳐도 line_w가 과도해지지 않도록 ±0.05 이내.

---

## 7. 덩어리/줄 사이 퐁당 간격

덩어리 사이 또는 줄 사이에 퐁당이 몇 개씩 끼어 있는지(간격)가 중요.

| avg_pong_gap (최근 퐁당 run 평균) | 방향 | line_w/pong_w | 의미 |
|----------------------------------|------|---------------|------|
| ≤ 1.5 | 올리는 | +0.03 | 짧은 간격 → 덩어리/줄이 가깝게 붙어있음 |
| ≥ 3.0 | 직진 | +0.03 | 긴 간격 → 퐁당이 김, 번갈아 픽 |
| 1.5 ~ 3.0 | - | 0 | 중간 |

---

## 8. 장퐁당(끊김) — 퐁당 몇 개까지?

퐁당 run이 길어지면 줄로 전환될 가능성이 높아짐. 장줄(5+)과 대칭.

| 조건 | 방향 | line_w/pong_w | 의미 |
|------|------|---------------|------|
| pong_phase + pong_runs[0] ≥ 5 | 올리는 | +0.05 / -0.025 | 퐁당 끊김 예상 → 줄 전환 |

**임계값 조정**: `analyze_pong_run_limit.py`로 CSV 분석 후, 퐁당 run 길이별 다음 결과(정/꺽) 분포 확인. 정 52%+ 나오기 시작하는 길이를 임계값으로 사용.

---

## 5. 요약

| 항목 | 현재 | 변경 후 |
|------|------|---------|
| 구조 | line_w/pong_w 가산이 여러 곳에 산재 | 1~4단계 파이프라인으로 정리 |
| 의도 | 코드만 봐서는 "올리는/직진" 구분 어려움 | 주석·함수명으로 명시 |
| 연패 | 예측픽 단계에서 미반영 | (선택) 규칙 허용 시 4단계에서 반영 |
