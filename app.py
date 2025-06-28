import os
import json
import logging
from datetime import datetime
import gspread
import requests
import docx
from io import BytesIO
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

# --- SETUP ---
# Set up basic logging to see errors in Render
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
# This line solves all our CORS problems for the frontend
CORS(app) 

# --- GLOBAL VARIABLES ---
spreadsheet = None
feedback_sheet = None
products_sheet = None
admin_sheet = None
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# --- GOOGLE SHEETS CONNECTION ---
# This block attempts to connect to Google Sheets on startup
try:
    # Get credentials from Render's environment variables
    SERVICE_ACCOUNT_INFO = json.loads(os.environ.get('GCP_CREDENTIALS_JSON'))
    gc = gspread.service_account_from_dict(SERVICE_ACCOUNT_INFO)
    spreadsheet = gc.open("PharmaFeedbackApp")
    feedback_sheet = spreadsheet.worksheet("Feedback")
    products_sheet = spreadsheet.worksheet("Products")
    admin_sheet = spreadsheet.worksheet("AdminUsers")
    app.logger.info("Successfully connected to Google Sheets.")
except Exception as e:
    app.logger.error(f"FATAL: Could not connect to Google Sheets. Check credentials/sharing. Error: {e}", exc_info=True)


# --- GEMINI HELPER FUNCTIONS ---
def analyze_with_gemini(text):
    if not GEMINI_API_KEY:
        return {"category": "Config Error", "sentiment": 0, "error": "GEMINI_API_KEY not set."}

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={GEMINI_API_KEY}"
    prompt = f"""Analyze the following customer feedback. Provide a JSON object with two keys: "category" and "sentiment". The "category" must be one of the following: [Packaging, Formula, Color, Smell, Efficacy, Side Effect, Price, Documentation, Other]. The "sentiment" must be a number between -1.0 (very negative) and 1.0 (very positive). Feedback to analyze: \"{text}\""""
    
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    headers = {'Content-Type': 'application/json'}

    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        
        json_response = response.json()
        model_response_text = json_response['candidates'][0]['content']['parts'][0]['text']
        clean_json_string = model_response_text.replace('```json', '').replace('```', '').strip()
        analysis = json.loads(clean_json_string)
        
        return {"category": analysis.get('category', 'Parse Error'), "sentiment": analysis.get('sentiment', 0), "error": ""}
    except Exception as e:
        app.logger.error(f"Gemini API Error: {e}", exc_info=True)
        return {"category": "AI_Error", "sentiment": 0, "error": str(e)}

def generate_text_with_gemini(prompt):
    if not GEMINI_API_KEY:
        return "Error: GEMINI_API_KEY not set."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    headers = {'Content-Type': 'application/json'}

    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        json_response = response.json()
        return json_response['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        app.logger.error(f"Gemini text generation error: {e}", exc_info=True)
        return "Error: Could not generate report text from AI."


# --- API ENDPOINTS (ROUTES) ---

@app.route('/get-products', methods=['GET'])
def get_products():
    try:
        products = products_sheet.col_values(1)[1:] 
        return jsonify({"status": "success", "products": products})
    except Exception as e:
        app.logger.error("An error occurred in /get-products", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/submit-feedback', methods=['POST'])
def submit_feedback():
    try:
        data = request.json
        ai_analysis = analyze_with_gemini(f"Feedback: {data['feedbackText']}. Suggestion: {data['suggestionText']}")
        timestamp = datetime.utcnow().isoformat() + "Z" 
        
        new_row = [timestamp, data.get('productName'), data.get('feedbackText'), data.get('suggestionText'), data.get('clientName'), data.get('clientEmail'), ai_analysis['category'], ai_analysis['sentiment'], ai_analysis['error']]
        feedback_sheet.append_row(new_row)
        return jsonify({"status": "success", "message": "Feedback submitted."})
    except Exception as e:
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
        app.logger.error("An error occurred in /admin-login", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/get-all-feedback', methods=['GET'])
def get_all_feedback():
    try:
        all_feedback = feedback_sheet.get_all_records()
        return jsonify({"status": "success", "feedback": all_feedback})
    except Exception as e:
        app.logger.error("An error occurred in /get-all-feedback", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

# --- NEW ENDPOINT FOR REPORT GENERATION ---
@app.route('/generate-report', methods=['GET'])
def generate_report():
    try:
        app.logger.info("Report generation started.")
        all_feedback = feedback_sheet.get_all_records()
        if not all_feedback:
            return jsonify({"status": "error", "message": "No feedback data to generate a report."}), 404

        data_summary_for_prompt = "\n".join([str(item) for item in all_feedback])
        report_prompt = f"""
        You are a senior business analyst for a pharmaceutical company. Your task is to write a concise, professional executive summary report based on the following raw customer feedback data. The report must be in English and should include these sections:
        1. **Overall Summary:** A brief, high-level overview of the findings.
        2. **Key Positive Themes:** What are customers consistently happy about?
        3. **Key Areas for Improvement:** What are the most common complaints? Group similar issues.
        4. **Actionable Recommendations:** Suggest 2-3 specific, concrete actions the company should take.

        Do not just list the data. Synthesize it into an insightful report.
        --- RAW DATA ---
        {data_summary_for_prompt}
        --- END OF RAW DATA ---
        """
        
        app.logger.info("Sending data to Gemini for report generation...")
        generated_report_text = generate_text_with_gemini(report_prompt)
        app.logger.info("Report text received from Gemini.")

        document = docx.Document()
        document.add_heading('Customer Feedback Report', level=0)
        for paragraph in generated_report_text.split('\n'):
            if paragraph.strip():
                document.add_paragraph(paragraph)

        file_stream = BytesIO()
        document.save(file_stream)
        file_stream.seek(0)

        app.logger.info("Word document created. Sending file...")
        return send_file(
            file_stream,
            as_attachment=True,
            download_name='Pharma_Feedback_Report.docx',
            mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        )

    except Exception as e:
        app.logger.error("An error occurred in /generate-report", exc_info=True)
        return jsonify({"status": "error", "message": "Could not generate report."}), 500


# --- START THE SERVER ---
# This block must be the very last thing in the file.
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)