"""
dashboard.py
============
Rich terminal dashboard for the MEV arbitrage bot.

Displays live stats refreshed every 2 seconds:
  - Bot status overview panel
  - Opportunities scanned / found
  - Execution stats: attempts, successes, profit
  - Recent transactions table
  - Current gas prices (base, priority, estimated cost)
  - Token pair price spreads across DEXes
  - Circuit breaker status
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.columns import Columns
from rich import box

if TYPE_CHECKING:
    from mempool_scanner    import MempoolScanner
    from opportunity_detector import OpportunityDetector
    from executor           import Executor
    from gas_optimizer      import GasOptimizer

log = logging.getLogger("mev.dashboard")

# ─────────────────────────────────────────────────────────────────────────────
# Color constants
# ─────────────────────────────────────────────────────────────────────────────
GREEN  = "bold green"
RED    = "bold red"
YELLOW = "bold yellow"
CYAN   = "bold cyan"
WHITE  = "bold white"
DIM    = "dim white"


def _fmt_usd(val: float) -> str:
    if val >= 1000:
        return f"${val:,.2f}"
    return f"${val:.4f}"


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _age(ts: float) -> str:
    secs = int(time.time() - ts)
    if secs < 60:
        return f"{secs}s ago"
    elif secs < 3600:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h ago"


class Dashboard:
    """
    Live Rich terminal dashboard.
    Reads state from scanner, detector, executor, and gas_optimizer.
    """

    def __init__(
        self,
        scanner,
        detector,
        executor,
        gas_optimizer,
        refresh_interval: float = 2.0,
    ) -> None:
        self.scanner        = scanner
        self.detector       = detector
        self.executor       = executor
        self.gas_optimizer  = gas_optimizer
        self.refresh_interval = refresh_interval
        self._running = False
        self._start_time = time.time()

    async def start(self) -> None:
        """Start the live dashboard loop."""
        self._running = True
        self._start_time = time.time()
        console = Console()

        log.info("[dashboard] Starting rich terminal dashboard...")

        with Live(
            self._build_layout(),
            console=console,
            refresh_per_second=1.0 / self.refresh_interval,
            screen=True,
        ) as live:
            while self._running:
                try:
                    live.update(self._build_layout())
                    await asyncio.sleep(self.refresh_interval)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log.error(f"[dashboard] Render error: {e}")
                    await asyncio.sleep(self.refresh_interval)

        log.info("[dashboard] Stopped.")

    def _build_layout(self) -> Layout:
        """Compose the full dashboard layout."""
        layout = Layout()

        layout.split_column(
            Layout(name="header",  size=3),
            Layout(name="top",     size=10),
            Layout(name="middle",  size=14),
            Layout(name="bottom"),
        )

        layout["top"].split_row(
            Layout(name="status",   ratio=2),
            Layout(name="scanning", ratio=1),
            Layout(name="gas",      ratio=1),
        )

        layout["middle"].split_row(
            Layout(name="executions", ratio=3),
            Layout(name="spreads",    ratio=2),
        )

        layout["header"].update(self._render_header())
        layout["status"].update(self._render_status())
        layout["scanning"].update(self._render_scanning())
        layout["gas"].update(self._render_gas())
        layout["executions"].update(self._render_executions())
        layout["spreads"].update(self._render_spreads())
        layout["bottom"].update(self._render_recent_txs())

        return layout

    # ─── Header ───────────────────────────────────────────────────────────────

    def _render_header(self) -> Panel:
        uptime_secs = int(time.time() - self._start_time)
        h = uptime_secs // 3600
        m = (uptime_secs % 3600) // 60
        s = uptime_secs % 60
        uptime_str = f"{h:02d}:{m:02d}:{s:02d}"

        circuit_status = (
            Text(" CIRCUIT OPEN ", style="bold red on red") if self.executor.circuit_broken
            else Text(" RUNNING ", style="bold black on green")
        )

        title = Text()
        title.append(" MEV ARB BOT ", style="bold white on dark_blue")
        title.append("  ")
        title.append(circuit_status)
        title.append(f"  Uptime: {uptime_str}", style=DIM)
        title.append(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", style=DIM)

        return Panel(title, style="blue")

    # ─── Status panel ─────────────────────────────────────────────────────────

    def _render_status(self) -> Panel:
        scanner_status  = Text("CONNECTED",    style=GREEN)  if self.scanner.is_connected  else Text("DISCONNECTED", style=RED)
        executor_status = Text("HALTED",       style=RED)    if self.executor.circuit_broken else Text("ACTIVE",      style=GREEN)
        dry_run         = not bool(getattr(self.executor, '_account', None) and
                                   getattr(self.executor, 'contract_address', '').replace('0', '').replace('x', ''))

        table = Table(box=None, show_header=False, padding=(0, 1))
        table.add_column("Key",   style=DIM,   width=22)
        table.add_column("Value", style=WHITE)

        table.add_row("Mempool Scanner:",    scanner_status)
        table.add_row("Executor:",           executor_status)
        table.add_row("Mode:",               Text("DRY RUN", style=YELLOW) if dry_run else Text("LIVE", style=GREEN))
        table.add_row("Contract:",           Text(self.executor.contract_address[:20] + "..." if len(self.executor.contract_address) > 20 else self.executor.contract_address, style=CYAN))
        table.add_row("Wallet:",             Text(self.executor.wallet_address[:20] + "..." if len(self.executor.wallet_address) > 20 else self.executor.wallet_address, style=CYAN))
        table.add_row("Consec. Losses:",     Text(str(self.executor.consecutive_losses), style=RED if self.executor.consecutive_losses > 0 else WHITE))

        return Panel(table, title="[bold]Bot Status[/bold]", border_style="blue")

    # ─── Scanning stats ───────────────────────────────────────────────────────

    def _render_scanning(self) -> Panel:
        last_tx = _age(self.scanner.last_tx_time) if self.scanner.last_tx_time else "N/A"
        last_scan = _age(self.detector.last_scan_time) if self.detector.last_scan_time else "N/A"

        table = Table(box=None, show_header=False, padding=(0, 1))
        table.add_column("Key",   style=DIM,   width=18)
        table.add_column("Value", style=WHITE)

        table.add_row("Txs Seen:",        f"{self.scanner.txs_seen:,}")
        table.add_row("Swaps Decoded:",   f"{self.scanner.swaps_decoded:,}")
        table.add_row("Pairs Scanned:",   f"{self.detector.pairs_scanned:,}")
        table.add_row("Arbs Found:",      Text(str(self.detector.opportunities_found), style=GREEN))
        table.add_row("Gas Too High:",    Text(str(self.detector.opportunities_missed), style=YELLOW))
        table.add_row("Reconnects:",      str(self.scanner.reconnects))
        table.add_row("Last Tx:",         last_tx)
        table.add_row("Last Scan:",       last_scan)

        return Panel(table, title="[bold]Mempool[/bold]", border_style="cyan")

    # ─── Gas panel ────────────────────────────────────────────────────────────

    def _render_gas(self) -> Panel:
        g = self.gas_optimizer
        base   = g.current_base_fee_gwei
        prio   = g.current_priority_gwei
        avg    = g.avg_base_fee_gwei_1h
        hi     = g.max_base_fee_gwei_1h
        lo     = g.min_base_fee_gwei_1h

        # Color base fee
        if base < 20:
            base_color = GREEN
        elif base < 40:
            base_color = YELLOW
        else:
            base_color = RED

        table = Table(box=None, show_header=False, padding=(0, 1))
        table.add_column("Key",   style=DIM,   width=16)
        table.add_column("Value", style=WHITE)

        table.add_row("Base Fee:",      Text(f"{base:.2f} Gwei", style=base_color))
        table.add_row("Priority Fee:",  f"{prio:.2f} Gwei")
        table.add_row("Max Fee (high):",f"{(base * 1.25 + prio):.2f} Gwei")
        table.add_row("Avg (1h):",      f"{avg:.2f} Gwei")
        table.add_row("Low (1h):",      f"{lo:.2f} Gwei")
        table.add_row("High (1h):",     f"{hi:.2f} Gwei")
        table.add_row("Est. Arb Cost:", f"~${(450_000 * base * 1.25 * 1e9 / 1e18) * 2500:.2f}")

        return Panel(table, title="[bold]Gas Prices[/bold]", border_style="yellow")

    # ─── Execution stats ──────────────────────────────────────────────────────

    def _render_executions(self) -> Panel:
        ex = self.executor
        success_rate = ex.get_success_rate()
        rate_color   = GREEN if success_rate >= 80 else (YELLOW if success_rate >= 50 else RED)

        table = Table(box=None, show_header=False, padding=(0, 1))
        table.add_column("Key",   style=DIM,   width=20)
        table.add_column("Value", style=WHITE)

        table.add_row("Total Attempts:",    f"{ex.total_attempts:,}")
        table.add_row("Successes:",         Text(str(ex.total_successes), style=GREEN))
        table.add_row("Failures:",          Text(str(ex.total_failures), style=RED if ex.total_failures > 0 else WHITE))
        table.add_row("Success Rate:",      Text(f"{success_rate:.1f}%", style=rate_color))
        table.add_row("Total Profit:",      Text(_fmt_usd(ex.total_profit_usd), style=GREEN))
        table.add_row("Circuit Breaker:",   Text("OPEN (bot halted)", style="bold red") if ex.circuit_broken else Text("Closed", style=GREEN))
        table.add_row("Consecutive Loss:",  Text(str(ex.consecutive_losses), style=RED if ex.consecutive_losses > 0 else DIM))

        return Panel(table, title="[bold]Execution Stats[/bold]", border_style="green")

    # ─── Price spreads ────────────────────────────────────────────────────────

    def _render_spreads(self) -> Panel:
        spreads = self.detector.last_spread_bps

        table = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        table.add_column("Pair",   style=CYAN,  width=14)
        table.add_column("Spread", style=WHITE, width=10, justify="right")
        table.add_column("Signal", width=8)

        if spreads:
            for pair_key, bps in sorted(spreads.items(), key=lambda x: x[1], reverse=True):
                if bps >= 100:
                    signal = Text("STRONG", style=GREEN)
                elif bps >= 70:
                    signal = Text("WATCH",  style=YELLOW)
                else:
                    signal = Text("weak",   style=DIM)
                table.add_row(pair_key, f"{bps} bps", signal)
        else:
            table.add_row("--", "--", Text("scanning", style=DIM))

        return Panel(table, title="[bold]DEX Spreads[/bold]", border_style="magenta")

    # ─── Recent transactions ──────────────────────────────────────────────────

    def _render_recent_txs(self) -> Panel:
        recent = self.executor.get_recent_executions(8)

        table = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        table.add_column("Time",    style=DIM,   width=10)
        table.add_column("Status",               width=8)
        table.add_column("Pair",    style=CYAN,  width=14)
        table.add_column("Buy DEX", style=DIM,   width=12)
        table.add_column("Sell DEX",style=DIM,   width=12)
        table.add_column("Profit",               width=12, justify="right")
        table.add_column("Gas",     style=DIM,   width=8,  justify="right")
        table.add_column("Tx Hash", style=DIM,   width=18)

        if not recent:
            table.add_row("--", "--", "--", "--", "--", "--", "--", "No executions yet")
        else:
            for r in reversed(recent):
                status = Text("OK",   style=GREEN) if r.success else Text("FAIL", style=RED)
                profit = Text(_fmt_usd(r.profit_usd), style=GREEN if r.profit_usd > 0 else RED)
                opp    = r.opportunity
                pair   = f"{opp.token_a[:6]}/{opp.token_b[:6]}"
                gas_str = f"{r.gas_price_gwei:.0f}g"
                tx_str  = (r.tx_hash[:16] + "...") if r.tx_hash else "N/A"
                time_str = _fmt_time(r.submitted_at)

                table.add_row(
                    time_str, status, pair,
                    opp.buy_dex[:10], opp.sell_dex[:10],
                    profit, gas_str, tx_str
                )

        return Panel(table, title="[bold]Recent Transactions[/bold]", border_style="blue")
