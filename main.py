#!/usr/bin/env python3
"""命令行入口。

  python main.py audit            边界自检：证明本系统不具备下单能力
  python main.py check-config     校验配置（不发邮件、不联网）
  python main.py test-email       发送一封测试邮件，验证 Gmail 链路
  python main.py once             单次轮询并打印账户与持仓风险快照
  python main.py scan             扫描全市场资金费 / 基差机会并打印机会榜
  python main.py cross            扫描跨交易所（币安 / Bybit / OKX）资金费价差
  python main.py directional      打印截面低波动调仓信号
  python main.py verify           对账此前发出信号的实际结果
  python main.py digest           立即发送当前待发的每日摘要
  python main.py run              启动守护循环（生产用法）

通用参数：--config / --env / --dry-run / --verbose

本系统只做信号提醒，不代下单。是否开仓、开多少、何时平仓由人决定。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from src.binance_client import BinanceError, BinanceFuturesClient
from src.config import ConfigError, load_config
from src.engine import AlertService, format_scan, format_snapshot
from src.notifier import Notifier
from src.store import Store
from src.timeutil import resolve_tz

log = logging.getLogger("binance-perp-alert")


def setup_logging(level: str, verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def build_client(cfg):
    return BinanceFuturesClient(
        base_url=cfg.binance_base_url,
        api_key=cfg.api_key,
        api_secret=cfg.api_secret,
        recv_window=cfg.recv_window,
        timeout=cfg.timeout_seconds,
    )


def build_service(cfg):
    from src.cross_exchange import PublicExchangeClient

    tz = resolve_tz(cfg.timezone)
    store = Store(cfg.db_path)
    client = build_client(cfg)
    notifier = Notifier(cfg, store, tz)
    cross_client = PublicExchangeClient(timeout=cfg.timeout_seconds)
    service = AlertService(cfg, tz, store, client, notifier, cross_client)
    return service, store, tz


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="币安永续合约信号提醒（只提醒，不代下单）")
    parser.add_argument(
        "command",
        choices=["run", "once", "scan", "cross", "directional", "verify",
                 "test-email", "digest", "check-config", "audit"],
        help="要执行的动作",
    )
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--env", default=".env", help="环境变量文件路径")
    parser.add_argument("--dry-run", action="store_true", help="不真正发送邮件")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    args = parser.parse_args(argv)

    # audit 刻意放在 load_config 之前，且不读取任何配置：
    # 边界自检必须能在配置损坏、依赖缺失、无网络的环境下运行。
    # 一个需要先信任配置才能运行的检查，不能用来证明边界。
    if args.command == "audit":
        from src.scope_guard import render_report, scan

        report = scan()
        print(render_report(report))
        return 0 if report.passed else 1

    # scan / cross / check-config 只依赖公开接口或本地配置，不要求填密钥。
    # test-email / digest 只走投递通道与本地数据库，同样不读账户。
    needs_credentials = args.command not in (
        "scan", "cross", "directional", "verify", "check-config",
        "test-email", "digest",
    )
    try:
        cfg = load_config(
            args.config, args.env, dry_run=args.dry_run,
            require_credentials=needs_credentials,
        )
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg.log_level, args.verbose)

    if args.command == "check-config":
        missing = []
        if not cfg.api_key:
            missing.append("BINANCE_API_KEY")
        if not cfg.api_secret:
            missing.append("BINANCE_API_SECRET")
        if not cfg.apprise_url:
            missing.append("APPRISE_URL")

        print("配置结构校验通过。")
        print(f"  时区          : {cfg.timezone}")
        print(f"  数据库        : {cfg.db_path}")
        print(f"  持仓轮询      : {cfg.positions_seconds}s")
        print(f"  机会扫描      : {cfg.opportunity_seconds}s")
        print(f"  跨所扫描      : {cfg.cross_seconds}s"
              + (f"（{', '.join(cfg.cross_exchanges)}）" if cfg.cross_enabled else "（已关闭）"))
        print(f"  方向性扫描    : {cfg.directional_seconds}s"
              f"（截面低波动，窗口 {cfg.thresholds.get('xs_lookback_days', 30):.0f} 天）")
        print(f"  投递通道      : "
              + (", ".join(f"{c.name}({'/'.join(c.severities)})" for c in cfg.notify_channels)
                 or "（未配置 URL，将回退到 APPRISE_URL）"))
        print(f"  资金费机会规则: "
              f"{'启用' if cfg.rule('funding_opportunity').enabled else '已关闭（回测为负期望）'}")
        print(f"  静默期        : {cfg.quiet_start.strftime('%H:%M')} - "
              f"{cfg.quiet_end.strftime('%H:%M')}")
        print(f"  每日摘要      : {cfg.digest_at.strftime('%H:%M')}")
        print(f"  每小时上限    : {cfg.max_per_hour} 封")
        print(f"  已配置标的    : {', '.join(cfg.symbols) or '（无）'}")
        print(f"  已配置规则    : {len(cfg.rules)} 条")
        print(f"  API Key       : {'已提供' if cfg.api_key else '缺失'}")
        print(f"  投递地址      : "
              + ("已提供" if (cfg.apprise_url or cfg.notify_channels) else "缺失"))

        if missing:
            print()
            print("还缺以下 .env 配置，补齐后才能运行 once / run / test-email：")
            for name in missing:
                print(f"  - {name}")
            print()
            print("提示：python main.py scan 现在就可以用（只读公开行情，不需要密钥）。")
            return 1
        print()
        print("全部就绪。建议先跑：python main.py test-email")
        return 0

    # scan / cross / directional 只读公开行情并展示结果，不涉及账户、不发送任何告警，
    # 因此既不需要密钥，也不需要初始化数据库与投递通道。
    if args.command in ("scan", "cross", "directional", "verify"):
        from src.cross_exchange import (
            ExchangeError,
            PublicExchangeClient,
            binance_quotes,
            build_cross_opportunities,
        )
        from src.engine import format_cross_scan
        from src.opportunity_engine import scan_opportunities

        client = build_client(cfg)
        try:
            premium_raw = client.premium_index()
            ticker_raw = client.ticker_24hr()
        except BinanceError as exc:
            print(f"[失败] 无法获取币安公开行情：{exc}", file=sys.stderr)
            return 1

        if args.command == "scan":
            opportunities = scan_opportunities(premium_raw, ticker_raw, cfg)
            print(format_scan(opportunities, cfg))
            return 0

        if args.command == "verify":
            from src.forward_check import render_reconciliation, verify_pending
            from src.store import Store

            store = Store(cfg.db_path)
            try:
                stats = verify_pending(store, cfg, client)
                if stats["verified"] or stats["failed"]:
                    print(f"本轮核对：成功 {stats['verified']} 条，"
                          f"失败 {stats['failed']} 条，"
                          f"超出单次上限未处理 {stats['skipped']} 条。")
                print()
                print(render_reconciliation(store))
            finally:
                store.close()
            return 0

        if args.command == "directional":
            from src.directional_engine import (
                build_signal,
                build_symbol_vol,
                evidence_block,
                format_signal_table,
                parse_klines,
            )

            min_volume = cfg.thresholds.get("min_volume_usdt_24h", 50_000_000.0)
            top_n = int(cfg.thresholds.get("xs_scan_top_n", 60))
            lookback = int(cfg.thresholds.get("xs_lookback_days", 30))
            k_long = int(cfg.thresholds.get("xs_k_long", 5))
            k_short = int(cfg.thresholds.get("xs_k_short", 5))

            pairs: list[tuple[str, float]] = []
            for item in ticker_raw:
                symbol = str(item.get("symbol", "")).upper()
                if not symbol.endswith("USDT"):
                    continue
                try:
                    volume = float(item.get("quoteVolume") or 0.0)
                except (TypeError, ValueError):
                    continue
                if volume >= min_volume:
                    pairs.append((symbol, volume))
            pairs.sort(key=lambda kv: kv[1], reverse=True)
            pairs = pairs[:top_n]

            print(f"从 {len(pairs)} 个流动性达标的合约中拉取日线，计算 {lookback} 天已实现波动率…")
            vols = []
            failed = 0
            for symbol, volume in pairs:
                try:
                    raw = client.klines(symbol, "1d", lookback + 2)
                except BinanceError:
                    failed += 1
                    continue
                snap = build_symbol_vol(symbol, parse_klines(raw), volume, lookback)
                if snap is not None:
                    vols.append(snap)
            if failed:
                print(f"（{failed} 个标的的日线拉取失败，已跳过）")

            if len(vols) < max(int(cfg.thresholds.get("xs_min_symbols", 20)), k_long + k_short):
                print(f"只有 {len(vols)} 个标的可用，不足以构建截面组合，本次不出信号。")
                return 1

            signal = build_signal(
                vols, k_long, k_short, lookback,
                datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            )
            print(format_signal_table(signal))
            print(evidence_block())
            return 0

        if not cfg.cross_enabled:
            print("[提示] 跨所扫描已在配置中关闭（cross_exchange.enabled: false）。")
            return 1

        quotes = list(binance_quotes(premium_raw, ticker_raw))
        cross_client = PublicExchangeClient(timeout=cfg.timeout_seconds)
        try:
            if "bybit" in cfg.cross_exchanges:
                quotes.extend(cross_client.bybit_quotes(cfg.bybit_base_url))
            if "okx" in cfg.cross_exchanges:
                quotes.extend(
                    cross_client.okx_quotes(cfg.okx_base_url, cfg.okx_max_symbols)
                )
        except ExchangeError as exc:
            print(f"[失败] 无法获取其他交易所公开行情：{exc}", file=sys.stderr)
            return 1

        opportunities = build_cross_opportunities(quotes, cfg)
        print(f"共获取 {len(quotes)} 条报价。")
        print(format_cross_scan(opportunities, cfg))
        return 0

    try:
        service, store, tz = build_service(cfg)
    except RuntimeError as exc:
        print(f"[启动失败] {exc}", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    try:
        if args.command == "test-email":
            ok = service.notifier.send_test(now)
            print("测试邮件已发送，请检查收件箱（含垃圾邮件目录）。" if ok else "测试邮件发送失败。")
            return 0 if ok else 1

        if args.command == "digest":
            pending = service.notifier.pending()
            if not pending:
                print("当前没有待发告警。")
                return 0
            ok = service.notifier.send_digest(now)
            if ok:
                service.notifier.mark_digest_sent(now)
            print(f"已发送 {len(pending)} 条待发告警。" if ok else "摘要发送失败。")
            return 0 if ok else 1

        if args.command == "once":
            snapshot = service.poll_positions(now)
            if snapshot is None:
                print("[失败] 无法获取账户数据，请检查 API Key、IP 白名单与网络。",
                      file=sys.stderr)
                return 1
            print(format_snapshot(snapshot, tz))
            return 0

        service.run_forever()
        return 0
    except KeyboardInterrupt:
        print("\n已手动停止。")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
