"""Local daily collection and queued portfolio catch-up, using existing implementations.

No HTTP endpoint enables this module. The shell entry point supplies a local overlap
lock; PostgreSQL workers retain their own claim, lease and transaction boundaries.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.config import Settings, get_settings
from app.db.connection import connect, redact, session_connection
from app.exchange_reference import reference_context, reference_transaction

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)


def plain(value):
    if is_dataclass(value):
        value = asdict(value)
    return json.loads(json.dumps(value, default=str))


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(plain(value), out, indent=2)
            out.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def has_failure(value) -> bool:
    if isinstance(value, list):
        return any(has_failure(child) for child in value)
    if not isinstance(value, dict):
        return False
    for key, child in value.items():
        if key in {"error", "failed", "failures", "failed_days", "index_failures", "unavailable"}:
            if child:
                return True
        if isinstance(child, (dict, list)) and has_failure(child):
            return True
    return value.get("status") in {"FAILED", "REJECTED", "QUARANTINED"}


def has_incomplete(value) -> bool:
    if isinstance(value, list):
        return any(has_incomplete(child) for child in value)
    if not isinstance(value, dict) or value.get("disabled"):
        return False
    if value.get("complete") is True:
        return False
    if value.get("complete") is False or value.get("busy"):
        return True
    markers = {
        "deferred",
        "missing",
        "stale",
        "unclassified",
        "skipped_missing_price",
        "still_rejected",
        "retrying",
        "unhealable",
    }
    return any(
        (key in markers and bool(child)) or has_incomplete(child) for key, child in value.items()
    )


def feed_days(options, state, today):
    previous = state.get("exchange_feeds_completed")
    return (
        max(options.days, (today - date.fromisoformat(previous)).days + 1)
        if previous
        else options.days
    )


def weekly_due(options, state, today):
    previous = state.get("weekly_completed")
    return (
        options.weekly
        or bool(state.get("weekly_after"))
        or not previous
        or (today - date.fromisoformat(previous)).days >= 7
    )


@contextmanager
def connection(settings):
    with connect(settings.database_url) as pooled, session_connection(pooled, settings) as direct:
        with reference_transaction(direct):
            yield direct


@dataclass
class Phase:
    name: str
    action: Callable
    max_seconds: float = 180


@dataclass
class Context:
    conn: Any
    settings: Settings
    options: Any
    state: dict
    deadline: float
    clock: Callable

    def budget(self, cap=210.0):
        return max(0.0, min(cap, self.deadline - self.clock()))

    @property
    def today(self):
        return datetime.now(IST).date()


class Runner:
    def __init__(self, settings, options, state, *, clock=time.monotonic):
        self.settings, self.options, self.state, self.clock = settings, options, state, clock
        self.deadline = clock() + options.max_minutes * 60
        self.results: list[dict] = []
        self.on_update: Callable = lambda: None

    def execute(self, phase):
        record = {"name": phase.name, "state": "deferred"}
        self.results.append(record)
        started = self.clock()
        if started >= self.deadline and phase.name != "queue_status":
            record["reason"] = "Run budget exhausted; work remains queued"
            print(json.dumps(record), flush=True)
            self.on_update()
            return
        record["state"] = "running"
        print(f"Starting {phase.name}", flush=True)
        self.on_update()
        try:
            with connection(self.settings) as conn:
                ctx = Context(
                    conn,
                    self.settings,
                    self.options,
                    self.state,
                    min(self.deadline, started + phase.max_seconds),
                    self.clock,
                )
                result = plain(phase.action(ctx))
            record["result"] = result
            record["state"] = (
                "failed"
                if has_failure(result)
                else "disabled"
                if result.get("disabled")
                else "incomplete"
                if has_incomplete(result)
                else "complete"
            )
        except Exception as exc:
            record.update(state="failed", error=type(exc).__name__, reason=redact(str(exc)))
            # Individual workers persist their own failures; the private run report also
            # retains this stage's reason while later independent stages still execute.
            logger.warning("daily_stage_failed: %s: %s", phase.name, record["reason"])
        record["elapsed_seconds"] = round(self.clock() - started, 3)
        print(json.dumps(record), flush=True)
        self.on_update()

    def exit_code(self):
        final = {row["name"]: row["state"] for row in self.results}
        replacements = {"catch_up": "final_catch_up", "fund_snapshots": "final_fund_snapshots"}
        states = {
            row["state"]
            for row in self.results
            if not (
                row["state"] == "incomplete"
                and final.get(replacements.get(row["name"])) == "complete"
            )
        }
        return 1 if "failed" in states else 2 if states & {"deferred", "incomplete"} else 0


def public_requests(ctx):
    from app.exchange_reference import collect_requests

    totals = {"attempted": 0, "collected": 0, "failed": []}
    while ctx.budget() > 0:
        result = collect_requests(ctx.conn, budget_seconds=ctx.budget(300))
        for key in ("attempted", "collected"):
            totals[key] += result[key]
        totals["failed"].extend(result["failed"])
        if not result["attempted"] or not (result["collected"] or result["failed"]):
            break
    return totals


def jobs(ctx):
    from app.jobs.drain import drain_once
    from app.storage.factory import get_store

    result = drain_once(
        ctx.conn, get_store(ctx.settings), limit=5000, budget_seconds=ctx.budget(120)
    )
    return result


def cash_prices(ctx):
    from app.prices.pipe import fetch_day
    from app.prices.store import store_day

    result = fetch_day(ctx.today)
    stored = store_day(ctx.conn, result)
    return {
        "stored": plain(stored),
        "failures": plain(result.failures),
        "not_published": plain(result.not_published),
        "price_date": ctx.today,
    }


#: Calendar days of universe price history kept complete. Momentum and low-volatility
#: factor scores need a close on every one of the last 253 sessions for each ranked
#: member (app.analytics._vendor.advisor.service); 253 sessions span about 370 days.
UNIVERSE_HISTORY_DAYS = 400


def universe_history(ctx):
    from app.prices.history import backfill_universe_prices

    end = ctx.today - timedelta(days=1)
    result = backfill_universe_prices(
        ctx.conn,
        end - timedelta(days=UNIVERSE_HISTORY_DAYS),
        end,
        time_budget_seconds=ctx.budget(600),
        today=ctx.today,
    )
    return {**result, "complete": result["attempted"] == result["pending"]}


def exchange_feeds(ctx):
    from app.nse_worker import daily_feeds, reopen

    reopen(ctx.conn)
    result = daily_feeds(
        ctx.conn,
        ctx.settings,
        feed_days(ctx.options, ctx.state, ctx.today),
        include_financials=False,
    )
    if not has_failure(result):
        ctx.state["exchange_feeds_completed"] = ctx.today.isoformat()
    return result


def universe(ctx):
    from app.db.universe import DEFAULT_UNIVERSE, sync_index_universe
    from app.prices.sector_sources import nse_nifty500_source, parse_nse_sector_list

    return {
        "stored": sync_index_universe(
            ctx.conn,
            DEFAULT_UNIVERSE,
            parse_nse_sector_list(nse_nifty500_source().fetch()),
            ctx.today,
        )
    }


def fno_prices(ctx):
    from app.db.fno_reference import backfill_expiry_settlements, backfill_fno_prices

    days = backfill_fno_prices(ctx.conn, ctx.today - timedelta(days=ctx.options.days), ctx.today)
    ctx.conn.commit()
    return {
        "days": days,
        "expiry": backfill_expiry_settlements(ctx.conn, time_budget_seconds=ctx.budget(150)),
    }


def index_history(ctx, name):
    from app.prices.index_sources import TRI_INDICES, fetch_and_store
    from app.prices.pipe import NSE_BHAVCOPY_EARLIEST

    stored = 0
    for year in range(ctx.today.year, NSE_BHAVCOPY_EARLIEST.year - 1, -1):
        if ctx.budget() <= 0:
            return {"stored": stored, "complete": False}
        start = max(date(year, 1, 1), NSE_BHAVCOPY_EARLIEST)
        end = min(date(year, 12, 31), ctx.today)
        stored += fetch_and_store(ctx.conn, start, end, index=TRI_INDICES[name])
        ctx.conn.commit()
    return {"stored": stored, "complete": True}


def risk_free_history(ctx):
    from app.db.risk_free import upsert
    from app.prices.risk_free import fetch

    first = ctx.conn.execute("SELECT min(txn_date) FROM transaction").fetchone()[0]
    start = (first - timedelta(days=7)) if first else ctx.today - timedelta(days=14)
    # Calendar-year scopes are stable archive keys; previous years are SQL reuse.
    windows = [
        (date(year, 1, 1), min(date(year, 12, 31), ctx.today))
        for year in range(start.year, ctx.today.year + 1)
    ]
    stored = 0
    for begin, end in reversed(windows):
        if ctx.budget() <= 0:
            return {"stored": stored, "complete": False}
        stored += upsert(ctx.conn, fetch(begin, end))
        ctx.conn.commit()
    return {"stored": stored, "complete": True}


def index_valuation(ctx, name):
    from app.db.index_valuation import upsert
    from app.prices.index_valuation import fetch

    return {
        "stored": upsert(
            ctx.conn, fetch(name, ctx.today - timedelta(days=ctx.options.days), ctx.today)
        )
    }


def amfi(ctx):
    if not (ctx.settings.mf_reference_enabled and ctx.settings.amfi_fetch_enabled):
        return {"disabled": "MF_REFERENCE_ENABLED/AMFI_FETCH_ENABLED"}
    from app.prices.amfi_jobs import refresh_daily

    result = plain(
        refresh_daily(
            ctx.conn, timeout_seconds=ctx.settings.amfi_http_timeout_seconds, today=ctx.today
        )
    )
    result["complete"] = result.get("status") != "BUSY" and not result.get("stale")
    return result


def catch_up(ctx):
    from app.post_import import run_cycle

    passes = []
    while ctx.budget() >= 30:
        result = run_cycle(
            ctx.conn, ctx.settings, budget_seconds=ctx.budget(), include_sectors=True
        )
        passes.append(result)
        if result.get("busy") or not result.get("more_work") or not result.get("progress"):
            break
    return {
        "passes": passes,
        "complete": bool(passes)
        and not passes[-1].get("more_work")
        and not has_incomplete(passes[-1]),
        "failures": [p for p in passes if has_failure(p)],
    }


def funds(ctx):
    if not ctx.settings.mf_valuation_enabled:
        return {"disabled": "MF_VALUATION_ENABLED"}
    from app.funds.series import catch_up_fund_snapshots

    result = plain(
        catch_up_fund_snapshots(ctx.conn, today=ctx.today, time_budget_seconds=ctx.budget(180))
    )
    result["complete"] = result.get("complete", False) and not result.get("skipped")
    return result


def fundamentals(ctx):
    from app.routers.analytics import _refresh_fundamentals

    result = _refresh_fundamentals(ctx.conn, ctx.options.fundamentals_limit)
    result["complete"] = result.get("stored", 0) == result.get("requested", 0) and not result.get(
        "skipped_no_symbol"
    )
    return result


def exchange_financials(ctx):
    from app.db import exchange_financials as financials
    from app.db.prices import held_isins
    from app.db.universe import current_universe_isins

    candidates = sorted({*held_isins(ctx.conn), *current_universe_isins(ctx.conn)})
    symbols = financials.stale_symbols(ctx.conn, candidates, limit=max(1, len(candidates)))
    remaining, stored, missing = len(symbols), 0, []
    for isin, symbol in symbols.items():
        if ctx.budget() <= 0:
            break
        result = financials.refresh(ctx.conn, {isin: symbol})
        stored += result["stored"]
        missing.extend(result["missing"])
        remaining -= 1
    return {
        "stored": stored,
        "missing": missing,
        "remaining": remaining,
        "complete": not remaining and not missing,
    }


def weekly_dividends(ctx):
    if not weekly_due(ctx.options, ctx.state, ctx.today):
        return {"disabled": "Weekly history was already collected in the last seven days"}
    from app.db.dividend_events import discover_dividends_from_yfinance
    from app.ingest.action_source import ActionBatchFailed

    cursor = ctx.state.get("weekly_after")
    try:
        result = discover_dividends_from_yfinance(
            ctx.conn,
            isins=None,
            rights_reference=ctx.settings.yahoo_data_rights_reference,
            after=cursor,
            time_budget_seconds=ctx.budget(180),
        )
    except ActionBatchFailed as exc:
        ctx.state["weekly_after"] = getattr(exc, "next_after", None)
        raise
    cursor = result.get("next_after")
    ctx.state["weekly_after"] = cursor
    if cursor is None:
        ctx.state["weekly_completed"] = ctx.today.isoformat()
    return {**result, "complete": cursor is None}


def analytics_warm(ctx):
    from app.db.analytics_cache import prune
    from app.jobs.drain import enqueue_analytics_warm

    pruned = prune(ctx.conn, ctx.today - timedelta(days=7))
    return {"queued": enqueue_analytics_warm(ctx.conn, ctx.today), "pruned": pruned}


def narrative(ctx, basic=False):
    if not ctx.settings.insight_generation_enabled:
        return {"disabled": "INSIGHT_GENERATION_ENABLED"}
    from app.routers import narrative, narrative_basic

    return (narrative_basic if basic else narrative)._run(ctx.conn, ctx.settings, ctx.today)


def queue_status(ctx):
    from app.prices.amfi_jobs import AMFI_TASK_KINDS

    tables = {}
    for name, sql in {
        "reference": "SELECT kind,state,count(*) FROM reference_sync_task GROUP BY kind,state",
        "jobs": "SELECT kind,status,count(*) FROM job GROUP BY kind,status",
        "public_requests": "SELECT state,count(*) FROM exchange_reference_request GROUP BY state",
        "snapshots": "SELECT investor_id,advised_only,count(*),max(as_of) "
        "FROM portfolio_snapshot GROUP BY investor_id,advised_only",
        "analytics_cache": "SELECT as_of,count(*) FROM analytics_report_cache GROUP BY as_of",
    }.items():
        tables[name] = plain(ctx.conn.execute(sql).fetchall())
    tables["missing_reference"] = plain(
        ctx.conn.execute(
            "SELECT kind,task_key,last_error,payload->'reference_missing' "
            "FROM reference_sync_task WHERE payload ? 'reference_missing' ORDER BY kind,task_key"
        ).fetchall()
    )
    tables["due_jobs"] = ctx.conn.execute(
        "SELECT count(*) FROM job WHERE status='PENDING' AND available_at<=now()"
    ).fetchone()[0]
    tables["failed_jobs"] = ctx.conn.execute(
        "SELECT count(*) FROM job WHERE status='FAILED'"
    ).fetchone()[0]
    tables["delayed_portfolio_jobs"] = ctx.conn.execute(
        "SELECT count(*) FROM job WHERE status='PENDING' AND available_at>now() "
        "AND kind IN ('parse_document','email_delivery','email_document','email_portfolio_refresh',"
        "'security_sectors','security_health_profiles','analytics_report_warm')"
    ).fetchone()[0]
    tables["reference_left"] = ctx.conn.execute(
        "SELECT count(*) FROM reference_sync_task WHERE state <> 'done' "
        "AND (%s OR kind <> ALL(%s))",
        (
            ctx.settings.mf_reference_enabled and ctx.settings.amfi_fetch_enabled,
            list(AMFI_TASK_KINDS),
        ),
    ).fetchone()[0]
    tables["uncollected"] = ctx.conn.execute(
        "SELECT count(*) FROM exchange_reference_request WHERE state <> 'collected'"
    ).fetchone()[0]
    tables["complete"] = not any(
        tables[k]
        for k in (
            "missing_reference",
            "due_jobs",
            "failed_jobs",
            "delayed_portfolio_jobs",
            "reference_left",
            "uncollected",
        )
    )
    return tables


def phases():
    from functools import partial

    from app.prices.index_sources import TRI_INDICES

    return [
        Phase("public_requests", public_requests, 300),
        Phase("jobs_before_imports", jobs),
        Phase("universe", universe),
        Phase("cash_prices", cash_prices),
        Phase("universe_history", universe_history, 600),
        Phase("exchange_feeds", exchange_feeds),
        Phase("fno_prices", fno_prices),
        *[Phase(f"tri_history:{name}", partial(index_history, name=name)) for name in TRI_INDICES],
        Phase("amfi_daily", amfi),
        Phase("catch_up", catch_up, 900),
        Phase("fund_snapshots", funds),
        Phase("risk_free_history", risk_free_history),
        *[
            Phase(f"index_valuation:{name}", partial(index_valuation, name=name))
            for name in TRI_INDICES
        ],
        Phase("fundamentals", fundamentals),
        Phase("exchange_financials", exchange_financials),
        Phase("weekly_dividends", weekly_dividends),
        Phase("enrichment_jobs", jobs),
        Phase("final_catch_up", catch_up, 900),
        Phase("final_fund_snapshots", funds),
        Phase("analytics_warm", analytics_warm),
        Phase("analytics_jobs", jobs),
        Phase("narrative", narrative),
        Phase("narrative_basic", partial(narrative, basic=True)),
        Phase("queue_status", queue_status),
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / "trackr-backend/.env.vercel")
    parser.add_argument("--max-minutes", type=float, default=90)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--fundamentals-limit", type=int, default=64)
    parser.add_argument("--weekly", action="store_true", help="force weekly dividend history")
    parser.add_argument("--plan", action="store_true", help="list stages; no settings/DB/network")
    parser.add_argument("--report", type=Path, default=ROOT / ".trackr-logs/daily-jobs-last.json")
    parser.add_argument(
        "--state-file", type=Path, default=ROOT / ".trackr-logs/daily-jobs-state.json"
    )
    options = parser.parse_args(argv)
    if (
        not math.isfinite(options.max_minutes)
        or options.max_minutes <= 0
        or options.days <= 0
        or options.fundamentals_limit <= 0
    ):
        parser.error("max-minutes, days and fundamentals-limit must be positive")
    if options.plan:
        print("\n".join(phase.name for phase in phases()))
        return 0
    logging.basicConfig(level=logging.WARNING)
    report = {"started_at": datetime.now(IST).isoformat(), "state": "running", "stages": []}
    write_json(options.report, report)
    runner = None
    try:
        if not options.env_file.is_file():
            raise ValueError(f"Environment file does not exist: {options.env_file}")
        from dotenv import dotenv_values

        values = dotenv_values(options.env_file)
        if not values.get("DATABASE_URL"):
            raise ValueError("The chosen environment file must explicitly contain DATABASE_URL")
        # Explicit file wins over a dev terminal's DATABASE_URL. Never shell-source it.
        os.environ.update(
            {key.upper(): value for key, value in values.items() if value is not None}
        )
        os.environ["TRACKR_ENV_FILE"] = str(options.env_file.resolve())
        os.environ["NSE_API_ENABLED"] = "true"  # collector process only
        get_settings.cache_clear()
        settings = get_settings()
        if (
            settings.storage_backend == "local"
            and not Path(settings.local_storage_root).is_absolute()
        ):
            # Match the normal backend working directory even when the .sh is invoked
            # from elsewhere. Never accidentally mark a stored document as missing.
            os.environ["LOCAL_STORAGE_ROOT"] = str(
                (ROOT / "trackr-backend" / settings.local_storage_root).resolve()
            )
            get_settings.cache_clear()
            settings = get_settings()
        with connection(settings) as conn:
            from app.db.migrate import check_drift

            missing = check_drift(conn)
            if missing:
                raise RuntimeError(
                    "Apply required migrations before daily jobs: " + ", ".join(missing)
                )
        state = json.loads(options.state_file.read_text()) if options.state_file.exists() else {}
        runner = Runner(settings, options, state)
        report["stages"] = runner.results
        runner.on_update = lambda: write_json(options.report, report)
        with reference_context(None, collect=True):
            for phase in phases():
                runner.execute(phase)
                write_json(options.state_file, state)
                write_json(options.report, report)
        report["exit_code"] = runner.exit_code()
    except KeyboardInterrupt:
        report.update(exit_code=130, error="Interrupted; uncompleted queued work remains durable")
    except Exception as exc:
        report.update(exit_code=1, error=type(exc).__name__, reason=redact(str(exc)))
    report["finished_at"] = datetime.now(IST).isoformat()
    report["state"] = {0: "complete", 1: "failed", 2: "incomplete", 130: "interrupted"}[
        report["exit_code"]
    ]
    write_json(options.report, report)
    print(
        json.dumps(
            {
                "exit_code": report["exit_code"],
                "report": str(options.report),
                "error": report.get("error"),
                "reason": report.get("reason"),
            }
        ),
        flush=True,
    )
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
