from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=30, pattern="^[a-zA-Z0-9_]+$")
    password: str = Field(..., min_length=6, max_length=72)


class UserResponse(BaseModel):
    id: int
    username: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str


class PostCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=280)


class PostResponse(BaseModel):
    id: int
    content: str
    author_username: str
    likes_count: int
    spotlight_slots_left: int
    liked_by_me: bool = False
    spotlighted_by_me: bool = False
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class FeedResponse(BaseModel):
    source: str  # "REDIS_CACHE (<1ms)" or "POSTGRES_DB (Cache Miss Rebuilt)"
    posts: list[PostResponse]