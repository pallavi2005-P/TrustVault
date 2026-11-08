
from pydantic import BaseModel, Field, ConfigDict


# User Registration Schema
class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=8, max_length=128)


# User Login Schema
class UserLogin(BaseModel):
    email: str
    password: str


# User Response Schema
class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


# JWT Token Response Schema
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# JWT Token Data Schema
class TokenData(BaseModel):
    user_id: int | None = None
