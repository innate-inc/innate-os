# Innate-OS Proxy Integration - Implementation Notes

## Summary

This document describes the refactoring to integrate the Innate Service Proxy client into innate-os, replacing direct API calls to Cartesia and OpenAI.

## Changes Made

### 1. Client Library Integration
- **Location**: `client/` folder copied to innate-os root
- **Purpose**: Provides proxy client adapters for Cartesia and OpenAI

### 2. TTS Handler (`ros2_ws/src/brain/brain_client/brain_client/tts_handler.py`)
- **Before**: Used `Cartesia(api_key=...)` directly
- **After**: Uses `ProxyCartesiaClient()` which reads `INNATE_PROXY_URL` and `INNATE_SERVICE_KEY` from environment
- **Changes**:
  - Removed `api_key` parameter (now optional for backward compatibility)
  - Changed `client.tts.bytes()` call to async (`await`) and wrapped in event loop
  - Updated initialization to use proxy client
  - `get_available_voices()` temporarily disabled (not yet implemented in proxy)

### 3. Micro Input (`inputs/micro_input.py`)
- **Before**: Used direct OpenAI WebSocket connection with `websocket-client` library
- **After**: Uses `ProxyOpenAIClient` when proxy is configured, falls back to direct connection
- **Changes**:
  - Uses `ProxyOpenAIClient.realtime.connect_sync()` method which provides a synchronous interface
  - The sync interface (`SyncRealtimeConnection`) is built into the proxy client adapter
  - Auto-detects proxy configuration (`INNATE_PROXY_URL` + `INNATE_SERVICE_KEY`)
  - Falls back to direct OpenAI connection if proxy not configured (backward compatible)
  - Removed wrapper class - sync bridging now handled directly in proxy client

### 4. Launch Configuration (`ros2_ws/src/brain/brain_client/launch/brain_client.launch.py`)
- **Before**: Required `cartesia_api_key` parameter
- **After**: Parameter deprecated but kept for backward compatibility
- **Note**: TTS handler now reads proxy config from environment variables

### 5. Environment Template (`.env.template`)
- **Added**: `INNATE_PROXY_URL` and `INNATE_SERVICE_KEY` (required)
- **Kept**: Legacy API keys (`CARTESIA_API_KEY`, `OPENAI_API_KEY`) for fallback compatibility

## Implementation Complexities

### ⚠️ Complexity 1: Async/Sync Bridge for OpenAI WebSocket

**Issue**: The proxy client uses async `websockets` library, but `micro_input.py` uses synchronous `websocket-client` library.

**Solution**: Added `SyncRealtimeConnection` class to `ProxyOpenAIClient` that:
- Provides `connect_sync()` method on `realtime` property
- Runs async event loop in separate thread internally
- Bridges async `websockets` library with sync `websocket-client` compatible API
- Maintains same interface as `RealtimeClient` for drop-in replacement
- Handles connection lifecycle: `start()`, `stop()`, `wait_until_connected()`, `send_json()`

**Implementation**: The sync interface is now part of the proxy client adapter (`client/adapters/openai_adapter.py`), eliminating the need for a separate wrapper class in `micro_input.py`.

**Potential Issues**:
- Event loop management across threads can be tricky
- Message handling may have slight latency due to thread switching
- Thread synchronization required for send operations

**Testing Required**:
- Verify WebSocket connection establishes correctly
- Verify messages are received and processed correctly
- Verify audio streaming works end-to-end
- Test sync interface in isolation

### ⚠️ Complexity 2: Async TTS Call in Sync Context

**Issue**: `TTSHandler.speak_text()` is synchronous, but proxy client's `tts.bytes()` is async.

**Solution**: Uses `asyncio.get_event_loop()` or creates new event loop and runs async call with `run_until_complete()`.

**Potential Issues**:
- If ROS2 event loop conflicts, may need to use thread pool executor
- Event loop creation/closing overhead on each call

**Testing Required**:
- Verify TTS generation works correctly
- Check for any event loop conflicts with ROS2
- Measure performance impact

### ⚠️ Complexity 3: Client Library Path Resolution

**Issue**: Client library needs to be importable from various locations in innate-os.

**Solution**: Adds `client/` directory to `sys.path` dynamically based on `INNATE_OS_ROOT` environment variable.

**Potential Issues**:
- If `INNATE_OS_ROOT` is not set correctly, imports will fail
- Path resolution happens at import time, may fail silently if client folder missing

**Testing Required**:
- Verify imports work from all entry points
- Test with different `INNATE_OS_ROOT` values
- Verify graceful degradation if client folder missing

## Backward Compatibility

- ✅ Legacy API keys (`CARTESIA_API_KEY`, `OPENAI_API_KEY`) still supported as fallback
- ✅ `cartesia_api_key` launch parameter still accepted (deprecated)
- ✅ Direct OpenAI connection still works if proxy not configured
- ✅ No breaking changes to existing interfaces

## Migration Path

1. **Deploy proxy service** (see `innate_service_proxy` repo)
2. **Generate robot token** using `scripts/generate_token.py`
3. **Update `.env` file** on robot:
   ```bash
   INNATE_PROXY_URL=https://your-proxy-url.run.app
   INNATE_SERVICE_KEY=your-generated-token
   ```
4. **Restart robot services** - proxy will be used automatically
5. **Remove legacy API keys** once verified working (optional)

## Testing Checklist

- [ ] TTS generation works via proxy
- [ ] OpenAI Realtime WebSocket connects via proxy
- [ ] Audio transcription works correctly
- [ ] Fallback to direct API works if proxy unavailable
- [ ] Error handling works correctly (invalid token, network errors, etc.)
- [ ] Performance is acceptable (latency, throughput)

## Dependencies

The proxy client requires:
- `httpx` (for HTTP requests)
- `websockets` (for WebSocket connections)
- `google-auth` (optional, for Cloud Run IAM - not needed if public access enabled)

Add to `requirements.txt` or install via:
```bash
pip install httpx websockets google-auth
```

