# Smart-Krishi-Backend
# 🌱 Smart Krishi - AI Powered Farming Assistant

Smart Krishi is an AI-based agriculture assistance platform designed to help farmers identify crop diseases, learn modern and natural farming techniques, and get farming guidance through an intelligent chatbot system.

The platform combines Machine Learning, Artificial Intelligence, and Web Technologies to provide a smart digital farming solution.

---

# 🚀 Features

## 🌿 Crop Disease Detection
- Upload crop leaf images
- AI-based disease prediction using CNN model
- Detects multiple crop diseases
- Provides confidence score
- Suggests organic and chemical treatments

---

## 🌾 Natural Farming Guide
- Organic farming techniques
- Jeevamrut preparation
- Beejamrut seed treatment
- Composting methods
- Organic pest control
- Crop rotation guidance

---

## 🚜 Modern Farming Techniques
- Precision farming
- Hydroponic farming
- Vertical farming
- Drone farming
- IoT smart irrigation systems

---

## 🤖 AI Farming Chatbot
- AI-powered farming assistant
- Answers agriculture-related questions
- Provides farming guidance
- Helps farmers with crop management advice

---

## 📩 Farmer Query System
- Farmers can submit farming-related issues
- Queries can be sent directly to admin email
- Helps farmers connect with experts

---

# 🛠️ Technologies Used

## Frontend
- React.js
- Vite
- Tailwind CSS
- JavaScript

---

## Backend
- Flask
- Flask-CORS
- REST API

---

## Machine Learning
- TensorFlow
- Keras
- CNN (Convolutional Neural Network)

---

## Database
- MySQL

---

## APIs
- Gemini API

---

# 📂 Project Structure

```plaintext
Smart-Krishi
│
├── frontend
│   ├── src
│   ├── public
│   └── package.json
│
├── backend
│   ├── app.py
│   ├── requirements.txt
│   ├── runtime.txt
│   │
│   ├── model
│   │   └── plant_disease_model.h5
│   │
│   ├── utils
│   │   ├── predictor.py
│   │   └── treatments.py
│   │
│   ├── uploads
│   │
│   └── templates
│
└── README.md

# 🧠 Machine Learning Model

The crop disease detection model is trained using the **PlantVillage dataset**.

---

## 📂 Dataset Information

- **Dataset:** PlantVillage  
- **Number of Classes:** 38  
- Multiple crop disease categories  
- Healthy and diseased leaf images  

---

## 🏗️ Model Architecture

- Convolutional Neural Network (CNN)  
- Image preprocessing  
- Data augmentation  
- Multi-class classification  

---

# ⚙️ Installation Guide

## 1️⃣ Clone Repository

```bash
git clone https://github.com/YOUR_USERNAME/Smart-Krishi.git
2️⃣ Frontend Setup
cd frontend

npm install

npm run dev

Frontend runs on:

http://localhost:5173
3️⃣ Backend Setup
cd backend

pip install -r requirements.txt
4️⃣ Create Environment Variables

Create a .env file inside the backend folder:

GEMINI_API_KEY=your_gemini_api_key

MAIL_USERNAME=your_email@gmail.com

MAIL_PASSWORD=your_app_password

MYSQL_HOST=localhost

MYSQL_USER=root

MYSQL_PASSWORD=your_password

MYSQL_DB=smart_krishi
5️⃣ Run Backend
python app.py

Backend runs on:

http://localhost:5000
## 📸 Disease Detection Workflow
User Uploads Leaf Image
        ↓
Frontend Sends Image to Backend
        ↓
Flask Backend Processes Image
        ↓
CNN Model Predicts Disease
        ↓
Treatment Suggestions Generated
        ↓
Results Displayed to User

### 📊 Supported Crop Diseases

The system supports disease detection for:

- Apple
- Tomato
- Potato
Corn
Grape
Pepper
Peach
Strawberry
Soybean
Orange

### 🔐 Authentication Features
User Login
User Registration
Protected Routes
Session Handling

### 🌍 Future Scope
Mobile application support
Multi-language support
Voice-based farming assistant
Weather integration
Real-time crop monitoring
AI-based fertilizer recommendation

### 📌 Advantages
Early disease detection
Reduces crop loss
Farmer-friendly interface
Promotes sustainable farming
Instant farming guidance

### ⚠️ Limitations
Requires internet connection
Depends on image quality
Limited to trained disease classes

##👨‍💻 Developed By
Shrikant Ambatkar
