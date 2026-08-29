"""Email delivery for XTag scheduled intelligence reports (Resend).

Same posture as db.py: if RESEND_API_KEY is unset, everything no-ops cleanly
and the caller is told why, rather than the app failing. Sending is never
allowed to take down a request or the scheduler.
"""
from __future__ import annotations

import html as _html
import logging
import os

import requests

log = logging.getLogger("xtag.mailer")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
# Resend's shared sender works with zero DNS setup, which is what makes this
# usable immediately. Swap REPORT_FROM for an address on your own verified
# domain when you have one — deliverability is much better from your own domain.
REPORT_FROM = os.environ.get("REPORT_FROM", "XTag <onboarding@resend.dev>").strip()
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://xtag.up.railway.app").strip().rstrip("/")

MAIL_ENABLED = bool(RESEND_API_KEY)
TIMEOUT = 15


def health() -> dict:
    if not MAIL_ENABLED:
        return {"enabled": False, "reason": "RESEND_API_KEY not set"}
    return {"enabled": True, "from": REPORT_FROM, "reason": None}


def send(to: str, subject: str, html_body: str) -> tuple[bool, str | None, str | None]:
    """Returns (ok, provider_id, error). Never raises."""
    if not MAIL_ENABLED:
        return False, None, "RESEND_API_KEY not set"
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                     "Content-Type": "application/json"},
            json={"from": REPORT_FROM, "to": [to], "subject": subject, "html": html_body},
            timeout=TIMEOUT,
        )
        if r.status_code >= 400:
            msg = r.text[:300]
            log.error("Resend %s: %s", r.status_code, msg)
            return False, None, f"HTTP {r.status_code}: {msg}"
        return True, (r.json() or {}).get("id"), None
    except Exception as e:
        log.error("Resend send failed: %s", e)
        return False, None, str(e)[:300]


# ── Report rendering ─────────────────────────────────────────────────────────
# Email HTML is deliberately old-fashioned: tables, inline styles, no external
# CSS or webfonts. Gmail and Outlook strip <style> blocks and most modern CSS,
# so the glassmorphism of the web UI cannot survive here — this aims for
# "clean and readable everywhere" instead.

def _e(s) -> str:
    return _html.escape(str(s if s is not None else ""))


_SENT_COLOR = {"positive": "#0d9669", "negative": "#dc2626", "neutral": "#71717a"}
_FRAME_COLOR = {"fear": "#f97316", "anger": "#ef4444", "threat": "#dc2626",
                "disinformation": "#ec4899", "hope": "#0d9669", "pride": "#7c3aed",
                "grief": "#64748b", "neutral": "#71717a"}


def render_report(query: str, payload: dict, brief: str | None = None,
                  history: dict | None = None, unsubscribe_token: str | None = None,
                  cadence_days: int = 7) -> str:
    totals = payload.get("totals") or {}
    sent = payload.get("sentiment") or {}
    narratives = payload.get("narratives") or []
    entities = (payload.get("entities") or {}).get("entities") or []
    coord = payload.get("coordination") or {}
    vel = payload.get("velocity") or {}

    mentions = totals.get("mentions", 0)
    net = sent.get("net")
    net_txt = f"{net:+.2f}" if isinstance(net, (int, float)) else "—"
    net_col = "#0d9669" if isinstance(net, (int, float)) and net > 0 else \
              "#dc2626" if isinstance(net, (int, float)) and net < 0 else "#71717a"

    def stat(label, value, color="#18181f"):
        return (f'<td align="center" style="padding:14px 8px;background:#f7f6f2;'
                f'border-radius:10px;">'
                f'<div style="font-size:26px;font-weight:300;color:{color};'
                f'line-height:1;">{_e(value)}</div>'
                f'<div style="font-size:10px;letter-spacing:.1em;text-transform:uppercase;'
                f'color:#88889a;margin-top:5px;">{_e(label)}</div></td>')

    parts = []
    parts.append(f'''<!doctype html><html><body style="margin:0;padding:0;background:#f0efe9;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0efe9;padding:26px 12px;">
<tr><td align="center">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;background:#ffffff;border-radius:14px;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
  <tr><td style="padding:24px 26px 6px;border-bottom:1px solid #eeedea;">
    <div style="font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:#c23d0c;font-weight:700;">XTag Narrative Intelligence</div>
    <div style="font-size:23px;color:#18181f;margin-top:8px;font-weight:600;line-height:1.3;">{_e(query)}</div>
    <div style="font-size:12px;color:#88889a;margin:6px 0 18px;">Automated report &middot; every {cadence_days} day{'s' if cadence_days != 1 else ''}</div>
  </td></tr>
  <tr><td style="padding:20px 26px 0;">
    <table width="100%" cellpadding="0" cellspacing="6"><tr>
      {stat("Mentions", f"{mentions:,}")}
      {stat("Net sentiment", net_txt, net_col)}
      {stat("Coordination", coord.get("coordination_score", 0))}
      {stat("State media", totals.get("state_media", 0), "#7c3aed")}
    </tr></table>
  </td></tr>''')

    if brief:
        safe = _e(brief).replace("**", "").replace("\n", "<br/>")
        parts.append(f'''<tr><td style="padding:22px 26px 0;">
  <div style="font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:#88889a;margin-bottom:8px;">Assessment</div>
  <div style="font-size:14px;line-height:1.75;color:#4a4a60;">{safe}</div>
</td></tr>''')

    if narratives:
        rows = []
        for n in narratives[:8]:
            fr = str(n.get("framing", "neutral")).lower()
            col = _FRAME_COLOR.get(fr, "#71717a")
            rows.append(f'''<tr><td style="padding:11px 0;border-bottom:1px solid #f2f1ee;">
  <div style="font-size:14px;font-weight:600;color:#18181f;line-height:1.4;">{_e(n.get("label"))}</div>
  <div style="margin-top:5px;">
    <span style="display:inline-block;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:{col};background:{col}1a;border-radius:9px;padding:2px 9px;">{_e(fr)}</span>
    <span style="font-size:11.5px;color:#88889a;margin-left:8px;">{_e(n.get("count", 0))} posts &middot; {_e(n.get("velocity", "stable"))}</span>
  </div>
  {f'<div style="font-size:12.5px;color:#4a4a60;margin-top:6px;line-height:1.6;">{_e(n.get("key_claim"))}</div>' if n.get("key_claim") else ''}
</td></tr>''')
        parts.append(f'''<tr><td style="padding:22px 26px 0;">
  <div style="font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:#88889a;margin-bottom:6px;">Narrative clusters ({len(narratives)})</div>
  <table width="100%" cellpadding="0" cellspacing="0">{''.join(rows)}</table>
</td></tr>''')

    if entities:
        chips = "".join(
            f'<span style="display:inline-block;font-size:11.5px;color:#18181f;background:#f7f6f2;'
            f'border:1px solid #eeedea;border-radius:14px;padding:4px 11px;margin:0 4px 5px 0;">'
            f'{_e(en.get("name"))} <span style="color:#88889a;font-size:10px;">{_e(en.get("mentions", 0))}</span></span>'
            for en in entities[:18])
        parts.append(f'''<tr><td style="padding:22px 26px 0;">
  <div style="font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:#88889a;margin-bottom:8px;">Key actors</div>
  <div>{chips}</div>
</td></tr>''')

    if history and (history.get("points") or 0) >= 2:
        c = history.get("change") or {}
        def delta(label, v):
            if v is None: return ""
            arrow = "&#9650;" if v > 0 else "&#9660;" if v < 0 else "&bull;"
            col = "#dc2626" if (label != "Sentiment" and v > 0) else "#0d9669" if v < 0 else "#71717a"
            return (f'<span style="display:inline-block;font-size:11.5px;color:{col};'
                    f'background:#f7f6f2;border-radius:12px;padding:3px 10px;margin-right:5px;">'
                    f'{label} {arrow} {v:+g}</span>')
        parts.append(f'''<tr><td style="padding:22px 26px 0;">
  <div style="font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:#88889a;margin-bottom:8px;">Change since tracking began ({history.get("points")} snapshots)</div>
  <div>{delta("Mentions", c.get("mentions"))}{delta("Coordination", c.get("coordination_score"))}{delta("Sentiment", c.get("sentiment_net"))}</div>
</td></tr>''')

    if vel.get("acceleration"):
        parts.append(f'''<tr><td style="padding:18px 26px 0;">
  <div style="font-size:12.5px;color:#4a4a60;">Velocity: <strong style="color:#18181f;">{_e(vel.get("acceleration"))}</strong>
  &nbsp;&middot;&nbsp; 6h {_e((vel.get("windows") or {}).get("6h", 0))} &middot; 24h {_e((vel.get("windows") or {}).get("24h", 0))} &middot; 7d {_e((vel.get("windows") or {}).get("7d", 0))}</div>
</td></tr>''')

    unsub = ""
    if unsubscribe_token:
        unsub = (f'<a href="{PUBLIC_BASE_URL}/unsubscribe?token={_e(unsubscribe_token)}" '
                 f'style="color:#88889a;text-decoration:underline;">Unsubscribe</a>')

    parts.append(f'''<tr><td style="padding:26px;">
  <a href="{PUBLIC_BASE_URL}/?q={_e(query)}" style="display:inline-block;background:#c23d0c;color:#ffffff;
     text-decoration:none;font-size:13px;font-weight:600;border-radius:9px;padding:11px 20px;">Open in XTag</a>
</td></tr>
<tr><td style="padding:0 26px 26px;border-top:1px solid #eeedea;padding-top:16px;">
  <div style="font-size:11px;color:#88889a;line-height:1.7;">
    Generated automatically by XTag. Figures reflect sources reachable at send time and may be incomplete.<br/>{unsub}
  </div>
</td></tr>
</table></td></tr></table></body></html>''')

    return "".join(parts)
