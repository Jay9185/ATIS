import os
import re
import math
import subprocess
import requests
import json
from datetime import datetime
from google import genai

# --- CONFIGURATION ---
API_KEY           = os.getenv("GEMINI_API_KEY",    "YOUR_GEMINI_API_KEY_HERE")
BOT_TOKEN         = os.getenv("TELEGRAM_BOT_TOKEN","YOUR_TELEGRAM_BOT_TOKEN_HERE")
TRMNL_WEBHOOK_URL = os.getenv("TRMNL_WEBHOOK_URL", "")

CHAT_IDS_RAW      = os.getenv("TELEGRAM_CHAT_IDS", "YOUR_ID_1")
CHAT_IDS          = [cid.strip() for cid in CHAT_IDS_RAW.split(",") if cid.strip()]

STREAM_URL        = "http://s1-fmt2.liveatc.net/kdvt3_atis"
AUDIO_FILE        = "/tmp/atis_temp.mp3"
STATE_FILE        = "last_atis_letter.txt"

RUNWAY_HEADINGS = {
    "7L":  74,  "25R": 254,
    "7R":  74,  "25L": 254,
    "7":   74,  "25":  254,
}

# --- HELPERS ---
def parse_wind(wind_text):
    if "calm" in wind_text.lower(): 
        return None, 0, None
        
    m_var = re.search(r'(?:variable|vrb)\s*(?:at|@|\-)?\s*(\d+)(?:.*?(?:g|gust|gusts)\s*(?:to\s*)?(\d+))?', wind_text, re.IGNORECASE)
    if m_var:
        spd = int(m_var.group(1))
        gust = int(m_var.group(2)) if m_var.group(2) else None
        return "VRB", spd, gust

    m = re.search(r'(\d{3})\s*(?:at|@|\-)\s*(\d+)(?:.*?(?:g|gust|gusts)\s*(?:to\s*)?(\d+))?', wind_text, re.IGNORECASE)
    if m: 
        dir_ = int(m.group(1))
        spd = int(m.group(2))
        gust = int(m.group(3)) if m.group(3) else None
        return dir_, spd, gust

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
        
    if wind_dir == "VRB":
        gust_text = f" (Gusts {gust_speed}kt)" if gust_speed else ""
        return f"Variable winds at {wind_speed}kt{gust_text}. Component calculation N/A."
        
    if wind_dir is None or wind_speed is None:
        return "Wind parsing failed"

    lines = []
    for rwy in runways:
        heading = RUNWAY_HEADINGS.get(rwy.upper())
        if heading:
            hw, xw = calc_wind_components(wind_dir, wind_speed, heading)
            hw_label = f"{abs(hw)}kt {'headwind' if hw >= 0 else 'tailwind'}"
            xw_label = f"{abs(xw)}kt from the {'right' if xw >= 0 else 'left'}"
            
            if gust_speed:
                hw_g, xw_g = calc_wind_components(wind_dir, gust_speed, heading)
                hw_label += f" (Gusts {abs(hw_g)}kt)"
                xw_label += f" (Gusts {abs(xw_g)}kt)"

            lines.append(f"- Rwy {rwy} ({heading:03d}°): {hw_label} | {xw_label}")
    return "\n".join(lines)

def escape_markdown(text):
    """Escape characters that break Telegram's legacy Markdown parser.
    Prevents malformed-entity 400 errors when LLM-generated text (NOTAMs,
    wind remarks, etc.) contains stray _, *, `, or [ characters."""
    if text is None:
        return ""
    text = str(text)
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for chat_id in CHAT_IDS:
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            print(f"Sent to Telegram ID: {chat_id}")
        except requests.exceptions.HTTPError:
            # Markdown parsing can still fail on edge-case input; fall back to plain text
            # so the notification isn't silently dropped.
            print(f"Markdown send failed for {chat_id} ({resp.status_code}: {resp.text}); retrying as plain text")
            try:
                plain_payload = {"chat_id": chat_id, "text": message}
                requests.post(url, json=plain_payload, timeout=10).raise_for_status()
                print(f"Sent (plain text fallback) to Telegram ID: {chat_id}")
            except Exception as e2:
                print(f"Failed to send to {chat_id} even as plain text: {e2}")
        except Exception as e:
            print(f"Failed to send to {chat_id}: {e}")

def send_trmnl_webhook(letter, time_z, wind, vis, sky, temp, alt, rwys_raw, wind_summary, notams):
    if not TRMNL_WEBHOOK_URL:
        return

    payload = {
        "merge_variables": {
            "letter": letter,
            "time": time_z,
            "wind": wind,
            "vis": vis,
            "sky": sky,
            "temp": temp,
            "alt": alt,
            "runways": rwys_raw,
            "wind_summary": wind_summary,
            "notams": notams
        }
    }
    try:
        response = requests.post(TRMNL_WEBHOOK_URL, json=payload, timeout=10)
        response.raise_for_status()
        print("Pushed update to TRMNL device.")
    except Exception as e:
        print(f"Failed to update TRMNL: {e}")

# --- MAIN LOGIC ---
def run_atis_monitor():
    print("Recording KDVT ATIS...")
    try:
        subprocess.run([
            'ffmpeg', '-y', '-user_agent', 'Mozilla/5.0',
            '-i', STREAM_URL, '-t', '120', '-ar', '16000', '-ac', '1',
            '-af', 'highpass=f=200,lowpass=f=3000', AUDIO_FILE
        ], capture_output=True, check=True)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace") if e.stderr else ""
        print(f"ffmpeg recording failed: {e}\n{stderr}")
        return
    except FileNotFoundError:
        print("ffmpeg is not installed or not on PATH.")
        return

    client = None
    file_upload = None

    try:
        client = genai.Client(api_key=API_KEY)
        file_upload = client.files.upload(file=AUDIO_FILE)
        
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
        
        # Strip any markdown code fence Gemini might wrap the JSON in, whether
        # or not it's tagged "```json" (dict.get(key, default) below still
        # wouldn't protect us if the model emits JSON `null` for a key it *does*
        # include, since that's a present key with value None, not a missing key).
        json_text = re.sub(r'^```(?:json)?\s*|\s*```$', '', response.text.strip())
        data = json.loads(json_text)

        # Use `or` (not just .get(key, default)) so an explicit JSON null for a
        # present key also falls back to the default, instead of propagating
        # None into code that assumes a string (e.g. letter.capitalize(),
        # wind_text.lower(), re.findall on runways).
        letter = (data.get("letter") or "None").capitalize()
        if letter.lower() == "none" or not letter:
            print("Tower closed or no letter. Skipping notification.")
            return

        time_z   = data.get("time") or "N/A"
        wind     = data.get("wind") or "N/A"
        vis      = data.get("vis") or "N/A"
        sky      = data.get("sky") or "N/A"
        temp     = data.get("temp") or "N/A"
        alt      = data.get("altimeter") or "N/A"
        rwys_raw = data.get("runways") or ""
        notams   = data.get("notams") or "N/A"

        runways_list = re.findall(r'\b(\d{1,2}[LRC]?)\b', rwys_raw)

        last_letter = ""
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f: last_letter = f.read().strip()

        if letter != last_letter:
            wind_summary = get_wind_summary(wind, runways_list)
            
            msg = (
                f"*KDVT ATIS — Info {letter}*\n"
                f"`------------------------`\n"
                f"*Time:* {escape_markdown(time_z)}\n"
                f"*Wind:* {escape_markdown(wind)}\n"
                f"*Vis:* {escape_markdown(vis)}\n"
                f"*Sky:* {escape_markdown(sky)}\n"
                f"*Temp:* {escape_markdown(temp)}\n"
                f"*Alt:* {escape_markdown(alt)}\n"
                f"*Runways:* {escape_markdown(rwys_raw)}\n\n"
                f"*Wind Components:*\n{wind_summary}\n\n"
                f"*NOTAMs:*\n_{escape_markdown(notams)}_"
            )
            
            send_telegram(msg)
            send_trmnl_webhook(letter, time_z, wind, vis, sky, temp, alt, rwys_raw, wind_summary, notams)
            
            with open(STATE_FILE, "w") as f: f.write(letter)
            print(f"Sent Information {letter}")
        else:
            print(f"No change (Information {letter}).")

    except json.JSONDecodeError as e:
        print(f"Failed to parse JSON from Gemini: {e}\nRaw output: {response.text}")
    except Exception as e:
        print(f"Error: {e}")

    finally:
        if client and file_upload:
            try: client.files.delete(name=file_upload.name)
            except Exception as e: print(f"Failed to delete uploaded file: {e}")
        if os.path.exists(AUDIO_FILE): os.remove(AUDIO_FILE)

if __name__ == "__main__":
    run_atis_monitor()
