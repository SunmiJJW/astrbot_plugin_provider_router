<div align="center">

# 🔀 Provider Router

[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-blueviolet?style=flat-square)](https://github.com/AstrBotDevs/AstrBot)
[![Version](https://img.shields.io/badge/version-1.1.1-blue?style=flat-square)]()
[![AstrBot Version](https://img.shields.io/badge/AstrBot-%E2%89%A5%204.10.4-green?style=flat-square)]()

**在最多三条可配置 provider 路线之间做消息智能分流。**

支持显式强制路由 · 短期黏性记忆 · 启发式路由 · 回复前缀标签 · 自适应工具档位 · 上下文自动清洗

</div>

---

## 📋 目录

- [插件定位](#-插件定位)
- [安装](#-安装)
- [三条路由](#three-routes)
- [快速开始](#-快速开始)
- [功能特色](#-功能特色)
- [配置建议](#-配置建议)
- [核心配置说明](#core-config)
- [日志排查速览](#-日志排查速览)
- [致谢与相关项目](#-致谢与相关项目)

---

## 🎯 插件定位

本插件运行在 `on_waiting_llm_request` 阶段，在 LLM 请求发出**之前**改写路由目标。

```
用户消息 ──▶ 黏性路由检查 ──▶ 规则匹配 ──▶ 启发式补判 ──▶ LLM 分类裁判(可选) ──▶ 兜底路由
                 │               │              │                   │                  │
                 └───────────────┴──────────────┴───────────────────┴──────────────────┘
                                                    ▼
                                          改写 selected_provider
                                          替换人格 prompt 段(可选)
                                          调整工具档位(可选)
                                          清洗上下文印记
```

| ✅ 它会做的事 | ❌ 它不会做的事 |
| :--- | :--- |
| **判断**消息更适合走哪条路由 | **主动唤醒**本来不会进 LLM 的消息 |
| **改写** `selected_provider` | **重建**整条聊天上下文历史 |
| **替换**对应的人格提示段 | **接管** AstrBot 以外的会话管理 |
| **清洗**当前请求里的控制词和标签 | **二次清理**已固化到历史的脏数据 |

> [!TIP]
> 如果你同时装了会重构 `req.contexts` / `req.prompt` 的其他插件，本插件只保证"当前请求阶段"的清洗。

---

## 📦 安装

**插件市场**（推荐）：在 AstrBot 管理面板的 **插件 → 插件市场** 中搜索 **「模型路由器」** 安装。

**手动安装**：将本仓库克隆到 AstrBot 的 `data/plugins/` 目录下，重启 AstrBot。

> [!IMPORTANT]
> 要求 AstrBot 版本 `≥ 4.10.4`。

---

<a id="three-routes"></a>
## 🛤️ 三条路由

| 槽位 | 定位 | 典型场景 |
| :---: | :--- | :--- |
| `primary` | 主路由 | 工具调用、代码分析、图片识别、联网检索、严谨任务 |
| `secondary` | 副路由 | 日常闲聊、情感陪伴、寒暄、轻量追问 |
| `tertiary` | 第三路由（可选） | 搜索优先、热点发散、实验性或另一种风格化路线 |

你可以把它们分别指向任意 provider，只要 AstrBot 里已经配好了对应的 provider ID。
不配置 `tertiary` 时，整体行为保持双路由兼容模式。

> [!NOTE]
> 本文档以作者的个人配置（primary → GPT、secondary → Gemini、tertiary → Grok）为示例。
> 你可以用**任意 provider** 填充这三个槽位——它们只是"路线 1 / 2 / 3"的代号，和具体模型品牌无关。

---

## 🚀 快速开始

### 第 1 步 · 配置 provider

最小必填只有两项：

```yaml
primary_provider_id: 你的强模型或多模态 provider
secondary_provider_id: 你的轻量闲聊 provider
```

如果想启用第三路由，再补：

```yaml
tertiary_provider_id: 你的第三路由 provider
```

### 第 2 步 · 推荐起步配置

```yaml
classifier_mode: rules_only        # 先用纯规则，稳定后再试 rules_then_llm
uncertain_route: keep_default      # 兜底不改写，使用 AstrBot 当前默认 provider
prefix_reply_with_route_label: false
sticky_override_enabled: true
sticky_override_rounds: 3
sticky_override_ttl_seconds: 600
sticky_release_on_opposite_signal: true
force_primary_regex: "^(?:优先主路由|走主线)\\s*"
force_secondary_regex: "^(?:优先副路由|走副线)\\s*"
force_tertiary_regex: ""           # 第三路由的口令按需配置
```

### 第 3 步 · 自测验证

向 Bot 发送以下四句话：

| # | 发送内容 | 预期行为 |
|---|---|---|
| 1 | `优先主路由 帮我分析这个报错` | 命中正则 → 主路由 |
| 2 | `优先副路由 陪我聊聊` | 命中正则 → 副路由 |
| 3 | `给我解释一下这个插件配置` | 知识类 → 倾向主路由 |
| 4 | `谢谢，晚安` | 情感类 → 倾向副路由 |

---

## ✨ 功能特色

### 🔀 三槽可选分流

- 支持 `primary / secondary / tertiary` 三条路由
- 直接改写 `selected_provider`

### ⚡ 规则优先

- 关键词分流、媒体优先主路由、链接优先主路由
- 可跳过命令样式消息（`/help`、`.provider` 等），命令前缀可自定义

### 🧠 可选二次判别

- `rules_only`：仅规则判断
- `rules_then_llm`：规则拿不准时补一轮分类模型
- 分类模型只需返回 `primary` / `secondary` / `tertiary` / `keep_default`
- 启用三槽时，插件会自动切换到内置的三槽分类 prompt，无需手动修改

### 🎯 显式强制与黏性路由

- 支持三条路由各自的 `force_*_regex` 正则口令
- 命中后短期记住选择（`sticky_override`）
- 群聊黏性严格按"会话 + 发送者"隔离，**不会串人**

### 🎭 Persona Override （人格覆写）

- 可分别配置 `primary_persona_id`、`secondary_persona_id`、`tertiary_persona_id`
- 仅替换 system prompt 里的 `Persona Instructions` 段

### 🪶 启发式补判

- 在规则和 classifier 之间补一层低成本 heuristic
- 识别 `搜一下 / 查一下  / 热点` 等搜索意图
- 短 follow-up（`继续 / 展开 / 详细点`）复用最近路由
- 引用回复优先沿用被引用消息的路由链路
- 群聊 follow-up 按引用 / 长度 / 时效做更严格限制

### 🧰 自适应工具档位

- 不同路由可配置不同的工具暴露强度（`full / light / param_only / off`）
- 区分普通模式与任务模式，任务模式下可自动升级 provider
- 默认关闭，需手动开启 `adaptive_tool_routing_enabled`

<details>
<summary><b>四个档位的对比</b></summary>

| 档位 | 工具是否保留 | `description` | `parameters` | 适合场景 |
| :--- | :--- | :--- | :--- | :--- |
| `full` | 保留 | 保留 | 保留完整 schema | 工具型任务、需要高调用成功率的回合 |
| `light` | 保留 | 保留 | 收瘦成空 `properties` 的轻量 object | 想保留工具入口，但优先压低 prompt 厚度 |
| `param_only` | 保留 | 清空 | 保留参数 schema | 想让模型主要依赖参数结构，而少看长描述文本 |
| `off` | 不保留 | 不适用 | 不适用 | 这轮明确不希望暴露工具，或想彻底压掉工具噪声 |

> [!NOTE]
> `light` **不是减少工具数量**，而是"工具还在，但 schema 更轻"。日志里看到 `tools=45->45` 说明工具集合不变，但暴露给模型的 schema 已被瘦身。

**判断顺序**：先决定 **lane**（`primary / secondary / tertiary`） → 再判断这条消息在当前 lane 下是 `normal` 还是 `task` → 按对应配置决定是否升级到 `*_task_provider_id` 以及使用哪个 `tool_mode`。

任务判定信号包括：消息含媒体、链接、代码特征、搜索意图、或命中 `task_demand_keywords` 中的关键词（可自定义）。

</details>

### 🧼 回复标签 + 上下文清洗

- 可在回复开头显示 `『GPT』、『Gemini』、『Grok』` 等路由标签
- 每次请求自动清洗标签和路由控制词，防止污染后续上下文

---

## 💡 配置建议

<details>
<summary><b>🅰️ 方案 A：高低算力灵活搭配</b></summary>

将主路由分配给强逻辑模型，副路由分配给轻快实惠模型。

- `primary_provider_id`：旗舰级模型（如 GPT-5.4）
- `secondary_provider_id`：轻量模型（如 Gemini-3.1-Flash-light）
- **适用场景**：技术解答走强模型，日常闲聊走轻巧模型

</details>

<details>
<summary><b>🅱️ 方案 B：图文多模态划分</b></summary>

将主路由留给多模态容器，副路由仅作普通文字对话。

- `primary_provider_id`：最稳的多模态 provider
- `secondary_provider_id`：最便宜的纯文字 provider
- **适用场景**：图片/截图/长文件走主路由，纯文本走副路由

</details>

<details>
<summary><b>🅲️ 方案 C：三槽均衡（推荐）</b></summary>

主路由负责深度任务，副路由负责轻聊天，第三路由承接搜索优先 / 热点 / 发散。

**核心配置**：

```yaml
primary_provider_id: GPT-5.4 / 强分析 provider
secondary_provider_id: Gemini-3.1-Flash-light / 轻聊天 provider
tertiary_provider_id: Grok-4.2-non-reasoning / 搜索优先 provider

primary_reply_prefix_label: "GPT"
secondary_reply_prefix_label: "Gemini"
tertiary_reply_prefix_label: "Grok"
```

<details>
<summary>展开完整关键词与口令配置</summary>

```yaml
primary_route_keywords: |
  工具
  排障
  报错
  分析
  配置

secondary_route_keywords: |
  闲聊
  晚安
  陪我聊聊
  安慰我

tertiary_route_keywords: |
  搜索优先
  最新
  热点
  发散
  帮我搜搜

force_primary_regex: "^(?:优先主路由|走主线|用gpt)\\s*"
force_secondary_regex: "^(?:优先副路由|走副线|用gemini)\\s*"
force_tertiary_regex: "^(?:优先第三路由|走三路|用grok)\\s*"
```

</details>

- **适用场景**：最贴近"三槽分工"的产品方向。对调 Gemini 和 Grok 只需交换 provider ID 和标签。

</details>

<details>
<summary><b>🅳️ 方案 D：三槽压测期</b></summary>

刚升级到三槽时，先观察日志和效果。

```yaml
classifier_mode: rules_only
prefix_reply_with_route_label: true
log_decisions: true
uncertain_route: keep_default
sticky_override_enabled: true
sticky_override_rounds: 3
sticky_override_ttl_seconds: 600
sticky_release_on_opposite_signal: true
```

- **适用场景**：先看一两天日志，再决定是否启用 `rules_then_llm`。

</details>

---

<a id="core-config"></a>
## ⚙️ 核心配置说明

### 1. 基础行为控制

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :---: | :---: | :--- |
| `enabled` | bool | `true` | 关闭后插件完全不参与路由 |
| `allow_group` | bool | `true` | 允许在群聊请求里改写 provider |
| `allow_private` | bool | `true` | 允许在私聊请求里改写 provider |
| `honor_existing_selection` | bool | `true` | 若 WebUI、API 或其他插件已提前指定 provider，本插件不覆盖 |
| `skip_command_like_messages` | bool | `true` | 命令样式消息（`/help`、`.provider`）不参与路由 |
| `command_like_prefixes` | text | `/` `.` `!` | 每行一个，命中这些前缀的消息被视为命令 |
| `route_media_to_primary` | bool | `true` | 图片、文件、语音、视频等默认优先走主路由 |
| `route_links_to_primary` | bool | `true` | 带 URL 的消息默认优先走主路由 |

### 2. 关键词分流

- `primary_route_keywords`：工具、技术、排障等重度任务信号
- `secondary_route_keywords`：闲聊、寒暄、陪聊等轻互动信号
- `tertiary_route_keywords`：搜索优先、热点、发散等第三路由信号

> [!NOTE]
> 关键词只是"优先分流倾向"，不会强制阻断其他意图。

### 3. 显式强制指令

```yaml
force_primary_regex: "^(?:优先主路由|走主线|用gpt)\\s*"
force_secondary_regex: "^(?:优先副路由|走副线|用gemini)\\s*"
force_tertiary_regex: "^(?:优先第三路由|走三路|用grok)\\s*"
```

命中后插件会：
- ⚡ **立即分流**到指定通道
- 🧹 **自动清洗**控制词，不干扰模型理解原文

### 4. 黏性记忆 (Sticky Route)

命中强制指令后，短期锁定选择：

| 配置项 | 说明 |
| :--- | :--- |
| `sticky_override_enabled` | 总开关 |
| `sticky_override_rounds` | 保持几个对话轮回 |
| `sticky_override_ttl_seconds` | 保持多少秒 |
| `sticky_release_on_opposite_signal` | 反方向指令时自动切换 |

> **作用域安全**：私聊按会话锁定；群聊严格按"会话 + 发送者"锁定，群友之间互不干扰。

### 5. 前缀标签与清洗

```yaml
prefix_reply_with_route_label: true
primary_reply_prefix_label: "GPT"        # 默认值为 "主路由"
secondary_reply_prefix_label: "Gemini"   # 默认值为 "副路由"
tertiary_reply_prefix_label: "Grok"      # 默认值为 "第三路由"
```

回复时追加 `『GPT』` 前缀，同时在当前请求中自动移除已有的路由标签，防止模型学着加标签。

### 6. LLM 分类裁判

```yaml
classifier_mode: rules_then_llm
classifier_provider_id: "你的轻量分类模型" #作者使用GPT-5.4nano
```

裁判模型极度轻量，只输出 `primary` / `secondary` / `tertiary` / `keep_default`。建议三槽稳定后再启用。

> [!TIP]
> 当三槽模式启用时（即 `tertiary_provider_id` 非空），插件会自动使用内置的三槽版分类 prompt，无需手动修改 `classifier_system_prompt`。

#### 补充：自适应工具档位和路由的关系

这两步是串行的：

- 第一步：先决定 **lane**
- 第二步：再决定这个 lane 下是 `normal profile` 还是 `task profile`

因此日志里如果看到：

- `lane=secondary`
- `tool_profile=task`
- `tool_mode=full`

它的意思不是"secondary 失效了"，而是：

- 这条消息仍然被判到 `secondary`
- 但它在 `secondary` 这条 lane 内，被识别成了更偏任务型的请求
- 所以切到了 `secondary_task_provider_id` / `secondary_task_tool_mode`

### 7. 兜底路由

```yaml
uncertain_route: keep_default   # 可选: keep_default / primary / secondary / tertiary
```

当规则、启发式、LLM classifier 都无法判定时：

| 值 | 行为 |
| :--- | :--- |
| `keep_default` | 不改写，继续使用 AstrBot 当前默认 provider |
| `primary` | 兜底到主路由 |
| `secondary` | 兜底到副路由 |
| `tertiary` | 兜底到第三路由（若未配置则自动退回 `keep_default`） |

### 8. 启发式判定

<details>
<summary>展开详细配置</summary>

在规则和 classifier 之间额外跑一层低成本启发式判断：

```yaml
heuristic_search_routing_enabled: true
heuristic_search_route_target: tertiary       # 搜索意图默认走哪条路由

heuristic_search_keywords: |                  # 触发搜索启发式的关键词
  搜一下
  查一下
  热点
  web search

heuristic_search_negative_keywords: |         # 阻断搜索启发式的关键词
  不要搜
  别联网
  不用查

heuristic_search_to_primary_enabled: true     # "搜完帮我分析"回主路由
heuristic_search_to_primary_keywords: |
  分析
  总结
  解读
  评估
  影响
  利弊
  报告
```

**短 follow-up 延续配置**：

```yaml
heuristic_follow_up_enabled: true
heuristic_follow_up_max_chars: 20             # 超过此长度不按短跟进处理
heuristic_follow_up_keywords: |
  继续
  展开
  详细点
  再说说
```

**群聊严格模式**：

```yaml
heuristic_group_follow_up_strict_enabled: true
heuristic_group_follow_up_max_chars_without_quote: 8
heuristic_group_follow_up_max_age_seconds_without_quote: 120
```

**最近路由历史**：

```yaml
recent_route_history_limit: 4                 # 每个会话保留几条
classifier_recent_route_context_limit: 3      # 裁判最多看几条
recent_route_collapse_consecutive_enabled: true # 连续同路由压成一段
```

> [!NOTE]
> **引用回复的优先级高于 recent route**：
> - 引用文本本身像搜索/技术/链接 → 按引用内容推路由
> - 引用的是别人的消息 → 不盲目沿用自己的 recent route
> - recent-route 历史能通过 `reply.id` 找到原路由 → 复用那条旧路由

</details>

### 9. 推荐测试顺序

如果你是第一次配三槽：

1. 先开 `prefix_reply_with_route_label: true` + `log_decisions: true`
2. 保持 `classifier_mode: rules_only`
3. 测试三条显式口令：`用gpt` / `用gemini` / `用grok`
4. 测试三类自然表达：深分析、轻闲聊、搜索优先
5. 观察 sticky 在群聊里是否只对同发送者生效
6. 稳定后关掉 `prefix_reply_with_route_label`

---

## 🔍 日志排查速览

开启 `log_decisions: true` 后，可在控制台读探针日志：

| 字段 | 含义 | 常见值 |
| :--- | :--- | :--- |
| `lane` | 最终命中的路由槽位 | `primary` / `secondary` / `tertiary` |
| `provider` | 最终被改写的 provider ID | 你配置的实际 provider |
| `applied` | 是否真的改写了 provider | `True` / `False` |
| `family` | 命中链路的大类 | `force_directive` / `lane_keyword` / `search_signal` / `follow_up` / `quote_history` / `fallback` |
| `path` | 可 grep 的命中路径 | `heuristic.follow_up_recent` / `fallback.uncertain_route←heuristic.search_like` |
| `source` | 谁做出的判决 | `rules` / `heuristic` / `llm` / `sticky` / `fallback` |
| `reason` | 判决依据 | 如 `force_primary_regex:…` |
| `sticky` | 是否使用黏性路由 | `True` / `False` |
| `force` | 是否由显式口令触发 | `True` / `False` |

> [!TIP]
> 优先看 `family` 和 `path`。`family` 适合肉眼判断，`path` 适合筛日志。

---

## 🙏 致谢与相关项目

- 本插件基于 [**AstrBot**](https://github.com/AstrBotDevs/AstrBot) 框架开发——一个灵活、可扩展的多平台聊天机器人框架。感谢 AstrBot 团队提供的开放生态！
- 🎀 感谢我的 Bot，Kayoko！

<div align="center">

---

<sub>Made with ❤️ for the AstrBot community</sub>

<a href="https://github.com/AstrBotDevs/AstrBot">
<img src="https://img.shields.io/badge/Powered%20by-AstrBot-blueviolet?style=for-the-badge&logo=github" alt="Powered by AstrBot" />
</a>

</div>
