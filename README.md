# 🛫 Automated ATIS Monitor (KDVT)

A proactive, serverless automation tool that monitors the Phoenix Deer Valley (KDVT) ATIS broadcast via LiveATC. Instead of manually tuning in or checking for updates, this script uses Google's Gemini AI to listen to the audio stream, parse the weather data, calculate wind components, and send a formatted push notification to Telegram whenever the ATIS information letter changes.

It is designed to run completely hands-off using GitHub Actions.

## ✨ Features

  * **AI-Powered Transcription:** Downloads live audio and uses `gemini-2.5-flash` to transcribe noisy aviation broadcasts into a structured JSON format.
  * **Smart Wind Calculator:** Automatically calculates headwind and crosswind components for active runways based on the broadcasted wind direction and speed. It gracefully handles variable winds (VRB) and calculates maximum components for wind gusts.
  * **State Tracking:** Remembers the last broadcasted ATIS letter (e.g., "Information Alpha") and only sends a notification when a new letter is published.
  * **100% Serverless:** Fully integrated with GitHub Actions to run on a set schedule without needing a dedicated server or local dependencies.
  * **Telegram Integration:** Delivers clean, Markdown-formatted weather briefs directly to your phone.

## 🛠️ Prerequisites

To deploy this project, you will need the following accounts and keys:

  * **GitHub Account:** To host the repository and run Actions.
  * **Telegram Bot:** A bot token and your chat ID (create a bot using [BotFather](https://www.google.com/search?q=https://core.telegram.org/bots/tutorial%23obtain-your-bot-token) on Telegram).
  * **Gemini API Key:** Get a free API key from [Google AI Studio](https://aistudio.google.com/).

## 🚀 Deployment Guide

Because this runs entirely on GitHub Actions, there is no need to install Python, FFmpeg, or any packages on your local computer. Just follow these steps:

### 1\. Fork or Clone the Repository

Upload this code to your own GitHub repository.

### 2\. Add Repository Secrets

The script relies on environment variables to keep your credentials secure. In your GitHub repository, navigate to **Settings** \> **Secrets and variables** \> **Actions** and add the following **Repository Secrets**:

| Secret Name | Description |
| :--- | :--- |
| `GEMINI_API_KEY` | Your Google Gemini API key. |
| `TELEGRAM_BOT_TOKEN` | The token provided by Telegram's BotFather. |
| `TELEGRAM_CHAT_ID` | The ID of the user or group chat where messages should be sent. |

### 3\. Grant Action Permissions

The workflow needs permission to commit the `last_atis_letter.txt` file back to the repository so it remembers the current ATIS letter.

1.  Go to **Settings** \> **Actions** \> **General**.
2.  Scroll down to **Workflow permissions**.
3.  Select **Read and write permissions** and click Save.

### 4\. Enable the Workflow

1.  Go to the **Actions** tab in your repository.
2.  If prompted, click **I understand my workflows, go ahead and enable them**.
3.  You can wait for the next scheduled run, or click on the **Proactive ATIS Monitor** workflow and hit **Run workflow** to test it immediately.

## ⏱️ How the Automation Works

The included `.github/workflows/atis_monitor.yml` handles all system dependencies (like FFmpeg) inside an ephemeral Ubuntu runner.

  * **Schedule:** The monitor checks the LiveATC stream at 5 minutes past the hour.
  * **Sleep Cycle:** To conserve GitHub Action minutes, the workflow is paused between 12:00 AM and 6:00 AM MST (07:00 to 13:00 UTC).
  * **State Commits:** The workflow automatically commits the state file back to your `main` branch so the next run knows if the ATIS has actually changed.

## 📝 Modifying for Other Airports

By default, this script is hardcoded for Phoenix Deer Valley (KDVT). To adapt it for another airport:

1.  Update `STREAM_URL` in `atis_master.py` to the desired LiveATC stream.
2.  Update the `RUNWAY_HEADINGS` dictionary with the specific runways and their magnetic headings.
3.  Modify the prompt inside the script to give the AI the correct airport context.

## 📄 License

This project is licensed under the MIT License. See the LICENSE file for details.
