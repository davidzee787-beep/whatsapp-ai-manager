from flask import Flask, request

app = Flask(__name__)

@app.route("/")
def home():
    return "Flask is working!", 200

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        challenge = request.args.get("hub.challenge", "")
        token = request.args.get("hub.verify_token", "")
        print(f"Token received: '{token}'")
        return challenge, 200
    return "ok", 200

if __name__ == "__main__":
    print("Test server running on port 5000")
    app.run(port=5000, debug=False)
