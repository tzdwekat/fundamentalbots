import plistlib
from pathlib import Path

from fundabot.orchestrate import schedule


def test_plists_content():
    out = schedule.plists(Path("/Users/t/.venvs/fundabot/bin/fundabot"), Path("/Users/t/src/fundabot"), Path("/Users/t/Library/Logs/fundabot"))
    assert set(out) == {"com.fundabot.poll", "com.fundabot.worker", "com.fundabot.weekly"}
    poll = plistlib.loads(out["com.fundabot.poll"].encode())
    assert poll["Label"] == "com.fundabot.poll" and poll["StartInterval"] == 600 and poll["ProgramArguments"] == ["/Users/t/.venvs/fundabot/bin/fundabot", "poll"]
    assert poll["WorkingDirectory"] == "/Users/t/src/fundabot" and poll["StandardOutPath"].startswith("/Users/t/Library/Logs/fundabot/") and poll["StandardErrorPath"].endswith(".err.log")
    worker = plistlib.loads(out["com.fundabot.worker"].encode())
    assert worker["StartInterval"] == 600 and worker["ProgramArguments"][1] == "worker"
    weekly = plistlib.loads(out["com.fundabot.weekly"].encode())
    assert weekly["StartCalendarInterval"] == {"Weekday": 0, "Hour": 6, "Minute": 0} and weekly["ProgramArguments"][1:] == ["scan", "--watchlist"]
    assert all(plistlib.loads(p.encode())["RunAtLoad"] is False for p in out.values())


def test_install_writes_and_bootstraps(tmp_path):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    paths = schedule.install(binary=Path("/v/bin/fundabot"), workdir=tmp_path, home=tmp_path, run=fake_run, uid=501)
    assert [p.name for p in paths] == ["com.fundabot.poll.plist", "com.fundabot.worker.plist", "com.fundabot.weekly.plist"]
    assert all(p.exists() and p.parent == tmp_path / "Library" / "LaunchAgents" for p in paths)
    assert (tmp_path / "Library" / "Logs" / "fundabot").is_dir()
    assert calls[0][:3] == ["launchctl", "bootstrap", "gui/501"] and calls[0][3] == str(paths[0])
    schedule.uninstall(home=tmp_path, run=fake_run, uid=501)
    assert calls[-1][:3] == ["launchctl", "bootout", "gui/501/com.fundabot.weekly"] and not paths[0].exists()


def test_status_lists_jobs():
    def fake_run(cmd, **kw):
        return type("R", (), {"returncode": 0, "stdout": "-\t0\tcom.fundabot.poll\n123\t0\tcom.fundabot.worker\n", "stderr": ""})()
    assert schedule.status(run=fake_run) == ["com.fundabot.poll", "com.fundabot.worker"]
