# Railway 배포 설정 가이드

## 1. Railway 계정 및 프로젝트 생성

### 1.1 Railway 가입/로그인
1. https://railway.app 접속
2. GitHub 계정으로 로그인 (또는 이메일 가입)

### 1.2 새 프로젝트 생성
1. Railway 대시보드에서 **"New Project"** 클릭
2. **"Deploy from GitHub repo"** 선택 (GitHub에 코드가 있는 경우)
   - 또는 **"Empty Project"** 선택 후 수동 배포

## 2. 코드 배포

### 2.1 GitHub 저장소 연결 (권장)
1. GitHub에 `token-hilo-analyzer` 저장소 생성
2. 다음 파일들을 커밋:
   - `app.py`
   - `requirements.txt`
   - `Procfile`
   - `.gitignore`
3. Railway에서 해당 저장소 선택

### 2.2 직접 배포 (GitHub 없이)
1. Railway 프로젝트에서 **"Settings"** → **"Source"**
2. **"Connect GitHub Repo"** 또는 **"Upload Files"** 선택
3. 프로젝트 파일 업로드

## 3. 환경 변수 설정

### 3.1 Railway 대시보드에서 설정
1. 프로젝트 → **"Variables"** 탭 클릭
2. 다음 환경 변수 추가:

```
BASE_URL=http://tgame365.com
UPDATE_INTERVAL=5000
TIMEOUT=30
MAX_RETRIES=3
```

**주의**: `BASE_URL`은 실제 토큰하이로우 사이트 URL로 변경 필요

### 3.2 PORT 변수
- Railway가 자동으로 `PORT` 환경 변수 설정
- 코드에서 `os.getenv('PORT', 5000)` 사용 중이므로 자동 처리됨

## 4. 배포 확인

### 4.1 배포 상태 확인
1. Railway 대시보드 → **"Deployments"** 탭
2. 배포 로그 확인
3. 성공 시 초록색 체크 표시

### 4.2 서비스 URL 확인
1. **"Settings"** → **"Domains"** 탭
2. Railway가 자동 생성한 도메인 확인
   - 예: `token-hilo-analyzer-production.up.railway.app`

## 5. API 테스트

### 5.1 헬스 체크
```bash
curl https://your-app.railway.app/health
```

### 5.2 현재 게임 상태 확인
```bash
curl https://your-app.railway.app/api/current-status
```

### 5.3 연승 데이터 확인
```bash
curl https://your-app.railway.app/api/streaks
```

## 6. 문제 해결

### 6.1 배포 실패 시
- **"Deployments"** → 로그 확인
- `requirements.txt` 의존성 확인
- 환경 변수 설정 확인

### 6.2 API 오류 시
- Railway 로그 확인 (대시보드 → **"Deployments"** → 로그)
- `BASE_URL` 환경 변수 확인
- 네트워크 연결 확인

### 6.3 로그 확인 방법
1. Railway 대시보드 → 프로젝트 선택
2. **"Deployments"** 탭 → 최신 배포 클릭
3. 로그 스트림 확인

## 7. 자동 배포 설정

### 7.1 GitHub 연동 시
- `main` 브랜치에 푸시하면 자동 배포
- 설정 변경: **"Settings"** → **"Deploy"**

### 7.2 수동 배포
- **"Deployments"** → **"Redeploy"** 클릭

## 8. 모니터링

### 8.1 사용량 확인
- Railway 대시보드 → **"Metrics"** 탭
- CPU, 메모리, 네트워크 사용량 확인

### 8.2 알림 설정
- **"Settings"** → **"Notifications"**
- 배포 실패, 오류 등 알림 설정 가능
