"""Write seed_cache/<vertical>.jsonl.gz from a data directory's databases.

    python3 app.py --data /tmp/seed --backfill 14 --no-serve     # or any data dir with history
    python3 tools/build_seed_cache.py /tmp/seed anime manga
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import seedcache  # noqa: E402
from store import Store  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    data = Path(sys.argv[1])
    for vertical in sys.argv[2:]:
        db = data / f"{vertical}.db"
        if not db.exists():
            sys.exit(f"{db} not found")
        n = seedcache.export(Store(db), vertical)
        path = seedcache.cache_path(vertical)
        print(f"{vertical}: {n} items -> {path.relative_to(ROOT)} ({path.stat().st_size / 1024:.0f} KB)")
