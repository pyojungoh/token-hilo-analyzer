// ==UserScript==
// @name         토큰하이로우 자동배팅 (nhs900)
// @namespace    https://github.com/
// @version      0.3
// @description  설정값을 사이트에 입력·클릭 테스트 → (선택) 예측기 API 연동 자동배팅
// @match        https://nhs900.com/*
// @match        http://nhs900.com/*
// @match        https://www.nhs900.com/*
// @match        http://www.nhs900.com/*
// @run-at       document-end
// @grant        GM_xmlhttpRequest
// @connect      *
// ==/UserScript==

(function() {
    'use strict';

    // ===== API 연동 설정 (나중에 사용) =====
    var APP_BASE_URL = 'https://your-app.railway.app';
    var DEFAULT_AMOUNT = '1000';
    var POLL_INTERVAL_MS = 3000;
    var AUTO_CLICK_ENABLED = false;
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

    function setAmountOnly(amountStr) {
        var unit = getUnitInput();
        if (!unit) return { ok: false, msg: '#unit 입력란을 찾을 수 없음' };
        var amt = (amountStr || '').trim() || DEFAULT_AMOUNT;
        unit.value = amt;
        unit.dispatchEvent(new Event('input', { bubbles: true }));
        unit.dispatchEvent(new Event('change', { bubbles: true }));
        return { ok: true, msg: '금액 ' + amt + ' 적용됨' };
    }

    function applyBet(pickColor, amountStr) {
        var unit = getUnitInput();
        var btn = pickColor === 'RED' ? getRedBtn() : getBlackBtn();
        if (!unit) return { ok: false, msg: '#unit 없음' };
        if (!btn) return { ok: false, msg: (pickColor === 'RED' ? '.btn_red' : '.btn_black') + ' 버튼 없음' };
        var amt = (amountStr || '').trim() || DEFAULT_AMOUNT;
        unit.value = amt;
        unit.dispatchEvent(new Event('input', { bubbles: true }));
        unit.dispatchEvent(new Event('change', { bubbles: true }));
        btn.click();
        return { ok: true, msg: pickColor + ' 배팅 적용 (금액 ' + amt + ')' };
    }

    // ----- 1) 설정값 → 사이트 입력 테스트 패널 (API 없음) -----
    function showStatus(el, text, isError) {
        if (!el) return;
        el.textContent = text;
        el.style.color = isError ? '#f44336' : '#81c784';
    }

    function injectTestPanel() {
        if (document.getElementById('token-hilo-bet-panel')) return;

        var panel = document.createElement('div');
        panel.id = 'token-hilo-bet-panel';
        panel.style.cssText = 'position:fixed !important;top:12px !important;right:12px !important;z-index:2147483647 !important;width:220px;padding:10px;' +
            'background:#1a1a2e !important;border:2px solid #64b5f6 !important;border-radius:8px;font-family:sans-serif;font-size:12px;color:#eee !important;box-shadow:0 4px 20px rgba(0,0,0,0.5) !important;';
        panel.innerHTML =
            '<div style="margin-bottom:6px;color:#64b5f6;font-weight:bold;">배팅 테스트</div>' +
            '<label style="display:block;margin-bottom:4px;">배팅금 <input type="text" id="th-bet-amount" value="1000" style="width:80px;margin-left:4px;padding:4px;background:#333;color:#fff;border:1px solid #555;" /></label>' +
            '<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px;">' +
            '<button type="button" id="th-btn-amount-only" style="padding:6px 10px;background:#37474f;color:#90a4ae;border:none;border-radius:4px;cursor:pointer;">금액만 입력</button>' +
            '<button type="button" id="th-btn-red" style="padding:6px 10px;background:#b71c1c;color:#fff;border:none;border-radius:4px;cursor:pointer;">RED</button>' +
            '<button type="button" id="th-btn-black" style="padding:6px 10px;background:#212121;color:#fff;border:none;border-radius:4px;cursor:pointer;">BLACK</button>' +
            '</div>' +
            '<div id="th-status" style="font-size:11px;color:#81c784;min-height:14px;"></div>';

        var target = document.body || document.documentElement;
        if (target) target.appendChild(panel);

        var amountInput = document.getElementById('th-bet-amount');
        var statusEl = document.getElementById('th-status');

        document.getElementById('th-btn-amount-only').addEventListener('click', function() {
            var res = setAmountOnly(amountInput ? amountInput.value : '');
            showStatus(statusEl, res.msg, !res.ok);
        });
        document.getElementById('th-btn-red').addEventListener('click', function() {
            var res = applyBet('RED', amountInput ? amountInput.value : '');
            showStatus(statusEl, res.msg, !res.ok);
        });
        document.getElementById('th-btn-black').addEventListener('click', function() {
            var res = applyBet('BLACK', amountInput ? amountInput.value : '');
            showStatus(statusEl, res.msg, !res.ok);
        });
    }

    function tryInject() {
        if (document.body || document.documentElement) injectTestPanel();
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', tryInject);
    } else {
        tryInject();
    }
    [500, 1000, 2000, 4000, 8000, 12000].forEach(function(ms) { setTimeout(tryInject, ms); });

    // ----- 1-2) 왼쪽 설정 → 오른쪽 사이트: 부모/iframe에서 오는 postMessage 수신 -----
    window.addEventListener('message', function(e) {
        if (!e.data || e.data.type !== 'TOKEN_HILO_APPLY') return;
        var pick = e.data.pick, amount = e.data.amount;
        var r;
        if (pick === 'AMOUNT_ONLY') {
            r = setAmountOnly(amount);
        } else if (pick === 'RED' || pick === 'BLACK') {
            r = applyBet(pick, amount);
        } else return;
        try {
            if (e.source && e.source.postMessage) {
                e.source.postMessage({ type: 'TOKEN_HILO_RESULT', ok: r.ok, msg: r.msg }, '*');
            }
        } catch (err) {}
    });

    // ----- 2) API 연동 자동배팅 (AUTO_CLICK_ENABLED 시) -----
    function applyPickFromApi(pickColor, round, amount) {
        if (!pickColor || (pickColor !== 'RED' && pickColor !== 'BLACK')) return;
        if (round != null && round === lastAppliedRound) return;
        var r = applyBet(pickColor, String(amount || DEFAULT_AMOUNT));
        if (r.ok) lastAppliedRound = round;
    }

    function poll() {
        if (APP_BASE_URL.indexOf('your-app') >= 0 || !AUTO_CLICK_ENABLED) return;
        GM_xmlhttpRequest({
            method: 'GET',
            url: APP_BASE_URL.replace(/\/$/, '') + '/api/current-pick',
            onload: function(res) {
                try {
                    var data = JSON.parse(res.responseText);
                    if (data.pick_color) applyPickFromApi(data.pick_color, data.round, data.suggested_amount || DEFAULT_AMOUNT);
                } catch (e) {}
            }
        });
    }
    setInterval(poll, POLL_INTERVAL_MS);
    poll();
})();
