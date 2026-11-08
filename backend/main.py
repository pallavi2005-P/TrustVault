
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    status
)
from fastapi.security import OAuth2PasswordRequestForm

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


# USER REGISTRATION API
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


# USER LOGIN API
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


# CURRENT USER PROFILE API
@app.get(
    "/users/me",
    response_model=UserResponse,
    tags=["Authentication"]
)
def get_my_profile(
    current_user: User = Depends(get_current_user)
):

    return current_user
    # AUDIT HISTORY API
@app.get("/audit-logs", tags=["Audit History"])
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
