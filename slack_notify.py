"""Post the day's scan results to a Slack channel via an incoming webhook.

Reads scans/signals_YYYY-MM-DD.json (always written) and
scans/scan_YYYY-MM-DD.json (written unless the regime is risk-off),
then sends a Block Kit message to SLACK_WEBHOOK_URL.

Usage:  python scripts/slack_notify.py [--top 10] [--dry-run]
Env:    SLACK_WEBHOOK_URL   (required unless --dry-run)
        RUN_URL             (optional link to the GitHub Actions run / dashboard artifact)
        PAPER_STATUS_FILE   (optional path to `--paper-status` output to include)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANS = ROOT / "scans"

REGIME_EMOJI = {
    "favorable": ":large_green_circle: FAVORABLE (tam risk)",
    "mixed": ":large_yellow_circle: MIXED (yarım risk)",
    "caution": ":large_orange_circle: CAUTION (işlem yok)",
    "risk_off": ":red_circle: RISK OFF (işlem yok)",
    "data_unavailable": ":warning: VERİ ALINAMADI (sinyal yok)",
}
LEVEL_TR = {
    "ath": "ATH",
    "multi_year": "Çok yıllık zirve",
    "52wk": "52H zirve",
    "prior_resistance": "Eski direnç",
}


def _latest(pattern: str, today: str) -> Path | None:
    exact = SCANS / pattern.format(today)
    if exact.exists():
        return exact
    return None


def _tv(ticker: str) -> str:
    return f"<https://www.tradingview.com/chart/?symbol={ticker}|{ticker}>"


def _fmt(v, nd=1, suffix=""):
    if v is None:
        return "-"
    try:
        return f"{float(v):.{nd}f}{suffix}"
    except (TypeError, ValueError):
        return str(v)


def build_blocks(today: str, top_n: int) -> tuple[list[dict], str]:
    sig_path = _latest("signals_{}.json", today)
    scan_path = _latest("scan_{}.json", today)

    if sig_path is None and scan_path is None:
        text = f"Qullamaggie tarama {today}: sonuç dosyası yok (tatil/hafta sonu ya da tarama hatası)."
        return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}], text

    sig = json.loads(sig_path.read_text()) if sig_path else {"summary": {}, "signals": []}
    scan = json.loads(scan_path.read_text()) if scan_path else {}
    summary = sig.get("summary", {})
    ctx = scan.get("market_context", {}) or {}
    regime = summary.get("regime") or ctx.get("regime", "?")

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Qullamaggie Breakout Tarama — {today}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Piyasa rejimi:* {REGIME_EMOJI.get(regime, regime)}"}},
    ]

    if ctx:
        breadth = (
            f"50MA üstü: *{_fmt(ctx.get('pct_above_50ma'), 0, '%')}*  ·  "
            f"20MA üstü: *{_fmt(ctx.get('pct_above_20ma'), 0, '%')}*  ·  "
            f"52H zirve/dip: *{ctx.get('new_highs', '-')}/{ctx.get('new_lows', '-')}*  ·  "
            f"VIX: *{_fmt(ctx.get('vix_level'))}* ({ctx.get('vix_direction', '-')})  ·  "
            f"Kırılım devamı: *{_fmt(ctx.get('breakout_followthrough'), 0, '%')}*"
        )
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": breadth}]})

    # ── Trade signals ──
    signals = sig.get("signals", [])
    blocks.append({"type": "divider"})
    if signals:
        lines = ["*:dart: İşlem sinyalleri* (limit giriş · stop · adet @ $"
                 f"{_fmt(summary.get('account_equity'), 0)} hesap, %{_fmt(100 * 0.01 * summary.get('risk_multiplier', 1), 1)} risk)"]
        for s in signals:
            lines.append(
                f"*{s['rank']}. {_tv(s['ticker'])}* {s.get('name', '')[:28]}\n"
                f"    Giriş *{s['limit_entry']:.2f}*  ·  Stop *{s['stop_price']:.2f}* (-{s['stop_distance_pct']:.1f}%)  ·  "
                f"{s['shares']} adet (${s['position_dollars']:,.0f})\n"
                f"    Son kapanış {s['last_close']:.2f}  ·  {LEVEL_TR.get(s.get('level_type'), s.get('level_type'))}  ·  "
                f"ADR {_fmt(s.get('adr20'), 1, '%')}  ·  {s.get('sector') or '-'}"
            )
        lines.append("_Çıkış: yarısı giriş+3×ATR'de, kalanı en yüksek kapanış−3×ATR trailing, en fazla 10 gün._")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    else:
        reason = summary.get("reason_no_signals") or "Kriterleri geçen aday yok."
        skips = {k: v for k, v in (summary.get("skip_reasons") or {}).items() if v}
        extra = f"\nElenenler: {skips}" if skips else ""
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"*:dart: İşlem sinyali yok.* {reason}{extra}"}})

    # ── Top-ranked watchlist ──
    watch = scan.get("watchlist", [])[:top_n]
    if watch:
        rows = [f"*:mag: İzleme listesi — ilk {len(watch)}*",
                "```#   Hisse   RS   HH/HL  ATRsık  Seviye        Mesafe  EMA"]
        for i, w in enumerate(watch, 1):
            rows.append(
                f"{i:<3} {w['ticker']:<7} {_fmt(w.get('rs_percentile'), 0):>3}  "
                f"{_fmt(w.get('hh_hl_pct'), 0, '%'):>5}  {_fmt(w.get('atr_compression'), 2):>6}  "
                f"{LEVEL_TR.get(w.get('level_type'), w.get('level_type') or '-'):<13} "
                f"{_fmt(w.get('abr_dist_to_level'), 1):>6}  {w.get('ema_stack') or '-'}"
            )
        rows[-1] += "```"
        rows.append(" ".join(_tv(w["ticker"]) for w in watch))
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(rows)}})

    # ── Paper account (optional) ──
    paper_file = os.environ.get("PAPER_STATUS_FILE")
    if paper_file and Path(paper_file).exists():
        txt = Path(paper_file).read_text(errors="ignore").strip()
        if txt:
            blocks.append({"type": "divider"})
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                           "text": "*:bar_chart: Paper hesap*\n```" + txt[-2500:] + "```"}})

    run_url = os.environ.get("RUN_URL")
    foot = "Sadece bilgi amaçlı, yatırım tavsiyesi değildir."
    if run_url:
        foot = f"<{run_url}|Tam HTML dashboard (Artifacts)>  ·  " + foot
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": foot}]})

    fallback = f"Qullamaggie tarama {today}: rejim {regime}, {len(signals)} sinyal"
    return blocks, fallback


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    blocks, fallback = build_blocks(args.date, args.top)
    payload = {"text": fallback, "blocks": blocks}

    if args.dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        print("SLACK_WEBHOOK_URL tanımlı değil", file=sys.stderr)
        return 1
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print("Slack:", r.status, r.read().decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
