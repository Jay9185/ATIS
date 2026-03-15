import os
import re
import math
import subprocess
import requests
from datetime import datetime
from google import genai

# --- CONFIGURATION ---
API_KEY       = os.getenv("GEMINI_API_KEY",    "YOUR_GEMINI_API_KEY_HERE")
BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN","YOUR_TELEGRAM_BOT_TOKEN_HERE")
CHAT_ID       = os.getenv("TELEGRAM_CHAT_ID",  "YOUR_TELEGRAM_CHAT_ID_HERE")

STREAM_URL    = "http://s1-fmt2.liveatc.net/kdvt3_atis"
AUDIO_FILE    = "/tmp/atis_temp.mp3"
STATE_FILE    = os.path.expanduser("~/atis_state/last_atis_letter.txt")

# Stable Model for 2026 environment
MODEL_ID      = "gemini-2.0-flash-001" 

# KDVT Runway Magnetic Headings
RUNWAY_HEADINGS = {
    "7L": 74, "7R": 74, "25L": 254, "25R": 254,
    "7": 74, "25": 254
}

# ──────────────────────────────────────────────
# HELPERS (WIND & RUNWAYS)
# ──────────────────────────────────────────────

def parse_wind(full_text):
    """
    Extracts wind even with gusts. 
    Examples: '270 at 10', '27010KT', '27015G25KT'
    """
    # Pattern 1: '270 at 15'
    m = re.search(r'wind[s]?\s+(\d{3})\s+at\s+(\d+)', full_text, re.IGNORECASE)
    if m: return int(m.group(1)), int(m.group(2))
    
    # Pattern 2: '27015G25KT' or '27010KT'
    m = re.search(r'\b(\d{3})(\d{2,3})(?:G\d+)?KT\b', full_text, re.IGNORECASE)
    if m: return int(m.group(1)), int(m.group(2))
    
    if re.search(r'\bcalm\b', full_text, re.IGNORECASE): return None, 0
    return None, None

def calc_wind_components(wind_dir, wind_speed, rwy_hdg):
    angle = math.radians(wind_dir - rwy_hdg)
    hw = round(wind_speed * math.cos(angle), 1)
    xw = round(wind_speed * math.sin(angle), 1)
    return hw, xw

def get_wind_summary(full_text, runways):
    wd, ws = parse_wind(full_text)
    if ws == 0: return "🌬️ *Wind:* Calm — No crosswind."
    if wd is None or ws is None: return "🌬️ *Wind:* Could not parse components."
    
    lines = [f"🌬️ *Wind Components* ({wd:03d}° @ {ws}kt):"]
    for rwy in runways:
        hdg = RUNWAY_HEADINGS.get(rwy.upper())
        if hdg:
            hw, xw = calc_wind_components(wd, ws, hdg)
            # Crosswind from Left or Right
            side = "Left" if xw < 0 else "Right"
            # Tailwind check
            hw_type = "Headwind ✅" if hw >= 0 else "Tailwind ⚠️"
            lines.append(f" • *Rwy {rwy}:* {abs(hw)}kt {hw_type} | {abs(xw)}kt from {side}")
    return "\n".join(lines)

# ──────────────────────────────────────────────
# MAIN MONITOR LOGIC
# ──────────────────────────────────────────────

def run_atis_monitor():
    # 1. Recording
    print("📻 Recording KDVT ATIS...")
    try:
        subprocess.run([
            'ffmpeg', '-y', '-user_agent', 'Mozilla/5.0',
            '-i', STREAM_URL, '-t', '60', '-ar', '16000', '-ac', '1',
            '-af', 'highpass=f=200,lowpass=f=3000', AUDIO_FILE
        ], capture_output=True, check=True)
        print("✅ Recording successful.")
    except Exception as e:
        print(f"❌ Recording failed: {e}")
        return

    # 2. AI Analysis
    print(f"🧠 Analyzing with Gemini ({MODEL_ID})...")
    client = genai.Client(api_key=API_KEY)
    file_upload = client.files.upload(file=AUDIO_FILE)

    try:
        prompt = """
        Listen to this ATIS recording and provide a verbatim transcription.
        Format your response EXACTLY as follows:

        LETTER: [Single Letter, e.g., Alpha]
        RUNWAYS IN USE: [Runways, e.g., 7L, 7R]
        ---
        TRANSCRIPT:
        [Full Text]
        """

        response = client.models.generate_content(
            model=MODEL_ID,
            contents=[prompt, file_upload]
        )
        full_text = response.text

        # 3. Parsing AI Output
        letter_match = re.search(r'LETTER:\s*(\w+)', full_text, re.I)
        current_letter = letter_match.group(1).strip().capitalize() if letter_match else "Unknown"
        
        rwy_match = re.search(r'RUNWAYS IN USE:\s*(.+)', full_text, re.I)
        runway_list = re.findall(r'\b(\d{1,2}[LRC]?)\b', rwy_match.group(1)) if rwy_match else []

        # 4. State Management
        last_letter = ""
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f: last_letter = f.read().strip()

        # 5. Logic: Only notify if it's a new ATIS letter
        if current_letter != "Unknown" and current_letter != last_letter:
            print(f"🆕 NEW ATIS: Information {current_letter} detected.")
            
            wind_info = get_wind_summary(full_text, runway_list)
            
            # Construct Final Telegram Message
            message = (
                f"✈️ *KDVT ATIS — Information {current_letter}*\n"
                f"━━━━━━━━━━━━━━\n"
                f"{full_text}\n\n"
                f"{wind_info}"
            )
            
            # Send Notification
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            requests.post(url, json={"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"})
            
            # Update State
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w") as f: f.write(current_letter)
            print("📨 Notification sent and state updated.")
        else:
            print(f"🔇 Information {current_letter} is current. No notification needed.")

    except Exception as e:
        print(f"❌ Error during AI processing: {e}")

    finally:
        # Cleanup Cloud and Local Files
        try: client.files.delete(name=file_upload.name)
        except: pass
        if os.path.exists(AUDIO_FILE): os.remove(AUDIO_FILE)
        print("🗑️ Cleanup complete.")

if __name__ == "__main__":
    run_atis_monitor()
