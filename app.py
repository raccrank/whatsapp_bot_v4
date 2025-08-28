# python_code.py

import os
import redis
import requests
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

# --- Configuration ---
# You will need to set these environment variables in your deployment environment.
# They are not hardcoded to ensure security.
ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER')
SELLERS_NUMBER = os.environ.get('SELLERS_NUMBER')
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6377')

# Initialize Flask app and Redis connection
app = Flask(__name__)
db = redis.from_url(REDIS_URL, decode_responses=True)

# --- Utility Functions ---

def send_whatsapp_message(to_number, message_body):
    """
    Sends a message using the Twilio API.
    This function acts as the bridge between your bot and Twilio.
    """
    try:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Messages.json"
        data = {
            'To': to_number,
            'From': PHONE_NUMBER,
            'Body': message_body,
        }
        response = requests.post(url, auth=(ACCOUNT_SID, AUTH_TOKEN), data=data)
        response.raise_for_status() # Raise an error for bad status codes
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error sending message to {to_number}: {e}")
        return None

def store_message_history(sender_number, message_body):
    """
    Stores the full conversation history in Redis.
    This is key to providing context to the seller during handoff.
    Messages are stored in a list for the given user's number.
    """
    # Create a unique key for the chat session
    session_key = f"chat_history:{sender_number}"
    
    # Push the new message to the end of the list
    db.rpush(session_key, message_body)
    
    # Trim the list to a reasonable size to prevent it from growing indefinitely
    db.ltrim(session_key, -50, -1) # Keep the last 50 messages

def get_full_chat_history(user_number):
    """
    Retrieves the entire chat history for a given user from Redis.
    This is what the seller will see to get full context.
    """
    session_key = f"chat_history:{user_number}"
    history_list = db.lrange(session_key, 0, -1)
    
    # Format the history into a readable string
    formatted_history = "\n".join(history_list)
    return formatted_history

def set_seller_active_chat(seller_number, buyer_number):
    """
    Explicitly links the seller's number to the buyer's number.
    This creates a session for the handoff.
    """
    db.set(f"seller_chat:{seller_number}", buyer_number)

def get_seller_active_chat(seller_number):
    """
    Retrieves the buyer's number the seller is currently talking to.
    """
    return db.get(f"seller_chat:{seller_number}")

# --- Bot Logic ---

def _handle_bot_incoming(from_number, message_body):
    """
    Handles messages from the buyer.
    This function decides whether to reply with a bot response or trigger a handoff.
    """
    resp = MessagingResponse()
    
    # Store the buyer's message in the history
    store_message_history(from_number, f"Buyer: {message_body}")

    # Your bot logic goes here. For this example, we'll use a simple keyword trigger.
    if "help" in message_body.lower():
        # Trigger the handoff
        
        # 1. Notify the buyer that a human is on the way
        bot_response_to_buyer = "One moment please, an agent will be with you shortly. I've passed along the full conversation to them."
        resp.message(bot_response_to_buyer)
        
        # 2. Set the active chat for the seller
        set_seller_active_chat(SELLERS_NUMBER, from_number)
        
        # 3. Retrieve the full history to send to the seller
        history_summary = get_full_chat_history(from_number)
        message_for_seller = (
            f"**Handoff Alert:** A customer requires human assistance. "
            f"You are now connected to the buyer. Simply reply to this message "
            f"to respond directly to the customer.\n\n"
            f"--- **Conversation History** ---\n"
            f"{history_summary}"
        )
        
        # 4. Notify the seller of the handoff with the full context
        send_whatsapp_message(SELLERS_NUMBER, message_for_seller)
        
    else:
        # Default bot response
        bot_response = f"I'm a bot! I received your message: '{message_body}'. "
        bot_response += "If you need human assistance, just reply with 'help'."
        resp.message(bot_response)
        
    return str(resp)

def _handle_seller_incoming(from_number, message_body):
    """
    Handles messages from the seller. This is the new, seamless relay logic.
    It checks for an active chat and forwards the message to the correct buyer.
    No special commands are needed.
    """
    # Find the active buyer's number for this seller
    buyer_number = get_seller_active_chat(from_number)

    # Check if there is an active chat session
    if buyer_number:
        # A session exists, so this is a direct reply to the buyer
        
        # Store the seller's message in the chat history
        store_message_history(buyer_number, f"Seller: {message_body}")
        
        # Forward the message directly to the buyer
        send_whatsapp_message(buyer_number, message_body)
        
        # Respond to the seller to confirm the message was sent
        resp = MessagingResponse()
        resp.message("Your message was sent to the buyer.")
        return str(resp)
    else:
        # No active chat found, send a message to the seller
        resp = MessagingResponse()
        resp.message("You do not have an active chat session with a buyer. You can respond to a handoff message to begin a session.")
        return str(resp)

# --- Flask Routes ---

@app.route('/whatsapp', methods=['POST'])
def webhook():
    """
    Main webhook endpoint to handle all incoming messages from Twilio.
    It routes the message to the correct handler based on the sender.
    """
    from_number = request.form.get('From')
    message_body = request.form.get('Body')
    
    if from_number == SELLERS_NUMBER:
        # Message is from the seller, handle it with the seller logic
        return _handle_seller_incoming(from_number, message_body)
    else:
        # Message is from a buyer (an unknown number), handle with bot logic
        return _handle_bot_incoming(from_number, message_body)

if __name__ == '__main__':
    app.run(port=5000, debug=True)
