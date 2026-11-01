from datetime import datetime, timedelta, timezone

from infra_agent_team.agents.cost_agent import HOURS_PER_MONTH, CostAgent

SPEC = {"region": "us-east-1", "usage_profile": {"requests_per_day": 1000, "avg_duration_ms": 100}, "ttl_hours": 4}


def res(t, attrs=None, actions=("create",)):
    return {"address": f"{t}.x", "type": t, "name": "x", "actions": list(actions), "attributes": attrs or {}}


def test_serverless_has_no_idle_cost():
    fc = CostAgent().forecast({"resources": [res("aws_lambda_function", {"memory_size": 128}),
                                             res("aws_apigatewayv2_api"), res("aws_iam_role")]}, SPEC)
    assert fc["idle_monthly"] == 0 and 0 < fc["monthly_total"] < 1 and fc["unpriced"] == []


def test_fixed_cost_resources_are_caught():
    fc = CostAgent().forecast({"resources": [res("aws_nat_gateway"), res("aws_instance", {"instance_type": "t3.small"})]}, SPEC)
    assert abs(fc["idle_monthly"] - (0.045 + 0.0208) * HOURS_PER_MONTH) < 0.01


def test_unknown_types_are_flagged_and_deletes_ignored():
    fc = CostAgent().forecast({"resources": [res("aws_mystery_thing"), res("aws_nat_gateway", actions=("delete",))]}, SPEC)
    assert fc["unpriced"] == ["aws_mystery_thing.x"] and fc["monthly_total"] == 0


def test_unknown_region_falls_back_to_us_east_1():
    fc = CostAgent().forecast({"resources": [res("aws_nat_gateway")]}, {**SPEC, "region": "ap-south-1"})
    assert "using us-east-1 prices" in fc["assumptions"]["note"]


def test_expired_runs():
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = [{"run_id": "a", "status": "deployed", "deployed_at": old, "spec": {"ttl_hours": 4}},
            {"run_id": "b", "status": "deployed", "deployed_at": new, "spec": {"ttl_hours": 4}},
            {"run_id": "c", "status": "destroyed", "deployed_at": old, "spec": {"ttl_hours": 4}}]
    assert [r["run_id"] for r in CostAgent().expired(runs)] == ["a"]


def test_never_deployed_costs_nothing():
    assert "never deployed" in CostAgent().current({"run_id": "r", "status": "rejected"})["note"]
