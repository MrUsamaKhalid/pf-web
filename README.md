# PropertyFinder Photo Downloader

Paste a PropertyFinder listing URL, get a ZIP of all its photos.

- **docs/** — static page (served by GitHub Pages from the `/docs` folder)
- **backend/** — Flask + Playwright scraper (deploy on Render)

## How it works

1. Frontend (GitHub Pages) POSTs the listing URL to the backend.
2. Backend opens the page in headless Chromium, scrolls to load lazy images,
   and collects photo URLs from both `__NEXT_DATA__` and the DOM.
3. It filters out logos/agents/icons, dedupes size variants, downloads the
   photos, and returns them as `photo_01.jpg`, `photo_02.jpg`, ... in a ZIP.
4. The browser downloads the ZIP automatically.

The ZIP is meant to be extracted manually and edited in Photoshop.

---

## Local testing

```bash
cd backend
python -m pip install -r requirements.txt
python -m playwright install chromium
python app.py
```

Test the API:

```bat
curl -X POST http://127.0.0.1:5000/scrape ^
  -H "Content-Type: application/json" ^
  -d "{\"url\":\"PASTE_PROPERTYFINDER_URL_HERE\"}" ^
  --output test.zip
```

Frontend: open `docs/index.html`, set `BACKEND_URL` to
`http://127.0.0.1:5000`, paste a listing URL, click Download.

---

## Deploy

### 1. Push to GitHub

```bash
git init
git add .
git commit -m "Initial PropertyFinder photo downloader"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/pf-web.git
git push -u origin main
```

### 2. Backend on Render

- New → **Web Service** → connect the `pf-web` repo.
- Root directory: `backend`
- Render reads `backend/render.yaml` (Docker runtime, Playwright image).
- Deploy, then copy the live URL, e.g. `https://pf-web-backend.onrender.com`.

### 3. Point the frontend at the backend

In `docs/index.html` replace:

```js
const BACKEND_URL = "PASTE_RENDER_BACKEND_URL_HERE";
```

with your Render URL, then:

```bash
git add docs/index.html
git commit -m "Add Render backend URL"
git push
```

### 4. GitHub Pages

Repo → Settings → Pages → Deploy from branch → `main` → `/docs` → Save.

Live at `https://YOUR_USERNAME.github.io/pf-web/`.
