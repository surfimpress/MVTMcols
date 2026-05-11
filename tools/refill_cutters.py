#!/usr/bin/env python3
"""Resilient cutter supervisor for the column-cutting campaign.

Maintains up to TARGET_SLOTS concurrent `cut_corpus.py --year YYYY`
streams. The pending-year queue lives on disk at QUEUE_FILE; the
supervisor reads it on startup, writes it back after every launch,
and self-heals if the supervisor itself crashes or the machine reboots.

Failure modes handled:
  - Machine reboot: launchd restarts this script; on startup we pgrep
    for live cutters and only launch what's missing; cutters that survived
    are recognised as occupying their slots.
  - Mid-year SIGKILL of a cutter: re-launching the same year is safe —
    `process_issue.py` skips issues whose `columns/<YYYY>-MM-DD/`
    already exists, so it picks up where it left off.
  - Supervisor crash: launchd KeepAlive restarts; queue is rebuilt from
    QUEUE_FILE; pgrep recovers slot state.
  - Graceful completion: when queue is empty and no cutters are alive,
    writes COMPLETE_FILE marker and exits 0. LaunchAgent's KeepAlive
    is configured (SuccessfulExit=false) to NOT restart on clean exit.

Install once (one-time, manual):
    bash tools/install_launchagent.sh

Manual run (for testing):
    python3 tools/refill_cutters.py
"""
import os, sys, time, subprocess, json, datetime as dt, tempfile

REPO = '/Users/peter/Projects/MVTM'
os.chdir(REPO)

TARGET_SLOTS = 4
POLL_SECS = 30

QUEUE_FILE = 'data/cut_queue.json'
COMPLETE_FILE = 'data/cut_campaign_complete.json'
LOG = '/tmp/refill_supervisor.log'

# Sentinel used to detect cutter processes via pgrep. Matches
# `cut_corpus.py --year YYYY` regardless of full python interpreter
# path.
CUTTER_CMD_TAG = 'cut_corpus.py'


def log(msg):
    line = f'{dt.datetime.now().isoformat(timespec="seconds")}  {msg}'
    print(line, flush=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')


def alive(pid):
    """True if pid is a running process we own."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def discover_running_cutters():
    """Find live `cut_corpus.py --year YYYY` processes belonging to this
    user. Returns dict year(int) -> pid(int). Used on startup so we
    don't double-launch what's already alive after a supervisor restart.

    Uses `ps -axo pid=,command=` rather than `pgrep -a` because macOS
    BSD pgrep does NOT support the -a flag (it silently ignores it and
    returns PIDs only), which previously caused this function to
    return an empty dict and trigger duplicate launches.
    """
    found = {}
    try:
        out = subprocess.check_output(
            ['ps', '-axo', 'pid=,command='], text=True)
    except subprocess.CalledProcessError:
        return found
    for line in out.splitlines():
        line = line.strip()
        if CUTTER_CMD_TAG not in line:
            continue
        # Format: "  PID  /path/to/python cut_corpus.py --year YYYY ..."
        parts = line.split()
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        # Look for "--year NNNN" anywhere in the rest of the args
        for i, tok in enumerate(parts):
            if tok == '--year' and i + 1 < len(parts):
                try:
                    year = int(parts[i + 1])
                    found[year] = pid
                except ValueError:
                    pass
                break
    return found


def read_queue():
    if not os.path.exists(QUEUE_FILE):
        return []
    with open(QUEUE_FILE) as f:
        return json.load(f)


def write_queue(queue):
    """Atomic write to QUEUE_FILE."""
    os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix='cut_queue.', suffix='.tmp', dir=os.path.dirname(QUEUE_FILE))
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(queue, f, indent=2)
        os.replace(tmp, QUEUE_FILE)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def write_complete_marker(launched_years):
    payload = {
        'completed_at': dt.datetime.now().isoformat(timespec='seconds'),
        'years_launched_this_run': launched_years,
    }
    with open(COMPLETE_FILE, 'w') as f:
        json.dump(payload, f, indent=2)


def launch(year):
    """Spawn a year stream detached from this supervisor."""
    log_path = f'/tmp/cut_{year}.log'
    nohup_path = f'/tmp/cut_{year}.nohup'
    p = subprocess.Popen(
        ['nohup', 'python3', 'cut_corpus.py',
         '--year', str(year),
         '--workers', '1',
         '--no-backup-check',
         '--log', log_path],
        stdout=open(nohup_path, 'w'),
        stderr=subprocess.STDOUT,
        start_new_session=True,  # detach so we can exit independently
    )
    return p.pid


def main():
    queue = read_queue()
    slots = discover_running_cutters()
    log(f'START   queue_len={len(queue)} live_cutters={slots} target_slots={TARGET_SLOTS}')

    # If the campaign was already completed by a previous run and
    # nothing's been queued since, there's nothing for launchd to do.
    if not queue and not slots:
        log('IDLE    nothing to do (queue empty, no live cutters) — exiting clean')
        if not os.path.exists(COMPLETE_FILE):
            write_complete_marker([])
        return

    launched_this_run = []

    # Top up to TARGET_SLOTS immediately if room.
    while len(slots) < TARGET_SLOTS and queue:
        year = queue.pop(0)
        # Defensive: don't relaunch a year that's somehow already alive.
        if year in slots:
            continue
        pid = launch(year)
        slots[year] = pid
        launched_this_run.append(year)
        write_queue(queue)
        log(f'LAUNCH  year={year} pid={pid} (startup top-up)  remaining_queue={len(queue)}')

    while slots or queue:
        exited = [y for y, pid in list(slots.items()) if not alive(pid)]
        for y in exited:
            log(f'EXIT    year={y} pid={slots[y]}')
            del slots[y]
            while len(slots) < TARGET_SLOTS and queue:
                ny = queue.pop(0)
                if ny in slots:
                    continue
                npid = launch(ny)
                slots[ny] = npid
                launched_this_run.append(ny)
                write_queue(queue)
                log(f'LAUNCH  year={ny} pid={npid}  remaining_queue={len(queue)}')

        if not slots and not queue:
            break
        time.sleep(POLL_SECS)

    log(f'END     queue drained, all cutters exited cleanly. '
        f'launched_this_run={launched_this_run}')
    write_complete_marker(launched_this_run)


if __name__ == '__main__':
    main()
