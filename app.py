
import os, json, re, difflib
from flask import Flask, request, Response
from dotenv import load_dotenv
import psycopg2
from psycopg2 import pool
from openai import OpenAI
from twilio.twiml.messaging_response import MessagingResponse

# -------------------------------------------------
# Boot
# -------------------------------------------------
load_dotenv()
PORT = int(os.getenv("PORT", 5000))
AZURE_DB_HOST = "thalabot-db.postgres.database.azure.com"
AZURE_DB_NAME = "postgres"
AZURE_DB_USER = "bloodbot"
AZURE_DB_PASSWORD = os.getenv("AZURE_DB_PASSWORD")  # Set this in your .env
AZURE_DB_PORT = 5432
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
PREFERRED_MODEL = os.getenv("OPENAI_MODEL", "gpt-5")

if not AZURE_DB_PASSWORD:
    raise RuntimeError("Missing AZURE_DB_PASSWORD in .env")
if not OPENAI_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY in .env")

client = OpenAI(api_key=OPENAI_KEY)

app = Flask(__name__)
sessions = {}

# -------------------------------------------------
# PostgreSQL Connection Pool
# -------------------------------------------------
db_pool = psycopg2.pool.SimpleConnectionPool(
    minconn=1,
    maxconn=10,
    host=AZURE_DB_HOST,
    dbname=AZURE_DB_NAME,
    user=AZURE_DB_USER,
    password=AZURE_DB_PASSWORD,
    port=AZURE_DB_PORT,
    sslmode="require"
)

def get_db_conn():
    return db_pool.getconn()

def release_db_conn(conn):
    db_pool.putconn(conn)

# -------------------------------------------------
# Constants / dictionaries
# -------------------------------------------------
BLOODS = {"A+","A-","B+","B-","AB+","AB-","O+","O-"}
BLOOD_SYNONYMS = {
    "A POS": "A+", "A POSITIVE":"A+", "A PLUS":"A+",
    "A NEG": "A-", "A NEGATIVE":"A-",
    "B POS": "B+", "B POSITIVE":"B+",
    "B NEG": "B-", "B NEGATIVE":"B-",
    "AB POS":"AB+", "AB POSITIVE":"AB+",
    "AB NEG":"AB-", "AB NEGATIVE":"AB-",
    "O POS":"O+", "O POSITIVE":"O+","O PLUS":"O+",
    "O NEG":"O-", "O NEGATIVE":"O-",
    "APOS":"A+","ANEG":"A-","BPOS":"B+","BNEG":"B-","ABPOS":"AB+","ABNEG":"AB-","OPOS":"O+","ONEG":"O-",
}
INDIAN_CITIES = [
    "Mumbai","Delhi","Bengaluru","Bangalore","Hyderabad","Ahmedabad","Chennai","Kolkata","Surat","Pune",
    "Jaipur","Lucknow","Kanpur","Nagpur","Indore","Thane","Bhopal","Visakhapatnam","Patna","Vadodara",
    "Ghaziabad","Ludhiana","Agra","Nashik","Faridabad","Meerut","Rajkot","Kalyan","Vasai","Srinagar",
    "Aurangabad","Dhanbad","Amritsar","Navi Mumbai","Allahabad","Prayagraj","Ranchi","Howrah","Coimbatore",
    "Jabalpur","Gwalior","Vijayawada","Jodhpur","Madurai","Raipur","Kota","Chandigarh","Guwahati",
    "Solapur","Hubli","Dharwad","Bareilly","Moradabad","Mysuru","Mysore","Gurugram","Gurgaon",
    "Aligarh","Jalandhar","Tiruchirappalli","Bhubaneswar","Salem","Warangal","Mira Bhayandar","Thiruvananthapuram",
    "Trivandrum","Bhiwandi","Saharanpur","Gorakhpur","Bikaner","Amravati","Noida","Jamshedpur","Bhilai",
    "Cuttack","Firozabad","Kochi","Ernakulam","Nellore","Bhavnagar","Dehradun","Durgapur","Asansol",
    "Rourkela","Nanded","Kolhapur","Ajmer","Akola","Gulbarga","Belgaum","Jamnagar","Ujjain","Loni",
    "Siliguri","Jhansi","Ulhasnagar","Jammu","Sangli","Mangalore","Erode","Tirunelveli","Muzaffarpur","Udaipur",
    "Rohtak","Karnal","Panipat","Rohini","Dwarka","Greater Noida"
]

# -------------------------------------------------
# Utilities
# -------------------------------------------------
def twiml_reply(text: str):
    r = MessagingResponse()
    r.message(text)
    xml = str(r)
    print("⬅ TwiML:", xml)
    return Response(xml, mimetype="application/xml")

def normalize_blood(txt: str):
    if not txt:
        return None
    t = txt.upper().strip().replace(" ", "")
    if t in BLOODS:
        return t
    t2 = (txt.upper()
            .replace("POSITIVE", " POSITIVE")
            .replace("NEGATIVE", " NEGATIVE")
            .replace("+", " +")
            .replace("-", " -")
            .replace(" ", ""))
    if t2 in BLOODS:
        return t2
    t3 = (txt.upper().replace("-", " NEG").replace("+", " POS").replace("  ", " ").strip())
    t3 = re.sub(r"\s+", " ", t3)
    if t3 in BLOOD_SYNONYMS:
        return BLOOD_SYNONYMS[t3]
    t4 = re.sub(r"[^A-Z\+\-]", "", txt.upper())
    if t4 in BLOODS:
        return t4
    return None

def normalize_phone(value: str):
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 10:
        return digits[-10:]
    return digits if digits else None

def normalize_city(txt: str):
    if not txt:
        return None
    s = txt.strip()
    special = {"Bangalore": "Bengaluru", "Gurgaon": "Gurugram", "Trivandrum": "Thiruvananthapuram", "Prayagraj":"Prayagraj"}
    if s in special:
        return special[s]
    match = difflib.get_close_matches(s, INDIAN_CITIES, n=1, cutoff=0.75)
    return match[0] if match else s.title()

def merge_known(data: dict, newbits: dict):
    out = dict(data or {})
    for k, v in (newbits or {}).items():
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        out[k] = v.strip() if isinstance(v, str) else v
    return out

def need_next(role: str, data: dict):
    if role == "donor":
        if not data.get("full_name"): return "full_name"
        if not normalize_blood(data.get("blood_type")): return "blood_type"
        if not data.get("city"): return "city"
        return None
    if role == "request":
        if not data.get("full_name"): return "full_name"
        if not normalize_blood(data.get("blood_type")): return "blood_type"
        if not data.get("city"): return "city"
        return None
    return "role"

def prompt_for(field: str):
    prompts = {
        "role": "Please reply with 1 (Donor) or 2 (Require Blood).",
        "full_name": "📝 Please share your Full Name:",
        "blood_type": "🩸 Which Blood Group? (A+, A-, B+, B-, AB+, AB-, O+, O-)",
        "city": "🏙 Which City?",
    }
    return prompts.get(field, "Please provide the required detail.")

def ai_extract(user_msg: str, profile_name: str, session_state: dict, client, PREFERRED_MODEL):
    sys = (
        "You are Blood Help Bot. Extract structured data from a short WhatsApp message.\n"
        "Fix obvious typos (e.g., 'mumbaai' -> 'Mumbai', 'o pos' -> 'O+').\n"
        "Return STRICT JSON with keys: intent, full_name, blood_type, city.\n"
        "intent ∈ {donor, request, reset, other}.\n"
        "blood_type must be one of [A+,A-,B+,B-,AB+,AB-,O+,O-] if present; else null.\n"
        "If the user greets (hi/hello/start/menu/restart), use intent='reset'.\n"
        "Do not include any extra keys. No explanations."
    )
    state_hint = {
        "known": session_state.get("data", {}),
        "role": session_state.get("role"),
        "step": session_state.get("step"),
        "profile_name": profile_name,
    }
    messages = [
        {"role": "system", "content": sys},
        {"role": "user", "content": f"Message: {user_msg}\nState: {json.dumps(state_hint)}\nReturn JSON only."}
    ]
    tried = []
    last_err = None
    for model in [PREFERRED_MODEL, "gpt-4.1-mini"]:
        if model in tried:
            continue
        tried.append(model)
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=messages,
            )
            raw = resp.choices[0].message.content
            data = json.loads(raw)
            return data, model
        except Exception as e:
            last_err = e
            continue
    print("⚠ AI fallback error:", last_err)
    return (
        {"intent": "other", "full_name": None, "blood_type": None, "city": None},
        f"error:{last_err}"
    )

# -------------------------------------------------
# Database Queries
# -------------------------------------------------
def insert_donor(payload):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO donors (full_name, blood_type, phone, city) VALUES (%s, %s, %s, %s) RETURNING id;",
                (payload["full_name"], payload["blood_type"], payload["phone"], payload["city"])
            )
            donor_id = cur.fetchone()[0]
            conn.commit()
            return donor_id
    except Exception as e:
        print("❌ PostgreSQL donor insert error:", e)
        conn.rollback()
        return None
    finally:
        release_db_conn(conn)

def insert_recipient(payload):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO recipients (full_name, blood_type, phone, city) VALUES (%s, %s, %s, %s) RETURNING id;",
                (payload["full_name"], payload["blood_type"], payload["phone"], payload["city"])
            )
            recipient_id = cur.fetchone()[0]
            conn.commit()
            return recipient_id
    except Exception as e:
        print("❌ PostgreSQL recipient insert error:", e)
        conn.rollback()
        return None
    finally:
        release_db_conn(conn)

def search_donors(blood_type, city):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT full_name, phone, city FROM donors WHERE blood_type = %s AND city ILIKE %s LIMIT 10;",
                (blood_type, f"%{city}%")
            )
            rows = cur.fetchall()
            donors = [
                {"full_name": r[0], "phone": r[1], "city": r[2]}
                for r in rows
            ]
            return donors
    except Exception as e:
        print("❌ PostgreSQL donor search error:", e)
        return []
    finally:
        release_db_conn(conn)

# -------------------------------------------------
# Routes
# -------------------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    body = (request.values.get("Body") or "").strip()
    from_number = request.values.get("From", "")
    profile_name = request.values.get("ProfileName") or "Friend"

    print(f"📩 From {from_number} ({profile_name}): {body}")

    session = sessions.get(from_number) or {"role": None, "step": "start", "data": {}}

    # --- Reset / Start ---
    if body.lower() in {"hi","hello","start","menu","restart"} or session.get("step") == "start":
        sessions[from_number] = {"role": None, "step": "choose_role", "data": {}}
        return twiml_reply(
            "👋 Hi, how may I help you?\n\n"
            "Please classify yourself:\n"
            "1️⃣ Donor\n"
            "2️⃣ Require Blood (Recipient Request)\n\n"
            "👉 Reply with 1 or 2 to continue."
        )

    # --- Choose role (supports numbers or words) ---
    if session.get("step") == "choose_role":
        b = body.strip().lower()
        if b in {"1", "donor"}:
            session["role"] = "donor"
            session["step"] = "collect"
            sessions[from_number] = session
            return twiml_reply("✅ Great! Registering you as a Donor.\nYou can reply naturally (e.g., 'A+ in Pune, my name is Ravi').")
        elif b in {"2", "request", "recipient"}:
            session["role"] = "request"
            session["step"] = "collect"
            sessions[from_number] = session
            return twiml_reply("🆘 Okay! Making a Blood Request.\nYou can reply naturally (e.g., 'Need AB- in Hyderabad').")
        else:
            return twiml_reply("⚠ Invalid choice.\nReply 1 for Donor or 2 for Request.")

    # --- Let AI parse the message, then fill missing fields ---
    ai, used_model = ai_extract(body, profile_name, session, client, PREFERRED_MODEL)
    print("🤖 AI model:", used_model)
    print("🤖 AI JSON:", ai)

    intent = (ai.get("intent") or "").lower()
    if intent == "reset":
        sessions[from_number] = {"role": None, "step": "choose_role", "data": {}}
        return twiml_reply(
            "🔄 Reset.\n"
            "1️⃣ Donor\n"
            "2️⃣ Require Blood\n\n"
            "👉 Reply with 1 or 2."
        )
    if intent in {"donor","request"} and not session.get("role"):
        session["role"] = "donor" if intent == "donor" else "request"

    data = merge_known(session.get("data", {}), {
        "full_name": ai.get("full_name"),
        "blood_type": ai.get("blood_type"),
        "city": ai.get("city"),
    })

    if data.get("blood_type"):
        bt = normalize_blood(data["blood_type"])
        if bt: data["blood_type"] = bt
        else:  data["blood_type"] = None

    if data.get("city"):
        data["city"] = normalize_city(data["city"])

    session["data"] = data
    session["step"] = "collect"

    if not session.get("role"):
        sessions[from_number] = session
        return twiml_reply(prompt_for("role"))

    missing = need_next(session["role"], data)
    if missing:
        # If missing full_name, use WhatsApp profile name automatically
        if missing == "full_name" and not data.get("full_name") and profile_name:
            data["full_name"] = profile_name
            session["data"] = data
            missing = need_next(session["role"], data)
            if not missing:
                pass  # Continue to next logic below
            else:
                sessions[from_number] = session
                return twiml_reply(prompt_for(missing))
        else:
            sessions[from_number] = session
            return twiml_reply(prompt_for(missing))

    # --- All fields present → act ---
    if session["role"] == "donor":
        payload = {
            "full_name": data["full_name"],
            "blood_type": data["blood_type"],
            "phone": normalize_phone(from_number),
            "city": data["city"],
        }
        print("🗄 Insert donor:", payload)
        donor_id = insert_donor(payload)
        if donor_id:
            msg = (
                "✅ Thanks! You’re registered as a donor.\n"
                f"Name: {payload['full_name']}\n"
                f"Group: {payload['blood_type']}\n"
                f"Phone: {payload['phone']}\n"
                f"City:  {payload['city']}"
            )
        else:
            msg = "⚠ Saved your info locally but DB insert failed. Please try again later."
        sessions.pop(from_number, None)
        return twiml_reply(msg)

    if session["role"] == "request":
        recipient_payload = {
            "full_name": data.get("full_name") or profile_name,
            "blood_type": data["blood_type"],
            "phone": normalize_phone(from_number),
            "city": data["city"],
        }
        print("🗄 Insert recipient:", recipient_payload)
        recipient_id = insert_recipient(recipient_payload)

        donors = search_donors(recipient_payload["blood_type"], recipient_payload["city"])
        print(f"🔎 Found donors: {len(donors)}")

        if donors:
            lines = [f"✅ Donors for {recipient_payload['blood_type']} in {recipient_payload['city']}:", ""]
            for i, d in enumerate(donors, 1):
                lines.append(f"{i}. {d['full_name']} — {normalize_phone(d['phone'])} ({d['city']})")
            lines.append("\n📞 Please contact donors directly.")
            reply = "\n".join(lines)
        else:
            reply = (
                f"❌ No donors found for {recipient_payload['blood_type']} in {recipient_payload['city']}.\n"
                "We’ll notify you if someone becomes available. Meanwhile you can place an emergency request here --> https://thala-connect-ai-28.lovable.app/"
            )

        sessions.pop(from_number, None)
        return twiml_reply(reply)

    sessions[from_number] = session
    return twiml_reply("I didn’t catch that. Reply 1 for Donor or 2 for Require Blood.")
