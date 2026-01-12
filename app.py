from flask import Flask, render_template, request, jsonify
from newspaper import Article
import nltk

# Download required nltk data (run once)
nltk.download('punkt')

app = Flask(__name__)

# --- Route 1: Serve the UI ---
@app.route('/')
def home():
    return render_template('index.html')

# --- Route 2: The Extraction Engine (Sprint 1) ---
@app.route('/extract', methods=['POST'])
def extract_news():
    data = request.get_json()
    url = data.get('url')

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        # Use Newspaper3k to scrape
        article = Article(url)
        article.download()
        article.parse()
        
        # We also need to call .nlp() to get keywords/summary basics if needed later
        # article.nlp() 

        return jsonify({
            "title": article.title,
            "text": article.text, # Sends full text for now
            "image": article.top_image,
            "publish_date": article.publish_date
        })

    except Exception as e:
        print(f"Error: {e}") # Print error to terminal for debugging
        return jsonify({"error": "Failed to extract content. Please check the URL."}), 500

if __name__ == '__main__':
    app.run(debug=True)