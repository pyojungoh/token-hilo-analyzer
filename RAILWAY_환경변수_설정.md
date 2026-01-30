# Railway 환경 변수 설정 가이드

## 필수 환경 변수

Railway에서 다음 환경 변수를 설정해야 합니다:

### 1. Socket.IO 서버 URL (필수)

**변수명**: `SOCKETIO_URL`  
**값**: `https://game.cmx258.com:8080`

이 환경 변수가 없으면 Socket.IO 연결이 시작되지 않아 베팅 데이터를 받을 수 없습니다.

### 2. 기본 URL (선택)

**변수명**: `BASE_URL`  
**값**: `http://tgame365.com` (기본값)

결과 JSON 파일을 가져올 때 사용합니다.

### 3. 기타 설정 (선택)

**변수명**: `TIMEOUT`  
**값**: `10` (기본값, 초)

**변수명**: `MAX_RETRIES`  
**값**: `2` (기본값)

**변수명**: `PORT`  
**값**: Railway가 자동으로 설정 (절대 수동으로 설정하지 마세요!)

## Railway에서 환경 변수 설정 방법

1. Railway 대시보드 접속
2. 프로젝트 선택
3. **Variables** 탭 클릭
4. **+ New Variable** 클릭
5. 다음 변수 추가:

```
변수명: SOCKETIO_URL
값: https://game.cmx258.com:8080
```

6. **Add** 클릭
7. 서버가 자동으로 재배포됩니다

## 확인 방법

Railway 로그에서 다음 메시지를 확인하세요:

- ✅ 성공: `[정보] Socket.IO 연결 시작: https://game.cmx258.com:8080`
- ✅ 성공: `[Socket.IO] 연결됨`
- ✅ 성공: `[Socket.IO] total 이벤트: RED X명, BLACK Y명`
- ❌ 실패: `[경고] SOCKETIO_URL 환경 변수가 설정되지 않았습니다`

## 문제 해결

### 빌드 실패: "PORT variable must be integer between 0 and 65535"

**원인**: Railway가 자동으로 PORT를 설정하는데, 수동으로 PORT 환경 변수를 설정했을 때 발생합니다.

**해결 방법**:
1. Railway 대시보드 → Variables 탭
2. `PORT` 환경 변수가 있는지 확인
3. **있다면 삭제** (Railway가 자동으로 설정합니다)
4. 서버가 자동으로 재배포됩니다

### Socket.IO 연결이 안 될 때

1. 환경 변수 `SOCKETIO_URL`이 올바르게 설정되었는지 확인
2. Railway 로그에서 연결 오류 메시지 확인
3. `https://game.cmx258.com:8080`이 접근 가능한지 확인
4. 빌드가 성공했는지 확인 (빌드 실패 시 서버가 실행되지 않음)

### 베팅 데이터가 0명으로 표시될 때

1. Socket.IO 연결 상태 확인 (로그에서 `[Socket.IO] 연결됨` 메시지)
2. `total` 이벤트가 수신되는지 확인 (로그에서 `[Socket.IO] total 이벤트` 메시지)
3. 게임이 진행 중인지 확인 (베팅이 없으면 0명이 정상)
