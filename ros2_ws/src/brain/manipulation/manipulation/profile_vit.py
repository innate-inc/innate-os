#!/usr/bin/env python3
import time
import torch
import torch.nn as nn
import timm
from torch.hub import load_state_dict_from_url

class DinoMLP(nn.Module):
    def __init__(self, backbone_name, proprio_dim=6, hidden_dim=512, action_dim=8):
        super().__init__()

        # 1) instantiate backbone WITHOUT pretrained weights
        backbone = timm.create_model(
            backbone_name,
            pretrained=False,
            num_classes=0,       # strip off any head
            global_pool="avg",
            norm_layer=nn.LayerNorm,
        )

        # 2) grab the checkpoint URL from default_cfg
        cfg = backbone.default_cfg
        url = cfg.get("url", None)
        if url is None:
            raise ValueError(f"No pretrained URL found for {backbone_name}")

        # 3) download checkpoint (may include a 'model' wrapper)
        sd = load_state_dict_from_url(url, map_location="cpu")
        sd = sd.get("model", sd)

        # 4) rename final norm → fc_norm and load
        new_sd = {}
        for k, v in sd.items():
            if k.startswith("norm."):
                new_sd["fc_norm." + k[len("norm.") :]] = v
            else:
                new_sd[k] = v

        # Handle position embedding size mismatch
        if "pos_embed" in new_sd:
            current_pos_embed = backbone.pos_embed
            pretrained_pos_embed = new_sd["pos_embed"]
            if current_pos_embed.shape != pretrained_pos_embed.shape:
                # Resize the position embeddings to match the current model
                new_sd["pos_embed"] = torch.nn.functional.interpolate(
                    pretrained_pos_embed.permute(0, 2, 1),
                    size=current_pos_embed.shape[1],
                    mode="linear"
                ).permute(0, 2, 1)

        # load into backbone (strict=False to ignore any leftover mismatches)
        backbone.load_state_dict(new_sd, strict=False)
        self.backbone = backbone

        # build our 2-layer MLP head
        embed_dim = backbone.num_features
        self.mlp = nn.Sequential(
            nn.Linear(embed_dim * 2 + proprio_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, action_dim),
        )

    def forward(self, img1, img2, proprio):
        f1 = self.backbone(img1)  # (B, embed_dim)
        f2 = self.backbone(img2)  # (B, embed_dim)
        x  = torch.cat((f1, f2, proprio), dim=1)
        return self.mlp(x)        # (B, action_dim)

def benchmark(model, img1, img2, proprio, device, warmup=10, iters=100):
    # Convert model to float16
    model = model.half()
    model.to(device).eval()
    
    # Convert inputs to float16
    img1 = img1.half().to(device)
    img2 = img2.half().to(device)
    proprio = proprio.half().to(device)
    
    # First warm up the model with a few forward passes
    print("Warming up model before tracing...")
    for _ in range(3):
        _ = model(img1, img2, proprio)
    
    # Now trace the model with float16 inputs
    print("Tracing model...")
    scripted = torch.jit.trace(model, (img1, img2, proprio))
    
    # Then compile for low overhead with float16 support
    print("Compiling model...")
    compiled_model = torch.compile(
        scripted,
        backend="inductor",
        mode="reduce-overhead"  # minimize launch checks
    )
    
    with torch.no_grad():
        print("Warming up...")
        for i in range(warmup):
            print(f"\rWarmup iteration {i+1}/{warmup}", end="", flush=True)
            _ = compiled_model(img1, img2, proprio)
        print("\nRunning benchmark...")

        torch.cuda.synchronize()
        t0 = time.time()
        for i in range(iters):
            print(f"\rBenchmark iteration {i+1}/{iters}", end="", flush=True)
            _ = compiled_model(img1, img2, proprio)
        torch.cuda.synchronize()
        t1 = time.time()
        print()
    return (t1 - t0) * 1000.0 / iters  # ms per inference

def print_model_size(model):
    total_params = sum(p.numel() for p in model.parameters())
    trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"Total parameters: {total_params:,}")
    print(f"Trainable parameters: {trainable_params:,}")
    print(f"Size in MB: {total_params * 4 / (1024 * 1024):.2f}")  # Assuming float32 (4 bytes)

def main():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model_names = [
        'vit_base_patch14_dinov2',
        'vit_small_patch14_dinov2'
    ]  # base and small variants only

    # synthetic inputs
    img1    = torch.randn(1, 3, 518, 518)
    img2    = torch.randn(1, 3, 518, 518)
    proprio = torch.randn(1, 6)

    print(f"Running on {device}\n")
    print("Testing DINO ViT variants...")
    for i, name in enumerate(model_names, 1):
        print(f"\n[{i}/{len(model_names)}] Testing {name}...")
        model = DinoMLP(name)
        print("\nModel size information:")
        print_model_size(model)
        ms = benchmark(model, img1, img2, proprio, device)
        print(f"\r[{i}/{len(model_names)}] {name:32s} → {ms:6.2f} ms  ({1000.0/ms:6.1f} FPS)")

if __name__ == "__main__":
    main()
