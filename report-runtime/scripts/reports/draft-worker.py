"""Persistent completed-export consumer. Runs only the draft CLI, never release.

The kernel lock survives worker restarts by releasing automatically. Each attempt
has its own log and machine receipt. Unready/failed entries remain retryable.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

SUCCESS = {"review", "unchanged", "published"}
MONTH = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])")
SECTORS = {"music-generation", "image-generation", "video-generation", "speech-text-conversion", "presentations-visualization"}


def read(file):
    return json.loads(file.read_text("utf-8"))


def atomic_json(file, data):
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(file.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, file)
        if os.name != 'nt':
            descriptor = os.open(file.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def worker_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "worker.lock").open("a+b") as stream:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            if not stream.read(1):
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def runtime_key(root):
    digest = hashlib.sha256(sys.version.encode())
    paths = set((root / "scripts/reports").rglob("*.py")) | set((root / "scripts/reports").rglob("*.mjs"))
    paths |= set((root / "content/reports").rglob("*.json")) | set((root / "content/reports").rglob("*.ts"))
    paths |= {root / p for p in ("lib/reports/model.ts", "lib/market-statistics.ts", "config/seo.ts", "public/_headers", "public/brand/sigpik-logo.png")}
    if (root / "runtime-baseline.json").exists():
        paths.add(root / "runtime-baseline.json")
    for file in sorted(paths):
        digest.update(str(file.relative_to(root)).encode())
        digest.update(file.read_bytes())
    font = Path(os.environ.get("REPORT_FONT", "C:/Windows/Fonts/msyh.ttc"))
    digest.update(font.read_bytes())  # Fail preflight instead of producing tofu glyphs.
    bold = font.with_name(font.name.replace('-Regular', '-Bold') if '-Regular' in font.name else font.stem + 'bd' + font.suffix)
    if bold.exists():
        digest.update(bold.read_bytes())
    digest.update(os.environ.get("REPORT_RUNTIME_VERSION", "local").encode())
    return digest.hexdigest()


def run_draft(root, directory, month, sector, attempt, timeout):
    receipt_path = attempt / "queue.json"
    command = [os.environ.get("REPORT_NODE", "node"), str(root / "scripts/reports/pipeline.mjs"), "from-exports",
               "--directory", str(directory), "--month", month, "--sectors", sector, "--receipt", str(receipt_path)]
    with (attempt / "process.log").open("wb") as log:
        process = subprocess.Popen(command, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=os.name != "nt", creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait()
            raise
    if code not in (0, 2) or not receipt_path.exists():
        raise RuntimeError(f"Draft CLI failed with exit {code}; see process.log")
    receipt = read(receipt_path)
    if receipt.get("month") != month or receipt.get("deployed") is not False or len(receipt.get("results", [])) != 1:
        raise RuntimeError("Invalid queue receipt")
    result = receipt["results"][0]
    if result.get("sector") != sector or result.get("status") not in SUCCESS | {"waiting", "blocked"}:
        raise RuntimeError("Invalid market receipt")
    if (code == 2) != (result["status"] == "blocked"):
        raise RuntimeError("Exit code and receipt disagree")
    return {**result, "exitCode": code, "queueReceipt": str(receipt_path)}


def poll(root, exports, state_root, *, now=None, generate=run_draft, identity=None, timeout=1800):
    now = time.time() if now is None else now
    identity = runtime_key(root) if identity is None else identity
    results = []
    exports.mkdir(parents=True, exist_ok=True)
    for month_dir in sorted(exports.iterdir()):
        if not month_dir.is_dir() or month_dir.is_symlink() or not MONTH.fullmatch(month_dir.name):
            continue
        for sector in sorted(SECTORS):
            completion = month_dir / sector / "complete.json"
            if not completion.is_file():
                continue  # No completion event yet. The producer records why.
            state_file = state_root / month_dir.name / sector / "state.json"
            try:
                prior = read(state_file) if state_file.exists() else {}
            except json.JSONDecodeError:
                # Atomic writes should prevent this; preserve damage for review
                # and recover through the generator's verified idempotent cache.
                damaged = state_file.with_name('state-damaged-' + uuid.uuid4().hex + '.txt')
                damaged.write_bytes(state_file.read_bytes())
                prior = {}
            key = hashlib.sha256(completion.read_bytes() + identity.encode()).hexdigest()
            if prior.get("key") == key:
                if prior.get("status") in SUCCESS or prior.get("retryAt", 0) > now:
                    continue
            attempts = prior.get("attempts", 0) + 1 if prior.get("key") == key else 1
            attempt = state_file.parent / "attempts" / uuid.uuid4().hex
            attempt.mkdir(parents=True)
            state = {"key": key, "month": month_dir.name, "sector": sector, "attempts": attempts,
                     "status": "running", "startedAt": now, "attemptDirectory": str(attempt), "deployed": False}
            atomic_json(state_file, state)
            try:
                result = generate(root, month_dir, month_dir.name, sector, attempt, timeout)
                if key != hashlib.sha256(completion.read_bytes() + identity.encode()).hexdigest():
                    raise RuntimeError("Completion event changed during generation; retry latest")
                state.update(result)
            except Exception as error:
                state.update(status="blocked", reason=str(error)[:500], errorType=type(error).__name__)
            state["finishedAt"] = time.time()
            if state["status"] not in SUCCESS:
                state["retryAt"] = now + min(21600, 300 * 2 ** min(attempts - 1, 7))
            atomic_json(attempt / "receipt.json", state)
            atomic_json(state_file, state)
            results.append(state)
            print(json.dumps({k: state[k] for k in ("month", "sector", "status", "attemptDirectory")}, ensure_ascii=False), flush=True)
    atomic_json(state_root / "heartbeat.json", {"checkedAt": time.time(), "runtimeKey": identity, "processed": len(results), "deployed": False})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--exports", type=Path, default=os.environ.get("REPORT_EXPORT_ROOT"))
    parser.add_argument("--state", type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=int(os.environ.get("REPORT_DRAFT_INTERVAL_SECONDS", "21600")))
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    if not args.exports:
        parser.error("--exports or REPORT_EXPORT_ROOT is required")
    root = args.root.resolve()
    state = (args.state or root / "work/reports/automation").resolve()
    # SIGTERM must unwind the child-process cleanup before releasing the lock.
    if os.name != "nt":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    with worker_lock(state):
        while True:
            results = poll(root, args.exports.resolve(), state, timeout=args.timeout)
            if args.once:
                states = [read(p) for p in state.glob('*/*/state.json')]
                return 2 if any(r['status'] == 'blocked' for r in states) else 0
            wait_for_next_poll(state, args.interval)


def wait_for_next_poll(state_root, interval):
    # Keep liveness fresh during the six-hour wait without scanning exports,
    # hashing the rendering runtime or retrying report generation.
    deadline = time.monotonic() + max(10, interval)
    while (remaining := deadline - time.monotonic()) > 0:
        time.sleep(min(60, remaining))
        (state_root / "heartbeat.json").touch()


if __name__ == "__main__":
    sys.exit(main())
