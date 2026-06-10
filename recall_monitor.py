import os
import json
import smtplib
import hashlib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import feedparser

# NHTSA official recall feed
NHTSA_FEED = "https://www.nhtsa.gov/rss/recalls"

# OEM newsroom feeds — items are filtered for recall keywords
OEM_FEEDS = {
    "Ford":       "https://media.ford.com/content/fordmedia/fna/us/en.rss.html",
    "GM":         "https://media.gm.com/rss/media-gm.rss",
    "Stellantis": "https://media.stellantis.com/en-us/feed",
    "Toyota":     "https://pressroom.toyota.com/category/pressreleases/feed/",
    "Honda":      "https://hondanews.com/en-US/releases/feed",
    "Hyundai":    "https://www.hyundainewsusa.com/feed/",
    "Kia":        "https://www.kiamedia.com/us/en/feed",
    "BMW":        "https://www.press.bmwgroup.com/usa/feed",
    "Mercedes":   "https://media.mercedes-benz.com/us/feed",
    "VW":         "https://media.vw.com/en-us/feed",
    "Tesla":      "https://www.tesla.com/blog/feed",
    "Rivian":     "https://rivian.com/newsroom/feed",
}

RECALL_KEYWORDS = {"recall", "safety", "defect", "remedy", "nhtsa", "campaign", "reimbursement"}

STATE_FILE = "seen_items.json"


def load_seen():
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(ids):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(ids), f, indent=2)


def item_id(entry):
    raw = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.md5(raw.encode()).hexdigest()


def is_recall_related(entry):
    text = (entry.get("title", "") + " " + entry.get("summary", "")).lower()
    return any(kw in text for kw in RECALL_KEYWORDS)


def check_feed(url, label, seen, require_keyword=False):
    new_items = []
    try:
        feed = feedparser.parse(url, request_headers={"User-Agent": "recall-monitor/1.0"})
        if feed.bozo and not feed.entries:
            print(f"[WARN] {label}: feed error — {feed.bozo_exception}")
            return []
        for entry in feed.entries:
            eid = item_id(entry)
            if eid in seen:
                continue
            if require_keyword and not is_recall_related(entry):
                continue
            new_items.append({
                "source": label,
                "title": entry.get("title", "(no title)"),
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "summary": entry.get("summary", "")[:400],
                "id": eid,
            })
    except Exception as e:
        print(f"[WARN] {label}: {e}")
    return new_items


def build_email(items):
    rows = []
    for item in items:
        summary_html = f"<br><small style='color:#555'>{item['summary']}</small>" if item["summary"] else ""
        rows.append(f"""
<tr>
  <td style="white-space:nowrap;padding:6px 10px"><b>{item['source']}</b></td>
  <td style="padding:6px 10px"><a href="{item['link']}">{item['title']}</a>{summary_html}</td>
  <td style="white-space:nowrap;padding:6px 10px;color:#555">{item['published']}</td>
</tr>""")

    n = len(items)
    return f"""<html><body style="font-family:sans-serif;font-size:14px">
<h2 style="color:#c0392b">&#9888; Automotive Recall Alert</h2>
<p><b>{n} new item{'s' if n != 1 else ''}</b> detected &mdash; {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</p>
<table border="0" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;border:1px solid #ddd">
  <tr style="background:#f2f2f2;font-size:12px;text-transform:uppercase">
    <th style="padding:6px 10px;text-align:left">Source</th>
    <th style="padding:6px 10px;text-align:left">Title</th>
    <th style="padding:6px 10px;text-align:left">Published</th>
  </tr>
  {''.join(rows)}
</table>
<p style="font-size:12px;color:#888;margin-top:16px">
  <a href="https://www.nhtsa.gov/recalls">NHTSA Recalls Database</a>
</p>
</body></html>"""


def send_email(html, count):
    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASS"]
    recipients = [e.strip() for e in os.environ["NOTIFY_EMAILS"].split(",")]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Recall Alert] {count} new item{'s' if count != 1 else ''}"
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.sendmail(user, recipients, msg.as_string())
    print(f"Email sent to: {', '.join(recipients)}")


def main():
    seen = load_seen()
    all_new = []

    print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] Checking NHTSA feed...")
    all_new += check_feed(NHTSA_FEED, "NHTSA", seen, require_keyword=False)

    for label, url in OEM_FEEDS.items():
        print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] Checking {label} newsroom...")
        all_new += check_feed(url, f"{label} Newsroom", seen, require_keyword=True)

    print(f"New items found: {len(all_new)}")

    if all_new:
        html = build_email(all_new)
        send_email(html, len(all_new))
        seen.update(item["id"] for item in all_new)
        save_seen(seen)
    else:
        print("Nothing to send.")


if __name__ == "__main__":
    main()
