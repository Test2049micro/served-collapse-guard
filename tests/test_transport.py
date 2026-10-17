"""Transport safety and name-lenience regressions found in review.

These cover the paths where the tool's *promises* and its behaviour diverged:
a bearer key sent in clear text, a key carried across a redirect, a userinfo URL
that reads like an internal host, `gpt-4o` -> `gpt-4o-mini` called compatible, and
a seat that failed 2 of 3 probes counted as stable evidence.
"""

import io
import json
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest import mock

from served_collapse_guard import cli
from served_collapse_guard.core import (
    _names_compatible,
    normalize_base,
    probe_model,
    probe_once,
)
from served_collapse_guard.vendors import VendorMap


class EndpointSafetyTest(unittest.TestCase):
    def test_bare_host_defaults_to_https(self):
        # the default used to be http, which put the bearer token in clear text on
        # exactly the internet-facing gateways this tool is pointed at
        self.assertTrue(normalize_base("api.example.com").startswith("https://"))

    def test_loopback_may_stay_cleartext(self):
        self.assertTrue(normalize_base("127.0.0.1:8000").startswith("http://"))
        self.assertTrue(normalize_base("localhost:4000/v1").startswith("http://"))

    def test_cleartext_public_endpoint_is_refused(self):
        with self.assertRaises(ValueError):
            normalize_base("http://gw.example.com")

    def test_cleartext_allowed_only_on_explicit_opt_in(self):
        self.assertTrue(normalize_base("http://gw.example.com", allow_insecure=True).startswith("http://"))

    def test_userinfo_is_refused(self):
        # https://gw.internal@attacker.example/v1 reads as an internal host and
        # connects to attacker.example
        with self.assertRaises(ValueError) as caught:
            normalize_base("https://gw.internal@attacker.example/v1")
        self.assertIn("userinfo", str(caught.exception))

    def test_unrecognised_base_path_is_refused_not_guessed(self):
        # /openai could want /openai/chat/completions or
        # /openai/v1/chat/completions; guessing produces a probe that talks to the
        # wrong place and then reports "no served field" as if the gateway were wrong
        with self.assertRaises(ValueError) as caught:
            normalize_base("https://gw.example.com/openai")
        self.assertIn("unrecognised path", str(caught.exception))
        self.assertEqual(normalize_base("https://gw.example.com/v1"),
                         "https://gw.example.com/v1/chat/completions")


class RedirectTest(unittest.TestCase):
    def test_redirect_is_reported_not_followed(self):
        # urllib re-attaches Authorization to the redirected request, so following
        # one would hand the key to the redirect target
        def opener(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 302, "found", {}, None)

        result = probe_once("https://gw.example.com", "gpt-4o", api_key="sk-secret",
                            opener=opener, timeout=2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "redirect_refused")

    def test_default_opener_is_not_plain_urlopen(self):
        with mock.patch("served_collapse_guard.core.build_opener") as built:
            built.return_value = mock.Mock(open=mock.Mock(side_effect=OSError("nope")))
            probe_once("127.0.0.1:1", "gpt-4o", timeout=1)
        built.assert_called_once()


class NameLenienceTest(unittest.TestCase):
    def test_pinned_date_is_compatible(self):
        self.assertTrue(_names_compatible("gpt-4o", "gpt-4o-2024-11-20"))

    def test_capability_suffix_is_not_compatible(self):
        # the shipped behaviour called these "the same model", hiding exactly the
        # silent downgrade the tool exists to surface
        for served in ("gpt-4o-mini", "gpt-4o-turbo", "gemini-2-5-flash-lite"):
            requested = "gpt-4o" if served.startswith("gpt") else "gemini-2-5-pro"
            with self.subTest(served=served):
                self.assertFalse(_names_compatible(requested, served))

    def test_downgrade_is_flagged_end_to_end(self):
        def opener(request, timeout=None):
            class Response:
                def read(self):
                    return json.dumps({"metadata": {"served_model": "gpt-4o-mini"}}).encode()

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            return Response()

        result = probe_model("127.0.0.1:1", "gpt-4o", samples=1, opener=opener)
        self.assertTrue(result.collapsed)
        self.assertEqual(result.collapsed_kind, "downgrade")


class StabilityTest(unittest.TestCase):
    def test_partial_failures_are_not_stable(self):
        side = [
            {"ok": True, "served": "gemini-2-5-pro"},
            {"ok": False, "served": None, "error": "timeout"},
            {"ok": True, "served": "gemini-2-5-pro"},
        ]
        with mock.patch("served_collapse_guard.core.probe_once", side_effect=side):
            result = probe_model("127.0.0.1:1", "gemini-2-5-pro", samples=3)
        self.assertFalse(result.stable, "2 of 3 agreeing is not a stable route")
        self.assertFalse(result.usable)


class UsageErrorExitCodeTest(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            return cli.main(argv), out.getvalue(), err.getvalue()

    def test_missing_vendor_map_exits_three(self):
        # exit 1 is "collapse found", 2 is "insufficient evidence"; a bad invocation
        # must not be readable as either
        code, _, err = self.run_cli(["probe", "--vendor-map", "/nonexistent/vendors.json", "gpt-4o"])
        self.assertEqual(code, 3, err)

    def test_malformed_vendor_map_exits_three(self):
        handle = NamedTemporaryFile("w", suffix=".json", delete=False)
        handle.write("[not, an, object]")
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        code, _, err = self.run_cli(["probe", "--vendor-map", handle.name, "gpt-4o"])
        self.assertEqual(code, 3, err)

    def test_header_without_equals_exits_three(self):
        code, _, err = self.run_cli(["probe", "--header", "oops", "gpt-4o"])
        self.assertEqual(code, 3, err)

    def test_cleartext_endpoint_reaches_the_user_as_usage_error(self):
        code, _, err = self.run_cli(["probe", "--endpoint", "http://gw.example.com", "gpt-4o"])
        self.assertEqual(code, 3, err)
        self.assertIn("cleartext", err)


class AliasMergeTest(unittest.TestCase):
    def test_file_aliases_extend_the_builtins(self):
        handle = NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"vendors": {"zz-": "zeta"}, "aliases": ["pool-"]}, handle)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        vendors = VendorMap.load(handle.name)
        self.assertTrue(vendors.is_router_alias("pool-3"))
        self.assertTrue(vendors.is_router_alias("auto-router/x"),
                        "a file that adds an alias must not drop the built-in one")
        self.assertEqual(vendors.vendor_of("zz-1"), "zeta")


class ApiKeySourceTest(unittest.TestCase):
    def test_general_purpose_env_var_is_not_read(self):
        # $OPENAI_API_KEY used to be picked up automatically and then sent to
        # whatever --endpoint named
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-from-generic-var"}, clear=True):
            args = cli.build_parser().parse_args(["probe", "gpt-4o"])
            self.assertIsNone(cli._resolve_api_key(args))

    def test_dedicated_env_var_is_read(self):
        with mock.patch.dict("os.environ", {"SERVED_GUARD_API_KEY": "sk-dedicated"}, clear=True):
            args = cli.build_parser().parse_args(["probe", "gpt-4o"])
            self.assertEqual(cli._resolve_api_key(args), "sk-dedicated")


if __name__ == "__main__":
    unittest.main(verbosity=2)
