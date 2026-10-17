"""Model-name to vendor classification, driven by a prefix table.

The shipped table covers publicly named models only. Unreleased or privately
renamed models (anonymised pool members, reseller aliases) must be registered by
the user through ``--vendor-map``; unmapped names are reported as ``unknown``
rather than guessed, because a wrong guess silently corrupts a vendor-de-duplication
vote count.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

# (prefix, vendor). Longest prefix wins, so "gpt-image" beats "gpt-".
DEFAULT_VENDORS: tuple[tuple[str, str], ...] = (
    ("claude", "anthropic"),
    ("aws.claude", "anthropic"),
    ("vertex_ai.claude", "anthropic"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("chatgpt", "openai"),
    ("gpt-image", "openai"),
    ("gpt", "openai"),
    ("text-embedding", "openai"),
    ("dall-e", "openai"),
    ("whisper", "openai"),
    ("gemini", "google"),
    ("gemma", "google"),
    ("vertex_ai.gemini", "google"),
    ("grok", "xai"),
    ("deepseek", "deepseek"),
    ("qwen", "alibaba"),
    ("qwq", "alibaba"),
    ("glm", "zhipu"),
    ("codegeex", "zhipu"),
    ("hunyuan", "tencent"),
    ("llama", "meta"),
    ("mistral", "mistral"),
    ("magistral", "mistral"),
    ("codestral", "mistral"),
    ("devstral", "mistral"),
    ("kimi", "moonshot"),
    ("moonshot", "moonshot"),
    ("doubao", "bytedance"),
    ("seedream", "bytedance"),
    ("step", "stepfun"),
    ("baichuan", "baichuan"),
    ("mimo", "xiaomi"),
    ("ernie", "baidu"),
    ("minimax", "minimax"),
    ("abab", "minimax"),
    ("command", "cohere"),
    ("yi", "lingyiwanwu"),
    ("nvidia/", "nvidia"),
    ("nova", "amazon"),
    ("mistral-large", "mistral"),
    ("reka", "reka"),
    ("titan", "amazon"),
)

# Router aliases resolve to whichever real model the router picked, so they carry
# no vendor claim of their own and must never be counted as a nominal seat.
DEFAULT_ALIAS_PATTERNS: tuple[str, ...] = ("auto-router/",)


class VendorMap:
    """Prefix table plus router-alias patterns."""

    def __init__(
        self,
        vendors: Sequence[tuple[str, str]] | Mapping[str, str] | None = None,
        alias_patterns: Iterable[str] | None = None,
    ) -> None:
        source = vendors if vendors is not None else DEFAULT_VENDORS
        if isinstance(source, Mapping):
            source = list(source.items())
        self._pairs = sorted(
            ((p.lower(), v) for p, v in source if p),
            key=lambda pv: len(pv[0]),
            reverse=True,
        )
        self._aliases = sorted({a.lower() for a in (alias_patterns or DEFAULT_ALIAS_PATTERNS)})

    @classmethod
    def load(cls, path: str | Path | None = None) -> "VendorMap":
        """Built-in table, optionally extended by a JSON or TOML file.

        The file may hold ``{"vendors": {...}, "aliases": [...]}`` or a bare
        ``{"prefix": "vendor"}`` mapping.
        """
        if path is None:
            return cls()
        text = Path(path).read_text(encoding="utf-8")
        data = _parse_mapping(text, str(path))
        vendors = data.get("vendors", data)
        alias = tuple(data["aliases"]) if isinstance(data.get("aliases"), list) else None
        merged = list(DEFAULT_VENDORS) + [(k, v) for k, v in vendors.items() if isinstance(v, str)]
        return cls(merged, alias)

    def with_aliases(self, extra: Iterable[str]) -> "VendorMap":
        """Same vendor table, plus additional router-alias patterns."""
        return VendorMap(self._pairs, list(self._aliases) + list(extra))

    def vendor_of(self, model: str | None) -> str:
        if not model:
            return "unknown"
        low = model.lower()
        for prefix, vendor in self._pairs:
            if low.startswith(prefix):
                return vendor
        return "unknown"

    def is_router_alias(self, model: str) -> bool:
        low = (model or "").lower()
        return any(a in low for a in self._aliases)


def _parse_mapping(text: str, name: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:
        raise SystemExit(
            f"{name}: not JSON, and TOML needs Python 3.11+ (or ship the map as JSON)"
        )
    return tomllib.loads(text)
