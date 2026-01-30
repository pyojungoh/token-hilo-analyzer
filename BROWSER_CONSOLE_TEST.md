# 브라우저 콘솔에서 데이터 확인 방법

## 1. tgame365.com 사이트 접속
1. 브라우저에서 `http://tgame365.com` 접속
2. F12를 눌러 개발자 도구 열기
3. Network 탭 선택
4. 페이지 새로고침 (F5)

## 2. Network 탭에서 확인
- 필터에 `json` 또는 `current_status` 입력
- 요청 목록에서 다음 파일들을 찾아보세요:
  - `current_status_frame.json`
  - `result.json`
  - `bet_result_log.csv`

## 3. 콘솔에서 직접 테스트
브라우저 콘솔(Console 탭)에서 다음 명령어를 실행:

```javascript
// 1. current_status_frame.json 테스트
fetch('http://tgame365.com/current_status_frame.json?t=' + Date.now())
  .then(r => r.json())
  .then(data => console.log('✅ 성공:', data))
  .catch(e => console.error('❌ 실패:', e));

// 2. frame/hilo 경로 테스트
fetch('http://tgame365.com/frame/hilo/current_status_frame.json?t=' + Date.now())
  .then(r => r.json())
  .then(data => console.log('✅ 성공 (frame/hilo):', data))
  .catch(e => console.error('❌ 실패 (frame/hilo):', e));

// 3. result.json 테스트
fetch('http://tgame365.com/result.json?t=' + Date.now())
  .then(r => r.json())
  .then(data => console.log('✅ 성공 (result):', data))
  .catch(e => console.error('❌ 실패 (result):', e));

// 4. frame/hilo/result.json 테스트
fetch('http://tgame365.com/frame/hilo/result.json?t=' + Date.now())
  .then(r => r.json())
  .then(data => console.log('✅ 성공 (frame/hilo/result):', data))
  .catch(e => console.error('❌ 실패 (frame/hilo/result):', e));
```

## 4. 성공한 URL 확인
- 콘솔에서 `✅ 성공`이 나온 URL을 확인
- 해당 URL을 Railway 서버 코드에 반영

## 5. Network 탭에서 실제 요청 확인
1. Network 탭에서 XHR 또는 Fetch 필터 선택
2. 페이지에서 게임 데이터를 로드할 때 발생하는 요청 확인
3. Request URL을 복사해서 사용
