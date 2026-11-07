# Bug 档案记录 (`bug.md`)

## 2026-11-07：浏览器 Cookie 持久化与注入导致 `flow.google.com` 的 `OSID` 丢失及被镜像污染

- **现象**：
  - 首次使用完整的 19 条 Google Cookie 时，首个标签页能正常进入 `https://flow.google.com/projects`，但在执行 `_warmup_google_context_cookies` -> `_persist_context_cookies_to_token` 将浏览器 Cookie 写回数据库后，Cookie 数量膨胀至 80 条；后续新建/重建标签页打开 `https://flow.google.com/projects` 时全部被重定向到 `https://accounts.google.com/v3/signin/identifier`（未登录状态）。
- **根因**：
  1. [`BrowserCaptchaService._build_personal_cookie_targets`](src/services/browser_captcha_personal.py) 与 [`BrowserCaptchaService._build_configured_browser_cookie_targets`](src/services/browser_captcha_personal.py) 在处理通过 `"url": "https://flow.google.com/"` 指定作用域（无 `domain` 字段）的 Host-Only Cookie（如 `OSID`、`__Secure-OSID`）时，原条件 `if explicit_domain or not google_family_scope:` 为 `False`，导致原 `flow.google.com` 域下的 `OSID` / `__Secure-OSID` 被直接丢弃。
  2. 同时，镜像逻辑将 `flow.google.com` 专属的 `OSID` / `__Secure-OSID` 镜像到了 `www.google.com` 和 `www.recaptcha.net`，随后 [`BrowserCaptchaService._serialize_browser_cookie_for_storage`](src/services/browser_captcha_personal.py) 在持久化回 `token.google_cookies` 时把这些镜像 Cookie 一并存入数据库，污染了原始 Cookie 集合。
- **修复方案**：
  1. 在 [`_build_personal_cookie_targets`](src/services/browser_captcha_personal.py) 和 [`_build_configured_browser_cookie_targets`](src/services/browser_captcha_personal.py) 中保留带有 `explicit_url` 的 Cookie，并禁止将 `scope_host == "flow.google.com"` 的专属 Cookie 镜像到 `www.google.com` / `www.recaptcha.net`。
  2. 在 [`_serialize_browser_cookie_for_storage`](src/services/browser_captcha_personal.py) 中过滤掉 `recaptcha.net` 镜像域 Cookie 以及 `www.google.com` 下的 `OSID` / `__Secure-OSID`，防止回写污染 `token.google_cookies`。

