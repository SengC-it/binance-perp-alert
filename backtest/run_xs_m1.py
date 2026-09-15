"""Safe M1-A CLI preflight.

Running this command verifies the pre-registration protocol only.  It never
downloads archives, reads a formal dataset, or emits M1 historical results.
"""

from __future__ import annotations

import argparse
import json

from .m1_runner import preflight


def main() -> int:
    parser = argparse.ArgumentParser(description="XS-LOWVOL M1 protocol preflight")
    parser.add_argument(
        "--protocol",
        default="research/m1/XS_LOWVOL_M1_PROTOCOL.yaml",
        help="pre-registered protocol YAML",
    )
    parser.add_argument(
        "--hash",
        default="research/m1/XS_LOWVOL_M1_PROTOCOL.sha256",
        dest="hash_path",
        help="protocol SHA-256 sidecar",
    )
    args = parser.parse_args()
    result = preflight(args.protocol, args.hash_path)
    print("M1-A PROTOCOL READY")
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    print("formal M1 historical backtest: NOT RUN")
    print("formal M1 return/sharpe review: NOT RUN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
