import asyncio
import sys
import time
import traceback

from synchronicity import Synchronizer


def sync_shutdown_handler():
    # this simulates any threading usage during event loop shutdown
    # e.g. a network request using getaddrinfo etc.
    # These are typically prohibited if the event loop shutdown is
    # part of the python interpreter itself shutting down
    time.sleep(0.1)
    print("ran shutdown handler", flush=True)


async def run():
    try:
        while True:
            print("running")
            await asyncio.sleep(0.3)
    except asyncio.CancelledError:
        print("cancelled")
        await asyncio.to_thread(sync_shutdown_handler)
        raise
    finally:
        print("stopping")
        await asyncio.sleep(0.1)
        print("exiting")


s = Synchronizer()

try:
    s.create_blocking(run)()
except KeyboardInterrupt:
    pass
