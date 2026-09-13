"""scope_guard 的边界不变量测试。

核心目标：一旦代码库里出现下单/改仓/划转端点、执行类标识符、或非 GET 请求，
测试立刻失败，阻止越界代码进入主干。
"""

from pathlib import Path

import pytest

from src.scope_guard import (
    FORBIDDEN_ENDPOINTS,
    FORBIDDEN_TOKENS,
    Finding,
    ScopeReport,
    default_roots,
    render_report,
    scan,
)


# ---------------------------------------------------------------------------
# 正向：当前代码库通过
# ---------------------------------------------------------------------------


def test_scan_passes_on_current_codebase():
    report = scan()
    assert report.passed, f"越界发现：{report.findings}"
    assert report.files_scanned > 0
    assert report.get_only
    assert report.endpoints_checked == len(FORBIDDEN_ENDPOINTS)
    assert report.tokens_checked == len(FORBIDDEN_TOKENS)


def test_default_roots_all_exist():
    for root in default_roots():
        assert root.exists(), f"默认扫描根目录不存在：{root}"


# ---------------------------------------------------------------------------
# 反向：越界发现能力
# ---------------------------------------------------------------------------


def test_scan_finds_forbidden_endpoint(tmp_path: Path):
    bad = tmp_path / "bad_module.py"
    bad.write_text(
        'resp = session.post("/fapi/v1/order", json=payload)',
        encoding="utf-8",
    )
    report = scan([tmp_path])
    assert not report.passed
    assert any(f.kind == "下单/改仓/划转端点" for f in report.findings)


def test_scan_finds_forbidden_token(tmp_path: Path):
    bad = tmp_path / "bad_module.py"
    bad.write_text("def place_order(symbol, side): pass", encoding="utf-8")
    report = scan([tmp_path])
    assert not report.passed
    assert any(f.kind == "执行类标识符" for f in report.findings)


def test_scan_finds_non_get_http(tmp_path: Path):
    bad = tmp_path / "bad_module.py"
    bad.write_text(
        'requests.post("https://api.example.com/trade")',
        encoding="utf-8",
    )
    report = scan([tmp_path])
    assert not report.passed
    assert any(f.kind == "非 GET 请求" for f in report.findings)


def test_exempt_files_are_skipped(tmp_path: Path):
    """scope_guard.py 本身包含禁用关键词，必须被豁免。"""
    exempt = tmp_path / "scope_guard.py"
    exempt.write_text('"/fapi/v1/order"', encoding="utf-8")
    report = scan([tmp_path])
    assert report.passed
    assert report.files_scanned == 0


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------


def test_render_report_passed():
    report = ScopeReport(files_scanned=10, get_only=True)
    text = render_report(report)
    assert "通过" in text
    assert "不具备下单" in text
    assert "人工决定" in text


def test_render_report_failed():
    report = ScopeReport(
        files_scanned=10,
        get_only=False,
        findings=[Finding("非 GET 请求", "a.py", 5, "POST")],
    )
    text = render_report(report)
    assert "失败" in text
    assert "a.py:5" in text
    assert "POST" in text
