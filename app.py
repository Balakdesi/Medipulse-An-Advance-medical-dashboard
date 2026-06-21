# pip install flask flask-cors requests groq werkzeug
#pip install python-dotenv
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from groq import Groq
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import time
import requests
import threading
import os
import socket
from datetime import datetime

load_dotenv()

app = Flask(__name__)
CORS(app) 

# 2. Fetch the keys securely from the environment
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'vault_records')
os.makedirs(UPLOAD_FOLDER, exist_ok=True) 

for filename in os.listdir(UPLOAD_FOLDER):
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    try:
        if os.path.isfile(file_path):
            os.remove(file_path)
    except Exception:
        pass

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024 

groq_client = Groq(api_key=GROQ_API_KEY)

try:
    with open("systemprompt.txt", "r", encoding="utf-8") as file:
        BASE_SYSTEM_PROMPT = file.read()
except FileNotFoundError:
    BASE_SYSTEM_PROMPT = "You are a helpful medical AI assistant. Answer concisely."

now_ts = time.time()
glucose_history = [
    {"timestamp": now_ts - 900, "value": 135.0, "source": "Deterministic Sensor"}, 
    {"timestamp": now_ts - 450, "value": 118.0, "source": "Deterministic Sensor"}, 
    {"timestamp": now_ts,       "value": 102.0, "source": "Deterministic Sensor"}  
]

system_state = {
    "countdown_active": False,
    "countdown_seconds_left": 30,
    "emergency_dispatched": False,
    "override_triggered": False,
    "high_alert_dispatched": False,
    "latest_report_text": "No reports uploaded yet.",
    "tir_score": 100,           
    "dka_warning": False,        
    "dka_alert_dispatched": False,
    "xai_telemetry_log": "[Initialization] Continuous telemetry parsing engine online.",
    "velocity_trend": "0.00 mg/dL/min"
}

countdown_thread = None

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Alert routing exception: {e}")

def run_countdown():
    global system_state
    while system_state["countdown_active"] and system_state["countdown_seconds_left"] > 0:
        time.sleep(1)
        if not system_state["countdown_active"]:
            return 
        system_state["countdown_seconds_left"] -= 1
        
    if system_state["countdown_seconds_left"] <= 0 and system_state["countdown_active"]:
        system_state["countdown_active"] = False
        system_state["emergency_dispatched"] = True
        latest_val = glucose_history[-1]["value"] if glucose_history else "Unknown"
        msg = (
            "🚨 *CRITICAL HYPOGLYCEMIA ALERT* 🚨\n\n"
            f"Patient is unresponsive. Current blood sugar is critically low at *{latest_val} mg/dL*. "
            "Emergency override window expired. Immediate assistance required!"
        )
        send_telegram_alert(msg)

def analyze_trends():
    global countdown_thread, system_state 
    if len(glucose_history) < 2: 
        return

    glucose_history.sort(key=lambda x: x['timestamp'])
    
    p2 = glucose_history[-1]
    
    p1 = glucose_history[-2]
    for point in reversed(glucose_history[:-1]):
        if (p2['timestamp'] - point['timestamp']) >= 30:
            p1 = point
            break
    
    dt_minutes = (p2['timestamp'] - p1['timestamp']) / 60.0
    
    if dt_minutes < 1.0:
        dt_minutes = 1.0
        
    velocity = (p2['value'] - p1['value']) / dt_minutes
    system_state["velocity_trend"] = f"{velocity:+.2f} mg/dL/min"

    latest_val = p2["value"]
    in_range_count = sum(1 for r in glucose_history if 70 <= r["value"] <= 180)
    system_state["tir_score"] = int((in_range_count / len(glucose_history)) * 100)
    projected_glucose = latest_val + (velocity * 20.0)
    
    system_state["xai_telemetry_log"] = (
        f"[Kinematic Analysis] Live value: {latest_val} mg/dL | Velocity: {velocity:+.2f} mg/dL/min. "
        f"Projected value in 20 mins: {projected_glucose:.1f} mg/dL."
    )

    if latest_val >= 250:
        system_state["dka_warning"] = True
        system_state["high_alert_dispatched"] = False 
        if not system_state["dka_alert_dispatched"]:
            msg = f"☣️ *DKA RISK PROTOCOL ACTIVATED* ☣️\nBlood sugar critically high ({latest_val} mg/dL). Check for urinary ketones immediately. Aggressive hydration required."
            send_telegram_alert(msg)
            system_state["dka_alert_dispatched"] = True
    else:
        system_state["dka_warning"] = False
        system_state["dka_alert_dispatched"] = False

    if 180 < latest_val < 250 and not system_state["high_alert_dispatched"]:
        msg = f"⚠️ *HYPERGLYCEMIA WARNING*\nPatient's blood sugar has entered the Caution Zone at *{latest_val} mg/dL*."
        send_telegram_alert(msg)
        system_state["high_alert_dispatched"] = True
    elif latest_val <= 180:
        system_state["high_alert_dispatched"] = False

    is_crashing = (latest_val <= 70 or projected_glucose <= 55)
    if is_crashing and not system_state["countdown_active"] and not system_state["emergency_dispatched"] and not system_state["override_triggered"]:
        system_state["countdown_active"] = True
        system_state["countdown_seconds_left"] = 30
        system_state["xai_telemetry_log"] += " [ALERT] Critical crash projection hit threshold. Initiating Human-In-The-Loop confirmation window."
        countdown_thread = threading.Thread(target=run_countdown)
        countdown_thread.start()       

@app.route('/')
def home():
    return send_file('index.html')

@app.route('/api/status', methods=['GET'])
def get_status():
    return jsonify({"history": glucose_history, "state": system_state, "host_ip": get_local_ip()})

@app.route('/api/log', methods=['POST'])
def log_glucose():
    global system_state
    data = request.json
    
    if data.get('time'):
        try:
            record_time = datetime.fromisoformat(data['time']).timestamp()
        except Exception:
            record_time = time.time()
    else:
        record_time = time.time()

    bg_val = data.get('value')
    if bg_val is not None:
        bg_val = float(bg_val)
    else:
        bg_val = glucose_history[-1]['value'] if glucose_history else 100.0

    if data.get('carbs'):
        bg_val += (float(data['carbs']) * 0.5)
    if data.get('insulin'):
        bg_val -= (float(data['insulin']) * 2.0)

    if bg_val < 20 or bg_val > 600:
        system_state["xai_telemetry_log"] = f"[GUARDRAIL TRIGGERED] Suppressed impossible medical outlier: {bg_val} mg/dL. Reverting timeline anomaly."
        return jsonify({"status": "error", "message": "Out of biological safety boundaries"}), 400

    new_reading = {
        "timestamp": record_time,
        "value": float(bg_val),
        "source": data.get('source', 'Manual Sync Entry')
    }
    
    glucose_history.append(new_reading)
    system_state["override_triggered"] = False 
    system_state["emergency_dispatched"] = False
    analyze_trends()
    return jsonify({"status": "success"})

@app.route('/dossier/<patient_id>')
def serve_dossier(patient_id):
    latest_val = glucose_history[-1]['value'] if glucose_history else "--"
    is_dka = system_state["dka_warning"]
    is_crash = latest_val != "--" and float(latest_val) <= 70

    status = "STABLE"
    color = "#2e7d32"
    if is_dka:
        status = "DKA RISK DETECTED"
        color = "#8e44ad"
    elif is_crash:
        status = "CRITICAL CRASH"
        color = "#d32f2f"

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Emergency Dossier</title>
    </head>
    <body style="font-family: sans-serif; background: #f4f7f9; padding: 20px; color: #333;">
        <div style="background: white; padding: 20px; border-radius: 10px; border-top: 5px solid {color}; box-shadow: 0 4px 10px rgba(0,0,0,0.1);">
            <h2 style="color: {color}; margin-top: 0; text-align: center;">🚑 MediPulse Emergency Handoff Dossier</h2>
            <hr style="border: 0; height: 1px; background: #eee; margin-bottom: 20px;">
            <p style="font-size: 14px; color: #666; margin: 5px 0;">Patient Reference Tracking ID:</p>
            <p style="font-size: 18px; font-weight: bold; margin: 0 0 15px 0;">{patient_id}</p>
            <div style="text-align: center; margin: 30px 0;">
                <p style="font-size: 14px; color: #666; margin: 0;">Live Extracted Blood Sugar</p>
                <h1 style="font-size: 4em; color: {color}; margin: 5px 0;">{latest_val}</h1>
                <span style="font-size: 18px; color: #999;">mg/dL</span>
            </div>
            <div style="background: #f8fcfd; padding: 15px; border-radius: 8px; border: 1px solid #eef2f5;">
                <p style="margin: 5px 0;"><strong>System Condition State:</strong> <span style="color: {color}; font-weight: bold;">{status}</span></p>
                <p style="margin: 5px 0;"><strong>Calculated Time in Range:</strong> {system_state['tir_score']}%</p>
                <p style="margin: 5px 0;"><strong>Latest Trend Vector:</strong> {system_state['velocity_trend']}</p>
            </div>
            <p style="text-align: center; color: #999; font-size: 11px; margin-top: 25px;">Securely transmitted via offline verification architecture layers.</p>
        </div>
    </body>
    </html>
    """

@app.route('/api/override', methods=['POST'])
def human_override():
    global system_state
    system_state["countdown_active"] = False
    system_state["countdown_seconds_left"] = 30
    system_state["override_triggered"] = True 
    system_state["xai_telemetry_log"] = "[Human Override Executed] Countdown stopped. Clinical safety control intercepted by patient."
    return jsonify({"status": "cancelled"})

import json 

@app.route('/api/save_report', methods=['POST'])
def save_report():
    global system_state
    data = request.json
    system_state["latest_report_text"] = data.get("text", "")
    return jsonify({"status": "saved"})

import re

@app.route('/api/vision/analyze', methods=['POST'])
def analyze_vision():
    global system_state
    data = request.json
    base64_image = data.get("image")
    mime_type = data.get("mime_type", "image/jpeg") 
    
    if not base64_image:
        return jsonify({"error": "No visual payload received"}), 400

    vision_prompt = """
    You are an expert clinical AI. Analyze this medical lab report image.
    Extract the active glucose reading and provide a short clinical summary.
    You MUST respond with a perfectly formatted JSON object exactly like this:
    {
        "extracted_glucose": 110,
        "confidence_score": 0.98,
        "summary": "Patient shows normal fasting glucose levels..."
    }
    If no glucose is found, set "extracted_glucose" to null.
    """

    try:
        vision_completion = groq_client.chat.completions.create(
            messages=[
                {
                    "role": "system", # FIX 1: System MUST be the first message!
                    "content": vision_prompt
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract clinical data from this image. Output strict JSON only."},
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}}
                    ]
                }
            ],
            model="llama-3.2-11b-vision-preview",
            temperature=0.1,
            max_tokens=500
        )
        
        raw_response = vision_completion.choices[0].message.content
        print(f"RAW VISION OUTPUT: {raw_response}") # Helpful to see in your terminal
        
        json_match = re.search(r'\{.*\}', raw_response, re.DOTALL)
        
        if json_match:
            clean_json = json_match.group(0)
            try:
                # FIX 2: Safety net so broken JSON doesn't crash the server
                analysis_data = json.loads(clean_json) 
            except json.JSONDecodeError:
                analysis_data = {"extracted_glucose": None, "confidence_score": 0.0, "summary": "Data extracted, but JSON formatting failed."}
        else:
            analysis_data = {"extracted_glucose": None, "confidence_score": 0.0, "summary": "Failed to parse structural data."}
            
        system_state["latest_report_text"] = analysis_data.get("summary", "Image processed.")
        
        return jsonify(analysis_data)
        
    except Exception as e:
        print(f"VISION API ERROR: {str(e)}") # Prints the exact Groq error to VSCode terminal
        return jsonify({"error": f"Vision engine failed: {str(e)}"}), 500

@app.route('/api/chat', methods=['POST'])
def chat_with_ai():
    data = request.json
    user_message = data.get("message", "")
    
    current_bg = glucose_history[-1]['value'] if glucose_history else "Unknown"
    velocity = system_state.get("velocity_trend", "0.00")
    report_memory = system_state["latest_report_text"]
    
    dynamic_system_prompt = BASE_SYSTEM_PROMPT + f"""
    [CRITICAL SECURITY LAYER - READ ONLY ARCHITECTURE]
    - Patient Live Glucose: {current_bg} mg/dL
    - Live Kinematic Velocity: {velocity}
    - Latest Lab Vision Summary: {report_memory}
    
    You are a predictive clinical specialist. Synthesize the velocity and lab reports to answer the user. Do not prescribe physical medication dosages.
    """
    try:
        chat_completion = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": dynamic_system_prompt},
                {"role": "user", "content": user_message}
            ],
            model="llama-3.1-8b-instant",
            temperature=0.2,
            max_tokens=1024,
        )
        ai_response = chat_completion.choices[0].message.content
        return jsonify({"response": ai_response})
    except Exception as e:
        return jsonify({"error": f"Inference engine offline: {str(e)}"}), 500

@app.route('/api/vault/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files: return jsonify({"error": "No file parameter structural fault"}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({"error": "Null filename validation exception"}), 400
    if file:
        original_filename = secure_filename(file.filename)
        name, ext = os.path.splitext(original_filename)
        unique_filename = f"{name}_{int(time.time())}{ext}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
        file.save(filepath)
        return jsonify({"status": "success", "filename": original_filename})

@app.route('/api/vault/files', methods=['GET'])
def list_files():
    files = []
    for filename in os.listdir(app.config['UPLOAD_FOLDER']):
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if os.path.isfile(filepath):
            files.append({"filename": filename, "timestamp": os.path.getctime(filepath)})
    files.sort(key=lambda x: x['timestamp'], reverse=True)
    return jsonify({"files": files})

if __name__ == '__main__':
    analyze_trends()  
    app.run(host='0.0.0.0', port=5000, debug=True)