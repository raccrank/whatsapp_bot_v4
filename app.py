import os
import json
import logging
import uuid
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from dotenv import load_dotenv
import redis

# -------- Configuration & Logging --------
load_dotenv("cred.env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("whatsapp-bot")

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
    raise RuntimeError("Missing Twilio credentials: set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in env.")

# Twilio messaging number (sandbox or business)
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")

# Seller and Supervisor (you) numbers — set these in cred.env or your environment
SELLER_NUMBER = os.environ.get("SELLER_NUMBER", "whatsapp:+2547XXXXXXXX")
SUPERVISOR_NUMBER = os.environ.get("SUPERVISOR_NUMBER", "")  # your number to be notified when seller asks for help

# Redis setup
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
redis_client = redis.StrictRedis.from_url(REDIS_URL, decode_responses=True)

# Twilio client
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# -------- Business data --------
PRODUCT_OPTIONS = {
    1: {"name": "aliengo kingsize black", "price": 150},
    2: {"name": "korobo 1 1/4\" blue", "price": 100},
    3: {"name": "wetop 1 1/4\" brown", "price": 100},
    4: {"name": "box with 50 booklets", "price": 2300},
}
DELIVERY_CHARGE = 200
POCHI_DETAILS = "Pochi la Biashara 0743706598"

# Redis keys helpers
def session_key(number):
    return f"session:{number}"

def orders_list_key():
    return "orders:recent"

# Session helpers (persistent)
def get_session(key):
    session_data = redis_client.get(session_key(key))
    return json.loads(session_data) if session_data else {"state": "initial"}

def set_session(key, value):
    redis_client.set(session_key(key), json.dumps(value))

def pop_session(key):
    redis_client.delete(session_key(key))

# Save order in Redis list (recent)
def save_order(order):
    redis_client.lpush(orders_list_key(), json.dumps(order))
    redis_client.ltrim(orders_list_key(), 0, 999)  # keep last 1000

# Utility: find buyer(s) which are in handoff with seller
def list_handoff_buyers_for_seller(seller_number=SELLER_NUMBER):
    buyers = []
    for k in redis_client.scan_iter("session:*"):
        sess = redis_client.get(k)
        if not sess:
            continue
        try:
            s = json.loads(sess)
        except Exception:
            continue
        if s.get("state") in ("handoff_seller", "handoff_supervisor") and s.get("linked_seller") == seller_number:
            buyers.append(k.replace("session:", ""))
    return buyers

# -------- Flask app --------
app = Flask(__name__)

# Keywords
BUYER_HANDOFF_KEYWORDS = ["agent", "human", "seller", "support"]
SELLER_HELP_KEYWORDS = ["#help", "#supervisor", "#assist"]  # seller types this to call you
SELLER_COMMANDS = ["#bot", "#end", "#list", "#switch", "#help", "#supervisor"]

# Helpers for product text
def product_menu_text():
    s = "Hey there! 🌿 Welcome to our rolling paper shop!\n\nHere's what we have:\n"
    for num, info in PRODUCT_OPTIONS.items():
        s += f"{num}. {info['name'].title()}: Ksh {info['price']}\n"
    s += "\nReply with the product number to order. Type 'help' to talk to a person or 'start' to restart."
    return s

def get_product_by_choice(choice: str):
    c = choice.strip().lower()
    if c.isdigit():
        pid = int(c)
        return PRODUCT_OPTIONS.get(pid)
    # exact name match
    for p in PRODUCT_OPTIONS.values():
        if p["name"].lower() == c:
            return p
    return None

# -------- Webhook (single endpoint for Twilio) --------
@app.route("/whatsapp", methods=["POST"])
def webhook():
    incoming_raw = request.values.get("Body", "") or ""
    incoming_msg = incoming_raw.strip()
    from_number = request.values.get("From")
    resp = MessagingResponse()

    logger.info("Incoming from %s: %s", from_number, incoming_msg)

    # If message is from seller, handle seller actions (relay to buyer or escalate)
    if from_number == SELLER_NUMBER:
        return _handle_seller_incoming(incoming_msg, resp)

    # Otherwise: buyer message
    return _handle_buyer_incoming(from_number, incoming_msg, resp)

# -------- Buyer handling (state machine + buyer->seller handoff) --------
def _handle_buyer_incoming(buyer_number, incoming_msg, resp: MessagingResponse):
    msg_lower = incoming_msg.lower()
    session = get_session(buyer_number)

    # Global shortcuts
    if msg_lower in ("menu", "catalog", "products"):
        resp.message(product_menu_text())
        if session.get("state") == "initial":
            session["state"] = "awaiting_product"
            set_session(buyer_number, session)
        return str(resp)

    # Restart
    if msg_lower == "start":
        session = {"state": "initial"}
        set_session(buyer_number, session)

    # Buyer requests human/seller anytime
    if any(k in msg_lower for k in BUYER_HANDOFF_KEYWORDS):
        logger.info("Buyer %s requests handoff to seller.", buyer_number)
        # mark session for seller handoff
        session["state"] = "handoff_seller"
        session["linked_seller"] = SELLER_NUMBER
        set_session(buyer_number, session)

        # Build summary for seller
        # include whatever data we have in session
        summary = (
            f"🚨 New buyer handoff: {buyer_number}\n"
            f"Last message: {incoming_msg}\n\n"
            f"Session data:\n{json.dumps(session.get('data', {}), indent=2)}\n\n"
            "Reply to this message to talk to the buyer. Commands for seller: #bot (return buyer to bot), #end (close chat), #list, #switch <whatsapp:+...>, #help"
        )
        client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, to=SELLER_NUMBER, body=summary)
        resp.message("Connecting you to the seller now. Please wait...")
        return str(resp)

    # Normal bot states
    state = session.get("state", "initial")

    if state == "initial":
        resp.message(product_menu_text())
        session["state"] = "awaiting_product"
        set_session(buyer_number, session)
        return str(resp)

    if state == "awaiting_product":
        product = get_product_by_choice(incoming_msg)
        if not product:
            resp.message("I didn't catch that product. Reply with a product number or 'menu' to see the list.")
            return str(resp)
        # store selection
        session.setdefault("data", {})
        session["data"]["product_id"] = next(k for k,v in PRODUCT_OPTIONS.items() if v==product)
        session["data"]["product_name"] = product["name"]
        session["data"]["price"] = product["price"]
        session["state"] = "awaiting_quantity"
        set_session(buyer_number, session)
        resp.message(f"How many units of *{product['name'].title()}* do you want? (reply with a number)")
        return str(resp)

    if state == "awaiting_quantity":
        if incoming_msg.isdigit():
            qty = int(incoming_msg)
            if qty <= 0:
                resp.message("Please enter a quantity greater than zero.")
                return str(resp)
            session["data"]["quantity"] = qty
            session["data"]["total"] = session["data"]["price"] * qty + DELIVERY_CHARGE
            session["state"] = "awaiting_location"
            set_session(buyer_number, session)
            resp.message("Thanks. Please share your delivery location (street/estate/pin).")
            return str(resp)
        else:
            resp.message("Please enter a number for quantity (e.g., 2).")
            return str(resp)

    if state == "awaiting_location":
        # accept any string for location
        session["data"]["location"] = incoming_msg
        order = {
            "order_id": uuid.uuid4().hex,
            "buyer": buyer_number,
            "product_name": session["data"].get("product_name"),
            "quantity": session["data"].get("quantity"),
            "price": session["data"].get("price"),
            "delivery_charge": DELIVERY_CHARGE,
            "total": session["data"].get("total"),
            "location": session["data"].get("location"),
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        # save soft order log
        save_order(order)

        # Confirm to buyer and handoff to seller automatically for order follow up
        resp.message(
            f"✅ Order recorded:\n{order['product_name']} x {order['quantity']}\n"
            f"Total: Ksh {order['total']}\nLocation: {order['location']}\n\nConnecting you to the seller for confirmation..."
        )
        session["state"] = "handoff_seller"
        session["linked_seller"] = SELLER_NUMBER
        session["data"]["last_order_id"] = order["order_id"]
        set_session(buyer_number, session)

        # Notify seller with order summary
        summary = (
            f"🆕 New order / handoff from {buyer_number}:\n"
            f"• Product: {order['product_name']}\n"
            f"• Qty: {order['quantity']}\n"
            f"• Location: {order['location']}\n"
            f"• Total: Ksh {order['total']}\n\n"
            "Reply here to chat with buyer. Seller commands: #bot, #end, #list, #switch <buyer>, #help, #supervisor"
        )
        client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, to=SELLER_NUMBER, body=summary)
        return str(resp)

    if state in ("handoff_seller", "handoff_supervisor"):
        # Relay buyer message to seller (if handoff_seller) OR to supervisor (if handoff_supervisor)
        if state == "handoff_seller":
            # send to the seller number
            client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, to=SELLER_NUMBER, body=f"{buyer_number} says: {incoming_msg}")
            resp.message("Message sent to seller.")
            return str(resp)
        else:
            # handoff_supervisor: forward to supervisor (you)
            if SUPERVISOR_NUMBER:
                client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, to=SUPERVISOR_NUMBER, body=f"{buyer_number} says: {incoming_msg}")
                resp.message("Message sent to supervisor.")
                return str(resp)
            else:
                resp.message("Supervisor not configured.")
                return str(resp)

    # Fallback
    resp.message("Type 'menu' to see products or 'help' to contact someone.")
    set_session(buyer_number, session)
    return str(resp)

# -------- Seller handling (seller->buyer relay, seller->supervisor handoff) --------
def _handle_seller_incoming(incoming_msg, resp: MessagingResponse):
    msg = incoming_msg.strip()
    lower = msg.lower()

    # Seller asking for help from supervisor
    if any(k in lower for k in SELLER_HELP_KEYWORDS):
        buyers = list_handoff_buyers_for_seller(SELLER_NUMBER)
        if not buyers:
            resp.message("No active buyer conversation to escalate.")
            return str(resp)
        for b in buyers:
            sess = get_session(b)
            sess["state"] = "handoff_supervisor"
            sess["linked_seller"] = SELLER_NUMBER
            set_session(b, sess)
            if SUPERVISOR_NUMBER:
                client.messages.create(
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=SUPERVISOR_NUMBER,
                    body=f"⚠️ Seller requested supervisor for chat with {b}.\nSeller note: {incoming_msg}"
                )
        resp.message("Supervisor notified. They’ll join shortly.")
        return str(resp)

    # --- Option A: reply system ---
    if lower.startswith("#reply"):
        parts = msg.split()
        if len(parts) < 3:
            resp.message("Usage: #reply whatsapp:+2547... your message here")
            return str(resp)
        buyer = parts[1]
        reply_text = " ".join(parts[2:])
        s = get_session(buyer)
        if s.get("state") not in ("handoff_seller", "handoff_supervisor"):
            resp.message(f"{buyer} is not in active handoff.")
            return str(resp)
        client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=buyer,
            body=f"👨‍💼 Seller: {reply_text}"
        )
        resp.message(f"✅ Sent reply to {buyer}")
        return str(resp)

    if lower.startswith("#end"):
        parts = msg.split()
        if len(parts) != 2:
            resp.message("Usage: #end whatsapp:+2547...")
            return str(resp)
        buyer = parts[1]
        s = get_session(buyer)
        if not s or s.get("state") not in ("handoff_seller", "handoff_supervisor"):
            resp.message(f"{buyer} is not in active handoff.")
            return str(resp)
        client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=buyer,
            body="✅ Chat closed by seller. You're back with the bot. Type 'menu' to continue."
        )
        s["state"] = "awaiting_product"
        s.pop("linked_seller", None)
        set_session(buyer, s)
        resp.message(f"Closed chat with {buyer}.")
        return str(resp)

    if lower == "#list":
        buyers = list_handoff_buyers_for_seller(SELLER_NUMBER)
        if not buyers:
            resp.message("No active buyers in handoff.")
            return str(resp)
        resp.message("Active buyers:\n" + "\n".join(buyers))
        return str(resp)

    if lower == "#help":
        resp.message("Seller commands:\n#list\n#reply <buyer> <message>\n#end <buyer>\n#supervisor")
        return str(resp)

    # Fallback: unrecognized seller command
    resp.message("⚠️ Unknown command. Use #help to see options.")
    return str(resp)


    # Otherwise treat seller message as reply to buyer.
    # Determine which buyer is the active one — pick the most recent in handoff (simple approach)
    buyers = list_handoff_buyers_for_seller(SELLER_NUMBER)
    if not buyers:
        resp.message("No active buyers to send this message to. Use #list to see active buyers.")
        return str(resp)

    # Default: choose the first in list (leftmost)
    target_buyer = buyers[0]
    # Relay message to buyer
    client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, to=target_buyer, body=f"Seller: {incoming_msg}")
    resp.message(f"Sent to {target_buyer}.")
    return str(resp)

# -------- Supervisor handling endpoint (you can also reply via Twilio number directly) --------
# Supervisor messages will come into the same /whatsapp endpoint if you use the same Twilio number.
# The supervisor replies are treated as a seller reply for routing simplicity (since SUPERVISOR_NUMBER may be different).
@app.route("/supervisor_reply", methods=["POST"])
def supervisor_reply():
    """
    Optional endpoint — use this if you want to send a supervisor message into a conversation
    from an external system (e.g., console). JSON: { "buyer": "whatsapp:+2547...", "message": "..." }
    """
    data = request.get_json() or {}
    buyer = data.get("buyer")
    message = data.get("message")
    if not buyer or not message:
        return jsonify({"error": "missing buyer or message"}), 400
    # mark buyer session as resumed with supervisor if needed
    s = get_session(buyer)
    s["state"] = "handoff_supervisor"
    s["linked_seller"] = SELLER_NUMBER
    set_session(buyer, s)
    client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, to=buyer, body=f"Supervisor: {message}")
    return jsonify({"status": "ok"}), 200

# -------- Orders endpoint (simple) --------
@app.route("/orders", methods=["GET"])
def orders():
    """
    Returns recent orders saved in Redis. This is useful for seller/review.
    """
    raw = redis_client.lrange(orders_list_key(), 0, 99)
    orders = []
    for r in raw:
        try:
            orders.append(json.loads(r))
        except Exception:
            continue
    return jsonify({"count": len(orders), "orders": orders}), 200

# -------- Run server --------
if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "False").lower() == "true"
    if debug_mode:
        logger.warning("⚠️ Running Flask in debug mode.")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=debug_mode)
