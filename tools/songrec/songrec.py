#!/usr/bin/env python3
"""Bundled SongRec CLI tool for KILLA'S BOSS audio bot."""
import asyncio
import json
import os
import sys

try:
    from shazamio import Shazam
except ImportError:
    print(json.dumps({"matches": []}))
    sys.exit(0)


async def _recognize(path):
    shazam = Shazam()
    try:
        return await shazam.recognize(path)
    except Exception:
        return {"matches": []}


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"matches": []}))
        sys.exit(1)

    file_path = sys.argv[-1]
    if not os.path.isfile(file_path):
        print(json.dumps({"matches": []}))
        sys.exit(1)

    try:
        data = asyncio.run(_recognize(file_path))
        print(json.dumps(data))
    except Exception:
        print(json.dumps({"matches": []}))


if __name__ == "__main__":
    main()
