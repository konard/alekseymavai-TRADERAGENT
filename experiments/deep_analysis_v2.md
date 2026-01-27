# Deep Analysis v2: Why Fibonacci Tower Still Not Showing

## Problem Statement
User confirms that after the first fix (PR #10 merged), the visualization still does not appear on the TradingView chart.

## Code Flow Analysis

### Current Logic Flow:
```
1. Calculate RSI, MACD, bearish candles (lines 32-43)
2. Determine buySignal (line 50)
3. IF buySignal → Update swingHigh, swingLow (lines 63-68)
4. Calculate Fibonacci levels using swingHigh, swingLow (lines 71-78)
5. IF buySignal AND showFibLevels AND not na() → Draw lines (lines 112-147)
```

## Critical Issues Identified

### Issue 1: Line Deletion Problem
**Lines 113-128**: Every time `buySignal` is true, we DELETE the old lines first.

**Problem**: In Pine Script, when you use `var` variables for lines, they persist across bars. However, the current logic:
- Creates lines only when `buySignal` is true
- But `buySignal` is typically only true for ONE bar
- On subsequent bars, the condition `if buySignal and showFibLevels` is FALSE
- So the lines drawn on that one bar just stay there (which is good)

**BUT**: If another buySignal occurs, we delete the old lines and create new ones. This is correct behavior.

### Issue 2: The Real Root Cause - Timing Issue

Looking at lines 63-68 and 71-78:

```pine
if buySignal
    swingHigh := ta.highest(high, fibLookback)  // Line 65
    swingLow := ta.lowest(low, fibLookback)      // Line 66

fibRange = swingHigh - swingLow                  // Line 71
fib0 = swingLow                                  // Line 72
...
```

**CRITICAL PROBLEM**:
- `swingHigh` and `swingLow` are initialized as `na` (line 57-58)
- On the FIRST ever bar where `buySignal` is true, they get updated
- But the Fibonacci calculations happen IMMEDIATELY after (lines 71-78)
- The check `if buySignal and showFibLevels and not na(swingHigh) and not na(swingLow)` (line 112) should work

**Wait - this should actually work!**

### Issue 3: Let's trace the ACTUAL execution

On bar where buySignal = true:
1. Line 63: `if buySignal` → TRUE
2. Lines 65-66: swingHigh and swingLow are set to valid values
3. Lines 71-78: Fibonacci levels calculated with valid values
4. Line 112: `if buySignal and showFibLevels and not na(swingHigh) and not na(swingLow)` → TRUE
5. Lines 131-137: Lines are drawn

**This should work!** So why doesn't it?

### Issue 4: The REAL Problem - Line Coordinates

Look at line 131:
```pine
fibLine0 := line.new(bar_index, fib0, bar_index + 1, fib0, ...)
```

We're creating a line from `bar_index` to `bar_index + 1` with `extend=extend.right`.

**Potential Issue**: What if `fib0`, `fib236`, etc. are outside the visible price range?

**More likely**: The lines ARE being created, but user might:
1. Not be scrolled to the right place
2. Not have the right timeframe
3. Not see buySignals triggering at all
4. Have lines but they're off-screen price-wise

### Issue 5: Missing Validation for fibRange

Lines 71-78 calculate Fibonacci levels:
```pine
fibRange = swingHigh - swingLow
fib0 = swingLow
fib236 = swingLow + fibRange * 0.236
...
```

If `swingHigh == swingLow`, then `fibRange = 0`, and all Fibonacci levels collapse to the same price. The lines would be drawn but would all be on top of each other.

## Root Cause Hypothesis

**Most Likely Causes:**

1. **No buySignals triggering**: The conditions are very strict:
   - 3 consecutive bearish candles
   - RSI < 30
   - MACD bullish crossover with previous histogram negative

   This might be very rare!

2. **Lines drawn but not visible**: Lines might be created but:
   - User needs to scroll to see them
   - Price range doesn't include the Fibonacci levels
   - Timeframe doesn't have qualifying signals

3. **buySignal triggers but lines disappear**: If there's a runtime error in line creation (though Pine Script should show errors)

## Testing Strategy

1. Add debugging plots to confirm buySignals are triggering
2. Add alert to confirm when lines are drawn
3. Reduce strictness of buySignal conditions for testing
4. Add plotchar to show when lines are being created

## Recommended Fix

Add more robust error handling and debugging:
1. Plot buySignal as a separate indicator
2. Add plotchar when lines are created
3. Ensure lines persist properly
4. Consider making the buy conditions less strict for testing
