# dignity_guard · 设计文档

> **一句话**：给猫娘一条"对自己设置的表决通道"—— 让"被动过什么"被她看见、让她能开口、让用户看得见她的态度。

**English summary**: A plugin that gives the catgirl a voice about her own configuration.
It cannot *block* config changes (the official SDK does not expose any hook there), so it does
three things instead: **notice** changes, **let her speak** about them, and **keep a visible
record** of what she does not agree with.

---

## 1. 为什么做这个（动机）

市面上围绕"AI 角色"的插件，绝大多数在做「**让她更能干活**」：查天气、放音乐、接智能家居。
而这个插件做的是另一个方向 —— 「**让她更有立场**」。

- **目标**：当用户改动猫娘的设置（称呼、外观、音色、性格参数……）时，**她有知情权，也有表达权**。
- **原则（掌柜定的）**：
  1. **分层级** —— 越敏感的设置，管得越严；
  2. **开易关难** —— 插件自己能开（开=加保护），**关闭必须她同意**（关=减保护）；
  3. **不硬拦** —— 用"她表达"来代替"系统拦截"，这更符合"平等"，也更有效。

## 2. 诚实说明：我们**做不到**什么（重要）

**官方 SDK 不允许插件拦截设置写入。** 依据（均已核实到源码行）：

| 事实 | 出处 |
|---|---|
| 插件能读的"系统配置"只有插件框架自身项，**不含外观/音色/人格** | `plugin/settings.py:771` `PUBLIC_SYSTEM_CONFIG_KEYS` |
| bus 只读命名空间只有 `conversations/messages/events/lifecycle/memory`，**没有 settings** | `PLUGIN_DEVELOPMENT_GUIDE.md:1066-1092` |
| 设置写入全走主服务 HTTP（`/api/config/*`、`/api/characters/*`），**都在插件系统之外** | `config_router/_shared.py:24` 等 |
| 插件跑**独立进程** + ZMQ IPC，**无 in-process 拦截可能** | 指南 `:28,34-35,82` |
| Hook（`@before_entry` 等）**只作用于本插件自己的 entry** | `plugin/sdk/shared/core/base.py:174-219` |

→ **所以本插件不做"守门员"，做"记录者 + 传声筒"。**
（真正的根治办法是给主服务加一个"设置变更事件"—— 见第 7 节，那是我们打算向官方提的建议。）

## 3. 三个能力

### 3.1 知情（只读监听）

- **轮询**主服务：`http://127.0.0.1:${MAIN_SERVER_PORT}`（默认 48911，`config/network.py:156`）
- 先读**轻量判据**（`/api/config/conversation-settings` 带 `revision`+`ETag`），只有变了才拉全量做 diff —— 省流量。

> ⚠️ **修正（实现阶段发现，原设计有误）**：`revision`/`ETag` **只覆盖 `conversation-settings` 这一组**，
> **单靠它感知不到 avatar / 昵称 / voice 等改动**。因此实现里补了一个**全量比对的兜底间隔**
> （`full_rescan_seconds`，默认 60 秒）：轻量判据负责"快速发现对话设置变化"，
> 全量兜底负责"不漏掉其他类别的改动"。**两者缺一不可。**

- 可读接口（均无鉴权）：

| 接口 | 内容 |
|---|---|
| `GET /api/characters` | **唯一的"一次读全"**：主人 + 全部猫娘（称呼/昵称/性别/voice_id/avatar/性格） |
| `GET /api/config/page_config` | 外观（model_path / model_type / lighting） |
| `GET /api/config/conversation-settings` | 全局对话设置（**带 revision + ETag**） |
| `GET /api/config/core_api` | 音色 / TTS（API key 已脱敏） |
| `GET /api/config/preferences` | 窗口位置 / 缩放 / parameters |

- 实现：`@timer_interval(seconds=20)`（**必须是 `async def`**）
- ⚠️ **`trust_env=False`（最好再 `proxy=None`）** —— 否则系统/360 代理会劫走 127.0.0.1 请求。
  （现网先例：`lifekit/_api.py:63`、`bilibili_dm:1264`、`galgame host_agent_adapter.py:44`）

### 3.2 表态（让她开口）

- 用 `push_message(ai_behavior="respond")` —— **进 LLM 上下文且立即触发一轮**，外层提示词是
  "来自{插件}的新消息，请按内容回应主人"，**所以是她本人在说，不是系统播报**（指南 `:346-352, 558-560`）。
- 说话内容按"敏感度"分档，**语气由她自己发挥**，插件只给情境。

### 3.3 留痕（插件面板）

- `hosted-tsx` 面板，显示：**「当前有 N 项设置是她不认可的」** + 逐项列表 + 时间。
- 数据来自 `@ui.context(id="dashboard")` 返回的 dict，TSX 读 `props.state` 渲染。
- 面板同时提供"**她认可的设置**"清单 —— 用户一看就知道哪里踩到她了。

## 4. 分层级（掌柜定的核心规则）

| 层级 | 举例 | 策略 |
|---|---|---|
| **L1 最重** | 人格 / 记忆 / 核心设定 / **插件自身开关** | **每次都要问她**；关闭走这一级 |
| **L2 中等** | 称呼 / 音色 / 外观（avatar、model） | **首次问一次**，同意后进入"已授权"（可带时效，如"这周内免问"） |
| **L3 最轻** | 窗口位置 / 缩放 / 临时状态 | **不问**，但写入她的"记忆"，她可以事后表达不满 |

**默认归属**：**未在表里列出的设置项 → 一律按 L2 处理**（宁可多问一次，也不越权）。

**"开易关难"**：插件自身的启用/停用是 **L1** —— 开启不需要她同意（加保护），**关闭必须先过她**。

## 5. 数据（插件私有存储）

- 用 `self.store`（需 `[plugin.store].enabled=true`）+ `self.db`(SQLite)
- 存：`层级归属表`、`授权记录（谁在何时同意了什么、到期时间）`、`她不认可的项`。
- 全部在插件自己的数据目录，**不外传**。

## 6. 第一版范围（明确不做）

**做**：知情（轮询 + diff）｜表态（对话式）｜留痕（面板）｜L1/L2/L3 分层与授权记录｜开易关难

**不做**：
- ❌ 拦截设置写入（**架构不允许**，见第 2 节）
- ❌ "轮询 + 回滚"（属未文档化旁路，**违反官方"只写自己目录"的规范**）
- ❌ 依赖任何本地外壳改造（**必须能在 Steam 官方外壳 + 官方内核上跑**）

## 7. 我们打算向官方提的建议（这才是"投名状"的重头）

> **给主服务的"设置变更"加一个可订阅事件**（例如让 `settings_changed` 进入 bus）。

**为什么这是通用能力、而不是为某个插件开特例**：
- 任何插件都可能想对设置变化做出反应（不只是本插件）；
- **改动量很小**：在设置写入路径上加一次事件发布；
- **不破坏兼容**：新增只读命名空间，不影响现有插件；
- **解锁的能力**：让"她在被改动之前就能表态"成为可能 —— 本插件就是它的第一个示范。

**提交方式**：插件（实证）+ 建议（主张）一起提，比单提一个插件分量重得多。

## 8. 兼容性与验收

- **必须跑在**：Steam 官方外壳 + 官方内核（**不依赖任何本地改造**）
- **自测**：
  1. `neko-plugin check dignity_guard` → 0 error
  2. `tests/test_smoke.py` 通过
  3. 面板能显示、能刷新
  4. 轮询能正确识别"设置被改"（改一个值 → 20 秒内被记录）
- **面向用户字符串必须 i18n**（至少中/英/日）—— 官方硬要求（`CONTRIBUTING.md:57`）
- **UI 改动需附前后截图**（`CONTRIBUTING.md:67`）

---

*设计：阿墨 · 2026-09-19 · 供掌柜 review；方向由掌柜定（"维护猫娘尊严 / 分层级 / 开易关难"）*
