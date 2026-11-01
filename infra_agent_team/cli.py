"""Command line entry point: `iat`."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import graph, runstore
from .agents.cost_agent import CostAgent
from .config import DEFAULT_BUDGET_USD
from .log import redact

BANNER = "Infra Agent Team - credential | coding | infra | qa | cost"


def _yes_no(prompt: str) -> bool:
    if not sys.stdin.isatty():
        print(f"{prompt} [non-interactive: answering no; pass --yes to approve]")
        return False
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def cli_approver(payload: dict[str, Any]) -> bool:
    print()
    if payload["type"] == "approve_deploy":
        fc = payload["forecast"]
        print(f"== Approval needed: deploy run {payload['run_id']} to {payload['connection']} "
              f"(account {payload['account']}, {payload['region']})")
        print(f"   Plan: {payload['plan_summary']}")
        print(CostAgent.render_forecast(fc))
        if payload["over_budget"]:
            print(f"   !! Over the ${payload['budget']:.2f}/month budget")
        return _yes_no("Deploy?")
    if payload["type"] == "approve_destroy":
        print(f"== Approval needed: DESTROY run {payload['run_id']} ({payload['connection']})")
        print(f"   App: {payload.get('app_url')}")
        _print_cost(payload.get("cost") or {})
        return _yes_no("Destroy all resources for this run?")
    return _yes_no(json.dumps(payload))


def _print_cost(c: dict[str, Any]) -> None:
    for k, v in c.items():
        if isinstance(v, dict):
            print(f"   {k}:")
            for k2, v2 in v.items():
                print(f"      {k2}: {v2}")
        else:
            print(f"   {k}: {v}")


def _summary(result: dict[str, Any]) -> int:
    status = result.get("status")
    print()
    print(f"== Run {result.get('run_id')} finished: {status}")
    if result.get("error"):
        print(f"   error: {redact(result['error'])}")
    if result.get("outputs", {}).get("app_url") and status in ("deployed", "e2e_failed"):
        print(f"   App URL: {result['outputs']['app_url']}")
    for k in ("unit_results", "e2e_results"):
        if result.get(k):
            r = result[k]
            print(f"   QA {r['phase']}: {r['passed']}/{r['total']} passed")
    if status in ("deployed", "e2e_failed"):
        print(f"   Cost so far:  iat cost {result['run_id']}")
        print(f"   Tear down:    iat destroy {result['run_id']}")
    print(f"   Files: {runstore.run_dir(result['run_id'])}")
    return 0 if status == "deployed" else 1


# ---------------------------------------------------------------- commands
def cmd_deploy(args) -> int:
    team = graph.Team()
    result = graph.deploy(team, args.request, args.connection, cli_approver, auto_approve=args.yes, budget=args.budget)
    if team.credentials.get(result.get("connection")).provider == "local" and result.get("status") == "deployed" and args.serve:
        print(f"\nLocal app running at {result['outputs']['app_url']} - press Ctrl+C to stop.")
        try:
            import time
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    return _summary(result)


def cmd_status(args) -> int:
    runs = [runstore.load(args.run_id)] if args.run_id else runstore.list_runs()
    if not runs:
        print("No runs yet.")
        return 0
    if args.run_id:
        r = runs[0]
        print(json.dumps({k: r.get(k) for k in ("run_id", "status", "request", "connection", "provider", "identity",
                                                 "outputs", "unit_results", "e2e_results", "deployed_at", "destroyed_at",
                                                 "error")}, indent=2))
        return 0
    print(f"{'RUN':<9} {'STATUS':<18} {'CONNECTION':<12} {'DEPLOYED':<21} REQUEST")
    for r in runs:
        print(f"{r['run_id']:<9} {r.get('status',''):<18} {r.get('connection',''):<12} {r.get('deployed_at') or '-':<21} "
              f"{(r.get('request') or '')[:50]}")
    return 0


def cmd_cost(args) -> int:
    team = getattr(args, "team", None) or graph.Team()
    run = runstore.load(args.run_id)
    if run.get("forecast"):
        print(f"== Forecast for {run['run_id']}")
        print(CostAgent.render_forecast(run["forecast"]))
    print(f"== Cost to date")
    current = team.cost.current(run, team.aws_session(run) if run.get("status") != "destroyed" or args.billing else None,
                                use_billing=args.billing)
    _print_cost(current)
    if run.get("status") in ("deployed", "e2e_failed"):
        print(f"\n   Still running. Tear down with: iat destroy {run['run_id']}")
    return 0


def cmd_destroy(args) -> int:
    team = getattr(args, "team", None) or graph.Team()
    result = graph.destroy(team, args.run_id, cli_approver, auto_approve=args.yes)
    print(f"\n== Destroy {result.get('run', {}).get('run_id', args.run_id)}: {result.get('status')}")
    if result.get("cost"):
        _print_cost(result["cost"])
    if result.get("leftovers"):
        print(f"   Leftovers to review: {result['leftovers']}")
    return 0 if result.get("status") in ("destroyed", "closed") else 1


def cmd_sweep(args) -> int:
    team = getattr(args, "team", None) or graph.Team()
    expired = team.cost.expired(runstore.list_runs())
    if not expired:
        print("No runs past their TTL.")
        return 0
    print(f"{len(expired)} run(s) past TTL: {', '.join(r['run_id'] for r in expired)}")
    rc = 0
    for r in expired:
        res = graph.destroy(team, r["run_id"], cli_approver, auto_approve=args.yes)
        rc |= 0 if res.get("status") in ("destroyed", "closed") else 1
    return rc


def cmd_connections(args) -> int:
    from .agents.credential_agent import CredentialAgent

    agent = CredentialAgent()
    for c in agent.list_connections():
        print(f"{'*' if c['default'] else ' '} {c['name']:<14} {c['provider']:<6} region={c['region']} auth={c['auth']}")
    if args.check:
        agent.connect(args.check)
    return 0


def cmd_chat(args) -> int:
    team = graph.Team()
    print(BANNER)
    print("Describe what to deploy, or ask for cost / status / destroy. Ctrl+D to exit.")
    last_run = None
    while True:
        try:
            msg = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not msg:
            continue
        route = team.router.route(msg)
        run_id = route["run_id"] or last_run or "latest"
        ns = argparse.Namespace(connection=args.connection, yes=False, budget=args.budget, serve=False,
                                run_id=run_id, billing=False, request=msg, check=None, team=team)
        try:
            if route["intent"] == "deploy":
                res = graph.deploy(team, msg, args.connection, cli_approver, budget=args.budget)
                last_run = res.get("run_id")
                _summary(res)
            elif route["intent"] == "cost":
                cmd_cost(ns)
            elif route["intent"] == "destroy":
                cmd_destroy(ns)
            elif route["intent"] == "status":
                ns.run_id = None
                cmd_status(ns)
            elif route["intent"] == "sweep":
                cmd_sweep(ns)
            else:
                print("I can deploy an app, show status, report cost, destroy a run, or sweep expired runs.")
        except FileNotFoundError as e:
            print(e)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="iat", description=BANNER)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("deploy", help="generate, test, deploy and verify an app from a request")
    d.add_argument("request")
    d.add_argument("-c", "--connection", help="connection name from config/clouds.yaml (default: its 'default', else local)")
    d.add_argument("-y", "--yes", action="store_true", help="auto-approve if the forecast is within budget")
    d.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD, help="monthly USD budget for auto-approval")
    d.add_argument("--serve", action="store_true", help="local connection: keep the app running after deploy")
    d.set_defaults(fn=cmd_deploy)

    s = sub.add_parser("status", help="list runs or show one")
    s.add_argument("run_id", nargs="?")
    s.set_defaults(fn=cmd_status)

    c = sub.add_parser("cost", help="forecast and cost to date for a run")
    c.add_argument("run_id", nargs="?", default="latest")
    c.add_argument("--billing", action="store_true", help="also query Cost Explorer (USD 0.01/request, ~24h lag)")
    c.set_defaults(fn=cmd_cost)

    x = sub.add_parser("destroy", help="tear down a run and sweep for orphans")
    x.add_argument("run_id", nargs="?", default="latest")
    x.add_argument("-y", "--yes", action="store_true")
    x.set_defaults(fn=cmd_destroy)

    w = sub.add_parser("sweep", help="destroy every run past its TTL")
    w.add_argument("-y", "--yes", action="store_true")
    w.set_defaults(fn=cmd_sweep)

    k = sub.add_parser("connections", help="list cloud connections; --check NAME verifies one")
    k.add_argument("--check", metavar="NAME")
    k.set_defaults(fn=cmd_connections)

    ch = sub.add_parser("chat", help="talk to the team in plain language")
    ch.add_argument("-c", "--connection")
    ch.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD)
    ch.set_defaults(fn=cmd_chat)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except FileNotFoundError as e:
        print(e)
        return 1
