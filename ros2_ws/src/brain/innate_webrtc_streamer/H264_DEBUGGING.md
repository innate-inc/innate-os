# H264 Encoding Debugging Summary

## Working Baseline
- **VP8** works perfectly - continuous encoder output, data flows to webrtcbin sink pads, video displays on client

## H264 Problem
x264enc/openh264enc produce **only 1 frame** then stop outputting. Frames continue being pushed to appsrc, but encoder doesn't produce more output.

## Attempted Solutions (all failed)

### x264enc configurations:
1. `tune=zerolatency speed-preset=ultrafast` - basic settings
2. Added `video/x-raw,format=I420` conversion 
3. Added `video/x-h264,profile=constrained-baseline` caps filter - **blocked data flow**
4. Added `bframes=0 cabac=false` - baseline compatible
5. Added `h264parse` after encoder - still stuck
6. Added `byte-stream=true` 
7. Added `queue max-size-buffers=2 leaky=downstream` before encoder - appsrc now flows, but encoder still stuck
8. Added `threads=1` - single threaded
9. Added `sliced-threads=true` 
10. Added `option-string="rc-lookahead=0:sync-lookahead=0:bframes=0"` - disable all lookahead

### openh264enc:
11. `openh264enc bitrate=2000000 complexity=low` - same issue, stuck after buffer #1

## Key Observations
- VP8: `encoder_main_out` shows buffer #1, #31, #61... (continuous)
- H264: `encoder_main_out` shows buffer #1 only, then nothing
- `appsrc_main_out` continues with queue (buffer #31, #61...) but encoder ignores them
- Payloader output is only 14 bytes (RTP header only, no payload)

## Possible Root Causes
1. **GStreamer H264 encoder issue on ARM/Jetson** - x264 may have threading/timing issues on this platform
2. **Missing NVIDIA hardware encoder** - `nvv4l2h264enc` not available
3. **Pipeline timing/scheduling issue** specific to H264 encoders

## Available Encoders on System
```
gst-inspect-1.0 | grep -E "(nvv4l2|openh264|omx.*264)"
```
- `avenc_h264_omx` - OpenMAX IL H.264 encoder
- `nvv4l2decoder` - NVIDIA decoder only (no encoder)
- `openh264enc` - OpenH264 encoder

## Options to Try
1. Check if there's a Jetson-specific H264 encoder (`jetson_multimedia_api`)
2. Use `avenc_h264_omx` (OpenMAX)
3. Stick with VP8 which works

## Working VP8 Pipeline (for reference)
```python
'appsrc name=src_main is-live=true format=time '
'caps=video/x-raw,format=RGB,width=640,height=480,framerate=30/1 ! '
'videoconvert ! '
'vp8enc deadline=1 error-resilient=partitions keyframe-max-dist=30 ! '
'rtpvp8pay pt=96 ! '
'application/x-rtp,media=video,encoding-name=VP8,clock-rate=90000,payload=96 ! '
'webrtc.sink_0'
```

## Final Attempt: Plain x264enc (NVIDIA Forum recommendation)
12. Plain `x264enc` with no parameters + `h264parse` + I420 conversion
    - Result: Encoder still stuck at buffer #1
    - Client also rejected offer (no fmtp line in SDP)

## Conclusion
H264 software encoding (x264enc, openh264enc) does not work properly on Jetson Orin Nano with GStreamer appsrc pipeline. The encoder produces only 1 frame then stops. **Use VP8 instead.**

## Date
December 15, 2025
