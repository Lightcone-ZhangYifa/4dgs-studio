# Desktop validation: scope and retained reports

Runtime source is frozen at `ec35705`; `6583689` is the final input-driver
revision. The checkout was clean when final-run preparation began. The report,
not a manually edited table, determines the exact source and working-tree state
of each run. Do not infer acceptance from an earlier run or from completed
partial groups.

## Final report status

The final source reports are retained at
[validation/2026-11-16/full-wayland.json](validation/2026-11-16/full-wayland.json) and
[validation/2026-11-16/full-xwayland.json](validation/2026-11-16/full-xwayland.json).
Selected application logs and the JSON reports are committed alongside this
document. Local generated GSVs, PNGs and complete logs are retained under
`viewer/dist/desktop-validation/6583689/` and excluded from Git. Both source
runs and the extracted Debian payload checks are verified below.

| Run | Final report / source | Complete requested scope | Result and clean-tree check | Cleanup / source integrity |
| --- | --- | --- | --- | --- |
| Checkout, Wayland, native EIS/portals | `full-wayland.json`; `65836898384dee6f0982540e336a1c405feb1a51` | Eight basic steps + 24 extended checks: parity 8, timeline 6, navigation 3, comparison 3, lifecycle 4 | **PASS**; `passed: true`, working-tree changes `[]` | Source unchanged; monitor 1→1; no cleanup error; timeline/navigation restored |
| Checkout, NVIDIA Xwayland, native EIS/portals | `full-xwayland.json`; `65836898384dee6f0982540e336a1c405feb1a51` | Eight basic steps + the same 24 extended checks | **PASS**; `passed: true`, working-tree changes `[]` | Source unchanged; monitor 1→1; no cleanup error; timeline/navigation restored |
| Extracted `.deb`, actual Xwayland windows | `packaged-runtime.json`; clean `6583689` | 31 application tests; chooser completion mocked | **PASS**, 31/31; 15.340 s process elapsed | Shared run monitor 1→1; no remaining Studio owner; no input session |
| Extracted `.deb`, actual Wayland windows | `packaged-runtime.json`; clean `6583689` | 31 application tests; chooser completion mocked | **PASS**, 31/31; 13.786 s process elapsed | Shared run monitor 1→1; no remaining Studio owner; no input session |
| Extracted `.deb`, fresh Chinese Wayland process | Same package report | Saved-language precedence, Chinese tabs and native-portal workspace save | **PASS**; `zh-Hans` over `LANGUAGE=en_US`; PNG 3288×2084 | Source unchanged; no keyboard/pointer injection; same cleanup |

Wayland full03 completed on 2026-11-16 from the clean source above. Its report
was inspected at `/tmp/gsv-parity-full-wayland-03/report.json`: every requested
check passed, native Mutter EIS orbit changed exported camera poses, camera
export/import roundtrip passed through native portals, and both timeline and
navigation restoration passed without errors. The owned source SHA256 remained
`5a70cc062e17e5676e4103299ec11d967023c964af436fe689b9911af34aba89`.
All recorded artifact sizes/hashes were checked. Workspace PNG was 3288×2084;
raw preview was 640×480 and the comparison pair 1280×480. Matched-reference
metrics were PSNR 5.15735, SSIM 0.00715091, MSE 0.304975 and MAE 0.473774; these
fixture values are workflow evidence, not a renderer-quality benchmark.

The run used the existing output (`temporary_virtual_output: false`), preserving
logical-monitor count 1→1. Actual libraries were Python 3.12.3, Ubuntu PyOpenGL
3.1.7, NumPy 1.26.4, GTK 4.14.5 and libadwaita 1.5.0. Its Mesa 25.2.8 llvmpipe
OpenGL 4.5 context used software rendering and CPU sorting. No Wayland hardware
performance conclusion follows.

Xwayland full33 also passed at the same clean source; its report was inspected
at `/tmp/gsv-parity-full-x11-33/report.json`. All eight basic steps, 24 extended
checks and both restorations passed; all 49 recorded artifact sizes and hashes
were checked independently for each run. Xwayland used NVIDIA RTX 4090 OpenGL
4.6, driver 620.70.06, with the same library versions above. It preserved source
hashes and monitor count 1→1, without cleanup errors. In each run, camera
export/import/export was byte-identical. Both exported the same 1280×480
comparison PNG SHA256:
`33e90a717dee420ba30d10b2b2acd99f88c43535e19f2d9d74e0ff0055680378`.
Workspace output was 3288×2084 on both protocols. Image equality here concerns
the calibrated CPU reference export, not identical Metal/OpenGL viewport pixels.

Native GTK/GDK diagnostics are retained in the complete-run application logs.
Wayland full03 logged two `Gtk-CRITICAL` `gtk_window_get_focus` assertions;
Xwayland full33 logged the same two plus two `Gdk-CRITICAL`
`gdk_monitor_get_geometry` assertions. Neither log contained a Python traceback.
Their origin has not been established by a backtrace. Passing functional and
artifact assertions does not establish a warning-free runtime; these diagnostics
do not change the recorded check, source-integrity or cleanup results. The older
`8d54a00` passive window-test logs below are separate historical evidence.

The detached Debian payload report was inspected at
`viewer/dist/desktop-validation/6583689/packaged-runtime/report.json` and is
retained as [packaged-runtime.json](validation/2026-11-16/packaged-runtime.json).
Its source was clean `6583689`. Both the app and Python SDK imported from
`package-root/usr/share/4dgs-studio/ubuntu`, using Ubuntu PyOpenGL 3.1.7. Each
backend passed all 31 application tests; chooser completion was mocked in these
unit tests. Both application logs contained zero `CRITICAL` and zero Python
traceback entries; resource warnings remain, so this is not a zero-warning claim.
This contrasts with the separate EIS full-run diagnostics above without
establishing their cause. Tests exercised an extracted payload, not a system-wide
`dpkg` installation.

A separate normal packaged Wayland process loaded an isolated preference fixture
(`language: zh-Hans`, `appearance: 2`) despite `LANGUAGE=en_US`. Its log selected
`zh-Hans` and real AT-SPI exposed `选中内容`, `视口`, `文档`. The actual native
Save portal produced `中文工作区.png` at 3288×2084, preserving the source. No EIS
session or synthetic keyboard/pointer input was used. GUI preference writes were
already exercised by the full lifecycle check; this is specifically a fresh
process preference/restart check, not all 24 groups repeated in Chinese.
Package validation preserved monitor count 1→1 and left no Studio bus owner.

[Package provenance](validation/2026-11-16/package-provenance.json) records the
tested package and runtime tree. The runtime-tree SHA256 was
`44d31b52735d0ec89d3770aec7bf2cfe262f74255167e217e80ce4f220bd0330`.
A documentation-only package rebuild can change the `.deb` hash; retain the
original tested manifest and check runtime-tree identity rather than assigning
the old package hash to a rebuilt artifact.

For a full source run, acceptance requires `report.passed`, exact clean source,
the complete requested group set, every result, source hashes and cleanup. The
group set is `parity`, `timeline`, `navigation`, `comparison`, `lifecycle`; an
intentionally restricted `--full-checks` run cannot establish the whole scope.

## Evidence categories

| Verification path | Recorded result | What it establishes |
| --- | --- | --- |
| Ubuntu automated regression | 245 tests: 243 passed, two benchmark tests skipped | Numerical/model/controller/GTK/OpenGL regressions; display tests use real GTK allocations and Mesa as configured |
| Python SDK suite | 78 passed | Python container/evaluator/camera/render contracts; macOS still uses its independent Swift/Metal implementation |
| Timeline/range checks | Seven tests and real AT-SPI Value probe passed | AccessibleRange native time, finite/bounds/insensitive checks and exact setting; not capture FPS or a frame-index substitute |
| Workspace snapshots | 14 tests passed; actual PNG 3288×2084 for logical 1644×1042 at scale 2 | Backing-scale native controls/colors; completed GL framebuffer and comparison source rasters remain unchanged |
| Inspector width | One mapped layout regression and an actual Wayland probe passed | Divider resizing respects natural minimum width, keeping camera controls reachable |
| Extracted Debian payload | 31/31 per backend at clean `6583689` | Real GNOME/GTK/OpenGL with app and SDK imported from packaged paths; chooser completion mocked |
| Packaged Chinese restart | Fresh Wayland process, Chinese tabs and actual portal PNG | Preference fixture honored over environment; no keyboard/pointer injection, no claim of complete Chinese full-run coverage |
| Earlier checkout real-window run | 31/31 Xwayland and 31/31 Wayland at `8d54a00` | Historical real compositor, GTK and OpenGL; chooser completion mocked, no portal/input claim |

The earlier `8d54a00` real-window run used the existing 3420×2148 scale-2 GNOME
output, retained logical-monitor count 1→1, exited all owned children and left no
Studio application-ID owner. Xwayland reported RTX 4090, NVIDIA 620.70.06,
OpenGL 4.6; Wayland reported Mesa 25.2.8 llvmpipe, OpenGL 4.5. Both used Ubuntu
PyOpenGL 3.1.7, Python 3.12.3, NumPy 1.26.4, GTK 4.14.5 and libadwaita 1.5.0.
The probe/application logs had no `Traceback` or `CRITICAL` entries; Wayland still
reported Mesa device warnings and used software rendering. The final retained
reports must record their own actual driver and library versions.

The native desktop runner uses the normal application, owned-window AT-SPI
controls, native portals and session-authorized Mutter EIS input. It rejects
Xvfb and does not mock chooser completion. `test_application.py` is a different
path: its real GTK/GL workflows **do mock chooser completion**, even when the
windows run on the real compositor or from the extracted Debian payload. Never
combine those scopes into one claim of portal acceptance.

## Current check IDs and future reruns

| Group | Report field | Verified on Wayland and Xwayland |
| --- | --- | --- |
| Basic open/play/author/save/import/comparison | `steps` | All eight passed, including native EIS orbit and native portal roundtrip/exports |
| Inspector, transport, range, views, camera fields, exact ID, preview, panels | `full_checks` | All eight passed |
| Focus; snap/Alt scrub; timeline keys; wheel/pan; key-drag cancel/retime | `timeline_checks` | All six passed; restoration passed with no errors |
| Viewport 1/3/7 keys | `navigation_key_checks` | All three passed; restoration passed with no errors |
| Selected render/observation, image inspection, native raster exports | `comparison_checks` | All three passed; native raw raster sizes and export hash invariance verified |
| Open cancel/failure, dirty-close cancel, isolated preferences | `lifecycle_checks` | All four passed; appearance changed live, language selection persisted; separate packaged Chinese restart/native-save check passed |

The six timeline IDs passing on both protocols are `owned-physical-timeline-focus`,
`physical-scrub-near-tick-snap-and-alt-bypass`,
`focused-home-left-right-space-keyboard`,
`physical-wheel-zoom-shift-wheel-and-middle-pan-home-fit`,
`selected-key-physical-drag-escape-cancel` and
`selected-key-physical-retime-and-native-undo`. “Physical” is the runner's ID
vocabulary for events delivered through the native input path; input is synthetic
Mutter EIS delivery to the owned app. It is **not** a claim that a human-operated
physical touchpad was tested. Group restoration/cleanup fields were inspected,
not just successful rows.

## Renderer performance has a separate scope

The `8d54a00` normal GtkGLArea RTX 4090/Xwayland benchmark at 1280×720 used one
warm-up and 11 measured resident frames. One million Gaussians measured orbit
median/P95 6.826/7.673 ms and prepared playback 7.014/7.079 ms, using `glFinish`
for completion. These exclude source reads and first upload, and are not desktop
presentation FPS, physical-input latency or the EGL source-streaming workload.
[PARITY.md](PARITY.md) retains the complete counts, CPU submission and pick data.

## Historical clean-source basic reports

Commit `599ca1c0be9ddeec372795bbbdd14ae499294005` passed the historical basic
workflow, with empty working-tree lists, on Wayland/llvmpipe and
Xwayland/RTX 4090. The checked-in reports remain
[Wayland](validation/2026-11-16/wayland.json) and
[Xwayland](validation/2026-11-16/xwayland.json). They verified native open/save portals,
playback, field/path selection, two-key camera roundtrip, workspace and
1280×480 calibrated-pair exports, source hashes and owned-process cleanup.
Xwayland additionally checked a real XTEST middle drag. The disconnected session
used one temporary 1920×1080 Mutter output and restored monitor count 1→0.
Artifacts remain under `viewer/dist/desktop-validation/{wayland,xwayland}/`.
These reports do not automatically validate the expanded current EIS workflow.

## Reproduce and remaining boundaries

From the real GNOME session, with AT-SPI, `libei.so.1` and the application's
runtime dependencies, use a fresh output directory and exclusive owned-window
input access:

```sh
/usr/bin/python3 viewer/ubuntu/tools/test_desktop_session.py \
  --allow-desktop-input --backend wayland --input-backend eis --full \
  --python /usr/bin/python3 --output /tmp/gsv-full-wayland-new

/usr/bin/python3 viewer/ubuntu/tools/test_desktop_session.py \
  --allow-desktop-input --backend x11 --prime --input-backend eis --full \
  --python /usr/bin/python3 --output /tmp/gsv-full-xwayland-new
```

Use the actual local Xwayland `DISPLAY`/`XAUTHORITY`; the runner changes GPU
selection only for its process. `--virtual-output` applies only when the GNOME
session has no active output; its GStreamer/PipeWire path is session-owned and
must restore the initial output count. Existing user work is not attached by
default. Only owned application exports are saved, not other desktop content.

The 80-action source map is a review guard, not runtime proof of every operation.
No result here establishes macOS application-to-application camera exchange,
Mac Retina hardware pixel equivalence, physical touchpad gestures, complete
screen-reader use, Intel/AMD coverage or hardware-accelerated Wayland. They remain
explicit release boundaries even if the final two native `--full` reports pass.
