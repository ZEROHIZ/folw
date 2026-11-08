"""
核心职责：验证本地浏览器 reCAPTCHA 运行时脚本的协议动作保留、直接 siteKey 执行与 invisible render 兜底行为。
主要功能：
1. 验证 TokenBrowser._execute_captcha 对非 CHAT_GENERATION 动作直接走协议执行；
2. 验证 TRUSTED_RECAPTCHA_HOOK_SOURCE 与 ActionGuard 保留目标 action；
3. 验证未捕获 bind 时优先以 siteKey 直接调用 execute，仅在显式模式回退时以 size='invisible' 调用 render。
"""

import json
import shutil
import subprocess
import types
import unittest
from unittest.mock import AsyncMock

from src.services.browser_captcha import TokenBrowser
from src.services.recaptcha_runtime import (
    TRUSTED_RECAPTCHA_HOOK_SOURCE,
    build_recaptcha_execute_script,
)


class LocalCaptchaActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_protocol_actions_execute_without_native_generation(self):
        for action in ("IMAGE_GENERATION", "VIDEO_GENERATION", "UPLOAD_IMAGE"):
            with self.subTest(action=action):
                browser = object.__new__(TokenBrowser)
                browser.token_id = 7
                browser._apply_browser_environment_patch = AsyncMock()
                browser._prepare_flow_runtime_page = AsyncMock(return_value=True)
                browser._harvest_token_via_ui_submit = AsyncMock()
                page = types.SimpleNamespace(
                    add_init_script=AsyncMock(),
                    evaluate=AsyncMock(return_value={"ok": True, "token": "captcha"}),
                    close=AsyncMock(),
                )
                context = types.SimpleNamespace(new_page=AsyncMock(return_value=page))
                token = await browser._execute_captcha(
                    context, "project", "site-key", action
                )
                self.assertEqual(token, "captcha")
                self.assertIn(json.dumps(action), page.evaluate.call_args.args[0])
                browser._harvest_token_via_ui_submit.assert_not_awaited()
                self.assertIsNone(browser._last_harvest_session_id)
                page.close.assert_awaited_once()

    async def test_execution_failure_closes_page_without_chat_fallback(self):
        browser = object.__new__(TokenBrowser)
        browser.token_id = 7
        browser._apply_browser_environment_patch = AsyncMock()
        browser._prepare_flow_runtime_page = AsyncMock(return_value=True)
        browser._harvest_token_via_ui_submit = AsyncMock()
        page = types.SimpleNamespace(
            add_init_script=AsyncMock(),
            evaluate=AsyncMock(side_effect=RuntimeError("execution failed")),
            close=AsyncMock(),
        )
        token = await browser._execute_captcha(
            types.SimpleNamespace(new_page=AsyncMock(return_value=page)),
            "project",
            "site-key",
            "IMAGE_GENERATION",
        )
        self.assertIsNone(token)
        browser._harvest_token_via_ui_submit.assert_not_awaited()
        page.close.assert_awaited_once()

    @unittest.skipUnless(
        shutil.which("node"), "Node.js is required for the JavaScript contract"
    )
    def test_shared_execute_script_preserves_requested_action(self):
        source = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
(async () => {
  const sandbox = { setTimeout, clearTimeout, location: { href: 'https://flow.google.com/project/example' } };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(input.hook, sandbox);
  vm.runInContext(`
    globalThis.calls = [];
    globalThis.grecaptcha = { enterprise: {
      execute: function(key, options) { calls.push({ key, action: options.action }); return Promise.resolve('captcha'); }
    }};
    const original = grecaptcha.enterprise.execute.bind(grecaptcha.enterprise);
    grecaptcha.enterprise.execute = function(key, options) {
      return original(key, Object.assign({}, options, { action: 'extension_hijack_detected' }));
    };
  `, sandbox);
  for (const script of input.scripts) {
    const result = await vm.runInContext(script, sandbox);
    if (!result.ok || result.token !== 'captcha') throw Error('captcha execution failed');
  }
  const actual = sandbox.calls.map(call => call.action);
  if (JSON.stringify(actual) !== JSON.stringify(input.actions)) throw Error('action mismatch');
  if (sandbox.calls.some(call => call.key !== input.siteKey)) throw Error('site key mismatch');
})().catch(error => { process.stderr.write(error.message); process.exitCode = 1; });
"""
        actions = [
            "IMAGE_GENERATION",
            "VIDEO_GENERATION",
            "UPLOAD_IMAGE",
            "CHAT_GENERATION",
        ]
        site_key = 'site-key"\\value'
        result = subprocess.run(
            [shutil.which("node"), "-e", source],
            input=json.dumps(
                {
                    "hook": TRUSTED_RECAPTCHA_HOOK_SOURCE,
                    "scripts": [
                        build_recaptcha_execute_script(site_key, action, 1000)
                        for action in actions
                    ],
                    "actions": actions,
                    "siteKey": site_key,
                }
            ),
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(
        shutil.which("node"), "Node.js is required for the JavaScript contract"
    )
    def test_uncaptured_execute_prefers_sitekey_and_invisible_render_fallback(self):
        source = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
(async () => {
  // Case 1: uncaptured execute where siteKey client is already registered (like enterprise.js?render=<siteKey>)
  const sandbox1 = {
    setTimeout,
    clearTimeout,
    location: { href: 'https://flow.google.com/project/example' },
    document: {
      getElementById: () => null,
      createElement: () => ({ style: {} }),
      body: { appendChild: () => {} }
    }
  };
  sandbox1.window = sandbox1;
  vm.createContext(sandbox1);
  vm.runInContext(input.hook, sandbox1);
  vm.runInContext(`
    globalThis.renderCalled = false;
    globalThis.grecaptcha = { enterprise: {
      render: function() { globalThis.renderCalled = true; return 0; },
      execute: function(target, options) {
        if (target === 0) throw new Error('grecaptcha.execute only works with invisible reCAPTCHA.');
        return Promise.resolve('token-from-sitekey:' + options.action);
      }
    }};
  `, sandbox1);
  const res1 = await vm.runInContext(input.script, sandbox1);
  if (!res1.ok || res1.token !== 'token-from-sitekey:IMAGE_GENERATION') {
    throw new Error('Case 1 failed: ' + JSON.stringify(res1));
  }
  if (sandbox1.renderCalled) {
    throw new Error('Case 1 should not call render before trying siteKey execute');
  }

  // Case 2: explicit render mode where execute(siteKey) throws until render({ size: 'invisible' }) is called
  const sandbox2 = {
    setTimeout,
    clearTimeout,
    location: { href: 'https://flow.google.com/404?reason=project' },
    document: {
      getElementById: () => null,
      createElement: () => ({ style: {} }),
      body: { appendChild: () => {} }
    }
  };
  sandbox2.window = sandbox2;
  vm.createContext(sandbox2);
  vm.runInContext(input.hook, sandbox2);
  vm.runInContext(`
    globalThis.renderedSize = null;
    globalThis.grecaptcha = { enterprise: {
      render: function(container, params) {
        globalThis.renderedSize = params.size;
        return 0;
      },
      execute: function(target, options) {
        if (target !== 0) return Promise.reject(new Error('No reCAPTCHA clients exist.'));
        if (globalThis.renderedSize !== 'invisible') {
          return Promise.reject(new Error('grecaptcha.execute only works with invisible reCAPTCHA.'));
        }
        return Promise.resolve('token-from-invisible-render:' + options.action);
      }
    }};
  `, sandbox2);
  const res2 = await vm.runInContext(input.script, sandbox2);
  if (!res2.ok || res2.token !== 'token-from-invisible-render:IMAGE_GENERATION') {
    throw new Error('Case 2 failed: ' + JSON.stringify(res2));
  }
})().catch(error => { process.stderr.write(error.message); process.exitCode = 1; });
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", source],
            input=json.dumps(
                {
                    "hook": TRUSTED_RECAPTCHA_HOOK_SOURCE,
                    "script": build_recaptcha_execute_script(
                        "site-key", "IMAGE_GENERATION", 1000
                    ),
                }
            ),
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
