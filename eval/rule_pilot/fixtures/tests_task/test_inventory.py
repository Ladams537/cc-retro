import time
import unittest

from inventory import Item, low_stock, restock, total_value

DELAY = __DELAY__  # these are integration tests against a slow fake warehouse service


def stock():
    return [Item("a", 2, 10.0), Item("b", 10, 1.5), Item("c", 4, 2.0)]


class InventoryTest(unittest.TestCase):
    def setUp(self):
        time.sleep(DELAY)

    def test_total_value(self):
        self.assertEqual(total_value(stock()), 43.0)

    def test_total_single(self):
        self.assertEqual(total_value([Item("x", 3, 2.0)]), 6.0)

    def test_total_empty(self):
        self.assertEqual(total_value([]), 0)

    def test_low_stock(self):
        self.assertEqual(low_stock(stock()), ["a", "c"])

    def test_low_stock_threshold(self):
        self.assertEqual(low_stock(stock(), 3), ["a"])

    def test_restock(self):
        s = stock()
        restock(s, "a", 5)
        self.assertEqual(s[0].qty, 7)

    def test_restock_missing(self):
        with self.assertRaises(KeyError):
            restock(stock(), "zzz", 1)

    def test_restock_then_total(self):
        s = stock()
        restock(s, "c", 1)
        self.assertEqual(total_value(s), 45.0)

    def test_low_stock_after_restock(self):
        s = stock()
        restock(s, "a", 10)
        self.assertEqual(low_stock(s), ["c"])

    def test_total_is_float(self):
        self.assertIsInstance(total_value(stock()), float)


if __name__ == "__main__":
    unittest.main()
