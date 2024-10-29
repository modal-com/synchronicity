import asyncio
import sys

from synchronicity import Synchronizer

if sys.version_info < (3, 11):
    # asyncio.Runner doesn't exist prior to 3.11,
    # and asyncio.run() is unsafe for sigints on <3.11
    from synchronicity.async_utils import Runner
else:
    from asyncio import Runner


async def run():
    try:
        while True:
            await asyncio.sleep(0.2)
            print("running")

    except asyncio.CancelledError:
        print("cancelled")
        await asyncio.sleep(0.1)
        print("handled cancellation")
        raise
    finally:
        await asyncio.sleep(0.1)
        print("exit async")


s = Synchronizer()

blocking_run = s.create_blocking(run)


try:
    with Runner() as runner:
        runner.run(blocking_run.aio())
except KeyboardInterrupt:
    print("keyboard interrupt")
