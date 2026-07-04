"""
Hedge Advisor Bot v4 -- tambah command /odds buat lookup odds langsung
=======================================================================
Semua fitur hedge advisor v3 tetap ada (kirim posisi | Sisi | Odds,
bot diem sampai edge threshold tercapai). DITAMBAH command baru:

  /odds Home vs Away

Bot bakal cari match itu di football & basketball, terus balikin:
  - Basketball -> Menang/kalah (moneyline) + Handicap (point spread)
  - Football   -> W1/Tie/W2 (1X2) + Over/Under total gol

Sama seperti v3, dirancang buat jalan di GitHub Actions (public repo),
baca ODDS_API_KEY & TELEGRAM_BOT_TOKEN dari environment/GitHub Secrets.
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
STATE_FILE = "state.json"
DEFAULT_THRESHOLD_PERCENT = 6

TELEGRAM_POLL_SECONDS = 4
ODDS_CHECK_INTERVAL_SECONDS = 60
MAX_RUNTIME_SECONDS = 5 * 3600 + 50 * 60

# ======================================


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "chat_id": None, "last_update_id": 0,
        "default_threshold": DEFAULT_THRESHOLD_PERCENT,
        "positions": [], "last_odds_check": 0,
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


def name_similarity(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


MIN_SINGLE_NAME_SIMILARITY = 0.5  # kedua nama tim WAJIB minimal semirip ini masing-masing
BASKETBALL_LEAGUE = "usa-nba"  # batasin basket cuma NBA

# --- Kontrol jelas biar gak narik/habisin kuota response event tanpa batas ---
EVENTS_PAGE_SIZE = 5000   # maks per request sesuai limit resmi odds-api.io
MAX_PAGES_PER_SPORT = 2   # 2 halaman = maks 10.000 event per sport, per fetch
# Efeknya: worst case cuma 2 API call per sport per fetch (bukan tiap baris
# /odds, karena events-nya di-cache & dipakai ulang -- lihat handle_odds_command).
# Naikin MAX_PAGES_PER_SPORT kalau ternyata masih banyak match yang "gak ketemu"
# padahal beneran ada; turunin kalau mau lebih hemat request.


def fetch_events(sport, league=None):
    all_events = []
    skip = 0

    for page_num in range(MAX_PAGES_PER_SPORT):
        params = {
            "apiKey": ODDS_API_KEY, "sport": sport, "status": STATUS_FILTER,
            "limit": EVENTS_PAGE_SIZE, "skip": skip,
        }
        if league:
            params["league"] = league
        try:
            r = requests.get(f"{ODDS_BASE_URL}/events", params=params, timeout=15)
        except requests.RequestException as e:
            print(f"[ERROR] Fetch events {sport}: {e}")
            break

        if r.status_code != 200:
            print(f"[WARN] Fetch events {sport}: {r.status_code} {r.text[:200]}")
            break

        page = r.json()
        all_events.extend(page)
        print(f"[INFO] Fetch events {sport} halaman {page_num + 1}: {len(page)} event")

        if len(page) < EVENTS_PAGE_SIZE:
            break  # udah halaman terakhir, gak perlu lanjut skip
        skip += EVENTS_PAGE_SIZE

    return all_events


def get_sport_events(sport):
    """Wrapper -- basket dibatasin ke NBA doang, bola tetap universal (semua liga)."""
    if sport == "basketball":
        return fetch_events(sport, league=BASKETBALL_LEAGUE)
    return fetch_events(sport)


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


def event_match_score(home_query, away_query, ev):
    """
    Return None kalau salah satu nama tim gak cukup mirip (mencegah kasus
    'Paraguay vs France' ketarik ke 'Turkiye vs France' cuma gara-gara
    salah satu sisi kebetulan sama persis). Kedua sisi WAJIB lolos ambang
    minimal masing-masing, baru dirata-rata buat nentuin skor akhir.
    """
    home_score = name_similarity(home_query, ev.get("home", ""))
    away_score = name_similarity(away_query, ev.get("away", ""))
    if home_score < MIN_SINGLE_NAME_SIMILARITY or away_score < MIN_SINGLE_NAME_SIMILARITY:
        return None
    return (home_score + away_score) / 2


def find_match_across_sports(home_query, away_query, events_by_sport=None):
    """
    Cari event yang paling cocok di SEMUA sport (football & basketball NBA).
    events_by_sport: kalau udah di-fetch sebelumnya (misal buat proses banyak
    baris /odds sekaligus), pass di sini biar GAK fetch ulang dari API tiap
    baris -- hemat request & kuota.
    Return (event, sport) atau (None, None) kalau gak ketemu.
    """
    if events_by_sport is None:
        events_by_sport = {sport: get_sport_events(sport) for sport in SPORTS}

    best_event, best_sport, best_score = None, None, 0
    for sport, events in events_by_sport.items():
        for ev in events:
            score = event_match_score(home_query, away_query, ev)
            if score is not None and score > best_score:
                best_score, best_event, best_sport = score, ev, sport
    if best_score >= FUZZY_MATCH_THRESHOLD:
        return best_event, best_sport
    return None, None


EXCLUDE_KEYWORDS = ["half", "1st", "2nd", "3rd", "4th", "quarter", "period",
                    "corner", "card", "booking", "team total", "asian total", " ht"]


def is_main_market(name_lower):
    """Buang market yang bukan full-match utama (babak 1, corner, kartu, dll)."""
    return not any(kw in name_lower for kw in EXCLUDE_KEYWORDS)


def is_half_line(hdp):
    """Cuma line .5 biasa (0.5, 1.5, 2.5, dst) -- buang quarter line (.25/.75)."""
    if hdp is None:
        return False
    frac = abs(hdp) % 1
    return abs(frac - 0.5) < 1e-6


def best_under_2(entries, field, half_line_only=False):
    """
    Dari semua entry odds, ambil yang harganya di field tersebut PALING BAGUS
    (paling tinggi) tapi masih di bawah 2.00. Kalau gak ada yang di bawah 2,
    fallback ke yang paling deket ke 2.00.
    """
    candidates = []
    for entry in entries:
        if half_line_only and not is_half_line(entry.get("hdp")):
            continue
        val = entry.get(field)
        if val is None:
            continue
        try:
            price = float(val)
        except (TypeError, ValueError):
            continue
        candidates.append((price, entry))

    if not candidates:
        return None

    under_2 = [c for c in candidates if c[0] < 2.0]
    if under_2:
        return max(under_2, key=lambda c: c[0])
    return min(candidates, key=lambda c: abs(c[0] - 2.0))


def format_basketball_odds(odds_response):
    """
    Format ringkas: Menang/kalah + 1 handicap line terbaik (odds < 2), per bookmaker.
    """
    home, away = odds_response.get("home", "?"), odds_response.get("away", "?")
    lines = [f"🏀 <b>{home} vs {away}</b>"]

    bookmakers = odds_response.get("bookmakers", {})
    for bk_name, markets in bookmakers.items():
        bk_lines = []
        winner_entries, handicap_entries = [], []

        for market in markets:
            name_lower = (market.get("name") or "").lower()
            if not is_main_market(name_lower):
                continue
            if "winner" in name_lower or "moneyline" in name_lower:
                winner_entries.extend(market.get("odds", []))
            elif "handicap" in name_lower or "spread" in name_lower:
                handicap_entries.extend(market.get("odds", []))

        if winner_entries:
            e = winner_entries[0]
            h, a = e.get("home"), e.get("away")
            if h is not None and a is not None:
                bk_lines.append(f"  Menang/Kalah: {home} {h} | {away} {a}")

        best_home = best_under_2(handicap_entries, "home", half_line_only=True)
        if best_home:
            price, entry = best_home
            bk_lines.append(f"  Handicap {entry.get('hdp')}: {home} {price} | {away} {entry.get('away')}")

        if bk_lines:
            lines.append(f"\n<b>{bk_name}</b>")
            lines.extend(bk_lines)

    if len(lines) == 1:
        lines.append("(Belum ada data odds tersedia buat match ini)")
    return "\n".join(lines)


WINNER_KEYWORDS = ["1x2", "winner", "moneyline", "match odds", "full time result",
                   "3 way", "3-way", "fulltime result", "ft result", "ml"]


def format_football_odds(odds_response):
    """
    Format ringkas: W1/Tie/W2 + 1 Over line terbaik + 1 Under line terbaik
    (odds < 2), per bookmaker -- bukan semua line total gol.
    """
    home, away = odds_response.get("home", "?"), odds_response.get("away", "?")
    lines = [f"⚽ <b>{home} vs {away}</b>"]

    bookmakers = odds_response.get("bookmakers", {})
    for bk_name, markets in bookmakers.items():
        bk_lines = []
        winner_entries, totals_entries = [], []
        all_market_names = [m.get("name") for m in markets]  # buat debug kalau gagal

        for market in markets:
            name_lower = (market.get("name") or "").lower()
            if not is_main_market(name_lower):
                continue
            if any(kw in name_lower for kw in WINNER_KEYWORDS):
                winner_entries.extend(market.get("odds", []))
            elif "over" in name_lower or "total" in name_lower:
                totals_entries.extend(market.get("odds", []))

        if winner_entries:
            e = winner_entries[0]
            h, d, a = e.get("home"), e.get("draw"), e.get("away")
            if h is not None and a is not None:
                draw_txt = f" | Tie {d}" if d is not None else ""
                bk_lines.append(f"  W1 (menang {home}) {h}{draw_txt} | W2 (menang {away}) {a}")
        else:
            print(f"[DEBUG] {bk_name} - {home} vs {away}: market W1/Tie/W2 gak ke-detect. "
                  f"Nama market yang ada: {all_market_names}")

        best_over = best_under_2(totals_entries, "over", half_line_only=True)
        best_under = best_under_2(totals_entries, "under", half_line_only=True)
        if best_over:
            price, entry = best_over
            bk_lines.append(f"  Over terbaik: {price} @ total {entry.get('hdp')} gol")
        if best_under:
            price, entry = best_under
            bk_lines.append(f"  Under terbaik: {price} @ total {entry.get('hdp')} gol")

        if bk_lines:
            lines.append(f"\n<b>{bk_name}</b>")
            lines.extend(bk_lines)

    if len(lines) == 1:
        lines.append("(Belum ada data odds tersedia buat match ini)")
    return "\n".join(lines)


def handle_odds_command(chat_id, query_text):
    match_lines = [l.strip() for l in query_text.splitlines() if l.strip()]
    if not match_lines:
        send_telegram_message(chat_id, "Format: /odds Home vs Away\n(bisa banyak baris sekaligus, 1 match per baris)")
        return

    # Fetch semua event SEKALI aja di sini, dipakai ulang buat semua baris di
    # bawah -- biar gak fetch+paginate ulang dari API tiap baris (boros kuota).
    events_by_sport = {sport: get_sport_events(sport) for sport in SPORTS}
    print(f"[INFO] /odds: {len(match_lines)} baris query, "
          f"{sum(len(v) for v in events_by_sport.values())} event ke-cache "
          f"({', '.join(f'{k}={len(v)}' for k, v in events_by_sport.items())})")

    for line in match_lines:
        teams = re.split(r"\s+vs\.?\s+|\s+v\s+|\s+-\s+", line, flags=re.IGNORECASE)
        if len(teams) != 2:
            send_telegram_message(chat_id, f"Format salah, dilewati: '{line}'")
            continue

        home_query, away_query = teams[0].strip(), teams[1].strip()
        event, sport = find_match_across_sports(home_query, away_query, events_by_sport)

        if not event:
            send_telegram_message(chat_id, f"'{line}' gak ketemu di football atau basketball (NBA).")
            continue

        odds_response = fetch_event_odds(event["id"])
        if not odds_response:
            send_telegram_message(chat_id, f"'{line}' ketemu match-nya, tapi gagal ambil data odds.")
            continue

        msg = format_basketball_odds(odds_response) if sport == "basketball" else format_football_odds(odds_response)
        send_telegram_message(chat_id, msg)
        time.sleep(0.3)  # jeda dikit biar gak kena rate limit Telegram kalau match-nya banyak


# ---------- Bagian hedge advisor (sama seperti v3) ----------

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
        market, held_side, point = "totals", totals_match.group(1).capitalize(), float(totals_match.group(2))
    else:
        market, held_side, point = "h2h", side_part, None
    return {
        "home": home, "away": away, "market": market, "held_side": held_side,
        "point": point, "held_odds": held_odds, "threshold": threshold,
        "notified": False, "created_at": datetime.now(timezone.utc).isoformat(),
    }


def find_matching_event(pos, events):
    best_event, best_score = None, 0
    for ev in events:
        score = event_match_score(pos["home"], pos["away"], ev)
        if score is not None and score > best_score:
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
                if not any(kw in name for kw in WINNER_KEYWORDS):
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
    events_by_sport = {sport: get_sport_events(sport) for sport in SPORTS}
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
            "<b>Hedge advisor:</b>\n"
            "Kirim posisi, format:\n"
            "Lakers vs Celtics | Over 220.5 | 1.90\n"
            "Lakers vs Celtics | Home | 1.85\n"
            "(Opsional tambah threshold di akhir: | 7)\n\n"
            "<b>Lookup odds langsung:</b>\n"
            "/odds Home vs Away\n"
            "-> Basketball: menang/kalah + handicap\n"
            "-> Football: W1/Tie/W2 + Over/Under gol\n\n"
            "/positions - lihat posisi hedge yang dipantau\n"
            "/clear - hapus semua posisi\n"
            "/threshold N - ubah threshold default (persen)")
        return

    if text.startswith("/odds"):
        query = text[len("/odds"):].strip()
        if not query:
            send_telegram_message(chat_id, "Format: /odds Home vs Away")
        else:
            handle_odds_command(chat_id, query)
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
        else:
            send_telegram_message(
                chat_id,
                f"Threshold default sekarang: {state['default_threshold']}%.\n"
                f"Buat ubah, ketik: /threshold 6"
            )
        return

    added = 0
    for line in text.splitlines():
        pos = parse_position_line(line, state["default_threshold"])
        if pos:
            state["positions"].append(pos)
            added += 1
    if added:
        send_telegram_message(chat_id, f"{added} posisi ditambahkan. Bot bakal notif kalau edge >= threshold masing-masing.")
    elif text and not text.startswith("/"):
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

    print("[INFO] Mendekati limit waktu GitHub Actions, sesi ini selesai.")
    save_state(state)


if __name__ == "__main__":
    main()
