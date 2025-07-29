#!/usr/bin/env python3
import cv2
import logging

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
    """Initialize the camera for capturing images."""
    if camera_index is None:
        logger.info("🔍 Searching for available cameras...")
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
        
        # Set camera properties for better image quality
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        camera.set(cv2.CAP_PROP_FPS, 30)
        
        # Verify the settings were applied
        actual_width = int(camera.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_height = int(camera.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = camera.get(cv2.CAP_PROP_FPS)
        logger.info(f"📐 Camera resolution set to: {actual_width}x{actual_height} @ {actual_fps:.1f}fps")
        
        # Warm up the camera by capturing a few frames
        for _ in range(5):
            ret, frame = camera.read()
            if not ret:
                logger.warning("⚠️  Camera warm-up frame failed")
        
        logger.info(f"✅ Camera initialized successfully at index {camera_index}")
        return camera, camera_index
        
    except Exception as e:
        logger.error(f"❌ Error initializing camera: {e}")
        return None, None 