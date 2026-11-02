import unittest

from fxbot.indicators import atr, ema


class EmaTests(unittest.TestCase):
    def test_hand_calculated(self):
        # SMA(1,2,3)=2 at idx2; k=0.5: idx3=4*.5+2*.5=3; idx4=5*.5+3*.5=4
        self.assertEqual(ema([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])

    def test_too_short_is_all_none(self):
        self.assertEqual(ema([1, 2], 3), [None, None])

    def test_bad_period(self):
        with self.assertRaises(ValueError):
            ema([1, 2, 3], 0)


class AtrTests(unittest.TestCase):
    def test_hand_calculated(self):
        highs = [10, 11, 12, 13]
        lows = [8, 9, 9, 11]
        closes = [9, 10, 11, 12]
        # TR = [2, 2, 3, 2]; ATR3 idx2 = 7/3; idx3 = (7/3*2 + 2)/3 = 20/9
        result = atr(highs, lows, closes, 3)
        self.assertIsNone(result[0])
        self.assertIsNone(result[1])
        self.assertAlmostEqual(result[2], 7 / 3, places=12)
        self.assertAlmostEqual(result[3], 20 / 9, places=12)

    def test_gap_uses_previous_close(self):
        # second candle gaps up: TR = |high - prev_close| = 15 - 9 = 6
        result = atr([10, 15], [8, 14], [9, 14.5], 2)
        self.assertAlmostEqual(result[1], (2 + 6) / 2, places=12)

    def test_length_mismatch(self):
        with self.assertRaises(ValueError):
            atr([1, 2], [1], [1, 2], 2)


if __name__ == "__main__":
    unittest.main()
