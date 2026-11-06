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
