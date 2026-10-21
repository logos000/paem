"""Audit a candidate set into separate AI-reviewed and quarantined artifacts."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evalsuite.reference_audit import audit_set

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--eval", type=Path, required=True)
    p.add_argument("--corpus", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--workers", type=int, default=20)
    p.add_argument("--limit", type=int, default=0)
    a = p.parse_args()
    print(json.dumps(audit_set(a.eval, a.corpus, a.out, workers=a.workers, limit=a.limit), ensure_ascii=False))
