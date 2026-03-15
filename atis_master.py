import os
import re
import math
import subprocess
import requests
from datetime import datetime
from google import genai

# --- CONFIGURATION ---
# Use environment variables for security (Secrets in GitHub Actions)
API_KEY       = os.getenv("GEMINI_API_KEY",    "YOUR_GEMINI_API_KEY_HERE")
BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN","YOUR_TELEGRAM_BOT_TOKEN_HERE")
CHAT_ID       = os.getenv("TELEGRAM_CHAT_ID",  "YOUR_TELEGRAM_CHAT_ID_HERE")

STREAM_URL    = "http://s1-fmt2.liveatc.net/kdvt3_atis"
AUDIO_FILE    = "/tmp/atis_temp.mp3"

# FIX 1: Use local directory for GitHub Actions workspace compatibility
STATE_FILE    = "last_atis_letter.txt"

# Standard headings for Deer Valley (KDVT)
RUNWAY_HEADINGS = {
    "7L":  74,  "25R": 254,
    "7R":  74,  "25L": 254,
    "7":   74,  "25":  254,
}

# ──────────────────────────────────────────────
# WIND & RUNWAY HELPERS
# ──────────────────────────────────────────────

def parse_wind(full_text):
    """Extracts wind direction and speed from ATIS text."""
    m = re.search(r'wind[s]?\s+(\d{3})\s+at\s+(\d+)', full_text, re.IGNORECASE)
    if m: return int(m.group(1)), int(m.group(2))
    m = re.search(r'\b(\d{3})(\d{2,3})(?:G\d+)?KT\b', full_text, re.IGNORECASE)
    if m: return int(m.group(1)), int(m.group(2))
    if re.search(r'\bcalm\b', full_text, re.IGNORECASE): return None, 0
    return None, None

def calc_wind_components(wind_dir, wind_speed, runway_heading):
    """Calculates headwind and crosswind components."""
    angle     = math.radians(wind_dir - runway_heading)
    headwind  = round(wind_speed * math.cos(angle), 1)
    crosswind = round(wind_speed * math.sin(angle), 1)
    return headwind, crosswind

def wind_components_summary(full_text, runways_in_use):
    wind_dir, wind_speed = parse_wind(full_text)
    if wind_speed == 0:
        return "🌬️ *Wind Components:* Calm — no crosswind."
    if wind_dir is None or wind_speed is None:
        return "🌬️ *Wind Components:* Parsing failed."

    lines = [f"🌬️ *Wind Components* ({wind_dir:03d}° at {wind_speed}kt):"]
    for rwy in runways_in_use:
        heading = RUNWAY_HEADINGS.get(rwy.upper())
        if heading:
            hw, xw = calc_wind_components(wind_dir, wind_speed, heading)
            hw_label = f"{abs(hw)}kt {'headwind ✅' if hw >= 0 else 'tailwind ⚠️'}"
            xw_label = f"{abs(xw)}kt from the {'right' if xw >= 0 else 'left'}"
            lines.append(f"  • Rwy {rwy} ({heading:03d}°): {hw_label} | {xw_label}")
    return "\n".join(lines)

# ──────────────────────────────────────────────
# CORE LOGIC
# ──────────────────────────────────────────────

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    requests.post(url, json=payload, timeout=10).raise_for_status()

def run_atis_monitor():
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%SZ")

    # FIX 2: Increased recording time to 120s to ensure a full ATIS loop is captured
    print("📻 Recording KDVT ATIS...")
    subprocess.run([
        'ffmpeg', '-y', '-user_agent', 'Mozilla/5.0',
        '-i', STREAM_URL, '-t', '120', '-ar', '16000', '-ac', '1',
        '-af', 'highpass=f=200,lowpass=f=3000', AUDIO_FILE
    ], capture_output=True, check=True)

    print("🧠 Analyzing with Gemini...")
    client = genai.Client(api_key=API_KEY)
    
    # FIX 3: Initialize file_upload outside the try block to prevent UnboundLocalError
    file_upload = None

    try:
        file_upload = client.files.upload(file=AUDIO_FILE)
        
        prompt = """
        Listen to this KDVT ATIS recording and transcribe it.
        Format your response EXACTLY as follows:
        LETTER: [Letter]
        TIME: [Zulu Time]
        WIND: [Wind Direction and Speed]
        VISIBILITY: [Visibility]
        SKY: [Sky Condition]
        ALTIMETER: [Altimeter]
        RUNWAYS IN USE: [Active Runways, e.g. 7L, 7R]
        ---
        FULL TRANSCRIPT:
        [Verbatim Text]
        """

        # FIX 4: Reverted to the stable production model name to prevent 404 errors
        response = client.models.generate_content(
            model='gemini-2.0-flash', 
            contents=[prompt, file_upload]
        )
        full_text = response.text

        # 3. Parse Letter and Runways
        current_letter = "Unknown"
        
        # FIX 5: Made regex case-insensitive to safely catch 'ALPHA' or 'Alpha'
        match = re.search(r'LETTER:\s*([A-Za-z]+)', full_text, re.MULTILINE)
        if match: current_letter = match.group(1).strip().capitalize()

        rwy_match = re.search(r'RUNWAYS IN USE:\s*(.+)', full_text, re.IGNORECASE)
        runways = re.findall(r'\b(\d{1,2}[LRC]?)\b', rwy_match.group(1)) if rwy_match else []

        # 4. State Check and Notify
        last_letter = ""
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f: last_letter = f.read().strip()

        if current_letter != "Unknown" and current_letter != last_letter:
            print(f"🆕 New ATIS Information {current_letter} detected.")
            wind_summary = wind_components_summary(full_text, runways)
            message = f"✈️ *KDVT ATIS — Information {current_letter}*\n\n{full_text}\n\n{wind_summary}"
            send_telegram(message)
            
            # Save the new state natively in the checkout directory
            with open(STATE_FILE, "w") as f: f.write(current_letter)
        else:
            print(f"🔇 No change (Information {current_letter}).")

    except Exception as e:
        print(f"❌ Error during processing: {e}")

    finally:
        # FIX 6: Safe cleanup checks that only run if the upload succeeded
        if file_upload:
            try:
                client.files.delete(name=file_upload.name)
            except Exception as e:
                print(f"⚠️ Failed to delete file from Gemini: {e}")
                
        if os.path.exists(AUDIO_FILE): 
            os.remove(AUDIO_FILE)

if __name__ == "__main__":
    run_atis_monitor()
