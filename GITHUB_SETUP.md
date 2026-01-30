# GitHub 저장소 생성 및 연결

## 1. GitHub에서 저장소 생성

### 방법 1: 웹 브라우저에서
1. https://github.com 접속 → 로그인
2. 우측 상단 **"+"** 버튼 → **"New repository"** 클릭
3. 저장소 정보 입력:
   - **Repository name**: `token-hilo-analyzer`
   - **Description**: `토큰하이로우 분석기 Railway 서버`
   - **Visibility**: Public 또는 Private 선택
   - **Initialize this repository with**: 체크하지 않음 (이미 로컬에 코드 있음)
4. **"Create repository"** 클릭

### 방법 2: GitHub CLI 사용 (설치된 경우)
```bash
gh repo create token-hilo-analyzer --public --source=. --remote=origin --push
```

## 2. 원격 저장소 연결

GitHub에서 저장소를 만든 후, 아래 명령어 실행:

```bash
cd c:\hi\token-hilo-analyzer
git remote add origin https://github.com/YOUR_USERNAME/token-hilo-analyzer.git
git branch -M main
git push -u origin main
```

**주의**: `YOUR_USERNAME`을 본인의 GitHub 사용자명으로 변경

## 3. 인증 방법

### Personal Access Token 사용 (권장)
1. GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
2. **"Generate new token"** 클릭
3. 권한 선택: `repo` 체크
4. 토큰 생성 후 복사
5. 푸시 시 비밀번호 대신 토큰 사용

### 또는 GitHub Desktop 사용
- GitHub Desktop 앱에서 저장소 열기
- 자동으로 인증 처리됨
