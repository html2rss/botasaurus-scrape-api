#!/bin/sh
# botasaurus_requests downloads its native lib at import time from the unauthenticated GitHub
# API (rate limits, timeouts) and leaves truncated files behind. Import with retries; on each
# failure drop partial downloads so the next attempt re-downloads.
set -u
PY="${PYTHON:-python3}"
bin_dir="$("$PY" -c 'import importlib.util as u, pathlib; print(pathlib.Path(u.find_spec("botasaurus_requests").origin).parent / "bin")')" || exit 1

for i in 1 2 3 4 5; do
    "$PY" -c "import botasaurus_requests" && exit 0
    echo "botasaurus_requests import failed (attempt $i); clearing native lib and retrying" >&2
    rm -f "$bin_dir"/hrequests-cgo-* "$bin_dir/temp"
    sleep $((i * 10))
done
exit 1
