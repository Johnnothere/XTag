"""
adint.py — advertising intelligence as a first-class XTag signal.

The idea
--------
Every source XTag collects today is ORGANIC: what people said. Ad transparency
archives are the only source that records what someone PAID for. Paid records
carry four things organic content structurally cannot:

    a payer        a named legal person, with a billing address
    a spend        resourcing, in money
    a targeting    the operator's own model of who is persuadable
    a date range   a campaign with hard edges, not a fuzzy narrative

The join key between the two worlds is the LANDING HOST. XTag already
canonicalises destination URLs in coordination.py; ad archives publish the
destination too. Nobody in the published literature has used it as a bridge.

Sources, in the order they are worth building
---------------------------------------------
  snap        free, no auth, bulk ZIP, EXACT spend and impressions, named
              interest targeting, and the only archive with real Gulf coverage
  microsoft   free, no auth, OData, per-country impression share
  tiktok      free, 2-day approval, richest structured targeting, EU-32 only
  meta        business verification required, EU/UK commercial only
  google      BigQuery political dataset, effectively US-only since the EU exit

Only `snap` is implemented here. The AdRecord schema is designed to absorb the
others: every archive expresses include/exclude targeting differently, so all of
them collapse to {dimension, operator, values}.

Contract
--------
Same as the rest of XTag: nothing raises, failures are named, absence is
reported rather than zeroed.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import datetime
from urllib.parse import urlsplit

try:
    import requests
except Exception:
    requests = None

SNAP_URL = "https://storage.googleapis.com/ad-manager-political-ads-dump/political/%s/PoliticalAds.zip"

_URL_RE = re.compile(r"https?://[^\s,\"'\\]+")
_WEBVIEW_RE = re.compile(r"web_view_url\s*:\s*(\S+)")

# Query keys stripped when canonicalising a landing URL. Mirrors
# coordination.py — the two must agree or the join silently fails.
_TRACKING = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "igshid", "si", "ref", "spm", "mc_cid", "mc_eid",
    "msclkid", "twclid", "ttclid", "scid", "snap_click_id",
}


def canon_host(url):
    """Canonical host. Must match coordination.py's rule exactly."""
    try:
        h = (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""
    for p in ("www.", "amp.", "m.", "mobile."):
        if h.startswith(p):
            h = h[len(p):]
    return h


def _ints(s):
    try:
        return int(float(str(s or "0").replace(",", "").strip() or 0))
    except Exception:
        return 0


def _dt(s):
    s = (s or "").strip()
    for fmt in ("%Y/%m/%d %H:%M:%S%z", "%Y/%m/%d %H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.replace("Z", "+0000") if fmt.endswith("%z") else s, fmt)
        except Exception:
            continue
    return None


def _split(s):
    return [v.strip() for v in (s or "").split(",") if v.strip()]


# --------------------------------------------------------------------------
# The common record
# --------------------------------------------------------------------------

def ad_record(**kw):
    """One advertisement, normalised across archives.

    `targeting` is a list of {dimension, operator, values}. Every archive has a
    different idiom for exclusion — Snap uses paired (Included)/(Excluded)
    columns, TikTok a single audience_targeting_exclude list, LinkedIn
    included[]/excluded[] per category, Microsoft a UsedForExclusion boolean
    with no values at all, Google separate geo_targeting_included/_excluded.
    All five collapse into this shape.
    """
    r = {
        "source": None, "ad_id": None,
        "advertiser": None, "payer": None,
        "billing_address": None, "billing_country": None,
        "spend": None, "currency": None, "spend_is_range": False,
        "impressions": None,
        "start": None, "stop": None,
        "countries": [], "languages": [], "age": None, "genders": [],
        "targeting": [],
        "landing_urls": [], "landing_hosts": [],
        "creative_url": None, "creative_text": None,
    }
    r.update(kw)
    return r


def _t(dim, op, values):
    return {"dimension": dim, "operator": op, "values": values}


# --------------------------------------------------------------------------
# Snap
# --------------------------------------------------------------------------

# (column, dimension) — parsed as paired Included/Excluded where both exist.
_SNAP_PAIRED = [
    ("Regions", "region"),
    ("Electoral Districts", "electoral_district"),
    ("Radius Targeting", "radius"),
    ("Metros", "metro"),
    ("Postal Codes", "postal_code"),
    ("Location Categories", "location_category"),
]
_SNAP_FLAT = [
    ("Interests", "interest"),
    ("Segments", "audience_segment"),
    ("AdvancedDemographics", "third_party_demographic"),
    ("OsType", "os"),
    ("Targeting Connection Type", "connection"),
    ("Targeting Carrier (ISP)", "carrier"),
]


def parse_snap_csv(text_or_file):
    """Parse a Snap PoliticalAds.csv into AdRecords.

    Column names drift between years ('Postal Code' vs 'Postal Codes',
    'Target Connection Type' vs 'Targeting Connection Type'), so everything is
    read from the header row rather than hardcoded.
    """
    fh = io.StringIO(text_or_file) if isinstance(text_or_file, str) else text_or_file
    out = []
    for row in csv.DictReader(fh):
        cp = row.get("CreativeProperties") or ""
        urls = _WEBVIEW_RE.findall(cp) or _URL_RE.findall(cp)
        hosts = sorted({h for h in (canon_host(u) for u in urls) if h})

        tgt = []
        for base, dim in _SNAP_PAIRED:
            for col, op in ((f"{base} (Included)", "include"), (f"{base} (Excluded)", "exclude"),
                            (f"{base[:-1]} (Included)", "include"), (f"{base[:-1]} (Excluded)", "exclude")):
                vals = _split(row.get(col))
                if vals:
                    tgt.append(_t(dim, op, vals))
        for col, dim in _SNAP_FLAT:
            vals = _split(row.get(col)) or _split(row.get(col.replace("Targeting ", "Target ")))
            if vals:
                tgt.append(_t(dim, "include", vals))

        addr = (row.get("BillingAddress") or "").strip()
        out.append(ad_record(
            source="snap",
            ad_id=(row.get("ADID") or "").strip(),
            advertiser=(row.get("OrganizationName") or "").strip() or None,
            payer=(row.get("PayingAdvertiserName") or "").strip() or None,
            billing_address=addr or None,
            billing_country=addr.rsplit(",", 1)[-1].strip().upper()[:2] if addr else None,
            spend=_ints(row.get("Spend")),
            currency=(row.get("Currency Code") or "").strip() or None,
            spend_is_range=False,                      # Snap publishes exact figures
            impressions=_ints(row.get("Impressions")),
            start=_dt(row.get("StartDate")),
            stop=_dt(row.get("EndDate")),
            countries=[c for c in [(row.get("CountryCode") or "").strip().lower()] if c],
            languages=_split(row.get("Language")),
            age=(row.get("AgeBracket") or "").strip() or None,
            genders=_split(row.get("Gender")),
            targeting=tgt,
            landing_urls=urls,
            landing_hosts=hosts,
            creative_url=(row.get("CreativeUrl") or "").strip() or None,
        ))
    return out


def load_snap(years=(2026,), path_for=None):
    """Fetch (or read) Snap archives. Returns (records, status)."""
    recs, notes = [], []
    for y in years:
        try:
            if path_for:
                with open(path_for(y), encoding="utf-8-sig") as f:
                    recs += parse_snap_csv(f)
                notes.append("%s:local" % y)
                continue
            if requests is None:
                notes.append("%s:no_client" % y)
                continue
            r = requests.get(SNAP_URL % y, timeout=60)
            if r.status_code != 200:
                notes.append("%s:http_%s" % (y, r.status_code))
                continue
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
                recs += parse_snap_csv(z.read(name).decode("utf-8-sig"))
            notes.append("%s:ok" % y)
        except Exception as e:
            notes.append("%s:error(%s)" % (y, type(e).__name__))
    return recs, ";".join(notes)


# --------------------------------------------------------------------------
# The join: paid <-> organic
# --------------------------------------------------------------------------

def join_by_host(ads, docs, doc_host=None):
    """Match ads to organic documents on canonical landing host.

    This is the bridge. An ad and a post that point at the same host are two
    halves of one distribution effort — one bought, one not.
    """
    dh = doc_host or (lambda d: canon_host(d.get("url") or ""))
    by_host = {}
    for d in docs or []:
        h = dh(d)
        if h:
            by_host.setdefault(h, []).append(d)

    matches, unmatched = [], []
    for a in ads:
        hit = [h for h in a["landing_hosts"] if h in by_host]
        (matches if hit else unmatched).append(
            {"ad": a, "hosts": hit, "docs": [d for h in hit for d in by_host[h]]} if hit else a)
    return {"matched": matches, "unmatched_ads": unmatched,
            "organic_hosts": len(by_host), "paid_hosts": len({h for a in ads for h in a["landing_hosts"]})}


def reach_decomposition(docs, ads, cluster_actors=(), actor_of=None):
    """Split a narrative's visibility three ways.

    XTag already separates CAPTURED from MANUFACTURED reach (Ferrara's 0.1-5.3%
    band). Paid data adds the third and previously invisible category.

      purchased      impressions someone bought
      manufactured   engagement from actors inside a coordination cluster
      captured       everything else

    Those three demand different responses, and today they are indistinguishable.
    """
    actor_of = actor_of or (lambda d: "%s:%s" % (d.get("platform"), d.get("author") or canon_host(d.get("url") or "")))
    cl = set(cluster_actors or ())
    manufactured = captured = 0
    for d in docs or []:
        e = d.get("engagement") or 0
        if isinstance(e, dict):
            e = sum(v for k, v in e.items() if k != "views" and isinstance(v, (int, float)))
        if actor_of(d) in cl:
            manufactured += e
        else:
            captured += e
    purchased = sum(a["impressions"] or 0 for a in ads)
    have_paid = bool(ads)
    return {
        "purchased_impressions": purchased if have_paid else None,
        "manufactured_engagement": manufactured,
        "captured_engagement": captured,
        "spend": sum(a["spend"] or 0 for a in ads) if have_paid else None,
        "paid_evidence": "found" if have_paid else "none_found",
        # An impression and an engagement are different units. Never ratio them.
        "note": ("Impressions and engagements are different units and are reported "
                 "separately, never combined into one share."),
    }


def topic_skew(paid_labels, organic_labels):
    """Over-representation of each theme in PAID output vs ORGANIC output.

    What an operator pays to amplify reveals priorities their organic feed
    masks. Marc Owen Jones' Sudan investigation is the only published use of
    this primitive: Islamists 5.86x, Sudan 4.65x, SAF/Burhan 4.59x,
    Saudi Arabia 3.15x over-represented in ads versus the same actors' editorial.

    XTag already extracts narrative labels per document, so both distributions
    are already computed — this is a ratio over things the system has.
    """
    def dist(labels):
        n = len(labels) or 1
        out = {}
        for l in labels:
            out[l] = out.get(l, 0) + 1
        return {k: v / n for k, v in out.items()}

    p, o = dist(paid_labels), dist(organic_labels)
    rows = []
    for theme in set(p) | set(o):
        pv, ov = p.get(theme, 0.0), o.get(theme, 0.0)
        rows.append({
            "theme": theme,
            "paid_share": round(pv, 4),
            "organic_share": round(ov, 4),
            "ratio": round(pv / ov, 2) if ov else None,      # None = paid-only theme
            "paid_only": ov == 0 and pv > 0,
        })
    rows.sort(key=lambda r: (r["ratio"] is None, -(r["ratio"] or 0)))
    return rows


# --------------------------------------------------------------------------
# Payer plausibility
# --------------------------------------------------------------------------

# Country-name -> ISO-2, for the archives that publish names rather than codes.
# Deliberately partial: an unmapped name yields no flag, never a wrong one.
ISO2 = {
    "united arab emirates": "AE", "qatar": "QA", "kuwait": "KW", "saudi arabia": "SA",
    "oman": "OM", "bahrain": "BH", "jordan": "JO", "iraq": "IQ", "lebanon": "LB",
    "israel": "IL", "egypt": "EG", "morocco": "MA", "tunisia": "TN", "algeria": "DZ",
    "turkey": "TR", "iran": "IR", "yemen": "YE", "syria": "SY", "pakistan": "PK",
    "india": "IN", "united states": "US", "united kingdom": "GB", "canada": "CA",
    "australia": "AU", "new zealand": "NZ", "france": "FR", "germany": "DE",
    "italy": "IT", "spain": "ES", "portugal": "PT", "netherlands": "NL",
    "belgium": "BE", "luxembourg": "LU", "ireland": "IE", "denmark": "DK",
    "sweden": "SE", "norway": "NO", "finland": "FI", "iceland": "IS",
    "austria": "AT", "switzerland": "CH", "poland": "PL", "czech republic": "CZ",
    "slovakia": "SK", "slovenia": "SI", "hungary": "HU", "romania": "RO",
    "bulgaria": "BG", "croatia": "HR", "greece": "GR", "cyprus": "CY",
    "malta": "MT", "estonia": "EE", "latvia": "LV", "lithuania": "LT",
    "mexico": "MX", "brazil": "BR", "south africa": "ZA", "nigeria": "NG",
    "indonesia": "ID",
}

_GIBBERISH = re.compile(r"^[a-z]{6,}$", re.I)
_VOWELS = re.compile(r"[aeiouAEIOUء-ي]")


def payer_plausibility(ad):
    """Score how implausible an ad's declared payer is.

    Two regimes, and both are useful. For real advertisers the payer resolves to
    a company you can screen. For covert operations it is adversarial noise —
    CheckFirst found Doppelganger declaring "Evelyn Anderson", "Fake" and
    "ascfaqwscf", because Meta does not verify the field at all.

    The noise IS the signal. Returns flags plus a 0-100 implausibility score;
    a high score is a reason to look, never a finding on its own.
    """
    flags = []
    payer = (ad.get("payer") or "").strip()
    adv = (ad.get("advertiser") or "").strip()

    if not payer:
        flags.append("payer_absent")
    else:
        if len(payer) < 4:
            flags.append("payer_too_short")
        if _GIBBERISH.match(payer) and not _VOWELS.search(payer[1:]):
            flags.append("payer_unpronounceable")
        if payer.lower() in {"fake", "test", "none", "n/a", "na", "self"}:
            flags.append("payer_placeholder")
        if adv and payer.lower() == adv.lower():
            flags.append("payer_equals_advertiser")

    # Country mismatch is only computable when both sides are ISO-2 codes.
    # Snap publishes billing country as "KW" and target country as "kuwait";
    # naive prefix comparison fires on 89% of a clean archive, which is how a
    # flag becomes noise. Compare only through an explicit map, and stay silent
    # when the target country is unmapped rather than guessing.
    bc = (ad.get("billing_country") or "").upper()
    tc = {ISO2.get(c.strip().lower()) for c in ad.get("countries") or []}
    tc.discard(None)
    if bc and tc and bc not in tc:
        flags.append("payer_country_differs_from_target")

    if not (ad.get("billing_address") or "").strip():
        flags.append("no_billing_address")

    weights = {
        "payer_absent": 45, "payer_placeholder": 45, "payer_unpronounceable": 35,
        "payer_too_short": 20, "no_billing_address": 20,
        "payer_country_differs_from_target": 15, "payer_equals_advertiser": 5,
    }
    score = min(100, sum(weights.get(f, 0) for f in flags))
    return {"score": score, "flags": flags,
            "screenable": score < 35 and bool(payer),
            "note": "Implausibility is a reason to investigate the payer, not evidence about the ad."}


def screening_queries(ads):
    """Build OpenSanctions /match payloads from payer records.

    Emitted only for ads whose payer is structurally plausible. Name variants
    matter: the archives carry an organisation string AND a payer string that
    frequently differ ('Hamad Hammoud' vs 'Hamad Hamoud Obaid Alshammari',
    'fahad' vs the Arabic legal name) — send both, plus the billing country.
    """
    out = {}
    for i, a in enumerate(ads):
        if not payer_plausibility(a)["screenable"]:
            continue
        names = sorted({n for n in (a.get("payer"), a.get("advertiser")) if n})
        props = {"name": names}
        if a.get("billing_country"):
            props["country"] = [a["billing_country"]]
        if a.get("billing_address"):
            props["address"] = [a["billing_address"]]
        out["q%d" % i] = {"schema": "LegalEntity", "properties": props}
    return {"queries": out}
