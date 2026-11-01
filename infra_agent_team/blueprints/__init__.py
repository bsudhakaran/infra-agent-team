"""Blueprint registry.

A blueprint is a vetted, parameterised starting point (app skeleton + Terraform). The
Coding agent picks one from the user's request and generates only the parts that are
safe to generate (business logic, UI metadata, tfvars). Add a cloud or app shape by
adding a folder here and an entry below.
"""

from __future__ import annotations

from pathlib import Path

from ..config import BLUEPRINT_DIR

BLUEPRINTS: dict[str, dict] = {
    "aws_small_ui": {
        "cloud": "aws",
        "summary": "Small web UI that accepts user input, runs operations on it, stores each "
                   "submission and shows a report. HTTP API Gateway + Lambda (Python) + DynamoDB. "
                   "Serverless, no fixed hourly cost.",
        "generated_files": ["app/operations.py", "app/app_meta.json", "infra/terraform.tfvars.json"],
        "regions": ["us-east-1", "us-east-2", "us-west-2", "eu-west-1", "eu-central-1", "ap-south-1", "ap-southeast-1"],
    },
}


def path(name: str) -> Path:
    if name not in BLUEPRINTS:
        raise KeyError(f"Unknown blueprint '{name}'. Available: {', '.join(BLUEPRINTS)}")
    return BLUEPRINT_DIR / name


def catalog_text() -> str:
    return "\n".join(f"- {k} (cloud={v['cloud']}): {v['summary']}" for k, v in BLUEPRINTS.items())
