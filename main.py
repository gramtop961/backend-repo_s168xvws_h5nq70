import os
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional

from database import db, create_document, get_documents
from bson.objectid import ObjectId

app = FastAPI(title="Scan & Archive API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Utility to build collection name
DOC_COLLECTION = "document"
FILE_COLLECTION = "fileblob"

# Schemas
class DocumentCreate(BaseModel):
    title: str
    tags: Optional[List[str]] = []
    notes: Optional[str] = None

class DocumentOut(BaseModel):
    id: str
    title: str
    tags: List[str] = []
    notes: Optional[str] = None
    mime_type: Optional[str] = None
    size: Optional[int] = None
    text_preview: Optional[str] = None

@app.get("/")
def read_root():
    return {"message": "Scan & Archive Backend Ready"}

@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }

    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    return response

# Endpoint to create a document metadata first (without file)
@app.post("/api/documents", response_model=DocumentOut)
async def create_document_metadata(payload: DocumentCreate):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    doc = {
        "title": payload.title,
        "tags": payload.tags or [],
        "notes": payload.notes,
        "mime_type": None,
        "size": None,
        "text_preview": None,
    }

    inserted_id = create_document(DOC_COLLECTION, doc)
    saved = db[DOC_COLLECTION].find_one({"_id": ObjectId(inserted_id)})
    return {
        "id": str(saved["_id"]),
        "title": saved.get("title"),
        "tags": saved.get("tags", []),
        "notes": saved.get("notes"),
        "mime_type": saved.get("mime_type"),
        "size": saved.get("size"),
        "text_preview": saved.get("text_preview"),
    }

# Upload file and attach to a document (by id)
@app.post("/api/documents/{doc_id}/upload", response_model=DocumentOut)
async def upload_document_file(doc_id: str, file: UploadFile = File(...)):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    try:
        oid = ObjectId(doc_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid document id")

    existing = db[DOC_COLLECTION].find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Document not found")

    # Read file bytes
    content = await file.read()

    # Store file into a separate collection as binary blob to keep things simple
    blob = {
        "doc_id": oid,
        "filename": file.filename,
        "content_type": file.content_type,
        "size": len(content),
        "content": content,  # stored as BSON binary
    }
    blob_id = create_document(FILE_COLLECTION, blob)

    # Update document metadata
    db[DOC_COLLECTION].update_one(
        {"_id": oid},
        {"$set": {"mime_type": file.content_type, "size": len(content), "file_blob_id": ObjectId(blob_id)}}
    )

    updated = db[DOC_COLLECTION].find_one({"_id": oid})

    # Naive text preview: if text or pdf, try to decode first KB
    text_preview = None
    if file.content_type and ("text" in file.content_type or file.content_type == "application/pdf"):
        try:
            text_preview = content[:1024].decode(errors="ignore")
        except Exception:
            text_preview = None
        db[DOC_COLLECTION].update_one({"_id": oid}, {"$set": {"text_preview": text_preview}})
        updated = db[DOC_COLLECTION].find_one({"_id": oid})

    return {
        "id": str(updated["_id"]),
        "title": updated.get("title"),
        "tags": updated.get("tags", []),
        "notes": updated.get("notes"),
        "mime_type": updated.get("mime_type"),
        "size": updated.get("size"),
        "text_preview": updated.get("text_preview"),
    }

# Download file content
@app.get("/api/documents/{doc_id}/download")
async def download_document_file(doc_id: str):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    try:
        oid = ObjectId(doc_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid document id")

    doc = db[DOC_COLLECTION].find_one({"_id": oid})
    if not doc or not doc.get("file_blob_id"):
        raise HTTPException(status_code=404, detail="File not found for this document")

    blob = db[FILE_COLLECTION].find_one({"_id": doc["file_blob_id"]})
    if not blob:
        raise HTTPException(status_code=404, detail="File blob missing")

    return StreamingResponse(iter([blob["content"]]), media_type=blob.get("content_type") or "application/octet-stream",
                             headers={"Content-Disposition": f"attachment; filename={blob.get('filename', 'file')}"})

# List documents
@app.get("/api/documents", response_model=List[DocumentOut])
async def list_documents(q: Optional[str] = None):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    filter_query = {}
    if q:
        # simple search on title or tags
        filter_query = {"$or": [
            {"title": {"$regex": q, "$options": "i"}},
            {"tags": {"$elemMatch": {"$regex": q, "$options": "i"}}}
        ]}

    docs = db[DOC_COLLECTION].find(filter_query).sort("created_at", -1)
    results = []
    for d in docs:
        results.append({
            "id": str(d["_id"]),
            "title": d.get("title"),
            "tags": d.get("tags", []),
            "notes": d.get("notes"),
            "mime_type": d.get("mime_type"),
            "size": d.get("size"),
            "text_preview": d.get("text_preview"),
        })
    return results

# Simple update for title/tags/notes
@app.put("/api/documents/{doc_id}", response_model=DocumentOut)
async def update_document(doc_id: str, payload: DocumentCreate):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    try:
        oid = ObjectId(doc_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid document id")

    update = {"title": payload.title, "tags": payload.tags or [], "notes": payload.notes}
    db[DOC_COLLECTION].update_one({"_id": oid}, {"$set": update})

    d = db[DOC_COLLECTION].find_one({"_id": oid})
    if not d:
        raise HTTPException(status_code=404, detail="Document not found")

    return {
        "id": str(d["_id"]),
        "title": d.get("title"),
        "tags": d.get("tags", []),
        "notes": d.get("notes"),
        "mime_type": d.get("mime_type"),
        "size": d.get("size"),
        "text_preview": d.get("text_preview"),
    }

# Delete document (and its blob if exists)
@app.delete("/api/documents/{doc_id}")
async def delete_document(doc_id: str):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    try:
        oid = ObjectId(doc_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid document id")

    d = db[DOC_COLLECTION].find_one({"_id": oid})
    if not d:
        raise HTTPException(status_code=404, detail="Document not found")

    if d.get("file_blob_id"):
        db[FILE_COLLECTION].delete_one({"_id": d["file_blob_id"]})
    db[DOC_COLLECTION].delete_one({"_id": oid})
    return {"status": "deleted"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
