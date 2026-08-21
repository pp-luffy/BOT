import os
import sys
import json
import re
import time
import requests
from datetime import datetime, timedelta, timezone

# --- 1. SETUP & ENVIRONMENT VARIABLES ---
IST = timezone(timedelta(hours=5, minutes=30))
today_str = datetime.now(IST).strftime('%Y-%m-%d')

user_request = os.environ.get("USER_REQUEST", "").strip()
chat_id = str(os.environ.get("INCOMING_CHAT_ID", "")).strip()
telegram_token = os.environ.get("TELEGRAM_TOKEN", "").strip()
gemini_key = os.environ.get("AGENT_TOKEN", "").strip()
groq_key = os.environ.get("GROQ_API_KEY", "").strip()
exam_name = os.environ.get("EXAM_NAME", "Competitive Examination").strip()

# --- 2. USAGE TRACKER SETUP ---
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

# --- 3. EXTRACT TOPIC ---
topic = re.sub(r'^\s*/random(?:@\w+)?\s*', '', user_request, flags=re.IGNORECASE).strip()
if not topic:
    topic = "General Awareness & Concept Review"

if telegram_token and chat_id:
    requests.post(
        f"https://api.telegram.org/bot{telegram_token}/sendMessage",
        json={"chat_id": chat_id, "text": f"⏳ *Drafting High-Difficulty Quiz on:* _{topic}_\n_Consulting AI Examiner..._", "parse_mode": "Markdown"}
    )

# --- 4. SYSTEM PROMPT WITH VARIABLE EXAM NAME ---
system_prompt = f"""You are the most ruthless, expert question setter for the {exam_name}.
Your task is to create ultra-high-difficulty, conceptually rigorous Multiple Choice Questions (MCQs) based on the user's prompt.

QUESTION COUNT INSTRUCTION:
If the user specifies a question count (e.g., "10 questions", "3 questions"), produce EXACTLY that count. Max 10. Otherwise, produce 4 questions.

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

# --- 5. UNIFIED FALLBACK LIST (ALL 9 MODELS) ---
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
    print(f"🔄 Attempting generation with {model}...")
    
    if provider == "gemini" and gemini_key:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
        payload = {
            "contents": [{"parts": [{"text": system_prompt + "\n\nTopic / Request: " + topic}]}],
            "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"}
        }
        try:
            response = requests.post(url, json=payload, timeout=35)
            if response.status_code == 200:
                resp_json = response.json()
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
            print(f"⚠️ Error with {model}: {e}")

    elif provider == "groq" and groq_key:
        headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": f"Topic: {topic}"}],
            "response_format": {"type": "json_object"},
            "temperature": 0.3
        }
        try:
            response = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=35)
            if response.status_code == 200:
                resp_json = response.json()
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
            print(f"⚠️ Error with {model}: {e}")

# Save updated usage
if quiz_data and successful_model:
    usage_data["usage"][successful_model]["tokens_used"] += tokens_consumed_this_run
    usage_data["usage"][successful_model]["requests_used"] += 1
    with open(tracker_file, "w", encoding="utf-8") as f:
        json.dump(usage_data, f)

# --- 6. DISPATCH QUIZ TO TELEGRAM ---
if quiz_data and telegram_token and chat_id:
    for i, q in enumerate(quiz_data, 1):
        question_text = q.get("question", "").strip()
        options = [str(opt)[:95] for opt in q.get("options", [])][:4]
        correct_id = int(q.get("correct_option_id", 0))
        explanation = str(q.get("explanation", ""))[:190]
        poll_question = question_text

        if len(question_text) > 300:
            requests.post(
                f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                json={"chat_id": chat_id, "text": f"📋 *Q{i}.* {question_text}", "parse_mode": "Markdown"}
            )
            time.sleep(1.0)
            poll_question = f"Select correct option for Q{i} above:"

        requests.post(f"https://api.telegram.org/bot{telegram_token}/sendPoll", json={
            "chat_id": chat_id, 
            "question": poll_question, 
            "options": options, 
            "type": "quiz", 
            "correct_option_id": correct_id, 
            "explanation": explanation, 
            "is_anonymous": False
        })
        time.sleep(1.2)
        
    requests.post(
        f"https://api.telegram.org/bot{telegram_token}/sendMessage",
        json={"chat_id": chat_id, "text": f"✅ Quiz generated successfully using `{successful_model}` (*{tokens_consumed_this_run}* tokens).", "parse_mode": "Markdown"}
    )
else:
    if telegram_token and chat_id:
        requests.post(
            f"https://api.telegram.org/bot{telegram_token}/sendMessage",
            json={"chat_id": chat_id, "text": "⚠️ Failed to generate quiz across all fallback models. Please try again."}
        )
