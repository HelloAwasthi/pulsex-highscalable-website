import time
import uuid
from contextlib import asynccontextmanager
from fastapi import Request, HTTPException
import redis.asyncio as redis

# ---------------------------------------------------------------------------
# RESUME BULLET #2: Atomic Lua Sliding-Window Rate Limiter (Sorted Sets)
# ---------------------------------------------------------------------------
SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local req_id = ARGV[4]

-- 1. Remove timestamps older than (now - window)
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)

-- 2. Count active requests in current sliding window
local current_requests = redis.call('ZCARD', key)

if current_requests >= limit then
    return 0 -- Reject (429 Too Many Requests)
end

-- 3. Record current request and set automatic key expiration
redis.call('ZADD', key, now, req_id)
redis.call('PEXPIRE', key, window)
return 1 -- Allowed
"""

# ---------------------------------------------------------------------------
# RESUME BULLET #4: Atomic Lua Safe Lock Release (Prevents deleting others' locks)
# ---------------------------------------------------------------------------
UNLOCK_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""


async def get_redis(request: Request) -> redis.Redis:
    return request.app.state.redis


class SlidingWindowRateLimiter:
    """FastAPI Dependency for per-IP Sliding Window Rate Limiting via Redis ZSET + Lua."""

    def __init__(self, max_requests: int = 20, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_ms = window_seconds * 1000

    async def __call__(self, request: Request):
        r: redis.Redis = request.app.state.redis
        client_ip = request.client.host if request.client else "127.0.0.1"
        key = f"rl:{request.url.path}:{client_ip}"
        now_ms = int(time.time() * 1000)
        req_id = f"{now_ms}:{uuid.uuid4().hex[:8]}"

        allowed = await r.eval(
            SLIDING_WINDOW_LUA,
            1,
            key,
            now_ms,
            self.window_ms,
            self.max_requests,
            req_id,
        )
        if allowed == 0:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded ({self.max_requests} req / {self.window_ms // 1000}s). Slow down!",
            )


@asynccontextmanager
async def distributed_lock(r: redis.Redis, resource_key: str, ttl_ms: int = 5000):
    """
    RESUME BULLET #4: Distributed Lock with SET NX PX and Atomic Lua Token Verification.
    Prevents race conditions and double-booking across multiple Uvicorn workers.
    """
    lock_key = f"lock:{resource_key}"
    owner_token = str(uuid.uuid4())

    acquired = await r.set(lock_key, owner_token, nx=True, px=ttl_ms)
    if not acquired:
        raise HTTPException(
            status_code=409,
            detail="High concurrency conflict: Resource is currently locked by another request. Try again.",
        )
    try:
        yield
    finally:
        # Atomically verify ownership before deleting lock
        await r.eval(UNLOCK_LUA, 1, lock_key, owner_token)