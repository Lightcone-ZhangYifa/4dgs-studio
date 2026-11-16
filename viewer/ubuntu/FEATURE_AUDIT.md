# macOS feature audit for the Ubuntu desktop frontend

Audit date: 2026-11-16. This is a source-to-operation inventory, with evidence
identified separately from implementation. It supplements [PARITY.md](PARITY.md)
and does not claim identical AppKit/GTK decoration, identical Metal/OpenGL pixels,
or a completed macOS-to-Ubuntu application exchange.

## Evidence and scope

The principal sources reviewed are `viewer/native/StudioComparison.swift`,
`StudioTimeline.swift`, `StudioPreviewControls.swift`, `StudioSnapshot.swift`,
`GSVObservation.swift` and `GSVRender.swift`. Menu and inspector entry points were
cross-checked against `App.swift`, `StudioActions.swift` and
`StudioInspector.swift`. Ubuntu paths below are relative to `viewer/ubuntu`.

**Implemented** means the entry point and behavior exist in the current source.
**Automated** means an assertion exists in the named test; it does not imply
physical input delivery. The audit-specific batch actually executed 23 comparison,
12 mapped GTK inspection-control and 4 extreme-clock timeline tests successfully.
The other test names below identify existing coverage, not a new execution claim.
GTK widget tests use Xvfb and real GTK allocations; the application tests substitute
file chooser completion with a selected local path.

**Desktop ID** refers to `steps` or `full_checks[].id` in the JSON report from
[tools/test_desktop_session.py](tools/test_desktop_session.py) and
[tools/desktop_parity.py](tools/desktop_parity.py). The latter runs with `--full`.
This document records the available checks, **not their execution status**. A
current actual GNOME report with `passed: true` is required before marking a
desktop row accepted. These scripts use the normal application, AT-SPI actions,
native dialogs and exported artifacts; they reject Xvfb. Rows marked “uncovered”
still need a dedicated desktop gesture or assertion.

Test names in the tables are in `tests/`. Method names identify production code,
not test-only application interfaces.

## Menus, workbench and source ownership

| macOS source and user operation | Ubuntu entry and behavior | Automated assertions | Desktop ID / remaining evidence |
| --- | --- | --- | --- |
| `App`, `StudioActions`: Open Scene; cancel loading; failed or superseded open | File menu/open button; `application.open_path`, asynchronous validated read-only `SceneDocument`; cancellation keeps the accepted document | `test_application.test_cancelled_load_disposes_then_replacement_uses_new_scene`, `test_failed_open_keeps_document_and_close_cancels_capture` | `native-open-dialog`; cancel/failure/replacement interactions uncovered |
| `StudioActions`: object selection and native-time playback | Outliner and viewport callbacks; first/previous/next/last, play/pause and Time field; source Float64 times retained | `test_application.test_scene_selection_playback_camera_animation_and_comparison`, `test_static_source_transport_includes_authored_camera_duration` | `outliner-field-selection-and-path-request`, `play-pause`, `all-sample-transport-commands` |
| `StudioInspector`: Selection / Viewport / Document pages | Three native notebook pages; contextual selection controls and document/domain rows | `test_application.test_inspector_context_key_selector_and_comparison_controls` | `selection-viewport-document-inspector` |
| `StudioInspector`: exact identity, mixed selection and primary member; Copy attributes | Exact signed-ID entry, native primary-member dropdown, owner-qualified identity; clipboard action | `test_application.test_mixed_primary_member_keeps_duplicate_ids_bound_to_owners`, `test_camera_control_toggle_keeps_other_selection_primary`; model int64 test | `exact-int64-inspection-and-copy`; mixed-primary switching uncovered |
| `App`, `StudioActions`: Frame scene/selection, Top/Front/Right/Isometric, previous/next view | View menu and viewport controls; navigation history independent of camera edit history | `test_viewport.test_presets_are_proper_world_up_rotations_and_preserve_orbit`, `test_center_selection_preset_and_free_view_preserve_required_state`, `test_history_restores_up_axis_and_ignores_inactive_focus_loss` | `all-presets-frame-and-view-history` |
| `App`: sidebar, inspector, fullscreen and navigation guide | Workbench menu actions and normal GTK window state | No dedicated assertion for the complete toggle/help sequence | `panels-fullscreen-navigation-help` |
| `App`: native text Undo/Redo versus camera Undo/Redo | Focused GTK text history receives ordinary Ctrl-Z; camera history has dedicated actions; Home remains local | `test_application.test_native_text_undo_preserves_camera_and_creation_undo_clears_selection` | Camera history is included in `native-camera-undo-redo-delete-import-roundtrip`; focused text history uncovered |
| `StudioActions`, camera inspector: create, record, rename, transform/FOV, select/retime/delete key, delete camera, import/export | Camera menu and contextual Selection inspector; separate authored tracks and camera-only GSV export | `test_application.test_scene_selection_playback_camera_animation_and_comparison`, `test_authored_paths_refresh_with_keys_and_editor_preserves_focused_text`, `test_source_and_geometry_selections_cannot_edit_previous_user_camera`; model camera roundtrip tests | `two-key-camera-authoring-and-native-save`, `native-camera-undo-redo-delete-import-roundtrip`, `camera-name-transform-fov-key-choice-retime-delete-undo` |
| `StudioActions`: asynchronous accepted saves survive later work | `jobs.CommandJobs` serializes accepted writes; source replacement invalidates UI callbacks, not chosen writes | `test_jobs.test_accepted_writes_survive_source_replacement_and_new_command`, `test_close_drains_every_accepted_command_without_gtk_callbacks` | Source SHA256 retained by desktop runner; concurrent source replacement during save uncovered |

## Timeline and transport

| macOS source and user operation | Ubuntu entry and behavior | Automated assertions | Desktop ID / remaining evidence |
| --- | --- | --- | --- |
| `StudioTimeline`: scrub, native sample snap; Option bypass | `timeline.Timeline`, Snap toggle; Alt temporarily bypasses snapping, without changing preference; gutter labels do not seek | `test_inspection_controls.test_alt_scrub_temporarily_bypasses_snap_without_changing_preference`, `test_label_gutter_does_not_seek_and_key_can_retime_beyond_source_end` | Time entry checked by `all-sample-transport-commands`; physical drag/Alt delivery uncovered |
| `StudioTimeline`: vertical scroll/pinch zoom; horizontal/Shift scroll or middle drag pan | Pointer-anchored scroll/pinch zoom, horizontal/Shift pan, middle-drag pan; source timestamps unchanged | `test_timeline_scroll_and_pinch_keep_pointer_time_fixed`, `test_horizontal_and_shift_scroll_pan_without_changing_time_scale` in `test_inspection_controls` | Physical wheel, touchpad and middle-drag timeline gestures uncovered |
| `StudioTimeline`: select and drag a camera key; Escape/focus loss cancel | Key lane, selected-key highlight and ghost; commits only a completed drag, can retime beyond source endpoints; source/key replacement cancels stale gesture | `test_inspection_controls.test_escape_cancels_drag_without_committing_key_retime`, `test_changed_camera_keys_or_focus_loss_cancel_the_old_drag`, `test_label_gutter_does_not_seek_and_key_can_retime_beyond_source_end` | Retime text action checked by `camera-name-transform-fov-key-choice-retime-delete-undo`; physical key drag/cancellation uncovered |
| `StudioTimeline`: Home fit, Left/Right samples; playback shortcut | Focusable accessible timeline; Home fits; arrows step native times; Space invokes transport | `test_inspection_controls.test_timeline_keyboard_steps_native_samples_home_fits_and_space_plays` | Menu sample commands checked; timeline-focused keys uncovered |
| `StudioTimeline`: automatic extent, named key lane and native units | New key extents auto-fit until user pans/zooms; selected camera label shown; second/frame/custom units and Float64 clock math retained | `test_inspection_controls.test_new_authored_extent_autofits_until_user_changes_timeline_window`; all four `test_timeline` extreme-clock tests; `test_application.test_empty_source_import_retains_frame_clock_and_rejects_session_conflict` | Normal second clock covered by runner; extreme/frame clocks and auto-fit uncovered |
| `StudioActions`: preview range, loop, speed and preview rate | In/Out fields, explicit enable, loop toggle, speed dropdown and rate field; edits commit on Enter, independent of capture metadata | `test_inspection_controls.test_preview_range_shades_only_its_native_time_interval`, `test_application.test_preview_rate_and_range_commit_only_after_enter` | `preview-range-rate-speed-loop` |

The timeline and comparison canvas have ordinary accessible names and keyboard
focus. They are not hidden behind test-only interfaces. Screen-reader traversal
and physical multi-touch acceptance are still separate desktop checks.

## Calibrated render/reference inspection

| macOS source and user operation | Ubuntu entry and behavior | Automated assertions | Desktop ID / remaining evidence |
| --- | --- | --- | --- |
| `StudioComparison`: toggle Render / GT, select a render view/capture, Return to 3D | Render / GT toggle; comparison-only render, observation and capture selectors; Return to 3D button; 3D navigation/preview controls disabled while comparing | `test_application.test_inspector_context_key_selector_and_comparison_controls`, `test_comparison_chooses_explicit_capture_and_switches_view_domain` | `render-reference-metrics-and-native-resolution-png`; selector/Return flow uncovered |
| `StudioComparison`, `StudioInspector`: Compare selected observation | Selection-inspector button calls `application._compare_selected_observation`; locates an explicitly linked view/collection, selects its capture | Context/selector implementation inspected; matching and membership assertions in `test_comparison.test_observation_membership_and_ambiguous_images` | Direct selected-observation button uncovered |
| `StudioComparison`, `StudioInspector`: Preview selected render view | Selection-inspector button calls `_preview_selected_render`; explicit preview choice remains available when the same view links observations | `test_application.test_explicit_calibrated_preview_never_scores_linked_reference_images`, `test_comparison.test_render_only_without_observations` | Direct selected-render button and preview-only image export uncovered |
| `StudioComparison`: pan/scroll, pinch/Option zoom shared image inspection; Fit and 1:1 | `image_view.ComparisonCanvas`: plain drag/scroll pan, Alt-scroll/pinch pointer-anchor zoom, shared transform; Fit and backing-pixel 1:1; image updates preserve transform | `test_inspection_controls.test_plain_image_scroll_pans_and_alt_zoom_preserves_both_pane_anchors`, `test_image_pinch_is_cumulative_fit_and_actual_pixels_keep_raw_pair` | Physical image gestures and HiDPI 1:1 uncovered |
| `StudioComparison`: declared RGB display and reference role | Display-only linear-sRGB transfer, clamping of HDR preview display copies; render/reference scientific arrays unchanged; role-specific right title | `test_inspection_controls.test_linear_hdr_preview_clamps_only_display_copy_and_is_focusable`; comparison affine/domain tests | Desktop runner locates computed metrics; role/linear/HDR display fixtures uncovered |
| `StudioComparison`: metrics, details and policy disclosure | Selection inspector reports PSNR, SSIM, MSE, MAE, valid pixels/windows plus expandable contract details; explicit preview has no reference or scores | `test_comparison.test_masked_metrics_have_no_false_perfect_score`, `test_strip_metrics_match_sdk_without_double_counting_halos`; UI explicit-preview test | `render-reference-metrics-and-native-resolution-png`; details expansion and masked fixture uncovered |
| `GSVObservation`, `GSVRender`: exact camera/view/time/field/observation linkage | `capture_catalog.build_capture_catalog` preserves separate manifest collection rows; `comparison.compare_view` never substitutes nearest observation/calibration or interpolated geometry; activates required domain | `test_comparison.test_static_camera_dynamic_field_exact_time`, `test_dynamic_camera_exact_calibration`, `test_observation_membership_and_ambiguous_images`; UI capture/domain test | Normal matched capture covered; ambiguous collections/cross-domain cases uncovered |
| `GSVObservation`: u8/f32 RGB, Boolean mask and strict native raster | SDK validation/read-only arrays; normalized u8; mask-aware full-resolution metrics in declared RGB space, no metric gamma conversion | `test_comparison.test_masked_metrics_have_no_false_perfect_score`, `test_mac_native_parser_fixtures_share_comparison_capabilities` | Basic matched u8 RGB checked; masks and Float32 collection uncovered |
| `GSVObservation`: PSNR range 1; SSIM 11×11 Gaussian σ1.5, population covariance, all 121 mask pixels valid | Shared `gsv.metrics.compare_rgb`; strip aggregation bounds temporary memory and avoids halo double-counting; no valid pixels/windows produce unavailable scores | `test_comparison.test_strip_metrics_match_sdk_without_double_counting_halos`, `test_masked_metrics_have_no_false_perfect_score` | Independent measured macOS numeric exchange not yet performed |
| `GSVRender`: explicit EWA profile, clipping, pixel center, affine then clamp | `comparison.render_ewa` / `_capture`: covariance/Jacobian, support/tile bounds, stable source ties, alpha cap/minimum and transmittance threshold; camera volume and output appearance | `test_comparison.test_anisotropic_covariance`, `test_front_to_back_depth_sort_and_stable_tie`, `test_near_far_inclusion_and_camera_volume`, `test_radius_clip_and_antialias_compensation`, `test_transmittance_termination_precedes_contribution`, `test_pixel_center_and_offset`, `test_affine_only_changes_prediction_and_applies_before_clamp` | CPU contract tests; not proof of Metal or a source training renderer producing identical pixels |
| `GSVRender`: unsupported distortion/time integration; transformed preview versus scoring | Explicit error for unsupported optics/shutter integration; transformed/output-unclamped view can preview, but cannot score against a native-raster reference | `test_comparison.test_unsupported_optics_and_time_integration`, `test_transformed_output_can_preview_but_cannot_score`, `test_unrepresentable_projection_is_an_error_not_an_empty_capture` | Desktop error presentation for these fixtures uncovered |
| `GSVObservation`, `StudioActions`: bounded image allocation and capture cancellation | 16,777,216 pixels, maximum side 16,384; cancellable read/render/metric stages; workers own source descriptor and obsolete result cannot publish | `test_comparison.test_cancel_and_allocation_limit`, `test_application.test_failed_open_keeps_document_and_close_cancels_capture` | Close/replacement during long comparison uncovered |

Ubuntu's quantitative path is an explicit CPU reference. The native Metal path
itself declares compositor approximations relative to a stored render profile.
Neither matching metadata nor passing PSNR/SSIM computations establishes
source-renderer pixel parity. Distorted/integrated captures remain inspectable
data, with calibrated rendering rejected explicitly.

## Preview controls and image export

| macOS source and user operation | Ubuntu entry and behavior | Automated assertions | Desktop ID / remaining evidence |
| --- | --- | --- | --- |
| `StudioPreviewControls`: resolution, grid, cameras, paths, trails | Native footer toggles and quality dropdown, persistent footer with controls disabled during comparison | `test_application.test_inspector_context_key_selector_and_comparison_controls`, `test_viewport.test_real_shader_blending_picking_resolution_and_disposal`, `test_grid_frustum_size_and_zero_interval_trails_match_native` | `all-preview-settings-toggles-and-resolution-exports` |
| `StudioPreviewControls`, `StudioInspector`: density, path opacity, trail history; size, opacity, exposure, near and background | Footer and Viewport inspector; controls change browsing only, not comparison policy; exact ranges below | `test_application.test_trajectory_capacity_error_keeps_scene_seek_and_playback_working`, `test_viewport.test_preview_controls_affect_shader_and_pick_consistently` | `all-preview-settings-toggles-and-resolution-exports`; min/max value rejection still requires boundary desktop checks |
| `StudioSnapshot`, screenshot action: viewport PNG | `application.export_image(False)` captures completed GL output at selected raster resolution; comparison mode exports native image(s), independent of pan/zoom | `test_application.test_ui_png_exports_preview_resolution_workspace_and_full_comparison`, `test_snapshots.test_gl_snapshot_has_preview_pixels_and_preserves_bindings`, `test_queued_gl_frame_cannot_export_previous_resolution` | `all-preview-settings-toggles-and-resolution-exports`, `render-reference-metrics-and-native-resolution-png` |
| `StudioSnapshot`: workspace PNG before save dialog dims content | `snapshots.capture_png_when_ready` waits for completed GTK/GL paint without blocking; immutable PNG prepared before native Save dialog; visible comparison remains visible | `test_snapshots.test_async_capture_waits_for_first_real_paint_and_finishes_once`, `test_async_capture_includes_new_layout_and_real_pixels`, `test_native_widget_snapshot_preserves_raster_and_color`; application PNG test | `native-workspace-png-save`; comparison-workspace versus hidden viewport desktop export uncovered |
| `StudioSnapshot`: capture cancellation/error and safe output | Snapshot callback/GLib sources disconnect on cancellation/unmap/unrealize; bounded readiness timeout; PNG writes atomic with symlink/source protection | `test_snapshots.test_async_cancel_and_unrealize_disconnect_without_late_completion`, `test_async_readiness_timeout_is_bounded_and_event_loop_runs`, `test_atomic_write_unicode_path_and_failure_preserves_previous`, `test_symlink_export_cannot_modify_referenced_source` | Native dialog tested; timeout/unmap and hostile output aliases remain automated-only |

Native and Ubuntu numeric settings, checked directly in the current source:

| Setting | macOS displayed range / initial value | Ubuntu displayed range / initial value |
| --- | --- | --- |
| Gaussian size | 0.1–4 / 1× | 0.1–4 / 1× |
| Opacity multiplier | 0.05–2 / 1× | 0.05–2 / 1× |
| Exposure multiplier | 0.1–4 / 1× | 0.1–4 / 1× |
| Path opacity | 0–100% / 18% | 0–100% / 18% |
| Path density | 1–100% / 100% | 1–100% / 100% |
| Trail history | integer 1–4096 / 8 | integer 1–4096 / 8 |
| Near clip, scene units | 0.0001–100 / 0.01 | 0.0001–100 / 0.01 |
| Resolution | 100%, 75%, 50% / 100% | 100%, 75%, 50% / 100% |
| Background | Studio RGB (0.055, 0.061, 0.071), black 0, gray 0.18, white 1 | Same presets; Studio default |
| Navigation sensitivity | No separate setting | 0.1–4 / 1, Ubuntu extension |

The lower-level path density remains capable of zero for callers/tests; the
visible desktop control matches macOS's minimum of 1%. GTK spin buttons and
AppKit slider/stepper layouts differ while preserving the displayed value domains.

## CPU scene I/O measurements

[tools/benchmark_scene_io.py](tools/benchmark_scene_io.py) generates deterministic
synthetic inputs in one command and measures each input in a fresh Python
subprocess. No desktop, GL context or GPU is involved. Existing scene destinations
are refused, and the generated files pass the normal writer validation. Measured
opening uses `SceneDocument`, including envelope/buffer integrity and object
semantic validation. Both Python positional-read functions are restored even
when a measured operation raises.

Reproduce from the repository root using a new output directory:

```sh
.venv/bin/python viewer/ubuntu/tools/benchmark_scene_io.py generate /tmp/gsv-scene-io-new
.venv/bin/python viewer/ubuntu/tools/benchmark_scene_io.py million /tmp/gsv-scene-io-new > /tmp/gsv-million-io.json
.venv/bin/python viewer/ubuntu/tools/benchmark_scene_io.py scaffold /tmp/gsv-scene-io-new > /tmp/gsv-scaffold-io.json
```

The 2026-11-16 working-tree measurement used an Intel Xeon Platinum 8380 at
2.30 GHz, 160 logical CPUs in the process affinity, Python 3.12.3 and NumPy 1.26.4
on Linux x86-64. Each warm result uses 101 operations; the two measurement workers
ran sequentially. These are observed wall times, without exclusive-machine load
control. The base source revision recorded in the JSON is `2c9b272`; these
measurements also include the then-current uncommitted source changes.

| Operation | Source / selected data | First operation after validation | Warm median / p95 |
| --- | --- | --- | --- |
| Full source opening and validation, persistent field | 1,000,000 identities × 2 samples; 129.77 MiB archive | 705.17 ms | Not repeated in the same worker |
| Prepare resident endpoints | Same field, time 0.25 | 54.83 ms | 0.193 / 0.202 ms for times 0.3–0.8; exact endpoint storage reused |
| Full source opening and validation, scaffold | 800,000 identities × 8 samples; 79.39 MiB archive | 477.03 ms | Not repeated in the same worker |
| Inspect one scaffold identity | ID 42 at time 2.5 | 0.563 ms | 0.215 / 0.251 ms |
| Extract one complete scaffold trajectory | ID 42, all 8 native samples | 4.421 ms | 2.552 / 2.860 ms |

“First”/“cold” describes application cache state **after full validation**, not a
cold disk: operating-system page caches are neither flushed nor controlled, and
validation already reads the source payloads. The counters report calls and
actual bytes returned by Python `os.pread`/`os.preadv` for the source inode, not
physical storage traffic or every possible file-read API. Instrumentation overhead
is included in the measured wall time.

For the scaffold, the positions array is 76,800,000 bytes. After validation,
metadata setup read zero additional bytes; selected inspection read 24 bytes in
2 calls, and the complete selected trajectory read 96 bytes in 8 calls. The warm
101 inspection/trajectory pairs read 12,120 bytes total. The complete native
timestamps and requested identity's positions were independently asserted. The
snapshot cache retained 6.10 MiB against its 64 MiB budget. Warm persistent-frame
preparation performed zero positional reads across all 101 operations.

| Worker | Startup VmHWM | Post-validation VmHWM | Final VmHWM | Final VmRSS |
| --- | --- | --- | --- | --- |
| Persistent million | 36.84 MiB | 224.66 MiB | 224.66 MiB | 191.54 MiB |
| Scaffold sparse inspection | 36.83 MiB | 133.98 MiB | 133.98 MiB | 47.34 MiB |

VmHWM is Linux's process peak resident set, including native NumPy allocations;
VmRSS is the resident set at that checkpoint. Generation ran in a different
process and cannot inflate these peaks. Full validation still examines all
source data; sparse inspection avoids another full position-array snapshot.
These CPU figures do not establish desktop frame rate, disk throughput, GPU
upload cost, or macOS performance. Source identity/size/mtime checks remained
unchanged, regeneration refused existing inputs, and exception-path restoration
of both positional-read functions was verified.

## Findings fixed during this audit and remaining acceptance

The scoped fixes add horizontal/Shift timeline panning, ignore label-gutter
scrubs, allow key retiming beyond the old source extent, cancel stale key drags
on focus/key-context changes, and auto-fit newly authored extents until a user
chooses a range. The timeline shows the edited camera name. Both custom widgets
have accessible names and accept focus. Image display now accepts valid HDR
render-only pixels by clamping only the display copy, labels the reference role,
and switches interpolation at the native 1.5× threshold. Numerically overflowing
projection fails explicitly rather than returning an apparently valid empty
capture. Each numerical/interaction fix has a regression assertion above.

Application integration adds the direct selected-observation/render actions,
comparison-only selector bar/Return button, and disables preview/navigation
controls during comparison. Those entry points must remain covered separately
from backend ability: a callable capture function alone does not provide a
desktop operation.

Outstanding evidence is explicit in the Desktop column: physical image/timeline
gestures, HiDPI behavior, screen-reader use, cancellation/error dialogs, and
several comparison selectors are not covered by the current desktop scripts.
The eight `--full` categories also require a successful actual-session report.
Cross-platform camera exchange requires running the macOS application, not just
loading a schema-equivalent fixture. macOS-vs-Ubuntu rendered pixel and performance
comparisons require measured artifacts from both backends on identified hardware.
