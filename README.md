# UET EE Notes — Streamlit

A simple university notes website for students.

## Features

### Students
- Browse subjects
- Search notes
- Open and read PDFs inside the website
- Move between PDF pages
- Copy selectable PDF text when available
- Download a PDF

### Admin
- Password-protected admin panel
- Add, edit, reorder, and delete subjects
- Upload multiple PDFs directly into a selected subject
- Edit PDF title/description/subject
- Replace a PDF without changing its title/position
- Delete PDFs
- Create/download a full backup ZIP
- Restore a previous backup

## 1. Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

The app normally opens at `http://localhost:8501`.

## 2. Configure the admin password

For local testing, create `.streamlit/secrets.toml`:

```toml
ADMIN_PASSWORD = "CHANGE_THIS_TO_A_LONG_RANDOM_PASSWORD"
MAX_UPLOAD_MB = 50
```

Do NOT commit `secrets.toml` to GitHub.

## 3. Important: persistent storage when hosting

Streamlit Community Cloud's local filesystem should not be treated as permanent storage. If students' PDFs must survive restarts/redeployments, use the included Supabase storage support.

Add these secrets:

```toml
ADMIN_PASSWORD = "your-long-admin-password"
MAX_UPLOAD_MB = 50

SUPABASE_URL = "https://YOUR_PROJECT.supabase.co"
SUPABASE_SERVICE_KEY = "YOUR_SERVER_SIDE_SERVICE_ROLE_KEY"
SUPABASE_BUCKET = "notes"
```

Create a private Supabase Storage bucket named `notes`.

The `SUPABASE_SERVICE_KEY` is a server-side secret. Never put it in frontend code, public files, GitHub, or student-visible text.

The app stores:
- `library.json` — subjects and PDF metadata
- `files/<id>.pdf` — uploaded PDFs

## 4. Streamlit Community Cloud

1. Put `app.py`, `requirements.txt`, and `.streamlit/config.toml` in a GitHub repository.
2. Deploy the repository as a Streamlit app.
3. In Streamlit Cloud, open the app's Secrets settings.
4. Add `ADMIN_PASSWORD`, and preferably the Supabase settings above.
5. Restart/redeploy the app.
6. Open **Admin** in the sidebar and sign in.
7. Add subjects first, then use **Upload files** to add PDFs to each subject.

## 5. How your website works

Students do not get an admin button that can modify files. All modifying operations call an admin-session guard. The admin panel is password protected.

The normal workflow is:

1. Admin → Subjects → Add subject
2. Admin → Upload files → select subject → choose PDF(s) → Upload
3. Students → Library → choose subject → Read

## 6. Backups

Use **Admin → Backup & restore → Prepare backup** regularly. A backup contains the subject metadata and uploaded PDFs. Store backups privately because they contain your site's notes.

## Notes

- PDF uploads are validated before being stored.
- Password-protected/encrypted PDFs are rejected.
- Maximum PDF pages and upload size are configurable in `app.py` / secrets.
- This version does not require student accounts; only the admin needs a password.
