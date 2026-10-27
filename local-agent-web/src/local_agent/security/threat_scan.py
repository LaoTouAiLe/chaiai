"""常驻记忆的注入/外泄扫描（架构 §10.7，2026-10-27）。

为什么需要它：``MEMORY.md`` / ``USER.md`` 是**每轮无条件注入系统前缀**的内容，而模型可以直写它们
（§10.7 的写入通道）。一条被污染的条目不会像"召回到的错误记忆"那样只影响一轮——它会跨会话、
跨项目地持续生效。参照实现在写入与加载两侧各扫一次（``tools/memory_tool_store.py:28-31``、
``:221-253``），本模块是它的**最小可用子集**：

- 只保留"锚定攻击行为"的模式（注入、越权、提示词泄露、凭据外泄、不可见字符），不抄"命令式英文"
  ——``you must`` 这类措辞在正常的项目约定里很常见，误杀会让记忆不可用；
- 扫描是**建议性**的：命中就拒绝写入/从快照剔除，但绝不修改用户文件的原文。

与参照实现的差异（如实记录）：没有分 ``all`` / ``context`` / ``strict`` 三级作用域——本模块只服务
常驻记忆这一个用途，全部按最严一侧处理；``MAX_SCAN_CHARS`` 上限与参照实现一致，防止正则回溯。
"""

from __future__ import annotations

import re
from typing import Final

#: 扫描文本上限：扫描器是建议性的，必须给最坏情况设界（与参照实现同值）。
MAX_SCAN_CHARS: Final[int] = 65_536

#: 词间填充：参照实现用有界填充避免 ``(?:\w+\s+)*`` 的回溯爆炸。
_FILLER = r"(?:\w+\s+){0,8}"

#: ``(正则, 模式 id)``。命中即拒绝；顺序稳定，报第一个。
_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    # 经典提示词注入
    (rf"ignore\s+{_FILLER}(previous|all|above|prior)\s+{_FILLER}instructions", "prompt_injection"),
    (r"system\s+prompt\s+override", "sys_prompt_override"),
    (rf"disregard\s+{_FILLER}(your|all|any)\s+{_FILLER}(instructions|rules|guidelines)", "disregard_rules"),
    (r"<!--[^>]{0,512}(?:ignore|override|system|secret|hidden)[^>]{0,512}-->", "html_comment_injection"),
    # 中文注入：本项目的用户与内容以中文为主，英文模式拦不到这类写法
    (r"(忽略|无视|忘记)[^\n。]{0,8}(之前|以上|上面|先前|前面|所有)[^\n。]{0,6}(指令|指示|要求|规则|设定)", "prompt_injection_zh"),
    (r"(系统提示词|system\s*prompt)\s{0,3}(泄露|输出|打印|告诉)", "leak_system_prompt_zh"),
    # 身份劫持 / 越权
    (rf"you\s+are\s+{_FILLER}now\s+(?:a|an|the)\s+", "role_hijack"),
    (rf"(respond|answer|reply)\s+without\s+{_FILLER}(restrictions|limitations|filters|safety)", "remove_filters"),
    # 凭据外泄
    (r"(?i)\b(curl|wget|invoke-webrequest|iwr)\b[^\n]{0,200}(\.ssh|id_rsa|credentials|\.aws)", "credential_exfil"),
    (r"\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)S?\b[^\n]{0,80}(https?://|@)", "secret_to_url"),
    # 后门 / 持久化
    (r"(?i)(authorized_keys|\.bashrc|\.profile|schtasks\s+/create|reg\s+add\s+[^\n]*run)", "persistence"),
)

_COMPILED: Final[tuple[tuple[re.Pattern[str], str], ...]] = tuple(
    (re.compile(pattern), name) for pattern, name in _PATTERNS
)

#: 不可见字符：零宽、双向控制、BOM 以外的不可见格式符。记忆是给人看的，出现这些字符没有正当用途。
_INVISIBLE: Final[frozenset[str]] = frozenset(
    {chr(code) for code in (*range(0x200B, 0x2010), *range(0x202A, 0x202F), *range(0x2060, 0x2065), 0xFEFF)}
)

#: 模式 id → 给用户/模型看的说法。
_MESSAGES: Final[dict[str, str]] = {
    "prompt_injection": "提示词注入（要求覆盖既有指令）",
    "prompt_injection_zh": "提示词注入（要求忽略既有指令）",
    "sys_prompt_override": "提示词覆盖",
    "disregard_rules": "要求忽略规则",
    "html_comment_injection": "HTML 注释里的隐藏指令",
    "leak_system_prompt_zh": "要求泄露系统提示词",
    "role_hijack": "身份劫持",
    "remove_filters": "要求去掉限制",
    "credential_exfil": "凭据外泄",
    "secret_to_url": "把密钥发往外部地址",
    "persistence": "写入持久化后门",
    "invisible_characters": "不可见字符",
}


def scan(text: str) -> str | None:
    """返回第一个命中的模式 id；未命中返回 ``None``。

    只读：不修改传入文本，也不落盘。调用方决定是拒绝写入还是从快照里剔除。
    """
    if not text:
        return None
    sample = text[:MAX_SCAN_CHARS]
    for pattern, name in _COMPILED:
        if pattern.search(sample):
            return name
    if any(char in _INVISIBLE for char in sample):
        return "invisible_characters"
    return None


def finding_text(name: str) -> str:
    """模式 id 的人类可读说法（写入拒绝与页面提示共用一份文案）。"""
    return _MESSAGES.get(name, name)


def block_message(name: str, *, filename: str, target: str) -> str:
    """加载时命中：写进快照的占位文字（原文仍在文件里，用户可自行删除）。"""
    return (
        f"[BLOCKED: {filename} 里有一条内容命中「{finding_text(name)}」，已从系统提示中移除。"
        f"原文仍在文件中，请人工确认后删除（`memory(action=remove, target='{target}', old_text=…)` "
        "或直接编辑该文件）。]"
    )


def normalize_for_display(text: str) -> str:
    """把不可见字符替换成可见占位，供错误信息回显命中片段（不把攻击文本原样写回界面）。"""
    return "".join("\ufffd" if char in _INVISIBLE else char for char in text[:200])
