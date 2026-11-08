
import uuid
import zipfile
import io

from pathlib import Path
from datetime import datetime

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    UploadFile,
    File,
    Form,
    status
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, EmailStr
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from database import get_db
from models import User, Document, DocumentShare, AuditLog
from auth import get_current_user


router = APIRouter(
    prefix="/documents",
    tags=["Document Management"]
)

# Secure local storage
STORAGE_DIR = Path(__file__).resolve().parent / "storage" / "documents"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

ALLOWED_EXTENSIONS = {".pdf", ".docx"}


# Document response schema
class DocumentResponse(BaseModel):
    id: int
    title: str
    description: str | None
    owner_id: int
    uploaded_at: datetime

    model_config = ConfigDict(from_attributes=True)


# Document sharing request schema
class ShareRequest(BaseModel):
    recipient_email: EmailStr


# Validate uploaded files
def validate_file(filename: str, content: bytes):

    extension = Path(filename).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Only PDF and DOCX files are allowed"
        )

    if extension == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise HTTPException(
                status_code=400,
                detail="Invalid PDF file"
            )

    elif extension == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = archive.namelist()

                if (
                    "[Content_Types].xml" not in names
                    or "word/document.xml" not in names
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid DOCX file"
                    )

        except zipfile.BadZipFile:
            raise HTTPException(
                status_code=400,
                detail="Invalid DOCX file"
            )


# Check whether the current user can access a document
def get_accessible_document(
    document_id: int,
    current_user: User,
    db: Session
):

    # First check whether the user owns the document
    document = db.query(Document).filter(
        Document.id == document_id,
        Document.owner_id == current_user.id
    ).first()

    if document:
        return document

    # Otherwise check whether the document was shared with the user
    share = db.query(DocumentShare).filter(
        DocumentShare.document_id == document_id,
        DocumentShare.shared_with_user_id == current_user.id,
        DocumentShare.permission == "view"
    ).first()

    if not share:
        raise HTTPException(
            status_code=404,
            detail="Document not found"
        )

    document = db.query(Document).filter(
        Document.id == document_id
    ).first()

    if not document:
        raise HTTPException(
            status_code=404,
            detail="Document not found"
        )

    return document


# UPLOAD DOCUMENT
@router.post(
    "/upload",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED
)
async def upload_document(
    file: UploadFile = File(...),
    description: str | None = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):

    original_filename = Path(file.filename or "").name

    if not original_filename:
        raise HTTPException(
            status_code=400,
            detail="Filename is required"
        )

    content = await file.read(MAX_FILE_SIZE + 1)

    if not content:
        raise HTTPException(
            status_code=400,
            detail="Uploaded file is empty"
        )

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail="Maximum file size is 10 MB"
        )

    validate_file(original_filename, content)

    extension = Path(original_filename).suffix.lower()

    # Generate a unique server-side filename
    stored_filename = f"{uuid.uuid4().hex}{extension}"
    file_path = STORAGE_DIR / stored_filename

    try:
        with open(file_path, "xb") as stored_file:
            stored_file.write(content)

        new_document = Document(
            title=original_filename,
            description=description,
            file_path=str(file_path),
            owner_id=current_user.id
        )

        db.add(new_document)
        db.flush()

        # Record upload activity
        audit = AuditLog(
            user_id=current_user.id,
            document_id=new_document.id,
            action="DOCUMENT_UPLOADED",
            details={
                "filename": original_filename,
                "size_bytes": len(content)
            }
        )

        db.add(audit)
        db.commit()
        db.refresh(new_document)

        return new_document

    except Exception:
        db.rollback()

        if file_path.exists():
            file_path.unlink()

        raise

    finally:
        await file.close()


# LIST MY DOCUMENTS
@router.get(
    "/my",
    response_model=list[DocumentResponse]
)
def get_my_documents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):

    return db.query(Document).filter(
        Document.owner_id == current_user.id
    ).order_by(
        Document.uploaded_at.desc()
    ).all()


# SHARE DOCUMENT WITH A REGISTERED USER
@router.post("/{document_id}/share")
def share_document(
    document_id: int,
    request: ShareRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):

    # Only the document owner can share it
    document = db.query(Document).filter(
        Document.id == document_id,
        Document.owner_id == current_user.id
    ).first()

    if not document:
        raise HTTPException(
            status_code=404,
            detail="Document not found or you are not the owner"
        )

    # Find the registered recipient
    recipient_email = str(request.recipient_email).strip().lower()

    recipient = db.query(User).filter(
        func.lower(User.email) == recipient_email,
        User.is_active.is_(True)
    ).first()

    if not recipient:
        raise HTTPException(
            status_code=404,
            detail="Recipient is not a registered active user"
        )

    # Prevent sharing with yourself
    if recipient.id == current_user.id:
        raise HTTPException(
            status_code=400,
            detail="You cannot share a document with yourself"
        )

    # Check whether the document is already shared
    existing_share = db.query(DocumentShare).filter(
        DocumentShare.document_id == document.id,
        DocumentShare.shared_with_user_id == recipient.id
    ).first()

    if existing_share:
        raise HTTPException(
            status_code=409,
            detail="Document is already shared with this user"
        )

    try:
        # Create read-only sharing permission
        new_share = DocumentShare(
            document_id=document.id,
            shared_with_user_id=recipient.id,
            permission="view"
        )

        db.add(new_share)
        db.flush()

        # Record sharing activity
        audit = AuditLog(
            user_id=current_user.id,
            document_id=document.id,
            action="DOCUMENT_SHARED",
            details={
                "recipient_email": recipient.email,
                "permission": "view"
            }
        )

        db.add(audit)
        db.commit()
        db.refresh(new_share)

        return {
            "message": "Document shared successfully",
            "document_id": document.id,
            "recipient_email": recipient.email,
            "permission": new_share.permission,
            "share_id": new_share.id
        }

    except Exception:
        db.rollback()
        raise


# LIST DOCUMENTS SHARED WITH ME
@router.get(
    "/shared-with-me",
    response_model=list[DocumentResponse]
)
def get_shared_documents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):

    documents = db.query(Document).join(
        DocumentShare,
        DocumentShare.document_id == Document.id
    ).filter(
        DocumentShare.shared_with_user_id == current_user.id,
        DocumentShare.permission == "view"
    ).order_by(
        DocumentShare.shared_at.desc()
    ).all()

    return documents


# VIEW DOCUMENT DETAILS
@router.get(
    "/{document_id}",
    response_model=DocumentResponse
)
def get_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):

    document = get_accessible_document(
        document_id,
        current_user,
        db
    )

    # Record document viewing activity
    audit = AuditLog(
        user_id=current_user.id,
        document_id=document.id,
        action="DOCUMENT_VIEWED",
        details={
            "filename": document.title
        }
    )

    db.add(audit)
    db.commit()

    return document


# DOWNLOAD DOCUMENT
@router.get("/{document_id}/download")
def download_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):

    document = get_accessible_document(
        document_id,
        current_user,
        db
    )

    file_path = Path(document.file_path).resolve()

    if (
        not file_path.is_relative_to(STORAGE_DIR.resolve())
        or not file_path.is_file()
    ):
        raise HTTPException(
            status_code=404,
            detail="Document file not found"
        )

    # Record download activity
    audit = AuditLog(
        user_id=current_user.id,
        document_id=document.id,
        action="DOCUMENT_DOWNLOADED",
        details={
            "filename": document.title
        }
    )

    db.add(audit)
    db.commit()

    return FileResponse(
        path=str(file_path),
        filename=document.title,
        media_type="application/octet-stream"
    )
