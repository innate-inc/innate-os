# Innate ACT Bundle

## Quickstart (Jetson Orin Nano)

### 1. Install System CUDA Libraries

```bash
sudo apt update
sudo apt install -y cuda-runtime-12-6 libcudnn9-cuda-12 cuda-cupti-12-6
```

### 2. Install CUDA-Enabled PyTorch

```bash
/usr/bin/python -m pip uninstall -y torch torchvision
/usr/bin/python -m pip install --upgrade pip
/usr/bin/python -m pip install --user --no-cache-dir --force-reinstall \
  --index-url https://pypi.jetson-ai-lab.io/jp6/cu126 \
  "torch==2.8.0" "torchvision==0.23.0"
/usr/bin/python -m pip install --user "numpy<2" einops pillow
```

## Bundle Contents

| File | Purpose |
|------|---------|
| `test.py` | Packed-runtime benchmark and reference comparison |
| `benchmark_act_orig.py` | `ACT_orig.py` eager / inductor benchmark and reference comparison |
| `_bench_common.py` | Shared helpers (memory snapshots, sample loading, report printing) |
| `ACT_orig.py` / `_act_orig_support.json` | ACT source model and its config/normalization stats |
| `act_orig_checkpoint.pth` | ACT source model checkpoint |
| `package/` | Packed runtime, bundled `sample_inputs.npz`, and `reference_outputs.npz` |

## Running Benchmarks

Run from the bundle root. Neither script takes iteration or tolerance flags; constants live at the top of each file.

### Packed runtime

```bash
/usr/bin/python test.py
```

Loads the runtime from `package/`, runs 5 warmup + 25 measured iterations on `package/sample_inputs.npz`, then compares one full action chunk against `package/reference_outputs.npz`. Prints latency, RSS at each phase, and max/mean abs diff + cosine similarity.

### ACT_orig

Eager mode:

```bash
/usr/bin/python benchmark_act_orig.py act_orig_checkpoint.pth
```

With `torch.compile` (inductor backend):

```bash
/usr/bin/python benchmark_act_orig.py act_orig_checkpoint.pth --compile
```

Device is auto-selected (`cuda` if available, else `cpu`). The inductor backend requires Triton:

```bash
/usr/bin/python -m pip install --user "triton==3.5.0"
```
