"""Agent 1 - Credential agent.

Holds every cloud connection and is the ONLY component that touches secret material.

* config/clouds.yaml stores connection definitions and *references* to secrets
  (an AWS profile name, an env-var name, a key-file path, a role ARN) - never values.
  A literal secret in the YAML is rejected.
* `connect()` resolves credentials, verifies who we are (sts:GetCallerIdentity etc.),
  enforces an account allow-list, and returns a non-secret identity for the graph state.
* Other agents get credentials only as a short-lived subprocess env (for Terraform) or a
  boto3 Session (for the Cost agent), always through this agent.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import clouds_config_path
from ..log import register_secret, say

AGENT = "credential-agent"

_FORBIDDEN_KEYS = {
    "aws_secret_access_key", "secret_access_key", "aws_session_token", "client_secret",
    "password", "private_key", "api_key", "token", "secret",
}

# Cloud env vars stripped from Terraform's environment so it only sees the chosen connection.
_CLOUD_ENV_PREFIXES = ("AWS_", "GOOGLE_", "CLOUDSDK_", "ARM_", "AZURE_")


class CredentialError(RuntimeError):
    pass


@dataclass
class Connection:
    name: str
    provider: str
    region: str | None = None
    project: str | None = None
    auth: dict[str, Any] = field(default_factory=dict)
    role_arn: str | None = None
    allowed_account_ids: list[str] = field(default_factory=list)


def _scan_for_literal_secrets(node: Any, path: str = "") -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).lower() in _FORBIDDEN_KEYS:
                raise CredentialError(
                    f"clouds.yaml contains a literal secret at '{path}{k}'. Store a reference instead "
                    f"(AWS profile, env var name, key file path, or role_arn)."
                )
            _scan_for_literal_secrets(v, f"{path}{k}.")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _scan_for_literal_secrets(v, f"{path}{i}.")


class CredentialAgent:
    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or clouds_config_path()
        self.default, self.connections = self._load()
        self._sessions: dict[str, Any] = {}   # in-memory only, never serialised

    # ---------------------------------------------------------------- config
    def _load(self) -> tuple[str, dict[str, Connection]]:
        conns = {"local": Connection(name="local", provider="local")}
        default = "local"   # safe default: nothing reaches a real cloud without explicit config
        if self.config_path.exists():
            raw = yaml.safe_load(self.config_path.read_text()) or {}
            _scan_for_literal_secrets(raw)
            for name, c in (raw.get("connections") or {}).items():
                conns[name] = Connection(
                    name=name,
                    provider=str(c.get("provider", "")).lower(),
                    region=c.get("region"),
                    project=c.get("project"),
                    auth=c.get("auth") or {},
                    role_arn=c.get("role_arn"),
                    allowed_account_ids=[str(a) for a in c.get("allowed_account_ids") or []],
                )
            default = raw.get("default", default)
        elif os.environ.get("AWS_PROFILE") or os.environ.get("AWS_ACCESS_KEY_ID"):
            conns["aws-env"] = Connection(name="aws-env", provider="aws",
                                          region=os.environ.get("AWS_REGION", "us-east-1"),
                                          auth={"type": "env"})
        return default, conns

    def list_connections(self) -> list[dict[str, Any]]:
        return [{"name": c.name, "provider": c.provider, "region": c.region, "project": c.project,
                 "auth": c.auth.get("type", "n/a"), "default": c.name == self.default}
                for c in self.connections.values()]

    def get(self, name: str | None) -> Connection:
        name = name or self.default
        if name not in self.connections:
            raise CredentialError(f"Unknown connection '{name}'. Known: {', '.join(self.connections)}")
        return self.connections[name]

    # ---------------------------------------------------------------- connect
    def connect(self, name: str | None = None, run_id: str = "adhoc") -> dict[str, Any]:
        conn = self.get(name)
        handler = getattr(self, f"_connect_{conn.provider}", None)
        if handler is None:
            raise CredentialError(f"Provider '{conn.provider}' not supported (aws | gcp | azure | local)")
        identity = handler(conn, run_id)
        identity.update(connection=conn.name, provider=conn.provider)
        say(AGENT, f"Connected '{conn.name}' -> {json.dumps({k: v for k, v in identity.items() if k != 'connection'})}")
        return identity

    def _connect_local(self, conn: Connection, run_id: str) -> dict[str, Any]:
        return {"account_id": "local", "region": "local", "principal": "local-emulator"}

    def _connect_aws(self, conn: Connection, run_id: str) -> dict[str, Any]:
        try:
            import boto3
            from botocore.exceptions import BotoCoreError, ClientError
        except ImportError as e:
            raise CredentialError("boto3 is required for AWS connections") from e

        auth_type = conn.auth.get("type", "profile")
        region = conn.region or "us-east-1"
        try:
            if auth_type == "profile":
                session = boto3.Session(profile_name=conn.auth.get("profile", "default"), region_name=region)
            elif auth_type == "env":
                session = boto3.Session(region_name=region)
            else:
                raise CredentialError(f"AWS auth type '{auth_type}' not supported (profile | env)")

            if conn.role_arn:   # recommended: base identity can only assume a scoped deployer role
                creds = session.client("sts").assume_role(
                    RoleArn=conn.role_arn, RoleSessionName=f"iat-{run_id}", DurationSeconds=3600,
                )["Credentials"]
                session = boto3.Session(
                    aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                    aws_session_token=creds["SessionToken"], region_name=region,
                )
            ident = session.client("sts").get_caller_identity()
        except (BotoCoreError, ClientError) as e:
            raise CredentialError(f"AWS authentication failed for '{conn.name}': {e}") from e

        account = ident["Account"]
        if conn.allowed_account_ids and account not in conn.allowed_account_ids:
            raise CredentialError(
                f"Account {account} is not in allowed_account_ids for '{conn.name}'. Refusing to continue."
            )
        frozen = session.get_credentials().get_frozen_credentials()
        for v in (frozen.access_key, frozen.secret_key, frozen.token):
            register_secret(v)
        self._sessions[conn.name] = session
        return {"account_id": account, "principal": ident["Arn"], "region": region}

    def _connect_gcp(self, conn: Connection, run_id: str) -> dict[str, Any]:
        auth_type = conn.auth.get("type", "adc")
        principal = "application-default-credentials"
        if auth_type == "service_account_file":
            path = Path(os.path.expanduser(conn.auth.get("credentials_file", "")))
            if not path.is_file():
                raise CredentialError(f"GCP key file not found for '{conn.name}': {path}")
            principal = json.loads(path.read_text()).get("client_email", principal)  # private key not kept
            self._sessions[conn.name] = {"GOOGLE_APPLICATION_CREDENTIALS": str(path)}
        elif auth_type == "adc":
            try:
                import google.auth

                google.auth.default()
            except ImportError:
                say(AGENT, "google-auth not installed; skipping ADC verification (pip install 'infra-agent-team[gcp]')")
            except Exception as e:
                raise CredentialError(f"GCP ADC not available: {e}. Run: gcloud auth application-default login") from e
            self._sessions[conn.name] = {}
        else:
            raise CredentialError(f"GCP auth type '{auth_type}' not supported (adc | service_account_file)")
        if not conn.project:
            raise CredentialError(f"GCP connection '{conn.name}' needs 'project'")
        return {"account_id": conn.project, "principal": principal, "region": conn.region or "us-central1"}

    def _connect_azure(self, conn: Connection, run_id: str) -> dict[str, Any]:
        needed = ["ARM_CLIENT_ID", "ARM_TENANT_ID", "ARM_SUBSCRIPTION_ID"]
        missing = [v for v in needed if not os.environ.get(v)]
        if missing:
            raise CredentialError(f"Azure connection '{conn.name}' missing env vars: {', '.join(missing)}")
        register_secret(os.environ.get("ARM_CLIENT_SECRET"))
        self._sessions[conn.name] = {}
        return {"account_id": os.environ["ARM_SUBSCRIPTION_ID"], "principal": os.environ["ARM_CLIENT_ID"],
                "region": conn.region or "eastus"}

    # ---------------------------------------------------------------- hand-outs
    def _ensure(self, name: str) -> Connection:
        conn = self.get(name)
        if conn.name not in self._sessions and conn.provider != "local":
            self.connect(conn.name)
        return conn

    def subprocess_env(self, name: str) -> dict[str, str]:
        """Environment for a Terraform subprocess, scoped to exactly one connection."""
        conn = self._ensure(name)
        env = {k: v for k, v in os.environ.items() if not k.startswith(_CLOUD_ENV_PREFIXES)}
        env.update(TF_IN_AUTOMATION="1", TF_INPUT="0")
        if conn.provider == "aws":
            session = self._sessions[conn.name]
            c = session.get_credentials().get_frozen_credentials()
            env.update(AWS_ACCESS_KEY_ID=c.access_key, AWS_SECRET_ACCESS_KEY=c.secret_key,
                       AWS_REGION=session.region_name, AWS_DEFAULT_REGION=session.region_name)
            if c.token:
                env["AWS_SESSION_TOKEN"] = c.token
        elif conn.provider == "gcp":
            env.update(self._sessions[conn.name])
            env.update(GOOGLE_PROJECT=conn.project or "", CLOUDSDK_CORE_PROJECT=conn.project or "")
        elif conn.provider == "azure":
            env.update({k: v for k, v in os.environ.items() if k.startswith("ARM_")})
        return env

    def boto3_session(self, name: str):
        conn = self._ensure(name)
        if conn.provider != "aws":
            raise CredentialError(f"'{name}' is not an AWS connection")
        return self._sessions[conn.name]
