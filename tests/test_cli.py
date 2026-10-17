import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from served_collapse_guard import cli
from served_collapse_guard import core as core_module
from served_collapse_guard.core import ProbeResult


def seat(requested, served, kind="none", ok=True, stable=True, error=None):
    vendors = cli.VendorMap()
    return ProbeResult(
        requested=requested,
        served=served,
        ok=ok,
        stable=stable,
        vendor=vendors.vendor_of(served) if served else "unknown",
        requested_vendor=vendors.vendor_of(requested),
        collapsed=kind != "none" if ok else None,
        collapsed_kind=kind,
        error=error,
    )


class ExitCodeContractTest(unittest.TestCase):
    def run_probe(self, mapping, argv):
        def fake(endpoint, model, **kw):
            return mapping[model]

        with mock.patch.object(cli, "probe_model", side_effect=fake):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = cli.main(argv)
        return code, buffer.getvalue()

    def test_clean_probe_exits_zero(self):
        code, out = self.run_probe(
            {"gpt-4o": seat("gpt-4o", "gpt-4o")},
            ["probe", "--endpoint", "h:1", "gpt-4o"],
        )
        self.assertEqual(code, 0, out)
        self.assertIn("ok", out)

    def test_collapse_exits_one(self):
        code, _ = self.run_probe(
            {"claude-sonnet-4-6": seat("claude-sonnet-4-6", "gemini-2-5-flash", "cross-vendor")},
            ["probe", "--endpoint", "h:1", "claude-sonnet-4-6"],
        )
        self.assertEqual(code, 1)

    def test_unknown_exits_two(self):
        code, out = self.run_probe(
            {"zzz-9": seat("zzz-9", None, "unknown", ok=False, error="timeout")},
            ["probe", "--endpoint", "h:1", "zzz-9"],
        )
        self.assertEqual(code, 2, out)
        self.assertIn("UNKNOWN", out)

    def test_found_collapse_outranks_a_later_unknown(self):
        mapping = {
            "claude-sonnet-4-6": seat("claude-sonnet-4-6", "gemini-2-5-flash", "cross-vendor"),
            "zzz-9": seat("zzz-9", None, "unknown", ok=False, error="timeout"),
        }
        code, _ = self.run_probe(mapping, ["probe", "--endpoint", "h:1", "claude-sonnet-4-6", "zzz-9"])
        self.assertEqual(code, 1)

    def test_no_models_is_usage_error(self):
        code, _ = self.run_probe({}, ["probe", "--endpoint", "h:1"])
        self.assertEqual(code, 3)

    def test_json_output_is_parsable_and_complete(self):
        code, out = self.run_probe(
            {"gpt-4o": seat("gpt-4o", "gpt-4o")},
            ["probe", "--endpoint", "h:1", "--json", "gpt-4o"],
        )
        rows = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]["requested"], "gpt-4o")
        self.assertIn("collapsed_kind", rows[0])


class JudgeCliTest(unittest.TestCase):
    def test_json_verdict_shape(self):
        mapping = {
            "claude-sonnet-4-6": seat("claude-sonnet-4-6", "gemini-2-5-flash", "cross-vendor"),
            "gpt-4o": seat("gpt-4o", "gemini-2-5-pro", "cross-vendor"),
        }

        def fake(endpoint, model, **kw):
            return mapping[model]

        with mock.patch("served_collapse_guard.core.probe_model", side_effect=fake):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = cli.main(["judge", "--endpoint", "h:1", "--json", "--seats", "claude-sonnet-4-6,gpt-4o"])
        report = json.loads(buffer.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(report["verdict"], "single-vendor")
        self.assertEqual(report["actual_vendors"], ["google"])

    def test_one_seat_is_usage_error(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(["judge", "--endpoint", "h:1", "--seats", "gpt-4o"])
        self.assertEqual(code, 3)


class EndpointGuardTest(unittest.TestCase):
    def test_bare_port_as_model_is_rejected(self):
        buffer = io.StringIO()
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            with redirect_stdout(buffer):
                code = cli.main(["probe", "--endpoint", "h:1", "8800"])
        self.assertEqual(code, 3)
        self.assertIn("looks like an endpoint", err.getvalue())


class VendorsSubcommandTest(unittest.TestCase):
    def test_offline_classification(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(["vendors", "claude-3-7", "gpt-5", "unheard-of-1"])
        out = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("anthropic", out)
        self.assertIn("unknown", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
