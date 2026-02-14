# 5단계 가이드: 라우트·블루프린트 분리

진행 전에 이 가이드를 따라 **순서와 범위**를 정한 뒤 작업합니다. 꼬이지 않도록 단계를 나눕니다.

---

## 1. 목표

- app.py의 **라우트 등록부**를 여러 파일로 나눔.
- **동작·URL·응답**은 그대로 유지 (사용자·매크로 영향 없음).
- OOM 방지: **새 파일을 만들고**, app.py는 **Blueprint 등록만** 최소 수정.

---

## 2. 현재 라우트 목록 (app.py 기준)

| URL | 함수 | 비고 |
|-----|------|------|
| `/` | index | 메인 |
| `/results` | results_page | 결과 페이지 (HTML) |
| `/betting-helper` | betting_helper_page | |
| `/practice` | practice_page | |
| `/docs/tampermonkey-auto-bet.user.js` | (스크립트) | |
| `/favicon.ico` | favicon | |
| `/health` | health_check | |
| **API** | | |
| `/api/results` | get_results | |
| `/api/current-prediction` | get_current_prediction | |
| `/api/calc-state` | api_calc_state | GET/POST |
| `/api/win-rate-buckets` | api_win_rate_buckets | |
| `/api/dont-bet-ranges` | api_dont_bet_ranges | |
| `/api/losing-streaks` | api_losing_streaks | |
| `/api/round-prediction` | api_save_round_prediction | POST |
| `/api/prediction-history` | api_save_prediction_history | POST |
| `/api/current-pick` | api_current_pick | GET/POST |
| `/api/server-time` | api_server_time | |
| `/api/current-status` | get_current_status | |
| `/api/streaks` | get_streaks | |
| `/api/streaks/<user_id>` | get_user_streak | |
| `/api/refresh` | refresh_data | POST |
| `/api/test-betting` | test_betting | |
| `/api/debug/db-status` | debug_db_status | |
| `/api/debug/init-db` | debug_init_db | POST |
| `/api/debug/results-check` | debug_results_check | |

---

## 3. 제안: Blueprint 나누기

| Blueprint | 파일 | 담당 URL | 우선순위 |
|-----------|------|----------|----------|
| `api_bp` | `routes_api.py` | `/api/*` 전부 | 1 (가장 먼저) |
| `pages_bp` | `routes_pages.py` | `/`, `/results`, `/betting-helper`, `/practice`, 스크립트, favicon | 2 |
| (선택) | | `/health` 는 app.py에 유지 또는 api_bp | - |

**주의**
- **순환 import**: `routes_api.py`에서 `from app import get_results` 후 app에서 `from routes_api import api_bp` 하면 순환 참조 발생.  
  → **핸들러 함수 자체를 routes_api.py에 두고**, 그 안에서만 `db`, `utils`, `prediction_logic` 등 필요한 모듈을 import. app 전역(캐시 등)이 필요하면 `flask.current_app` 또는 별도 서비스 모듈로 빼서 사용.
- 한 번에 전부 옮기지 말고, **한 Blueprint(한 파일)씩** 적용 후 서버/매크로로 검증.

---

## 4. 작업 순서 (꼬이지 않게)

### 4.1 준비
- [ ] 이 가이드와 현재 문서 동기화 여부 확인.
- [ ] `backup-modular-4step` 태그로 복원 가능한지 확인 (`git tag -l`).

### 4.2 1차: API 전용 Blueprint (`routes_api.py`)
1. **새 파일** `routes_api.py` 생성 (app.py는 아직 수정하지 않음).
2. `routes_api.py`에서:
   - `Blueprint('api', __name__, url_prefix='/api')` 생성.
   - 위 표의 **API 라우트만** (`/api/*`) 이 블루프린트에 등록.
   - 각 라우트 핸들러는 **app에서 import** (예: `from app import get_results, api_calc_state, ...`).
3. **app.py** 수정 (최소한만):
   - `from routes_api import api_bp` 후 `app.register_blueprint(api_bp)` 추가.
   - 기존 `@app.route('/api/...')` 와 해당 `def` **전부 삭제** (한 번에 하면 OOM 위험 → **스크립트/패치로 삭제** 고려).
4. 서버 실행 후 `/api/results`, `/api/current-pick` 등 호출해서 동작 확인.
5. 문제 없으면 커밋.

### 4.3 2차: 페이지 Blueprint (`routes_pages.py`)
1. **새 파일** `routes_pages.py` 생성.
2. `/`, `/results`, `/betting-helper`, `/practice`, 스크립트, favicon 라우트만 이 블루프린트로 이동 (url_prefix 없음 또는 `/`).
3. app.py에서 해당 `@app.route` 및 `def` 제거 (역시 스크립트/패치 권장).
4. 서버로 각 페이지 접속해서 확인.
5. 문제 없으면 커밋.

### 4.4 3차: health 등 나머지
- `/health` 는 app.py에 두거나, api_bp 또는 별도 블루프린트로 이동.
- 선택 사항이며, 1·2차 안정화 후 진행.

---

## 5. OOM 방지 원칙

- **app.py에서 대량 삭제/수정 금지** (에디터로 열지 않고 처리).
- 라우트 제거는 **라인 번호 기반 스크립트** 또는 **패치**로만 수행.
- **새 파일** (routes_api.py, routes_pages.py) 은 작게 유지 → Cursor에서 편집해도 부담 적음.
- 한 단계 완료할 때마다 **실행·호출 테스트** 후 커밋.

---

## 6. 복원

- 문제 생기면: `git checkout backup-modular-4step` 로 4단계 완료 시점으로 복원.
- 5단계 진행 중에는 단계별로 태그 추가 권장 (예: `step5-api-bp-done`).

---

## 7. 체크리스트 요약

| 단계 | 내용 | 완료 |
|------|------|------|
| 4.1 | 준비·백업 확인 | |
| 4.2 | routes_api.py 생성 + app에 등록, 기존 /api/* 제거 | |
| 4.3 | routes_pages.py 생성 + 등록, 기존 페이지 라우트 제거 | |
| 4.4 | (선택) health 등 나머지 정리 | |

---

이 가이드는 **할 일과 순서**만 정의합니다. 실제 코드 변경은 이 순서를 지키면서, OOM 방지 규칙에 따라 진행합니다.
