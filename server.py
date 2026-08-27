import asyncio
import csv
import io
import ipaddress
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr, Field
from starlette.middleware.cors import CORSMiddleware

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

UPLOAD_DIR = ROOT_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = FastAPI()
api_router = APIRouter(prefix="/api")
app.mount("/api/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# ---------------- Email (Emergent managed Resend) ----------------
EMAIL_BASE_URL = "https://integrations.emergentagent.com"
EMAIL_KEY = os.environ["EMERGENT_EMAIL_KEY"]
EMAIL_FROM_NAME = os.environ["EMAIL_FROM_NAME"]
EMAIL_REPLY_TO = os.environ.get("EMAIL_REPLY_TO")
OWNER_EMAIL = os.environ["OWNER_EMAIL"]

_SHORTENERS = ("bit.ly", "tinyurl.com", "t.co", "is.gd", "cutt.ly", "goo.gl", "rebrand.ly")
_CRED_ASK = ("reply with your password", "reply with the code", "send your password", "cvv",
             "send us your password", "enter your password below", "confirm your card number",
             "your full card number", "seed phrase", "recovery phrase", "verify your card",
             "social security number", "confirm your bank details")
_HOSTISH = re.compile(r"\b(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})", re.I)


def _host_ok(host: str) -> bool:
    if not host or "xn--" in host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return not any(host == s or host.endswith("." + s) for s in _SHORTENERS)


def _same_site(shown: str, real: str) -> bool:
    return shown == real or real.endswith("." + shown) or shown.endswith("." + real)


class _EmailScan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.urls, self.anchors = set(), [], []
        self._href, self._text = None, []

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag.lower())
        self.urls += [v for k, v in attrs if k.lower() in ("href", "src") and v]
        if tag.lower() == "a":
            self._href = dict((k.lower(), v) for k, v in attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.anchors.append((self._href, "".join(self._text)))
            self._href, self._text = None, []


def _assert_safe_email(subject: str, html: str) -> None:
    scan = _EmailScan(); scan.feed(html)
    if scan.tags & {"form", "input", "textarea", "select"}:
        raise ValueError("No forms or input fields in email (G2)")
    body = f"{subject}\n{html}".lower()
    for p in _CRED_ASK:
        if p in body:
            raise ValueError(f"Email asks the recipient for credentials: {p!r} (G2)")
    for url in scan.urls:
        low = url.strip().lower()
        if low.startswith(("mailto:", "tel:", "cid:", "#")):
            continue
        if not low.startswith("https://"):
            raise ValueError(f"Email links/assets must be absolute https: {url!r} (G3)")
        host = urlparse(low).hostname or ""
        if not _host_ok(host) or urlparse(low).username is not None:
            raise ValueError(f"Shortened, numeric-host or credential-bearing URL: {url!r} (G3)")
    for href, text in scan.anchors:
        real = urlparse(href.strip().lower()).hostname or ""
        if not real:
            continue
        for m in _HOSTISH.finditer(text):
            if not _same_site(m.group(1).lower(), real):
                raise ValueError(f"Anchor text {m.group(1)!r} ≠ real link host {real!r} (G3)")


async def send_email(*, to: str, subject: str, html: str, reply_to: str | None = None) -> str | None:
    _assert_safe_email(subject, html)
    payload = {"to": [to], "subject": subject, "html": html, "from_name": EMAIL_FROM_NAME}
    if reply_to or EMAIL_REPLY_TO:
        payload["contact_email"] = reply_to or EMAIL_REPLY_TO
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.post(
                f"{EMAIL_BASE_URL}/api/v1/email/send",
                headers={"X-Email-Key": EMAIL_KEY},
                json=payload,
            )
        resp.raise_for_status()
        return resp.json().get("id")
    except httpx.HTTPStatusError as e:
        logger.error(f"Email send failed: {e.response.status_code} {e.response.text}")
    except Exception as e:
        logger.error(f"Email send error: {e}")


def _row(label: str, value: str) -> str:
    return (f'<tr><td style="padding:8px 16px;font-size:12px;color:#6E818C;text-transform:uppercase;'
            f'letter-spacing:1px;vertical-align:top">{escape(label)}</td>'
            f'<td style="padding:8px 16px;font-size:14px;color:#061E29">{escape(value)}</td></tr>')


async def notify_lead(lead: dict) -> None:
    subject = f"New DITEC Enquiry — {lead.get('interest', 'General')}"
    rows = "".join(_row(k, str(v)) for k, v in lead.items() if k not in ("id", "created_at") and v)
    html = ('<table role="presentation" width="100%" style="background:#F5F5F5;padding:24px">'
            '<tr><td style="font-family:Arial,sans-serif">'
            '<h2 style="color:#061E29;margin:0 0 16px">New enquiry via the DITEC website</h2>'
            f'<table role="presentation" style="background:#FFFFFF;border-radius:12px">{rows}</table>'
            '<p style="font-size:12px;color:#888;margin-top:16px">Sent by DITEC website lead capture. '
            'We never ask for passwords or card details by email.</p>'
            '</td></tr></table>')
    try:
        await send_email(to=OWNER_EMAIL, subject=subject, html=html)
    except Exception as e:
        logger.error(f"Lead notification email failed: {e}")


# ---------------- Models ----------------
class LeadCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    company: str | None = Field(default=None, max_length=160)
    phone: str = Field(min_length=6, max_length=20)
    email: EmailStr
    interest: str = Field(default="Student Programmes", max_length=60)


class ContactCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    organisation: str | None = Field(default=None, max_length=160)
    designation: str | None = Field(default=None, max_length=120)
    email: EmailStr
    phone: str = Field(min_length=6, max_length=20)
    city: str | None = Field(default=None, max_length=120)
    interest: str = Field(default="Other", max_length=60)
    message: str | None = Field(default=None, max_length=4000)


class ArticleIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    slug: str | None = Field(default=None, max_length=200)
    category: str = Field(default="Perspectives", max_length=80)
    date: str = Field(default="", max_length=60)
    readTime: str = Field(default="5 min read", max_length=40)
    summary: str = Field(default="", max_length=600)
    question: str = Field(default="", max_length=5000)
    context: str = Field(default="", max_length=5000)
    perspective: str = Field(default="", max_length=5000)
    example: str = Field(default="", max_length=5000)
    meaning: str = Field(default="", max_length=5000)
    cover: str | None = Field(default=None, max_length=600)
    published: bool = True


class BulkArticles(BaseModel):
    articles: list[ArticleIn]


class ContentSet(BaseModel):
    value: str = Field(max_length=8000)


class LinkIn(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=400)
    location: str = Field(default="footer", pattern="^(header|footer)$")


class SettingsIn(BaseModel):
    navCtaLabel: str | None = Field(default=None, max_length=60)
    footerStatement: str | None = Field(default=None, max_length=300)
    contactEmail: str | None = Field(default=None, max_length=120)
    contactPhone: str | None = Field(default=None, max_length=40)
    scholarshipLabel: str | None = Field(default=None, max_length=80)
    scholarshipDate: str | None = Field(default=None, max_length=60)
    gtmId: str | None = Field(default=None, max_length=40)
    customHead: str | None = Field(default=None, max_length=4000)
    colorNavy: str | None = Field(default=None, max_length=20)
    colorAccent: str | None = Field(default=None, max_length=20)
    colorGold: str | None = Field(default=None, max_length=20)
    colorPaper: str | None = Field(default=None, max_length=20)
    fontPreset: str | None = Field(default=None, max_length=40)
    customFontUrl: str | None = Field(default=None, max_length=600)
    fontScale: str | None = Field(default=None, max_length=20)


class StoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(default="", max_length=160)
    quote: str = Field(default="", max_length=1500)
    image: str | None = Field(default=None, max_length=600)
    published: bool = True


class SectionIn(BaseModel):
    page: str = Field(min_length=1, max_length=40)
    tag: str = Field(default="", max_length=120)
    heading: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=3000)
    bullets: list[str] = Field(default_factory=list)
    image: str | None = Field(default=None, max_length=600)
    ctaLabel: str | None = Field(default=None, max_length=80)
    ctaUrl: str | None = Field(default=None, max_length=400)
    theme: str = Field(default="light", pattern="^(light|dark|gold)$")
    placement: str = Field(default="beforeFooter", pattern="^(afterHero|midPage|beforeFooter)$")
    order: int = 0


class ThemeIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    values: dict = Field(default_factory=dict)


def require_admin(x_admin_key: str | None = Header(default=None)):
    if x_admin_key != os.environ.get("ADMIN_KEY"):
        raise HTTPException(status_code=401, detail="Invalid admin key")


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or uuid.uuid4().hex[:8]


def _clean(doc: dict) -> dict:
    doc.pop("_id", None)
    return doc


# ---------------- Public ----------------
@api_router.get("/")
async def root():
    return {"message": "DITEC API"}


@api_router.get("/health")
async def health():
    return {"status": "ok"}


@api_router.post("/leads", status_code=201)
async def create_lead(payload: LeadCreate):
    doc = payload.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["created_at"] = datetime.now(timezone.utc).isoformat()
    await db.leads.insert_one(doc)
    asyncio.create_task(notify_lead(doc))
    return {"status": "success", "id": doc["id"]}


@api_router.post("/contact", status_code=201)
async def create_inquiry(payload: ContactCreate):
    doc = payload.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["created_at"] = datetime.now(timezone.utc).isoformat()
    await db.inquiries.insert_one(doc)
    asyncio.create_task(notify_lead(doc))
    return {"status": "success", "id": doc["id"]}


@api_router.get("/articles")
async def public_articles():
    return await db.articles.find({"published": {"$ne": False}}, {"_id": 0}).sort("created_at", 1).to_list(500)


@api_router.get("/articles/{slug}")
async def public_article(slug: str):
    doc = await db.articles.find_one({"slug": slug, "published": {"$ne": False}}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    return doc


@api_router.get("/content/overrides")
async def public_overrides():
    docs = await db.content_overrides.find({}, {"_id": 0}).to_list(2000)
    return {d["key"]: d["value"] for d in docs}


@api_router.get("/links")
async def public_links():
    return await db.links.find({}, {"_id": 0}).sort("created_at", 1).to_list(200)


@api_router.get("/settings")
async def public_settings():
    doc = await db.settings.find_one({"id": "site"}, {"_id": 0})
    return doc or {}


# ---------------- Admin: leads / inquiries ----------------
@api_router.get("/admin/leads", dependencies=[Depends(require_admin)])
async def list_leads():
    return await db.leads.find({}, {"_id": 0}).sort("created_at", -1).to_list(5000)


@api_router.get("/admin/inquiries", dependencies=[Depends(require_admin)])
async def list_inquiries():
    return await db.inquiries.find({}, {"_id": 0}).sort("created_at", -1).to_list(5000)


async def _export(collection, fields):
    rows = await db[collection].find({}, {"_id": 0}).sort("created_at", -1).to_list(10000)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=ditec-{collection}.csv"})


@api_router.get("/admin/leads/export.csv")
async def export_leads(key: str = Query(default=None)):
    if key != os.environ.get("ADMIN_KEY"):
        raise HTTPException(status_code=401, detail="Invalid admin key")
    return await _export("leads", ["created_at", "name", "email", "phone", "company", "interest"])


@api_router.get("/admin/inquiries/export.csv")
async def export_inquiries(key: str = Query(default=None)):
    if key != os.environ.get("ADMIN_KEY"):
        raise HTTPException(status_code=401, detail="Invalid admin key")
    return await _export("inquiries", ["created_at", "name", "email", "phone", "organisation", "designation", "city", "interest", "message"])


@api_router.get("/admin/stats", dependencies=[Depends(require_admin)])
async def admin_stats():
    return {
        "leads": await db.leads.count_documents({}),
        "inquiries": await db.inquiries.count_documents({}),
        "articles": await db.articles.count_documents({}),
        "media": await db.media.count_documents({}),
        "links": await db.links.count_documents({}),
        "overrides": await db.content_overrides.count_documents({}),
    }


# ---------------- Admin: articles ----------------
@api_router.get("/admin/articles", dependencies=[Depends(require_admin)])
async def admin_articles():
    return await db.articles.find({}, {"_id": 0}).sort("created_at", 1).to_list(500)


@api_router.post("/admin/articles", status_code=201, dependencies=[Depends(require_admin)])
async def admin_create_article(payload: ArticleIn):
    doc = payload.model_dump()
    base = _slugify(doc.pop("slug", None) or doc["title"])
    slug = base
    i = 2
    while await db.articles.find_one({"slug": slug}):
        slug = f"{base}-{i}"
        i += 1
    doc["slug"] = slug
    doc["id"] = str(uuid.uuid4())
    doc["created_at"] = datetime.now(timezone.utc).isoformat()
    await db.articles.insert_one(doc)
    return _clean(doc)


@api_router.post("/admin/articles/bulk", status_code=201, dependencies=[Depends(require_admin)])
async def admin_bulk_articles(payload: BulkArticles):
    inserted = 0
    for a in payload.articles:
        doc = a.model_dump()
        slug = _slugify(doc.pop("slug", None) or doc["title"])
        if await db.articles.find_one({"slug": slug}):
            continue
        doc["slug"] = slug
        doc["id"] = str(uuid.uuid4())
        doc["created_at"] = datetime.now(timezone.utc).isoformat()
        await db.articles.insert_one(doc)
        inserted += 1
    return {"inserted": inserted}


@api_router.put("/admin/articles/{article_id}", dependencies=[Depends(require_admin)])
async def admin_update_article(article_id: str, payload: ArticleIn):
    doc = payload.model_dump()
    doc.pop("slug", None)
    res = await db.articles.update_one({"id": article_id}, {"$set": doc})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return _clean(await db.articles.find_one({"id": article_id}, {"_id": 0}))


@api_router.delete("/admin/articles/{article_id}", dependencies=[Depends(require_admin)])
async def admin_delete_article(article_id: str):
    await db.articles.delete_one({"id": article_id})
    return {"status": "deleted"}


# ---------------- Admin: content overrides ----------------
@api_router.get("/admin/content", dependencies=[Depends(require_admin)])
async def admin_content():
    return await db.content_overrides.find({}, {"_id": 0}).to_list(2000)


@api_router.put("/admin/content/{key}", dependencies=[Depends(require_admin)])
async def admin_set_content(key: str, payload: ContentSet):
    await db.content_overrides.update_one(
        {"key": key},
        {"$set": {"key": key, "value": payload.value, "updated_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )
    return {"status": "saved", "key": key}


@api_router.delete("/admin/content/{key}", dependencies=[Depends(require_admin)])
async def admin_reset_content(key: str):
    await db.content_overrides.delete_one({"key": key})
    return {"status": "reset", "key": key}


# ---------------- Admin: links ----------------
@api_router.post("/admin/links", status_code=201, dependencies=[Depends(require_admin)])
async def admin_create_link(payload: LinkIn):
    doc = payload.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["created_at"] = datetime.now(timezone.utc).isoformat()
    await db.links.insert_one(doc)
    return _clean(doc)


@api_router.delete("/admin/links/{link_id}", dependencies=[Depends(require_admin)])
async def admin_delete_link(link_id: str):
    await db.links.delete_one({"id": link_id})
    return {"status": "deleted"}


# ---------------- Admin: settings ----------------
@api_router.put("/admin/settings", dependencies=[Depends(require_admin)])
async def admin_set_settings(payload: SettingsIn):
    doc = {k: v for k, v in payload.model_dump().items() if v is not None}
    doc["id"] = "site"
    await db.settings.update_one({"id": "site"}, {"$set": doc}, upsert=True)
    return _clean(await db.settings.find_one({"id": "site"}, {"_id": 0}))


# ---------------- Admin: media ----------------
@api_router.post("/admin/media", status_code=201, dependencies=[Depends(require_admin)])
async def admin_upload(file: UploadFile = File(...)):
    ext = Path(file.filename or "file").suffix.lower()[:10]
    fname = f"{uuid.uuid4().hex}{ext}"
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (25MB max)")
    (UPLOAD_DIR / fname).write_bytes(data)
    ct = (file.content_type or "")
    if ct.startswith("image/") or ext in (".webp", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".avif"):
        kind = "image"
    elif ct.startswith("video/") or ext in (".mp4", ".webm", ".mov"):
        kind = "video"
    elif ext in (".ttf", ".otf", ".woff", ".woff2"):
        kind = "font"
    else:
        kind = "doc"
    doc = {
        "id": str(uuid.uuid4()),
        "filename": fname,
        "original": file.filename,
        "url": f"/api/uploads/{fname}",
        "kind": kind,
        "size": len(data),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.media.insert_one(doc)
    return _clean(doc)


@api_router.get("/admin/media", dependencies=[Depends(require_admin)])
async def admin_media():
    return await db.media.find({}, {"_id": 0}).sort("created_at", -1).to_list(1000)


@api_router.delete("/admin/media/{media_id}", dependencies=[Depends(require_admin)])
async def admin_delete_media(media_id: str):
    doc = await db.media.find_one({"id": media_id})
    if doc:
        try:
            (UPLOAD_DIR / doc["filename"]).unlink(missing_ok=True)
        except Exception:
            pass
        await db.media.delete_one({"id": media_id})
    return {"status": "deleted"}


# ---------------- Stories ----------------
@api_router.get("/stories")
async def public_stories():
    return await db.stories.find({"published": {"$ne": False}}, {"_id": 0}).sort("created_at", 1).to_list(200)


@api_router.get("/admin/stories", dependencies=[Depends(require_admin)])
async def admin_stories():
    return await db.stories.find({}, {"_id": 0}).sort("created_at", 1).to_list(500)


@api_router.post("/admin/stories", status_code=201, dependencies=[Depends(require_admin)])
async def admin_create_story(payload: StoryIn):
    doc = payload.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["created_at"] = datetime.now(timezone.utc).isoformat()
    await db.stories.insert_one(doc)
    return _clean(doc)


@api_router.put("/admin/stories/{story_id}", dependencies=[Depends(require_admin)])
async def admin_update_story(story_id: str, payload: StoryIn):
    res = await db.stories.update_one({"id": story_id}, {"$set": payload.model_dump()})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return _clean(await db.stories.find_one({"id": story_id}, {"_id": 0}))


@api_router.delete("/admin/stories/{story_id}", dependencies=[Depends(require_admin)])
async def admin_delete_story(story_id: str):
    await db.stories.delete_one({"id": story_id})
    return {"status": "deleted"}


# ---------------- Custom sections ----------------
@api_router.get("/sections")
async def public_sections(page: str = Query(...)):
    return await db.sections.find({"page": page}, {"_id": 0}).sort("order", 1).to_list(100)


@api_router.get("/admin/sections", dependencies=[Depends(require_admin)])
async def admin_sections():
    return await db.sections.find({}, {"_id": 0}).sort("created_at", 1).to_list(500)


@api_router.post("/admin/sections", status_code=201, dependencies=[Depends(require_admin)])
async def admin_create_section(payload: SectionIn):
    doc = payload.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["created_at"] = datetime.now(timezone.utc).isoformat()
    await db.sections.insert_one(doc)
    return _clean(doc)


class ReorderIn(BaseModel):
    ids: list[str]


@api_router.put("/admin/sections/reorder", dependencies=[Depends(require_admin)])
async def admin_reorder_sections(payload: ReorderIn):
    for i, sid in enumerate(payload.ids):
        await db.sections.update_one({"id": sid}, {"$set": {"order": i}})
    return {"status": "reordered", "count": len(payload.ids)}


@api_router.put("/admin/sections/{section_id}", dependencies=[Depends(require_admin)])
async def admin_update_section(section_id: str, payload: SectionIn):
    res = await db.sections.update_one({"id": section_id}, {"$set": payload.model_dump()})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return _clean(await db.sections.find_one({"id": section_id}, {"_id": 0}))


@api_router.delete("/admin/sections/{section_id}", dependencies=[Depends(require_admin)])
async def admin_delete_section(section_id: str):
    await db.sections.delete_one({"id": section_id})
    return {"status": "deleted"}


# ---------------- Theme presets ----------------
@api_router.get("/admin/themes", dependencies=[Depends(require_admin)])
async def admin_themes():
    return await db.themes.find({}, {"_id": 0}).sort("created_at", 1).to_list(100)


@api_router.post("/admin/themes", status_code=201, dependencies=[Depends(require_admin)])
async def admin_create_theme(payload: ThemeIn):
    doc = payload.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["created_at"] = datetime.now(timezone.utc).isoformat()
    await db.themes.insert_one(doc)
    return _clean(doc)


@api_router.delete("/admin/themes/{theme_id}", dependencies=[Depends(require_admin)])
async def admin_delete_theme(theme_id: str):
    await db.themes.delete_one({"id": theme_id})
    return {"status": "deleted"}


# ---------------- Analytics ----------------
@api_router.get("/admin/analytics", dependencies=[Depends(require_admin)])
async def admin_analytics():
    from collections import Counter
    leads = await db.leads.find({}, {"_id": 0, "created_at": 1, "interest": 1}).to_list(10000)
    inquiries = await db.inquiries.find({}, {"_id": 0, "created_at": 1, "interest": 1}).to_list(10000)
    counts = Counter((l.get("created_at") or "")[:10] for l in leads)
    today = datetime.now(timezone.utc).date()
    days = [{"date": (today - timedelta(days=i)).isoformat(), "count": counts.get((today - timedelta(days=i)).isoformat(), 0)} for i in range(13, -1, -1)]
    interests = Counter(l.get("interest") or "General" for l in leads)
    qinterests = Counter(q.get("interest") or "Other" for q in inquiries)
    return {
        "leadsByDay": days,
        "leadsByInterest": [{"label": k, "count": v} for k, v in interests.most_common(8)],
        "inquiriesByInterest": [{"label": k, "count": v} for k, v in qinterests.most_common(8)],
    }


# ---------------- Popups ----------------
class PopupIn(BaseModel):
    type: str = Field(default="notification", pattern="^(form|image|notification)$")
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(default="", max_length=1200)
    image: str | None = Field(default=None, max_length=600)
    ctaLabel: str | None = Field(default=None, max_length=80)
    ctaUrl: str | None = Field(default=None, max_length=400)
    delaySeconds: int = Field(default=3, ge=0, le=120)
    active: bool = True


@api_router.get("/popups")
async def public_popups():
    return await db.popups.find({"active": True}, {"_id": 0}).sort("created_at", 1).to_list(50)


@api_router.get("/admin/popups", dependencies=[Depends(require_admin)])
async def admin_popups():
    return await db.popups.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)


@api_router.post("/admin/popups", status_code=201, dependencies=[Depends(require_admin)])
async def admin_create_popup(payload: PopupIn):
    doc = payload.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["created_at"] = datetime.now(timezone.utc).isoformat()
    await db.popups.insert_one(doc)
    return _clean(doc)


@api_router.put("/admin/popups/{popup_id}", dependencies=[Depends(require_admin)])
async def admin_update_popup(popup_id: str, payload: PopupIn):
    res = await db.popups.update_one({"id": popup_id}, {"$set": payload.model_dump()})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return _clean(await db.popups.find_one({"id": popup_id}, {"_id": 0}))


@api_router.delete("/admin/popups/{popup_id}", dependencies=[Depends(require_admin)])
async def admin_delete_popup(popup_id: str):
    await db.popups.delete_one({"id": popup_id})
    return {"status": "deleted"}


# ---------------- Weekly reports ----------------
def _check_key(key):
    if key != os.environ.get("ADMIN_KEY"):
        raise HTTPException(status_code=401, detail="Invalid admin key")


async def _weekly_data():
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    leads = await db.leads.find({"created_at": {"$gte": since}}, {"_id": 0}).sort("created_at", -1).to_list(5000)
    inquiries = await db.inquiries.find({"created_at": {"$gte": since}}, {"_id": 0}).sort("created_at", -1).to_list(5000)
    return leads, inquiries


@api_router.get("/admin/reports/weekly.csv")
async def weekly_csv(key: str = Query(default=None)):
    _check_key(key)
    leads, inquiries = await _weekly_data()
    buf = io.StringIO()
    fields = ["type", "created_at", "name", "email", "phone", "organisation", "interest", "message"]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for r in leads:
        writer.writerow({**r, "type": "lead", "organisation": r.get("company") or ""})
    for r in inquiries:
        writer.writerow({**r, "type": "enquiry"})
    stamp = datetime.now(timezone.utc).date().isoformat()
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=ditec-weekly-{stamp}.csv"})


@api_router.get("/admin/reports/weekly.pdf")
async def weekly_pdf(key: str = Query(default=None)):
    _check_key(key)
    from collections import Counter
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

    leads, inquiries = await _weekly_data()
    navy = colors.HexColor("#061E29")
    electric = colors.HexColor("#1B2CC1")
    gold = colors.HexColor("#FCE59A")
    muted = colors.HexColor("#6E818C")

    styles = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=16 * mm)
    story = []

    story.append(Paragraph("DITEC — Weekly Lead Report", styles["Title"]))
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=7)).date().isoformat()
    story.append(Paragraph(f"Period: {week_start} to {now.date().isoformat()} · Generated {now.strftime('%d %b %Y, %H:%M UTC')}", styles["Normal"]))
    story.append(Spacer(1, 8 * mm))

    interests = Counter(l.get("interest") or "General" for l in leads)
    summary = Table(
        [["Total Leads", "Total Enquiries", "Top Interest"],
         [str(len(leads)), str(len(inquiries)), interests.most_common(1)[0][0] if interests else "—"]],
        colWidths=[58 * mm, 58 * mm, 58 * mm],
    )
    summary.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), navy), ("TEXTCOLOR", (0, 0), (-1, 0), gold),
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#F5F5F5")), ("TEXTCOLOR", (0, 1), (-1, 1), navy),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"), ("FONTSIZE", (0, 1), (-1, 1), 16),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(summary)
    story.append(Spacer(1, 8 * mm))

    if interests:
        story.append(Paragraph("Leads by Interest", styles["Heading2"]))
        rows = [["Interest", "Leads"]] + [[k, str(v)] for k, v in interests.most_common(10)]
        t = Table(rows, colWidths=[130 * mm, 44 * mm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), electric), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F5F5")]),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#DCE3E8")),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(t)
        story.append(Spacer(1, 8 * mm))

    story.append(Paragraph("Leads & Enquiries (last 7 days)", styles["Heading2"]))
    rows = [["Date", "Type", "Name", "Contact", "Interest"]]
    for r in leads:
        rows.append([(r.get("created_at") or "")[:10], "Lead", r.get("name", ""), r.get("email", ""), r.get("interest", "")])
    for r in inquiries:
        rows.append([(r.get("created_at") or "")[:10], "Enquiry", r.get("name", ""), r.get("email", ""), r.get("interest", "")])
    if len(rows) == 1:
        rows.append(["—", "—", "No new leads or enquiries this week", "", ""])
    t = Table(rows, colWidths=[24 * mm, 20 * mm, 42 * mm, 52 * mm, 36 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), navy), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F5F5")]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#DCE3E8")), ("TEXTCOLOR", (0, 1), (-1, -1), muted),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(t)
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph("Generated by the DITEC Admin Console · ditec.org@gmail.com", styles["Normal"]))

    doc.build(story)
    buf.seek(0)
    stamp = now.date().isoformat()
    return Response(content=buf.read(), media_type="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename=ditec-weekly-report-{stamp}.pdf"})


# ---------------- Sitemap ----------------
STATIC_PATHS = [
    "/", "/about", "/students", "/programmes", "/programmes/flagship", "/programmes/technical",
    "/programmes/business-creator", "/scholarship", "/enterprises", "/enterprises/csr",
    "/enterprises/talent", "/enterprises/marketing-cell", "/enterprises/research",
    "/enterprises/membership", "/institutions", "/press", "/contact",
    "/legal/privacy", "/legal/terms", "/legal/cookies", "/legal/ethics",
]


@api_router.get("/sitemap.xml")
async def sitemap():
    base = os.environ.get("SITE_URL", "").rstrip("/")
    urls = [(f"{base}{p}", None, "0.8" if p == "/" else "0.7") for p in STATIC_PATHS]
    async for a in db.articles.find({"published": {"$ne": False}}, {"slug": 1, "created_at": 1}):
        lastmod = (a.get("created_at") or "")[:10] or None
        urls.append((f"{base}/press/{a['slug']}", lastmod, "0.6"))
    body = "".join(
        f"<url><loc>{loc}</loc>{f'<lastmod>{lm}</lastmod>' if lm else ''}<priority>{pri}</priority></url>"
        for loc, lm, pri in urls
    )
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + body + "</urlset>")
    return Response(content=xml, media_type="application/xml")


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
