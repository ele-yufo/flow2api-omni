# Pro 账号池 ST 自我续命 — 设计文档

- 日期：2026-06-17
- 状态：已实测验证，待实现
- 背景关联：去水印功能（commit a4ceee0 / 03081be）落地后，计划用 ~15 个 Pro 账号池替代单个 Ultra 账号以降成本

## 1. 背景与目标

### 现状痛点
当前为每个账号开一个常驻 Chrome（挂 `Flow2API Token Updater` 扩展），扩展每 60 分钟后台打开 labs.google、读出 `__Secure-next-auth.session-token`（下称 **ST**）POST 给 `/api/plugin/update-token` 续命。扩展是**未打包扩展**，每个 profile 都要开开发者模式手动导入。

扩展到 15 个 Pro 账号后，这套方式的问题：
- 需要 15 个浏览器/profile 常驻，内存与运维负担大；
- 每个新号都要「开发者模式 + 导入扩展」，繁琐；
- 维护变成「每天伺候一堆浏览器」的打地鼠。

### 核心目标
**让 15 个 Pro 账号的凭证管理做到：每个号 ST 只注入一次，之后服务器纯 HTTP 自我续命，零常驻浏览器，运维降级为「偶尔某个号失效时重登一次」。**

### 关键认知（澄清「管什么」）
系统里真正需要管理的**只有 ST**。其余全自动：
- **AT（bearer）**：短命，`token_manager._should_refresh_at`（`token_manager.py:371`）在快过期时用 `flow_client.st_to_at(st)`（`flow_client.py:790`）自动换，**纯 HTTP，不开浏览器**。
- **reCAPTCHA token**：每次提交由 `_get_recaptcha_token(project_id, ...)`（`flow_client.py:1184`）从**唯一共享登录浏览器**（`/opt/flow2api-profiles/ultra`）统一出，**不按账号开浏览器**。
- 负载均衡器（`load_balancer.select_token`）已支持在多 token 间按 tier/load 选号。

因此「15 个浏览器常驻」纯粹是 ST 注入方式的副产物，架构上并不需要。

## 2. 关键实测发现（本设计的事实基础）

2026-06-17 用库中真实 Pro 账号 ST（id=15, gentanaka606@gmail.com）对 `GET https://labs.google/fx/api/auth/session` 做只读探针，复用项目自身 `_build_request_headers`（`use_st=True`, `impersonate=chrome110`），结论：

1. **每次换 AT，labs.google 都通过 `Set-Cookie` 回发一个轮换后的新 ST**（新旧值不同）。当前 `_make_request`（`flow_client.py:498`）只取 `response.json()`，把这个续期 cookie 丢弃了。
2. **轮换是滚动续期**：回发的 session-token cookie 属性为 `Expires=Thu, 16 Jul 2026 ...`，即从「当下」起 ~30 天。也就是说每用一次就把 ST 寿命续满 30 天。
3. **旧 ST 在轮换后仍有效**：用同一个旧 ST 连打两次都返回 200。说明 NextAuth 为 JWT(JWE `dir` 加密) 无状态策略，签发新 token 不会立刻吊销旧 token → **捕获并存库不会打断在途的并发请求，并发安全**。

推论：**只要服务器至少每 30 天用一次某账号，并把回发的新 ST 存库，该账号 ST 即可永久自我续命，无需任何浏览器（除首次获取 ST）。** 失效仅来自 Google 主动撤销（封号/改密/风控）。

## 3. 架构总览

```
[首次] 人工登录 labs.google → DevTools/cookies.txt 拿到 ST → 智能粘贴框 → DB.tokens.st
                                                                      │
[此后全自动，零浏览器]                                                 ▼
  生成请求 / 每日保活巡检 ──> token_manager 触发 st_to_at(st)
                                     │  请求里带旧 ST(Cookie)
                                     ▼
                          labs.google /auth/session
                                     │  响应: body=AT + Set-Cookie=轮换后新 ST
                                     ▼
                 flow_client.st_to_at 解析 Set-Cookie → 返回 {access_token, expires, user, rotated_st}
                                     │
                                     ▼
              token_manager._do_refresh_at 在 per-token 刷新锁内，把 rotated_st 回写 DB
                                     │
                                     ▼
                         DB.tokens.st 始终保持「~30 天后才过期」
```

管理单元只有 `tokens.st` 一列。AT、credits、ST 续期全部搭在「本来就会发生的 AT 刷新」这趟车上完成。

## 4. 组件设计

### 组件 1：ST 轮换捕获（核心，改动最小）

**目标**：把 labs.google 回发的轮换 ST 接住并存库。

- `flow_client.st_to_at(st)` 改为额外返回 `rotated_st`：在该调用的响应中解析 `Set-Cookie` 里的 `__Secure-next-auth.session-token`，放入返回字典 `{"access_token", "expires", "user", "rotated_st"}`。`rotated_st` 在未回发或解析失败时为 `None`。
  - 实现要点：`_make_request` 目前只返回 `response.json()`。为该路径提供「连同响应 cookie 一起返回」的能力（例如新增内部参数 `return_set_cookie: bool=False`，仅 `st_to_at` 使用，对其余调用零影响），由 `st_to_at` 解析出 session-token 值。`flow_client` **保持无状态、不直接访问 DB**（沿用现有分层）。
- `token_manager._do_refresh_at(token_id, st)`（`token_manager.py:477`）在拿到 `rotated_st` 后，于**已有的 per-token 刷新锁** `_refresh_locks` 内，满足以下条件才回写 `tokens.st`：
  1. `rotated_st` 非空；
  2. `rotated_st != 当前 st`（避免无意义写）；
  3. 通过长度护栏 `len(rotated_st) >= 200`（ST 实测 ~1064；护栏防止把异常短值写入，参见 [[feedback-captcha-token-length-guard]] 的同类思想）。
- 选择 `st_to_at` 作为唯一捕获点的理由：AT 刷新、`update-token`、`add-token`、每日保活**全部经过 `st_to_at`**，单一收口即覆盖所有路径。

**并发安全**：实测旧 ST 轮换后仍有效，且回写在 per-token 锁内串行，绝不会让在途请求拿到失效 ST。

### 组件 2：每日保活巡检（兜底闲置账号）

**目标**：保证即使某账号长期没有生成请求，其 ST 也不会逼近 30 天死线。

- 在 `main.py` lifespan 内，照搬现有 `auto_unban_task`（`main.py:116`）范式新增 `st_keepalive_task`：
  - 周期默认 24 小时（远小于 30 天，留足余量），可配置；
  - 遍历所有 `is_active=1` 的 token，对每个调用一个新方法 `token_manager.keepalive_rotate_st(token)`，其内部**强制**调用 `st_to_at`（即使 AT 仍有效也要触发，以滚动 ST），复用组件 1 的回写逻辑；
  - 单个 token 失败不影响其余（逐个 try/except），失败计入组件 4 的健康状态。
- 注意 `_should_refresh_at` 仅在 AT 剩 <1h 时刷新，无法覆盖「AT 长期有效但闲置」的账号，故保活必须独立强制触发，不能复用 `ensure_valid_token` 的惰性判断。

### 组件 3：智能粘贴注入（替代「开发者模式导入扩展」）

**目标**：首次拿 ST 的步骤做到零安装、容错。**确定的主用法（用户选定）：用户登录后导出 `cookies.txt`，把全文整段粘进后台文本框，服务器自动抠出 ST。** 用户无需关心 cookies.txt 里哪一条是 ST。

- 新增解析器 `extract_session_token(raw: str) -> str`，自动从以下任一格式抽取 `__Secure-next-auth.session-token`：
  1. **Netscape `cookies.txt` 全文**（制表符分隔，第 6 列为 name、第 7 列为 value）—— **主路径**；
  2. **裸 ST 值**（以 `eyJ` 开头的 JWE）；
  3. **`Cookie:` 请求头**（`a=b; __Secure-next-auth.session-token=...; c=d`，按 `;` 切分找键）；
  4. **JSON 数组**（DevTools「Copy all as JSON」/ EditThisCookie 导出的 `[{"name":..,"value":..}]`）。
  - 抽取后施加同一长度护栏（`>= 200`）；抽不到或过短则返回 400 报错并提示。
  - 解析鲁棒性：cookies.txt 可能含注释行（`# ...`）、`#HttpOnly_` 前缀域名行、空行，解析器需跳过/容忍；按 name 列精确匹配 `__Secure-next-auth.session-token`，命中多条时取最后一条（最新）。
- 接入点：
  - `POST /api/tokens`（`admin.py:684`）的 `AddTokenRequest` 增加可选字段 `raw: Optional[str]`；当提供 `raw` 时先 `extract_session_token` 再走原 `st` 流程（`st` 与 `raw` 二选一）。
  - `manage.html` 增加一个「粘贴 cookies.txt 全文 / ST / Cookie 头」文本框（多行 textarea）。
- **前端明确指出目标网站，支持跳转与复制**：在文本框上方放一个操作区，含目标 URL `https://labs.google/fx/tools/flow`，提供：
  - 一个**可点击跳转**的链接（`target="_blank" rel="noopener"`，新标签打开，用户在此登录 Google 账号——未登录会自动跳 `accounts.google.com`，登录后该页即种下 `__Secure-next-auth.session-token`）；
  - 一个**「复制网址」按钮**（一键复制上述 URL 到剪贴板）。
  - 内联帮助文字给出取 cookies.txt 的两种零安装/低成本方式，并备注 **HttpOnly 约束**（见下）：
    > ① 浏览器装「Get cookies.txt LOCALLY」类扩展，在 labs.google Flow 页导出 cookies.txt → 全文粘贴；
    > 或 ② DevTools 法（零安装）：F12 → Application → Cookies → `https://labs.google` → 复制 `__Secure-next-auth.session-token` 的 Value → 粘贴。
- **HttpOnly 约束说明**：`__Secure-next-auth.session-token` 是 HttpOnly，网页 JS（书签小工具）读不到，只有 DevTools / 带 cookies 权限的扩展（含 cookies.txt 导出类扩展）能取。故首次获取仍需人工借助 DevTools 或扩展，但**全程一次性**，拿到后浏览器即可关闭。
- 现有 Chrome 扩展与 `/api/plugin/update-token`（`admin.py:1713`）**保留但降级**为「开新号时可选的一次性工具」，不再承担每小时保活职责。

### 组件 4：健康看板 + 告警 + 额度感知调度

**目标**：把「打地鼠」频率降到「偶尔某号被 Google 撤销时重登一次」，并避免额度耗尽的号继续被选中报错。

- **健康状态**：为每个 token 记录「最近一次 ST 续期成功时间」「连续续期失败次数」。当 `st_to_at` 持续返回 401/`UNAUTHENTICATED`（ST 被撤销），沿用现有 `disable_token` 流程并把 `ban_reason` 标记为 `ST_REVOKED`。
  - `GET /api/tokens`（`admin.py:650`）返回体补充上述字段；`manage.html` 列表用色块标识「健康 / 即将过期 / 已失效」。
  - 可选告警：`setting.toml` 增加 `st_alert_webhook_url`，非空时在某号被标 `ST_REVOKED` 时 POST 一条通知（账号 email + 原因）。为空则仅记日志。
- **额度感知调度**（增量）：15 个 Pro 各 1000 额度，`credits` 已在每次 `_do_refresh_at` 经 `get_credits` 刷新（`token_manager.py:515`）。在 `load_balancer.select_token`（`load_balancer.py:120`）增加：
  - 跳过 `credits <= min_credits_to_select`（配置，默认 1）的 active token；
  - polling 模式下按剩余额度排序优先选额度多的，平滑耗尽全池。

## 5. 数据流（端到端）

1. **首次注入**：人工登录 → DevTools/cookies.txt 取 ST → 智能粘贴框 → `extract_session_token` → `POST /api/tokens` → `token_manager.add_token`（内部 `st_to_at` 验证并取 email/credits/AT）→ 入库。
2. **生成请求**：`routes` → `load_balancer.select_token`（额度感知）→ `ensure_valid_token` → 必要时 `_do_refresh_at` → `st_to_at` 换 AT 并**捕获轮换 ST 回写** → 提交生成。
3. **每日保活**：`st_keepalive_task` → 遍历 active token → `keepalive_rotate_st` → `st_to_at` → 回写轮换 ST。
4. **失效处理**：`st_to_at` 连续 401 → `disable_token(ban_reason=ST_REVOKED)` → 健康看板标红 + 可选 webhook → 人工对该号重登一次、重新粘贴。

## 6. 错误处理与边界

- **并发**：ST 回写在 per-token 刷新锁内串行；旧 ST 轮换后仍有效（实测）→ 在途请求不受影响。
- **轮换缺失**：某次响应未回发 Set-Cookie（NextAuth `updateAge` 未到）→ `rotated_st=None`，跳过回写，沿用旧 ST，无害。
- **30 天死线**：每日保活确保闲置号也持续滚动；唯一风险是「服务停机 > 30 天」导致全池 ST 自然过期，需重新注入——属可接受的极端运维场景，README 注明。
- **Google 主动撤销**：改密/封号/风控会使 ST 立即失效，`st_to_at` 返回 401 → 标 `ST_REVOKED` → 人工重登。这是设计中唯一保留的人工动作，频率极低。
- **长度护栏**：抽取/轮换得到的 ST 必须 `>= 200` 字节，防止把 `undefined`/截断值写库。
- **`st` 列 UNIQUE 约束**：`tokens.st` 为 UNIQUE，轮换只是更新当前账号自己的值，不冲突。

## 7. 明确不做（YAGNI）

- ❌ per-account 浏览器 profile / 把 `persistent_profile_path` 做成按账号（出码仍一个共享登录浏览器统一出，与账号池无关）。
- ❌ 服务器端无头采集器浏览器（ST 既能纯 HTTP 续命，采集器无必要）。
- ❌ 自动化 Google 登录（Google 反自动化，不碰；首次登录保持人工）。
- ❌ 多用户 admin / 每客户端配额（与本目标无关）。

## 8. 测试策略（测试优先）

- **单元**：
  - `extract_session_token` 覆盖 4 种输入格式 + 垃圾输入（抽不到应报错）+ 过短输入（护栏拦截）。
  - `_do_refresh_at` 回写逻辑：`rotated_st` 存在且不同且达标 → 写库；缺失/相同/过短 → 不写；验证在锁内。
- **集成（mock flow_client 的 HTTP 层）**：
  - `st_to_at` 能从伪造的 `Set-Cookie` 响应头解析出 `rotated_st`。
  - `st_keepalive_task` 对闲置 token 触发并完成 ST 回写。
  - `load_balancer.select_token` 跳过 `credits <= min_credits_to_select` 的 token。
- **端到端（真实账号，手动）**：观察某账号 DB 中 `st` 在两次刷新间发生变化、AT 仍有效、生成正常；确认捕获逻辑与上游 2026-06-17 实测行为一致。

## 9. 影响的配置与文档（同 commit 同步）

- **新增配置**（`config/setting.toml` + `config/setting_example.toml` + `src/core/config.py`）：
  - `[token] st_keepalive_enabled`（默认 true）、`st_keepalive_interval_hours`（默认 24）；
  - `[call_logic] min_credits_to_select`（默认 1）；
  - `[admin] st_alert_webhook_url`（默认空）。
- **README.md**：更新「持久化登录 profile / token 管理」章节，说明 ST 自我续命机制、智能粘贴注入、扩展降级为一次性工具、停机 >30 天的注意事项。
- **API 文档**：`POST /api/tokens` 新增 `raw` 字段；`GET /api/tokens` 返回新增健康字段。
- 扩展说明：标注 `Flow2API Token Updater` 角色变更（不再负责保活）。
