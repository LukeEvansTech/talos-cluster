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
COPY_LIMIT = 4 * 2**30  # larger databases are checked in place, immutable, so a non-empty -wal is unverified
DENIED = (errno.EACCES, errno.EPERM)
MAGIC = b"SQLite format 3\x00"
DB_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db3")


def looks_like_sqlite(name, head):
    """True for an intact header, and for a damaged one that still shows SQLite's shape.

    Damage can hit the magic itself, so a few corrupted magic bytes, the fixed payload-fraction bytes
    (offsets 21-23) or a zeroed header on a database-named file all still count.
    """
    if head.startswith(MAGIC) or head[21:24] == b"\x40\x20\x20":
        return True
    if len(head) >= 16 and sum(a != b for a, b in zip(head[:16], MAGIC)) <= 3:
        return True
    return name.lower().endswith(DB_SUFFIXES) and bool(head) and not any(head)


def read_tree(root):
    """Read every regular file; return (report, sqlite paths)."""
    report = {"files": 0, "bytes": 0, "denied": [], "eio": []}
    dbs = []

    def walk_error(err):
        if err.errno in DENIED:
            report["denied"].append(os.path.relpath(err.filename, root))
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
                    report["denied"].append(os.path.relpath(path, root))
                else:
                    report["eio"].append(f"{os.path.relpath(path, root)}: {e.strerror}")
                continue
            report["files"] += 1
            report["bytes"] += total
            if looks_like_sqlite(n, head):
                dbs.append(path)
    return report, dbs


def check_db(path):
    """Return ("ok"|"bad"|"unverified", detail) from quick_check."""
    wal = path + "-wal"
    if os.path.getsize(path) > COPY_LIMIT and os.path.exists(wal) and os.path.getsize(wal) > 0:
        return "unverified", "too large to copy and has a non-empty -wal that an immutable read would skip"
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
        return ("ok", "") if rows == [("ok",)] else ("bad", str(rows[:3])[:200])
    except (sqlite3.Error, OSError) as e:  # "not a database" is itself the finding
        return "bad", str(e)
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)


def main():
    """Print the JSON report for /d."""
    report, dbs = read_tree(ROOT)
    report["sqlite"] = len(dbs)
    report["bad"], report["unverified"] = [], []
    for path in dbs:
        verdict, why = check_db(path)
        if verdict != "ok":
            report[verdict].append(f"{os.path.relpath(path, ROOT)}: {why}")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
