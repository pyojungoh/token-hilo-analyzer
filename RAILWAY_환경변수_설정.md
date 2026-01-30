# Railway 환경 변수 설정 가이드

## ⚠️ 중요: 설정 순서

**반드시 다음 순서대로 설정하세요:**

1. **먼저**: PostgreSQL 데이터베이스 서비스 추가 및 연결
2. **그 다음**: 환경 변수 설정

## 1단계: PostgreSQL 데이터베이스 추가 (가장 먼저!)

### Railway에서 PostgreSQL 추가
1. Railway 대시보드 → 프로젝트 선택
2. **"+ New"** 클릭 → **"Database"** → **"Add PostgreSQL"**
3. PostgreSQL 서비스가 생성됩니다

### DATABASE_URL 확인 및 설정
1. PostgreSQL 서비스 선택 → **"Variables"** 탭
2. `DATABASE_URL` 또는 `POSTGRES_URL` 값 복사 (전체 연결 URL)
3. 메인 서비스(웹 서비스) 선택 → **"Variables"** 탭
4. `DATABASE_URL` 변수가 없으면:
   - **"+ New Variable"** 클릭
   - 변수명: `DATABASE_URL`
   - 값: PostgreSQL에서 복사한 전체 URL 붙여넣기
   - **"Add"** 클릭

**⚠️ 이 단계를 먼저 하지 않으면 데이터베이스 테이블이 생성되지 않습니다!**

## 2단계: 필수 환경 변수 설정

### 1. DATABASE_URL (1단계에서 설정 완료)

### 2. SOCKETIO_URL (필수)

**변수명**: `SOCKETIO_URL`  
**값**: `https://game.cmx258.com:8080`

### 3. BASE_URL (선택)

**변수명**: `BASE_URL`  
**값**: `http://tgame365.com` (기본값)

## ⚠️ 주의사항

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

## 문제 해결

### 데이터베이스 테이블이 생성되지 않을 때
1. **1단계를 먼저 했는지 확인** (PostgreSQL 서비스 추가 및 DATABASE_URL 설정)
2. Railway 로그에서 `[❌ 경고] DATABASE_URL 환경 변수가 설정되지 않았습니다` 확인
3. 메인 서비스 Variables 탭에서 `DATABASE_URL` 확인
4. 없으면 PostgreSQL 서비스 Variables에서 복사하여 추가

### PORT 오류: "PORT variable must be integer between 0 and 65535"
- Railway 대시보드 → Variables 탭
- `PORT` 환경 변수가 있으면 **삭제** (Railway가 자동 설정)
