# Production GSV validation and optimization

The 2026-11-16 production run uses existing training exports from the local
`/data2` corpus. It does not generate Gaussian benchmark data. A targeted
metadata inventory found 22 uncompressed containers and 1,257 compressed
archives, excluding two AppleDouble files; this was not an exhaustive scan of
every directory on the volume.

Four representative persistent-identity scenes contain 160,000, 640,000,
1,280,000 and 2,560,000 stored identities, each with 36 native samples and a
51-view camera rig. Container sizes are respectively 1,234,229,797,
2,720,999,559, 9,864,620,207 and 19,728,279,829 bytes. The 160k accepted archive
was losslessly extracted into the ignored validation directory and its entire
decompressed SHA256 matched the archive sidecar. The other three containers
were opened directly. Production sources were never rewritten or chmodded.

Local evidence, images, source paths and camera arrays remain under
`viewer/dist/production-validation/`, excluded from Git. Retained summaries use
dataset aliases and manifest digests without publishing production images or
geometry. These scenes exercise the declared packed RGB preview; linked full
SH tensors remain intact. This is not a directional-SH quality certification.

## Measured host-side repairs

Before product source is fixed at `76f9545`; endpoint retention was committed as
`adc4895`, and batched camera calibration as `bf7a410`. Final I/O measurements
identify the latter commit and exact model/camera source hashes. Each scene was
loaded in a fresh process. Six intervals each have 15 warm prepares. First
application operation does not mean cold storage: neither the OS page cache nor
background training jobs were controlled. Source API read bytes are not physical
disk traffic. Loading times must not be used to infer a storage speedup.

| Stored identities | Warm prepare before, ms | Warm prepare after, ms | All 51 camera views after, ms |
| ---: | ---: | ---: | ---: |
| 160,000 | 0.0419 | 0.0241 | 1.059 |
| 640,000 | 0.1316 | 0.0219 | 1.070 |
| 1,280,000 | 0.2557 | 0.0206 | 1.035 |
| 2,560,000 | 125.188 | 0.0208 | 0.924 |

Values are medians of interval medians; camera values are medians of evaluations
at the first, middle and last native times. Before batching, all 51 views took
approximately 20 ms on each production scene. An independent old-source
reference checked 51 views at five times, including interpolation and both
outside endpoints: all 255 K/pose/near/far results had zero absolute difference,
and raster conventions and every inspection detail remained equal.

The fixed optional 256 MiB LRU previously could not retain a 2.56M-point endpoint
pair. Fifteen seeks within one interval reread 4,915,200,000 bytes; the six tested
intervals reread 29,491,200,000 bytes in total. Final warm reads are zero. One
current immutable endpoint pair is now retained independently of the optional
LRU, neighboring intervals reuse their shared source sample, and exact endpoint
and interior validity counts are computed once. This is required O(N) active
frame storage, not a 256 MiB total-process memory cap. Cancellation, source
mutation checks, owner-qualified signed identities and close/release behavior
are covered by regression tests.

For 1.28M points, adjacent interval source reads fall from 163,840,000 to
81,920,000 bytes. The CPU-only 2.56M benchmark's final RSS falls from about
750 MiB to 421 MiB. These are CPU benchmark values, not whole-application RSS or
presentation FPS. Calibration batching reads a rig's arrays and finds its time
interval once, then evaluates poses together; single-view inspection still
computes only its requested view and preserves foreign-domain inspection.

## Endpoint/calibration GPU window results

All four endpoint/calibration profiles at `bf7a410` completed the same 94-frame protocol in real GTK
windows at 1280×720 on NVIDIA RTX 4090, OpenGL 4.6 / driver 620.70.06, through
GNOME's Xwayland backend. Two large-scene before profiles also completed all
94 frames. No Gaussian source was synthesized or downsampled for these runs.

| Identities | Warm playback GPU elapsed median, ms | Queued GL completion median, ms |
| ---: | ---: | ---: |
| 160,000 | 5.673 | 10.260 |
| 640,000 | 11.075 | 18.419 |
| 1,280,000 | 31.465 | 37.951 |
| 2,560,000 | 128.667 | 138.468 |

Both GPUs were at 100% utilization in every before/after system snapshot during
the final viewport profiles. The measured GPU timeline includes shared-device
scheduling; these numbers do not establish an idle-device limit or attribute
GPU timing changes to the endpoint repair. CPU preparation in the 2.56M-point
GTK benchmark fell from 119.607 to 0.04665 ms while geometry remained unchanged.
Neither CPU-only numbers nor reciprocal GPU times are desktop presentation FPS.

For both 1.28M and 2.56M scenes, before/after 1280×720 RGBA screenshots were
byte-for-byte identical, with zero changed pixels and zero channel difference.
All five exact owner/identity picks, initial camera matrices, source manifests
and source stat signatures matched. The benchmark script hash and sampling
protocol were identical. Peak RSS in the final GL processes was approximately
348 / 567 / 874 / 1,641 MiB, including driver resources and readback.

The session originally had no output. A controlled Mutter/PipeWire virtual
output at 1920×1080 provided the current GNOME desktop; it was not Xvfb or an EGL
pbuffer. Window rendering was hardware accelerated where the actual GL strings
above establish that fact. Native Wayland software checks remain a separate
category.

These timing reports precede the NVIDIA visibility-query repair below. Their
old visible-count fields are incorrect on that driver; image/pick equality and
host/device timing measurements remain separate evidence.

The retained [I/O summary](validation/2026-11-16-production/io-summary.json),
[camera comparison](validation/2026-11-16-production/camera-numeric.json) and
[viewport summary](validation/2026-11-16-production/viewport-summary.json)
identify source revisions, manifest/code/report digests, per-stage median/p95
timings, memory, integrity and strict image/identity comparisons. Raw local
reports remain available for an audit without committing private scene data.

## Native production desktop workflows

The normal application passed seven native checks on each of the four scenes
with NVIDIA/Xwayland, and the same seven on the 160k scene with Wayland/llvmpipe:
native Open, three source-time seeks with PNG exports, bounded play/pause,
owner-qualified exact identity inspection, and camera recording/export with a
real EIS middle drag proved by changed exported poses plus a workspace snapshot.
All test applications closed normally and preserved the controlled output and
production sources. Source-code digests were unchanged during each run.

Requested playback duration was five seconds; measured windows including
start/stop overhead were 5.674 / 5.659 / 5.730 / 6.872 seconds for the four
NVIDIA runs, and 5.992 seconds for Wayland 160k. Every run observed advancing
native time and verified stable time after pause. These sampled timeline values
are not completed-render or presentation rates.

The 2.56M-point Wayland software attempt passed native Open, then an AT-SPI
GetRoleName call timed out before the first seek's screenshot action. Cleanup
completed and source/code integrity held. This run is **not accepted** as a
complete large-scene Wayland desktop validation. The evidence does not identify
a PNG-rendering failure, driver crash or exact timeout cause. Hardware Wayland
is unverified on this remote host, whose tested native Wayland context is
llvmpipe. The failure is retained alongside the five complete runs in the
[desktop summary](validation/2026-11-16-production/desktop-summary.json).

After the visibility repair, the latest `771be79` application repeated the full
seven checks on NVIDIA/Xwayland 2.56M and Wayland/llvmpipe 160k. Both passed;
playback windows were 5.774 and 6.474 seconds with stable pauses. All six seek
PNGs matched their pre-repair hashes, actual EIS orbits changed exported poses,
all seven product-module hashes remained stable, and both applications closed
normally. See the separate [corrected desktop summary](validation/2026-11-16-production/corrected-desktop-summary.json).

## Reliable visibility counting

Production source-camera views exposed a discrepancy between the GPU counter
and independently projected source geometry. On this RTX 4090 / 620.70.06
context, the old GL_PRIMITIVES_GENERATED query returned input count even for a
minimal geometry shader emitting zero vertices. Existing 10-row correctness
tests expected 3 / 2 / 0 as controls changed but returned 10. Direct capture of
the projection flags showed the expected three visible rows.

Commit `771be79` uses GL3.3 transform-feedback primitives-written queries with a
single GPU scratch buffer of four bytes per possible source primitive. Only the
availability-guarded scalar result returns to the CPU. The three-query pool and
maximum 10 Hz sampling remain bounded; buffer bindings/ranges are restored and
query failure leaves rendering available. The color/picking shaders are
unchanged. Nine actual NVIDIA correctness/state/pixel/resource tests and all
14 Mesa visibility tests pass, followed by the full Ubuntu suite: **258 tests,
256 passed and two opt-in benchmarks skipped**.

All four real sources then completed another 94-frame protocol at this exact
repair revision. Counts below use the fixed source camera at the same first
interval time; each matches a full-source, chunked Float64 CPU projection oracle
with zero count difference on these datasets. This does not imply identical
Float64/Float32 behavior at every possible future frustum boundary.

| Source identities | Frustum-visible identities | Warm playback GPU median, ms | Queued GL completion median, ms |
| ---: | ---: | ---: | ---: |
| 160,000 | 120,116 | 1.060 | 4.316 |
| 640,000 | 346,450 | 3.868 | 7.201 |
| 1,280,000 | 688,578 | 9.491 | 13.091 |
| 2,560,000 | 1,377,453 | 67.859 | 71.369 |

All four corrected screenshots and five exact picks per scene matched the
earlier endpoint/calibration runs; the two largest scenes also matched the
original before-repair images/picks. All six comparisons had zero changed RGBA
pixels. GPU utilization during these later runs varied (GPU0 20–99%, GPU1
61–100%), so lower observed GPU times do not establish a performance benefit
from changing the counter. The benchmark tool also now copies each completed
count and its time/pose/sequence metadata from the same metrics snapshot; its
script digest consequently differs from the earlier reports, while all 94 case
plans match exactly.

The [visibility repair evidence](validation/2026-11-16-production/visibility-summary.json)
retains the failure reproduction, minimal shader diagnosis, driver-specific
regressions and full-source CPU oracle. The [corrected viewport summary](validation/2026-11-16-production/corrected-viewport-summary.json)
retains all final timings and strict image/pick/integrity comparisons. Earlier
raw reports and summaries are preserved rather than silently corrected.

## Reproduction and measurement boundaries

Run from the repository root, using a Python environment with the Ubuntu
dependencies in [README.md](README.md). Every output directory must be new.
The optional benchmark PNG export uses Pillow (`python3-pil`); the input driver
also needs the existing AT-SPI/Mutter EIS test dependencies and a GNOME session.

```sh
python viewer/ubuntu/tools/benchmark_production_io.py \
  --scene /path/to/production.gsv --output /tmp/new-production-io

python viewer/ubuntu/tools/benchmark_production_viewport.py \
  --scene /path/to/production.gsv --output /tmp/new-production-viewport \
  --backend x11 --camera first --width 1280 --height 720 \
  --samples 12 --warmup 2 --intervals 4 --picks 5 \
  --require-hardware --stat-only-integrity --snapshot

python viewer/ubuntu/tools/test_production_desktop.py \
  --allow-desktop-input --scene /path/to/production.gsv \
  --output /tmp/new-production-desktop --backend x11 --prime
```

The GL benchmark uses a real GTK window and the normal production GLArea render
callback. Completion fences and native GPU queries separate source preparation,
CPU submission and completed GL work. Serial frame latency includes main-loop
scheduling and excludes later compositor presentation. Its navigation calls are
programmatic; actual native input is tested separately by the desktop driver.
GPU timing on this machine is shared with ongoing training jobs and can include
scheduling effects. Full GPU utilization prevents a claim of an idle-device
benchmark or a guaranteed 60 FPS desktop.

The desktop driver launches the normal Studio process, uses public AT-SPI and
native dialogs, and sends native Mutter EIS input. It refuses an existing Studio
instance. Time publication is distinct from GPU completion; a completed PNG is
only an upper latency bound including automation, portal interaction, readback,
encoding and I/O. A camera export must prove the actual pointer orbit changed
the recorded pose. Optional virtual output creation requires an explicit flag
and is allowed only when no output already exists.

Whole-source hashes are optional for large raw containers. Stat-only runs record
device/inode/size/mtime/ctime before and after, while the normal document loader
retains supported semantic validation and accessed-buffer checksum verification.
Never replace a production file with a benchmark fixture or flush system caches.

Earlier complete synthetic parity validation remains historical evidence in
[DESKTOP_VALIDATION.md](DESKTOP_VALIDATION.md). Production performance results do
not replace its feature-specific comparison, lifecycle and editing checks.
