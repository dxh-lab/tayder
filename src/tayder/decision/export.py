"""Read-only export: python -m tayder.decision.export --journal PATH --output PATH."""
import argparse
import json
import sqlite3
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.resolve() == args.journal.resolve():
        raise ValueError("output must differ from journal")
    with sqlite3.connect(args.journal.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM decisions ORDER BY created_at").fetchall()
    result = [{"decision_id": r["decision_id"], "proposal_id": r["proposal_id"],
               "request": json.loads(r["request_json"]),
               "result": json.loads(r["result_json"]) if r["result_json"] else None} for r in rows]
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result


if __name__ == "__main__":
    main()
