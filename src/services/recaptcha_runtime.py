"""
核心职责：本地浏览器 Provider 共享的 reCAPTCHA 注入与执行运行时。
主要功能：
1. 注入 trusted reCAPTCHA Hook 与 ActionGuard，捕获 Flow 原生绑定的 execute 并防止 action 劫持；
2. 构建安全的 reCAPTCHA 执行脚本，优先以 siteKey 直接执行，仅在显式模式回退时以 size='invisible' 挂载独立空容器；
3. 彻底消除 placeholder element 非空报错与 invisible reCAPTCHA 模式不匹配报错。
"""

import json


TRUSTED_RECAPTCHA_HOOK_SOURCE = r"""
(() => {
    if (window.__flow2apiTrustedRecaptchaHookInstalled) return;
    Object.defineProperty(
        window,
        '__flow2apiTrustedRecaptchaHookInstalled',
        { value: true }
    );

    const storageKey = '__flow2apiTrustedRecaptchaExecute';
    const originalBind = Function.prototype.bind;
    Function.prototype.bind = function(thisArg, ...args) {
        const bound = originalBind.call(this, thisArg, ...args);
        try {
            const enterprise = window.grecaptcha && window.grecaptcha.enterprise;
            if (
                !window[storageKey] &&
                enterprise &&
                this === enterprise.execute &&
                typeof bound === 'function'
            ) {
                window[storageKey] = bound;
            }
        } catch (error) {}
        return bound;
    };

    // New Flow replaces the public execute wrapper with an assignment that
    // overwrites the caller action by extension_hijack_detected. Preserve the
    // requested action when that exact assignment shape is used.
    Object.defineProperty(
        window,
        '__flow2apiRecaptchaActionGuardInstalled',
        { value: true }
    );
    const originalAssign = Object.assign;
    Object.assign = function(target, ...sources) {
        let nextSources = sources;
        if (sources.length >= 2) {
            const previous = sources[sources.length - 2];
            const last = sources[sources.length - 1];
            if (
                previous &&
                typeof previous.action === 'string' &&
                previous.action &&
                last &&
                last.action === 'extension_hijack_detected'
            ) {
                nextSources = sources.slice(0, -1);
            }
        }
        return originalAssign.call(this, target, ...nextSources);
    };
})();
"""

RECAPTCHA_EXECUTE_SOURCE = r"""(async (siteKey, requestedAction, timeoutMs) => {
            const storageKey = '__flow2apiTrustedRecaptchaExecute';
            for (let attempt = 0; attempt < 40; attempt += 1) {
                if (window[storageKey]) break;
                const enterprise = window.grecaptcha && window.grecaptcha.enterprise;
                const currentExecute = enterprise && enterprise.execute;
                if (
                    attempt >= 4 &&
                    typeof currentExecute === 'function' &&
                    !String(currentExecute).includes('extension_hijack_detected')
                ) {
                    break;
                }
                await new Promise(resolve => setTimeout(resolve, 250));
            }

            const capturedExecute = !!window[storageKey];
            let execute = window[storageKey];
            let executeSource = '';
            try {
                executeSource = String(execute || '');
            } catch (error) {}

            if (!execute) {
                const enterprise = window.grecaptcha && window.grecaptcha.enterprise;
                execute = enterprise && enterprise.execute;
                try {
                    executeSource = String(execute || '');
                } catch (error) {}
                if (
                    executeSource.includes('extension_hijack_detected') &&
                    !window.__flow2apiRecaptchaActionGuardInstalled
                ) {
                    return {
                        ok: false,
                        error: 'trusted reCAPTCHA action guard is unavailable',
                        debug: {
                            hookInstalled: !!window.__flow2apiTrustedRecaptchaHookInstalled,
                            actionGuard: !!window.__flow2apiRecaptchaActionGuardInstalled,
                            captured: !!window.__flow2apiTrustedRecaptchaExecute,
                            url: location.href
                        }
                    };
                }
            }

            if (typeof execute !== 'function') {
                return { ok: false, error: 'reCAPTCHA execute is unavailable' };
            }

            const runExecute = (target) => new Promise((resolve, reject) => {
                let settled = false;
                const finish = (handler, value) => {
                    if (settled) return;
                    settled = true;
                    handler(value);
                };
                const timer = setTimeout(() => {
                    finish(reject, new Error('execute timeout'));
                }, timeoutMs);
                try {
                    Promise.resolve(execute(target, { action: requestedAction }))
                        .then(value => {
                            clearTimeout(timer);
                            finish(resolve, value);
                        })
                        .catch(error => {
                            clearTimeout(timer);
                            finish(reject, error);
                        });
                } catch (error) {
                    clearTimeout(timer);
                    finish(reject, error);
                }
            });

            let executeTarget = siteKey;
            try {
                const token = await runExecute(executeTarget);
                return { ok: true, token };
            } catch (primaryError) {
                const primaryMessage = primaryError && primaryError.message
                    ? primaryError.message
                    : String(primaryError);
                if (
                    !capturedExecute &&
                    primaryMessage !== 'execute timeout' &&
                    window.grecaptcha &&
                    window.grecaptcha.enterprise &&
                    typeof window.grecaptcha.enterprise.render === 'function'
                ) {
                    try {
                        if (window.__flow2apiRecaptchaClientId === undefined) {
                            let container = document.getElementById('__flow2api_recaptcha_holder');
                            if (!container) {
                                container = document.createElement('div');
                                container.id = '__flow2api_recaptcha_holder';
                                container.style.display = 'none';
                                (document.body || document.documentElement || document).appendChild(container);
                            }
                            window.__flow2apiRecaptchaClientId =
                                window.grecaptcha.enterprise.render(
                                    container,
                                    { sitekey: siteKey, size: 'invisible', action: requestedAction }
                                );
                        }
                        if (window.__flow2apiRecaptchaClientId !== undefined) {
                            executeTarget = window.__flow2apiRecaptchaClientId;
                            const token = await runExecute(executeTarget);
                            return { ok: true, token };
                        }
                    } catch (fallbackError) {
                        primaryError = fallbackError;
                    }
                }
                const debug = {
                    captured: capturedExecute,
                    actionGuard: !!window.__flow2apiRecaptchaActionGuardInstalled,
                    renderType: window.grecaptcha && window.grecaptcha.enterprise
                        ? typeof window.grecaptcha.enterprise.render
                        : 'missing',
                    clientId: String(window.__flow2apiRecaptchaClientId),
                    executeTarget: String(executeTarget),
                    url: location.href
                };
                return {
                    ok: false,
                    error: (primaryError && primaryError.message ? primaryError.message : String(primaryError))
                        + ' | ' + JSON.stringify(debug)
                };
            }
        })(__SITE_KEY__, __REQUESTED_ACTION__, __TIMEOUT_MS__)"""


def build_recaptcha_execute_script(site_key: str, action: str, timeout_ms: int) -> str:
    return (
        RECAPTCHA_EXECUTE_SOURCE
        .replace("__SITE_KEY__", json.dumps(site_key))
        .replace("__REQUESTED_ACTION__", json.dumps(action))
        .replace("__TIMEOUT_MS__", json.dumps(timeout_ms))
    )
