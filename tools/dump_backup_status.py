#!/usr/bin/env python3
"""Dump issue_backups → columns/backup_status.json for the viewer.

The viewer needs to know which issues live on Drive so it can render
the cylinder badge on each issue card. The DB row is the source of
truth (md5_verified=1 means the bytes are mirrored and checksummed);
this script projects that into a tiny JSON the viewer fetches
alongside index.json.

Called automatically from tools/backup_issue.sh after a successful
DB UPSERT, so the viewer stays in step with the campaign without
any manual refresh.

Output shape:
    {
      "generated_at": "2026-05-11T23:27:07Z",
      "issues": {
        "1969-04-03": {"remote": "mvtm:", "verified_at": "..."},
        ...
      }
    }

Atomic write (tmp + os.replace) so a half-written file never reaches
the viewer's HTTP cache.
"""
import json, os, sqlite3, sys, tempfile
from datetime import datetime, timezone

REPO = '/Users/peter/Projects/MVTM'
DB = os.path.join(REPO, 'data', 'mvtm.db')
OUT = os.path.join(REPO, 'columns', 'backup_status.json')


def main():
    if not os.path.exists(DB):
        print(f'error: {DB} not found', file=sys.stderr)
        sys.exit(2)

    con = sqlite3.connect(DB)
    cur = con.execute(
        """SELECT year, month, day, remote, verified_at
             FROM issue_backups
             WHERE md5_verified = 1
             ORDER BY year, month, day"""
    )
    issues = {}
    for y, m, d, remote, verified_at in cur:
        key = f'{y:04d}-{m:02d}-{d:02d}'
        issues[key] = {'remote': remote, 'verified_at': verified_at}
    con.close()

    payload = {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'issues': issues,
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='backup_status.', suffix='.tmp',
                               dir=os.path.dirname(OUT))
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(payload, f, indent=1)
        # mkstemp creates the temp file as 0600; the web server needs
        # to be able to read it after os.replace, so widen perms before
        # the rename. 0644 matches what other viewer JSONs use.
        os.chmod(tmp, 0o644)
        os.replace(tmp, OUT)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise

    print(f'wrote {OUT}: {len(issues)} backed-up issue(s)')


if __name__ == '__main__':
    main()
