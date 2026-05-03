import os
import datetime
import threading
import base64
import json
import anthropic
from flask import Flask, jsonify, request
from flask_cors import CORS
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

# Global alarm state
alarm_state = {
    "active": False,
    "confirmed": False,
    "escalation_level": 1,
    "message": "",
    "events": [],
    "reminders": ""
}

def get_calendar_events():
    import json
    from google.oauth2 import service_account
    
    service_account_info = json.loads(os.getenv('GOOGLE_SERVICE_ACCOUNT'))
    creds = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=['https://www.googleapis.com/auth/calendar.readonly']
    )
    
    service = build('calendar', 'v3', credentials=creds)
    
    now = datetime.datetime.utcnow().isoformat() + 'Z'
    end = (datetime.datetime.utcnow() + datetime.timedelta(hours=18)).isoformat() + 'Z'
    
    events_result = service.events().list(
        calendarId=os.getenv('GOOGLE_CALENDAR_ID', 'primary'),
        timeMin=now,
        timeMax=end,
        maxResults=10,
        singleEvents=True,
        orderBy='startTime'
    ).execute()
    
    return events_result.get('items', [])

def generate_wakeup_message(events, escalation_level=1, reminders=""):
    client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

    events_text = ""
    if events:
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            # Parse and format time nicely
            try:
                dt = datetime.datetime.fromisoformat(start.replace('Z', '+00:00'))
                time_str = dt.strftime('%I:%M %p')
            except:
                time_str = start
            events_text += f"- {event['summary']} at {time_str}\n"
    else:
        events_text = "No specific events scheduled today."

    tones = {
        1: "friendly and upbeat, like an enthusiastic friend who knows your schedule. Mention the most exciting or important thing happening today.",
        2: "more insistent and urgent. Emphasize what's at stake. They need to get up NOW.",
        3: "very direct and persistent. Make it clear that staying in bed is not an option. Get specific about consequences.",
        4: "extremely urgent. This is the final warning. Be relentless. Do not let them go back to sleep."
    }

    tone = tones.get(escalation_level, tones[4])

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": f"""You are a wake-up assistant speaking directly to someone who needs to get out of bed.
Generate a short spoken wake-up message (2-3 sentences max, under 40 words).

Today's schedule:
{events_text}

Personal reminders:
{reminders if reminders else "None provided."}

Tone: {tone}

Rules:
- Use actual event names and times for urgency
- Speak directly, second person ("you")  
- No emojis, no special characters, no markdown
- Sound like a real person talking, not a robot
- Make them WANT to or NEED to get up"""
        }]
    )

    return message.content[0].text

def verify_photo(image_data):
    """Use Claude vision to verify the photo shows the coffee maker or kitchen"""
    client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
    
    # Remove data URL prefix if present
    if ',' in image_data:
        image_data = image_data.split(',')[1]
    
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": image_data
                    }
                },
                {
                    "type": "text",
                    "text": "Does this photo show a kitchen, coffee maker, coffee machine, kitchen counter, or any kitchen appliance? Answer only YES or NO."
                }
            ]
        }]
    )
    
    response = message.content[0].text.strip().upper()
    return "YES" in response

def trigger_alarm():
    """Called by scheduler at 6:30am"""
    global alarm_state
    print(f"Alarm triggered at {datetime.datetime.now()}")
    
    try:
        events = get_calendar_events()
        alarm_state["events"] = [
            {
                "summary": e.get("summary", "Event"),
                "start": e["start"].get("dateTime", e["start"].get("date"))
            } for e in events
        ]
    except Exception as e:
        print(f"Calendar error: {e}")
        alarm_state["events"] = []

    message = generate_wakeup_message(alarm_state["events"], 1)
    alarm_state["active"] = True
    alarm_state["confirmed"] = False
    alarm_state["escalation_level"] = 1
    alarm_state["message"] = message

def escalate_alarm():
    """Called every 5 minutes if not confirmed"""
    global alarm_state
    if alarm_state["active"] and not alarm_state["confirmed"]:
        level = min(alarm_state["escalation_level"] + 1, 4)
        alarm_state["escalation_level"] = level
        events = alarm_state.get("events", [])
        
        # Reconstruct event objects for Claude
        event_objs = [{"summary": e["summary"], "start": {"dateTime": e["start"]}} for e in events]
        message = generate_wakeup_message(event_objs, level)
        alarm_state["message"] = message
        print(f"Escalated to level {level}: {message}")

# ── Routes ──────────────────────────────────────────────

@app.route('/api/status')
def status():
    return jsonify(alarm_state)

@app.route('/api/confirm', methods=['POST'])
def confirm():
    global alarm_state
    data = request.get_json()
    image_data = data.get('image', '')
    
    if not image_data:
        return jsonify({"success": False, "message": "No image provided"}), 400

    verified = verify_photo(image_data)
    
    if verified:
        alarm_state["active"] = False
        alarm_state["confirmed"] = True
        return jsonify({"success": True, "message": "Confirmed! Good morning. Go get that coffee."})
    else:
        return jsonify({"success": False, "message": "That doesn't look like the kitchen. Try again."}), 400

@app.route('/api/test', methods=['POST'])
def test_alarm():
    """Trigger a test alarm immediately"""
    global alarm_state
    trigger_alarm()
    return jsonify({"success": True, "message": "Test alarm triggered"})

@app.route('/health')
def health():
    return jsonify({"status": "ok", "time": datetime.datetime.now().isoformat()})

@app.route('/api/reminders', methods=['POST'])
def set_reminders():
    global alarm_state
    data = request.get_json()
    alarm_state["reminders"] = data.get("reminders", "")
    return jsonify({"success": True})

@app.route('/api/upcoming')
def upcoming():
    try:
        events = get_calendar_events()
        return jsonify({"events": [{"summary": e.get("summary","Event"), "start": e["start"].get("dateTime", e["start"].get("date"))} for e in events]})
    except Exception as ex:
        return jsonify({"events": [], "error": str(ex)})

@app.route('/api/set-alarm', methods=['POST'])
def set_alarm():
    global alarm_state
    data = request.get_json()
    wake_time = data.get('time', '06:30')
    reminders = data.get('reminders', '')
    alarm_state['reminders'] = reminders
    hour, minute = map(int, wake_time.split(':'))
    scheduler.reschedule_job('main_alarm', trigger='cron', hour=hour, minute=minute)
    return jsonify({"success": True, "time": wake_time})

# ── Scheduler ───────────────────────────────────────────

scheduler = BackgroundScheduler()

# Main alarm - 6:30am every day
scheduler.add_job(trigger_alarm, 'cron', hour=16, minute=10, id='main_alarm')

# Escalation checks every 5 minutes
scheduler.add_job(escalate_alarm, 'interval', minutes=5, id='escalation')

scheduler.start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
