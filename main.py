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


PLUGIN_NAME = "astrbot_plugin_provider_router"
ROUTE_DECISION_KEY = "_provider_router_decision"
RECENT_ROUTE_TTL_SECONDS = 1800
CLASSIFIER_CONTEXT_RECORD_LIMIT = 4
PRIMARY_TARGET = "primary"
SECONDARY_TARGET = "secondary"
ROUTE_TARGETS = (PRIMARY_TARGET, SECONDARY_TARGET)
TARGET_ALIAS_MAP = {
    "gpt": PRIMARY_TARGET,
    "primary": PRIMARY_TARGET,
    "gemini": SECONDARY_TARGET,
    "secondary": SECONDARY_TARGET,
}
FORCE_PRIMARY_REGEX_REASON_PREFIX = "force_primary_regex:"
FORCE_SECONDARY_REGEX_REASON_PREFIX = "force_secondary_regex:"
STICKY_FORCE_REASON_PREFIXES = (
    FORCE_PRIMARY_REGEX_REASON_PREFIX,
    FORCE_SECONDARY_REGEX_REASON_PREFIX,
)
# Keep these legacy reason prefixes in the sticky-break lists so sessions
# created before the 0.2.0 public schema cleanup can still release correctly.
LEGACY_PRIMARY_REASON_PREFIXES = (
    "tool_intent_keyword:",
    "professional_keyword:",
)
LEGACY_SECONDARY_REASON_PREFIXES = ("casual_keyword:",)
SECONDARY_STICKY_BREAK_REASON_PREFIXES = (
    FORCE_PRIMARY_REGEX_REASON_PREFIX,
    "message_has_media",
    "contains_link",
    "primary_keyword:",
    "code_like_message",
    *LEGACY_PRIMARY_REASON_PREFIXES,
)
PRIMARY_STICKY_BREAK_REASON_PREFIXES = (
    FORCE_SECONDARY_REGEX_REASON_PREFIX,
    "secondary_keyword:",
    *LEGACY_SECONDARY_REASON_PREFIXES,
    "soft_casual_keyword:",
)
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


@dataclass
class RouteDecision:
    target: str | None
    reason: str
    source: str


@register(
    PLUGIN_NAME,
    "AnegasakiNene",
    "Route messages between two configurable provider targets.",
    "0.2.0",
)
class ProviderRouterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._recent_routes_by_chat: dict[str, dict[str, str | float]] = {}
        self._sticky_overrides_by_chat: dict[str, dict[str, str | float | int]] = {}
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

    def _normalize_route_target(self, value: object) -> str | None:
        token = str(value or "").strip().lower()
        if not token or token == "keep_default":
            return None
        return TARGET_ALIAS_MAP.get(token)

    def _force_reason_prefix(self, target: str, kind: str) -> str:
        if target == PRIMARY_TARGET:
            return FORCE_PRIMARY_REGEX_REASON_PREFIX
        if target == SECONDARY_TARGET:
            return FORCE_SECONDARY_REGEX_REASON_PREFIX
        return ""

    def _force_target_from_reason(self, reason: str) -> str | None:
        if reason.startswith(FORCE_PRIMARY_REGEX_REASON_PREFIX):
            return PRIMARY_TARGET
        if reason.startswith(FORCE_SECONDARY_REGEX_REASON_PREFIX):
            return SECONDARY_TARGET
        return None

    def _route_provider_id(self, target: str) -> str:
        if target == PRIMARY_TARGET:
            return self._cfg_str_any(("primary_provider_id", "gpt_provider_id"))
        if target == SECONDARY_TARGET:
            return self._cfg_str_any(("secondary_provider_id", "gemini_provider_id"))
        return ""

    def _route_persona_id(self, target: str) -> str:
        if target == PRIMARY_TARGET:
            return self._cfg_str_any(("primary_persona_id", "gpt_persona_id"))
        if target == SECONDARY_TARGET:
            return self._cfg_str_any(("secondary_persona_id", "gemini_persona_id"))
        return ""

    def _route_keywords(self, target: str) -> list[str]:
        if target == PRIMARY_TARGET:
            explicit = self._cfg_str("primary_route_keywords")
            legacy_keys = ("tool_intent_keywords", "professional_keywords")
        elif target == SECONDARY_TARGET:
            explicit = self._cfg_str("secondary_route_keywords")
            legacy_keys = ("casual_keywords",)
        else:
            return []

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
        return items

    def _force_regex_patterns(self, target: str) -> list[re.Pattern[str]]:
        if target == PRIMARY_TARGET:
            return self._compile_regex_list(
                self.config.get("force_primary_regex", "")
                or self.config.get("force_gpt_regex", "")
            )
        if target == SECONDARY_TARGET:
            return self._compile_regex_list(
                self.config.get("force_secondary_regex", "")
                or self.config.get("force_gemini_regex", "")
            )
        return []

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

        for target in ROUTE_TARGETS:
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
        patterns: list[re.Pattern[str]] = []
        for target in ROUTE_TARGETS:
            patterns.extend(self._force_regex_patterns(target))
        return patterns

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
        prefixes: list[str] = []
        seen: set[str] = set()
        for target in ROUTE_TARGETS:
            label = self._get_route_prefix_label(target)
            if not label:
                continue
            prefix = f"\u300e{label}\u300f"
            if prefix in seen:
                continue
            seen.add(prefix)
            prefixes.append(prefix)
        return prefixes

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

    def _detect_target_from_actual_provider(self, event: AstrMessageEvent) -> str | None:
        # `_actual_llm_provider_family` is an existing upstream event extra name.
        # Keep reading it for compatibility, but normalize its value into the
        # new primary/secondary route slots immediately.
        actual_target = self._normalize_route_target(
            event.get_extra("_actual_llm_provider_family", "")
        )
        if actual_target in ROUTE_TARGETS:
            return actual_target

        actual_provider_id = str(event.get_extra("_actual_llm_provider_id", "") or "").strip()
        for target in ROUTE_TARGETS:
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
            if target in ROUTE_TARGETS:
                return target

        selected_provider = str(event.get_extra("selected_provider", "") or "").strip()
        for target in ROUTE_TARGETS:
            provider_id = self._route_provider_id(target)
            if selected_provider and provider_id and selected_provider == provider_id:
                return target

        current_provider = self.context.get_using_provider(umo=event.unified_msg_origin)
        current_provider_id = ""
        if isinstance(current_provider, Provider):
            current_provider_id = str(current_provider.meta().id or "").strip()

        for target in ROUTE_TARGETS:
            provider_id = self._route_provider_id(target)
            if current_provider_id and provider_id and current_provider_id == provider_id:
                return target
        return None

    def _get_route_prefix_label(self, target: str) -> str:
        if target == PRIMARY_TARGET:
            return (
                self._cfg_str_any(
                    ("primary_reply_prefix_label", "gpt_reply_prefix_label"),
                    "主路由",
                )
                or "主路由"
            )
        if target == SECONDARY_TARGET:
            return (
                self._cfg_str_any(
                    ("secondary_reply_prefix_label", "gemini_reply_prefix_label"),
                    "副路由",
                )
                or "副路由"
            )
        return ""

    def _build_route_reply_prefix(self, event: AstrMessageEvent) -> str:
        if not self._reply_prefix_enabled():
            return ""

        target = self._get_route_target(event)
        if target not in ROUTE_TARGETS:
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
        current_block = (
            self._build_persona_block(current_persona_prompt)
            if current_persona_prompt
            else ""
        )

        if current_block and current_block in system_prompt:
            return system_prompt.replace(current_block, target_block, 1), "replaced_exact"

        generic_pattern = re.compile(
            r"(?s)\n# Persona Instructions\n\n.*?(?=(?:\n##? [^\n]|\Z))"
        )
        if generic_pattern.search(system_prompt):
            return generic_pattern.sub(target_block, system_prompt, count=1), "replaced_section"

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
        if target not in ROUTE_TARGETS:
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

    def _extract_reply_quote_text(self, event: AstrMessageEvent) -> str:
        for comp in event.get_messages():
            if not isinstance(comp, Comp.Reply):
                continue
            if getattr(comp, "message_str", None):
                return self._sanitize_context_visible_text(
                    str(comp.message_str).strip()
                )
            chain_text = self._outline_chain(getattr(comp, "chain", None))
            if chain_text:
                return self._sanitize_context_visible_text(chain_text)
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

    def _get_recent_route_context(
        self,
        event: AstrMessageEvent,
    ) -> dict[str, str | float] | None:
        route = self._recent_routes_by_chat.get(event.unified_msg_origin)
        if not route:
            return None

        route_timestamp = float(route.get("timestamp", 0.0) or 0.0)
        if route_timestamp and time.time() - route_timestamp > RECENT_ROUTE_TTL_SECONDS:
            self._recent_routes_by_chat.pop(event.unified_msg_origin, None)
            return None

        if not event.is_private_chat():
            current_sender = str(event.get_sender_id() or "").strip()
            previous_sender = str(route.get("sender_id") or "").strip()
            if not current_sender or current_sender != previous_sender:
                return None

        return route

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

    def _sticky_break_reason(
        self,
        sticky_target: str,
        rules_decision: RouteDecision,
    ) -> str | None:
        if rules_decision.target is None or rules_decision.target == sticky_target:
            return None

        reason = rules_decision.reason or ""
        if sticky_target == SECONDARY_TARGET:
            prefixes = SECONDARY_STICKY_BREAK_REASON_PREFIXES
        else:
            prefixes = PRIMARY_STICKY_BREAK_REASON_PREFIXES

        if any(reason.startswith(prefix) for prefix in prefixes):
            return reason
        return None

    def _is_force_reason(self, reason: str) -> bool:
        return any(reason.startswith(prefix) for prefix in STICKY_FORCE_REASON_PREFIXES)

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
        if decision.target not in ROUTE_TARGETS:
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
        quoted_reply = self._extract_reply_quote_text(event)
        recent_route = self._get_recent_route_context(event)
        angel_context = self._extract_angelheart_context_summary(event)
        follow_up_like = bool(quoted_reply) or (
            bool(recent_route) and len(normalized) <= 48
        )

        lines = [
            f"platform={event.get_platform_name()}",
            f"private_chat={event.is_private_chat()}",
            f"has_media={self._message_has_media(event)}",
            f"follow_up_like={follow_up_like}",
            f"message={self._clip_text(sanitized_text or outline, 220)}",
        ]

        if outline and outline != sanitized_text:
            lines.append(f"message_outline={self._clip_text(outline, 320)}")
        if quoted_reply:
            lines.append(f"quoted_reply={self._clip_text(quoted_reply, 220)}")

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
            "system_prompt": (
                self._cfg_str(
                    "classifier_system_prompt",
                    DEFAULT_CLASSIFIER_SYSTEM_PROMPT,
                )
                or DEFAULT_CLASSIFIER_SYSTEM_PROMPT
            ),
            "persist": False,
        }
        try:
            response = await provider.text_chat(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[provider_router] classifier request failed: %s", exc)
            return RouteDecision(None, "classifier_request_failed", "llm")

        raw = (response.completion_text or "").strip().lower()
        token_match = re.search(r"\b(primary|secondary|gpt|gemini|keep_default)\b", raw)
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
        if fallback in ROUTE_TARGETS:
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

    def _apply_route(self, event: AstrMessageEvent, decision: RouteDecision) -> bool:
        if decision.target not in ROUTE_TARGETS:
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

    def _store_decision(self, event: AstrMessageEvent, decision: RouteDecision) -> None:
        event.set_extra(
            ROUTE_DECISION_KEY,
            {
                "target": decision.target or "keep_default",
                "reason": decision.reason,
                "source": decision.source,
            },
        )

    def _remember_route(
        self,
        event: AstrMessageEvent,
        text: str,
        decision: RouteDecision,
    ) -> None:
        if decision.target not in ROUTE_TARGETS:
            return
        preview = text.replace("\n", " ").strip()
        if len(preview) > 80:
            preview = preview[:77] + "..."
        self._recent_routes_by_chat[event.unified_msg_origin] = {
            "target": decision.target,
            "reason": decision.reason,
            "source": decision.source,
            "sender_id": event.get_sender_id(),
            "text_preview": preview,
            "timestamp": time.time(),
        }

    def _log_decision(
        self,
        event: AstrMessageEvent,
        text: str,
        decision: RouteDecision,
        applied: bool,
    ) -> None:
        if not self._cfg_bool("log_decisions", True):
            return
        preview = text.replace("\n", " ").strip()
        if len(preview) > 80:
            preview = preview[:77] + "..."
        logger.info(
            "[provider_router] route=%s | applied=%s | source=%s | reason=%s | chat=%s | text=%r",
            decision.target or "keep_default",
            applied,
            decision.source,
            decision.reason,
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
            self._store_decision(event, decision)
            self._log_decision(event, text, decision, applied=False)
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
            if sticky_target in ROUTE_TARGETS:
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
                elif not sticky_break_reason:
                    decision = self._build_sticky_decision(event, sticky)
                    applied = self._apply_route(event, decision)
                    self._store_decision(event, decision)
                    self._remember_route(event, effective_text, decision)
                    self._log_decision(event, text, decision, applied)
                    return
                else:
                    decision = self._build_sticky_decision(event, sticky)
                    applied = self._apply_route(event, decision)
                    self._store_decision(event, decision)
                    self._remember_route(event, effective_text, decision)
                    self._log_decision(event, text, decision, applied)
                    return

        decision = self._rules_decision(event, text)
        classifier_decision: RouteDecision | None = None
        if (
            decision.target is None
            and decision.reason == "no_rule_match"
            and self._cfg_str("classifier_mode", "rules_only") == "rules_then_llm"
        ):
            classifier_decision = await self._llm_classifier_decision(event, text)
            decision = classifier_decision

        if decision.target is None:
            decision = self._compose_fallback_decision(classifier_decision or decision)

        applied = self._apply_route(event, decision)
        self._store_decision(event, decision)
        self._remember_route(event, effective_text, decision)
        self._arm_sticky_override(event, decision)
        self._log_decision(event, text, decision, applied)

    @filter.on_llm_request(priority=-10001)
    async def sanitize_llm_request_prompt(self, event: AstrMessageEvent, req: Any) -> None:
        original_text = str(event.get_extra("_provider_router_original_message_str", "") or "")
        rewritten_text = str(event.get_extra("_provider_router_rewritten_message_str", "") or "")
        reason = str(event.get_extra("_provider_router_strip_reason", "") or "")

        if original_text and rewritten_text and reason:
            self._sanitize_request_prompt(req, original_text, rewritten_text, reason)
        self._sanitize_force_directives_in_request_prompt(req)
        self._sanitize_route_reply_prefixes_in_request_prompt(req)
        await self._apply_route_persona_override(event, req)

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
