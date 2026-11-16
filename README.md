# 4DGS Studio

4DGS Studio is a desktop viewer for dynamic Gaussian scenes. Open a GSV file to inspect a Gaussian over time, follow a calibrated camera, or compare a camera render with its recorded reference image. The Python package reads and writes the same files and can run independently of Studio.

## Open a scene

Download the Apple silicon application from [GitHub Releases](https://github.com/Lightcone-ZhangYifa/4dgs-studio/releases). Unzip it, open **4DGS Studio** and press **⌘O** to choose a `.gsv` file. Try `colored-ring.gsv` from the same release to explore trajectories and two moving cameras. The app runs offline on macOS 13 or later. Release ZIPs use an ad-hoc signature. If your Mac requires a notarized application, use the source build below.

To create a small scene with moving Gaussians, an animated camera and reference images, run these commands from a clone of this repository with Python 3.10 or later.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ./packages/gsv
python packages/gsv/examples/write_scene.py example.gsv
gsv validate example.gsv
```

Select a Gaussian object to see its colored motion paths, or click a single Gaussian to follow its identity. The preview controls adjust path density, opacity and trail length. Selecting a camera reveals its path and calibration. A file with a render view and matching RGB observations also supports a side-by-side image comparison and metrics. Camera animations you create can be saved as a separate GSV file.

## Ubuntu Desktop

A native GTK 4 / libadwaita and OpenGL frontend is available on the Ubuntu development branch. See the [Ubuntu guide](viewer/ubuntu/README.md) for Ubuntu 24.04+ dependencies, launching, Debian packaging, tests, and the [acceptance inventory](viewer/ubuntu/PARITY.md). Hardware GPU and Wayland release acceptance is still required.

```sh
bash viewer/build_ubuntu.sh
```

## Build on macOS

With Xcode Command Line Tools installed on an Apple silicon Mac, run:

```sh
bash viewer/build_native.sh
open "viewer/dist/4DGS Studio.app"
```

| Component | Documentation |
| --- | --- |
| Ubuntu GTK 4 / OpenGL viewer | [Ubuntu guide](viewer/ubuntu/README.md) |
| Native AppKit and Metal viewer | [Studio guide](viewer/README.md) |
| NumPy-based reader, writer, codecs and CLI | [Python API and examples](packages/gsv/README.md) |
| File layout, object types, time, cameras and render views | [GSV specification](docs/gsv/README.md) |
| Local tests and contributions | [Contributing](CONTRIBUTING.md) |

Created by [Yifa Zhang · 张亦法](https://github.com/Lightcone-ZhangYifa). Released under the [MIT License](LICENSE).
