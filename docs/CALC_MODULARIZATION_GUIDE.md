# 계산기 모듈 분리 가이드 (OOM 방지 단계별)

**✅ 계산기 분리 완료** (3.1~3.5 적용됨. calc_handlers.py 사용 중.)

app.py에서 계산기 관련 코드만 별도 모듈로 옮깁니다. **매 단계마다 app.py를 Cursor에서 열지 않고, 스크립트/패치만 사용**합니다.

---

## 1. 대상 정리

### 이미 다른 모듈에 있는 것 (옮기지 않음)
| 항목 | 위치 |
|------|------|
| get_calc_state, save_calc_state | db.py |
| _apply_results_to_calcs, _server_calc_effective_pick_and_amount | apply_logic.py |
| _update_calc_paused_after_round, _calculate_calc_profit_server | prediction_logic.py |

### app.py에 남아 있는 계산기 관련 (옮길 대상)
| 함수 | app.py 라인(참고) | 용도 |
|------|-------------------|------|
| _merge_calc_histories | 541~ | 회차별 calc history 병합 |
| _get_all_calc_session_ids | 588~ | 실행 중인 계산기 세션 ID 목록 |
| api_calc_state | 8013~ | GET/POST /api/calc-state 핸들러 |

- **의존 관계**: `api_calc_state` 가 `_merge_calc_histories` 사용. `apply_logic._apply_results_to_calcs` 가 `_get_all_calc_session_ids` 사용(현재 app에서 가져옴).
- **옮긴 뒤**: `calc_handlers.py`(또는 `handlers/calc.py`) 하나에 위 3개를 두고, app.py는 해당 모듈에서 import만 하도록 정리.

---

## 2. OOM 방지 원칙 (반드시 지킬 것)

- **app.py를 Cursor에서 열지 않는다.** 필요한 위치는 `grep`으로만 확인.
- **app.py 수정은 스크립트로만 한다.**  
  - 추가: “특정 문자열 다음에 한 줄 삽입” 스크립트  
  - 제거: “특정 라인 범위 또는 패턴(데코레이터 등)만 제거” 스크립트  
  - 모든 스크립트는 **한 줄씩 읽고 쓰기** (전체 로드 금지).
- **한 번에 하나씩만** 옮기고, 매 단계 끝에 `python -c "from app import app; print('OK')"` 로 확인.
- 문제 생기면 `git checkout app.py` 또는 해당 파일만 복구 후, 그 단계부터 다시.

---

## 3. 단계별 작업 순서

### 3.0 준비
- [ ] `git status` 로 작업 트리 정리. 필요 시 커밋 한 번 하고 진행.
- [ ] 복원용 태그 있으면 확인: `git tag -l`

### 3.1 calc_handlers.py 생성 (app.py 수정 없음)
- **할 일**: 새 파일 `calc_handlers.py` 만 생성.
- **내용**:  
  - 상단에 `# 계산기 전용: _merge_calc_histories, _get_all_calc_session_ids, api_calc_state` 등 주석만 넣고,  
  - `from db import get_calc_state, save_calc_state` 등 필요한 import만 넣어 두기.  
  - 함수 본문은 **아직 비우지 말고**, 이 단계에서는 **파일만 만든다**.
- **OOM**: 새 파일만 추가하므로 app.py 미사용. 안전.
- **검증**: `python -c "import calc_handlers; print('OK')"` (import만 되면 됨).

### 3.2 _merge_calc_histories 추출
- **할 일**:  
  1. app.py에서 `_merge_calc_histories` **함수 정의 라인 번호**를 grep으로 확인.  
  2. “해당 라인부터 다음 `^def ` 전까지”를 **한 줄씩 읽어서** 추출하는 스크립트 작성 후 실행 → 추출된 내용을 `calc_handlers.py`에 붙여 넣기.  
  3. app.py에서는 **해당 함수 블록만 제거**하는 스크립트 실행(라인 범위로 제거, 한 줄씩 처리).  
  4. app.py 상단에 `from calc_handlers import _merge_calc_histories` 추가(한 줄 삽입 스크립트).
- **OOM**: app.py는 읽지 않고, grep·라인 범위 스크립트만 사용. 삽입/제거도 스크립트.
- **검증**: `python -c "from app import app; print('OK')"` 및 계산기 화면에서 history 병합 동작 확인.

### 3.3 _get_all_calc_session_ids 추출
- **할 일**:  
  1. grep으로 `_get_all_calc_session_ids` 정의 라인·끝 라인 확인.  
  2. 위와 동일하게 “추출 스크립트 → calc_handlers에 추가”, “app.py에서 해당 블록 제거 스크립트”, “app.py에 import 한 줄 추가 스크립트” 순서로 진행.  
  3. `apply_logic.py`에서 `_get_all_calc_session_ids` 를 쓰는 부분이 있으면, `from calc_handlers import _get_all_calc_session_ids` (또는 함수 내 lazy import)로 변경.
- **OOM**: 동일하게 app.py 전체 읽기 없이, 라인/패턴 기준 스크립트만 사용.
- **검증**: `from app import app` 및 계산기 회차 반영·세션 목록 동작 확인.

### 3.4 api_calc_state 추출
- **할 일**:  
  1. grep으로 `api_calc_state` 정의 라인·끝 라인 확인.  
  2. “추출 스크립트”로 해당 블록만 읽어서 `calc_handlers.py`에 추가.  
  3. `calc_handlers` 안에서 `request`, `get_calc_state`, `save_calc_state`, `_merge_calc_histories`, prediction_logic·apply_logic 등 필요한 것만 import (순환되지 않게).  
  4. app.py에서 해당 블록 제거 스크립트 실행.  
  5. app.py에 `from calc_handlers import api_calc_state` 추가.  
  6. `routes_api.py`의 `register_api_routes(app)` 안에서 `api_calc_state` 를 **app이 아니라 calc_handlers에서** 가져오도록 수정: `from calc_handlers import api_calc_state` 후 등록.
- **OOM**: app.py는 계속 “라인 범위 제거 + 한 줄 삽입” 스크립트만 사용. Cursor에서 app.py 열지 않기.
- **검증**: `from app import app` 및 `/api/calc-state` GET·POST 호출, 계산기 UI 동작 확인.

### 3.5 정리 및 문서 ✅ 완료
- [x] `docs/MODULARIZATION_STEPS.md` 및 본 가이드에 “계산기 분리 완료” 표시.
- [x] `calc_handlers.py` 상단 docstring에 제공 함수 나열 (public: api_calc_state, internal: _merge_calc_histories, _get_all_calc_session_ids).

---

## 4. 스크립트 작성 시 공통 규칙

- **추출**: `open('app.py')` 로 한 줄씩 읽고, “시작 라인 ≤ i ≤ 끝 라인”일 때만 버퍼에 넣어서 `calc_handlers.py`에 append. app.py 전체를 한 번에 읽지 않기.
- **제거**: app.py를 한 줄씩 읽고, “현재 라인 번호가 제거 대상 범위에 있으면 쓰지 않음” → 새 파일에 쓰고 `replace(app.py.new, app.py)`.
- **삽입**: “특정 문자열(예: `from prediction_logic import`)이 포함된 줄 다음에 한 줄 삽입” 방식. 파일 전체를 메모리에 올리지 않고 한 줄씩 읽으며, 해당 줄 다음에만 새 줄 삽입.

---

## 5. 롤백

- **app.py만 복구**: `git checkout app.py`
- **전체 복구**: `git checkout backup-modular-4step` 등 이전 태그/커밋으로 되돌리기.
- 단계별로 커밋해 두면, 문제 시 직전 단계로만 되돌리기 가능.

---

이 가이드는 “계산기만 단계별로 옮기기”와 “OOM 나지 않게 하기”에만 초점을 둡니다. 실제 스크립트 이름(예: `step_calc_01_extract_merge.py`)은 구현 시 정하면 됩니다.
