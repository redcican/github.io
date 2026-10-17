# Shikun Chen · academic homepage

Static site for GitHub Pages. No build step: `index.html` reads the JSON files in `data/`.

## What updates by itself
`.github/workflows/update.yml` runs every Monday (and on demand from the Actions tab). It runs
`scripts/update.py`, which pulls:

- **works** from ORCID, with authors, venues and citation counts from OpenAlex
- **peer reviews** from ORCID, with journal names resolved by ISSN through OpenAlex
- **fundings** from ORCID into the Projects section
- **citations / h-index / i10-index** from Google Scholar (add a `SERPAPI_KEY` repository secret if Scholar blocks the direct read; OpenAlex is the fallback)

New items are appended to `data/*.json` and committed; GitHub Pages redeploys on that commit.
Nothing is ever deleted automatically.

## What you edit by hand
- `data/profile.json`: name, position, bio, links, research themes (each theme's `keywords` decide which papers it counts and filters).
- `data/cv.json`: the Education, Experience and Honors sections. Each entry has `start`, `end` (empty means now), `title`, `org`, `detail`, plus `_zh` versions. The sync never touches it.
- `data/projects.json`: add projects that are not in ORCID (`title`, `funder`, `grant`, `role`, `start`, `end`, `url`).
- Thumbnails: each paper gets a small figure drawn from its topic. To use a real figure, put an image in `images/` and set the paper's `"thumb": "images/name.png"` (syncs never touch it).
- `data/publications.json`: fix any field, then add its name to that entry's `"locked": [...]` so syncs never overwrite it.
- `config.json`: `hide` takes DOIs (lowercase) of works to leave off the page; `author_aliases` fixes author names; set `contact_email` to get OpenAlex's faster lane.

## Setup
1. Push this folder to `<username>.github.io` (or any repo).
2. Settings → Pages → Deploy from branch → `main` / root.
3. Settings → Actions → General → Workflow permissions → Read and write.
4. Run "Sync publications" once from the Actions tab to fill peer reviews.

Preview locally with `python3 -m http.server` and open http://localhost:8000.
