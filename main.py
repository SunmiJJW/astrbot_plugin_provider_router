import copy
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import Provider
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.tool import ToolSet
from astrbot.core.provider.func_tool_manager import FunctionToolManager


PLUGIN_NAME = "astrbot_plugin_provider_router"
ROUTE_DECISION_KEY = "_provider_router_decision"
TOOL_PROFILE_KEY = "_provider_router_tool_profile"
RECENT_ROUTE_TTL_SECONDS = 1800
CLASSIFIER_CONTEXT_RECORD_LIMIT = 4
DEFAULT_RECENT_ROUTE_HISTORY_LIMIT = 4
DEFAULT_CLASSIFIER_RECENT_ROUTE_CONTEXT_LIMIT = 3
PRIMARY_TARGET = "primary"
SECONDARY_TARGET = "secondary"
TERTIARY_TARGET = "tertiary"
ROUTE_TARGETS = (PRIMARY_TARGET, SECONDARY_TARGET, TERTIARY_TARGET)
TARGET_ALIAS_MAP = {
    "gpt": PRIMARY_TARGET,
    "primary": PRIMARY_TARGET,
    "gemini": SECONDARY_TARGET,
    "secondary": SECONDARY_TARGET,
    "tertiary": TERTIARY_TARGET,
    "grok": TERTIARY_TARGET,
    "third": TERTIARY_TARGET,
}
FORCE_PRIMARY_REGEX_REASON_PREFIX = "force_primary_regex:"
FORCE_SECONDARY_REGEX_REASON_PREFIX = "force_secondary_regex:"
FORCE_TERTIARY_REGEX_REASON_PREFIX = "force_tertiary_regex:"
STICKY_FORCE_REASON_PREFIXES = (
    FORCE_PRIMARY_REGEX_REASON_PREFIX,
    FORCE_SECONDARY_REGEX_REASON_PREFIX,
    FORCE_TERTIARY_REGEX_REASON_PREFIX,
)
# Keep these legacy reason prefixes in the sticky-break lists so sessions
# created before the 0.2.0 public schema cleanup can still release correctly.
LEGACY_PRIMARY_REASON_PREFIXES = (
    "tool_intent_keyword:",
    "professional_keyword:",
)
LEGACY_SECONDARY_REASON_PREFIXES = ("casual_keyword:",)
DEFAULT_CLASSIFIER_SYSTEM_PROMPT = (
    "你是 AstrBot 的消息路由器。\n"
    "只返回一个词：primary、secondary 或 keep_default。\n"
    "默认优先返回 secondary。\n"
    "如果消息明显是闲聊、寒暄、陪伴、情绪安抚、玩笑、角色互动、简短追问、轻改写、简单翻译、娱乐闲谈，返回 secondary。\n"
    "如果同一发送者正在延续最近一轮已经走 secondary 的轻对话，尤其带引用回复、延续闲聊语境或只是短跟进时，也优先返回 secondary。\n"
    "只有当消息明显需要工具、插件、命令、截图或图像分析、联网搜索、代码或配置排查、结构化处理、严谨事实核实，或涉及技术、学术、法律、财务、医疗等高要求判断时，才返回 primary。\n"
    "如果只是普通解释、背景补充、轻追问，不要因为看起来像知识问题就切到 primary。\n"
    "只有真的判断不出来时才返回 keep_default。\n"
    "不要解释，不要输出其他内容。"
)
DEFAULT_HEURISTIC_SEARCH_KEYWORDS = (
    "搜一下\n搜搜\n查一下\n查查\n帮我搜\n帮我查\n搜索一下\n联网搜\n联网查\n"
    "上网搜\n上网查\nweb search\nlook up\nsearch for\ngoogle一下\nbing一下\n"
    "网友怎么说\n大家怎么说\n最近新闻\n最新新闻\n实时消息\n最新动态\n热点\n热搜"
)
DEFAULT_HEURISTIC_SEARCH_NEGATIVE_KEYWORDS = (
    "不要搜\n别搜\n不用搜\n无需搜索\n不用查\n别查\n无需查\n不要查\n"
    "别联网\n不要联网\n无需联网\n先别搜\n先别查"
)
DEFAULT_HEURISTIC_SEARCH_TO_PRIMARY_KEYWORDS = (
    "分析\n深度分析\n严谨分析\n总结\n做个总结\n帮我总结\n"
    "解读\n详细解读\n评估\n影响\n什么影响\n利弊\n对比\n比较\n"
    "原因\n为什么\n拆解\n梳理\n报告\n方案\n建议\n判断\n推演\n"
    "实际意味着什么\n风险\n机会\n走势\n"
    "analyze\nanalysis\nsummarize\nsummary\nexplain the impact\nimpact\ncompare\ncomparison\n"
    "evaluate\nevaluation\nreason why\nwhy it matters\nreport\nbrief"
)
DEFAULT_HEURISTIC_FOLLOW_UP_KEYWORDS = (
    "继续\n然后呢\n那呢\n这个呢\n还有呢\n展开\n细说\n详细点\n具体点\n"
    "接着说\n继续说\n再讲讲\n再说说\n再展开\n顺便呢"
)
DEFAULT_HEURISTIC_GROUP_SHORT_FOLLOW_UP_MAX_CHARS = 8
FOLLOW_UP_QUOTED_ONLY_PREFIX_TOKENS = (
    "\u4e3a\u4ec0\u4e48",
    "\u4e3a\u5565",
    "\u600e\u4e48",
    "\u600e\u4e48\u505a",
    "why",
    "how",
)
DEFAULT_HEURISTIC_GROUP_SHORT_FOLLOW_UP_MAX_AGE_SECONDS = 120
DEFAULT_TASK_DEMAND_KEYWORDS = (
    "帮我\n请你\n请帮\n处理\n整理\n总结\n分析\n写一下\n生成\n查询\n查一下\n搜一下\n"
    "翻译\n配置\n调试\n排查\n文件\n图片\n截图\n插件\n工具\nrun\nexecute\nopen\n"
    "search\nlook up\nanalyze\nsummarize\ntranslate\nwrite\ncreate\ngenerate\n"
    "debug\nconfig\nfile\nimage\nscreenshot\nplugin\ntool"
)
TOOL_MODE_FULL = "full"
TOOL_MODE_LIGHT = "light"
TOOL_MODE_PARAM_ONLY = "param_only"
TOOL_MODE_OFF = "off"
ALLOWED_TOOL_MODES = {
    TOOL_MODE_FULL,
    TOOL_MODE_LIGHT,
    TOOL_MODE_PARAM_ONLY,
    TOOL_MODE_OFF,
}


@dataclass
class RouteDecision:
    target: str | None
    reason: str
    source: str


@dataclass(frozen=True, slots=True)
class RouteLaneSpec:
    name: str
    enabled: bool
    provider_id: str
    task_provider_id: str
    persona_id: str
    reply_label: str
    keyword_entries: tuple[str, ...]
    force_patterns: tuple[re.Pattern[str], ...]
    aliases: tuple[str, ...]
    priority: int
    normal_tool_mode: str
    task_tool_mode: str


@dataclass(frozen=True, slots=True)
class SanitizeCatalog:
    reply_prefixes: tuple[str, ...]
    force_patterns: tuple[re.Pattern[str], ...]


@dataclass(frozen=True, slots=True)
class RoutingOutcome:
    target: str | None
    provider_id: str
    reason: str
    source: str
    applied: bool
    used_sticky: bool
    used_fallback: bool
    used_force_directive: bool


@dataclass(frozen=True, slots=True)
class LaneToolProfile:
    target: str
    effective_provider_id: str
    tool_mode: str
    profile_kind: str
    reason: str
    upgraded_provider: bool


@dataclass(frozen=True, slots=True)
class ReplyQuoteInfo:
    text: str
    sender_id: str
    sender_nickname: str
    reply_id: str = ""


@register(
    PLUGIN_NAME,
    "AnegasakiNene",
    "Route messages between up to three configurable provider lanes.",
    "1.1.1",
)
class ProviderRouterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._recent_routes_by_chat: dict[str, list[dict[str, str | float]] | dict[str, str | float]] = {}
        self._sticky_overrides_by_chat: dict[str, dict[str, str | float | int]] = {}
        self._lane_specs_cache: dict[str, RouteLaneSpec] | None = None
        self._sanitize_catalog_cache: SanitizeCatalog | None = None
        logger.info(
            "[provider_router] loaded | enabled=%s | classifier_mode=%s",
            self._cfg_bool("enabled", True),
            self._cfg_str("classifier_mode", "rules_only"),
        )

    def _cfg_bool(self, key: str, default: bool) -> bool:
        return bool(self.config.get(key, default))

    def _cfg_str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        if value is None:
            return default
        return str(value).strip()

    def _cfg_str_any(self, keys: tuple[str, ...], default: str = "") -> str:
        for key in keys:
            if key not in self.config:
                continue
            value = self._cfg_str(key)
            if value:
                return value
        return default

    def _cfg_bool_any(self, keys: tuple[str, ...], default: bool) -> bool:
        for key in keys:
            if key in self.config and self.config.get(key) is not None:
                return bool(self.config.get(key))
        return default

    def _reply_prefix_enabled(self) -> bool:
        return self._cfg_bool_any(
            ("prefix_reply_with_route_label", "prefix_reply_with_provider_family"),
            False,
        )

    def _cfg_int(self, key: str, default: int) -> int:
        value = self.config.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _cfg_text_items(self, key: str, default_raw: str = "") -> tuple[str, ...]:
        raw_value = self.config.get(key, default_raw)
        return tuple(self._iter_text_items(raw_value))

    def _normalize_route_target(self, value: object) -> str | None:
        token = str(value or "").strip().lower()
        if not token or token == "keep_default":
            return None
        return TARGET_ALIAS_MAP.get(token)

    def _heuristic_search_route_target(self) -> str | None:
        configured_target = self._normalize_route_target(
            self._cfg_str("heuristic_search_route_target", TERTIARY_TARGET)
        )
        if configured_target is None:
            configured_target = TERTIARY_TARGET

        candidates: list[str] = [configured_target]
        if PRIMARY_TARGET not in candidates:
            candidates.append(PRIMARY_TARGET)

        enabled_targets = self._enabled_route_targets()
        for candidate in candidates:
            if candidate in enabled_targets and self._route_provider_id(candidate):
                return candidate
        return None

    def _force_reason_prefix(self, target: str, kind: str) -> str:
        if target == PRIMARY_TARGET:
            return FORCE_PRIMARY_REGEX_REASON_PREFIX
        if target == SECONDARY_TARGET:
            return FORCE_SECONDARY_REGEX_REASON_PREFIX
        if target == TERTIARY_TARGET:
            return FORCE_TERTIARY_REGEX_REASON_PREFIX
        return ""

    def _force_target_from_reason(self, reason: str) -> str | None:
        if reason.startswith(FORCE_PRIMARY_REGEX_REASON_PREFIX):
            return PRIMARY_TARGET
        if reason.startswith(FORCE_SECONDARY_REGEX_REASON_PREFIX):
            return SECONDARY_TARGET
        if reason.startswith(FORCE_TERTIARY_REGEX_REASON_PREFIX):
            return TERTIARY_TARGET
        return None

    def _lane_config_keys(self, target: str) -> dict[str, tuple[str, ...] | str | tuple[str, ...]]:
        if target == PRIMARY_TARGET:
            return {
                "provider": ("primary_provider_id", "gpt_provider_id"),
                "task_provider": ("primary_task_provider_id",),
                "persona": ("primary_persona_id", "gpt_persona_id"),
                "label": ("primary_reply_prefix_label", "gpt_reply_prefix_label"),
                "keywords_explicit": "primary_route_keywords",
                "keywords_legacy": ("tool_intent_keywords", "professional_keywords"),
                "force_explicit": ("force_primary_regex", "force_gpt_regex"),
                "normal_tool_mode": "primary_normal_tool_mode",
                "task_tool_mode": "primary_task_tool_mode",
                "aliases": (PRIMARY_TARGET, "gpt"),
                "default_label": "Primary",
                "priority": 10,
                "default_normal_tool_mode": TOOL_MODE_FULL,
                "default_task_tool_mode": TOOL_MODE_FULL,
            }
        if target == SECONDARY_TARGET:
            return {
                "provider": ("secondary_provider_id", "gemini_provider_id"),
                "task_provider": ("secondary_task_provider_id",),
                "persona": ("secondary_persona_id", "gemini_persona_id"),
                "label": ("secondary_reply_prefix_label", "gemini_reply_prefix_label"),
                "keywords_explicit": "secondary_route_keywords",
                "keywords_legacy": ("casual_keywords",),
                "force_explicit": ("force_secondary_regex", "force_gemini_regex"),
                "normal_tool_mode": "secondary_normal_tool_mode",
                "task_tool_mode": "secondary_task_tool_mode",
                "aliases": (SECONDARY_TARGET, "gemini"),
                "default_label": "Secondary",
                "priority": 20,
                "default_normal_tool_mode": TOOL_MODE_LIGHT,
                "default_task_tool_mode": TOOL_MODE_FULL,
            }
        if target == TERTIARY_TARGET:
            return {
                "provider": ("tertiary_provider_id",),
                "task_provider": ("tertiary_task_provider_id",),
                "persona": ("tertiary_persona_id",),
                "label": ("tertiary_reply_prefix_label",),
                "keywords_explicit": "tertiary_route_keywords",
                "keywords_legacy": (),
                "force_explicit": ("force_tertiary_regex",),
                "normal_tool_mode": "tertiary_normal_tool_mode",
                "task_tool_mode": "tertiary_task_tool_mode",
                "aliases": (TERTIARY_TARGET, "grok"),
                "default_label": "Tertiary",
                "priority": 30,
                "default_normal_tool_mode": TOOL_MODE_LIGHT,
                "default_task_tool_mode": TOOL_MODE_FULL,
            }
        return {
            "provider": (),
            "task_provider": (),
            "persona": (),
            "label": (),
            "keywords_explicit": "",
            "keywords_legacy": (),
            "force_explicit": (),
            "normal_tool_mode": "",
            "task_tool_mode": "",
            "aliases": (),
            "default_label": "",
            "priority": 999,
            "default_normal_tool_mode": TOOL_MODE_FULL,
            "default_task_tool_mode": TOOL_MODE_FULL,
        }

    def _normalize_tool_mode(self, value: object, default: str = TOOL_MODE_FULL) -> str:
        token = str(value or "").strip().lower()
        if token in ALLOWED_TOOL_MODES:
            return token
        return default

    def _collect_route_keywords(self, target: str) -> tuple[str, ...]:
        keys = self._lane_config_keys(target)
        explicit_key = str(keys.get("keywords_explicit") or "")
        explicit = self._cfg_str(explicit_key) if explicit_key else ""
        legacy_keys = tuple(keys.get("keywords_legacy") or ())

        raw_values: list[object]
        if explicit:
            raw_values = [explicit]
        else:
            raw_values = [self.config.get(key, "") for key in legacy_keys]

        items: list[str] = []
        seen: set[str] = set()
        for raw_value in raw_values:
            for item in self._iter_text_items(raw_value):
                lowered = item.lower()
                if lowered in seen:
                    continue
                seen.add(lowered)
                items.append(item)
        return tuple(items)

    def _collect_force_patterns(self, target: str) -> tuple[re.Pattern[str], ...]:
        keys = tuple(self._lane_config_keys(target).get("force_explicit") or ())
        raw_value = ""
        for key in keys:
            raw_value = str(self.config.get(key, "") or "").strip()
            if raw_value:
                break
        return tuple(self._compile_regex_list(raw_value))

    def _build_lane_spec(self, target: str) -> RouteLaneSpec:
        keys = self._lane_config_keys(target)
        provider_id = self._cfg_str_any(tuple(keys.get("provider") or ()))
        task_provider_id = self._cfg_str_any(tuple(keys.get("task_provider") or ()))
        persona_id = self._cfg_str_any(tuple(keys.get("persona") or ()))
        reply_label = (
            self._cfg_str_any(tuple(keys.get("label") or ()), str(keys.get("default_label") or ""))
            or str(keys.get("default_label") or "")
        )
        normal_tool_mode = self._normalize_tool_mode(
            self._cfg_str(
                str(keys.get("normal_tool_mode") or ""),
                str(keys.get("default_normal_tool_mode") or TOOL_MODE_FULL),
            ),
            str(keys.get("default_normal_tool_mode") or TOOL_MODE_FULL),
        )
        task_tool_mode = self._normalize_tool_mode(
            self._cfg_str(
                str(keys.get("task_tool_mode") or ""),
                str(keys.get("default_task_tool_mode") or TOOL_MODE_FULL),
            ),
            str(keys.get("default_task_tool_mode") or TOOL_MODE_FULL),
        )
        keyword_entries = self._collect_route_keywords(target)
        force_patterns = self._collect_force_patterns(target)
        aliases = tuple(str(alias).strip().lower() for alias in tuple(keys.get("aliases") or ()) if str(alias).strip())
        priority = int(keys.get("priority") or 999)
        enabled = (
            target in {PRIMARY_TARGET, SECONDARY_TARGET}
            or bool(provider_id)
            or bool(persona_id)
            or bool(keyword_entries)
            or bool(force_patterns)
        )
        return RouteLaneSpec(
            name=target,
            enabled=enabled,
            provider_id=provider_id,
            task_provider_id=task_provider_id,
            persona_id=persona_id,
            reply_label=reply_label,
            keyword_entries=keyword_entries,
            force_patterns=force_patterns,
            aliases=aliases,
            priority=priority,
            normal_tool_mode=normal_tool_mode,
            task_tool_mode=task_tool_mode,
        )

    def _build_lane_specs(self) -> dict[str, RouteLaneSpec]:
        return {
            target: self._build_lane_spec(target)
            for target in ROUTE_TARGETS
        }

    def _lane_specs(self) -> dict[str, RouteLaneSpec]:
        cached = getattr(self, "_lane_specs_cache", None)
        if cached is None:
            cached = self._build_lane_specs()
            self._lane_specs_cache = cached
        return cached

    def _route_lane_spec(self, target: str) -> RouteLaneSpec | None:
        return self._lane_specs().get(target)

    def _enabled_route_targets(self) -> tuple[str, ...]:
        return tuple(
            target
            for target, spec in sorted(
                self._lane_specs().items(),
                key=lambda item: item[1].priority,
            )
            if spec.enabled
        )

    def _route_provider_id(self, target: str) -> str:
        spec = self._route_lane_spec(target)
        return spec.provider_id if spec else ""

    def _route_task_provider_id(self, target: str) -> str:
        spec = self._route_lane_spec(target)
        return spec.task_provider_id if spec else ""

    def _route_persona_id(self, target: str) -> str:
        spec = self._route_lane_spec(target)
        return spec.persona_id if spec else ""

    def _route_keywords(self, target: str) -> list[str]:
        spec = self._route_lane_spec(target)
        return list(spec.keyword_entries) if spec else []

    def _force_regex_patterns(self, target: str) -> list[re.Pattern[str]]:
        spec = self._route_lane_spec(target)
        return list(spec.force_patterns) if spec else []

    def _route_normal_tool_mode(self, target: str) -> str:
        spec = self._route_lane_spec(target)
        return spec.normal_tool_mode if spec else TOOL_MODE_FULL

    def _route_task_tool_mode(self, target: str) -> str:
        spec = self._route_lane_spec(target)
        return spec.task_tool_mode if spec else TOOL_MODE_FULL

    def _iter_text_items(
        self,
        raw_value: object,
        *,
        split_commas: bool = True,
    ) -> list[str]:
        if raw_value is None:
            return []
        if isinstance(raw_value, list | tuple | set):
            items = []
            for item in raw_value:
                text = str(item).strip()
                if text:
                    items.append(text)
            return items

        text = str(raw_value)
        if split_commas:
            text = text.replace(",", "\n")
        return [line.strip() for line in text.splitlines() if line.strip()]

    def _compile_regex_list(self, raw_value: object) -> list[re.Pattern[str]]:
        compiled: list[re.Pattern[str]] = []
        for pattern in self._iter_text_items(raw_value, split_commas=False):
            try:
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                logger.warning(
                    "[provider_router] skip invalid regex %r: %s",
                    pattern,
                    exc,
                )
        return compiled

    def _match_regex_list(
        self,
        text: str,
        patterns: Iterable[re.Pattern[str]],
    ) -> str | None:
        for pattern in patterns:
            if pattern.search(text):
                return pattern.pattern
        return None

    def _extract_force_directive_from_reason(
        self,
        reason: str,
    ) -> tuple[str, str, str] | None:
        for prefix in STICKY_FORCE_REASON_PREFIXES:
            if reason.startswith(prefix):
                target = self._force_target_from_reason(reason)
                if not target:
                    return None
                return target, "regex", reason[len(prefix) :]
        return None

    def _detect_force_directive_reason(self, text: str) -> str:
        if not text:
            return ""

        for target in self._enabled_route_targets():
            forced = self._match_regex_list(text, self._force_regex_patterns(target))
            if forced:
                return f"{self._force_reason_prefix(target, 'regex')}{forced}"

        return ""

    def _strip_force_directive_by_reason(
        self,
        text: str,
        reason: str,
    ) -> str:
        extracted = self._extract_force_directive_from_reason(reason)
        if not extracted:
            return text

        _, kind, marker = extracted
        if not marker:
            return text

        try:
            pattern = re.compile(marker, re.IGNORECASE)
        except re.error as exc:
            logger.warning(
                "[provider_router] skip strip for invalid force regex %r: %s",
                marker,
                exc,
            )
            return text

        if not pattern:
            return text

        match = pattern.search(text)
        if not match:
            return text

        stripped = self._cleanup_text_after_removal(
            text[: match.start()],
            text[match.end() :],
            drop_trailing_punctuation=not bool(text[match.end() :].strip()),
        )
        if not stripped:
            logger.info(
                "[provider_router] matched explicit route directive %r but kept original text because stripping would make prompt empty",
                marker,
            )
            return text
        return stripped

    def _cleanup_text_after_removal(
        self,
        before: str,
        after: str,
        *,
        drop_trailing_punctuation: bool = False,
    ) -> str:
        updated = f"{before} {after}"
        updated = re.sub(r"\s+", " ", updated).strip()
        updated = re.sub(
            r"^[\s\u3001\u3002\uff0c\uff01\uff1f\uff1a\uff1b\uff5e,\.!\?:;~]+",
            "",
            updated,
        ).lstrip()
        if drop_trailing_punctuation:
            updated = re.sub(
                r"[\s\u3001\u3002\uff0c\uff01\uff1f\uff1a\uff1b\uff5e,\.!\?:;~]+$",
                "",
                updated,
            ).rstrip()
        updated = re.sub(r"\s+", " ", updated).strip()
        return updated

    def _strip_force_regex_directive(
        self,
        text: str,
        decision: RouteDecision,
    ) -> str:
        return self._strip_force_directive_by_reason(text, decision.reason)

    def _rewrite_event_message_chain_text(
        self,
        event: AstrMessageEvent,
        rewritten_text: str,
    ) -> None:
        message_obj = getattr(event, "message_obj", None)
        if message_obj is None:
            return

        chain = getattr(message_obj, "message", None)
        if not isinstance(chain, list):
            return

        if not any(isinstance(comp, Comp.Plain) for comp in chain):
            return

        new_chain: list[Any] = []
        plain_written = False
        for comp in chain:
            if isinstance(comp, Comp.Plain):
                if not plain_written and rewritten_text:
                    new_chain.append(Comp.Plain(rewritten_text))
                plain_written = True
                continue
            new_chain.append(comp)

        message_obj.message = new_chain

    def _sanitize_request_prompt(
        self,
        req: Any,
        original_text: str,
        rewritten_text: str,
        reason: str,
    ) -> None:
        if not original_text or rewritten_text == original_text:
            return

        original_prompt = str(getattr(req, "prompt", "") or "")
        if not original_prompt:
            return

        updated_prompt = original_prompt
        if original_text in updated_prompt:
            updated_prompt = updated_prompt.replace(original_text, rewritten_text, 1)
        else:
            updated_prompt = self._strip_force_directive_by_reason(
                updated_prompt,
                reason,
            )

        if updated_prompt == original_prompt:
            return

        req.prompt = updated_prompt
        logger.info(
            "[provider_router] sanitized req.prompt after downstream prompt rewrite | reason=%s | before=%r | after=%r",
            reason,
            self._clip_text(original_prompt, 160),
            self._clip_text(updated_prompt, 160),
        )

    def _all_force_directive_patterns(self) -> list[re.Pattern[str]]:
        return list(self._sanitize_catalog().force_patterns)

    def _build_sanitize_catalog(self) -> SanitizeCatalog:
        reply_prefixes: list[str] = []
        seen_reply_prefixes: set[str] = set()
        force_patterns: list[re.Pattern[str]] = []
        seen_force_patterns: set[str] = set()

        for target, spec in self._lane_specs().items():
            if spec.enabled and spec.reply_label:
                prefix = f"\u300e{spec.reply_label}\u300f"
                if prefix not in seen_reply_prefixes:
                    seen_reply_prefixes.add(prefix)
                    reply_prefixes.append(prefix)

            for pattern in spec.force_patterns:
                key = pattern.pattern
                if key in seen_force_patterns:
                    continue
                seen_force_patterns.add(key)
                force_patterns.append(pattern)

        return SanitizeCatalog(
            reply_prefixes=tuple(reply_prefixes),
            force_patterns=tuple(force_patterns),
        )

    def _sanitize_catalog(self) -> SanitizeCatalog:
        cached = getattr(self, "_sanitize_catalog_cache", None)
        if cached is None:
            cached = self._build_sanitize_catalog()
            self._sanitize_catalog_cache = cached
        return cached

    def _strip_force_directives_from_text(self, text: str) -> str:
        updated = str(text or "")
        if not updated:
            return updated

        patterns = self._all_force_directive_patterns()
        if not patterns:
            return updated

        sanitized_lines: list[str] = []
        changed = False
        for line in updated.splitlines(keepends=True):
            newline = ""
            body = line
            if body.endswith("\r\n"):
                body = body[:-2]
                newline = "\r\n"
            elif body.endswith("\n"):
                body = body[:-1]
                newline = "\n"

            leader_match = re.match(r"^(\s*(?:[^\r\n]{0,80}[:：]\s*)?)", body)
            leader = leader_match.group(1) if leader_match else ""
            content = body[len(leader) :]
            sanitized_content = content

            for pattern in patterns:
                match = pattern.search(sanitized_content)
                if not match:
                    continue
                candidate = f"{sanitized_content[: match.start()]} {sanitized_content[match.end() :]}"
                candidate = re.sub(r"\s+", " ", candidate).strip()
                candidate = re.sub(
                    r"^[\s\u3001\u3002\uff0c\uff01\uff1f\uff1a\uff1b\uff5e,\.!\?:;~]+",
                    "",
                    candidate,
                ).lstrip()
                candidate = re.sub(r"\s+", " ", candidate).strip()
                sanitized_content = candidate

            sanitized_body = f"{leader}{sanitized_content}" if sanitized_content else leader.rstrip()
            if sanitized_body != body:
                changed = True
            sanitized_lines.append(f"{sanitized_body}{newline}")

        if not changed:
            return updated
        return "".join(sanitized_lines)

    def _sanitize_context_visible_text(self, text: str) -> str:
        updated = self._strip_force_directives_from_text(text)
        updated = self._strip_route_reply_prefixes_from_text(updated)
        return updated

    def _route_reply_prefixes(self) -> list[str]:
        return list(self._sanitize_catalog().reply_prefixes)

    def _strip_route_reply_prefixes_from_text(self, text: str) -> str:
        updated = str(text or "")
        if not updated or not self._reply_prefix_enabled():
            return updated
        return self._strip_route_reply_prefixes_linewise(updated)

    def _strip_route_reply_prefixes_linewise(self, text: str) -> str:
        prefixes = self._route_reply_prefixes()
        if not prefixes:
            return text

        sanitized_lines: list[str] = []
        changed = False
        for line in text.splitlines(keepends=True):
            newline = ""
            body = line
            if body.endswith("\r\n"):
                body = body[:-2]
                newline = "\r\n"
            elif body.endswith("\n"):
                body = body[:-1]
                newline = "\n"

            sanitized_body = body
            for prefix in prefixes:
                pattern = re.compile(
                    rf"(^|\s*(?:[^\r\n]{{0,80}}[:：]\s*)?)(?:{re.escape(prefix)}\s*)+(?=\S|$)",
                )
                while True:
                    match = pattern.search(sanitized_body)
                    if not match:
                        break
                    replacement = self._cleanup_text_after_removal(
                        sanitized_body[: match.start()],
                        sanitized_body[match.end() :],
                        drop_trailing_punctuation=not bool(
                            sanitized_body[match.end() :].strip()
                        ),
                    )
                    if replacement == sanitized_body:
                        break
                    sanitized_body = replacement

            if sanitized_body != body:
                changed = True
            sanitized_lines.append(f"{sanitized_body}{newline}")

        if not changed:
            return text
        return "".join(sanitized_lines)

    def _sanitize_route_reply_prefixes_in_request_prompt(self, req: Any) -> None:
        original_prompt = str(getattr(req, "prompt", "") or "")
        if not original_prompt:
            return

        updated_prompt = self._strip_route_reply_prefixes_from_text(original_prompt)
        if updated_prompt == original_prompt:
            return

        req.prompt = updated_prompt
        logger.info(
            "[provider_router] sanitized route reply prefix from req.prompt | before=%r | after=%r",
            self._clip_text(original_prompt, 160),
            self._clip_text(updated_prompt, 160),
        )

    def _sanitize_force_directives_in_request_prompt(self, req: Any) -> None:
        original_prompt = str(getattr(req, "prompt", "") or "")
        if not original_prompt:
            return

        updated_prompt = self._strip_force_directives_from_text(original_prompt)
        if updated_prompt == original_prompt:
            return

        req.prompt = updated_prompt
        logger.info(
            "[provider_router] sanitized explicit route directive from req.prompt history | before=%r | after=%r",
            self._clip_text(original_prompt, 160),
            self._clip_text(updated_prompt, 160),
        )

    def _sanitize_request_content_value(self, content: Any) -> tuple[Any, bool]:
        if isinstance(content, str):
            updated = self._sanitize_context_visible_text(content)
            return updated, updated != content

        if not isinstance(content, list):
            return content, False

        changed = False
        for item in content:
            if isinstance(item, dict):
                item_type = str(item.get("type", "")).strip().lower()
                if item_type != "text":
                    continue
                original_text = str(item.get("text", "") or "")
                updated_text = self._sanitize_context_visible_text(original_text)
                if updated_text != original_text:
                    item["text"] = updated_text
                    changed = True
                continue

            item_type = str(getattr(item, "type", "") or "").strip().lower()
            if item_type != "text" or not hasattr(item, "text"):
                continue
            original_text = str(getattr(item, "text", "") or "")
            updated_text = self._sanitize_context_visible_text(original_text)
            if updated_text != original_text:
                setattr(item, "text", updated_text)
                changed = True

        return content, changed

    def _sanitize_request_contexts(self, req: Any) -> None:
        contexts = getattr(req, "contexts", None)
        if not isinstance(contexts, list) or not contexts:
            return

        changed_count = 0
        for message in contexts:
            if isinstance(message, dict):
                role = str(message.get("role", "") or "").strip().lower()
                if role not in {"user", "assistant"}:
                    continue
                updated_content, changed = self._sanitize_request_content_value(
                    message.get("content")
                )
                if changed:
                    message["content"] = updated_content
                    changed_count += 1
                continue

            role = str(getattr(message, "role", "") or "").strip().lower()
            if role not in {"user", "assistant"} or not hasattr(message, "content"):
                continue
            original_content = getattr(message, "content")
            updated_content, changed = self._sanitize_request_content_value(
                original_content
            )
            if changed:
                setattr(message, "content", updated_content)
                changed_count += 1

        if changed_count:
            logger.info(
                "[provider_router] sanitized visible route markers in req.contexts | changed_messages=%s",
                changed_count,
            )

    def _sanitize_request_extra_user_content_parts(self, req: Any) -> None:
        parts = getattr(req, "extra_user_content_parts", None)
        if not isinstance(parts, list) or not parts:
            return

        changed_count = 0
        for part in parts:
            if isinstance(part, dict):
                part_type = str(part.get("type", "") or "").strip().lower()
                if part_type != "text":
                    continue
                original_text = str(part.get("text", "") or "")
                updated_text = self._sanitize_context_visible_text(original_text)
                if updated_text != original_text:
                    part["text"] = updated_text
                    changed_count += 1
                continue

            part_type = str(getattr(part, "type", "") or "").strip().lower()
            if part_type != "text" or not hasattr(part, "text"):
                continue
            original_text = str(getattr(part, "text", "") or "")
            updated_text = self._sanitize_context_visible_text(original_text)
            if updated_text != original_text:
                setattr(part, "text", updated_text)
                changed_count += 1

        if changed_count:
            logger.info(
                "[provider_router] sanitized visible route markers in req.extra_user_content_parts | changed_parts=%s",
                changed_count,
            )

    def _detect_target_from_actual_provider(self, event: AstrMessageEvent) -> str | None:
        # `_actual_llm_provider_family` is an existing upstream event extra name.
        # Keep reading it for compatibility, but normalize its value into the
        # route lane slots immediately.
        actual_target = self._normalize_route_target(
            event.get_extra("_actual_llm_provider_family", "")
        )
        if actual_target in self._enabled_route_targets():
            return actual_target

        actual_provider_id = str(event.get_extra("_actual_llm_provider_id", "") or "").strip()
        for target in self._enabled_route_targets():
            provider_id = self._route_provider_id(target)
            if actual_provider_id and provider_id and actual_provider_id == provider_id:
                return target

        return None

    def _get_route_target(self, event: AstrMessageEvent) -> str | None:
        if actual_target := self._detect_target_from_actual_provider(event):
            return actual_target

        decision = event.get_extra(ROUTE_DECISION_KEY, {}) or {}
        if isinstance(decision, dict):
            target = self._normalize_route_target(decision.get("target"))
            applied = bool(decision.get("applied", False))
            if target in self._enabled_route_targets() and applied:
                return target

        selected_provider = str(event.get_extra("selected_provider", "") or "").strip()
        for target in self._enabled_route_targets():
            provider_id = self._route_provider_id(target)
            if selected_provider and provider_id and selected_provider == provider_id:
                return target

        current_provider = self.context.get_using_provider(umo=event.unified_msg_origin)
        current_provider_id = ""
        if isinstance(current_provider, Provider):
            current_provider_id = str(current_provider.meta().id or "").strip()

        for target in self._enabled_route_targets():
            provider_id = self._route_provider_id(target)
            if current_provider_id and provider_id and current_provider_id == provider_id:
                return target
        return None

    def _get_route_prefix_label(self, target: str) -> str:
        spec = self._route_lane_spec(target)
        return spec.reply_label if spec else ""

    def _adaptive_tool_routing_enabled(self) -> bool:
        return self._cfg_bool("adaptive_tool_routing_enabled", False)

    def _task_demand_keywords(self) -> list[str]:
        return list(
            self._cfg_text_items(
                "task_demand_keywords",
                DEFAULT_TASK_DEMAND_KEYWORDS,
            )
        )

    def _provider_id_is_available(self, provider_id: str) -> bool:
        if not provider_id:
            return False
        try:
            return bool(self.context.get_provider_by_id(provider_id))
        except Exception:  # noqa: BLE001
            return False

    def _detect_lane_task_demand(
        self,
        event: AstrMessageEvent,
        text: str,
        target: str,
    ) -> str | None:
        normalized = self._normalize_text(text)
        if self._message_has_media(event):
            return "task:message_has_media"

        if normalized:
            if re.search(r"https?://|www\.", normalized):
                return "task:contains_link"

            primary_keyword = self._contains_keyword(
                normalized,
                self._route_keywords(PRIMARY_TARGET),
            )
            if primary_keyword:
                return f"task:primary_keyword:{primary_keyword}"

            if self._looks_code_like(text):
                return "task:code_like_message"

            negative_search_signal = self._detect_negative_search_signal(normalized)
            if not negative_search_signal:
                search_to_primary_signal = self._detect_search_to_primary_signal(
                    normalized
                )
                if search_to_primary_signal:
                    return f"task:search_to_primary:{search_to_primary_signal}"

                search_like_signal = self._detect_search_like_signal(normalized)
                if search_like_signal:
                    return f"task:search_like:{search_like_signal}"

            task_keyword = self._contains_keyword(normalized, self._task_demand_keywords())
            if task_keyword:
                return f"task:keyword:{task_keyword}"

        recent_route = self._get_recent_route_context(event)
        quote_info = self._extract_reply_quote_info(event)
        follow_up_signal = self._detect_follow_up_signal(
            normalized,
            quote_info.text if quote_info else "",
        )
        if (
            follow_up_signal
            and recent_route
            and self._can_reuse_recent_route_for_follow_up(
                event,
                normalized,
                quote_info,
                recent_route,
            )
        ):
            recent_target = self._normalize_route_target(recent_route.get("target"))
            recent_profile_kind = str(
                recent_route.get("tool_profile_kind") or ""
            ).strip()
            if recent_target == target and recent_profile_kind == "task":
                recent_reason = str(recent_route.get("task_reason") or "").strip()
                suffix = f"|after:{recent_reason}" if recent_reason else ""
                return f"task:follow_up:{follow_up_signal}{suffix}"

        return None

    def _build_lane_tool_profile(
        self,
        event: AstrMessageEvent,
        text: str,
        decision: RouteDecision,
        applied: bool,
    ) -> LaneToolProfile | None:
        if (
            not applied
            or not self._adaptive_tool_routing_enabled()
            or decision.target not in self._enabled_route_targets()
        ):
            return None

        assert decision.target is not None
        base_provider_id = self._route_provider_id(decision.target)
        if not base_provider_id:
            return None

        task_reason = self._detect_lane_task_demand(event, text, decision.target)
        if task_reason:
            task_provider_id = self._route_task_provider_id(decision.target)
            effective_provider_id = (
                task_provider_id
                if self._provider_id_is_available(task_provider_id)
                else base_provider_id
            )
            if task_provider_id and effective_provider_id == base_provider_id:
                logger.warning(
                    "[provider_router] adaptive task provider unavailable for lane=%s | configured=%s | fallback=%s",
                    decision.target,
                    task_provider_id,
                    base_provider_id,
                )
            return LaneToolProfile(
                target=decision.target,
                effective_provider_id=effective_provider_id,
                tool_mode=self._route_task_tool_mode(decision.target),
                profile_kind="task",
                reason=task_reason,
                upgraded_provider=bool(
                    task_provider_id and effective_provider_id == task_provider_id
                ),
            )

        return LaneToolProfile(
            target=decision.target,
            effective_provider_id=base_provider_id,
            tool_mode=self._route_normal_tool_mode(decision.target),
            profile_kind="normal",
            reason="normal_lane_profile",
            upgraded_provider=False,
        )

    def _store_tool_profile(
        self,
        event: AstrMessageEvent,
        profile: LaneToolProfile | None,
    ) -> None:
        if profile is None:
            event.set_extra(TOOL_PROFILE_KEY, {})
            return
        event.set_extra(
            TOOL_PROFILE_KEY,
            {
                "target": profile.target,
                "effective_provider_id": profile.effective_provider_id,
                "tool_mode": profile.tool_mode,
                "profile_kind": profile.profile_kind,
                "reason": profile.reason,
                "upgraded_provider": profile.upgraded_provider,
            },
        )

    def _coerce_request_tool_set(self, req: Any) -> ToolSet | None:
        tool_set = getattr(req, "func_tool", None)
        if isinstance(tool_set, FunctionToolManager):
            req.func_tool = tool_set.get_full_tool_set()
            tool_set = req.func_tool
        if not isinstance(tool_set, ToolSet):
            return None
        return tool_set

    def _tool_set_for_mode(
        self,
        tool_set: ToolSet,
        mode: str,
    ) -> ToolSet | None:
        if mode == TOOL_MODE_FULL:
            return tool_set
        if mode == TOOL_MODE_OFF:
            return None

        cloned_tool_set = ToolSet()
        for tool in tool_set.tools:
            if hasattr(tool, "active") and not bool(getattr(tool, "active", True)):
                continue
            cloned_tool = copy.copy(tool)
            if mode == TOOL_MODE_LIGHT:
                cloned_tool.parameters = {"type": "object", "properties": {}}
            elif mode == TOOL_MODE_PARAM_ONLY:
                cloned_tool.parameters = (
                    copy.deepcopy(tool.parameters)
                    if tool.parameters
                    else {"type": "object", "properties": {}}
                )
                cloned_tool.description = ""
            cloned_tool_set.add_tool(cloned_tool)
        return cloned_tool_set

    def _apply_route_tool_profile(
        self,
        event: AstrMessageEvent,
        req: Any,
    ) -> None:
        if not self._adaptive_tool_routing_enabled():
            return

        profile = event.get_extra(TOOL_PROFILE_KEY, {}) or {}
        if not isinstance(profile, dict):
            return

        tool_mode = self._normalize_tool_mode(
            profile.get("tool_mode"),
            TOOL_MODE_FULL,
        )
        tool_set = self._coerce_request_tool_set(req)
        if tool_mode == TOOL_MODE_FULL or tool_set is None:
            return

        transformed_tool_set = self._tool_set_for_mode(tool_set, tool_mode)
        original_count = len(tool_set.tools)
        transformed_count = (
            len(transformed_tool_set.tools)
            if isinstance(transformed_tool_set, ToolSet)
            else 0
        )
        req.func_tool = transformed_tool_set
        logger.info(
            "[provider_router] adaptive tool profile applied | lane=%s | profile=%s | tool_mode=%s | tools=%s->%s | provider=%s",
            profile.get("target") or "unknown",
            profile.get("profile_kind") or "unknown",
            tool_mode,
            original_count,
            transformed_count,
            profile.get("effective_provider_id") or "<default>",
        )

    def _build_route_reply_prefix(self, event: AstrMessageEvent) -> str:
        if not self._reply_prefix_enabled():
            return ""

        target = self._get_route_target(event)
        if target not in self._enabled_route_targets():
            return ""

        label = self._get_route_prefix_label(target)
        if not label:
            return ""

        return f"\u300e{label}\u300f"

    async def _resolve_current_persona_prompt(
        self,
        event: AstrMessageEvent,
        req: Any,
    ) -> str:
        persona_manager = getattr(self.context, "persona_manager", None)
        if persona_manager is None:
            return ""

        conversation = getattr(req, "conversation", None)
        conversation_persona_id = getattr(conversation, "persona_id", None)
        provider_settings = self.context.get_config(
            umo=event.unified_msg_origin
        ).get("provider_settings", {})

        try:
            _, persona, _, _ = await persona_manager.resolve_selected_persona(
                umo=event.unified_msg_origin,
                conversation_persona_id=conversation_persona_id,
                platform_name=event.get_platform_name(),
                provider_settings=provider_settings,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[provider_router] failed to resolve current persona for prompt swap: %s",
                exc,
            )
            return ""

        if not persona:
            return ""
        return str(persona.get("prompt") or "")

    async def _load_persona_prompt_by_id(self, persona_id: str) -> str:
        persona_id = str(persona_id or "").strip()
        if not persona_id:
            return ""

        persona_manager = getattr(self.context, "persona_manager", None)
        if persona_manager is None:
            return ""

        try:
            persona = await persona_manager.get_persona(persona_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[provider_router] failed to load persona %r: %s",
                persona_id,
                exc,
            )
            return ""

        return str(getattr(persona, "system_prompt", "") or "")

    def _build_persona_block(self, prompt: str) -> str:
        return f"\n# Persona Instructions\n\n{prompt}\n"

    def _swap_persona_block(
        self,
        system_prompt: str,
        current_persona_prompt: str,
        target_persona_prompt: str,
    ) -> tuple[str, str]:
        target_block = self._build_persona_block(target_persona_prompt)
        target_block_at_start = target_block.lstrip("\n")
        current_block = (
            self._build_persona_block(current_persona_prompt)
            if current_persona_prompt
            else ""
        )
        current_block_at_start = current_block.lstrip("\n") if current_block else ""

        if current_block and current_block in system_prompt:
            return system_prompt.replace(current_block, target_block, 1), "replaced_exact"

        if current_block_at_start and system_prompt.startswith(current_block_at_start):
            suffix = system_prompt[len(current_block_at_start) :]
            return f"{target_block_at_start}{suffix}", "replaced_exact"

        generic_pattern = re.compile(
            r"(?s)(?:\A|\n)# Persona Instructions\n\n.*?(?=(?:\n##? [^\n]|\Z))"
        )
        if generic_pattern.search(system_prompt):
            updated_system_prompt = generic_pattern.sub(
                lambda match: (
                    target_block_at_start if match.start() == 0 else target_block
                ),
                system_prompt,
                count=1,
            )
            return updated_system_prompt, "replaced_section"

        if not system_prompt:
            return target_block.lstrip("\n"), "inserted_new_section"

        separator = "" if system_prompt.endswith("\n") else "\n"
        return f"{system_prompt}{separator}{target_block.lstrip()}", "appended_missing_section"

    async def _apply_route_persona_override(
        self,
        event: AstrMessageEvent,
        req: Any,
    ) -> None:
        target = self._get_route_target(event)
        if target not in self._enabled_route_targets():
            return

        persona_id = self._route_persona_id(target)
        if not persona_id:
            return

        target_persona_prompt = await self._load_persona_prompt_by_id(persona_id)
        if not target_persona_prompt.strip():
            logger.warning(
                "[provider_router] persona override target=%s persona=%r but prompt is empty or persona could not be loaded",
                target,
                persona_id,
            )
            return

        original_system_prompt = str(getattr(req, "system_prompt", "") or "")
        current_persona_prompt = await self._resolve_current_persona_prompt(event, req)
        updated_system_prompt, mode = self._swap_persona_block(
            original_system_prompt,
            current_persona_prompt,
            target_persona_prompt,
        )

        if updated_system_prompt == original_system_prompt:
            return

        req.system_prompt = updated_system_prompt
        logger.info(
            "[provider_router] applied route persona override | target=%s | persona=%s | mode=%s",
            target,
            persona_id,
            mode,
        )

    def _rewrite_event_prompt_text(
        self,
        event: AstrMessageEvent,
        original_text: str,
        rewritten_text: str,
        reason: str,
    ) -> None:
        if rewritten_text == original_text:
            return

        event.message_str = rewritten_text
        if getattr(event, "message_obj", None) is not None:
            event.message_obj.message_str = rewritten_text
        self._rewrite_event_message_chain_text(event, rewritten_text)
        event.set_extra("_provider_router_original_message_str", original_text)
        event.set_extra("_provider_router_rewritten_message_str", rewritten_text)
        event.set_extra("_provider_router_strip_reason", reason)
        logger.info(
            "[provider_router] stripped explicit route directive | reason=%s | before=%r | after=%r",
            reason,
            self._clip_text(original_text, 160),
            self._clip_text(rewritten_text, 160),
        )

    def _contains_keyword(self, text: str, keywords: list[str]) -> str | None:
        lowered = text.lower()
        for keyword in keywords:
            token = keyword.lower()
            if token and token in lowered:
                return keyword
        return None

    def _normalize_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", text).strip().lower()

    def _clip_text(self, text: str, limit: int) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3] + "..."

    def _outline_chain(self, chain: list | None) -> str:
        if not chain:
            return ""

        parts: list[str] = []
        for comp in chain:
            if isinstance(comp, Comp.Plain):
                parts.append(comp.text)
            elif isinstance(comp, Comp.Image):
                parts.append("[image]")
            elif isinstance(comp, Comp.File):
                parts.append("[file]")
            elif isinstance(comp, Comp.Record):
                parts.append("[audio]")
            elif isinstance(comp, Comp.Video):
                parts.append("[video]")
            elif isinstance(comp, Comp.At):
                parts.append(f"@{getattr(comp, 'name', '') or getattr(comp, 'qq', '')}")
            elif isinstance(comp, Comp.AtAll):
                parts.append("@all")
            elif isinstance(comp, Comp.Forward):
                parts.append("[forward]")
            elif isinstance(comp, Comp.Reply):
                if getattr(comp, "message_str", None):
                    sender = getattr(comp, "sender_nickname", "") or "quoted"
                    parts.append(f"[quote:{sender} {comp.message_str}]")
                else:
                    parts.append("[quote]")
            else:
                parts.append(f"[{getattr(comp, 'type', 'segment')}]")
        return " ".join(part for part in parts if part).strip()

    def _extract_reply_quote_info(
        self,
        event: AstrMessageEvent,
    ) -> ReplyQuoteInfo | None:
        for comp in event.get_messages():
            if not isinstance(comp, Comp.Reply):
                continue
            quoted_text = ""
            if getattr(comp, "message_str", None):
                quoted_text = self._sanitize_context_visible_text(
                    str(comp.message_str).strip()
                )
            else:
                chain_text = self._outline_chain(getattr(comp, "chain", None))
                if chain_text:
                    quoted_text = self._sanitize_context_visible_text(chain_text)
            sender_id = str(getattr(comp, "sender_id", "") or "").strip()
            sender_nickname = str(getattr(comp, "sender_nickname", "") or "").strip()
            reply_id = str(getattr(comp, "id", "") or "").strip()
            if quoted_text or sender_id or sender_nickname or reply_id:
                return ReplyQuoteInfo(
                    text=quoted_text,
                    sender_id=sender_id,
                    sender_nickname=sender_nickname,
                    reply_id=reply_id,
                )
        return None

    def _extract_reply_quote_text(self, event: AstrMessageEvent) -> str:
        quote_info = self._extract_reply_quote_info(event)
        if quote_info:
            return quote_info.text
        return ""

    def _message_has_media(self, event: AstrMessageEvent) -> bool:
        for comp in event.get_messages():
            if isinstance(comp, (Comp.Image, Comp.File, Comp.Record, Comp.Video)):
                return True
            if isinstance(comp, Comp.Reply) and getattr(comp, "chain", None):
                for reply_comp in comp.chain:
                    if isinstance(
                        reply_comp,
                        (Comp.Image, Comp.File, Comp.Record, Comp.Video),
                    ):
                        return True
        return False

    def _looks_command_like(self, text: str) -> bool:
        if not self._cfg_bool("skip_command_like_messages", True):
            return False
        prefixes = self._iter_text_items(
            self.config.get("command_like_prefixes", "/\n.\n!"),
        )
        stripped = text.strip()
        return any(stripped.startswith(prefix) for prefix in prefixes if prefix)

    def _looks_code_like(self, text: str) -> bool:
        code_markers = [
            "```",
            "traceback",
            "exception",
            "error:",
            "stack trace",
            "stacktrace",
            "nullpointer",
            "syntaxerror",
            "typeerror",
            "valueerror",
            "import ",
            "def ",
            "class ",
            "function ",
            "console.log",
            "npm ",
            "pip ",
            "uv ",
            "docker ",
            "curl ",
            "api ",
            ".py",
            ".js",
            ".ts",
            ".json",
            "http://",
            "https://",
            "c:\\",
            "/api/",
        ]
        lowered = text.lower()
        if any(marker in lowered for marker in code_markers):
            return True
        if re.search(r"line \d+", lowered):
            return True
        if re.search(r"[{}<>$]{2,}", text):
            return True
        return False

    def _detect_search_like_signal(self, normalized_text: str) -> str | None:
        if not self._cfg_bool("heuristic_search_routing_enabled", True):
            return None
        for phrase in self._cfg_text_items(
            "heuristic_search_keywords",
            DEFAULT_HEURISTIC_SEARCH_KEYWORDS,
        ):
            if phrase and phrase in normalized_text:
                return phrase
        return None

    def _detect_negative_search_signal(self, normalized_text: str) -> str | None:
        if not self._cfg_bool("heuristic_search_routing_enabled", True):
            return None
        for phrase in self._cfg_text_items(
            "heuristic_search_negative_keywords",
            DEFAULT_HEURISTIC_SEARCH_NEGATIVE_KEYWORDS,
        ):
            if phrase and phrase in normalized_text:
                return phrase
        return None

    def _detect_search_to_primary_signal(self, normalized_text: str) -> str | None:
        if not self._cfg_bool("heuristic_search_routing_enabled", True):
            return None
        if not self._cfg_bool("heuristic_search_to_primary_enabled", True):
            return None
        for phrase in self._cfg_text_items(
            "heuristic_search_to_primary_keywords",
            DEFAULT_HEURISTIC_SEARCH_TO_PRIMARY_KEYWORDS,
        ):
            if phrase and phrase in normalized_text:
                return phrase
        return None

    def _reason_implies_search_routing(self, reason: str) -> bool:
        raw_reason = str(reason or "").strip()
        if not raw_reason:
            return False

        anchor = self._decision_reason_anchor(raw_reason)
        if anchor.startswith(
            (
                "heuristic:search_like:",
                "heuristic:quoted_search_like:",
                "heuristic:angelheart_needs_search",
            )
        ):
            return True

        if anchor.startswith("heuristic:follow_up_recent:") and "|after:" in raw_reason:
            after_part = raw_reason.split("|after:", 1)[1].strip()
            after_source, separator, after_reason = after_part.partition(":")
            if separator and after_reason:
                return self._reason_implies_search_routing(after_reason)
            return self._reason_implies_search_routing(after_part)

        keyword_prefixes = (
            "tertiary_keyword:",
            "heuristic:quoted_tertiary_keyword:",
        )
        for prefix in keyword_prefixes:
            if not anchor.startswith(prefix):
                continue
            keyword = anchor[len(prefix) :].strip()
            normalized_keyword = self._normalize_text(keyword)
            return bool(
                normalized_keyword
                and self._detect_search_like_signal(normalized_keyword)
            )

        return False

    def _detect_follow_up_signal(
        self,
        normalized_text: str,
        quoted_reply: str,
    ) -> str | None:
        if not self._cfg_bool("heuristic_follow_up_enabled", True):
            return None
        if not normalized_text:
            return None

        if len(normalized_text) > self._cfg_int("heuristic_follow_up_max_chars", 20) and not quoted_reply:
            return None

        exact_tokens = set(
            self._cfg_text_items(
                "heuristic_follow_up_keywords",
                DEFAULT_HEURISTIC_FOLLOW_UP_KEYWORDS,
            )
        )
        if normalized_text in exact_tokens:
            return normalized_text
        if not quoted_reply and any(
            normalized_text.startswith(token)
            for token in FOLLOW_UP_QUOTED_ONLY_PREFIX_TOKENS
            if token
        ):
            return None

        prefix_tokens = (
            "继续",
            "那",
            "再",
            "然后",
            "具体",
            "详细",
            "展开",
            "接着",
            "顺便",
            "那这个",
            "这个",
            "为啥",
            "为什么",
            "怎么",
        )
        for token in prefix_tokens:
            if normalized_text.startswith(token):
                return token
        return None

    def _can_reuse_recent_route_for_follow_up(
        self,
        event: AstrMessageEvent,
        normalized_text: str,
        quote_info: ReplyQuoteInfo | None,
        recent_route: dict[str, str | float] | None,
    ) -> bool:
        if not recent_route:
            return False

        if event.is_private_chat():
            return True

        if not self._cfg_bool("heuristic_group_follow_up_strict_enabled", True):
            return True

        if quote_info:
            current_sender_id = str(event.get_sender_id() or "").strip()
            quote_sender_id = str(quote_info.sender_id or "").strip()
            if quote_sender_id and current_sender_id and quote_sender_id != current_sender_id:
                return False
            return True

        max_chars = max(
            self._cfg_int(
                "heuristic_group_follow_up_max_chars_without_quote",
                DEFAULT_HEURISTIC_GROUP_SHORT_FOLLOW_UP_MAX_CHARS,
            ),
            0,
        )
        if max_chars and len(normalized_text) > max_chars:
            return False

        route_timestamp = float(recent_route.get("timestamp", 0.0) or 0.0)
        if not route_timestamp:
            return False

        max_age_seconds = max(
            self._cfg_int(
                "heuristic_group_follow_up_max_age_seconds_without_quote",
                DEFAULT_HEURISTIC_GROUP_SHORT_FOLLOW_UP_MAX_AGE_SECONDS,
            ),
            0,
        )
        if max_age_seconds and time.time() - route_timestamp > max_age_seconds:
            return False

        return True

    def _quoted_reply_heuristic_decision(
        self,
        quote_info: ReplyQuoteInfo | None,
        negative_search_signal: str | None,
    ) -> RouteDecision:
        if not quote_info or not quote_info.text:
            return RouteDecision(None, "no_quoted_reply_match", "heuristic")

        normalized_quote = self._normalize_text(quote_info.text)
        if not normalized_quote:
            return RouteDecision(None, "no_quoted_reply_match", "heuristic")

        quote_search_like_signal = (
            None
            if negative_search_signal
            else self._detect_search_like_signal(normalized_quote)
        )
        if quote_search_like_signal:
            target = self._heuristic_search_route_target()
            if target:
                return RouteDecision(
                    target,
                    f"heuristic:quoted_search_like:{quote_search_like_signal}",
                    "heuristic",
                )

        quote_tertiary_keyword = self._contains_keyword(
            normalized_quote,
            self._route_keywords(TERTIARY_TARGET),
        )
        if quote_tertiary_keyword and self._route_provider_id(TERTIARY_TARGET):
            return RouteDecision(
                TERTIARY_TARGET,
                f"heuristic:quoted_tertiary_keyword:{quote_tertiary_keyword}",
                "heuristic",
            )

        if re.search(r"https?://|www\.", normalized_quote):
            return RouteDecision(PRIMARY_TARGET, "heuristic:quoted_contains_link", "heuristic")

        quote_primary_keyword = self._contains_keyword(
            normalized_quote,
            self._route_keywords(PRIMARY_TARGET),
        )
        if quote_primary_keyword:
            return RouteDecision(
                PRIMARY_TARGET,
                f"heuristic:quoted_primary_keyword:{quote_primary_keyword}",
                "heuristic",
            )

        if self._looks_code_like(quote_info.text):
            return RouteDecision(PRIMARY_TARGET, "heuristic:quoted_code_like", "heuristic")

        return RouteDecision(None, "no_quoted_reply_match", "heuristic")

    def _content_items_to_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, list):
            return str(content or "").strip()

        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                text = str(item).strip()
                if text:
                    parts.append(text)
                continue
            item_type = str(item.get("type", "")).strip().lower()
            if item_type == "text":
                text = str(item.get("text", "")).strip()
                if text:
                    parts.append(text)
            elif item_type == "image_url":
                parts.append("[image]")
            elif item_type in {"input_audio", "audio"}:
                parts.append("[audio]")
            elif item_type == "video":
                parts.append("[video]")
            elif item_type == "file":
                parts.append("[file]")
        return " ".join(parts).strip()

    def _extract_angelheart_context_summary(
        self,
        event: AstrMessageEvent,
    ) -> dict[str, Any]:
        raw_context = getattr(event, "angelheart_context", "")
        if not raw_context:
            return {}
        try:
            context = json.loads(raw_context)
        except (TypeError, ValueError):
            return {}

        decision = context.get("secretary_decision") or {}
        records = context.get("chat_records") or []
        summary_records: list[str] = []

        for record in reversed(records):
            if not isinstance(record, dict):
                continue
            record_text = self._content_items_to_text(record.get("content"))
            if not record_text:
                continue
            sender_name = str(
                record.get("sender_name") or record.get("role") or "unknown"
            ).strip()
            role = str(record.get("role") or "").strip() or "unknown"
            record_text = self._sanitize_context_visible_text(record_text)
            summary_records.append(
                f"{sender_name}({role}): {self._clip_text(record_text, 120)}"
            )
            if len(summary_records) >= CLASSIFIER_CONTEXT_RECORD_LIMIT:
                break

        summary_records.reverse()
        return {
            "reply_strategy": str(decision.get("reply_strategy") or "").strip(),
            "topic": str(decision.get("topic") or "").strip(),
            "reply_target": str(decision.get("reply_target") or "").strip(),
            "needs_search": bool(
                context.get("needs_search", False)
                or decision.get("needs_search", False)
            ),
            "recent_records": summary_records,
        }

    def _recent_route_history_limit(self) -> int:
        return max(
            self._cfg_int("recent_route_history_limit", DEFAULT_RECENT_ROUTE_HISTORY_LIMIT),
            1,
        )

    def _classifier_recent_route_context_limit(self) -> int:
        return max(
            self._cfg_int(
                "classifier_recent_route_context_limit",
                DEFAULT_CLASSIFIER_RECENT_ROUTE_CONTEXT_LIMIT,
            ),
            1,
        )

    def _coerce_recent_route_records(
        self,
        raw_route_data: Any,
    ) -> list[dict[str, str | float]]:
        if isinstance(raw_route_data, dict):
            return [raw_route_data]
        if isinstance(raw_route_data, list):
            return [item for item in raw_route_data if isinstance(item, dict)]
        return []

    def _current_message_id(self, event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        return str(getattr(message_obj, "message_id", "") or "").strip()

    def _recent_route_collapse_enabled(self) -> bool:
        return self._cfg_bool("recent_route_collapse_consecutive_enabled", True)

    def _should_collapse_recent_route_record(
        self,
        new_record: dict[str, str | float],
        existing_record: dict[str, str | float],
    ) -> bool:
        if not self._recent_route_collapse_enabled():
            return False
        new_target = str(new_record.get("target") or "").strip()
        existing_target = str(existing_record.get("target") or "").strip()
        if not new_target or new_target != existing_target:
            return False
        new_sender_id = str(new_record.get("sender_id") or "").strip()
        existing_sender_id = str(existing_record.get("sender_id") or "").strip()
        if new_sender_id and existing_sender_id and new_sender_id != existing_sender_id:
            return False
        return True

    def _get_recent_route_contexts(
        self,
        event: AstrMessageEvent,
        limit: int | None = None,
        same_sender_only: bool = True,
    ) -> list[dict[str, str | float]]:
        raw_routes = self._recent_routes_by_chat.get(event.unified_msg_origin)
        if not raw_routes:
            return []

        records = self._coerce_recent_route_records(raw_routes)
        if not records:
            self._recent_routes_by_chat.pop(event.unified_msg_origin, None)
            return []

        now = time.time()
        valid_records: list[dict[str, str | float]] = []
        for record in records:
            route_timestamp = float(record.get("timestamp", 0.0) or 0.0)
            if route_timestamp and now - route_timestamp > RECENT_ROUTE_TTL_SECONDS:
                continue
            valid_records.append(record)

        if not valid_records:
            self._recent_routes_by_chat.pop(event.unified_msg_origin, None)
            return []

        if valid_records != records:
            self._recent_routes_by_chat[event.unified_msg_origin] = valid_records[
                : self._recent_route_history_limit()
            ]

        if same_sender_only and not event.is_private_chat():
            current_sender = str(event.get_sender_id() or "").strip()
            if not current_sender:
                return []
            valid_records = [
                record
                for record in valid_records
                if str(record.get("sender_id") or "").strip() == current_sender
            ]

        if limit is not None and limit > 0:
            return valid_records[:limit]
        return valid_records

    def _find_recent_route_for_quoted_message(
        self,
        event: AstrMessageEvent,
        quote_info: ReplyQuoteInfo | None,
    ) -> dict[str, str | float] | None:
        if not quote_info or not quote_info.reply_id:
            return None
        for record in self._get_recent_route_contexts(
            event,
            limit=self._recent_route_history_limit(),
            same_sender_only=False,
        ):
            if str(record.get("message_id", "") or "").strip() == quote_info.reply_id:
                return record
        return None

    def _get_recent_route_context(
        self,
        event: AstrMessageEvent,
    ) -> dict[str, str | float] | None:
        records = self._get_recent_route_contexts(event, limit=1)
        return records[0] if records else None

    def _get_sticky_override_context(
        self,
        event: AstrMessageEvent,
    ) -> dict[str, str | float | int] | None:
        sticky = self._sticky_overrides_by_chat.get(event.unified_msg_origin)
        if not sticky:
            return None

        expires_at = float(sticky.get("expires_at", 0.0) or 0.0)
        if expires_at and time.time() > expires_at:
            self._sticky_overrides_by_chat.pop(event.unified_msg_origin, None)
            return None

        remaining_turns = int(sticky.get("remaining_turns", 0) or 0)
        if remaining_turns <= 0:
            self._sticky_overrides_by_chat.pop(event.unified_msg_origin, None)
            return None

        if not event.is_private_chat():
            current_sender = str(event.get_sender_id() or "").strip()
            previous_sender = str(sticky.get("sender_id") or "").strip()
            if not current_sender or current_sender != previous_sender:
                return None

        return sticky

    def _is_strong_route_reason(self, target: str, reason: str) -> bool:
        if not reason:
            return False
        if self._is_force_reason(reason):
            return True
        if target == PRIMARY_TARGET:
            prefixes = (
                "message_has_media",
                "contains_link",
                "primary_keyword:",
                "heuristic:search_to_primary:",
                "code_like_message",
                "heuristic:search_like:",
                "heuristic:angelheart_needs_search",
                *LEGACY_PRIMARY_REASON_PREFIXES,
            )
            return any(reason.startswith(prefix) for prefix in prefixes)
        if target == SECONDARY_TARGET:
            prefixes = (
                "secondary_keyword:",
                *LEGACY_SECONDARY_REASON_PREFIXES,
                "soft_casual_keyword:",
            )
            return any(reason.startswith(prefix) for prefix in prefixes)
        if target == TERTIARY_TARGET:
            prefixes = (
                "tertiary_keyword:",
                "heuristic:search_like:",
                "heuristic:angelheart_needs_search",
            )
            return any(reason.startswith(prefix) for prefix in prefixes)
        return False

    def _sticky_break_reason(
        self,
        sticky_target: str,
        rules_decision: RouteDecision,
    ) -> str | None:
        if rules_decision.target is None or rules_decision.target == sticky_target:
            return None

        reason = rules_decision.reason or ""
        if self._is_strong_route_reason(rules_decision.target, reason):
            return reason
        return None

    def _is_force_reason(self, reason: str) -> bool:
        return any(reason.startswith(prefix) for prefix in STICKY_FORCE_REASON_PREFIXES)

    def _decision_reason_anchor(self, reason: str) -> str:
        anchor = str(reason or "").strip()
        if "|after:" in anchor:
            anchor = anchor.split("|after:", 1)[0]
        if "|" in anchor:
            anchor = anchor.split("|", 1)[0]
        return anchor.strip()

    def _decision_base_path(self, reason: str, source: str) -> str:
        anchor = self._decision_reason_anchor(reason)
        normalized_source = str(source or "unknown").strip() or "unknown"

        if not anchor:
            return f"{normalized_source}.unknown"

        if self._is_force_reason(anchor):
            forced_target = self._force_target_from_reason(anchor)
            if forced_target:
                return f"rules.force_{forced_target}_regex"
            return "rules.force_regex"

        if anchor == "message_has_media":
            return "rules.message_has_media"
        if anchor == "contains_link":
            return "rules.contains_link"
        if anchor == "command_like_message":
            return "rules.command_like_message"
        if anchor == "code_like_message":
            return "rules.code_like_message"
        if anchor.startswith("primary_keyword:"):
            return "rules.primary_keyword"
        if anchor.startswith("secondary_keyword:"):
            return "rules.secondary_keyword"
        if anchor.startswith("tertiary_keyword:"):
            return "rules.tertiary_keyword"
        if anchor.startswith("soft_casual_keyword:"):
            return "rules.soft_casual_keyword"
        if anchor == "heuristic:angelheart_needs_search":
            return "heuristic.angelheart_needs_search"
        if anchor.startswith("heuristic:search_to_primary:"):
            return "heuristic.search_to_primary"
        if anchor.startswith("heuristic:search_like:"):
            return "heuristic.search_like"
        if anchor.startswith("heuristic:quoted_route_history:"):
            return "heuristic.quoted_route_history"
        if anchor.startswith("heuristic:quoted_search_like:"):
            return "heuristic.quoted_search_like"
        if anchor.startswith("heuristic:quoted_tertiary_keyword:"):
            return "heuristic.quoted_tertiary_keyword"
        if anchor == "heuristic:quoted_contains_link":
            return "heuristic.quoted_contains_link"
        if anchor.startswith("heuristic:quoted_primary_keyword:"):
            return "heuristic.quoted_primary_keyword"
        if anchor == "heuristic:quoted_code_like":
            return "heuristic.quoted_code_like"
        if anchor.startswith("heuristic:follow_up_recent:"):
            return "heuristic.follow_up_recent"
        if anchor == "no_heuristic_match":
            return "heuristic.no_match"
        if anchor == "no_quoted_reply_match":
            return "heuristic.no_quoted_reply_match"
        if anchor.startswith("sticky_override:"):
            return "sticky.override"
        if anchor.startswith("classifier:"):
            return "llm.classifier"
        if anchor.startswith("uncertain_route:"):
            return "fallback.uncertain_route"

        safe_anchor = re.sub(r"[^a-z0-9._-]+", "_", anchor.lower()).strip("_")
        if not safe_anchor:
            safe_anchor = "unknown"
        return f"{normalized_source}.{safe_anchor}"

    def _decision_path_summary(self, reason: str, source: str) -> str:
        path = self._decision_base_path(reason, source)
        raw_reason = str(reason or "").strip()
        if "|after:" not in raw_reason:
            return path

        after_part = raw_reason.split("|after:", 1)[1].strip()
        after_source, separator, after_reason = after_part.partition(":")
        if not separator or not after_reason:
            return path

        after_path = self._decision_base_path(after_reason, after_source)
        if not after_path or after_path == path:
            return path
        return f"{path}<-{after_path}"

    def _decision_family(self, reason: str, source: str) -> str:
        path = self._decision_path_summary(reason, source).split("<-", 1)[0]

        if path.startswith("rules.force_"):
            return "force_directive"
        if path == "rules.message_has_media":
            return "media"
        if path == "rules.contains_link":
            return "link"
        if path in {
            "rules.primary_keyword",
            "rules.secondary_keyword",
            "rules.tertiary_keyword",
            "rules.soft_casual_keyword",
        }:
            return "lane_keyword"
        if path == "rules.code_like_message":
            return "technical_signal"
        if path == "rules.command_like_message":
            return "command_like"
        if path in {
            "heuristic.angelheart_needs_search",
            "heuristic.search_like",
            "heuristic.search_to_primary",
        }:
            return "search_signal"
        if path == "heuristic.quoted_route_history":
            return "quote_history"
        if path.startswith("heuristic.quoted_"):
            return "quote_signal"
        if path == "heuristic.follow_up_recent":
            return "follow_up"
        if path == "sticky.override":
            return "sticky"
        if path == "llm.classifier":
            return "classifier"
        if path == "fallback.uncertain_route":
            return "fallback"
        if path in {
            "heuristic.no_match",
            "heuristic.no_quoted_reply_match",
        }:
            return "no_match"
        if path.startswith("rules."):
            return "rules"
        if path.startswith("heuristic."):
            return "heuristic"
        return str(source or "other").strip() or "other"

    def _release_sticky_override(
        self,
        event: AstrMessageEvent,
        reason: str,
    ) -> None:
        sticky = self._sticky_overrides_by_chat.pop(event.unified_msg_origin, None)
        if not sticky:
            return
        logger.info(
            "[provider_router] sticky released | target=%s | reason=%s | chat=%s",
            sticky.get("target", "keep_default"),
            reason,
            event.unified_msg_origin,
        )

    def _build_sticky_decision(
        self,
        event: AstrMessageEvent,
        sticky: dict[str, str | float | int],
    ) -> RouteDecision:
        remaining_turns = int(sticky.get("remaining_turns", 0) or 0)
        total_turns = int(sticky.get("total_turns", remaining_turns) or remaining_turns)
        armed_reason = str(sticky.get("armed_reason") or "explicit_override")
        decision = RouteDecision(
            str(sticky.get("target") or "keep_default"),
            (
                "sticky_override:"
                f"{armed_reason}|remaining_before={remaining_turns}|total={total_turns}"
            ),
            "sticky",
        )

        if remaining_turns <= 1:
            self._sticky_overrides_by_chat.pop(event.unified_msg_origin, None)
        else:
            sticky["remaining_turns"] = remaining_turns - 1
            self._sticky_overrides_by_chat[event.unified_msg_origin] = sticky

        return decision

    def _arm_sticky_override(
        self,
        event: AstrMessageEvent,
        decision: RouteDecision,
    ) -> None:
        if not self._cfg_bool("sticky_override_enabled", True):
            return
        if decision.target not in self._enabled_route_targets():
            return
        if not any(
            decision.reason.startswith(prefix)
            for prefix in STICKY_FORCE_REASON_PREFIXES
        ):
            return

        sticky_rounds = max(self._cfg_int("sticky_override_rounds", 3), 0)
        if sticky_rounds <= 0:
            self._sticky_overrides_by_chat.pop(event.unified_msg_origin, None)
            return

        ttl_seconds = max(self._cfg_int("sticky_override_ttl_seconds", 600), 0)
        expires_at = time.time() + ttl_seconds if ttl_seconds > 0 else 0.0
        self._sticky_overrides_by_chat[event.unified_msg_origin] = {
            "target": decision.target,
            "sender_id": event.get_sender_id(),
            "armed_reason": decision.reason,
            "remaining_turns": sticky_rounds,
            "total_turns": sticky_rounds,
            "expires_at": expires_at,
            "timestamp": time.time(),
        }
        logger.info(
            "[provider_router] sticky armed | target=%s | rounds=%s | ttl=%ss | reason=%s | chat=%s",
            decision.target,
            sticky_rounds,
            ttl_seconds,
            decision.reason,
            event.unified_msg_origin,
        )

    def _build_classifier_prompt(self, event: AstrMessageEvent, text: str) -> str:
        sanitized_text = self._sanitize_context_visible_text(text)
        normalized = self._normalize_text(sanitized_text)
        outline = self._sanitize_context_visible_text(
            (event.get_message_outline() or "").strip()
        )
        quote_info = self._extract_reply_quote_info(event)
        quoted_reply = quote_info.text if quote_info else ""
        recent_routes = self._get_recent_route_contexts(
            event,
            limit=self._classifier_recent_route_context_limit(),
        )
        recent_route = recent_routes[0] if recent_routes else None
        angel_context = self._extract_angelheart_context_summary(event)
        search_like_signal = self._detect_search_like_signal(normalized)
        negative_search_signal = self._detect_negative_search_signal(normalized)
        search_to_primary_signal = (
            None if negative_search_signal else self._detect_search_to_primary_signal(normalized)
        )
        follow_up_signal = self._detect_follow_up_signal(normalized, quoted_reply)
        follow_up_like = bool(quoted_reply) or (
            bool(recent_route) and len(normalized) <= 48
        )

        lines = [
            f"platform={event.get_platform_name()}",
            f"private_chat={event.is_private_chat()}",
            f"has_media={self._message_has_media(event)}",
            f"follow_up_like={follow_up_like}",
            f"search_like_signal={search_like_signal or ''}",
            f"negative_search_signal={negative_search_signal or ''}",
            f"search_to_primary_signal={search_to_primary_signal or ''}",
            f"follow_up_signal={follow_up_signal or ''}",
            f"message={self._clip_text(sanitized_text or outline, 220)}",
        ]

        if outline and outline != sanitized_text:
            lines.append(f"message_outline={self._clip_text(outline, 320)}")
        if quoted_reply:
            lines.append(f"quoted_reply={self._clip_text(quoted_reply, 220)}")
        if quote_info and quote_info.sender_id:
            current_sender_id = str(event.get_sender_id() or "").strip()
            lines.append(
                f"quoted_reply_sender_matches_current={quote_info.sender_id == current_sender_id}"
            )
        quoted_route_record = self._find_recent_route_for_quoted_message(event, quote_info)
        if quoted_route_record:
            lines.append(
                f"quoted_reply_route_target={quoted_route_record.get('target', 'keep_default')}"
            )
            lines.append(
                f"quoted_reply_route_reason={self._clip_text(str(quoted_route_record.get('reason', '')), 120)}"
            )

        if recent_route:
            lines.append(
                f"previous_route_target={recent_route.get('target', 'keep_default')}"
            )
            lines.append(
                f"previous_route_reason={self._clip_text(str(recent_route.get('reason', '')), 120)}"
            )
            previous_text = str(recent_route.get("text_preview") or "").strip()
            if previous_text:
                lines.append(
                    f"previous_route_text={self._clip_text(previous_text, 120)}"
                )
            repeat_count = int(recent_route.get("repeat_count", 1) or 1)
            if repeat_count > 1:
                lines.append(f"previous_route_repeat_count={repeat_count}")
        for idx, route_record in enumerate(recent_routes[1:], start=2):
            lines.append(
                f"previous_route_{idx}_target={route_record.get('target', 'keep_default')}"
            )
            lines.append(
                f"previous_route_{idx}_reason={self._clip_text(str(route_record.get('reason', '')), 120)}"
            )
            route_text = str(route_record.get("text_preview") or "").strip()
            if route_text:
                lines.append(
                    f"previous_route_{idx}_text={self._clip_text(route_text, 120)}"
                )
            repeat_count = int(route_record.get("repeat_count", 1) or 1)
            if repeat_count > 1:
                lines.append(f"previous_route_{idx}_repeat_count={repeat_count}")

        if angel_context:
            reply_strategy = angel_context.get("reply_strategy", "")
            topic = angel_context.get("topic", "")
            reply_target = angel_context.get("reply_target", "")
            if reply_strategy:
                lines.append(
                    f"angelheart_reply_strategy={self._clip_text(reply_strategy, 80)}"
                )
            if topic:
                lines.append(f"angelheart_topic={self._clip_text(topic, 120)}")
            if reply_target:
                lines.append(
                    f"angelheart_reply_target={self._clip_text(reply_target, 80)}"
                )
            lines.append(
                f"angelheart_needs_search={angel_context.get('needs_search', False)}"
            )
            for idx, record in enumerate(
                angel_context.get("recent_records", []),
                start=1,
            ):
                lines.append(f"recent_context_{idx}={record}")

        return "\n".join(lines)

    def _classifier_system_prompt(self) -> str:
        custom = self._cfg_str("classifier_system_prompt")
        tertiary_enabled = bool(self._route_provider_id(TERTIARY_TARGET))
        if custom and (custom != DEFAULT_CLASSIFIER_SYSTEM_PROMPT or not tertiary_enabled):
            return custom

        if tertiary_enabled:
            return (
                "你是 AstrBot 的消息路由器。\n"
                "只返回一个词：primary、secondary、tertiary 或 keep_default。\n"
                "secondary 适合日常闲聊、寒暄、短跟进、轻陪伴。\n"
                "primary 适合工具、代码、配置、截图/图片分析、联网检索、严谨分析与专业任务。\n"
                "tertiary 适合第三路由承担的实验性路线，例如搜索优先、热点发散、另一种风格化对话。\n"
                "只有真的判断不出来时才返回 keep_default。\n"
                "不要解释，不要输出其他内容。"
            )
        return custom or DEFAULT_CLASSIFIER_SYSTEM_PROMPT

    def _event_allowed(self, event: AstrMessageEvent) -> bool:
        if not self._cfg_bool("enabled", True):
            return False
        if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
            return False
        if event.is_private_chat():
            return self._cfg_bool("allow_private", True)
        return self._cfg_bool("allow_group", True)

    def _should_respect_existing_selection(self, event: AstrMessageEvent) -> bool:
        if not self._cfg_bool("honor_existing_selection", True):
            return False
        return bool(event.get_extra("selected_provider")) or bool(
            event.get_extra("selected_model")
        )

    def _rules_decision(self, event: AstrMessageEvent, text: str) -> RouteDecision:
        normalized = self._normalize_text(text)
        has_media = self._message_has_media(event)

        if has_media and self._cfg_bool_any(("route_media_to_primary", "route_media_to_gpt"), True):
            return RouteDecision(PRIMARY_TARGET, "message_has_media", "rules")

        if self._looks_command_like(text):
            return RouteDecision(None, "command_like_message", "rules")

        force_reason = self._detect_force_directive_reason(text)
        force_target = self._force_target_from_reason(force_reason)
        if force_reason and force_target:
            return RouteDecision(force_target, force_reason, "rules")

        if self._cfg_bool_any(("route_links_to_primary", "route_links_to_gpt"), True) and re.search(
            r"https?://|www\.",
            normalized,
        ):
            return RouteDecision(PRIMARY_TARGET, "contains_link", "rules")

        primary_keyword = self._contains_keyword(
            normalized,
            self._route_keywords(PRIMARY_TARGET),
        )
        if primary_keyword:
            return RouteDecision(
                PRIMARY_TARGET,
                f"primary_keyword:{primary_keyword}",
                "rules",
            )

        if self._looks_code_like(text):
            return RouteDecision(PRIMARY_TARGET, "code_like_message", "rules")

        secondary_keyword = self._contains_keyword(
            normalized,
            self._route_keywords(SECONDARY_TARGET),
        )
        if secondary_keyword:
            return RouteDecision(
                SECONDARY_TARGET,
                f"secondary_keyword:{secondary_keyword}",
                "rules",
            )

        tertiary_keyword = self._contains_keyword(
            normalized,
            self._route_keywords(TERTIARY_TARGET),
        )
        if tertiary_keyword:
            return RouteDecision(
                TERTIARY_TARGET,
                f"tertiary_keyword:{tertiary_keyword}",
                "rules",
            )

        if len(normalized) <= 24 and re.fullmatch(r"[\w\s\u4e00-\u9fff?!,.~]+", normalized):
            soft_casual = self._contains_keyword(
                normalized,
                [
                    "hi",
                    "hello",
                    "hey",
                    "你好",
                    "哈喽",
                    "在吗",
                    "早安",
                    "晚安",
                    "谢谢",
                    "哈哈",
                    "hh",
                    "233",
                    "抱抱",
                    "夸夸我",
                    "安慰我",
                    "陪我聊",
                ],
            )
            if soft_casual:
                return RouteDecision(
                    SECONDARY_TARGET,
                    f"soft_casual_keyword:{soft_casual}",
                    "rules",
                )

        return RouteDecision(None, "no_rule_match", "rules")

    def _heuristic_decision(
        self,
        event: AstrMessageEvent,
        text: str,
    ) -> RouteDecision:
        normalized = self._normalize_text(text)
        quote_info = self._extract_reply_quote_info(event)
        quoted_reply = quote_info.text if quote_info else ""
        recent_route = self._get_recent_route_context(event)
        angel_context = self._extract_angelheart_context_summary(event)
        negative_search_signal = self._detect_negative_search_signal(normalized)
        search_to_primary_signal = (
            None if negative_search_signal else self._detect_search_to_primary_signal(normalized)
        )

        if angel_context.get("needs_search", False) and not negative_search_signal:
            target = self._heuristic_search_route_target()
            if target:
                return RouteDecision(target, "heuristic:angelheart_needs_search", "heuristic")

        search_like_signal = None if negative_search_signal else self._detect_search_like_signal(normalized)
        if search_like_signal:
            if search_to_primary_signal:
                return RouteDecision(
                    PRIMARY_TARGET,
                    f"heuristic:search_to_primary:{search_like_signal}|{search_to_primary_signal}",
                    "heuristic",
                )
            target = self._heuristic_search_route_target()
            if target:
                return RouteDecision(
                    target,
                    f"heuristic:search_like:{search_like_signal}",
                    "heuristic",
                )

        quoted_route_record = self._find_recent_route_for_quoted_message(event, quote_info)
        if quoted_route_record:
            quoted_target = self._normalize_route_target(quoted_route_record.get("target"))
            if quoted_target in self._enabled_route_targets():
                if negative_search_signal and self._reason_implies_search_routing(
                    str(quoted_route_record.get("reason") or "")
                ):
                    return RouteDecision(None, "no_heuristic_match", "heuristic")
                return RouteDecision(
                    quoted_target,
                    f"heuristic:quoted_route_history:{quoted_target}",
                    "heuristic",
                )

        quoted_decision = self._quoted_reply_heuristic_decision(
            quote_info,
            negative_search_signal,
        )
        if quoted_decision.target is not None:
            return quoted_decision

        follow_up_signal = self._detect_follow_up_signal(normalized, quoted_reply)
        if (
            recent_route
            and follow_up_signal
            and self._can_reuse_recent_route_for_follow_up(
                event,
                normalized,
                quote_info,
                recent_route,
            )
        ):
            recent_target = self._normalize_route_target(recent_route.get("target"))
            if negative_search_signal and self._reason_implies_search_routing(
                str(recent_route.get("reason") or "")
            ):
                return RouteDecision(None, "no_heuristic_match", "heuristic")
            if recent_target in self._enabled_route_targets():
                recent_reason = str(recent_route.get("reason") or "").strip()
                recent_source = str(recent_route.get("source") or "unknown").strip() or "unknown"
                after_suffix = (
                    f"|after:{recent_source}:{recent_reason}"
                    if recent_reason
                    else ""
                )
                return RouteDecision(
                    recent_target,
                    f"heuristic:follow_up_recent:{recent_target}:{follow_up_signal}{after_suffix}",
                    "heuristic",
                )

        return RouteDecision(None, "no_heuristic_match", "heuristic")

    async def _llm_classifier_decision(
        self,
        event: AstrMessageEvent,
        text: str,
    ) -> RouteDecision:
        provider_id = self._cfg_str("classifier_provider_id")
        if not provider_id:
            return RouteDecision(None, "classifier_provider_missing", "llm")

        provider = self.context.get_provider_by_id(provider_id)
        if not provider:
            logger.warning(
                "[provider_router] classifier provider not found: %s",
                provider_id,
            )
            return RouteDecision(None, "classifier_provider_not_found", "llm")
        if not isinstance(provider, Provider):
            logger.warning(
                "[provider_router] classifier provider has invalid type: %s",
                type(provider),
            )
            return RouteDecision(None, "classifier_provider_invalid_type", "llm")

        kwargs = {
            "prompt": self._build_classifier_prompt(event, text),
            "session_id": uuid.uuid4().hex,
            "system_prompt": self._classifier_system_prompt(),
            "persist": False,
        }
        try:
            response = await provider.text_chat(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[provider_router] classifier request failed: %s", exc)
            return RouteDecision(None, "classifier_request_failed", "llm")

        raw = (response.completion_text or "").strip().lower()
        token_match = re.search(
            r"\b(primary|secondary|tertiary|gpt|gemini|grok|third|keep_default)\b",
            raw,
        )
        if token_match:
            token = token_match.group(1)
            if token == "keep_default":
                return RouteDecision(None, "classifier_keep_default", "llm")
            target = self._normalize_route_target(token)
            if target:
                return RouteDecision(target, f"classifier:{token}", "llm")

        logger.warning(
            "[provider_router] classifier returned unexpected content: %r",
            raw,
        )
        return RouteDecision(None, "classifier_unexpected_output", "llm")

    def _fallback_decision(self) -> RouteDecision:
        fallback = self._normalize_route_target(
            self._cfg_str("uncertain_route", "keep_default")
        )
        if fallback in self._enabled_route_targets() and self._route_provider_id(fallback):
            return RouteDecision(fallback, f"uncertain_route:{fallback}", "fallback")
        return RouteDecision(None, "uncertain_route:keep_default", "fallback")

    def _compose_fallback_decision(
        self,
        prior_decision: RouteDecision | None,
    ) -> RouteDecision:
        fallback = self._fallback_decision()
        if (
            prior_decision
            and prior_decision.reason
            and prior_decision.reason != fallback.reason
        ):
            return RouteDecision(
                fallback.target,
                f"{fallback.reason}|after:{prior_decision.source}:{prior_decision.reason}",
                "fallback",
            )
        return fallback

    def _build_routing_outcome(
        self,
        decision: RouteDecision,
        applied: bool,
        effective_provider_id: str = "",
    ) -> RoutingOutcome:
        provider_id = (
            effective_provider_id
            or (self._route_provider_id(decision.target) if decision.target else "")
        )
        return RoutingOutcome(
            target=decision.target,
            provider_id=provider_id,
            reason=decision.reason,
            source=decision.source,
            applied=applied,
            used_sticky=decision.source == "sticky",
            used_fallback=decision.source == "fallback",
            used_force_directive=self._is_force_reason(decision.reason),
        )

    def _apply_route(self, event: AstrMessageEvent, decision: RouteDecision) -> bool:
        if decision.target not in self._enabled_route_targets():
            return False

        provider_id = self._route_provider_id(decision.target)

        applied = False
        if provider_id:
            event.set_extra("selected_provider", provider_id)
            event.set_extra("selected_model", "")
            applied = True

        if not applied:
            logger.info(
                "[provider_router] matched %s but provider_id is empty, keep default provider",
                decision.target,
            )
        return applied

    def _store_decision(self, event: AstrMessageEvent, outcome: RoutingOutcome) -> None:
        decision_family = self._decision_family(outcome.reason, outcome.source)
        decision_path = self._decision_path_summary(outcome.reason, outcome.source)
        event.set_extra(
            ROUTE_DECISION_KEY,
            {
                "target": outcome.target or "keep_default",
                "provider_id": outcome.provider_id,
                "reason": outcome.reason,
                "source": outcome.source,
                "decision_family": decision_family,
                "decision_path": decision_path,
                "applied": outcome.applied,
                "used_sticky": outcome.used_sticky,
                "used_fallback": outcome.used_fallback,
                "used_force_directive": outcome.used_force_directive,
            },
        )

    def _remember_route(
        self,
        event: AstrMessageEvent,
        text: str,
        outcome: RoutingOutcome,
    ) -> None:
        if outcome.target not in self._enabled_route_targets() or not outcome.applied:
            return
        preview = text.replace("\n", " ").strip()
        if len(preview) > 80:
            preview = preview[:77] + "..."
        tool_profile = event.get_extra(TOOL_PROFILE_KEY, {}) or {}
        record = {
            "target": outcome.target,
            "provider_id": outcome.provider_id,
            "reason": outcome.reason,
            "source": outcome.source,
            "sender_id": event.get_sender_id(),
            "message_id": self._current_message_id(event),
            "text_preview": preview,
            "timestamp": time.time(),
            "repeat_count": 1,
        }
        if isinstance(tool_profile, dict):
            record["tool_profile_kind"] = str(
                tool_profile.get("profile_kind") or ""
            ).strip()
            record["tool_mode"] = str(tool_profile.get("tool_mode") or "").strip()
            record["task_reason"] = str(tool_profile.get("reason") or "").strip()
            record["effective_provider_id"] = str(
                tool_profile.get("effective_provider_id") or ""
            ).strip()
        existing = self._coerce_recent_route_records(
            self._recent_routes_by_chat.get(event.unified_msg_origin)
        )
        history: list[dict[str, str | float]]
        if existing and self._should_collapse_recent_route_record(record, existing[0]):
            collapsed_head = {
                **existing[0],
                **record,
                "repeat_count": int(existing[0].get("repeat_count", 1) or 1) + 1,
            }
            history = [collapsed_head, *existing[1:]]
        else:
            history = [record, *existing]
        self._recent_routes_by_chat[event.unified_msg_origin] = history[
            : self._recent_route_history_limit()
        ]

    def _log_decision(
        self,
        event: AstrMessageEvent,
        text: str,
        outcome: RoutingOutcome,
        tool_profile: LaneToolProfile | None = None,
    ) -> None:
        if not self._cfg_bool("log_decisions", True):
            return
        preview = text.replace("\n", " ").strip()
        if len(preview) > 80:
            preview = preview[:77] + "..."
        decision_family = self._decision_family(outcome.reason, outcome.source)
        decision_path = self._decision_path_summary(outcome.reason, outcome.source)
        logger.info(
            "[provider_router] lane=%s | provider=%s | applied=%s | family=%s | path=%s | source=%s | reason=%s | tool_profile=%s | tool_mode=%s | task_reason=%s | sticky=%s | fallback=%s | force=%s | chat=%s | text=%r",
            outcome.target or "keep_default",
            outcome.provider_id or "<default>",
            outcome.applied,
            decision_family,
            decision_path,
            outcome.source,
            outcome.reason,
            tool_profile.profile_kind if tool_profile else "disabled",
            tool_profile.tool_mode if tool_profile else TOOL_MODE_FULL,
            tool_profile.reason if tool_profile else "",
            outcome.used_sticky,
            outcome.used_fallback,
            outcome.used_force_directive,
            event.unified_msg_origin,
            preview,
        )

    @filter.on_waiting_llm_request(priority=120)
    async def route_provider(self, event: AstrMessageEvent) -> None:
        if not self._event_allowed(event):
            return
        if self._should_respect_existing_selection(event):
            return

        text = (event.get_message_str() or "").strip()
        if not text and not self._message_has_media(event):
            return
        if self._looks_command_like(text):
            decision = RouteDecision(None, "command_like_message", "rules")
            outcome = self._build_routing_outcome(decision, applied=False)
            self._store_tool_profile(event, None)
            self._store_decision(event, outcome)
            self._log_decision(event, text, outcome)
            return

        strip_reason = self._detect_force_directive_reason(text)
        effective_text = (
            self._strip_force_directive_by_reason(text, strip_reason)
            if strip_reason
            else text
        )
        if strip_reason:
            self._rewrite_event_prompt_text(event, text, effective_text, strip_reason)

        sticky = self._get_sticky_override_context(event)
        if sticky:
            sticky_target = self._normalize_route_target(sticky.get("target"))
            if sticky_target in self._enabled_route_targets():
                force_target = self._force_target_from_reason(strip_reason) if strip_reason else None
                if force_target and force_target == sticky_target:
                    self._arm_sticky_override(
                        event,
                        RouteDecision(force_target, strip_reason, "rules"),
                    )
                    refreshed_sticky = self._get_sticky_override_context(event)
                    if refreshed_sticky:
                        sticky = refreshed_sticky
                sticky_probe = self._rules_decision(event, text)
                sticky_break_reason = self._sticky_break_reason(
                    sticky_target,
                    sticky_probe,
                )
                allow_release = self._cfg_bool("sticky_release_on_opposite_signal", True)
                if sticky_break_reason and (
                    allow_release or self._is_force_reason(sticky_break_reason)
                ):
                    self._release_sticky_override(
                        event,
                        f"opposite_signal:{sticky_break_reason}",
                    )
                else:
                    decision = self._build_sticky_decision(event, sticky)
                    applied = self._apply_route(event, decision)
                    tool_profile = self._build_lane_tool_profile(
                        event,
                        effective_text,
                        decision,
                        applied,
                    )
                    if tool_profile and tool_profile.effective_provider_id:
                        event.set_extra(
                            "selected_provider",
                            tool_profile.effective_provider_id,
                        )
                        event.set_extra("selected_model", "")
                    outcome = self._build_routing_outcome(
                        decision,
                        applied,
                        tool_profile.effective_provider_id if tool_profile else "",
                    )
                    self._store_tool_profile(event, tool_profile)
                    self._store_decision(event, outcome)
                    self._remember_route(event, effective_text, outcome)
                    self._log_decision(event, text, outcome, tool_profile)
                    return

        decision = self._rules_decision(event, text)
        heuristic_decision: RouteDecision | None = None
        classifier_decision: RouteDecision | None = None
        if (
            decision.target is None
            and decision.reason == "no_rule_match"
        ):
            heuristic_decision = self._heuristic_decision(event, text)
            decision = heuristic_decision

        if (
            decision.target is None
            and decision.reason == "no_heuristic_match"
            and self._cfg_str("classifier_mode", "rules_only") == "rules_then_llm"
        ):
            classifier_decision = await self._llm_classifier_decision(event, text)
            decision = classifier_decision

        if decision.target is None:
            decision = self._compose_fallback_decision(
                classifier_decision or heuristic_decision or decision
            )

        applied = self._apply_route(event, decision)
        tool_profile = self._build_lane_tool_profile(
            event,
            effective_text,
            decision,
            applied,
        )
        if tool_profile and tool_profile.effective_provider_id:
            event.set_extra("selected_provider", tool_profile.effective_provider_id)
            event.set_extra("selected_model", "")
        outcome = self._build_routing_outcome(
            decision,
            applied,
            tool_profile.effective_provider_id if tool_profile else "",
        )
        self._store_tool_profile(event, tool_profile)
        self._store_decision(event, outcome)
        self._remember_route(event, effective_text, outcome)
        self._arm_sticky_override(event, decision)
        self._log_decision(event, text, outcome, tool_profile)

    @filter.on_llm_request(priority=-10001)
    async def sanitize_llm_request_prompt(self, event: AstrMessageEvent, req: Any) -> None:
        original_text = str(event.get_extra("_provider_router_original_message_str", "") or "")
        rewritten_text = str(event.get_extra("_provider_router_rewritten_message_str", "") or "")
        reason = str(event.get_extra("_provider_router_strip_reason", "") or "")

        if original_text and rewritten_text and reason:
            self._sanitize_request_prompt(req, original_text, rewritten_text, reason)
        self._sanitize_force_directives_in_request_prompt(req)
        self._sanitize_route_reply_prefixes_in_request_prompt(req)
        self._sanitize_request_contexts(req)
        self._sanitize_request_extra_user_content_parts(req)
        await self._apply_route_persona_override(event, req)
        self._apply_route_tool_profile(event, req)

    @filter.on_decorating_result(priority=20)
    async def decorate_reply_with_route_label(self, event: AstrMessageEvent) -> None:
        if not self._cfg_bool("enabled", True):
            return

        result = event.get_result()
        if result is None or not result.chain or not result.is_model_result():
            return

        prefix = self._build_route_reply_prefix(event)
        if not prefix:
            return

        for comp in result.chain:
            if not isinstance(comp, Comp.Plain):
                continue
            if comp.text.startswith(prefix):
                return
            comp.text = f"{prefix}{comp.text}"
            return
