import os
import signal
import subprocess
import sys


def assert_prints(p: subprocess.Popen, *messages: str):
    for msg in messages:
        line = p.stdout.readline()
        if not line:
            print("STDERR")
            print(p.stderr.read())
            raise Exception("Unexpected empty line in output, see stderr:")
        assert line == msg + "\n"


def test_shutdown():
    # We run it in a separate process so we can simulate interrupting it
    fn = os.path.join(os.path.dirname(__file__), "_shutdown.py")
    p = subprocess.Popen(
        [sys.executable, fn],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PYTHONUNBUFFERED": "1"},
        encoding="utf8",
    )
    assert_prints(
        p,
        "calling wrapped func",
        "starting up synchronicity event loop",
        *(["running"] * 3),  # wait for 3 "running" messages before siginting the process
    )
    p.send_signal(signal.SIGINT)
    assert_prints(
        p,
        "eof",
        "start shutting down synchronicity event loop",
        "cancelled",
        "stopping",
        "exiting",
        "finished shutting down synchronicity event loop",
    )
    out, err = p.communicate()
    assert out == ""
    assert err == ""
    assert p.returncode == 0


def test_shutdown_ctx_mgr():
    # We run it in a separate process so we can simulate interrupting it
    fn = os.path.join(os.path.dirname(__file__), "_shutdown_ctxmgr.py")
    p = subprocess.Popen(
        [sys.executable, fn],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PYTHONUNBUFFERED": "1"},
        encoding="utf8",
    )
    assert_prints(
        p,
        "starting up synchronicity event loop",  # start up loop explicitly
        "calling wrapped func",
        *(["running"] * 3),
    )
    p.send_signal(signal.SIGINT)

    assert_prints(
        p,
        "start shutting down synchronicity event loop",
        "cancelled",
        "stopping",
        "exiting",
        "finished shutting down synchronicity event loop",  # shut down loop explicitly,
        "eof",
    )
    out, err = p.communicate()
    assert out == ""
    assert err == ""
    assert p.returncode == 0
