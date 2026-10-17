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
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
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


def normalize_base(endpoint: str) -> str:
    """Accept ``host:port``, ``http://host:port``, ``.../v1`` or a full URL."""
    url = (endpoint or "").strip().rstrip("/")
    if not url:
        raise ValueError("endpoint is empty")
    if not url.startswith(("http://", "https://")):
        if ":" not in url and "." not in url:
            raise ValueError(f"endpoint {endpoint!r} is not host:port or a URL")
        url = f"http://{url}"
    if url.endswith("completions"):
        return url
    if url.endswith("/v1"):
        return f"{url}/chat/completions"
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


def _names_compatible(requested: str, served: str) -> bool:
    """True when served is just a pinned/expanded spelling of the same model.

    Gateways echo ``gpt-4o-2024-11-20`` for a request of ``gpt-4o``. Calling that
    a downgrade would make the guard cry wolf, so a prefix relation on either
    side counts as the same model. Use ``--strict-names`` to disable.
    """
    req, srv = requested.lower(), served.lower()
    if req == srv:
        return True
    for a, b in ((req, srv), (srv, req)):
        if b.startswith(a) and (len(b) == len(a) or b[len(a)] in "-_.:"):
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
    served_fields: Sequence[str] = DEFAULT_SERVED_FIELDS,
    requested_fields: Sequence[str] = DEFAULT_REQUESTED_FIELDS,
    opener: Callable[..., object] = urllib.request.urlopen,
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
    request = urllib.request.Request(normalize_base(endpoint), data=body, headers=request_headers)
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
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
    served = served_values[0]
    is_alias = vendors.is_router_alias(model)
    requested_vendor = vendors.vendor_of(model)
    served_vendor = vendors.vendor_of(served)

    if not is_alias and requested_vendor == "unknown":
        return ProbeResult(
            requested=model,
            served=served,
            ok=False,
            stable=len(distinct) == 1,
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
        stable=len(distinct) == 1,
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
