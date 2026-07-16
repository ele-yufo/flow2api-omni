# P1 config 迁入 shared 地基 Implementation Plan（已执行）

**Goal:** 建立 `src/shared/` 地基,把 config provider 迁入 `src/shared/config/`,消除"隐式全局叶子模块",并加二期多租户构造接缝。零行为变更。

## 关键约束发现（决定范围）

`config` 是**单例 + 运行时可变**:`database.reload_config_to_memory` 用 30 个 `set_*` 把 DB 值回灌进内存单例;admin 改配置后 reload。所有消费者 `from ..core.config import config` 共享同一实例——这是 admin 运行时改配置能生效的命脉。**故不能改成不可变**;去全局化=把隐式全局换成显式可注入的**同一共享 provider**。

## 本次范围（安全 + 有真实价值）

- **做**:建 `src/shared/` 包;config provider 迁 `src/shared/config/provider.py`(类名保留 `Config` + `Settings` 别名);路径解析改**位置无关**(向上找 `config/setting.toml`);加可选 `config_path` 参数（二期按租户构造独立配置实例的接缝）;`core/config.py` 变 9 行 re-export shim。
- **YAGNI 延后到二期**:19 处 `import config` 改注入式——二期多租户接线时才需要,现在做高风险低收益。单例仍是全局共享实例,可变性不变。

## 验证门（已过）

- `test_config_clamp` golden 严格通过（不 REGEN）。
- 全量 112 绿。
- 三条 import 路径(`core.config` / `shared.config` / `shared.config.provider`)的 `config` 是**同一实例**（可变性保持）。
- 所有消费 config 的模块(含 `src.main` 整个 app 装配)import 正常。

## 文件

- Create: `src/shared/__init__.py`, `src/shared/config/__init__.py`, `src/shared/config/provider.py`（从 config.py 迁移 + 补丁）
- Modify: `src/core/config.py` → 9 行 re-export shim
