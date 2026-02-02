# Railway 배포 체크리스트

배포가 안 될 때 아래를 순서대로 확인하세요.

## 1. GitHub 연결
- [Railway 대시보드](https://railway.app/dashboard) → 프로젝트 선택 → **서비스** 클릭
- **Settings** → **Source**: 이 저장소(`pyojungoh/token-hilo-analyzer`)가 연결되어 있는지 확인
- 연결 안 되어 있으면 **Connect Repo** → GitHub에서 `token-hilo-analyzer` 선택

## 2. 브랜치
- **Settings** → **Branch**: `master` 로 설정되어 있는지 확인 (푸시하는 브랜치와 동일해야 함)

## 3. 수동 배포 트리거
- **Deployments** 탭 → **Deploy** 또는 **Redeploy** 버튼으로 수동 배포 실행
- 푸시만으로 자동 배포가 안 되면, 여기서 한 번 수동 배포 후 **Settings** → **Deploy**에서 "Auto Deploy"가 켜져 있는지 확인

## 4. 빌드/배포 로그 확인
- **Deployments** → 최신 배포 클릭 → **View Logs**
- **Build** 단계에서 에러가 나면 (Python 버전, `pip install` 실패 등) 로그 메시지로 원인 확인
- **Deploy** 단계에서 헬스체크 실패 시: 앱이 `/health` 를 100초 안에 응답하는지 확인 (현재 `railway.json`에 `healthcheckPath: "/health"`, `healthcheckTimeout: 100` 설정됨)

## 5. 환경 변수
- **Variables** 탭에서 `DATABASE_URL` 등 필요한 환경 변수가 설정되어 있는지 확인 (`.env.example` 참고)

## 6. 루트 디렉터리
- 이 저장소는 **루트에** `app.py`, `Procfile`, `requirements.txt` 가 있음. **Root Directory**는 비워 두거나 `/` 로 두면 됨 (서브폴더가 아님).

---

수정 후 다시 배포하려면:
```bash
git add .
git commit -m "배포 설정 수정"
git push origin master
```
그 다음 Railway에서 **Redeploy** 한 번 실행해 보세요.
