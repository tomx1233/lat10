#!/usr/bin/env python3
"""Bundled SongRec CLI wrapper for KILLA'S BOSS audio bot."""
import asyncio
import json
import os
import sys

try:
    from shazamio import Shazam
except ImportError as exc:
    sys.stderr.write(f"shazamio unavailable: {exc}\n")
    sys.exit(2)


async def _recognize(path):
    shazam = Shazam()
    try:
        return await shazam.recognize(path)
    finally:
        close = getattr(shazam, "close", None)
        if callable(close):
            res = close()
            if asyncio.iscoroutine(res):
                await res


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("usage: songrec <subcommand> <audio_file>\n")
        sys.exit(1)

    file_path = sys.argv[-1]
    if not os.path.isfile(file_path):
        sys.stderr.write(f"file not found: {file_path}\n")
        sys.exit(1)

    try:
        data = asyncio.run(_recognize(file_path))
        if not isinstance(data, dict) or not isinstance(data.get("matches"), list):
            sys.stderr.write("inconclusive response from recognizer\n")
            sys.exit(2)
        print(json.dumps(data))
    except Exception as exc:
        sys.stderr.write(f"recognition error: {exc}\n")
        sys.exit(2)


if __name__ == "__main__":
    main()
