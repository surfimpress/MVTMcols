"""
python3 -m transcribe.cleanup YYYY-MM-DD

Remove downloaded source PNGs for the given issue.
Slices are left in place. Prints the count removed.
"""
from __future__ import annotations
import sys
import os

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, os.pardir))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    date_str = sys.argv[1]
    from transcribe.download import remove_issue_downloads
    n = remove_issue_downloads(REPO_ROOT, date_str)
    print(f"Removed {n} downloaded PNGs for {date_str}")
