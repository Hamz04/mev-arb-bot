"""
main.py
=======
MEV Arbitrage Bot — Main entry point

Starts the async event loop and coordinates:
  - Mempool scanner  (WebSocket subscription to pending txs)
  - Opportunity detector  (cross-DEX price monitoring)
  - Executor  (flash loan transaction builder + submitter)
  - Dashboard  (rich terminal UI)

Usage:
    cd bot
    python main.py

    # Or with a specific log level:
    LOG_LEVEL=DEBUG python main.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# ── Load environment variables before any other imports ──────────────────────
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

# ── Local imports (after env is loaded) ──────────────────────────────────────
from mempool_scanner    import MempoolScanner
from opportunity_detector import OpportunityDetector, ArbOpportunity
from executor           import Executor
from gas_optimizer      import GasOptimizer
from dashboard          import Dashboard

# ─────────────────────────────────────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────────────────────────────────────

def setup_logging() -> logging.Logger:
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    log_file  = os.getenv("LOG_FILE", "")

    fmt = "%(asctime)s [%(levelname)-8s] %(name)-20s %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(level=log_level, format=fmt, datefmt=datefmt, handlers=handlers)

    # Silence noisy third-party loggers
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("web3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    return logging.getLogger("mev.main")


# ─────────────────────────────────────────────────────────────────────────────
# Config loader
# ─────────────────────────────────────────────────────────────────────────────

class Config:
    """Typed config loaded from environment variables."""

    def __init__(self) -> None:
        self.private_key:    str   = os.getenv("PRIVATE_KEY", "")
        self.ws_url:         str   = os.getenv("WEBSOCKET_URL", "")
        self.mainnet_rpc:    str   = os.getenv("MAINNET_RPC_URL", "")
        self.sepolia_rpc:    str   = os.getenv("SEPOLIA_RPC_URL", "")
        self.contract_addr:  str   = os.getenv("FLASHLOAN_CONTRACT_ADDRESS", "")
        self.min_profit_usd: float = float(os.getenv("MIN_PROFIT_USD", "10"))
        self.max_gas_gwei:   float = float(os.getenv("MAX_GAS_GWEI", "50"))
        self.min_profit_bps: int   = int(os.getenv("MIN_PROFIT_BPS", "30"))
        self.max_loan_usd:   float = float(os.getenv("MAX_LOAN_USD", "100000"))
        self.max_consec_loss:int   = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
        self.dashboard_refresh: float = float(os.getenv("DASHBOARD_REFRESH", "2"))

        # DEX router addresses
        self.uni_v2_router:  str = os.getenv("UNISWAP_V2_ROUTER",  "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D")
        self.uni_v3_router:  str = os.getenv("UNISWAP_V3_ROUTER",  "0xE592427A0AEce92De3Edee1F18E0157C05861564")
        self.uni_v3_quoter:  str = os.getenv("UNISWAP_V3_QUOTER",  "0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6")
        self.sushi_router:   str = os.getenv("SUSHISWAP_ROUTER",   "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F")

        # Token addresses
        self.weth:  str = os.getenv("WETH",  "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
        self.usdc:  str = os.getenv("USDC",  "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")
        self.usdt:  str = os.getenv("USDT",  "0xdAC17F958D2ee523a2206206994597C13D831ec7")
        self.dai:   str = os.getenv("DAI",   "0x6B175474E89094C44Da98b954EedeAC495271d0F")
        self.wbtc:  str = os.getenv("WBTC",  "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599")

    def validate(self) -> list[str]:
        """Return list of validation errors (empty = config is valid)."""
        errors = []
        if not self.private_key or self.private_key == "0x" + "0" * 64:
            errors.append("PRIVATE_KEY not set or is placeholder")
        if not self.ws_url:
            errors.append("WEBSOCKET_URL not set")
        if not self.mainnet_rpc:
            errors.append("MAINNET_RPC_URL not set")
        if not self.contract_addr or self.contract_addr == "0x" + "0" * 40:
            errors.append("FLASHLOAN_CONTRACT_ADDRESS not set — run deploy.js first")
        return errors

    def __repr__(self) -> str:
        pk_preview = f"{self.private_key[:6]}...{self.private_key[-4:]}" if len(self.private_key) > 10 else "NOT SET"
        return (
            f"Config(ws_url={self.ws_url!r}, "
            f"contract={self.contract_addr!r}, "
            f"min_profit_usd={self.min_profit_usd}, "
            f"max_gas_gwei={self.max_gas_gwei}, "
            f"private_key={pk_preview})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# ArbBot — top-level orchestrator
# ─────────────────────────────────────────────────────────────────────────────

class ArbBot:
    """
    Coordinates all bot subsystems:
      scanner   -> detects mempool swap txs
      detector  -> monitors DEX prices, finds arb opportunities
      executor  -> executes flash loan arbs on-chain
      dashboard -> displays live stats in terminal
    """

    def __init__(self, config: Config, logger: logging.Logger) -> None:
        self.config   = config
        self.log      = logger
        self._running = False
        self._tasks: list[asyncio.Task] = []

        # Shared queue: scanner -> detector -> executor
        self.opportunity_queue: asyncio.Queue[ArbOpportunity] = asyncio.Queue(maxsize=100)

        # Instantiate subsystems
        self.gas_optimizer = GasOptimizer(rpc_url=config.mainnet_rpc)

        self.scanner = MempoolScanner(
            ws_url=config.ws_url,
            rpc_url=config.mainnet_rpc,
            uni_v2_router=config.uni_v2_router,
            uni_v3_router=config.uni_v3_router,
            sushi_router=config.sushi_router,
        )

        self.detector = OpportunityDetector(
            rpc_url=config.mainnet_rpc,
            uni_v2_router=config.uni_v2_router,
            uni_v3_router=config.uni_v3_router,
            uni_v3_quoter=config.uni_v3_quoter,
            sushi_router=config.sushi_router,
            min_profit_usd=config.min_profit_usd,
            min_profit_bps=config.min_profit_bps,
            max_gas_gwei=config.max_gas_gwei,
            gas_optimizer=self.gas_optimizer,
        )

        self.executor = Executor(
            rpc_url=config.mainnet_rpc,
            private_key=config.private_key,
            contract_address=config.contract_addr,
            gas_optimizer=self.gas_optimizer,
            max_consecutive_losses=config.max_consec_loss,
        )

        self.dashboard = Dashboard(
            scanner=self.scanner,
            detector=self.detector,
            executor=self.executor,
            gas_optimizer=self.gas_optimizer,
            refresh_interval=config.dashboard_refresh,
        )

    async def start(self) -> None:
        self._running = True
        self.log.info("=" * 60)
        self.log.info("  MEV Arbitrage Bot Starting")
        self.log.info("=" * 60)
        self.log.info(f"Contract:    {self.config.contract_addr}")
        self.log.info(f"Min profit:  ${self.config.min_profit_usd}")
        self.log.info(f"Max gas:     {self.config.max_gas_gwei} Gwei")
        self.log.info(f"Circuit:     stop after {self.config.max_consec_loss} consecutive losses")

        # Start all subsystems as concurrent tasks
        self._tasks = [
            asyncio.create_task(self._run_scanner(),   name="scanner"),
            asyncio.create_task(self._run_detector(),  name="detector"),
            asyncio.create_task(self._run_executor(),  name="executor"),
            asyncio.create_task(self._run_dashboard(), name="dashboard"),
        ]

        self.log.info("All subsystems started. Bot is live.")

        try:
            # Wait for all tasks — any unhandled exception will propagate here
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            self.log.info("Bot shutting down (cancelled).")
        except Exception as e:
            self.log.error(f"Fatal error in bot: {e}", exc_info=True)
        finally:
            await self.stop()

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self.log.info("Stopping all tasks...")
        for task in self._tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.log.info("Bot stopped cleanly.")

    async def _run_scanner(self) -> None:
        """Run the mempool scanner. Feeds the detector's internal swap queue."""
        self.log.info("[scanner] Starting mempool scanner...")
        try:
            await self.scanner.start()
        except Exception as e:
            self.log.error(f"[scanner] Fatal: {e}", exc_info=True)
            raise

    async def _run_detector(self) -> None:
        """Run the opportunity detector. Pulls from scanner, pushes to opportunity_queue."""
        self.log.info("[detector] Starting opportunity detector...")
        try:
            await self.detector.start(
                mempool_queue=self.scanner.swap_queue,
                opportunity_queue=self.opportunity_queue,
            )
        except Exception as e:
            self.log.error(f"[detector] Fatal: {e}", exc_info=True)
            raise

    async def _run_executor(self) -> None:
        """Run the executor. Consumes from opportunity_queue, executes flash loans."""
        self.log.info("[executor] Starting executor...")
        try:
            await self.executor.start(opportunity_queue=self.opportunity_queue)
        except Exception as e:
            self.log.error(f"[executor] Fatal: {e}", exc_info=True)
            raise

    async def _run_dashboard(self) -> None:
        """Run the terminal dashboard."""
        self.log.info("[dashboard] Starting dashboard...")
        try:
            await self.dashboard.start()
        except Exception as e:
            self.log.error(f"[dashboard] Fatal: {e}", exc_info=True)
            raise


# ─────────────────────────────────────────────────────────────────────────────
# Signal handling
# ─────────────────────────────────────────────────────────────────────────────

def setup_signal_handlers(bot: ArbBot, loop: asyncio.AbstractEventLoop) -> None:
    """Register SIGINT / SIGTERM handlers for clean shutdown."""
    def _shutdown(sig_name: str) -> None:
        print(f"\nReceived {sig_name} — shutting down gracefully...")
        loop.create_task(bot.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig.name: _shutdown(s))
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def async_main() -> None:
    log = setup_logging()
    config = Config()

    log.info("Loading configuration...")
    log.debug(repr(config))

    # Validate config
    errors = config.validate()
    if errors:
        log.warning("Configuration warnings (bot will run in simulation mode):")
        for err in errors:
            log.warning(f"  - {err}")
        log.warning("Set all variables in .env to enable live trading.")

    # Create and start the bot
    bot = ArbBot(config=config, logger=log)

    loop = asyncio.get_running_loop()
    setup_signal_handlers(bot, loop)

    await bot.start()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    except Exception as e:
        print(f"\nFatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
