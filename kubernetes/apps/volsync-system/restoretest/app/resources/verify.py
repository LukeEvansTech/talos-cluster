"""Runs inside the verify pod: read every byte under /d and check every SQLite file.

Prints one JSON line for restoretest.py. Reading whole files surfaces EIO from a damaged
filesystem; SQLite is checked on a private copy so a -wal beside it is replayed as the app would.
"""

import errno
import json
import os
import shutil
import sqlite3
import stat

ROOT = "/d"
SCRATCH = "/tmp/db"
COPY_LIMIT = 4 * 2**30  # larger databases are checked in place, immutable, without their -wal
DENIED = (errno.EACCES, errno.EPERM)
MAGIC = b"SQLite format 3\x00"
# A database whose header page was zeroed has no magic left, so these names are checked anyway.
DB_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db3")


def read_tree(root):
    """Read every regular file; return (report, sqlite paths)."""
    report = {"files": 0, "bytes": 0, "denied": 0, "eio": []}
    dbs = []

    def walk_error(err):
        if err.errno in DENIED:
            report["denied"] += 1
        else:
            report["eio"].append(f"{os.path.relpath(err.filename, root)}: {err.strerror}")

    for dirpath, dirs, names in os.walk(root, onerror=walk_error):
        if dirpath == root and "lost+found" in dirs:
            dirs.remove("lost+found")
        for n in names:
            path = os.path.join(dirpath, n)
            try:
                # lstat, not isfile(): isfile() swallows EIO and would skip a damaged inode silently.
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    continue
                with open(path, "rb") as fh:
                    head = fh.read(100)
                    total = len(head)
                    while chunk := fh.read(1 << 20):
                        total += len(chunk)
            except OSError as e:
                if e.errno in DENIED:
                    report["denied"] += 1
                else:
                    report["eio"].append(f"{os.path.relpath(path, root)}: {e.strerror}")
                continue
            report["files"] += 1
            report["bytes"] += total
            if head.startswith(MAGIC) or (n.lower().endswith(DB_SUFFIXES) and head and not any(head)):
                dbs.append(path)
    return report, dbs


def check_db(path):
    """Return None when quick_check passes, else the failure text."""
    try:
        if os.path.getsize(path) <= COPY_LIMIT:
            os.makedirs(SCRATCH, exist_ok=True)
            target = os.path.join(SCRATCH, "db")
            for suffix in ("", "-wal", "-journal"):
                if os.path.exists(path + suffix):
                    shutil.copyfile(path + suffix, target + suffix)
            con = sqlite3.connect(target)
        else:
            con = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
        rows = con.execute("pragma quick_check").fetchall()
        con.close()
        return None if rows == [("ok",)] else str(rows[:3])[:200]
    except (sqlite3.Error, OSError) as e:  # "not a database" is itself the finding
        return str(e)
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)


def main():
    """Print the JSON report for /d."""
    report, dbs = read_tree(ROOT)
    report["sqlite"] = len(dbs)
    report["bad"] = []
    for path in dbs:
        if (why := check_db(path)) is not None:
            report["bad"].append(f"{os.path.relpath(path, ROOT)}: {why}")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
