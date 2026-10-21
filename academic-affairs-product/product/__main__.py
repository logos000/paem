"""Run the product API: python -m product  (respects PRODUCT_HOST/PRODUCT_PORT)."""

from __future__ import annotations

import os
import sys


def main() -> int:
    import uvicorn

    uvicorn.run(
        "product.app:app",
        host=os.getenv("PRODUCT_HOST", "127.0.0.1"),
        port=int(os.getenv("PRODUCT_PORT", "8770")),
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
