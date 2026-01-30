# F12 개발자 도구로 데이터 확인하기

## 1단계: 사이트 접속 및 개발자 도구 열기

1. 브라우저에서 `http://tgame365.com` 접속
2. **F12** 키를 누르거나 **우클릭 → 검사** 클릭
3. 개발자 도구가 열리면 **Network (네트워크)** 탭 클릭

---

## 2단계: Network 탭에서 실제 요청 확인

### 필터 설정
1. Network 탭 상단의 **필터 입력란**에 다음 중 하나 입력:
   - `json` (JSON 파일만 보기)
   - `current_status` (현재 상태 관련)
   - `result` (결과 관련)
   - `frame` (프레임 관련)

2. 또는 **XHR** 또는 **Fetch/XHR** 필터 버튼 클릭

### 페이지 새로고침
- **F5** 또는 **Ctrl+R**로 페이지 새로고침
- Network 탭에 요청 목록이 나타남

### 찾아야 할 파일들
다음 파일명을 찾아보세요:
- `current_status_frame.json`
- `result.json`
- `bet_result_log.csv`
- 또는 비슷한 이름의 파일들

---

## 3단계: 요청 상세 정보 확인

### 파일 클릭하면 보이는 정보:
1. **Headers (헤더)** 탭:
   - **Request URL**: 실제 요청 주소 (이게 중요!)
   - **Request Method**: GET 또는 POST
   - **Request Headers**: 요청 헤더들
   - **Response Headers**: 응답 헤더들

2. **Preview (미리보기)** 탭:
   - JSON 데이터 구조 확인
   - 실제 데이터 내용 확인

3. **Response (응답)** 탭:
   - 원본 응답 데이터 전체 확인

---

## 4단계: Console 탭에서 직접 테스트

1. **Console (콘솔)** 탭 클릭
2. 아래 코드를 복사해서 붙여넣고 **Enter**:

```javascript
// 1. 현재 페이지의 모든 네트워크 요청 확인
console.log('=== 네트워크 요청 확인 ===');
performance.getEntriesByType('resource').forEach(resource => {
    if (resource.name.includes('json') || resource.name.includes('status') || resource.name.includes('result')) {
        console.log('발견:', resource.name);
    }
});

// 2. 가능한 URL들 직접 테스트
const testUrls = [
    'http://tgame365.com/current_status_frame.json',
    'http://tgame365.com/frame/hilo/current_status_frame.json',
    'http://tgame365.com/hilo/current_status_frame.json',
    'http://tgame365.com/result.json',
    'http://tgame365.com/frame/hilo/result.json'
];

console.log('=== URL 테스트 시작 ===');
testUrls.forEach((url, index) => {
    setTimeout(() => {
        fetch(url + '?t=' + Date.now())
            .then(response => {
                if (response.ok) {
                    console.log(`✅ [${index + 1}] 성공: ${url}`);
                    return response.json();
                } else {
                    console.log(`❌ [${index + 1}] 실패 (${response.status}): ${url}`);
                    throw new Error('HTTP ' + response.status);
                }
            })
            .then(data => {
                console.log(`   데이터 샘플:`, data);
                console.log(`   데이터 키:`, Object.keys(data));
                if (Array.isArray(data)) {
                    console.log(`   배열 길이:`, data.length);
                }
            })
            .catch(error => {
                console.log(`   오류:`, error.message);
            });
    }, index * 500); // 0.5초 간격으로 테스트
});
```

---

## 5단계: 실제 게임 페이지에서 확인

1. `http://tgame365.com`에서 **실제 게임 페이지**로 이동
2. 게임이 로드되면 Network 탭 확인
3. 게임 데이터를 가져오는 요청 찾기

---

## 6단계: 찾은 정보 공유하기

다음 정보를 복사해서 알려주세요:

1. **성공한 URL**:
   - Network 탭에서 찾은 실제 요청 URL
   - 또는 Console에서 `✅ 성공`으로 표시된 URL

2. **데이터 구조**:
   - Preview 탭에서 본 데이터 구조
   - 또는 Console에서 출력된 `데이터 키` 목록

3. **요청 헤더**:
   - Headers 탭의 Request Headers
   - 특히 `Referer`, `Origin` 등

---

## 빠른 확인 방법

Console에 이 코드를 붙여넣으세요:

```javascript
// 빠른 확인
fetch('http://tgame365.com/current_status_frame.json?t=' + Date.now())
  .then(r => r.json())
  .then(d => {
    console.log('✅ 성공!');
    console.log('데이터:', d);
    console.log('키:', Object.keys(d));
  })
  .catch(e => {
    console.log('❌ 실패:', e.message);
    console.log('다른 경로 시도 중...');
    return fetch('http://tgame365.com/frame/hilo/current_status_frame.json?t=' + Date.now());
  })
  .then(r => r && r.ok ? r.json() : null)
  .then(d => {
    if (d) {
      console.log('✅ frame/hilo 경로 성공!');
      console.log('데이터:', d);
    }
  })
  .catch(e => console.log('모든 경로 실패'));
```

---

## 중요 체크리스트

- [ ] Network 탭에서 `current_status_frame.json` 요청 찾았는가?
- [ ] Request URL이 무엇인가?
- [ ] 응답이 200 OK인가? (404면 실패)
- [ ] Preview에서 데이터가 보이는가?
- [ ] Console 테스트에서 성공한 URL이 있는가?

이 정보를 알려주시면 서버 코드에 정확히 반영하겠습니다!
