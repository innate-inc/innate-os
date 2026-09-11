# Person Re-identification Model

`osnet_x0_25_msmt17.onnx` is an inference-only ONNX export of the official
OSNet x0.25 MSMT17 `combineall` checkpoint from
[`deep-person-reid`](https://github.com/KaiyangZhou/deep-person-reid). The
classifier was omitted; the model emits a 512-dimensional appearance embedding
for a 256 x 128 RGB input.

- Source checkpoint: `osnet_x0_25_msmt17_combineall_256x128_amsgrad_ep150_stp60_lr0.0015_b64_fb10_softmax_labelsmooth_flip_jitter.pth`
- Source URL: <https://drive.google.com/file/d/1Kkx2zW89jq_NETu4u42CFZTMVD5Hwm6e/view>
- Export date: 2026-08-25
- ONNX opset: 17
- ONNX SHA-256: `7e49cb6b5a9b3fe3701a975900d5a98b80f5c3a5754208e46652d6bbcf29ce08`

OSNet and `deep-person-reid` are copyright Kaiyang Zhou and contributors and
are distributed under the MIT License. See the upstream repository for the
license and model documentation.
