# Root Cause Hypothesis - Final Analysis

## The Real Problem

After deep analysis, I believe the issue is one of these:

### Hypothesis 1: buySignal Never Triggers (MOST LIKELY)
The conditions are EXTREMELY strict:
- 3 consecutive bearish candles
- RSI < 30 (oversold)
- MACD bullish crossover
- Previous MACD histogram < 0

This combination might occur very rarely or never on the user's chart/timeframe.

**Evidence**: User says "no visualization" - not "visualization disappeared" or "visualization is wrong". This suggests lines were never drawn at all.

**Test**: User needs to:
1. Check the debug table (buy signal count)
2. Try relaxed conditions
3. Look at different timeframes (4H or 1D on volatile pairs like BTC/USD)

### Hypothesis 2: Line Drawing Issue in Pine Script v5

In Pine Script v5, there's a known behavior with var + line.delete() + line.new():

**Problem Pattern**:
```pine
var line myLine = na
if condition
    line.delete(myLine)  // Deletes the line
    myLine := line.new(...)  // Creates new line
```

**Potential Issue**: If the line object isn't properly deleted or if there's a timing issue, the new line might not render.

**Better Pattern**:
```pine
var line myLine = na
if condition
    if not na(myLine)
        line.delete(myLine)
    myLine := line.new(...)
```

OR use line.set_* functions to update existing lines instead of delete+create.

### Hypothesis 3: Coordinate Validation Issue

Even with the check `not na(swingHigh) and not na(swingLow)`, there could be issues:
- swingHigh == swingLow (fibRange = 0)
- Lines are off-screen (price range is very different)
- bar_index + 1 might cause issues

### Hypothesis 4: extend=extend.right Not Working as Expected

Some users report that `extend=extend.right` doesn't always work as expected in overlay indicators, especially if:
- The chart is zoomed in a certain way
- There are too many lines
- The indicator is in a certain panel

## Recommended Fix Strategy

### Fix 1: Add Comprehensive Debugging (for user testing)
- Debug table showing signal counts
- Visual indicators for each condition
- Alert when lines are drawn
- Check RSI value in real-time

### Fix 2: Use More Robust Line Management
Instead of:
```pine
var line fibLine = na
if condition
    line.delete(fibLine)
    fibLine := line.new(...)
```

Use:
```pine
var line fibLine = na
if condition
    if na(fibLine)
        fibLine := line.new(...)
    else
        line.set_xy1(fibLine, bar_index, fib0)
        line.set_xy2(fibLine, bar_index + 1, fib0)
```

OR keep the delete approach but add more validation.

### Fix 3: Alternative Rendering Method
Instead of using lines with extend=extend.right, use plot() with plotchar() or shapes:
```pine
plot(buySignal ? fib0 : na, color=color.red, linewidth=2, style=plot.style_linebr)
```

This ensures the levels are always visible when condition is true.

### Fix 4: Keep Lines Visible After Signal
The current approach only shows lines when buySignal = true (one bar).
We need lines to PERSIST across bars.

**Current**:
```pine
if buySignal and showFibLevels  // Only true for ONE bar
    // draw lines
```

**Should be** (if we want continuous levels):
```pine
var bool hasSignaled = false
if buySignal
    hasSignaled := true

if hasSignaled and showFibLevels
    // keep drawing/updating lines
```

Wait - actually the current approach IS correct because we use `var line` which persists, and `extend=extend.right` extends the line. So lines should persist after being drawn once.

## Conclusion

**Most Likely Issue**: buySignal simply never triggers on the user's chart.

**Fix**:
1. Add debugging to confirm this hypothesis
2. Provide relaxed conditions for testing
3. Improve line width and visibility
4. Add validation for fibRange > 0

**User needs to**:
1. Test on BTC/USD or ETH/USD (volatile pairs)
2. Use 4H or 1D timeframe
3. Scroll through historical data
4. Check RSI indicator separately to see if it ever goes below 30
