"""launchd jobs: poll and worker every 10 minutes, the cheap watchlist scan weekly."""
import os
import plistlib
import subprocess
import sys
from pathlib import Path

JOBS = {
    "com.fundabot.poll": {"args": ["poll"], "StartInterval": 600},
    "com.fundabot.worker": {"args": ["worker"], "StartInterval": 600},
    "com.fundabot.weekly": {"args": ["scan", "--watchlist"], "StartCalendarInterval": {"Weekday": 0, "Hour": 6, "Minute": 0}},
}


def plists(binary: Path, workdir: Path, log_dir: Path) -> dict[str, str]:
    out = {}
    for label, job in JOBS.items():
        d = {"Label": label, "ProgramArguments": [str(binary), *job["args"]], "WorkingDirectory": str(workdir), "RunAtLoad": False,
             "StandardOutPath": str(log_dir / f"{label}.log"), "StandardErrorPath": str(log_dir / f"{label}.err.log")}
        d.update({k: v for k, v in job.items() if k != "args"})
        out[label] = plistlib.dumps(d).decode()
    return out


def _agents_dir(home: Path) -> Path:
    return home / "Library" / "LaunchAgents"


def install(binary: Path | None = None, workdir: Path | None = None, home: Path | None = None, run=subprocess.run, uid: int | None = None) -> list[Path]:
    binary = binary or Path(sys.argv[0]).resolve()
    workdir, home = workdir or Path.cwd(), home or Path.home()
    uid = os.getuid() if uid is None else uid
    log_dir = home / "Library" / "Logs" / "fundabot"
    log_dir.mkdir(parents=True, exist_ok=True)
    _agents_dir(home).mkdir(parents=True, exist_ok=True)
    paths = []
    for label, xml in plists(binary, workdir, log_dir).items():
        p = _agents_dir(home) / f"{label}.plist"
        if p.exists():
            run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True, text=True)
        p.write_text(xml)
        run(["launchctl", "bootstrap", f"gui/{uid}", str(p)], capture_output=True, text=True)
        paths.append(p)
    return paths


def uninstall(home: Path | None = None, run=subprocess.run, uid: int | None = None) -> list[str]:
    home = home or Path.home()
    uid = os.getuid() if uid is None else uid
    removed = []
    for label in JOBS:
        run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True, text=True)
        p = _agents_dir(home) / f"{label}.plist"
        if p.exists():
            p.unlink()
            removed.append(label)
    return removed


def status(run=subprocess.run) -> list[str]:
    r = run(["launchctl", "list"], capture_output=True, text=True)
    return [line.split("\t")[-1].strip() for line in r.stdout.splitlines() if "com.fundabot." in line]
