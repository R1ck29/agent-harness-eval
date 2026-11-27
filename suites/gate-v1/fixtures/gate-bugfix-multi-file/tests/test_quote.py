import unittest

from shipping import quote
from shipping.rates import rate_for
from shipping.weight import billable_weight


class TestQuote(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(quote("A", 1.0), 5.0)

    def test_case_insensitive_zone(self):
        self.assertEqual(quote("b", 2.0), 16.0)

    def test_half_kilo(self):
        self.assertEqual(quote("C", 0.5), 6.25)

    def test_rate_and_weight(self):
        self.assertEqual(rate_for("A"), 5.0)
        self.assertEqual(billable_weight(1.5), 1.5)


if __name__ == "__main__":
    unittest.main()
