import os
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import json
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# Store sessions (in-memory for now, use Redis/DB in production)
sessions = {}

# Twilio client
account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
twilio_client = Client(account_sid, auth_token)
AGENT_NUMBER = os.getenv("AGENT_NUMBER")  # your live agent number

def set_session(user_number, session_data):
    sessions[user_number] = session_data

def get_session(user_number):
    return sessions.get(user_number, {"state": "bot"})

def pop_session(user_number):
    if user_number in sessions:
        del sessions[user_number]

@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    incoming_msg = request.form.get("Body", "").strip().lower()
    from_number = request.form.get("From")
    user_session = get_session(from_number)
    resp = MessagingResponse()

    # 1. If user is in handoff mode
    if user_session["state"] == "handoff":
        if incoming_msg == "#end":
            resp.message("✅ Handoff ended. You're now chatting with the bot again.")
            pop_session(from_number)
        else:
            # Forward customer message to agent
            forward_to_agent(from_number, incoming_msg)
        return str(resp)

    # 2. If agent responds
    if from_number == f"whatsapp:{AGENT_NUMBER}":
        # Format: #reply + customer_number + message
        if incoming_msg.startswith("#reply"):
            try:
                _, customer_number, *reply_message = incoming_msg.split()
                reply_message = " ".join(reply_message)
                send_to_customer(customer_number, reply_message)
                resp.message(f"✅ Reply sent to {customer_number}")
            except Exception as e:
                resp.message("⚠️ Format: #reply <customer_number> <message>")
        elif incoming_msg == "#end":
            # End session (agent manually ends)
            resp.message("✅ Session ended. Customer back to bot.")
            # Find and remove customer session
            for k, v in list(sessions.items()):
                if v.get("state") == "handoff":
                    pop_session(k)
        else:
            resp.message("⚠️ Use #reply or #end while in agent mode.")
        return str(resp)

    # 3. Normal customer flow
    if incoming_msg == "#help":
        resp.message("🔔 You’re now connected to a live agent. Please wait...")
        user_session["state"] = "handoff"
        set_session(from_number, user_session)
        notify_agent(from_number)
    else:
        resp.message(f"🤖 Bot reply: You said '{incoming_msg}'")

    return str(resp)

# ---- Helper Functions ----

def forward_to_agent(customer_number, msg):
    """Forward customer messages to agent"""
    twilio_client.messages.create(
        from_="whatsapp:+14155238886",  # Twilio sandbox number
        to=f"whatsapp:{AGENT_NUMBER}",
        body=f"📨 From {customer_number}: {msg}"
    )

def send_to_customer(customer_number, msg):
    """Send agent messages back to customer"""
    twilio_client.messages.create(
        from_="whatsapp:+14155238886",
        to=customer_number,
        body=f"👨‍💼 Agent: {msg}"
    )

def notify_agent(customer_number):
    """Notify agent of new handoff"""
    twilio_client.messages.create(
        from_="whatsapp:+14155238886",
        to=f"whatsapp:{AGENT_NUMBER}",
        body=f"🔔 Customer {customer_number} requested help. Use #reply {customer_number} <message> to respond, #end to finish."
    )

if __name__ == "__main__":
    app.run(port=5000, debug=True)
