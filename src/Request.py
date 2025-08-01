from enum import StrEnum
from typing import Optional, Union
import asyncio
import json
import random
import time

import httpx

from src.Config import TaskConfig, RequestConfig


class ResponseStatus(StrEnum):
    """Response status categories."""

    SUCCESS = "Success"
    HTTP_4XX = "HTTP 4xx"
    HTTP_5XX = "HTTP 5xx"
    HTTP_ERROR = "HTTP Error"
    TIMEOUT = "Timeout"
    TIMEOUT_CONNECT = "Timeout Connect"
    EXCEPTION = "Exception"


class RequestState(StrEnum):
    """Request worker state."""

    WAITING = "Waiting"
    WORKING = "Working"
    STOPPED = "Stopped"


class RateLimiter:
    """Rate limiter to control requests per second."""

    def __init__(self, max_rps: float):
        self._interval = 1.0 / max(1.0, max_rps)
        self._lock = asyncio.Lock()
        self.last_request_time = 0.0

    async def acquire(self):
        async with self._lock:
            current_time = time.time()
            wait_time = self._interval - (current_time - self.last_request_time)
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            self.last_request_time = time.time()


class RequestWorker:
    """Individual request worker for concurrent execution."""

    def __init__(
        self,
        task_config: TaskConfig,
        session: httpx.AsyncClient,
        rate_limiter: Optional[RateLimiter] = None,
    ):
        self.task_config = task_config
        self.session = session
        self.rate_limiter = rate_limiter
        self.state = RequestState.WAITING
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._stats_callback = None

    def set_stats_callback(self, callback):
        """Set callback function to report statistics"""
        self._stats_callback = callback

    def get_state(self) -> RequestState:
        """Get current worker state"""
        return self.state

    async def join(self, timeout: Optional[float] = None) -> bool:
        """Wait for task completion with optional timeout"""
        if self._task is None:
            return True

        try:
            await asyncio.wait_for(self._task, timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def _select_request(self) -> RequestConfig:
        """Select a request based on the policy order"""
        if self.task_config.policy.order == "random":
            return random.choice(self.task_config.requests)
        else:
            # Fallback to first request if order is not supported
            return self.task_config.requests[0]

    def _prepare_request_data(self, request_config: RequestConfig) -> Optional[str]:
        """Prepare request data for the selected request"""
        if request_config.method.upper() in ["POST", "PUT", "PATCH"]:
            if request_config.data:
                if isinstance(request_config.data, dict):
                    return json.dumps(request_config.data)
                return str(request_config.data)
        return None

    def _classify_response(self, response: Union[httpx.Response, Exception]) -> ResponseStatus:
        """Classify response into status category"""
        if isinstance(response, httpx.Response):
            return {
                2: ResponseStatus.SUCCESS,
                4: ResponseStatus.HTTP_4XX,
                5: ResponseStatus.HTTP_5XX,
            }.get(response.status_code // 100, ResponseStatus.HTTP_ERROR)
        elif isinstance(response, httpx.ConnectTimeout):
            return ResponseStatus.TIMEOUT_CONNECT
        elif isinstance(response, httpx.TimeoutException):
            return ResponseStatus.TIMEOUT
        else:
            return ResponseStatus.EXCEPTION

    @staticmethod
    def estimate_response_size(response: httpx.Response) -> int:
        """Estimate the total size of request and response"""
        request = response.request
        request_headers_len = len("\r\n".join(f"{k}: {v}" for k, v in request.headers.items()))
        request_body_len = len(request.content) if request.content else 0
        response_headers_len = len("\r\n".join(f"{k}: {v}" for k, v in response.headers.items()))
        response_body_len = len(response.content) if response.content else 0
        return request_headers_len + request_body_len + response_headers_len + response_body_len

    async def _execute_request(self) -> tuple[Union[httpx.Response, Exception], float, int]:
        """Execute a single request and return response, time taken, and bytes count"""
        # Select request for this iteration
        request_config = self._select_request()
        post_data = self._prepare_request_data(request_config)

        # Merge default headers with request-specific headers
        merged_headers = {}
        merged_headers.update(self.task_config.prefabs.default_headers)
        merged_headers.update(request_config.headers)

        start_time = time.time()

        try:
            self.state = RequestState.WORKING

            if post_data is not None:
                response = await self.session.post(request_config.url, content=post_data, headers=merged_headers)
            else:
                response = await self.session.get(request_config.url, headers=merged_headers)

            # Calculate bytes transferred
            bytes_count = self.estimate_response_size(response) if isinstance(response, httpx.Response) else 0

        except Exception as e:
            response = e
            bytes_count = 0
        finally:
            self.state = RequestState.WAITING

        response_time = time.time() - start_time
        return response, response_time, bytes_count

    async def _run(self):
        """Internal run method"""
        try:
            while not self._stop_event.is_set():
                if self.rate_limiter:
                    await self.rate_limiter.acquire()

                if self._stop_event.is_set():
                    break

                response, response_time, bytes_count = await self._execute_request()

                # Report statistics if callback is set
                if self._stats_callback:
                    status = self._classify_response(response)
                    self._stats_callback(response, time.time() - response_time, status, response_time, bytes_count)

                if self._stop_event.is_set():
                    break

        finally:
            self.state = RequestState.STOPPED

    def start(self):
        """Start the worker task"""
        if self._task is None or self._task.done():
            self.state = RequestState.WAITING
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self, timeout: Optional[float] = 10.0):
        """Stop the worker task with optional timeout"""
        self._stop_event.set()
        if self._task and not self._task.done():
            try:
                if timeout is None:
                    # Wait indefinitely
                    await self._task
                else:
                    # Wait with timeout
                    await asyncio.wait_for(self._task, timeout=timeout)
            except asyncio.TimeoutError:
                try:
                    self._task.cancel()
                    await self._task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
        self.state = RequestState.STOPPED

    def __del__(self):
        """Cleanup when worker is destroyed"""
        if self._task and not self._task.done():
            self._task.cancel()
