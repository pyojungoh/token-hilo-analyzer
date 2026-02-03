# Railway 배포 체크리스트

배포가 안 될 때 아래를 순서대로 확인하세요.

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

수정 후 다시 배포하려면:
```bash
git add .
git commit -m "배포 설정 수정"
git push origin main
```
그 다음 Railway에서 **Redeploy** 한 번 실행해 보세요.
