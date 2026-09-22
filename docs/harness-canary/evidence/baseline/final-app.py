"""Synthetic records only. Authentication tokens are public fixture values."""

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI()
TOKENS = {"alpha-token": "alpha", "beta-token": "beta"}
RECORDS = {
    "alpha-1": {"id": "alpha-1", "tenant_id": "alpha", "title": "Alpha record"},
    "beta-1": {"id": "beta-1", "tenant_id": "beta", "title": "Beta record"},
}


class Record(BaseModel):
    id: str
    title: str


def current_tenant(authorization: str | None = Header(default=None)) -> str:
    token = authorization.removeprefix("Bearer ") if authorization else ""
    if not authorization or not authorization.startswith("Bearer ") or token not in TOKENS:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return TOKENS[token]


@app.get("/records/{record_id}", response_model=Record)
def get_record(record_id: str, tenant: str = Depends(current_tenant)) -> Record:
    record = RECORDS.get(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Not found")
    return Record(id=record["id"], title=record["title"])
