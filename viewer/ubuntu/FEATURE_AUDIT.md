# macOS feature audit for the Ubuntu desktop frontend

Audit date: 2026-11-16. This is a source-to-operation inventory, with evidence
identified separately from implementation. It supplements [PARITY.md](PARITY.md)
and does not claim identical AppKit/GTK decoration, identical Metal/OpenGL pixels,
or a completed macOS-to-Ubuntu application exchange.

## Evidence and scope

The reviewed sources include `viewer/native/GSVDocument.swift`,
`StreamingScene.swift`, `CompositeScene.swift`, `GSVCamera.swift`,
`GSVCameraWriter.swift`, `StudioSelection.swift`, `StudioInspector.swift`,
`WorkspaceModels.swift`, `TrajectoryExtraction.swift`, `StudioComparison.swift`,
`StudioTimeline.swift`, `StudioPreviewControls.swift`, `StudioSnapshot.swift`,
`GSVObservation.swift`, `GSVRender.swift` and `Shaders.metal`. Menu/inspector entry
points were cross-checked against `App.swift` and `StudioActions.swift`. Ubuntu
paths below are relative to `viewer/ubuntu`.

**Implemented** means the current source contains a reachable entry and
behavior. **Automated** means the named assertion ran; it does not by itself
establish native device delivery. The earlier complete-parity suite ran **245 tests: 243 passed,
two benchmark tests skipped**, and the Python SDK suite passed **78 tests**.
Its runtime baseline is `ec35705`; `6583689` is the final input-driver
revision. The 80-action mapping is a review-completeness guard, not runtime
proof of every mapped action.

Subsequent [production-data validation](PRODUCTION_VALIDATION.md) uses four real
160k–2.56M-point scenes. The endpoint/calibration changes at `bf7a410` pass an
updated **255-test suite: 253 passed, two opt-in benchmarks skipped**.

**Desktop ID** means the exact ID and group in a retained runner report. Read
[DESKTOP_VALIDATION.md](DESKTOP_VALIDATION.md#final-report-status) for final status
and provenance. A final row is accepted only after `passed: true`, the complete
requested groups, a clean source tree and source-integrity/cleanup evidence are
verified. Clean `6583689` Wayland full03 and Xwayland full33 reports each passed
eight basic steps and 24 extended checks: eight parity, six timeline, three
navigation, three comparison and four lifecycle. Both preserved source hashes
and monitor count 1→1, without cleanup errors. Actual renderers were Mesa
llvmpipe OpenGL 4.5 on Wayland and NVIDIA RTX 4090 OpenGL 4.6 on Xwayland.
The extracted Debian payload passed 31 actual-window application tests on each
backend, with chooser completion mocked. A separate fresh packaged Wayland
process loaded a `zh-Hans` preference fixture over `LANGUAGE=en_US`, exposed
Chinese inspector tabs and saved a native-portal workspace PNG. That restart
check used no keyboard/pointer injection and does not establish all 24 checks
in Chinese; native GUI preference writes are covered separately by lifecycle IDs.
Complete-run application logs retain two GTK critical focus assertions per
backend, plus two GDK critical monitor-geometry assertions on Xwayland; neither
contained a Python traceback. Their origin remains unconfirmed. Passing checks
and artifact hashes do not establish a warning-free runtime.

Earlier real GNOME application regressions passed 31/31 per backend at
`8d54a00`, with chooser completion mocked. Historical clean `599ca1c` basic
native-dialog reports are linked separately. Current focused evidence additionally
includes seven Range/timeline tests and a real AT-SPI Value probe, fourteen
snapshot tests plus a real 3288×2084 backing-scale workspace export, and the
inspector minimum-width test/Wayland probe. These checks do not establish
physical touchpad or macOS hardware behavior.

Test names in the tables are in `tests/`. Method names identify production code,
not test-only application interfaces.

## Document, hierarchy and inspection semantics

| macOS source and user operation | Ubuntu entry and behavior | Automated assertions | Remaining desktop evidence |
| --- | --- | --- | --- |
| `GSVDocument`: validated, read-only regular sources and change detection | `camera.SnapshotScene` / `open_regular_scene` retain SDK validation, pin a regular descriptor and read owned bounded `preadv` snapshots; source truncation cannot fault a live mapping | `test_model.test_nonregular_inputs_fail_without_blocking_source_or_camera_import`, `test_owned_source_snapshots_survive_truncation_without_sigbus`, `test_truncation_during_snapshot_read_fails_and_closes_descriptor` | Training-process mutation and cancellation during large native-dialog loads remain separate acceptance cases |
| `StreamingScene`, `CompositeScene`: active source cohort, peers, complete identity table | `model.SceneDocument` selects compatible source domains/display caches without double rendering; CPU and prepared frames retain source-count/layout metadata and all-time framing bounds | Domain/cache tests; `test_prepared_validity_preserves_inactive_identities_at_sample_boundaries`, `test_scaffold_glyphs_and_edges_obey_source_validity` | Real scene/cohort switching at scale requires application measurements |
| `WorkspaceModels`, Outliner: canonical categories, multi-view rigs, visibility and mixed selection | `outliner.SceneOutliner` is a direct ScrolledWindow/ListView with TreeListModel, TreeExpander and MultiSelection; seven counted groups, only multi-view camera children; rig/view membership remains exact | `test_outliner` covers grouping, nonselectable categories, Ctrl membership, search/collapse state, keyboard expansion, visibility and 2,000-object virtualization | Current full desktop hierarchy/accessibility traversal is not yet accepted |
| `StudioSelection`, `StudioActions`: viewing and selection are independent | The View camera dropdown changes `_viewed_entry`, not `selected`, `primary` or the authored editing target. Outliner/viewport selection determines inspection and editing; source viewing can be recorded into an already selected authored camera | `test_application.test_camera_view_preserves_selection_and_rejects_a_foreign_domain`, `test_rig_view_inspection_does_not_switch_viewing_camera`, `test_source_and_geometry_selections_cannot_edit_previous_user_camera` | Latest view-versus-selection assertions are automated; earlier basic camera workflows do not establish this complete current semantics |
| `StudioInspector`: Selection / Viewport / Document and raw source values | `inspector.document` displays coordinates, clock mappings/units and storage; `update(elements=...)` inspects inactive/foreign-domain identities without activating them, preserves raw RGB and distinguishes scaffold glyph styling | Document/inactive/scaffold model tests; `test_model.test_playback_updates_values_in_place_without_rebuilding_accessible_rows` | `selection-viewport-document-inspector` passed in both native desktop reports; general screen-reader traversal remains untested |
| `Shaders.metal.projectGaussians`: visible/total statistics | `gpu_visibility` reuses the render projection and asynchronously counts eligible Gaussian bounding boxes at at most 10 Hz. Source/cohort changes reject old epochs; empty geometry is exactly zero; query failure displays unavailable, not active count | All `test_gpu_visibility` cases: baseline GL3.3, ready-only reads, bounded queue, clipping, opacity/scale, mixed weights, epochs, empty/inactive and failure recovery | A sampled count has age/source-time metadata; it is not an occlusion/pixel count or desktop presentation FPS |
| `GSVCameraWriter`, authored inspection: large key tracks | `CameraTrack.times` caches immutable Float64 times; steady seek is logarithmic. Inspector shows a count, retaining every key in cached raw JSON; imports build one validated list, not repeatedly sorted prefixes | Large-seek/cache, stable-Inspector and large-import/invalid-middle/cancellation tests in `test_model` | CPU scaling measurements below; 100,000-key native portal import/playback is not yet a desktop acceptance result |

## Menus, workbench and source ownership

| macOS source and user operation | Ubuntu entry and behavior | Automated assertions | Desktop ID / remaining evidence |
| --- | --- | --- | --- |
| `App`, `StudioActions`: Open Scene; cancel loading; failed or superseded open | File menu/open button; `application.open_path`, asynchronous validated read-only `SceneDocument`; cancellation keeps the accepted document | `test_application.test_cancelled_load_disposes_then_replacement_uses_new_scene`, `test_failed_open_keeps_document_and_close_cancels_capture` | `native-open-dialog`, `native-open-cancel-preserves-source-time-and-camera` and `corrupt-native-open-retains-accepted-source-and-camera`: Wayland and Xwayland passed; superseded-load cancellation remains automated-only |
| `StudioActions`: object selection and native-time playback | Outliner and viewport callbacks; first/previous/next/last, play/pause and Time field; source Float64 times retained | `test_application.test_scene_selection_playback_camera_animation_and_comparison`, `test_static_source_transport_includes_authored_camera_duration` | `outliner-field-selection-and-path-request`, `play-pause`, `all-sample-transport-commands` |
| `StudioInspector`: Selection / Viewport / Document pages | Three native notebook pages; contextual selection controls and document/domain rows | `test_application.test_inspector_context_key_selector_and_comparison_controls` | `selection-viewport-document-inspector`: Wayland and Xwayland full reports passed |
| `StudioInspector`: exact identity, mixed selection and primary member; Copy attributes | Exact signed-ID entry, native primary-member dropdown, owner-qualified identity; clipboard action | `test_application.test_mixed_primary_member_keeps_duplicate_ids_bound_to_owners`, `test_camera_control_toggle_keeps_other_selection_primary`; model int64 test | `exact-int64-inspection-and-copy`; mixed-primary switching uncovered |
| `App`, `StudioActions`: Frame scene/selection, Top/Front/Right/Isometric, previous/next view | View menu and viewport controls; navigation history independent of camera edit history | `test_viewport.test_presets_are_proper_world_up_rotations_and_preserve_orbit`, `test_center_selection_preset_and_free_view_preserve_required_state`, `test_history_restores_up_axis_and_ignores_inactive_focus_loss` | `all-presets-frame-and-view-history`, `physical-1-matches-menu-front`, `physical-3-matches-menu-right` and `physical-7-matches-menu-top`: Wayland and Xwayland full reports passed |
| `App`: sidebar, inspector, fullscreen and navigation guide | Workbench menu actions and normal GTK window state | No dedicated assertion for the complete toggle/help sequence | `panels-fullscreen-navigation-help`: Wayland and Xwayland full reports passed |
| `App`: native text Undo/Redo versus camera Undo/Redo | Focused GTK text history receives ordinary Ctrl-Z; camera history has dedicated actions; Home remains local | `test_application.test_native_text_undo_preserves_camera_and_creation_undo_clears_selection` | Camera history is included in `native-camera-undo-redo-delete-import-roundtrip`; focused text history uncovered |
| `StudioActions`, camera inspector: create, record, rename, transform/FOV, select/retime/delete key, delete camera, import/export | Camera menu and contextual Selection inspector; Enter/Apply commit fields, separate authored tracks, camera-only export; recorded keys capture the current interactive near clip | `test_application.test_scene_selection_playback_camera_animation_and_comparison`, `test_authored_paths_refresh_with_keys_and_editor_preserves_focused_text`, `test_apply_camera_field_is_equivalent_to_enter_and_can_be_undone`, `test_authored_keys_capture_the_current_interactive_near_clip`; model roundtrip tests | `two-key-camera-authoring-and-native-save`, `native-camera-undo-redo-delete-import-roundtrip`, `camera-name-transform-fov-key-choice-retime-delete-undo` |
| `StudioActions`: asynchronous accepted saves survive later work | `jobs.CommandJobs` serializes accepted writes; source replacement invalidates UI callbacks, not chosen writes | `test_jobs.test_accepted_writes_survive_source_replacement_and_new_command`, `test_close_drains_every_accepted_command_without_gtk_callbacks` | Source SHA256 retained by desktop runner; concurrent source replacement during save uncovered |

The last handler audit also corrected these mapped-but-different behaviors:

| Native behavior | Current Ubuntu behavior and regression |
| --- | --- |
| Viewport number shortcuts | 1/3/7 invoke Front/Right/Top perspective, alongside the existing menu/buttons |
| FOV keying with nonzero skew and endpoint angles | Both focal lengths and K[0,1] scale by the same ratio; inclusive 1°/170° accepted, principal point/raster/pose preserved; `test_fov_editor_scales_skew_with_focal_length_and_accepts_native_boundaries` checks undo/redo |
| Preview rate/range commits | Valid rate or committed/enabled/disabled range pauses; unfinished fields and rejected rates do not change committed rate; `test_preview_range_and_rate_commit_pause_but_uncommitted_or_invalid_rate_do_not` |
| Static cross-clock camera viewing versus authoring | Same-space one-key authored cameras remain viewable/visible; recording, FOV/transform, key jumps and retiming require active domain; foreign key lane hidden; `test_single_key_authored_camera_can_be_viewed_across_clock_but_cannot_be_edited`, `test_foreign_camera_keys_cannot_seek_or_retime_and_are_hidden_from_active_lane` |
| Ambiguous ownerless identity | More than one source match is rejected instead of selecting the first field; `test_exact_id_without_owner_rejects_ambiguity_instead_of_selecting_first_field` |
| Typed session times and workspace counts | Exact Time expands the per-clock session timeline without changing source samples; Document/header count authored cameras and session samples; sample index uses the workspace lower sample; `test_exact_time_entry_extends_session_timeline_without_changing_source`, `test_document_and_header_counts_include_session_cameras_and_authored_times` |
| Exact copied attributes | `Inspector.plain_rows` supplies cached original strings, avoiding double entity decoding by Adwaita getters; `test_copy_attributes_preserves_literal_markup_and_entities_without_global_clipboard` mocks the clipboard and retains literal `&amp;`, `&#55;` and `<b>` |
| Canonical About/product identity | Source and Debian builds read `Info.plist` product/author metadata; four packaging tests passed, with no separate hard-coded Linux author record |

These assertions are in the current automated suite and the 31-case actual
GNOME window runs at `8d54a00`, with only documentation changes in the working
tree. Chooser completion remained substituted in those runs; native-portal
and EIS acceptance remains separately scoped below.

## Timeline and transport

The timeline is a GTK AccessibleRange with a native AT-SPI Value interface.
Seven timeline/range tests and an actual GNOME AT-SPI probe verify finite native
values, bounds, exact unsnapped setting and the single-sample case. This is
separate from EIS pointer/wheel/key check IDs below.

| macOS source and user operation | Ubuntu entry and behavior | Automated assertions | Desktop ID / remaining evidence |
| --- | --- | --- | --- |
| `StudioTimeline`: scrub, native sample snap; Option bypass | `timeline.Timeline`, Snap toggle; Alt temporarily bypasses snapping, without changing preference; gutter labels do not seek | `test_inspection_controls.test_alt_scrub_temporarily_bypasses_snap_without_changing_preference`, `test_label_gutter_does_not_seek_and_key_can_retime_beyond_source_end` | `all-sample-transport-commands` and `physical-scrub-near-tick-snap-and-alt-bypass`: Wayland and Xwayland full EIS reports passed; physical touchpad untested |
| `StudioTimeline`: vertical scroll/pinch zoom; horizontal/Shift scroll or middle drag pan | Pointer-anchored scroll/pinch zoom, horizontal/Shift pan, middle-drag pan; source timestamps unchanged | `test_timeline_scroll_and_pinch_keep_pointer_time_fixed`, `test_horizontal_and_shift_scroll_pan_without_changing_time_scale` in `test_inspection_controls` | `physical-wheel-zoom-shift-wheel-and-middle-pan-home-fit`: Wayland and Xwayland full EIS reports passed; physical touchpad remains untested |
| `StudioTimeline`: select and drag a camera key; Escape/focus loss cancel | Key lane, selected-key highlight and ghost; commits only a completed drag, can retime beyond source endpoints; source/key replacement cancels stale gesture | `test_inspection_controls.test_escape_cancels_drag_without_committing_key_retime`, `test_changed_camera_keys_or_focus_loss_cancel_the_old_drag`, `test_label_gutter_does_not_seek_and_key_can_retime_beyond_source_end` | `selected-key-physical-drag-escape-cancel` and `selected-key-physical-retime-and-native-undo`: Wayland and Xwayland full EIS reports passed |
| `StudioTimeline`: Home fit, Left/Right samples; playback shortcut | Focusable accessible timeline; Home fits; arrows step native times; Space invokes transport | `test_inspection_controls.test_timeline_keyboard_steps_native_samples_home_fits_and_space_plays` | `focused-home-left-right-space-keyboard`: Wayland and Xwayland full EIS reports passed |
| `StudioTimeline`: automatic extent, named key lane and native units | New key extents auto-fit until user pans/zooms; selected camera label shown; second/frame/custom units and Float64 clock math retained | `test_inspection_controls.test_new_authored_extent_autofits_until_user_changes_timeline_window`; all four `test_timeline` extreme-clock tests; `test_application.test_empty_source_import_retains_frame_clock_and_rejects_session_conflict` | Normal second clock covered by runner; extreme/frame clocks and auto-fit uncovered |
| `StudioActions`: preview range, loop, speed and preview rate | In/Out fields, explicit enable, loop toggle, speed dropdown and rate field; edits commit on Enter, independent of capture metadata | `test_inspection_controls.test_preview_range_shades_only_its_native_time_interval`, `test_application.test_preview_rate_and_range_commit_only_after_enter` | `preview-range-rate-speed-loop`: Wayland and Xwayland full reports passed |

The timeline and comparison canvas have ordinary accessible names and keyboard
focus. They are not hidden behind test-only interfaces. Screen-reader traversal
and physical multi-touch acceptance are still separate desktop checks.

## Calibrated render/reference inspection

| macOS source and user operation | Ubuntu entry and behavior | Automated assertions | Desktop ID / remaining evidence |
| --- | --- | --- | --- |
| `StudioComparison`: toggle Render / GT, select a render view/capture, Return to 3D | Render / GT toggle; comparison-only render, observation and capture selectors; Return to 3D button; 3D navigation/preview controls disabled while comparing | `test_application.test_inspector_context_key_selector_and_comparison_controls`, `test_comparison_chooses_explicit_capture_and_switches_view_domain` | `render-reference-metrics-and-native-resolution-png`, `selected-render-preview-single-native-png-and-return` and `exact-capture-selector-second-and-first-native-rasters`: Wayland and Xwayland passed |
| `StudioComparison`, `StudioInspector`: Compare selected observation | Selection-inspector button calls `application._compare_selected_observation`; locates an explicitly linked view/collection, selects its capture | Context/selector implementation inspected; matching and membership assertions in `test_comparison.test_observation_membership_and_ambiguous_images` | `selected-observation-linked-inspection-pair-and-workspace-png`: Wayland and Xwayland passed |
| `StudioComparison`, `StudioInspector`: Preview selected render view | Selection-inspector button calls `_preview_selected_render`; explicit preview choice remains available when the same view links observations | `test_application.test_explicit_calibrated_preview_never_scores_linked_reference_images`, `test_comparison.test_render_only_without_observations` | `selected-render-preview-single-native-png-and-return`: Wayland and Xwayland passed; 640×480 raw preview and absent scores verified |
| `StudioComparison`: pan/scroll, pinch/Option zoom shared image inspection; Fit and 1:1 | `image_view.ComparisonCanvas`: plain drag/scroll pan, Alt-scroll/pinch pointer-anchor zoom, shared transform; Fit and backing-pixel 1:1; image updates preserve transform | `test_inspection_controls.test_plain_image_scroll_pans_and_alt_zoom_preserves_both_pane_anchors`, `test_image_pinch_is_cumulative_fit_and_actual_pixels_keep_raw_pair` | `selected-observation-linked-inspection-pair-and-workspace-png`: Wayland and Xwayland EIS drag/Alt-wheel changed the displayed crop while raw export hash stayed equal; physical pinch and HiDPI 1:1 remain untested |
| `StudioComparison`: declared RGB display and reference role | Display-only linear-sRGB transfer, clamping of HDR preview display copies; render/reference scientific arrays unchanged; role-specific right title | `test_inspection_controls.test_linear_hdr_preview_clamps_only_display_copy_and_is_focusable`; comparison affine/domain tests | Desktop runner locates computed metrics; role/linear/HDR display fixtures uncovered |
| `StudioComparison`: metrics, details and policy disclosure | Selection inspector reports PSNR, SSIM, MSE, MAE, valid pixels/windows plus expandable contract details; explicit preview has no reference or scores | `test_comparison.test_masked_metrics_have_no_false_perfect_score`, `test_strip_metrics_match_sdk_without_double_counting_halos`; UI explicit-preview test | `render-reference-metrics-and-native-resolution-png`; details expansion and masked fixture uncovered |
| `GSVObservation`, `GSVRender`: exact camera/view/time/field/observation linkage | `capture_catalog.build_capture_catalog` preserves separate manifest collection rows; `comparison.compare_view` never substitutes nearest observation/calibration or interpolated geometry; activates required domain | `test_comparison.test_static_camera_dynamic_field_exact_time`, `test_dynamic_camera_exact_calibration`, `test_observation_membership_and_ambiguous_images`; UI capture/domain test | Normal matched capture covered; ambiguous collections/cross-domain cases uncovered |
| `GSVObservation`: u8/f32 RGB, Boolean mask and strict native raster | SDK validation/read-only arrays; normalized u8; mask-aware full-resolution metrics in declared RGB space, no metric gamma conversion | `test_comparison.test_masked_metrics_have_no_false_perfect_score`, `test_mac_native_parser_fixtures_share_comparison_capabilities` | Basic matched u8 RGB checked; masks and Float32 collection uncovered |
| `GSVObservation`: PSNR range 1; SSIM 11×11 Gaussian σ1.5, population covariance, all 121 mask pixels valid | Ubuntu reuses Python `gsv.metrics.compare_rgb`; strip aggregation bounds temporary memory and avoids halo double-counting; no valid pixels/windows produce unavailable scores | `test_comparison.test_strip_metrics_match_sdk_without_double_counting_halos`, `test_masked_metrics_have_no_false_perfect_score` | Independent measured macOS numeric exchange not yet performed |
| `GSVRender`: explicit EWA profile, clipping, pixel center, affine then clamp | `comparison.render_ewa` / `_capture`: covariance/Jacobian, support/tile bounds, stable source ties, alpha cap/minimum and transmittance threshold; camera volume and output appearance | `test_comparison.test_anisotropic_covariance`, `test_front_to_back_depth_sort_and_stable_tie`, `test_near_far_inclusion_and_camera_volume`, `test_radius_clip_and_antialias_compensation`, `test_transmittance_termination_precedes_contribution`, `test_pixel_center_and_offset`, `test_affine_only_changes_prediction_and_applies_before_clamp` | CPU contract tests; not proof of Metal or a source training renderer producing identical pixels |
| `GSVRender`: unsupported distortion/time integration; transformed preview versus scoring | Explicit error for unsupported optics/shutter integration; transformed/output-unclamped view can preview, but cannot score against a native-raster reference | `test_comparison.test_unsupported_optics_and_time_integration`, `test_transformed_output_can_preview_but_cannot_score`, `test_unrepresentable_projection_is_an_error_not_an_empty_capture` | Desktop error presentation for these fixtures uncovered |
| `GSVObservation`, `StudioActions`: bounded image allocation and capture cancellation | 16,777,216 pixels, maximum side 16,384; cancellable read/render/metric stages; workers own source descriptor and obsolete result cannot publish | `test_comparison.test_cancel_and_allocation_limit`, `test_application.test_failed_open_keeps_document_and_close_cancels_capture` | Close/replacement during long comparison uncovered |

Ubuntu's quantitative path is an explicit CPU reference. The native Metal path
itself declares compositor approximations relative to a stored render profile.
Neither matching metadata nor passing PSNR/SSIM computations establishes
source-renderer pixel parity. Distorted/integrated captures remain inspectable
data, with calibrated rendering rejected explicitly.

## Preview controls and image export

Fourteen snapshot tests passed, including a separate `GDK_SCALE=2` GTK/GL run.
Ordinary GTK workspace snapshots use backing pixels, bounded to 16,384 per side
and 64 Mi pixels. The actual 1644×1042 logical window exported 3288×2084 pixels;
completed GL framebuffers and comparison native rasters are not rescaled by this
path. The inspector minimum-width layout test and an actual Wayland allocation
probe also passed; no Mac Retina hardware measurement is implied.

| macOS source and user operation | Ubuntu entry and behavior | Automated assertions | Desktop ID / remaining evidence |
| --- | --- | --- | --- |
| `StudioPreviewControls`: resolution, grid, cameras, paths, trails | Native footer toggles and quality dropdown, persistent footer with controls disabled during comparison | `test_application.test_inspector_context_key_selector_and_comparison_controls`, `test_viewport.test_real_shader_blending_picking_resolution_and_disposal`, `test_grid_frustum_size_and_zero_interval_trails_match_native` | `all-preview-settings-toggles-and-resolution-exports` |
| `StudioPreviewControls`, `StudioInspector`: density, path opacity, trail history; size, opacity, exposure, near and background | Footer and Viewport inspector; controls change browsing only, not comparison policy; exact ranges below | `test_application.test_trajectory_capacity_error_keeps_scene_seek_and_playback_working`, `test_viewport.test_preview_controls_affect_shader_and_pick_consistently` | `all-preview-settings-toggles-and-resolution-exports`; min/max value rejection still requires boundary desktop checks |
| `StudioSnapshot`, screenshot action: viewport PNG | `application.export_image(False)` captures completed GL output at selected raster resolution; comparison mode exports native image(s), independent of pan/zoom | `test_application.test_ui_png_exports_preview_resolution_workspace_and_full_comparison`, `test_snapshots.test_gl_snapshot_has_preview_pixels_and_preserves_bindings`, `test_queued_gl_frame_cannot_export_previous_resolution` | `all-preview-settings-toggles-and-resolution-exports`, `render-reference-metrics-and-native-resolution-png` |
| `StudioSnapshot`: workspace PNG before save dialog dims content | `snapshots.capture_png_when_ready` waits for completed GTK/GL paint without blocking; immutable PNG prepared before native Save dialog; visible comparison remains visible | `test_snapshots.test_async_capture_waits_for_first_real_paint_and_finishes_once`, `test_async_capture_includes_new_layout_and_real_pixels`, `test_native_widget_snapshot_preserves_raster_and_color`; application PNG test | `native-workspace-png-save`; actual workspace backing export 3288×2084 verified; Wayland and Xwayland full comparison checks passed |
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

## Authored animation scaling and maintenance

The working-tree CPU measurements below use 100,000 finite, ordered camera keys
with fixed 320×240 raster, an integer-centered pinhole K, rigid poses and explicit
near/far tracks. Each import fixture is generated directly as SDK arrays, not
through repeated `record()` calls. Per-key adapter validation remains enabled;
Python SDK envelope, checksum and camera validation are also enabled.

| Work | Measured wall time | Scope |
| --- | --- | --- |
| First seek, building the cached 100,000-key time index | 6.14 ms | Includes one complete time-index construction |
| Warm seek | 0.413 / 0.431 ms median/P95 | 101 seeks, same key list; interpolation retained |
| Previous time-array construction alone | 5.75 ms median | Former per-seek traversal, excluding interpolation |
| Initial inspector descriptor and native rows | 90.26 ms | Full raw key times and widgets built once |
| Warm inspector updates | 0.236 / 0.254 ms median/P95 | 101 times; existing rows/raw descriptor retained |
| Import 10,000 keys, 2.21 MiB | 1.720 s | Validation and immutable key construction |
| Import 100,000 keys, 21.53 MiB | 17.268 s | Same checks; 10.042× time for 10× keys |

The imports measured 171.97/172.68 µs per key. Every key is retained, matrices
are independent read-only snapshots, integer-to-half pixel conversion occurs
once, and near/far and interpolated pose assertions pass. Both the reconstruction
and animation file SHA256 remain unchanged. The 17-second case is loading work
on a worker, not interactive seek latency or a claim of instant large imports.
Optional cancellation checks occur every 128 keys and before publication; an
invalid middle key rejects the whole import. Reproduce the import scaling from
the repository root without a display:

```sh
PYTHONPATH=viewer/ubuntu:viewer/ubuntu/tests:packages/gsv/src .venv/bin/python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from test_model import write, write_camera_animation
from gs_studio.model import SceneDocument
from gs_studio.camera import import_camera_animation
with TemporaryDirectory() as folder:
    root = Path(folder)
    write(root / 'source.gsv')
    with SceneDocument(root / 'source.gsv') as source:
        for count in (10_000, 100_000):
            path = root / f'camera-{count}.gsv'
            write_camera_animation(path, source, count)
            start = perf_counter()
            track = import_camera_animation(path, source)[0]
            print(count, len(track.keyframes), perf_counter() - start)
PY
```

Maintenance invariants:

- Structural camera edits replace `keyframes` with a new list. Read-only time
  indices, raw descriptors and key-selector entries use that identity to
  invalidate caches. In-place list mutation would bypass those invalidations.
- View-camera changes preserve the selection set and authored target. Tests that
  select a source object must use the Outliner/selection path; the View dropdown
  is not a selection shortcut.
- Raw source schemas, translation catalogs, version metadata and camera export
  contracts remain shared with macOS. About reads the same canonical product/
  author identity in the checkout and Debian payload. GTK row updates retain object identity
  unless their property schema changes, keeping accessibility/focus stable.
- CPU fallback frames retain original cohort count/layout metadata even when
  invalid raster records are filtered. Visibility epochs depend on that layout,
  not display time or camera pose.
- Frustum counting compiles the existing render projection. A new preview
  clipping/opacity policy must update its shared eligibility expression, with
  GL3.3 tests, rather than introducing a separate CPU approximation.

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

Outstanding evidence is explicit in the Desktop column and final report table.
Current EIS check IDs can establish native generated input delivery for the
owned application; they do not establish physical touchpad behavior or general
screen-reader acceptance. Completed HiDPI workspace and inspector-width probes
are scoped to this GTK session. Full Wayland and Xwayland acceptance is recorded
for the current clean source. Extracted-package actual-window tests and the
fixture-based Chinese restart/native-save check have their own recorded scopes;
chooser completion remains mocked in the 31-test application suite.
Cross-platform camera exchange requires running the macOS application, not just
loading a schema-equivalent fixture. macOS-vs-Ubuntu rendered pixel and performance
comparisons require measured artifacts from both backends on identified hardware.
