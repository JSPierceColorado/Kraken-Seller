import os
import json
import time
import traceback
from datetime import datetime, timezone
from typing import Dict, Any, List

import gspread
from google.oauth2.service_account import Credentials

from kraken.spot import User, Market, Trade
from kraken.exceptions import KrakenUnknownAssetError, KrakenInvalidArgumentsError

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


def get_env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


def get_env_float(name: str, default: float) -> float:
    """
    Read a float value from an environment variable.
    If missing or invalid, return the provided default.
    """
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        print(f"WARNING: Invalid float for {name}={val!r}, using default {default}")
        return default


# Strategy parameters
STOP_LOSS_PCT = get_env_float("STOP_LOSS_PCT", -3.0)            # sell if unarmed and <= this %
ARM_THRESHOLD_PCT = get_env_float("ARM_THRESHOLD_PCT", 5.0)     # mark as Armed at >= +5%
TRAILING_DROP_PCT = get_env_float("TRAILING_DROP_PCT", 3.0)     # sell when Armed and ATH - current >= 3%

# Simple baked-in fee buffer (in percent) to roughly cover Kraken fees
# e.g. 0.5 ~ 0.5% buffer
FEE_BUFFER_PCT = 0.5


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

    existing = ws.row_values(1)
    if not existing:
        ws.append_row(HEADERS, value_input_option="USER_ENTERED")
    elif existing != HEADERS:
        print("WARNING: Existing header row differs from expected HEADERS.")

    return ws


def build_row(
    asset_alt: str,
    asset_code: str,
    pair: str,
    position_size: float,
    cost_basis,
    current_price: float,
    unreal_pct: float,
    ath_unreal_pct: float,
    armed: bool,
    status: str,
    realized_pct,
    last_updated: str,
) -> List[Any]:
    def fmt(x):
        # Increased precision: keep up to 10 decimal places for floats
        if isinstance(x, float):
            return round(x, 10)
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

        print("KrakenTrailingSellBot initialized.")
        print(f"Base currency: {self.base_currency}")
        print(f"Polling interval: {self.poll_interval} seconds")
        print(f"Dry run mode: {self.dry_run}")
        print(f"Configured STOP_LOSS_PCT: {STOP_LOSS_PCT}%")
        print(f"Configured ARM_THRESHOLD_PCT: {ARM_THRESHOLD_PCT}%")
        print(f"Configured TRAILING_DROP_PCT: {TRAILING_DROP_PCT}%")
        print(f"Fee buffer (FEE_BUFFER_PCT): {FEE_BUFFER_PCT}%")
        print(
            "CostBasis behavior: "
            "first time an asset appears, on blank CostBasis, or on reactivation, "
            "it is initialized to the current price and then kept until you change it."
        )

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
          - altnames containing '.' (e.g. ETH.F) which may not have spot pairs
        """
        balances = self.user.get_balances()
        holdings: Dict[str, Dict[str, Any]] = {}

        for asset_code, data in balances.items():
            balance = float(data.get("balance", "0") or "0")
            if balance <= 0:
                continue

            info = self.asset_info.get(asset_code, {})
            altname = info.get("altname", asset_code)

            # Skip base/fiat
            if altname.upper() in (self.base_currency, "USD", "EUR"):
                continue
            # Skip fee token
            if altname.upper() == "KFEE":
                continue
            # Skip derivative / staked style tokens like ETH.F for this spot bot
            if "." in altname:
                print(
                    f"Skipping holding {altname} ({asset_code}): "
                    f"contains '.' and may not have a spot {altname}{self.base_currency} pair."
                )
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
        inner = next(iter(ticker.values()))
        last = float(inner["c"][0])
        return last

    def _place_market_sell(
        self, altname: str, balance: float, reason: str
    ) -> bool:
        """
        Place a full-position market sell, returns True on "we consider it sold".
        Honors DRY_RUN.

        Note: `reduce_only` is NOT used here because it's only valid
        for leveraged orders, not spot.
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
            )
            print(f"Kraken order response: {resp}")
            return True
        except KrakenInvalidArgumentsError as e:
            # Specifically catch the "reduce_only" / invalid-arguments style issues
            print(
                f"KrakenInvalidArgumentsError while selling {altname}: {e!r}. "
                "Order was rejected by Kraken."
            )
            traceback.print_exc()
            return False
        except Exception as e:
            print(f"Error while selling {altname}: {e!r}")
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

        Use expected_headers to avoid gspread complaining about duplicate
        blank header cells.
        """
        records = self.ws.get_all_records(
            default_blank="",
            expected_headers=HEADERS,
        )
        positions: Dict[str, Dict[str, Any]] = {}

        for idx, rec in enumerate(records):
            asset = str(rec.get("Asset", "")).strip()
            if not asset:
                continue
            row_number = idx + 2  # row 1 = header
            positions[asset] = {"row": row_number, "data": rec}

        return positions

    def _write_row(self, row: int, values: List[Any]) -> None:
        range_name = f"A{row}:L{row}"
        self.ws.update(range_name, [values], value_input_option="USER_ENTERED")

    def _append_row(self, values: List[Any]) -> None:
        """
        Append a new row starting at column A, avoiding any weird internal
        sheet "table range" offsets that can place data in later columns.
        """
        # Number of used rows in column A (including header)
        last_row = len(self.ws.col_values(1))
        next_row = last_row + 1  # next empty row after the last used

        range_name = f"A{next_row}:L{next_row}"
        self.ws.update(range_name, [values], value_input_option="USER_ENTERED")

    # ---------- Core logic per cycle ----------

    def run_once(self):
        """
        One full cycle:
        - pull Kraken balances
        - pull/create/update sheet rows
        - enforce stop / arming / trailing TP

        CostBasis behavior:
        - First time an asset appears, CostBasis is set to the current price.
        - If a row exists but has blank CostBasis, it is set to the current price.
        - If an asset was previously CLOSED/CLOSED_EXTERNAL and is seen again
          in holdings, its row is reactivated (Status=ACTIVE) and CostBasis is
          reset to the current price.
        - After that, CostBasis is kept as-is unless you manually change it.
        - When an asset is CLOSED or CLOSED_EXTERNAL, CostBasis is cleared in
          the sheet so the next reactivation is treated as fresh.
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        holdings = self._get_holdings()
        print(f"Found {len(holdings)} non-zero holdings on Kraken.")

        positions = self._read_positions()
        active_altnames = set(holdings.keys())

        for altname, hinfo in holdings.items():
            asset_code = hinfo["asset_code"]
            balance = hinfo["balance"]

            try:
                price = self._get_price(altname)
            except KrakenUnknownAssetError as e:
                # Known situation: no such asset pair (e.g. newly listed or special token)
                print(
                    f"Skipping {altname}: unknown asset pair {altname}{self.base_currency} "
                    f"({e!r})"
                )
                continue
            except Exception as e:
                print(f"Failed to fetch price for {altname}: {e!r}")
                traceback.print_exc()
                continue

            pair = f"{altname}{self.base_currency}"

            # --- Load existing sheet row or create defaults ---
            if altname in positions:
                row = positions[altname]["row"]
                rec = positions[altname]["data"]
                status = (rec.get("Status") or "ACTIVE").upper()
                raw_cost_basis = rec.get("CostBasis")
                ath_unreal_raw = rec.get("ATHUnrealizedPct")
                armed_raw = rec.get("Armed")
                realized_pct = rec.get("RealizedPct")

                # Reactivation case: previously non-ACTIVE, now held again.
                if status != "ACTIVE":
                    status = "ACTIVE"
                    cost_basis_value = price
                    cost_basis_cell = price
                    ath_unreal = 0.0
                    armed = False
                    realized_pct = ""
                else:
                    # ACTIVE row
                    if str(raw_cost_basis).strip():
                        cost_basis_value = float(raw_cost_basis)
                        cost_basis_cell = cost_basis_value
                    else:
                        # Blank CostBasis on an ACTIVE row -> initialize to current price
                        cost_basis_value = price
                        cost_basis_cell = price

                    ath_unreal = (
                        float(ath_unreal_raw)
                        if str(ath_unreal_raw).strip()
                        else 0.0
                    )
                    armed = str(armed_raw).strip().lower() in ("true", "1", "yes", "y")
            else:
                # New asset: first time it appears in the sheet,
                # initialize CostBasis to the current price.
                row = None
                status = "ACTIVE"
                cost_basis_value = price
                cost_basis_cell = price
                ath_unreal = 0.0
                armed = False
                realized_pct = ""

            # Compute unrealized percentage P&L (gross)
            if cost_basis_value == 0:
                unreal_pct = 0.0
            else:
                unreal_pct = (price - cost_basis_value) / cost_basis_value * 100.0

            # Update all-time-high unrealized (gross)
            ath_unreal = max(ath_unreal, unreal_pct)

            sell_reason = None

            if status == "ACTIVE":
                if armed:
                    # Trailing take profit:
                    # require an extra FEE_BUFFER_PCT drop so there is room for fees
                    if (ath_unreal - unreal_pct) >= (TRAILING_DROP_PCT + FEE_BUFFER_PCT):
                        sell_reason = "TRAILING_TAKE_PROFIT"
                else:
                    # Adjust thresholds by the buffer.

                    # STOP loss: trigger a bit earlier (less negative) so after fees
                    # you end up roughly near your configured STOP_LOSS_PCT.
                    stop_loss_trigger = STOP_LOSS_PCT + FEE_BUFFER_PCT

                    # ARM threshold: require a bit more profit so that after fees
                    # you still roughly have ARM_THRESHOLD_PCT.
                    arm_trigger = ARM_THRESHOLD_PCT + FEE_BUFFER_PCT

                    if unreal_pct <= stop_loss_trigger:
                        sell_reason = "STOP_LOSS"
                    elif unreal_pct >= arm_trigger:
                        armed = True

            if sell_reason and balance > 0:
                print(
                    f"{altname} signal: {sell_reason}, unreal_pct={unreal_pct:.2f}%, "
                    f"ATH={ath_unreal:.2f}%, size={balance}"
                )
                sold_ok = self._place_market_sell(altname, balance, sell_reason)
                if sold_ok:
                    # Log a fee-buffered realized P&L approximation
                    realized_pct = unreal_pct - FEE_BUFFER_PCT
                    status = "CLOSED"
                    balance = 0.0
                    unreal_pct = 0.0
                    # Clear CostBasis on close so reactivation is fresh
                    cost_basis_value = 0.0
                    cost_basis_cell = ""
                else:
                    print(f"Sell failed for {altname}; leaving as ACTIVE this cycle.")

            row_values = build_row(
                asset_alt=altname,
                asset_code=asset_code,
                pair=pair,
                position_size=balance,
                cost_basis=cost_basis_cell,
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

        # Mark assets that disappeared from Kraken as CLOSED_EXTERNAL
        for altname, pdata in positions.items():
            if altname in active_altnames:
                continue

            rec = pdata["data"]
            status = (rec.get("Status") or "ACTIVE").upper()
            if status != "ACTIVE":
                continue

            row = pdata["row"]
            asset_code = rec.get("KrakenAssetCode", "")
            pair = rec.get("Pair", f"{altname}{self.base_currency}")

            # When an asset disappears from holdings without this bot selling it,
            # mark it as CLOSED_EXTERNAL and clear CostBasis so a future position
            # is treated as fresh.
            cost_basis_cell = ""  # clear
            current_price = float(rec.get("CurrentPrice") or 0.0)
            ath_unreal = float(rec.get("ATHUnrealizedPct") or 0.0)
            armed_raw = rec.get("Armed")
            armed = str(armed_raw).strip().lower() in ("true", "1", "yes", "y")

            row_values = build_row(
                asset_alt=altname,
                asset_code=asset_code,
                pair=pair,
                position_size=0.0,
                cost_basis=cost_basis_cell,
                current_price=current_price,
                unreal_pct=0.0,
                ath_unreal_pct=ath_unreal,
                armed=armed,
                status="CLOSED_EXTERNAL",
                realized_pct="",
                last_updated=now_iso,
            )
            self._write_row(row, row_values)
            print(
                f"{altname} no longer on Kraken. "
                f"Marked as CLOSED_EXTERNAL and cleared CostBasis."
            )

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


if __name__ == "__main__":
    bot = KrakenTrailingSellBot()
    bot.run_forever()
