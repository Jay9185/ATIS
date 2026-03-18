import os
import re
import math
import subprocess
import requests
import json
from datetime import datetime
from google import genai

# --- CONFIGURATION ---
API_KEY       = os.getenv("GEMINI_API_KEY",    "YOUR_GEMINI_API_KEY_HERE")
BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN","YOUR_TELEGRAM_BOT_TOKEN_HERE")
CHAT_ID       = os.getenv("TELEGRAM_CHAT_ID",  "YOUR_TELEGRAM_CHAT_ID_HERE")

STREAM_URL    = "http://s1-fmt2.liveatc.net/kdvt3_atis"
AUDIO_FILE    = "/tmp/atis_temp.mp3"
STATE_FILE    = "last_atis_letter.txt"

RUNWAY_HEADINGS = {
    "7L":  74,  "25R": 254,
    "7R":  74,  "25L": 254,
    "7":   74,  "25":  254,
}

# --- HELPERS ---
def parse_wind(wind_text):
    # Upgraded to capture optional gusts (e.g., "250 at 15 gusts 25")
    m = re.search(r'(\d{3})\s*(?:at|@|\-)\s*(\d+)(?:.*?(?:g|gust|gusts)\s*(?:to\s*)?(\d+))?', wind_text, re.IGNORECASE)
    if m: 
        dir_ = int(m.group(1))
        spd = int(m.group(2))
        gust = int(m.group(3)) if m.group(3) else None
        return dir_, spd, gust
    if "calm" in wind_text.lower(): 
        return None, 0, None
    return None, None, None

def calc_wind_components(wind_dir, wind_speed, runway_heading):
    angle = math.radians(wind_dir - runway_heading)
    headwind = round(wind_speed * math.cos(angle), 1)
    crosswind = round(wind_speed * math.sin(angle), 1)
    return headwind, crosswind

def get_wind_summary(wind_text, runways):
    wind_dir, wind_speed, gust_speed = parse_wind(wind_text)
    if wind_speed == 0:
        return "Calm - no crosswind"
    if wind_dir is None or wind_speed is None:
        return "Wind parsing failed"

    lines = []
    for rwy in runways:
        heading = RUNWAY_HEADINGS.get(rwy.upper())
        if heading:
            hw, xw = calc_wind_components(wind_dir, wind_speed, heading)
            hw_label = f"{abs(hw)}kt {'headwind' if hw >= 0 else 'tailwind'}"
            xw_label = f"{abs(xw)}kt from the {'right' if xw >= 0 else 'left'}"
            
            # Add gust components if they exist
            if gust_speed:
                hw_g, xw_g = calc_wind_components(wind_dir, gust_speed, heading)
                hw_label += f" (Gusts {abs(hw_g)}kt)"
                xw_label += f" (Gusts {abs(xw_g)}kt)"

            lines.append(f"- Rwy {rwy} ({heading:03d}°): {hw_label} | {xw_label}")
    return "\n".join(lines)

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    requests.post(url, json=payload, timeout=10).raise_for_status()

# --- MAIN LOGIC ---
def run_atis_monitor():
    print("Recording KDVT ATIS...")
    subprocess.run([
        'ffmpeg', '-y', '-user_agent', 'Mozilla/5.0',
        '-i', STREAM_URL, '-t', '120', '-ar', '16000', '-ac', '1',
        '-af', 'highpass=f=200,lowpass=f=3000', AUDIO_FILE
    ], capture_output=True, check=True)

    client = genai.Client(api_key=API_KEY)
    file_upload = None

    try:
        file_upload = client.files.upload(file=AUDIO_FILE)
        
        # Upgraded prompt with location context and strict JSON formatting
        prompt = """
        Listen to this Phoenix Deer Valley (KDVT) ATIS/ASOS recording.
        You are an expert aviation transcriber. Be highly accurate with weather data and KDVT runway designators (7R, 7L, 25R, 25L).
        Extract the aviation details and return ONLY a valid JSON object with the following exact keys (do not use markdown blocks):
        {
            "letter": "Alpha", 
            "time": "1253Z", 
            "wind": "250 at 15 gusts 20", 
            "vis": "10 SM", 
            "sky": "Clear", 
            "temp": "25/10", 
            "altimeter": "29.92", 
            "runways": "7R, 7L", 
            "notams": "Brief summary here"
        }
        If the tower is closed or no information letter is given, set "letter" to "None".
        """

        response = client.models.generate_content(
            model='gemini-2.5-flash', 
            contents=[prompt, file_upload]
        )
        
        # Clean the response in case the LLM wraps it in markdown
        json_text = response.text.strip().removeprefix("```json").removesuffix("```").strip()
        data = json.loads(json_text)

        # Parse Fields safely via Dictionary Keys
        letter = data.get("letter", "None").capitalize()
        if letter.lower() == "none" or not letter:
            print("Tower closed or no letter. Skipping notification.")
            return

        time_z   = data.get("time", "N/A")
        wind     = data.get("wind", "N/A")
        vis      = data.get("vis", "N/A")
        sky      = data.get("sky", "N/A")
        temp     = data.get("temp", "N/A")
        alt      = data.get("altimeter", "N/A")
        rwys_raw = data.get("runways", "")
        notams   = data.get("notams", "N/A")

        runways_list = re.findall(r'\b(\d{1,2}[LRC]?)\b', rwys_raw)

        # State check
        last_letter = ""
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f: last_letter = f.read().strip()

        if letter != last_letter:
            wind_summary = get_wind_summary(wind, runways_list)
            
            # Markdown Layout
            msg = (
                f"*KDVT ATIS — Info {letter}*\n"
                f"`------------------------`\n"
                f"*Time:* {time_z}\n"
                f"*Wind:* {wind}\n"
                f"*Vis:* {vis}\n"
                f"*Sky:* {sky}\n"
                f"*Temp:* {temp}\n"
                f"*Alt:* {alt}\n"
                f"*Runways:* {rwys_raw}\n\n"
                f"*Wind Components:*\n{wind_summary}\n\n"
                f"*NOTAMs:*\n_{notams}_"
            )
            
            send_telegram(msg)
            with open(STATE_FILE, "w") as f: f.write(letter)
            print(f"Sent Information {letter}")
        else:
            print(f"No change (Information {letter}).")

    except json.JSONDecodeError as e:
        print(f"Failed to parse JSON from Gemini: {e}\nRaw output: {response.text}")
    except Exception as e:
        print(f"Error: {e}")

    finally:
        if file_upload:
            try: client.files.delete(name=file_upload.name)
            except: pass
        if os.path.exists(AUDIO_FILE): os.remove(AUDIO_FILE)

if __name__ == "__main__":
    run_atis_monitor()
