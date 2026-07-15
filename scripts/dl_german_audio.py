#!/usr/bin/env python3
import sys
sys.path.insert(0, "/app")
from downloader import STAGING_DIR
from xdcc_client import xdcc_download


def status(msg, level="info"):
    print(f"[{level}] {msg}", flush=True)


STAGING_DIR.mkdir(parents=True, exist_ok=True)

success, filename = xdcc_download(
    server="irc.abjects.net",
    port=6667,
    channel="#moviegods",
    bot="[MG]-HDTV|US|S|Snx1",
    pack="599",
    output_dir=STAGING_DIR,
    timeout=180,
    status_callback=status,
)
print("RESULT:", "SUCCESS" if success else "FAILED", filename, flush=True)
