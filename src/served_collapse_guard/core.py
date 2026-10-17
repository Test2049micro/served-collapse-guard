"""Probe an OpenAI-compatible endpoint and report which model actually served.

Design rules that the whole tool rests on:

* ``served_model`` from the response is the only truth. A gateway's advertised
  model list is the thing under test, so it is never consulted.
* No value means no verdict. A probe that cannot read a served model returns
  ``ok=False`` and the exit code says "insufficient evidence" -- it must never be
  reported as a pass, otherwise the guard becomes a machine for confirming bias.
* A value that varies between samples is not evidence either: an unstable route
  is excluded from a judge vote rather than counted on the side of whichever
  sample happened to come first.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Redirects are refused rather than followed.

    urllib re-attaches ``Authorization`` to the redirected request, so a gateway
    answering 302 would hand the key to whatever host it pointed at. For an audit
    tool that is given arbitrary endpoints, that is an exfiltration path.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_RejectRedirects())
from dataclasses import dataclass, field

from .vendors import VendorMap

DEFAULT_SERVED_FIELDS = ("metadata.served_model", "model")
DEFAULT_REQUESTED_FIELDS = ("metadata.requested_model",)
COMPLETIONS_PATH = "/v1/chat/completions"
PROBE_PROMPT = "Reply with exactly: OK"


@dataclass
class ProbeResult:
    """One seat's verdict: what was asked for versus what answered."""

    requested: str
    served: str | None = None
    ok: bool = False
    stable: bool = False
    vendor: str = "unknown"
    requested_vendor: str = "unknown"
    collapsed: bool | None = None
    collapsed_kind: str = "unknown"
    is_alias: bool = False
    error: str | None = None
    samples: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """Countable as evidence in a vote: answered, and answered consistently."""
        return self.ok and self.stable

    def to_dict(self) -> dict:
        return {
            "requested": self.requested,
            "served": self.served,
            "ok": self.ok,
            "stable": self.stable,
            "vendor": self.vendor,
            "requested_vendor": self.requested_vendor,
            "collapsed": self.collapsed,
            "collapsed_kind": self.collapsed_kind,
            "is_alias": self.is_alias,
            "error": self.error,
            "samples": self.samples,
        }


@dataclass
class JudgeReport:
    seats: list[ProbeResult]
    nominal_vendors: list[str]
    actual_vendors: list[str]
    unusable: list[str]

    @property
    def distinct_actual(self) -> int:
        return len(self.actual_vendors)

    @property
    def verdict(self) -> str:
        if self.unusable:
            return "insufficient-evidence"
        if self.distinct_actual < 2:
            return "single-vendor"
        if self.distinct_actual < len(self.nominal_vendors):
            return "partial-collapse"
        return "multi-vendor"

    @property
    def exit_code(self) -> int:
        return {
            "insufficient-evidence": 2,
            "single-vendor": 1,
            "partial-collapse": 1,
            "multi-vendor": 0,
        }[self.verdict]

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "exit_code": self.exit_code,
            "nominal_vendors": self.nominal_vendors,
            "actual_vendors": self.actual_vendors,
            "distinct_actual": self.distinct_actual,
            "unusable_seats": self.unusable,
            "seats": [s.to_dict() for s in self.seats],
        }


def _is_loopback(url: str) -> bool:
    host = url.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0].lower()
    return host in ("localhost", "::1", "0.0.0.0") or host.startswith("127.")


def normalize_base(endpoint: str, *, allow_insecure: bool = False) -> str:
    """Accept ``host:port``, ``http(s)://host:port``, ``.../v1`` or a full URL.

    Two rules here are about the API key rather than tidiness:

    * a bare host defaults to **https**. Defaulting to http would put the bearer
      token in clear text on exactly the internet-facing gateways this tool audits;
      loopback and ``--allow-insecure`` are the only cleartext paths.
    * userinfo is rejected. ``https://gw.internal@attacker.example/v1`` reads like
      an internal host and connects to ``attacker.example`` -- with a key attached
      that is an exfiltration path that looks like a typo.
    """
    url = (endpoint or "").strip().rstrip("/")
    if not url:
        raise ValueError("endpoint is empty")
    if not url.startswith(("http://", "https://")):
        if ":" not in url and "." not in url:
            raise ValueError(f"endpoint {endpoint!r} is not host:port or a URL")
        scheme = "http" if _is_loopback(url) or allow_insecure else "https"
        url = f"{scheme}://{url}"
    if not allow_insecure and url.startswith("http://") and not _is_loopback(url):
        raise ValueError(
            f"refusing to send credentials over cleartext: {url} "
            "(use https, or --allow-insecure for a local gateway)"
        )
    authority = url.split("://", 1)[1].split("/", 1)[0]
    if "@" in authority:
        raise ValueError(f"endpoint must not contain userinfo: {endpoint!r}")
    if url.endswith("completions"):
        return url
    if url.endswith("/v1"):
        return f"{url}/chat/completions"
    path = url.split("://", 1)[1].split("/", 1)[1] if "/" in url.split("://", 1)[1] else ""
    if path:
        # any other base path is a guess waiting to be wrong: /openai could want
        # /openai/chat/completions or /openai/v1/chat/completions depending on the
        # gateway, and silently appending one of them produces a probe that talks
        # to the wrong place
        raise ValueError(
            f"endpoint {endpoint!r} has an unrecognised path. Use host:port, "
            ".../v1, or the full .../chat/completions URL"
        )
    return f"{url}{COMPLETIONS_PATH}"


def _dig(payload: dict, dotted: str):
    node = payload
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) else None


def extract_served(
    payload: dict,
    served_fields: Sequence[str] = DEFAULT_SERVED_FIELDS,
    requested_fields: Sequence[str] = DEFAULT_REQUESTED_FIELDS,
) -> tuple[str | None, str | None]:
    for f in served_fields:
        value = _dig(payload, f)
        if value:
            served = value
            requested = None
            for rf in requested_fields:
                requested = _dig(payload, rf)
                if requested:
                    break
            return served, requested
    return None, None


_CAPABILITY_WORDS = ("mini", "lite", "small", "nano", "micro", "pro", "max", "turbo",
                     "preview", "latest", "thinking", "high", "low", "fast")


def _names_compatible(requested: str, served: str) -> bool:
    """True when served is only a *version pin* of the requested model.

    Gateways echo ``gpt-4o-2024-11-20`` for a request of ``gpt-4o``, and calling
    that a downgrade would make the guard cry wolf. Accepting any suffix, though,
    would call ``gpt-4o`` -> ``gpt-4o-mini`` compatible -- which is exactly the
    silent capability cut this tool exists to surface. So the extra tail has to
    look like a date or version number, and must not contain a capability word.
    """
    req, srv = requested.lower(), served.lower()
    if req == srv:
        return True
    for longer, shorter in ((srv, req), (req, srv)):
        if not longer.startswith(shorter) or len(longer) == len(shorter):
            continue
        if longer[len(shorter)] not in "-_.:":
            continue
        tail = longer[len(shorter) + 1:]
        if not tail or not re.fullmatch(r"[0-9][0-9a-z.\-]*", tail):
            continue
        if any(word in tail.replace("-", ".").split(".") for word in _CAPABILITY_WORDS):
            continue
        return True
    return False


def probe_once(
    endpoint: str,
    model: str,
    *,
    api_key: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 45.0,
    max_tokens: int = 10,
    prompt: str = PROBE_PROMPT,
    allow_insecure: bool = False,
    served_fields: Sequence[str] = DEFAULT_SERVED_FIELDS,
    requested_fields: Sequence[str] = DEFAULT_REQUESTED_FIELDS,
    opener: Callable[..., object] | None = None,
) -> dict:
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
    ).encode()
    request_headers = {"Content-Type": "application/json"}
    if api_key:
        request_headers["Authorization"] = f"Bearer {api_key}"
    request_headers.update(headers or {})
    request = urllib.request.Request(normalize_base(endpoint, allow_insecure=allow_insecure),
                                     data=body, headers=request_headers)
    if opener is None:
        opener = build_opener().open
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            return {"ok": False, "status": exc.code, "served": None, "error": "redirect_refused"}
        return {"ok": False, "status": exc.code, "served": None, "error": f"http_{exc.code}"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "status": None, "served": None, "error": type(exc).__name__}
    except (json.JSONDecodeError, ValueError):
        return {"ok": False, "status": 200, "served": None, "error": "unparsable_json"}

    served, requested_echo = extract_served(payload, served_fields, requested_fields)
    if not served:
        return {"ok": False, "status": 200, "served": None, "error": "no_served_field"}
    return {
        "ok": True,
        "status": 200,
        "served": served,
        "requested": requested_echo or model,
    }


def probe_model(
    endpoint: str,
    model: str,
    *,
    samples: int = 2,
    vendors: VendorMap | None = None,
    strict_names: bool = False,
    **probe_kwargs,
) -> ProbeResult:
    """Sample one seat ``samples`` times and classify what came back.

    Two distinct failures are both called collapse: the answer came from another
    vendor (which breaks a vendor-de-duplicated vote), and the answer came from
    the same vendor but a different, weaker model (which does not break the vote
    but silently weakens capability, so it still has to be visible).
    """
    vendors = vendors or VendorMap()
    results = [probe_once(endpoint, model, **probe_kwargs) for _ in range(max(1, samples))]
    served_values = [r["served"] for r in results if r.get("ok")]
    if not served_values:
        return ProbeResult(
            requested=model,
            ok=False,
            error=results[0].get("error") or "unknown",
            requested_vendor=vendors.vendor_of(model),
        )
    distinct = sorted(set(served_values))
    # stability means "every sample agreed", not "every sample that happened to
    # answer agreed": a seat that times out on 2 of 3 probes is not a stable route
    # and must not be counted as evidence in a judge vote
    stable = len(distinct) == 1 and len(served_values) == len(results)
    served = served_values[0]
    is_alias = vendors.is_router_alias(model)
    requested_vendor = vendors.vendor_of(model)
    served_vendor = vendors.vendor_of(served)

    if not is_alias and requested_vendor == "unknown":
        return ProbeResult(
            requested=model,
            served=served,
            ok=False,
            stable=stable,
            vendor=served_vendor,
            requested_vendor="unknown",
            collapsed=None,
            collapsed_kind="unmapped",
            samples=distinct,
            error="unmapped_requested_model",
        )

    cross_vendor = served_vendor != requested_vendor
    if strict_names:
        downgrade = served_vendor == requested_vendor and served.lower() != model.lower()
    else:
        downgrade = served_vendor == requested_vendor and not _names_compatible(model, served)

    collapsed = False if is_alias else (cross_vendor or downgrade)
    kind = "alias" if is_alias else "cross-vendor" if cross_vendor else "downgrade" if downgrade else "none"
    return ProbeResult(
        requested=model,
        served=served,
        ok=True,
        stable=stable,
        vendor=served_vendor,
        requested_vendor=requested_vendor,
        collapsed=collapsed,
        collapsed_kind=kind,
        is_alias=is_alias,
        samples=distinct,
    )


def judge_seats(
    endpoint: str,
    seats: Sequence[str],
    *,
    samples: int = 2,
    vendors: VendorMap | None = None,
    strict_names: bool = False,
    **probe_kwargs,
) -> JudgeReport:
    """Ask every nominal seat the same question and count real vendors.

    Only usable evidence counts: a seat that did not answer, answered
    inconsistently, or whose requested name is unmapped cannot vote, and its
    presence pushes the verdict to ``insufficient-evidence`` rather than being
    quietly dropped -- dropping it would let an unreachable seat look like
    agreement.
    """
    vendors = vendors or VendorMap()
    results = [
        probe_model(
            endpoint,
            seat,
            samples=samples,
            vendors=vendors,
            strict_names=strict_names,
            **probe_kwargs,
        )
        for seat in seats
    ]
    usable = [r for r in results if r.usable]
    unusable = [r.requested for r in results if not r.usable]
    actual = sorted({r.vendor for r in usable if r.vendor != "unknown"})
    nominal = sorted({r.requested_vendor for r in usable if not r.is_alias and r.requested_vendor != "unknown"})
    return JudgeReport(seats=results, nominal_vendors=nominal, actual_vendors=actual, unusable=unusable)
