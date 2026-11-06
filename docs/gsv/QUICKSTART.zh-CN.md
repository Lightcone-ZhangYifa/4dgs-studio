# GSV 使用指南

GSV 是一个封装格式。文件里可以只有摄像机、只有运动 scaffold、只有高斯表示，也可以完全为空。不同高斯表示都以场景对象存储。身份持续高斯轨迹（Identity-Persistent Gaussian Trajectories）使用 `gsv.gaussian.persistent-identity`，其核心约束是每个 Gaussian ID 在整段序列中保持身份。

## 如何组织一个可对比的场景

| 对象 | 保存什么 | 不应混入什么 |
|---|---|---|
| Camera | K、位姿、图像尺寸、稳定视角 ID；可选裁剪视体、镜头和拍摄信息 | 高斯合成算法、看图软件的显示曝光 |
| Gaussian field | 指定数学表示中的几何、运动、身份和外观数据 | 相机曝光补偿、GT 图像 |
| Render profile | 实际高斯中心过滤、投影导数、协方差滤波、支持范围、alpha 和合成规则 | 根据论文名字猜出的参数 |
| Render view | 选择哪个相机视角、哪些高斯对象、哪个 profile；输出尺寸、背景、颜色域和时间策略 | 隐式选择“第一个相机”或重复绘制原生模型与显示缓存 |
| RGB observation | 对应相机样本和视角的原始 RGB、可选有效掩码、颜色域及来源角色 | 模型自己渲染后冒充的实拍 GT |
| Camera appearance | 可选的相机颜色 gain/bias，作用于完整合成图像 | 对 GT 做自动亮度拟合 |

每类对象都可以独立存在。Render view 自身需要引用完整的场景内对象；它不是外部文件快捷方式。相机动画单独导出时，只保存完整的相机对象，不生成悬空的场景引用。

## 近远范围为什么分开

`camera.projection.depth_mapping` 描述源投影矩阵把深度映射到什么范围。
`camera.view_volume` 描述人为设定的相机轴向可见范围。
`render.profile.visibility.center_depth` 描述算法实际过滤 Gaussian 中心的范围。

三者不会自动互相覆盖。例如，一个源投影矩阵用 near=0.01，但其 CUDA kernel 丢弃 z≤0.2 的 Gaussian，必须分别保存这两个事实。无限远有明确表示，不会偷偷替换成一个很大的数。中心过滤也不会被描述成精确的表面或片元裁剪。

## Python 入门

安装本地源码：

```sh
python -m pip install ./packages/gsv
```

创建空文件或只有相机的文件：

```python
import numpy as np
from gsv import Writer, Scene, add_camera

with Writer("empty.gsv"):
    pass

K = np.array([[800., 0., 640.],
              [0., 800., 360.],
              [0., 0., 1.]])[None, None]
pose = np.eye(4)[None, None]
with Writer("camera.gsv") as out:
    add_camera(out, "capture", [0.], K, pose, [[1280, 720]],
               pixel_center="half", view_ids=["camera-A"],
               view_volume={"near": [[0.05]], "far": [[0.]],
                            "far_unbounded": [[True]]},
               optics={"focal_length_mm": [[35.]], "f_number": [[2.8]]},
               acquisition={"exposure_seconds": [[0.01]], "iso": [[200.]]})

with Scene("camera.gsv") as scene:
    scene.validate()
    camera = scene.object("capture")
    intrinsics = scene.array(camera["data"]["projection"]["intrinsics"])
    camera_to_world = scene.array(camera["data"]["pose"]["camera_to_world"])
```

这里 T=1、V=1；实际多时刻多视角分别使用 `[T,V,3,3]` 和 `[T,V,4,4]`。镜头和拍摄轨迹是 `[T,V]`。K 决定投影射线，镜头毫米参数是附加拍摄信息，不能反过来覆盖 K。

完整的“高斯 + 相机 + GT + 渲染配置 + 渲染视图”可运行示例在
[`write_scene.py`](../../packages/gsv/examples/write_scene.py)。各 helper 的参数、流式写入和读取示例见 [Python 库](../../packages/gsv/README.md)。

`add_camera_rgb` 接收确切的 camera sample 与 view index，图像保持原尺寸、原像素。
图像有自己的 `times`：静态摄像机只需保存一次 K/位姿，整个视频的 GT 可以重复引用这个标定样本。
`add_render_view` 通过 `observation_ids` 明确选择可用于该视图对比的观测。
所有写入先校验再原子发布，校验失败不会发布一个“完成”的文件。

## 在 4DGS Studio 中查看

打开 GSV 后，可在场景层级中查看摄像机、Gaussian 对象及其他对象。
含有匹配 RGB 观测的渲染视图支持渲染与 GT 对比。选择层级中的 Render view 可预览它明确绑定的相机、对象和渲染设置；没有 GT 时使用单窗格预览，不显示图像一致性分数。普通三维检查也不要求 GT。

比较必须使用匹配的相机、时间、尺寸和颜色域。换了渲染配置、相机、对象或时间，之前的分数就不能继续显示为当前分数。PSNR、SSIM、MSE、MAE 是指定条件下的图像一致性指标，不能证明轨迹或伪标签具有物理真值。

文件可记录 OpenCV Brown/fisheye 畸变、曝光、快门和滚动读出信息。当前原生 renderer 不因此自动获得畸变投影或时间积分能力；不支持的标定渲染会明确拒绝。输出裁剪或缩放映射可以保存，但当前 Studio 尚不执行非恒等输出映射，也不会重采样 GT 后给出分数。Metal 与源 CUDA 的支持范围和合成顺序差异会明确显示，不能把近似渲染称为逐像素复现。

精确字段和能力边界见 [相机与渲染契约](RENDER_CONTRACT.md)，调研依据见 [设计研究](CAMERA_RESEARCH.md)。
