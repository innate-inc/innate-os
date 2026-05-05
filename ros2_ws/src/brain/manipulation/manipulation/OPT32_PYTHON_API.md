# Python API

This document is generated at export time from the packaged runtime metadata
and the original source-policy module when it is available.

## Public Entry Points

- Import through `ACT.py` or `policy.py`, not `opt32_client_runtime` directly.
  The client bundle preloads hidden support libraries before importing the compiled module.
- `ACT.py: ACTPolicy(package_dir=..., **kwargs)`
- `policy.py: load_policy(*, family=None, package_dir=..., **kwargs)`

## Wrapper Construction

- `ACT.py` subclasses the compiled `opt32_client_runtime.ACTPolicy` class.
- The shim only injects the default packaged `package_dir`; runtime execution stays in the compiled module.
- The runtime `ACTConfig` is reconstructed from the packaged architecture metadata, not from live source code.
- `policy.py` delegates to the manifest-driven `opt32_client_runtime.load_policy(...)` entry point.

## Source ACTConfig Fields

Configuration for the Action Chunking Transformer policy.

- `n_obs_steps: int = 1`
- `chunk_size: int = 100`
- `n_action_steps: int = 100`
- `speed: float = 1.0`
- `input_shapes: Dict[str, List[int]] = field(default_factory=dict)`
- `output_shapes: Dict[str, List[int]] = field(default_factory=dict)`
- `normalization_mapping: Dict[str, str] = field(default_factory=lambda : {FeatureType.VISUAL.value: NormalizationMode.MEAN_STD.value, FeatureType.STATE.value: NormalizationMode.MEAN_STD.value, FeatureType.ACTION.value: NormalizationMode.MEAN_STD.value, FeatureType.ENVIRONMENT_STATE.value: NormalizationMode.MEAN_STD.value})`
- `vision_backbone: str = 'resnet18'`
- `pretrained_backbone_weights: Optional[str] = 'ResNet18_Weights.IMAGENET1K_V1'`
- `replace_final_stride_with_dilation: bool = False`
- `pre_norm: bool = False`
- `dim_model: int = 512`
- `n_heads: int = 8`
- `dim_feedforward: int = 3200`
- `feedforward_activation: str = 'relu'`
- `n_encoder_layers: int = 4`
- `n_decoder_layers: int = 1`
- `use_vae: bool = True`
- `latent_dim: int = 32`
- `n_vae_encoder_layers: int = 4`
- `temporal_ensemble_coeff: Optional[float] = None`
- `dropout: float = 0.1`
- `kl_weight: float = 10.0`
- `optimizer_lr: float = 1e-05`
- `optimizer_weight_decay: float = 0.0001`
- `optimizer_lr_backbone: float = 1e-05`

## Source ACTPolicy Surface

- `__init__(self, config: ACTConfig, dataset_stats: Optional[Dict[str, Dict[str, Tensor]]] = None)`
- `get_optim_params(self) -> Dict`
- `reset(self)`
- `select_action(self, batch: Dict[str, Tensor]) -> Tensor`
- `forward(self, batch: Dict[str, Tensor]) -> Tuple[Tensor, Dict]`

## Runtime Wrapper Surface

- `reset()`: Supported. Resets serving state.
- `close()`: Runtime-only. Releases the native runtime session eagerly.
- `select_action(batch)`: Supported. Primary serving inference API.
- `forward(batch)`: Inference-only adaptation. Delegates to serving inference and does not preserve training-loss behavior.
- `__call__(batch)`: Inference-only adaptation. Delegates to serving inference.
- `eval()`: Supported. Returns self for source-level compatibility.
- `to(device)`: Supported. Records the requested device string on the wrapper.

Runtime properties:
- `config`: Runtime ACTConfig reconstructed from the packaged architecture metadata.
- `device`: Current device string tracked by the wrapper.
- `package_dir`: Resolved runtime package directory used by the native session.

## Source Methods Not Preserved

- `get_optim_params(self) -> Dict`: Not exposed by the runtime wrapper.

## Notes

- This is an inference-only runtime wrapper.
- The compiled wrapper preserves the serving contract, not the training internals.
- Call `reset()` before starting a new serving episode when the source policy expects stateful serving semantics.
