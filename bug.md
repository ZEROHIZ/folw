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

## 2026-11-07：reCAPTCHA 执行降级挂载到非空 DOM 引发 `reCAPTCHA placeholder element must be empty`

- **现象**：
  - 打码页面在未捕获到原生 `Function.prototype.bind` Hook 凭据（例如 Google Flow 前端打包更新去掉了 bind、或页面加载超时）时，触发 fallback 降级执行逻辑，报错 `reCAPTCHA execute 未返回有效 token: reCAPTCHA placeholder element must be empty`，导致重试 5 次全部失败。
- **根因**：
  - [`src/services/recaptcha_runtime.py`](src/services/recaptcha_runtime.py) 在 `!capturedExecute` 时，直接将 `document.documentElement`（`<html>` 节点）作为挂载目标传入 `grecaptcha.enterprise.render`。Google 官方 reCAPTCHA API 强制要求挂载的目标占位节点必须完全为空，因此抛出异常；且原逻辑在 `catch (renderError)` 中直接返回失败，完全阻断了后续使用 `siteKey` 直连 `execute` 的机会。
- **修复方案**：
  1. 在 [`src/services/recaptcha_runtime.py`](src/services/recaptcha_runtime.py) 中，动态创建专属的隐藏空节点 `div#__flow2api_recaptcha_holder` 挂载到文档中，满足 Google 的空占位节点规范。
  2. 增强容错降级链：若 `render` 过程中出现任何异常，不再中断流程，而是优雅回退将目标重置为 `siteKey`，直接调用 `execute(siteKey, ...)` 获取验证码，确保打码链路的韧性。

## 2026-11-07：`grecaptcha.execute only works with invisible reCAPTCHA.` 与项目页 `/404?reason=project` 重定向

- **现象**：
  - 日志报错 `ℹ️ [BrowserCaptcha] Token-0 打码 真实页面已就绪: url=https://flow.google.com/404?reason=project` 以及 `⚠️ [BrowserCaptcha] reCAPTCHA execute 未返回有效 token: grecaptcha.execute only works with invisible reCAPTCHA. | {"captured":false,"actionGuard":true,"renderType":"function","clientId":"0","executeTarget":"0","url":"https://flow.google.com/404?reason=project"}`。
- **根因**：
  1. Flow 前端 `AiSandboxAngularFrontend` 通过 `enterprise.js?trustedtypes=true&render=<siteKey>` 加载 Enterprise reCAPTCHA，并在特性开关 `a.na.Zaa()` 未开启时不调用 `c.execute.bind(c)` 也不注入 `extension_hijack_detected`，导致 `capturedExecute` 为 `false`。此时 [`RECAPTCHA_EXECUTE_SOURCE`](src/services/recaptcha_runtime.py) 在未尝试 `execute(siteKey, ...)` 的情况下提前调用了 `grecaptcha.enterprise.render(container, { sitekey: siteKey, action: requestedAction })` 且未传 `size: 'invisible'`，导致 Google 默认渲染为 `normal` 复选框组件（`clientId = 0`），随后调用 `execute(0, ...)` 抛出 `grecaptcha.execute only works with invisible reCAPTCHA.`。
  2. 数据库 [`data/flow.db`](data/flow.db) 中 Token 1 绑定的 4 个 `project_id`（如 `c372e5b4-3b98-4764-b813-05028b8982fc`）在该 Google 账号下不存在（`ngNC2` 返回 `NOT_FOUND`），导致访问 `/project/<id>` 时被前端路由重定向到 `/404?reason=project`。
- **修复方案**：
  1. 重构 [`src/services/recaptcha_runtime.py`](src/services/recaptcha_runtime.py) 中的 `RECAPTCHA_EXECUTE_SOURCE`：当 `grecaptcha.enterprise.execute` 就绪且未被 `extension_hijack_detected` 污染时跳过无意义的 10s 轮询等待；执行时优先以 `siteKey` 直接调用 `execute(siteKey, { action: requestedAction })`，仅在显式模式回退时才以 `{ sitekey: siteKey, size: 'invisible', action: requestedAction }` 调用 `render`。
  2. 在 [`TokenBrowser._prepare_flow_runtime_page`](src/services/browser_captcha.py) 中增加 `/404` 重定向检测，若项目页落入 `/404` 自动回退至 `https://flow.google.com/projects`；同时将 [`data/flow.db`](data/flow.db) 中 Token 1 的项目池同步为账号下真实存在的 4 个项目 ID。

## 2026-11-07：开启「自动校验登录态」后 Cookie 依然过期及「过期时间 54分钟」始终标红

- **现象**：
  1. 用户一直开启「自动校验登录态（刷新间隔 30 分钟）」，但运行一段时间后 Google Cookie 仍然过期掉线。
  2. 刚刷新或刚导入成功的账号，列表中的「过期时间」立即显示红色的 `54分钟`。
- **根因**：
  1. **协议刷新未触发 Google 会话滚动续期（`__Secure-1PSIDTS` 未轮换）**：[`TokenManager._try_refresh_st`](src/services/token_manager.py) 原逻辑先调用 `_try_protocol_refresh_st`（仅用 `curl_cffi` 请求 `GET https://flow.google.com/projects`）。纯 HTTP 请求不会执行页面内的 Google 账号保活脚本（`accounts.google.com/RotateCookies`），因此不会续期 `__Secure-1PSIDTS`，但只要当时还没死就会返回 `True`，导致直接跳过后面的真实浏览器刷新。等 `__Secure-1PSIDTS` 自然到期后整个会话直接失效。
  2. **原始 Cookie 字符串转存丢失 `.google.com` 作用域**：[`ProtocolLogin._cookie_jar`](src/services/protocol_login.py) 将无 `domain` 的原始 Cookie Header 统一存成了 `domain="flow.google.com", hostOnly=True`，导致 [`build_browser_cookie_targets`](src/services/browser_cookie_utils.py) 认为其已有显式作用域而未展开到 `.google.com`，使得浏览器内 `accounts.google.com` 拿不到 `SID` / `__Secure-1PSID` 无法完成旋转续期。
  3. **过期预警阈值设置过大**：[`FlowClient.st_to_at`](src/services/flow_current.py) 每次将下次校验时间 `at_expires` 设为 `当前时间 + 55 分钟`，而 [`get_tokens`](src/api/admin.py) 将 `at_expiring_within_1h` 设为 `< 3600 秒`（60 分钟），导致任何刚刷新的账号永远小于 60 分钟，前端一直标红显示 `54分钟`。
- **修复方案**：
  1. 在 [`build_browser_cookie_targets`](src/services/browser_cookie_utils.py) 中识别被合成为 `flow.google.com` (`hostOnly=True`) 的非 `OSID` Google 根域 Cookie，自动将其同时展开到 `target_urls`（覆盖 `.google.com`）。
  2. 在 [`TokenManager._try_refresh_st`](src/services/token_manager.py) 中，当处于 `browser` / `cloakbrowser` / `personal` 模式且常驻浏览器已启动时，优先通过常驻浏览器刷新 Cookie，并在 [`TokenBrowser.refresh_google_cookies`](src/services/browser_captcha.py) 中增加 `3.0s` 页面保活等待以完成 `RotateCookies` 续期。
  3. 将 [`get_tokens`](src/api/admin.py) 的即将过期标红阈值从 `< 3600`（60 分钟）调整为 `< 900`（15 分钟）。

## 2026-11-07：`GET /api/captcha/runtime-status?method=cloakbrowser` 抛出 `ValueError: Unsupported runtime kind: cloakbrowser`

- **现象**：
  - 切换或加载 `cloakbrowser` 打码模式时，后台轮询 `/api/captcha/runtime-status?method=cloakbrowser` 返回 `500 Internal Server Error`，日志报错 `ValueError: Unsupported runtime kind: cloakbrowser`。
- **根因**：
  - [`src/core/browser_runtime_status.py`](src/core/browser_runtime_status.py) 的 `_RUNTIME_STATUS` 字典仅注册了 `"browser"` 和 `"personal"`，未包含新增的 `"cloakbrowser"`。
- **修复方案**：
  - 在 [`src/core/browser_runtime_status.py`](src/core/browser_runtime_status.py) 的 `_RUNTIME_STATUS` 中注册 `"cloakbrowser"`，并在 `_update_status` 中将 `"browser"` 与 `"cloakbrowser"` 状态同步更新。

## 2026-11-07：多任务同时发起时排在后面的请求 30 秒超时失败
- **现象**：
  - 同时发起多个图片/视频生成请求时，排在后面的请求在等待约 30 秒后直接报错超时（`Browser captcha queue wait timeout`）。
- **根因**：
  1. [`BrowserCaptchaService.QUEUE_WAIT_SECONDS`](src/services/browser_captcha.py) 原先硬编码为 `30.0` 秒，而单个有头浏览器槽位完成一次页面预热（6s）+ reCAPTCHA 取码 + 请求提交耗时约 15~25 秒，并发数超过槽位数时第 3 个及之后的请求必在 30 秒内拿不到槽位而抛出 `BrowserCaptchaQueueTimeout`。
  2. 生成入口 [`GenerationHandler.handle_generation`](src/services/generation_handler.py) 缺少前置 FIFO 队列调度，所有并发请求同时涌入并提前占用 Token 与打码等待队列，且无法在后台查看或手动取消尚未开始执行的排队任务。
- **修复方案**：
  1. 新增 [`GenerationQueueManager`](src/services/queue_manager.py)，根据当前打码方式与浏览器槽位容量计算动态并发上限，超出并发的请求按 FIFO 顺序排队等待（支持在系统配置中自定义 `queue_timeout`），并支持随时手动取消处于 `queued` 状态的待执行任务。
  2. 将 [`BrowserCaptchaService.QUEUE_WAIT_SECONDS`](src/services/browser_captcha.py) 提升至 `600.0` 秒（并动态取 `max(600.0, image_timeout * 2)`）。
  3. 在 [`src/api/admin.py`](src/api/admin.py) 与 [`static/manage.html`](static/manage.html) 中新增「队列任务」标签页与取消接口（`GET /api/queue/tasks`、`POST /api/queue/{task_id}/cancel`、`POST /api/queue/cancel-all`）。

## 2026-11-09：移除 CloakBrowser 静默回退，内核与授权缺失时直接显式报错阻断

- **现象**：
  - 当打码方式显式配置为 `cloakbrowser` 时，若服务器/容器内未安装 CloakBrowser Pro 内核或未配置 License Key，系统原先会静默降级回退到 Playwright 普通 Chromium。这会导致用户误以为自己在用高强度防指纹浏览器，但实际上使用的是极易被风控检测的普通 Chromium。
- **根因**：
  - [`src/services/browser_captcha.py`](src/services/browser_captcha.py) 的 `_ensure_browser_installed()` 在探测不到 `_resolve_cloakbrowser_executable()` 时，自动向下走到了普通 Chromium 探测及自动下载逻辑；`TokenBrowser._create_browser()` 在 executable 为空时直接回退传给 Playwright 启动默认浏览器。
- **修复方案**：
  1. 在 [`src/services/browser_captcha.py`](src/services/browser_captcha.py) 的 `_ensure_browser_installed()` 与 `TokenBrowser._create_browser()` 中增加严格校验：当 `config.captcha_method == "cloakbrowser"` 时，若 Pro 内核文件不存在或未配置 License Key，立即抛出明确异常（`RuntimeError: 当前打码方式为 [cloakbrowser]，但未检测到 CloakBrowser Pro 内核文件 / 未配置 License Key`），绝不静默回退。
  2. 在 [`src/api/admin.py`](src/api/admin.py) 的 `_prepare_captcha_runtime()` 中增加前置检查，避免用户误配置后进入无意义的等待，直接提示用户在管理端更换打码方式或安装内核。

## 2026-11-08：合并上游主分支功能（Omni 1.2 Flash 与 账号配额耗尽/风控无感换号重试）

- **背景与需求**：
  - 上游官方仓库更新了两个关键功能：
    1. **Omni 1.2 Flash 视频模型支持**：引入 `macaron_cookie_t2v_*` / `macaron_cookie_r2v_*`（3s~15s，横竖屏），替换上游即将于 2026-12-05 下线的旧代 `abra` 模型族。
    2. **账号配额耗尽/风控自动换号重试 (PR #194)**：当账号报错 `PUBLIC_ERROR_USER_QUOTA_REACHED`（额度耗尽）或 `PUBLIC_ERROR_UNUSUAL_ACTIVITY`（风控）时，自动将该账号标记冷却并无缝切换至下一个可用 Token，最多重试 6 次，避免客户端直接报错失败。
- **冲突防范与精细合并**：
  - 本地拥有 CloakBrowser 专业版环境隔离、FIFO 请求队列管理器（`queue_manager.py`）、后台静默开关以及 `.google.com` 被动 SSO 续期等核心改动，禁止直接执行粗暴的 `git merge upstream/main`。
  - **实现方案**：
    1. [`src/services/load_balancer.py`](src/services/load_balancer.py)：同步上游的额度冷却与风控连续失败追踪（`_quota_cooldown`、`_risk_streak`、`mark_quota_cooldown`、`mark_risk_cooldown`、`clear_quota_cooldown`），在 `select_token` 中引入 `exclude_token_ids`。
    2. [`src/services/generation_handler.py`](src/services/generation_handler.py)：
       - 注册 `omni-1.2-flash` 3s~15s 全系列模型及默认 `macaron_cookie_r2v_8s` 参考图模型；
       - 在排队等待后执行的生成循环中接入 `for token_attempt in range(MAX_TOKEN_FAILOVER_ATTEMPTS)` 换号循环，在 `except` 中区分配额耗尽与风控，并保持与 `queue_manager` 状态汇报完全兼容；
       - 在 `_should_count_token_error` 中排除 `PUBLIC_ERROR_USER_QUOTA_REACHED`，防止账号额度用尽时被误判为异常 Token 而遭到禁用。
    3. [`src/services/token_manager.py`](src/services/token_manager.py)：为 `refresh_credits` 增加 `raise_on_error: bool = False` 参数，换号时主动拉取真实余额以作为冷却解除的精准基准。
    4. 引入 [`tests/test_token_quota_failover.py`](tests/test_token_quota_failover.py)，全量 18 项单测全部通过验证。


- **现象**：
  - 空闲挂机 1 小时后，后台刷新报错 `ValueError: Flow 页面未返回已登录账号，请重新同步 Google Cookies`，Cookie 依然在 1 小时整点过期。
- **根因**：
  1. **Playwright URL 注入方式产生 `hostOnly=True` 阻断 `accounts.google.com`**：此前 [`build_browser_cookie_targets`](src/services/browser_cookie_utils.py) 通过 `"url": "https://flow.google.com/"` 和 `"url": "https://www.google.com/"` 展开 Cookie，Playwright/Chromium 对传 `url` 的 Cookie 一律标记为 `hostOnly=True`（仅限该子域名），而非 `domain=".google.com"`。而 Google 的核心滚动续期接口运行在 `https://accounts.google.com/RotateCookies`，由于 `SID`、`__Secure-1PSID`、`__Secure-1PSIDTS` 被锁死在 `flow.google.com` 和 `www.google.com` 的 `hostOnly=True` 下，浏览器向 `accounts.google.com` 发请求时根本不会携带这些根 Cookie，导致 `__Secure-1PSIDTS` 无法旋转续期，1 小时后必然过期。
  2. **常驻保活页停留在 `about:blank` 且启动参数禁用了后台网络**：[`TokenBrowser`](src/services/browser_captcha.py) 启动参数包含 `--disable-background-networking`，且 `_shared_keepalive_page` 停留在 `about:blank`，挂机期间浏览器内没有打开 Flow 页面执行 Google 原生 `RotateCookies` 定时器；同时 `refresh_google_cookies` 未先走 `accounts.google.com/ServiceLogin?passive=true` 被动 SSO 续期，也未同步更新 `_shared_bound_cookie_signature` 导致下次复用时 `clear_cookies()` 清空原生会话。
- **修复方案**：
  1. 在 [`build_browser_cookie_targets`](src/services/browser_cookie_utils.py) 与 [`ProtocolLogin._cookie_jar`](src/services/protocol_login.py) 中，将除 `OSID`/`__Secure-OSID` 以外的所有 Google 根会话 Cookie 统一设置为 `domain=".google.com"`（`hostOnly=False`），确保 `accounts.google.com`、`flow.google.com`、`www.google.com` 共享同一套根登录态。
  2. 在 [`TokenBrowser._create_browser`](src/services/browser_captcha.py) 中移除 `--disable-background-networking` 并启用 `--disable-background-timer-throttling`；在 `_warmup_browser_slot` 与 [`TokenBrowser.refresh_google_cookies`](src/services/browser_captcha.py) 中让 `_shared_keepalive_page` 常驻 `https://flow.google.com/projects`，刷新时先经过 `accounts.google.com/ServiceLogin?passive=true&continue=https%3A%2F%2Fflow.google.com%2Fprojects` 完成联合续期并同步更新 `_shared_bound_cookie_signature`。
  3. 在 [`TokenManager._try_refresh_st`](src/services/token_manager.py) 中改为直接调用 `await BrowserCaptchaService.get_instance(self.db)`，并将默认刷新周期调优为 15 分钟。


