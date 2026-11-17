# Contributing

Please describe the problem your change solves and how to reproduce it. For changes to a GSV object or encoding, update the contract in `docs/gsv/` and include an example that exercises the affected reader and writer.

## Python SDK

From the repository root, install the package and run the tests in a virtual environment.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ./packages/gsv
python -I -m unittest discover -s packages/gsv/tests -v
```

## Studio

On an Apple silicon Mac with Xcode Command Line Tools installed, run:

```sh
bash viewer/build_native.sh
open 'viewer/dist/4DGS Studio.app'
```

The build checks the English and Simplified Chinese catalogs and verifies the signed app bundle. For changes to picking, rendering or playback, include the scene size, viewport resolution, hardware and steps used to verify the change in the app. Use a small synthetic scene when reporting a problem with private data.

Run the native CPU checks with `python3 viewer/tools/test_native.py --suite cpu`.

## Release packages

On an Apple silicon Mac, run `python3 viewer/tools/package_app.py` to build and verify the signed application ZIP. In a Python environment with `build` installed, run `python tools/build_sdk.py dist` to create the GSV source and wheel distributions. The wheel is built from the source archive, whose tar entries use neutral ownership metadata.

## Ubuntu Desktop

See [the Ubuntu guide](viewer/ubuntu/README.md) for distro dependencies, launch and
Xvfb/Mesa acceptance commands. Keep shared format, encoding and metric semantics in
`packages/gsv`; keep platform widgets and GPU APIs in their own viewer directories.
Both platforms consume the same `viewer/Resources/*/Localizable.strings` catalogs.
Changes to interaction or camera authoring should update the feature inventory in
`viewer/ubuntu/PARITY.md`, reuse contract fixtures across frontends, and include
behavioral tests for source immutability, exact timestamps and stale work disposal.
Validate the Debian package with `python3 viewer/tools/package_ubuntu.py`.

Run `python3 viewer/tools/audit_studio_features.py` when changing a desktop
operation. The reviewed mapping in `viewer/ubuntu/feature-map.json` covers every
macOS `@objc` action in the app/workspace and Studio controllers. Adding or
removing an action fails CI until its Ubuntu entry and behavior are reviewed.
This is a completeness check, not a functional test. Keep executable behavior
evidence in `FEATURE_AUDIT.md`, shared mathematical fixtures and the relevant
unit/GPU tests. Repeat the opted-in `--full` GNOME runner on the committed source
for release evidence; preserve its reports, artifact hashes, renderer and timing
boundaries instead of treating widget tests as physical desktop acceptance.
