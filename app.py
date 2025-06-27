import logging
from datetime import datetime
import os
import json
import gspread
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

# --- SETUP ---
app = Flask(__name__)
CORS(app) # This one line solves all our CORS problems.
logging.basicConfig(level=logging.INFO)
# --- GOOGLE SHEETS AUTHENTICATION ---
# This requires a 'credentials.json' file from a Google Cloud Service Account.
# We will set this up in the next steps.
try:
    SERVICE_ACCOUNT_INFO = json.loads(os.environ.get('GCP_CREDENTIALS_JSON'))
    gc = gspread.service_account_from_dict(SERVICE_ACCOUNT_INFO)
    spreadsheet = gc.open("PharmaFeedbackApp")
    feedback_sheet = spreadsheet.worksheet("Feedback")
    products_sheet = spreadsheet.worksheet("Products")
    admin_sheet = spreadsheet.worksheet("AdminUsers")
    print("Successfully connected to Google Sheets.")
except Exception as e:
    print(f"ERROR: Could not connect to Google Sheets. Check credentials. Error: {e}")

# --- GEMINI API SETUP ---
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

def analyze_with_gemini(text):
    if not GEMINI_API_KEY:
        return {"category": "Error", "sentiment": 0, "error": "GEMINI_API_KEY not set."}

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={GEMINI_API_KEY}"
    prompt = f"""Analyze the following customer feedback. Provide a JSON object with two keys: "category" and "sentiment".
    The "category" must be one of the following: [Packaging, Formula, Color, Smell, Efficacy, Side Effect, Price, Documentation, Other].
    The "sentiment" must be a number between -1.0 (very negative) and 1.0 (very positive).
    Feedback to analyze: \"{text}\""""
    
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    headers = {'Content-Type': 'application/json'}

    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status() # Raises an exception for bad responses (4xx or 5xx)
        
        json_response = response.json()
        model_response_text = json_response['candidates'][0]['content']['parts'][0]['text']
        clean_json_string = model_response_text.replace('```json', '').replace('```', '').strip()
        analysis = json.loads(clean_json_string)
        
        return {"category": analysis.get('category', 'Parse Error'), "sentiment": analysis.get('sentiment', 0), "error": ""}
    except Exception as e:
        print(f"Gemini Error: {e}")
        return {"category": "AI_Error", "sentiment": 0, "error": str(e)}

# --- API ENDPOINTS ---

@app.route('/get-products', methods=['GET'])
def get_products():
    try:
        products = products_sheet.col_values(1)[1:] # Get all values from col 1, except the header
        return jsonify({"status": "success", "products": products})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/submit-feedback', methods=['POST'])
def submit_feedback():
    try:
        data = request.json
        app.logger.info(f"Received feedback data: {data}") # New log line

        ai_analysis = analyze_with_gemini(f"Feedback: {data['feedbackText']}. Suggestion: {data['suggestionText']}")
        app.logger.info(f"AI Analysis result: {ai_analysis}") # New log line

        # A much more reliable way to get a timestamp
        timestamp = datetime.utcnow().isoformat() + "Z" 
        
        new_row = [
            timestamp,
            data.get('productName'),
            data.get('feedbackText'),
            data.get('suggestionText'),
            data.get('clientName'),
            data.get('clientEmail'),
            ai_analysis['category'],
            ai_analysis['sentiment'],
            ai_analysis['error']
        ]
        
        app.logger.info(f"Appending new row: {new_row}") # New log line
        feedback_sheet.append_row(new_row)
        
        return jsonify({"status": "success", "message": "Feedback submitted."})
        
    except Exception as e:
        # This will now log the detailed error traceback to the Render console
        app.logger.error("An error occurred in /submit-feedback", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500
        
@app.route('/admin-login', methods=['POST'])
def admin_login():
    try:
        data = request.json
        users = admin_sheet.get_all_records()
        for user in users:
            if user['Username'] == data['username'] and str(user['Password']) == data['password']:
                return jsonify({"status": "success", "message": "Login successful."})
        return jsonify({"status": "error", "message": "Invalid credentials."}), 401
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))