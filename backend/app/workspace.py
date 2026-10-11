from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from .auth import owner
from . import storage as db
from . import workspace_data as data
from .settings import get_settings
from .extraction import extract_products
from .receipt_import import email_text, extract_text
from .prices import amazon_asin_from_url, fetch_amazon_price

router = APIRouter(prefix='/api/workspace', tags=['Workspace'])


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class ProductInput(Input):
    product_name: str = Field(min_length=1, max_length=160)
    brand: str = Field(default='', max_length=100)
    merchant: str = Field(default='', max_length=100)
    purchase_date: date | None = None
    amount_paid: float | None = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    payment_card: str = Field(default='', max_length=80)
    order_number: str = Field(default='', max_length=100)
    identifiers: dict[str, str] = Field(default_factory=dict, max_length=20)
    source_fingerprint: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    item_index: int = Field(default=1, ge=1, le=100)

    @field_validator('identifiers')
    @classmethod
    def identifiers_bounded(cls, values):
        if any(len(k) > 40 or len(v) > 200 for k, v in values.items()):
            raise ValueError('Product identifiers are too long.')
        return values

    def product_fields(self):
        identifiers = {**self.identifiers, 'merchant': self.merchant,
                       'purchase_date': self.purchase_date.isoformat() if self.purchase_date else None,
                       'amount_paid': f'{self.amount_paid:.2f}' if self.amount_paid is not None else None,
                       'payment_card': self.payment_card, 'order_number': self.order_number}
        return {'product_name': self.product_name, 'brand': self.brand or None,
                'identifiers': identifiers,
                'field_provenance': {k: {'source': 'user', 'user_confirmed': True, 'confirmed_at': db._now()}
                                     for k in ['brand', 'product_name', *identifiers]}}


class CaseInput(Input):
    product_id: str
    case_type: Literal['return', 'warranty', 'price_adjustment', 'rebate', 'card_benefit']
    due_date: date


class CaseStatus(Input):
    status: Literal['open', 'completed', 'not_applicable']


class PriceInput(Input):
    product_id: str
    url: str = Field(max_length=500)
    target_price: float = Field(gt=0, le=100_000, allow_inf_nan=False)


class BenefitInput(Input):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal['rebate', 'card_benefit']
    merchant: str = Field(min_length=1, max_length=100)
    payment_card: str = Field(default='', max_length=80)
    starts_on: date
    ends_on: date
    amount_cap: float = Field(gt=0, le=100_000, allow_inf_nan=False)
    terms_url: str = Field(default='', max_length=1000)
    terms: str = Field(min_length=1, max_length=4000)

    @model_validator(mode='after')
    def validate_terms(self):
        if self.ends_on < self.starts_on:
            raise ValueError('Offer end date must be on or after its start date.')
        check_url(self.terms_url)
        if self.kind == 'card_benefit' and not self.payment_card:
            raise ValueError('Enter the card label required by this benefit (never its full number).')
        return self


class ClaimInput(Input):
    case_id: str
    merchant: str = Field(min_length=1, max_length=100)
    issue: str = Field(min_length=1, max_length=4000)
    portal_url: str = Field(default='', max_length=1000)
    status: Literal['draft', 'submitted', 'won', 'denied'] = 'draft'
    recovered_amount: float = Field(default=0, ge=0, le=1_000_000, allow_inf_nan=False)

    @model_validator(mode='after')
    def validate_claim(self):
        check_url(self.portal_url)
        if self.recovered_amount and self.status != 'won':
            raise ValueError('Record recovered money only after a claim succeeds.')
        return self


class TextReceipt(Input):
    text: str = Field(min_length=10, max_length=100_000)


def check_url(url: str):
    parsed = urlparse(url)
    if url and (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password):
        raise ValueError('Use an HTTPS merchant or terms URL.')


def owned_case(handle: str, case_id: str) -> dict:
    case = next((row for row in db.list_owner_recovery_cases(owner_handle=handle) if row['id'] == case_id), None)
    if not case:
        raise HTTPException(404, 'Action not found.')
    return case


def match_benefits(products: list[dict], benefits: list[dict]) -> list[dict]:
    matches = []
    normalize = lambda value: ' '.join(str(value or '').casefold().split())
    for record in benefits:
        benefit = record['payload']
        for product in products:
            fields = product.get('identifiers') or {}
            if product.get('status') != 'owned' or normalize(fields.get('merchant')) != normalize(benefit['merchant']):
                continue
            missing = []
            bought = fields.get('purchase_date')
            try:
                bought_date = date.fromisoformat(bought or '')
            except ValueError:
                missing.append('purchase date')
                bought_date = None
            if bought_date and not (date.fromisoformat(benefit['starts_on']) <= bought_date <= date.fromisoformat(benefit['ends_on'])):
                continue
            if benefit.get('payment_card'):
                if not fields.get('payment_card'):
                    missing.append('payment card label')
                elif normalize(fields['payment_card']) != normalize(benefit['payment_card']):
                    continue
            matches.append({'benefit_id': record['id'], 'product_id': product['id'],
                            'name': benefit['name'], 'amount_cap': benefit['amount_cap'],
                            'status': 'needs_details' if missing else 'potential_match',
                            'missing': missing, 'reason': 'Merchant, purchase window, and recorded card are compared against your saved terms. Other restrictions require review.'})
    return matches


@router.get('')
def snapshot(handle: str = Depends(owner)):
    snapshot = db.get_dashboard_snapshot(owner_handle=handle)
    benefits = data.records(handle, 'benefit')
    return {**snapshot, 'documents': data.documents(handle), 'benefits': benefits,
            'claims': data.records(handle, 'claim'), 'recall_checks': data.records(handle, 'recall'),
            'benefit_matches': match_benefits(snapshot['products'], benefits),
            'demo': get_settings().demo_mode, 'generated_at': db._now()}


@router.post('/products')
def add_product(body: ProductInput, handle: str = Depends(owner)):
    if body.source_fingerprint and not any(d['fingerprint'] == body.source_fingerprint for d in data.documents(handle)):
        raise HTTPException(404, 'Receipt document not found.')
    return db.save_confirmed_product(owner_handle=handle, **body.product_fields(),
        source_type='receipt' if body.source_fingerprint else 'manual',
        receipt_fingerprint=body.source_fingerprint,
        receipt_item_index=body.item_index if body.source_fingerprint else None)


@router.put('/products/{product_id}')
def edit_product(product_id: str, body: ProductInput, handle: str = Depends(owner)):
    return data.edit_product(handle, product_id, body.product_fields())


@router.post('/products/{product_id}/archive')
def archive_product(product_id: str, handle: str = Depends(owner)):
    data.product(handle, product_id)
    db.update_product_status(product_id=product_id, owner_handle=handle, status='no_longer_owned')
    data.edit_product(handle, product_id, {'monitoring_enabled': False})
    for watch in db.list_owner_price_watches(owner_handle=handle):
        if watch['product_id'] == product_id:
            db.pause_price_watch(owner_handle=handle, watch_id=watch['id'])
    for case in db.list_owner_recovery_cases(owner_handle=handle):
        if case['product_id'] == product_id and case['status'] == 'open':
            db.set_recovery_case_status(owner_handle=handle, case_id=case['id'], status='not_applicable')
    return {'archived': True}


async def retain_and_extract(handle: str, raw: bytes, mime: str, filename: str):
    fingerprint = hashlib.sha256(raw).hexdigest()
    await run_in_threadpool(db.save_receipt_image, owner_handle=handle, fingerprint=fingerprint,
                            image_bytes=raw, mime_type=mime, filename=filename[:200])
    # Retaining the original does not depend on extraction succeeding.
    try:
        if mime == 'message/rfc822':
            text = email_text(raw)
            extraction = await run_in_threadpool(extract_text, text)
        else:
            extraction = await run_in_threadpool(extract_products, raw, mime, 'receipt')
        return {'fingerprint': fingerprint, 'items': [x.model_dump() for x in extraction.items[:100]],
                'note': extraction.text_needing_confirmation, 'retained': True}
    except Exception:
        return {'fingerprint': fingerprint, 'items': [], 'retained': True,
                'note': 'Your original is saved. Automatic extraction was unavailable or could not read this receipt; enter the details manually.'}


@router.post('/documents')
async def upload_document(file: UploadFile = File(...), handle: str = Depends(owner)):
    raw = await file.read(10 * 1024 * 1024 + 1)
    if not raw or len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, 'Upload a nonempty document of at most 10 MB.')
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):
        mime = 'image/png'
    elif raw.startswith(b'\xff\xd8\xff'):
        mime = 'image/jpeg'
    elif raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        mime = 'image/webp'
    elif raw.startswith(b'%PDF-'):
        mime = 'application/pdf'
    elif (file.filename or '').lower().endswith('.eml'):
        try:
            email_text(raw)
        except (ValueError, UnicodeError) as error:
            raise HTTPException(422, str(error)) from error
        mime = 'message/rfc822'
    else:
        raise HTTPException(415, 'Use a JPEG, PNG, WebP, PDF, or exported .eml receipt email.')
    return await retain_and_extract(handle, raw, mime, file.filename or 'Receipt')


@router.post('/email-text')
async def import_email_text(body: TextReceipt, handle: str = Depends(owner)):
    from email.message import EmailMessage
    email = EmailMessage()
    email['Subject'] = 'Receipt text supplied by owner'
    email.set_content(body.text)
    return await retain_and_extract(handle, email.as_bytes(), 'message/rfc822', 'receipt-email.eml')


@router.get('/documents/{fingerprint}')
def download_document(fingerprint: str, handle: str = Depends(owner)):
    raw, mime = data.document_bytes(handle, fingerprint)
    ext = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp',
           'application/pdf': 'pdf', 'message/rfc822': 'eml'}[mime]
    return Response(raw, media_type=mime, headers={
        'Content-Disposition': f'attachment; filename="lodestone-receipt.{ext}"',
        'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})


@router.post('/actions')
def add_action(body: CaseInput, handle: str = Depends(owner)):
    data.product(handle, body.product_id)
    # A repeated click should not create a second identical deadline.
    for existing in db.list_owner_recovery_cases(owner_handle=handle):
        if existing['product_id'] == body.product_id and existing['case_type'] == body.case_type and str(existing['due_date']) == str(body.due_date) and existing['status'] == 'open':
            return existing
    return db.create_recovery_case(owner_handle=handle, product_id=body.product_id,
                                   case_type=body.case_type, due_date=body.due_date.isoformat())


@router.patch('/actions/{case_id}')
def update_action(case_id: str, body: CaseStatus, handle: str = Depends(owner)):
    owned_case(handle, case_id)
    db.set_recovery_case_status(owner_handle=handle, case_id=case_id, status=body.status)
    return {'updated': True}


@router.post('/prices')
def add_price(body: PriceInput, handle: str = Depends(owner)):
    data.product(handle, body.product_id)
    try:
        asin = amazon_asin_from_url(body.url)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    for watch in db.list_owner_price_watches(owner_handle=handle):
        if watch['product_id'] == body.product_id and watch['asin'] == asin and watch['is_active']:
            raise HTTPException(409, 'This product already has an active watch. Pause it before adding a new target.')
    return db.create_price_watch(owner_handle=handle, product_id=body.product_id, asin=asin,
                                 product_url=f'https://www.amazon.com/dp/{asin}', target_price=body.target_price)


@router.post('/prices/{watch_id}/check')
async def check_price(watch_id: str, handle: str = Depends(owner)):
    watch = next((r for r in db.list_owner_price_watches(owner_handle=handle) if r['id'] == watch_id), None)
    if not watch:
        raise HTTPException(404, 'Price watch not found.')
    if not watch['is_active']:
        raise HTTPException(409, 'This watch is paused.')
    try:
        result = await run_in_threadpool(fetch_amazon_price, watch['asin'])
    except Exception as error:
        db.record_price_check(owner_handle=handle, watch_id=watch_id, current_price=None,
                              error='No current provider price was available. Try again later.')
        raise HTTPException(503, 'Price checking is unavailable. Confirm the Keepa configuration or try again later.') from error
    return db.record_price_check(owner_handle=handle, watch_id=watch_id,
                                 current_price=result['price'], provider_title=result.get('title'))


@router.post('/prices/{watch_id}/pause')
def pause_price(watch_id: str, handle: str = Depends(owner)):
    if not db.pause_price_watch(owner_handle=handle, watch_id=watch_id):
        raise HTTPException(404, 'Price watch not found.')
    return {'paused': True}


@router.post('/products/{product_id}/recall')
async def check_recall(product_id: str, handle: str = Depends(owner)):
    from .main import search_cpsc_recalls
    product = data.product(handle, product_id)
    identifiers = product.get('identifiers') or {}
    result = await search_cpsc_recalls(q=f"{product.get('brand') or ''} {product['product_name']}".strip()[:100],
                                       model=identifiers.get('model_number'), date_code=identifiers.get('date_code'))
    return data.put(handle, 'recall', {'product_id': product_id, 'result': result},
                    data.stable_id(handle, 'recall', product_id))


@router.post('/benefits')
def add_benefit(body: BenefitInput, handle: str = Depends(owner)):
    return data.put(handle, 'benefit', body.model_dump(mode='json'))


@router.delete('/benefits/{benefit_id}')
def delete_benefit(benefit_id: str, handle: str = Depends(owner)):
    data.remove(handle, 'benefit', benefit_id)
    return {'deleted': True}


@router.put('/claims')
def save_claim(body: ClaimInput, handle: str = Depends(owner)):
    case = owned_case(handle, body.case_id)
    row = data.put(handle, 'claim', {**body.model_dump(mode='json'), 'product_id': case['product_id']},
                   data.stable_id(handle, 'claim', body.case_id))
    db.set_recovery_case_status(owner_handle=handle, case_id=body.case_id,
        status={'won': 'completed', 'denied': 'not_applicable'}.get(body.status, 'open'))
    return row


@router.get('/claims/{case_id}/packet')
def claim_packet(case_id: str, handle: str = Depends(owner)):
    from .conversation import _claim_packet
    case = owned_case(handle, case_id)
    product = data.product(handle, case['product_id'])
    claim = next((r['payload'] for r in data.records(handle, 'claim') if r['payload']['case_id'] == case_id), None)
    merchant = claim['merchant'] if claim else (product.get('identifiers') or {}).get('merchant') or 'Merchant'
    text = _claim_packet(case, product, merchant)
    if claim:
        text += f"\n\nYour issue/request: {claim['issue']}\nSubmission channel: {claim['portal_url'] or '[confirm with merchant]'}\nRecorded status: {claim['status']}\n"
    details = product.get('identifiers') or {}
    text += f"\nSaved order number: {details.get('order_number') or '[unknown]'}\nSaved amount paid (USD): {details.get('amount_paid') or '[unknown]'}\n"
    if product.get('receipt_fingerprint'):
        text += '\nA retained receipt is available in your Lodestone Documents tab. Download it and attach it when submitting.\n'
    text += '\nSubmission checklist: verify the merchant channel and terms; complete missing details; attach the receipt; save the confirmation number; update the claim status in Lodestone. This packet has not been sent.\n'
    return Response(text, media_type='text/plain', headers={'Content-Disposition': 'attachment; filename="lodestone-claim.txt"', 'Cache-Control': 'no-store'})


@router.get('/export')
def export_workspace(handle: str = Depends(owner)):
    return snapshot(handle)


@router.delete('')
def delete_workspace(handle: str = Depends(owner)):
    return {'deleted_products': db.delete_owner_products(owner_handle=handle)}


@router.post('/demo-seed')
def demo_seed(handle: str = Depends(owner)):
    if not get_settings().demo_mode:
        raise HTTPException(404, 'Not found.')
    from pathlib import Path
    from datetime import timedelta
    raw = (Path(__file__).resolve().parents[2] / 'fixtures/demo_mycharge_receipt.jpg').read_bytes()
    fingerprint = hashlib.sha256(raw).hexdigest()
    db.save_receipt_image(owner_handle=handle, fingerprint=fingerprint, image_bytes=raw,
                          mime_type='image/jpeg', filename='Historical demo receipt.jpg')
    product = db.save_confirmed_product(owner_handle=handle, brand='myCharge', product_name='POWER HUB (historical demo)',
        identifiers={'model_number': 'AO10FK-A', 'date_code': '1521', 'merchant': 'Demo Store',
                     'purchase_date': date.today().isoformat(), 'amount_paid': '49.99', 'payment_card': 'Demo card'},
        source_type='receipt', receipt_fingerprint=fingerprint, receipt_item_index=1)
    add_action(CaseInput(product_id=product['id'], case_type='return', due_date=date.today()+timedelta(days=7)), handle)
    db.save_match_evidence(product_id=product['id'], notice=__import__('app.matcher', fromlist=['NOTICE']).NOTICE,
                          decision='affected', evidence={'historical_fixture': True, 'demo': True})
    return {'seeded': True}
