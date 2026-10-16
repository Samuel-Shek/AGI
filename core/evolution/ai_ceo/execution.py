"""Reviewable engineering job contracts, never an unguarded execution escape.

The Codex CLI has been detected, but an independently privileged, measured
executor has not been installed. Therefore this release can prepare a contract
and report its gates; execute() refuses even if external text asks otherwise.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import re

from .policy import require_capability


@dataclass(frozen=True)
class EngineeringJob:
    task_id: str
    asset_id: str
    source_sha256: str
    baseline_sha256: str
    grant_id: str | None = None
    model: str = "gpt-6-astra"
    reasoning_effort: str = "max"
    workspace_ref: str | None = None
    model_destination: str | None = None
    approved_budget: int | None = None
    isolation_receipt: str | None = None

    def review(self) -> dict:
        for value in (self.source_sha256, self.baseline_sha256):
            if not re.fullmatch(r"[a-f0-9]{64}", value):
                raise ValueError("job must pin source and baseline hashes")
        return {"contract": asdict(self), "adapter_status": "contract_only",
                "launch_allowed": False, "expected_interface": "codex exec JSONL and structured final output",
                "required_gates": ["scoped grant", "approved model transmission and budget",
                                   "OS isolation acceptance", "dedicated worker identity",
                                   "fixed verifier and release adapter"],
                "production_write": False}


class CodexExecutionAdapter:
    def prepare(self, job: EngineeringJob) -> dict:
        return job.review()

    def execute(self, job: EngineeringJob):
        job.review()
        require_capability("worker")


class ProductionAdapter:
    def apply(self, *_args, **_kwargs):
        require_capability("production")


class ModelPlanner:
    def propose(self, *_args, **_kwargs):
        require_capability("model")
