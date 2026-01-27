# Fibonacci Tower Indicator

## Overview

**Fibonacci Tower** is a TradingView indicator written in Pine Script v5 that identifies buy signals based on a multi-condition strategy combining technical analysis tools with automatic Fibonacci level construction.

## Strategy Logic

The indicator generates buy signals when **all three conditions** are met simultaneously:

1. **Three Consecutive Bearish Candles**: Price action shows three candles in a row where the closing price is lower than the opening price, indicating downward momentum that may be exhausting.

2. **RSI Oversold Condition**: The Relative Strength Index (RSI) falls below the oversold threshold (default: 30), suggesting the asset may be oversold and due for a reversal.

3. **MACD Reversal**: The MACD line crosses above the signal line (bullish crossover) while the histogram was negative in the previous bar, indicating a potential momentum shift from bearish to bullish.

## Fibonacci Tower Feature

When a buy signal is detected, the indicator automatically constructs a "Fibonacci Tower" by:

1. Identifying the swing high and swing low within the lookback period (default: 100 bars)
2. Calculating and plotting seven Fibonacci retracement levels:
   - **0.0%** (Tower Base) - Red solid line
   - **23.6%** - Orange dashed line
   - **38.2%** - Yellow dashed line
   - **50.0%** - Blue solid line (thicker)
   - **61.8%** (Golden Ratio) - Green dashed line (thicker)
   - **78.6%** - Purple dashed line
   - **100.0%** (Tower Top) - Red solid line

These levels serve as potential support/resistance zones and profit-taking targets.

## Input Parameters

### RSI Settings
- **RSI Length**: Period for RSI calculation (default: 14)
- **RSI Oversold Level**: Threshold for oversold condition (default: 30)

### MACD Settings
- **MACD Fast Length**: Fast EMA period (default: 12)
- **MACD Slow Length**: Slow EMA period (default: 26)
- **MACD Signal Length**: Signal line period (default: 9)

### Fibonacci Settings
- **Fibonacci Lookback Period**: Bars to scan for swing high/low (default: 100)
- **Show Fibonacci Levels**: Toggle to display/hide Fibonacci lines

### Display Settings
- **Show Buy Signals**: Toggle to display/hide buy signal markers
- **Show Level Labels**: Toggle to display/hide Fibonacci level labels

## How to Use

### Installation

1. Open TradingView and navigate to the Pine Editor
2. Create a new indicator
3. Copy the entire code from `fibonacci_tower.pine`
4. Save and add to your chart

### Interpretation

- **Green Triangle Up + "BUY" label**: Indicates all three conditions are met
- **Green background highlight**: Visual confirmation of buy signal
- **Fibonacci levels**: Use these as potential targets or areas to watch for price reactions

### Best Practices

- **Confirm signals**: Don't rely solely on this indicator. Use additional analysis like volume, support/resistance zones, and higher timeframe trends
- **Risk management**: Always use stop losses below the signal candle or below the 0% Fibonacci level
- **Profit targets**: Consider taking partial profits at key Fibonacci levels (38.2%, 50%, 61.8%)
- **Timeframe**: Test the indicator on different timeframes to find what works best for your trading style

## Alert Setup

The indicator includes a built-in alert condition:

1. Right-click on the chart → "Add Alert"
2. Select "Fibonacci Tower Buy Signal" from the condition dropdown
3. Configure your notification preferences
4. Create the alert

You'll receive notifications whenever all three conditions align for a buy signal.

## Technical Implementation Details

- **Pine Script Version**: v5
- **Overlay**: Yes (indicator appears on the price chart)
- **Maximum Lines**: 500 (allows for historical Fibonacci levels)
- **Calculations**:
  - RSI: Uses `ta.rsi()` function
  - MACD: Uses `ta.macd()` function returning line, signal, and histogram
  - Swing points: Uses `ta.highest()` and `ta.lowest()` over lookback period

## Limitations and Considerations

- **Lagging indicators**: RSI and MACD are based on historical prices and may lag in fast-moving markets
- **False signals**: No indicator is perfect; false signals can occur, especially in ranging markets
- **Whipsaws**: Multiple buy signals may appear in choppy conditions
- **Lookback dependency**: Fibonacci levels quality depends on the selected lookback period

## Customization Ideas

You can modify the script to:
- Add additional filters (e.g., volume confirmation, trend filter)
- Adjust the number of bearish candles required
- Change RSI and MACD parameters for different assets or timeframes
- Add sell signal logic for a complete strategy
- Extend Fibonacci levels beyond 100% (161.8%, 200%, etc.)

## Troubleshooting

### 🚨 IMPORTANT: Check the Debug Table First! (v1.2+)

The indicator now shows a **debug table in the top-right corner** with critical information:

```
Buy Signals: 0        ← If this is 0 (RED), signals haven't triggered yet
RSI: 45.23           ← Current RSI value (GREEN if < 30)
Fib Active: NO       ← Whether Fibonacci lines are currently drawn
Swing Range: N/A     ← Calculated swing high-low range
Status: No signals yet ← Overall status message
```

### No Visualization Appearing?

**Step 1: Look at the Debug Table**

#### Case A: "Buy Signals: 0" (shown in RED)
**Meaning**: No buy signals have triggered yet on this chart.

**Why**: The indicator requires ALL three conditions simultaneously:
- 3 consecutive bearish candles (close < open for 3 bars)
- RSI below 30 (oversold)
- MACD bullish crossover with negative histogram

These conditions are RARE and may not occur on all timeframes/assets.

**Solutions**:
1. ✅ **Try different timeframes**: 4H or 1D work better than 15m or 1H
2. ✅ **Try volatile pairs**: BTC/USD, ETH/USD, or crypto pairs during corrections
3. ✅ **Scroll through historical data**: Look back several weeks or months
4. ✅ **Temporarily adjust RSI**: In settings, try RSI=35 or RSI=40 for testing
5. ✅ **Watch for partial markers**:
   - **Orange "3"**: Three bearish candles condition met
   - **Blue "R"**: RSI oversold condition met
   - **Purple "M"**: MACD bullish crossover occurred

   When you see all three markers together, you're close to a signal!

#### Case B: "Buy Signals: 5" (GREEN) but "Fib Active: NO"
**Meaning**: Signals triggered but lines weren't drawn due to validation failure.

**Likely causes**:
- Swing high equals swing low (no price range)
- Invalid coordinate values

**Solution**: This is a bug - please report with screenshot!

#### Case C: "Buy Signals: 5" (GREEN) and "Fib Active: YES" but can't see lines
**Meaning**: Lines are drawn but not visible in current view.

**Solutions**:
1. ✅ **Scroll to the signal bar**: Lines appear where the buy signal triggered
2. ✅ **Zoom out vertically**: Lines might be outside visible price range
3. ✅ **Check other indicators**: Too many overlays might hide the lines
4. ✅ **Verify settings**: "Show Fibonacci Levels" must be enabled
5. ✅ **Check line visibility**: Lines are now thicker (width=2-3) and solid style

**Step 2: Verify Settings**
- Ensure "Show Fibonacci Levels" is enabled
- Ensure "Show Buy Signals" is enabled
- Ensure "Show Level Labels" is enabled

**Step 3: Chart Requirements**
- Need at least 100+ bars of history (default lookback period)
- Sufficient price volatility to create oversold conditions

**Step 4: Script Compilation**
- No compilation errors in Pine Editor
- Pine Script v5 compatible

### Common Issues

- **"No signals yet" status**: Normal if market conditions don't meet all three criteria. Try different assets/timeframes.
- **Lines not extending**: Lines use `extend=extend.right` to auto-extend to chart edge
- **Labels not visible**: Labels now have white text on semi-transparent colored backgrounds (enhanced in v1.2)
- **Old signals disappearing**: By design, only most recent signal's levels are shown
- **Partial conditions met**: You'll see "3", "R", or "M" markers when individual conditions are true

## Version History

- **v1.2**: Major enhancement with debugging tools (2026-01-27)
  - ✅ **Added debug table** showing real-time indicator status
  - ✅ **Visual condition markers** (3/R/M) for partial signals
  - ✅ **Enhanced line visibility** with thicker, solid lines (width=2-3)
  - ✅ **Improved label contrast** with white text on colored backgrounds
  - ✅ **Better validation** with fibRange > 0 check before drawing
  - ✅ **Safer line deletion** with existence check before delete
  - ✅ **Buy signal counter** to track historical trigger count
  - ⚠️ **Key insight**: If you see no lines, check the debug table first!

- **v1.1**: Bug fixes for visualization (2026-01-27)
  - Fixed line rendering using `extend=extend.right` instead of future bar indices
  - Added validation to prevent drawing with NA coordinates
  - Improved label positioning for consistency
  - Enhanced reliability in real-time mode

- **v1.0**: Initial release with core functionality
  - Three bearish candles detection
  - RSI oversold condition
  - MACD reversal detection
  - Automatic Fibonacci tower construction
  - Visual buy signals and alerts

## Credits

Developed for TRADERAGENT crypto trading system.

## License

This source code is subject to the terms of the Mozilla Public License 2.0.

## References

For more information on the technical indicators used:
- [Fibonacci Retracements Guide](https://pineify.app/resources/blog/pine-script-fibonacci-guide)
- [Combining Indicators in Pine Script](https://pineify.app/resources/blog/how-to-combine-two-indicators-in-tradingview-pine-script)
- [Pine Script Documentation](https://www.tradingview.com/pine-script-docs/)
