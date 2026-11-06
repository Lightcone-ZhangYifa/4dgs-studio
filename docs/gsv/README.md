# GSV documentation

GSV is an extensible container for independently optional reconstruction objects.
It supports empty documents, multiple dynamic representations, calibrated cameras,
scaffolds, motion bases, tracks, geometry, bindings, optional camera-bound RGB
observations and provenance. Identity-Persistent Gaussian Trajectories is a
first-class representation alongside the other Gaussian encodings.

- [中文使用指南](QUICKSTART.zh-CN.md): 对象关系、近远范围和 Python 入门。
- [Specification](SPECIFICATION.md): normative envelope, arrays and scene contracts.
- [Camera and rendering](RENDER_CONTRACT.md): projection, capture, rendering and optional GT associations.
- [Camera design research](CAMERA_RESEARCH.md): primary sources and design decisions.
- [Object encodings](ENCODINGS.md): mathematical semantics and auxiliary objects.
- [Design rationale](DESIGN.md): architecture, integrity and extension process.
- [Python library](../../packages/gsv/README.md): installation and examples.
- [4DGS Studio](../../README.md): native macOS inspection and camera authoring.

The Python SDK can validate safe storage independently of model execution.
Analytical evaluators are available for static, persistent-identity, spacetime polynomial,
joint covariance4D and SE3-basis Gaussian geometry. Neural/scaffold deformation
objects preserve native parameters and recipes; evaluating those methods uses
the pinned source implementation. Studio can inspect these objects and display
explicitly linked source-evaluated caches, with their limitations recorded.

The NumPy SDK includes explicit-domain MSE, MAE, PSNR and Gaussian-window SSIM
for exact camera-aligned render/reference images. Missing GT stays missing;
camera-only, Gaussian-only and empty documents remain valid.
