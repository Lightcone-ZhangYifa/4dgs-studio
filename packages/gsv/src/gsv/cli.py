"""Command-line tools for GSV scene containers."""

import argparse
import json
from pathlib import Path

from .codecs import add_camera
from .container import GSVError, Scene, Writer


def write_cameras(source, output):
    data = json.loads(Path(source).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("cameras"), list):
        raise GSVError("Expected an object with a cameras array")
    with Writer(output, metadata=data.get("metadata", {})) as writer:
        for field in ("spaces", "clocks"):
            if field in data:
                if not isinstance(data[field], dict):
                    raise GSVError(f"{field} must be a dictionary")
                writer.manifest[field] = data[field]
        for index, camera in enumerate(data["cameras"]):
            if not isinstance(camera, dict):
                raise GSVError(f"cameras[{index}] must be an object")
            try:
                add_camera(writer, **camera)
            except (TypeError, ValueError) as error:
                raise GSVError(f"cameras[{index}]: {error}") from error


def main():
    parser = argparse.ArgumentParser(description="Inspect and validate GSV scenes")
    commands = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("info", "List scene objects and storage counts"),
        ("validate", "Verify scene data and check object contracts"),
    ):
        commands.add_parser(command, help=help_text).add_argument("path")
    cameras = commands.add_parser(
        "camera-from-json", help="Write a scene from camera calibration JSON"
    )
    cameras.add_argument("source")
    cameras.add_argument("output")
    args = parser.parse_args()

    try:
        if args.command == "camera-from-json":
            write_cameras(args.source, args.output)
            return
        with Scene(args.path) as scene:
            if args.command == "validate":
                result = scene.validate()
            else:
                fields = ("id", "label", "type", "encoding", "space", "clock")
                result = {
                    "path": str(scene.path),
                    "objects": [
                        {k: obj.get(k) for k in fields} for obj in scene.objects
                    ],
                    "arrays": len(scene.manifest["arrays"]),
                    "buffers": len(scene.manifest["buffers"]),
                }
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (OSError, ValueError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")


if __name__ == "__main__":
    main()
