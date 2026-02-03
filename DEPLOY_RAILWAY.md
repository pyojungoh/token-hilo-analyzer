# Railway 배포 체크리스트

**이 저장소에는 `Dockerfile`이 있습니다.** Railway는 루트에 Dockerfile이 있으면 자동으로 그걸로 빌드/실행합니다.

배포가 안 될 때 아래를 **순서대로** 확인하세요.

---

## 🔴 배포가 안 될 때 바로 할 일

1. **Railway 대시보드** → 해당 프로젝트 → **서비스** 클릭
2. **Settings** 탭에서:
   - **Root Directory**: **비움** (또는 `.`). `token-hilo-analyzer` 등 서브폴더 이름이 있으면 **지우기**.
   - **Branch**: **`main`** 인지 확인.
3. **Deployments** 탭 → **Redeploy** (또는 **Deploy**) 한 번 클릭
4. 같은 페이지에서 **최신 배포** 클릭 → **View Logs**:
   - **Build** 로그: `pip install` 실패, Python 버전 에러가 있는지 확인
   - **Deploy** 로그: 앱이 시작됐는지, 크래시 메시지가 있는지 확인
5. **Variables** 탭: `DATABASE_URL` 등 필수 환경 변수 있는지 확인 (없으면 앱이 시작 시 크래시할 수 있음)

로그에 나온 **에러 메시지**를 보고 아래 "자주 나오는 에러"에서 맞는 항목을 찾아 수정하세요.

---

## ⚠️ 먼저 확인 (이걸 잘못 쓰면 푸시해도 배포 안 됨)

### Root Directory (루트 디렉터리)
- 이 GitHub 저장소는 **앱이 저장소 루트에 있습니다** (app.py, Procfile, requirements.txt가 repo 최상단).
- Railway **Settings** → **Root Directory** 를 **비워 두세요** (또는 `.`). 서브폴더 이름을 넣으면 해당 폴더가 repo에 없어 빌드 실패합니다.

### 브랜치
- **Settings** → **Branch**: **`main`** 으로 설정 (`master`면 푸시해도 배포 트리거 안 됨)

---

## 1. GitHub 연결
- [Railway 대시보드](https://railway.app/dashboard) → 프로젝트 선택 → **서비스** 클릭
- **Settings** → **Source**: 이 저장소(`pyojungoh/token-hilo-analyzer`)가 연결되어 있는지 확인
- 연결 안 되어 있으면 **Connect Repo** → GitHub에서 `token-hilo-analyzer` 선택

## 2. 수동 배포 트리거
- **Deployments** 탭 → **Deploy** 또는 **Redeploy** 버튼으로 수동 배포 실행
- 푸시만으로 자동 배포가 안 되면, 여기서 한 번 수동 배포 후 **Settings** → **Deploy**에서 "Auto Deploy"가 켜져 있는지 확인

## 3. 빌드/배포 로그 확인
- **Deployments** → 최신 배포 클릭 → **View Logs**
- **Build** 단계에서 에러가 나면 (Python 버전, `pip install` 실패 등) 로그 메시지로 원인 확인
- **Deploy** 단계에서 헬스체크 실패 시: 앱이 `/health` 를 100초 안에 응답하는지 확인 (현재 `railway.json`에 `healthcheckPath: "/health"`, `healthcheckTimeout: 100` 설정됨)

## 4. 환경 변수
- **Variables** 탭에서 `DATABASE_URL` 등 필요한 환경 변수가 설정되어 있는지 확인 (`.env.example` 참고)

---

## 자주 나오는 에러와 대처

| 로그/증상 | 원인 | 대처 |
|-----------|------|------|
| `No such file or directory`, `app.py` 못 찾음 | Root Directory가 잘못됨 | Settings → Root Directory **비우기** |
| Build 실패, `requirements.txt` 없음 | 루트가 아님 | 위와 동일, Root Directory 비우기 |
| `pip install` 실패 (psycopg2 등) | 빌드 환경 문제 | Railway가 제공하는 Python 런타임 사용 중이면 대부분 해결됨. `runtime.txt`에 `python-3.11.9` 있음 확인 |
| Deploy 후 바로 크래시 | 앱 시작 시 예외 (DB 연결 등) | Variables에 `DATABASE_URL` 설정. View Logs에서 Python traceback 확인 |
| 헬스체크 실패 / 배포 실패 | 앱이 100초 안에 `/health` 200을 안 줌 | 앱이 정상 기동하는지 로그로 확인. DB 등 의존성 때문에 늦게 뜨면 Variables·네트워크 확인 |

---

수정 후 다시 배포하려면:
```bash
git add .
git commit -m "배포 설정 수정"
git push origin main
```
그 다음 Railway에서 **Redeploy** 한 번 실행해 보세요.
