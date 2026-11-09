
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    Query,
    Response,
    status
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm

from pydantic import BaseModel, EmailStr, Field, ConfigDict

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from database import (
    engine,
    test_connection,
    Base,
    get_db
)

import models

from models import User, AuditLog

from schemas import (
    UserCreate,
    UserLogin,
    UserResponse,
    Token
)

from auth import (
    hash_password,
    verify_password,
    create_access_token,
    get_current_user
)

from documents import router as documents_router


# LOGGING CONFIGURATION
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# DATABASE INITIALIZATION
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        test_connection()
        Base.metadata.create_all(bind=engine)
        logger.info("TrustVault database initialized successfully")

    except Exception:
        logger.exception("Database initialization failed")
        raise

    yield

    logger.info("TrustVault application shutting down")


# FASTAPI APPLICATION
app = FastAPI(
    title="TrustVault API",
    description="Secure Document Sharing with a Real Audit Trail",
    version="1.0.0",
    lifespan=lifespan
)


# FRONTEND REGISTRATION SCHEMA
class FrontendRegister(BaseModel):
    full_name: str = Field(min_length=3, max_length=100)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


# FRONTEND USER RESPONSE SCHEMA
class FrontendUserResponse(BaseModel):
    id: int
    full_name: str = Field(validation_alias="username")
    email: EmailStr
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


# FRONTEND USER SEARCH RESPONSE
class FrontendUserSummary(BaseModel):
    id: str
    full_name: str
    email: EmailStr


# CORS MIDDLEWARE
DEFAULT_FRONTEND_ORIGINS = [
    "http://127.0.0.1:5500",
    "http://localhost:5500",
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://trustvault-frontend-rmii.onrender.com",
]

configured_origins = os.getenv("FRONTEND_ORIGINS", "")

FRONTEND_ORIGINS = list(dict.fromkeys(
    origin.strip().rstrip("/")
    for origin in (
        DEFAULT_FRONTEND_ORIGINS
        + configured_origins.split(",")
    )
    if origin.strip()
))

logger.info("Allowed frontend origins: %s", FRONTEND_ORIGINS)

app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# REGISTER DOCUMENT MANAGEMENT ROUTER
app.include_router(documents_router)


# HOME API
@app.get("/", tags=["General"])
def home():
    return {
        "message": "Welcome to TrustVault",
        "status": "running",
        "version": "1.0.0"
    }


# HEALTH API
@app.get("/health", tags=["General"])
def health_check():
    return {
        "status": "healthy",
        "service": "TrustVault API",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


# DATABASE HEALTH API
@app.get("/db-health", tags=["Database"])
def database_health():
    try:
        test_connection()

        return {
            "status": "connected",
            "database": "trustvault",
            "message": "Database connection successful"
        }

    except Exception:
        logger.exception("Database health check failed")

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database connection failed"
        )


# GET DATABASE TABLES
@app.get("/tables", tags=["Database"])
def get_tables():
    return {
        "database": "trustvault",
        "tables": sorted(Base.metadata.tables.keys()),
        "total_tables": len(Base.metadata.tables)
    }


# EXISTING USER REGISTRATION API
@app.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Authentication"]
)
def register_user(
    user_data: UserCreate,
    db: Session = Depends(get_db)
):
    email = user_data.email.strip().lower()

    existing_user = db.query(User).filter(
        User.email == email
    ).first()

    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )

    new_user = User(
        username=user_data.username.strip(),
        email=email,
        password_hash=hash_password(user_data.password)
    )

    try:
        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        logger.info("New user registered successfully")
        return new_user

    except IntegrityError:
        db.rollback()

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )


# FRONTEND REGISTRATION API
@app.post(
    "/auth/register",
    response_model=FrontendUserResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Authentication"]
)
def frontend_register(
    user_data: FrontendRegister,
    db: Session = Depends(get_db)
):
    email = user_data.email.strip().lower()

    existing_user = db.query(User).filter(
        User.email == email
    ).first()

    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )

    new_user = User(
        username=user_data.full_name.strip(),
        email=email,
        password_hash=hash_password(user_data.password)
    )

    try:
        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        logger.info("Frontend user registered successfully")
        return new_user

    except IntegrityError:
        db.rollback()

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )


# EXISTING USER LOGIN API
@app.post(
    "/login",
    response_model=Token,
    tags=["Authentication"]
)
def login_user(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    email = form_data.username.strip().lower()

    user = db.query(User).filter(
        User.email == email
    ).first()

    if not user or not verify_password(
        form_data.password,
        user.password_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"}
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive"
        )

    access_token = create_access_token(
        data={"sub": str(user.id)}
    )

    return {
        "access_token": access_token,
        "token_type": "bearer"
    }


# FRONTEND LOGIN API
@app.post(
    "/auth/login",
    response_model=Token,
    tags=["Authentication"]
)
def frontend_login(
    user_data: UserLogin,
    db: Session = Depends(get_db)
):
    email = user_data.email.strip().lower()

    user = db.query(User).filter(
        User.email == email
    ).first()

    if not user or not verify_password(
        user_data.password,
        user.password_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password"
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive"
        )

    access_token = create_access_token(
        data={"sub": str(user.id)}
    )

    return {
        "access_token": access_token,
        "token_type": "bearer"
    }


# FRONTEND CURRENT USER API
@app.get(
    "/auth/me",
    response_model=FrontendUserResponse,
    tags=["Authentication"]
)
def frontend_get_current_user(
    current_user: User = Depends(get_current_user)
):
    return current_user


# FRONTEND LOGOUT API
@app.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Authentication"]
)
def frontend_logout(
    current_user: User = Depends(get_current_user)
):
    # JWT is stateless. The frontend removes its stored token.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# EXISTING CURRENT USER PROFILE API
@app.get(
    "/users/me",
    response_model=UserResponse,
    tags=["Authentication"]
)
def get_my_profile(
    current_user: User = Depends(get_current_user)
):
    return current_user


# FRONTEND USER SEARCH API
@app.get(
    "/users/search",
    response_model=list[FrontendUserSummary],
    tags=["Users"]
)
def search_users(
    email: str = Query(default="", max_length=255),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    search_email = email.strip().lower()

    if not search_email:
        return []

    users = (
        db.query(User)
        .filter(
            User.is_active.is_(True),
            User.id != current_user.id,
            User.email.ilike(f"%{search_email}%")
        )
        .order_by(User.email.asc())
        .limit(10)
        .all()
    )

    return [
        {
            "id": str(user.id),
            "full_name": user.username,
            "email": user.email
        }
        for user in users
    ]


# FRONTEND ACTIVITY HISTORY API
@app.get(
    "/activity",
    tags=["Audit History"]
)
def get_frontend_activity(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    action_map = {
        "DOCUMENT_UPLOADED": "uploaded",
        "DOCUMENT_SHARED": "shared",
        "DOCUMENT_REVOKED": "revoked",
        "DOCUMENT_VIEWED": "viewed",
        "DOCUMENT_DOWNLOADED": "downloaded",
        "DOCUMENT_DELETED": "deleted"
    }

    logs = (
        db.query(AuditLog)
        .filter(
            AuditLog.user_id == current_user.id,
            AuditLog.action.in_(action_map.keys())
        )
        .order_by(AuditLog.timestamp.desc())
        .all()
    )

    result = []

    for log in logs:
        document = log.document
        actor = log.user

        action = action_map.get(log.action)

        if action is None:
            continue

        # Deleted documents no longer have a document relationship.
        # Their filename is preserved in the audit log details.
        if document:
            document_name = document.title
            owner = document.owner
        else:
            details = log.details or {}

            document_name = (
                details.get("filename", "Deleted document")
                if isinstance(details, dict)
                else "Deleted document"
            )

            owner = actor if action == "deleted" else None

        if not document_name or not owner:
            continue

        result.append({
            "id": str(log.id),
            "document_name": document_name,
            "owner": {
                "id": str(owner.id),
                "full_name": owner.username,
                "email": owner.email
            },
            "actor": {
                "id": str(actor.id),
                "full_name": actor.username,
                "email": actor.email
            } if actor else None,
            "recipient": None,
            "action": action,
            "created_at": (
                log.timestamp.isoformat()
                if log.timestamp else None
            )
        })

        if len(result) >= limit:
            break

    return result


# EXISTING AUDIT HISTORY API
@app.get(
    "/audit-logs",
    tags=["Audit History"]
)
def get_audit_logs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    logs = (
        db.query(AuditLog)
        .filter(AuditLog.user_id == current_user.id)
        .order_by(AuditLog.timestamp.desc())
        .limit(100)
        .all()
    )

    return [
        {
            "id": log.id,
            "user_id": log.user_id,
            "document_id": log.document_id,
            "action": log.action,
            "details": log.details,
            "ip_address": log.ip_address,
            "timestamp": log.timestamp
        }
        for log in logs
    ]
