#!/usr/bin/env python3
"""Export the KiCad BOM and price it with live DigiKey and Mouser data.

Usage (from the project folder):
    python3 tools/costed_bom.py [--boards 1,10] [--project hello-pcb]

Writes:
    build/<project>-bom.csv         plain BOM from kicad-cli
    build/<project>-bom-costed.csv  per-part DigiKey/Mouser prices, stock and links

Each schematic symbol needs DigiKey and Mouser fields holding the distributor
part numbers (plus Manufacturer and MPN for the report). Credentials are read
from ~/.config/distributor-api.env:
    MOUSER_API_KEY=...
    DIGIKEY_CLIENT_ID=...
    DIGIKEY_CLIENT_SECRET=...
DigiKey needs a Production app subscribed to Product Information V4.
"""
import argparse
import csv
import datetime
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ENV_FILE = os.path.expanduser("~/.config/distributor-api.env")
BOM_FIELDS = "Reference,Value,Footprint,${QUANTITY},Manufacturer,MPN,DigiKey,Mouser,Datasheet"
BOM_LABELS = "Refs,Value,Footprint,Qty,Manufacturer,MPN,DigiKey,Mouser,Datasheet"


def load_env():
    env = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        sys.exit(f"Missing {ENV_FILE}")
    return env


class Secrets:
    """Keeps credentials out of error messages."""

    def __init__(self, *values):
        self.values = [v for v in values if v]

    def scrub(self, text):
        for v in self.values:
            text = text.replace(v, "***")
        return text


def http_json(req, secrets, what):
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:400]
        sys.exit(f"{what}: HTTP {e.code}: {secrets.scrub(body)}")
    except urllib.error.URLError as e:
        sys.exit(f"{what}: {secrets.scrub(str(e))}")


def mouser_lookup(pns, key):
    secrets = Secrets(key)
    out = {}
    for pn in pns:
        body = json.dumps({"SearchByPartRequest": {"mouserPartNumber": pn, "partSearchOptions": "Exact"}}).encode()
        req = urllib.request.Request(
            "https://api.mouser.com/api/v1/search/partnumber?apiKey=" + urllib.parse.quote(key),
            data=body, headers={"Content-Type": "application/json", "Accept": "application/json"})
        r = http_json(req, secrets, f"Mouser {pn}")
        if r.get("Errors"):
            sys.exit(f"Mouser {pn}: {r['Errors']}")
        match = [p for p in (r.get("SearchResults") or {}).get("Parts", []) if p["MouserPartNumber"] == pn]
        if not match:
            sys.exit(f"Mouser {pn}: not found")
        p = match[0]
        breaks = [(b["Quantity"], float(b["Price"].replace("$", "").replace(",", ""))) for b in p["PriceBreaks"]]
        avail = p.get("Availability") or ""
        stock = int(avail.split()[0]) if avail[:1].isdigit() else 0
        out[pn] = {"breaks": breaks, "stock": stock, "link": p["ProductDetailUrl"].split("?")[0],
                   "item": f"{p['Manufacturer']} {p['ManufacturerPartNumber']}"}
    return out


def digikey_lookup(pns, cid, secret):
    secrets = Secrets(cid, secret)
    tok_req = urllib.request.Request(
        "https://api.digikey.com/v1/oauth2/token",
        data=urllib.parse.urlencode({"client_id": cid, "client_secret": secret,
                                     "grant_type": "client_credentials"}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    token = http_json(tok_req, secrets, "DigiKey token")["access_token"]
    secrets.values.append(token)
    out = {}
    for pn in pns:
        req = urllib.request.Request(
            "https://api.digikey.com/products/v4/search/%s/productdetails" % urllib.parse.quote(pn, safe=""),
            headers={"Authorization": "Bearer " + token, "X-DIGIKEY-Client-Id": cid,
                     "X-DIGIKEY-Locale-Site": "US", "X-DIGIKEY-Locale-Language": "en",
                     "X-DIGIKEY-Locale-Currency": "USD", "Accept": "application/json"})
        prod = http_json(req, secrets, f"DigiKey {pn}")["Product"]
        var = [v for v in prod["ProductVariations"] if v["DigiKeyProductNumber"] == pn]
        if not var:
            sys.exit(f"DigiKey {pn}: no matching package variation")
        v = var[0]
        out[pn] = {"breaks": [(b["BreakQuantity"], b["UnitPrice"]) for b in v["StandardPricing"]],
                   "stock": v.get("QuantityAvailableforPackageType") or 0,
                   "link": prod["ProductUrl"],
                   "item": f"{prod['Manufacturer']['Name']} {prod['ManufacturerProductNumber']}"}
    return out


def price_for(breaks, qty):
    """Unit price and quantity actually bought, honoring the first break as a minimum."""
    breaks = sorted(breaks)
    buy = max(qty, breaks[0][0])
    unit = breaks[0][1]
    for bq, bp in breaks:
        if bq <= buy:
            unit = bp
    return unit, buy


def fmt_unit(u):
    t = f"{u:.5f}".rstrip("0")
    return t if len(t.split(".")[1]) >= 2 else t + "0"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project", default="hello-pcb", help="KiCad project name (default: hello-pcb)")
    ap.add_argument("--boards", default="1,10", help="comma-separated board quantities to price (default: 1,10)")
    args = ap.parse_args()
    boards = [int(n) for n in args.boards.split(",")]

    os.makedirs("build", exist_ok=True)
    bom_csv = f"build/{args.project}-bom.csv"
    out_csv = f"build/{args.project}-bom-costed.csv"
    # kicad-cli may run inside Flatpak with a private /tmp, so keep paths inside the project.
    subprocess.run(["kicad-cli", "sch", "export", "bom", "--fields", BOM_FIELDS, "--labels", BOM_LABELS,
                    "-o", bom_csv, f"{args.project}.kicad_sch"], check=True, stdout=subprocess.DEVNULL)
    with open(bom_csv) as f:
        rows = list(csv.DictReader(f))
    missing = [r["Refs"] for r in rows if not (r.get("DigiKey") and r.get("Mouser"))]
    if missing:
        sys.exit(f"Missing DigiKey/Mouser fields on: {', '.join(missing)}")

    env = load_env()
    for k in ("MOUSER_API_KEY", "DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"):
        if not env.get(k):
            sys.exit(f"{k} not set in {ENV_FILE}")
    dists = {
        "DigiKey": digikey_lookup(sorted({r["DigiKey"] for r in rows}), env["DIGIKEY_CLIENT_ID"],
                                  env["DIGIKEY_CLIENT_SECRET"]),
        "Mouser": mouser_lookup(sorted({r["Mouser"] for r in rows}), env["MOUSER_API_KEY"]),
    }

    def label(n):
        return f"{n} board{'s' * (n > 1)}"

    cols = ["Refs", "Value", "Qty/board", "Manufacturer", "MPN"]
    for d in dists:
        cols += [f"{d} PN", f"{d} item", f"{d} stock"]
        for n in boards:
            cols += [f"{d} unit @{label(n)} (USD)", f"{d} total {label(n)} (USD)"]
    cols += [f"Cheaper @{label(boards[0])}"] + [f"{d} link" for d in dists]

    totals = {}
    out = []
    for r in rows:
        q = int(r["Qty"])
        o = {"Refs": r["Refs"], "Value": r["Value"], "Qty/board": q,
             "Manufacturer": r["Manufacturer"], "MPN": r["MPN"]}
        first = {}
        for d, data in dists.items():
            info = data[r[d]]
            o[f"{d} PN"], o[f"{d} stock"], o[f"{d} link"] = r[d], info["stock"], info["link"]
            o[f"{d} item"] = info["item"]  # what this distributor actually sells (may differ from MPN)
            for n in boards:
                unit, buy = price_for(info["breaks"], q * n)
                ext = unit * buy
                o[f"{d} unit @{label(n)} (USD)"] = fmt_unit(unit)
                o[f"{d} total {label(n)} (USD)"] = f"{ext:.2f}"
                totals[(d, n)] = totals.get((d, n), 0) + ext
                if n == boards[0]:
                    first[d] = round(ext, 4)
        best = min(first.values())
        winners = [d for d, v in first.items() if v == best]
        o[f"Cheaper @{label(boards[0])}"] = winners[0] if len(winners) == 1 else "same"
        out.append(o)

    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(out)
        t = {c: "" for c in cols}
        t["Refs"] = "TOTAL"
        for (d, n), v in totals.items():
            t[f"{d} total {label(n)} (USD)"] = f"{v:.2f}"
        t[f"{next(iter(dists))} link"] = (f"Live prices from DigiKey Product Information v4 and Mouser Search APIs, "
                                         f"{datetime.date.today().isoformat()}. Excludes shipping and tax.")
        w.writerow(t)

    print(f"Wrote {bom_csv} and {out_csv}")
    for n in boards:
        print(f"  {label(n):>9}: " + "  ".join(f"{d} ${totals[(d, n)]:.2f}" for d in dists))
    low = [f"{r['Refs']} ({d} {dists[d][r[d]]['stock']})" for r in rows for d in dists
           if dists[d][r[d]]["stock"] < int(r["Qty"]) * max(boards)]
    if low:
        print("  Low stock: " + ", ".join(low))


if __name__ == "__main__":
    main()
