from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import mysql.connector
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import requests
import random
import razorpay
import os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "darshan_secret_key")

# ---------- DATABASE CONNECTION ----------
try:
    db = mysql.connector.connect(
        host=os.getenv("MYSQL_HOST"),
        port=int(os.getenv("MYSQL_PORT")),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE")
    )
    cursor = db.cursor(dictionary=True)
    print("✅ Connected to Railway MySQL (from .env) successfully!")
except mysql.connector.Error as err:
    print("❌ MySQL connection failed:", err)

# ---------- RAZORPAY CONFIG ----------
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "rzp_test_Rcs7vDldguHQpy")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "ToQx1kGOUTpQZ14DQHN7W5JE")
razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# ---------- BASIC ROUTES ----------
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        password = generate_password_hash(request.form['password'])
        try:
            cursor.execute("INSERT INTO users (name, email, password) VALUES (%s, %s, %s)", (name, email, password))
            db.commit()
            flash("Signup successful! Please login.", "success")
            return redirect(url_for('login'))
        except:
            flash("Email already exists!", "danger")
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        cursor.execute("SELECT * FROM users WHERE email=%s", (email,))
        user = cursor.fetchone()

        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['name'] = user['name']
            flash(f"Welcome back, {user['name']}!", "success")
            return redirect(url_for('menu'))
        else:
            flash("Invalid credentials", "danger")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash("Logged out successfully.", "info")
    return redirect(url_for('home'))


# ---------- MENU ----------
@app.route('/menu')
def menu():
    cursor.execute("SELECT * FROM menu WHERE category != 'Premium Drinks'")
    items = cursor.fetchall()
    return render_template("menu.html", items=items)


# ---------- CART ----------
@app.route('/add_to_cart/<int:menu_id>')
def add_to_cart(menu_id):
    if 'user_id' not in session:
        flash("Please login to add items to cart!", "warning")
        return redirect(url_for('login'))

    user_id = session['user_id']
    cursor.execute("SELECT * FROM cart WHERE user_id=%s AND menu_id=%s", (user_id, menu_id))
    existing = cursor.fetchone()

    if existing:
        cursor.execute("UPDATE cart SET quantity = quantity + 1 WHERE user_id=%s AND menu_id=%s", (user_id, menu_id))
    else:
        cursor.execute("INSERT INTO cart (user_id, menu_id, quantity) VALUES (%s, %s, 1)", (user_id, menu_id))
    db.commit()
    flash("Item added to cart!", "success")
    return redirect(url_for('menu'))

@app.route('/cart')
def cart():
    if 'user_id' not in session:
        flash("Login to view your cart!", "warning")
        return redirect(url_for('login'))

    user_id = session['user_id']
    cursor.execute("""
        SELECT cart.id AS cart_id, menu.dish_name, menu.price, cart.quantity, 
        (menu.price * cart.quantity) AS total
        FROM cart 
        JOIN menu ON cart.menu_id = menu.id
        WHERE cart.user_id = %s
    """, (user_id,))
    items = cursor.fetchall()
    total_price = sum(item['total'] for item in items)
    return render_template('cart.html', items=items, total_price=total_price)

@app.route('/remove_item/<int:cart_id>')
def remove_item(cart_id):
    cursor.execute("DELETE FROM cart WHERE id=%s", (cart_id,))
    db.commit()
    flash("Item removed from cart.", "info")
    return redirect(url_for('cart'))


# ---------- CHECKOUT ----------
@app.route('/checkout', methods=['GET', 'POST'])
def checkout():
    if 'user_id' not in session:
        flash("Login to checkout!", "warning")
        return redirect(url_for('login'))

    user_id = session['user_id']
    cursor.execute("""
        SELECT cart.id AS cart_id, menu.id AS menu_id, menu.dish_name, menu.price, cart.quantity
        FROM cart JOIN menu ON cart.menu_id = menu.id
        WHERE cart.user_id = %s
    """, (user_id,))
    items = cursor.fetchall()

    if not items:
        flash("Cart is empty!", "danger")
        return redirect(url_for('menu'))

    total_price = sum(item['price'] * item['quantity'] for item in items)
    total_paise = int(total_price * 100)

    if request.method == 'GET':
        return render_template('cart.html', items=items, total_price=total_price)

    customer_name = request.form.get('customer_name')
    phone = request.form.get('phone')
    address = request.form.get('address')
    city = request.form.get('city')
    pincode = request.form.get('pincode')

    try:
        cursor.execute("""
            INSERT INTO orders (user_id, total_price, order_date, status, customer_name, phone, address, city, pincode)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (user_id, total_price, datetime.now(), 'Pending', customer_name, phone, address, city, pincode))
        db.commit()
        order_db_id = cursor.lastrowid

        razor_order = razorpay_client.order.create({
            'amount': total_paise,
            'currency': 'INR',
            'payment_capture': '1',
            'receipt': str(order_db_id)
        })

        cursor.execute("UPDATE orders SET razorpay_order_id = %s WHERE id = %s", (razor_order.get('id'), order_db_id))
        db.commit()

        for it in items:
            cursor.execute(
                "INSERT INTO order_items (order_id, menu_id, quantity, price) VALUES (%s, %s, %s, %s)",
                (order_db_id, it['menu_id'], it['quantity'], it['price'])
            )
        db.commit()

        return render_template('payment.html',
                               total_price=total_price,
                               razorpay_order_id=razor_order.get('id'),
                               razorpay_key=RAZORPAY_KEY_ID,
                               user=session.get('name'))
    except Exception as e:
        print("❌ Checkout Error:", e)
        flash("Failed to start checkout. Try again.", "danger")
        return redirect(url_for('cart'))


# ---------- PAYMENT SUCCESS ----------
@app.route('/payment_success', methods=['GET'])
def payment_success():
    payment_id = request.args.get('payment_id')
    razor_order_id = request.args.get('order_id') or request.args.get('razorpay_order_id')

    if not payment_id:
        flash("Payment details missing.", "danger")
        return redirect(url_for('menu'))

    try:
        user_id = session.get('user_id')
        cursor.execute("INSERT INTO payments (payment_id, user_id, status) VALUES (%s, %s, %s)",
                       (payment_id, user_id, 'Success'))
        db.commit()

        if razor_order_id:
            cursor.execute("UPDATE orders SET status = %s WHERE razorpay_order_id = %s", ('Paid', razor_order_id))
            db.commit()
            cursor.execute("DELETE FROM cart WHERE user_id = %s", (user_id,))
            db.commit()

        flash("Payment successful! Your order is confirmed.", "success")
        return redirect(url_for('menu'))
    except Exception as e:
        print("❌ Payment Success Handler Error:", e)
        flash("Payment recorded but something went wrong.", "warning")
        return redirect(url_for('menu'))


# ---------- FEEDBACK ----------
@app.route('/feedback', methods=['GET', 'POST'])
def feedback():
    if request.method == 'POST':
        if 'user_id' not in session:
            flash("Please login to submit feedback!", "warning")
            return redirect(url_for('login'))

        message = request.form['message']
        rating = request.form['rating']
        user_id = session['user_id']

        cursor.execute("INSERT INTO feedback (user_id, message, rating) VALUES (%s, %s, %s)",
                       (user_id, message, rating))
        db.commit()
        flash("Thanks for your feedback!", "success")
        return redirect(url_for('home'))
    return render_template('feedback.html')


# ---------- ADMIN ----------
@app.route('/admin')
def admin_dashboard():
    cursor.execute("SELECT COUNT(*) AS total_orders FROM orders")
    total_orders = cursor.fetchone()['total_orders']

    cursor.execute("SELECT COUNT(*) AS total_users FROM users")
    total_users = cursor.fetchone()['total_users']

    cursor.execute("SELECT SUM(total_price) AS total_revenue FROM orders")
    total_revenue = cursor.fetchone()['total_revenue'] or 0

    cursor.execute("SELECT * FROM orders ORDER BY order_date DESC LIMIT 10")
    recent_orders = cursor.fetchall()

    return render_template('admin_dashboard.html',
                           total_orders=total_orders,
                           total_users=total_users,
                           total_revenue=total_revenue,
                           orders=recent_orders)


# ---------- RUN ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
