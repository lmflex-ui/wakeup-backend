import os
import datetime
import json
import anthropic
from flask import Flask, jsonify, request
from flask_cors import CORS
from googleapiclient.discovery import build
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

alarm_state = {
    "active": False,
    "confirmed": False,
    "escalation_level": 1,
    "message": "",
    "events": [],
    "reminders": "",
    "alarm_hour": None,
    "alarm_minute": None
}

def get_calendar_events():
    from google.oauth2 import service_account
    service_account_info = json.loads(os.getenv('GOOGLE_SERVICE_ACCOUNT'))
    creds = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=['https://www.googleapis.com/auth/calendar.readonly']
    )
    service = build('calendar', 'v3', credentials=creds)
    now = (datetime.datetime.utcnow() - datetime.timedelta(hours=12)).isoformat() + 'Z'
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
            start = event.get('start', '')
            if isinstance(start, dict):
                start = start.get('dateTime', start.get('date', ''))
            try:
                dt = datetime.datetime.fromisoformat(start.replace('Z', '+00:00'))
                time_str = dt.strftime('%I:%M %p')
            except:
                time_str = start
            events_text += f"- {event.get('summary', 'Event')} at {time_str}\n"
    else:
        events_text = "No specific events scheduled today."

    tones = {
        1: "friendly and real, like a friend who knows your life. Mention the most important thing today.",
        2: "more insistent and urgent. Emphasize what's at stake. They need to get up NOW.",
        3: "very direct. Staying in bed is not an option. Get specific about consequences.",
        4: "extremely urgent. Final warning. Be relentless. Do not let them go back to sleep."
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
- Speak directly, second person
- No emojis, no special characters, no markdown
- Sound like a real person, not a robot
- Make them WANT to or NEED to get up"""
        }]
    )
    return message.content[0].text

def verify_photo(image_data):
    client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
    if ',' in image_data:
        image_data = image_data.split(',')[1]
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_data}},
                {"type": "text", "text": "Does this photo show a kitchen, coffee maker, coffee machine, kitchen counter, or any kitchen appliance? Answer only YES or NO."}
            ]
        }]
    )
    return "YES" in message.content[0].text.strip().upper()

def do_trigger_alarm():
    global alarm_state
    print(f"Alarm triggered at {datetime.datetime.now()}")
    try:
        events = get_calendar_events()
        alarm_state["events"] = [
            {"summary": e.get("summary", "Event"), "start": e["start"].get("dateTime", e["start"].get("date", ""))}
            for e in events
        ]
    except Exception as e:
        print(f"Calendar error: {e}")
        alarm_state["events"] = []
    message = generate_wakeup_message(alarm_state["events"], 1, alarm_state.get("reminders", ""))
    alarm_state["active"] = True
    alarm_state["confirmed"] = False
    alarm_state["escalation_level"] = 1
    alarm_state["message"] = message

def do_escalate():
    global alarm_state
    if alarm_state["active"] and not alarm_state["confirmed"]:
        level = min(alarm_state["escalation_level"] + 1, 4)
        alarm_state["escalation_level"] = level
        events = alarm_state.get("events", [])
        message = generate_wakeup_message(events, level, alarm_state.get("reminders", ""))
        alarm_state["message"] = message
        print(f"Escalated to level {level}")

def check_alarm_time():
    global alarm_state
    if alarm_state["alarm_hour"] is None:
        return
    if alarm_state["active"] or alarm_state["confirmed"]:
        return
    now = datetime.datetime.utcnow()
    if now.hour == alarm_state["alarm_hour"] and now.minute == alarm_state["alarm_minute"]:
        print(f"Auto-triggering alarm at {now}")
        do_trigger_alarm()

@app.route('/api/status')
def status():
    return jsonify({
        "active": alarm_state["active"],
        "confirmed": alarm_state["confirmed"],
        "escalation_level": alarm_state["escalation_level"],
        "message": alarm_state["message"],
        "events": alarm_state["events"]
    })

@app.route('/api/set-alarm', methods=['POST'])
def set_alarm():
    global alarm_state
    data = request.get_json()
    utc_time = data.get('time', '06:30')
    reminders = data.get('reminders', '')
    hour, minute = map(int, utc_time.split(':'))
    alarm_state["alarm_hour"] = hour
    alarm_state["alarm_minute"] = minute
    alarm_state["reminders"] = reminders
    alarm_state["active"] = False
    alarm_state["confirmed"] = False
    alarm_state["escalation_level"] = 1
    alarm_state["message"] = ""
    alarm_state["events"] = []
    print(f"Alarm set for {hour:02d}:{minute:02d} UTC")
    return jsonify({"success": True})

@app.route('/api/test', methods=['POST'])
def test_alarm():
    global alarm_state
    alarm_state["active"] = False
    alarm_state["confirmed"] = False
    do_trigger_alarm()
    return jsonify({"success": True, "message": "Test alarm triggered"})

@app.route('/api/reset', methods=['POST'])
def reset_alarm():
    global alarm_state
    alarm_state["active"] = False
    alarm_state["confirmed"] = False
    alarm_state["escalation_level"] = 1
    alarm_state["message"] = ""
    return jsonify({"success": True})

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

@app.route('/api/upcoming')
def upcoming():
    try:
        events = get_calendar_events()
        return jsonify({"events": [
            {"summary": e.get("summary", "Event"), "start": e["start"].get("dateTime", e["start"].get("date", ""))}
            for e in events
        ]})
    except Exception as ex:
        return jsonify({"events": [], "error": str(ex)})

@app.route('/health')
def health():
    return jsonify({"status": "ok", "time": datetime.datetime.utcnow().isoformat()})

scheduler = BackgroundScheduler()
scheduler.add_job(check_alarm_time, 'interval', minutes=1, id='check_alarm')
scheduler.add_job(do_escalate, 'interval', minutes=5, id='escalation')
scheduler.start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
