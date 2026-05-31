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
