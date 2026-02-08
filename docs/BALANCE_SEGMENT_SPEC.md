# 밸런스 구간·전환점 스펙 (같은 게 나오는 확률 오르내림)

그래프 밸런스(같은 게 나오는 확률)가 **높아졌다 → 낮아졌다 → 높아졌다** 반복되는 구간을 정의하고, **전환점**을 캐치해서 `compute_prediction`의 `line_w` / `pong_w`에 소폭 보정을 거는 방법을 구체적으로 정리한 문서입니다.

---

## 1. 입력·전제

- **입력**: `graph_values` (이미 `compute_prediction`에서 사용 중)
  - `graph_values[i]` = 최신부터 i번째 구간의 "0번·15번 카드 색 같음" 여부
  - `True` = 같은 게 나옴(정/줄), `False` = 바뀜(꺽/퐁당)
  - 인덱스 0이 **가장 최신**
- **밸런스**: 한 구간에서 "같은 게 나온 비율" = 해당 윈도우 내 `True` 비율 (0.0 ~ 1.0 또는 0~100%)

---

## 2. 밸런스 곡선 만들기

### 2-1. 1차: 구간별 밸런스

- **윈도우 길이** `W_BALANCE = 10` (튜닝 범위: 8 ~ 15)
- 각 위치 `i`에 대해:
  - `window_i = graph_values[i : i + W_BALANCE]`
  - `valid = [v for v in window_i if v is True or v is False]`
  - `balance_raw[i] = (valid에서 True인 개수) / len(valid)` (valid가 비면 `None`)
- `i`는 `0` ~ `len(graph_values) - W_BALANCE`까지만 유효.
- **최신(현재) 구간**에 해당하는 값은 `balance_raw[0]` = 최근 10구간에서 같은 게 나온 비율.

### 2-2. 2차: 스무딩 (선택)

- **스무딩 윈도우** `SMOOTH = 5` (튜닝 범위: 3 ~ 7)
- `balance_smooth[i] = average(balance_raw[i : i + SMOOTH])` (None 제외하고 평균, 부족하면 있는 만큼만)
- **현재 밸런스**로 쓸 값:
  - 옵션 A: `current_balance = balance_raw[0]` (스무딩 없이 최근 W_BALANCE만)
  - 옵션 B: `current_balance = balance_smooth[0]` (스무딩 적용)
- 구현 시 **옵션 A**로 시작해도 되고, 노이즈가 크면 **옵션 B**로 전환.

---

## 3. 구간 정의 (높은 구간 / 낮은 구간)

### 3-1. 기준 구간

- **기준 길이** `L_REF = 60` (튜닝 범위: 40 ~ 100)
- `balance_raw` 또는 `balance_smooth`에서 인덱스 `0` ~ `min(L_REF, len(유효한 값))` 구간을 모아 **기준 분포**로 씀.
- 즉, “최근 60개 구간(또는 있는 만큼)”의 밸런스 값들.

### 3-2. 높음/낮음 임계값

**방법 1: 백분위(권장)**

- 기준 구간 값들로 백분위 계산:
  - `P_HIGH = 60` → 상위 40% 진입선
  - `P_LOW = 40` → 하위 40% 진입선
- `threshold_high = percentile(기준 구간, P_HIGH)`  
  `threshold_low = percentile(기준 구간, P_LOW)`
- 데이터가 적으면(예: 20개 미만) **구간 판별 스킵** → 보정 없음.

**방법 2: 고정 비율**

- `threshold_high = 0.60` (60% 이상이면 “높은 구간”)
- `threshold_low = 0.40` (40% 이하면 “낮은 구간”)
- 두 임계값 사이면 “중간” → 전환 판별 시에는 **직전 구간 유지** 또는 “중간”으로 두고 보정 안 함.

### 3-3. 현재 구간 라벨

- `current_balance`가 `None`이면 → 구간 없음, 보정 없음.
- `current_balance >= threshold_high` → `segment = 'high'`
- `current_balance <= threshold_low` → `segment = 'low'`
- 그 사이 → `segment = 'mid'` (또는 직전 segment 유지해서 전환만 감지)

---

## 4. 전환점 정의

### 4-1. 직전 구간

- **직전 밸런스**는 “한 스텝 이전” 구간의 밸런스로 정의:
  - `prev_balance = balance_raw[1]` 또는 `balance_smooth[1]` (스무딩 쓸 때)
- 같은 임계값으로 `prev_segment` 계산: `'high'` / `'low'` / `'mid'`.

### 4-2. 전환 이벤트

- **높은 구간 → 낮은 구간**: `prev_segment == 'high'` 이고 `segment == 'low'`
- **낮은 구간 → 높은 구간**: `prev_segment == 'low'` 이고 `segment == 'high'`
- `'mid'`는 “전환으로 인정하지 않음”으로 두면, 노이즈로 인한 잦은 전환을 줄일 수 있음.

### 4-3. “전환 직후”만 쓰기 (선택)

- **전환 직후 N 스텝**: 전환 발생 후 1~2회차만 보정 적용하고 이후에는 적용 안 함.
- 구현 시에는 **매 회차** “지금 전환이냐?”만 보면 되고, “N 스텝”은 나중에 플래그로 넣을 수 있음.
- 1차 구현: **전환이 발생한 그 회차**에만 보정 적용해도 됨.

---

## 5. compute_prediction에 넣을 출력

### 5-1. 함수 시그니처 제안

```
balance_phase = _balance_segment_phase(graph_values, ...)
```

- **입력**: `graph_values` (최소 길이 권장: `W_BALANCE + L_REF` 또는 50 이상이면 유리).
- **반환**: 다음 중 하나
  - `'transition_to_low'`  : 밸런스가 높은 구간 → 낮은 구간으로 꺾임 (같은 게 나올 비율 하락 국면)
  - `'transition_to_high'`  : 밸런스가 낮은 구간 → 높은 구간으로 꺾임 (같은 게 나올 비율 상승 국면)
  - `None` : 위 두 가지가 아님 (보정 없음)

- 필요하면 `segment`(현재 구간)도 반환해 로그/디버그용으로 사용 가능.

### 5-2. 데이터 부족 시

- `len(graph_values) < W_BALANCE + 5` 정도면 `balance_raw`가 2개 이상 나오기 어려우므로 **None 반환** (보정 없음).

---

## 6. 가중치 보정 (line_w / pong_w)

- **적용 위치**: `compute_prediction` 안에서, 기존 퐁당/덩어리 phase·V자·U자·연패 보정 **다 적용한 뒤**, **정규화(total_w) 직전**에 한 번만 적용.

| balance_phase         | 의미                         | 보정 (예시, 튜닝 범위) |
|-----------------------|------------------------------|-------------------------|
| `'transition_to_low'` | 같은 게 나올 비율이 줄어드는 국면 | `pong_w += 0.06`, `line_w = max(0, line_w - 0.03)` |
| `'transition_to_high'`| 같은 게 나올 비율이 늘어나는 국면  | `line_w += 0.06`, `pong_w = max(0, pong_w - 0.03)` |
| `None`                | 해당 없음                    | 보정 없음               |

- **튜닝 범위**: 증분은 `0.04 ~ 0.10` 사이에서 조정. 기존 phase 보정보다 작게 가져가면 기존 로직을 덮어쓰지 않고 보조만 함.

---

## 7. 파라미터 요약표

| 이름           | 권장값 | 튜닝 범위 | 설명 |
|----------------|--------|-----------|------|
| `W_BALANCE`    | 10     | 8 ~ 15    | 밸런스 계산 시 한 구간의 윈도우 길이 |
| `SMOOTH`       | 5      | 3 ~ 7     | 스무딩 윈도우 (스무딩 쓸 때) |
| `L_REF`        | 60     | 40 ~ 100  | 기준 분포용 구간 길이 |
| `P_HIGH`       | 60     | 55 ~ 65   | “높은 구간” 하한 백분위 (%) |
| `P_LOW`        | 40     | 35 ~ 45   | “낮은 구간” 상한 백분위 (%) |
| 고정 비율 대안 | 0.6 / 0.4 | -   | threshold_high / threshold_low |
| 보정량 (pong_w/line_w) | ±0.06 / ∓0.03 | ±0.04 ~ ±0.10 | 전환 시 가중치 증분 |

---

## 8. 구현 순서 제안

1. **`_balance_raw_series(graph_values, window=10)`**  
   - `graph_values`로부터 구간별 밸런스(비율) 리스트 반환. None 처리 포함.
2. **`_balance_segment_phase(graph_values, ...)`**  
   - 위 2~5절대로 현재/직전 구간·전환 여부 계산 후 `'transition_to_low'` / `'transition_to_high'` / `None` 반환.
3. **`compute_prediction` 내**  
   - phase·chunk·V자·U자 보정 다음, `balance_phase = _balance_segment_phase(graph_values)` 호출.  
   - `balance_phase`에 따라 `line_w`, `pong_w` 소폭 가감 후 기존대로 정규화 및 확률 계산.

이 스펙대로 구현하면 “밸런스가 오르내리는 부분”을 구간·전환점으로 캐치해, 현재 쓰는 예측 공식에 추가 보정만 넣을 수 있습니다.
