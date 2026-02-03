import os
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY')

# Database connection
def get_db():
    conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    return conn

# Simple user class for authentication
class User(UserMixin):
    def __init__(self, id, username):
        self.id = id
        self.username = username

# Flask-Login setup
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    return User(user_id, 'admin')  # Simplified for now

# Routes
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        # Simplified auth - we'll improve this later
        if username == 'admin' and password == 'foxtown2026':
            user = User('1', username)
            login_user(user)
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid credentials', 'error')
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')

@app.route('/recipes')
@login_required
def recipes():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Get all recipes with ingredient count
    cur.execute("""
        SELECT r.*, COUNT(ri.id) as ingredient_count
        FROM recipes r
        LEFT JOIN recipe_ingredients ri ON r.id = ri.recipe_id
        GROUP BY r.id
        ORDER BY r.name
    """)
    
    recipes_list = cur.fetchall()
    cur.close()
    conn.close()
    
    return render_template('recipes.html', recipes=recipes_list)

if __name__ == '__main__':
    app.run(debug=True, port=5000)