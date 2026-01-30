# Socket.IO 연결 확인 가이드

## 1. Network 탭에서 Socket 확인

1. F12 → Network 탭
2. 필터를 **Socket**으로 변경
3. 페이지 새로고침 (F5)
4. Socket.IO 연결이 나타나는지 확인

## 2. Console에서 Socket.IO 확인

F12 → Console 탭에서 아래 코드 실행:

```javascript
// Socket.IO 연결 확인
console.log('=== Socket.IO 연결 확인 ===');

// 1. Socket.IO 라이브러리 확인
if (typeof io !== 'undefined') {
    console.log('✅ Socket.IO 라이브러리 발견');
    
    // 2. 활성 연결 확인
    const sockets = [];
    for (let key in window) {
        if (window[key] && typeof window[key] === 'object') {
            if (window[key].io && window[key].connected) {
                sockets.push({
                    key: key,
                    socket: window[key],
                    url: window[key].io.uri,
                    connected: window[key].connected
                });
            }
        }
    }
    
    if (sockets.length > 0) {
        console.log('✅ Socket.IO 연결 발견:', sockets);
        sockets.forEach(s => {
            console.log(`  - ${s.key}: ${s.url}, 연결됨: ${s.connected}`);
        });
    } else {
        console.log('❌ 활성 Socket.IO 연결 없음');
    }
} else {
    console.log('❌ Socket.IO 라이브러리 없음');
}

// 3. Network 리소스에서 Socket.IO URL 찾기
console.log('\n=== Network 리소스 확인 ===');
performance.getEntriesByType('resource').forEach(r => {
    if (r.name.includes('socket.io') || r.name.includes('socket')) {
        console.log('발견:', r.name);
    }
});

// 4. WebSocket 연결 확인
console.log('\n=== WebSocket 연결 확인 ===');
const wsConnections = [];
for (let key in window) {
    if (window[key] && window[key].constructor && window[key].constructor.name === 'WebSocket') {
        wsConnections.push({
            key: key,
            url: window[key].url,
            readyState: window[key].readyState
        });
    }
}
if (wsConnections.length > 0) {
    console.log('✅ WebSocket 연결 발견:', wsConnections);
} else {
    console.log('❌ WebSocket 연결 없음');
}
```

## 3. Socket.IO 이벤트 확인

Console에서 아래 코드 실행하여 이벤트 리스너 확인:

```javascript
// Socket.IO 이벤트 리스너 확인
console.log('=== Socket.IO 이벤트 확인 ===');

// 전역 변수에서 Socket.IO 인스턴스 찾기
let socketInstance = null;
for (let key in window) {
    if (window[key] && typeof window[key] === 'object') {
        if (window[key].on && window[key].emit && window[key].connected !== undefined) {
            socketInstance = window[key];
            console.log(`✅ Socket.IO 인스턴스 발견: ${key}`);
            console.log(`   URL: ${socketInstance.io ? socketInstance.io.uri : 'unknown'}`);
            console.log(`   연결됨: ${socketInstance.connected}`);
            break;
        }
    }
}

if (socketInstance) {
    // 이벤트 리스너 확인
    if (socketInstance._callbacks) {
        console.log('이벤트 리스너:', Object.keys(socketInstance._callbacks));
    }
    
    // 수동으로 이벤트 리스너 추가하여 확인
    socketInstance.on('total', (data) => {
        console.log('📊 [total 이벤트 수신]', data);
    });
    
    socketInstance.on('status', (data) => {
        console.log('📈 [status 이벤트 수신]', data);
    });
    
    socketInstance.onAny((eventName, ...args) => {
        console.log(`🔔 [Socket.IO 이벤트] ${eventName}:`, args);
    });
    
    console.log('✅ 이벤트 리스너 추가 완료. 게임을 진행하면 이벤트가 표시됩니다.');
} else {
    console.log('❌ Socket.IO 인스턴스를 찾을 수 없습니다.');
}
```

## 4. 확인할 정보

위 코드 실행 후 다음 정보를 알려주세요:

1. **Socket 필터에서 연결이 보이나요?**
   - 보이면: 연결 URL 전체 주소
   - 안 보이면: "Socket 연결 없음"

2. **Console에서 Socket.IO 인스턴스를 찾았나요?**
   - 찾았으면: URL과 연결 상태
   - 못 찾았으면: "인스턴스 없음"

3. **이벤트 이름이 무엇인가요?**
   - `total`, `status` 외에 다른 이벤트가 있나요?
   - 실제로 수신되는 이벤트 이름

4. **베팅 데이터가 어떤 이벤트로 오나요?**
   - 게임을 진행하면서 Console에 표시되는 이벤트 확인
