"""
Hedge Advisor Bot v3 -- dirancang buat jalan di GitHub Actions
================================================================
Sama persis logic-nya kayak v2 (odds-api.io + Telegram), tapi ada
batas waktu jalan (~5 jam 50 menit) biar cocok sama limit GitHub Actions
(maks 6 jam per sesi). Workflow-nya bakal auto-restart sesi baru pas
sesi lama abis, jadi EFEKNYA JALAN TERUS-MENERUS tanpa kamu perlu
server/VPS/kartu kredit sama sekali.

CATATAN: script ini HARUS jalan di dalam repo GitHub yang PUBLIC,
karena GitHub Actions cuma gratis UNLIMITED buat repo public. Repo
private cuma dapet jatah 2000 menit/bulan gratis -- gak akan cukup
buat jalan 24/7.

Karena repo-nya public, JANGAN taro API key/token langsung di kode.
Semua rahasia (ODDS_API_KEY, TELEGRAM_BOT_TOKEN) dibaca dari GitHub
Secrets, bukan ditulis di file ini.
"""

import os
import re
import time
import json
import difflib
import requests
from datetime import datetime, timezone

# ============ KONFIGURASI ============

ODDS_API_KEY = os.environ["ODDS_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

ODDS_BASE_URL = "https://api.odds-api.io/v3"
BOOKMAKERS = os.environ.get("BOOKMAKERS", "Bet365,SBOBET")
SPORTS = ["football", "basketball"]
STATUS_FILTER = "pending,live"

FUZZY_MATCH_THRESHOLD = 0.6
STATE_FILE = "state.json"  # disimpen di root repo, di-commit balik tiap sesi
DEFAULT_THRESHOLD_PERCENT = 6

TELEGRAM_POLL_SECONDS = 4          # cek pesan Telegram tiap 4 detik
ODDS_CHECK_INTERVAL_SECONDS = 60   # cek odds tiap 1 menit
MAX_RUNTIME_SECONDS = 5 * 3600 + 50 * 60  # 5 jam 50 menit -> keluar sebelum limit 6 jam

# ======================================


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "chat_id": None,
        "last_update_id": 0,
        "default_threshold": DEFAULT_THRESHOLD_PERCENT,
        "positions": [],
        "last_odds_check": 0,
    }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def telegram_get_updates(offset):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"offset": offset, "timeout": 3}, timeout=8)
        return r.json().get("result", [])
    except requests.RequestException as e:
        print(f"[ERROR] getUpdates gagal: {e}")
        return []


def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
        if r.status_code != 200:
            print(f"[WARN] Gagal kirim: {r.status_code} {r.text[:200]}")
    except requests.RequestException as e:
        print(f"[ERROR] Kirim gagal: {e}")


def parse_position_line(line, default_threshold):
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 3:
        return None

    match_part, side_part, odds_part = parts[0], parts[1], parts[2]
    threshold = float(parts[3]) if len(parts) > 3 else default_threshold

    teams = re.split(r"\s+vs\.?\s+|\s+v\s+|\s+-\s+", match_part, flags=re.IGNORECASE)
    if len(teams) != 2:
        return None
    home, away = teams[0].strip(), teams[1].strip()

    try:
        held_odds = float(odds_part)
    except ValueError:
        return None

    totals_match = re.match(r"(over|under)\s+([\d.]+)", side_part, flags=re.IGNORECASE)
    if totals_match:
        market = "totals"
        held_side = totals_match.group(1).capitalize()
        point = float(totals_match.group(2))
    else:
        market = "h2h"
        held_side = side_part
        point = None

    return {
        "home": home, "away": away, "market": market, "held_side": held_side,
        "point": point, "held_odds": held_odds, "threshold": threshold,
        "notified": False, "created_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_events(sport):
    try:
        r = requests.get(f"{ODDS_BASE_URL}/events", params={
            "apiKey": ODDS_API_KEY, "sport": sport, "status": STATUS_FILTER, "limit": 500,
        }, timeout=15)
        if r.status_code == 200:
            return r.json()
        print(f"[WARN] Fetch events {sport}: {r.status_code} {r.text[:200]}")
        return []
    except requests.RequestException as e:
        print(f"[ERROR] Fetch events {sport}: {e}")
        return []


def fetch_event_odds(event_id):
    try:
        r = requests.get(f"{ODDS_BASE_URL}/odds", params={
            "apiKey": ODDS_API_KEY, "eventId": event_id, "bookmakers": BOOKMAKERS,
        }, timeout=15)
        if r.status_code == 200:
            return r.json()
        print(f"[WARN] Fetch odds {event_id}: {r.status_code} {r.text[:200]}")
        return None
    except requests.RequestException as e:
        print(f"[ERROR] Fetch odds {event_id}: {e}")
        return None


def name_similarity(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_matching_event(pos, events):
    best_event, best_score = None, 0
    for ev in events:
        score = (name_similarity(pos["home"], ev.get("home", "")) +
                 name_similarity(pos["away"], ev.get("away", ""))) / 2
        if score > best_score:
            best_score, best_event = score, ev
    return best_event if best_score >= FUZZY_MATCH_THRESHOLD else None


def resolve_held_and_hedge_team(pos):
    side_lower = pos["held_side"].strip().lower()
    if side_lower == "home":
        held_team = pos["home"]
    elif side_lower == "away":
        held_team = pos["away"]
    else:
        held_team = pos["held_side"]
    hedge_team = pos["away"] if held_team.lower() == pos["home"].lower() else pos["home"]
    return held_team, hedge_team


def find_best_hedge_odds(odds_response, pos):
    bookmakers = odds_response.get("bookmakers", {})
    best_odds, best_bookmaker = None, None

    if pos["market"] == "totals":
        target_field = "under" if pos["held_side"].lower() == "over" else "over"
        for bk_name, markets in bookmakers.items():
            for market in markets:
                name = (market.get("name") or "").lower()
                if "over" not in name and "total" not in name:
                    continue
                for entry in market.get("odds", []):
                    hdp = entry.get("hdp")
                    if hdp is None or pos["point"] is None or abs(float(hdp) - pos["point"]) > 0.01:
                        continue
                    val = entry.get(target_field)
                    if val is None:
                        continue
                    try:
                        price = float(val)
                    except (TypeError, ValueError):
                        continue
                    if best_odds is None or price > best_odds:
                        best_odds, best_bookmaker = price, bk_name
    else:
        _, hedge_team = resolve_held_and_hedge_team(pos)
        target_field = "home" if hedge_team.lower() == pos["home"].lower() else "away"
        for bk_name, markets in bookmakers.items():
            for market in markets:
                name = (market.get("name") or "").lower()
                if "winner" not in name and "1x2" not in name and "moneyline" not in name:
                    continue
                for entry in market.get("odds", []):
                    val = entry.get(target_field)
                    if val is None:
                        continue
                    try:
                        price = float(val)
                    except (TypeError, ValueError):
                        continue
                    if best_odds is None or price > best_odds:
                        best_odds, best_bookmaker = price, bk_name

    return best_odds, best_bookmaker


def evaluate_position(pos, best_hedge_odds):
    breakeven = pos["held_odds"] / (pos["held_odds"] - 1)
    edge_percent = ((best_hedge_odds - breakeven) / breakeven) * 100
    stake_hedge = 1 * pos["held_odds"] / best_hedge_odds
    profit = pos["held_odds"] - 1 - stake_hedge
    return {"breakeven": breakeven, "edge_percent": edge_percent,
            "stake_hedge_per_dollar": stake_hedge, "profit_per_dollar": profit}


def check_positions(state):
    if not state["positions"] or not state["chat_id"]:
        return
    events_by_sport = {sport: fetch_events(sport) for sport in SPORTS}
    all_events = [ev for evs in events_by_sport.values() for ev in evs]

    for pos in state["positions"]:
        if pos["notified"]:
            continue
        event = find_matching_event(pos, all_events)
        if not event:
            continue
        odds_response = fetch_event_odds(event["id"])
        if not odds_response:
            continue
        best_hedge_odds, bookmaker = find_best_hedge_odds(odds_response, pos)
        if best_hedge_odds is None:
            continue
        result = evaluate_position(pos, best_hedge_odds)
        if result["edge_percent"] >= pos["threshold"]:
            held_team, hedge_team = resolve_held_and_hedge_team(pos)
            side_label = f"{pos['held_side']} {pos['point']}" if pos["point"] else held_team
            hedge_label = (
                ("Under" if pos["held_side"].lower() == "over" else "Over") + f" {pos['point']}"
                if pos["market"] == "totals" else hedge_team
            )
            msg = (
                f"🎯 <b>Edge tercapai: {pos['home']} vs {pos['away']}</b>\n"
                f"Posisi kamu: {side_label} @ {pos['held_odds']}\n"
                f"Hedge tersedia: {hedge_label} @ {best_hedge_odds} ({bookmaker})\n"
                f"Breakeven: {result['breakeven']:.2f} | Edge: +{result['edge_percent']:.1f}%\n"
                f"Profit terjamin per $1 stake: ${result['profit_per_dollar']:.2f}\n"
                f"Stake hedge per $1 stake awal: ${result['stake_hedge_per_dollar']:.2f}"
            )
            send_telegram_message(state["chat_id"], msg)
            pos["notified"] = True


def handle_incoming_message(state, message):
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()
    state["chat_id"] = chat_id

    if text == "/help":
        send_telegram_message(chat_id,
            "Kirim posisi, format:\n"
            "Lakers vs Celtics | Over 220.5 | 1.90\n"
            "Lakers vs Celtics | Home | 1.85\n\n"
            "Opsional tambah threshold custom di akhir:\n"
            "Lakers vs Celtics | Over 220.5 | 1.90 | 7\n\n"
            "/positions - lihat semua posisi\n"
            "/clear - hapus semua\n"
            "/threshold N - ubah threshold default (persen)")
        return

    if text == "/positions":
        if not state["positions"]:
            send_telegram_message(chat_id, "Belum ada posisi yang dipantau.")
        else:
            lines = []
            for p in state["positions"]:
                side = f"{p['held_side']} {p['point']}" if p["point"] else p["held_side"]
                status = "✅ sudah notif" if p["notified"] else "⏳ menunggu"
                lines.append(f"- {p['home']} vs {p['away']}: {side} @ {p['held_odds']} (threshold {p['threshold']}%) {status}")
            send_telegram_message(chat_id, "\n".join(lines))
        return

    if text == "/clear":
        state["positions"] = []
        send_telegram_message(chat_id, "Semua posisi dihapus.")
        return

    if text.startswith("/threshold"):
        parts = text.split()
        if len(parts) == 2:
            try:
                state["default_threshold"] = float(parts[1])
                send_telegram_message(chat_id, f"Threshold default diubah jadi {parts[1]}%.")
            except ValueError:
                send_telegram_message(chat_id, "Format: /threshold 6")
        return

    added = 0
    for line in text.splitlines():
        pos = parse_position_line(line, state["default_threshold"])
        if pos:
            state["positions"].append(pos)
            added += 1
    if added:
        send_telegram_message(chat_id, f"{added} posisi ditambahkan. Bot bakal notif kalau edge >= threshold masing-masing.")
    else:
        send_telegram_message(chat_id, "Gak kebaca sebagai posisi. Ketik /help buat lihat formatnya.")


def main():
    state = load_state()
    start_time = time.time()
    print(f"[INFO] Sesi dimulai {datetime.now(timezone.utc).isoformat()}, maks runtime {MAX_RUNTIME_SECONDS}s")

    while time.time() - start_time < MAX_RUNTIME_SECONDS:
        for update in telegram_get_updates(state["last_update_id"] + 1):
            state["last_update_id"] = update["update_id"]
            if "message" in update:
                handle_incoming_message(state, update["message"])
        save_state(state)

        now_ts = time.time()
        if now_ts - state["last_odds_check"] >= ODDS_CHECK_INTERVAL_SECONDS:
            try:
                check_positions(state)
            except Exception as e:
                print(f"[FATAL] Error saat cek posisi: {e}")
            state["last_odds_check"] = now_ts
            save_state(state)

        time.sleep(TELEGRAM_POLL_SECONDS)

    print("[INFO] Mendekati limit waktu GitHub Actions, sesi ini selesai. Sesi baru bakal lanjut otomatis.")
    save_state(state)


if __name__ == "__main__":
    main()
