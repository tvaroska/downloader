"""Background job management system for batch processing."""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import redis.asyncio as redis
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    """Job status enumeration."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobInfo(BaseModel):
    """Job information model."""
    job_id: str = Field(..., description="Unique job identifier")
    status: JobStatus = Field(..., description="Current job status")
    created_at: datetime = Field(..., description="Job creation timestamp")
    started_at: Optional[datetime] = Field(None, description="Job start timestamp")
    completed_at: Optional[datetime] = Field(None, description="Job completion timestamp")
    progress: int = Field(0, description="Progress percentage (0-100)")
    total_urls: int = Field(0, description="Total number of URLs to process")
    processed_urls: int = Field(0, description="Number of URLs processed")
    successful_urls: int = Field(0, description="Number of successfully processed URLs")
    failed_urls: int = Field(0, description="Number of failed URLs")
    error_message: Optional[str] = Field(None, description="Error message if job failed")
    request_data: Dict[str, Any] = Field(..., description="Original request data")
    results_available: bool = Field(False, description="Whether results are available for download")
    expires_at: Optional[datetime] = Field(None, description="Job expiration timestamp")


class JobResult(BaseModel):
    """Job result model for completed jobs."""
    job_id: str = Field(..., description="Job identifier")
    status: JobStatus = Field(..., description="Final job status")
    total_duration: float = Field(..., description="Total processing time in seconds")
    results: List[Dict[str, Any]] = Field(..., description="Individual URL results")
    summary: Dict[str, Any] = Field(..., description="Processing summary")
    created_at: datetime = Field(..., description="Job creation timestamp")
    completed_at: datetime = Field(..., description="Job completion timestamp")


class JobManager:
    """Redis-based job management system."""
    
    def __init__(self, redis_url: str, job_ttl: int = 3600 * 24):  # 24 hours default TTL
        """
        Initialize job manager.
        
        Args:
            redis_url: Redis connection URL
            job_ttl: Job time-to-live in seconds
        """
        self.redis_url = redis_url
        self.job_ttl = job_ttl
        self.redis_client: Optional[redis.Redis] = None
        self._background_tasks: Dict[str, asyncio.Task] = {}
        
    async def connect(self):
        """Connect to Redis."""
        try:
            self.redis_client = redis.from_url(self.redis_url, decode_responses=True)
            # Test connection
            await self.redis_client.ping()
            logger.info("Connected to Redis for job management")
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            raise
            
    async def disconnect(self):
        """Disconnect from Redis and cleanup."""
        # Cancel all running background tasks
        for job_id, task in self._background_tasks.items():
            if not task.done():
                logger.info(f"Cancelling background task for job {job_id}")
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        self._background_tasks.clear()
        
        if self.redis_client:
            await self.redis_client.close()
            logger.info("Disconnected from Redis")
    
    def _get_job_key(self, job_id: str) -> str:
        """Get Redis key for job info."""
        return f"job:{job_id}"
    
    def _get_result_key(self, job_id: str) -> str:
        """Get Redis key for job results."""
        return f"job_result:{job_id}"
    
    async def create_job(self, request_data: Dict[str, Any]) -> str:
        """
        Create a new background job.
        
        Args:
            request_data: Original request data
            
        Returns:
            Job ID
        """
        if not self.redis_client:
            raise RuntimeError("Redis client not connected")
        
        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        expires_at = datetime.fromtimestamp(time.time() + self.job_ttl, timezone.utc)
        
        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=now,
            total_urls=len(request_data.get("urls", [])),
            request_data=request_data,
            expires_at=expires_at
        )
        
        # Store job info in Redis
        job_key = self._get_job_key(job_id)
        await self.redis_client.setex(
            job_key,
            self.job_ttl,
            job_info.model_dump_json()
        )
        
        logger.info(f"Created job {job_id} with {job_info.total_urls} URLs")
        return job_id
    
    async def get_job_info(self, job_id: str) -> Optional[JobInfo]:
        """
        Get job information.
        
        Args:
            job_id: Job identifier
            
        Returns:
            JobInfo if found, None otherwise
        """
        if not self.redis_client:
            raise RuntimeError("Redis client not connected")
        
        job_key = self._get_job_key(job_id)
        job_data = await self.redis_client.get(job_key)
        
        if not job_data:
            return None
        
        try:
            return JobInfo.model_validate_json(job_data)
        except Exception as e:
            logger.error(f"Failed to parse job info for {job_id}: {e}")
            return None
    
    async def update_job_status(
        self,
        job_id: str,
        status: JobStatus,
        progress: Optional[int] = None,
        processed_urls: Optional[int] = None,
        successful_urls: Optional[int] = None,
        failed_urls: Optional[int] = None,
        error_message: Optional[str] = None
    ):
        """Update job status and progress."""
        if not self.redis_client:
            raise RuntimeError("Redis client not connected")
        
        job_info = await self.get_job_info(job_id)
        if not job_info:
            logger.warning(f"Attempted to update non-existent job {job_id}")
            return
        
        # Update fields
        job_info.status = status
        now = datetime.now(timezone.utc)
        
        if status == JobStatus.RUNNING and job_info.started_at is None:
            job_info.started_at = now
        elif status in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]:
            job_info.completed_at = now
            job_info.results_available = (status == JobStatus.COMPLETED)
        
        if progress is not None:
            job_info.progress = progress
        if processed_urls is not None:
            job_info.processed_urls = processed_urls
        if successful_urls is not None:
            job_info.successful_urls = successful_urls
        if failed_urls is not None:
            job_info.failed_urls = failed_urls
        if error_message is not None:
            job_info.error_message = error_message
        
        # Save updated job info
        job_key = self._get_job_key(job_id)
        await self.redis_client.setex(
            job_key,
            self.job_ttl,
            job_info.model_dump_json()
        )
        
        logger.debug(f"Updated job {job_id} status to {status}")
    
    async def store_job_results(self, job_id: str, results: List[Dict[str, Any]], summary: Dict[str, Any]):
        """Store job results."""
        if not self.redis_client:
            raise RuntimeError("Redis client not connected")
        
        job_info = await self.get_job_info(job_id)
        if not job_info:
            logger.warning(f"Attempted to store results for non-existent job {job_id}")
            return
        
        job_result = JobResult(
            job_id=job_id,
            status=job_info.status,
            total_duration=(
                (job_info.completed_at - job_info.started_at).total_seconds()
                if job_info.completed_at and job_info.started_at
                else 0.0
            ),
            results=results,
            summary=summary,
            created_at=job_info.created_at,
            completed_at=job_info.completed_at or datetime.now(timezone.utc)
        )
        
        # Store results in Redis
        result_key = self._get_result_key(job_id)
        await self.redis_client.setex(
            result_key,
            self.job_ttl,
            job_result.model_dump_json()
        )
        
        logger.info(f"Stored results for job {job_id}")
    
    async def get_job_results(self, job_id: str) -> Optional[JobResult]:
        """Get job results."""
        if not self.redis_client:
            raise RuntimeError("Redis client not connected")
        
        result_key = self._get_result_key(job_id)
        result_data = await self.redis_client.get(result_key)
        
        if not result_data:
            return None
        
        try:
            return JobResult.model_validate_json(result_data)
        except Exception as e:
            logger.error(f"Failed to parse job results for {job_id}: {e}")
            return None
    
    async def start_background_job(self, job_id: str, job_processor_func, *args, **kwargs):
        """Start background job processing."""
        async def job_wrapper():
            try:
                await self.update_job_status(job_id, JobStatus.RUNNING)
                logger.info(f"Starting background processing for job {job_id}")
                
                # Call the actual job processor
                results, summary = await job_processor_func(job_id, *args, **kwargs)
                
                # Store results and mark as completed
                await self.store_job_results(job_id, results, summary)
                await self.update_job_status(job_id, JobStatus.COMPLETED)
                
                logger.info(f"Job {job_id} completed successfully")
                
            except asyncio.CancelledError:
                await self.update_job_status(job_id, JobStatus.CANCELLED)
                logger.info(f"Job {job_id} was cancelled")
                raise
                
            except Exception as e:
                await self.update_job_status(
                    job_id, 
                    JobStatus.FAILED, 
                    error_message=str(e)
                )
                logger.error(f"Job {job_id} failed: {e}")
                
            finally:
                # Cleanup background task reference
                if job_id in self._background_tasks:
                    del self._background_tasks[job_id]
        
        # Start background task
        task = asyncio.create_task(job_wrapper())
        self._background_tasks[job_id] = task
        
        logger.info(f"Started background task for job {job_id}")
        return task
    
    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a running job."""
        # Cancel background task if running
        if job_id in self._background_tasks:
            task = self._background_tasks[job_id]
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                logger.info(f"Cancelled background task for job {job_id}")
                return True
        
        # Update job status if it exists
        job_info = await self.get_job_info(job_id)
        if job_info and job_info.status in [JobStatus.PENDING, JobStatus.RUNNING]:
            await self.update_job_status(job_id, JobStatus.CANCELLED)
            return True
        
        return False
    
    async def cleanup_expired_jobs(self):
        """Clean up expired jobs (this would typically be called by a scheduled task)."""
        # This is handled automatically by Redis TTL, but we could add
        # additional cleanup logic here if needed
        pass


# Global job manager instance
_job_manager: Optional[JobManager] = None


async def get_job_manager() -> JobManager:
    """Get the global job manager instance."""
    global _job_manager
    
    if _job_manager is None:
        redis_url = os.getenv("REDIS_URI", "redis://localhost:6379")
        _job_manager = JobManager(redis_url)
        await _job_manager.connect()
    
    return _job_manager


async def cleanup_job_manager():
    """Clean up the global job manager."""
    global _job_manager
    
    if _job_manager:
        await _job_manager.disconnect()
        _job_manager = None