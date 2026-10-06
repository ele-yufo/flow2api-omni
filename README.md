# flowproxy

把 [Google Flow](https://flow.google.com) 的图片和视频生成，变成你自己的 API。

- **OpenAI 兼容**：`/v1/chat/completions`，任何支持自定义 OpenAI base URL 的工具都能直接用
- **Gemini 官方格式**：`:generateContent` / `:streamGenerateContent`，可平替 Google SDK
- **图片**：Nano Banana 2.1，15 个模型变体（5 种画幅 × 1K/2K/4K），不消耗账号额度
- **视频**：Gemini Omni 1.1 Flash，64 个变体（文生/多图参考/首帧/首尾帧 × 4 档时长 × 横竖屏 × 1080P/4K），外加视频延长与编辑
- **多账号池**：额度感知负载均衡、按账号等级路由、浏览器自动保活、登录态打码

## 运行

需要 Docker，或者 Python 3.11+。

**Docker：**

```bash
git clone https://github.com/ele-yufo/flowproxy.git
cd flowproxy
cp config/setting_example.toml config/setting.toml
# 编辑 setting.toml：改 api_key、admin 账号密码
docker compose up -d --build
```

**Python：**

```bash
git clone https://github.com/ele-yufo/flowproxy.git
cd flowproxy
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config/setting_example.toml config/setting.toml
python main.py
```

服务默认监听 `http://localhost:18282`。

## 三分钟跑通

```bash
# 文生图（免费）
curl http://localhost:18282/v1/chat/completions \
  -H "Authorization: Bearer $FLOWPROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.2-flash-image-landscape",
    "messages": [{"role": "user", "content": "一只橘猫坐在窗台上，午后阳光"}]
  }'
```

返回的 message 里是 `![Generated Image](图片URL)`。

```bash
# 文生视频（1080P，4 秒）
curl http://localhost:18282/v1/chat/completions \
  -H "Authorization: Bearer $FLOWPROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini_omni_t2v_4s_1080p",
    "messages": [{"role": "user", "content": "热气球缓缓升过晨雾中的群山"}]
  }'

# Gemini 官方格式
curl "http://localhost:18282/v1beta/models/gemini-3.2-flash-image-square:generateContent" \
  -H "x-goog-api-key: $FLOWPROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [{"parts": [{"text": "一颗木桌上的红苹果，极简背景"}]}]
  }'
```

## 模型目录

`GET /v1/models` 返回全部 80 个模型。

**图片 · Nano Banana 2.1（`gemini-3.2-flash-image-*`）**

| 画幅 | 1K | 2K | 4K |
|---|---|---|---|
| landscape / portrait / square / four-three / three-four | ✓ | ✓ | ✓（仅 Ultra 账号） |

支持文生图和参考图改图（消息里带 `image_url`）。全部 0 额度。

**视频 · Gemini Omni 1.1 Flash（`gemini_omni_*`）**

| 类型 | 命名 | 输入 |
|---|---|---|
| 文生视频 | `gemini_omni_t2v_{4,6,8,10}s_{1080p,4k}` | 纯文本 |
| 多图参考 | `gemini_omni_r2v_{...}` | 最多 7 张参考图 |
| 首帧 | `gemini_omni_i2v_{...}` | 恰好 1 张图 |
| 首尾帧 | `gemini_omni_fl_{...}` | 恰好 2 张图（按顺序） |
| 延长/编辑 | `gemini_omni_edit` | 1 条源视频引用 + 指令 |

竖屏在中间加 `_portrait`，如 `gemini_omni_t2v_portrait_8s_1080p`。

计费：原生生成 7-15 额度（按时长）；1080P 是 720P 原生生成的免费上采样，4K 上采样 +50 额度（仅 Ultra 账号）；视频延长固定 20 额度。

## 配置

`config/setting.toml`（从 `setting_example.toml` 复制后编辑）：

| 段 | 管什么 |
|---|---|
| `[global]` | `api_key`（调用密钥）、管理后台账号密码 |
| `[server]` | 监听端口（默认 18282）、CORS 白名单 |
| `[call_logic]` | 调度：账号层级优先、低额度熔断阈值 |
| `[captcha]` | 打码模式（推荐 `personal` 持久化登录态） |
| `[keepalive]` | 浏览器保活：profile 目录、刷新周期 |

## 账号池

导入多个 Google 账号后，flowproxy 自动维护它们：

- **负载均衡**：按剩余额度（credits）调度，额度触底的账号自动退出轮询，回涨后自动恢复
- **等级路由**：Ult > Pro > Free；4K 模型只走 Ultra 账号
- **浏览器保活**：每个账号一个独立的持久化 Chrome profile，后台浏览器定期刷新会话并原子写回数据库——这是账号不掉线的根本保障。账号生命周期由 `token_lifecycle` 表管理，`persistent` 模式常驻刷新（主力账号，周期 1200 秒），`warm` 模式到期才拉起（备用账号，退休期 43200 秒）；会员过期（`membership_expired`）自动摘池，续费后自动恢复
- **登录态打码**：reCAPTCHA 用 profile 里的登录态 cookie 提交，拒绝率从匿名态的 30%+ 降到个位数

保活依赖虚拟显示器（Xvfb `:10`）和人工介入通道（XRDP `:11`）。账号导入、健康检查、静默重授权都走 `scripts/tokens.py` CLI。完整文档见 [`docs/operations/browser-keepalive.md`](docs/operations/browser-keepalive.md)。

## 告警

配置 Discord webhook 后，账号失效、可用池告急、额度耗尽会自动推送。另有每小时一次的健康巡检。

## 开发

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

测试以 golden 特征化为主（锁定行为基线，`REGEN_GOLDEN=1` 重新捕获）。提交前全量跑一遍。

## 致谢

本项目派生自 [TheSmallHanCat/flow2api](https://github.com/TheSmallHanCat/flow2api)（MIT License）：Flow 的请求逆向、验证码处理框架和 Web 管理界面来自上游，配套的 Flow2API-Token-Updater Chrome 扩展同样出自上游作者。

## License

[MIT](LICENSE)
