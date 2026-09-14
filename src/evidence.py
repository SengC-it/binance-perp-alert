"""版本化 evidence artifact 的 provenance 约束。

研究数字只有在由当前回测生成器、当前冻结 spec 和明确的数据集共同生成时，
才能进入运行时告警。这个模块只负责 schema/provenance 校验，不计算策略结果。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

EVIDENCE_SCHEMA_VERSION = 2
EVIDENCE_ARTIFACT_TYPE = "xs_lowvol_evidence"
EVIDENCE_GENERATOR = "backtest.cross_sectional.generate_evidence"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stale_evidence(
    *,
    strategy_id: str,
    spec_hash: str,
    reason: str,
    generated_at: str | None = None,
    dataset_id: str | None = None,
    dataset_sha256: str | None = None,
    code_commit_sha: str | None = None,
) -> dict[str, Any]:
    """构造不含研究数字的 stale artifact。"""
    generated_at = generated_at or utc_now_iso()
    provenance = {
        "dataset_id": dataset_id,
        "dataset_sha256": dataset_sha256,
        "code_commit_sha": code_commit_sha,
        "generated_at": generated_at,
        "generator": EVIDENCE_GENERATOR,
    }
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "artifact_type": EVIDENCE_ARTIFACT_TYPE,
        "generator": EVIDENCE_GENERATOR,
        "strategy": "xs_lowvol",
        "strategy_id": strategy_id,
        "variant": "Control",
        "spec_hash": spec_hash,
        "status": "EVIDENCE_STALE",
        "stale_reason": reason,
        "dataset_id": dataset_id,
        "dataset_sha256": dataset_sha256,
        "dataset_fingerprint": {
            "algorithm": "sha256",
            "sha256": dataset_sha256,
        },
        "code_commit_sha": code_commit_sha,
        "generated_at": generated_at,
        "provenance": provenance,
    }


def validate_current_evidence(
    raw: Any,
    *,
    expected_strategy_id: str,
    expected_spec_hash: str,
) -> tuple[bool, str]:
    """验证 artifact 是否具备成为 CURRENT 的最小 provenance。"""
    if not isinstance(raw, dict):
        return False, "artifact top-level 不是 object"
    if raw.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        return False, "schema_version 不匹配"
    if raw.get("evidence_schema_version") != EVIDENCE_SCHEMA_VERSION:
        return False, "evidence_schema_version 不匹配"
    if raw.get("artifact_type") != EVIDENCE_ARTIFACT_TYPE:
        return False, "artifact_type 不匹配"
    if raw.get("generator") != EVIDENCE_GENERATOR:
        return False, "generator 不匹配"
    if raw.get("strategy") != "xs_lowvol":
        return False, "strategy 不匹配"
    if raw.get("strategy_id") != expected_strategy_id:
        return False, "strategy_id 不匹配"
    if raw.get("spec_hash") != expected_spec_hash:
        return False, "spec_hash 不匹配"
    if raw.get("status") != "CURRENT":
        return False, "artifact status 不是 CURRENT"

    dataset_sha256 = raw.get("dataset_sha256")
    if not isinstance(dataset_sha256, str) or not _SHA256.fullmatch(dataset_sha256):
        return False, "dataset_sha256 缺失或格式非法"
    fingerprint = raw.get("dataset_fingerprint")
    if not isinstance(fingerprint, dict):
        return False, "dataset_fingerprint 缺失"
    if fingerprint.get("algorithm") != "sha256" or fingerprint.get("sha256") != dataset_sha256:
        return False, "dataset fingerprint 与 dataset_sha256 不一致"

    code_commit_sha = raw.get("code_commit_sha")
    if not isinstance(code_commit_sha, str) or not _COMMIT_SHA.fullmatch(code_commit_sha):
        return False, "code_commit_sha 缺失或格式非法"
    generated_at = raw.get("generated_at")
    if not isinstance(generated_at, str):
        return False, "generated_at 缺失"
    try:
        datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return False, "generated_at 格式非法"

    provenance = raw.get("provenance")
    if not isinstance(provenance, dict):
        return False, "provenance 缺失"
    expected_provenance = {
        "dataset_id": raw.get("dataset_id"),
        "dataset_sha256": dataset_sha256,
        "code_commit_sha": code_commit_sha,
        "generated_at": generated_at,
        "generator": EVIDENCE_GENERATOR,
    }
    for key, value in expected_provenance.items():
        if provenance.get(key) != value:
            return False, f"provenance.{key} 不一致"
    if not isinstance(raw.get("dataset_id"), str) or not raw["dataset_id"]:
        return False, "dataset_id 缺失"
    return True, "ok"
