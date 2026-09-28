"""UET EE Notes - Streamlit edition.

Students: browse subjects, read PDFs directly in the page, download them.
Admin (password protected): add/rename/reorder/delete subjects, upload/edit/
replace/delete PDFs, backup and restore everything.

Run locally:   streamlit run app.py
Configuration: see README.md (ADMIN_PASSWORD is required for the admin panel).
"""
from __future__ import annotations

import hmac
import html
import io
import json
import os
import re
import secrets
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

import pymupdf as fitz
import requests
import streamlit as st

APP_NAME = "UET EE Notes"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
PATH_RE = re.compile(r"^(library\.json|files/[A-Za-z0-9_-]{1,60}\.pdf)$")
MAX_PAGES = 2000
MAX_RESTORE_MB = 1000


# --------------------------------------------------------------------------
# Configuration (Streamlit secrets first, then environment variables)
# --------------------------------------------------------------------------
def cfg(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name)
        if value not in (None, ""):
            return str(value)
    except Exception:
        pass
    return os.environ.get(name, default)


def _int_cfg(name: str, default: int) -> int:
    try:
        return max(1, int(cfg(name, str(default))))
    except ValueError:
        return default


MAX_UPLOAD_MB = _int_cfg("MAX_UPLOAD_MB", 50)  # keep in sync with .streamlit/config.toml


# --------------------------------------------------------------------------
# Storage backends: local disk (default) or Supabase Storage (persistent)
# --------------------------------------------------------------------------
def _check_path(path: str) -> str:
    if not PATH_RE.match(path):
        raise ValueError("Invalid storage path.")
    return path


class LocalStore:
    kind = "local"

    def __init__(self, root: str):
        self.root = Path(root)
        (self.root / "files").mkdir(parents=True, exist_ok=True)

    def read(self, path: str):
        p = self.root / _check_path(path)
        return p.read_bytes() if p.is_file() else None

    def write(self, path: str, data: bytes) -> None:
        p = self.root / _check_path(path)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)  # atomic: never leaves a half-written file

    def delete(self, path: str) -> None:
        (self.root / _check_path(path)).unlink(missing_ok=True)


class SupabaseStore:
    """Talks to Supabase Storage over its REST API (private bucket, service key)."""

    kind = "supabase"

    def __init__(self, url: str, key: str, bucket: str):
        self.base = f"{url.rstrip('/')}/storage/v1/object/{bucket}"
        self.headers = {"Authorization": f"Bearer {key}", "apikey": key}

    def read(self, path: str):
        r = requests.get(f"{self.base}/{_check_path(path)}", headers=self.headers, timeout=60)
        if r.status_code in (400, 404):  # Supabase reports a missing object as 400 or 404
            return None
        r.raise_for_status()
        return r.content

    def write(self, path: str, data: bytes) -> None:
        ctype = "application/json" if path.endswith(".json") else "application/pdf"
        r = requests.post(
            f"{self.base}/{_check_path(path)}",
            headers={**self.headers, "Content-Type": ctype, "x-upsert": "true"},
            data=data,
            timeout=180,
        )
        r.raise_for_status()

    def delete(self, path: str) -> None:
        r = requests.delete(f"{self.base}/{_check_path(path)}", headers=self.headers, timeout=60)
        if r.status_code not in (200, 204, 400, 404):
            r.raise_for_status()


@st.cache_resource
def get_store():
    url, key = cfg("SUPABASE_URL"), cfg("SUPABASE_SERVICE_KEY")
    if url and key:
        return SupabaseStore(url, key, cfg("SUPABASE_BUCKET", "notes"))
    return LocalStore(cfg("DATA_DIR", str(Path(__file__).resolve().parent / "data")))


@st.cache_resource
def get_lock():
    return threading.Lock()


@st.cache_resource
def _login_attempts() -> dict:
    return {"fails": 0, "locked_until": 0.0}


# --------------------------------------------------------------------------
# Library data (subjects + files metadata stored in library.json)
# --------------------------------------------------------------------------
def clean_library(raw) -> dict:
    """Validate and normalise library data (also used when restoring a backup)."""
    raw = raw if isinstance(raw, dict) else {}
    subjects, files, subject_ids, file_ids = [], [], set(), set()
    for s in raw.get("subjects", []):
        try:
            sid = str(s["id"])
            if not ID_RE.match(sid) or sid in subject_ids:
                continue
            subject_ids.add(sid)
            subjects.append({"id": sid, "name": str(s["name"])[:100],
                             "description": str(s.get("description", ""))[:500]})
        except (KeyError, TypeError, AttributeError):
            continue
    for f in raw.get("files", []):
        try:
            fid, path, sid = str(f["id"]), str(f["path"]), str(f["subject_id"])
            if (not ID_RE.match(fid) or fid in file_ids or path == "library.json"
                    or not PATH_RE.match(path) or sid not in subject_ids):
                continue
            file_ids.add(fid)
            files.append({"id": fid, "subject_id": sid, "title": str(f["title"])[:200],
                          "description": str(f.get("description", ""))[:1000], "path": path,
                          "pages": int(f.get("pages", 0)), "size": int(f.get("size", 0)),
                          "uploaded": float(f.get("uploaded", 0))})
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return {"subjects": subjects, "files": files}


def read_library_fresh() -> dict:
    data = get_store().read("library.json")
    if not data:
        return {"subjects": [], "files": []}
    try:
        return clean_library(json.loads(data))
    except ValueError:
        # Never treat a damaged file as "empty": the next save would erase everything.
        raise RuntimeError("library.json is damaged. Restore a backup from the Admin panel "
                           "or fix the file in your storage.")


@st.cache_data(ttl=30, show_spinner=False)
def load_library() -> dict:
    return read_library_fresh()


def mutate(fn):
    """Read-modify-write the library under a lock, then clear caches."""
    with get_lock():
        lib = read_library_fresh()
        result = fn(lib)
        get_store().write("library.json", json.dumps(lib, indent=1).encode("utf-8"))
    st.cache_data.clear()
    return result


def new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(6)


def best_delete(path: str) -> None:
    try:
        get_store().delete(path)
    except Exception:
        pass  # an orphaned blob is harmless; a lost note is not


# --------------------------------------------------------------------------
# PDF helpers
# --------------------------------------------------------------------------
def validate_pdf(raw: bytes) -> int:
    """Return the page count or raise ValueError with a student/admin friendly message."""
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        raise ValueError(f"file is larger than {MAX_UPLOAD_MB} MB")
    if b"%PDF-" not in raw[:1024]:
        raise ValueError("this is not a PDF file")
    try:
        with fitz.open(stream=raw, filetype="pdf") as doc:
            if doc.needs_pass or doc.is_encrypted:
                raise ValueError("the PDF is password protected")
            pages = doc.page_count
    except ValueError:
        raise
    except Exception:
        raise ValueError("the PDF is damaged or cannot be read")
    if not 1 <= pages <= MAX_PAGES:
        raise ValueError(f"the PDF must have between 1 and {MAX_PAGES} pages")
    return pages


@st.cache_data(max_entries=6, show_spinner=False)
def pdf_bytes(path: str):
    return get_store().read(path)


@st.cache_data(max_entries=120, show_spinner=False)
def render_page(path: str, page: int) -> bytes:
    raw = pdf_bytes(path)
    with fitz.open(stream=raw, filetype="pdf") as doc:
        pix = doc[page - 1].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        return pix.tobytes("jpeg", jpg_quality=85)


@st.cache_data(max_entries=120, show_spinner=False)
def page_text(path: str, page: int) -> str:
    raw = pdf_bytes(path)
    with fitz.open(stream=raw, filetype="pdf") as doc:
        return doc[page - 1].get_text().strip()


def fmt_size(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def fmt_date(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d %b %Y") if ts else "-"


def default_title(filename: str) -> str:
    stem = re.sub(r"\.pdf$", "", filename, flags=re.I)
    return re.sub(r"[_]+", " ", stem).strip()[:200] or "Untitled note"


def download_name(title: str) -> str:
    return (re.sub(r"[^\w\s\-().]", "", title).strip()[:80] or "note") + ".pdf"


# --------------------------------------------------------------------------
# Admin operations (every one re-checks the admin session)
# --------------------------------------------------------------------------
def is_admin() -> bool:
    return bool(st.session_state.get("is_admin"))


def guard() -> None:
    if not is_admin():
        raise PermissionError("Admin sign-in required.")


def add_subject(name: str, description: str) -> None:
    guard()
    name = name.strip()
    if not name:
        raise ValueError("Please enter a subject name.")

    def op(lib):
        if any(s["name"].lower() == name.lower() for s in lib["subjects"]):
            raise ValueError("A subject with this name already exists.")
        lib["subjects"].append({"id": new_id("s"), "name": name[:100],
                                "description": description.strip()[:500]})
    mutate(op)


def update_subject(sid: str, name: str, description: str) -> None:
    guard()
    name = name.strip()
    if not name:
        raise ValueError("Please enter a subject name.")

    def op(lib):
        for s in lib["subjects"]:
            if s["id"] != sid and s["name"].lower() == name.lower():
                raise ValueError("Another subject already uses this name.")
        for s in lib["subjects"]:
            if s["id"] == sid:
                s["name"], s["description"] = name[:100], description.strip()[:500]
                return
        raise ValueError("Subject not found.")
    mutate(op)


def move_subject(sid: str, delta: int) -> None:
    guard()

    def op(lib):
        subs = lib["subjects"]
        i = next((k for k, s in enumerate(subs) if s["id"] == sid), None)
        if i is not None and 0 <= i + delta < len(subs):
            subs[i], subs[i + delta] = subs[i + delta], subs[i]
    mutate(op)


def delete_subject(sid: str) -> int:
    guard()

    def op(lib):
        gone = [f for f in lib["files"] if f["subject_id"] == sid]
        lib["subjects"] = [s for s in lib["subjects"] if s["id"] != sid]
        lib["files"] = [f for f in lib["files"] if f["subject_id"] != sid]
        return gone
    gone = mutate(op)
    for f in gone:
        best_delete(f["path"])
    return len(gone)


def add_files(subject_id: str, items: list[tuple[str, str, bytes]]):
    """items = [(title, original_filename, raw_bytes)] -> (added_count, error_messages)."""
    guard()
    staged, errors = [], []
    for title, filename, raw in items:
        try:
            pages = validate_pdf(raw)
            fid = new_id("f")
            path = f"files/{fid}.pdf"
            get_store().write(path, raw)  # blob first, metadata second
            staged.append({"id": fid, "subject_id": subject_id,
                           "title": title.strip()[:200] or default_title(filename),
                           "description": "", "path": path, "pages": pages,
                           "size": len(raw), "uploaded": time.time()})
        except ValueError as e:
            errors.append(f"{filename}: {e}")
        except (OSError, requests.RequestException):
            errors.append(f"{filename}: could not be saved (storage error)")
    if staged:
        def op(lib):
            if not any(s["id"] == subject_id for s in lib["subjects"]):
                raise ValueError("The subject no longer exists.")
            lib["files"].extend(staged)
        try:
            mutate(op)
        except Exception:
            for f in staged:
                best_delete(f["path"])
            raise
    return len(staged), errors


def update_file(fid: str, title: str, description: str, subject_id: str) -> None:
    guard()
    if not title.strip():
        raise ValueError("Please enter a title.")

    def op(lib):
        if not any(s["id"] == subject_id for s in lib["subjects"]):
            raise ValueError("Choose an existing subject.")
        for f in lib["files"]:
            if f["id"] == fid:
                f["title"], f["description"] = title.strip()[:200], description.strip()[:1000]
                f["subject_id"] = subject_id
                return
        raise ValueError("File not found.")
    mutate(op)


def move_file(fid: str, delta: int) -> None:
    guard()

    def op(lib):
        files = lib["files"]
        i = next((k for k, f in enumerate(files) if f["id"] == fid), None)
        if i is None:
            return
        same = [k for k, f in enumerate(files) if f["subject_id"] == files[i]["subject_id"]]
        pos = same.index(i) + delta
        if 0 <= pos < len(same):
            j = same[pos]
            files[i], files[j] = files[j], files[i]
    mutate(op)


def replace_file(fid: str, raw: bytes) -> None:
    guard()
    pages = validate_pdf(raw)
    path = f"files/{new_id('f')}.pdf"
    get_store().write(path, raw)

    def op(lib):
        for f in lib["files"]:
            if f["id"] == fid:
                old = f["path"]
                f.update(path=path, pages=pages, size=len(raw), uploaded=time.time())
                return old
        raise ValueError("File not found.")
    try:
        old = mutate(op)
    except Exception:
        best_delete(path)
        raise
    best_delete(old)


def delete_file(fid: str) -> None:
    guard()

    def op(lib):
        gone = [f for f in lib["files"] if f["id"] == fid]
        lib["files"] = [f for f in lib["files"] if f["id"] != fid]
        return gone
    for f in mutate(op):
        best_delete(f["path"])


def build_backup() -> bytes:
    guard()
    lib = read_library_fresh()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("library.json", json.dumps(lib, indent=1))
        for f in lib["files"]:
            data = get_store().read(f["path"])
            if data is not None:
                z.writestr(f["path"], data)
    return buf.getvalue()


def restore_backup(raw: bytes) -> tuple[int, int]:
    """Replace the whole library with the contents of a backup ZIP."""
    guard()
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("This is not a valid ZIP backup.")
    with z:
        if sum(i.file_size for i in z.infolist()) > MAX_RESTORE_MB * 1024 * 1024:
            raise ValueError("The backup is too large to restore here.")
        names = set(z.namelist())
        if "library.json" not in names:
            raise ValueError("library.json is missing - this is not a UET EE Notes backup.")
        try:
            lib = clean_library(json.loads(z.read("library.json")))
        except ValueError:
            raise ValueError("library.json in the backup is damaged.")
        blobs = {}
        for f in lib["files"]:
            if f["path"] not in names:
                raise ValueError(f"Backup is incomplete: {f['path']} is missing.")
            data = z.read(f["path"])
            f["pages"], f["size"] = validate_pdf(data), len(data)
            blobs[f["path"]] = data
    with get_lock():
        old = read_library_fresh()
        for path, data in blobs.items():
            get_store().write(path, data)
        get_store().write("library.json", json.dumps(lib, indent=1).encode("utf-8"))
    for f in old["files"]:
        if f["path"] not in blobs:
            best_delete(f["path"])
    st.cache_data.clear()
    return len(lib["subjects"]), len(lib["files"])


# --------------------------------------------------------------------------
# UI helpers
# --------------------------------------------------------------------------
def inject_css() -> None:
    st.markdown(
        """<style>
        .brand{display:flex;align-items:center;gap:10px;font-size:1.3rem;font-weight:700;color:#194e43}
        .brand-icon{background:#e6f194;color:#194e43;border-radius:10px;padding:2px 9px;font-family:Georgia,serif}
        .tag{display:inline-block;background:#e6f194;color:#194e43;border-radius:20px;
             padding:2px 11px;font-size:.75rem;font-weight:600}
        .block-container{padding-top:2.2rem}
        </style>""",
        unsafe_allow_html=True,
    )


def flash(msg: str, kind: str = "success") -> None:
    st.session_state.setdefault("_flash", []).append((kind, msg))


def show_flash() -> None:
    for kind, msg in st.session_state.pop("_flash", []):
        getattr(st, kind)(msg)


def set_state(key: str, value) -> None:
    st.session_state[key] = value


def clear_file_param() -> None:
    st.query_params.clear()


def subject_map(lib: dict) -> dict:
    return {s["id"]: s for s in lib["subjects"]}


# --------------------------------------------------------------------------
# Student pages
# --------------------------------------------------------------------------
def open_note(fid: str) -> None:
    st.query_params["file"] = fid


def close_note() -> None:
    st.query_params.clear()


def step_page(delta: int, total: int) -> None:
    st.session_state.reader_page = min(max(int(st.session_state.reader_page) + delta, 1), total)


def page_library() -> None:
    lib = load_library()
    subjects = subject_map(lib)
    fid = st.query_params.get("file")
    if fid:
        note = next((f for f in lib["files"] if f["id"] == fid), None)
        if note:
            return reader(note, subjects[note["subject_id"]]["name"])
        st.warning("That note is no longer available.")
        st.query_params.clear()

    st.title("Your notes library", anchor=False)
    st.caption("Pick a subject, open a note and read it right here.")
    if not lib["subjects"]:
        st.info("No subjects have been added yet. Please check back soon.")
        return

    c1, c2 = st.columns([1, 2])
    choice = c1.selectbox("Subject", ["all"] + [s["id"] for s in lib["subjects"]],
                          format_func=lambda i: "All subjects" if i == "all" else subjects[i]["name"])
    query = c2.text_input("Search", placeholder="Search notes by title or description").strip().lower()

    shown = [f for f in lib["files"]
             if (choice == "all" or f["subject_id"] == choice)
             and (not query or query in f["title"].lower() or query in f["description"].lower()
                  or query in subjects[f["subject_id"]]["name"].lower())]
    if choice != "all" and subjects[choice]["description"]:
        st.write(subjects[choice]["description"])

    m1, m2, m3 = st.columns(3)
    m1.metric("Subjects", len(lib["subjects"]))
    m2.metric("Notes in library", len(lib["files"]))
    m3.metric("Notes shown", len(shown))

    if not shown:
        st.info("No notes match this view yet.")
        return
    if choice == "all":  # group by subject, in the admin's chosen subject order
        order = {s["id"]: i for i, s in enumerate(lib["subjects"])}
        shown.sort(key=lambda f: order[f["subject_id"]])
    for i in range(0, len(shown), 2):
        cols = st.columns(2)
        for col, f in zip(cols, shown[i:i + 2]):
            with col, st.container(border=True):
                st.markdown(f"<span class='tag'>{html.escape(subjects[f['subject_id']]['name'])}</span>",
                            unsafe_allow_html=True)
                st.subheader(f["title"], anchor=False)
                if f["description"]:
                    st.caption(f["description"])
                st.caption(f"{f['pages']} pages · {fmt_size(f['size'])} · added {fmt_date(f['uploaded'])}")
                st.button("📖 Read", key=f"read_{f['id']}", on_click=open_note,
                          args=(f["id"],), width="stretch")


def reader(note: dict, subject_name: str) -> None:
    total = note["pages"]
    if st.session_state.get("reader_file") != note["id"]:
        st.session_state.reader_file = note["id"]
        st.session_state.reader_page = 1
    st.session_state.reader_page = min(max(int(st.session_state.get("reader_page", 1)), 1), total)

    st.button("← Back to library", on_click=close_note)
    st.caption(subject_name)
    st.title(note["title"], anchor=False)
    if note["description"]:
        st.write(note["description"])

    raw = pdf_bytes(note["path"])
    if raw is None:
        st.error("This file is missing from storage. Please tell the admin.")
        return

    def nav(suffix: str) -> None:
        page = st.session_state.reader_page
        a, b, c = st.columns([1, 1, 1])
        a.button("◀ Previous", key=f"prev_{suffix}", on_click=step_page, args=(-1, total),
                 disabled=page <= 1, width="stretch")
        b.markdown(f"<div style='text-align:center;padding-top:.45rem'>Page <b>{page}</b> of {total}</div>",
                   unsafe_allow_html=True)
        c.button("Next ▶", key=f"next_{suffix}", on_click=step_page, args=(1, total),
                 disabled=page >= total, width="stretch")

    t1, t2, t3 = st.columns([1.2, 1.6, 1.2], vertical_alignment="bottom")
    t1.number_input("Go to page", 1, total, key="reader_page", step=1)
    width = t2.radio("Page width", ["Narrow", "Medium", "Full"], index=1, horizontal=True, key="reader_width")
    t3.download_button("⬇ Download PDF", data=raw, file_name=download_name(note["title"]),
                       mime="application/pdf", width="stretch")

    nav("top")
    page = st.session_state.reader_page
    ratio = {"Narrow": [1, 2, 1], "Medium": [1, 6, 1], "Full": [0.001, 20, 0.001]}[width]
    with st.spinner("Loading page..."):
        img = render_page(note["path"], page)
    _, mid, _ = st.columns(ratio)
    mid.image(img, width="stretch")
    with st.expander("📝 Text of this page (select and copy)"):
        text = page_text(note["path"], page)
        st.text(text if text else "No selectable text on this page (it is probably a scanned image).")
    nav("bottom")
    st.caption(f"Share this note: add `?file={note['id']}` to the website address.")


# --------------------------------------------------------------------------
# Admin pages
# --------------------------------------------------------------------------
def admin_login() -> None:
    password = cfg("ADMIN_PASSWORD")
    st.title("Admin sign-in", anchor=False)
    if not password:
        st.error("The admin panel is disabled because ADMIN_PASSWORD is not set. "
                 "Add it in your app's Secrets (see README.md), then reload.")
        return
    attempts = _login_attempts()
    with st.form("login"):
        entered = st.text_input("Admin password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if not submitted:
        return
    wait = int(attempts["locked_until"] - time.time())
    if wait > 0:
        st.error(f"Too many wrong attempts. Try again in {wait // 60 + 1} minute(s).")
    elif hmac.compare_digest(entered.encode(), password.encode()):
        attempts["fails"] = 0
        st.session_state.is_admin = True
        st.rerun()
    else:
        attempts["fails"] += 1
        if attempts["fails"] >= 5:
            attempts.update(fails=0, locked_until=time.time() + 300)
        time.sleep(1)
        st.error("Incorrect password.")


def tab_dashboard(lib: dict) -> None:
    store = get_store()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Subjects", len(lib["subjects"]))
    c2.metric("Files", len(lib["files"]))
    c3.metric("Total pages", sum(f["pages"] for f in lib["files"]))
    c4.metric("Storage used", fmt_size(sum(f["size"] for f in lib["files"])))
    if store.kind == "supabase":
        st.success("Storage: Supabase (files survive restarts and redeploys).")
    elif Path("/mount/src").exists():
        st.warning("Storage: local disk on Streamlit Community Cloud. **Uploaded files will be erased "
                   "whenever the app restarts or redeploys.** Connect Supabase (see README.md) or "
                   "download a backup regularly.")
    else:
        st.info("Storage: local disk (the `data` folder next to app.py).")
    st.subheader("Recently added", anchor=False)
    subjects = subject_map(lib)
    recent = sorted(lib["files"], key=lambda f: f["uploaded"], reverse=True)[:5]
    if recent:
        st.dataframe([{"Title": f["title"], "Subject": subjects[f["subject_id"]]["name"],
                       "Pages": f["pages"], "Added": fmt_date(f["uploaded"])} for f in recent],
                     hide_index=True, width="stretch")
    else:
        st.caption("Nothing uploaded yet. Add a subject, then upload PDFs.")


def do_move_subject(sid: str, delta: int) -> None:
    move_subject(sid, delta)


def tab_subjects(lib: dict) -> None:
    st.subheader("Add a subject", anchor=False)
    with st.form("add_subject", clear_on_submit=True):
        name = st.text_input("Subject name", max_chars=100, placeholder="e.g. Circuit Analysis")
        desc = st.text_area("Description (optional)", max_chars=500, height=80)
        if st.form_submit_button("➕ Add subject", type="primary"):
            try:
                add_subject(name, desc)
                flash(f"Subject “{name.strip()}” added. You can upload files to it now.")
                st.rerun()
            except ValueError as e:
                st.error(str(e))

    st.subheader(f"Your subjects ({len(lib['subjects'])})", anchor=False)
    if not lib["subjects"]:
        st.caption("No subjects yet.")
    last = len(lib["subjects"]) - 1
    for i, s in enumerate(lib["subjects"]):
        sid = s["id"]
        count = sum(1 for f in lib["files"] if f["subject_id"] == sid)
        with st.container(border=True):
            c1, c2, c3 = st.columns([6, 1, 1], vertical_alignment="center")
            c1.markdown(f"**{s['name']}** · {count} file{'' if count == 1 else 's'}")
            c2.button("⬆", key=f"sup_{sid}", on_click=do_move_subject, args=(sid, -1), disabled=i == 0,
                      help="Move up", width="stretch")
            c3.button("⬇", key=f"sdn_{sid}", on_click=do_move_subject, args=(sid, 1), disabled=i == last,
                      help="Move down", width="stretch")
            with st.expander("Edit or delete"):
                with st.form(f"edit_subject_{sid}"):
                    new_name = st.text_input("Name", s["name"], max_chars=100)
                    new_desc = st.text_area("Description", s["description"], max_chars=500, height=80)
                    if st.form_submit_button("💾 Save changes"):
                        try:
                            update_subject(sid, new_name, new_desc)
                            flash("Subject updated.")
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))
                if st.session_state.get("confirm_del_subject") == sid:
                    st.warning(f"Delete “{s['name']}” and its {count} file(s)? This cannot be undone.")
                    a, b = st.columns(2)
                    if a.button("Yes, delete permanently", key=f"yes_{sid}", type="primary"):
                        n = delete_subject(sid)
                        st.session_state.pop("confirm_del_subject", None)
                        flash(f"Subject deleted along with {n} file(s).")
                        st.rerun()
                    if b.button("Cancel", key=f"no_{sid}"):
                        st.session_state.pop("confirm_del_subject", None)
                        st.rerun()
                else:
                    st.button("🗑 Delete this subject…", key=f"del_{sid}", on_click=set_state,
                              args=("confirm_del_subject", sid))


def tab_upload(lib: dict) -> None:
    if not lib["subjects"]:
        st.info("Create a subject first (Subjects tab), then come back here to upload files.")
        return
    subjects = subject_map(lib)
    gen = st.session_state.setdefault("up_gen", 0)
    sid = st.selectbox("Upload into subject", [s["id"] for s in lib["subjects"]],
                       format_func=lambda i: subjects[i]["name"], key="upload_subject")
    uploads = st.file_uploader(f"PDF files (up to {MAX_UPLOAD_MB} MB each - you can pick several)",
                               type=["pdf"], accept_multiple_files=True, key=f"uploader_{gen}")
    titles = {}
    for f in uploads or []:
        fkey = getattr(f, "file_id", f"{f.name}{f.size}")
        titles[fkey] = st.text_input(f"Title for {f.name}", default_title(f.name),
                                     max_chars=200, key=f"title_{gen}_{fkey}")
    if uploads and st.button(f"⬆ Upload {len(uploads)} file(s)", type="primary"):
        items = [(titles[getattr(f, "file_id", f"{f.name}{f.size}")], f.name, f.getvalue()) for f in uploads]
        with st.spinner("Uploading..."):
            try:
                added, errors = add_files(sid, items)
            except (ValueError, OSError, requests.RequestException) as e:
                st.error(f"Upload failed: {e}")
                return
        if added:
            flash(f"{added} file(s) uploaded to “{subjects[sid]['name']}”.")
        for msg in errors:
            flash(msg, "error")
        st.session_state.up_gen = gen + 1  # resets the uploader
        st.rerun()


def tab_manage(lib: dict) -> None:
    if not lib["files"]:
        st.info("No files yet. Use the Upload files tab.")
        return
    subjects = subject_map(lib)
    filter_opts = ["all"] + [s["id"] for s in lib["subjects"]]
    if st.session_state.get("manage_filter") not in filter_opts:
        st.session_state.pop("manage_filter", None)
    flt = st.selectbox("Show files from", filter_opts, key="manage_filter",
                       format_func=lambda i: "All subjects" if i == "all" else subjects[i]["name"])
    shown = [f for f in lib["files"] if flt == "all" or f["subject_id"] == flt]
    if not shown:
        st.info("This subject has no files yet.")
        return
    st.dataframe([{"Title": f["title"], "Subject": subjects[f["subject_id"]]["name"], "Pages": f["pages"],
                   "Size": fmt_size(f["size"]), "Added": fmt_date(f["uploaded"])} for f in shown],
                 hide_index=True, width="stretch")

    ids = [f["id"] for f in shown]
    if st.session_state.get("manage_file") not in ids:
        st.session_state.pop("manage_file", None)
    fid = st.selectbox("Choose a file to edit", ids, key="manage_file",
                       format_func=lambda i: next(f["title"] for f in shown if f["id"] == i))
    f = next(x for x in shown if x["id"] == fid)

    st.markdown("---")
    with st.form(f"edit_file_{fid}"):
        title = st.text_input("Title", f["title"], max_chars=200)
        desc = st.text_area("Description", f["description"], max_chars=1000, height=80)
        sub_ids = [s["id"] for s in lib["subjects"]]
        target = st.selectbox("Subject", sub_ids, index=sub_ids.index(f["subject_id"]),
                              format_func=lambda i: subjects[i]["name"])
        if st.form_submit_button("💾 Save changes", type="primary"):
            try:
                update_file(fid, title, desc, target)
                flash("File updated.")
                st.rerun()
            except ValueError as e:
                st.error(str(e))

    same = [x["id"] for x in lib["files"] if x["subject_id"] == f["subject_id"]]
    o1, o2, _ = st.columns([1, 1, 4])
    o1.button("⬆ Move up", key=f"fup_{fid}", disabled=same.index(fid) == 0,
              on_click=move_file, args=(fid, -1), width="stretch")
    o2.button("⬇ Move down", key=f"fdn_{fid}", disabled=same.index(fid) == len(same) - 1,
              on_click=move_file, args=(fid, 1), width="stretch")

    gen = st.session_state.setdefault("rep_gen", 0)
    new_pdf = st.file_uploader("Replace the PDF (keeps title and position)", type=["pdf"],
                               key=f"replace_{fid}_{gen}")
    if new_pdf and st.button("🔁 Replace PDF"):
        try:
            replace_file(fid, new_pdf.getvalue())
            st.session_state.rep_gen = gen + 1
            flash("PDF replaced.")
            st.rerun()
        except (ValueError, OSError, requests.RequestException) as e:
            st.error(f"Could not replace the PDF: {e}")

    if st.session_state.get("confirm_del_file") == fid:
        st.warning(f"Delete “{f['title']}” permanently?")
        a, b = st.columns(2)
        if a.button("Yes, delete permanently", key=f"yesf_{fid}", type="primary"):
            delete_file(fid)
            st.session_state.pop("confirm_del_file", None)
            flash("File deleted.")
            st.rerun()
        if b.button("Cancel", key=f"nof_{fid}"):
            st.session_state.pop("confirm_del_file", None)
            st.rerun()
    else:
        st.button("🗑 Delete this file…", key=f"delf_{fid}", on_click=set_state, args=("confirm_del_file", fid))


def tab_backup(lib: dict) -> None:
    st.subheader("Download a backup", anchor=False)
    st.caption("One ZIP with every subject and PDF. Keep a copy somewhere safe (Google Drive, USB).")
    if st.button("Prepare backup"):
        with st.spinner("Building backup..."):
            st.session_state.backup_zip = build_backup()
    if st.session_state.get("backup_zip"):
        st.download_button("⬇ Download backup ZIP", st.session_state.backup_zip,
                           file_name=f"uet-ee-notes-backup-{datetime.now():%Y%m%d-%H%M}.zip",
                           mime="application/zip", type="primary")

    st.subheader("Restore from a backup", anchor=False)
    st.warning("Restoring **replaces** all current subjects and files with the backup's contents.")
    gen = st.session_state.setdefault("restore_gen", 0)
    z = st.file_uploader("Backup ZIP", type=["zip"], key=f"restore_{gen}")
    sure = st.checkbox("I understand this replaces everything currently in the library")
    if z and sure and st.button("♻ Restore now", type="primary"):
        try:
            with st.spinner("Restoring..."):
                subs, files = restore_backup(z.getvalue())
            st.session_state.restore_gen = gen + 1
            st.session_state.pop("backup_zip", None)
            flash(f"Restored {subs} subject(s) and {files} file(s).")
            st.rerun()
        except (ValueError, OSError, requests.RequestException) as e:
            st.error(f"Restore failed: {e}")


def page_admin() -> None:
    if not is_admin():
        return admin_login()
    top, out = st.columns([6, 1], vertical_alignment="center")
    top.title("Admin panel", anchor=False)
    if out.button("Sign out"):
        st.session_state.is_admin = False
        st.rerun()
    show_flash()
    lib = read_library_fresh()  # admin always sees the newest data
    tabs = st.tabs(["Dashboard", "Subjects", "Upload files", "Manage files", "Backup & restore"])
    with tabs[0]:
        tab_dashboard(lib)
    with tabs[1]:
        tab_subjects(lib)
    with tabs[2]:
        tab_upload(lib)
    with tabs[3]:
        tab_manage(lib)
    with tabs[4]:
        tab_backup(lib)


# --------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title=APP_NAME, page_icon="📘", layout="wide")
    inject_css()
    with st.sidebar:
        st.markdown('<div class="brand"><span class="brand-icon">EE</span>UET EE Notes</div>',
                    unsafe_allow_html=True)
        st.caption("Study notes for electrical engineering students")
        st.radio("Go to", ["📚 Library", "🔐 Admin"], key="nav", label_visibility="collapsed",
                 index=1 if "admin" in st.query_params else 0, on_change=clear_file_param)
        if is_admin():
            st.success("Signed in as admin")
    try:
        page_library() if st.session_state.nav.startswith("📚") else page_admin()
    except PermissionError:
        st.error("Please sign in as admin again.")
    except requests.RequestException:
        st.error("Could not reach the file storage. Please try again in a moment.")
    except (RuntimeError, OSError) as e:
        st.error(f"Storage problem: {e}")


if __name__ == "__main__":
    main()
