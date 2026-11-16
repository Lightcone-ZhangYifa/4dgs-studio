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
