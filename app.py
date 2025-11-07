from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import mysql.connector
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import requests
import random
import razorpay


app = Flask(__name__)
app.secret_key = "darshan_secret_key"

# ---------- DATABASE CONNECTION ----------
db = mysql.connector.connect(
    host="localhost",
    user="root",
    password="",
    database="restaurant_system"
)
cursor = db.cursor(dictionary=True)

# ---------- RAZORPAY CONFIG ----------
razorpay_client = razorpay.Client(auth=("rzp_test_Rcs7vDldguHQpy", "ToQx1kGOUTpQZ14DQHN7W5JE"))


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
    cursor.execute("SELECT * FROM menu")
    items = cursor.fetchall()
    return render_template('menu.html', items=items)

# ---------- CART FUNCTIONALITY ----------
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

# make sure razorpay_client already configured earlier in file

@app.route('/checkout', methods=['GET', 'POST'])
def checkout():
    if 'user_id' not in session:
        flash("Login to checkout!", "warning")
        return redirect(url_for('login'))

    user_id = session['user_id']

    # load cart items (same as before)
    cursor.execute("""
        SELECT cart.id AS cart_id, menu.id AS menu_id, menu.dish_name, menu.price, cart.quantity
        FROM cart JOIN menu ON cart.menu_id = menu.id
        WHERE cart.user_id = %s
    """, (user_id,))
    items = cursor.fetchall()

    if not items:
        flash("Cart is empty!", "danger")
        return redirect(url_for('menu'))

    # compute total
    total_price = sum(item['price'] * item['quantity'] for item in items)
    total_paise = int(total_price * 100)

    # if GET just show cart page (for safety)
    if request.method == 'GET':
        return render_template('cart.html', items=items, total_price=total_price)

    # POST: user submitted address form -> capture address & create DB order + Razorpay order
    customer_name = request.form.get('customer_name')
    phone = request.form.get('phone')
    address = request.form.get('address')
    city = request.form.get('city')
    pincode = request.form.get('pincode')

    try:
        # 1) Insert order row into DB with Pending status (we will update razorpay id after creating Razorpay order)
        cursor.execute(
            "INSERT INTO orders (user_id, total_price, order_date, status, customer_name, phone, address, city, pincode) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (user_id, total_price, datetime.now(), 'Pending', customer_name, phone, address, city, pincode)
        )
        db.commit()
        order_db_id = cursor.lastrowid

        # 2) Create Razorpay order
        print("💰 Creating Razorpay order for:", total_price, "INR (", total_paise, "paise )")
        razor_order = razorpay_client.order.create({
            'amount': total_paise,
            'currency': 'INR',
            'payment_capture': '1',
            # optionally add receipt: str(order_db_id)
            'receipt': str(order_db_id)
        })
        print("✅ Razorpay order created:", razor_order.get('id'))

        # 3) Update DB order with razorpay_order_id
        cursor.execute("UPDATE orders SET razorpay_order_id = %s WHERE id = %s", (razor_order.get('id'), order_db_id))
        db.commit()

        # Save cart items to order_items table now (so they show up in admin) — keep status Pending until payment confirmed
        for it in items:
            cursor.execute(
                "INSERT INTO order_items (order_id, menu_id, quantity, price) VALUES (%s, %s, %s, %s)",
                (order_db_id, it['menu_id'], it['quantity'], it['price'])
            )
        db.commit()

        # NOTE: we DO NOT clear cart yet. We'll clear it after successful payment (in payment_success)
        # Render payment page with razorpay order id
        return render_template('payment.html',
                               total_price=total_price,
                               razorpay_order_id=razor_order.get('id'),
                               razorpay_key="rzp_test_Rcs7vDldguHQpy",  # your test key id
                               user=session.get('name'))

    except Exception as e:
        print("❌ Checkout Error:", e)
        flash("Failed to start checkout. Try again.", "danger")
        return redirect(url_for('cart'))


@app.route('/payment_success', methods=['GET'])
def payment_success():
    # expected query params from JS handler: payment_id and razorpay_order_id
    payment_id = request.args.get('payment_id')
    razor_order_id = request.args.get('order_id') or request.args.get('razorpay_order_id')

    if not payment_id:
        flash("Payment details missing.", "danger")
        return redirect(url_for('menu'))

    try:
        # 1) Save payment record
        user_id = session.get('user_id')
        cursor.execute("INSERT INTO payments (payment_id, user_id, status) VALUES (%s, %s, %s)",
                       (payment_id, user_id, 'Success'))
        db.commit()

        # 2) Mark corresponding order as Paid using razorpay_order_id
        if razor_order_id:
            cursor.execute("UPDATE orders SET status = %s WHERE razorpay_order_id = %s", ('Paid', razor_order_id))
            db.commit()
            # Clear cart for the user
            cursor.execute("DELETE FROM cart WHERE user_id = %s", (user_id,))
            db.commit()

        flash("Payment successful! Your order is confirmed.", "success")
        return redirect(url_for('menu'))

    except Exception as e:
        print("❌ Payment Success Handler Error:", e)
        flash("Payment recorded but something went wrong. Contact admin.", "warning")
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

@app.route('/add_dish', methods=['POST'])
def add_dish():
    dish_name = request.form['dish_name']
    price = request.form['price']
    category = request.form['category']
    cursor.execute("INSERT INTO menu (dish_name, price, category) VALUES (%s, %s, %s)",
                   (dish_name, price, category))
    db.commit()
    flash("Dish added successfully!", "success")
    return redirect(url_for('admin_dashboard'))

# ---------- AI + API MENU FETCH ----------
def item_exists(dish_name):
    cursor.execute("SELECT id FROM menu WHERE dish_name = %s", (dish_name,))
    return cursor.fetchone() is not None

def insert_menu_item_safe(dish_name, price, category, image_url=None):
    if item_exists(dish_name):
        return False
    cursor.execute(
        "INSERT INTO menu (dish_name, price, category, image_url) VALUES (%s, %s, %s, %s)",
        (dish_name, price, category, image_url)
    )
    db.commit()
    return True

@app.route('/fetch_dishes')
def fetch_dishes():
    inserted = []
    failed = []

    # ---------- 1️⃣ Fetch real dishes by cuisine ----------
    cuisines = ["Indian", "Italian", "Chinese", "Mexican", "French", "Turkish"]
    for cuisine in cuisines:
        try:
            url = f"https://www.themealdb.com/api/json/v1/1/filter.php?a={cuisine}"
            r = requests.get(url, timeout=8)
            data = r.json()
            if data and data.get('meals'):
                for meal in data['meals'][:10]:  # 10 dishes per cuisine
                    name = meal.get('strMeal')
                    image = meal.get('strMealThumb')
                    price = random.randint(149, 699)
                    if insert_menu_item_safe(name, price, cuisine, image):
                        inserted.append(name)
        except Exception as e:
            failed.append(f"{cuisine}: {str(e)}")

    # ---------- 2️⃣ Fetch dessert dishes ----------
    try:
        r = requests.get("https://www.themealdb.com/api/json/v1/1/filter.php?c=Dessert", timeout=8)
        data = r.json()
        if data and data.get('meals'):
            for meal in data['meals'][:10]:
                name = meal.get('strMeal')
                image = meal.get('strMealThumb')
                price = random.randint(99, 299)
                if insert_menu_item_safe(name, price, "Dessert", image):
                    inserted.append(name)
    except Exception as e:
        failed.append(f"Dessert: {str(e)}")

    # ---------- 3️⃣ Fetch soft beverages ----------
    try:
        drinks = ["Coffee", "Juice", "Tea", "Milkshake"]
        for drink in drinks:
            name = f"{drink} Special"
            price = random.randint(99, 249)
            insert_menu_item_safe(name, price, "Beverages", None)
            inserted.append(name)
    except Exception as e:
        failed.append(f"Beverages: {str(e)}")

    # ---------- 4️⃣ Fetch Hard Drinks / Cocktails ----------
    try:
        r = requests.get("https://www.thecocktaildb.com/api/json/v1/1/filter.php?c=Cocktail", timeout=8)
        data = r.json()
        if data and data.get('drinks'):
            for drink in data['drinks'][:25]:  # 25 drinks for larger variety
                name = drink.get('strDrink')
                image = drink.get('strDrinkThumb')
                price = random.randint(249, 899)
                if insert_menu_item_safe(name, price, "Hard Drink", image):
                    inserted.append(name)
    except Exception as e:
        failed.append(f"HardDrinks: {str(e)}")

    # ---------- 5️⃣ Add AI-style local extras ----------
    extras = [
        ("Paneer Butter Masala", 249, "Indian"),
        ("Chicken Biryani", 299, "Indian"),
        ("Veg Manchurian", 229, "Chinese"),
        ("Mutton Seekh Kebab", 349, "Indian"),
        ("Margherita Pizza", 199, "Italian"),
        ("Tandoori Roti", 49, "Indian"),
        ("Chocolate Brownie", 149, "Dessert"),
        ("Mango Lassi", 119, "Beverages"),
        ("Whiskey Sour", 499, "Hard Drink"),
        ("Vodka Cranberry", 459, "Hard Drink"),
    ]
    for dish, price, cat in extras:
        if insert_menu_item_safe(dish, price, cat, None):
            inserted.append(dish)

    return jsonify({
        "status": "done",
        "inserted_count": len(inserted),
        "sample_items": inserted[:15],
        "failed_sources": failed
    })


# ---------- RUN ----------
if __name__ == "__main__":
    app.run(debug=True)
