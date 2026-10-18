# served-collapse-guard

**Does the model you asked for actually answer?**

An OpenAI-compatible gateway will tell you it routes to eight vendors. When a
request for `claude-sonnet-4-6` is served by `gemini-2-5-flash`, nothing in the
response body you usually read says so. This tool sends a 10-token probe, reads
`served_model` back out of the response, and tells you which model actually
answered — per seat, and across a whole seat list.

Zero runtime dependencies, single `import`, Python 3.9+.

## Why configuration checks are not enough

Asking the gateway what it *can* serve is not the same question, and it is the
wrong one:

- `/v1/models` lists what is configured. Silent fallback lives in the request
  path, not in the model list.
- Multiple API keys for one vendor are redundancy against rate limits, not
  diversity of reasoning. Nothing in a config distinguishes "8 keys across 4
  vendors" from "8 aliases for the same 1 vendor".
- A router alias (`auto-router/…`) resolves per request. What it resolved to
  *today* is only observable in the response.

So this tool is a **black-box audit of the response**, and deliberately knows
nothing about your gateway's internals.

## Two failures it separates

| kind | what happened | why it matters |
|---|---|---|
| `cross-vendor` | seat asked Claude, got Gemini | breaks any vote counted per vendor |
| `downgrade` | seat asked `…-pro`, got `…-lite` from the *same* vendor | does not break the vote, but quietly removes capability |

Both are reported; only the first destroys vendor de-duplication.

## Install

```bash
pipx install served-collapse-guard      # or: pip install served-collapse-guard

# or straight from the v0.1.0 release asset (no clone, no PyPI account needed):
pipx install https://github.com/Test2049micro/served-collapse-guard/releases/download/v0.1.0/served_collapse_guard-0.1.0-py3-none-any.whl
```

## Usage

```bash
# one seat
served-collapse-guard probe --endpoint 127.0.0.1:4000 claude-sonnet-4-6
claude-sonnet-4-6                -> gemini-2-5-flash          vendor=google      stable=y COLLAPSE(cross-vendor)

# several seats, machine-readable
served-collapse-guard probe --endpoint 127.0.0.1:4000 --json \
  -m gpt-4o -m gemini-2-5-pro --samples 3

# the question a multi-model review actually needs answered
served-collapse-guard judge --seats claude-sonnet-4-6,gpt-4o,gemini-2-5-pro --endpoint …
#   seat claude-sonnet-4-6        -> gemini-2-5-flash          vendor=google  stable=y COLLAPSE(cross-vendor)
#   seat gpt-4o                   -> gemini-2-5-flash-lite     vendor=google  stable=y COLLAPSE(cross-vendor)
#   seat gemini-2-5-pro           -> gemini-2-5-flash-lite     vendor=google  stable=y COLLAPSE(downgrade)
#
#   claimed vendors  3: ['anthropic', 'google', 'openai']
#   actual vendors   1: ['google']
#   verdict: single-vendor -- fewer than 2 real vendors answered -- the review is not multi-vendor
```

Three nominally different vendors collapsed onto one. A "majority vote" between
those seats was one model answering itself three times. Output lines are commented
so the block stays copy-pasteable; with two of three seats on their own vendor the
verdict is `partial-collapse` instead, which is also exit 1.

### Exit codes

| code | meaning |
|---|---|
| `0` | every seat answered from its own vendor, no downgrade |
| `1` | collapse detected (or the seat list does not span vendors) |
| `2` | **insufficient evidence** — a seat did not answer, answered inconsistently, or is unmapped |
| `3` | usage error, nothing was probed |

`2` is separate from `0` on purpose. Silence from an unreachable gateway is not
proof of honesty, and a guard that reports "pass" when it learned nothing is a
bias-confirming machine.

## Gate the review on it

```bash
served-collapse-guard judge --seats "$SEATS" --endpoint "$GW" || {
  echo "seats are not vendor-distinct; do not deliver this as a multi-vendor conclusion" >&2
  exit 1
}
```

Run it before the review, not after: re-voting an invalid panel is cheaper than
re-explaining a delivered conclusion.

## Options that matter

| flag | why it exists |
|---|---|
| `--field a.b` | gateways store the served model in different places. Tried in order; default `metadata.served_model`, then `model`. |
| `--vendor-map file.json` | registers your private / anonymised pool names. Unmapped names yield `unknown`, never a guess. |
| `--alias pool-` | router aliases carry no vendor claim, so they can't be a "nominal" seat. |
| `--samples 3` | a route that varies between samples is flux, not evidence. Unstable seats are excluded and force verdict `insufficient-evidence`. |
| `--allow-insecure` | cleartext http to a non-loopback endpoint is **refused** (exit 3), because the bearer key rides in the header. Loopback stays http by default; a public host defaults to https |
| `--strict-names` | `gpt-4o` → `gpt-4o-2024-11-20` is a pinned version, not a downgrade. Lenient by default; strict if you want every spelling change visible. |
| `--no-auth` / `SERVED_GUARD_API_KEY` | the key is never printed or written to output. |
| `--endpoint` | accepts `host:port`, `http(s)://host:port`, `…/v1`, or a full `…/chat/completions` URL. Any other base path is refused rather than guessed (`/openai` is ambiguous, and guessing produces a probe that talks to the wrong place while reporting "no served field" as if the gateway were at fault). |

`examples/vendors.json` shows the map format. Aliases and vendors in that file
**extend** the built-ins; they do not replace them.

## What the key is protected from

- redirects are refused, not followed. urllib re-attaches `Authorization` to the
  redirected request, so a gateway answering 302 would hand the key to whatever it
  pointed at; `--json` reports `redirect_refused`.
- `userinfo` in an endpoint is rejected: `https://gw.internal@attacker.example/v1`
  reads like an internal host and connects to `attacker.example`.
- the key is read only from `SERVED_GUARD_API_KEY` (or `--api-key`). `$OPENAI_API_KEY`
  is deliberately **not** picked up: this tool sends requests to an arbitrary
  `--endpoint`, and quietly borrowing a general-purpose key would ship it there.

## Limits, stated plainly

- **It can only see what the gateway admits.** Some mask `served_model`, or
  report the *requested* model there. Then every seat looks honest. Treat a
  uniform `metadata.served_model` that always equals the request as suspicious,
  and cross-check with `--field`.
- **Prefix matching is a heuristic.** Vendor classification reads a name prefix
  table; a new model line needs a table entry or it classifies as `unknown`, which
  the tool reports as insufficient evidence rather than guessing.
- **Capability detection is name-based.** `gpt-4o` -> `gpt-4o-mini` is caught
  because "mini" is a capability word; a gateway that silently swaps in an
  unlabeled weaker model with a compatible-looking name is invisible to this check.
- **Probes are billable.** 10 `max_tokens` per sample, small but not free.
- Vendor classification is a name-prefix table, not a registry. If a vendor
  renames a model line, the table needs a PR.

## Development

```bash
PYTHONPATH=src python -m unittest discover -s tests -v   # no dependencies at all
pip install -e ".[dev]" && pytest -q        # same suite via pytest
```

The suite is built around four case types per behaviour: positive (real collapse
must fire), negative (clean route must stay silent), absence (no answer must not
read as pass), and flux (varying answers are not evidence). `tests/test_http.py`
runs against a real socket rather than a mock, so URL construction and header
behaviour are covered too.

## 中文摘要

网关说自己能路由八家模型，和它实际给你服务了哪家，是两个问题。查 `/v1/models`
问的是第一个，本工具问的是第二个：发一个 `max_tokens=10` 的探测，从响应的
`served_model` 读回真值，和请求的模型按厂商前缀比对。它区分两种塌缩——跨族塌缩
（要 Claude 给了 Gemini，破坏「按厂商去重计票」）和同族降档（pro 塌到 lite，不破坏
计票但静默削能力，同样必须可见）。`judge` 直接回答多模型评审最要害的一件事：
名义三家、实际几家；实际独立厂商不足两家时，所谓「共识」就是一个模型自问自答。
取不到真值一律判 `2 证据不足`，绝不判通过——没有读数却报「正常」的守卫，等于把
偏见包装成结论。运行时零依赖，Python 3.9+。

## License

MIT
