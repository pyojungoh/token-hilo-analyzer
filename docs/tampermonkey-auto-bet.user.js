// ==UserScript==
// @name         토큰하이로우 예측 연동 자동배팅 (nhs900)
// @namespace    https://github.com/
// @version      0.1
// @description  예측기 앱 /api/current-pick 픽에 따라 배팅 사이트에서 RED/BLACK 자동 입력·클릭
// @match        https://nhs900.com/*
// @grant        GM_xmlhttpRequest
// @connect      *
// ==/UserScript==

(function() {
    'use strict';

    // ===== 설정 (반드시 수정) =====
    var APP_BASE_URL = 'https://your-app.railway.app';  // 예측기 앱 주소 (끝에 / 없이)
    var DEFAULT_AMOUNT = '1000';                        // 기본 배팅금
    var POLL_INTERVAL_MS = 3000;                        // 픽 조회 간격 (ms)
    var AUTO_CLICK_ENABLED = false;                     // true로 바꾸면 자동 클릭 활성화 (주의)

    var lastAppliedRound = null;

    function getUnitInput() {
        return document.querySelector('#unit');
    }
    function getRedBtn() {
        return document.querySelector('button.btn_red') || document.querySelector('.btn_red');
    }
    function getBlackBtn() {
        return document.querySelector('button.btn_black') || document.querySelector('.btn_black');
    }

    function applyPick(pickColor, round, amount) {
        if (!pickColor || pickColor !== 'RED' && pickColor !== 'BLACK') return;
        if (round != null && round === lastAppliedRound) return;

        var unit = getUnitInput();
        var btn = pickColor === 'RED' ? getRedBtn() : getBlackBtn();
        if (!unit || !btn) return;

        unit.value = amount || DEFAULT_AMOUNT;
        unit.dispatchEvent(new Event('input', { bubbles: true }));
        btn.click();
        lastAppliedRound = round;
        console.log('[자동배팅] ' + pickColor + ' 회차 ' + round + ' 적용');
    }

    function poll() {
        if (APP_BASE_URL.indexOf('your-app') >= 0) return;

        GM_xmlhttpRequest({
            method: 'GET',
            url: APP_BASE_URL.replace(/\/$/, '') + '/api/current-pick',
            onload: function(res) {
                try {
                    var data = JSON.parse(res.responseText);
                    if (data.pick_color && AUTO_CLICK_ENABLED) {
                        applyPick(data.pick_color, data.round, DEFAULT_AMOUNT);
                    }
                } catch (e) {}
            }
        });
    }

    setInterval(poll, POLL_INTERVAL_MS);
    poll();
})();
