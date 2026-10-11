"""Owner-scoped workspace records shared by the browser and messaging surfaces."""
from __future__ import annotations
import copy
import uuid
from fastapi.encoders import jsonable_encoder
from .storage import _MEMORY, _now, get_supabase, uses_memory_store


def records(owner: str, kind: str) -> list[dict]:
    if uses_memory_store():
        with _MEMORY.lock:
            return [copy.deepcopy(r) for r in _MEMORY.workspace_records.values()
                    if r['owner_handle'] == owner and r['kind'] == kind]
    return (get_supabase().table('workspace_records').select('*')
            .eq('owner_handle', owner).eq('kind', kind).order('created_at').limit(1000).execute().data or [])


def put(owner: str, kind: str, payload: dict, record_id: str | None = None) -> dict:
    record_id = record_id or str(uuid.uuid4())
    row = {'id': record_id, 'owner_handle': owner, 'kind': kind,
           'payload': jsonable_encoder(payload), 'updated_at': _now()}
    if uses_memory_store():
        with _MEMORY.lock:
            existing = _MEMORY.workspace_records.get(record_id)
            if existing and (existing['owner_handle'] != owner or existing['kind'] != kind):
                raise LookupError('Record not found.')
            row['created_at'] = existing['created_at'] if existing else _now()
            _MEMORY.workspace_records[record_id] = copy.deepcopy(row)
        return row
    # Deterministic IDs are derived with the owner and kind; all external edits use an owned lookup first.
    existing = (get_supabase().table('workspace_records').select('owner_handle,kind')
                .eq('id', record_id).limit(1).execute().data or [])
    if existing and (existing[0]['owner_handle'] != owner or existing[0]['kind'] != kind):
        raise LookupError('Record not found.')
    return get_supabase().table('workspace_records').upsert(row).execute().data[0]


def remove(owner: str, kind: str, record_id: str) -> None:
    if uses_memory_store():
        with _MEMORY.lock:
            r = _MEMORY.workspace_records.get(record_id)
            if r and r['owner_handle'] == owner and r['kind'] == kind:
                del _MEMORY.workspace_records[record_id]
        return
    get_supabase().table('workspace_records').delete().eq('owner_handle', owner).eq('kind', kind).eq('id', record_id).execute()


def documents(owner: str) -> list[dict]:
    if uses_memory_store():
        with _MEMORY.lock:
            return [{'fingerprint': fingerprint, 'mime_type': r['mime_type'],
                     'created_at': r.get('created_at'), 'filename': r.get('filename', 'Purchase document')}
                    for (handle, fingerprint), r in _MEMORY.receipt_images.items() if handle == owner]
    return (get_supabase().table('receipt_images').select('fingerprint,mime_type,created_at,filename')
            .eq('owner_handle', owner).order('created_at', desc=True).limit(1000).execute().data or [])


def document_bytes(owner: str, fingerprint: str) -> tuple[bytes, str]:
    if uses_memory_store():
        with _MEMORY.lock:
            row = _MEMORY.receipt_images.get((owner, fingerprint))
            if not row:
                raise LookupError('Document not found.')
            return row['bytes'], row['mime_type']
    rows = (get_supabase().table('receipt_images').select('storage_path,mime_type')
            .eq('owner_handle', owner).eq('fingerprint', fingerprint).limit(1).execute().data or [])
    if not rows:
        raise LookupError('Document not found.')
    row = rows[0]
    return get_supabase().storage.from_('receipt-images').download(row['storage_path']), row['mime_type']


def product(owner: str, product_id: str) -> dict:
    if uses_memory_store():
        with _MEMORY.lock:
            row = _MEMORY.products.get(product_id)
            if not row or row['owner_handle'] != owner:
                raise LookupError('Product not found.')
            return copy.deepcopy(row)
    rows = (get_supabase().table('products').select('*').eq('owner_handle', owner)
            .eq('id', product_id).limit(1).execute().data or [])
    if not rows:
        raise LookupError('Product not found.')
    return rows[0]


def edit_product(owner: str, product_id: str, changes: dict) -> dict:
    product(owner, product_id)
    changes = {**jsonable_encoder(changes), 'updated_at': _now()}
    if uses_memory_store():
        with _MEMORY.lock:
            _MEMORY.products[product_id].update(copy.deepcopy(changes))
            return copy.deepcopy(_MEMORY.products[product_id])
    return (get_supabase().table('products').update(changes).eq('owner_handle', owner)
            .eq('id', product_id).execute().data[0])


def stable_id(owner: str, kind: str, key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'lodestone:{owner}:{kind}:{key}'))
