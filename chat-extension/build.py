#!/usr/bin/env python
"""Pack the chat extension into dist/davinci-resolve.mcpb (a zip)."""

import json
import os
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
MEMBERS = ["manifest.json", "server/bridge.py"]


def main():
    manifest = json.load(open(os.path.join(ROOT, "manifest.json"), encoding="utf-8"))
    dist = os.path.join(ROOT, "dist")
    os.makedirs(dist, exist_ok=True)
    out = os.path.join(dist, "davinci-resolve.mcpb")

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for member in MEMBERS:
            src = os.path.join(ROOT, member.replace("/", os.sep))
            if not os.path.isfile(src):
                raise SystemExit("missing: %s" % src)
            z.write(src, member)  # forward slashes, manifest at the archive root

    print("%s %s -> %s (%d bytes)"
          % (manifest["name"], manifest["version"], out, os.path.getsize(out)))


if __name__ == "__main__":
    main()
