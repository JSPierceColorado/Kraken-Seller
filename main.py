import os
import json
import time
import traceback
from datetime import datetime, timezone
from typing import Dict, Any, List

import gspread
from google.oauth2.service_account import Credentials

from kraken.spot import User, Market, Trade
from kraken.exceptions import KrakenException

# ==========================
# CONFIG / CONSTANTS
# ==========================

HEADERS = [
    "Asset",            # Human-friendly asset (altname, e.g. XBT, ETH)
    "KrakenAssetCode",  # Kraken internal code (e.g. XXBT)
    "Pair",             # Trading pair used (e.g. XBTUSD)
    "PositionSize",     # Current size in asset units
    "CostBasis",        # Entry price used for % calculations
    "CurrentPrice",     # Last price from ticker
    "UnrealizedPct",    # Current % gain/loss (if ACTIVE)
    "ATHUnrealizedPct", # All-time high % gain while tracked
    "Armed",            # TRUE/FALSE
    "Status",           # ACTIVE / CLOSED / CLOSED_EXTERNAL
    "RealizedPct",      # Pct gain at close (for CLOSED rows)
    "LastUpdated",      # ISO timestamp
]

# Hard-coded rules from your spec
STOP_LOSS_PCT = -3.0     # sell if unarmed and <= -3%
ARM_THRESHOLD_PCT = 5.0  # mark as Armed at >= +5%
TRAILING_DROP_PCT = 3.0  # sell when Armed and ATH - current >= 3%


# ==========================
# HELPER FUNCTIONS
# ==========================

def get_env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


def load_gspread_worksheet() -> gspread.Worksheet:
    """
    Connect to Google Sheets using GOOGLE_CREDS_JSON and
    return the 'Kraken-Trader' worksheet inside 'Active-Investing'.
    Creates the worksheet (with headers) if missing.
    """
    creds_json = os.environ["GOOGLE_CREDS_JSON"]
    sheet_name = os.getenv("GOOGLE_SHEET_NAME", "Active-Investing")
    worksheet_title = os.getenv("GOOGLE_WORKSHEET_TITLE", "Kraken-Trader")

    service_account_info = json.loads(creds_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = Credentials.from_service_account_info(
        service_account_info, scopes=scopes
    )
    client = gspread.authorize(credentials)

    spreadsheet = client.open(sheet_name)

    try:
        ws = spreadsheet.worksheet(worksheet_title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(
            title=worksheet_title,
            rows="200",
            cols=str(len(HEADERS)),
        )
        ws.append_row(HEADERS, value_input_option="USER_ENTERED")

    # If sheet exists but no headers, ensure they’re present
    existing = ws.row_values(1)
    if not existing:
        ws.append_row(HEADERS, value_input_option="USER_ENTERED")
    elif existing != HEADERS:
        # Minimal safety: don't auto-rewrite a custom header row,
        # but you *can* manually align to HEADERS for full functionality.
        print("WARNING: Existing header row differs from expected HEADERS.")

    return ws


def build_row(
    asset_alt: str,
    asset_code: str,
    pair: str,
    position_size: float,
    cost_basis: float,
    current_price: float,
    unreal_pct: float,
    ath_unreal_pct: float,
    armed: bool,
    status: str,
    realized_pct,
    last_updated: str,
) -> List[Any]:
    def fmt(x):
        if isinstance(x, float):
            return round(x, 6)
        return x

    return [
        asset_alt,
        asset_code,
        pair,
        fmt(position_size),
        fmt(cost_basis),
        fmt(current_price),
        fmt(unreal_pct),
        fmt(ath_unreal_pct),
        "TRUE" if armed else "FALSE",
        status,
        "" if realized_pct in (None, "") else fmt(float(realized_pct)),
        last_updated,
    ]


# ==========================
# KRAKEN TRAILING SELL BOT
# ==========================

class KrakenTrailingSellBot:
    def __init__(self):
        # ENV config
        self.base_currency = os.getenv("BASE_CURRENCY", "USD").upper()
        self.poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
        self.dry_run = get_env_bool("DRY_RUN", default=False)

        self.kraken_key = os.environ["KRAKEN_API_KEY"]
        self.kraken_secret = os.environ["KRAKEN_API_SECRET"]

        # API clients
        self.user = User(key=self.kraken_key, secret=self.kraken_secret)
        self.market = Market()  # public is fine
        self.trade = Trade(key=self.kraken_key, secret=self.kraken_secret)

        # Google Sheets
        self.ws = load_gspread_worksheet()

        # Cache Kraken asset info so we can map codes -> altnames
        self.asset_info = self.market.get_assets()
        # asset_info example: { 'XXBT': {'altname': 'XBT', ...}, ... }

        print("KrakenTrailingSellBot initialized.")
        print(f"Base currency: {self.base_currency}")
        print(f"Polling interval: {self.poll_interval} seconds")
        print(f"Dry run mode: {self.dry_run}")

    # ---------- Kraken helpers ----------

    def _get_holdings(self) -> Dict[str, Dict[str, Any]]:
        """
        Return holdings keyed by asset altname:
        {
          'XBT': {'asset_code': 'XXBT', 'balance': 2.1},
          'ETH': {...}
        }

        Skips:
          - zero balances
          - base currency (e.g. USD)
          - obvious fee tokens (KFEE)
        """
        balances = self.user.get_balances()
        holdings: Dict[str, Dict[str, Any]] = {}

        for asset_code, data in balances.items():
            balance = float(data.get("balance", "0") or "0")
            if balance <= 0:
                continue

            info = self.asset_info.get(asset_code, {})
            altname = info.get("altname", asset_code)

            # Skip base currency & fee token
            if altname.upper() in (self.base_currency, "USD", "EUR"):
                continue
            if altname.upper() == "KFEE":
                continue

            holdings[altname] = {
                "asset_code": asset_code,
                "balance": balance,
            }

        return holdings

    def _get_price(self, altname: str) -> float:
        """
        Get last trade price for altname/base_currency pair,
        e.g. 'XBT' -> 'XBTUSD'.
        """
        pair = f"{altname}{self.base_currency}"
        ticker = self.market.get_ticker(pair=pair)
        # Result dict key may be e.g. 'XXBTZUSD' or 'XBTUSD'; just grab first
        inner = next(iter(ticker.values()))
        last = float(inner["c"][0])
        return last

    def _place_market_sell(
        self, altname: str, balance: float, reason: str
    ) -> bool:
        """
        Place a full-position market sell, returns True on "we consider it sold".
        Honors DRY_RUN.
        """
        pair = f"{altname}{self.base_currency}"
        print(
            f"[SELL] {altname} pair={pair} volume={balance} reason={reason} "
            f"{'(DRY_RUN)' if self.dry_run else ''}"
        )

        if self.dry_run:
            return True

        try:
            resp = self.trade.create_order(
                ordertype="market",
                side="sell",
                pair=pair,
                volume=balance,
                reduce_only=True,  # don't go short by accident
            )
            print(f"Kraken order response: {resp}")
            return True
        except KrakenException as e:
            print(f"KrakenException while selling {altname}: {e}")
            traceback.print_exc()
        except Exception as e:
            print(f"Unexpected error while selling {altname}: {e!r}")
            traceback.print_exc()

        return False

    # ---------- Sheet helpers ----------

    def _read_positions(self) -> Dict[str, Dict[str, Any]]:
        """
        Read all rows from sheet into:
        {
          'XBT': {
              'row': 2,
              'data': {<header->value mapping>}
          },
          ...
        }
        """
        records = self.ws.get_all_records(default_blank="")
        positions: Dict[str, Dict[str, Any]] = {}

        for idx, rec in enumerate(records):
            asset = str(rec.get("Asset", "")).strip()
            if not asset:
                continue
            row_number = idx + 2  # because row 1 = header
            positions[asset] = {"row": row_number, "data": rec}

        return positions

    def _write_row(self, row: int, values: List[Any]) -> None:
        range_name = f"A{row}:L{row}"
        self.ws.update(range_name, [values], value_input_option="USER_ENTERED")

    def _append_row(self, values: List[Any]) -> None:
        self.ws.append_row(values, value_input_option="USER_ENTERED")

    # ---------- Core logic per cycle ----------

    def run_once(self):
        """
        One full cycle:
        - pull Kraken balances
        - pull/create/update sheet rows
        - enforce stop / arming / trailing TP
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        # 1) Live Kraken data
        holdings = self._get_holdings()
        print(f"Found {len(holdings)} non-zero holdings on Kraken.")

        # 2) Existing sheet state
        positions = self._read_positions()

        # Keep track of which assets are currently active on Kraken
        active_altnames = set(holdings.keys())

        # 3) First: update/insert holdings and possibly SELL
        for altname, hinfo in holdings.items():
            asset_code = hinfo["asset_code"]
            balance = hinfo["balance"]

            # Get live price
            try:
                price = self._get_price(altname)
            except Exception as e:
                print(f"Failed to fetch price for {altname}: {e!r}")
                traceback.print_exc()
                continue

            pair = f"{altname}{self.base_currency}"

            if altname in positions:
                row = positions[altname]["row"]
                rec = positions[altname]["data"]
                status = (rec.get("Status") or "ACTIVE").upper()
                cost_basis = rec.get("CostBasis")
                ath_unreal = rec.get("ATHUnrealizedPct")
                armed_raw = rec.get("Armed")

                cost_basis = float(cost_basis) if str(cost_basis).strip() else price
                ath_unreal = float(ath_unreal) if str(ath_unreal).strip() else 0.0
                armed = str(armed_raw).strip().lower() in ("true", "1", "yes", "y")
                realized_pct = rec.get("RealizedPct")
            else:
                # New asset: start tracking from here
                row = None
                status = "ACTIVE"
                cost_basis = price  # baseline = current price
                ath_unreal = 0.0
                armed = False
                realized_pct = ""

            # Re-open if previously closed but asset reappears
            if status != "ACTIVE":
                status = "ACTIVE"
                # New “campaign” – you could also keep previous basis if you prefer
                cost_basis = price
                ath_unreal = 0.0
                armed = False
                realized_pct = ""

            # Compute current % gain/loss
            if cost_basis == 0:
                unreal_pct = 0.0
            else:
                unreal_pct = (price - cost_basis) / cost_basis * 100.0

            # Update all-time high % gain
            ath_unreal = max(ath_unreal, unreal_pct)

            sell_reason = None

            if status == "ACTIVE":
                if armed:
                    # Trailing take profit: sell if drawdown from ATH >= 3%
                    if (ath_unreal - unreal_pct) >= TRAILING_DROP_PCT:
                        sell_reason = "TRAILING_TAKE_PROFIT"
                else:
                    # Unarmed: hard -3% stop
                    if unreal_pct <= STOP_LOSS_PCT:
                        sell_reason = "STOP_LOSS"
                    elif unreal_pct >= ARM_THRESHOLD_PCT:
                        armed = True  # arm at +5%, don't sell yet

            # Execute sell if needed
            if sell_reason and balance > 0:
                print(
                    f"{altname} signal: {sell_reason}, unreal_pct={unreal_pct:.2f}%, "
                    f"ATH={ath_unreal:.2f}%, size={balance}"
                )
                sold_ok = self._place_market_sell(altname, balance, sell_reason)
                if sold_ok:
                    # Record close with realized %
                    realized_pct = unreal_pct
                    status = "CLOSED"
                    balance = 0.0
                    unreal_pct = 0.0  # no longer unrealized
                else:
                    print(f"Sell failed for {altname}; leaving as ACTIVE this cycle.")

            # Prepare row values
            row_values = build_row(
                asset_alt=altname,
                asset_code=asset_code,
                pair=pair,
                position_size=balance,
                cost_basis=cost_basis,
                current_price=price,
                unreal_pct=unreal_pct if status == "ACTIVE" else 0.0,
                ath_unreal_pct=ath_unreal,
                armed=armed,
                status=status,
                realized_pct=realized_pct,
                last_updated=now_iso,
            )

            if row is None:
                self._append_row(row_values)
                print(f"Created row for {altname}.")
            else:
                self._write_row(row, row_values)
                print(f"Updated row for {altname} (Status={status}).")

        # 4) Mark assets that disappeared from Kraken as CLOSED_EXTERNAL
        for altname, pdata in positions.items():
            if altname in active_altnames:
                continue

            rec = pdata["data"]
            status = (rec.get("Status") or "ACTIVE").upper()
            if status != "ACTIVE":
                continue  # already closed

            row = pdata["row"]
            asset_code = rec.get("KrakenAssetCode", "")
            pair = rec.get("Pair", f"{altname}{self.base_currency}")
            cost_basis = float(rec.get("CostBasis") or 0.0)
            current_price = float(rec.get("CurrentPrice") or 0.0)
            ath_unreal = float(rec.get("ATHUnrealizedPct") or 0.0)
            armed_raw = rec.get("Armed")
            armed = str(armed_raw).strip().lower() in ("true", "1", "yes", "y")

            # No Kraken position anymore – treat as externally closed
            row_values = build_row(
                asset_alt=altname,
                asset_code=asset_code,
                pair=pair,
                position_size=0.0,
                cost_basis=cost_basis,
                current_price=current_price,
                unreal_pct=0.0,
                ath_unreal_pct=ath_unreal,
                armed=armed,
                status="CLOSED_EXTERNAL",
                realized_pct="",  # unknown
                last_updated=now_iso,
            )
            self._write_row(row, row_values)
            print(f"{altname} no longer on Kraken. Marked as CLOSED_EXTERNAL.")

    def run_forever(self):
        print("Starting main loop. Ctrl+C to exit (locally).")
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                print("Received KeyboardInterrupt, shutting down.")
                raise
            except Exception as e:
                print(f"Top-level error in cycle: {e!r}")
                traceback.print_exc()

            time.sleep(self.poll_interval)


# ==========================
# ENTRYPOINT
# ==========================

if __name__ == "__main__":
    bot = KrakenTrailingSellBot()
    bot.run_forever()
