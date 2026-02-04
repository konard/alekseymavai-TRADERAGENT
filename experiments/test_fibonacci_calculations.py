#!/usr/bin/env python3
"""
Test script to verify Fibonacci calculations match the example from issue #97
"""

def calculate_fibonacci_levels(low, high, candle_type="green"):
    """
    Calculate Fibonacci levels for trading

    Args:
        low: Low price of the candle
        high: High price of the candle
        candle_type: "green" for LONG or "red" for SHORT

    Returns:
        dict: Dictionary with all Fibonacci levels
    """
    if candle_type == "green":
        # For green candle (LONG):
        # 1.0 = Low (base)
        # 0.0 = High (entry #1)
        fib_base = low
        fib_top = high
        fib_range = fib_top - fib_base

        levels = {
            "1.0 (Base)": low,
            "0.820 (Stop Loss)": low + fib_range * 0.180,
            "0.618 (Entry #3)": low + fib_range * 0.382,
            "0.5 (Entry #2)": low + fib_range * 0.500,
            "0.0 (Entry #1)": high,
            "-0.618 (TP1)": high + fib_range * 0.618,
            "-1.618 (TP2)": high + fib_range * 1.618,
            "-2.618 (TP3)": high + fib_range * 2.618,
        }
    else:  # red candle
        # For red candle (SHORT):
        # 1.0 = High (base)
        # 0.0 = Low (entry #1)
        fib_base = high
        fib_top = low
        fib_range = fib_base - fib_top

        levels = {
            "1.0 (Base)": high,
            "0.820 (Stop Loss)": high - fib_range * 0.180,
            "0.618 (Entry #3)": high - fib_range * 0.382,
            "0.5 (Entry #2)": high - fib_range * 0.500,
            "0.0 (Entry #1)": low,
            "-0.618 (TP1)": low - fib_range * 0.618,
            "-1.618 (TP2)": low - fib_range * 1.618,
            "-2.618 (TP3)": low - fib_range * 2.618,
        }

    return levels, fib_range


def test_example_from_issue():
    """Test the exact example from issue #97"""
    print("=" * 70)
    print("TEST: Example from Issue #97")
    print("=" * 70)

    # Example from issue: Green candle with Low = $93,000, High = $93,500
    low = 93000
    high = 93500

    print(f"\nGreen Candle (LONG):")
    print(f"Low:  ${low:,.0f}")
    print(f"High: ${high:,.0f}")

    levels, fib_range = calculate_fibonacci_levels(low, high, "green")

    print(f"\nRange: ${fib_range:,.0f}")
    print("\nCalculated Fibonacci Levels:")
    print("-" * 70)

    for level_name, price in levels.items():
        print(f"{level_name:25s} = ${price:,.2f}")

    # Expected values from issue
    print("\n" + "=" * 70)
    print("Expected values from Issue #97:")
    print("-" * 70)
    print(f"{'0.5 (Entry #2)':25s} = $93,250  (Expected)")
    print(f"{'0.618 (Entry #3)':25s} = $93,191  (Expected)")
    print(f"{'0.82 (Stop Loss)':25s} = $93,080  (Expected)")
    print(f"{'-0.618 (TP1)':25s} = $93,809  (Expected)")
    print(f"{'-1.618 (TP2)':25s} = $94,309  (Expected)")
    print(f"{'-2.618 (TP3)':25s} = $94,809  (Expected)")

    # Verify calculations
    print("\n" + "=" * 70)
    print("VERIFICATION:")
    print("-" * 70)

    tolerance = 1.0  # Allow $1 difference due to rounding

    checks = [
        ("Entry #2 (0.5)", levels["0.5 (Entry #2)"], 93250),
        ("Entry #3 (0.618)", levels["0.618 (Entry #3)"], 93191),
        ("Stop Loss (0.820)", levels["0.820 (Stop Loss)"], 93080),
        ("TP1 (-0.618)", levels["-0.618 (TP1)"], 93809),
        ("TP2 (-1.618)", levels["-1.618 (TP2)"], 94309),
        ("TP3 (-2.618)", levels["-2.618 (TP3)"], 94809),
    ]

    all_passed = True
    for name, calculated, expected in checks:
        diff = abs(calculated - expected)
        status = "✓ PASS" if diff <= tolerance else "✗ FAIL"
        print(f"{name:20s}: Calculated=${calculated:,.2f}, Expected=${expected:,.2f}, Diff=${diff:,.2f} {status}")
        if diff > tolerance:
            all_passed = False

    print("\n" + "=" * 70)
    if all_passed:
        print("✓ ALL TESTS PASSED!")
    else:
        print("✗ SOME TESTS FAILED - Check calculations")
    print("=" * 70)


def test_red_candle_example():
    """Test a red candle (SHORT) example"""
    print("\n\n" + "=" * 70)
    print("TEST: Red Candle (SHORT) Example")
    print("=" * 70)

    # Example: Red candle with High = $95,000, Low = $94,500
    low = 94500
    high = 95000

    print(f"\nRed Candle (SHORT):")
    print(f"High: ${high:,.0f}")
    print(f"Low:  ${low:,.0f}")

    levels, fib_range = calculate_fibonacci_levels(low, high, "red")

    print(f"\nRange: ${fib_range:,.0f}")
    print("\nCalculated Fibonacci Levels:")
    print("-" * 70)

    for level_name, price in levels.items():
        print(f"{level_name:25s} = ${price:,.2f}")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    test_example_from_issue()
    test_red_candle_example()
