import asyncio
import json
import random
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload
import redis.asyncio as redis

from app.config import settings
from app.database import Base, engine, get_db
from app.models import User, Post, PostLike, SpotlightClaim
from app.schemas import UserCreate, TokenResponse, PostCreate, PostResponse, FeedResponse
from app.auth import (
    hash_password,
    verify_password,
    create_access_token,
    get_current_user,
    get_optional_user,
)
from app.redis_engine import get_redis, SlidingWindowRateLimiter, distributed_lock

FEED_CACHE_KEY = "feed:top_posts"
LEADERBOARD_ZSET_KEY = "leaderboard:posts"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create PostgreSQL tables on startup
    Base.metadata.create_all(bind=engine)
    # Initialize Async Redis Connection Pool
    app.state.redis = redis.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        max_connections=30,
    )
    yield
    await app.state.redis.aclose()


app = FastAPI(title="PulseX - High-Concurrency Social & Flash Engine", lifespan=lifespan)

# Enable CORS so frontend JavaScript can talk to FastAPI
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 1. AUTH ROUTES (Register, Login, Stateless JWT Revocation Logout)
# ---------------------------------------------------------------------------
@app.post(
    "/api/auth/register",
    response_model=TokenResponse,
    dependencies=[Depends(SlidingWindowRateLimiter(max_requests=10, window_seconds=60))],
)
async def register(payload: UserCreate, db: Session = Depends(get_db)):
    existing = db.scalar(select(User).where(User.username == payload.username.lower()))
    if existing:
        raise HTTPException(status_code=400, detail="Username already taken")

    user = User(username=payload.username.lower(), password_hash=hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(user.id, user.username)
    return TokenResponse(access_token=token, username=user.username)


@app.post(
    "/api/auth/login",
    response_model=TokenResponse,
    dependencies=[Depends(SlidingWindowRateLimiter(max_requests=10, window_seconds=60))],
)
async def login(payload: UserCreate, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == payload.username.lower()))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    token = create_access_token(user.id, user.username)
    return TokenResponse(access_token=token, username=user.username)


@app.post("/api/auth/logout")
async def logout(
    auth_data: dict = Depends(get_current_user),
    r: redis.Redis = Depends(get_redis),
):
    """RESUME BULLET #3: Blacklist JWT jti in Redis for its exact remaining TTL."""
    jti = auth_data["jti"]
    exp = auth_data["exp"]
    remaining_ttl = exp - int(time.time())
    if remaining_ttl > 0:
        await r.set(f"bl:jti:{jti}", "revoked", ex=remaining_ttl)
    return {"message": "Logged out and token revoked in Redis"}


# ---------------------------------------------------------------------------
# 2. FEED ROUTE (Multi-Layer Cache-Aside + Stampede Protection + Most Liked First)
# ---------------------------------------------------------------------------
@app.get(
    "/api/posts",
    response_model=FeedResponse,
    dependencies=[Depends(SlidingWindowRateLimiter(max_requests=60, window_seconds=60))],
)
async def get_top_posts(
    db: Session = Depends(get_db),
    r: redis.Redis = Depends(get_redis),
    current_user: User | None = Depends(get_optional_user),
):
    """
    RESUME BULLET #1: Multi-Layer Cache-Aside with Stampede Protection & TTL Jitter.
    Ranks posts by likes_count DESC (Most Liked First), then created_at DESC.
    """
    cached_json = await r.get(FEED_CACHE_KEY)
    source = "REDIS_CACHE (<1ms)"

    if cached_json:
        raw_posts = json.loads(cached_json)
    else:
        # CACHE MISS: Acquire Mutex Lock so 5,000 concurrent requests don't stampede Postgres
        stampede_lock = "lock:rebuild:feed"
        acquired = await r.set(stampede_lock, "1", nx=True, px=3000)

        if acquired:
            try:
                source = "POSTGRES_DB (Cache Rebuilt)"
                stmt = (
                    select(Post)
                    .options(joinedload(Post.author))
                    .order_by(Post.likes_count.desc(), Post.created_at.desc())
                    .limit(50)
                )
                posts_db = db.scalars(stmt).all()

                raw_posts = [
                    {
                        "id": p.id,
                        "content": p.content,
                        "author_username": p.author.username,
                        "likes_count": p.likes_count,
                        "spotlight_slots_left": p.spotlight_slots_left,
                        "created_at": p.created_at.isoformat(),
                    }
                    for p in posts_db
                ]

                # Save to Redis with TTL Jitter (30s to 45s) to prevent Cache Avalanche
                jitter_ttl = 30 + random.randint(0, 15)
                await r.set(FEED_CACHE_KEY, json.dumps(raw_posts), ex=jitter_ttl)
            finally:
                await r.delete(stampede_lock)
        else:
            # Another worker is rebuilding the cache right now; wait 50ms and read from Redis
            await asyncio.sleep(0.05)
            retry_cache = await r.get(FEED_CACHE_KEY)
            raw_posts = json.loads(retry_cache) if retry_cache else []
            source = "REDIS_CACHE (Post-Mutex Wait)"

    # Decorate with current user's personal Like / Spotlight status
    liked_ids = set()
    spotlight_ids = set()
    if current_user and raw_posts:
        liked_rows = db.scalars(
            select(PostLike.post_id).where(PostLike.user_id == current_user.id)
        ).all()
        spotlight_rows = db.scalars(
            select(SpotlightClaim.post_id).where(SpotlightClaim.user_id == current_user.id)
        ).all()
        liked_ids = set(liked_rows)
        spotlight_ids = set(spotlight_rows)

    enriched = [
        PostResponse(
            **p,
            liked_by_me=(p["id"] in liked_ids),
            spotlighted_by_me=(p["id"] in spotlight_ids),
        )
        for p in raw_posts
    ]
    return FeedResponse(source=source, posts=enriched)


# ---------------------------------------------------------------------------
# 3. CREATE POST, ATOMIC LIKE, & LIMITED SPOTLIGHT RESERVATION
# ---------------------------------------------------------------------------
@app.post(
    "/api/posts",
    response_model=PostResponse,
    dependencies=[Depends(SlidingWindowRateLimiter(max_requests=15, window_seconds=60))],
)
async def create_post(
    payload: PostCreate,
    auth_data: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    r: redis.Redis = Depends(get_redis),
):
    user: User = auth_data["user"]
    new_post = Post(content=payload.content.strip(), author_id=user.id)
    db.add(new_post)
    db.commit()
    db.refresh(new_post)

    # Register post in Redis Leaderboard ZSET and invalidate feed cache
    await r.zadd(LEADERBOARD_ZSET_KEY, {f"post:{new_post.id}": 0})
    await r.delete(FEED_CACHE_KEY)

    return PostResponse(
        id=new_post.id,
        content=new_post.content,
        author_username=user.username,
        likes_count=new_post.likes_count,
        spotlight_slots_left=new_post.spotlight_slots_left,
        liked_by_me=False,
        spotlighted_by_me=False,
        created_at=new_post.created_at,
    )


@app.post("/api/posts/{post_id}/like")
async def toggle_like(
    post_id: int,
    auth_data: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    r: redis.Redis = Depends(get_redis),
):
    """Uses Distributed Lock + Postgres Transaction + Redis ZSET + Cache Invalidation."""
    user: User = auth_data["user"]

    async with distributed_lock(r, f"like:{post_id}:{user.id}", ttl_ms=3000):
        post = db.get(Post, post_id)
        if not post:
            raise HTTPException(status_code=404, detail="Post not found")

        existing_like = db.scalar(
            select(PostLike).where(PostLike.post_id == post_id, PostLike.user_id == user.id)
        )

        if existing_like:
            db.delete(existing_like)
            post.likes_count = max(0, post.likes_count - 1)
            delta = -1
            action = "unliked"
        else:
            db.add(PostLike(user_id=user.id, post_id=post_id))
            post.likes_count += 1
            delta = 1
            action = "liked"

        db.commit()

        # Update real-time Redis ZSET score & invalidate cached feed
        await r.zincrby(LEADERBOARD_ZSET_KEY, delta, f"post:{post_id}")
        await r.delete(FEED_CACHE_KEY)

        return {"action": action, "likes_count": post.likes_count}


@app.post("/api/posts/{post_id}/spotlight")
async def claim_spotlight_slot(
    post_id: int,
    auth_data: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    r: redis.Redis = Depends(get_redis),
):
    """
    RESUME BULLET #4: Concurrency Control for Inventory Reservations.
    Each post only has 5 Spotlight Boost slots (each slot also gives +5 bonus likes).
    Uses a Redis Distributed Lock per post_id so 100 concurrent users can NEVER oversell the 5 slots.
    """
    user: User = auth_data["user"]

    # Lock on the entire post_id inventory across all Uvicorn workers
    async with distributed_lock(r, f"spotlight_inventory:{post_id}", ttl_ms=5000):
        post = db.get(Post, post_id)
        if not post:
            raise HTTPException(status_code=404, detail="Post not found")

        if post.spotlight_slots_left <= 0:
            raise HTTPException(status_code=400, detail="All 5 Spotlight slots are sold out for this post!")

        already_claimed = db.scalar(
            select(SpotlightClaim).where(
                SpotlightClaim.post_id == post_id, SpotlightClaim.user_id == user.id
            )
        )
        if already_claimed:
            raise HTTPException(status_code=400, detail="You already claimed a Spotlight slot on this post!")

        # Decrement inventory and grant +5 Super-Likes atomically
        post.spotlight_slots_left -= 1
        post.likes_count += 5
        db.add(SpotlightClaim(user_id=user.id, post_id=post_id))
        db.commit()

        await r.zincrby(LEADERBOARD_ZSET_KEY, 5, f"post:{post_id}")
        await r.delete(FEED_CACHE_KEY)

        return {
            "message": "Spotlight Reserved! (+5 Super-Likes added)",
            "spotlight_slots_left": post.spotlight_slots_left,
            "likes_count": post.likes_count,
        }


# Serve Frontend Static Files
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def serve_frontend():
    return FileResponse("static/index.html")