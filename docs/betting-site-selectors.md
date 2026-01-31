# 배팅 사이트 DOM 셀렉터 참고 (nhs900 토큰하이로우)

외부 자동화(확장 프로그램 등)에서 사용할 수 있는 셀렉터 정리.  
**예측기 앱은 해당 사이트에 직접 접근하지 않으며, 아래 정보는 사용자가 수동/자동화 시 참고용입니다.**

## 입력·버튼 셀렉터

| 요소 | 셀렉터 | 비고 |
|------|--------|------|
| 배팅금 입력 | `#unit` | `<input type="text" id="unit">` |
| RED 배팅 버튼 | `button.btn_red` 또는 `.btn_red` | 클릭 시 RED 배팅 |
| BLACK 배팅 버튼 | `button.btn_black` 또는 `.btn_black` | 클릭 시 BLACK 배팅 |
| RED 배당률 hidden | `#rate_red` | value="1.97" |
| BLACK 배당률 hidden | `#rate_black` | value="1.97" |

## 배팅 연동 테스트 페이지

- **GET `/betting-helper`**  
  자동배팅 개발·테스트용 **별도 페이지**. 목표는 **실제 배팅 사이트에서 예측 픽에 따라 자동 배팅**할 수 있게 하는 것.  
  `/api/current-pick`을 3초마다 폴링하여 RED/BLACK/보류, 회차, 확률을 표시.  
  자동 배팅은 우리 앱과 배팅 사이트가 다른 도메인이므로, **배팅 사이트에서 동작하는 Tampermonkey 스크립트**로 구현 (아래 참고).

- **GET `/docs/tampermonkey-auto-bet.user.js`**  
  Tampermonkey용 자동배팅 스크립트. nhs900 페이지에서 우리 앱 `/api/current-pick`을 조회해 RED/BLACK에 따라 `#unit` 입력·`button.btn_red`/`button.btn_black` 클릭.  
  스크립트 내 `APP_BASE_URL`, `DEFAULT_AMOUNT`, `AUTO_CLICK_ENABLED` 수정 후 사용.

## 예측기 앱 연동 API

- **GET `/api/current-pick`**  
  현재 예측 픽 조회.  
  응답 예: `{ "pick_color": "RED" | "BLACK" | null, "round": 회차, "probability": 확률, "suggested_amount": null, "updated_at": "ISO 시간" }`  
  외부 도구가 폴링하여 `pick_color`에 따라 해당 사이트의 RED/BLACK 버튼에 매핑할 수 있음.

- **POST `/api/current-pick`**  
  예측기 화면에서 픽이 갱신될 때 앱이 자동으로 호출. (수동 호출 불필요)

## 주의

- 최소/최대 배팅 금액, 약관, 계정 정책은 해당 사이트 규정을 따릅니다.
- 자동 배팅 구현 시 유지보수·DOM 변경·약관 위반 리스크는 사용자 책임입니다.
