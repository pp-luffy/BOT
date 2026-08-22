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
default_difficulty = 3  # Level 1 (Easiest) to Level 5 (Most Brutal)

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
    "users": {}  
}

def load_tracker():
    if os.path.exists(tracker_file):
        try:
            with open(tracker_file, "r", encoding="utf-8") as f:
                saved_data = json.load(f)
                
                # Retain user preferences (exam and difficulty) permanently across date rollovers
                for user_hash, user_info in saved_data.get("users", {}).items():
                    usage_data["users"].setdefault(user_hash, {
                        "exam": user_info.get("exam", default_exam_name),
                        "difficulty": user_info.get("difficulty", default_difficulty)
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
    user_data = usage_data["users"].setdefault(hashed_user_id, {"exam": default_exam_name, "difficulty": default_difficulty, "usage": {}})
    usage_stats = user_data.setdefault("usage", {})
    model_stats = usage_stats.setdefault(model_name, {"tokens_used": 0, "requests_used": 0})
    model_stats["tokens_used"] += tokens
    model_stats["requests_used"] += 1

load_tracker()

if hashed_user_id not in usage_data["users"] and chat_id in user_secrets:
    usage_data["users"][hashed_user_id] = {"exam": default_exam_name, "difficulty": default_difficulty, "usage": {}}

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

# --- 4. LAYER 2: LIGHTWEIGHT INTENT, EXAM & DIFFICULTY CHECKER ---
clean_request = re.sub(r'^\s*/(?:start|quiz|random)(?:@\w+)?\s*', '', user_request, flags=re.IGNORECASE).strip()
if not clean_request:
    clean_request = "Hello"

current_saved_exam = usage_data["users"][hashed_user_id].get("exam", default_exam_name)
current_saved_difficulty = usage_data["users"][hashed_user_id].get("difficulty", default_difficulty)

intent_prompt = f"""Analyze the user's message: "{clean_request}"
Classify it into EXACTLY ONE of these categories:
1. "quiz": Any educational topic, subject, or request for questions.
2. "casual": Greetings, thanks, general chat, asking for help, OR requesting an exam/difficulty setting change without a topic.

SYSTEM CONTEXT: 
- Current active exam setting: "{current_saved_exam}"
- Current difficulty level: {current_saved_difficulty} (Scale 1-5, 1=Easiest, 5=Most brutal)

EXTRACTION TASKS:
1. EXAM: Check if the user explicitly mentions studying for a specific exam/test (e.g., "UPSC", "JEE", "AWS").
2. DIFFICULTY: Check if they want to change the difficulty. If they say "level 4", set to 4. If they say "increase difficulty" or "make it harder", add 1 to the current level. If "decrease" or "easier", subtract 1. Cap it between 1 and 5.

Return ONLY valid JSON matching this structure:
{{
  "intent": "casual" or "quiz",
  "reply": "If casual, write a short, friendly reply (e.g., acknowledging setting changes). If quiz, leave empty.",
  "extracted_exam": "Name of the exam if explicitly mentioned, otherwise null.",
  "extracted_difficulty": Integer between 1 and 5 if a difficulty change is requested, otherwise null.
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
                
            settings_changed = False
            
            extracted_exam = parsed.get("extracted_exam")
            if extracted_exam:
                usage_data["users"][hashed_user_id]["exam"] = extracted_exam
                settings_changed = True
                
            extracted_difficulty = parsed.get("extracted_difficulty")
            if extracted_difficulty is not None and isinstance(extracted_difficulty, int):
                extracted_difficulty = max(1, min(5, extracted_difficulty)) 
                usage_data["users"][hashed_user_id]["difficulty"] = extracted_difficulty
                settings_changed = True
                
            if settings_changed:
                save_tracker()
            
            tokens_used = resp_json.get('usageMetadata', {}).get('totalTokenCount', 0)
            record_usage("gemini-3.1-flash-lite", tokens_used)
        except Exception:
            pass

if intent == "casual":
    tg_url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    post_json(tg_url, {"chat_id": chat_id, "text": casual_reply})
    sys.exit(0)

# --- 5. QUIZ GENERATION FLOW (ENGLISH ONLY) ---
active_exam_name = usage_data["users"][hashed_user_id].get("exam", default_exam_name)
active_difficulty = usage_data["users"][hashed_user_id].get("difficulty", default_difficulty)
target_language = "Odia" if ("odia" in active_exam_name.lower() or "odia" in clean_request.lower()) else "English"

tg_url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
post_json(tg_url, {"chat_id": chat_id, "text": f"⏳ *Drafting Quiz...*\n_Topic: {clean_request}_\n_Exam: {active_exam_name}_\n_Difficulty: Level {active_difficulty}/5_\n_Language: {target_language}_", "parse_mode": "Markdown"})

random_seed = int(time.time() * 1000)

difficulty_map = {
    1: "Level 1 (Easiest): Basic, straightforward factual questions. Single-concept.",
    2: "Level 2 (Easy-Moderate): Standard questions requiring basic conceptual understanding.",
    3: "Level 3 (Moderate-Hard): Standard competitive exam difficulty. Requires conceptual clarity and analysis.",
    4: "Level 4 (Hard): High difficulty. Multi-faceted analysis, complex interlinkages.",
    5: "Level 5 (Most Brutal): Ultra-high difficulty. Extremely rigorous, highly complex multi-statement questions designed to trap even experts."
}
difficulty_instruction = difficulty_map.get(active_difficulty, difficulty_map[3])

# --- DYNAMIC TEMPERATURE SCALING ---
temp_map = {
    1: {"temp": 0.70, "top_p": 0.85},
    2: {"temp": 0.75, "top_p": 0.90},
    3: {"temp": 0.80, "top_p": 0.95},
    4: {"temp": 0.85, "top_p": 0.95},
    5: {"temp": 0.95, "top_p": 0.98}
}

gen_settings = temp_map.get(active_difficulty, temp_map[3])
gen_temp = gen_settings["temp"]
gen_top_p = gen_settings["top_p"]

system_prompt = f"""You are an expert question setter and examiner for {active_exam_name}.
Your task is to create Multiple Choice Questions (MCQs) based on the user's prompt.

[GENERATION SEED: {random_seed}]
[DIFFICULTY SETTING: {difficulty_instruction}]

EXAM STYLE & QUALITY INSTRUCTIONS:
1. TARGET DIFFICULTY: Strictly adhere to the requested {difficulty_instruction}. Adjust the complexity of the concepts and distractors accordingly.
2. 🛑 SYLLABUS & GEOGRAPHICAL CONTEXT: The questions MUST strictly align with the standard syllabus of the {active_exam_name}. If it is an Indian competitive exam (e.g., UPSC, State PSCs), strongly anchor historical, political, and economic topics strictly to the Indian context and their impact on India. DO NOT include irrelevant foreign domestic trivia unless explicitly asked.
3. QUESTION FORMAT: For levels 3, 4, and 5, use complex multi-statement formats in the QUESTION BODY. Example: "Consider the following statements regarding [Topic]: 1. [Statement A] 2. [Statement B]. Which is correct?"
4. Keep the actual options extremely short (e.g., "1 only", "2 only", "Both 1 and 2", "Neither 1 nor 2") so they fit inside Telegram's strict limits.
5. 🛑 NOVELTY & RANDOMNESS: Use the GENERATION SEED to guarantee absolute novelty. NEVER generate standard, textbook, or overused questions. Explore obscure, highly specific, and creative sub-topics.
6. 🛑 CRITICAL: GENERATE ENTIRELY IN ENGLISH, regardless of the target exam.
7. Always independently verify specific factual claims against authoritative sources.

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

# 🛑 GENERATION MODELS (Gemini First)
generation_models_to_try = [
    ("gemini", "gemini-3.7-flash"),
    ("groq", "openai/gpt-oss-120b"),
    ("gemini", "gemini-3.6-flash"),
    ("gemini", "gemini-3.5-flash"),
    ("groq", "qwen/qwen3.6-27b"),
    ("groq", "openai/gpt-oss-20b")
]

# 🛑 VERIFICATION MODELS (Groq First)
verification_models_to_try = [
    ("groq", "openai/gpt-oss-120b"),
    ("gemini", "gemini-3.7-flash"),
    ("gemini", "gemini-3.6-flash"),
    ("gemini", "gemini-3.5-flash"),
    ("groq", "qwen/qwen3.6-27b"),
    ("groq", "openai/gpt-oss-20b")
]

# 👑 VIP USER INJECTION (Admin only & Level 5 only)
if chat_id == admin_chat_id and active_difficulty == 5:
    generation_models_to_try.insert(0, ("gemini", "gemini-3.1-pro"))
    verification_models_to_try.insert(0, ("gemini", "gemini-3.1-pro"))

quiz_data = None
gen_model = None
gen_tokens = 0

for provider, model in generation_models_to_try:
    print(f"🔄 Attempting generation with {model} at Temp: {gen_temp}...")
    if provider == "gemini" and gemini_key:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
        payload = {
            "contents": [{"parts": [{"text": system_prompt + "\n\nTopic / Request: " + clean_request}]}],
            "generationConfig": {
                "temperature": gen_temp, 
                "topP": gen_top_p, 
                "maxOutputTokens": 4096, 
                "responseMimeType": "application/json"
            }
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
            "temperature": gen_temp,
            "top_p": gen_top_p,
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

# --- 5.5 LAYER: VERIFICATION AND CORRECTION LOOP (BATCHED) ---
ver_models_used = set()
ver_tokens = 0

if quiz_data and gen_model:
    post_json(tg_url, {"chat_id": chat_id, "text": f"🔍 *Verifying {len(quiz_data)} generated answers for factual accuracy...*", "parse_mode": "Markdown"})
    
    verified_quiz_data = []
    chunk_size = 4  # Process in batches of 4 to prevent LLM Array Laziness
    
    for i in range(0, len(quiz_data), chunk_size):
        chunk = quiz_data[i:i+chunk_size]
        chunk_verified = False
        
        verify_prompt = f"""You are an expert fact-checker and exam reviewer for {active_exam_name}.
Below is a JSON containing {len(chunk)} multiple-choice questions generated by an AI.
Your job is to independently verify EVERY question using a zero-trust approach.

TASK:
1. Verify the facts from scratch: Do not assume the provided explanation or answer key is accurate.
2. For multi-statement questions: Establish the absolute truth value of EACH statement independently BEFORE evaluating the options. Watch for nuanced terminology errors.
3. Ensure the `correct_option_id` (0-based index) actually points to the factually correct option. 
4. If a question contains false premises or flawed statements, you MUST rewrite the question text, the statements, OR the options to create a completely accurate question.
5. Fix any misleading information or hallucinations in the `explanation`. The explanation must explicitly justify why the right answer is right, and why the others are wrong.
6. 🛑 CRITICAL: VERIFY ENTIRELY IN ENGLISH. DO NOT TRANSLATE.

CRITICAL RULES:
1. Explanations strictly under 190 chars. Options under 95 chars.
2. `correct_option_id` MUST be a 0-based index (0, 1, 2, or 3).
3. YOU MUST RETURN EXACTLY {len(chunk)} QUESTIONS. Do not remove, delete, or skip any questions from the input JSON.
4. Return ONLY valid JSON matching the exact original structure: {{"questions": [...]}}

INPUT JSON TO VERIFY:
{json.dumps({"questions": chunk}, indent=2)}"""

        for provider, model in verification_models_to_try:
            print(f"🔍 Attempting chunk verification with {model}...")
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
                        if parsed and len(parsed) == len(chunk):
                            verified_quiz_data.extend(parsed)
                            tokens = resp_json.get('usageMetadata', {}).get('totalTokenCount', 0)
                            ver_tokens += tokens
                            ver_models_used.add(model)
                            record_usage(model, tokens) # Dynamically record chunk verification tokens
                            chunk_verified = True
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
                        if parsed and len(parsed) == len(chunk):
                            verified_quiz_data.extend(parsed)
                            tokens = resp_json.get('usage', {}).get('total_tokens', 0)
                            ver_tokens += tokens
                            ver_models_used.add(model)
                            record_usage(model, tokens)
                            chunk_verified = True
                            break 
                    except Exception:
                        pass
        
        if not chunk_verified:
            print("⚠️ All verification models failed for this chunk. Appending unverified original.")
            verified_quiz_data.extend(chunk)

    quiz_data = verified_quiz_data

# --- 5.7 LAYER: CHEAP TRANSLATION TO ODIA (BATCHED) ---
trans_tokens = 0
trans_model = None

if quiz_data and target_language == "Odia" and gemini_key:
    post_json(tg_url, {"chat_id": chat_id, "text": "🔤 *Translating into Odia...*", "parse_mode": "Markdown"})
    
    translated_quiz_data = []
    chunk_size = 4
    
    for i in range(0, len(quiz_data), chunk_size):
        chunk = quiz_data[i:i+chunk_size]
        
        translation_prompt = f"""You are an expert translator. 
Translate the 'question', 'options', and 'explanation' values in the following JSON into academic Odia suitable for a competitive exam.
DO NOT change the 'correct_option_id' numbers. DO NOT change the JSON keys.
Return ONLY valid JSON matching the exact original structure.

INPUT JSON:
{json.dumps({"questions": chunk}, ensure_ascii=False)}"""

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={gemini_key}"
        payload = {
            "contents": [{"parts": [{"text": translation_prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
        }
        status, resp_json = post_json(url, payload)
        
        chunk_translated = False
        if status == 200:
            try:
                raw_content = resp_json['candidates'][0]['content']['parts'][0]['text']
                raw_content = re.sub(r'^```(?:json)?\n?|```$', '', raw_content.strip(), flags=re.IGNORECASE).strip()
                parsed = json.loads(raw_content).get("questions", [])
                
                if parsed and len(parsed) == len(chunk):
                    translated_quiz_data.extend(parsed)
                    tokens = resp_json.get('usageMetadata', {}).get('totalTokenCount', 0)
                    trans_tokens += tokens
                    record_usage("gemini-3.1-flash-lite", tokens)
                    trans_model = "gemini-3.1-flash-lite"
                    chunk_translated = True
            except Exception:
                pass
                
        if not chunk_translated:
            translated_quiz_data.extend(chunk)
            
    quiz_data = translated_quiz_data

# --- 6. SAVE USAGE & DISPATCH QUIZ ---
if quiz_data and gen_model:
    record_usage(gen_model, gen_tokens)
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
    
    if ver_models_used:
        ver_model_str = ", ".join(ver_models_used)
        footer_text += f"\n🔍 Verified by `{ver_model_str}`."
    else:
        footer_text += f"\n⚠️ *Sent unverified* (Verification models timed out)."
        
    if trans_model:
        footer_text += f"\n🔤 Translated by `{trans_model}`."
        
    footer_text += f"\n*(Total tokens: {total_tokens})*"
        
    post_json(tg_url, {"chat_id": chat_id, "text": footer_text, "parse_mode": "Markdown"})
else:
    post_json(tg_url, {"chat_id": chat_id, "text": "⚠️ Failed to generate quiz across all fallback models. Please try again."})
