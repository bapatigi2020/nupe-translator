from flask import Flask, render_template, request

app = Flask(__name__)

# Dictionary for rule-based translation
english_to_nupe = {
    "how are you": "ke we wo na o",
    "thank you": "kubetunyi",
    "come here": "be ba bo",
    "i am fine": "mi gan la",
    "what is your name": "eza emi la",
    "good morning": "ku be lazhin",
    "good night": "ina zhi",
    "please": "dami",
    "yes": "eh",
    "no": "ayi",
	"person": "eza"
}

@app.route("/", methods=["GET", "POST"])
def index():
    translation = ""
    if request.method == "POST":
        english_input = request.form["english_text"].lower().strip("?!. ")
        translation = english_to_nupe.get(english_input, "Translation not found")
    return render_template("index.html", translation=translation)

if __name__ == "__main__":
    app.run(debug=True)
