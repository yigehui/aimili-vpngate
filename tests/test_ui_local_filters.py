import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_FILE = ROOT / "vpngate_manager.py"


class UiLocalFiltersTests(unittest.TestCase):
    def test_node_and_pool_manage_filters_and_page_sizes_exist(self) -> None:
        text = UI_FILE.read_text(encoding="utf-8")
        required_snippets = [
            'id="exit_ip_filter"',
            'id="page_size_select"',
            'let pageSize = 20;',
            'id="pool_manage_country_filter"',
            'id="pool_manage_ip_type_filter"',
            'id="pool_manage_exit_ip_filter"',
            'id="pool_manage_page_size_select"',
            'let poolManagePageSize = 20;',
        ]
        for snippet in required_snippets:
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, text)


if __name__ == "__main__":
    unittest.main()
