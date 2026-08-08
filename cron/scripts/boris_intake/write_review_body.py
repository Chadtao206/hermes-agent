#!/usr/bin/env python3
"""Write a review body file without needing a shell.

Exists so terminal_guard can allow exactly two argv[0] values. `gh pr review
--body-file` needs a file on disk; a heredoc or redirect would require shell
metacharacters, which the guard refuses outright.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--text", required=True)
    args = ap.parse_args()

    raw_ws = os.environ.get("PR_REVIEW_RUN_WORKSPACE", "")
    if not raw_ws:
        sys.stderr.write("write_review_body: PR_REVIEW_RUN_WORKSPACE is unset\n")
        return 3
    workspace = Path(raw_ws).resolve()
    out = Path(args.out).resolve()
    if not str(out).startswith(str(workspace) + os.sep):
        sys.stderr.write(f"write_review_body: {out} is outside the run workspace\n")
        return 3
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(args.text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
