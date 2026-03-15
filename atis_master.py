import os
import re
import math
import subprocess
import requests
from datetime import datetime
from google import genai

# --- CONFIGURATION ---
# Set these directly here OR use environment variables
API_KEY       = os.getenv("GEMINI_API_KEY",    "YOUR_GEMINI_API_KEY_HERE")
BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN","YOUR_TELEGRAM_BOT_TOKEN_HERE")
CHAT_ID       = os.getenv("TELEGRAM_CHAT_ID",  "YOUR_TELEGRAM_CHAT_ID_HERE")

STREAM_URL    = "http://s1-fmt2.liveatc.net/kdvt3_atis"
AUDIO_FILE    = "/tmp/atis_temp.mp3"           # Temp file, always deleted after use
LOGS_DIR      = os.path.expanduser("~/atis_logs")  # Transcript storage
STATE_FILE    = os.path.expanduser("~/atis_state/last_atis_letter.txt")

# KDVT runway headings — add more if needed
RUNWAY_HEADINGS = {
    "7L":  74,  "25R": 254,
    "7R":  74,  "25L": 254,
    "7":   74,  "25":  254,
}


# ──────────────────────────────────────────────
# WIND CALCULATIONS
# ──────────────────────────────────────────────

def parse_wind(full_text):
    """
    Extract wind direction and speed from ATIS text.
    Handles:
      - "270 at 8" / "270 at 8 gusting 15"
      - METAR style "27008KT" / "27008G15KT"
      - "calm"
    Returns (direction_deg, speed_kt) or (None, 0) for calm, (None, None) if unparseable.
    """
    # Plain-English style: "270 at 8"
    m = re.search(r'wind[s]?\s+(\d{3})\s+at\s+(\d+)', full_text, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))

    # METAR style: "27008KT" or "27008G15KT"
    m = re.search(r'\b(\d{3})(\d{2,3})(?:G\d+)?KT\b', full_text, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))

    # Calm winds
    if re.search(r'\bcalm\b', full_text, re.IGNORECASE):
        return None, 0

    return None, None  # Unparseable


def calc_wind_components(wind_dir, wind_speed, runway_heading):
    """
    Returns (headwind_kt, crosswind_kt).
    Positive headwind = into the wind (good).
    Positive crosswind = from the right, negative = from the left.
    """
    angle     = math.radians(wind_dir - runway_heading)
    headwind  = round(wind_speed * math.cos(angle), 1)
    crosswind = round(wind_speed * math.sin(angle), 1)
    return headwind, crosswind


def wind_components_summary(full_text, runways_in_use):
    """Build a formatted wind component block for all active runways."""
    wind_dir, wind_speed = parse_wind(full_text)

    if wind_speed == 0:
        return "🌬️ *Wind Components:* Calm — no crosswind component."

    if wind_dir is None or wind_speed is None:
        return "🌬️ *Wind Components:* Could not parse wind data from ATIS."

    lines = [f"🌬️ *Wind Components* (winds {wind_dir:03d}° at {wind_speed}kt):"]

    for rwy in runways_in_use:
        heading = RUNWAY_HEADINGS.get(rwy.upper())
        if heading is None:
            lines.append(f"  • Runway {rwy}: heading unknown — skipped.")
            continue

        hw, xw = calc_wind_components(wind_dir, wind_speed, heading)

        hw_label = f"{abs(hw)}kt {'headwind ✅' if hw >= 0 else 'tailwind ⚠️'}"
        xw_label = f"{abs(xw)}kt from the {'right' if xw >= 0 else 'left'}"

        lines.append(f"  • Rwy {rwy} ({heading:03d}°): {hw_label} | {xw_label}")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# RUNWAY PARSING
# ──────────────────────────────────────────────

def parse_runways_from_text(full_text):
    """
    Fallback: scan full ATIS text for runway mentions.
    Matches patterns like: 'Runway 7L', 'Runways 7L and 25R'
    """
    matches = re.findall(
        r'\bRunway[s]?\s+([\d]{1,2}[LRC]?(?:\s+and\s+[\d]{1,2}[LRC]?)*)',
        full_text, re.IGNORECASE
    )
    runways = []
    for match in matches:
        parts = re.split(r'\s+and\s+', match, flags=re.IGNORECASE)
        for p in parts:
            rwy = p.strip().upper()
            if rwy and rwy not in runways:
                runways.append(rwy)
    return runways


# ──────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────

def send_telegram(message):
    url     = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    response = requests.post(url, json=payload, timeout=10)
    response.raise_for_status()


# ──────────────────────────────────────────────
# STATE
# ──────────────────────────────────────────────

def get_last_letter():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return f.read().strip()
    return ""


def save_last_letter(letter):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        f.write(letter)


# ──────────────────────────────────────────────
# TRANSCRIPT
# ──────────────────────────────────────────────

def save_transcript(timestamp, letter, full_text, runways, wind_summary):
    os.makedirs(LOGS_DIR, exist_ok=True)
    label    = letter if letter != "Unknown" else "UNKNOWN"
    filename = os.path.join(LOGS_DIR, f"atis_{timestamp}_{label}.txt")
    with open(filename, "w") as f:
        f.write(f"Timestamp  : {timestamp}\n")
        f.write(f"Letter     : {letter}\n")
        f.write(f"Runways    : {', '.join(runways) if runways else 'Not parsed'}\n")
        f.write(f"{'-' * 40}\n")
        f.write(full_text)
        f.write(f"\n{'-' * 40}\n")
        f.write(wind_summary.replace("*", ""))  # Strip Markdown asterisks for plain text
    print(f"📄 Transcript saved: {filename}")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def run_atis_monitor():
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%SZ")

    # 1. Record 60s of KDVT ATIS
    #    User-Agent spoofing prevents LiveATC 403 blocks from cloud/home IPs
    print("📻 Recording KDVT ATIS...")
    result = subprocess.run([
        'ffmpeg', '-y',
        '-user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36',
        '-i', STREAM_URL,
        '-t', '60',
        '-ar', '16000',
        '-ac', '1',
        '-af', 'highpass=f=200,lowpass=f=3000',
        AUDIO_FILE
    ], capture_output=True, text=True)

    if result.returncode != 0:
        print("❌ ffmpeg failed:")
        print(result.stderr[-2000:])  # Last 2000 chars to avoid wall of text
        raise subprocess.CalledProcessError(result.returncode, 'ffmpeg')

    print("✅ Recording complete.")

    # 2. Transcribe and analyze with Gemini
    print("🧠 Analyzing with Gemini...")
    client      = genai.Client(api_key=API_KEY)
    file_upload = client.files.upload(file=AUDIO_FILE)

    try:
        prompt = """
        Listen to this KDVT ATIS recording and transcribe it accurately.
        Format your response EXACTLY as follows:

        LETTER: [Information Letter, e.g. Alpha]
        TIME: [Observation time Zulu]
        WIND: [Wind direction and speed, e.g. 270 at 8]
        VISIBILITY: [Visibility]
        SKY: [Sky conditions]
        TEMP/DEW: [Temperature and dew point]
        ALTIMETER: [Altimeter setting]
        RUNWAYS IN USE: [List all active runways separated by commas, e.g. 7L, 25R]
        NOTICES: [Any NOTAMs or special notices, or 'None']

        ---
        FULL TRANSCRIPT:
        [Provide the full verbatim ATIS text here]
        """

        response  = client.models.generate_content(
            model='gemini-2.0-flash',
            contents=[prompt, file_upload]
        )
        full_text = response.text

        # 3. Parse ATIS letter
        current_letter = "Unknown"
        match = re.search(r'^LETTER:\s*([A-Z][a-z]*)', full_text, re.MULTILINE)
        if match:
            current_letter = match.group(1).strip()

        # 4. Parse active runways — structured field first, then full-text fallback
        runways_in_use = []
        rwy_line = re.search(r'^RUNWAYS IN USE:\s*(.+)', full_text, re.IGNORECASE | re.MULTILINE)
        if rwy_line:
            runways_in_use = re.findall(r'\b(\d{1,2}[LRC]?)\b', rwy_line.group(1).upper())
        if not runways_in_use:
            runways_in_use = parse_runways_from_text(full_text)

        print(f"📋 ATIS Letter    : {current_letter}")
        print(f"🛬  Runways in use : {', '.join(runways_in_use) if runways_in_use else 'Not detected'}")

        # 5. Calculate wind components for active runways
        wind_summary = wind_components_summary(full_text, runways_in_use)
        print(wind_summary.replace("*", ""))

        # 6. Always save the transcript regardless of whether letter changed
        save_transcript(timestamp, current_letter, full_text, runways_in_use, wind_summary)

        # 7. Compare with last known letter — only notify on a real change
        last_letter = get_last_letter()

        if current_letter == "Unknown":
            print("⚠️  Could not parse ATIS letter. Skipping notification and state update.")

        elif current_letter == last_letter:
            print(f"🔇 No change — still Information {current_letter}. No message sent.")

        else:
            print(f"🆕 New ATIS: {current_letter} (was: {last_letter or 'none'}). Notifying...")
            message = (
                f"✈️ *KDVT ATIS — Information {current_letter}*\n\n"
                f"{full_text}\n\n"
                f"{wind_summary}"
            )
            send_telegram(message)
            save_last_letter(current_letter)
            print("📨 Telegram message sent and state updated.")

    finally:
        # Clean up Gemini-hosted file
        try:
            client.files.delete(name=file_upload.name)
            print("🗑️  Cleaned up Gemini file.")
        except Exception as e:
            print(f"⚠️  Could not delete Gemini file: {e}")

        # Always delete the local temp audio file
        if os.path.exists(AUDIO_FILE):
            os.remove(AUDIO_FILE)
            print("🗑️  Deleted temp audio file.")


if __name__ == "__main__":
    run_atis_monitor()
