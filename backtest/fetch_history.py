"""拉取并缓存币安公开历史数据（批量文件版）。

只读 `data.binance.vision` 上的公开月度归档，**不需要 API Key，不下任何订单**。
相比行情 API，批量文件的好处是可复现、无限频、且能取到已下架合约的历史。

缓存目录：backtest/cache/
  universe.json          标的池
  {SYMBOL}.json          单个标的的合并数据：
                           perp    [[date, close, quote_volume], ...]
                           spot    [[date, close], ...]
                           funding [[ts_ms, rate], ...]

用法：
  python -m backtest.fetch_history --months 12
  python -m backtest.fetch_history --symbols BTCUSDT,ETHUSDT --months 12
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

log = logging.getLogger("backtest.fetch")

BASE = "https://data.binance.vision/data"
CACHE_DIR = Path(__file__).resolve().parent / "cache"
FUNDING_INTERVALS_PER_DAY = 3  # 币安 U 本位默认每 8 小时结算一次

# 候选标的池：主流 + 中市值，均为 USDT 本位永续。
# 实际能否使用由数据可用性决定（无现货交易对或历史不足的会被自动剔除）。
DEFAULT_UNIVERSE = [
    # 大市值
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "TRXUSDT",
    "BCHUSDT", "ETCUSDT", "XLMUSDT", "ALGOUSDT", "VETUSDT", "FILUSDT",
    "ATOMUSDT", "NEARUSDT", "UNIUSDT", "AAVEUSDT", "MKRUSDT", "CRVUSDT",
    "LDOUSDT", "EOSUSDT", "THETAUSDT", "GRTUSDT", "SANDUSDT", "MANAUSDT",
    "AXSUSDT", "GALAUSDT", "FTMUSDT", "MATICUSDT",
    # 中市值 / 高资金费常客
    "ARBUSDT", "OPUSDT", "SUIUSDT", "TIAUSDT", "INJUSDT", "SEIUSDT",
    "APTUSDT", "RUNEUSDT", "IMXUSDT", "STXUSDT", "PEPEUSDT", "SHIBUSDT",
    "FLOKIUSDT", "BONKUSDT", "WIFUSDT", "ORDIUSDT", "WLDUSDT", "JUPUSDT",
    "PYTHUSDT", "ENAUSDT", "ETHFIUSDT", "RENDERUSDT", "TAOUSDT", "ARUSDT",
    "FETUSDT", "ENSUSDT", "JASMYUSDT", "CFXUSDT", "SUSDT", "POLUSDT",
    "TONUSDT", "ZKUSDT", "STRKUSDT", "ALTUSDT", "PIXELUSDT", "AEVOUSDT",
    "BLURUSDT", "DYDXUSDT", "GMTUSDT", "APEUSDT", "CHZUSDT", "ONEUSDT",
    "ZILUSDT", "IOTAUSDT", "KAVAUSDT", "ROSEUSDT", "ANKRUSDT", "CKBUSDT",
]


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "binance-perp-alert-backtest/0.1"})
    return s


def month_keys(n: int, end: date | None = None) -> list[str]:
    """返回最近 n 个完整自然月的 'YYYY-MM'，按时间升序。"""
    end = end or date.today()
    y, m = end.year, end.month
    # 回退到上一个完整月
    m -= 1
    if m == 0:
        m, y = 12, y - 1
    out: list[str] = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))


# ---------------------------------------------------------------------------
# 文件地址
# ---------------------------------------------------------------------------

def funding_url(symbol: str, month: str) -> str:
    return f"{BASE}/futures/um/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{month}.zip"


def perp_kline_url(symbol: str, month: str, interval: str = "1d") -> str:
    return f"{BASE}/futures/um/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{month}.zip"


def spot_kline_url(symbol: str, month: str, interval: str = "1d") -> str:
    return f"{BASE}/spot/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{month}.zip"


# ---------------------------------------------------------------------------
# 下载与解析
# ---------------------------------------------------------------------------

def fetch_csv(session: requests.Session, url: str, attempts: int = 4) -> str | None:
    """下载并解压单个归档，返回 CSV 文本。404 返回 None（视为不可用）。"""
    last: Exception | None = None
    for i in range(attempts):
        try:
            r = session.get(url, timeout=60)
            if r.status_code == 404:
                return None
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}")
            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                inner = zf.namelist()[0]
                return zf.read(inner).decode("utf-8", "ignore")
        except Exception as exc:  # 网络抖动 / SSL EOF
            last = exc
            time.sleep(0.7 * (i + 1))
    log.warning("下载失败 %s：%s", url.rsplit("/", 1)[-1], last)
    return None


def parse_funding_csv(text: str) -> list[list[Any]]:
    """解析 fundingRate 归档。

    列为 calc_time, funding_interval_hours, last_funding_rate。

    **结算间隔必须保留**：币安会对部分合约把结算间隔从 8 小时调整为 4 小时
    （本数据集 73 个标的里有 21 个不是 8 小时），硬编码 3 次/天会低估这些
    合约的年化资金费收益。

    返回 [[fundingTime_ms, fundingRate, interval_hours], ...]
    """
    out: list[list[Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("calc_time"):
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        try:
            ts = int(parts[0])
            interval = float(parts[1]) if parts[1] else 8.0
            rate = float(parts[2])
        except (TypeError, ValueError):
            continue
        if interval <= 0:
            interval = 8.0
        out.append([ts, rate, interval])
    return out


def parse_kline_csv(text: str, full_ohlc: bool) -> list[list[Any]]:
    """解析 K 线归档。

    列：open_time, open, high, low, close, volume, close_time, quote_volume, ...

    注意现货归档的时间戳是**微秒**、永续是**毫秒**，这里统一按量级判断。

    full_ohlc=True  → [date, open, high, low, close, volume, quote_volume]
    full_ohlc=False → [date, close]
    """
    out: list[list[Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("open_time"):
            continue
        parts = line.split(",")
        if len(parts) < 8:
            continue
        try:
            ts = int(parts[0])
            if ts > 10 ** 14:      # 微秒 -> 毫秒
                ts //= 1000
            day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            if full_ohlc:
                out.append([
                    day,
                    float(parts[1]),   # open
                    float(parts[2]),   # high
                    float(parts[3]),   # low
                    float(parts[4]),   # close
                    float(parts[5]),   # volume（币）
                    float(parts[7]),   # quote_volume（USDT）
                ])
            else:
                out.append([day, float(parts[4])])
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# 单标的抓取
# ---------------------------------------------------------------------------

def _fetch_month(session: requests.Session, symbol: str, month: str) -> dict[str, Any]:
    """抓取一个标的某个月的三种数据。"""
    perp_text = fetch_csv(session, perp_kline_url(symbol, month))
    spot_text = fetch_csv(session, spot_kline_url(symbol, month))
    fund_text = fetch_csv(session, funding_url(symbol, month))
    return {
        "month": month,
        "perp": parse_kline_csv(perp_text, True) if perp_text else [],
        "spot": parse_kline_csv(spot_text, False) if spot_text else [],
        "funding": parse_funding_csv(fund_text) if fund_text else [],
        "has_spot_file": spot_text is not None,
    }


def fetch_symbol(
    session: requests.Session,
    symbol: str,
    months: list[str],
    workers: int = 6,
) -> dict[str, Any] | None:
    """抓取并合并一个标的的全部数据。现货缺失或数据量不足时返回 None。"""
    chunks: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_month, session, symbol, m): m for m in months}
        for fut in as_completed(futures):
            try:
                chunks.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                log.warning("%s %s 抓取异常：%s", symbol, futures[fut], exc)

    perp: list[list[Any]] = []
    spot: list[list[Any]] = []
    funding: list[list[Any]] = []
    spot_months = 0
    for chunk in sorted(chunks, key=lambda c: c["month"]):
        perp.extend(chunk["perp"])
        spot.extend(chunk["spot"])
        funding.extend(chunk["funding"])
        if chunk["has_spot_file"]:
            spot_months += 1

    # 现货必须覆盖全部月份，否则不是真正的 delta 中性标的
    if spot_months < len(months):
        log.info("%-12s 现货数据不完整（%d/%d 月），剔除",
                 symbol, spot_months, len(months))
        return None
    if len(perp) < len(months) * 25:
        log.info("%-12s 永续日线不足（%d 根），剔除", symbol, len(perp))
        return None

    perp.sort(key=lambda r: r[0])
    spot.sort(key=lambda r: r[0])
    funding.sort(key=lambda r: r[0])
    return {"symbol": symbol, "perp": perp, "spot": spot, "funding": funding}


def load_symbol(name: str) -> dict[str, Any] | None:
    path = CACHE_DIR / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_all(symbols: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """读取已缓存的所有标的。"""
    names = list(symbols) if symbols else [p.stem for p in CACHE_DIR.glob("*.json")
                                           if p.stem != "universe"]
    out = []
    for name in sorted(names):
        rec = load_symbol(name)
        if rec:
            out.append(rec)
    return out


def download(
    symbols: list[str],
    months: list[str],
    refresh: bool = False,
    symbol_workers: int = 6,
) -> list[str]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    session = make_session()
    ok: list[str] = []
    for i, symbol in enumerate(symbols, 1):
        path = CACHE_DIR / f"{symbol}.json"
        if path.exists() and not refresh:
            ok.append(symbol)
            log.info("[%d/%d] %-12s 已缓存", i, len(symbols), symbol)
            continue
        t0 = time.time()
        try:
            record = fetch_symbol(session, symbol, months, workers=symbol_workers)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%d/%d] %-12s 失败：%s", i, len(symbols), symbol, exc)
            continue
        if record is None:
            continue
        path.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
        ok.append(symbol)
        log.info("[%d/%d] %-12s 永续 %d 日 / 现货 %d 日 / 资金费 %d 条  (%.0fs)",
                 i, len(symbols), symbol, len(record["perp"]),
                 len(record["spot"]), len(record["funding"]), time.time() - t0)
    return ok


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="拉取币安公开历史数据（批量文件）")
    ap.add_argument("--months", type=int, default=12, help="回溯的完整月数")
    ap.add_argument("--symbols", default="", help="指定标的，逗号分隔")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新下载")
    ap.add_argument("--workers", type=int, default=6, help="单标的的并发月数")
    args = ap.parse_args()

    months = month_keys(args.months)
    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else DEFAULT_UNIVERSE)

    print(f"区间：{months[0]} ~ {months[-1]}（{len(months)} 个完整月）")
    print(f"候选标的：{len(symbols)} 个")

    t0 = time.time()
    ok = download(symbols, months, refresh=args.refresh, symbol_workers=args.workers)
    print(f"\n完成：{len(ok)}/{len(symbols)} 个标的可用，耗时 {time.time() - t0:.0f}s")
    print("可用标的：" + ", ".join(ok))

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / "universe.json").write_text(
        json.dumps({"months": months, "symbols": ok}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
