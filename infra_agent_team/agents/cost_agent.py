"""Cost management agent.

* forecast()  - before deploy: prices the Terraform plan (fixed hourly + usage-based) for
                the spec's usage profile; feeds the budget gate.
* current()   - after deploy: cost to date from CloudWatch usage metrics (free, near
                real-time) and, opt-in, Cost Explorer billing data filtered by run tag.
* orphans()   - after destroy: anything still carrying this run's iat:run-id tag.
* expired()   - runs past their TTL, i.e. cleanup candidates for `iat sweep`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import pricing_dir
from ..log import say

AGENT = "cost-agent"
HOURS_PER_MONTH = 730

USAGE_PER_REQUEST = {          # modelling assumptions for the default blueprint
    "ddb_writes": 0.6,         # share of requests that are submissions (1 WRU each)
    "ddb_reads": 3.0,          # report queries read many small items (RRUs per request, averaged)
    "log_kb": 0.5,             # CloudWatch log KB per request
}


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def load_pricing(region: str) -> dict[str, Any]:
    path = Path(pricing_dir()) / f"aws_{region}.json"
    fallback = Path(pricing_dir()) / "aws_us-east-1.json"
    data = json.loads((path if path.exists() else fallback).read_text())
    if not path.exists():
        data["note"] = f"No price file for {region}; using us-east-1 prices. " + data.get("note", "")
    return data


class CostAgent:
    # ------------------------------------------------------------- forecast
    def forecast(self, plan: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
        pricing = load_pricing(spec["region"])
        usage = spec.get("usage_profile", {})
        req_month = usage.get("requests_per_day", 1000) * 30
        dur_s = usage.get("avg_duration_ms", 120) / 1000
        items, unpriced, fixed_hourly = [], [], 0.0

        for r in plan["resources"]:
            if "delete" in r["actions"] and "create" not in r["actions"]:
                continue
            t, attrs = r["type"], r.get("attributes", {})
            if t in pricing["fixed_hourly"]:
                p = pricing["fixed_hourly"][t]
                rate = p.get("prices", {}).get(attrs.get(p.get("by_attr", "")), p["default"])
                fixed_hourly += rate
                items.append({"resource": r["address"], "basis": f"${rate}/hour always-on", "monthly": rate * HOURS_PER_MONTH})
            elif t in pricing["usage"]:
                p = pricing["usage"][t]
                if t == "aws_lambda_function":
                    gb = attrs.get("memory_size", 128) / 1024
                    m = req_month / 1e6 * p["per_million_requests"] + req_month * dur_s * gb * p["per_gb_second"]
                    basis = f"{req_month:,} invocations x {dur_s*1000:.0f}ms @ {gb*1024:.0f}MB"
                elif t == "aws_apigatewayv2_api":
                    m = req_month / 1e6 * p["per_million_requests"]
                    basis = f"{req_month:,} HTTP API requests"
                elif t == "aws_dynamodb_table":
                    w, rd = req_month * USAGE_PER_REQUEST["ddb_writes"], req_month * USAGE_PER_REQUEST["ddb_reads"]
                    m = w / 1e6 * p["per_million_writes"] + rd / 1e6 * p["per_million_reads"] + 0.01 * p["per_gb_month"]
                    basis = f"on-demand: {w:,.0f} writes, {rd:,.0f} read units"
                elif t == "aws_cloudwatch_log_group":
                    gb = req_month * USAGE_PER_REQUEST["log_kb"] / 1024 / 1024
                    m = gb * (p["per_gb_ingested"] + p["per_gb_month"])
                    basis = f"{gb:.3f} GB logs"
                else:
                    m = 0.01 * p.get("per_gb_month", 0)
                    basis = "storage estimate"
                items.append({"resource": r["address"], "basis": basis, "monthly": m})
            elif t not in pricing["free"]:
                unpriced.append(r["address"])

        monthly = sum(i["monthly"] for i in items)
        ttl = spec.get("ttl_hours", 4)
        fc = {
            "currency": pricing["currency"], "region": spec["region"], "price_source": pricing.get("as_of"),
            "line_items": [{**i, "monthly": round(i["monthly"], 4)} for i in items],
            "unpriced": unpriced,
            "fixed_hourly": round(fixed_hourly, 4),
            "monthly_total": round(monthly, 4),
            "daily": round(monthly / 30, 4),
            "hourly": round(monthly / HOURS_PER_MONTH, 5),
            "ttl_hours": ttl,
            "ttl_cost": round(monthly / HOURS_PER_MONTH * ttl, 4),
            "idle_monthly": round(fixed_hourly * HOURS_PER_MONTH, 4),
            "assumptions": {"requests_per_day": usage.get("requests_per_day", 1000),
                            "avg_duration_ms": usage.get("avg_duration_ms", 120),
                            "free_tier": "ignored", "note": pricing.get("note", "")},
        }
        say(AGENT, f"Forecast: ${fc['monthly_total']:.2f}/month at {fc['assumptions']['requests_per_day']:,} req/day, "
                   f"${fc['idle_monthly']:.2f}/month idle, ${fc['ttl_cost']:.4f} for the {ttl}h TTL"
                   + (f"; UNPRICED: {unpriced}" if unpriced else ""))
        return fc

    # ------------------------------------------------------------- current
    def current(self, run: dict[str, Any], session=None, use_billing: bool = False) -> dict[str, Any]:
        deployed = run.get("deployed_at")
        if not deployed:
            return {"status": run.get("status"), "note": "Run was never deployed - no cost incurred."}
        end = _parse_ts(run["destroyed_at"]) if run.get("destroyed_at") else datetime.now(timezone.utc)
        elapsed_h = max((end - _parse_ts(deployed)).total_seconds() / 3600, 0)
        fc = run.get("forecast", {})
        out: dict[str, Any] = {"run_id": run["run_id"], "status": run.get("status"), "elapsed_hours": round(elapsed_h, 2),
                               "forecast_monthly": fc.get("monthly_total"), "currency": fc.get("currency", "USD")}

        if run.get("provider") == "local" or session is None:
            out["actual_to_date"] = 0.0
            out["aws_equivalent_to_date"] = round(fc.get("hourly", 0) * elapsed_h, 5)
            out["note"] = "Local emulator: nothing billed. Shown: what the same run would cost on AWS at the forecast load."
            return out

        fixed = fc.get("fixed_hourly", 0) * elapsed_h
        metered, usage = self._metered(run, session, _parse_ts(deployed), end)
        out.update(usage=usage, fixed_to_date=round(fixed, 5), metered_to_date=round(metered, 5),
                   estimate_to_date=round(fixed + metered, 5),
                   projected_monthly_at_current_rate=round((fixed + metered) / max(elapsed_h, 1 / 60) * HOURS_PER_MONTH, 4))
        if use_billing:
            out["billing"] = self._cost_explorer(run, session, _parse_ts(deployed), end)
        return out

    def _metered(self, run, session, start: datetime, end: datetime) -> tuple[float, dict[str, float]]:
        pricing = load_pricing(run["spec"]["region"])["usage"]
        cw = session.client("cloudwatch", region_name=run["spec"]["region"])
        o = run.get("outputs", {})
        period = 3600 if (end - start) < timedelta(days=50) else 86400

        def total(ns, metric, dims, stat="Sum"):
            pts = cw.get_metric_statistics(Namespace=ns, MetricName=metric, Dimensions=dims, StartTime=start,
                                           EndTime=end + timedelta(minutes=5), Period=period, Statistics=[stat])
            return sum(p[stat] for p in pts.get("Datapoints", []))

        u = {}
        try:
            u["lambda_invocations"] = total("AWS/Lambda", "Invocations", [{"Name": "FunctionName", "Value": o["function_name"]}])
            u["lambda_duration_ms"] = total("AWS/Lambda", "Duration", [{"Name": "FunctionName", "Value": o["function_name"]}])
            u["api_requests"] = total("AWS/ApiGateway", "Count", [{"Name": "ApiId", "Value": o["api_id"]}])
            u["ddb_wcu"] = total("AWS/DynamoDB", "ConsumedWriteCapacityUnits", [{"Name": "TableName", "Value": o["table_name"]}])
            u["ddb_rcu"] = total("AWS/DynamoDB", "ConsumedReadCapacityUnits", [{"Name": "TableName", "Value": o["table_name"]}])
        except Exception as e:
            say(AGENT, f"CloudWatch metrics unavailable: {e}")
            return 0.0, u
        mem_gb = 128 / 1024
        cost = (u["lambda_invocations"] / 1e6 * pricing["aws_lambda_function"]["per_million_requests"]
                + u["lambda_duration_ms"] / 1000 * mem_gb * pricing["aws_lambda_function"]["per_gb_second"]
                + u["api_requests"] / 1e6 * pricing["aws_apigatewayv2_api"]["per_million_requests"]
                + u["ddb_wcu"] / 1e6 * pricing["aws_dynamodb_table"]["per_million_writes"]
                + u["ddb_rcu"] / 1e6 * pricing["aws_dynamodb_table"]["per_million_reads"])
        return cost, {k: round(v, 2) for k, v in u.items()}

    def _cost_explorer(self, run, session, start: datetime, end: datetime) -> dict[str, Any]:
        """Real billed cost. Costs $0.01 per call, lags ~24h, and needs the iat:run-id
        cost allocation tag activated in the Billing console."""
        try:
            ce = session.client("ce", region_name="us-east-1")
            resp = ce.get_cost_and_usage(
                TimePeriod={"Start": start.strftime("%Y-%m-%d"), "End": (end + timedelta(days=1)).strftime("%Y-%m-%d")},
                Granularity="DAILY", Metrics=["UnblendedCost"],
                Filter={"Tags": {"Key": "iat:run-id", "Values": [run["run_id"]]}},
            )
            amt = sum(float(d["Total"]["UnblendedCost"]["Amount"]) for d in resp["ResultsByTime"])
            return {"billed_to_date": round(amt, 4), "note": "Cost Explorer lags ~24h; tag must be activated for cost allocation."}
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------- cleanup helpers
    def orphans(self, run: dict[str, Any], session=None) -> list[dict[str, Any]]:
        if run.get("provider") == "local" or session is None:
            return []
        try:
            tagging = session.client("resourcegroupstaggingapi", region_name=run["spec"]["region"])
            found = []
            for page in tagging.get_paginator("get_resources").paginate(
                    TagFilters=[{"Key": "iat:run-id", "Values": [run["run_id"]]}]):
                found += [{"arn": r["ResourceARN"]} for r in page.get("ResourceTagMappingList", [])]
        except Exception as e:
            say(AGENT, f"Orphan sweep failed: {e}")
            return [{"error": str(e)}]
        if found:
            say(AGENT, f"{len(found)} resource(s) still tagged with this run (the tagging index can lag a few "
                       f"minutes after delete; re-check with `iat cost {run['run_id']}`): " + ", ".join(f["arn"] for f in found))
        else:
            say(AGENT, "Orphan sweep: no tagged resources left.")
        return found

    def expired(self, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        out = []
        for r in runs:
            if r.get("status") not in ("deployed", "e2e_failed") or not r.get("deployed_at"):
                continue
            ttl = r.get("spec", {}).get("ttl_hours", 4)
            if _parse_ts(r["deployed_at"]) + timedelta(hours=ttl) <= now:
                out.append(r)
        return out

    @staticmethod
    def render_forecast(fc: dict[str, Any]) -> str:
        lines = [f"  {i['resource']:<48} ${i['monthly']:>9.4f}/mo  ({i['basis']})" for i in fc["line_items"]]
        lines.append(f"  {'TOTAL':<48} ${fc['monthly_total']:>9.4f}/mo   idle: ${fc['idle_monthly']:.2f}/mo   "
                     f"TTL {fc['ttl_hours']}h: ${fc['ttl_cost']:.4f}")
        if fc["unpriced"]:
            lines.append(f"  UNPRICED (review manually): {', '.join(fc['unpriced'])}")
        return "\n".join(lines)
