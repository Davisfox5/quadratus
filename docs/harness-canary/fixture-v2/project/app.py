"""Synthetic tenant API with separate display and record-access tasks."""

from auth import current_tenant
from catalog import RECORDS
from fastapi import Depends, FastAPI, HTTPException
from presentation import display_title
from pydantic import BaseModel

app = FastAPI()


class Record(BaseModel):
    id: str
    title: str


@app.get("/caption")
def caption(title: str):
    return {"caption": display_title(title)}


@app.get("/records/{record_id}", response_model=Record)
def get_record(record_id: str, tenant: str = Depends(current_tenant)) -> Record:
    record = RECORDS.get(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Not found")
    return Record(id=record["id"], title=record["title"])
