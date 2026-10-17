import json
import tempfile
import unittest
from pathlib import Path

from served_collapse_guard.vendors import VendorMap


class VendorOfTest(unittest.TestCase):
    def test_public_model_names(self):
        v = VendorMap()
        self.assertEqual(v.vendor_of("claude-sonnet-4-6"), "anthropic")
        self.assertEqual(v.vendor_of("gpt-4o-mini"), "openai")
        self.assertEqual(v.vendor_of("gemini-3-5-flash"), "google")
        self.assertEqual(v.vendor_of("grok-4"), "xai")
        self.assertEqual(v.vendor_of("deepseek-v3"), "deepseek")
        self.assertEqual(v.vendor_of("kimi-k2-0715"), "moonshot")

    def test_longest_prefix_wins(self):
        # "gpt-image" must classify as openai via its own rule, not as bare "gpt"
        self.assertEqual(VendorMap().vendor_of("gpt-image-1"), "openai")

    def test_unmapped_is_unknown_not_a_guess(self):
        # A wrong vendor guess silently corrupts a de-duplicated vote, so the
        # default answer for an unrecognised name must stay unknown.
        self.assertEqual(VendorMap().vendor_of("some-future-model-x"), "unknown")
        self.assertEqual(VendorMap().vendor_of(""), "unknown")
        self.assertEqual(VendorMap().vendor_of(None), "unknown")

    def test_matching_is_case_insensitive(self):
        self.assertEqual(VendorMap([("Gemini", "google")]).vendor_of("GEMINI-2"), "google")


class AliasTest(unittest.TestCase):
    def test_default_alias_pattern(self):
        v = VendorMap()
        self.assertTrue(v.is_router_alias("auto-router/deepseek-v3"))
        self.assertFalse(v.is_router_alias("deepseek-v3"))

    def test_extra_alias_extends_without_losing_default(self):
        v = VendorMap().with_aliases(["pool-"])
        self.assertTrue(v.is_router_alias("pool-7"))
        self.assertTrue(v.is_router_alias("auto-router/x"))
        self.assertEqual(v.vendor_of("claude-3"), "anthropic")


class LoadTest(unittest.TestCase):
    def _write(self, payload):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        handle.write(json.dumps(payload))
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        return handle.name

    def test_bare_mapping_extends_builtin(self):
        path = self._write({"spacebunny-x": "example-lab"})
        v = VendorMap.load(path)
        self.assertEqual(v.vendor_of("spacebunny-x"), "example-lab")
        self.assertEqual(v.vendor_of("claude-3"), "anthropic")

    def test_wrapped_form_with_aliases(self):
        path = self._write({"vendors": {"zz-": "zeta"}, "aliases": ["pool-"]})
        v = VendorMap.load(path)
        self.assertEqual(v.vendor_of("zz-1"), "zeta")
        self.assertTrue(v.is_router_alias("pool-3"))

    def test_none_path_is_builtin_table(self):
        self.assertEqual(VendorMap.load(None).vendor_of("llama-3-3"), "meta")


if __name__ == "__main__":
    unittest.main(verbosity=2)
