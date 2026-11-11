"""
The demo's run history: 45 days of Contoso's schedules, as the jobs collector
would have stored them.

- `pl_master` (daily 02:00 W. Europe) invokes `pl_ingest_d365`, then
  `nb_orchestrate_silver` (which `%run`s `nb_common` and `runMultiple`s the
  silver notebooks), then `nb_gold_model`, then refreshes the Sales Model.
- `pl_ingest_d365` copies three D365 tables, runs `nb_silver_customers` and
  refreshes `df_fx_rates` — whose ECB source has answered 401 for ten days, so
  the ingest pipeline fails while `pl_master` (which carries on "on completion")
  stays green: the case a monitor of top-level runs alone would miss.
- Two `pl_master` failures (a silver merge, a throttled capacity), a manual
  re-run, a cancelled manual run.
- `pl_daily_sales` every four hours; the Sales Model's own weekday refreshes.
- Running right now: a manual re-run of `pl_master`, past its ingest and inside
  `nb_orchestrate_silver` — what the monitor's live views show.

Deterministic (seeded), anchored to the time the demo is built.
"""

import datetime as dt
import random
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import delete

from . import ids
from .collectors.jobs import link_child_runs
from .models import ItemSchedule, JobActivityRun, JobRun

DAYS = 45
BERLIN = ZoneInfo("Europe/Berlin")


def seed(session, customer_id: str, tenant_id: str) -> dict:
    from .demo import (
        DF_FX_RATES,
        NB_GOLD_MODEL,
        NB_ORCHESTRATE,
        NB_SILVER_CUSTOMERS,
        PL_DAILY,
        PL_INGEST,
        PL_MASTER,
        SM_SALES,
        WS_BI,
        WS_DATA,
    )

    item = {
        "master": ids.item_id(WS_DATA, PL_MASTER),
        "ingest": ids.item_id(WS_DATA, PL_INGEST),
        "daily": ids.item_id(WS_DATA, PL_DAILY),
        "orchestrate": ids.item_id(WS_DATA, NB_ORCHESTRATE),
        "customers": ids.item_id(WS_DATA, NB_SILVER_CUSTOMERS),
        "gold": ids.item_id(WS_DATA, NB_GOLD_MODEL),
        "fx": ids.item_id(WS_DATA, DF_FX_RATES),
        "model": ids.item_id(WS_BI, SM_SALES),
    }
    for table in (JobActivityRun, JobRun, ItemSchedule):
        session.execute(delete(table).where(table.tenant_id == tenant_id))
    session.flush()

    rng = random.Random(20261111)
    now = dt.datetime.utcnow().replace(microsecond=0)
    counts = {"demo_runs": 0, "demo_activity_runs": 0}

    def guid(*parts) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, "fabribuddy-demo/" + "/".join(map(str, parts))))

    def run(key, start, end, status, job_type, invoke, failure="") -> JobRun:
        row = JobRun(
            customer_id=customer_id,
            tenant_id=tenant_id,
            item_id=item[key],
            run_guid=guid(key, start.isoformat()),
            job_type=job_type,
            status=status,
            started_at=start,
            ended_at=end,
            parameters={
                "invokeType": invoke,
                **({"failureReason": {"message": failure}} if failure else {}),
            },
        )
        session.add(row)
        session.flush()
        counts["demo_runs"] += 1
        return row

    def activity(parent, name, kind, start, end, status, target=None, child=None, error=""):
        session.add(
            JobActivityRun(
                customer_id=customer_id,
                tenant_id=tenant_id,
                job_run_id=parent.id,
                activity_run_id=guid(parent.run_guid, name),
                name=name,
                activity_type=kind,
                status=status,
                started_at=start,
                ended_at=end,
                target_item_id=item[target] if target else None,
                target_run_guid=child.run_guid if child else "",
                error=error,
            )
        )
        counts["demo_activity_runs"] += 1

    def secs(low, high) -> dt.timedelta:
        return dt.timedelta(seconds=rng.randint(low, high))

    fx_error = (
        "DataSource.Error: Web.Contents failed to get contents from "
        "'https://data-api.ecb.europa.eu/service/data/EXR/D..EUR.SP00.A' (401): Unauthorized"
    )

    def ingest(start: dt.datetime, days_ago: int, invoke: str, cancel_at=None):
        """One pl_ingest_d365 run and its children; returns (run, end, ok)."""
        t = start + secs(2, 4)
        copies = []
        for name, low, high in (
            ("Copy_CustTable", 80, 150),
            ("Copy_SalesTable", 170, 290),
            ("Copy_SalesLine", 360, 540),
        ):
            begin = t + secs(1, 6)
            copies.append((name, begin, begin + secs(low, high)))
        foreach_end = max(c[2] for c in copies) + secs(1, 3)
        fx_start = t + secs(1, 3)
        fx_failed = days_ago < 10
        fx_end = fx_start + (secs(20, 35) if fx_failed else secs(45, 95))
        silver_start = foreach_end + secs(2, 5)
        silver_end = silver_start + secs(170, 300)
        end = max(silver_end, fx_end) + secs(2, 4)
        if cancel_at:
            end = cancel_at
        status = "Cancelled" if cancel_at else ("Failed" if fx_failed else "Completed")
        parent = run(
            "ingest",
            start,
            end,
            status,
            "Pipeline",
            invoke,
            "Activity Refresh_FX failed: " + fx_error if fx_failed and not cancel_at else "",
        )

        fx_run = run(
            "fx",
            fx_start + secs(1, 2),
            min(fx_end, end),
            "Failed" if fx_failed else "Completed",
            "Refresh",
            "Pipeline",
            fx_error if fx_failed else "",
        )
        activity(
            parent,
            "Refresh_FX",
            "RefreshDataflow",
            fx_start,
            min(fx_end, end),
            "Failed" if fx_failed else "Succeeded",
            "fx",
            fx_run,
            fx_error if fx_failed else "",
        )
        if cancel_at:
            activity(parent, "ForEach_Tables", "ForEach", t, cancel_at, "Cancelled")
            for name, begin, _finish in copies:
                if begin < cancel_at:
                    activity(parent, name, "Copy", begin, cancel_at, "Cancelled")
            return parent, end, False
        activity(parent, "ForEach_Tables", "ForEach", t, foreach_end, "Succeeded")
        for name, begin, finish in copies:
            activity(parent, name, "Copy", begin, finish, "Succeeded")
        customers = run(
            "customers", silver_start + secs(1, 3), silver_end, "Completed", "RunNotebook", "Pipeline"
        )
        activity(
            parent,
            "Run_Silver_Customers",
            "TridentNotebook",
            silver_start,
            silver_end,
            "Succeeded",
            "customers",
            customers,
        )
        return parent, end, not fx_failed

    def master(start: dt.datetime, days_ago: int, invoke: str, *, silver_fails=False,
               refresh_fails=False, cancel_after=None):
        cancel_at = start + cancel_after if cancel_after else None
        t = start + secs(3, 6)
        child, ingest_end, ingest_ok = ingest(t + secs(1, 2), days_ago, "Pipeline", cancel_at)
        if cancel_at:
            parent = run("master", start, cancel_at + secs(1, 3), "Cancelled", "Pipeline", invoke,
                         "Cancelled by Megan Bowen")
            activity(parent, "Ingest_D365", "InvokePipeline", t, cancel_at, "Cancelled",
                     "ingest", child)
            return parent
        steps = []
        steps.append(("Ingest_D365", "InvokePipeline", t, ingest_end + secs(1, 3),
                      "Succeeded" if ingest_ok else "Failed", "ingest", child,
                      "" if ingest_ok else "Invoked pipeline pl_ingest_d365 failed: " + fx_error))
        s_start = steps[-1][3] + secs(2, 5)
        s_end = s_start + (secs(150, 240) if silver_fails else secs(380, 560))
        silver_error = (
            "RunMultipleFailedException: 1 of 2 notebooks failed. silver_sales: "
            "AnalysisException: [DELTA_FAILED_TO_MERGE_FIELDS] Failed to merge fields "
            "'net_amount' and 'net_amount' (DECIMAL(18,2) vs DOUBLE)"
        )
        orch = run("orchestrate", s_start + secs(2, 4), s_end,
                   "Failed" if silver_fails else "Completed", "RunNotebook", "Pipeline",
                   silver_error if silver_fails else "")
        steps.append(("Silver_Orchestration", "TridentNotebook", s_start, s_end,
                      "Failed" if silver_fails else "Succeeded", "orchestrate", orch,
                      silver_error if silver_fails else ""))
        end = s_end
        failure = ""
        if silver_fails:
            failure = "Activity Silver_Orchestration failed: " + silver_error
        else:
            g_start = s_end + secs(2, 5)
            g_end = g_start + secs(230, 360)
            gold = run("gold", g_start + secs(2, 4), g_end, "Completed", "RunNotebook", "Pipeline")
            steps.append(("Gold_Model", "TridentNotebook", g_start, g_end, "Succeeded",
                          "gold", gold, ""))
            r_start = g_end + secs(2, 5)
            r_end = r_start + (secs(40, 80) if refresh_fails else secs(110, 190))
            refresh_error = (
                "The operation was throttled by Power BI because of insufficient capacity "
                "(background CU usage 118% of F64). Retry after 2026-11-05 01:30 UTC."
            )
            model = run("model", r_start + secs(1, 3), r_end,
                        "Failed" if refresh_fails else "Completed", "Refresh", "ViaEnhancedApi",
                        refresh_error if refresh_fails else "")
            steps.append(("Refresh_Sales_Model", "PBISemanticModelRefresh", r_start, r_end,
                          "Failed" if refresh_fails else "Succeeded", "model", model,
                          refresh_error if refresh_fails else ""))
            end = r_end
            if refresh_fails:
                failure = "Activity Refresh_Sales_Model failed: " + refresh_error
        parent = run("master", start, end + secs(1, 3), "Failed" if failure else "Completed",
                     "Pipeline", invoke, failure)
        for name, kind, begin, finish, status, target, child_run, error in steps:
            activity(parent, name, kind, begin, finish, status, target, child_run, error)
        return parent

    # pl_master: nightly at 02:00 Berlin time, plus a re-run and a cancelled run.
    today = now.date()
    for days_ago in range(DAYS, -1, -1):
        day = today - dt.timedelta(days=days_ago)
        local = dt.datetime(day.year, day.month, day.day, 2, 0, tzinfo=BERLIN)
        start = local.astimezone(dt.UTC).replace(tzinfo=None) + secs(0, 9)
        if start > now - dt.timedelta(minutes=45):
            continue
        master(start, days_ago, "Scheduled",
               silver_fails=days_ago == 17, refresh_fails=days_ago == 6)
        if days_ago == 17:
            master(start.replace(hour=8, minute=41) + secs(0, 30), days_ago, "Manual")
        if days_ago == 23:
            master(start.replace(hour=13, minute=5), days_ago, "Manual",
                   cancel_after=dt.timedelta(minutes=3, seconds=12))

    # Running now: Megan's manual re-run of pl_master, 17 minutes in. The ingest
    # is done (failed on the FX refresh again, carried on "on completion");
    # the silver orchestration is running; gold and the refresh haven't started.
    start = now - dt.timedelta(minutes=17)
    t = start + secs(3, 6)
    child, ingest_end, ingest_ok = ingest(t + secs(1, 2), 0, "Pipeline")
    s_start = ingest_end + secs(3, 5)
    orch = run("orchestrate", s_start + secs(2, 4), None, "InProgress", "RunNotebook", "Pipeline")
    parent = run("master", start, None, "InProgress", "Pipeline", "Manual")
    activity(parent, "Ingest_D365", "InvokePipeline", t, ingest_end + secs(1, 3),
             "Succeeded" if ingest_ok else "Failed", "ingest", child,
             "" if ingest_ok else "Invoked pipeline pl_ingest_d365 failed: " + fx_error)
    activity(parent, "Silver_Orchestration", "TridentNotebook", s_start, None, "InProgress",
             "orchestrate", orch)

    # pl_daily_sales: every four hours from 00:30 UTC.
    pos_error = (
        "ErrorCode=SqlFailedToConnect,'Cannot connect to SQL Database: "
        "'pos-sql.contoso.com', Database: 'POS'. Login timeout expired.'"
    )
    first = dt.datetime.combine(today - dt.timedelta(days=DAYS), dt.time(0, 30))
    slot = 0
    start = first
    while start < now - dt.timedelta(minutes=10):
        failed = slot % 37 == 11 or slot % 53 == 40
        t = start + secs(2, 8)
        lookup_end = t + secs(4, 9)
        copy_end = lookup_end + (secs(31, 31) if failed else secs(55, 130))
        end = copy_end + (secs(1, 2) if failed else secs(3, 6))
        parent = run("daily", start + secs(0, 3), end, "Failed" if failed else "Completed",
                     "Pipeline", "Scheduled",
                     "Activity Copy_POS_Sales failed: " + pos_error if failed else "")
        activity(parent, "Lookup_Watermark", "Lookup", t, lookup_end, "Succeeded")
        activity(parent, "Copy_POS_Sales", "Copy", lookup_end + secs(1, 2), copy_end,
                 "Failed" if failed else "Succeeded", error=pos_error if failed else "")
        if not failed:
            activity(parent, "Set_Watermark", "Script", copy_end + secs(1, 2), end, "Succeeded")
        slot += 1
        start += dt.timedelta(hours=4)

    # Sales Model: its own weekday refreshes at 07:00, 12:00 and 17:00 Berlin time.
    for days_ago in range(DAYS, -1, -1):
        day = today - dt.timedelta(days=days_ago)
        if day.weekday() >= 5:
            continue
        for hour in (7, 12, 17):
            local = dt.datetime(day.year, day.month, day.day, hour, 0, tzinfo=BERLIN)
            start = local.astimezone(dt.UTC).replace(tzinfo=None) + secs(5, 40)
            if start > now - dt.timedelta(minutes=10):
                continue
            failed = days_ago == 6 and hour == 7
            run("model", start, start + (secs(30, 60) if failed else secs(95, 210)),
                "Failed" if failed else "Completed", "Refresh", "Scheduled",
                "The operation was throttled by Power BI because of insufficient capacity."
                if failed else "")

    # Schedules, as the Job Scheduler and the model's refresh schedule report them.
    for key, job_type, config in (
        ("master", "Pipeline", {
            "type": "Daily", "times": ["02:00"],
            "startDateTime": "2026-01-01T00:00:00", "endDateTime": "2027-12-31T23:59:00",
            "localTimeZoneId": "W. Europe Standard Time",
        }),
        ("daily", "Pipeline", {
            "type": "Cron", "interval": 240,
            "startDateTime": "2026-01-01T00:30:00", "endDateTime": "2027-12-31T23:59:00",
            "localTimeZoneId": "UTC",
        }),
        ("model", "Refresh", {
            "type": "Weekly",
            "weekdays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
            "times": ["07:00", "12:00", "17:00"],
            "localTimeZoneId": "W. Europe Standard Time",
        }),
    ):
        session.add(
            ItemSchedule(
                customer_id=customer_id,
                tenant_id=tenant_id,
                item_id=item[key],
                schedule_guid=guid("schedule", key),
                job_type=job_type,
                enabled=True,
                config=config,
            )
        )
    session.flush()
    link_child_runs(session, tenant_id)
    session.commit()
    return counts
