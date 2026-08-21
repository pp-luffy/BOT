import os
import sys
import json
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

# --- HELPER: ZERO-DEPENDENCY HTTP REQUESTS ---
def post_json(url, payload=None, headers=None, timeout=35):
    headers = headers or {}
    if "Content-Type" not in headers:
        headers["Content-Type"] = "application/json"
        
    data = json.dumps(payload).encode('utf-8') if payload else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.getcode(), json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))
    except Exception as e:
        return 500, {"error": str(e)}

# --- 1. SETUP & ENVIRONMENT VARIABLES ---
IST = timezone(timedelta(hours=5, minutes=30))
today_str = datetime.now(IST).strftime('%Y-%m-%d')

user_request = os.environ.get("USER_REQUEST", "").strip()
chat_id = str(os.environ.get("INCOMING_CHAT_ID", "")).strip()
allowed_chat_id = str(os.environ.get("ALLOWED_CHAT_ID", "")).strip()
telegram_token = os.environ.get("TELEGRAM_TOKEN", "").strip()
gemini_key = os.environ.get("AGENT_TOKEN", "").strip()
groq_key = os.environ.get("GROQ_API_KEY", "").strip()
exam_name = os.environ.get("EXAM_NAME", "Competitive Examination").strip()

# --- 2. LAYER 1: SILENT AUTHORIZATION GATE ---
if not chat_id or chat_id != allowed_chat_id:
    print(f"⛔ Unauthorized access attempt from Chat ID: {chat_id}. Stopping silently.")
    sys.exit(0)

# --- 3. USAGE TRACKER SETUP ---
tracker_file = "usage_tracker.json"
usage_data = {
    "date": today_str,
    "usage": {
        "openai/gpt-oss-120b": {"tokens_used": 0, "requests_used": 0},
        "openai/gpt-oss-20b": {"tokens_used": 0, "requests_used": 0},
        "openai/gpt-oss-safeguard-20b": {"tokens_used": 0, "requests_used": 0},
        "qwen/qwen3.6-27b": {"tokens_used": 0, "requests_used": 0},
        "gemini-3.7-flash": {"tokens_used": 0, "requests_used": 0},
        "gemini-3.6-flash": {"tokens_used": 0, "requests_used": 0},
        "gemini-3.5-flash": {"tokens_used": 0, "requests_used": 0},
        "gemini-3.5-flash-lite": {"tokens_used": 0, "requests_used": 0},
        "gemini-3.1-flash-lite": {"tokens_used": 0, "requests_used": 0}
    }
}

def load_tracker():
    if os.path.exists(tracker_file):
        try:
            with open(tracker_file, "r", encoding="utf-8") as f:
                saved_data = json.load(f)
                if saved_data.get("date") == today_str:
                    for model_key in usage_data["usage"]:
                        if model_key in saved_data.get("usage", {}):
                            usage_data["usage"][model_key] = saved_data["usage"][model_key]
        except Exception:
            pass

def save_tracker():
    with open(tracker_file, "w", encoding="utf-8") as f:
        json.dump(usage_data, f)

load_tracker()

# --- 4. LAYER 2: LIGHTWEIGHT INTENT CHECKER ---
clean_request = re.sub(r'^\s*/(?:start|quiz|random)(?:@\w+)?\s*', '', user_request, flags=re.IGNORECASE).strip()
if not clean_request:
    clean_request = "Hello"

intent_prompt = f"""Analyze the user's message: "{clean_request}"
Classify it into EXACTLY ONE of these categories:
1. "casual": Greetings, thanks, general chat, or asking for help.
2. "quiz": Any educational topic, subject, or request for questions.

Return ONLY valid JSON matching this structure:
{{
  "intent": "casual" or "quiz",
  "reply": "If casual, write a short, friendly reply. If quiz, leave empty."
}}"""

intent = "quiz"
casual_reply = "Hello! How can I help you study today?"

print("🔄 Running lightweight intent check...")
if gemini_key:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={gemini_key}"
    payload = {
        "contents": [{"parts": [{"text": intent_prompt}]}],
        "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"}
    }
    
    status, resp_json = post_json(url, payload, timeout=15)
    
    if status == 200:
        try:
            raw_content = resp_json['candidates'][0]['content']['parts'][0]['text']
            raw_content = re.sub(r'^```(?:json)?\n?|```$', '', raw_content.strip(), flags=re.IGNORECASE).strip()
            parsed = json.loads(raw_content)
            
            intent = parsed.get("intent", "quiz").lower()
            if parsed.get("reply"):
                casual_reply = parsed.get("reply")
                
            tokens_used = resp_json.get('usageMetadata', {}).get('totalTokenCount', 0)
            usage_data["usage"]["gemini-3.1-flash-lite"]["tokens_used"] += tokens_used
            usage_data["usage"]["gemini-3.1-flash-lite"]["requests_used"] += 1
        except Exception as e:
            print(f"⚠️ JSON parsing failed: {e}")
    else:
        print(f"⚠️ Intent API Error ({status}): {resp_json}. Defaulting to quiz.")

# If casual, reply and exit immediately
if intent == "casual":
    tg_url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    post_json(tg_url, {"chat_id": chat_id, "text": casual_reply})
    save_tracker()
    print("✅ Handled casually. Exiting.")
    sys.exit(0)

# --- 5. QUIZ GENERATION FLOW ---
tg_url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
post_json(tg_url, {"chat_id": chat_id, "text": f"⏳ *Drafting High-Difficulty Quiz on:* _{clean_request}_\n_Consulting AI Examiner..._", "parse_mode": "Markdown"})

system_prompt = f"""You are the most ruthless, expert question setter for the {exam_name}.
Your task is to create ultra-high-difficulty, conceptually rigorous Multiple Choice Questions (MCQs) based on the user's prompt.

QUESTION COUNT INSTRUCTION:
If the user specifies a question count (e.g., "10 questions"), produce EXACTLY that count. Max 10. Otherwise, produce 4 questions.

CRITICAL RULES:
1. Explanations strictly under 190 characters. Options under 95 characters.
2. Return ONLY valid JSON matching this exact structure:
{{
  "questions": [
    {{
      "question": "Full question text...",
      "options": ["A", "B", "C", "D"],
      "correct_option_id": 1, 
      "explanation": "Brief explanation under 190 chars total."
    }}
  ]
}}"""

models_to_try = [
    ("gemini", "gemini-3.7-flash"),
    ("groq", "openai/gpt-oss-120b"),
    ("gemini", "gemini-3.6-flash"),
    ("gemini", "gemini-3.5-flash"),
    ("groq", "qwen/qwen3.6-27b"),
    ("groq", "openai/gpt-oss-20b"),
    ("groq", "openai/gpt-oss-safeguard-20b"),
    ("gemini", "gemini-3.5-flash-lite"),
    ("gemini", "gemini-3.1-flash-lite")
]

quiz_data = None
tokens_consumed_this_run = 0
successful_model = None

for provider, model in models_to_try:
    print(f"🔄 Attempting quiz generation with {model}...")
    
    if provider == "gemini" and gemini_key:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
        payload = {
            "contents": [{"parts": [{"text": system_prompt + "\n\nTopic / Request: " + clean_request}]}],
            "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"}
        }
        status, resp_json = post_json(url, payload)
        
        if status == 200:
            try:
                raw_content = resp_json['candidates'][0]['content']['parts'][0]['text']
                raw_content = re.sub(r'^```(?:json)?\n?|```$', '', raw_content.strip(), flags=re.IGNORECASE).strip()
                parsed = json.loads(raw_content).get("questions", [])
                if parsed:
                    quiz_data = parsed
                    tokens_consumed_this_run = resp_json.get('usageMetadata', {}).get('totalTokenCount', 0)
                    successful_model = model
                    print(f"✅ Success with {model}")
                    break
            except Exception as e:
                print(f"⚠️ Parsing error with {model}: {e}")
        else:
            print(f"⚠️ API error with {model}: {resp_json}")

    elif provider == "groq" and groq_key:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {groq_key}"}
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": f"Topic: {clean_request}"}],
            "response_format": {"type": "json_object"},
            "temperature": 0.3
        }
        status, resp_json = post_json(url, payload, headers)
        
        if status == 200:
            try:
                raw_content = resp_json['choices'][0]['message']['content']
                raw_content = re.sub(r'^```(?:json)?\n?|```$', '', raw_content.strip(), flags=re.IGNORECASE).strip()
                parsed = json.loads(raw_content).get("questions", [])
                if parsed:
                    quiz_data = parsed
                    tokens_consumed_this_run = resp_json.get('usage', {}).get('total_tokens', 0)
                    successful_model = model
                    print(f"✅ Success with {model}")
                    break 
            except Exception as e:
                print(f"⚠️ Parsing error with {model}: {e}")
        else:
            print(f"⚠️ API error with {model}: {resp_json}")

# --- 6. SAVE USAGE & DISPATCH QUIZ ---
if quiz_data and successful_model:
    usage_data["usage"][successful_model]["tokens_used"] += tokens_consumed_this_run
    usage_data["usage"][successful_model]["requests_used"] += 1
    save_tracker()

    for i, q in enumerate(quiz_data, 1):
        question_text = q.get("question", "").strip()
        options = [str(opt)[:95] for opt in q.get("options", [])][:4]
        correct_id = int(q.get("correct_option_id", 0))
        explanation = str(q.get("explanation", ""))[:190]
        poll_question = question_text

        if len(question_text) > 300:
            post_json(tg_url, {"chat_id": chat_id, "text": f"📋 *Q{i}.* {question_text}", "parse_mode": "Markdown"})
            time.sleep(1.0)
            poll_question = f"Select correct option for Q{i} above:"

        poll_url = f"https://api.telegram.org/bot{telegram_token}/sendPoll"
        post_json(poll_url, {
            "chat_id": chat_id, 
            "question": poll_question, 
            "options": options, 
            "type": "quiz", 
            "correct_option_id": correct_id, 
            "explanation": explanation, 
            "is_anonymous": False
        })
        time.sleep(1.2)
        
    post_json(tg_url, {"chat_id": chat_id, "text": f"✅ Quiz generated successfully using `{successful_model}` (*{tokens_consumed_this_run}* tokens).", "parse_mode": "Markdown"})
else:
    post_json(tg_url, {"chat_id": chat_id, "text": "⚠️ Failed to generate quiz across all fallback models. Please try again."})
