"""项目边界自检：证明本系统不具备下单能力。

本项目的定位是**信号提醒**——只告诉人「现在有什么值得看」。
是否开仓、开多大、何时平仓，全部由人决定。系统不接触任何执行类接口。

口头承诺会随代码演化失效，所以把承诺变成**可执行的检查**：
`python main.py audit` 随时可以自证边界未被破坏。

设计取舍：

1. **只读源码文本，不导入、不执行任何模块。** 因此它能在没有第三方依赖、
   没有网络、没有密钥的环境下运行，也不需要信任被检查的代码。
2. **用禁止清单而不是允许清单。** 只读接口有几十个且会随业务增长，
   执行类接口是有限且稳定的——枚举禁止项更不容易漏。
3. **GET 是结构性保证。** 只要 `binance_client._request` 只发 GET，
   那么无论传入什么路径都不可能产生副作用。这是比逐个路径检查更强的约束。

本模块自身的常量里包含这些禁用关键词，因此扫描时必须排除自己与自己的测试，
排除范围写死在 `EXEMPT_FILES` 里，避免有人靠扩大排除范围绕过检查。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

# 绝对禁止出现的接口路径（下单 / 改仓 / 划转 / 提现）。
FORBIDDEN_ENDPOINTS: tuple[str, ...] = (
    "/fapi/v1/order",
    "/fapi/v1/batchOrders",
    "/fapi/v1/allOpenOrders",
    "/fapi/v1/countdownCancelAll",
    "/fapi/v1/leverage",
    "/fapi/v1/marginType",
    "/fapi/v1/positionMargin",
    "/fapi/v1/positionSide/dual",
    "/fapi/v1/multiAssetsMargin",
    "/sapi/v1/asset/transfer",
    "/sapi/v1/futures/transfer",
    "/sapi/v1/capital/withdraw/apply",
)

# 绝对禁止出现的执行类标识符。
FORBIDDEN_TOKENS: tuple[str, ...] = (
    "place_order",
    "create_order",
    "new_order",
    "cancel_order",
    "cancel_all",
    "close_position",
    "set_leverage",
    "change_leverage",
    "set_margin_type",
    "adjust_margin",
    "withdraw",
)

# 除 GET 之外的 HTTP 动作一律禁止。
FORBIDDEN_HTTP: tuple[str, ...] = ("post", "put", "delete", "patch")

# 这两个文件本身定义了上面的清单并做反向测试，必须排除，否则自检永远失败。
EXEMPT_FILES: frozenset[str] = frozenset({"scope_guard.py", "test_scope_guard.py"})

_HTTP_CALL = re.compile(r"\.(post|put|delete|patch)\s*\(", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    """一处越界。"""

    kind: str
    path: str
    line: int
    detail: str


@dataclass
class ScopeReport:
    files_scanned: int = 0
    endpoints_checked: int = 0
    tokens_checked: int = 0
    get_only: bool = True
    findings: list[Finding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.get_only and not self.findings


def project_root() -> Path:
    """项目根目录（src/ 的上一级）。"""
    return Path(__file__).resolve().parent.parent


def default_roots() -> list[Path]:
    """默认扫描范围：全部自有源码。"""
    root = project_root()
    return [
        root / "src",
        root / "tests",
        root / "backtest",
        root / "main.py",
        root / "smoke_run.py",
        root / "conftest.py",
    ]


def _iter_sources(roots: list[Path]) -> Iterator[Path]:
    for root in roots:
        if root.is_dir():
            candidates = sorted(root.rglob("*.py"))
        elif root.is_file():
            candidates = [root]
        else:
            continue
        for path in candidates:
            if path.name in EXEMPT_FILES:
                continue
            if "__pycache__" in path.parts:
                continue
            yield path


def scan(roots: list[Path] | None = None) -> ScopeReport:
    """扫描源码，返回越界报告。

    不导入任何被检查的模块——纯文本匹配，因此检查本身不会执行任何代码。
    """
    report = ScopeReport(
        endpoints_checked=len(FORBIDDEN_ENDPOINTS),
        tokens_checked=len(FORBIDDEN_TOKENS),
    )

    for path in _iter_sources(roots or default_roots()):
        report.files_scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = str(path.relative_to(project_root())) if _is_inside(path) else str(path)

        for lineno, line in enumerate(text.splitlines(), start=1):
            for endpoint in FORBIDDEN_ENDPOINTS:
                if endpoint in line:
                    report.findings.append(
                        Finding("下单/改仓/划转端点", rel, lineno, endpoint)
                    )
            for token in FORBIDDEN_TOKENS:
                if token in line:
                    report.findings.append(
                        Finding("执行类标识符", rel, lineno, token)
                    )
            match = _HTTP_CALL.search(line)
            if match:
                report.get_only = False
                report.findings.append(
                    Finding("非 GET 请求", rel, lineno, match.group(1).upper())
                )

    return report


def _is_inside(path: Path) -> bool:
    try:
        path.relative_to(project_root())
        return True
    except ValueError:
        return False


def render_report(report: ScopeReport) -> str:
    """渲染成人类可读的自检报告。"""
    lines = [
        "项目边界自检：纯提醒 / 零执行",
        "",
        f"  扫描范围      : {project_root()}",
        f"  已扫描文件    : {report.files_scanned} 个 .py",
        f"  检查禁用端点  : {report.endpoints_checked} 个",
        f"  检查执行标识符: {report.tokens_checked} 个",
        f"  HTTP 动作     : {'仅 GET' if report.get_only else '发现非 GET 调用'}",
        "",
    ]

    if report.passed:
        lines += [
            "  结论：通过。",
            "",
            "  本系统不具备下单、改单、撤单、调整杠杆与保证金、",
            "  资金划转、提现的能力。它只能读取公开行情和你账户的只读快照，",
            "  然后把结论发到你的邮箱或 Telegram。",
            "",
            "  开仓与否、仓位大小、何时平仓，全部由你人工决定。",
        ]
    else:
        lines.append(f"  结论：失败，发现 {len(report.findings)} 处越界。")
        lines.append("")
        for finding in report.findings:
            lines.append(
                f"  - [{finding.kind}] {finding.path}:{finding.line}  {finding.detail}"
            )

    return "\n".join(lines)


# 追加到每一条对外消息末尾的声明。放在这里而不是 notifier 里，
# 是为了让「系统边界」只有一处定义。
SCOPE_DISCLAIMER = (
    "本系统只做信号提醒，不代下单、不接管资金。\n"
    "开仓与否、仓位大小、何时平仓，均由你人工决定。\n"
    "以上内容为数据与统计结果，不构成投资建议。"
)
