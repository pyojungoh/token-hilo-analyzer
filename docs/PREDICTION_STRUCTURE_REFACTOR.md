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

## 5. 요약

| 항목 | 현재 | 변경 후 |
|------|------|---------|
| 구조 | line_w/pong_w 가산이 여러 곳에 산재 | 1~4단계 파이프라인으로 정리 |
| 의도 | 코드만 봐서는 "올리는/직진" 구분 어려움 | 주석·함수명으로 명시 |
| 연패 | 예측픽 단계에서 미반영 | (선택) 규칙 허용 시 4단계에서 반영 |
