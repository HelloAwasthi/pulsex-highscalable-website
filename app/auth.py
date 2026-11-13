import time
import uuid
import jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
import redis.asyncio as redis

from app.config import settings
from app.database import get_db
from app.models import User
from app.redis_engine import get_redis

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(user_id: int, username: str) -> str:
    now = int(time.time())
    exp = now + (settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    payload = {
        "sub": str(user_id),
        "username": username,
        "jti": str(uuid.uuid4()),  # Unique Token ID for Redis Revocation
        "iat": now,
        "exp": exp,
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
    r: redis.Redis = Depends(get_redis),
) -> dict:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        jti = payload.get("jti")
        user_id = int(payload.get("sub"))
    except (jwt.PyJWTError, ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    # RESUME BULLET #3: Sub-millisecond Redis Blacklist check for logged-out tokens
    if await r.exists(f"bl:jti:{jti}"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has been logged out")

    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists")

    return {"user": user, "jti": jti, "exp": payload["exp"]}


async def get_optional_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
    r: redis.Redis = Depends(get_redis),
) -> User | None:
    """Allows logged-out visitors to still view the feed while logged-in users see their liked states."""
    if not token:
        return None
    try:
        auth_data = await get_current_user(token=token, db=db, r=r)
        return auth_data["user"]
    except HTTPException:
        return None