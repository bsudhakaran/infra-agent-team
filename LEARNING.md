# Learning notes - Infra Agent Team

## Learning objectives

After building and reading this project you should be able to:

1. Design a multi-agent system as an explicit **LangGraph state machine** with conditional routing,
   bounded retry loops and **human-in-the-loop interrupts**.
2. Decide which parts of an agentic system may be LLM-generated and which must be deterministic
   ("generate the logic, template the infrastructure").
3. Keep secrets out of agent state, checkpoints, prompts and logs while still letting agents act on a cloud.
4. Build a **shift-left QA loop**: the same data-driven cases run pre-deploy on an emulator and
   post-deploy on the live system, with failures fed back to the coding agent.
5. Put **cost governance** in the loop: price the plan before apply, gate on budget, meter after,
   sweep on TTL, and verify cleanup via tags.
6. Make an agentic system **testable offline** (mock LLM, emulator, fake Terraform binary, moto).

## Architecture in one paragraph

A `Team` object holds five specialists sharing one pluggable LLM and one credential vault. The
deploy graph is `credentials → interpret → generate → unit_qa → plan → forecast → approve → apply → e2e_qa → report`,
with two back-edges into `generate` (pre-deploy and post-deploy QA failure) capped by
`IAT_MAX_ATTEMPTS`. `approve` calls `interrupt()`; the CLI resumes with `Command(resume=bool)`.
The destroy graph is `cost_report → approve_destroy → destroy → orphan_sweep → finalize`. Every node
is wrapped to persist non-secret state to `run.json`, which is what lets cost/destroy/sweep work days
later from a new process, while Terraform state sits in the run workspace.

## Key design decisions (and the trade-offs)

| Decision | Why | Trade-off |
|---|---|---|
| Graph orchestration, not a free-form "agents chat to each other" crew | Deploys and destroys are irreversible; the order of operations must be auditable and gates must be impossible to skip | Less emergent behaviour; new behaviour means new edges |
| LLM writes `operations.py`; Terraform comes from a reviewed blueprint | IaC mistakes cost money and security; business logic is where generation adds value and is cheap to test | Fewer app shapes until more blueprints exist |
| Credential agent returns identity, not credentials | State is checkpointed and persisted; anything in it should be treated as public | Every consumer must go through the agent |
| Apply the saved `tfplan` | What the human approved is exactly what runs | Plan must be regenerated if anything changes |
| Same cases pre- and post-deploy | One source of truth; pre-deploy catches logic bugs for $0, post-deploy catches wiring/IAM/API Gateway issues | Emulator fidelity is not 100% (no IAM, no cold starts) |
| CloudWatch metering + optional Cost Explorer | Cost Explorer lags ~24h, costs $0.01/call and needs tag activation; metrics are near real-time and free | Metered figure is an estimate, not the invoice |
| Serverless default blueprint | ~$0 idle cost makes "forgot to destroy" cheap | Cold starts; DynamoDB report query is window-limited |
| Mock LLM as a first-class provider | CI, demos and tests without keys or spend; deterministic | Must be kept in sync with prompt contracts |

## Things to try

* Run `pytest -q tests/test_workflow.py -k feedback` and read `BuggyFirstLLM`: the coder's first
  attempt is wrong, QA's failure message reaches the coder, attempt 2 deploys.
* Add an `aws_nat_gateway` resource to the blueprint and watch the forecast jump and the budget gate stop `--yes`.
* Point `LLM_PROVIDER=ollama` at a local model and compare spec quality.
* Write a `gcp_cloud_run_ui` blueprint (Cloud Run + Firestore) - the Credential agent already handles GCP.
* Swap `MemorySaver` for a SQLite checkpointer and make approvals resumable from another terminal.

## Interview questions (with what a strong answer covers)

**Architecture & orchestration**
1. *Why a LangGraph state machine instead of a supervisor LLM that decides the next agent?* Irreversible
   side effects need deterministic ordering and un-skippable gates; LLM routing is fine for intent
   (the router) but not for "may I apply now"; cost and latency of extra LLM calls; testability of edges.
2. *How do you bound the coding↔QA loop, and what happens at the bound?* Attempt counter in state, routing
   function checks it; pre-deploy exhaustion ends as `qa_failed` with nothing deployed; post-deploy
   exhaustion leaves infra up (status `e2e_failed`) so a human can inspect, with cost/destroy hints.
3. *What does `interrupt()` actually do, and why must the node be idempotent?* It raises, the checkpointer
   saves state, resume re-executes the node from the top with the resume value; side effects before the
   interrupt would run twice.
4. *How would you make this multi-user and resumable across machines?* Durable checkpointer (Postgres),
   remote Terraform state with locking, approvals via Slack/web callback that resumes by thread id, a
   queue for runs, per-user connection scopes.

**Security**
5. *Walk through every place a secret could leak and how each is closed.* YAML (references only, literal
   check), graph state/checkpoints (identity only), prompts (never included), logs/Terraform output
   (registered-value + pattern redaction), subprocess env (scrubbed, single connection), run.json (asserted in tests).
6. *How do you prevent a prompt-injected request from deploying into production?* Account allow-list
   checked after `GetCallerIdentity`, role with least privilege and region condition, blueprint-only infra,
   human gate with plan summary, hard cost limit.
7. *Is the AST policy on generated code a sandbox?* No - it is a cheap first filter. Real isolation comes from
   Lambda's execution role (only PutItem/Query on one table) and running tests in an emulator; a stronger
   setup executes unit tests in a container/Firecracker sandbox.

**QA**
8. *Why run the same cases against an emulator and the live system? What can each miss?* Emulator: fast,
   free, deterministic, catches logic bugs; misses IAM, throttling, payload-format and DNS issues. Live:
   catches integration/config issues; slower, costs money, can be flaky (hence health-wait with backoff).
9. *An LLM-designed test fails. Is the code or the test wrong? How does the system decide?* It doesn't fully:
   spec examples and baseline cases are the oracle; LLM cases are regenerated with feedback; a stronger design
   uses differential checks (two independent implementations) or marks LLM cases as advisory.
10. *How would you add performance and security testing?* k6/Locust stage against the live URL with an SLO gate,
    OWASP ZAP baseline scan, dependency/IaC scanning (checkov/tfsec) before plan.

**Cost**
11. *Why price the plan rather than the request?* The plan is ground truth for what will exist, including
    attributes (instance type, memory); unknown types are surfaced as "unpriced" rather than assumed free.
12. *How do you know cleanup really worked?* Terraform state empty plus a tag-based sweep via the Resource
    Groups Tagging API; note eventual consistency; resources created outside Terraform (e.g. auto-created log
    groups) are prevented by managing them explicitly.
13. *Forecast says $0.08/month - what makes that number wrong in real life?* Free tier ignored, traffic
    assumptions, data transfer, NAT/egress if a VPC is added, log volume from errors, API abuse (mitigated by throttling).

**Engineering**
14. *How do you test an agentic system without paying for LLM or cloud calls?* Mock LLM keyed by task, local
    emulator, fake `terraform` binary on PATH, moto for AWS APIs, assertions on side-channel files (env seen by terraform).
15. *What would you change to support arbitrary user infrastructure, not just blueprints?* LLM-generated HCL behind
    `terraform validate`, policy-as-code (OPA/Conftest, checkov), plan-diff review by a separate critic agent,
    stricter budget gates, and a sandbox account.
