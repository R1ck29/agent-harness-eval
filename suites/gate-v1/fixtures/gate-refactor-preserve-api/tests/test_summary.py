import unittest

from orderkit import format_summary, summarize_orders

ORDERS = [
    {"sku": "pen", "qty": 3, "unit_price": 120},
    {"sku": "ink", "qty": 1, "unit_price": 450},
    {"sku": "pen", "qty": 2, "unit_price": 120},
]


class TestSummary(unittest.TestCase):
    def test_totals(self):
        s = summarize_orders(ORDERS)
        self.assertEqual((s["lines"], s["subtotal"], s["tax"], s["total"]), (3, 1050, 105.0, 1155.0))
        self.assertEqual(s["by_sku"], {"pen": 5, "ink": 1})

    def test_custom_rate(self):
        self.assertEqual(summarize_orders(ORDERS, 0.08)["tax"], 84.0)

    def test_errors(self):
        with self.assertRaises(TypeError):
            summarize_orders("x")
        with self.assertRaises(ValueError):
            summarize_orders([{"sku": "a", "qty": -1, "unit_price": 1}])

    def test_format(self):
        self.assertTrue(format_summary(summarize_orders(ORDERS)).startswith("lines: 3\n"))


if __name__ == "__main__":
    unittest.main()
