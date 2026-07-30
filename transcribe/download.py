"""Download column PNGs from Google Drive for the transcription pipeline.

Columns in the Almonte-columns Drive folder are publicly accessible, so
plain HTTPS fetches work with no OAuth. The drive_id comes from the
file_assets table in mvtm.db.

Downloads are cached locally at:
    transcribe/work/downloads/<YYYY-MM-DD>/p<N>/<filename>

which mirrors the local_path structure in file_assets. If a file already
exists at that path and is non-empty, the network fetch is skipped.

The default delay between successive downloads (DEFAULT_DELAY) is kept at
0.5 s so a full issue (~50 files) takes ~25 s to claim, well below any
automated rate-detection threshold at Google.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import urllib.request

DOWNLOAD_SUBDIR = os.path.join("transcribe", "work", "downloads")
DEFAULT_DELAY = 0.5  # seconds between successive Drive fetches

# Google Drive serves an HTML "can't scan this file for viruses"
# interstitial instead of the file body for larger files. The page embeds
# a form whose hidden inputs give the real download URL's query params.
_VIRUS_WARNING_MARKER = b"Virus scan warning"
_HIDDEN_INPUT_RE = re.compile(
    rb'name="(id|export|confirm|uuid)"\s+value="([^"]*)"')


def _resolve_virus_scan_redirect(html: bytes) -> str | None:
    """Return the real download URL if `html` is a Drive virus-scan page."""
    if _VIRUS_WARNING_MARKER not in html:
        return None
    params = dict(_HIDDEN_INPUT_RE.findall(html))
    if not params:
        return None
    query = "&".join(
        f"{k.decode()}={v.decode()}" for k, v in params.items())
    return f"https://drive.usercontent.google.com/download?{query}"


def local_cache_path(repo_root: str, date_str: str, page: int,
                     filename: str) -> str:
    """Absolute path where a column PNG is cached locally."""
    return os.path.join(repo_root, DOWNLOAD_SUBDIR,
                        date_str, f"p{page}", filename)


def download_column(drive_id: str, dest_path: str,
                    *, delay: float = DEFAULT_DELAY) -> str:
    """Download a Drive file by ID to dest_path and return its SHA-256 hex.

    Skips the network request if dest_path already exists and is non-empty,
    computing the hash from the cached file instead.

    Raises RuntimeError if Drive returns an empty response.
    """
    if os.path.isfile(dest_path) and os.path.getsize(dest_path) > 0:
        return _sha256_file(dest_path)

    url = f"https://drive.google.com/uc?export=download&id={drive_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()

    redirect_url = _resolve_virus_scan_redirect(data)
    if redirect_url is not None:
        req = urllib.request.Request(
            redirect_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()

    if not data:
        raise RuntimeError(
            f"Empty response downloading drive_id={drive_id}")

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(data)

    if delay > 0:
        time.sleep(delay)

    return hashlib.sha256(data).hexdigest()


def remove_issue_downloads(repo_root: str, date_str: str) -> int:
    """Delete all cached column PNGs for one issue; return the file count.

    Removes the per-issue directory tree under
    ``transcribe/work/downloads/<date_str>/``. Safe to call after
    transcription is complete so the Mac Mini's disk stays clear.
    """
    import shutil
    issue_dir = os.path.join(repo_root, DOWNLOAD_SUBDIR, date_str)
    if not os.path.isdir(issue_dir):
        return 0
    count = sum(1 for _, _, files in os.walk(issue_dir) for _ in files)
    shutil.rmtree(issue_dir)
    return count


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
