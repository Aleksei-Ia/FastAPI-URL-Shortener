from pydantic import BaseModel, HttpUrl
from typing import Optional
from datetime import datetime

class UserCreate(BaseModel):
    username: str
    password: str

class LinkInfo(BaseModel):
    short_code: str
    original_url: HttpUrl
    created_at: datetime
    expires_at: Optional[datetime] = None
    user_id: Optional[int] = None

    class Config:
        orm_mode = True

class LinkStats(BaseModel):
    short_code: str
    original_url: HttpUrl
    created_at: datetime
    last_accessed: datetime
    click_count: int
    expires_at: Optional[datetime]
    user_id: Optional[int] = None

    class Config:
        orm_mode = True
