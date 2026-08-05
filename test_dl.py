import asyncio
from unittest.mock import MagicMock
from download_file import progress

async def main():
    # Simulate pyrogram calling progress
    await progress(0, 1000)
    await progress(500, 1000)
    await progress(1000, 1000)

asyncio.run(main())
