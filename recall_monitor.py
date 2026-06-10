import os
import json
import io
import zipfile
import smtplib
import hashlib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
import requests
import pandas as pd
import feedparser

NHTSA_ZIP_URL = "https://static.nhtsa.gov/odi/ffdd/rcl/FLAT_RCL_POST_2010.zip"

# Exact MAKETXT values used in the NHTSA flat file
MAJOR_MAKES = {
    "FORD", "LINCOLN",
    "CHEVROLET", "GMC", "BUICK", "CADILLAC",
    "CHRYSLER", "DODGE", "JEEP", "RAM", "FIAT", "ALFA ROMEO",
    "TOYOTA", "LEXUS",
    "HONDA", "ACURA",
    "HYUNDAI", "KIA", "GENESIS",
    "VOLKSWAGEN", "AUDI", "PORSCHE",
    "BMW", "MINI",
    "MERCEDES-BENZ",
    "TESLA",
    "RIVIAN",
}

# Column names from NHTSA data dictionary (tab-delimited, no header row)
COLUMNS = [
    "RECORD_ID", "CAMPNO", "MAKETXT", "MODELTXT", "YEARTXT",
    "MFGCAMPNO", "COMPNAME", "MFGNAME", "BGMAN", "ENDMAN",
    "RCLTYPECD", "POTAFF", "ODATE", "INFLUENCED_BY", "MFGTXT",
    "RCDATE", "DATEA", "RPNO", "FMVSS", "DESC_DEFECT",
    "CONEQUENCE_DEFECT", "CORRECTIVE_ACTION", "NOTES", "RCL_CMPT_ID",
    "MFR_COMP_NAME", "MFR_COMP_DESC", "MFR_COMP_PTNO",
    "DO_NOT_DRIVE", "PARK_OUTSIDE",
]

# Google News OEM feeds — catches press releases before NHTSA filing lands
OEM_NEWS_FEEDS = {
    "Ford":       "https://news.google.com/rss/search?q=ford+vehicle+recall&hl=en-US&gl=US&ceid=US:en",
    "GM":         "https://news.google.com/rss/search?q=general+motors+vehicle+recall&hl=en-US&gl=US&ceid=US:en",
    "Stellantis": "https://news.google.com/rss/search?q=stellantis+vehicle+recall&hl=en-US&gl=US&ceid=US:en",
    "Toyota":     "https://news.google.com/rss/search?q=toyota+vehicle+recall&hl=en-US&gl=US&ceid=US:en",
    "Honda":      "https://news.google.com/rss/search?q=honda+vehicle+recall&hl=en-US&gl=US&ceid=US:en",
    "Hyundai":    "https://news.google.com/rss/search?q=hyundai+vehicle+recall&hl=en-US&gl=US&ceid=US:en",
    "Kia":        "https://news.google.com/rss/search?q=kia+vehicle+recall&hl=en-US&gl=US&ceid=US:en",
    "BMW":        "https://news.google.com/rss/search?q=bmw+vehicle+recall&hl=en-US&gl=US&ceid=US:en",
    "Mercedes":   "https://news.google.com/rss/search?q=mercedes-benz+vehicle+recall&hl=en-US&gl=US&ceid=US:en",
    "VW":         "https://news.google.com/rss/search?q=volkswagen+vehicle+recall&hl=en-US&gl=US&ceid=US:en",
    "Tesla":      "https://news.google.com/rss/search?q=tesla+vehicle+recall&hl=en-US&gl=US&ceid=US:en",
    "Rivian":     "https://news.google.com/rss/search?q=rivian+vehicle+recall&hl=en-US&gl=US&ceid=US:en",
}

RECALL_KEYWORDS = {"recall", "safety", "defect", "remedy", "nhtsa", "campaign"}
STATE_FILE = "seen_items.json"


def load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def check_nhtsa(state):
    seen_campnos = set(state.get("seen_campnos", []))
    last_etag = state.get("nhtsa_etag", "")

    head = requests.head(NHTSA_ZIP_URL, timeout=30)
    current_etag = head.headers.get("etag", "")

    if current_etag and current_etag == last_etag:
        print("NHTSA flat file unchanged (ETag match). Skipping download.")
        return [], seen_campnos, last_etag

    print("NHTSA flat file updated. Downloading (14MB)...")
    resp = requests.get(NHTSA_ZIP_URL, timeout=180)
    resp.raise_for_status()

    z = zipfile.ZipFile(io.BytesIO(resp.content))
    with z.open(z.namelist()[0]) as f:
        df = pd.read_csv(
            f, sep="\t", header=None, names=COLUMNS,
            encoding="latin-1", dtype=str, low_memory=False,
            on_bad_lines="skip",  # some text fields contain embedded tabs
        )

    df["MAKETXT"] = df["MAKETXT"].str.upper().str.strip()
    df = df[df["MAKETXT"].isin(MAJOR_MAKES)]

    # 7-day lookback window; dedup by CAMPNO handles re-alerts
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
    df = df[df["RCDATE"].fillna("") >= cutoff]
    df = df.drop_duplicates(subset=["CAMPNO"])

    new_recalls = df[~df["CAMPNO"].isin(seen_campnos)].to_dict("records")
    seen_campnos.update(df["CAMPNO"].tolist())

    return new_recalls, seen_campnos, current_etag


def check_news_feed(url, label, seen_ids):
    new_items = []
    try:
        feed = feedparser.parse(url, request_headers={"User-Agent": "recall-monitor/1.0"})
        if feed.bozo and not feed.entries:
            print(f"[WARN] {label}: {feed.bozo_exception}")
            return None
        for entry in feed.entries:
            raw = entry.get("id") or entry.get("link") or entry.get("title", "")
            eid = hashlib.md5(raw.encode()).hexdigest()
            if eid in seen_ids:
                continue
            text = (entry.get("title", "") + " " + entry.get("summary", "")).lower()
            if not any(kw in text for kw in RECALL_KEYWORDS):
                continue
            new_items.append({
                "source": label,
                "title": entry.get("title", "(no title)"),
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "id": eid,
            })
    except Exception as e:
        print(f"[WARN] {label}: {e}")
        return None
    return new_items


def fmt_potaff(val):
    try:
        return f"{int(float(val or 0)):,}"
    except (ValueError, TypeError):
        return val or "N/A"


def fmt_date(val):
    try:
        return datetime.strptime(str(val).strip(), "%Y%m%d").strftime("%Y-%m-%d")
    except Exception:
        return val or ""


def build_nhtsa_section(recalls):
    rows = []
    for r in recalls:
        campno = r.get("CAMPNO", "")
        nhtsa_url = f"https://www.nhtsa.gov/vehicle/recalls#%21?nhtsaId={campno}"
        flags = ""
        if str(r.get("DO_NOT_DRIVE", "")).upper() in ("YES", "Y"):
            flags += '<b style="color:red">⚠ DO NOT DRIVE</b>&nbsp;'
        if str(r.get("PARK_OUTSIDE", "")).upper() in ("YES", "Y"):
            flags += '<b style="color:orange">⚠ PARK OUTSIDE</b>'

        defect = (r.get("DESC_DEFECT") or "")[:350]
        consequence = (r.get("CONEQUENCE_DEFECT") or "")[:200]

        rows.append(f"""
<tr style="border-top:1px solid #ddd;vertical-align:top">
  <td style="padding:8px 10px">
    {flags}{'<br>' if flags else ''}
    <b><a href="{nhtsa_url}">{campno}</a></b><br>
    <b>{r.get('MAKETXT','')} {r.get('MODELTXT','')}</b> ({r.get('YEARTXT','')})<br>
    <small style="color:#666">{r.get('COMPNAME','')}</small>
  </td>
  <td style="padding:8px 10px;white-space:nowrap">{fmt_date(r.get('RCDATE',''))}</td>
  <td style="padding:8px 10px;white-space:nowrap">{fmt_potaff(r.get('POTAFF',''))} vehicles</td>
  <td style="padding:8px 10px;font-size:12px">
    <b>Defect:</b> {defect}<br><br>
    <b>Consequence:</b> {consequence}
  </td>
</tr>""")

    return f"""
<h3 style="margin-top:0;color:#c0392b">NHTSA Official Filings &mdash; {len(recalls)} new</h3>
<table border="0" cellpadding="0" cellspacing="0"
  style="border-collapse:collapse;width:100%;border:1px solid #ddd;font-size:13px">
  <tr style="background:#f2f2f2;font-size:11px;text-transform:uppercase">
    <th style="padding:6px 10px;text-align:left;min-width:160px">Campaign</th>
    <th style="padding:6px 10px;text-align:left">Filed</th>
    <th style="padding:6px 10px;text-align:left">Affected</th>
    <th style="padding:6px 10px;text-align:left">Details</th>
  </tr>
  {''.join(rows)}
</table>"""


def build_news_section(news_items):
    rows = []
    for item in news_items:
        rows.append(f"""
<tr style="border-top:1px solid #ddd">
  <td style="padding:6px 10px;white-space:nowrap"><b>{item['source']}</b></td>
  <td style="padding:6px 10px"><a href="{item['link']}">{item['title']}</a></td>
  <td style="padding:6px 10px;white-space:nowrap;color:#666">{item['published']}</td>
</tr>""")

    return f"""
<h3 style="color:#2980b9">Press Coverage / Early Signals &mdash; {len(news_items)} new</h3>
<table border="0" cellpadding="0" cellspacing="0"
  style="border-collapse:collapse;width:100%;border:1px solid #ddd;font-size:13px">
  <tr style="background:#f2f2f2;font-size:11px;text-transform:uppercase">
    <th style="padding:6px 10px;text-align:left">Brand</th>
    <th style="padding:6px 10px;text-align:left">Headline</th>
    <th style="padding:6px 10px;text-align:left">Published</th>
  </tr>
  {''.join(rows)}
</table>"""


def send_email(subject, html):
    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASS"]
    recipients = [e.strip() for e in os.environ["NOTIFY_EMAILS"].split(",")]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.sendmail(user, recipients, msg.as_string())
    print(f"Email sent to: {', '.join(recipients)}")


def main():
    state = load_state()

    # --- NHTSA flat file (primary source) ---
    print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] Checking NHTSA flat file...")
    nhtsa_recalls, seen_campnos, new_etag = check_nhtsa(state)
    print(f"New NHTSA filings: {len(nhtsa_recalls)}")

    # --- Google News OEM feeds (early warning: press releases before NHTSA filing) ---
    seen_news_ids = set(state.get("seen_news_ids", []))
    all_news = []
    for label, url in OEM_NEWS_FEEDS.items():
        print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] Checking {label}...")
        results = check_news_feed(url, label, seen_news_ids)
        if results:
            all_news += results
    print(f"New press coverage items: {len(all_news)}")

    # --- Email ---
    if nhtsa_recalls or all_news:
        parts = []
        if nhtsa_recalls:
            n = len(nhtsa_recalls)
            parts.append(f"{n} NHTSA filing{'s' if n != 1 else ''}")
        if all_news:
            n = len(all_news)
            parts.append(f"{n} press item{'s' if n != 1 else ''}")
        subject = f"[Recall Alert] {' + '.join(parts)}"

        sections = []
        if nhtsa_recalls:
            sections.append(build_nhtsa_section(nhtsa_recalls))
        if all_news:
            sections.append(build_news_section(all_news))

        html = f"""<html><body style="font-family:sans-serif;font-size:14px;max-width:960px;margin:0 auto">
<h2 style="color:#c0392b;margin-bottom:4px">&#9888; Automotive Recall Alert</h2>
<p style="color:#888;margin-top:0">{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</p>
{'<br>'.join(sections)}
<p style="font-size:11px;color:#bbb;margin-top:24px">
  <a href="https://www.nhtsa.gov/recalls">NHTSA Recalls Database</a>
</p>
</body></html>"""

        send_email(subject, html)
    else:
        print("Nothing to send.")

    # --- Persist state ---
    seen_news_ids.update(item["id"] for item in all_news)
    state["nhtsa_etag"] = new_etag
    state["seen_campnos"] = sorted(seen_campnos)
    state["seen_news_ids"] = sorted(seen_news_ids)
    save_state(state)


if __name__ == "__main__":
    main()
