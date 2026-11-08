"""Restore test: replay each app's VolSync restore into a scratch PVC and prove the data reads back.

Each target copies a live ReplicationDestination (`<app>-dst` for R2, `<app>-nfs-dst` for NFS), so
the test runs the exact disaster-recovery path: mover restore, VolumeSnapshot, PVC populated from
it. A verify pod then mounts the PVC read-only, reads every byte and checks every SQLite file.
Design and the 2026-11-08 incident behind it: docs/docs/apps/restoretest.md.
"""

import concurrent.futures
import copy
import datetime
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from typing import Any

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
VS = "/apis/volsync.backube/v1alpha1"
CORE = "/api/v1/namespaces"
MANAGED = {"app.kubernetes.io/managed-by": "restoretest"}

RUN = os.environ.get("RUN_ID") or "rt-" + datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M%S")
REPOS = [r.strip() for r in os.environ.get("REPOS", "r2,nfs").split(",") if r.strip()]
SHARDS = int(os.environ.get("SHARDS", "4"))
SHARD = os.environ.get("SHARD", "")  # "" = this ISO week's shard, "all", or a number
ONLY = {s.strip() for s in os.environ.get("ONLY", "").split(",") if s.strip()}  # ns/app,...
MAX_CAPACITY_GI = float(os.environ.get("MAX_CAPACITY_GI", "50"))
CONCURRENCY = int(os.environ.get("CONCURRENCY", "3"))
RESTORE_TIMEOUT = int(os.environ.get("RESTORE_TIMEOUT", "2700"))
VERIFY_TIMEOUT = int(os.environ.get("VERIFY_TIMEOUT", "2700"))
KEEP_FAILED = os.environ.get("KEEP_FAILED", "true") == "true"
# Past the Job's activeDeadlineSeconds (6h) no run can still own an object, so the sweep is safe.
SWEEP_AGE = int(os.environ.get("SWEEP_AGE", "25200"))
SUFFIX = f"{zlib.crc32(RUN.encode()):08x}"[:5]  # per-run names, so overlapping runs never collide

_PRINT_LOCK = threading.Lock()
_SSL: dict[str, ssl.SSLContext] = {}


def read(path):
    """Return a small text file's contents."""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def log(tag, msg):
    """Print one timestamped line; targets run in threads."""
    with _PRINT_LOCK:
        print(f"{datetime.datetime.now(datetime.UTC):%H:%M:%S} [{tag}] {msg}", flush=True)


def call(method, path, body=None, missing_ok=False, raw=False) -> Any:
    """Call the Kubernetes API with the pod's service account; JSON in and out unless raw."""
    if "ctx" not in _SSL:
        _SSL["ctx"] = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    base = f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT']}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {read(f'{SA}/token').strip()}")  # projected token rotates
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, context=_SSL["ctx"], timeout=60) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404 and missing_ok:
            return None
        raise RuntimeError(f"{method} {path}: HTTP {e.code} {e.read()[:300]!r}") from None
    return payload.decode(errors="replace") if raw else json.loads(payload or b"{}")


def gib(quantity):
    """Convert a Kubernetes quantity to GiB."""
    units = {"Ki": 2**-20, "Mi": 2**-10, "Gi": 1, "Ti": 2**10, "K": 1e3 / 2**30, "M": 1e6 / 2**30}
    units |= {"G": 1e9 / 2**30, "T": 1e12 / 2**30}
    for suffix, factor in sorted(units.items(), key=lambda kv: -len(kv[0])):
        if quantity.endswith(suffix):
            return float(quantity[: -len(suffix)]) * factor
    return float(quantity) / 2**30


def classify(name):
    """Map a ReplicationDestination name to (repo, app), or None for anything else."""
    if name.endswith("-nfs-dst"):
        return "nfs", name[: -len("-nfs-dst")]
    if name.endswith("-dst"):
        return "r2", name[: -len("-dst")]
    return None


def select(items, week):
    """Pick this run's targets from all ReplicationDestinations; return (shard, picked, skipped)."""
    shard = None if SHARD == "all" or ONLY else (int(SHARD) if SHARD else week % SHARDS)
    picked, skipped = [], []
    for rd in items:
        meta = rd["metadata"]
        kind = classify(meta["name"])
        mover = rd["spec"].get("restic") or rd["spec"].get("kopia")
        ours = (meta.get("labels") or {}).get("app.kubernetes.io/managed-by") == "restoretest"
        if ours or not kind or not mover:
            continue
        repo, app = kind
        key = f"{meta['namespace']}/{app}"
        if repo not in REPOS or (ONLY and key not in ONLY):
            continue
        if shard is not None and zlib.crc32(key.encode()) % SHARDS != shard:
            continue
        cap = mover.get("capacity", "5Gi")
        if gib(cap) > MAX_CAPACITY_GI and not ONLY:
            skipped.append(f"{key}:{repo} ({cap})")
            continue
        picked.append({"ns": meta["namespace"], "app": app, "repo": repo, "rd": rd, "capacity": cap})
    return shard, sorted(picked, key=lambda t: (t["ns"], t["app"], t["repo"])), skipped


def wait(fn, timeout, interval=10):
    """Poll fn until it returns something truthy or the timeout passes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return None


def cleanup(ns, name):
    """Delete a target's verify pod, PVC and ReplicationDestination, and wait until they are gone."""
    paths = [f"{CORE}/{ns}/persistentvolumeclaims/{name}", f"{VS}/namespaces/{ns}/replicationdestinations/{name}"]
    for path in [f"{CORE}/{ns}/pods/{name}-verify", *paths]:
        call("DELETE", path, {"propagationPolicy": "Foreground"}, missing_ok=True)
    if not wait(lambda: not any(call("GET", p, missing_ok=True) for p in paths), 300, 5):
        raise RuntimeError(f"{ns}/{name} did not delete within 300s")


def sweep():
    """Delete earlier runs' scratch objects older than any run can last, whatever this run selects."""
    selector = "labelSelector=app.kubernetes.io%2Fmanaged-by%3Drestoretest"
    now = datetime.datetime.now(datetime.UTC)
    found = set()
    for path in (f"{VS}/replicationdestinations", "/api/v1/persistentvolumeclaims", "/api/v1/pods"):
        for item in call("GET", f"{path}?{selector}")["items"]:
            meta = item["metadata"]
            born = datetime.datetime.fromisoformat(meta["creationTimestamp"].replace("Z", "+00:00"))
            if (now - born).total_seconds() > SWEEP_AGE:
                found.add((meta["namespace"], meta["name"].removesuffix("-verify")))
    for ns, name in sorted(found):
        log("sweep", f"deleting {ns}/{name} from an earlier run")
        cleanup(ns, name)


def restore(ns, name, source_spec):
    """Run the copied ReplicationDestination to completion; return its mover spec."""
    spec = copy.deepcopy(source_spec)
    spec["trigger"] = {"manual": RUN}
    mover = spec.get("restic") or spec.get("kopia")
    mover["cleanupTempPVC"] = True
    mover["cleanupCachePVC"] = True
    body = {
        "apiVersion": "volsync.backube/v1alpha1",
        "kind": "ReplicationDestination",
        "metadata": {"name": name, "labels": MANAGED},
        "spec": spec,
    }
    call("POST", f"{VS}/namespaces/{ns}/replicationdestinations", body)

    def status():
        st = (call("GET", f"{VS}/namespaces/{ns}/replicationdestinations/{name}") or {}).get("status") or {}
        return st if st.get("lastManualSync") == RUN else None

    st = wait(status, RESTORE_TIMEOUT)
    if not st:
        raise RuntimeError(f"restore did not finish in {RESTORE_TIMEOUT}s")
    mover_status = st.get("latestMoverStatus") or {}
    if mover_status.get("result") != "Successful" or not (st.get("latestImage") or {}).get("name"):
        logs = str(mover_status.get("logs", ""))[-400:]
        raise RuntimeError(f"restore result {mover_status.get('result')!r}: {logs}")
    return mover


def verify_pod(name, mover, image, verify_src):
    """Build the read-only verify pod, running as the mover's user so it can read what was backed up."""
    msc = mover.get("moverSecurityContext") or {}
    uid, gid = msc.get("runAsUser", 1000), msc.get("runAsGroup", 1000)
    pod_sc = {"runAsUser": uid, "runAsGroup": gid, "seccompProfile": {"type": "RuntimeDefault"}}
    if msc.get("fsGroup") is not None:
        pod_sc["supplementalGroups"] = [msc["fsGroup"]]  # fsGroup itself would chown the restored tree
    if uid != 0:
        pod_sc["runAsNonRoot"] = True
    container = {
        "name": "verify",
        "image": image,
        "command": ["python3", "-I", "-c", verify_src],
        "volumeMounts": [{"name": "d", "mountPath": "/d", "readOnly": True}, {"name": "tmp", "mountPath": "/tmp"}],
        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "512Mi"}},
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
    }
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": f"{name}-verify", "labels": MANAGED},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "securityContext": pod_sc,
            "volumes": [
                {"name": "d", "persistentVolumeClaim": {"claimName": name, "readOnly": True}},
                {"name": "tmp", "emptyDir": {"sizeLimit": "6Gi"}},
            ],
            "containers": [container],
        },
    }


def verify(ns, name, capacity, mover, image, verify_src):
    """Populate a PVC from the restore, run the verify pod on it, and return its report."""
    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "labels": MANAGED},
        "spec": {
            "accessModes": mover.get("accessModes") or ["ReadWriteOnce"],
            "dataSourceRef": {"apiGroup": "volsync.backube", "kind": "ReplicationDestination", "name": name},
            "resources": {"requests": {"storage": capacity}},
            "storageClassName": mover.get("storageClassName"),
        },
    }
    call("POST", f"{CORE}/{ns}/persistentvolumeclaims", pvc)
    call("POST", f"{CORE}/{ns}/pods", verify_pod(name, mover, image, verify_src))

    def finished():
        pod = call("GET", f"{CORE}/{ns}/pods/{name}-verify")
        return pod["status"].get("phase") in ("Succeeded", "Failed")

    if not wait(finished, VERIFY_TIMEOUT):
        events = call("GET", f"{CORE}/{ns}/events?fieldSelector=involvedObject.name%3D{name}-verify")["items"]
        why = "; ".join(
            f"{e.get('reason')}: {e.get('message', '')[:200]}" for e in events if e.get("type") == "Warning"
        )
        raise RuntimeError(f"verify pod did not finish in {VERIFY_TIMEOUT}s ({why or 'no warning events'})")
    out = call("GET", f"{CORE}/{ns}/pods/{name}-verify/log", raw=True)
    lines = [line for line in out.splitlines() if line.startswith("{")]
    if not lines:
        raise RuntimeError(f"verify printed no result: {out[-400:]}")
    return json.loads(lines[-1])


def problems(report):
    """Return the reasons a verify report fails, empty when it passes."""
    found = []
    if report["files"] == 0:
        found.append("restored volume is empty")
    if report["eio"]:
        found.append(f"read errors in {report['eio'][:5]}")
    if report["bad"]:
        found.append(f"SQLite failures {report['bad'][:5]}")
    return found


def run_one(t, image, verify_src):
    """Restore and verify one target; never raises, so one failure cannot stop the run."""
    ns, repo = t["ns"], t["repo"]
    name = f"rt-{t['app']}" + ("-nfs" if repo == "nfs" else "") + f"-{SUFFIX}"
    tag = f"{ns}/{t['app']}:{repo}"
    started = time.time()
    result = {"target": tag, "ok": False}
    try:
        mover = restore(ns, name, t["rd"]["spec"])
        report = verify(ns, name, t["capacity"], mover, image, verify_src)
        result |= report
        if found := problems(report):
            raise RuntimeError("; ".join(found))
        result["ok"] = True
        log(
            tag,
            f"OK files={report['files']} bytes={report['bytes']} sqlite={report['sqlite']} "
            f"denied={report['denied']} unverified={report.get('unverified', [])} in {time.time() - started:.0f}s",
        )
    except Exception as e:  # pylint: disable=broad-exception-caught  # the error text is the report
        result["error"] = str(e)
        log(tag, f"FAIL {e}")
    if result["ok"] or not KEEP_FAILED:
        try:
            cleanup(ns, name)
        except Exception as e:  # pylint: disable=broad-exception-caught  # the next run retries cleanup
            log(tag, f"cleanup failed: {e}")
    else:
        log(tag, f"kept {ns}/{name} for inspection; a run more than {SWEEP_AGE // 3600}h later deletes it")
    return result


def main():
    """Run this week's targets and exit non-zero if any failed or none ran."""
    verify_src = read(os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify.py"))
    sweep()
    items = call("GET", f"{VS}/replicationdestinations")["items"]
    shard, picked, skipped = select(items, datetime.date.today().isocalendar().week)
    own = call("GET", f"{CORE}/{read(f'{SA}/namespace').strip()}/pods/{os.environ['HOSTNAME']}")
    image = own["spec"]["containers"][0]["image"]
    which = shard if shard is not None else "all"
    log("run", f"{RUN}: shard {which} of {SHARDS}, {len(picked)} restores, repos={REPOS}")
    for s in skipped:
        log("run", f"skipped over {MAX_CAPACITY_GI:g}Gi: {s}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        results = list(pool.map(lambda t: run_one(t, image, verify_src), picked))
    bad = [r for r in results if not r["ok"]]
    log("run", f"summary: {len(results) - len(bad)} passed, {len(bad)} failed, {len(skipped)} skipped")
    for r in bad:
        log("run", f"FAILED {r['target']}: {r.get('error')}")
    passed = len(results) - len(bad)
    print(json.dumps({"run": RUN, "passed": passed, "failed": [r["target"] for r in bad], "skipped": len(skipped)}))
    return 1 if bad or not picked else 0


if __name__ == "__main__":
    sys.exit(main())
