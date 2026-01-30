"""Training service client adapter for robots."""

import logging
import asyncio
import tempfile
import subprocess
from typing import Optional, Dict, Any, List
import httpx
from pathlib import Path
import traceback
import time

# Fallback logger for when no ROS logger is provided
_fallback_logger = logging.getLogger(__name__)


class TrainingClient:
    """
    Client for interacting with the training service.
    
    The training service uses direct endpoints at /v1/training/... rather than
    going through the /v1/services/... router used by other services.
    
    Follows the same pattern as CartesiaAdapter - takes parent ProxyClient.
    """
    
    def __init__(self, parent, logger=None):
        """
        Initialize training client.
        
        Args:
            parent: Parent ProxyClient instance (provides proxy_url and auth)
            logger: Optional ROS logger to use. If provided, creates a child logger
                    with '.training' suffix. Falls back to Python logging if not provided.
        """
        self._parent = parent
        self.proxy_url = parent.proxy_url
        self._innate_service_key = parent.innate_service_key
        self._async_client: Optional[httpx.AsyncClient] = None
        
        # Set up logger - use ROS logger with child name if provided, else fallback
        if logger is not None:
            # ROS loggers have get_child() method to create named sub-loggers
            if hasattr(logger, 'get_child'):
                self.logger = logger.get_child('training')
            else:
                # If it's a wrapped logger (like UniversalLogger), use it directly
                self.logger = logger
        else:
            self.logger = _fallback_logger
        
        # Validate that proxy_url and auth are set
        if not self.proxy_url:
            raise ValueError(
                "proxy_url is not set. Set INNATE_PROXY_URL environment variable "
                "or ensure ProxyClient is properly initialized."
            )
        if not self._innate_service_key:
            raise ValueError(
                "innate_service_key is not set. Set INNATE_SERVICE_KEY environment variable "
                "or ensure ProxyClient is properly initialized."
            )
    
    def _get_token(self) -> str:
        """Get authentication token, ensuring it's properly formatted."""
        token = self._parent.innate_service_key or self._innate_service_key
        
        if not token:
            raise ValueError(
                "X-Innate-Token is missing. Ensure INNATE_SERVICE_KEY is set and ProxyClient is initialized correctly."
            )
        
        # Ensure token is a string and strip whitespace (server expects .strip())
        token = str(token).strip()
        
        # Remove comments (everything after first space) - tokens in .env files may have comments
        # Example: "token_value # This is R7-1's key" -> "token_value"
        # Parse by space, not hashtag, as requested
        if ' ' in token:
            token = token.split(' ')[0].strip()
        
        if not token:
            raise ValueError(
                "X-Innate-Token is empty after stripping. Check INNATE_SERVICE_KEY environment variable."
            )
        
        return token
    
    def _get_headers(self) -> Dict[str, str]:
        """Get authentication headers."""
        token = self._get_token()
        headers = {"X-Innate-Token": token}
        self.logger.debug(f"Built headers with token length: {len(token)}, preview: {token[:20]}...")
        return headers
    
    def _get_async_client(self, timeout: float = 60.0) -> httpx.AsyncClient:
        """Get or create async HTTP client."""
        # Always create our own client to avoid event loop issues across threads
        # Headers will be passed explicitly in each request for reliability
        return httpx.AsyncClient(timeout=timeout)
    
    def _build_training_url(self, endpoint: str) -> str:
        """Build training service URL (uses /v1/training/... not /v1/services/training/...)."""
        if not self.proxy_url:
            raise ValueError(
                "proxy_url is not set. Set INNATE_PROXY_URL environment variable "
                "or pass proxy_url to TrainingClient constructor."
            )
        endpoint = endpoint.lstrip("/")
        return f"{self.proxy_url}/v1/training/{endpoint}"
    
    async def request_upload_permission(
        self,
        filename: str,
        content_type: str = "application/octet-stream",
        training_params: Optional[Dict[str, Any]] = None,
        primitive_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Request permission to upload training data and get presigned URL.
        
        Args:
            filename: Name of the file to upload (e.g., "dataset.h5")
            content_type: MIME type of the file
            training_params: Optional training parameters (batch_size, max_steps, etc.)
            primitive_name: Optional primitive name (sent as top-level field for server to store)
        
        Returns:
            Dict with:
                - job_id: Unique job identifier
                - upload_url: Presigned URL to POST to initiate resumable upload
                - message: Instructions for next steps
        """
        payload = {
            "filename": filename,
            "content_type": content_type,
        }
        if training_params:
            payload["training_params"] = training_params
        if primitive_name:
            payload["primitive_name"] = primitive_name
        
        client = self._get_async_client()
        url = self._build_training_url("/upload-permission")
        headers = self._get_headers()
        
        call_stack = ''.join(traceback.format_stack()[-4:-1])  # Get caller info
        self.logger.info(f"🔐 Requesting upload permission (auth handshake)")
        self.logger.info(f"  URL: {url}")
        self.logger.info(f"  Filename: {filename}")
        self.logger.info(f"  Content-Type: {content_type}")
        self.logger.info(f"  Caller stack: {call_stack}")
        self.logger.info(f"  Training params: {training_params}")
        self.logger.debug(f"  Headers: X-Innate-Token present={bool(headers.get('X-Innate-Token'))}")
        
        try:
            self.logger.info(f"  Sending POST request to proxy...")
            response = await client.post(url, json=payload, headers=headers)
            self.logger.info(f"  ✓ Received response: {response.status_code}")
            response.raise_for_status()
            
            result = response.json()
            job_id = result.get("job_id")
            upload_url = result.get("upload_url")
            
            self.logger.info(f"✅ Upload permission granted!")
            self.logger.info(f"  Job ID: {job_id}")
            self.logger.info(f"  Presigned URL received: {upload_url[:80]}...")
            self.logger.info(f"  Content-Type: {result.get('content_type', 'N/A')}")
            
            return result
        except httpx.HTTPStatusError as e:
            # Log the response body for debugging
            error_detail = ""
            try:
                error_detail = f" Response: {e.response.text}"
            except Exception:
                pass
            self.logger.error(f"Upload permission request failed: {e.response.status_code}{error_detail}")
            self.logger.error(f"Request URL: {url}")
            self.logger.error(f"Request payload: {payload}")
            raise
    
    async def initiate_resumable_upload(
        self,
        upload_url: str,
        content_type: str = "application/octet-stream",
    ) -> str:
        """
        Initiate resumable upload session by POSTing to the presigned URL.
        
        Args:
            upload_url: Presigned URL from request_upload_permission()
            content_type: Content type that was used when requesting upload permission
                          (must match what was signed in the URL)
        
        Returns:
            Session URI from Location header (use this for chunked uploads)
        """
        self.logger.info(f"📤 Initiating resumable upload session")
        self.logger.info(f"  POST to presigned URL: {upload_url[:80]}...")
        self.logger.info(f"  Headers: x-goog-resumable=start, Content-Type={content_type}")
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            self.logger.debug(f"  Sending POST request with empty body...")
            response = await client.post(
                upload_url,
                headers={
                    "x-goog-resumable": "start",
                    "Content-Type": content_type,
                },
                content=b""
            )
            self.logger.info(f"  ✓ Response: {response.status_code} {response.reason_phrase}")
            response.raise_for_status()
            
            # Extract session URI from Location header
            session_uri = response.headers.get("Location")
            if not session_uri:
                raise ValueError("No Location header in response - resumable upload session not started")
            
            self.logger.info(f"✅ Resumable upload session initiated!")
            self.logger.info(f"  Session URI: {session_uri[:80]}...")
            
            return session_uri
    
    async def upload_chunk(
        self,
        session_uri: str,
        data: bytes,
        start_byte: int,
        total_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Upload a chunk of data to the resumable upload session.
        
        Args:
            session_uri: Session URI from initiate_resumable_upload()
            data: Bytes to upload
            start_byte: Starting byte position
            total_size: Total size of file (for Content-Range header)
        
        Returns:
            Response status and headers
        """
        end_byte = start_byte + len(data) - 1
        chunk_size_mb = len(data) / (1024 * 1024)
        
        headers = {
            "Content-Range": f"bytes {start_byte}-{end_byte}/{total_size if total_size else '*'}"
        }
        
        self.logger.debug(f"  📦 Uploading chunk: bytes {start_byte}-{end_byte} ({chunk_size_mb:.2f} MB)")
        
        async with httpx.AsyncClient(timeout=300.0) as client:  # Long timeout for large uploads
            response = await client.put(
                session_uri,
                content=data,
                headers=headers
            )
            
            status_code = response.status_code
            self.logger.debug(f"    → Response: {status_code} {response.reason_phrase}")
            
            # For GCS resumable uploads:
            # - 200/201 = Upload complete
            # - 308 = Resume Incomplete (normal during chunked uploads, not an error)
            # - Other status codes = actual errors
            if status_code not in [200, 201, 308]:
                self.logger.error(f"    ✗ Unexpected status code: {status_code}")
                response.raise_for_status()
            elif status_code == 308:
                self.logger.debug(f"    ✓ Chunk uploaded (308 Resume Incomplete - normal)")
            else:
                self.logger.info(f"    ✅ Upload complete! (Status: {status_code})")
            
            return {
                "status_code": response.status_code,
                "headers": dict(response.headers)
            }
    
    async def upload_file_resumable(
        self,
        file_path: str,
        job_id: str,
        upload_url: str,
        filename: Optional[str] = None,
        content_type: str = "application/octet-stream",
        chunk_size: int = 32 * 1024 * 1024,  # 32MB chunks (increased for faster uploads)
        training_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Upload a file using resumable upload protocol.
        
        This method uploads a file using an existing job_id and upload_url.
        Call request_upload_permission() first to get these values.
        
        Flow:
        1. Initiate resumable upload session
        2. Upload file in chunks
        3. Handle resume on failure
        
        Args:
            file_path: Path to local file to upload
            job_id: Job ID from request_upload_permission()
            upload_url: Upload URL from request_upload_permission()
            filename: Optional filename (defaults to file_path basename)
            content_type: MIME type of the file (default: "application/octet-stream")
            chunk_size: Size of each chunk in bytes (default: 32MB for faster uploads)
            training_params: Optional training parameters (not used, but kept for compatibility)
        
        Returns:
            Dict with job_id and upload status
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        
        filename = filename or path.name
        file_size = path.stat().st_size
        
        file_size_mb = file_size / (1024 * 1024)
        self.logger.info(f"📤 Starting resumable upload")
        self.logger.info(f"  File: {filename}")
        self.logger.info(f"  Job ID: {job_id}")
        self.logger.info(f"  Size: {file_size:,} bytes ({file_size_mb:.2f} MB)")
        self.logger.info(f"  Chunk size: {chunk_size:,} bytes ({chunk_size / (1024*1024):.2f} MB)")
        self.logger.info(f"  Estimated chunks: {(file_size + chunk_size - 1) // chunk_size}")
        
        # Step 2: Initiate resumable upload session
        # Note: content_type must match what was used in request_upload_permission
        # because it's included in the signed headers of the presigned URL
        session_uri = await self.initiate_resumable_upload(upload_url, content_type=content_type)
        
        # Step 3: Upload file in chunks (streaming from disk to avoid RAM issues)
        self.logger.info(f"🚀 Starting chunked upload: {file_size:,} bytes in chunks of {chunk_size:,} bytes")
        self.logger.info(f"  📝 Streaming from disk (not loading entire file into memory)")
        uploaded_bytes = 0
        
        try:
            # Open file in binary mode and read chunks sequentially
            # This ensures we only keep one chunk in memory at a time
            with open(file_path, "rb") as f:
                while uploaded_bytes < file_size:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    
                    result = await self.upload_chunk(
                        session_uri=session_uri,
                        data=chunk,
                        start_byte=uploaded_bytes,
                        total_size=file_size
                    )
                    
                    # Check if upload is complete (200/201) or still in progress (308)
                    if result["status_code"] in [200, 201]:
                        # Upload complete!
                        uploaded_bytes = file_size
                        self.logger.info(f"Upload complete: {uploaded_bytes} bytes uploaded")
                        break
                    elif result["status_code"] == 308:
                        # Resume Incomplete - normal during chunked uploads
                        # Update uploaded_bytes based on Range header if available
                        range_header = result["headers"].get("Range")
                        if range_header:
                            # Range header format: "bytes=0-8388607"
                            # Extract the end byte
                            try:
                                end_byte = int(range_header.split("=")[1].split("-")[1])
                                uploaded_bytes = end_byte + 1
                            except (IndexError, ValueError):
                                # Fallback to calculated value
                                uploaded_bytes += len(chunk)
                        else:
                            uploaded_bytes += len(chunk)
                    else:
                        # Unexpected status code - should not happen
                        self.logger.warning(f"Unexpected status code: {result['status_code']}")
                        # For unexpected status codes, still increment by chunk size
                        uploaded_bytes += len(chunk)
                    
                    progress = (uploaded_bytes / file_size) * 100
                    uploaded_mb = uploaded_bytes / (1024 * 1024)
                    total_mb = file_size / (1024 * 1024)
                    self.logger.info(f"  📊 Progress: {uploaded_bytes:,}/{file_size:,} bytes ({uploaded_mb:.2f}/{total_mb:.2f} MB) - {progress:.1f}%")
            
            self.logger.info(f"✅ Upload complete: {uploaded_bytes:,} bytes ({uploaded_bytes / (1024*1024):.2f} MB) uploaded")
            return {
                "job_id": job_id,
                "status": "uploaded",
                "bytes_uploaded": uploaded_bytes
            }
        
        except Exception as e:
            self.logger.error(f"Upload failed at {uploaded_bytes} bytes: {e}")
            # Check how many bytes were actually uploaded
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.put(
                        session_uri,
                        headers={"Content-Range": f"bytes */{file_size}"}
                    )
                    if response.status_code == 308:  # Resume Incomplete
                        range_header = response.headers.get("Range")
                        if range_header:
                            # Parse Range header to get uploaded bytes
                            uploaded_bytes = int(range_header.split("-")[1]) + 1
            except Exception:
                pass
            
            raise Exception(f"Upload failed at {uploaded_bytes}/{file_size} bytes. Resume from byte {uploaded_bytes}")
    
    async def notify_upload_complete(
        self,
        job_id: str,
        training_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Notify proxy that upload is complete and submit training job.
        
        Args:
            job_id: Job ID from request_upload_permission()
            training_params: Optional training parameters to override
        
        Returns:
            Dict with job_id, status, and training_service_job_id
        """
        payload = {}
        if training_params:
            payload["training_params"] = training_params
        
        self.logger.info(f"📨 Notifying proxy that upload is complete")
        self.logger.info(f"  Job ID: {job_id}")
        self.logger.info(f"  Training params: {training_params}")
        
        # Use longer timeout for upload-complete as server may need to process/validate
        client = self._get_async_client(timeout=300.0)  # 5 minutes
        url = self._build_training_url(f"/jobs/{job_id}/upload-complete")
        
        self.logger.info(f"  POST to: {url}")
        self.logger.info(f"  Timeout: 300 seconds (5 minutes)")
        
        try:
            self.logger.info(f"  Sending upload-complete notification...")
            response = await client.post(url, json=payload, headers=self._get_headers())
            self.logger.info(f"  ✓ Response: {response.status_code} {response.reason_phrase}")
            response.raise_for_status()
            
            result = response.json()
            self.logger.info(f"✅ Upload complete notification sent successfully!")
            self.logger.info(f"  Job status: {result.get('status', 'N/A')}")
            self.logger.info(f"  Message: {result.get('message', 'N/A')}")
            
            return result
        except httpx.ReadTimeout:
            self.logger.warning(
                f"Request to notify upload complete timed out after 5 minutes. "
                f"This may indicate the server is still processing. "
                f"Job ID: {job_id}. You can check status later with get_job_status()."
            )
            raise
    
    async def get_job_status(self, job_id: str) -> Dict[str, Any]:
        """
        Get current status of a training job.
        
        Args:
            job_id: Job identifier
        
        Returns:
            Dict with job status, timestamps, paths, etc.
        """
        client = self._get_async_client()
        url = self._build_training_url(f"/jobs/{job_id}")
        
        # Explicitly pass headers to ensure they're sent (consistent with list_jobs)
        response = await client.get(url, headers=self._get_headers())
        response.raise_for_status()
        return response.json()
    
    async def get_job_status_by_name(self, primitive_name: str) -> Dict[str, Any]:
        """
        Get current status of a training job by primitive name.
        
        Queries the most recent job for the given primitive name.
        This requires the server to support querying by primitive_name.
        
        Args:
            primitive_name: Primitive name (e.g., "minecraft_wave")
        
        Returns:
            Dict with job status, timestamps, paths, etc.
            Returns the most recent job for the given primitive name.
        
        Raises:
            httpx.HTTPStatusError: If the request fails (404 if no job found)
        """
        client = self._get_async_client()
        url = self._build_training_url(f"/jobs/by-name/{primitive_name}")
        
        self.logger.info(f"📊 Querying job status by primitive name: {primitive_name}")
        self.logger.debug(f"  URL: {url}")
        
        # Explicitly pass headers to ensure they're sent
        response = await client.get(url, headers=self._get_headers())
        response.raise_for_status()
        
        result = response.json()
        self.logger.info(f"✅ Retrieved job status for {primitive_name}: {result.get('status', 'unknown')}")
        
        return result
    
    async def list_jobs(
        self,
        status_filter: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        List training jobs for the current user.
        
        Queries the proxy service which queries its database (BigQuery).
        No local state needed - always gets latest from server.
        
        Args:
            status_filter: Optional status filter (e.g., "running", "completed", "failed")
                         If None, returns all jobs
            limit: Optional limit on number of jobs to return
        
        Returns:
            List of job dicts with status, timestamps, etc.
        """
        client = self._get_async_client()
        url = self._build_training_url("/jobs")
        
        params = {}
        if status_filter:
            params["status"] = status_filter
        if limit:
            params["limit"] = limit
        
        # Explicitly pass headers to ensure they're sent (httpx may not always use constructor headers)
        # This matches how get_job_status works (which succeeds)
        headers = self._get_headers()
        self.logger.debug(f"Making request to {url} with headers: X-Innate-Token present={bool(headers.get('X-Innate-Token'))}")
        
        try:
            response = await client.get(url, params=params, headers=headers)
        except httpx.HTTPStatusError as e:
            # If 401, log detailed debugging info
            if e.response.status_code == 401:
                self.logger.error(f"401 Unauthorized for {url}")
                self.logger.error(f"Response: {e.response.text[:200]}")
                # Check if token is set
                token_set = bool(self._innate_service_key)
                parent_token_set = bool(self._parent.innate_service_key)
                token_value = self._parent.innate_service_key or self._innate_service_key
                self.logger.error(f"Token in TrainingClient: {token_set}")
                self.logger.error(f"Token in ProxyClient: {parent_token_set}")
                self.logger.error(f"Token length: {len(token_value) if token_value else 0}")
                self.logger.error(f"Token preview (first 20 chars): {token_value[:20] + '...' if token_value and len(token_value) > 20 else (token_value or 'None')}")
                self.logger.error(f"Proxy URL: {self.proxy_url}")
                self.logger.error("⚠️  Token may be expired or invalid. Check INNATE_SERVICE_KEY environment variable.")
                self.logger.error("⚠️  Server response: 'Invalid or expired authentication token'")
                self.logger.error("⚠️  Action: Verify INNATE_SERVICE_KEY is correct and not expired")
                self.logger.error(f"⚠️  Note: Training endpoints use /v1/training/... (not /v1/services/...)")
                self.logger.error(f"⚠️  Make sure the token has permission for training endpoints")
            raise
        
        response.raise_for_status()
        return response.json()
    
    async def get_incomplete_jobs(self) -> List[Dict[str, Any]]:
        """
        Get all incomplete jobs for the current user.
        
        Returns jobs with status: pending, uploading, submitted, running
        
        Returns:
            List of incomplete job dicts
        """
        incomplete_statuses = ["pending", "uploading", "submitted", "running"]
        all_jobs = await self.list_jobs()
        
        # Filter to incomplete jobs
        incomplete = [
            job for job in all_jobs
            if job.get("status") in incomplete_statuses
        ]
        
        return incomplete
    
    async def get_download_url(
        self,
        job_id: str,
        filename: str,
    ) -> Dict[str, Any]:
        """
        Get presigned URL for downloading trained model files.
        
        Args:
            job_id: Job identifier
            filename: Name of the file to download (e.g., "model.ckpt")
        
        Returns:
            Dict with download_url and expiration info
        """
        client = self._get_async_client()
        url = self._build_training_url(f"/jobs/{job_id}/download")
        
        try:
            response = await client.get(url, params={"filename": filename}, headers=self._get_headers())
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            # Log detailed error information
            error_detail = ""
            try:
                error_detail = f" Response: {e.response.text}"
                # Try to parse JSON error if available
                try:
                    error_json = e.response.json()
                    if "detail" in error_json:
                        error_detail = f" Server error: {error_json['detail']}"
                except:
                    pass
            except Exception:
                pass
            self.logger.error(f"Failed to get download URL for {filename} (job {job_id}): {e.response.status_code}{error_detail}")
            self.logger.debug(f"Request URL: {url}")
            self.logger.debug(f"Request params: filename={filename}")
            raise
    
    async def download_file(
        self,
        job_id: str,
        filename: str,
        output_path: str,
        chunk_size: int = 8 * 1024 * 1024,  # 8MB chunks
    ) -> str:
        """
        Download a trained model file.
        
        Args:
            job_id: Job identifier
            filename: Name of the file to download
            output_path: Local path to save the file
            chunk_size: Chunk size for streaming download
        
        Returns:
            Path to downloaded file
        """
        # Get download URL
        download_info = await self.get_download_url(job_id, filename)
        download_url = download_info["download_url"]
        
        # Download file using streaming to avoid RAM issues
        self.logger.info(f"📥 Downloading {filename} to {output_path}")
        self.logger.info(f"  📝 Streaming in chunks of {chunk_size / (1024*1024):.2f} MB (not loading entire file into memory)")
        
        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream("GET", download_url) as response:
                response.raise_for_status()
                
                # Try to get file size from Content-Length header if available
                content_length = response.headers.get("Content-Length")
                file_size_bytes = None
                if content_length:
                    try:
                        file_size_bytes = int(content_length)
                        file_size_mb = file_size_bytes / (1024 * 1024)
                        self.logger.info(f"  📊 File size: {file_size_bytes:,} bytes ({file_size_mb:.2f} MB)")
                    except (ValueError, TypeError):
                        pass
                
                # Stream chunks directly to disk, one chunk at a time
                # This ensures we only keep one chunk in memory at a time
                downloaded_bytes = 0
                with open(output_path, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=chunk_size):
                        f.write(chunk)
                        downloaded_bytes += len(chunk)
                        # Log progress periodically (every 4 chunks or if we know file size)
                        if file_size_bytes:
                            progress = (downloaded_bytes / file_size_bytes) * 100
                            if downloaded_bytes % (chunk_size * 4) == 0 or downloaded_bytes == file_size_bytes:
                                downloaded_mb = downloaded_bytes / (1024 * 1024)
                                self.logger.info(f"  📊 Progress: {downloaded_bytes:,}/{file_size_bytes:,} bytes ({downloaded_mb:.2f}/{file_size_mb:.2f} MB) - {progress:.1f}%")
                        elif downloaded_bytes % (chunk_size * 4) == 0:
                            downloaded_mb = downloaded_bytes / (1024 * 1024)
                            self.logger.debug(f"  📊 Downloaded: {downloaded_bytes:,} bytes ({downloaded_mb:.2f} MB)")
        
        self.logger.info(f"Download complete: {output_path}")
        return output_path
    
    async def wait_for_completion(
        self,
        job_id: str,
        poll_interval: int = 60,
        timeout: Optional[int] = None,
        adaptive_polling: bool = True,
    ) -> Dict[str, Any]:
        """
        Poll job status until completion or failure.
        
        Uses adaptive polling intervals:
        - uploading/submitted: poll_interval (default: 60s)
        - running: 5x poll_interval (default: 5 minutes)
        - This reduces API calls for long-running training jobs
        
        Args:
            job_id: Job identifier
            poll_interval: Base seconds between status checks (default: 60)
            timeout: Maximum seconds to wait (None = no timeout)
            adaptive_polling: Use longer intervals for running jobs (default: True)
        
        Returns:
            Final job status dict
        
        Raises:
            TimeoutError: If timeout is exceeded
        """
        start_time = time.time()
        last_status = None
        
        while True:
            status = await self.get_job_status(job_id)
            job_status = status.get("status")
            
            # Adaptive polling: use longer intervals for running jobs
            if adaptive_polling:
                if job_status == "running":
                    current_interval = poll_interval * 5  # 5 minutes default
                elif job_status in ["submitted", "uploading"]:
                    current_interval = poll_interval  # 1 minute default
                else:
                    current_interval = poll_interval
            else:
                current_interval = poll_interval
            
            # Only log if status changed
            if job_status != last_status:
                self.logger.info(f"Job {job_id} status: {job_status} (polling every {current_interval}s)")
                last_status = job_status
            
            if job_status in ["completed", "failed", "cancelled"]:
                return status
            
            if timeout and (time.time() - start_time) > timeout:
                raise TimeoutError(f"Job {job_id} did not complete within {timeout} seconds")
            
            await asyncio.sleep(current_interval)
    
    async def upload_primitive_folder(
        self,
        primitive_path: str,
        job_id: str,
        upload_url: str,
        primitive_name: Optional[str] = None,
        chunk_size: int = 32 * 1024 * 1024,  # 32MB chunks
        training_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Upload an entire primitive folder (all episodes + metadata) as a tarball.
        
        Creates tarball on disk, uploads in chunks, then deletes it.
        Uses tar command for efficient compression with lower memory usage.
        The folder should contain:
        - data/ directory with episode H5 files and dataset_metadata.json
        - metadata.json (optional)
        
        Args:
            primitive_path: Path to primitive folder (e.g., "primitives/minecraft_wave")
            job_id: Job ID from request_upload_permission()
            upload_url: Upload URL from request_upload_permission()
            primitive_name: Optional primitive name (defaults to folder name)
            chunk_size: Size of each chunk in bytes (default: 32MB)
            training_params: Optional training parameters (not used, but kept for compatibility)
        
        Returns:
            Dict with job_id and upload status
        """
        primitive_dir = Path(primitive_path)
        if not primitive_dir.exists() or not primitive_dir.is_dir():
            raise ValueError(f"Primitive folder not found: {primitive_path}")
        
        primitive_name = primitive_name
        
        # Check required structure
        data_dir = primitive_dir / "data"
        if not data_dir.exists():
            raise ValueError(f"Primitive folder must contain 'data/' directory: {primitive_path}")
        
        dataset_metadata = data_dir / "dataset_metadata.json"
        if not dataset_metadata.exists():
            raise ValueError(f"Primitive folder must contain 'data/dataset_metadata.json': {primitive_path}")
        
        self.logger.info(f"📦 Packaging primitive folder into tarball")
        self.logger.info(f"  Source: {primitive_path}")
        self.logger.info(f"  Primitive name: {primitive_name}")
        
        # Count files efficiently without storing all file objects in memory
        # Skip if it fails to avoid memory issues
        file_count = 0
        total_data_size = 0
        try:
            # Use a generator to avoid loading all paths into memory at once
            for f in data_dir.rglob("*"):
                if f.is_file():
                    file_count += 1
                    try:
                        total_data_size += f.stat().st_size
                    except (OSError, PermissionError):
                        # Skip files we can't stat
                        pass
            self.logger.info(f"  Found {file_count} file(s) in data/ directory")
            self.logger.info(f"  Total data size: {total_data_size:,} bytes ({total_data_size / (1024**2):.2f} MB)")
        except Exception as e:
            self.logger.warning(f"  Warning: Could not count files (non-critical): {e}")
            self.logger.info(f"  Proceeding with tarball creation...")
        
        # Create tarball in temporary directory using tar command (more memory efficient)
        with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as tmp_file:
            tar_path = tmp_file.name
        
        try:
            self.logger.info(f"  Creating tarball: {tar_path}")
            self.logger.info(f"  Using tar command for efficient compression...")
            
            # Use tar command instead of Python tarfile for better memory efficiency
            tar_cmd = ['tar', 'czf', tar_path, '-C', str(primitive_dir)]
            tar_cmd.append('data')
            
            # Add metadata.json if it exists
            metadata_file = primitive_dir / "metadata.json"
            if metadata_file.exists():
                tar_cmd.append('metadata.json')
            
            self.logger.debug(f"  Command: {' '.join(tar_cmd)}")
            
            # Run tar command - don't capture output to save memory
            # Only capture stderr for error messages
            result = subprocess.run(
                tar_cmd,
                stdout=subprocess.DEVNULL,  # Don't capture stdout
                stderr=subprocess.PIPE,     # Only capture stderr for errors
                text=True,
                timeout=600  # 10 minute timeout
            )
            
            if result.returncode != 0:
                stderr = result.stderr[:500] if result.stderr else "Unknown error"
                raise RuntimeError(f"tar command failed (exit {result.returncode}): {stderr}")
            
            tar_size = Path(tar_path).stat().st_size
            tar_size_mb = tar_size / (1024**2)
            compression_ratio = (1 - tar_size / total_data_size) * 100 if total_data_size > 0 else 0
            
            self.logger.info(f"✅ Tarball created successfully!")
            self.logger.info(f"  Size: {tar_size:,} bytes ({tar_size_mb:.2f} MB)")
            self.logger.info(f"  Compression ratio: {compression_ratio:.1f}%")
            self.logger.info(f"  Files included: {file_count}")
            
            # Upload the tarball
            filename = f"{primitive_name}.tar.gz"
            self.logger.info(f"📤 Uploading tarball: {filename}")
            
            result = await self.upload_file_resumable(
                file_path=tar_path,
                job_id=job_id,
                upload_url=upload_url,
                filename=filename,
                content_type="application/gzip",
                chunk_size=chunk_size,
                training_params=training_params,
            )
            
            return result
            
        finally:
            # Clean up temporary tarball
            try:
                if Path(tar_path).exists():
                    Path(tar_path).unlink()
                    self.logger.debug(f"  Cleaned up temporary tarball: {tar_path}")
            except Exception as e:
                self.logger.warning(f"Failed to delete temporary tarball: {e}")
    
    async def close(self):
        """Close the HTTP client."""
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None
        # Note: We don't close parent ProxyClient - it's managed by the node
    
    async def __aenter__(self):
        """Async context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()



