"""Regression tests for the collapse verdict.

Anti-false-green rules each case guards:
* positive  -- a real collapse must be reported (both cross-vendor and downgrade)
* negative  -- a clean route must stay silent, else the guard is a noise source
* absence   -- no answer must never read as a pass
* flux      -- an answer that varies between samples is not evidence
"""

import json
import unittest
from unittest import mock

from served_collapse_guard import core
from served_collapse_guard.core import (
    _names_compatible,
    extract_served,
    judge_seats,
    normalize_base,
    probe_model,
    probe_once,
)
from served_collapse_guard.vendors import VendorMap


def opener_for(payload, status=200):
    raw = json.dumps(payload).encode()

    class Response:
        def read(self):
            return raw

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout=None):
        if status >= 400:
            raise core.urllib.error.HTTPError(request.full_url, status, "err", {}, None)
        return Response()

    return opener


class BaseUrlTest(unittest.TestCase):
    def test_host_port(self):
        self.assertEqual(normalize_base("127.0.0.1:8000"), "http://127.0.0.1:8000/v1/chat/completions")

    def test_v1_suffix(self):
        self.assertEqual(normalize_base("https://gw.test/v1"), "https://gw.test/v1/chat/completions")

    def test_full_url_passthrough(self):
        self.assertEqual(normalize_base("https://gw.test/x/completions"), "https://gw.test/x/completions")

    def test_trailing_slash_and_scheme(self):
        self.assertEqual(normalize_base("http://localhost:9/v1/"), "http://localhost:9/v1/chat/completions")

    def test_model_name_is_not_an_endpoint(self):
        with self.assertRaises(ValueError):
            normalize_base("claude-sonnet-4-6")


class ExtractTest(unittest.TestCase):
    def test_metadata_wins_over_model_echo(self):
        payload = {"model": "router-default", "metadata": {"served_model": "gemini-2-5"}}
        self.assertEqual(extract_served(payload)[0], "gemini-2-5")

    def test_falls_back_to_model(self):
        self.assertEqual(extract_served({"model": "gpt-4o"})[0], "gpt-4o")

    def test_non_string_is_not_a_model(self):
        self.assertEqual(extract_served({"model": {"nested": 1}})[0], None)

    def test_custom_field_path(self):
        payload = {"x_org": {"served": "llama-3-3"}}
        self.assertEqual(extract_served(payload, ("x_org.served",))[0], "llama-3-3")


class NameCompatibilityTest(unittest.TestCase):
    def test_pinned_date_is_same_model(self):
        self.assertTrue(_names_compatible("gpt-4o", "gpt-4o-2024-11-20"))

    def test_different_family_is_not(self):
        self.assertFalse(_names_compatible("gpt-4o", "gpt-5"))


class ProbeModelTest(unittest.TestCase):
    def _run(self, served, requested, model, samples=2, **kw):
        op = opener_for({"model": "x", "metadata": {"served_model": served, "requested_model": requested}})
        return probe_model("h:1", model, samples=samples, opener=op, **kw)

    def test_cross_vendor_collapse(self):
        r = self._run("gemini-2-5-flash", "claude-sonnet-4-6", "claude-sonnet-4-6")
        self.assertTrue(r.ok)
        self.assertTrue(r.collapsed)
        self.assertEqual(r.collapsed_kind, "cross-vendor")
        self.assertEqual(r.vendor, "google")
        self.assertEqual(r.requested_vendor, "anthropic")

    def test_same_vendor_downgrade_is_visible(self):
        # does not break vendor de-duplication, but capability was silently cut
        r = self._run("gemini-2-5-flash-lite", "gemini-2-5-pro", "gemini-2-5-pro")
        self.assertTrue(r.collapsed)
        self.assertEqual(r.collapsed_kind, "downgrade")

    def test_clean_route_is_silent(self):
        r = self._run("claude-sonnet-4-6", "claude-sonnet-4-6", "claude-sonnet-4-6")
        self.assertFalse(r.collapsed)
        self.assertEqual(r.collapsed_kind, "none")

    def test_pinned_suffix_is_not_a_downgrade(self):
        r = self._run("gpt-4o-2024-11-20", "gpt-4o", "gpt-4o")
        self.assertFalse(r.collapsed)

    def test_host_port_still_becomes_http_for_loopback(self):
        self.assertEqual(core.normalize_base("127.0.0.1:8000"),
                         "http://127.0.0.1:8000/v1/chat/completions")

    def test_public_host_without_scheme_becomes_https(self):
        self.assertEqual(core.normalize_base("gw.example.com"),
                         "https://gw.example.com/v1/chat/completions")

    def test_strict_names_does_flag_pinned_suffix(self):
        r = self._run("gpt-4o-2024-11-20", "gpt-4o", "gpt-4o", strict_names=True)
        self.assertEqual(r.collapsed_kind, "downgrade")

    def test_router_alias_never_collapses(self):
        r = self._run("qwen-2-5", "auto-router/x", "auto-router/x")
        self.assertFalse(r.collapsed)
        self.assertEqual(r.collapsed_kind, "alias")
        self.assertTrue(r.is_alias)

    def test_unmapped_requested_name_is_not_a_verdict(self):
        # without a vendor claim there is nothing to collapse against
        r = self._run("gemini-2-5", "zzz-unknown-9", "zzz-unknown-9")
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "unmapped_requested_model")
        self.assertIsNone(r.collapsed)

    def test_unstable_route_is_not_evidence(self):
        side = [
            {"ok": True, "served": "gemini-2-5-flash"},
            {"ok": True, "served": "qwen-2-5"},
        ]
        with mock.patch.object(core, "probe_once", side_effect=side):
            r = probe_model("h:1", "claude-sonnet-4-6", samples=2)
        self.assertFalse(r.stable)
        self.assertFalse(r.usable)
        self.assertEqual(sorted(r.samples), ["gemini-2-5-flash", "qwen-2-5"])

    def test_no_answer_is_unknown(self):
        with mock.patch.object(core, "probe_once", return_value={"ok": False, "served": None, "error": "timeout"}):
            r = probe_model("h:1", "claude-sonnet-4-6", samples=1)
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "timeout")
        self.assertIsNone(r.collapsed)


class ProbeOnceTest(unittest.TestCase):
    def test_missing_served_field(self):
        r = probe_once("h:1", "m", opener=opener_for({"id": "chatcmpl-1"}))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "no_served_field")

    def test_http_error_is_not_a_pass(self):
        r = probe_once("h:1", "m", opener=opener_for({}, status=500))
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], 500)

    def test_unparsable_body(self):
        class Response:
            def read(self):
                return b"not json"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        r = probe_once("h:1", "m", opener=lambda request, timeout=None: Response())
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "unparsable_json")


class JudgeTest(unittest.TestCase):
    def _seat(self, requested, served, kind="none"):
        vendors = VendorMap()
        return core.ProbeResult(
            requested=requested,
            served=served,
            ok=True,
            stable=True,
            vendor=vendors.vendor_of(served),
            requested_vendor=vendors.vendor_of(requested),
            collapsed=kind != "none",
            collapsed_kind=kind,
        )

    def _judge(self, mapping):
        def fake(endpoint, model, **kw):
            return mapping[model]

        with mock.patch.object(core, "probe_model", side_effect=lambda e, m, **kw: fake(e, m, **kw)):
            return judge_seats("h:1", list(mapping))

    def test_all_seats_land_on_one_vendor(self):
        report = self._judge({
            "claude-sonnet-4-6": self._seat("claude-sonnet-4-6", "gemini-2-5-flash", "cross-vendor"),
            "gpt-4o": self._seat("gpt-4o", "gemini-2-5-pro", "cross-vendor"),
        })
        self.assertEqual(report.verdict, "single-vendor")
        self.assertEqual(report.exit_code, 1)

    def test_distinct_vendors_pass(self):
        report = self._judge({
            "claude-sonnet-4-6": self._seat("claude-sonnet-4-6", "claude-sonnet-4-6"),
            "gpt-4o": self._seat("gpt-4o", "gpt-4o"),
            "gemini-2-5-pro": self._seat("gemini-2-5-pro", "gemini-2-5-pro"),
        })
        self.assertEqual(report.verdict, "multi-vendor")
        self.assertEqual(report.exit_code, 0)

    def test_partial_collapse_reported(self):
        # 3 claimed vendors, 2 answer from their own, one seat lands on another
        report = self._judge({
            "claude-sonnet-4-6": self._seat("claude-sonnet-4-6", "claude-sonnet-4-6"),
            "gemini-2-5-pro": self._seat("gemini-2-5-pro", "claude-sonnet-4-6", "cross-vendor"),
            "gpt-4o": self._seat("gpt-4o", "gpt-4o"),
        })
        self.assertEqual(report.nominal_vendors, ["anthropic", "google", "openai"])
        self.assertEqual(report.actual_vendors, ["anthropic", "openai"])
        self.assertEqual(report.verdict, "partial-collapse")
        self.assertEqual(report.exit_code, 1)

    def test_two_seats_both_landing_on_one_vendor_is_single_vendor(self):
        report = self._judge({
            "claude-sonnet-4-6": self._seat("claude-sonnet-4-6", "claude-sonnet-4-6"),
            "gemini-2-5-pro": self._seat("gemini-2-5-pro", "claude-sonnet-4-6", "cross-vendor"),
        })
        self.assertEqual(report.verdict, "single-vendor")
        self.assertEqual(report.exit_code, 1)

    def test_unreachable_seat_is_not_counted_as_agreement(self):
        dead = core.ProbeResult(requested="gpt-4o", ok=False, error="timeout", requested_vendor="openai")
        report = self._judge({
            "claude-sonnet-4-6": self._seat("claude-sonnet-4-6", "claude-sonnet-4-6"),
            "gpt-4o": dead,
        })
        self.assertEqual(report.verdict, "insufficient-evidence")
        self.assertEqual(report.exit_code, 2)
        self.assertEqual(report.unusable, ["gpt-4o"])

    def test_unstable_seat_blocks_the_claim(self):
        wobbling = self._seat("gpt-4o", "gpt-4o")
        wobbling.stable = False
        report = self._judge({
            "claude-sonnet-4-6": self._seat("claude-sonnet-4-6", "claude-sonnet-4-6"),
            "gpt-4o": wobbling,
        })
        self.assertEqual(report.verdict, "insufficient-evidence")

    def test_alias_seat_does_not_claim_a_vendor(self):
        alias = core.ProbeResult(
            requested="auto-router/x", served="gemini-2-5-flash", ok=True, stable=True,
            vendor="google", requested_vendor="unknown", collapsed=False,
            collapsed_kind="alias", is_alias=True,
        )
        report = self._judge({
            "auto-router/x": alias,
            "claude-sonnet-4-6": self._seat("claude-sonnet-4-6", "claude-sonnet-4-6"),
        })
        self.assertEqual(report.nominal_vendors, ["anthropic"])
        self.assertEqual(report.actual_vendors, ["anthropic", "google"])
        self.assertEqual(report.verdict, "multi-vendor")


if __name__ == "__main__":
    unittest.main(verbosity=2)
