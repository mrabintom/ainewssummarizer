from flask import Flask, render_template, redirect, url_for, request, flash, jsonify, send_file
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import requests
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
from newspaper import Article, Config
import nltk
from gtts import gTTS
import io
from datetime import datetime

# --- IMPORT DATABASE ---
# Ensure models.py has User, Summary, and ChatLog tables
from models import db, User, Summary, ChatLog

# --- NLTK FIX ---
# Automatically downloads missing tokenizer files to prevent crashes
try:
    nltk.data.find('tokenizers/punkt')
    nltk.data.find('tokenizers/punkt_tab')
except LookupError:
    print("Downloading missing NLTK files...")
    nltk.download('punkt')
    nltk.download('punkt_tab')

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-change-this'
# Using your exact PostgreSQL credentials
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://postgres:Admin%40123@localhost/newsai'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

# --- AI SETUP ---
# Using 'flan-t5-base' for smarter, context-aware chatting and summarizing
print("Loading Intelligent AI Model...")
model_name = "google/flan-t5-base" 
tokenizer = AutoTokenizer.from_pretrained(model_name)
model = AutoModelForSeq2SeqLM.from_pretrained(model_name)

# --- LOGIN MANAGER ---
login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- ROUTES ---

@app.route('/')
def home():
    if current_user.is_authenticated:
        if current_user.is_admin:
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        
        if user and check_password_hash(user.password, password):
            login_user(user)
            # SMART REDIRECT: Admins go to Admin Panel, Users go to Dashboard
            if user.is_admin:
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('dashboard'))
            
        flash('Invalid email or password.', 'error')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        password = request.form.get('password')
        
        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'error')
            return redirect(url_for('register'))
            
        hashed_pw = generate_password_hash(password, method='scrypt')
        new_user = User(name=name, email=email, password=hashed_pw)
        db.session.add(new_user)
        db.session.commit()
        flash('Account created! Please login.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

# --- USER DASHBOARD ---
@app.route('/dashboard')
@login_required
def dashboard():
    # Prevent Admins from accessing the regular user dashboard
    if current_user.is_admin: 
        return redirect(url_for('admin_dashboard'))
    
    API_KEY = 'YOUR_NEWSAPI_KEY_HERE' # <--- PASTE YOUR NEWSAPI KEY HERE
    url = f'https://newsapi.org/v2/top-headlines?category=technology&language=en&apiKey={API_KEY}'
    try:
        articles = requests.get(url).json().get('articles', [])
    except:
        articles = []
    return render_template('dashboard.html', articles=articles)

# --- ADMIN DASHBOARD ---
@app.route('/admin_dashboard')
@login_required
def admin_dashboard():
    # Security Check: Kick out non-admins
    if not current_user.is_admin:
        return redirect(url_for('dashboard')) 
    
    # Fetch all data for the Admin Control Panel
    users = User.query.all()
    total_summaries = Summary.query.count()
    
    # Fetch latest 50 records for the global tracking tables
    recent_summaries = Summary.query.order_by(Summary.date_created.desc()).limit(50).all()
    chat_logs = ChatLog.query.order_by(ChatLog.timestamp.desc()).limit(50).all()
    
    return render_template('admin_dashboard.html', 
                           users=users, 
                           total_summaries=total_summaries, 
                           recent_summaries=recent_summaries,
                           chat_logs=chat_logs)

# --- ADMIN ACTIONS: DELETE USER ---
@app.route('/admin/delete_user/<int:user_id>')
@login_required
def delete_user(user_id):
    if not current_user.is_admin: 
        return redirect(url_for('dashboard'))
    
    user_to_delete = User.query.get(user_id)
    if user_to_delete:
        # Delete their associated data first to avoid Foreign Key constraint errors
        Summary.query.filter_by(user_id=user_id).delete()
        ChatLog.query.filter_by(user_id=user_id).delete()
        db.session.delete(user_to_delete)
        db.session.commit()
        flash(f'User {user_to_delete.name} deleted successfully.', 'success')
    return redirect(url_for('admin_dashboard'))

# --- AI SUMMARIZATION ROUTE ---
@app.route('/summarize_article', methods=['POST'])
@login_required
def summarize_article():
    data = request.json
    url = data.get('url')
    length_opt = int(data.get('length', 80)) 

    if not url: return jsonify({'error': 'No URL provided'}), 400

    try:
        # 1. Scrape Article (Windows Safe Config to prevent WinError 3)
        config = Config()
        config.browser_user_agent = 'Mozilla/5.0'
        config.request_timeout = 10
        config.memoize_articles = False
        config.fetch_images = False

        article = Article(url, config=config)
        article.download()
        article.parse()
        article.nlp()
        
        input_text = article.text
        if len(input_text) < 50:
            return jsonify({'error': "Article content too short or inaccessible."}), 400

        # 2. Smart Prompting based on requested word length
        if length_opt <= 20:
            prompt_style = "Summarize this news in 1 short sentence (approx 20 words). Simple English."
        elif length_opt <= 40:
            prompt_style = "Summarize this news in 2 sentences (approx 40 words). Simple English."
        else:
            prompt_style = "Summarize this news in a paragraph (approx 80 words). Simple English."

        prompt = f"{prompt_style} Content: {input_text[:3000]}"
        
        input_ids = tokenizer(prompt, return_tensors="pt", max_length=1024, truncation=True).input_ids
        
        # 3. Generate Summary (Generate slightly more than needed)
        outputs = model.generate(
            input_ids, 
            max_length=length_opt + 20, 
            min_length=length_opt - 10, 
            length_penalty=1.0, 
            num_beams=4, 
            early_stopping=True
        )
        
        raw_summary = tokenizer.decode(outputs[0], skip_special_tokens=True)

        # 4. Strict Professional Truncation
        words = raw_summary.split()
        if len(words) > length_opt:
            final_summary = " ".join(words[:length_opt])
            if not final_summary.endswith('.'):
                final_summary += "..." 
        else:
            final_summary = raw_summary

        # 5. Save to Database (Search History)
        new_s = Summary(
            title=article.title, 
            original_url=url, 
            summary_text=final_summary, 
            author=current_user
        )
        db.session.add(new_s)
        db.session.commit()
        
        return jsonify({'summary': final_summary, 'title': article.title})

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'error': str(e)}), 500

# --- INTERACTIVE CHATBOT ROUTE ---
@app.route('/chat_about_article', methods=['POST'])
@login_required
def chat_about_article():
    data = request.json
    q = data.get('question')
    ctx = data.get('context')
    
    # 1. Structure the prompt so the AI understands its task
    prompt = f"Answer this question based on the context. Keep it short.\n\nContext: {ctx[:2000]}\n\nQuestion: {q}"
    
    # 2. Tokenize with strict truncation to fit model memory
    input_ids = tokenizer(prompt, return_tensors="pt", max_length=512, truncation=True).input_ids
    
    # 3. Generate Answer
    outputs = model.generate(input_ids, max_length=100)
    answer = tokenizer.decode(outputs[0], skip_special_tokens=True)
    
    # 4. Save to Admin Logs (Spy Mode)
    new_log = ChatLog(user_id=current_user.id, question=q, answer=answer)
    db.session.add(new_log)
    db.session.commit()
    
    return jsonify({'answer': answer})

# --- TEXT TO SPEECH ROUTE ---
@app.route('/text_to_speech', methods=['POST'])
@login_required
def text_to_speech():
    text = request.json.get('text')
    tts = gTTS(text=text, lang='en')
    mp3_fp = io.BytesIO()
    tts.write_to_fp(mp3_fp)
    mp3_fp.seek(0)
    return send_file(mp3_fp, mimetype="audio/mpeg", download_name="summary.mp3")

# --- PERSONAL HISTORY ROUTE (Fixes the BuildError) ---
@app.route('/history')
@login_required
def history():
    # Regular users only see their own history
    user_summaries = Summary.query.filter_by(user_id=current_user.id).order_by(Summary.date_created.desc()).all()
    return render_template('history.html', summaries=user_summaries)

# --- LOGOUT ---
@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    # IMPORTANT: use_reloader=False prevents the models from loading twice and crashing your laptop RAM
    app.run(debug=True, use_reloader=False)