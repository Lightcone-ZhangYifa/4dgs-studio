# 4DGS Studio for macOS

Open a GSV with **⌘O** or drag it into the viewport. The Scene Outliner groups
Gaussian fields, cameras, motion scaffolds, render views and observations. Select
an object to see its encoding, clock, coordinate space and properties. Source
reconstructions open read-only.

## Build and launch

Use an Apple silicon Mac running macOS 13 or later, with Xcode Command Line
Tools and Python 3. Run these commands from the repository root:

```sh
bash viewer/build_native.sh
open "viewer/dist/4DGS Studio.app"
```

The app uses AppKit and Metal and runs offline. The source build verifies its
local signature before replacing an existing app. To create the arm64 release
ZIP, run `python3 viewer/tools/package_app.py`. The script checks the bundle
contents, signature, archive integrity and executable permissions.

## Selection and motion

Click a Gaussian in the viewport or search for its signed 64-bit identity in
the inspector. **⌘-click** toggles individual elements and objects in a mixed
selection. The primary member determines which properties the inspector shows.
The **F** key frames the selection.

Selecting a Gaussian field shows its colored trajectories. Picking a single
Gaussian highlights it with an amber marker and displays its path. Selecting a
camera highlights its wireframe and adds its motion path. To look through a
camera, double-click its Outliner row or use the viewport camera menu.

The strip below the viewport controls pixel resolution, the grid, camera
wireframes, paths and recent trails. Full paths start at 18% opacity. Trails
cover the previous eight source intervals and update with the displayed sample.
Density defaults to 100%. Lowering it reduces the number of field paths while
preserving the trajectories of individually selected Gaussians. Source validity
gaps split a trajectory.

## Navigation

| Gesture in the viewport | Action |
| --- | --- |
| Option + left drag, or middle drag | Orbit the current center |
| Shift + middle drag, Shift + left drag, or right drag | Pan |
| Control + left drag, or mouse wheel | Dolly |
| Two-finger scroll / Option + two-finger scroll | Pan / orbit |
| Pinch / rotate gesture | Dolly / camera roll |
| F / Home | Frame selection / frame scene |
| Space / left or right arrow | Play or pause / step a source sample |

The amber cross marks the orbit center. Selecting a primitive sets the center
to its position. The Viewport inspector can also center navigation on visible
scene depth. Choose Turntable to preserve the navigation up axis or Trackball
for image-plane rotation. Previous View and Next View recall earlier viewpoints.

## Time and camera animation

The timeline displays native timestamps in the document's clock units. Drag the
ruler to scrub, scroll to zoom and middle-drag to pan. The transport can loop a
defined range, snap to source samples or step one sample at a time.

Choose **Camera → Create Camera from View**. Move the viewport camera, position
the playhead and press **⌘K** to record a key. Click a keyframe to inspect it,
drag its diamond to retime it or use the camera inspector to edit its pose and
field of view. Camera Undo and Redo operate on authored camera edits.

**Save Camera Animation…** writes a separate GSV linked to the source document's
manifest digest. To edit it again, open the matching source scene and choose
**Import Camera Animation…**.

## Render and reference images

Choose a render view, then **Render / GT** in the preview strip. When a view has
matching camera observations, Studio shows the calibrated render on the left
and the source image on the right. Both panes share zoom and pan. The inspector
computes PSNR, SSIM, MSE and MAE on Float32 pixels at the recorded resolution.
Selecting a different time or camera invalidates the previous scores. A view
without a reference image supports a single calibrated preview.

Comparison uses the explicit geometry, projection, render profile and output
associated with the selected view. It requires matching image and geometry
times and compatible color/coordinate domains. Distorted cameras, shutter
integration and transformed output rasters are available for inspection only.
The comparison size limit is 16 megapixels and 16,384 pixels per axis.

## Representation support

Studio directly renders static Gaussian fields, identity-persistent Gaussian
trajectories and scaffold glyphs. Other registered representations can carry a
linked, evaluated display field with its own timestamps and source provenance.
The inspector exposes their native objects and relations separately. The
[format registry](../docs/gsv/ENCODINGS.md) documents each encoding.

The Metal renderer projects anisotropic covariance, sorts all visible splats
by center depth and blends truncated Gaussian footprints. Occlusion for path
overlays uses a center-depth surface, and translucent paths use weighted
blending. Image scores describe this renderer and its supported appearance.
Pixel resolution and path density can be adjusted independently for large scenes.

Choose **4DGS Studio → Language** for English, Simplified Chinese or the system
preference. Changes apply at the next launch. Source object names, identities
and timestamps retain their document values.

## Native checks

```sh
python3 viewer/tools/test_native.py --suite cpu
python3 -m pip install ./packages/gsv
python3 viewer/tools/test_native.py --suite metal
```

The CPU suite runs timeline, asynchronous I/O and localization checks. The Metal
suite requires a local Apple GPU. It generates scenes in a temporary directory
and checks identity paths, camera picking, allocation rollback, calibrated
image comparison and analytical image projections. All test sources and
fixtures are independent of local research datasets.
