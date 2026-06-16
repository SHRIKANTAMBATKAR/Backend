import os
import requests
import json
import sys
import base64
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from dotenv import load_dotenv
from utils.treatments import disease_treatments

import psycopg2
import psycopg2.extras
from werkzeug.security import generate_password_hash, check_password_hash
import smtplib
import ssl
from email.message import EmailMessage

from apscheduler.schedulers.background import BackgroundScheduler
import datetime as _dt_global
import atexit

# Load environment variables
load_dotenv()

# Database Configuration (PostgreSQL)
db_config = {
    'host': os.getenv('DB_HOST'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASSWORD'),
    'database': os.getenv('DB_NAME'),
    'port': int(os.getenv('DB_PORT', 5432)), # Default Postgres port falls back to 5432 if not set
}

def get_db_connection():
    try:
        # PostgreSQL SSL settings
        ssl_args = {"sslmode": "require"}
        ca_path = os.path.join(os.path.dirname(__file__), 'ca.pem')
        if os.path.exists(ca_path):
            ssl_args["sslrootcert"] = ca_path
            
        return psycopg2.connect(**db_config, **ssl_args)
    except psycopg2.Error as e:
        print(f"❌ Error connecting to PostgreSQL Database: {e}")
        return None

def init_db():
    print("DEBUG: Initializing database tables (PostgreSQL)...")
    conn = get_db_connection()
    if conn is None:
        return
    
    try:
        with conn.cursor() as cursor:
            # Create users table (SERIAL for autoincrement in Postgres)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Create care_requests table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS care_requests (
                    id SERIAL PRIMARY KEY,
                    farmer_name VARCHAR(255) NOT NULL,
                    mobile_number VARCHAR(20) NOT NULL,
                    crop_name VARCHAR(100) NOT NULL,
                    issue TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Create password reset tokens table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS password_reset_tokens (
                    id SERIAL PRIMARY KEY,
                    email VARCHAR(255) NOT NULL,
                    token VARCHAR(255) UNIQUE NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Create weather subscriptions table for auto-alert system
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS weather_subscriptions (
                    id SERIAL PRIMARY KEY,
                    email VARCHAR(255) NOT NULL,
                    lat DOUBLE PRECISION NOT NULL,
                    lon DOUBLE PRECISION NOT NULL,
                    location_name VARCHAR(255) NOT NULL DEFAULT 'Your Location',
                    last_alerted_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(email)
                )
            """)
        conn.commit()
        print("✅ Database initialized completely.")
    except psycopg2.Error as e:
         print(f"❌ Error formatting database: {e}")
    finally:
        conn.close()

# Handle Windows console encoding issues for emojis
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        # Fallback for older Python versions
        pass

app = Flask(__name__)
CORS(app)

# Initialize database on startup
init_db()

# ──────────────────────────────────────────────────────────────
# BACKGROUND AUTO-ALERT SCHEDULER
# ──────────────────────────────────────────────────────────────
ALERT_COOLDOWN_HOURS = 3  # Don't re-alert the same user within 3 hours

def _auto_weather_check_job():
    """
    Background job: runs every 30 minutes.
    Fetches all weather subscriptions, checks conditions for each,
    and sends alert emails when medium/high severity conditions are detected.
    Enforces a cooldown to avoid spamming.
    """
    print(f"[AutoAlert] Running weather check job at {_dt_global.datetime.utcnow().isoformat()}Z")
    conn = get_db_connection()
    if conn is None:
        print("[AutoAlert] DB connection failed — skipping job.")
        return

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute("SELECT id, email, lat, lon, location_name, last_alerted_at FROM weather_subscriptions")
            subscriptions = cursor.fetchall()

        print(f"[AutoAlert] Found {len(subscriptions)} subscription(s) to check.")

        for sub in subscriptions:
            email         = sub['email']
            lat           = sub['lat']
            lon           = sub['lon']
            location_name = sub['location_name']
            last_alerted  = sub['last_alerted_at']

            # Enforce cooldown
            if last_alerted is not None:
                since_hours = (_dt_global.datetime.utcnow() - last_alerted).total_seconds() / 3600
                if since_hours < ALERT_COOLDOWN_HOURS:
                    print(f"[AutoAlert] Skipping {email} — cooldown ({since_hours:.1f}h < {ALERT_COOLDOWN_HOURS}h)")
                    continue

            # Fetch weather
            try:
                params = {
                    "latitude":  lat,
                    "longitude": lon,
                    "current": [
                        "temperature_2m", "relativehumidity_2m",
                        "precipitation", "windspeed_10m",
                        "weathercode", "apparent_temperature"
                    ],
                    "hourly": [
                        "temperature_2m", "precipitation",
                        "windspeed_10m", "relativehumidity_2m"
                    ],
                    "forecast_days": 2,
                    "timezone": "auto"
                }
                resp = requests.get(OPEN_METEO_BASE, params=params, timeout=10)
                resp.raise_for_status()
                wd = resp.json()
            except Exception as we:
                print(f"[AutoAlert] Weather fetch error for {email}: {we}")
                continue

            # Build hourly list
            hourly_raw = wd.get("hourly", {})
            now_utc    = _dt_global.datetime.utcnow()
            hours_list = []
            for i, t in enumerate(hourly_raw.get("time", [])[:48]):
                try:
                    slot_time = _dt_global.datetime.fromisoformat(t)
                except Exception:
                    continue
                diff_hours = (slot_time - now_utc).total_seconds() / 3600
                if 0 <= diff_hours <= 6:
                    hours_list.append({
                        "temperature_2m":      hourly_raw.get("temperature_2m",     [0]*48)[i] or 0,
                        "precipitation":       hourly_raw.get("precipitation",       [0]*48)[i] or 0,
                        "windspeed_10m":       hourly_raw.get("windspeed_10m",       [0]*48)[i] or 0,
                        "relativehumidity_2m": hourly_raw.get("relativehumidity_2m", [0]*48)[i] or 0,
                    })

            current_raw = wd.get("current", {})
            current = {
                "temperature_2m":       current_raw.get("temperature_2m", 25),
                "apparent_temperature": current_raw.get("apparent_temperature", 25),
                "relativehumidity_2m":  current_raw.get("relativehumidity_2m", 50),
                "precipitation":        current_raw.get("precipitation", 0),
                "windspeed_10m":        current_raw.get("windspeed_10m", 0),
                "weathercode":          current_raw.get("weathercode", 0),
            }

            all_alerts = _condition_engine(current, hours_list)
            bad_alerts = [a for a in all_alerts if a.get("severity") in ("high", "medium")]

            if not bad_alerts:
                print(f"[AutoAlert] {email} @ {location_name}: All clear — no email needed.")
                continue

            # Send alert email
            sent = _send_weather_alert_email(email, location_name, bad_alerts)
            if sent:
                # Update last_alerted_at
                try:
                    conn2 = get_db_connection()
                    if conn2:
                        with conn2.cursor() as cur2:
                            cur2.execute(
                                "UPDATE weather_subscriptions SET last_alerted_at = %s WHERE email = %s",
                                (_dt_global.datetime.utcnow(), email)
                            )
                        conn2.commit()
                        conn2.close()
                except Exception as dbe:
                    print(f"[AutoAlert] DB update error for {email}: {dbe}")
                print(f"[AutoAlert] ✅ Email sent to {email} with {len(bad_alerts)} alert(s).")
            else:
                print(f"[AutoAlert] ❌ Failed to send email to {email}.")

    except Exception as e:
        print(f"[AutoAlert] Unexpected error in job: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# Start scheduler — only once (gunicorn spawns multiple workers; use env flag to avoid duplicates)
if os.environ.get("SCHEDULER_STARTED") != "1":
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        _auto_weather_check_job,
        trigger="interval",
        minutes=30,
        id="auto_weather_alert",
        replace_existing=True,
        next_run_time=_dt_global.datetime.utcnow() + _dt_global.timedelta(seconds=10)  # first run 10s after start
    )
    _scheduler.start()
    atexit.register(lambda: _scheduler.shutdown(wait=False))
    os.environ["SCHEDULER_STARTED"] = "1"
    print("[AutoAlert] ✅ Background scheduler started — checks every 30 minutes.")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")
GEMINI_MODEL = "gemini-2.5-flash"
# Use v1 endpoint as it's more stable for some regions
GEMINI_CHAT_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ANALYSIS_PROMPT_TEMPLATE = """
As an expert agricultural scientist, analyze this image of a crop disease. 
Identify the crop name and the specific disease name. 
If the plant is healthy, state 'Healthy' as the disease name.

Return ONLY valid JSON in this exact format. Do not include any markdown formatting like ```json.
{
  "crop": "Crop Name",
  "disease": "Disease Name",
  "confidence": 95.0,
  "description": "A brief scientific description of the disease/condition.",
  "symptoms": "List of key symptoms visible on the plant.",
  "treatment": "Detailed natural and organic treatment solutions.",
  "prevention": "Practical tips to prevent this disease in the future."
}
"""

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/register", methods=["POST"])
def register_user():
    data = request.get_json(force=True)
    name = data.get("name")
    email = data.get("email")
    password = data.get("password")

    if not name or not email or not password:
         return jsonify({"error": "Missing required fields"}), 400

    hashed_password = generate_password_hash(password)

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Database connection failed"}), 500
    
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            # Check if user already exists
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cursor.fetchone():
                return jsonify({"error": "Email already registered"}), 409

            # Insert new user
            cursor.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (%s, %s, %s)",
                (name, email, hashed_password)
            )
        conn.commit()
        return jsonify({"message": "User registered successfully"}), 201
    except psycopg2.Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route("/api/login", methods=["POST"])
def login_user():
    data = request.get_json(force=True)
    email = data.get("email")
    password = data.get("password")

    if not email or not password:
         return jsonify({"error": "Missing email or password"}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Database connection failed"}), 500

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            # Fetch user
            cursor.execute("SELECT id, name, email, password_hash FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()

            if user and check_password_hash(user['password_hash'], password):
                return jsonify({
                    "message": "Login successful",
                    "user": {
                        "id": user['id'],
                        "name": user['name'],
                        "email": user['email']
                    }
                }), 200
            else:
                 return jsonify({"error": "Invalid email or password"}), 401
    except psycopg2.Error as e:
         return jsonify({"error": str(e)}), 500
    finally:
         conn.close()

@app.route("/api/contact", methods=["POST"])
def contact_expert():
    data = request.get_json(force=True)
    farmer_name = data.get("farmerName")
    mobile_number = data.get("mobileNumber")
    crop_name = data.get("cropName")
    issue = data.get("issue")

    if not all([farmer_name, mobile_number, crop_name, issue]):
        return jsonify({"error": "Missing required fields"}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Database connection failed"}), 500

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(
                "INSERT INTO care_requests (farmer_name, mobile_number, crop_name, issue) VALUES (%s, %s, %s, %s)",
                (farmer_name, mobile_number, crop_name, issue)
            )
        conn.commit()

        # Send Email Notification
        if EMAIL_SENDER and EMAIL_PASSWORD and EMAIL_RECEIVER:
            try:
                msg = EmailMessage()
                msg.set_content(
                    f"New Contact Request from Farmer:\n\n"
                    f"Name: {farmer_name}\n"
                    f"Mobile: {mobile_number}\n"
                    f"Crop: {crop_name}\n\n"
                    f"Issue:\n{issue}"
                )
                msg["Subject"] = f"Smart Krishi Alert: New Form Submission from {farmer_name}"
                msg["From"] = EMAIL_SENDER
                msg["To"] = EMAIL_RECEIVER

                context = ssl.create_default_context()
                with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
                    # Strip quotes if they somehow get included in dotenv parsing
                    srv_pw = EMAIL_PASSWORD.strip('"').strip("'")
                    server.login(EMAIL_SENDER, srv_pw)
                    server.send_message(msg)
            except Exception as email_err:
                print(f"DEBUG: Error sending email: {email_err}")

        return jsonify({"message": "Your request has been submitted successfully!"}), 201
    except psycopg2.Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True)
    messages = data.get("messages", [])
    system_prompt = data.get("systemPrompt", "")

    if not messages:
        return jsonify({"error": "No messages provided"}), 400

    contents = []

    # Add system prompt
    if system_prompt:
        contents.append({
            "role": "user",
            "parts": [{"text": system_prompt}]
        })

    # Add conversation history
    for msg in messages:
        contents.append({
            "role": "user" if msg.get("from") == "user" else "model",
            "parts": [{"text": msg.get("text", "")}]
        })

    body = {
        "contents": contents,
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 400
        }
    }

    try:
        resp = requests.post(
            GEMINI_CHAT_URL,
            headers={"Content-Type": "application/json"},
            json=body,
            timeout=30
        )

        resp.raise_for_status()
        result = resp.json()

        reply = (
            result.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "AI could not respond.")
        )

        return jsonify({"reply": reply})

    except requests.exceptions.RequestException as e:
        print(f"DEBUG: Gemini API Request Error in /api/chat: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"DEBUG: Response status: {e.response.status_code}")
            print(f"DEBUG: Response body: {e.response.text}")
        return jsonify({"error": "Failed to get response from AI"}), 502

@app.route("/api/predict", methods=["POST"])
def predict():
    if "image" not in request.files:
        return jsonify({"error": "No image uploaded"}), 400

    file = request.files["image"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    filepath = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(filepath)

    try:
        # Determine mime type from filename
        mime_type = "image/jpeg"
        if file.filename.lower().endswith(".png"):
            mime_type = "image/png"
        elif file.filename.lower().endswith(".webp"):
            mime_type = "image/webp"

        # Read image and encode to base64
        with open(filepath, "rb") as image_file:
            img_data = base64.b64encode(image_file.read()).decode('utf-8')

        # 1. Gemini Identification and Analysis
        prompt = ANALYSIS_PROMPT_TEMPLATE
        
        body = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": img_data
                        }
                    }
                ]
            }],
            "generationConfig": {
                "temperature": 0.1, 
                "maxOutputTokens": 2000,
                "response_mime_type": "application/json"
            }
        }

        resp = requests.post(
            GEMINI_CHAT_URL,
            headers={"Content-Type": "application/json"},
            json=body,
            timeout=55  # 55s — fits within gunicorn's 120s worker timeout
        )
        
        if resp.status_code != 200:
            print(f"DEBUG: Gemini API Error: {resp.status_code}")
            print(f"DEBUG: Response Body: {resp.text}")
            return jsonify({"error": f"AI Error: {resp.status_code}"}), 502

        result = resp.json()
        parts = result.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        if not parts:
            print(f"DEBUG: Empty response from Gemini: {result}")
            return jsonify({"error": "AI returned an empty response"}), 500

        text = parts[0].get("text", "")
        
        # Extract JSON from response robustly
        import re
        # Try to find JSON block { ... }
        json_match = re.search(r"(\{[\s\S]*\})", text)
        if not json_match:
             print(f"DEBUG: Failed to find JSON in response: {text}")
             return jsonify({"error": "AI could not provide a valid analysis format"}), 500

        try:
            gemini_data = json.loads(json_match.group(1))
        except Exception as e:
            # Try cleaning common markdown junk if any
            cleaned_text = re.sub(r"```json\s?|```", "", text).strip()
            try:
                gemini_data = json.loads(cleaned_text)
            except:
                print(f"DEBUG: JSON Parse Error: {e}")
                print(f"DEBUG: Raw response text: {text}")
                return jsonify({"error": "Error parsing AI response"}), 500

        # Ensure all fields exist
        return jsonify({
            "crop": gemini_data.get("crop", "Unknown"),
            "disease": gemini_data.get("disease", "Unknown"),
            "confidence": float(gemini_data.get("confidence", 85.0)),
            "description": gemini_data.get("description", "Analysis pending..."),
            "symptoms": gemini_data.get("symptoms", "Look for unusual spots or wilting."),
            "treatment": gemini_data.get("treatment", "Refer to general farming guides."),
            "prevention": gemini_data.get("prevention", "Maintain soil health and crop rotation.")
        })

    except Exception as e:
        try:
            print(f"Error during prediction or analysis: {str(e).encode('ascii', 'ignore').decode('ascii')}")
        except:
            print("An unknown error occurred during prediction.")
        return jsonify({"error": str(e)}), 500

# ──────────────────────────────────────────────────────────────
# PASSWORD RESET ROUTES
# ──────────────────────────────────────────────────────────────

import secrets
import datetime as _dt

@app.route("/api/forgot-password", methods=["POST"])
def forgot_password():
    data = request.get_json(force=True)
    email = data.get("email", "").strip().lower()

    if not email:
        return jsonify({"error": "Email is required"}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Database connection failed"}), 500

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute("SELECT id, name FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()

            if not user:
                # Return success anyway to prevent email enumeration
                return jsonify({"message": "If this email is registered, a reset link has been sent."}), 200

            # Delete any existing tokens for this email
            cursor.execute("DELETE FROM password_reset_tokens WHERE email = %s", (email,))

            token = secrets.token_urlsafe(32)
            expires_at = _dt.datetime.utcnow() + _dt.timedelta(minutes=30)

            cursor.execute(
                "INSERT INTO password_reset_tokens (email, token, expires_at) VALUES (%s, %s, %s)",
                (email, token, expires_at)
            )
        conn.commit()

        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
        reset_url = f"{frontend_url}/reset-password?token={token}"

        if EMAIL_SENDER and EMAIL_PASSWORD:
            try:
                msg = EmailMessage()
                msg["Subject"] = "Smart Krishi — Password Reset Request"
                msg["From"] = EMAIL_SENDER
                msg["To"] = email
                msg.set_content(
                    f"Hello {user['name']},\n\n"
                    f"We received a request to reset your Smart Krishi password.\n\n"
                    f"Click the link below (valid for 30 minutes):\n{reset_url}\n\n"
                    f"If you did not request this, ignore this email.\n\n"
                    f"— The Smart Krishi Team"
                )
                msg.add_alternative(f"""\
<html>
  <body style="font-family:Arial,sans-serif;color:#333;max-width:560px;margin:auto;">
    <div style="background:linear-gradient(135deg,#16a34a,#059669);padding:24px;border-radius:12px 12px 0 0;text-align:center;">
      <h1 style="color:white;margin:0;font-size:22px;">&#127807; Smart Krishi</h1>
    </div>
    <div style="padding:28px 24px;background:#fff;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 12px 12px;">
      <h2 style="color:#111827;">Password Reset Request</h2>
      <p>Hello <b>{user['name']}</b>,</p>
      <p>Click the button below to set a new password:</p>
      <div style="text-align:center;margin:28px 0;">
        <a href="{reset_url}" style="background:linear-gradient(135deg,#16a34a,#059669);color:white;padding:14px 28px;border-radius:10px;text-decoration:none;font-weight:bold;font-size:15px;">Reset My Password</a>
      </div>
      <p style="color:#6b7280;font-size:13px;">This link expires in <b>30 minutes</b>.</p>
      <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0;">
      <p style="color:#9ca3af;font-size:12px;text-align:center;">&copy; 2025 Smart Krishi</p>
    </div>
  </body>
</html>""", subtype='html')
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
                    srv_pw = EMAIL_PASSWORD.strip('"').strip("'")
                    server.login(EMAIL_SENDER, srv_pw)
                    server.send_message(msg)
            except Exception as email_err:
                print(f"DEBUG: Password reset email error: {email_err}")

        return jsonify({"message": "If this email is registered, a reset link has been sent."}), 200

    except psycopg2.Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/reset-password", methods=["POST"])
def reset_password():
    data = request.get_json(force=True)
    token = data.get("token", "").strip()
    new_password = data.get("password", "").strip()

    if not token or not new_password:
        return jsonify({"error": "Token and new password are required"}), 400
    if len(new_password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Database connection failed"}), 500

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(
                "SELECT email, expires_at FROM password_reset_tokens WHERE token = %s",
                (token,)
            )
            record = cursor.fetchone()

            if not record:
                return jsonify({"error": "Invalid or expired reset link."}), 400

            if _dt.datetime.utcnow() > record["expires_at"]:
                cursor.execute("DELETE FROM password_reset_tokens WHERE token = %s", (token,))
                conn.commit()
                return jsonify({"error": "Reset link has expired. Please request a new one."}), 400

            hashed = generate_password_hash(new_password)
            cursor.execute(
                "UPDATE users SET password_hash = %s WHERE email = %s",
                (hashed, record["email"])
            )
            cursor.execute("DELETE FROM password_reset_tokens WHERE token = %s", (token,))

        conn.commit()
        return jsonify({"message": "Password updated successfully! You can now log in."}), 200

    except psycopg2.Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────
# WEATHER ALERT SYSTEM
# ──────────────────────────────────────────────────────────────

OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_GEO  = "https://geocoding-api.open-meteo.com/v1/search"

def _condition_engine(current, hourly_next6):
    """
    Analyse current & near-future weather and return a list of alert dicts.
    Each alert: { type, severity, icon, message, actions, eta_minutes }
    eta_minutes = how many minutes until the condition is expected to start
                  (0 means it is already happening).
    """
    alerts = []

    temp_c        = current.get("temperature_2m", 25)
    precip_mm     = current.get("precipitation",   0)
    wind_kmh      = current.get("windspeed_10m",   0)
    humidity_pct  = current.get("relativehumidity_2m", 50)

    # hourly_next6 is a list of dicts for each of the next 6 hours
    def find_eta(key, threshold, above=True):
        """Return minutes until threshold is crossed, or None if never in window."""
        for i, h in enumerate(hourly_next6):
            val = h.get(key, 0)
            crossed = (val >= threshold) if above else (val <= threshold)
            if crossed:
                return i * 60  # each slot is 1 hour apart
        return None

    # 1. Heavy Rain
    if precip_mm >= 10:
        alerts.append({
            "type": "heavy_rain", "severity": "high", "icon": "🌧️",
            "title": "Heavy Rain",
            "message": f"Current rainfall is {precip_mm:.1f} mm/hr — heavy rain in progress.",
            "actions": [
                "Stop all pesticide / fertilizer spraying immediately",
                "Cover stored crops and hay bales with tarpaulins",
                "Clear field drainage channels to prevent waterlogging",
                "Delay harvesting until rain subsides"
            ],
            "eta_minutes": 0
        })
    else:
        eta = find_eta("precipitation", 10)
        if eta is not None:
            alerts.append({
                "type": "heavy_rain", "severity": "medium" if eta > 30 else "high", "icon": "🌧️",
                "title": "Heavy Rain Approaching",
                "message": f"Heavy rain (≥10 mm/hr) expected in ~{eta} minutes.",
                "actions": [
                    "Finish any pending spraying in the next 20–25 minutes",
                    "Prepare drainage and cover sensitive crops",
                    "Move farm equipment to sheltered areas",
                    "Set up temporary crop covers for vulnerable plants"
                ],
                "eta_minutes": eta
            })

    # 2. Heat Stress
    if temp_c >= 40:
        alerts.append({
            "type": "heat_stress", "severity": "high", "icon": "🌡️",
            "title": "Extreme Heat Stress",
            "message": f"Temperature is {temp_c:.1f}°C — extreme heat stress for crops.",
            "actions": [
                "Increase irrigation frequency immediately",
                "Apply mulch around crop bases to retain soil moisture",
                "Provide shade nets for sensitive vegetables",
                "Avoid field work between 11 AM and 4 PM",
                "Check for signs of leaf wilting and scorching"
            ],
            "eta_minutes": 0
        })
    elif temp_c >= 35:
        alerts.append({
            "type": "heat_stress", "severity": "medium", "icon": "🌡️",
            "title": "High Temperature Warning",
            "message": f"Temperature is {temp_c:.1f}°C — moderate heat stress expected.",
            "actions": [
                "Schedule irrigation for early morning or evening",
                "Monitor soil moisture levels closely",
                "Reduce physical workload on livestock",
                "Ensure adequate water for livestock"
            ],
            "eta_minutes": 0
        })
    else:
        eta = find_eta("temperature_2m", 35)
        if eta is not None:
            alerts.append({
                "type": "heat_stress", "severity": "medium", "icon": "🌡️",
                "title": "Heat Stress Approaching",
                "message": f"Temperature expected to reach ≥35°C in ~{eta} minutes.",
                "actions": [
                    "Pre-schedule irrigation before temperatures peak",
                    "Prepare shade solutions for sensitive crops",
                    "Complete field work as early as possible today"
                ],
                "eta_minutes": eta
            })

    # 3. High Wind
    if wind_kmh >= 50:
        alerts.append({
            "type": "high_wind", "severity": "high", "icon": "🌬️",
            "title": "Dangerous Wind Speed",
            "message": f"Wind speed is {wind_kmh:.1f} km/h — dangerous for crops and structures.",
            "actions": [
                "Stop all aerial / sprayer operations immediately",
                "Secure poly-house and greenhouse structures",
                "Support tall crops (maize, sunflower) with stakes",
                "Avoid burning crop residue in windy conditions",
                "Secure tarpaulins and covers tightly"
            ],
            "eta_minutes": 0
        })
    elif wind_kmh >= 30:
        alerts.append({
            "type": "high_wind", "severity": "medium", "icon": "🌬️",
            "title": "Moderate Wind Advisory",
            "message": f"Wind speed is {wind_kmh:.1f} km/h — spraying efficiency reduced.",
            "actions": [
                "Reduce spray volume and pressure when spraying",
                "Spray parallel to wind direction to minimize drift",
                "Check stability of greenhouse covers and nets"
            ],
            "eta_minutes": 0
        })
    else:
        eta = find_eta("windspeed_10m", 50)
        if eta is not None:
            alerts.append({
                "type": "high_wind", "severity": "medium" if eta > 30 else "high", "icon": "🌬️",
                "title": "High Wind Approaching",
                "message": f"Wind speed expected to exceed 50 km/h in ~{eta} minutes.",
                "actions": [
                    "Complete all spraying and dusting operations soon",
                    "Begin securing poly-house structures",
                    "Stake tall crops before winds arrive"
                ],
                "eta_minutes": eta
            })

    # 4. Frost Risk
    if temp_c <= 2:
        alerts.append({
            "type": "frost_risk", "severity": "high", "icon": "🧊",
            "title": "Active Frost Conditions",
            "message": f"Temperature is {temp_c:.1f}°C — frost damage is occurring or imminent.",
            "actions": [
                "Cover frost-sensitive crops with cloth/plastic immediately",
                "Apply light sprinkler irrigation (ice acts as insulator)",
                "Light smudge fires at field edges (where permitted)",
                "Do NOT harvest frost-damaged produce immediately — wait for thaw",
                "Check greenhouse heating systems"
            ],
            "eta_minutes": 0
        })
    elif temp_c <= 5:
        alerts.append({
            "type": "frost_risk", "severity": "medium", "icon": "🧊",
            "title": "Frost Risk Alert",
            "message": f"Temperature is {temp_c:.1f}°C — frost risk is elevated.",
            "actions": [
                "Cover sensitive crops (tomatoes, peppers, cucumbers) before nightfall",
                "Pre-warm greenhouses to maintain safe temperatures",
                "Apply light irrigation in evening to release latent heat",
                "Harvest ripe produce before overnight freeze"
            ],
            "eta_minutes": 0
        })
    else:
        eta = find_eta("temperature_2m", 5, above=False)
        if eta is not None:
            alerts.append({
                "type": "frost_risk", "severity": "medium", "icon": "🧊",
                "title": "Frost Risk Tonight",
                "message": f"Temperature expected to drop below 5°C in ~{eta} minutes.",
                "actions": [
                    "Prepare crop covers and have them ready to deploy",
                    "Harvest mature produce before evening",
                    "Pre-heat greenhouses 1 hour before frost is expected"
                ],
                "eta_minutes": eta
            })

    # 5. Storm Warning (combined: rain ≥ 10 mm AND wind ≥ 40 km/h)
    future_rain_eta  = find_eta("precipitation", 10)
    future_wind_eta  = find_eta("windspeed_10m", 40)
    storm_now  = (precip_mm >= 8 and wind_kmh >= 40)
    storm_soon = (future_rain_eta is not None and future_wind_eta is not None
                  and abs(future_rain_eta - future_wind_eta) <= 60)

    if storm_now:
        alerts.append({
            "type": "storm", "severity": "high", "icon": "⛈️",
            "title": "Storm in Progress",
            "message": "Combined heavy rain and high winds — dangerous storm conditions.",
            "actions": [
                "Evacuate all people and livestock to shelter",
                "Secure or move all portable farm equipment",
                "Do NOT operate any machinery outdoors",
                "Protect irrigation lines and pumps from flooding",
                "Document crop damage for insurance claims after storm"
            ],
            "eta_minutes": 0
        })
    elif storm_soon:
        eta = max(future_rain_eta or 0, future_wind_eta or 0)
        alerts.append({
            "type": "storm", "severity": "high", "icon": "⛈️",
            "title": "Storm Warning",
            "message": f"Dangerous storm conditions (rain + high winds) expected in ~{eta} minutes.",
            "actions": [
                "Complete all outdoor field work immediately",
                "Move livestock to sheltered areas now",
                "Secure greenhouses and poly-houses",
                "Store all portable equipment and tools"
            ],
            "eta_minutes": eta
        })

    # If no alerts, return a safe status
    if not alerts:
        alerts.append({
            "type": "safe", "severity": "low", "icon": "✅",
            "title": "All Clear — Good Farming Conditions",
            "message": "Weather conditions are favourable for all farming activities.",
            "actions": [
                "Good time for pesticide / fertilizer spraying",
                "Proceed with harvesting and field operations",
                "Schedule irrigation as per crop water needs"
            ],
            "eta_minutes": None
        })

    return alerts


@app.route("/api/weather", methods=["GET"])
def get_weather_alerts():
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)

    if lat is None or lon is None:
        return jsonify({"error": "lat and lon query parameters are required"}), 400

    try:
        # Fetch current + hourly forecast from Open-Meteo (free, no key)
        params = {
            "latitude":  lat,
            "longitude": lon,
            "current":   [
                "temperature_2m",
                "relativehumidity_2m",
                "precipitation",
                "windspeed_10m",
                "weathercode",
                "apparent_temperature"
            ],
            "hourly": [
                "temperature_2m",
                "precipitation",
                "windspeed_10m",
                "relativehumidity_2m"
            ],
            "forecast_days": 2,
            "timezone": "auto"
        }

        resp = requests.get(OPEN_METEO_BASE, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        current_raw = data.get("current", {})
        hourly_raw  = data.get("hourly", {})

        # Build hourly list for next 6 hours
        hours_list = []
        times = hourly_raw.get("time", [])
        import datetime
        now_utc = datetime.datetime.utcnow()

        for i, t in enumerate(times[:48]):
            try:
                slot_time = datetime.datetime.fromisoformat(t)
            except Exception:
                continue
            diff_hours = (slot_time - now_utc).total_seconds() / 3600
            if 0 <= diff_hours <= 6:
                hours_list.append({
                    "time":                  t,
                    "temperature_2m":        hourly_raw.get("temperature_2m",       [None]*48)[i],
                    "precipitation":         hourly_raw.get("precipitation",        [None]*48)[i],
                    "windspeed_10m":         hourly_raw.get("windspeed_10m",        [None]*48)[i],
                    "relativehumidity_2m":   hourly_raw.get("relativehumidity_2m",  [None]*48)[i],
                })

        # Sanitise None values
        for h in hours_list:
            for k in ("temperature_2m", "precipitation", "windspeed_10m", "relativehumidity_2m"):
                if h.get(k) is None:
                    h[k] = 0

        current = {
            "temperature_2m":       current_raw.get("temperature_2m", 25),
            "apparent_temperature":  current_raw.get("apparent_temperature", 25),
            "relativehumidity_2m":   current_raw.get("relativehumidity_2m", 50),
            "precipitation":         current_raw.get("precipitation", 0),
            "windspeed_10m":         current_raw.get("windspeed_10m", 0),
            "weathercode":           current_raw.get("weathercode", 0),
        }

        alerts = _condition_engine(current, hours_list)

        return jsonify({
            "location": {"lat": lat, "lon": lon},
            "current": current,
            "alerts": alerts,
            "fetched_at": now_utc.isoformat() + "Z"
        })

    except requests.exceptions.RequestException as e:
        print(f"Weather API Error: {e}")
        return jsonify({"error": "Failed to fetch weather data"}), 502
    except Exception as e:
        print(f"Weather processing error: {e}")
        return jsonify({"error": str(e)}), 500



# ──────────────────────────────────────────────────────────────
# PERSONALIZED FARMING RECOMMENDATION SYSTEM
# ──────────────────────────────────────────────────────────────

FARMING_RECOMMENDATION_PROMPT = """
You are an expert agricultural scientist specializing in natural and organic farming in India.
A farmer has provided the following details:

- Crop Name: {crop}
- Soil Type: {soil}
- Season: {season}
- Location / Region: {location}
- Farming Method Preferred: {method}
- Main Problem Faced: {problem}

Provide a personalized farming recommendation. Be CONCISE — each field must be 1-3 sentences max.
Return ONLY valid JSON in exactly this format (no markdown, no code fences, no newlines inside string values):
{{
  "fertilizer": "Concise fertilizer recommendation with key quantities",
  "farming_method": "Best farming method for this crop/season/soil in 2 sentences",
  "irrigation": "Water schedule: how much, how often, best time of day",
  "disease_prevention": "Top 2 diseases in this season and their organic prevention methods",
  "weather_advice": "Key weather precautions for this season and region",
  "quick_tips": ["Tip 1", "Tip 2", "Tip 3", "Tip 4"]
}}
"""

@app.route("/api/farming-recommendation", methods=["POST"])
def farming_recommendation():
    data = request.get_json(force=True)
    crop     = data.get("crop", "").strip()
    soil     = data.get("soil", "").strip()
    season   = data.get("season", "").strip()
    location = data.get("location", "").strip()
    method   = data.get("method", "").strip()
    problem  = data.get("problem", "").strip()

    if not crop or not soil or not season:
        return jsonify({"error": "Crop, soil type, and season are required fields."}), 400

    prompt = FARMING_RECOMMENDATION_PROMPT.format(
        crop=crop or "Not specified",
        soil=soil or "Not specified",
        season=season or "Not specified",
        location=location or "Not specified",
        method=method or "Organic / Natural",
        problem=problem or "No specific problem mentioned"
    )

    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 3000
        }
    }

    try:
        resp = requests.post(
            GEMINI_CHAT_URL,
            headers={"Content-Type": "application/json"},
            json=body,
            timeout=40
        )
        resp.raise_for_status()
        result = resp.json()

        candidates = result.get("candidates", [])
        if not candidates:
            print(f"DEBUG farming-rec: no candidates. Full response: {result}")
            return jsonify({"error": "AI returned no candidates"}), 500

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        if not parts:
            finish = candidates[0].get("finishReason", "UNKNOWN")
            print(f"DEBUG farming-rec: empty parts, finishReason={finish}, candidate={candidates[0]}")
            return jsonify({"error": f"AI content blocked or empty (reason: {finish})"}), 500

        text = parts[0].get("text", "").strip()
        print(f"DEBUG farming-rec: raw text (first 400 chars): {text[:400]}")

        if not text:
            return jsonify({"error": "AI returned empty text"}), 500

        import re
        rec_data = None

        # Attempt 1: direct JSON parse
        try:
            rec_data = json.loads(text)
        except Exception:
            pass

        # Attempt 2: extract JSON object with regex
        if rec_data is None:
            json_match = re.search(r"(\{[\s\S]*\})", text)
            if json_match:
                try:
                    rec_data = json.loads(json_match.group(1))
                except Exception as je:
                    print(f"DEBUG farming-rec: regex JSON parse failed: {je}")

        # Attempt 3: strip markdown fences then parse
        if rec_data is None:
            cleaned = re.sub(r"```json\s?|```", "", text).strip()
            try:
                rec_data = json.loads(cleaned)
            except Exception as je:
                print(f"DEBUG farming-rec: all parse attempts failed: {je}")
                print(f"DEBUG farming-rec: full text: {text}")
                return jsonify({"error": "AI response could not be parsed. Please try again."}), 500

        return jsonify({
            "fertilizer":        rec_data.get("fertilizer", "Use compost and Jeevamrut."),
            "farming_method":    rec_data.get("farming_method", "Natural farming recommended."),
            "irrigation":        rec_data.get("irrigation", "Water regularly as per crop needs."),
            "disease_prevention":rec_data.get("disease_prevention", "Apply neem oil spray weekly."),
            "weather_advice":    rec_data.get("weather_advice", "Monitor weather forecasts regularly."),
            "quick_tips":        rec_data.get("quick_tips", []),
        })

    except requests.exceptions.RequestException as e:
        print(f"DEBUG farming-rec: Gemini request error: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"DEBUG farming-rec: status={e.response.status_code}, body={e.response.text[:400]}")
        return jsonify({"error": "Failed to reach AI service. Please try again."}), 502
    except Exception as e:
        print(f"DEBUG farming-rec: unexpected error: {e}")
        return jsonify({"error": str(e)}), 500



# ──────────────────────────────────────────────────────────────
# WEATHER ALERT EMAIL
# ──────────────────────────────────────────────────────────────

def _send_weather_alert_email(to_email: str, location_name: str, alerts: list):
    """
    Send a beautifully styled HTML weather-alert email to `to_email`.
    Only call this when alerts contain at least one medium/high severity item.
    Returns True on success, False on failure.
    """
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        print("DEBUG: EMAIL_SENDER or EMAIL_PASSWORD not configured — skipping email.")
        return False

    severity_labels = {
        "high":   ("🔴 HIGH ALERT",   "#dc2626", "#fef2f2", "#fee2e2"),
        "medium": ("🟡 MEDIUM ALERT", "#d97706", "#fffbeb", "#fef3c7"),
        "low":    ("🟢 ALL CLEAR",    "#16a34a", "#f0fdf4", "#dcfce7"),
    }

    # Build one HTML block per alert
    alert_html_parts = []
    for a in alerts:
        sev = a.get("severity", "low")
        label, color, bg, border_color = severity_labels.get(sev, severity_labels["low"])
        icon  = a.get("icon", "")
        title = a.get("title", "Weather Alert")
        msg   = a.get("message", "")
        eta   = a.get("eta_minutes")
        actions = a.get("actions", [])

        eta_html = ""
        if eta == 0:
            eta_html = f'<p style="margin:8px 0 0;font-size:12px;color:#6b7280;">⏱ Status: <b>In Progress Now</b></p>'
        elif eta is not None:
            eta_html = f'<p style="margin:8px 0 0;font-size:12px;color:#6b7280;">⏱ Expected in approximately <b>{eta} minutes</b></p>'

        actions_html = "".join(
            f'<li style="margin:6px 0;font-size:14px;color:#374151;"><span style="display:inline-block;min-width:22px;height:22px;line-height:22px;text-align:center;border-radius:50%;background:#fff;border:1px solid #d1d5db;font-size:11px;font-weight:700;color:#6b7280;margin-right:8px;">{i+1}</span>{act}</li>'
            for i, act in enumerate(actions)
        )

        alert_html_parts.append(f"""
        <div style="border:1.5px solid {border_color};border-radius:12px;background:{bg};margin-bottom:18px;overflow:hidden;">
          <div style="padding:18px 20px;">
            <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px;">
              <span style="font-size:22px;">{icon}</span>
              <span style="font-size:17px;font-weight:700;color:#111827;">{title}</span>
              <span style="font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px;background:{color};color:#fff;">{label}</span>
            </div>
            <p style="margin:0;font-size:14px;color:#4b5563;line-height:1.6;">{msg}</p>
            {eta_html}
          </div>
          {"<div style='border-top:1px solid " + border_color + ";padding:14px 20px;'><p style='margin:0 0 10px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#6b7280;'>🛡️ Preventive Actions</p><ul style='margin:0;padding:0;list-style:none;'>" + actions_html + "</ul></div>" if actions else ""}
        </div>
        """)

    import datetime as _dt2
    now_str = _dt2.datetime.now().strftime("%d %b %Y, %I:%M %p")
    all_alerts_html = "".join(alert_html_parts)

    html_body = f"""\
<html>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:30px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

        <!-- Header -->
        <tr><td style="background:linear-gradient(135deg,#16a34a,#059669);border-radius:14px 14px 0 0;padding:28px 32px;text-align:center;">
          <h1 style="margin:0;color:#fff;font-size:24px;font-weight:800;">🌿 Smart Krishi</h1>
          <p style="margin:6px 0 0;color:#bbf7d0;font-size:14px;">Weather Alert Notification</p>
        </td></tr>

        <!-- Body -->
        <tr><td style="background:#fff;border:1px solid #e5e7eb;border-top:none;padding:28px 32px;">

          <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px;padding:14px 18px;margin-bottom:24px;">
            <p style="margin:0;font-size:13px;color:#1d4ed8;">
              📍 <b>Location:</b> {location_name} &nbsp;|&nbsp; 🕐 <b>Generated at:</b> {now_str}
            </p>
          </div>

          <h2 style="font-size:18px;color:#111827;margin:0 0 6px;">🚨 Weather Alerts Detected</h2>
          <p style="font-size:14px;color:#6b7280;margin:0 0 20px;">
            The following weather conditions require your attention. Please take the listed preventive actions to protect your crops and livestock.
          </p>

          {all_alerts_html}

          <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:10px;padding:14px 18px;margin-top:8px;">
            <p style="margin:0;font-size:12px;color:#9ca3af;text-align:center;">
              Weather data powered by <a href="https://open-meteo.com" style="color:#0ea5e9;">Open-Meteo</a>.
              Always cross-check with your local meteorology department for official forecasts.
            </p>
          </div>
        </td></tr>

        <!-- Footer -->
        <tr><td style="background:linear-gradient(135deg,#16a34a,#059669);border-radius:0 0 14px 14px;padding:18px 32px;text-align:center;">
          <p style="margin:0;color:#bbf7d0;font-size:12px;">Stay safe &amp; farm smart · © 2025 Smart Krishi</p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

    plain_body = f"Smart Krishi — Weather Alert for {location_name}\n\n"
    for a in alerts:
        plain_body += f"[{a.get('severity','').upper()}] {a.get('icon','')} {a.get('title','')}\n"
        plain_body += f"{a.get('message','')}\n"
        for i, act in enumerate(a.get("actions", []), 1):
            plain_body += f"  {i}. {act}\n"
        plain_body += "\n"

    try:
        msg = EmailMessage()
        msg["Subject"] = f"🌿 Smart Krishi Weather Alert — {location_name}"
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = to_email
        msg.set_content(plain_body)
        msg.add_alternative(html_body, subtype="html")

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            srv_pw = EMAIL_PASSWORD.strip('"').strip("'")
            server.login(EMAIL_SENDER, srv_pw)
            server.send_message(msg)
        print(f"DEBUG: Weather alert email sent to {to_email}")
        return True
    except Exception as e:
        print(f"DEBUG: Weather alert email error: {e}")
        return False


@app.route("/api/weather/send-alert", methods=["POST"])
def send_weather_alert_email():
    """
    POST { email, lat, lon, locationName }
    Fetches current weather, runs condition engine, emails bad-weather alerts
    (medium or high severity) to the provided email address.
    """
    data          = request.get_json(force=True)
    to_email      = (data.get("email") or "").strip()
    lat           = data.get("lat")
    lon           = data.get("lon")
    location_name = (data.get("locationName") or "Your Location").strip()

    # Validate inputs
    if not to_email:
        return jsonify({"error": "Email address is required"}), 400
    if lat is None or lon is None:
        return jsonify({"error": "lat and lon are required"}), 400

    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return jsonify({"error": "lat and lon must be numeric"}), 400

    # ── Fetch weather from Open-Meteo ────────────────────────
    try:
        params = {
            "latitude":  lat,
            "longitude": lon,
            "current": [
                "temperature_2m", "relativehumidity_2m",
                "precipitation", "windspeed_10m",
                "weathercode", "apparent_temperature"
            ],
            "hourly": [
                "temperature_2m", "precipitation",
                "windspeed_10m", "relativehumidity_2m"
            ],
            "forecast_days": 2,
            "timezone": "auto"
        }
        resp = requests.get(OPEN_METEO_BASE, params=params, timeout=10)
        resp.raise_for_status()
        wd = resp.json()
    except requests.exceptions.RequestException as e:
        print(f"Weather fetch error in send-alert: {e}")
        return jsonify({"error": "Failed to fetch weather data"}), 502

    # ── Build hourly next-6-hours list ───────────────────────
    import datetime as _dta
    hourly_raw = wd.get("hourly", {})
    now_utc    = _dta.datetime.utcnow()
    hours_list = []
    for i, t in enumerate(hourly_raw.get("time", [])[:48]):
        try:
            slot_time = _dta.datetime.fromisoformat(t)
        except Exception:
            continue
        diff_hours = (slot_time - now_utc).total_seconds() / 3600
        if 0 <= diff_hours <= 6:
            hours_list.append({
                "temperature_2m":      hourly_raw.get("temperature_2m",      [0]*48)[i] or 0,
                "precipitation":       hourly_raw.get("precipitation",        [0]*48)[i] or 0,
                "windspeed_10m":       hourly_raw.get("windspeed_10m",        [0]*48)[i] or 0,
                "relativehumidity_2m": hourly_raw.get("relativehumidity_2m",  [0]*48)[i] or 0,
            })

    current_raw = wd.get("current", {})
    current = {
        "temperature_2m":      current_raw.get("temperature_2m", 25),
        "apparent_temperature": current_raw.get("apparent_temperature", 25),
        "relativehumidity_2m":  current_raw.get("relativehumidity_2m", 50),
        "precipitation":        current_raw.get("precipitation", 0),
        "windspeed_10m":        current_raw.get("windspeed_10m", 0),
        "weathercode":          current_raw.get("weathercode", 0),
    }

    # ── Run condition engine ─────────────────────────────────
    all_alerts   = _condition_engine(current, hours_list)
    bad_alerts   = [a for a in all_alerts if a.get("severity") in ("high", "medium")]

    if not bad_alerts:
        return jsonify({
            "sent":         False,
            "alerts_count": 0,
            "message":      "No bad weather conditions detected right now — no alert email was sent. Conditions look safe!"
        }), 200

    # ── Send email ───────────────────────────────────────────
    sent = _send_weather_alert_email(to_email, location_name, bad_alerts)
    if sent:
        return jsonify({
            "sent":         True,
            "alerts_count": len(bad_alerts),
            "message":      f"Weather alert email sent to {to_email} with {len(bad_alerts)} alert(s)."
        }), 200
    else:
        return jsonify({"error": "Failed to send email. Please check server email configuration."}), 500



# ──────────────────────────────────────────────────────────────
# WEATHER AUTO-ALERT SUBSCRIPTION ROUTES
# ──────────────────────────────────────────────────────────────

@app.route("/api/weather/subscribe", methods=["POST"])
def subscribe_weather_alerts():
    """
    POST { email, lat, lon, locationName }
    Saves or updates the user's location for automatic weather alert emails.
    """
    data          = request.get_json(force=True)
    email         = (data.get("email") or "").strip().lower()
    lat           = data.get("lat")
    lon           = data.get("lon")
    location_name = (data.get("locationName") or "Your Location").strip()

    if not email:
        return jsonify({"error": "Email is required"}), 400
    if lat is None or lon is None:
        return jsonify({"error": "lat and lon are required"}), 400
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return jsonify({"error": "lat and lon must be numeric"}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Database connection failed"}), 500

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO weather_subscriptions (email, lat, lon, location_name)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (email) DO UPDATE
                    SET lat = EXCLUDED.lat,
                        lon = EXCLUDED.lon,
                        location_name = EXCLUDED.location_name,
                        last_alerted_at = NULL
            """, (email, lat, lon, location_name))
        conn.commit()
        return jsonify({
            "subscribed": True,
            "message": f"Auto-alerts enabled for {email} at {location_name}. You'll be emailed when bad weather is detected (checked every 30 min)."
        }), 200
    except psycopg2.Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/weather/subscribe", methods=["DELETE"])
def unsubscribe_weather_alerts():
    """
    DELETE { email }
    Removes the user's weather alert subscription.
    """
    data  = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()

    if not email:
        return jsonify({"error": "Email is required"}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Database connection failed"}), 500

    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM weather_subscriptions WHERE email = %s", (email,))
        conn.commit()
        return jsonify({"subscribed": False, "message": "Auto-alerts disabled successfully."}), 200
    except psycopg2.Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/weather/subscription", methods=["GET"])
def get_subscription_status():
    """
    GET /api/weather/subscription?email=user@example.com
    Returns the current subscription status and next alert eligibility.
    """
    email = request.args.get("email", "").strip().lower()
    if not email:
        return jsonify({"error": "email query parameter is required"}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"error": "Database connection failed"}), 500

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(
                "SELECT email, lat, lon, location_name, last_alerted_at, created_at FROM weather_subscriptions WHERE email = %s",
                (email,)
            )
            row = cursor.fetchone()

        if not row:
            return jsonify({"subscribed": False}), 200

        last_alerted = row["last_alerted_at"]
        cooldown_remaining_min = 0
        if last_alerted is not None:
            since_hours = (_dt_global.datetime.utcnow() - last_alerted).total_seconds() / 3600
            remaining   = max(0, ALERT_COOLDOWN_HOURS - since_hours)
            cooldown_remaining_min = int(remaining * 60)

        return jsonify({
            "subscribed":             True,
            "email":                  row["email"],
            "location_name":          row["location_name"],
            "lat":                    row["lat"],
            "lon":                    row["lon"],
            "last_alerted_at":        row["last_alerted_at"].isoformat() + "Z" if last_alerted else None,
            "cooldown_remaining_min": cooldown_remaining_min,
            "check_interval_min":     30,
        }), 200
    except psycopg2.Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/geocode", methods=["GET"])
def geocode_city():
    """Proxy for Open-Meteo geocoding to avoid CORS issues from frontend."""
    city = request.args.get("city", "").strip()
    if not city:
        return jsonify({"error": "city parameter is required"}), 400
    try:
        resp = requests.get(OPEN_METEO_GEO, params={"name": city, "count": 5, "language": "en", "format": "json"}, timeout=8)
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=True, host="0.0.0.0", port=port)
