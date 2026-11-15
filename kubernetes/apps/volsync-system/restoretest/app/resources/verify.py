"""Runs inside the verify pod: read every byte under /d and check every SQLite file.

Prints one JSON line for restoretest.py. Reading whole files surfaces EIO from a damaged
filesystem; SQLite is checked on a private copy so a -wal beside it is replayed as the app would.
"""

import errno
import json
import os
import re
import shutil
import sqlite3
import stat

ROOT = "/d"
SCRATCH = "/tmp/db"
COPY_LIMIT = 4 * 2**30  # larger databases are checked in place, immutable, so a non-empty -wal is unverified
DENIED = (errno.EACCES, errno.EPERM)
MAGIC = b"SQLite format 3\x00"
DB_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db3")
# An FTS3/4 table built with an app's own C tokenizer (Thunderbird's mozporter) cannot be opened here.
UNKNOWN_TOKENIZER = re.compile(r"(?:unknown|no such) tokenizer: (\S+)")


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


def quick_check(target, uri=False):
    """Return quick_check's rows for a database."""
    con = sqlite3.connect(target, uri=uri)
    try:
        return con.execute("pragma quick_check").fetchall()
    finally:
        con.close()


def drop_tokenizer_tables(target, tokenizer):
    """Remove the FTS tables using tokenizer from a copy's schema; return their names.

    Their shadow tables (_content, _segments, _segdir) stay, so quick_check still reads every page of
    the index data; only the virtual table, which needs the missing tokenizer to open, is skipped.
    """
    con = sqlite3.connect(target)
    try:
        rows = con.execute("select name, sql from sqlite_master where type = 'table'").fetchall()
        names = [
            name
            for name, sql in rows
            if (sql or "").lower().startswith("create virtual table")
            and re.search(rf"tokenize\W+{re.escape(tokenizer.lower())}\b", sql.lower())
        ]
        if names:
            con.execute("pragma writable_schema = on")
            con.executemany("delete from sqlite_master where type = 'table' and name = ?", [(n,) for n in names])
            con.commit()
        return names
    finally:
        con.close()


def check_db(path):
    """Return ("ok"|"bad"|"unverified", detail) from quick_check; detail on "ok" is a note."""
    sidecars = [path + "-wal", path + "-journal"]  # an immutable read would skip either's recovery
    if os.path.getsize(path) > COPY_LIMIT and any(os.path.exists(f) and os.path.getsize(f) > 0 for f in sidecars):
        return "unverified", "too large to copy and has a non-empty -wal or -journal that an immutable read would skip"
    note = ""
    try:
        if os.path.getsize(path) <= COPY_LIMIT:
            os.makedirs(SCRATCH, exist_ok=True)
            target = os.path.join(SCRATCH, "db")
            for suffix in ("", "-wal", "-journal"):
                if os.path.exists(path + suffix):
                    shutil.copyfile(path + suffix, target + suffix)
            try:
                rows = quick_check(target)
            except sqlite3.Error as e:
                match = UNKNOWN_TOKENIZER.search(str(e))
                dropped = drop_tokenizer_tables(target, match.group(1)) if match else []
                if not dropped:
                    raise
                rows = quick_check(target)
                note = f"FTS table(s) {dropped} use tokenizer {match.group(1)!r}, not loadable here; data pages checked"
        else:
            rows = quick_check(f"file:{path}?immutable=1", uri=True)
        return ("ok", note) if rows == [("ok",)] else ("bad", str(rows[:3])[:200])
    except sqlite3.Error as e:
        if UNKNOWN_TOKENIZER.search(str(e)) and os.path.getsize(path) > COPY_LIMIT:
            return "unverified", f"too large to copy, and {e}"
        return "bad", str(e)  # "not a database" is itself the finding
    except OSError as e:
        return "bad", str(e)
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)


def main():
    """Print the JSON report for /d."""
    report, dbs = read_tree(ROOT)
    report["sqlite"] = len(dbs)
    report["bad"], report["unverified"], report["notes"] = [], [], []
    for path in dbs:
        verdict, why = check_db(path)
        if why:
            report["notes" if verdict == "ok" else verdict].append(f"{os.path.relpath(path, ROOT)}: {why}")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
