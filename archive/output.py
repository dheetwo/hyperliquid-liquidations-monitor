"""
Output Formatters
==================

Different output formats for scan results:
- Console (pretty print)
- JSON file
- CSV file  
- Telegram notification
"""

import json
import csv
import os
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import List, Optional
from pathlib import Path
from zoneinfo import ZoneInfo

from models import ScanResult, FlaggedPosition

logger = logging.getLogger(__name__)

# EST timezone
EST = ZoneInfo("America/New_York")


def format_timestamp_est(dt: datetime) -> str:
    """Format datetime as pretty EST string for filenames (e.g., 'Jan-14-2026_8-30PM')"""
    # Convert to EST
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_est = dt.astimezone(EST)
    # Format: Mon-DD-YYYY_H-MMAM/PM
    return dt_est.strftime("%b-%d-%Y_%-I-%M%p")


def archive_old_files(output_dir: str, keep_recent: int = 2) -> None:
    """
    Keep only the most recent files in output_dir, archive the rest.

    Args:
        output_dir: Directory containing output files
        keep_recent: Number of recent files to keep per type (json/csv)
    """
    import shutil

    output_path = Path(output_dir)
    archive_path = output_path / "archive"

    for ext in [".json", ".csv"]:
        # Get all files of this type, sorted by modification time (newest first)
        files = sorted(
            output_path.glob(f"*{ext}"),
            key=lambda f: f.stat().st_mtime,
            reverse=True
        )

        # Archive files beyond the keep_recent limit
        files_to_archive = files[keep_recent:]

        if files_to_archive:
            archive_path.mkdir(parents=True, exist_ok=True)

            for f in files_to_archive:
                dest = archive_path / f.name
                shutil.move(str(f), str(dest))
                logger.debug(f"Archived: {f.name}")


class OutputFormatter(ABC):
    """Base class for output formatters"""
    
    @abstractmethod
    def output(self, result: ScanResult) -> None:
        """Output the scan results"""
        pass


class ConsoleFormatter(OutputFormatter):
    """Pretty print results to console"""
    
    def output(self, result: ScanResult) -> None:
        print("\n" + "=" * 60)
        print("HYPERDASH LARGE POSITION SCAN RESULTS")
        print("=" * 60)
        print(f"Timestamp: {result.timestamp.isoformat()}")
        print(f"Assets Scanned: {result.assets_scanned}")
        print(f"Positions Checked: {result.total_positions_checked}")
        print(f"Positions Flagged: {result.flagged_count}")
        
        if result.errors:
            print(f"\n⚠️  Errors: {len(result.errors)}")
            for error in result.errors[:5]:  # Show first 5 errors
                print(f"   - {error}")
        
        if result.flagged_positions:
            print("\n" + "-" * 60)
            print("FLAGGED POSITIONS")
            print("-" * 60)
            
            # Group by asset
            by_asset = {}
            for fp in result.flagged_positions:
                asset = fp.position.asset
                if asset not in by_asset:
                    by_asset[asset] = []
                by_asset[asset].append(fp)
            
            for asset, positions in sorted(by_asset.items()):
                print(f"\n📊 {asset} ({len(positions)} position{'s' if len(positions) > 1 else ''})")
                for fp in positions:
                    pos = fp.position
                    side_emoji = "🟢" if pos.side.value == "long" else "🔴"
                    oi_str = f" ({pos.oi_percentage:.2%} OI)" if pos.oi_percentage else ""
                    reasons = ", ".join([r.value for r in fp.alert_reasons])

                    print(f"   {side_emoji} ${pos.notional_usd:,.0f}{oi_str}")
                    print(f"      Trader: {pos.trader_address[:16]}...")

                    # Price info
                    price_parts = []
                    if pos.entry_price:
                        price_parts.append(f"Entry: ${pos.entry_price:,.2f}")
                    if pos.current_price:
                        price_parts.append(f"Current: ${pos.current_price:,.2f}")
                    if pos.liquidation_price:
                        price_parts.append(f"Liq: ${pos.liquidation_price:,.2f}")
                    if price_parts:
                        print(f"      {' | '.join(price_parts)}")

                    print(f"      Alert: {reasons}")
        else:
            print("\n✅ No positions flagged")
        
        print("\n" + "=" * 60)


class JsonFormatter(OutputFormatter):
    """Output results to JSON file"""
    
    def __init__(self, output_dir: str = "./output", filename: str = "large_positions"):
        self.output_dir = output_dir
        self.filename = filename
    
    def output(self, result: ScanResult) -> None:
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)

        timestamp_str = format_timestamp_est(result.timestamp)
        filepath = os.path.join(self.output_dir, f"{self.filename}_{timestamp_str}.json")

        # Filter out positions with liq proximity > 10%
        def get_liq_proximity(pos):
            if pos.current_price and pos.liquidation_price and pos.current_price > 0:
                return abs(pos.current_price - pos.liquidation_price) / pos.current_price
            return None

        filtered_positions = []
        for fp in result.flagged_positions:
            prox = get_liq_proximity(fp.position)
            # Include if no liq data OR proximity <= 10%
            if prox is None or prox <= 0.10:
                filtered_positions.append(fp)

        # Create filtered result dict
        result_dict = result.to_dict()
        result_dict["flagged_positions"] = [
            {
                "asset": fp.position.asset,
                "trader": fp.position.trader_address,
                "side": fp.position.side.value,
                "notional_usd": fp.position.notional_usd,
                "entry_price": fp.position.entry_price,
                "current_price": fp.position.current_price,
                "liquidation_price": fp.position.liquidation_price,
                "oi_percentage": fp.position.oi_percentage,
                "alert_reasons": [r.value for r in fp.alert_reasons]
            }
            for fp in filtered_positions
        ]
        result_dict["flagged_count"] = len(filtered_positions)

        with open(filepath, 'w') as f:
            json.dump(result_dict, f, indent=2)

        logger.info(f"JSON output saved to: {filepath}")
        print(f"📄 Results saved to: {filepath}")

        # Archive old files, keep only 2 most recent
        archive_old_files(self.output_dir, keep_recent=2)


class CsvFormatter(OutputFormatter):
    """Output results to CSV file"""
    
    def __init__(self, output_dir: str = "./output", filename: str = "large_positions"):
        self.output_dir = output_dir
        self.filename = filename
    
    def output(self, result: ScanResult) -> None:
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)

        timestamp_str = format_timestamp_est(result.timestamp)
        filepath = os.path.join(self.output_dir, f"{self.filename}_{timestamp_str}.csv")

        # Filter out positions with liq proximity > 10%
        def get_liq_proximity(pos):
            if pos.current_price and pos.liquidation_price and pos.current_price > 0:
                return abs(pos.current_price - pos.liquidation_price) / pos.current_price
            return None

        filtered_positions = []
        for fp in result.flagged_positions:
            prox = get_liq_proximity(fp.position)
            # Include if no liq data OR proximity <= 10%
            if prox is None or prox <= 0.10:
                filtered_positions.append(fp)

        # Sort positions by notional descending
        sorted_positions = sorted(
            filtered_positions,
            key=lambda fp: fp.position.notional_usd,
            reverse=True
        )

        with open(filepath, 'w', newline='') as f:
            writer = csv.writer(f)

            # Header
            writer.writerow([
                "Asset",
                "Side",
                "Notional (USD)",
                "Entry Price",
                "Current Price",
                "Liquidation Price",
                "Liq Proximity %",
                "OI %",
                "Alert Reasons",
                "Trader"
            ])

            # Data rows
            for fp in sorted_positions:
                pos = fp.position

                # Calculate liq proximity
                liq_prox = ""
                if pos.current_price and pos.liquidation_price and pos.current_price > 0:
                    prox = abs(pos.current_price - pos.liquidation_price) / pos.current_price * 100
                    liq_prox = f"{prox:.1f}%"

                # Format values
                entry = f"${pos.entry_price:,.2f}" if pos.entry_price else ""
                current = f"${pos.current_price:,.2f}" if pos.current_price else ""
                liq = f"${pos.liquidation_price:,.2f}" if pos.liquidation_price else ""
                oi_pct = f"{pos.oi_percentage * 100:.2f}%" if pos.oi_percentage else ""

                writer.writerow([
                    pos.asset,
                    pos.side.value.upper(),
                    f"${pos.notional_usd:,.0f}",
                    entry,
                    current,
                    liq,
                    liq_prox,
                    oi_pct,
                    ", ".join([r.value for r in fp.alert_reasons]),
                    pos.trader_address
                ])
        
        logger.info(f"CSV output saved to: {filepath}")
        print(f"📄 Results saved to: {filepath}")

        # Archive old files, keep only 2 most recent
        archive_old_files(self.output_dir, keep_recent=2)


class TelegramFormatter(OutputFormatter):
    """Send results via Telegram bot"""
    
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._validate()
    
    def _validate(self):
        if not self.bot_token or not self.chat_id:
            raise ValueError("Telegram bot_token and chat_id are required")
    
    def _send_message(self, text: str) -> bool:
        """Send a message via Telegram Bot API"""
        import requests
        
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        
        try:
            response = requests.post(url, json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML"
            }, timeout=10)
            
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False
    
    def output(self, result: ScanResult) -> None:
        if not result.flagged_positions:
            logger.info("No positions to report to Telegram")
            return
        
        # Build message
        lines = [
            "🔍 <b>Hyperdash Large Position Alert</b>",
            f"📅 {result.timestamp.strftime('%Y-%m-%d %H:%M UTC')}",
            f"📊 Found <b>{result.flagged_count}</b> large positions",
            ""
        ]
        
        # Group by asset
        by_asset = {}
        for fp in result.flagged_positions:
            asset = fp.position.asset
            if asset not in by_asset:
                by_asset[asset] = []
            by_asset[asset].append(fp)
        
        for asset, positions in sorted(by_asset.items(), key=lambda x: -sum(p.position.notional_usd for p in x[1])):
            lines.append(f"<b>{asset}</b>:")
            for fp in positions[:5]:  # Limit to 5 per asset
                pos = fp.position
                side_emoji = "🟢" if pos.side.value == "long" else "🔴"
                oi_str = f" ({pos.oi_percentage:.1%} OI)" if pos.oi_percentage else ""
                
                lines.append(f"  {side_emoji} ${pos.notional_usd:,.0f}{oi_str}")
                lines.append(f"     <code>{pos.trader_address[:20]}...</code>")
            
            if len(positions) > 5:
                lines.append(f"  ... and {len(positions) - 5} more")
            lines.append("")
        
        message = "\n".join(lines)
        
        # Telegram has a 4096 char limit
        if len(message) > 4000:
            message = message[:4000] + "\n... (truncated)"
        
        if self._send_message(message):
            logger.info("Telegram notification sent")
            print("📱 Telegram notification sent")
        else:
            logger.error("Failed to send Telegram notification")


class MultiFormatter(OutputFormatter):
    """Combine multiple formatters"""
    
    def __init__(self, formatters: List[OutputFormatter]):
        self.formatters = formatters
    
    def output(self, result: ScanResult) -> None:
        for formatter in self.formatters:
            try:
                formatter.output(result)
            except Exception as e:
                logger.error(f"Formatter {type(formatter).__name__} failed: {e}")


def create_formatters(
    console: bool = True,
    json_output: bool = False,
    csv_output: bool = False,
    telegram_token: str = None,
    telegram_chat: str = None,
    output_dir: str = "./output",
    filename: str = "large_positions"
) -> OutputFormatter:
    """
    Factory function to create formatter(s) based on configuration.
    
    Returns a single formatter or MultiFormatter if multiple are requested.
    """
    formatters = []
    
    if console:
        formatters.append(ConsoleFormatter())
    
    if json_output:
        formatters.append(JsonFormatter(output_dir, filename))
    
    if csv_output:
        formatters.append(CsvFormatter(output_dir, filename))
    
    if telegram_token and telegram_chat:
        try:
            formatters.append(TelegramFormatter(telegram_token, telegram_chat))
        except ValueError as e:
            logger.warning(f"Could not create Telegram formatter: {e}")
    
    if not formatters:
        formatters.append(ConsoleFormatter())
    
    if len(formatters) == 1:
        return formatters[0]
    
    return MultiFormatter(formatters)
