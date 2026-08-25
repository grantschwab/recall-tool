import os
import json
import io
import zipfile
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
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
TIMING_LOG = "nhtsa_timing_log.jsonl"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Timing log
# ---------------------------------------------------------------------------

def append_log(entry):
    with open(TIMING_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def load_log():
    entries = []
    try:
        with open(TIMING_LOG) as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    except FileNotFoundError:
        pass
    return entries


def print_timing_summary():
    entries = load_log()
    if not entries:
        return

    checks = [e for e in entries if e.get("event") == "file_check"]
    detections = [e for e in entries if e.get("event") == "recall_detected"]

    print("\n--- NHTSA Timing Summary ---")
    print(f"Total file checks logged: {len(checks)}")

    if len(checks) >= 2:
        # Compute average hours between flat file updates
        update_events = [c for c in checks if c.get("etag_changed")]
        print(f"File updates observed: {len(update_events)}")
        if len(update_events) >= 2:
            times = sorted(
                datetime.fromisoformat(e["ts"]) for e in update_events
            )
            gaps = [(times[i+1] - times[i]).total_seconds() / 3600
                    for i in range(len(times) - 1)]
            avg_gap = sum(gaps) / len(gaps)
            print(f"Avg hours between NHTSA updates: {avg_gap:.1f}h")

    if detections:
        print(f"\nRecalls detected: {len(detections)}")
        print(f"{'Campaign':<14} {'Make':<12} {'Filed':<12} {'Detected':<22} {'Lag (hrs)'}")
        print("-" * 75)
        for d in sorted(detections, key=lambda x: x["detected_at"], reverse=True)[:20]:
            lag = d.get("lag_hours", "?")
            lag_str = f"{lag:.1f}" if isinstance(lag, float) else str(lag)
            print(
                f"{d.get('campno',''):<14} "
                f"{d.get('make',''):<12} "
                f"{d.get('rcdate',''):<12} "
                f"{d.get('detected_at','')[:19]:<22} "
                f"{lag_str}"
            )
    print("--- End Summary ---\n")


# ---------------------------------------------------------------------------
# NHTSA flat file
# ---------------------------------------------------------------------------

def check_nhtsa(state, now_utc, force=False):
    seen_campnos = set(state.get("seen_campnos", []))
    last_etag = state.get("nhtsa_etag", "")

    head = requests.head(NHTSA_ZIP_URL, timeout=30)
    current_etag = head.headers.get("etag", "")
    last_modified = head.headers.get("last-modified", "")
    print(f"  ETag      : {current_etag}")
    print(f"  Last-Modified: {last_modified}")

    etag_changed = current_etag != last_etag

    if not etag_changed and not force:
        print("  Flat file unchanged. Skipping download.")
        append_log({
            "event": "file_check",
            "ts": now_utc,
            "last_modified": last_modified,
            "etag_changed": False,
            "new_recalls_found": 0,
        })
        return [], seen_campnos, current_etag

    if force and not etag_changed:
        print("  FORCE mode: downloading despite unchanged ETag.")

    print("  Flat file updated — downloading (14MB)...")
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

    # Write CSV: one row per campaign (2020-present), newest first.
    # Multiple models/years per campaign are joined as comma-separated values.
    # Text columns excluded; full details one click away via NHTSA_URL.
    csv_df = df[df["RCDATE"].fillna("") >= "20200101"].copy()
    agg = csv_df.groupby("CAMPNO").agg(
        MAKES=("MAKETXT", lambda x: ", ".join(sorted(set(x)))),
        MODELS=("MODELTXT", lambda x: ", ".join(sorted(set(x.dropna())))),
        YEARS=("YEARTXT", lambda x: ", ".join(sorted(set(x.dropna())))),
        COMPNAME=("COMPNAME", "first"),
        RCDATE=("RCDATE", "first"),
        ODATE=("ODATE", "first"),
        POTAFF=("POTAFF", "first"),
        INFLUENCED_BY=("INFLUENCED_BY", "first"),
        DO_NOT_DRIVE=("DO_NOT_DRIVE", "first"),
        PARK_OUTSIDE=("PARK_OUTSIDE", "first"),
    ).reset_index()
    agg["NHTSA_URL"] = "https://www.nhtsa.gov/vehicle/recalls#?nhtsaId=" + agg["CAMPNO"]
    agg = agg.sort_values("RCDATE", ascending=False)
    col_order = [
        "CAMPNO", "NHTSA_URL", "MAKES", "MODELS", "YEARS", "COMPNAME",
        "RCDATE", "ODATE", "POTAFF", "INFLUENCED_BY", "DO_NOT_DRIVE", "PARK_OUTSIDE",
    ]
    agg[col_order].to_csv("recalls_2020_present.csv", index=False)
    print(f"  CSV written: {len(agg):,} campaigns (2020-present, major OEMs)")

    # No recency cutoff here: NHTSA sometimes doesn't publish a campaign to the
    # flat file until well over a week after its filing date, and gating
    # "new" detection on a rolling window would let those slip through
    # unnoticed forever. seen_campnos alone is what prevents re-notification.
    df = df[df["RCDATE"].fillna("") >= "20200101"]
    df = df.drop_duplicates(subset=["CAMPNO"])

    if force:
        # Test mode: send a small sample of the most recent campaigns as a
        # test email, regardless of seen state. Not the full 2020-present set
        # now that detection isn't windowed to the last 7 days.
        new_recalls = df.sort_values("RCDATE", ascending=False).head(5).to_dict("records")
    else:
        new_recalls = df[~df["CAMPNO"].isin(seen_campnos)].to_dict("records")
    seen_campnos.update(df["CAMPNO"].tolist())

    # Log this file update
    append_log({
        "event": "file_check",
        "ts": now_utc,
        "last_modified": last_modified,
        "etag_changed": True,
        "new_recalls_found": len(new_recalls),
    })

    # Log each new recall with filing-to-detection lag
    for r in new_recalls:
        rcdate_str = str(r.get("RCDATE", "")).strip()
        lag_hours = None
        try:
            rcdate_dt = datetime.strptime(rcdate_str, "%Y%m%d").replace(
                tzinfo=timezone.utc
            )
            now_dt = datetime.fromisoformat(now_utc)
            lag_hours = round((now_dt - rcdate_dt).total_seconds() / 3600, 1)
        except Exception:
            pass

        append_log({
            "event": "recall_detected",
            "ts": now_utc,
            "campno": r.get("CAMPNO", ""),
            "make": r.get("MAKETXT", ""),
            "model": r.get("MODELTXT", ""),
            "year": r.get("YEARTXT", ""),
            "rcdate": rcdate_str,
            "odate": r.get("ODATE", ""),
            "potaff": r.get("POTAFF", ""),
            "detected_at": now_utc,
            "flat_file_modified": last_modified,
            "lag_hours": lag_hours,
        })

    return new_recalls, seen_campnos, current_etag


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

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


def build_email(recalls, force=False):
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
    banner = ""
    if force:
        banner = """<p style="background:#fff3cd;color:#856404;padding:8px 12px;border-radius:4px;margin:0 0 12px 0">
&#9888; This is a manually triggered test run, not a live recall alert.
</p>"""
    return f"""<html><body style="font-family:sans-serif;font-size:14px;max-width:960px;margin:0 auto">
<h2 style="color:#c0392b;margin-bottom:4px">&#9888; NHTSA Recall Alert &mdash; {n} new filing{'s' if n != 1 else ''}</h2>
<p style="color:#888;margin-top:0">{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</p>
{banner}
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
<p style="font-size:12px;color:#888;margin-top:24px">
  <a href="https://github.com/grantschwab/recall-tool/blob/master/recalls_2020_present.csv">&#128202; Browse all recalls (2020–present)</a>
  &nbsp;&nbsp;|&nbsp;&nbsp;
  <a href="https://raw.githubusercontent.com/grantschwab/recall-tool/master/recalls_2020_present.csv">&#11015; Download CSV</a>
  &nbsp;&nbsp;|&nbsp;&nbsp;
  <a href="https://www.nhtsa.gov/recalls">NHTSA Recalls Database</a>
</p>
</body></html>"""


def send_email(html, count, force=False):
    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASS"]
    recipients = [e.strip() for e in os.environ["NOTIFY_EMAILS"].split(",")]
    msg = MIMEMultipart("alternative")
    prefix = "[TEST] " if force else ""
    msg["Subject"] = f"{prefix}[Recall Alert] {count} new NHTSA filing{'s' if count != 1 else ''}"
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.sendmail(user, recipients, msg.as_string())
    print(f"Email sent to: {', '.join(recipients)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    now_utc = datetime.now(timezone.utc).isoformat()
    state = load_state()

    force = os.environ.get("FORCE_EMAIL", "").lower() == "true"
    if force:
        print("*** FORCE MODE — will email regardless of ETag or seen state ***")

    print(f"[{now_utc[:19]}Z] Checking NHTSA flat file...")
    new_recalls, seen_campnos, new_etag = check_nhtsa(state, now_utc, force=force)
    print(f"New NHTSA filings: {len(new_recalls)}")

    if new_recalls:
        html = build_email(new_recalls, force=force)
        send_email(html, len(new_recalls), force=force)
    else:
        print("Nothing to send.")

    state["nhtsa_etag"] = new_etag
    state["seen_campnos"] = sorted(seen_campnos)
    save_state(state)

    print_timing_summary()


if __name__ == "__main__":
    main()
