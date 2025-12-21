import math
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple, Dict, List
from enum import Enum

import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision
from torch import Tensor
from torchvision.models._utils import IntermediateLayerGetter
from torchvision.ops.misc import FrozenBatchNorm2d
import numpy as np
import einops
from itertools import chain # For _reset_parameters

# --- Start of Normalization Utility Classes and Functions ---

class NormalizationMode(Enum):
    IDENTITY = "identity"
    MEAN_STD = "mean_std"
    MIN_MAX = "min_max"

class FeatureType(Enum):
    VISUAL = "VISUAL"
    STATE = "STATE"
    ACTION = "ACTION"
    ENVIRONMENT_STATE = "ENVIRONMENT_STATE" # Added for completeness

@dataclass
class PolicyFeature:
    name: str
    shape: List[int]
    type: FeatureType

def _no_stats_error_str(name: str) -> str:
    return (
        f"`{name}` is infinity. You should either initialize `ACTPolicy` with `dataset_stats`, "
        "or ensure stats are loaded from a pretrained model's state_dict."
    )

def create_stats_buffers(
    features: Dict[str, PolicyFeature],
    norm_map: Dict[str, NormalizationMode],
    stats: Optional[Dict[str, Dict[str, Tensor]]] = None,
) -> Dict[str, nn.ParameterDict]:
    stats_buffers = {}

    for key, ft in features.items():
        norm_mode_str = norm_map.get(ft.type.value, NormalizationMode.IDENTITY.value)
        norm_mode = NormalizationMode(norm_mode_str)

        if norm_mode == NormalizationMode.IDENTITY:
            continue

        shape = list(ft.shape) # Ensure it's a list for modification

        if ft.type == FeatureType.VISUAL:
            assert len(shape) == 3, f"Number of dimensions of {key} != 3 ({shape=})"
            c, h, w = shape
            assert c < h and c < w, f"{key} is not channel first ({shape=})"
            # Override image shape to be invariant to height and width for stats
            shape = [c, 1, 1]

        buffer_dict = {}
        if norm_mode == NormalizationMode.MEAN_STD:
            mean = torch.ones(shape, dtype=torch.float32) * torch.inf
            std = torch.ones(shape, dtype=torch.float32) * torch.inf
            buffer_dict = {
                "mean": nn.Parameter(mean, requires_grad=False),
                "std": nn.Parameter(std, requires_grad=False),
            }
        elif norm_mode == NormalizationMode.MIN_MAX:
            min_val = torch.ones(shape, dtype=torch.float32) * torch.inf
            max_val = torch.ones(shape, dtype=torch.float32) * torch.inf # Renamed to avoid conflict with builtin max
            buffer_dict = {
                "min": nn.Parameter(min_val, requires_grad=False),
                "max": nn.Parameter(max_val, requires_grad=False),
            }
        
        buffer = nn.ParameterDict(buffer_dict)

        if stats and key in stats:
            # Ensure stats tensors are float32
            if norm_mode == NormalizationMode.MEAN_STD:
                if "mean" in stats[key]:
                    buffer["mean"].data = stats[key]["mean"].clone().to(dtype=torch.float32).view(shape)
                if "std" in stats[key]:
                    buffer["std"].data = stats[key]["std"].clone().to(dtype=torch.float32).view(shape)
            elif norm_mode == NormalizationMode.MIN_MAX:
                if "min" in stats[key]:
                    buffer["min"].data = stats[key]["min"].clone().to(dtype=torch.float32).view(shape)
                if "max" in stats[key]:
                    buffer["max"].data = stats[key]["max"].clone().to(dtype=torch.float32).view(shape)
        
        stats_buffers[key] = buffer
    return stats_buffers

class Normalize(nn.Module):
    def __init__(
        self,
        features: Dict[str, PolicyFeature],
        norm_map: Dict[str, NormalizationMode], # Dict[FeatureType_str, NormalizationMode_str]
        stats: Optional[Dict[str, Dict[str, Tensor]]] = None,
    ):
        super().__init__()
        self.features = features
        self.norm_map = norm_map # Dict[FeatureType_value, NormalizationMode_value]
        
        # Convert norm_map values from string to Enum for create_stats_buffers
        self.norm_map_enum = {k: NormalizationMode(v) for k,v in norm_map.items()}

        stats_buffers_dict = create_stats_buffers(features, self.norm_map, stats)
        # Register buffers as submodules or attributes to ensure they are part of the model state
        for key, buffer_param_dict in stats_buffers_dict.items():
            self.add_module("buffer_" + key.replace(".", "_"), buffer_param_dict)


    @torch.no_grad()
    def forward(self, batch: Dict[str, Tensor]) -> Dict[str, Tensor]:
        # Create a shallow copy to avoid modifying the original batch in place if it's passed around
        processed_batch = dict(batch)
        for key, ft in self.features.items():
            if key not in processed_batch:
                continue

            norm_mode_str = self.norm_map.get(ft.type.value, NormalizationMode.IDENTITY.value)
            norm_mode = NormalizationMode(norm_mode_str)

            if norm_mode == NormalizationMode.IDENTITY:
                continue

            buffer_name = "buffer_" + key.replace(".", "_")
            if not hasattr(self, buffer_name):
                # This can happen if a feature was in self.features but had no valid norm_mode
                # or wasn't processed by create_stats_buffers correctly for some reason.
                print(f"Warning: No normalization buffer found for {key}. Skipping normalization.")
                continue
                
            buffer = getattr(self, buffer_name)

            if norm_mode == NormalizationMode.MEAN_STD:
                mean = buffer["mean"]
                std = buffer["std"]
                if torch.isinf(mean).any(): raise ValueError(_no_stats_error_str(f"{key} mean"))
                if torch.isinf(std).any(): raise ValueError(_no_stats_error_str(f"{key} std"))
                processed_batch[key] = (processed_batch[key] - mean) / (std + 1e-8)
            elif norm_mode == NormalizationMode.MIN_MAX:
                min_val = buffer["min"] # Renamed to avoid conflict
                max_val = buffer["max"] # Renamed to avoid conflict
                if torch.isinf(min_val).any(): raise ValueError(_no_stats_error_str(f"{key} min"))
                if torch.isinf(max_val).any(): raise ValueError(_no_stats_error_str(f"{key} max"))
                
                normalized_to_0_1 = (processed_batch[key] - min_val) / (max_val - min_val + 1e-8)
                processed_batch[key] = normalized_to_0_1 * 2.0 - 1.0
            else:
                raise ValueError(f"Unsupported normalization mode: {norm_mode}")
        return processed_batch

class Unnormalize(nn.Module):
    def __init__(
        self,
        features: Dict[str, PolicyFeature],
        norm_map: Dict[str, NormalizationMode], # Dict[FeatureType_str, NormalizationMode_str]
        stats: Optional[Dict[str, Dict[str, Tensor]]] = None,
    ):
        super().__init__()
        self.features = features
        self.norm_map = norm_map # Dict[FeatureType_value, NormalizationMode_value]
        
        self.norm_map_enum = {k: NormalizationMode(v) for k,v in norm_map.items()}

        stats_buffers_dict = create_stats_buffers(features, self.norm_map, stats)
        for key, buffer_param_dict in stats_buffers_dict.items():
            self.add_module("buffer_" + key.replace(".", "_"), buffer_param_dict)

    @torch.no_grad()
    def forward(self, batch: Dict[str, Tensor]) -> Dict[str, Tensor]:
        processed_batch = dict(batch)
        for key, ft in self.features.items():
            if key not in processed_batch:
                continue
            
            norm_mode_str = self.norm_map.get(ft.type.value, NormalizationMode.IDENTITY.value)
            norm_mode = NormalizationMode(norm_mode_str)

            if norm_mode == NormalizationMode.IDENTITY:
                continue

            buffer_name = "buffer_" + key.replace(".", "_")
            if not hasattr(self, buffer_name):
                print(f"Warning: No normalization buffer found for {key}. Skipping unnormalization.")
                continue

            buffer = getattr(self, buffer_name)

            if norm_mode == NormalizationMode.MEAN_STD:
                mean = buffer["mean"]
                std = buffer["std"]
                if torch.isinf(mean).any(): raise ValueError(_no_stats_error_str(f"{key} mean"))
                if torch.isinf(std).any(): raise ValueError(_no_stats_error_str(f"{key} std"))
                processed_batch[key] = processed_batch[key] * std + mean
            elif norm_mode == NormalizationMode.MIN_MAX:
                min_val = buffer["min"]
                max_val = buffer["max"]
                if torch.isinf(min_val).any(): raise ValueError(_no_stats_error_str(f"{key} min"))
                if torch.isinf(max_val).any(): raise ValueError(_no_stats_error_str(f"{key} max"))
                
                unnormalized_from_0_1 = (processed_batch[key] + 1.0) / 2.0
                processed_batch[key] = unnormalized_from_0_1 * (max_val - min_val) + min_val
            else:
                raise ValueError(f"Unsupported normalization mode: {norm_mode}")
        return processed_batch

# --- End of Normalization Utility Classes and Functions ---

@dataclass
class ACTConfig:
    """Configuration for the Action Chunking Transformer policy."""
    # Input / output structure
    n_obs_steps: int = 1
    chunk_size: int = 100
    n_action_steps: int = 100
    speed: float = 1.0
    input_shapes: Dict[str, List[int]] = field(default_factory=dict)
    output_shapes: Dict[str, List[int]] = field(default_factory=dict)
    # Example: input_shapes = {"observation.image_rgb": [3, 224, 224], "observation.state": [7]}
    #          output_shapes = {"action": [7]}
    
    normalization_mapping: Dict[str, str] = field(default_factory=lambda: {
        FeatureType.VISUAL.value: NormalizationMode.MEAN_STD.value,
        FeatureType.STATE.value: NormalizationMode.MEAN_STD.value,
        FeatureType.ACTION.value: NormalizationMode.MEAN_STD.value,
        FeatureType.ENVIRONMENT_STATE.value: NormalizationMode.MEAN_STD.value,
    })
    
    # Architecture
    vision_backbone: str = "resnet18"
    pretrained_backbone_weights: Optional[str] = "ResNet18_Weights.IMAGENET1K_V1"
    replace_final_stride_with_dilation: bool = False
    
    # Transformer parameters
    pre_norm: bool = False
    dim_model: int = 512
    n_heads: int = 8
    dim_feedforward: int = 3200
    feedforward_activation: str = "relu"
    n_encoder_layers: int = 4
    n_decoder_layers: int = 1
    
    # VAE parameters
    use_vae: bool = True
    latent_dim: int = 32
    n_vae_encoder_layers: int = 4
    
    # Inference
    temporal_ensemble_coeff: Optional[float] = None
    
    # Training
    dropout: float = 0.1
    kl_weight: float = 10.0
    
    # Optimizer settings
    optimizer_lr: float = 1e-5
    optimizer_weight_decay: float = 1e-4
    optimizer_lr_backbone: float = 1e-5

    @property
    def image_input_keys(self) -> List[str]:
        """Returns a sorted list of keys for image observations."""
        return sorted([k for k in self.input_shapes if k.startswith("observation.image")])

    def _get_policy_features(self, shapes_dict: Dict[str, List[int]]) -> Dict[str, PolicyFeature]:
        features = {}
        for key, shape in shapes_dict.items():
            ftype = None
            if key.startswith("observation.image"):
                ftype = FeatureType.VISUAL
            elif key == "observation.state":
                ftype = FeatureType.STATE
            elif key == "observation.environment_state":
                ftype = FeatureType.ENVIRONMENT_STATE
            elif key == "action":
                ftype = FeatureType.ACTION
            
            if ftype:
                features[key] = PolicyFeature(name=key, shape=list(shape), type=ftype)
            else:
                # Default or raise error if a key in shapes_dict cannot be typed
                # For now, we'll just skip features we can't type for normalization purposes.
                # A more robust solution might involve explicit type mapping in config.
                print(f"Warning: Could not infer FeatureType for '{key}'. It will not be normalized/unnormalized by default.")
        return features

    @property
    def input_features(self) -> Dict[str, PolicyFeature]:
        return self._get_policy_features(self.input_shapes)

    @property
    def output_features(self) -> Dict[str, PolicyFeature]:
        # Typically, output_features for normalization refers to "action"
        return self._get_policy_features(self.output_shapes)

    def __post_init__(self):
        if not self.vision_backbone.startswith("resnet"):
            raise ValueError(f"vision_backbone must be a ResNet variant. Got {self.vision_backbone}")
        if self.temporal_ensemble_coeff is not None and self.n_action_steps > 1:
            raise ValueError("n_action_steps must be 1 when using temporal ensembling")
        if self.n_action_steps > self.chunk_size:
            raise ValueError("n_action_steps cannot be greater than chunk_size")

class ACTPolicy(nn.Module):
    def __init__(self, config: ACTConfig, dataset_stats: Optional[Dict[str, Dict[str, Tensor]]] = None):
        super().__init__()
        self.config = config
        
        # Normalization modules
        self.normalize_inputs = Normalize(config.input_features, config.normalization_mapping, dataset_stats)
        # For normalizing target actions for VAE and loss
        self.normalize_targets = Normalize(config.output_features, config.normalization_mapping, dataset_stats)
        # For unnormalizing predicted actions
        self.unnormalize_outputs = Unnormalize(config.output_features, config.normalization_mapping, dataset_stats)
        
        self.model = ACT(config)
        
        if config.temporal_ensemble_coeff is not None:
            # Calculate effective chunk size after resampling
            effective_chunk_size = int(config.chunk_size / config.speed)
            self.temporal_ensembler = ACTTemporalEnsembler(
                config.temporal_ensemble_coeff, 
                effective_chunk_size
            )
        
        self.reset()

    def get_optim_params(self) -> Dict:
        return [
            {
                "params": [
                    p for n, p in self.named_parameters()
                    if not n.startswith("model.backbone") and p.requires_grad
                ]
            },
            {
                "params": [
                    p for n, p in self.named_parameters()
                    if n.startswith("model.backbone") and p.requires_grad
                ],
                "lr": self.config.optimizer_lr_backbone,
            },
        ]

    def reset(self):
        if self.config.temporal_ensemble_coeff is not None:
            self.temporal_ensembler.reset()
        else:
            self._action_queue = deque([], maxlen=self.config.n_action_steps)

    def _prepare_batch_for_model(self, batch: Dict[str, Tensor]) -> Dict[str, Tensor]:
        # Create a shallow copy to avoid modifying the original batch
        processed_batch = dict(batch)
        if self.config.image_input_keys:
            # Ensure that the image keys exist in the processed_batch before trying to access them
            images_to_collate = []
            for key in self.config.image_input_keys:
                if key in processed_batch:
                    images_to_collate.append(processed_batch[key])
                else:
                    # This case should be handled by the caller ensuring all declared inputs are present
                    # or the config reflecting only available inputs.
                    print(f"Warning: Image key '{key}' declared in 'image_input_keys' but not found in batch for _prepare_batch_for_model.")
            if images_to_collate:
                 processed_batch["observation.images"] = images_to_collate
            elif "observation.images" not in processed_batch and self.config.image_input_keys:
                # If no images were found but were expected, this might be an issue.
                # For now, if "observation.images" is already populated (e.g. by a data loader), we use it.
                # Otherwise, if keys were expected but not found, this could be an error state.
                # However, `ACT.forward` is robust to `observation.images` not being present if no image_input_keys are in config.
                pass

        # Ensure 'action' and 'action_is_pad' are in model_batch if they exist in the input batch
        # (they would be the normalized versions if this function is called after normalize_targets)
        if "action" in batch and "action" not in processed_batch:
            processed_batch["action"] = batch["action"]
        if "action_is_pad" in batch and "action_is_pad" not in processed_batch:
            processed_batch["action_is_pad"] = batch["action_is_pad"]
            
        return processed_batch

    @torch.no_grad()
    def select_action(self, batch: Dict[str, Tensor]) -> Tensor:
        self.eval()
        # Create a working copy of the batch to avoid in-place modification of external batch
        working_batch = {k: v.clone() if isinstance(v, Tensor) else v for k, v in batch.items()}
        
        # Normalize inputs (observations)
        working_batch = self.normalize_inputs(working_batch) 
        
        model_batch = self._prepare_batch_for_model(working_batch) 

        if self.config.temporal_ensemble_coeff is not None:
            actions_chunk_normalized = self.model(model_batch)[0] 
            actions_chunk = self.unnormalize_outputs({"action": actions_chunk_normalized})["action"]
            
            # Resample actions based on speed parameter
            if self.config.speed != 1:
                actions_chunk = self._resample_actions(actions_chunk, self.config.speed)
            
            action = self.temporal_ensembler.update(actions_chunk)
            return action

        if len(self._action_queue) == 0:
            actions_chunk_normalized = self.model(model_batch)[0] 
            actions_to_queue_normalized = actions_chunk_normalized[:, :self.config.n_action_steps]
            actions_to_queue = self.unnormalize_outputs({"action": actions_to_queue_normalized})["action"]
            
            # Resample actions based on speed parameter
            if self.config.speed != 1:
                actions_to_queue = self._resample_actions(actions_to_queue, self.config.speed)
            
            self._action_queue.extend(actions_to_queue.transpose(0, 1))
        
        return self._action_queue.popleft()

    def _resample_actions(self, actions: Tensor, speed: float) -> Tensor:
        """Linearly resample actions based on speed factor.
        
        Args:
            actions: Input tensor of shape (batch, seq_len, action_dim)
            speed: Speed factor. If > 1, downsample. If < 1, upsample. If = 1, no change.
            
        Returns:
            Resampled actions tensor
        """
        batch_size, seq_len, action_dim = actions.shape
        
        new_seq_len = int(seq_len / speed)
        
        if new_seq_len == 0:
            raise ValueError(f"Speed factor {speed} results in zero sequence length for input length {seq_len}")
        
        if new_seq_len == seq_len:
            # No resampling needed
            return actions
        
        # Use interpolate to linearly resample along the sequence dimension
        # actions: (batch, seq_len, action_dim) -> (batch, action_dim, seq_len)
        actions_transposed = actions.transpose(1, 2)
        
        # Interpolate along the last dimension (sequence)
        resampled = F.interpolate(
            actions_transposed, 
            size=new_seq_len, 
            mode='linear', 
            align_corners=True
        )
        
        # Transpose back: (batch, action_dim, new_seq_len) -> (batch, new_seq_len, action_dim)
        return resampled.transpose(1, 2)

    def forward(self, batch: Dict[str, Tensor]) -> Tuple[Tensor, Dict]:
        # Create a working copy of the batch to avoid in-place modification
        working_batch = {k: v.clone() if isinstance(v, Tensor) else v for k, v in batch.items()}
        
        working_batch = self.normalize_inputs(working_batch) 
        working_batch = self.normalize_targets(working_batch) 
        model_batch = self._prepare_batch_for_model(working_batch)
        
        actions_hat, (mu_hat, log_sigma_x2_hat) = self.model(model_batch)
        
        normalized_target_actions = working_batch["action"] 
        l1_loss = F.l1_loss(normalized_target_actions, actions_hat, reduction="none")
        
        if "action_is_pad" in working_batch: # Use padding info from the (potentially modified) working_batch
            action_is_pad = working_batch["action_is_pad"]
            l1_loss = (l1_loss * ~action_is_pad.unsqueeze(-1)).sum() / ((~action_is_pad.unsqueeze(-1)).sum() + 1e-8) # Added epsilon for stability
        else:
            l1_loss = l1_loss.mean()

        loss_dict = {"l1_loss": l1_loss.item()}
        
        if self.config.use_vae and mu_hat is not None and log_sigma_x2_hat is not None: # Check VAE was active
            # KL divergence
            mean_kld = (-0.5 * (1 + log_sigma_x2_hat - mu_hat.pow(2) - log_sigma_x2_hat.exp())).sum(-1).mean()
            loss_dict["kld_loss"] = mean_kld.item()
            loss = l1_loss + mean_kld * self.config.kl_weight
        else:
            loss = l1_loss

        return loss, loss_dict

class ACTEncoder(nn.Module):
    """Convenience module for running multiple encoder layers."""
    
    def __init__(self, config: ACTConfig, is_vae_encoder: bool = False):
        super().__init__()
        self.is_vae_encoder = is_vae_encoder
        num_layers = config.n_vae_encoder_layers if self.is_vae_encoder else config.n_encoder_layers
        self.layers = nn.ModuleList([ACTEncoderLayer(config) for _ in range(num_layers)])
        self.norm = nn.LayerNorm(config.dim_model) if config.pre_norm else nn.Identity()

    def forward(
        self, x: Tensor, pos_embed: Optional[Tensor] = None, key_padding_mask: Optional[Tensor] = None
    ) -> Tensor:
        for layer in self.layers:
            x = layer(x, pos_embed=pos_embed, key_padding_mask=key_padding_mask)
        x = self.norm(x)
        return x

class ACTEncoderLayer(nn.Module):
    def __init__(self, config: ACTConfig):
        super().__init__()
        self.self_attn = nn.MultiheadAttention(config.dim_model, config.n_heads, dropout=config.dropout)

        # Feed forward layers
        self.linear1 = nn.Linear(config.dim_model, config.dim_feedforward)
        self.dropout = nn.Dropout(config.dropout)
        self.linear2 = nn.Linear(config.dim_feedforward, config.dim_model)

        self.norm1 = nn.LayerNorm(config.dim_model)
        self.norm2 = nn.LayerNorm(config.dim_model)
        self.dropout1 = nn.Dropout(config.dropout)
        self.dropout2 = nn.Dropout(config.dropout)

        self.activation = get_activation_fn(config.feedforward_activation)
        self.pre_norm = config.pre_norm

    def forward(self, x: Tensor, pos_embed: Optional[Tensor] = None, key_padding_mask: Optional[Tensor] = None) -> Tensor:
        skip = x
        if self.pre_norm:
            x = self.norm1(x)
        q = k = x if pos_embed is None else x + pos_embed
        x = self.self_attn(q, k, value=x, key_padding_mask=key_padding_mask)[0]
        x = skip + self.dropout1(x)
        
        if self.pre_norm:
            skip = x
            x = self.norm2(x)
        else:
            x = self.norm1(x)
            skip = x
            
        x = self.linear2(self.dropout(self.activation(self.linear1(x))))
        x = skip + self.dropout2(x)
        
        if not self.pre_norm:
            x = self.norm2(x)
        return x

class ACTDecoder(nn.Module):
    def __init__(self, config: ACTConfig):
        super().__init__()
        self.layers = nn.ModuleList([ACTDecoderLayer(config) for _ in range(config.n_decoder_layers)])
        self.norm = nn.LayerNorm(config.dim_model)

    def forward(
        self,
        x: Tensor,
        encoder_out: Tensor,
        decoder_pos_embed: Optional[Tensor] = None,
        encoder_pos_embed: Optional[Tensor] = None,
    ) -> Tensor:
        for layer in self.layers:
            x = layer(
                x, encoder_out, 
                decoder_pos_embed=decoder_pos_embed, 
                encoder_pos_embed=encoder_pos_embed
            )
        if self.norm is not None:
            x = self.norm(x)
        return x

class ACTDecoderLayer(nn.Module):
    def __init__(self, config: ACTConfig):
        super().__init__()
        self.self_attn = nn.MultiheadAttention(config.dim_model, config.n_heads, dropout=config.dropout)
        self.multihead_attn = nn.MultiheadAttention(config.dim_model, config.n_heads, dropout=config.dropout)

        # Feed forward layers
        self.linear1 = nn.Linear(config.dim_model, config.dim_feedforward)
        self.dropout = nn.Dropout(config.dropout)
        self.linear2 = nn.Linear(config.dim_feedforward, config.dim_model)

        self.norm1 = nn.LayerNorm(config.dim_model)
        self.norm2 = nn.LayerNorm(config.dim_model)
        self.norm3 = nn.LayerNorm(config.dim_model)
        self.dropout1 = nn.Dropout(config.dropout)
        self.dropout2 = nn.Dropout(config.dropout)
        self.dropout3 = nn.Dropout(config.dropout)

        self.activation = get_activation_fn(config.feedforward_activation)
        self.pre_norm = config.pre_norm

    def maybe_add_pos_embed(self, tensor: Tensor, pos_embed: Optional[Tensor]) -> Tensor:
        return tensor if pos_embed is None else tensor + pos_embed

    def forward(
        self,
        x: Tensor,
        encoder_out: Tensor,
        decoder_pos_embed: Optional[Tensor] = None,
        encoder_pos_embed: Optional[Tensor] = None,
    ) -> Tensor:
        skip = x
        if self.pre_norm:
            x = self.norm1(x)
        q = k = self.maybe_add_pos_embed(x, decoder_pos_embed)
        x = self.self_attn(q, k, value=x)[0]
        x = skip + self.dropout1(x)
        
        if self.pre_norm:
            skip = x
            x = self.norm2(x)
        else:
            x = self.norm1(x)
            skip = x
            
        x = self.multihead_attn(
            query=self.maybe_add_pos_embed(x, decoder_pos_embed),
            key=self.maybe_add_pos_embed(encoder_out, encoder_pos_embed),
            value=encoder_out,
        )[0]
        x = skip + self.dropout2(x)
        
        if self.pre_norm:
            skip = x
            x = self.norm3(x)
        else:
            x = self.norm2(x)
            skip = x
            
        x = self.linear2(self.dropout(self.activation(self.linear1(x))))
        x = skip + self.dropout3(x)
        
        if not self.pre_norm:
            x = self.norm3(x)
        return x

class ACTSinusoidalPositionEmbedding2d(nn.Module):
    """2D sinusoidal positional embeddings."""
    
    def __init__(self, dimension: int):
        super().__init__()
        self.dimension = dimension
        self._two_pi = 2 * math.pi
        self._eps = 1e-6
        self._temperature = 10000

    def forward(self, x: Tensor) -> Tensor:
        not_mask = torch.ones_like(x[0, :1])
        y_range = not_mask.cumsum(1, dtype=torch.float32)
        x_range = not_mask.cumsum(2, dtype=torch.float32)

        y_range = y_range / (y_range[:, -1:, :] + self._eps) * self._two_pi
        x_range = x_range / (x_range[:, :, -1:] + self._eps) * self._two_pi

        inverse_frequency = self._temperature ** (
            2 * (torch.arange(self.dimension, dtype=torch.float32, device=x.device) // 2) / self.dimension
        )

        x_range = x_range.unsqueeze(-1) / inverse_frequency
        y_range = y_range.unsqueeze(-1) / inverse_frequency

        pos_embed_x = torch.stack((x_range[..., 0::2].sin(), x_range[..., 1::2].cos()), dim=-1).flatten(3)
        pos_embed_y = torch.stack((y_range[..., 0::2].sin(), y_range[..., 1::2].cos()), dim=-1).flatten(3)
        pos_embed = torch.cat((pos_embed_y, pos_embed_x), dim=3).permute(0, 3, 1, 2)

        return pos_embed

def create_sinusoidal_pos_embedding(num_positions: int, dimension: int) -> Tensor:
    """1D sinusoidal positional embeddings."""
    def get_position_angle_vec(position):
        return [position / np.power(10000, 2 * (hid_j // 2) / dimension) for hid_j in range(dimension)]

    sinusoid_table = np.array([get_position_angle_vec(pos_i) for pos_i in range(num_positions)])
    sinusoid_table[:, 0::2] = np.sin(sinusoid_table[:, 0::2])
    sinusoid_table[:, 1::2] = np.cos(sinusoid_table[:, 1::2])
    return torch.from_numpy(sinusoid_table).float()

def get_activation_fn(activation: str) -> Callable:
    """Return an activation function given a string."""
    if activation == "relu":
        return F.relu
    if activation == "gelu":
        return F.gelu
    if activation == "glu":
        return F.glu
    raise RuntimeError(f"activation should be relu/gelu/glu, not {activation}.")

class ACTTemporalEnsembler:
    """Temporal ensembling as described in Algorithm 2 of https://arxiv.org/abs/2304.13705.
    
    The weights are calculated as wᵢ = exp(-temporal_ensemble_coeff * i) where w₀ is the oldest action.
    They are then normalized to sum to 1 by dividing by Σwᵢ.
    """
    
    def __init__(self, temporal_ensemble_coeff: float, chunk_size: int) -> None:
        self.chunk_size = chunk_size
        self.ensemble_weights = torch.exp(-temporal_ensemble_coeff * torch.arange(chunk_size))
        self.ensemble_weights_cumsum = torch.cumsum(self.ensemble_weights, dim=0)
        self.reset()

    def reset(self):
        """Resets the online computation variables."""
        self.ensembled_actions = None
        # (chunk_size,) count of how many actions are in the ensemble for each time step
        self.ensembled_actions_count = None

    def update(self, actions: Tensor) -> Tensor:
        """
        Takes a (batch, chunk_size, action_dim) sequence of actions, update the temporal ensemble for all
        time steps, and pop/return the next batch of actions in the sequence.
        """
        self.ensemble_weights = self.ensemble_weights.to(device=actions.device)
        self.ensemble_weights_cumsum = self.ensemble_weights_cumsum.to(device=actions.device)
        
        if self.ensembled_actions is None:
            # Initialize with the first sequence of actions
            self.ensembled_actions = actions.clone()
            self.ensembled_actions_count = torch.ones(
                (self.chunk_size, 1), dtype=torch.long, device=self.ensembled_actions.device
            )
        else:
            # Update existing ensemble
            self.ensembled_actions *= self.ensemble_weights_cumsum[self.ensembled_actions_count - 1]
            self.ensembled_actions += actions[:, :-1] * self.ensemble_weights[self.ensembled_actions_count]
            self.ensembled_actions /= self.ensemble_weights_cumsum[self.ensembled_actions_count]
            self.ensembled_actions_count = torch.clamp(self.ensembled_actions_count + 1, max=self.chunk_size)
            
            # Add the last action which has no prior online average
            self.ensembled_actions = torch.cat([self.ensembled_actions, actions[:, -1:]], dim=1)
            self.ensembled_actions_count = torch.cat(
                [self.ensembled_actions_count, torch.ones_like(self.ensembled_actions_count[-1:])]
            )
        
        # Return the first action and update the queue
        action, self.ensembled_actions, self.ensembled_actions_count = (
            self.ensembled_actions[:, 0],
            self.ensembled_actions[:, 1:],
            self.ensembled_actions_count[1:],
        )
        return action

class ACT(nn.Module):
    """Action Chunking Transformer: The underlying neural network for ACTPolicy.
    Closely follows the implementation in lerobot.common.policies.act.modeling_act.py.
    """

    def __init__(self, config: ACTConfig):
        super().__init__()
        self.config = config

        # VAE Encoder (optional)
        if self.config.use_vae:
            self.vae_encoder = ACTEncoder(config, is_vae_encoder=True)
            self.vae_encoder_cls_embed = nn.Embedding(1, config.dim_model)
            if "observation.state" in self.config.input_shapes:
                self.vae_encoder_robot_state_input_proj = nn.Linear(
                    self.config.input_shapes["observation.state"][0], config.dim_model
                )
            self.vae_encoder_action_input_proj = nn.Linear(
                self.config.output_shapes["action"][0], config.dim_model
            )
            self.vae_encoder_latent_output_proj = nn.Linear(config.dim_model, config.latent_dim * 2)
            
            num_input_token_vae_encoder = 1 + config.chunk_size  # cls + action_sequence
            if "observation.state" in self.config.input_shapes:
                num_input_token_vae_encoder += 1
            self.register_buffer(
                "vae_encoder_pos_enc",
                create_sinusoidal_pos_embedding(num_input_token_vae_encoder, config.dim_model).unsqueeze(0),
            )

        # Vision Backbone (if image inputs are present)
        if self.config.image_input_keys:
            # Note: torchvision.models.<name> will give a warning if weights are not default or None.
            # The original code uses ResNet18_Weights.IMAGENET1K_V1 which is a specific enum.
            # For simplicity here, we pass the string, but for exact weight loading,
            # one might need to map this string to the torchvision.models.weights enum.
            weights_arg = self.config.pretrained_backbone_weights
            try:
                # Attempt to load weights using the new enum style if possible
                from torchvision.models import ResNet18_Weights
                if weights_arg == "ResNet18_Weights.IMAGENET1K_V1": # example
                    weights_arg = ResNet18_Weights.IMAGENET1K_V1
                elif weights_arg is None:
                    pass # No pretrained weights
                else:
                    print(f"Warning: Pretrained backbone weights '{weights_arg}' might not be directly loadable as a string. Consider using torchvision.models.weights enums.")

            except ImportError:
                 if weights_arg is not None:
                    print(f"Warning: torchvision.models.weights enums not found (likely older torchvision). Pretrained backbone weights '{weights_arg}' passed as string.")


            backbone_model = getattr(torchvision.models, config.vision_backbone)(
                replace_stride_with_dilation=[False, False, config.replace_final_stride_with_dilation],
                weights=weights_arg,
                norm_layer=FrozenBatchNorm2d,
            )
            self.backbone = IntermediateLayerGetter(backbone_model, return_layers={"layer4": "feature_map"})
            self.encoder_img_feat_input_proj = nn.Conv2d(
                backbone_model.fc.in_features, config.dim_model, kernel_size=1
            )
            self.encoder_cam_feat_pos_embed = ACTSinusoidalPositionEmbedding2d(config.dim_model // 2)


        # Main Transformer (Encoder + Decoder)
        self.encoder = ACTEncoder(config, is_vae_encoder=False) # Main encoder
        self.decoder = ACTDecoder(config)

        # Input projections for the main encoder
        if "observation.state" in self.config.input_shapes:
            self.encoder_robot_state_input_proj = nn.Linear(
                self.config.input_shapes["observation.state"][0], config.dim_model
            )
        if "observation.environment_state" in self.config.input_shapes:
            self.encoder_env_state_input_proj = nn.Linear(
                self.config.input_shapes["observation.environment_state"][0], config.dim_model
            )
        self.encoder_latent_input_proj = nn.Linear(config.latent_dim, config.dim_model)

        # Positional embeddings for 1D features in the main encoder
        n_1d_tokens = 1  # for the latent sample
        if "observation.state" in self.config.input_shapes:
            n_1d_tokens += 1
        if "observation.environment_state" in self.config.input_shapes:
            n_1d_tokens += 1
        self.encoder_1d_feature_pos_embed = nn.Embedding(n_1d_tokens, config.dim_model)

        # Decoder learnable query embeddings
        self.decoder_pos_embed = nn.Embedding(config.chunk_size, config.dim_model)

        # Final action regression head
        self.action_head = nn.Linear(config.dim_model, self.config.output_shapes["action"][0])

        self._reset_parameters()

    def _reset_parameters(self):
        """Xavier-uniform initialization of the main transformer's encoder and decoder parameters."""
        for p in chain(self.encoder.parameters(), self.decoder.parameters()):
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def forward(self, batch: Dict[str, Tensor]) -> Tuple[Tensor, Tuple[Optional[Tensor], Optional[Tensor]]]:
        # Determine batch_size from one of the input tensors
        if self.config.image_input_keys and "observation.images" in batch and batch["observation.images"]:
            batch_size = batch["observation.images"][0].shape[0]
        elif "observation.state" in batch:
            batch_size = batch["observation.state"].shape[0]
        elif "observation.environment_state" in batch:
            batch_size = batch["observation.environment_state"].shape[0]
        else:
            if self.config.use_vae and "action" in batch:
                 batch_size = batch["action"].shape[0]
            else:
                 # print("Warning: Could not infer batch_size from standard observation keys.") # Removed
                 # Fallback, assuming latent_sample will handle device.
                 batch_size = 1 
                 if "action" in batch: batch_size = batch["action"].shape[0] # override

        mu: Optional[Tensor] = None
        log_sigma_x2: Optional[Tensor] = None
        latent_sample: Tensor

        # VAE Encoder Path (if training with VAE and actions are provided)
        if self.config.use_vae and self.training and "action" in batch:
            cls_embed = self.vae_encoder_cls_embed.weight.unsqueeze(0).repeat(batch_size, 1, 1)
            
            vae_tokens = [cls_embed]
            if "observation.state" in batch and hasattr(self, 'vae_encoder_robot_state_input_proj'):
                robot_state_embed = self.vae_encoder_robot_state_input_proj(batch["observation.state"])
                vae_tokens.append(robot_state_embed.unsqueeze(1))
            
            action_embed = self.vae_encoder_action_input_proj(batch["action"])
            vae_tokens.append(action_embed)
            
            vae_encoder_input = torch.cat(vae_tokens, dim=1)
            # Ensure pos_enc matches the sequence length of vae_encoder_input
            pos_embed_vae = self.vae_encoder_pos_enc[:, :vae_encoder_input.shape[1], :]


            key_padding_mask_vae = None
            if "action_is_pad" in batch:
                num_prefix_tokens = vae_encoder_input.shape[1] - batch["action_is_pad"].shape[1]
                prefix_pad = torch.full((batch_size, num_prefix_tokens), False, device=batch["action"].device)
                key_padding_mask_vae = torch.cat([prefix_pad, batch["action_is_pad"]], dim=1)

            cls_token_out = self.vae_encoder(
                vae_encoder_input.permute(1, 0, 2),
                pos_embed=pos_embed_vae.permute(1, 0, 2),
                key_padding_mask=key_padding_mask_vae
            )[0]  # Output of CLS token

            latent_pdf_params = self.vae_encoder_latent_output_proj(cls_token_out)
            mu = latent_pdf_params[:, :self.config.latent_dim]
            log_sigma_x2 = latent_pdf_params[:, self.config.latent_dim:]  # This is 2*log(sigma)
            
            std = (log_sigma_x2 * 0.5).exp()
            latent_sample = mu + std * torch.randn_like(std)
        else:
            # Use zeros for latent sample if not using VAE path
            # Determine device from an existing tensor in batch or a model parameter
            if batch:
                device = batch[list(batch.keys())[0]].device
            else: # fallback if batch is empty (should not happen in normal use)
                device = next(self.parameters()).device
            latent_sample = torch.zeros(batch_size, self.config.latent_dim, device=device)

        # Main Transformer Encoder Input Preparation
        encoder_1d_tokens = [self.encoder_latent_input_proj(latent_sample)]
        
        # Positional embeddings for 1D tokens (latent, robot_state, env_state)
        # These are added before passing to the nn.TransformerEncoderLayer
        current_1d_token_idx = 0
        pos_embed_1d_components = [self.encoder_1d_feature_pos_embed.weight[current_1d_token_idx].unsqueeze(0).unsqueeze(0).repeat(1, batch_size, 1)]
        current_1d_token_idx +=1

        if "observation.state" in batch and hasattr(self, 'encoder_robot_state_input_proj'):
            encoder_1d_tokens.append(self.encoder_robot_state_input_proj(batch["observation.state"]))
            pos_embed_1d_components.append(self.encoder_1d_feature_pos_embed.weight[current_1d_token_idx].unsqueeze(0).unsqueeze(0).repeat(1, batch_size, 1))
            current_1d_token_idx +=1
            
        if "observation.environment_state" in batch and hasattr(self, 'encoder_env_state_input_proj'):
            encoder_1d_tokens.append(self.encoder_env_state_input_proj(batch["observation.environment_state"]))
            pos_embed_1d_components.append(self.encoder_1d_feature_pos_embed.weight[current_1d_token_idx].unsqueeze(0).unsqueeze(0).repeat(1, batch_size, 1))

        # Stack 1D tokens: each is (B, D), stack to (Num_1D_tokens, B, D)
        encoder_1d_sequence = torch.stack(encoder_1d_tokens, dim=0)
        encoder_1d_pos_embed_sequence = torch.cat(pos_embed_1d_components, dim=0)


        # Image Feature Processing
        all_cam_features_processed = []
        all_cam_pos_embeds_processed = []
        if self.config.image_input_keys and "observation.images" in batch and hasattr(self, 'backbone') and batch["observation.images"]:
            # print(f"[ACT.forward] Processing {len(batch['observation.images'])} image tensors.") # Removed
            for i, img_tensor in enumerate(batch["observation.images"]): # list of (B, C, H, W)
                # print(f"[ACT.forward] Image tensor {i} input shape: {img_tensor.shape}") # Removed
                if img_tensor.shape[0] != batch_size:
                    raise ValueError(
                        f"Image tensor batch size {img_tensor.shape[0]} "
                        f"does not match determined batch_size {batch_size}."
                    )

                feature_map = self.backbone(img_tensor)["feature_map"] # (B, D_backbone, H', W')
                # print(f"[ACT.forward] Image tensor {i} feature_map shape: {feature_map.shape}") # Removed
                
                if feature_map.shape[0] != batch_size:
                    pass # Warning was here, removed for profiling
                    # print( # Removed
                    #     f"Warning: Backbone output feature_map batch size {feature_map.shape[0]} "
                    #     f"does not match expected batch_size {batch_size}. "
                    #     f"Input image batch was {img_tensor.shape[0]}."
                    # )
                
                projected_feature = self.encoder_img_feat_input_proj(feature_map) # (B, D_model, H', W')
                # print(f"[ACT.forward] Image tensor {i} projected_feature shape: {projected_feature.shape}") # Removed

                if projected_feature.shape[0] != batch_size:
                     pass # Warning was here, removed for profiling
                    #  print( # Removed
                    #     f"Warning: Projected feature batch size {projected_feature.shape[0]} "
                    #     f"does not match expected batch_size {batch_size}."
                    # )

                pos_embed_template_2d = self.encoder_cam_feat_pos_embed(projected_feature[:1]).to(dtype=projected_feature.dtype) # (1, D_model, H', W')
                # print(f"[ACT.forward] Image tensor {i} pos_embed_template_2d shape: {pos_embed_template_2d.shape}") # Removed
                
                pos_embed_2d_batched = pos_embed_template_2d.repeat(batch_size, 1, 1, 1) # (batch_size, D_model, H', W')
                # print(f"[ACT.forward] Image tensor {i} pos_embed_2d_batched shape: {pos_embed_2d_batched.shape}") # Removed
                
                rearranged_projected_feature = einops.rearrange(projected_feature, "b c h w -> (h w) b c")
                rearranged_pos_embed = einops.rearrange(pos_embed_2d_batched, "b c h w -> (h w) b c")
                # print(f"[ACT.forward] Image tensor {i} rearranged_projected_feature shape: {rearranged_projected_feature.shape}") # Removed
                # print(f"[ACT.forward] Image tensor {i} rearranged_pos_embed shape: {rearranged_pos_embed.shape}") # Removed

                all_cam_features_processed.append(rearranged_projected_feature)
                all_cam_pos_embeds_processed.append(rearranged_pos_embed)

        # Concatenate 1D and 2D (image) features for the main encoder
        # --- Start Logging before torch.cat for positional embeddings ---
        # print(f"[ACT.forward] Shape of encoder_1d_pos_embed_sequence: {encoder_1d_pos_embed_sequence.shape}") # Removed
        for idx, tensor in enumerate(all_cam_pos_embeds_processed):
            # print(f"[ACT.forward] Shape of all_cam_pos_embeds_processed[{idx}]: {tensor.shape}") # Removed
            pass
        # --- End Logging ---
        if all_cam_features_processed: # Check if there are camera features to concatenate
            full_encoder_input_sequence = torch.cat([encoder_1d_sequence] + all_cam_features_processed, dim=0)
            full_encoder_pos_embed = torch.cat([encoder_1d_pos_embed_sequence] + all_cam_pos_embeds_processed, dim=0)
            # --- Start Logging after torch.cat ---
            # print(f"[ACT.forward] Shape of full_encoder_input_sequence (with images): {full_encoder_input_sequence.shape}") # Removed
            # print(f"[ACT.forward] Shape of full_encoder_pos_embed (with images): {full_encoder_pos_embed.shape}") # Removed
            # --- End Logging ---
        else:
            full_encoder_input_sequence = encoder_1d_sequence
            full_encoder_pos_embed = encoder_1d_pos_embed_sequence
            # --- Start Logging after torch.cat ---
            # print(f"[ACT.forward] Shape of full_encoder_input_sequence (1D only): {full_encoder_input_sequence.shape}") # Removed
            # print(f"[ACT.forward] Shape of full_encoder_pos_embed (1D only): {full_encoder_pos_embed.shape}") # Removed
            # --- End Logging ---
            
        # Main Encoder Forward Pass
        encoder_output = self.encoder(
            full_encoder_input_sequence,
            pos_embed=full_encoder_pos_embed
        ) # (S_total, B, D)

        # Main Transformer Decoder
        # Decoder queries are learnable embeddings.
        # Original DETR uses zeros as query input and adds pos_embed to q, k.
        # ACT modeling_act.py does:
        # decoder_in = torch.zeros_like(self.decoder_pos_embed.weight.unsqueeze(1).repeat(1, batch_size, 1))
        # decoder_out = self.decoder(decoder_in, encoder_out, encoder_pos_embed=..., decoder_pos_embed=self.decoder_pos_embed.weight.unsqueeze(1))
        # This means decoder_pos_embed is added to query and key inside ACTDecoderLayer.
        
        # Prepare decoder input (usually zeros) and query positional embeddings
        decoder_query_embed = self.decoder_pos_embed.weight.unsqueeze(1).repeat(1, batch_size, 1) # (chunk_size, B, D)
        decoder_input_tgt = torch.zeros_like(decoder_query_embed)

        decoder_output = self.decoder(
            x=decoder_input_tgt,
            encoder_out=encoder_output,
            decoder_pos_embed=decoder_query_embed, # These are the "object queries" or action slot embeddings
            encoder_pos_embed=full_encoder_pos_embed # Positional embeddings of the encoder output
        ) # (chunk_size, B, D)

        # Action Prediction Head
        # Permute decoder_output to (B, chunk_size, D) before passing to Linear head
        predicted_actions = self.action_head(decoder_output.permute(1, 0, 2))

        return predicted_actions, (mu, log_sigma_x2)

# Continue with ACT, ACTTemporalEnsembler, and other supporting classes...
