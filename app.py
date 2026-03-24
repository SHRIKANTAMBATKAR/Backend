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

# Database Configuration (Aiven PostgreSQL)
db_config = {
    'host': os.getenv('DB_HOST'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASSWORD'),
    'database': os.getenv('DB_NAME'),
    'port': int(os.getenv('DB_PORT')),
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
    except pymysql.MySQLError as e:
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
            timeout=40
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=True, host="0.0.0.0", port=port)
