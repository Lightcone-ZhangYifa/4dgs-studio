#!/usr/bin/env python3
"""Run the native regression suite with temporary synthetic inputs."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile

VIEWER = Path(__file__).resolve().parents[1]
ROOT = VIEWER.parent
NATIVE = VIEWER / "native"
TESTS = NATIVE / "tests"
RENDERER = ["Navigation.swift", "GaussianRenderer.swift", "TrajectoryRendering.swift",
            "CameraPicking.swift"]


def run(*args, env=None):
    subprocess.run(args, check=True, cwd=ROOT, env=env, timeout=300)


def probe(work, name, sources, *arguments, metal=False):
    contents = work / f"{name}.app/Contents"
    binary = contents / "MacOS" / name
    binary.parent.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleExecutable": name, "CFBundleIdentifier": "org.gsv.tests." + name,
        "CFBundlePackageType": "APPL",
    }))
    if metal:
        resources = contents / "Resources"
        resources.mkdir()
        shutil.copyfile(NATIVE / "Shaders.metal", resources / "Shaders.metal")
        shutil.copytree(VIEWER / "Resources/en.lproj", resources / "en.lproj")
    run("xcrun", "swiftc", "-swift-version", "5", "-D", "STUDIO_TESTING", "-O",
        *(NATIVE / p for p in sources), TESTS / f"{name}.swift", "-o", binary)
    env = dict(os.environ)
    if metal:
        env["MTL_DEBUG_LAYER"] = "1"
    report_path = work / f"{name}.json"
    run(binary, *arguments, report_path, "-StudioLanguage", "en", env=env)
    report = json.loads(report_path.read_text())
    if report.get("passed") is not True:
        raise RuntimeError(f"{name} failed")


def cpu_suite(work):
    run(sys.executable, VIEWER / "tools/audit_localization.py")
    probe(work, "TimelineWindowAcceptance", ["StudioTimeline.swift", "Localization.swift"])
    probe(work, "SceneIOLifecycleAcceptance", ["SceneIOLifecycle.swift"])


def metal_suite(work):
    from importlib import import_module
    sys.path.insert(0, str(TESTS))
    fixtures = import_module("camera_render_fixtures")
    geometry_dir = work / "geometry"
    run(sys.executable, TESTS / "make_trajectory_fixtures.py", geometry_dir)
    probe(work, "TrajectoryExtractionProbe", [
        "GSVDocument.swift", "StreamingScene.swift", "CompositeScene.swift",
        "TrajectoryExtraction.swift", "TrajectoryRendering.swift", "Localization.swift"],
        geometry_dir / "trajectory_contract.gsv", metal=True)
    probe(work, "TrajectoryRendererAcceptance", RENDERER, metal=True)
    probe(work, "CameraPickingAcceptance", RENDERER, metal=True)
    probe(work, "RendererAllocationAcceptance", RENDERER, metal=True)
    run(sys.executable, TESTS / "check_metal_geometry.py", "--report", work / "geometry.json",
        env={**os.environ, "MTL_DEBUG_LAYER": "1"})
    cameras = work / "cameras"
    fixtures.generate(cameras)
    fixtures.write_clock_domains(cameras / "domains.gsv")
    sources = sorted(p.name for p in NATIVE.glob("*.swift") if p.name != "App.swift")
    probe(work, "CameraRenderContractProbe", sources, cameras, metal=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=["cpu", "metal"], required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="studio-tests-") as work:
        (cpu_suite if args.suite == "cpu" else metal_suite)(Path(work))
    print(f"{args.suite} suite passed")


if __name__ == "__main__":
    main()
