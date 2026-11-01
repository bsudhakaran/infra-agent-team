# infra-agent-team

A team of LLM agents, built with [LangGraph](https://github.com/langchain-ai/langgraph), that turns a plain-English request into a tested, cost-checked, running cloud app. Each step is guarded, and you can tear everything down again with one command.

```text
$ iat deploy "Deploy a small AWS UI that accepts user inputs, performs some operations and reports"
```

From that one sentence the team:

1. **Connects** to the cloud account you picked and checks which account it is.
2. **Interprets** the request as a structured spec: app name, region, operations, worked examples, expected traffic and time-to-live.
3. **Generates** the app. The infrastructure comes from a reviewed Terraform blueprint. The LLM writes only the business logic, and that code must pass a policy check before anything runs.
4. **Unit-tests** the generated app in a local emulator *before* anything is deployed. If tests fail, the feedback goes back to the coding agent, which tries again.
5. **Plans** the Terraform and **forecasts** the monthly cost.
6. **Asks you to approve** the deploy. Within budget, `--yes` can approve it for you. Over the hard limit, the deploy is blocked.
7. **Applies** the plan, then runs the same tests **end-to-end** against the live URL.
8. Later, it **reports cost to date**, **destroys** the stack, **checks for orphaned resources**, and **sweeps** runs past their TTL.

## Purpose

The project explores how far an agent team can safely take "describe it → it's running → it's gone" for cloud infrastructure. The guardrails matter as much as the generation:

| Concern | How it is handled |
|---|---|
| LLM writes unsafe infrastructure | The infrastructure comes from vetted blueprints. The LLM can only fill in parameters (`terraform.tfvars.json`), not write Terraform. |
| LLM writes unsafe code | `operations.py` goes through an AST policy check: only allow-listed imports, and no `eval`/`exec`/`open`/`__import__`/... |
| Untested code reaches the cloud | QA runs every test case in a local emulator first. Deploy only happens once the tests pass. |
| Surprise bills | The cost forecast feeds a budget gate: above `IAT_BUDGET_USD` a human must approve, and above `IAT_HARD_LIMIT_USD` the deploy is blocked. |
| Forgotten resources | Every resource is tagged `iat:run-id` / `iat:expires-at`. `iat sweep` destroys expired runs, and destroy checks for orphans. |
| Leaked credentials | Only the Credential agent touches secrets. `clouds.yaml` holds *references* (a profile name, role ARN or env var), and literal secrets are rejected. Secrets are redacted from logs and never written to run state. |
| Wrong account | Optionally, the tool assumes a scoped deployer role with 1-hour credentials, and an `allowed_account_ids` allow-list blocks any other account. |

## The agents

| Agent | File | Role |
|---|---|---|
| Credential | [agents/credential_agent.py](infra_agent_team/agents/credential_agent.py) | Loads connections, resolves and verifies credentials, enforces the account allow-list, and hands out scoped env/sessions |
| Coding | [agents/coding_agent.py](infra_agent_team/agents/coding_agent.py) | Turns the request into a spec, builds the workspace from a blueprint, and writes `operations.py` |
| QA | [agents/qa_agent.py](infra_agent_team/agents/qa_agent.py) | Designs test cases (baseline + LLM edge cases) and runs them in the unit phase (emulator) and the e2e phase (live URL) |
| Infra | [agents/infra_agent.py](infra_agent_team/agents/infra_agent.py) | Runs `terraform init/validate/plan/apply/destroy`, or the local emulator for the `local` connection |
| Cost | [agents/cost_agent.py](infra_agent_team/agents/cost_agent.py) | Prices the plan, reports cost to date (CloudWatch, optionally Cost Explorer), finds orphans and expired runs |
| Router | [agents/router.py](infra_agent_team/agents/router.py) | Maps chat messages to deploy / cost / destroy / status / sweep |

They are wired together as two LangGraph state machines in [graph.py](infra_agent_team/graph.py):

```text
Deploy:   credentials -> interpret -> generate -> unit_qa --fail--> generate   (fix loop, max IAT_MAX_ATTEMPTS)
                                                     | pass
                                                    plan -> forecast -> approve (human gate)
                                                                          | yes
                                                    e2e_qa <- apply <-----+
                                       fail -> generate (fix + redeploy)  | pass -> report

Destroy:  cost_report -> approve_destroy (human gate) -> destroy -> orphan_sweep -> finalize
```

## What can it deploy today?

There is one blueprint, **`aws_small_ui`**
([blueprints/aws_small_ui](infra_agent_team/blueprints/aws_small_ui)). It is a small web UI that accepts text input, runs 3–8 operations on it, stores each submission and shows a report. It is built from:

- API Gateway HTTP API → Lambda (Python) → DynamoDB (on-demand)
- An IAM role for the Lambda function, and a CloudWatch log group

It is serverless, so it has no fixed hourly cost. A typical forecast is a few cents a month.

Connections can target `aws`, `gcp`, `azure` or `local`. The credential handling exists for GCP and Azure, but **AWS is the only cloud with a blueprint right now**. The `local` connection deploys to an in-process emulator, so you can run the whole flow with no cloud account.

## Requirements

- Python **3.10+**
- *Optional:* [Terraform](https://developer.hashicorp.com/terraform/install) or OpenTofu on your `PATH`, but only for real cloud deploys
- *Optional:* an LLM provider (Anthropic, OpenAI or a local Ollama). Without one, a deterministic **mock LLM** is used, which is fine for demos, CI and tests.
- *Optional:* an AWS account with a CLI profile, for real deploys

## Installation

```bash
git clone <this-repo-url> infra-agent-team
cd infra-agent-team

python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1

pip install -e ".[dev]"
```

Then add the extra for your LLM provider:

```bash
pip install -e ".[anthropic]"   # Claude via langchain-anthropic
pip install -e ".[openai]"      # OpenAI via langchain-openai
pip install -e ".[ollama]"      # local models via langchain-ollama
pip install -e ".[gcp]"         # google-auth, to verify GCP connections
```

This installs the `iat` command. `python -m infra_agent_team ...` works too.

## Quick start (offline, no cloud, no API key)

With no LLM configured and no `config/clouds.yaml`, the tool uses the mock LLM and the `local` emulator, so nothing leaves your machine:

```bash
iat deploy "Deploy a small AWS UI that accepts inputs and reports" --serve
```

Open the printed `App URL` in a browser, then press Ctrl+C. After that:

```bash
iat status          # list runs
iat cost            # forecast + cost to date for the latest run
iat destroy --yes   # tear it down
```

## Setup

### 1. Choose an LLM

Set these as environment variables. `.env.example` lists them all.

> **Note:** the tool does **not** load `.env` by itself. Export the variables in your shell, or load the file with your own tooling, e.g. `set -a; source .env; set +a` in bash.

| Variable | Values | Notes |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` \| `openai` \| `ollama` \| `mock` | If unset: `anthropic` when `ANTHROPIC_API_KEY` is set, else `openai` when `OPENAI_API_KEY` is set, else `mock` |
| `LLM_MODEL` | model name | Defaults: `claude-sonnet-5-5`, `gpt-4o-mini`, `llama3.1` |
| `LLM_TEMPERATURE` | float | Default `0` |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | key | For the hosted providers |
| `OLLAMA_BASE_URL` | URL | Default `http://localhost:11434` |

```bash
# example (bash)
export LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
```

```powershell
# example (PowerShell)
$env:LLM_PROVIDER = "ollama"
$env:LLM_MODEL = "llama3.1"
```

### 2. Set guardrails (optional)

| Variable | Default | Meaning |
|---|---|---|
| `IAT_BUDGET_USD` | `5` | Monthly forecast above this always needs a human "yes" (also the `--budget` default) |
| `IAT_HARD_LIMIT_USD` | `50` | Monthly forecast above this is blocked outright |
| `IAT_TTL_HOURS` | `4` | Default lifetime of a run before `iat sweep` destroys it |
| `IAT_MAX_ATTEMPTS` | `3` | Coding ↔ QA fix-loop attempts |
| `IAT_HOME` | `./.iat` | Where run workspaces, Terraform state and QA reports go |
| `IAT_CLOUDS_CONFIG` | `./config/clouds.yaml` | Connection file |
| `IAT_PRICING_DIR` | bundled | Directory of `aws_<region>.json` price files |

### 3. Connect a cloud account (for real deploys)

```bash
cp config/clouds.example.yaml config/clouds.yaml
```

Edit `config/clouds.yaml`. It holds connection **definitions and references**, never secret values. The tool refuses to load the file if it finds keys like `secret_access_key`, `password` or `token`.

```yaml
default: aws-dev
connections:
  aws-dev:
    provider: aws
    region: us-east-1
    auth:
      type: profile          # profile (incl. SSO) | env
      profile: iat-dev
    role_arn: arn:aws:iam::123456789012:role/iat-deployer   # recommended
    allowed_account_ids: ["123456789012"]                   # hard guard
```

**Recommended AWS setup:**

1. Create a role, e.g. `iat-deployer`, with the least-privilege policy in [config/iam/deployer-policy.json](config/iam/deployer-policy.json). The policy allows only the serverless resources the blueprint needs, only `iat-*` IAM roles, and read-only cost APIs. It is limited to `us-east-1` by default, so edit `aws:RequestedRegion` if you deploy elsewhere.
2. Give your base identity (the `iat-dev` profile) permission **only** to `sts:AssumeRole` on that role.
3. Check the connection:

```bash
iat connections                 # list connections (* = default)
iat connections --check aws-dev # resolve credentials, assume the role, verify the account
```

Each run assumes the role with 1-hour credentials. Terraform runs with only that connection's credentials, and all other `AWS_*`/`GOOGLE_*`/`ARM_*` variables are stripped from its environment.

`local` is always available. If there is no `clouds.yaml`, it is the default.

## Usage

```bash
# Deploy (asks for approval; --yes auto-approves when the forecast is within budget)
iat deploy "Small UI that counts words, reverses text and sums numbers" -c aws-dev
iat deploy "..." -c aws-dev --yes --budget 2

# Inspect
iat status                 # all runs
iat status r1a2b3c         # one run (JSON)
iat cost r1a2b3c           # forecast + cost to date from CloudWatch usage
iat cost r1a2b3c --billing # also query Cost Explorer (USD 0.01/request, ~24h lag)

# Clean up
iat destroy r1a2b3c        # asks first; reports final cost and any orphans
iat sweep --yes            # destroy every run past its TTL

# Conversational mode (the router agent picks the action)
iat chat -c aws-dev
you> deploy a tiny UI that converts text to uppercase
you> how much has it cost so far?
you> tear it down
```

`run_id` defaults to `latest` for `cost` and `destroy`. Exit code is `0` on success (deployed/destroyed) and `1` otherwise. That makes it scriptable, but in a non-interactive shell an approval prompt is answered "no" unless you pass `--yes`.

### What a run leaves on disk

Each run lives under `.iat/runs/<run_id>/`:

```text
run.json                      # non-secret state: request, spec, plan, forecast, outputs, QA results, status
summary.md                    # human-readable result + next commands
workspace/app/                # handler.py, operations.py (generated), ui.html, app_meta.json
workspace/infra/              # blueprint Terraform + generated terraform.tfvars.json, terraform state, terraform.log
qa/test_design.md             # QA agent's test design
qa/test_cases.json            # cases run in both phases
qa/test_generated.py          # the same cases as a standalone pytest file
qa/report_unit.md / report_e2e.md, results_*.json
```

**Keep `.iat/`** as long as a run is deployed. It holds the Terraform state that `iat destroy` needs. The folder is git-ignored.

## Project layout

```text
infra_agent_team/
  cli.py            # `iat` command
  graph.py          # LangGraph deploy/destroy workflows + human-in-the-loop interrupts
  agents/           # credential, coding, qa, infra, cost, router
  blueprints/       # vetted app + Terraform templates (aws_small_ui)
  pricing/          # price tables used by the cost forecast
  emulator.py       # local API Gateway + Lambda emulator (unit QA and `local` connection)
  terraform.py      # safe terraform/tofu wrapper (plan JSON, outputs, redacted logs)
  llm.py            # pluggable LLM layer; mock_llm.py = deterministic offline model
  runstore.py       # .iat/runs/<id>/run.json persistence
  log.py            # agent logging with secret redaction
config/
  clouds.example.yaml
  iam/deployer-policy.json
tests/              # pytest suite (agents + end-to-end workflow, mock LLM, moto for AWS)
```

## Development

```bash
pip install -e ".[dev]" "moto[sts,resourcegroupstaggingapi,cloudwatch]"
pytest -q

# validate the blueprint (needs terraform)
cd infra_agent_team/blueprints/aws_small_ui/infra
terraform fmt -check && terraform init -backend=false && terraform validate
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs the tests, then an offline end-to-end deploy → cost → destroy with the mock LLM, then validates the blueprint.

### Adding a blueprint

1. Add a folder under `infra_agent_team/blueprints/<name>/` with `app/` and `infra/`. Every resource should carry the `iat:*` tags.
2. Register it in `BLUEPRINTS` in [blueprints/\_\_init\_\_.py](infra_agent_team/blueprints/__init__.py), with its cloud, summary, generated files and supported regions.
3. Add prices for any new billable resource types to `pricing/aws_<region>.json` (or a new provider file). Resources without a price are flagged `unpriced`, and auto-approval is then not allowed.

## Caveats

- This is a sample/learning project. Use a sandbox account, keep the budget low, and run `iat sweep` regularly.
- Cost forecasts use the bundled price tables and an assumed usage profile. They are estimates, not quotes.
- Cost to date comes from CloudWatch metrics. Cost Explorer (`--billing`) data lags by about 24 hours.
- A local deploy is in-process only. It stops when the `iat` process exits, unless you keep it running with `--serve`.


Sample pytest result:
<img width="1205" height="205" alt="image" src="https://github.com/user-attachments/assets/0005ad25-ff5d-4d1e-a06d-22a2f3377ddd" />

Sample Result:
<img width="1543" height="405" alt="image" src="https://github.com/user-attachments/assets/c6e9543b-7efb-4416-b493-b73c11a1f34c" />


