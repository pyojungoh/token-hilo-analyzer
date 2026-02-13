# 자동 배팅 매크로 (에뮬레이터)

Analyzer 서버의 예측 픽(`/api/current-pick`)을 폴링해서, RED/BLACK 픽이 바뀔 때 **LDPlayer 등 에뮬레이터**에서 해당 버튼을 ADB로 자동 클릭하는 데스크톱 프로그램입니다.

## 요구 사항

- Python 3.8+
- Analyzer 서버가 떠 있어야 하고, DB에 `current_pick`이 저장되는 환경이어야 합니다. (웹에서 결과 페이지를 열고 계산기/예측이 동작하면 픽이 저장됩니다.)
- LDPlayer(또는 ADB 지원 에뮬레이터) 실행 및 ADB 연결

## 설치

1. 터미널(또는 명령 프롬프트)을 열고, 프로젝트의 매크로 폴더로 이동합니다.
   ```bash
   cd token-hilo-analyzer/auto-betting-macro
   ```
2. Python 패키지를 설치합니다. (PyQt5, requests, pynput 등)
   ```bash
   pip install -r requirements.txt
   ```

## 실행

**실행**: `python emulator_macro.py` 또는 `run-emulator-macro.bat`

한 창에서 다음을 모두 처리합니다.

- **설정**: Analyzer URL, 계산기 1/2/3 선택, 배팅 금액, ADB 기기(예: 127.0.0.1:5555)
- **좌표 설정**: 배팅금액·정정·레드·블랙 좌표 찾기 (버튼 누르면 창 최소화 → LDPlayer 화면에서 해당 위치 클릭 → `emulator_coords.json` 저장). 기본 배팅금액 저장 포함.
- **표시**: 회차(★△○), 금액, 배팅픽, 정/꺽 카드, 경고·합선·승률
- **동작**: **시작**을 누르면 **다음 픽부터** 배팅 (금액 입력 → 정정 → RED/BLACK 탭, ADB)

의존성: `pynput` (좌표 찾기용), `PyQt5`, `requests` (pip install -r requirements.txt).

## 픽/금액 출처

- **계산기 1/2/3 선택 시**: 픽·회차·금액을 **`GET /api/current-pick?calculator=N`** 에서 가져옵니다. 분석기 웹의 해당 계산기(반픽/승률반픽/마틴/멈춤)와 동일한 픽·금액으로 배팅합니다.
- **current_pick 비어 있을 때**: 픽이 갱신될 때까지 대기합니다.

## EXE 빌드 (배포용 단일 실행 파일)

Python 없이 실행하려면 PyInstaller로 EXE를 만듭니다.

1. 의존성 설치: `pip install -r requirements.txt` 및 `pip install pyinstaller`
2. **PowerShell**에서 매크로 폴더로 이동 후:
   ```powershell
   cd auto-betting-macro
   .\build_exe.ps1
   ```
   (또는 `powershell -ExecutionPolicy Bypass -File build_exe.ps1`)
3. 빌드는 **3~8분** 걸릴 수 있습니다. 완료 후 `dist\TokenHiloEmulatorMacro.exe`가 생성됩니다.
4. EXE 실행 시 `emulator_coords.json`은 EXE와 **같은 폴더**에 두거나, 첫 실행 후 좌표 찾기로 생성하면 됩니다.

- `PermissionError`(파일 사용 중)가 나면: 다른 터미널/탐색기에서 `build` 폴더를 연 상태가 아닌지 확인하고, `build`·`dist` 폴더를 삭제한 뒤 `build_exe.ps1`을 다시 실행하세요.

## 사용 시 참고

- Analyzer 쪽에서 **결과 페이지를 열고 예측/계산기가 동작 중**이어야 `/api/current-pick`에 픽이 저장됩니다. 결과 페이지를 한 번이라도 열어 두고 매크로를 쓰는 것이 좋습니다.
- LDPlayer 사용법·ADB 연결은 `LDPLAYER_AUTO_BETTING_GUIDE.md` 참고.

## 연습 페이지 (마틴·좌표 테스트)

실제 배팅 없이 매크로와 마틴 금액을 테스트하려면 Analyzer의 **연습 페이지**를 사용할 수 있습니다.

1. **에뮬레이터 브라우저**에서 `https://(분석기주소)/practice` 를 엽니다. (예: `https://web-production-28c2.up.railway.app/practice`)
2. 매크로에서 **좌표 찾기**로 이 페이지의 배팅금액·RED·BLACK·정정 위치를 잡습니다.
3. 분석기 **결과 페이지**는 PC 브라우저에서 열어 두고, 계산기를 실행한 뒤 매크로 **시작**을 누릅니다.
4. 연습 페이지의 **액션 로그**에서 정정 시점의 금액이 마틴 단계(1배→2배→4배…)대로 들어가는지 확인합니다.

계산기 2/3으로 테스트할 때는 주소에 `?calculator=2` 또는 `?calculator=3`을 붙이면 됩니다.
