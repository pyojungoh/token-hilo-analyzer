# 매크로 재구축 분석 — 금액·속도 문제 원인

## 1. 현재 데이터 흐름 (복잡·겹침)

```
[분석기 서버]
  ├─ POST /api/current-pick-relay (클라이언트 25ms) → relay 캐시 갱신
  │    └─ 조건: 1행 vs 상단, from_post, last_post_time...
  ├─ 스케줄러 0.3초마다 _push_current_pick_from_calc
  │    └─ 조건: last_post 0.5초 이내면 스킵
  └─ _update_current_pick_relay_cache → _ws_emit_pick_update
       └─ 50ms 후 2회 emit (2회 확인용)

[매크로]
  ├─ WebSocket pick_update 수신 → _on_ws_pick_received
  │    ├─ amt_val = pick.get("suggested_amount")
  │    ├─ 2회 확인: (round, pick, amount) 동일 2번
  │    ├─ PUSH_BET_DELAY 60ms
  │    └─ _run_bet → _do_bet
  ├─ fetch_current_pick (GET) — 연결 시 1회만
  └─ _run_push_server (POST /push-pick) — 미사용(WebSocket 사용 중)
```

---

## 2. 금액 관련 코드 경로 (겹침)

| 위치 | 역할 | 문제 |
|------|------|------|
| **서버** | | |
| app.py POST | 클라이언트 상단 vs 1행 검증, from_post | 조건 분기 많음 |
| app.py _push_current_pick_from_calc | 1행 번들 relay | last_post 0.5초 체크로 스킵 |
| app.py _update_current_pick_relay_cache | 캐시+emit | from_post 플래그 |
| app.py _ws_emit_pick_update | 2회 emit (50ms 간격) | 속도 지연 |
| **매크로** | | |
| emulator_macro L1209 | amt_val = pick.get("suggested_amount") | 단순 |
| emulator_macro L1256 | key = (round, pick, use_amt) 2회 확인 | 동일값 2번 대기 |
| emulator_macro L1271 | PUSH_BET_DELAY 60ms | 추가 지연 |
| emulator_macro L1311 | amount_from_calc → _do_bet | 최종 전달 |

---

## 3. 속도 지연 요약

| 구간 | 시간 | 비고 |
|------|------|------|
| 서버 2회 emit 간격 | 50ms | 2회 확인용 |
| 매크로 2회 확인 대기 | 0~50ms | 같은 key 2번 올 때까지 |
| PUSH_BET_DELAY | 60ms | 배팅 전 대기 |
| BET_DELAY_AFTER_AMOUNT_TAP | 10ms | 금액 칸 탭 후 |
| BET_DELAY_AFTER_INPUT | 10ms | 금액 입력 후 |
| BET_DELAY_AFTER_BACK | 60ms | 키보드 닫힌 뒤 |
| BET_DELAY_AFTER_COLOR_TAP | 10ms | 픽 버튼 탭 후 |
| BET_DELAY_AFTER_CONFIRM | 20ms | 정정 탭 후 |
| **총 (픽 수신→ADB 완료)** | **~220ms+** | |

---

## 4. ADB 배팅 순서 (유지)

```
1. 배팅금액 칸 탭 (tap_swipe)
2. 10ms 대기
3. adb_input_text(금액)
4. 10ms 대기
5. adb_keyevent(4) BACK
6. 60ms 대기
7. 레드/블랙 탭
8. 10ms 대기
9. 정정 탭
10. 20ms 대기
```

→ **ADB 연결·순서는 정확함. 변경 불필요.**

---

## 5. 문제 요약

1. **금액 경로 겹침**: 서버 POST(상단) vs 스케줄러(1행) vs last_post_time → 조건이 많아 타이밍에 따라 다른 값 전달
2. **2회 확인 + 2회 emit**: 50ms×2 + 60ms = 최소 110ms 지연
3. **매크로는 단순**: 받은 값 그대로 사용. 문제는 **서버에서 어떤 값이 오느냐**

---

## 6. 재구축 권장안

### A. 서버 단순화
- **단일 출처**: 클라이언트 POST 시 상단 금액 그대로 relay. 검증/1행 비교 제거.
- **스케줄러**: 0.3초마다 무조건 1행 relay (last_post 체크 제거). POST가 25ms마다 오므로 마지막 POST 값이 유지됨.
- **2회 emit**: 50ms → 20ms로 단축. 또는 1회만 emit (2회 확인 제거 시).

### B. 매크로 단순화
- **2회 확인**: 유지(잘못된 값 1회 수신 방지) 또는 1회로 완화(속도 우선)
- **지연 단축**: PUSH_BET_DELAY 60ms → 30ms
- **단일 진입점**: WebSocket만 사용. GET/push-pick 제거 또는 연결 확인용으로만.

### C. 최소 변경안 (빠른 적용)
1. 서버: POST 시 클라이언트 suggested_amount **무조건** relay (검증 제거)
2. 서버: 스케줄러 last_post 체크 제거 → 항상 1행 푸시
3. 서버: 2회 emit 간격 50ms → 20ms
4. 매크로: PUSH_BET_DELAY 60ms → 30ms

---

## 7. 적용 완료: macro_pick_transmit (전송 전용 DB)

```
[서버 1행]  _get_calc_row1_bundle(c)
       │
       ├─ POST: 1행 계산 → macro_pick_transmit 저장 → relay
       ├─ 스케줄러: 1행 계산 → macro_pick_transmit 저장 → relay
       ▼
[macro_pick_transmit]  calculator_id, round_num, pick_color, suggested_amount, running
       │
       └─ relay 캐시 ← 여기서만 읽음 (단일 출처)
```

- 클라이언트 값 사용 안 함. 서버 1행만 저장·전송.
- 2회 emit: 50ms → 20ms
- PUSH_BET_DELAY: 60ms → 30ms

---

## 8. ADB 관련 (변경 없음)

- `_do_bet` 내 `adb_swipe`, `adb_input_text`, `adb_keyevent` 순서
- `_apply_window_offset` 좌표 보정
- `load_coords()` emulator_coords.json
- `BET_DELAY_*` 상수들 (입력/탭 간 최소 대기)

→ **ADB 연결·실행 로직은 그대로 유지.**
