"""Show Builder and SuperSource Builder on the One.

static/builders.tgz (tools/build-builders.py) unpacks once per build and both run on
their own ports when someone first opens one, and each stops itself after 10 minutes
with no page open (tools/build-builders.py). They sit in this app's cgroup, so a restart
of the app takes them with it and an update can't leave an old copy running.
Niced: a builder never takes time from the timer or Companion.
"""
import hashlib, json, os, shutil, socket, subprocess, sys, tarfile, threading, time
from pathlib import Path

DATA = Path.home() / "downstage-builders"       # shows, recipes, exports. factory reset and prepare-golden remove it
PORTS = {"show": 8787, "supersource": 8789}
SCRIPTS = {"show": "Companion/tools/show-builder/show-builder.py", "supersource": "supersource/twobox/server.py"}
_lock = threading.Lock()


def _unpacked(base_dir):
    tgz = Path(base_dir) / "static" / "builders.tgz"
    sha = hashlib.sha256(tgz.read_bytes()).hexdigest()[:12]
    apps = DATA / "app"
    here = apps / sha
    if not here.is_dir():
        tmp = apps / (sha + ".new")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        with tarfile.open(tgz) as tf:
            tf.extractall(tmp, filter="data")
        tmp.rename(here)
        for old in apps.iterdir():
            if old != here:
                shutil.rmtree(old, ignore_errors=True)
    return here


def _up(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start(base_dir):
    """Both run, because each opens the other and Show Builder lists SuperSource's exports."""
    with _lock:
        if all(_up(p) for p in PORTS.values()):
            return PORTS
        app = _unpacked(base_dir)
        show, ss = DATA / "show", DATA / "supersource"
        for d in (show, ss):
            d.mkdir(parents=True, exist_ok=True)
        links = ss / "links.json"
        if not links.exists():
            links.write_text(json.dumps({"companion": "127.0.0.1:8000"}))
        for name, port in PORTS.items():
            if _up(port):
                continue
            env = dict(os.environ, DOWNSTAGE_DATA=str(show if name == "show" else ss), DOWNSTAGE_SS_DATA=str(ss))
            log = open(DATA / f"{name}.log", "ab")
            subprocess.Popen(["nice", "-n", "10", sys.executable, str(app / SCRIPTS[name]), "--port", str(port), "--no-browser"],
                             cwd=str(DATA), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            log.close()
        until = time.time() + 20
        while time.time() < until and not all(_up(p) for p in PORTS.values()):
            time.sleep(0.2)
        missing = [n for n, p in PORTS.items() if not _up(p)]
        if missing:
            raise RuntimeError(f"{' and '.join(missing)} did not start. See {DATA}/{missing[0]}.log")
        return PORTS
