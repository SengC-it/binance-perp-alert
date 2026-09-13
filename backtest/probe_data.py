"""探测币安历史数据文件格式与可用性（带重试）。"""

import io
import time
import urllib.error
import urllib.request
import zipfile

BASE = "https://data.binance.vision/data"

CANDIDATES = [
    ("funding_um", f"{BASE}/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2026-08.zip"),
    ("funding_um_alt", f"{BASE}/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2025-09.zip"),
    ("perp_1h", f"{BASE}/futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2026-08.zip"),
    ("spot_1h", f"{BASE}/spot/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2026-08.zip"),
    ("spot_1d", f"{BASE}/spot/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2026-08.zip"),
]


def fetch(url: str, attempts: int = 4) -> bytes:
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 backtest-probe"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (i + 1))
    raise last  # type: ignore[misc]


for name, url in CANDIDATES:
    print("=" * 78)
    print(name, url.rsplit("/", 1)[-1])
    print("=" * 78)
    t0 = time.time()
    try:
        blob = fetch(url)
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR: {type(exc).__name__}: {exc}")
        print()
        continue
    print(f"  size = {len(blob)} bytes   ({time.time() - t0:.1f}s)")
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            inner = zf.namelist()[0]
            text = zf.read(inner).decode("utf-8", "ignore")
    except zipfile.BadZipFile:
        text = blob.decode("utf-8", "ignore")
        inner = "(not zip)"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    print(f"  inner = {inner}   rows = {len(lines)}")
    for ln in lines[:4]:
        print("   >", ln[:150])
    print("   ...")
    for ln in lines[-2:]:
        print("   >", ln[:150])
    print()
