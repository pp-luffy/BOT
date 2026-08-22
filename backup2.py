import os
import sys
import json
import re
import time
import hashlib
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

# --- HELPER: ZERO-DEPENDENCY HTTP REQUESTS ---
def post_json(url, payload=None, headers=None, timeout=35):
    headers = headers or {}
    if "Content-Type" not in headers:
        headers["Content-Type"] = "application/json"
    
    if "User-Agent" not in headers:
        headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        
    data = json.dumps(payload).encode('utf-8') if payload else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode('utf-8')
            try:
                return response.getcode(), json.loads(body)
            except json.JSONDecodeError:
                return response.getcode(), {"error": "Invalid JSON format", "raw_body": body[:200]}
                
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"error": "Non-JSON HTTP Error (likely HTML)", "raw_body": body[:200]}
            
    except Exception as e:
        return 500, {"error": str(e)}

# --- 1. SETUP & ENVIRONMENT VARIABLES ---
IST = timezone(timedelta(hours=5, minutes=30))
today_str = datetime.now(IST).strftime('%Y-%m-%d')

user_request = os.environ.get("USER_REQUEST", "").strip()
chat_id = str(os.environ.get("INCOMING_CHAT_ID", "")).strip()
telegram_token = os.environ.get("TELEGRAM_TOKEN", "").strip()
admin_chat_id = str(os.environ.get("ADMIN_CHAT_ID", "")).strip()

# Clean hidden non-breaking spaces that can break JSON parsing
secrets_json = os.environ.get("USER_SECRETS_JSON", "{}").strip().replace("\u00a0", " ")
try:
    user_secrets = json.loads(secrets_json)
except json.JSONDecodeError as e:
    print(f"⚠️ Error parsing USER_SECRETS_JSON: {e}")
    user_secrets = {}

# Extract user-specific details if authorized
gemini_key = ""
groq_key = ""
default_exam_name = "Competitive Examination"

if chat_id in user_secrets:
    gemini_key = user_secrets[chat_id].get("gemini_key", "").strip()
    groq_key = user_secrets[chat_id].get("groq_key", "").strip()
    default_exam_name = user_secrets[chat_id].get("exam", "Competitive Examination").strip()

# Create a secure, anonymized hash of the Chat ID for public repository commits
hashed_user_id = hashlib.sha256(chat_id.encode('utf-8')).hexdigest()[:16] if chat_id else "unknown"

# --- 2. USAGE TRACKER SETUP ---
tracker_file = "usage_tracker.json"
usage_data = {
    "date": today_str,
    "unauthorized_alerts": 0,
    "users": {}  # Will hold { hashed_id: { exam: "...", usage: { ... } } }
}

def load_tracker():
    if os.path.exists(tracker_file):
        try:
            with open(tracker_file, "r", encoding="utf-8") as f:
                saved_data = json.load(f)
                
                # Retain user preferences (e.g. active exam setting) permanently across date rollovers
                for user_hash, user_info in saved_data.get("users", {}).items():
                    usage_data["users"].setdefault(user_hash, {
                        "exam": user_info.get("exam", default_exam_name)
                    })
                    
                # Restore API usage counts only if date matches today
                if saved_data.get("date") == today_str:
                    usage_data["unauthorized_alerts"] = saved_data.get("unauthorized_alerts", 0)
                    for user_hash, user_info in saved_data.get("users", {}).items():
                        if "usage" in user_info:
                            usage_data["users"][user_hash]["usage"] = user_info["usage"]
        except Exception:
            pass

def save_tracker():
    with open(tracker_file, "w", encoding="utf-8") as f:
        json.dump(usage_data, f, indent=2)

def record_usage(model_name, tokens):
    user_data = usage_data["users"].setdefault(hashed_user_id, {"exam": default_exam_name, "usage": {}})
    usage_stats = user_data.setdefault("usage", {})
    model_stats = usage_stats.setdefault(model_name, {"tokens_used": 0, "requests_used": 0})
    model_stats["tokens_used"] += tokens
    model_stats["requests_used"] += 1

load_tracker()

# Ensure current authorized user is present in usage data
if hashed_user_id not in usage_data["users"] and chat_id in user_secrets:
    usage_data["users"][hashed_user_id] = {"exam": default_exam_name, "usage": {}}

# --- 3. LAYER 1: SILENT AUTHORIZATION GATE ---
if not chat_id or chat_id not in user_secrets:
    print(f"⛔ Unauthorized access attempt from Chat ID: {chat_id}. Stopping silently.")
    alerts_sent = usage_data.get("unauthorized_alerts", 0)
    if alerts_sent < 3:
        if admin_chat_id and telegram_token:
            alert_url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
            safe_request = user_request.replace('*', '').replace('_', '').replace('`', '').replace('[', '')
            alert_text = f"⚠️ *Unauthorized Access Attempt*\nAn unknown user with Chat ID `{chat_id}` tried to access your bot.\n\n*Their message:* {safe_request}"
            post_json(alert_url, {"chat_id": admin_chat_id, "text": alert_text, "parse_mode": "Markdown"})
        usage_data["unauthorized_alerts"] = alerts_sent + 1
        save_tracker()
    sys.exit(0)

# --- 4. LAYER 2: LIGHTWEIGHT INTENT & EXAM CHECKER ---
clean_request = re.sub(r'^\s*/(?:start|quiz|random)(?:@\w+)?\s*', '', user_request, flags=re.IGNORECASE).strip()
if not clean_request:
    clean_request = "Hello"

current_saved_exam = usage_data["users"][hashed_user_id].get("exam", default_exam_name)
intent_prompt = f"""Analyze the user's message: "{clean_request}"
Classify it into EXACTLY ONE of these categories:
1. "quiz": Any educational topic, subject, or request for questions.
2. "casual": Greetings, thanks, general chat, or asking for help.

SYSTEM CONTEXT: The user's currently active exam setting is "{current_saved_exam}". If the user asks what exam they are set to, classify as "casual" and clearly tell them this exam name in the reply.

Also, check if the user explicitly mentions studying for a specific exam, test, or certification (e.g., "UPSC", "JEE", "AWS", "SAT", "NEET", "OPSC", "OSSC").

Return ONLY valid JSON matching this structure:
{{
  "intent": "casual" or "quiz",
  "reply": "If casual, write a short, friendly reply. If quiz, leave empty.",
  "extracted_exam": "Name of the exam if explicitly mentioned, otherwise null."
}}"""

intent = "quiz"
casual_reply = "Hello! How can I help you study today?"

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
            extracted_exam = parsed.get("extracted_exam")
            if extracted_exam:
                usage_data["users"][hashed_user_id]["exam"] = extracted_exam
                save_tracker()
            
            tokens_used = resp_json.get('usageMetadata', {}).get('totalTokenCount', 0)
            record_usage("gemini-3.1-flash-lite", tokens_used)
        except Exception:
            pass

if intent == "casual":
    tg_url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    post_json(tg_url, {"chat_id": chat_id, "text": casual_reply})
    save_tracker()
    sys.exit(0)

# --- 5. QUIZ GENERATION FLOW (ENGLISH ONLY) ---
active_exam_name = usage_data["users"][hashed_user_id].get("exam", default_exam_name)
target_language = "Odia" if ("odia" in active_exam_name.lower() or "odia" in clean_request.lower()) else "English"

tg_url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
post_json(tg_url, {"chat_id": chat_id, "text": f"⏳ *Drafting Conceptual Quiz on:* _{clean_request}_\n_Target Exam: {active_exam_name}_\n_Language: {target_language}..._", "parse_mode": "Markdown"})

random_seed = int(time.time() * 1000)

system_prompt = f"""You are the most ruthless and expert question setter for {active_exam_name}.
Your task is to create ultra-high-difficulty, conceptually rigorous Multiple Choice Questions (MCQs) based on the user's prompt.

[GENERATION SEED: {random_seed}]

EXAM STYLE & QUALITY INSTRUCTIONS:
1. STRICTLY AVOID basic factual, direct definition, or memory-based questions.
2. Focus on conceptual clarity, multi-faceted analysis, interlinkages, and application of knowledge.
3. For conceptual depth, place complex statements IN THE QUESTION BODY. Example: "Consider the following statements regarding [Topic]: 1. [Statement A] 2. [Statement B]. Which of the statements given above is/are correct?"
4. Keep the actual options extremely short (e.g., "1 only", "2 only", "Both 1 and 2", "Neither 1 nor 2") so they fit inside Telegram's strict limits.
5. 🛑 CRITICAL: GENERATE ENTIRELY IN ENGLISH, regardless of the target exam.

QUESTION COUNT INSTRUCTION:
If the user specifies a count, you MUST produce EXACTLY that count (Max 15). Otherwise, produce 4 questions. DO NOT STOP EARLY.

CRITICAL JSON RULES:
1. Explanations strictly under 190 chars. Options strictly under 95 chars.
2. `correct_option_id` MUST be a 0-based integer index (0, 1, 2, or 3).
3. Return ONLY valid JSON matching this exact structure:
{{
  "questions": [
    {{
      "question": "Consider the following statements: 1. ... 2. ... Which is correct?",
      "options": ["1 only", "2 only", "Both 1 and 2", "Neither 1 nor 2"],
      "correct_option_id": 2, 
      "explanation": "Brief explanation validating why statement 1 and 2 are true/false under 190 chars total."
    }}
  ]
}}"""

models_to_try = [
    ("groq", "openai/gpt-oss-120b"),
    ("gemini", "gemini-3.7-flash"),
    ("gemini", "gemini-3.6-flash"),
    ("gemini", "gemini-3.5-flash"),
    ("groq", "qwen/qwen3.6-27b"),
    ("groq", "openai/gpt-oss-20b")
]

quiz_data = None
gen_model = None
gen_tokens = 0

for provider, model in models_to_try:
    print(f"🔄 Attempting generation with {model}...")
    if provider == "gemini" and gemini_key:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
        payload = {
            "contents": [{"parts": [{"text": system_prompt + "\n\nTopic / Request: " + clean_request}]}],
            "generationConfig": {"temperature": 0.75, "maxOutputTokens": 4096, "responseMimeType": "application/json"}
        }
        status, resp_json = post_json(url, payload)
        if status == 200:
            try:
                raw_content = resp_json['candidates'][0]['content']['parts'][0]['text']
                raw_content = re.sub(r'^```(?:json)?\n?|```$', '', raw_content.strip(), flags=re.IGNORECASE).strip()
                parsed = json.loads(raw_content).get("questions", [])
                if parsed:
                    quiz_data = parsed
                    gen_tokens = resp_json.get('usageMetadata', {}).get('totalTokenCount', 0)
                    gen_model = model
                    break
            except Exception:
                pass

    elif provider == "groq" and groq_key:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {groq_key}"}
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": f"Topic: {clean_request}"}],
            "response_format": {"type": "json_object"},
            "temperature": 0.75,
            "max_tokens": 4096 
        }
        status, resp_json = post_json(url, payload, headers)
        if status == 200:
            try:
                raw_content = resp_json['choices'][0]['message']['content']
                raw_content = re.sub(r'^```(?:json)?\n?|```$', '', raw_content.strip(), flags=re.IGNORECASE).strip()
                parsed = json.loads(raw_content).get("questions", [])
                if parsed:
                    quiz_data = parsed
                    gen_tokens = resp_json.get('usage', {}).get('total_tokens', 0)
                    gen_model = model
                    break 
            except Exception:
                pass

# --- 5.5 LAYER: VERIFICATION AND CORRECTION LOOP (ENGLISH ONLY) ---
ver_model = None
ver_tokens = 0

if quiz_data and gen_model:
    post_json(tg_url, {"chat_id": chat_id, "text": f"🔍 *Verifying {len(quiz_data)} generated answers for factual accuracy...*", "parse_mode": "Markdown"})
    
    verify_prompt = f"""You are an expert fact-checker and exam reviewer for {active_exam_name}.
Below is a JSON containing multiple-choice questions generated by an AI.
Your job is to independently verify EVERY question. 

TASK:
1. Ensure the `correct_option_id` (0-based index) actually points to the factually correct option. 
2. If the answer is wrong, correct the `correct_option_id` to point to the right answer, or re-write the options to make it accurate.
3. Fix any misleading information in the `explanation`.
4. 🛑 CRITICAL: VERIFY ENTIRELY IN ENGLISH. DO NOT TRANSLATE.
5. Most important: check statements one by one again and validate if correct and check if given answer is correct.
6. explanation should also be validated.

CRITICAL RULES:
1. Explanations strictly under 190 chars. Options under 95 chars.
2. `correct_option_id` MUST be a 0-based index (0, 1, 2, or 3).
3. YOU MUST RETURN EXACTLY {len(quiz_data)} QUESTIONS. Do not remove, delete, or skip any questions from the input JSON.
4. Return ONLY valid JSON matching the exact original structure: {{"questions": [...]}}

INPUT JSON TO VERIFY:
{json.dumps({"questions": quiz_data}, indent=2)}"""

    for provider, model in models_to_try:
        print(f"🔍 Attempting verification with {model}...")
        if provider == "gemini" and gemini_key:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
            payload = {
                "contents": [{"parts": [{"text": verify_prompt}]}],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096, "responseMimeType": "application/json"}
            }
            status, resp_json = post_json(url, payload)
            if status == 200:
                try:
                    raw_content = resp_json['candidates'][0]['content']['parts'][0]['text']
                    raw_content = re.sub(r'^```(?:json)?\n?|```$', '', raw_content.strip(), flags=re.IGNORECASE).strip()
                    parsed = json.loads(raw_content).get("questions", [])
                    if parsed:
                        quiz_data = parsed  
                        ver_tokens = resp_json.get('usageMetadata', {}).get('totalTokenCount', 0)
                        ver_model = model
                        print(f"✅ Verification successful ({len(quiz_data)} questions).")
                        break
                except Exception:
                    pass

        elif provider == "groq" and groq_key:
            url = "https://api.groq.com/openai/v1/chat/completions"
            headers = {"Authorization": f"Bearer {groq_key}"}
            payload = {
                "model": model,
                "messages": [{"role": "system", "content": verify_prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
                "max_tokens": 4096
            }
            status, resp_json = post_json(url, payload, headers)
            if status == 200:
                try:
                    raw_content = resp_json['choices'][0]['message']['content']
                    raw_content = re.sub(r'^```(?:json)?\n?|```$', '', raw_content.strip(), flags=re.IGNORECASE).strip()
                    parsed = json.loads(raw_content).get("questions", [])
                    if parsed:
                        quiz_data = parsed  
                        ver_tokens = resp_json.get('usage', {}).get('total_tokens', 0)
                        ver_model = model
                        print(f"✅ Verification successful ({len(quiz_data)} questions).")
                        break 
                except Exception:
                    pass

# --- 5.7 LAYER: CHEAP TRANSLATION TO ODIA ---
trans_tokens = 0
trans_model = None

if quiz_data and target_language == "Odia" and gemini_key:
    post_json(tg_url, {"chat_id": chat_id, "text": "🔤 *Translating into Odia...*", "parse_mode": "Markdown"})
    
    translation_prompt = f"""You are an expert translator. 
Translate the 'question', 'options', and 'explanation' values in the following JSON into academic Odia suitable for a competitive exam.
DO NOT change the 'correct_option_id' numbers. DO NOT change the JSON keys.
Return ONLY valid JSON matching the exact original structure.

INPUT JSON:
{json.dumps({"questions": quiz_data}, ensure_ascii=False)}"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={gemini_key}"
    payload = {
        "contents": [{"parts": [{"text": translation_prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
    }
    status, resp_json = post_json(url, payload)
    
    if status == 200:
        try:
            raw_content = resp_json['candidates'][0]['content']['parts'][0]['text']
            raw_content = re.sub(r'^```(?:json)?\n?|```$', '', raw_content.strip(), flags=re.IGNORECASE).strip()
            parsed = json.loads(raw_content).get("questions", [])
            if parsed:
                quiz_data = parsed
                trans_tokens = resp_json.get('usageMetadata', {}).get('totalTokenCount', 0)
                trans_model = "gemini-3.1-flash-lite"
        except Exception:
            pass

# --- 6. SAVE USAGE & DISPATCH QUIZ ---
if quiz_data and gen_model:
    record_usage(gen_model, gen_tokens)
    if ver_model:
        record_usage(ver_model, ver_tokens)
    if trans_model:
        record_usage(trans_model, trans_tokens)
    
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
        
    total_tokens = gen_tokens + ver_tokens + trans_tokens
    footer_text = f"✅ Generated {len(quiz_data)} conceptual questions in {target_language} using `{gen_model}`."
    if ver_model:
        footer_text += f"\n🔍 Verified by `{ver_model}`."
    if trans_model:
        footer_text += f"\n🔤 Translated by `{trans_model}`."
    footer_text += f"\n*(Total tokens: {total_tokens})*"
        
    post_json(tg_url, {"chat_id": chat_id, "text": footer_text, "parse_mode": "Markdown"})
else:
    post_json(tg_url, {"chat_id": chat_id, "text": "⚠️ Failed to generate quiz across all fallback models. Please try again."})
