#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

from __future__ import annotations

import importlib
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import message_filters
import numpy as np
import rclpy
from cv_bridge import CvBridge
from rclpy.node import Node
from sensor_msgs.msg import CameraInfo, Image, PointCloud2
from sensor_msgs_py import point_cloud2
from stereo_msgs.msg import DisparityImage

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


class TrtEngineRunner:
    def __init__(self, torch_module: Any, engine_path: Path) -> None:
        import tensorrt as trt

        self._torch = torch_module
        self._trt = trt
        self._logger = trt.Logger(trt.Logger.WARNING)
        with open(engine_path, "rb") as f:
            self._engine = trt.Runtime(self._logger).deserialize_cuda_engine(f.read())
        if self._engine is None:
            raise RuntimeError(f"Failed to deserialize TensorRT engine: {engine_path}")
        self._context = self._engine.create_execution_context()
        self._input_names = self._io_tensor_names(trt.TensorIOMode.INPUT)
        self._output_names = self._io_tensor_names(trt.TensorIOMode.OUTPUT)

    def _io_tensor_names(self, mode: Any) -> list[str]:
        return [
            self._engine.get_tensor_name(i)
            for i in range(self._engine.num_io_tensors)
            if self._engine.get_tensor_mode(self._engine.get_tensor_name(i)) == mode
        ]

    def _trt_dtype_to_torch(self, dtype: Any) -> Any:
        trt = self._trt
        mapping = {
            trt.DataType.FLOAT: self._torch.float32,
            trt.DataType.HALF: self._torch.float16,
            trt.DataType.BF16: self._torch.bfloat16,
            trt.DataType.INT32: self._torch.int32,
            trt.DataType.INT8: self._torch.int8,
            trt.DataType.BOOL: self._torch.bool,
        }
        if dtype not in mapping:
            raise RuntimeError(f"Unsupported TensorRT dtype: {dtype}")
        return mapping[dtype]

    def expected_input_hw(self, name: str) -> tuple[int, int] | None:
        if name not in self._input_names:
            return None
        shape = tuple(self._engine.get_tensor_shape(name))
        if len(shape) < 4:
            return None
        h = int(shape[-2])
        w = int(shape[-1])
        if h <= 0 or w <= 0:
            return None
        return h, w

    @property
    def input_names(self) -> list[str]:
        return list(self._input_names)

    @property
    def output_names(self) -> list[str]:
        return list(self._output_names)

    def __call__(self, inputs_by_name: dict[str, Any]) -> dict[str, Any]:
        for name, tensor in list(inputs_by_name.items()):
            expected_dtype = self._trt_dtype_to_torch(self._engine.get_tensor_dtype(name))
            if tensor.dtype != expected_dtype:
                inputs_by_name[name] = tensor.to(expected_dtype)
            if not inputs_by_name[name].is_contiguous():
                inputs_by_name[name] = inputs_by_name[name].contiguous()
            self._context.set_input_shape(name, tuple(inputs_by_name[name].shape))

        outputs: dict[str, Any] = {}
        for name in self._output_names:
            shape = tuple(self._context.get_tensor_shape(name))
            dtype = self._trt_dtype_to_torch(self._engine.get_tensor_dtype(name))
            outputs[name] = self._torch.empty(shape, device="cuda", dtype=dtype)

        for name, tensor in inputs_by_name.items():
            self._context.set_tensor_address(name, int(tensor.data_ptr()))
        for name, tensor in outputs.items():
            self._context.set_tensor_address(name, int(tensor.data_ptr()))

        stream = self._torch.cuda.current_stream().cuda_stream
        if not self._context.execute_async_v3(stream):
            raise RuntimeError("TensorRT execution failed.")
        return outputs


@dataclass(frozen=True)
class StereoIntrinsics:
    fx: float
    fy: float
    cx: float
    cy: float
    baseline_m: float
    frame_id: str


@dataclass(frozen=True)
class StereoCalibration:
    intrinsics: StereoIntrinsics
    width: int
    height: int
    left_map1: np.ndarray
    left_map2: np.ndarray
    right_map1: np.ndarray
    right_map2: np.ndarray


class FastFoundationStereoNode(Node):
    def __init__(self) -> None:
        super().__init__("fast_foundation_stereo")

        self.declare_parameter("left_image_topic", "left/image_raw")
        self.declare_parameter("right_image_topic", "right/image_raw")
        self.declare_parameter("left_camera_info_topic", "left/camera_info")
        self.declare_parameter("right_camera_info_topic", "right/camera_info")
        self.declare_parameter("depth_topic", "depth/image_rect_raw")
        self.declare_parameter("disparity_topic", "disparity")
        self.declare_parameter("pointcloud_topic", "points")
        self.declare_parameter(
            "model_repo",
            "/home/jetson1/innate-os/ros2_ws/src/third_party/stereo_models/Fast-FoundationStereo",
        )
        self.declare_parameter("model_path", "")
        self.declare_parameter("inference_backend", "pytorch")
        self.declare_parameter("trt_engine_path", "")
        self.declare_parameter("trt_left_input_name", "left_image")
        self.declare_parameter("trt_right_input_name", "right_image")
        self.declare_parameter("trt_output_name", "disparity")
        self.declare_parameter("venv_path", "/home/jetson1/innate-os/.venvs/fast_foundation_stereo")
        self.declare_parameter("add_venv_site_packages", False)
        self.declare_parameter("disable_torch_compile_helpers", True)
        self.declare_parameter("sync_queue_size", 12)
        self.declare_parameter("sync_slop_sec", 0.03)
        self.declare_parameter("scale", 1.0)
        self.declare_parameter("valid_iters", 8)
        self.declare_parameter("max_disp", 192)
        self.declare_parameter("remove_invisible", True)
        self.declare_parameter("use_hierarchical", False)
        self.declare_parameter("use_amp", True)
        self.declare_parameter("synchronize_cuda_timing", True)
        self.declare_parameter("min_depth_m", 0.2)
        self.declare_parameter("max_depth_m", 5.0)
        self.declare_parameter("pointcloud_stride", 2)
        self.declare_parameter("publish_pointcloud", True)
        self.declare_parameter("fallback_baseline_m", 0.0)
        self.declare_parameter("log_inference_every_n", 30)
        self.declare_parameter("rectify_inputs", True)

        self.left_image_topic = str(self.get_parameter("left_image_topic").value)
        self.right_image_topic = str(self.get_parameter("right_image_topic").value)
        self.left_camera_info_topic = str(self.get_parameter("left_camera_info_topic").value)
        self.right_camera_info_topic = str(self.get_parameter("right_camera_info_topic").value)
        self.depth_topic = str(self.get_parameter("depth_topic").value)
        self.disparity_topic = str(self.get_parameter("disparity_topic").value)
        self.pointcloud_topic = str(self.get_parameter("pointcloud_topic").value)
        self.model_repo = Path(str(self.get_parameter("model_repo").value)).expanduser().resolve()
        self.model_path_param = str(self.get_parameter("model_path").value).strip()
        self.inference_backend = str(self.get_parameter("inference_backend").value).strip().lower()
        self.trt_engine_path_param = str(self.get_parameter("trt_engine_path").value).strip()
        self.trt_left_input_name = str(self.get_parameter("trt_left_input_name").value).strip()
        self.trt_right_input_name = str(self.get_parameter("trt_right_input_name").value).strip()
        self.trt_output_name = str(self.get_parameter("trt_output_name").value).strip()
        self.venv_path = Path(str(self.get_parameter("venv_path").value)).expanduser().resolve()
        self.add_venv_site_packages = bool(self.get_parameter("add_venv_site_packages").value)
        self.disable_torch_compile_helpers = bool(self.get_parameter("disable_torch_compile_helpers").value)
        self.sync_queue_size = int(self.get_parameter("sync_queue_size").value)
        self.sync_slop_sec = float(self.get_parameter("sync_slop_sec").value)
        self.scale = float(self.get_parameter("scale").value)
        self.valid_iters = int(self.get_parameter("valid_iters").value)
        self.max_disp = int(self.get_parameter("max_disp").value)
        self.remove_invisible = bool(self.get_parameter("remove_invisible").value)
        self.use_hierarchical = bool(self.get_parameter("use_hierarchical").value)
        self.use_amp = bool(self.get_parameter("use_amp").value)
        self.synchronize_cuda_timing = bool(self.get_parameter("synchronize_cuda_timing").value)
        self.min_depth_m = float(self.get_parameter("min_depth_m").value)
        self.max_depth_m = float(self.get_parameter("max_depth_m").value)
        self.pointcloud_stride = max(1, int(self.get_parameter("pointcloud_stride").value))
        self.publish_pointcloud = bool(self.get_parameter("publish_pointcloud").value)
        self.fallback_baseline_m = float(self.get_parameter("fallback_baseline_m").value)
        self.log_inference_every_n = max(1, int(self.get_parameter("log_inference_every_n").value))
        self.rectify_inputs = bool(self.get_parameter("rectify_inputs").value)

        self.bridge = CvBridge()
        self.left_info: CameraInfo | None = None
        self.right_info: CameraInfo | None = None
        self.calibration: StereoCalibration | None = None
        self._state_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self._frames_seen = 0
        self._frames_published = 0
        self._frames_dropped_busy = 0
        self._frames_unrectified = 0

        self._ensure_repo_on_path()
        self._torch: Any = self._import_module("torch")
        self._amp_dtype = self._torch.float16
        self.device = self._torch.device("cuda" if self._torch.cuda.is_available() else "cpu")
        if self.add_venv_site_packages:
            self._prepend_venv_site_packages()
        if self.disable_torch_compile_helpers:
            self._disable_compiled_helpers()
        input_padder_mod = self._import_module("core.utils.utils")
        self._input_padder_cls = getattr(input_padder_mod, "InputPadder")
        self.model = self._load_model()
        if self.inference_backend == "pytorch":
            self.model = self.model.to(self.device).eval()
        elif self.device.type != "cuda":
            raise RuntimeError("TensorRT backend requires CUDA.")

        self.depth_pub = self.create_publisher(Image, self.depth_topic, 10)
        self.disparity_pub = self.create_publisher(DisparityImage, self.disparity_topic, 10)
        self.pointcloud_pub = self.create_publisher(PointCloud2, self.pointcloud_topic, 10)

        self.create_subscription(CameraInfo, self.left_camera_info_topic, self._on_left_camera_info, 10)
        self.create_subscription(CameraInfo, self.right_camera_info_topic, self._on_right_camera_info, 10)

        left_sub = message_filters.Subscriber(self, Image, self.left_image_topic)
        right_sub = message_filters.Subscriber(self, Image, self.right_image_topic)
        sync = message_filters.ApproximateTimeSynchronizer(
            [left_sub, right_sub],
            queue_size=max(2, self.sync_queue_size),
            slop=max(0.001, self.sync_slop_sec),
        )
        sync.registerCallback(self._on_stereo_images)
        self._sync = sync
        self._left_sub = left_sub
        self._right_sub = right_sub

        self.get_logger().info(
            f"Fast-FoundationStereo ready: model={self._resolve_model_path()} device={self.device.type} "
            f"backend={self.inference_backend} inputs=({self.left_image_topic}, {self.right_image_topic}) "
            f"rectify={self.rectify_inputs}"
        )

    def _ensure_repo_on_path(self) -> None:
        if not self.model_repo.exists():
            raise RuntimeError(f"model_repo does not exist: {self.model_repo}")
        repo_text = str(self.model_repo)
        if repo_text not in sys.path:
            sys.path.insert(0, repo_text)

    def _prepend_venv_site_packages(self) -> None:
        candidates = sorted((self.venv_path / "lib").glob("python*/site-packages"))
        for site_dir in candidates:
            text = str(site_dir)
            if text not in sys.path:
                sys.path.insert(0, text)

    def _disable_compiled_helpers(self) -> None:
        helper_names = (
            "build_gwc_volume_optimized_pytorch1",
            "build_concat_volume_optimized_pytorch",
            "build_concat_volume_optimized_pytorch1",
        )
        module_names = ("core.submodule", "core.foundation_stereo")
        unwrapped = 0
        for module_name in module_names:
            module = importlib.import_module(module_name)
            for helper_name in helper_names:
                helper = getattr(module, helper_name, None)
                original = getattr(helper, "__wrapped__", None)
                if helper is not None and original is not None:
                    setattr(module, helper_name, original)
                    unwrapped += 1
        if unwrapped:
            self.get_logger().info(f"Disabled {unwrapped} torch.compile helper wrappers for Jetson compatibility.")

    def _import_module(self, module_name: str) -> Any:
        try:
            return __import__(module_name, fromlist=["*"])
        except Exception as exc:
            raise RuntimeError(
                f"Failed to import '{module_name}'. Install missing deps in system python, or set "
                f"add_venv_site_packages:=true with a compatible venv."
            ) from exc

    def _resolve_model_path(self) -> Path:
        if self.model_path_param and self.model_path_param.endswith(".engine"):
            return Path(self.model_path_param).expanduser().resolve()
        if self.model_path_param:
            return Path(self.model_path_param).expanduser().resolve()
        choices = sorted(self.model_repo.glob("weights/**/model_best_bp2_serialize.pth"))
        if choices:
            return choices[0]
        return self.model_repo / "weights" / "23-36-37" / "model_best_bp2_serialize.pth"

    def _resolve_trt_engine_path(self) -> Path:
        if self.trt_engine_path_param:
            return Path(self.trt_engine_path_param).expanduser().resolve()
        if self.model_path_param.endswith(".engine"):
            return Path(self.model_path_param).expanduser().resolve()
        if self.model_path_param.endswith(".onnx"):
            return Path(self.model_path_param).expanduser().resolve().with_suffix(".engine")
        candidates = sorted(self.model_repo.glob("engines/**/*.engine"))
        if candidates:
            return candidates[0]
        return self.model_repo / "engines" / "fast_foundationstereo.engine"

    def _load_model(self) -> Any:
        if self.inference_backend == "trt":
            return self._load_trt_runner()
        if self.inference_backend != "pytorch":
            raise RuntimeError(f"Unsupported inference_backend '{self.inference_backend}'. Use 'pytorch' or 'trt'.")
        model_path = self._resolve_model_path()
        if not model_path.exists():
            raise RuntimeError(
                f"Fast-FoundationStereo weights missing: {model_path}. "
                "Download a checkpoint into Fast-FoundationStereo/weights and set model_path if needed."
            )
        model = self._torch.load(str(model_path), map_location="cpu", weights_only=False)
        model.args.valid_iters = int(self.valid_iters)
        model.args.max_disp = int(self.max_disp)
        if hasattr(model.args, "mixed_precision"):
            model.args.mixed_precision = bool(self.use_amp)
        self._torch.set_grad_enabled(False)
        return model

    def _load_trt_runner(self) -> TrtEngineRunner:
        if self.device.type != "cuda":
            raise RuntimeError("TensorRT backend requires CUDA.")
        if not self.trt_left_input_name or not self.trt_right_input_name:
            raise RuntimeError("Set trt_left_input_name and trt_right_input_name.")
        engine_path = self._resolve_trt_engine_path()
        if not engine_path.exists():
            raise RuntimeError(
                f"TensorRT engine missing: {engine_path}. Export ONNX and build engine first."
            )
        runner = TrtEngineRunner(self._torch, engine_path)
        missing = [
            name
            for name in (self.trt_left_input_name, self.trt_right_input_name)
            if name not in runner.input_names
        ]
        if missing:
            raise RuntimeError(
                f"Engine inputs {missing} not found. Engine has inputs: {runner.input_names}"
            )
        return runner

    def _on_left_camera_info(self, msg: CameraInfo) -> None:
        with self._state_lock:
            self.left_info = msg
            self.calibration = self._try_build_calibration(self.left_info, self.right_info)

    def _on_right_camera_info(self, msg: CameraInfo) -> None:
        with self._state_lock:
            self.right_info = msg
            self.calibration = self._try_build_calibration(self.left_info, self.right_info)

    def _try_build_calibration(self, left: CameraInfo | None, right: CameraInfo | None) -> StereoCalibration | None:
        if left is None or right is None:
            return None
        if float(left.k[0]) <= 0.0 or float(right.k[0]) <= 0.0:
            return None

        width = int(left.width)
        height = int(left.height)
        if width <= 0 or height <= 0:
            return None

        p1 = np.asarray(left.p, dtype=np.float64).reshape(3, 4)
        p2 = np.asarray(right.p, dtype=np.float64).reshape(3, 4)
        k1 = np.asarray(left.k, dtype=np.float64).reshape(3, 3)
        k2 = np.asarray(right.k, dtype=np.float64).reshape(3, 3)

        fx = float(p1[0, 0]) if float(p1[0, 0]) > 0.0 else float(k1[0, 0])
        fy = float(p1[1, 1]) if float(p1[1, 1]) > 0.0 else float(k1[1, 1])
        cx = float(p1[0, 2]) if float(p1[0, 0]) > 0.0 else float(k1[0, 2])
        cy = float(p1[1, 2]) if float(p1[1, 1]) > 0.0 else float(k1[1, 2])

        baseline = 0.0
        if float(p2[0, 0]) > 0.0:
            baseline = abs(float(p2[0, 3]) / float(p2[0, 0]))
        if baseline <= 0.0:
            baseline = self.fallback_baseline_m
        if fx <= 0.0 or fy <= 0.0 or baseline <= 0.0:
            return None

        try:
            left_map1, left_map2 = cv2.initUndistortRectifyMap(
                k1,
                np.asarray(left.d, dtype=np.float64),
                np.asarray(left.r, dtype=np.float64).reshape(3, 3),
                p1[:, :3],
                (width, height),
                cv2.CV_32FC1,
            )
            right_map1, right_map2 = cv2.initUndistortRectifyMap(
                k2,
                np.asarray(right.d, dtype=np.float64),
                np.asarray(right.r, dtype=np.float64).reshape(3, 3),
                p2[:, :3],
                (width, height),
                cv2.CV_32FC1,
            )
        except Exception as exc:
            self.get_logger().warn(f"Failed to build rectification maps: {exc}")
            return None

        frame_id = left.header.frame_id or right.header.frame_id or "camera_optical_frame"
        return StereoCalibration(
            intrinsics=StereoIntrinsics(fx=fx, fy=fy, cx=cx, cy=cy, baseline_m=float(baseline), frame_id=frame_id),
            width=width,
            height=height,
            left_map1=left_map1,
            left_map2=left_map2,
            right_map1=right_map1,
            right_map2=right_map2,
        )

    def _to_rgb(self, msg: Image) -> np.ndarray:
        if msg.encoding in ("rgb8", "bgr8", "mono8"):
            arr = self.bridge.imgmsg_to_cv2(msg, desired_encoding=msg.encoding)
        else:
            arr = self.bridge.imgmsg_to_cv2(msg, desired_encoding="bgr8")
            return cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)

        if msg.encoding == "rgb8":
            return arr
        if msg.encoding == "bgr8":
            return cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)
        if msg.encoding == "mono8":
            return cv2.cvtColor(arr, cv2.COLOR_GRAY2RGB)
        return cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)

    def _rectify_pair(
        self,
        left_rgb: np.ndarray,
        right_rgb: np.ndarray,
        calibration: StereoCalibration,
    ) -> tuple[np.ndarray, np.ndarray]:
        if not self.rectify_inputs:
            self._frames_unrectified += 1
            return left_rgb, right_rgb
        h, w = left_rgb.shape[:2]
        if (w, h) != (calibration.width, calibration.height) or right_rgb.shape[:2] != (h, w):
            self._frames_unrectified += 1
            if self._frames_unrectified % self.log_inference_every_n == 1:
                self.get_logger().warn(
                    "Skipping rectification: frame/camera_info shape mismatch "
                    f"frame={w}x{h} calib={calibration.width}x{calibration.height}"
                )
            return left_rgb, right_rgb
        left_rect = cv2.remap(left_rgb, calibration.left_map1, calibration.left_map2, interpolation=cv2.INTER_LINEAR)
        right_rect = cv2.remap(right_rgb, calibration.right_map1, calibration.right_map2, interpolation=cv2.INTER_LINEAR)
        return left_rect, right_rect

    def _infer_disparity(self, left_rgb: np.ndarray, right_rgb: np.ndarray) -> tuple[np.ndarray, float]:
        original_h, original_w = left_rgb.shape[:2]
        if self.scale != 1.0:
            scaled_w = max(32, int(round(original_w * self.scale)))
            scaled_h = max(32, int(round(original_h * self.scale)))
            left_proc = cv2.resize(left_rgb, (scaled_w, scaled_h), interpolation=cv2.INTER_LINEAR)
            right_proc = cv2.resize(right_rgb, (scaled_w, scaled_h), interpolation=cv2.INTER_LINEAR)
        else:
            left_proc = left_rgb
            right_proc = right_rgb

        proc_h, proc_w = left_proc.shape[:2]
        disp_np: np.ndarray
        if self.device.type == "cuda" and self.synchronize_cuda_timing:
            self._torch.cuda.synchronize(device=self.device)
        start = time.perf_counter()
        if self.inference_backend == "trt":
            if not isinstance(self.model, TrtEngineRunner):
                raise RuntimeError("TensorRT backend selected but no TensorRT runner is loaded.")
            expected_hw = self.model.expected_input_hw(self.trt_left_input_name)
            if expected_hw is not None and expected_hw != (proc_h, proc_w):
                eh, ew = expected_hw
                left_proc = cv2.resize(left_proc, (ew, eh), interpolation=cv2.INTER_LINEAR)
                right_proc = cv2.resize(right_proc, (ew, eh), interpolation=cv2.INTER_LINEAR)
                proc_h, proc_w = eh, ew
            left_norm = ((left_proc.astype(np.float32) / 255.0) - IMAGENET_MEAN) / IMAGENET_STD
            right_norm = ((right_proc.astype(np.float32) / 255.0) - IMAGENET_MEAN) / IMAGENET_STD
            left_tensor = self._torch.as_tensor(left_norm, device=self.device).float()[None].permute(0, 3, 1, 2)
            right_tensor = self._torch.as_tensor(right_norm, device=self.device).float()[None].permute(0, 3, 1, 2)
            outputs = self.model(
                {
                    self.trt_left_input_name: left_tensor,
                    self.trt_right_input_name: right_tensor,
                }
            )
            if self.trt_output_name in outputs:
                disp_tensor = outputs[self.trt_output_name]
            elif outputs:
                disp_tensor = outputs[next(iter(outputs.keys()))]
            else:
                raise RuntimeError("TensorRT runner returned no outputs.")
            disp_np = disp_tensor.detach().float().cpu().numpy().reshape(proc_h, proc_w).astype(np.float32)
        else:
            left_tensor = self._torch.as_tensor(left_proc, device=self.device).float()[None].permute(0, 3, 1, 2)
            right_tensor = self._torch.as_tensor(right_proc, device=self.device).float()[None].permute(0, 3, 1, 2)
            padder = self._input_padder_cls(left_tensor.shape, divis_by=32, force_square=False)
            left_tensor, right_tensor = padder.pad(left_tensor, right_tensor)
            with self._torch.inference_mode():
                with self._torch.amp.autocast(
                    "cuda",
                    enabled=bool(self.use_amp and self.device.type == "cuda"),
                    dtype=self._amp_dtype,
                ):
                    if self.use_hierarchical:
                        disp_tensor = self.model.run_hierachical(
                            left_tensor,
                            right_tensor,
                            iters=int(self.valid_iters),
                            test_mode=True,
                            small_ratio=0.5,
                        )
                    else:
                        disp_tensor = self.model.forward(
                            left_tensor,
                            right_tensor,
                            iters=int(self.valid_iters),
                            test_mode=True,
                            optimize_build_volume="pytorch1",
                        )
            disp = padder.unpad(disp_tensor.float())
            disp_np = disp.detach().cpu().numpy().reshape(proc_h, proc_w).astype(np.float32)
        if self.device.type == "cuda" and self.synchronize_cuda_timing:
            self._torch.cuda.synchronize(device=self.device)
        infer_ms = 1000.0 * (time.perf_counter() - start)

        if (proc_h, proc_w) != (original_h, original_w):
            disp_np = cv2.resize(disp_np, (original_w, original_h), interpolation=cv2.INTER_LINEAR)
        if proc_w != original_w:
            disp_np = disp_np / (float(proc_w) / float(original_w))
        disp_np = np.clip(disp_np, 0.0, None)
        if self.remove_invisible:
            xx = np.broadcast_to(
                np.arange(disp_np.shape[1], dtype=np.float32)[None, :],
                disp_np.shape,
            )
            disp_np[(xx - disp_np) < 0.0] = np.inf
        return disp_np, infer_ms

    def _depth_from_disparity(self, disparity: np.ndarray, intrinsics: StereoIntrinsics) -> np.ndarray:
        depth = np.full(disparity.shape, np.nan, dtype=np.float32)
        valid = np.isfinite(disparity) & (disparity > 0.0)
        if np.any(valid):
            depth[valid] = intrinsics.fx * intrinsics.baseline_m / disparity[valid]
        range_valid = np.isfinite(depth) & (depth >= self.min_depth_m) & (depth <= self.max_depth_m)
        depth[~range_valid] = np.nan
        return depth

    def _publish_depth(self, header: Any, depth_m: np.ndarray) -> None:
        depth_mm = np.zeros(depth_m.shape, dtype=np.uint16)
        valid = np.isfinite(depth_m) & (depth_m > 0.0)
        if np.any(valid):
            mm = np.clip(np.rint(depth_m[valid] * 1000.0), 1.0, 65535.0).astype(np.uint16)
            depth_mm[valid] = mm
        msg = Image()
        msg.header = header
        msg.height = int(depth_mm.shape[0])
        msg.width = int(depth_mm.shape[1])
        msg.encoding = "16UC1"
        msg.is_bigendian = False
        msg.step = int(depth_mm.shape[1] * 2)
        msg.data = depth_mm.tobytes()
        self.depth_pub.publish(msg)

    def _publish_disparity(self, header: Any, disparity: np.ndarray, intrinsics: StereoIntrinsics) -> None:
        image_msg = Image()
        image_msg.header = header
        image_msg.height = int(disparity.shape[0])
        image_msg.width = int(disparity.shape[1])
        image_msg.encoding = "32FC1"
        image_msg.is_bigendian = False
        image_msg.step = int(disparity.shape[1] * 4)
        image_msg.data = disparity.astype(np.float32, copy=False).tobytes()

        disp_msg = DisparityImage()
        disp_msg.header = header
        disp_msg.image = image_msg
        disp_msg.f = float(intrinsics.fx)
        disp_msg.t = float(intrinsics.baseline_m)
        disp_msg.min_disparity = 0.0
        disp_msg.max_disparity = float(max(1, self.max_disp))
        disp_msg.delta_d = 1.0
        self.disparity_pub.publish(disp_msg)

    def _publish_pointcloud(self, header: Any, depth_m: np.ndarray, intrinsics: StereoIntrinsics) -> None:
        if not self.publish_pointcloud:
            return
        if self.pointcloud_pub.get_subscription_count() == 0:
            return

        stride = max(1, self.pointcloud_stride)
        depth = depth_m[::stride, ::stride]
        h, w = depth.shape
        uu, vv = np.meshgrid(
            np.arange(w, dtype=np.float32) * stride,
            np.arange(h, dtype=np.float32) * stride,
            indexing="xy",
        )
        valid = np.isfinite(depth) & (depth > 0.0)
        if not np.any(valid):
            cloud = point_cloud2.create_cloud_xyz32(header, [])
            self.pointcloud_pub.publish(cloud)
            return

        z = depth[valid]
        x = (uu[valid] - intrinsics.cx) * z / intrinsics.fx
        y = (vv[valid] - intrinsics.cy) * z / intrinsics.fy
        points = np.column_stack((x, y, z)).astype(np.float32, copy=False)
        cloud = point_cloud2.create_cloud_xyz32(header, points.tolist())
        self.pointcloud_pub.publish(cloud)

    def _on_stereo_images(self, left_msg: Image, right_msg: Image) -> None:
        self._frames_seen += 1
        if not self._infer_lock.acquire(blocking=False):
            self._frames_dropped_busy += 1
            return
        try:
            with self._state_lock:
                calibration = self.calibration
            if calibration is None:
                if self._frames_seen % self.log_inference_every_n == 1:
                    self.get_logger().warn("Waiting for valid stereo calibration from camera_info.")
                return
            intrinsics = calibration.intrinsics

            left_rgb = self._to_rgb(left_msg)
            right_rgb = self._to_rgb(right_msg)
            left_rgb, right_rgb = self._rectify_pair(left_rgb, right_rgb, calibration)
            disparity, infer_ms = self._infer_disparity(left_rgb, right_rgb)
            depth = self._depth_from_disparity(disparity, intrinsics)

            header = left_msg.header
            if not header.frame_id:
                header.frame_id = intrinsics.frame_id
            self._publish_depth(header, depth)
            self._publish_disparity(header, disparity, intrinsics)
            self._publish_pointcloud(header, depth, intrinsics)
            self._frames_published += 1

            if self._frames_published % self.log_inference_every_n == 0:
                self.get_logger().info(
                    f"published={self._frames_published} infer={infer_ms:.1f}ms "
                    f"dropped_busy={self._frames_dropped_busy}"
                )
        except Exception as exc:
            self.get_logger().error(f"Inference callback failed: {exc}")
        finally:
            self._infer_lock.release()


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node: FastFoundationStereoNode | None = None
    try:
        node = FastFoundationStereoNode()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            try:
                node.destroy_node()
            except Exception:
                pass
        if rclpy.ok():
            try:
                rclpy.shutdown()
            except Exception:
                pass


if __name__ == "__main__":
    main()
