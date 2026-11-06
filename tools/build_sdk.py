#!/usr/bin/env python3
"""Build the GSV source distribution and wheel without host account metadata."""
import argparse
import copy
import gzip
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

PACKAGE = Path(__file__).resolve().parents[1] / "packages" / "gsv"


def normalized_sdist(source, output, unpack, epoch):
    with tarfile.open(source, "r:gz") as original, output.open("wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=epoch) as gz:
            with tarfile.open(fileobj=gz, mode="w|") as clean:
                for member in original:
                    if (not (member.isfile() or member.isdir())
                            or Path(member.name).is_absolute() or ".." in Path(member.name).parts):
                        raise ValueError(f"Unexpected sdist entry: {member.name}")
                    entry = copy.copy(member)
                    entry.uid = entry.gid = 0
                    entry.uname = entry.gname = ""
                    entry.mtime = epoch
                    entry.pax_headers = {}
                    clean.addfile(entry, original.extractfile(member) if member.isfile() else None)
    # All entries were checked above. Build the wheel from exactly the shipped archive.
    with tarfile.open(output, "r:gz") as clean:
        clean.extractall(unpack, members=clean.getmembers())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        src = work / "source"
        shutil.copytree(PACKAGE, src, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "*.egg-info", "build", "dist", ".DS_Store"))
        raw = work / "raw"
        subprocess.run([sys.executable, "-m", "build", "--sdist", str(src),
                        "--outdir", str(raw)], check=True)
        archive, = raw.glob("*.tar.gz")
        dist = work / "dist"
        dist.mkdir()
        unpack = work / "unpacked"
        normalized_sdist(archive, dist / archive.name, unpack, epoch)
        project, = unpack.iterdir()
        subprocess.run([sys.executable, "-m", "build", "--wheel", str(project),
                        "--outdir", str(dist)], check=True)
        wheel, = dist.glob("*.whl")
        for artifact in (dist / archive.name, wheel):
            shutil.copyfile(artifact, destination / artifact.name)
            print(destination / artifact.name)


if __name__ == "__main__":
    main()
