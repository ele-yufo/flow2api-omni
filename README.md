# flowproxy

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Docker](https://img.shields.io/badge/docker-supported-blue.svg)](https://www.docker.com/)

**flowproxy** — Google Flow（flow.google.com）媒体生成的反向代理，将其无缝封装成 OpenAI 兼容与 Gemini 官方双协议的本地 API 服务。

---

## 项目定位与价值

在处理大规模、自动化的高阶多媒体生成任务时，Google Flow 提供了极高水准的底层生成模型。
然而，其原生 Web 界面设计天然阻碍了工程化集成与高并发调用。
flowproxy 旨在抹平这层网页交互壁垒。
通过接管底层的会话刷新与身份校验验证流，flowproxy 为开发者提供了一个高可用的 API 端点。
使得下游的自动化引擎、对话框架或自有应用，能够像调用本地原生接口一样，透明、顺滑地调度 Google 最前沿的生成式媒体 AI 模型。

## 核心特性总览

本项目在架构与实现层面上，围绕稳定性、多协议适配以及性能调度实现了多项核心能力：

1. **零成本图片生成引擎**：
   底层对接 Nano Banana 2.1（`gemini-3.2-flash-image` 族）。
   完整提供 5 种画幅比例（横向、竖向、方形、4:3、3:4）与 3 种分辨率（1K、2K、4K）的组合，共计 15 个模型变体。
   支持文生图（Text-to-Image）与参考图改图（Image-to-Image），**且所有生成请求均不消耗账号额度**。
2. **多维细粒度视频生成管线**：
   底层对接 Gemini Omni 1.1 Flash（`gemini_omni_*` 族）。
   涵盖文生视频、多图参考（单次最多支持 7 张）、基于首帧或首尾双帧驱动。
   提供 4 档精准时长（4秒、6秒、8秒、10秒）、横竖屏物理方向，以及 1080P/4K 画质选项，共计 64 个变体。
   额外扩展 `gemini_omni_edit` 端点，支持视频延长与可链式编辑。
   计费侧：1080P 及上采样完全免费；4K 上采样每次消耗 50 额度，并在路由侧强制限定仅 Ultra 账号可用。
3. **高可用多账号池与智能调度**：
   实现大规模 Google 账号资源池化管理。
   内置基于 credits（可用额度）感知的负载均衡器，账号额度触及低水位自动退出轮询队列。
   支持按账号等级实现路由隔离（优先级：Ultra > Pro > Free）。
   配额耗尽节点自动摘除，并在下一个计费周期或外部状态更新后实现自愈恢复。
4. **有头浏览器级自动持久保活**：
   为池中每个账号分配并独立持久化专属 Chrome profile。
   有头浏览器定期刷新会话、执行身份校验，并将合法凭证原子写回数据库。
   数据底层由 `token_lifecycle` 表对节点状态进行精准流转管理（涵盖 `persistent` 与 `warm` 状态）。
   针对检测到的会员过期异常（触发 `membership_expired` 事件），系统立即将其摘出路由池，等待续费后自愈重入队列。
5. **无感 reCAPTCHA 验证码穿透**：
   复用 profile 内的持久化登录态 cookie 提交人机验证。
   将传统匿名态下高达 30% 以上的 API 拒绝率，压降至个位数。
6. **双协议无缝统一接入**：
   暴露标准的 OpenAI 规范端点 `/v1/chat/completions`。
   原生兼容 Gemini 官方规范端点 `:generateContent` 与 `:streamGenerateContent`。
   在官方协议实现上，完整且深度支持 `systemInstruction`、`inlineData` 及 `fileData` 参数。
7. **可观测性与健康守护**：
   集成 Discord webhook 实时告警机制（涵盖账号失效、可用池规模告急、系统额度耗尽等风险场景）。
   配置全局每小时触发的健康巡检定时守护进程（timer）。
   提供配套的轻量级 Web 管理界面与前端模型测试沙箱。

## 快速开始

本项目提供多种环境下的部署方式，推荐使用以下两种方案。

### 方式一：Docker 部署（推荐）

容器化方案是部署该服务最快捷、隔离性最好的方式。仓库根目录已内置对应的 Dockerfile。

```bash
# 1. 克隆项目仓库到本地
git clone https://github.com/ele-yufo/flowproxy.git
cd flowproxy

# 2. 复制并初始化配置文件
cp config/setting_example.toml config/setting.toml

# 3. 按需修改 setting.toml 配置后，一键构建并启动
docker compose up -d --build
```

### 方式二：Python 裸机部署

对于需要进行原生二次开发或部署在特定宿主机环境下的用户，请准备 Python 3.11+ 的运行环境。

```bash
# 1. 克隆项目仓库
git clone https://github.com/ele-yufo/flowproxy.git
cd flowproxy

# 2. 创建并激活独立的虚拟环境
python3.11 -m venv venv
source venv/bin/activate

# 3. 安装依赖与预备环境
pip install -r requirements.txt

# 4. 初始化配置参数
cp config/setting_example.toml config/setting.toml

# 5. 启动主进程
python main.py
```

## 配置说明

服务的中枢行为由 `config/setting.toml` 决定。请重点核对与调整以下关键配置段：

*   **`[global]`**：API 访问密钥与管理后台账号——部署后第一时间改成强随机值。
*   **`[server]`**：监听地址与端口（默认 `18282`）、CORS 白名单（Chrome 扩展入口需加精确 Origin）。
*   **`[call_logic]`**：调度核心——负载均衡策略、账号层级优先（Ult > Pro > Free）、低额度熔断阈值。
*   **`[keepalive]`**：浏览器保活——profile 目录、刷新周期、`XRDP`/Xvfb 显示器绑定。
*   **`[captcha]`**：打码模式（推荐 `personal` 持久化登录态）与 profile 路径。

## 模型目录与计费详情

以下表格枚举了内部映射的两大核心模型家族、代表性变体示例及其平台级资源消耗说明。

| 模型家族 | 代表变体示例 | 功能说明 | 额度消耗 |
| :--- | :--- | :--- | :--- |
| `gemini-3.2-flash-image` | `gemini-3.2-flash-image-square`<br>`gemini-3.2-flash-image-landscape-4k` | Nano Banana 2.1 引擎图像生成。<br>支持 15 个物理变体组合。 | **0** |
| `gemini_omni_*` | `gemini_omni_t2v_4s_1080p`<br>`gemini_omni_r2v_portrait_10s_4k` | Omni 1.1 Flash 引擎视频生成。<br>提供高达 64 种细分控制组合变体。 | 原生生成 7-15 额度（按时长）<br>1080P 上采样 +0；4K 上采样 +50（限 Ultra） |
| `gemini_omni_edit` | `gemini_omni_edit` | 高阶视频扩展操作。<br>支持针对已有视频特征延长生成与编辑。 | 固定 20 额度/次 |

## API 使用示例

服务默认监听 `http://localhost:18282`，API 请求需携带与配置文件一致的密钥（下文以环境变量 `$FLOWPROXY_KEY` 为例）。

### 1. 图片生成（OpenAI 兼容协议）

调用标准 `/v1/chat/completions` 请求 4K 宽幅高质量图像。

```bash
curl -X POST http://localhost:18282/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $FLOWPROXY_KEY" \
  -d '{
    "model": "gemini-3.2-flash-image-landscape-4k",
    "messages": [
      {
        "role": "user",
        "content": "A highly detailed macro shot of a cybernetic beetle resting on a glowing neon leaf, 8k resolution, cinematic lighting."
      }
    ]
  }'
```

### 2. 视频生成（OpenAI 兼容协议，附带多图参考）

基于 `/v1/chat/completions` 并通过多模态数组阵列传递文本 Prompt 与参考图像，驱动 6 秒场景生成。

```bash
curl -X POST http://localhost:18282/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $FLOWPROXY_KEY" \
  -d '{
    "model": "gemini_omni_t2v_6s_1080p",
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "text", "text": "Smooth aerial drone shot transitioning across the landscape provided, from dawn to midday lighting."},
          {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQ..."}}
        ]
      }
    ]
  }'
```

### 3. 标准请求（Gemini 官方协议）

使用官方规范承载请求，实现对现有 Google SDK 调用架构的无感替换。

```bash
curl -X POST "http://localhost:18282/v1beta/models/gemini-3.2-flash-image-square-2k:generateContent" \
  -H "Content-Type: application/json" \
  -H "x-goog-api-key: $FLOWPROXY_KEY" \
  -d '{
    "contents": [
      {
        "parts": [{"text": "A futuristic city skyline enveloped in thick rain and volumetric fog, concept art style."}]
      }
    ]
  }'
```

## 账号池与保活工作原理

flowproxy 强韧的可用性底座在于其围绕数据库状态机构建的闭环生命周期管理系统。
池内每个账号的生命周期由 `token_lifecycle` 表独立管理，与业务启停解耦。
`persistent` 模式下浏览器常驻后台，按短周期持续刷新会话，适合主力账号；
`warm` 模式则到期才拉起浏览器、刷完即关，适合低频备用账号。
两种模式可按账号混用，改动写库即生效，无需重启服务。

为了从根本上解决会话凭空失效与风控登出问题，底层保活引擎为池内每一组账号强制分配并挂载了独立的持久化 Chrome profile 目录。
后台调度器定期拉起有头浏览器在后台静默刷新通信管道、进行二次校验，并原子级地写回凭证。
当系统与底层通信时触发 reCAPTCHA 挑战防线，保活模块不再试图绕开验证逻辑，而是直接重用 profile 中的已登录态合法 cookie，这一举措从根本上将常规代理池动辄 30% 以上的身份校验拒绝率拦截并压缩至个位数边缘。
此外，针对可能触发配额封锁的特殊 `membership_expired` 事件响应，引擎将在立即将受影响账号摘出可用池；在侦测到目标账号的会员权限续费充值到账后，状态机将执行闭环自愈重启，节点被重新释放回生产池。

## 高级运维

针对企业级长时间无人值守运行，本项目设计了完善的命令行工具集与守护架构方案：

*   **进程级持久化运行**：
    生产环境下拒绝脆弱的临时会话运行方式，强烈推荐将其托付给 systemd 守护体系。
    代码仓库已随包内置了标准的 `flowproxy-keepalive.service` 配置文件模板，按需校准路径后即可接入。
*   **命令行数据管理**：
    内置高级 CLI 指令集 `tokens.py`。
    支持对复杂池态数据的自动化干预，包含大批量凭证文件的重载验证以及死锁记录的强制修剪。
*   **多层级定时器与健康度调参**：
    系统外围集成每小时触发级别的整体资源巡检。
    在最底层的有头浏览器进程生命周期约束上，通过配置文件暴露了特定的数字时间刻度常量：例如借助 `1200` 用于短间隔心跳与活性快速探测探测机制，以及借助 `43200` 承载半日长周期内必须完成的强行深度刷新任务，实现了时间切片的精细把控。
*   **显示器环境的构建与依赖挂载**：
    由于登录态打码依赖完整的浏览器渲染环境，宿主机服务器通常需要挂载一个底层的虚拟帧缓冲器。
    我们指定并推荐的基础架构为：启动 Xvfb 服务并持久挂载于系统级 `:10` 显示器供服务层渲染使用。
    同时，出于针对风控黑盒的复杂排查与异常介入考虑，推荐在同机器额外启动并配置 XRDP 服务，将其绑定并暴露于 `:11` 显示器，借此建立外部管理人员可视化介入与人工救援的调试安全通道。
    有关该环节的完整资源包与配置步骤指引，请必须仔细阅览仓库内的 `browser-keepalive.md` 文档。

## 开发与测试

为了确保请求代理与模型特征调换逻辑的严格性，项目使用 pytest 作为主要的测试检验环境，覆盖面涵盖鉴权机制、参数映射与多模态负载拆解。
针对云端不可控且呈现显著非确定性输出的 AI 媒体数据生成模型本身，本测试套件大规模引入了 golden 特征化测试。
不再机械要求每次生成的响应保持纯字节层的一一对应，而是抽取请求链中的架构标记特征与格式节点元数据并固化为基准配置；一旦测试请求产生的结构及关键帧节点匹配黄金验证，即可判定状态通过，避免了误判。

## 致谢

本项目在核心架构的初期设计上，派生自开发者 TheSmallHanCat 的优秀开源作品 flow2api（基于 MIT 许可证发行，上游目前已停止功能维护）。
最初实现对底层 Google Flow 请求逆向工程破译的调用实现、reCAPTCHA 环境的高效对抗与处理逻辑框架，以及项目附带的美观精简的 Web 管理交互界面，均来自该项目。
同样值得一提的是，在测试验证阶段所仰赖的一键式账户凭证自动化提取 Chrome 浏览器插件（Flow2API-Token-Updater）亦出自上游作者之手。
在此，对原作者为开源社区奉献的技术灵感与代码资产致以感谢。

## 许可证

本项目的所有程序代码及说明材料依据并遵从 [MIT License](https://opensource.org/licenses/MIT) 开源许可协议发布。
