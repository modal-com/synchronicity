import asyncio
import logging
import sys

from synchronicity import Synchronizer
from synchronicity.synchronizer import logger

logger.addHandler(logging.StreamHandler(sys.stdout))
logger.setLevel(logging.DEBUG)


async def run():
    try:
        while True:
            print("running")
            await asyncio.sleep(0.3)
    except asyncio.CancelledError:
        print("cancelled")
        raise
    finally:
        print("stopping")
        await asyncio.sleep(0.1)
        print("exiting")


s = Synchronizer()
wrapped_func = s.create_blocking(run)

try:
    with s:
        print("calling wrapped func")
        wrapped_func()
except KeyboardInterrupt:
    pass

print("eof")
