#!/usr/bin/env python3
import cv2
import logging
import json
import os

CAMERA_CONFIG_PATH = "/tmp/camera_config.json"

def _load_camera_config(logger: logging.Logger):
    """Load camera configuration from a JSON file."""
    if os.path.exists(CAMERA_CONFIG_PATH):
        try:
            with open(CAMERA_CONFIG_PATH, 'r') as f:
                config = json.load(f)
            logger.info(f"✅ Loaded camera config from {CAMERA_CONFIG_PATH}")
            return config
        except Exception as e:
            logger.warning(f"⚠️ Could not load or parse camera config file: {e}")
            _delete_camera_config(logger) # Delete corrupted file
            return None
    return None

def _save_camera_config(config, logger: logging.Logger):
    """Save camera configuration to a JSON file."""
    try:
        with open(CAMERA_CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=4)
        logger.info(f"💾 Saved camera config to {CAMERA_CONFIG_PATH}")
    except Exception as e:
        logger.error(f"❌ Failed to save camera config: {e}")

def _delete_camera_config(logger: logging.Logger):
    """Deletes the camera config file if it exists."""
    if os.path.exists(CAMERA_CONFIG_PATH):
        try:
            os.remove(CAMERA_CONFIG_PATH)
            logger.info(f"🗑️ Deleted invalid camera config file.")
        except Exception as e:
            logger.warning(f"⚠️ Could not delete camera config file: {e}")

def _find_available_cameras(logger: logging.Logger, preferred_backend=cv2.CAP_V4L2):
    """Find available camera indices by testing them with preferred backend."""
    available_cameras = []
    
    # Test camera indices 0-10 (should cover most cases)
    for index in range(10):
        try:
            # Try with preferred backend first
            cap = cv2.VideoCapture(index, preferred_backend)
            if cap.isOpened():
                # Try to read a frame to verify the camera works
                ret, frame = cap.read()
                if ret and frame is not None:
                    # Get camera properties to log
                    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    fps = cap.get(cv2.CAP_PROP_FPS)
                    available_cameras.append(index)
                    logger.info(f"📹 Found working camera at index {index}: {width}x{height} @ {fps:.1f}fps")
            cap.release()
        except Exception as e:
            logger.debug(f"Camera index {index} failed: {e}")
    
    return available_cameras

def initialize_camera(logger: logging.Logger, camera_index=None, preferred_backend=cv2.CAP_V4L2):
    """Initialize the camera for capturing images, using a cached configuration if available."""
    
    # 1. If no specific index is requested, try to use a saved configuration
    if camera_index is None:
        config = _load_camera_config(logger)
        if config:
            try:
                logger.info(f"💡 Using saved camera config: Index {config['camera_index']} @ {config['width']}x{config['height']}")
                
                cam = cv2.VideoCapture(config['camera_index'], preferred_backend)
                
                if cam.isOpened():
                    # Set properties from the loaded configuration
                    cam.set(cv2.CAP_PROP_FRAME_WIDTH, config['width'])
                    cam.set(cv2.CAP_PROP_FRAME_HEIGHT, config['height'])
                    cam.set(cv2.CAP_PROP_FPS, config['fps'])
                    
                    # Verify that the settings were applied correctly
                    actual_width = int(cam.get(cv2.CAP_PROP_FRAME_WIDTH))
                    actual_height = int(cam.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    
                    if actual_width == config['width'] and actual_height == config['height']:
                        actual_fps = cam.get(cv2.CAP_PROP_FPS)
                        logger.info(f"📐 Camera resolution set from config: {actual_width}x{actual_height} @ {actual_fps:.1f}fps")
                        
                        # Warm-up camera
                        for _ in range(5):
                            ret, _ = cam.read()
                            if not ret:
                                logger.warning("⚠️ Camera warm-up frame failed during config load")
                                break
                        
                        logger.info(f"✅ Camera initialized successfully from saved config at index {config['camera_index']}")
                        return cam, config['camera_index']
                    else:
                        logger.warning(f"⚠️ Camera properties do not match saved config ({actual_width}x{actual_height}). Re-detecting...")
                        cam.release()
                else:
                    logger.warning(f"⚠️ Failed to open camera at saved index {config['camera_index']}. Re-detecting...")

                # If we reach here, the saved configuration is invalid
                _delete_camera_config(logger)

            except Exception as e:
                logger.error(f"❌ Error initializing camera from config: {e}. Re-detecting...")
                _delete_camera_config(logger)

    # 2. Fallback to detection if no config exists, it was invalid, or an index was specified
    if camera_index is None:
        logger.info("🔍 No valid camera config found. Searching for available cameras...")
        available_cameras = _find_available_cameras(logger, preferred_backend)
        
        if not available_cameras:
            logger.error("❌ No working cameras found")
            return None, None
        
        # Use the first available camera
        camera_index = available_cameras[0]
        logger.info(f"📹 Using camera index {camera_index}")
        
        if len(available_cameras) > 1:
            logger.info(f"💡 Other available cameras: {available_cameras[1:]}")
    
    # Initialize the camera
    try:
        camera = cv2.VideoCapture(camera_index, preferred_backend)
        
        if not camera.isOpened():
            logger.error(f"❌ Failed to open camera at index {camera_index}")
            return None, None
        
        # Set desired camera properties for better image quality
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        camera.set(cv2.CAP_PROP_FPS, 30)
        
        # Verify the settings were applied
        actual_width = int(camera.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_height = int(camera.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = camera.get(cv2.CAP_PROP_FPS)
        logger.info(f"📐 Camera resolution set to: {actual_width}x{actual_height} @ {actual_fps:.1f}fps")
        
        # Save the successful configuration for next time
        new_config = {
            'camera_index': camera_index,
            'width': actual_width,
            'height': actual_height,
            'fps': actual_fps,
            'backend': preferred_backend
        }
        _save_camera_config(new_config, logger)

        # Warm up the camera by capturing a few frames
        for _ in range(5):
            ret, _ = camera.read()
            if not ret:
                logger.warning("⚠️  Camera warm-up frame failed")
        
        logger.info(f"✅ Camera initialized successfully at index {camera_index}")
        return camera, camera_index
        
    except Exception as e:
        logger.error(f"❌ Error initializing camera: {e}")
        return None, None 