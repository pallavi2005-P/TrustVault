
import uuid
import zipfile
import io

from pathlib import Path
from datetime import datetime
from typing import Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    UploadFile,
    File,
    Form,
    Query,
    status
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
from sqlalchemy import func

from database import get_db
from models import User, Document, DocumentShare, AuditLog
from auth import get_current_user


router = APIRouter(
    prefix="/documents",
    tags=["Document Management"]
)

STORAGE_DIR = Path(__file__).resolve().parent / "storage" / "documents"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".docx"}


class ShareRequest(BaseModel):
    recipient_email: EmailStr


class FrontendShareRequest(BaseModel):
    recipient_id: int
    permission: Literal["read"] = "read"


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


def get_file_size(document: Document) -> int:
    file_path = Path(document.file_path)

    if file_path.is_file():
        return file_path.stat().st_size

    return 0


def get_owner_summary(owner: User) -> dict:
    return {
        "id": str(owner.id),
        "full_name": owner.username,
        "email": owner.email
    }


def get_document_response(
    document: Document,
    db: Session,
    shared_at=None
) -> dict:

    share_count = db.query(func.count(DocumentShare.id)).filter(
        DocumentShare.document_id == document.id
    ).scalar() or 0

    response = {
        "id": str(document.id),
        "name": document.title,
        "file_type": Path(document.title).suffix.lower().replace(".", ""),
        "size_bytes": get_file_size(document),
        "uploaded_at": document.uploaded_at,
        "owner": get_owner_summary(document.owner),
        "folder": None,
        "share_count": share_count,
        "description": document.description
    }

    if shared_at is not None:
        response["shared_at"] = shared_at
        response["permission"] = "read"

    return response


def get_accessible_document(
    document_id: int,
    current_user: User,
    db: Session
):

    document = db.query(Document).filter(
        Document.id == document_id,
        Document.owner_id == current_user.id
    ).first()

    if document:
        return document

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


def create_share(
    document: Document,
    recipient: User,
    current_user: User,
    db: Session
):

    if recipient.id == current_user.id:
        raise HTTPException(
            status_code=400,
            detail="You cannot share a document with yourself"
        )

    if not recipient.is_active:
        raise HTTPException(
            status_code=404,
            detail="Recipient is not an active user"
        )

    existing_share = db.query(DocumentShare).filter(
        DocumentShare.document_id == document.id,
        DocumentShare.shared_with_user_id == recipient.id
    ).first()

    if existing_share:
        raise HTTPException(
            status_code=409,
            detail="Document is already shared with this user"
        )

    new_share = DocumentShare(
        document_id=document.id,
        shared_with_user_id=recipient.id,
        permission="view"
    )

    db.add(new_share)
    db.flush()

    db.add(AuditLog(
        user_id=current_user.id,
        document_id=document.id,
        action="DOCUMENT_SHARED",
        details={
            "recipient_email": recipient.email,
            "permission": "read"
        }
    ))

    db.commit()
    db.refresh(new_share)

    return {
        "id": str(new_share.id),
        "document_id": str(document.id),
        "recipient": get_owner_summary(recipient),
        "permission": "read",
        "created_at": new_share.shared_at
    }


# LIST DOCUMENTS
@router.get("/")
def get_documents(
    search: str | None = None,
    folder: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):

    query = db.query(Document).filter(
        Document.owner_id == current_user.id
    )

    if search:
        query = query.filter(
            Document.title.ilike(f"%{search.strip()}%")
        )

    # Folder metadata is not stored in the current database model.
    # The parameter is accepted for frontend compatibility.

    documents = query.order_by(
        Document.uploaded_at.desc()
    ).all()

    return [
        get_document_response(document, db)
        for document in documents
    ]


# UPLOAD DOCUMENT
@router.post(
    "/upload",
    status_code=status.HTTP_201_CREATED
)
async def upload_document(
    file: UploadFile = File(...),
    folder: str | None = Form(None),
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

        db.add(AuditLog(
            user_id=current_user.id,
            document_id=new_document.id,
            action="DOCUMENT_UPLOADED",
            details={
                "filename": original_filename,
                "size_bytes": len(content)
            }
        ))

        db.commit()
        db.refresh(new_document)

        return get_document_response(new_document, db)

    except Exception:
        db.rollback()

        if file_path.exists():
            file_path.unlink()

        raise

    finally:
        await file.close()


# KEEP THE OLDER /my ENDPOINT
@router.get("/my")
def get_my_documents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):

    documents = db.query(Document).filter(
        Document.owner_id == current_user.id
    ).order_by(
        Document.uploaded_at.desc()
    ).all()

    return [
        get_document_response(document, db)
        for document in documents
    ]


# LIST DOCUMENTS SHARED WITH ME
@router.get("/shared-with-me")
def get_shared_documents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):

    shares = db.query(DocumentShare).filter(
        DocumentShare.shared_with_user_id == current_user.id,
        DocumentShare.permission == "view"
    ).order_by(
        DocumentShare.shared_at.desc()
    ).all()

    results = []

    for share in shares:
        document = db.query(Document).filter(
            Document.id == share.document_id
        ).first()

        if document:
            results.append(
                get_document_response(
                    document,
                    db,
                    shared_at=share.shared_at
                )
            )

    return results


# SHARE DOCUMENT USING RECIPIENT ID
@router.post("/{document_id}/shares")
def share_document_by_id(
    document_id: int,
    request: FrontendShareRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):

    document = db.query(Document).filter(
        Document.id == document_id,
        Document.owner_id == current_user.id
    ).first()

    if not document:
        raise HTTPException(
            status_code=404,
            detail="Document not found or you are not the owner"
        )

    recipient = db.query(User).filter(
        User.id == request.recipient_id,
        User.is_active.is_(True)
    ).first()

    if not recipient:
        raise HTTPException(
            status_code=404,
            detail="Recipient is not a registered active user"
        )

    try:
        return create_share(
            document,
            recipient,
            current_user,
            db
        )

    except Exception:
        db.rollback()
        raise


# LIST SHARES FOR A DOCUMENT
@router.get("/{document_id}/shares")
def get_document_shares(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):

    document = db.query(Document).filter(
        Document.id == document_id,
        Document.owner_id == current_user.id
    ).first()

    if not document:
        raise HTTPException(
            status_code=404,
            detail="Document not found or you are not the owner"
        )

    shares = db.query(DocumentShare).filter(
        DocumentShare.document_id == document_id
    ).all()

    results = []

    for share in shares:
        recipient = db.query(User).filter(
            User.id == share.shared_with_user_id
        ).first()

        if recipient:
            results.append({
                "id": str(share.id),
                "document_id": str(document.id),
                "recipient": get_owner_summary(recipient),
                "permission": "read",
                "created_at": share.shared_at
            })

    return results


# REVOKE DOCUMENT ACCESS
@router.delete("/{document_id}/shares/{share_id}")
def revoke_document_share(
    document_id: int,
    share_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):

    document = db.query(Document).filter(
        Document.id == document_id,
        Document.owner_id == current_user.id
    ).first()

    if not document:
        raise HTTPException(
            status_code=404,
            detail="Document not found or you are not the owner"
        )

    share = db.query(DocumentShare).filter(
        DocumentShare.id == share_id,
        DocumentShare.document_id == document_id
    ).first()

    if not share:
        raise HTTPException(
            status_code=404,
            detail="Share record not found"
        )

    db.add(AuditLog(
        user_id=current_user.id,
        document_id=document.id,
        action="DOCUMENT_REVOKED",
        details={
            "shared_with_user_id": share.shared_with_user_id
        }
    ))

    db.delete(share)
    db.commit()

    return {"message": "Document access revoked successfully"}


# KEEP THE OLDER EMAIL-BASED SHARE ENDPOINT
@router.post("/{document_id}/share")
def share_document_by_email(
    document_id: int,
    request: ShareRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):

    document = db.query(Document).filter(
        Document.id == document_id,
        Document.owner_id == current_user.id
    ).first()

    if not document:
        raise HTTPException(
            status_code=404,
            detail="Document not found or you are not the owner"
        )

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

    try:
        result = create_share(
            document,
            recipient,
            current_user,
            db
        )

        return {
            "message": "Document shared successfully",
            "document_id": document.id,
            "recipient_email": recipient.email,
            "permission": "view",
            "share_id": result["id"]
        }

    except Exception:
        db.rollback()
        raise


# VIEW DOCUMENT DETAILS
@router.get("/{document_id}")
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

    db.add(AuditLog(
        user_id=current_user.id,
        document_id=document.id,
        action="DOCUMENT_VIEWED",
        details={"filename": document.title}
    ))

    db.commit()

    return get_document_response(document, db)


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

    db.add(AuditLog(
        user_id=current_user.id,
        document_id=document.id,
        action="DOCUMENT_DOWNLOADED",
        details={"filename": document.title}
    ))

    db.commit()

    return FileResponse(
        path=str(file_path),
        filename=document.title,
        media_type="application/octet-stream"
    )


# DELETE DOCUMENT
@router.delete("/{document_id}")
def delete_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):

    document = db.query(Document).filter(
        Document.id == document_id,
        Document.owner_id == current_user.id
    ).first()

    if not document:
        raise HTTPException(
            status_code=404,
            detail="Document not found or you are not the owner"
        )

    file_path = Path(document.file_path)

    # Preserve previous audit history while removing its document FK.
    db.query(AuditLog).filter(
        AuditLog.document_id == document.id
    ).update(
        {AuditLog.document_id: None},
        synchronize_session=False
    )

    db.query(DocumentShare).filter(
        DocumentShare.document_id == document.id
    ).delete(synchronize_session=False)

    db.add(AuditLog(
        user_id=current_user.id,
        document_id=None,
        action="DOCUMENT_DELETED",
        details={"filename": document.title}
    ))

    db.delete(document)
    db.commit()

    if file_path.is_file():
        try:
            file_path.unlink()
        except OSError:
            pass

    return {"message": "Document deleted successfully"}
