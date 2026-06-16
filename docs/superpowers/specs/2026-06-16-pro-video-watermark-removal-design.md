# Pro 视频去水印设计（inverse-alpha un-blend）

- 日期：2026-06-16
- 状态：设计已对齐，待评审
- 作者：yufo + Claude

## 1. 背景与目标

本项目是多账号账号池。个人长期使用单个 **Ultra** 账号，但 Ultra 订阅成本高。目标是恢复账号池本应具备的多账号轮转能力，**用多个 Pro 账号轮转生成**来替代 Ultra。

唯一阻碍：**Pro 账号生成的视频右下角带一个半透明 sparkle（四角星 ✦）水印**，Ultra 没有。需要一个轻量、不牺牲画质（"完全看不出后期处理"）的方式去掉它。

**成功标准**：Pro 生成的视频经处理后，水印在原生分辨率下肉眼完全不可见，且不引入可察觉的画质损耗；Ultra/Free 内容不受影响、零额外开销。

## 2. 关键调研结论（证据）

### 2.1 账号 tier 已知且持久化
- `Token.user_paygate_tier`（`src/core/models.py:29`）落库（`src/core/database.py:391/536/820`），来自上游 `userPaygateTier`（`src/services/token_manager.py:238/519/752`）。
- `PAYGATE_TIER_TWO`=Ultra、`PAYGATE_TIER_ONE`=Pro、`PAYGATE_TIER_NOT_PAID`=Free（`src/core/account_tiers.py`）。
- **结论**：生成内容时其账号 tier 已知，去水印按 tier 路由即可，无需从画面检测水印。

### 2.2 水印物理特性（实测）
| 项 | 结论 |
|---|---|
| 形态 | 半透明白色四角星（Gemini sparkle），alpha 叠加层 |
| 位置 | 固定贴在**距右下角约 120px** 处（横竖屏一致，见下） |
| 大小 | 720 档约 45×55px |
| 动态 | **静止**（整段不动、不闪、不跟随主体） |
| 触发 | **仅视频**；Pro 图片无水印（实测 1 张 t2i 干净）；仅 Pro，Ultra 无 |

横竖屏定位（同为 720/1280 维度）：
- 横屏 1280×720：中心 (1158,600) → 距右 ~122px / 距下 ~120px
- 竖屏 720×1280：中心 (~598,~1161) → 距右 ~122px / 距下 ~119px
- **推论**：水印按"距右下角固定像素偏移"放置，与横竖屏无关 → **同一分辨率档横竖屏共用一套 profile**；分辨率档（default 720 / 1080p）各标一套，offset 与尺寸随分辨率缩放。

### 2.3 un-blend 可行性（标定实验已验证）
模型（逐像素逐通道）：`O = k·B + d`，其中 `k = 1−α`、`d = α·C`（α=透明度蒙版、C=水印色、B=被遮真实背景、O=观测）。还原：`B = (O − d) / k`。

用 4 条平滑纯色背景 Pro 短视频标定（多帧平均降噪 + 环形插值估 B + 逐像素回归）：
- **`min(k)=0.656`，最大不透明度仅 34%，无实心核** → 每个水印像素都可还原，除法噪声放大 ≤1.5×。
- per-channel alpha = 0.280/0.285/0.283（三通道一致）→ "固定叠加层"模型严格成立，标定一次长期可用。
- 留出的"苹果"视频上重建：水印区平均偏差 27 → 6（降到背景噪声地板）；**原生 1× 下水印已基本消失**，仅 4× 像素级抠图残留淡轮廓。
- 残留来源：标定样本仅 4 条 + 硬阈值漏掉软边缘 → **标定精度问题，非原理墙**。

**结论**：un-blend 是该半透明水印的最优解——唯一能还原真实像素的方法（才谈得上无痕，且不挑背景），同时最轻（纯 CPU 逐像素运算，复用 alpha 合成逆运算这一现成算法）。

## 3. 范围

**做**：t2v / r2v / i2v 视频去水印，横竖屏，default 与 1080p 两档分辨率。

**不做**（本轮）：
- 图片去水印（图片无水印）。
- 异步/轮询返回（用同步）。
- 检测上游水印是否变更的自适应机制（先靠固定 profile + 兜底透传）。

## 4. 架构与组件

总体：视频生成完成后、返回客户端前，对 **TIER_ONE(Pro) 视频**插入去水印阶段；**TIER_TWO(Ultra)/Free 维持现状透传**。**同步**处理（阻塞到完成再返回本地 URL）。

### 4.1 标定 profile（离线，一次性，每分辨率档一套）
- 标定脚本 `scripts/calibrate_watermark.py`（一次性运维工具，非运行时）：
  1. 临时只留 Pro 账号 active，生成 N=8–12 条平滑纯色背景短视频（覆盖颜色+明暗），多帧平均。
  2. 环形插值估 B → 逐像素回归 `k`、`d` 图。
  3. 羽化全 alpha（含辉光边缘）；导出 ROI、距角偏移 offset。
  4. 在留出样本上重建、断言残差 < 阈值后落盘。
  5. 始终恢复 Ultra（finally）。
- profile 文件：`src/services/watermark/profiles/{720,1080}.npz`（与去水印模块同属 `watermark` package，见 4.2）
  - 内容：`k`、`d`（float32，shape=ROI×3）、`roi`(w,h)、`offset_from_corner`(dx,dy)、`tier`、`calibrated_at`、`sample_count`、`residual`。
  - 随仓库提交，保证可复现。

### 4.2 去水印模块 `src/services/watermark/` package
组织：`src/services/watermark/__init__.py`、`remover.py`（去水印逻辑）、`profiles/`（标定产物）。沿用项目"按 feature 组织"约定。
- 接口：`async def remove_watermark(local_path: str, width: int, height: int) -> str`（返回处理后本地路径；不可处理时抛特定异常由上层兜底）。
- 流程：选匹配 profile（按 max(w,h) 归档到 720/1080）→ ROI 锚点 `(W−dx, H−dy)` → ffmpeg 解码 rawvideo 管道 → numpy **仅对 ROI** 套 `B=(O−d)/k`（羽化混合避免边界突变）→ 管道喂回 ffmpeg 重编码（x264 **CRF 16–18 视觉无损**，保持 fps/音轨/像素格式）→ 输出本地文件。
- 纯 CPU；ROI ~100×130px，逐帧运算开销可忽略；瓶颈在整段重编码（4–10s 片约数秒）。

### 4.3 接入 `src/services/generation_handler.py` 视频路径
- 视频生成成功、拿到上游 URL 后，已有 `normalized_tier`。
- 当 `normalized_tier == PAYGATE_TIER_ONE` 且为视频：
  下载上游视频 → `watermark_remover.remove_watermark()` → 存 `file_cache` → 返回**本地 URL**（与图片现有本地缓存路径一致）。
- 其余 tier：维持现状透传上游签名 URL。

### 4.4 配置（`config/setting.toml` + 同步 `setting_example.toml`）
```toml
[watermark]
remove_for_pro = true          # 总开关
reencode_crf = 17              # x264 CRF，视觉无损
profiles_dir = "src/services/watermark/profiles"
```
对应 `src/core/config.py` 加属性访问器（带默认值与范围校验）。

## 5. 数据流

```
generate video ──► upstream signed URL
        │
        ├─ tier==Ultra/Free ─────────────────────► 透传 URL（不变）
        │
        └─ tier==Pro(TIER_ONE) ─► download ─► watermark_remover
                                              （选profile→ROI→un-blend→CRF17重编码）
                                   ─► file_cache ─► 本地 URL ─► 返回
```

## 6. 错误处理与兜底（铁律：去水印绝不能让生成失败）

- 无匹配 profile（未知分辨率）→ 记 warning + **回退透传上游 URL**。
- ffmpeg/处理异常 → 记 error + **回退透传上游 URL**。
- 下载失败 → 沿用现有上游 URL 失败处理逻辑。
- `remove_for_pro=false` → 全部透传（等同关闭特性）。

## 7. 测试策略

- **un-blend 数学单测**：合成已知 sparkle（已知 α、C）叠到多种已知背景 → 还原 → 断言逐像素误差 < 阈值；含 min(k) 边界、噪声放大上界。
- **profile 加载 / ROI 定位单测**：横屏与竖屏锚点 `(W−dx,H−dy)` 计算正确；分辨率归档正确。
- **tier 路由测试**：Pro→处理、Ultra/Free→透传（mock generation_handler）。
- **集成测试**：用已抓 `tests/fixtures/pro_sample.mp4` 跑全流程，断言水印区残差 < 阈值、输出可解码、时长/fps/音轨不变。
- **兜底测试**：无 profile / 损坏视频 → 确认回退透传、请求不失败。

## 8. 风险与未决

- **标定漂移**：上游若改水印样式/位置/分辨率，固定 profile 失效。缓解：兜底透传不致崩；后续可加轻量自检（在已知 ROI 测残差，超阈值告警提示重标定）。
- **1080p offset 待标定确认**：1080 档的 offset 与水印尺寸需在实现期实标（推测按比例 ~1.5×，不假设）。
- **重编码代次损失**：CRF 17 对整帧引入极小损耗（用户已确认接受"视觉无损"）。
- **同步延迟**：每条 Pro 视频增加下载+重编码数秒（用户已确认同步等待）。
- **额度/时间成本**：标定与测试需生成若干 Pro 视频（一次性）。

## 9. 文档同步

- 新增 `[watermark]` 配置 → 更新 `setting_example.toml` 与 README 配置说明。
- 新增 `scripts/calibrate_watermark.py` 命令 → README 命令参考补充标定步骤。
- tier 路由行为变更 → README/架构说明注明 Pro 视频走本地处理、Ultra 透传。
