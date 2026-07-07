import os
import re
import math
import time
import subprocess
import requests
import json
import logging
from datetime import datetime, timezone
from google import genai

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("atis_master")

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
            logger.info(f"Sent to Telegram ID: {chat_id}")
        except requests.exceptions.HTTPError:
            # Markdown parsing can still fail on edge-case input; fall back to plain text
            # so the notification isn't silently dropped.
            logger.warning(f"Markdown send failed for {chat_id} ({resp.status_code}: {resp.text}); retrying as plain text")
            try:
                plain_payload = {"chat_id": chat_id, "text": message}
                requests.post(url, json=plain_payload, timeout=10).raise_for_status()
                logger.info(f"Sent (plain text fallback) to Telegram ID: {chat_id}")
            except Exception as e2:
                logger.error(f"Failed to send to {chat_id} even as plain text: {e2}")
        except Exception as e:
            logger.error(f"Failed to send to {chat_id}: {e}")

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
        logger.info("Pushed update to TRMNL device.")
    except Exception as e:
        logger.error(f"Failed to update TRMNL: {e}")

def call_with_retries(fn, attempts=3, base_delay=5, what="operation"):
    """Call fn() with retries on transient failure (exponential backoff).
    Re-raises the last exception if every attempt fails."""
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt < attempts:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(f"{what} failed (attempt {attempt}/{attempts}): {e}. Retrying in {delay}s...")
                time.sleep(delay)
            else:
                logger.error(f"{what} failed after {attempts} attempts: {e}")
    raise last_exc

def check_atis_time_freshness(time_z, max_age_minutes=90):
    """Sanity-check that the transcribed Zulu time (e.g. '1253Z') is recent,
    to help flag a garbled/hallucinated transcription rather than silently
    sending a stale-looking ATIS update. Returns a warning string or None."""
    m = re.match(r'^(\d{2})(\d{2})Z?$', str(time_z).strip(), re.IGNORECASE)
    if not m:
        return None  # can't parse; not fatal, just skip the check

    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return f"Transcribed time '{time_z}' is not a valid HHMM time"

    from datetime import timedelta
    now = datetime.now(timezone.utc)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # ATIS time could be just before/after UTC midnight relative to "now";
    # check yesterday/today/tomorrow variants and take the closest.
    diffs = [abs((now - (candidate + timedelta(days=d))).total_seconds()) / 60 for d in (-1, 0, 1)]
    age_minutes = min(diffs)

    if age_minutes > max_age_minutes:
        return f"Transcribed time '{time_z}' is {age_minutes:.0f} min from current UTC ({now.strftime('%H%MZ')}); possible transcription error"
    return None

# --- MAIN LOGIC ---
def run_atis_monitor():
    logger.info("Recording KDVT ATIS...")
    try:
        subprocess.run([
            'ffmpeg', '-y', '-user_agent', 'Mozilla/5.0',
            '-i', STREAM_URL, '-t', '120', '-ar', '16000', '-ac', '1',
            '-af', 'highpass=f=200,lowpass=f=3000', AUDIO_FILE
        ], capture_output=True, check=True)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace") if e.stderr else ""
        logger.error(f"ffmpeg recording failed: {e}\n{stderr}")
        return
    except FileNotFoundError:
        logger.error("ffmpeg is not installed or not on PATH.")
        return

    client = None
    file_upload = None
    response = None

    try:
        client = genai.Client(api_key=API_KEY)
        file_upload = client.files.upload(file=AUDIO_FILE)

        # Some google-genai SDK versions process the upload asynchronously;
        # poll until it's ACTIVE (or FAILED) before referencing it in generate_content.
        wait_start = time.time()
        while getattr(file_upload, "state", None) is not None and str(file_upload.state) not in ("ACTIVE", "State.ACTIVE"):
            if str(getattr(file_upload, "state", "")) in ("FAILED", "State.FAILED"):
                raise RuntimeError(f"Gemini file upload failed to process: {file_upload}")
            if time.time() - wait_start > 60:
                raise TimeoutError("Timed out waiting for Gemini file upload to become ACTIVE")
            time.sleep(2)
            file_upload = client.files.get(name=file_upload.name)

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

        response = call_with_retries(
            lambda: client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[prompt, file_upload]
            ),
            attempts=3, base_delay=5, what="Gemini generate_content call"
        )

        # Strip any markdown code fence Gemini might wrap the JSON in, whether
        # or not it's tagged "```json" (dict.get(key, default) below still
        # wouldn't protect us if the model emits JSON `null` for a key it *does*
        # include, since that's a present key with value None, not a missing key).
        json_text = re.sub(r'^```(?:json)?\s*|\s*```$', '', response.text.strip())
        data = json.loads(json_text)

        if not isinstance(data, dict):
            logger.error(f"Gemini returned JSON that wasn't an object (got {type(data).__name__}): {json_text[:300]}")
            return

        # Use `or` (not just .get(key, default)) so an explicit JSON null for a
        # present key also falls back to the default, instead of propagating
        # None into code that assumes a string (e.g. letter.capitalize(),
        # wind_text.lower(), re.findall on runways).
        letter = (data.get("letter") or "None").capitalize()
        if letter.lower() == "none" or not letter:
            logger.info("Tower closed or no letter. Skipping notification.")
            return

        time_z   = data.get("time") or "N/A"
        wind     = data.get("wind") or "N/A"
        vis      = data.get("vis") or "N/A"
        sky      = data.get("sky") or "N/A"
        temp     = data.get("temp") or "N/A"
        alt      = data.get("altimeter") or "N/A"
        rwys_raw = data.get("runways") or ""
        notams   = data.get("notams") or "N/A"

        freshness_warning = check_atis_time_freshness(time_z)
        if freshness_warning:
            logger.warning(freshness_warning)

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
            logger.info(f"Sent Information {letter}")
        else:
            logger.info(f"No change (Information {letter}).")

    except json.JSONDecodeError as e:
        raw = response.text if response is not None else "<no response>"
        logger.error(f"Failed to parse JSON from Gemini: {e}\nRaw output: {raw}")
    except Exception as e:
        logger.exception(f"Error: {e}")

    finally:
        if client and file_upload:
            try: client.files.delete(name=file_upload.name)
            except Exception as e: logger.warning(f"Failed to delete uploaded file: {e}")
        if os.path.exists(AUDIO_FILE): os.remove(AUDIO_FILE)

if __name__ == "__main__":
    run_atis_monitor()
