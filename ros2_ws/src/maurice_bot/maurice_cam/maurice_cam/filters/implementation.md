# Disparity Filter Chain — Implementation Notes

## Architecture

The filter chain runs inside `StereoDepthEstimator::processFrame()` after VPI SGM
extracts a float32 disparity map.  Every filter operates on single-channel `CV_32FC1`
data.  The chain sandwiches the active filters between a downsample and upsample step
so all filtering happens at reduced resolution (default: ÷4 → 160×120 at 640×480 input).

```
disparity F32 (640×480)
  │
  ├─ downsample ÷4 ──→ 160×120 F32
  │    │
  │    ├─ [configurable filter_order list, each independently toggleable]
  │    │    depth_clamp, domain_transform, speckle, edge_invalidation, ...
  │    │
  │    ├─ clone → disparity_lowres (for point clouds)
  │    │
  │    └─ upsample ×4 ──→ 640×480 F32
  │
  └─ published as disparity / depth
```

Source files:
- `filter_chain.cpp`     — parameter init, logging, `applyFilterChain()` orchestration
- `simple_filters.cpp`   — median, bilateral, hole-fill, depth-clamp, edge-invalidation, speckle
- `advanced_filters.cpp` — domain transform (CUDA wrapper), temporal filter
- `domain_transform.cu`  — CUDA kernels for the domain transform

---

## Domain Transform Filter (CUDA)

### Paper

**"Domain Transform for Edge-Aware Image and Video Processing"**
Eduardo S. L. Gastal & Manuel M. Oliveira, ACM TOG 30(4), SIGGRAPH 2011.

We implement the **Recursive Filtering (RF)** mode — Section 3.3 of the paper.
Reference MATLAB code: `RF.m` from the authors' distribution.

### Algorithm

The RF mode performs N iterations of alternating horizontal + vertical 1D
recursive (IIR) causal/anti-causal scans.

**Per iteration i (0-indexed):**

1. Compute the iteration-dependent sigma (Eq. 14):

$$\sigma_{H_i} = \sigma_s \cdot \sqrt{3} \cdot \frac{2^{N - i - 1}}{\sqrt{4^N - 1}}$$

   This gives broad smoothing first (large σ), then fine refinement (small σ).

2. Compute feedback coefficient:

$$a = e^{-\sqrt{2} \,/\, \sigma_{H_i}}$$

3. Compute per-pixel domain transform derivative from the **original** image
   (not the in-place result — prevents edge erosion):

$$D(x) = 1 + \frac{\sigma_s}{\sigma_r} \cdot |I_{\text{orig}}(x) - I_{\text{orig}}(x-1)|$$

4. Compute per-pixel weight: $V(x) = a^{D(x)} = e^{-\sqrt{2} \cdot D(x) \,/\, \sigma_{H_i}}$

5. Forward scan (left→right): $F(x) = F(x) + V(x) \cdot (F(x-1) - F(x))$
6. Backward scan (right→left): same with reversed direction.
7. Repeat steps 3–6 for the vertical direction.

### Self-Guided Variant

Our use case is disparity filtering where the disparity map serves as both the
**guide image** (for computing derivatives/weights) and the **signal being
smoothed**.  This simplifies the kernel — no separate guide image buffer needed
on the GPU.

Invalid pixels (disparity ≤ 0) break the recursive chain.  They are never
smoothed into or from.  This is important for stereo disparity where 0 = unknown.

### Parameters

| YAML parameter | Paper symbol | Default | Description |
|---|---|---|---|
| `domain_transform.sigma_s` | σ_s | 30.0 | Spatial standard deviation — controls smoothing extent |
| `domain_transform.sigma_r` | σ_r | 5.0 | Range standard deviation — edge sensitivity |
| `domain_transform.iterations` | N | 3 | Number of H+V passes — paper recommends 3 |

**Tuning guide:**
- Increase `sigma_s` → smoother over larger areas (at same σ_r, σ just gets broader)
- Decrease `sigma_r` → sharper edge preservation (depth discontinuities more respected)
- Increase iterations → better convergence to 2D ideal (diminishing returns after 3)

### CUDA Implementation (`domain_transform.cu`)

**Architecture: one thread per row/column**

- Horizontal pass: launch `⌈height/128⌉` blocks × 128 threads. Each thread
  processes one row with a full forward + backward sequential scan.
- Vertical pass: launch `⌈width/128⌉` blocks × 128 threads. Each thread
  processes one column with strided access (stride = width).

At 320×240 × F32, the working set is 2 × 300 KB = 600 KB, which fits
comfortably in the Jetson Orin Nano's 1 MB L2 cache.

**Memory layout:**
- `d_img`  — working copy (read/write, filtered in-place)
- `d_orig` — original disparity (read-only, for derivative computation)
- Both are persistent across frames (lazy-alloc, reused via module-level statics)
- `dt_stream` — non-blocking CUDA stream (separate from VPI's stream)

**Per-frame data flow:**
```
Host cv::Mat ──cudaMemcpyAsync──→  d_img   (working copy)
Host cv::Mat ──cudaMemcpyAsync──→  d_orig  (original, read-only)
                                     │
                  ┌──────────────────┘
                  │  for each iteration i:
                  │    dt_horizontal_kernel(d_img, d_orig, ...)
                  │    dt_vertical_kernel(d_img, d_orig, ...)
                  │
d_img ──cudaMemcpyAsync──→  Host cv::Mat
cudaStreamSynchronize(dt_stream)
```

**CUDA event timing** is built in — logs a breakdown every ~1 s to stderr:
```
[domain_transform.cu] 320x240 3 iter | upload 0.12 ms | kernels 2.00 ms | download 0.09 ms | total 2.20 ms
```

**Performance** (Jetson Orin Nano, MAXN_SUPER, 320×240, 3 iterations):

| Stage | Time |
|---|---|
| Upload (H→D, 2 × 300 KB) | ~0.12 ms |
| Kernels (3 × H+V = 6 launches) | ~2.0 ms |
| Download (D→H, 300 KB) | ~0.09–0.17 ms |
| **Total (GPU event time)** | **~2.2 ms** |

The wall-clock domain_transform entry in the filter chain timer matches the
GPU event total almost exactly (~2.2–2.3 ms), confirming the sync is just
waiting on actual kernel work — no mysterious stalls or scheduling overhead.

**Previous CPU implementation**: 6–8 ms at same resolution (2 iterations).
Current CUDA version runs 3 iterations (better quality) in ~2.2 ms wall = **3× faster**.

---

## Other Filters (CPU — not yet migrated)

All other filters remain CPU-only OpenCV implementations.  Listed by migration
priority:

### Edge Invalidation — `invalidateEdges()` (1.0–5.0 ms, spiky)

Pipeline: `F32→U8 normalize → GaussianBlur 3×3 → Canny → dilate → mask`

**VPI migration opportunity: HIGH**
- `vpiSubmitCanny` — CUDA backend, edge detection
- `vpiSubmitMorphology` — CUDA backend, dilation with structuring element
- `vpiSubmitGaussianFilter` — CUDA backend, blur
- Entire sub-pipeline could be GPU-only.  Expected: <0.2 ms.
- Note: the spiky variance (1–5 ms) may be from CPU scheduling jitter, which
  GPU would eliminate.

### Speckle Removal — `filterSpeckles()` (~0.5 ms)

Pipeline: `F32→S16 (×16) → cv::filterSpeckles → S16→F32`

- Already fast at 0.5 ms.  The F32↔S16 conversion is the main overhead.
- No VPI equivalent.  A CUDA connected-component kernel could do it but ROI is small.
- **Low priority** — not worth the complexity.

### Depth Clamp — `clampByDepth()` (~0.2 ms)

Trivial element-wise threshold.  Could be a one-liner CUDA kernel but at 0.2 ms
it's not worth a separate kernel launch.  Could piggyback on the domain transform
upload if we wanted — run a clamp kernel on d_img before the DT iterations.

### Median / Bilateral / Hole Fill / Temporal

All currently disabled in `filter_order`.  If enabled:
- **Median**: VPI `vpiSubmitMedianFilter` on CUDA — trivial swap, ~0.05 ms at 160×120.
- **Bilateral**: VPI `vpiSubmitBilateralFilter` on CUDA — ~0.04 ms (3×3).
- **Hole fill**: Custom 4-neighbour search — no VPI equivalent.  Could CUDA-ify
  but it's only useful when enabled.
- **Temporal**: IIR blend — needs frame history on GPU.  Moderate complexity.

---

## Future Work

1. **Edge invalidation → VPI/CUDA** — biggest remaining win.  Canny + dilate + mask
   as a GPU chain would cut the spiky 1–5 ms down to <0.2 ms.

2. **Keep disparity on GPU through the entire filter chain** — currently each filter
   does CPU↔GPU round-trips.  If the chain were: VPI extract disparity (GPU) →
   CUDA domain transform → CUDA edge invalidation → download once → publish,
   we'd eliminate 2+ memcpy round-trips.

3. **Pinned host memory** for the domain transform upload/download — `cudaMallocHost`
   instead of pageable cv::Mat memory would speed up DMA transfers.
