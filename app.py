# python_code.py

import os
import json
import logging
import uuid
import requests
from datetime import datetime, timezone
from flask import Flask, request
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
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6377")
redis_client = redis.StrictRedis.from_url(REDIS_URL, decode_responses=True)

# Twilio client
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# --- Business data ---
PRODUCT_OPTIONS = {
    1: {"name": "aliengo kingsize black", "price": 150},
    2: {"name": "korobo 1 1/4\" blue", "price": 100},
    3: {"name": "wetop 1 1/4\" brown", "price": 100},
    4: {"name": "box with 50 booklets", "price": 2300},
}
DELIVERY_CHARGE = 200
POCHI_DETAILS = "Pochi la Biashara 0743706598"

# --- Redis keys and helpers ---
def session_key(number):
    """Generates a key for a user's session state."""
    return f"session:{number}"

def chat_history_key(number):
    """Generates a key for a user's chat history."""
    return f"chat_history:{number}"

def seller_active_chat_key(seller_number):
    """Generates a key for the seller's active chat session."""
    return f"seller_active_chat:{seller_number}"

def get_session(key):
    """Retrieves and deserializes a user's session from Redis."""
    session_data = redis_client.get(session_key(key))
    return json.loads(session_data) if session_data else {"state": "initial"}

def set_session(key, value):
    """Serializes and stores a user's session in Redis."""
    redis_client.set(session_key(key), json.dumps(value))

def store_message_history(sender_number, message_body):
    """Stores a message in the conversation history list in Redis."""
    redis_client.rpush(chat_history_key(sender_number), message_body)
    redis_client.ltrim(chat_history_key(sender_number), -50, -1) # Keep last 50 messages

def get_full_chat_history(user_number):
    """Retrieves and formats the full chat history for a user."""
    history_list = redis_client.lrange(chat_history_key(user_number), 0, -1)
    return "\n".join(history_list)

def get_seller_active_chat(seller_number):
    """Retrieves the buyer's number the seller is currently talking to."""
    return redis_client.get(seller_active_chat_key(seller_number))

def set_seller_active_chat(seller_number, buyer_number):
    """Explicitly links the seller's number to the buyer's number."""
    redis_client.set(seller_active_chat_key(seller_number), buyer_number)

def pop_seller_active_chat(seller_number):
    """Removes the seller's active chat session."""
    redis_client.delete(seller_active_chat_key(seller_number))

# --- Flask app and webhook ---
app = Flask(__name__)

@app.route("/whatsapp", methods=["POST"])
def webhook():
    """
    Main webhook endpoint to handle all incoming messages from Twilio.
    It routes the message to the correct handler based on the sender.
    """
    incoming_msg = request.values.get("Body", "").strip()
    from_number = request.values.get("From")

    logger.info("Incoming from %s: %s", from_number, incoming_msg)

    # If message is from seller, handle seller actions
    if from_number == SELLER_NUMBER:
        return _handle_seller_incoming(from_number, incoming_msg)

    # Otherwise: buyer message
    return _handle_buyer_incoming(from_number, incoming_msg)

# --- Buyer handling (state machine + handoff) ---
def product_menu_text():
    """Generates the text for the product menu."""
    s = "Hey there! 🌿 Welcome to our rolling paper shop!\n\nHere's what we have:\n"
    for num, info in PRODUCT_OPTIONS.items():
        s += f"{num}. {info['name'].title()}: Ksh {info['price']}\n"
    s += "\nReply with the product number to order. Type 'help' to talk to a person."
    return s

def get_product_by_choice(choice: str):
    """Finds a product based on a user's input."""
    if choice.isdigit():
        pid = int(choice)
        return PRODUCT_OPTIONS.get(pid)
    # Exact name match
    for p in PRODUCT_OPTIONS.values():
        if p["name"].lower() == choice.lower():
            return p
    return None

def _handle_buyer_incoming(buyer_number, incoming_msg):
    """Handles messages from the buyer based on conversation state."""
    resp = MessagingResponse()
    msg_lower = incoming_msg.lower()
    session = get_session(buyer_number)

    # Store the buyer's message in history
    store_message_history(buyer_number, f"Buyer: {incoming_msg}")

    # Check for handoff
    if "help" in msg_lower and session.get("state") not in ("handoff_seller", "handoff_supervisor"):
        # Trigger the seamless handoff
        session["state"] = "handoff_seller"
        session["linked_seller"] = SELLER_NUMBER
        set_session(buyer_number, session)
        set_seller_active_chat(SELLER_NUMBER, buyer_number)

        history_summary = get_full_chat_history(buyer_number)
        message_for_seller = (
            f"**Handoff Alert:** A customer requires human assistance. "
            f"You are now connected to the buyer. Simply reply to this message "
            f"to respond directly to the customer.\n\n"
            f"--- **Conversation History** ---\n"
            f"{history_summary}"
        )
        client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, to=SELLER_NUMBER, body=message_for_seller)
        
        resp.message("I've connected you with the seller. They will respond shortly.")
        return str(resp)

    # Handle a chat already in handoff
    if session.get("state") in ("handoff_seller", "handoff_supervisor"):
        # Relay buyer message to the bot's chat with the seller
        client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, to=SELLER_NUMBER, body=f"{buyer_number} says: {incoming_msg}")
        resp.message("Message sent to seller.")
        return str(resp)

    # The original bot conversation flow is now here
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
        session.setdefault("data", {})
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
        
        # Notify buyer of order confirmation
        resp.message(
            f"✅ Order recorded:\n{order['product_name']} x {order['quantity']}\n"
            f"Total: Ksh {order['total']}\nLocation: {order['location']}\n\nI've connected you with the seller, they will respond shortly to confirm your order details."
        )

        # Trigger seamless handoff
        session["state"] = "handoff_seller"
        session["linked_seller"] = SELLER_NUMBER
        set_session(buyer_number, session)
        set_seller_active_chat(SELLER_NUMBER, buyer_number)
        
        # Send full chat history and order summary to seller
        history_summary = get_full_chat_history(buyer_number)
        message_for_seller = (
            f"🆕 **New Order & Handoff Alert** from {buyer_number}!\n\n"
            f"--- **Order Details** ---\n"
            f"• Product: {order['product_name']}\n"
            f"• Qty: {order['quantity']}\n"
            f"• Location: {order['location']}\n"
            f"• Total: Ksh {order['total']}\n\n"
            f"--- **Conversation History** ---\n"
            f"{history_summary}\n\n"
            "You are now connected to the buyer. Simply reply to this message to continue the conversation."
        )
        client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, to=SELLER_NUMBER, body=message_for_seller)

        return str(resp)

    # Fallback response for unknown state or message
    resp.message("I'm not sure what to do with that. Type 'menu' to see products or 'help' to contact a person.")
    return str(resp)


# --- Seller handling (seamless relay + commands) ---
def _handle_seller_incoming(seller_number, incoming_msg):
    """Handles messages from the seller, including commands and seamless relay."""
    resp = MessagingResponse()
    msg_lower = incoming_msg.lower()

    # Commands for the seller to manage conversations
    if msg_lower == "#end":
        buyer_number = get_seller_active_chat(seller_number)
        if not buyer_number:
            resp.message("No active chat to end.")
            return str(resp)
        
        # Notify buyer chat is closed
        client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=buyer_number,
            body="✅ Chat closed by seller. You're back with the bot. Type 'menu' to continue."
        )
        
        # Reset buyer session
        session = get_session(buyer_number)
        session["state"] = "awaiting_product"
        session.pop("linked_seller", None)
        set_session(buyer_number, session)
        
        # Clear seller's active chat
        pop_seller_active_chat(seller_number)
        
        resp.message(f"Closed chat with {buyer_number}.")
        return str(resp)
    
    # Check for other commands if you decide to add them back in the future
    # elif msg_lower == "#help":
    #     resp.message("Seller commands: #end")
    #     return str(resp)

    # Seamless relay logic: Assume any message that isn't a command is a reply
    buyer_number = get_seller_active_chat(seller_number)
    if not buyer_number:
        resp.message("You do not have an active chat session with a buyer. You can respond to a handoff message to begin a session.")
        return str(resp)
    
    # Store seller's message in the chat history
    store_message_history(buyer_number, f"Seller: {incoming_msg}")
    
    # Forward message directly to the buyer
    client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, to=buyer_number, body=f"👨‍💼 Seller: {incoming_msg}")
    
    resp.message("Your message was sent to the buyer.")
    return str(resp)


if __name__ == '__main__':
    app.run(port=5000, debug=True)
