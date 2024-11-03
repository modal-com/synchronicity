import pytest
import signal
import subprocess
import sys
from pathlib import Path


class PopenWithCtrlC(subprocess.Popen):
    def __init__(self, *args, creationflags=0, **kwargs):
        if sys.platform == "win32":
            # needed on windows to separate ctrl-c lifecycle of subprocess from parent:
            creationflags = creationflags | subprocess.CREATE_NEW_CONSOLE  # type: ignore

        super().__init__(*args, **kwargs, creationflags=creationflags)

    def send_ctrl_c(self):
        # platform independent way to replicate the behavior of Ctrl-C:ing a cli app
        if sys.platform == "win32":
            # windows doesn't support sigint, and subprocess.CTRL_C_EVENT has a bunch
            # of gotchas since it's bound to a console which is the same for the parent
            # process by default, and can't be sent using the python standard library
            # to a separate process's console
            import console_ctrl

            console_ctrl.send_ctrl_c(self.pid)  # noqa [E731]
        else:
            self.send_signal(signal.SIGINT)


def test_shutdown():
    # We run it in a separate process so we can simulate interrupting it
    fn = Path(__file__).parent / "support" / "_shutdown.py"
    p = PopenWithCtrlC([sys.executable, "-u", fn], stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf8")
    for i in range(2):  # this number doesn't matter, it's a while loop
        assert p.stdout.readline() == "running\n"
    p.send_ctrl_c()
    assert p.stdout.readline() == "cancelled\n"
    assert p.stdout.readline() == "handled cancellation\n"
    assert p.stdout.readline() == "exit async\n"
    assert (
        p.stdout.readline() == "keyboard interrupt\n"
    )  # we want the keyboard interrupt to come *after* the running function has been cancelled!

    assert p.stderr.read().strip() == ""


def test_keyboard_interrupt_reraised_as_is(synchronizer):
    @synchronizer.create_blocking
    async def a():
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        a()


def test_shutdown_during_ctx_mgr_setup():
    # We run it in a separate process so we can simulate interrupting it
    fn = Path(__file__).parent / "support" / "_shutdown_ctx_mgr.py"
    p = PopenWithCtrlC(
        [sys.executable, "-u", fn, "enter"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf8",
    )
    for i in range(2):  # this number doesn't matter, it's a while loop
        assert p.stdout.readline() == "enter\n"
    p.send_ctrl_c()
    assert p.stdout.readline() == "exit\n"
    assert p.stdout.readline() == "keyboard interrupt\n"
    assert p.stderr.read() == ""


def test_shutdown_during_ctx_mgr_yield():
    # We run it in a separate process so we can simulate interrupting it
    fn = Path(__file__).parent / "support" / "_shutdown_ctx_mgr.py"
    p = PopenWithCtrlC(
        [sys.executable, "-u", fn, "yield"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf8"
    )
    for i in range(2):  # this number doesn't matter, it's a while loop
        assert p.stdout.readline() == "in ctx\n"
    p.send_ctrl_c()
    assert p.stdout.readline() == "exit\n"
    assert p.stdout.readline() == "keyboard interrupt\n"
    assert p.stderr.read() == ""


@pytest.mark.parametrize("run_number", range(10))  # don't allow this to flake!
def test_shutdown_during_async_run(run_number):
    fn = Path(__file__).parent / "support" / "_shutdown_async_run.py"
    p = PopenWithCtrlC(
        [sys.executable, "-u", fn],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )

    def line():
        # debugging help
        line_data = p.stdout.readline()
        print(line_data)
        return line_data

    assert line() == "running\n"
    p.send_ctrl_c()
    print("sigint sent")
    while (next_line := line()) == "running\n":
        pass
    assert next_line == "cancelled\n"
    stdout, stderr = p.communicate(timeout=5)
    print(stderr)
    assert (
        stdout
        == """handled cancellation
exit async
keyboard interrupt
"""
    )
    assert stderr == ""
