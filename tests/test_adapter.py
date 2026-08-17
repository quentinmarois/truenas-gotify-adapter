import importlib.util
import pathlib
import sys
import unittest


MODULE_PATH = pathlib.Path(__file__).parents[1] / "truenas-gotify.py"
spec = importlib.util.spec_from_file_location("truenas_gotify", MODULE_PATH)
adapter = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = adapter
spec.loader.exec_module(adapter)


class UrlTests(unittest.TestCase):
    def test_normalize_truenas_https(self):
        self.assertEqual(
            adapter.normalize_truenas_ws_url("https://nas.example.com"),
            "wss://nas.example.com/api/current",
        )

    def test_normalize_truenas_http(self):
        self.assertEqual(
            adapter.normalize_truenas_ws_url("http://10.0.0.2/"),
            "ws://10.0.0.2/api/current",
        )

    def test_normalize_truenas_ws_path(self):
        self.assertEqual(
            adapter.normalize_truenas_ws_url("wss://nas.example.com/api/current"),
            "wss://nas.example.com/api/current",
        )

    def test_normalize_bare_host(self):
        self.assertEqual(
            adapter.normalize_truenas_ws_url("nas.example.com"),
            "wss://nas.example.com/api/current",
        )

    def test_normalize_gotify(self):
        self.assertEqual(
            adapter.normalize_gotify_url("https://gotify.example.com/"),
            "https://gotify.example.com/message",
        )


class FormattingTests(unittest.TestCase):
    def test_html_to_text(self):
        self.assertEqual(
            adapter.html_to_text("Pool &lt;tank&gt;<br><ul><li>Disk failed</li></ul>"),
            "Pool <tank>\nDisk failed",
        )

    def test_alert_key_prefers_uuid(self):
        self.assertEqual(adapter.alert_key({"uuid": "abc", "id": "other"}), "abc")


class PriorityTests(unittest.TestCase):
    def test_default_priority_bands(self):
        self.assertEqual(adapter.DEFAULT_PRIORITIES["INFO"], 1)
        self.assertEqual(adapter.DEFAULT_PRIORITIES["WARNING"], 4)
        self.assertEqual(adapter.DEFAULT_PRIORITIES["CRITICAL"], 8)
        self.assertEqual(adapter.DEFAULT_PRIORITIES["EMERGENCY"], 10)


if __name__ == "__main__":
    unittest.main()
