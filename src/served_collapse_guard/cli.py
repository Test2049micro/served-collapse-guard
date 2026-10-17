"""Command line entry point. Exit codes are part of the contract:

===== =====================================================================
0     every seat answered from its own vendor, no downgrade detected
1     collapse found (cross-vendor, same-vendor downgrade, or a vote that
      collapsed onto fewer vendors than it claims)
2     insufficient evidence: a seat did not answer, answered inconsistently,
      or its requested name is not in the vendor map
3     usage error (nothing was probed)
===== =====================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Sequence

from . import __version__
from .core import DEFAULT_SERVED_FIELDS, PROBE_PROMPT, judge_seats, probe_model
from .vendors import VendorMap

# Only a purpose-built variable: auto-picking $OPENAI_API_KEY would quietly ship
# that key to whatever host --endpoint names.
API_KEY_ENV = ("SERVED_GUARD_API_KEY",)

VERDICT_EXPLANATION = {
    "insufficient-evidence": "not enough evidence to claim a multi-vendor review",
    "single-vendor": "fewer than 2 real vendors answered -- the review is not multi-vendor",
    "partial-collapse": (
        "fewer vendors answered than the seat list claims"
        " -- de-duplicate votes by actual vendor, not by seat name"
    ),
    "multi-vendor": "every seat answered from a distinct vendor -- review holds",
}


def _resolve_api_key(args: argparse.Namespace) -> str | None:
    if args.api_key:
        return args.api_key
    if args.no_auth:
        return None
    for name in API_KEY_ENV:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _build_vendors(args: argparse.Namespace) -> VendorMap:
    vendors = VendorMap.load(getattr(args, "vendor_map", None))
    extra = getattr(args, "alias", None)
    return vendors.with_aliases(extra) if extra else vendors


def _probe_kwargs(args: argparse.Namespace) -> dict:
    headers = {}
    for item in args.header or []:
        if "=" not in item:
            raise ValueError(f"--header expects name=value, got {item!r}")
        key, value = item.split("=", 1)
        headers[key] = value
    fields = tuple(args.field) or DEFAULT_SERVED_FIELDS
    return {
        "api_key": _resolve_api_key(args),
        "headers": headers,
        "timeout": args.timeout,
        "max_tokens": args.max_tokens,
        "prompt": args.prompt,
        "allow_insecure": args.allow_insecure,
        "served_fields": fields,
    }


def _seat_lines(rows, json_mode: bool) -> list[str]:
    lines = []
    for r in rows:
        if json_mode:
            continue
        if not r.ok:
            lines.append(f"{r.requested:<32} UNKNOWN   error={r.error}")
            continue
        flag = {
            "cross-vendor": "COLLAPSE(cross-vendor)",
            "downgrade": "COLLAPSE(downgrade)",
            "alias": "alias",
            "none": "ok",
        }[r.collapsed_kind]
        lines.append(
            f"{r.requested:<32} -> {r.served:<28} vendor={r.vendor:<11} "
            f"stable={'y' if r.stable else 'N'} {flag}"
        )
    return lines


def cmd_probe(args: argparse.Namespace) -> int:
    models = list(args.models) + list(args.m)
    if not models:
        print("error: give at least one model to probe", file=sys.stderr)
        return 3
    vendors = _build_vendors(args)
    kwargs = _probe_kwargs(args)
    rows = [
        probe_model(
            args.endpoint,
            m,
            samples=args.samples,
            vendors=vendors,
            strict_names=args.strict_names,
            **kwargs,
        )
        for m in models
    ]
    for line in _seat_lines(rows, args.json):
        print(line)
    collapsed = [r for r in rows if r.collapsed]
    unknown = [r for r in rows if not r.ok]
    if args.json:
        print(json.dumps([r.to_dict() for r in rows], ensure_ascii=False))
    if collapsed:
        return 1
    return 2 if unknown else 0


def cmd_judge(args: argparse.Namespace) -> int:
    seats = [s.strip() for s in args.seats.split(",") if s.strip()] or list(args.models)
    if len(seats) < 2:
        print("error: judge needs at least 2 seats", file=sys.stderr)
        return 3
    vendors = _build_vendors(args)
    report = judge_seats(
        args.endpoint,
        seats,
        samples=args.samples,
        vendors=vendors,
        strict_names=args.strict_names,
        **_probe_kwargs(args),
    )
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False))
    else:
        for line in _seat_lines(report.seats, False):
            print(f"  seat {line}")
        print()
        print(f"claimed vendors  {len(report.nominal_vendors)}: {report.nominal_vendors}")
        print(f"actual vendors   {report.distinct_actual}: {report.actual_vendors}")
        if report.unusable:
            print(f"unusable seats   {report.unusable}")
        print(
            f"verdict: {report.verdict} -- {VERDICT_EXPLANATION[report.verdict]}"
        )
    return report.exit_code


def cmd_vendors(args: argparse.Namespace) -> int:
    vendors = _build_vendors(args)
    rows = {m: {"vendor": vendors.vendor_of(m), "is_router_alias": vendors.is_router_alias(m)} for m in args.models}
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
    else:
        for model, info in rows.items():
            print(f"{model:<32} vendor={info['vendor']:<11} alias={'y' if info['is_router_alias'] else 'n'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="served-collapse-guard",
        description="Audit which model an OpenAI-compatible gateway actually served you.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--endpoint",
        default=os.environ.get("SERVED_GUARD_ENDPOINT", "127.0.0.1:8000"),
        help="host:port, http(s)://host:port, .../v1 or a full completions URL"
        " (default: 127.0.0.1:8000, or $SERVED_GUARD_ENDPOINT)",
    )
    common.add_argument("--samples", type=int, default=2, help="probes per seat to test route stability")
    common.add_argument("--timeout", type=float, default=45.0, help="seconds per probe")
    common.add_argument("--max-tokens", type=int, default=10, help="tiny on purpose, still billable")
    common.add_argument("--prompt", default=PROBE_PROMPT, help="probe prompt text")
    common.add_argument(
        "--field",
        action="append",
        default=[],
        help="response path holding the served model, tried in order"
        f" (default {' '.join(DEFAULT_SERVED_FIELDS)})",
    )
    common.add_argument("--header", action="append", default=[], help="extra header name=value, repeatable")
    common.add_argument("--api-key", help="bearer token; prefer $SERVED_GUARD_API_KEY over the flag")
    common.add_argument("--no-auth", action="store_true", help="send no Authorization header")
    common.add_argument("--allow-insecure", action="store_true",
                        help="permit cleartext http to a non-loopback endpoint (not recommended: the key goes in the header)")
    common.add_argument("--vendor-map", help="JSON or TOML file extending the vendor prefix table")
    common.add_argument("--alias", action="append", default=[], help="router alias pattern, repeatable")
    common.add_argument(
        "--strict-names",
        action="store_true",
        help="treat any spelling difference (e.g. a pinned date suffix) as a downgrade",
    )
    common.add_argument("--json", action="store_true", help="machine-readable output")

    sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe", parents=[common], help="check named models for collapse")
    probe.add_argument("models", nargs="*", help="model ids to probe")
    probe.add_argument("-m", action="append", default=[], help="add one model id, repeatable")
    probe.set_defaults(func=cmd_probe)

    judge = sub.add_parser("judge", parents=[common], help="does this seat list really span vendors?")
    judge.add_argument("--seats", default="", help="comma separated seat model ids")
    judge.add_argument("models", nargs="*", help="or positional model ids")
    judge.set_defaults(func=cmd_judge)

    vendors = sub.add_parser("vendors", parents=[common], help="classify model names offline")
    vendors.add_argument("models", nargs="+", help="model ids to classify")
    vendors.set_defaults(func=cmd_vendors)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(args, "m") and args.models:
        numeric = [m for m in args.models if m.isdigit() or (":" in m and "/" not in m)]
        if numeric:
            print(
                f"error: {numeric[0]!r} looks like an endpoint, not a model. Use --endpoint host:port.",
                file=sys.stderr,
            )
            return 3
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except json.JSONDecodeError as exc:
        print(f"error: bad JSON in vendor map: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
