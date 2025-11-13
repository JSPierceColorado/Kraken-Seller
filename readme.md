# Kraken Trailing Sell Bot (Google Sheets + Kraken Spot API)

This bot monitors your **Kraken spot holdings** and manages a tracking sheet in Google Sheets called **`Kraken-Trader`** (inside the `Active-Investing` spreadsheet). It applies a **stop-loss** and **trailing take-profit** strategy per asset, optionally placing **real market sell orders** on Kraken.

It is designed to run continuously in a loop, updating every N seconds.

> ⚠️ **Warning:** This script can place **live sell orders** on Kraken. Use at your own risk. Start in `DRY_RUN` mode and/or with small test balances first.

---

## 🧾 What the Bot Does

Every cycle (`run_once`):

1. Connects to Kraken and fetches your balances.
2. Filters out:

   * Zero balances
   * Base currency (e.g. `USD`, `EUR`)
   * Fee tokens like `KFEE`
3. For each remaining asset (by **altname**, e.g. `XBT`, `ETH`):

   * Looks up or creates a row in the **Kraken-Trader** sheet.
   * Fetches the current price (`altname + BASE_CURRENCY`, e.g. `XBTUSD`).
   * Computes **unrealized %** gain/loss.
   * Tracks and updates **ATH (all-time-high) unrealized %**.
   * Applies the following rules:

     * **Stop-Loss:** if `unreal_pct <= -3%` (unarmed) → sell.
     * **Arm:** if `unreal_pct >= +5%` → `Armed = TRUE`.
     * **Trailing Take Profit:** if `Armed` and `(ATH - unreal_pct) >= 3%` → sell.
   * Places a **full-position market sell** for any triggered asset (unless `DRY_RUN` is enabled).
   * Writes the updated row back to Google Sheets (status, armed flag, P&L, timestamp).
4. After updating all current holdings, it scans sheet rows for assets that **no longer appear in Kraken balances** and marks them as **`CLOSED_EXTERNAL`**.

This process repeats indefinitely in `run_forever()` with a configurable polling interval.

---

## 📊 Google Sheets Layout

Spreadsheet name (default): **`Active-Investing`**
Worksheet title (default): **`Kraken-Trader`**

When the worksheet is created or first used, it expects the following headers in row 1 (A–L):

| Col | Header             | Description                                    |
| --- | ------------------ | ---------------------------------------------- |
| A   | `Asset`            | Kraken asset altname (e.g. `XBT`, `ETH`).      |
| B   | `KrakenAssetCode`  | Kraken internal asset code (e.g. `XXBT`).      |
| C   | `Pair`             | Trading pair (e.g. `XBTUSD`).                  |
| D   | `PositionSize`     | Current balance tracked (asset units).         |
| E   | `CostBasis`        | Entry price used for unrealized % calculation. |
| F   | `CurrentPrice`     | Last trade price (from ticker).                |
| G   | `UnrealizedPct`    | Current % gain/loss (ACTIVE positions only).   |
| H   | `ATHUnrealizedPct` | Highest unrealized % gain seen while tracked.  |
| I   | `Armed`            | `TRUE`/`FALSE`: whether trailing TP is armed.  |
| J   | `Status`           | `ACTIVE` / `CLOSED` / `CLOSED_EXTERNAL`.       |
| K   | `RealizedPct`      | Realized % gain at close (for CLOSED rows).    |
| L   | `LastUpdated`      | ISO-8601 UTC timestamp.                        |

* On first run, if the sheet doesn’t exist, it is created and this header row is inserted.
* If a header row exists but differs, a warning is printed (but the script still proceeds).

Each asset altname (e.g. `XBT`) is keyed to a single row.

---

## 🎯 Exit Rules

These constants are defined near the top of the script:

```python
STOP_LOSS_PCT = -3.0     # sell if unarmed and <= -3%
ARM_THRESHOLD_PCT = 5.0  # mark as Armed at >= +5%
TRAILING_DROP_PCT = 3.0  # sell when Armed and ATH - current >= 3%
```

### Stop-Loss

* If `Status == ACTIVE`, not armed, and
* `UnrealizedPct <= STOP_LOSS_PCT` (i.e. <= -3%),
* Then issue a **market sell** for the full balance, reason = `STOP_LOSS`.

### Arm

* When `UnrealizedPct >= ARM_THRESHOLD_PCT` (+5%),
* Set `Armed = TRUE`.

### Trailing Take Profit

* If `Armed == TRUE` and
* `(ATHUnrealizedPct - UnrealizedPct) >= TRAILING_DROP_PCT` (i.e. you’ve dropped 3% or more off the peak),
* Then issue a **market sell** for the full balance, reason = `TRAILING_TAKE_PROFIT`.

### External Closes

* After processing all Kraken holdings, the bot scans sheet rows.
* If an asset exists in the sheet **but not in balances**, and its status is `ACTIVE`, it is marked as **`CLOSED_EXTERNAL`** (position closed outside the bot), with zero `PositionSize` and `UnrealizedPct`.

---

## 🔐 Environment Variables

The script uses these environment variables:

| Variable                 | Required | Default            | Description                                                           |
| ------------------------ | -------- | ------------------ | --------------------------------------------------------------------- |
| `GOOGLE_CREDS_JSON`      | Yes      | —                  | Service account JSON for Google Sheets/Drive (full JSON as a string). |
| `GOOGLE_SHEET_NAME`      | No       | `Active-Investing` | Name of the spreadsheet.                                              |
| `GOOGLE_WORKSHEET_TITLE` | No       | `Kraken-Trader`    | Name of the worksheet/tab.                                            |
| `KRAKEN_API_KEY`         | Yes      | —                  | Kraken API key.                                                       |
| `KRAKEN_API_SECRET`      | Yes      | —                  | Kraken API secret.                                                    |
| `BASE_CURRENCY`          | No       | `USD`              | Quote currency (e.g. `USD`, `EUR`).                                   |
| `POLL_INTERVAL_SECONDS`  | No       | `60`               | Seconds between cycles in the main loop.                              |
| `DRY_RUN`                | No       | `False`            | If truthy (`"1"`, `"true"`, `"yes"`), no real sell orders are placed. |

`DRY_RUN` is interpreted case-insensitively with values like `1`, `true`, `yes`, `y`, `on`.

---

## 🔌 Kraken API Integration

The bot uses the official `kraken.spot` client:

```python
from kraken.spot import User, Market, Trade

self.user = User(key=self.kraken_key, secret=self.kraken_secret)
self.market = Market()
self.trade = Trade(key=self.kraken_key, secret=self.kraken_secret)
```

Key calls:

* `self.user.get_balances()` — returns spot balances per asset code.
* `self.market.get_assets()` — used to map internal codes to altname (e.g. `XXBT` → `XBT`).
* `self.market.get_ticker(pair=pair)` — fetches ticker data to get the last trade price.
* `self.trade.create_order(...)` — submits full-position **market sell** orders with `reduce_only=True`.

---

## 📦 Installation

You’ll need:

* Python 3.9+ (recommended)
* A Google Cloud service account with Sheets & Drive API enabled
* A Kraken account with API keys that have **read balances** and **trade** permissions

Example dependencies:

```bash
pip install gspread google-auth kraken-sdk
```

(Use the exact package name/version that provides `kraken.spot`; adjust accordingly.)

---

## ▶️ Running the Bot

After configuring environment variables and installing dependencies, run:

```bash
python kraken_trailing_sell_bot.py
```

`if __name__ == "__main__":` constructs the bot and calls `run_forever()`:

```python
if __name__ == "__main__":
    bot = KrakenTrailingSellBot()
    bot.run_forever()
```

You’ll see logs like:

```text
KrakenTrailingSellBot initialized.
Base currency: USD
Polling interval: 60 seconds
Dry run mode: False
Found 3 non-zero holdings on Kraken.
XBT signal: TRAILING_TAKE_PROFIT, unreal_pct=12.34%, ATH=16.01%, size=0.050000
[SELL] XBT pair=XBTUSD volume=0.05 reason=TRAILING_TAKE_PROFIT
Updated row for XBT (Status=CLOSED).
ETH no longer on Kraken. Marked as CLOSED_EXTERNAL.
```

Press `Ctrl+C` (locally) to stop the process.

---

## 🛡️ Safety & Suggestions

* **Always** test in `DRY_RUN=true` mode before enabling live sells.
* Consider using a separate Kraken account or small amounts for initial testing.
* Double-check `BASE_CURRENCY` and ensure it matches your market pairs (e.g. `XBTUSD`).
* Enhance logging (e.g., log to file, structured logs) for auditing.
* Consider adding:

  * Per-asset overrides for thresholds
  * E-mail / Discord / Slack alerts on sells
  * Max daily sells or capital-at-risk limits

---

## 📄 License

Add your preferred license here (MIT, Apache 2.0, etc.).
