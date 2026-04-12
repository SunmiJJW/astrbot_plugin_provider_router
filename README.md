#  Provider Router

[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-blueviolet?style=flat-square)](https://github.com/AstrBotDevs/AstrBot)

一个给 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 用的双路由分流插件。

它打破了单一模型进行对话的局限，将调度路线抽象为两个独立通道：

- `primary`：主路由，通常用于处理工具、技术、联网分析、截图解析等严谨任务
- `secondary`：副路由，通常用来承接日常闲聊、情感陪伴、寒暄及轻量级追问

你可以把它们分别指向任意 provider，只要 AstrBot 里已经配好了对应的 provider ID。

## ✨ 功能特色

### 🔀 双路由分流

- 支持 `primary / secondary` 两条路由
- 直接改写 `selected_provider`

### ⚡ 规则优先

- 支持关键词分流
- 支持媒体优先主路由
- 支持链接优先主路由
- 支持跳过命令样式消息

### 🧠 可选二次判别

- 支持 `rules_only`
- 支持 `rules_then_llm`
- 分类模型只需返回 `primary`、`secondary` 或 `keep_default`

### 🎯 显式强制与黏性路由

- 支持 `force_primary_regex`
- 支持 `force_secondary_regex`
- 命中后可短期记住同一会话的选择
- 群聊黏性按“同一会话 + 同一发送者”生效，不会串人

### 🎭 Persona Override (人格覆写)

- 可分别配置 `primary_persona_id` 与 `secondary_persona_id`
- 仅替换 system prompt 里的 `Persona Instructions` 段
- 不会在这里整套重建人格对象，只是替换发给指定模型的人格提示词

### 🧼 回复标签+上下文清洗

- **来源展示**：支持在回复开头显示当前命中路由的 `『标签』`（如『GPT』）；通常这会对应到某个具体 provider。
- **自动清洗**：每次请求时会尽量清洗这些标签和路由控制词，确保它们**只用来“给人看”**，而不会留在上下文里长期污染后续轮次。

## 🛡️ 插件边界

本插件运行在 `on_waiting_llm_request` 阶段，主要负责在 LLM 请求发出前改写路由目标。

| ✅ 它会做的事 | ❌ 它不会做的事 |
| :--- | :--- |
| **判断**这条消息更适合走哪条路由 | **主动唤醒**本来就不会进入 LLM 的消息 |
| 视情况**改写** `selected_provider` | 自己去**重建**整条聊天上下文历史 |
| 按配置**替换**对应的人格提示段 | **接管** AstrBot 以外的完整会话管理 |
| 尽量**清洗**当前请求里的显示控制词和回复标签 | **二次清理** 已被其他插件固化成历史的脏数据 |

> [!TIP]
> 如果你同时装了会重构 `req.contexts` / `req.prompt` 的其他插件，本插件只保证“当前请求阶段”的清洗；后续历史上下文是否还会再次出现，取决于上游或下游插件自己的实现。

## 🚀 快速开始

### 1. 先配置两条 provider

最小必填项只有这两个：

```yaml
primary_provider_id: 你的强模型或多模态 provider
secondary_provider_id: 你的轻量闲聊 provider
```

### 2. 推荐先从这套起步

```yaml
classifier_mode: rules_only
uncertain_route: keep_default
prefix_reply_with_route_label: false
sticky_override_enabled: true
sticky_override_rounds: 3
sticky_override_ttl_seconds: 600
sticky_release_on_opposite_signal: true
force_primary_regex: "^(?:优先主路由|走主线)\\s*"
force_secondary_regex: "^(?:优先副路由|走副线)\\s*"
```

### 3. 然后做四条自测

**试试向 Bot 发送以下四句话**：

1. 👉 `优先主路由 帮我分析这个报错` （**预期**：命中正则，偏向主路由）
2. 👉 `优先副路由 陪我聊聊` （**预期**：命中正则，偏向副路由）
3. 👉 `给我解释一下这个插件配置` （**预期**：知识类倾向，通常走主路由）
4. 👉 `谢谢，晚安` （**预期**：情感类倾向，通常走副路由）

## 💡 配置建议

### 🅰️ 方案 A：高低算力灵活搭配
> 将主路由分配给强逻辑模型，副路由分配给轻快实惠模型。

- `primary_provider_id`：`openai/gpt-5.4` （或其他旗舰级模型）
- `secondary_provider_id`：`google_gemini/gemini-3.1-flash-lite-preview`
- **👉 适用场景**：技术解答、逻辑分析、编写代码等重负载任务走强模型；日常闲聊、情感陪聊等零碎互动走轻巧模型。

### 🅱️ 方案 B：图文多模态划分
> 将主路由留给可以看图、看文件的多模态容器，副路由仅作普通文字对话。

- `primary_provider_id`：你手头最稳的**多模态** provider
- `secondary_provider_id`：你最便宜的**纯文字** provider
- **👉 适用场景**：带图片、截图分享或长文件阅读的复杂任务交由主路由处理；纯文本聊天的轻微对话由副路由快速响应。

---

## ⚙️ 核心配置说明

### 💬 1. 关键词分流机制
- `primary_route_keywords`：如 `工具, 技术, 排障, 报错` 等重度任务信号。
- `secondary_route_keywords`：如 `闲聊, 寒暄, 陪聊, 晚安` 等轻互动信号。

> [!NOTE]
> 这套关键词仅仅是“**优先分流倾向**”判别，并不会生硬地强制阻断其他意图。

### 🕹️ 2. 显式强制指令
为了防止误判，推荐设定极为清晰的“口令前缀”：

```yaml
force_primary_regex: "^(?:优先主路由|走主线)\\s*"
force_secondary_regex: "^(?:优先副路由|走副线)\\s*"
```

当用户发送 `优先主路由 帮我看下这个库为何报错` 时，插件会：
- ⚡ **立即分流** 到指定的模型通道。
- 🧹 **自动清洗** 会自动把诸如“优先主路由”这部分修饰词从请求里清理掉，保证不干扰大模型理解原本上下文。

### 🧲 3. 自适应黏性记忆 (Sticky Route)
在命中了“强制指令”后，可以用记忆功能短期锁定选择：

- `sticky_override_enabled`：总开关
- `sticky_override_rounds`：让状态能够保持几个对话轮回
- `sticky_override_ttl_seconds`：状态能维持多少秒
- `sticky_release_on_opposite_signal`：当用户忽然喊出反方向指令时，切换至另一路由

> **作用域安全**：
> - 👤 私聊：按“当前会话”锁定。
> - 👥 群聊：严格按照“当前会话”+“这个说话的人”单独锁定，**群友之间的调优指令互不干扰**。

### 🏷️ 4. 前缀标签与清洗透传
如果希望用户能感知当前这轮大致命中了哪条路由：

```yaml
prefix_reply_with_route_label: true
primary_reply_prefix_label: "GPT"
secondary_reply_prefix_label: "Gemini"
```

当回复被下发前，会在文字最前面追加 `『GPT』`。通常这会对应你配置给主路由的模型或 provider。此外，考虑到上下文堆叠，**插件会竭力清洗自己生成的印记**，确保模型不会傻乎乎学着加标签或者被标签内容带歪。

### 🤖 5. 引入 LLM 分类裁判
对于规则无法完全覆盖的复杂对话场景，可以尝试引入大模型进行二次意图判别：

```yaml
classifier_mode: rules_then_llm            # 开启基于大模型判别的模式
classifier_provider_id: "openai/gpt-5.4-nano" # 指定专门负责意图分类裁判的微型模型
```

裁判模型极度轻量，只会输出 `primary` / `secondary` / `keep_default` 三个判断依据。

---

## 日志排查速览

当不确定流转路线时，开启 `log_decisions: true`，可在控制台读探针日志：

| 探针字段 | 包含的内容与含义 | 常见类型 |
| :--- | :--- | :--- |
| `route` | **最终将前往哪个通道** | `primary` / `secondary` / `keep_default` |
| `source` | **这是由谁做出的判决** | `rules` (触发规则) / `llm` (大模型当裁判) / `sticky` (黏性记忆遗留) |
| `reason` | **判决依据的细则** | 比如由于捕获了 `force_primary_regex` 强力命令等。 |

## 致谢

- 感谢 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 框架的灵活性，让本插件得以实现！
- 感谢 GPT5.4 和 Gemini3.1 提供代码补全。本插件的设计初衷便是为了兼顾 GPT-5.4 的逻辑深度与 Gemini 3.1 的卓越交互体验。
- 🎀 感谢我的Bot，Kayoko！
