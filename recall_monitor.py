import os
import json
import io
import zipfile
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
import requests
import pandas as pd

NHTSA_ZIP_URL = "https://static.nhtsa.gov/odi/ffdd/rcl/FLAT_RCL_POST_2010.zip"

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

COLUMNS = [
    "RECORD_ID", "CAMPNO", "MAKETXT", "MODELTXT", "YEARTXT",
    "MFGCAMPNO", "COMPNAME", "MFGNAME", "BGMAN", "ENDMAN",
    "RCLTYPECD", "POTAFF", "ODATE", "INFLUENCED_BY", "MFGTXT",
    "RCDATE", "DATEA", "RPNO", "FMVSS", "DESC_DEFECT",
    "CONEQUENCE_DEFECT", "CORRECTIVE_ACTION", "NOTES", "RCL_CMPT_ID",
    "MFR_COMP_NAME", "MFR_COMP_DESC", "MFR_COMP_PTNO",
    "DO_NOT_DRIVE", "PARK_OUTSIDE",
]

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
            on_bad_lines="skip",
        )

    df["MAKETXT"] = df["MAKETXT"].str.upper().str.strip()
    df = df[df["MAKETXT"].isin(MAJOR_MAKES)]

    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
    df = df[df["RCDATE"].fillna("") >= cutoff]
    df = df.drop_duplicates(subset=["CAMPNO"])

    new_recalls = df[~df["CAMPNO"].isin(seen_campnos)].to_dict("records")
    seen_campnos.update(df["CAMPNO"].tolist())

    return new_recalls, seen_campnos, current_etag


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


def build_email(recalls):
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

    n = len(recalls)
    return f"""<html><body style="font-family:sans-serif;font-size:14px;max-width:960px;margin:0 auto">
<h2 style="color:#c0392b;margin-bottom:4px">&#9888; NHTSA Recall Alert &mdash; {n} new filing{'s' if n != 1 else ''}</h2>
<p style="color:#888;margin-top:0">{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</p>
<table border="0" cellpadding="0" cellspacing="0"
  style="border-collapse:collapse;width:100%;border:1px solid #ddd;font-size:13px">
  <tr style="background:#f2f2f2;font-size:11px;text-transform:uppercase">
    <th style="padding:6px 10px;text-align:left;min-width:160px">Campaign</th>
    <th style="padding:6px 10px;text-align:left">Filed</th>
    <th style="padding:6px 10px;text-align:left">Affected</th>
    <th style="padding:6px 10px;text-align:left">Details</th>
  </tr>
  {''.join(rows)}
</table>
<p style="font-size:11px;color:#bbb;margin-top:24px">
  <a href="https://www.nhtsa.gov/recalls">NHTSA Recalls Database</a>
</p>
</body></html>"""


def send_email(html, count):
    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASS"]
    recipients = [e.strip() for e in os.environ["NOTIFY_EMAILS"].split(",")]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Recall Alert] {count} new NHTSA filing{'s' if count != 1 else ''}"
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.sendmail(user, recipients, msg.as_string())
    print(f"Email sent to: {', '.join(recipients)}")


def main():
    state = load_state()

    print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] Checking NHTSA flat file...")
    new_recalls, seen_campnos, new_etag = check_nhtsa(state)
    print(f"New NHTSA filings: {len(new_recalls)}")

    if new_recalls:
        html = build_email(new_recalls)
        send_email(html, len(new_recalls))
    else:
        print("Nothing to send.")

    state["nhtsa_etag"] = new_etag
    state["seen_campnos"] = sorted(seen_campnos)
    save_state(state)


if __name__ == "__main__":
    main()
