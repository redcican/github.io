#!/usr/bin/env python3
"""Sync publications, peer reviews, funded projects and metrics into data/*.json.

Sources: the public ORCID API decides which works, peer reviews and fundings are
yours; OpenAlex (looked up by DOI) adds author lists, venues, citation counts and
journal names for ISSNs.
Existing entries are never deleted, and fields listed in an entry's "locked"
array are never overwritten, so hand edits survive every sync.
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG = json.loads((ROOT / "config.json").read_text())
ORCID = CONFIG["orcid"]
MAILTO = CONFIG.get("contact_email", "")
TODAY = dt.date.today().isoformat()
UA = "scholar-homepage-sync/1.0" + (f" (mailto:{MAILTO})" if MAILTO else "")


def get_json(url, accept="application/json", tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # network hiccups, 429s
            if i == tries - 1:
                print(f"warn: {url}: {e}", file=sys.stderr)
                return None
            time.sleep(2 ** i)


def openalex(path, **params):
    if MAILTO:
        params["mailto"] = MAILTO
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    return get_json(f"https://api.openalex.org/{path}{q}")


def orcid(section):
    return get_json(f"https://pub.orcid.org/v3.0/{ORCID}/{section}")


def val(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def norm_title(t):
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def key_of(p):
    return ("doi:" + p["doi"]) if p.get("doi") else ("t:" + norm_title(p["title"]))


def load(name):
    path = DATA / name
    return json.loads(path.read_text()) if path.exists() else {"items": []}


def save(name, obj):
    """Write only when content changed, so unchanged runs make no commit."""
    path = DATA / name
    old = json.loads(path.read_text()) if path.exists() else None
    if old is not None:
        a = {k: v for k, v in old.items() if k != "updated"}
        b = {k: v for k, v in obj.items() if k != "updated"}
        if a == b:
            return False
    obj["updated"] = TODAY
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    return True


ALIASES = CONFIG.get("author_aliases", {})
HIDDEN = {h.lower() for h in CONFIG.get("hide", [])}
EXCLUDED_TYPES = set(CONFIG.get("exclude_types", ["preprint", "dataset", "other", "data-set"]))
TYPE_MAP = {"journal-article": "journal", "article": "journal", "conference-paper": "conference",
            "conference-proceedings": "conference", "proceedings-article": "conference",
            "book-chapter": "chapter", "book": "book", "preprint": "preprint", "posted-content": "preprint"}


def date_from(d):
    y, m, dd = val(d, "year", "value"), val(d, "month", "value"), val(d, "day", "value")
    return "-".join(x for x in [y, m, dd] if x) if y else None


# ---------- publications ----------

def abstract_text(inv):
    """OpenAlex stores abstracts as {word: [positions]}; rebuild the running text."""
    if not inv:
        return ""
    pos = sorted((i, word) for word, idx in inv.items() for i in idx)
    return " ".join(word for _, word in pos)


def from_openalex(w):
    doi = (w.get("doi") or "").replace("https://doi.org/", "").lower() or None
    src = val(w, "primary_location", "source") or {}
    p = {
        "title": w.get("title") or w.get("display_name") or "",
        "authors": [ALIASES.get(a["author"]["display_name"], a["author"]["display_name"])
                    for a in w.get("authorships", [])],
        "venue": src.get("display_name") or val(w, "primary_location", "raw_source_name") or "",
        "year": w.get("publication_year"),
        "date": w.get("publication_date"),
        "type": TYPE_MAP.get(w.get("type_crossref") or w.get("type"), w.get("type") or "other"),
        "doi": doi,
        "url": f"https://doi.org/{doi}" if doi else (w.get("id") or ""),
        "citations": w.get("cited_by_count", 0),
        "abstract": abstract_text(w.get("abstract_inverted_index")),
    }
    if p["type"] == "journal" and src.get("type") == "conference":
        p["type"] = "conference"
    return p


def openalex_by_doi(doi):
    """Exact lookup of one work. OpenAlex's author-level ORCID matching is too loose to list works."""
    w = openalex(f"works/doi:{doi}") if doi else None
    return from_openalex(w) if w and w.get("id") else None


def orcid_works():
    r = orcid("works") or {}
    out = {}
    for g in r.get("group", []):
        s = (g.get("work-summary") or [{}])[0]
        doi = None
        for e in val(s, "external-ids", "external-id") or []:
            if e.get("external-id-type") == "doi":
                doi = e.get("external-id-value", "").lower().replace("https://doi.org/", "")
        date = date_from(s.get("publication-date") or {})
        p = {
            "title": val(s, "title", "title", "value") or "",
            "authors": [],
            "venue": val(s, "journal-title", "value") or "",
            "year": int(date[:4]) if date else None,
            "date": date,
            "type": TYPE_MAP.get(s.get("type"), s.get("type") or "other"),
            "doi": doi,
            "url": f"https://doi.org/{doi}" if doi else (val(s, "url", "value") or ""),
            "citations": 0,
        }
        if p["title"]:
            out[key_of(p)] = p
    return out


def sync_publications():
    store = load("publications.json")
    items = store["items"]
    index = {key_of(p): p for p in items}
    by_title = {norm_title(p["title"]): p for p in items}
    works = orcid_works()
    if not works:
        print("No works from ORCID; leaving publications unchanged.")
        return []
    added = []
    for k, p in works.items():
        if (p.get("doi") or "") in HIDDEN or norm_title(p["title"]) in HIDDEN:
            continue
        cur = index.get(k) or by_title.get(norm_title(p["title"]))
        if cur is None and p["type"] in EXCLUDED_TYPES:
            continue
        rich = openalex_by_doi(p.get("doi"))
        if rich:
            for f in ("venue", "date", "year"):
                rich[f] = p[f] or rich[f] if f == "venue" else rich[f] or p[f]
            if "arxiv" in (rich["venue"] or "").lower():
                rich["type"] = "preprint"
            rich["title"] = p["title"] or rich["title"]
            if p["type"] in ("journal", "conference") and rich["type"] != "preprint":
                rich["type"] = p["type"]
            p = rich
        if not p.get("year"):
            continue
        if cur is None:
            p["added"] = TODAY
            items.append(p)
            added.append(p)
            continue
        locked = set(cur.get("locked", []))
        for f, v in p.items():
            if f in locked or v in (None, "", []):
                continue
            if f == "citations" or not cur.get(f):
                cur[f] = v
    added += merge_scholar(items)
    items.sort(key=lambda p: (p.get("date") or str(p.get("year") or "")), reverse=True)
    save("publications.json", store)
    return added


def scholar_articles():
    """Article list of the Google Scholar profile through SerpApi (needs SERPAPI_KEY)."""
    key, user = os.environ.get("SERPAPI_KEY"), CONFIG.get("scholar_id")
    if not key or not user:
        return []
    out, start = [], 0
    while True:
        q = urllib.parse.urlencode({"engine": "google_scholar_author", "author_id": user, "api_key": key,
                                    "hl": "en", "num": 100, "start": start, "sort": "pubdate"})
        r = get_json(f"https://serpapi.com/search.json?{q}") or {}
        page = r.get("articles") or []
        out += page
        if len(page) < 100:
            return out
        start += 100


def openalex_by_title(title):
    """Find a work by exact (normalised) title, to get its DOI and full author list."""
    r = openalex("works", filter=f"title.search:{re.sub(r'[^A-Za-z0-9 ]', ' ', title)[:200]}", **{"per-page": 10})
    for w in (r or {}).get("results", []):
        if norm_title(w.get("title")) == norm_title(title):
            return from_openalex(w)
    return None


def merge_scholar(items):
    """Add papers that are on Google Scholar but missing from ORCID, and take Scholar's per-paper citations."""
    arts = scholar_articles()
    if not arts:
        return []
    by_title = {norm_title(p["title"]): p for p in items}
    report, added = [], []
    for a in arts:
        t = a.get("title") or ""
        nt = norm_title(t)
        cites = val(a, "cited_by", "value") or 0
        cur = by_title.get(nt)
        status = "listed"
        if cur is not None:
            if "citations" not in set(cur.get("locked", [])):
                cur["citations"] = max(cites, cur.get("citations") or 0)
        elif nt in HIDDEN:
            status = "hidden"
        else:
            p = openalex_by_title(t)
            if p and ((p.get("doi") or "") in HIDDEN):
                status = "hidden"
            elif "arxiv" in (a.get("publication") or "").lower() or (p and p["type"] in EXCLUDED_TYPES):
                status = "skipped (preprint)"
            else:
                if not p:
                    venue = re.split(r"\s+\d|,", a.get("publication") or "")[0].strip()
                    p = {"title": t, "authors": [ALIASES.get(x.strip(), x.strip()) for x in (a.get("authors") or "").split(",") if x.strip()],
                         "venue": venue, "year": int(a["year"]) if str(a.get("year", "")).isdigit() else None,
                         "date": None, "type": "journal", "doi": None, "url": a.get("link") or "", "citations": cites}
                if p.get("year"):
                    p["citations"] = max(cites, p.get("citations") or 0)
                    p["added"] = TODAY
                    p["source"] = "scholar"
                    items.append(p)
                    by_title[nt] = p
                    added.append(p)
                    status = "added"
                else:
                    status = "skipped (no year)"
        report.append({"title": t, "year": a.get("year"), "publication": a.get("publication"), "status": status})
    save("scholar.json", {"count": len(arts), "items": report})
    return added


# ---------- peer review ----------

def sync_reviews():
    r = orcid("peer-reviews")
    if r is None:
        return 0
    summaries = []

    def walk(o):
        if isinstance(o, dict):
            if "review-group-id" in o:
                summaries.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(r)

    store = load("reviews.json")
    known = {x["group"]: x for x in store["items"]}
    groups = {}
    for s in summaries:
        gid = s["review-group-id"]
        g = groups.setdefault(gid, {"count": 0, "years": set(), "publisher": val(s, "convening-organization", "name")})
        g["count"] += 1
        y = val(s, "completion-date", "year", "value")
        if y:
            g["years"].add(int(y))
    items = []
    for gid, g in groups.items():
        prev = known.get(gid, {})
        journal = prev.get("journal")
        if not journal and gid.startswith("issn:"):
            src = openalex(f"sources/issn:{gid[5:]}")
            journal = (src or {}).get("display_name")
        ys = sorted(g["years"])
        items.append({
            "group": gid,
            "journal": journal or g["publisher"] or gid,
            "publisher": prev.get("publisher") or (g["publisher"] if journal else ""),
            "count": g["count"],
            "years": (f"{ys[0]}–{ys[-1]}" if len(ys) > 1 and ys[0] != ys[-1] else str(ys[0])) if ys else "",
        })
    items.sort(key=lambda x: (-x["count"], x["journal"]))
    before = sum(x.get("count", 0) for x in store["items"])
    store["items"] = items
    save("reviews.json", store)
    return sum(x["count"] for x in items) - before


# ---------- projects (ORCID fundings + hand-written entries) ----------

def sync_projects():
    r = orcid("fundings")
    if r is None:
        return []
    store = load("projects.json")
    items = store["items"]
    known = {x.get("orcid_put_code") for x in items}
    added = []
    for g in r.get("group", []):
        s = (g.get("funding-summary") or [{}])[0]
        code = s.get("put-code")
        if code in known:
            continue
        grant = next((e.get("external-id-value") for e in val(s, "external-ids", "external-id") or []
                      if e.get("external-id-type") == "grant_number"), "")
        p = {
            "title": val(s, "title", "title", "value") or "",
            "funder": val(s, "organization", "name") or "",
            "grant": grant,
            "role": "",
            "start": val(s, "start-date", "year", "value") or "",
            "end": val(s, "end-date", "year", "value") or "",
            "url": val(s, "url", "value") or "",
            "orcid_put_code": code,
            "added": TODAY,
        }
        items.append(p)
        added.append(p)
    items.sort(key=lambda p: p.get("start") or "", reverse=True)
    save("projects.json", store)
    return added


def scholar_metrics():
    """Citations, h-index and i10-index from the public Google Scholar profile.

    Scholar has no API. With a SERPAPI_KEY secret the numbers come through SerpApi;
    otherwise the profile page is read directly, which Scholar sometimes blocks.
    Returns None when neither works, so the last known numbers are kept.
    """
    user = CONFIG.get("scholar_id")
    if not user:
        return None
    key = os.environ.get("SERPAPI_KEY")
    if key:
        q = urllib.parse.urlencode({"engine": "google_scholar_author", "author_id": user, "api_key": key, "hl": "en"})
        r = get_json(f"https://serpapi.com/search.json?{q}")
        table = val(r, "cited_by", "table") or []
        flat = {k: v.get("all") for row in table for k, v in row.items() if isinstance(v, dict)}
        if flat.get("citations") is not None:
            return {"citations": flat["citations"], "h_index": flat.get("h_index", 0), "i10_index": flat.get("i10_index", 0)}
    url = f"https://scholar.google.com/citations?user={user}&hl=en"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"warn: Google Scholar: {e}", file=sys.stderr)
        return None
    cells = [int(x) for x in re.findall(r'class="gsc_rsb_std">(\d+)<', html)]
    if len(cells) < 6:
        print("warn: Google Scholar page had no citation table (likely blocked)", file=sys.stderr)
        return None
    return {"citations": cells[0], "h_index": cells[2], "i10_index": cells[4]}


def sync_metrics(n_pubs):
    old = load("metrics.json")
    m = scholar_metrics()
    if m:
        source = "Google Scholar"
    elif old.get("source") == "Google Scholar":
        m, source = {k: old[k] for k in ("citations", "h_index", "i10_index") if k in old}, "Google Scholar"
    else:
        a = openalex(f"authors/orcid:{ORCID}")
        if not a:
            return
        m = {"citations": a.get("cited_by_count", 0), "h_index": val(a, "summary_stats", "h_index") or 0}
        source = "OpenAlex"
    save("metrics.json", {"works": n_pubs, **m, "source": source})
    print(f"Metrics from {source}: {m}")
    return source


def main():
    new_pubs = sync_publications()
    new_reviews = sync_reviews()
    new_projects = sync_projects()
    source = sync_metrics(len(load("publications.json")["items"]))
    lines = [f"- New paper: {p['title']} ({p.get('venue') or p['year']})" for p in new_pubs]
    lines += [f"- New project: {p['title']}" for p in new_projects]
    if new_reviews > 0:
        lines.append(f"- {new_reviews} new peer review record(s)")
    summary = "\n".join(lines) or f"- Citation counts refreshed ({source or 'unchanged'})"
    print(summary)
    (ROOT / ".sync-summary.md").write_text(summary + "\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("## Homepage sync\n" + summary + "\n")


if __name__ == "__main__":
    main()
