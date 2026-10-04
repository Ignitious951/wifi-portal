#!/usr/bin/env python3
"""
================================================================================
VELARIS - Pay to Connect WiFi Billing System (STK Push + SMS Verify)
Complete Single File - Production Ready
================================================================================
Flow:
1. User selects plan
2. User enters M-Pesa phone number
3. Portal sends STK Push (M-Pesa prompt) to their phone
4. User enters PIN → money goes to Till
5. User receives M-Pesa SMS confirmation
6. User pastes SMS on portal
7. System verifies code (each code used once) → access granted
================================================================================
"""

import os
import sys
import json
import hashlib
import re
import base64
import subprocess
import logging
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from apscheduler.schedulers.background import BackgroundScheduler

# ===================================================================
# CONFIGURATION
# ===================================================================

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "ignitious123"

BUSINESS_NAME = "VELARIS"

# M-Pesa Daraja API Credentials (Get from developer.safaricom.co.ke)
MPESA_CONSUMER_KEY = "UkvebjkIaD2lb4173pP89Xkza9OQrLo3CQyLcEe90mc0VGft"
MPESA_CONSUMER_SECRET = "OKzh6qrwmbfGFlTOT2GxT7ZSxOqASGnPgyG2RCqV8HLC8i5miUC6nbucY0mOOHXp"
MPESA_TILL_NUMBER = "1671404"          # Your Till Number (Buy Goods)
MPESA_PASSKEY = "bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919"    # From Safaricom Daraja
MPESA_CALLBACK_URL = "https://wifi-portal-2.onrender.com/mpesa/callback"
MPESA_ENVIRONMENT = "sandbox"           # "sandbox" or "production"
MPESA_TEST_MODE = True                  # True = skip real API (testing)

SECRET_KEY = "velaris-super-secret-key-change-this"
DEBUG_MODE = False
PORT = 5000
HOST = "0.0.0.0"

# ===================================================================
# LOGGING
# ===================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ===================================================================
# FLASK SETUP
# ===================================================================

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///velaris_billing.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'admin_login'
login_manager.login_message = None

# ===================================================================
# DATABASE MODELS
# ===================================================================

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Customer(db.Model):
    __tablename__ = 'customers'
    id = db.Column(db.Integer, primary_key=True)
    mac_address = db.Column(db.String(17), unique=True, nullable=False)
    ip_address = db.Column(db.String(45))
    phone_number = db.Column(db.String(15))
    plan = db.Column(db.String(50))
    start_time = db.Column(db.DateTime)
    expiry_time = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_active = db.Column(db.DateTime, default=datetime.utcnow)

class Transaction(db.Model):
    __tablename__ = 'transactions'
    id = db.Column(db.Integer, primary_key=True)
    mpesa_code = db.Column(db.String(20), unique=True, nullable=False)
    phone_number = db.Column(db.String(15))
    amount = db.Column(db.Float, nullable=False)
    plan = db.Column(db.String(50))
    customer_mac = db.Column(db.String(17))
    status = db.Column(db.String(20), default='used')
    raw_message = db.Column(db.Text)
    stk_checkout_id = db.Column(db.String(50))  # Link to STK Push
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    used_at = db.Column(db.DateTime)

class STKRequest(db.Model):
    """Track STK Push requests until customer pastes SMS"""
    __tablename__ = 'stk_requests'
    id = db.Column(db.Integer, primary_key=True)
    checkout_id = db.Column(db.String(50), unique=True, nullable=False)
    phone_number = db.Column(db.String(15), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    plan = db.Column(db.String(50))
    customer_mac = db.Column(db.String(17))
    status = db.Column(db.String(20), default='pending')  # pending, completed, failed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Setting(db.Model):
    __tablename__ = 'settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

# ===================================================================
# DEFAULT PLANS
# ===================================================================

DEFAULT_PLANS = {
    "1hr": {"name": "1 Hour", "price": 18, "duration": 3600},
    "3hr": {"name": "3 Hours", "price": 49, "duration": 10800},
    "1day": {"name": "1 Day", "price": 99, "duration": 86400},
    "7day": {"name": "1 Week", "price": 500, "duration": 604800}
}

# ===================================================================
# HELPERS
# ===================================================================

def get_plans():
    setting = Setting.query.filter_by(key='plans').first()
    if setting:
        try:
            return json.loads(setting.value)
        except:
            return DEFAULT_PLANS
    return DEFAULT_PLANS

def save_plans(plans):
    setting = Setting.query.filter_by(key='plans').first()
    if setting:
        setting.value = json.dumps(plans)
    else:
        setting = Setting(key='plans', value=json.dumps(plans))
        db.session.add(setting)
    db.session.commit()

def get_client_mac():
    try:
        ip = request.remote_addr
        if os.name == 'nt':
            output = subprocess.check_output(f'arp -a {ip}', shell=True).decode()
            match = re.search(r'([0-9A-Fa-f]{2}-[0-9A-Fa-f]{2}-[0-9A-Fa-f]{2}-[0-9A-Fa-f]{2}-[0-9A-Fa-f]{2}-[0-9A-Fa-f]{2})', output)
            if match:
                return match.group(1).replace('-', ':')
        else:
            output = subprocess.check_output(f'arp -n {ip}', shell=True).decode()
            match = re.search(r'([0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2})', output)
            if match:
                return match.group(1)
    except:
        pass
    return f"00:00:00:{request.remote_addr.replace('.', ':')}"

def format_time(seconds):
    if seconds <= 0:
        return "Expired"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    parts = []
    if days > 0: parts.append(f"{int(days)}d")
    if hours > 0: parts.append(f"{int(hours)}h")
    if minutes > 0 and days == 0: parts.append(f"{int(minutes)}m")
    return " ".join(parts) if parts else "0s"

def parse_mpesa_message(message):
    """Parse M-Pesa SMS to extract code, amount, phone"""
    result = {'valid': False, 'code': None, 'amount': None, 'phone': None, 'error': None}
    if not message or len(message.strip()) < 20:
        result['error'] = 'Message too short'
        return result
    msg = message.strip().upper()

    # M-Pesa code: 10 chars, starts with letter
    code_matches = re.findall(r'\b([A-Z]{3}[A-Z0-9]{7})\b', msg)
    if not code_matches:
        code_matches = re.findall(r'\b([A-Z][A-Z0-9]{9})\b', msg)
    if code_matches:
        result['code'] = code_matches[0]
    else:
        result['error'] = 'Could not find M-Pesa code'
        return result

    # Amount
    for pattern in [r'KSH\s*([\d,]+(?:\.\d{2})?)', r'KES\s*([\d,]+(?:\.\d{2})?)']:
        match = re.search(pattern, msg)
        if match:
            try:
                result['amount'] = float(match.group(1).replace(',', ''))
                break
            except: pass
    if result['amount'] is None:
        result['error'] = 'Could not find amount'
        return result

    # Phone
    phone_match = re.search(r'(?:254|0)([17]\d{8})', msg)
    if phone_match:
        result['phone'] = phone_match.group(1)

    result['valid'] = True
    return result

def find_matching_plan(amount, plans):
    for key, plan in plans.items():
        if abs(plan['price'] - amount) < 1:
            return key, plan
    return None, None

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ===================================================================
# M-PESA STK PUSH
# ===================================================================

class MpesaSTK:
    def __init__(self):
        self.consumer_key = MPESA_CONSUMER_KEY
        self.consumer_secret = MPESA_CONSUMER_SECRET
        self.till = MPESA_TILL_NUMBER
        self.passkey = MPESA_PASSKEY
        self.callback = MPESA_CALLBACK_URL
        self.env = MPESA_ENVIRONMENT
        self.base = 'https://sandbox.safaricom.co.ke' if self.env == 'sandbox' else 'https://api.safaricom.co.ke'

    def get_token(self):
        if not self.consumer_key or not self.consumer_secret:
            return None
        auth = base64.b64encode(f"{self.consumer_key}:{self.consumer_secret}".encode()).decode()
        try:
            r = requests.get(f"{self.base}/oauth/v1/generate?grant_type=client_credentials",
                            headers={"Authorization": f"Basic {auth}"}, timeout=10)
            if r.status_code == 200:
                return r.json().get('access_token')
        except Exception as e:
            logger.error(f"Token error: {e}")
        return None

    def stk_push(self, phone, amount, plan_key, customer_mac):
        """Send STK Push. In TEST MODE, simulate."""
        if MPESA_TEST_MODE:
            # Simulated checkout ID for testing
            checkout_id = f"SIM{int(datetime.now().timestamp())}"
            logger.info(f"[TEST] STK Push simulated: {phone}, KES {amount}")
            return {'success': True, 'checkout_id': checkout_id, 'simulated': True}

        token = self.get_token()
        if not token:
            return {'success': False, 'message': 'Failed to get M-Pesa token'}

        # Format phone
        p = re.sub(r'^\+?254|^0', '', phone)
        p = f"254{p}"

        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        pw = base64.b64encode(f"{self.till}{self.passkey}{ts}".encode()).decode()

        payload = {
            "BusinessShortCode": self.till,
            "Password": pw,
            "Timestamp": ts,
            "TransactionType": "CustomerBuyGoodsOnline",  # Buy Goods (Till)
            "Amount": str(int(amount)),
            "PartyA": p,
            "PartyB": self.till,
            "PhoneNumber": p,
            "CallBackURL": self.callback,
            "AccountReference": f"VELARIS-{customer_mac[:6]}",
            "TransactionDesc": f"WiFi {get_plans().get(plan_key, {}).get('name','Plan')}"
        }

        try:
            r = requests.post(f"{self.base}/mpesa/stkpush/v1/processrequest",
                            json=payload,
                            headers={"Authorization": f"Bearer {token}",
                                    "Content-Type": "application/json"},
                            timeout=15)
            res = r.json()
            if res.get('ResponseCode') == '0':
                checkout_id = res.get('CheckoutRequestID')
                logger.info(f"STK Push sent: {checkout_id}")
                return {'success': True, 'checkout_id': checkout_id}
            else:
                logger.error(f"STK failed: {res}")
                return {'success': False, 'message': res.get('ResponseDescription', 'STK failed')}
        except Exception as e:
            logger.error(f"STK error: {e}")
            return {'success': False, 'message': str(e)}

mpesa_stk = MpesaSTK()

# ===================================================================
# NETWORK MANAGER
# ===================================================================

class NetworkManager:
    def __init__(self):
        self.platform = 'windows' if os.name == 'nt' else 'linux'

    def allow_device(self, mac):
        try:
            if self.platform == 'windows':
                subprocess.run(f'netsh advfirewall firewall add rule name="Velaris_{mac.replace(":","")}" dir=in action=allow',
                             shell=True, capture_output=True, timeout=5)
            else:
                subprocess.run(f'sudo iptables -I FORWARD -m mac --mac-source {mac} -j ACCEPT',
                             shell=True, capture_output=True, timeout=5)
            return True
        except: return False

    def block_device(self, mac):
        try:
            if self.platform == 'windows':
                subprocess.run(f'netsh advfirewall firewall delete rule name="Velaris_{mac.replace(":","")}"',
                             shell=True, capture_output=True, timeout=5)
            else:
                subprocess.run(f'sudo iptables -D FORWARD -m mac --mac-source {mac} -j ACCEPT',
                             shell=True, capture_output=True, timeout=5)
            return True
        except: return False

network = NetworkManager()

# ===================================================================
# PUBLIC ROUTES
# ===================================================================

@app.route('/')
def index():
    client_mac = get_client_mac()
    customer = Customer.query.filter_by(mac_address=client_mac, is_active=True).first()
    if customer and customer.expiry_time and customer.expiry_time > datetime.utcnow():
        remaining = (customer.expiry_time - datetime.utcnow()).total_seconds()
        return render_template_string(ACTIVE_HTML,
                                     customer=customer,
                                     remaining=format_time(remaining),
                                     remaining_seconds=remaining,
                                     business_name=BUSINESS_NAME)
    plans = get_plans()
    return render_template_string(INDEX_HTML,
                                plans=plans,
                                client_mac=client_mac,
                                business_name=BUSINESS_NAME)

@app.route('/send-prompt', methods=['POST'])
def send_prompt():
    """Send STK Push to customer's phone"""
    try:
        phone = request.form.get('phone', '').strip()
        plan_key = request.form.get('plan', '').strip()
        client_mac = request.form.get('mac', '').strip()

        # Clean phone
        phone_digits = re.sub(r'\D', '', phone)
        if len(phone_digits) < 9:
            return jsonify({'success': False, 'message': 'Enter a valid phone number (e.g., 0712345678)'})

        plans = get_plans()
        if plan_key not in plans:
            return jsonify({'success': False, 'message': 'Please select a valid plan'})

        plan = plans[plan_key]

        # Send STK Push
        result = mpesa_stk.stk_push(phone_digits, plan['price'], plan_key, client_mac)

        if not result['success']:
            return jsonify({'success': False, 'message': result.get('message', 'Failed to send prompt')})

        # Save the STK request
        stk = STKRequest(
            checkout_id=result['checkout_id'],
            phone_number=phone_digits,
            amount=plan['price'],
            plan=plan_key,
            customer_mac=client_mac,
            status='pending'
        )
        db.session.add(stk)
        db.session.commit()

        logger.info(f"STK saved: {result['checkout_id']} for {phone_digits}")
        return jsonify({
            'success': True,
            'checkout_id': result['checkout_id'],
            'message': f'Payment prompt sent to {phone_digits}. Enter your PIN to pay KES {plan["price"]}.',
            'plan': plan['name'],
            'amount': plan['price']
        })
    except Exception as e:
        logger.error(f"Send prompt error: {e}")
        return jsonify({'success': False, 'message': f'Error: {str(e)}'})

@app.route('/verify', methods=['POST'])
def verify_payment():
    """Verify pasted M-Pesa SMS and grant access"""
    try:
        client_mac = request.form.get('mac', '')
        message = request.form.get('mpesa_message', '').strip()
        selected_plan = request.form.get('plan', '')
        checkout_id = request.form.get('checkout_id', '')

        if not message:
            return jsonify({'success': False, 'message': 'Please paste your M-Pesa confirmation message'})
        if not selected_plan:
            return jsonify({'success': False, 'message': 'Please select a plan first'})

        # Parse
        parsed = parse_mpesa_message(message)
        if not parsed['valid']:
            return jsonify({'success': False, 'message': f"Invalid message: {parsed.get('error')}"})

        code = parsed['code']
        amount = parsed['amount']
        phone = parsed['phone']

        logger.info(f"Verify: code={code}, amount={amount}, phone={phone}")

        # Check if code already used
        existing = Transaction.query.filter_by(mpesa_code=code).first()
        if existing:
            return jsonify({
                'success': False,
                'message': f'M-Pesa code {code} has already been used. Each code works only once.'
            })

        # Match plan by amount
        plans = get_plans()
        plan_key, plan = find_matching_plan(amount, plans)
        if not plan_key:
            return jsonify({
                'success': False,
                'message': f'Amount KES {amount:.0f} does not match any plan.'
            })

        if selected_plan != plan_key:
            return jsonify({
                'success': False,
                'message': f'You selected {plans[selected_plan]["name"]} but paid {plan["name"]}. Select the correct plan.'
            })

        # Mark STK as completed if exists
        if checkout_id:
            stk = STKRequest.query.filter_by(checkout_id=checkout_id).first()
            if stk:
                stk.status = 'completed'

        # Create transaction (unique code guaranteed)
        transaction = Transaction(
            mpesa_code=code,
            phone_number=phone or 'unknown',
            amount=amount,
            plan=plan_key,
            customer_mac=client_mac,
            status='used',
            raw_message=message[:500],
            stk_checkout_id=checkout_id or None,
            used_at=datetime.utcnow()
        )
        db.session.add(transaction)

        # Grant access
        customer = Customer.query.filter_by(mac_address=client_mac).first()
        expiry = datetime.utcnow() + timedelta(seconds=plan['duration'])

        if customer:
            customer.is_active = True
            customer.expiry_time = expiry
            customer.plan = plan_key
            customer.phone_number = phone or customer.phone_number
            customer.last_active = datetime.utcnow()
        else:
            customer = Customer(
                mac_address=client_mac,
                ip_address=request.remote_addr,
                phone_number=phone or 'unknown',
                plan=plan_key,
                start_time=datetime.utcnow(),
                expiry_time=expiry,
                is_active=True
            )
            db.session.add(customer)

        db.session.commit()
        network.allow_device(client_mac)

        logger.info(f"✅ Access granted: {client_mac} - {plan['name']} ({code})")

        return jsonify({
            'success': True,
            'message': f'Payment verified! Code {code} accepted for {plan["name"]}.',
            'plan': plan['name'],
            'expiry': expiry.isoformat()
        })
    except Exception as e:
        logger.error(f"Verify error: {e}")
        return jsonify({'success': False, 'message': f'Error: {str(e)}'})

@app.route('/mpesa/callback', methods=['POST'])
def mpesa_callback():
    """Receive M-Pesa callback (optional - for logging)"""
    data = request.json
    logger.info(f"M-Pesa callback: {data}")
    try:
        stk = data.get('Body', {}).get('stkCallback', {})
        checkout_id = stk.get('CheckoutRequestID')
        result_code = stk.get('ResultCode')
        if checkout_id:
            req = STKRequest.query.filter_by(checkout_id=checkout_id).first()
            if req:
                req.status = 'paid' if result_code == '0' else 'failed'
                db.session.commit()
    except: pass
    return jsonify({'ResultCode': 0, 'ResultDesc': 'Success'})

# ===================================================================
# ADMIN ROUTES
# ===================================================================

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if current_user.is_authenticated:
        return redirect(url_for('admin_dashboard'))
    if request.method == 'POST':
        u = request.form.get('username')
        p = request.form.get('password')
        user = User.query.filter_by(username=u).first()
        if user and user.password == hashlib.md5(p.encode()).hexdigest():
            login_user(user, remember=True)
            return redirect(url_for('admin_dashboard'))
        flash('Invalid credentials', 'error')
    return render_template_string(ADMIN_LOGIN_HTML, business_name=BUSINESS_NAME)

@app.route('/admin/logout')
@login_required
def admin_logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/admin')
@login_required
def admin_dashboard():
    total_customers = Customer.query.count()
    active_customers = Customer.query.filter(
        Customer.is_active == True,
        Customer.expiry_time > datetime.utcnow()
    ).count()
    total_transactions = Transaction.query.count()
    total_revenue = db.session.query(db.func.sum(Transaction.amount)).scalar() or 0
    recent = Transaction.query.order_by(Transaction.created_at.desc()).limit(10).all()
    active = Customer.query.filter(
        Customer.is_active == True,
        Customer.expiry_time > datetime.utcnow()
    ).all()
    return render_template_string(ADMIN_DASHBOARD_HTML,
                                 total_customers=total_customers,
                                 active_customers=active_customers,
                                 total_transactions=total_transactions,
                                 total_revenue=total_revenue,
                                 recent_transactions=recent,
                                 active_sessions=active,
                                 now=datetime.utcnow(),
                                 business_name=BUSINESS_NAME)

@app.route('/admin/customers')
@login_required
def admin_customers():
    customers = Customer.query.order_by(Customer.created_at.desc()).all()
    return render_template_string(ADMIN_CUSTOMERS_HTML,
                                 customers=customers,
                                 now=datetime.utcnow(),
                                 business_name=BUSINESS_NAME)

@app.route('/admin/transactions')
@login_required
def admin_transactions():
    transactions = Transaction.query.order_by(Transaction.created_at.desc()).all()
    return render_template_string(ADMIN_TRANSACTIONS_HTML,
                                 transactions=transactions,
                                 business_name=BUSINESS_NAME)

@app.route('/admin/plans', methods=['GET', 'POST'])
@login_required
def admin_plans():
    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'add':
            new_key = request.form.get('new_key', '').strip().lower()
            new_name = request.form.get('new_name', '').strip()
            new_price = request.form.get('new_price', '0')
            new_duration = request.form.get('new_duration', '0')
            if not (new_key and new_name and new_price and new_duration):
                flash('All fields required', 'error')
                return redirect(url_for('admin_plans'))
            try:
                plans = get_plans()
                if new_key in plans:
                    flash(f'Plan "{new_key}" already exists', 'error')
                    return redirect(url_for('admin_plans'))
                plans[new_key] = {'name': new_name, 'price': float(new_price), 'duration': int(new_duration)}
                save_plans(plans)
                flash(f'Plan "{new_name}" added', 'success')
            except ValueError:
                flash('Invalid price/duration', 'error')
            return redirect(url_for('admin_plans'))
        elif action == 'update':
            plans = get_plans()
            count = int(request.form.get('count', 0))
            for i in range(count):
                key = request.form.get(f'key_{i}', '').strip()
                if not key or key not in plans: continue
                try:
                    plans[key] = {
                        'name': request.form.get(f'name_{i}', '').strip(),
                        'price': float(request.form.get(f'price_{i}', 0)),
                        'duration': int(request.form.get(f'duration_{i}', 0))
                    }
                except ValueError:
                    flash(f'Invalid data for {key}', 'error')
                    return redirect(url_for('admin_plans'))
            save_plans(plans)
            flash('Plans updated', 'success')
            return redirect(url_for('admin_plans'))
        elif action == 'delete':
            key = request.form.get('delete_key', '').strip()
            if key:
                plans = get_plans()
                if key in plans:
                    name = plans[key]['name']
                    del plans[key]
                    save_plans(plans)
                    flash(f'Plan "{name}" deleted', 'success')
            return redirect(url_for('admin_plans'))
    plans = get_plans()
    return render_template_string(ADMIN_PLANS_HTML, plans=plans, business_name=BUSINESS_NAME)

@app.route('/admin/change-password', methods=['GET', 'POST'])
@login_required
def admin_change_password():
    if request.method == 'POST':
        current_pw = request.form.get('current_password', '')
        new_pw = request.form.get('new_password', '')
        confirm_pw = request.form.get('confirm_password', '')
        if current_user.password != hashlib.md5(current_pw.encode()).hexdigest():
            flash('Current password is incorrect', 'error')
            return redirect(url_for('admin_change_password'))
        if len(new_pw) < 6:
            flash('New password must be 6+ characters', 'error')
            return redirect(url_for('admin_change_password'))
        if new_pw != confirm_pw:
            flash('Passwords do not match', 'error')
            return redirect(url_for('admin_change_password'))
        current_user.password = hashlib.md5(new_pw.encode()).hexdigest()
        db.session.commit()
        flash('Password changed successfully!', 'success')
        return redirect(url_for('admin_change_password'))
    return render_template_string(ADMIN_CHANGE_PASSWORD_HTML, business_name=BUSINESS_NAME)

@app.route('/admin/toggle/<mac>')
@login_required
def toggle_user(mac):
    c = Customer.query.filter_by(mac_address=mac).first()
    if c:
        if c.is_active and c.expiry_time and c.expiry_time > datetime.utcnow():
            c.is_active = False
            network.block_device(mac)
            flash(f'Access revoked for {mac}', 'warning')
        else:
            c.is_active = True
            c.expiry_time = datetime.utcnow() + timedelta(hours=1)
            network.allow_device(mac)
            flash(f'Access granted for {mac} (1 hour)', 'success')
        db.session.commit()
    return redirect(url_for('admin_customers'))

# ===================================================================
# BACKGROUND JOB
# ===================================================================

def cleanup_expired():
    with app.app_context():
        try:
            expired = Customer.query.filter(
                Customer.is_active == True,
                Customer.expiry_time < datetime.utcnow()
            ).all()
            for c in expired:
                c.is_active = False
                network.block_device(c.mac_address)
            if expired:
                db.session.commit()
        except Exception as e:
            logger.error(f"Cleanup: {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(cleanup_expired, 'interval', minutes=1)
scheduler.start()
logger.info("Scheduler started")

# ===================================================================
# PUBLIC HTML - NO TILL DISPLAYED
# ===================================================================

INDEX_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pay to Connect to {{ business_name }}</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; min-height:100vh; background:#080c1a; padding:20px; display:flex; align-items:center; justify-content:center; }
        .container { max-width:560px; width:100%; background:rgba(14,22,42,0.95); border-radius:28px; padding:36px 32px; border:1px solid rgba(255,255,255,0.06); box-shadow:0 50px 120px rgba(0,0,0,0.8); position:relative; max-height:95vh; overflow-y:auto; }
        .container::before { content:''; position:absolute; top:0; left:0; right:0; height:3px; background:linear-gradient(90deg,#0066ff,#00ccff,#0066ff); border-radius:28px 28px 0 0; }
        .header { text-align:center; margin-bottom:24px; }
        .logo { width:64px; height:64px; background:linear-gradient(145deg,#0066ff,#0044cc); border-radius:18px; display:flex; align-items:center; justify-content:center; margin:0 auto 12px; font-size:28px; box-shadow:0 12px 48px rgba(0,102,255,0.25); }
        .header h1 { font-size:22px; font-weight:800; color:#fff; }
        .header .brand { color:#4a7aff; }
        .header .subtitle { color:#5a6a8f; font-size:13px; margin-top:4px; }
        .section-label { font-size:10px; font-weight:700; color:#5a6a8f; text-transform:uppercase; letter-spacing:0.8px; margin-bottom:10px; }
        .plans { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:20px; }
        .plan { border:1px solid rgba(255,255,255,0.06); border-radius:14px; padding:12px 6px; text-align:center; cursor:pointer; transition:all 0.3s; background:rgba(255,255,255,0.02); position:relative; }
        .plan:hover { border-color:rgba(0,102,255,0.25); background:rgba(0,102,255,0.04); }
        .plan.active { border-color:#0066ff; background:rgba(0,102,255,0.10); }
        .plan.active::after { content:'✓'; position:absolute; top:-5px; right:-5px; width:18px; height:18px; background:#0066ff; border-radius:50%; font-size:10px; color:#fff; display:flex; align-items:center; justify-content:center; }
        .plan .name { font-size:9px; font-weight:700; color:#5a6a8f; text-transform:uppercase; }
        .plan .price { font-size:17px; font-weight:800; color:#fff; margin:2px 0; }
        .plan .duration { font-size:9px; color:#3a4a6f; }
        .plan.popular { border-color:rgba(0,102,255,0.3); }
        .plan.popular::before { content:'⭐ BEST'; position:absolute; top:-8px; left:50%; transform:translateX(-50%); background:linear-gradient(145deg,#0066ff,#0044cc); color:#fff; font-size:7px; font-weight:700; padding:2px 8px; border-radius:10px; }
        .steps { background:rgba(255,255,255,0.02); border-radius:12px; padding:14px; margin-bottom:20px; border:1px solid rgba(255,255,255,0.04); }
        .steps .step { display:flex; gap:10px; padding:6px 0; font-size:12px; color:#8a9bbf; }
        .steps .step .num { width:20px; height:20px; background:rgba(0,102,255,0.15); color:#4a7aff; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:10px; font-weight:700; flex-shrink:0; }
        .form-group { margin-bottom:16px; }
        .form-group label { display:block; font-size:10px; font-weight:700; color:#5a6a8f; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:6px; }
        .form-group input[type=tel], .form-group textarea {
            width:100%; padding:14px 16px;
            border:1px solid rgba(255,255,255,0.06);
            border-radius:12px; font-size:15px;
            background:rgba(255,255,255,0.03); color:#fff;
        }
        .form-group textarea { min-height:100px; font-size:13px; font-family:monospace; resize:vertical; line-height:1.5; }
        .form-group input:focus, .form-group textarea:focus { outline:none; border-color:#0066ff; background:rgba(0,102,255,0.04); }
        .form-group input::placeholder, .form-group textarea::placeholder { color:#2a3a5f; }
        .prefix-wrapper { position:relative; }
        .prefix-wrapper .prefix { position:absolute; left:14px; top:50%; transform:translateY(-50%); color:#3a4a6f; font-weight:600; font-size:14px; }
        .prefix-wrapper input { padding-left:52px !important; }
        .hint-text { font-size:10px; color:#3a4a6f; margin-top:5px; }
        .btn { width:100%; padding:16px; border:none; border-radius:12px; font-size:15px; font-weight:700; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:10px; transition:all 0.3s; }
        .btn-prompt { background:linear-gradient(145deg,#0066ff,#0044cc); color:#fff; }
        .btn-prompt:hover:not(:disabled) { transform:translateY(-2px); box-shadow:0 16px 48px rgba(0,102,255,0.30); }
        .btn-verify { background:linear-gradient(145deg,#10b981,#059669); color:#fff; }
        .btn-verify:hover:not(:disabled) { transform:translateY(-2px); box-shadow:0 16px 48px rgba(16,185,129,0.30); }
        .btn:disabled { opacity:0.5; cursor:not-allowed; }
        .btn .spinner { display:none; width:18px; height:18px; border:2px solid rgba(255,255,255,0.2); border-top:2px solid #fff; border-radius:50%; animation:spin 0.8s linear infinite; }
        .btn.loading .spinner { display:block; }
        .btn.loading .btn-text { display:none; }
        @keyframes spin { to { transform:rotate(360deg); } }
        .status-msg { margin-top:14px; padding:12px 16px; border-radius:12px; display:none; font-size:13px; line-height:1.5; }
        .status-msg.show { display:block; }
        .status-msg.success { background:rgba(16,185,129,0.10); color:#34d399; border:1px solid rgba(16,185,129,0.2); }
        .status-msg.error { background:rgba(239,68,68,0.10); color:#f87171; border:1px solid rgba(239,68,68,0.2); }
        .status-msg.info { background:rgba(0,102,255,0.08); color:#60a5fa; border:1px solid rgba(0,102,255,0.2); }
        .step-section { display:none; }
        .step-section.active { display:block; animation:fadeIn 0.4s ease; }
        @keyframes fadeIn { from { opacity:0; transform:translateY(10px); } to { opacity:1; transform:translateY(0); } }
        .divider { text-align:center; color:#2a3a5f; font-size:11px; margin:20px 0; position:relative; }
        .divider::before, .divider::after { content:''; position:absolute; top:50%; width:40%; height:1px; background:rgba(255,255,255,0.06); }
        .divider::before { left:0; }
        .divider::after { right:0; }
        .footer { text-align:center; margin-top:20px; padding-top:16px; border-top:1px solid rgba(255,255,255,0.04); font-size:10px; color:#1a2a4a; }
        .footer a { color:#4a7aff; text-decoration:none; }
        @media (max-width:480px) { .container { padding:24px 18px; } }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="logo">📶</div>
            <h1>Pay to Connect to <span class="brand">{{ business_name }}</span></h1>
            <p class="subtitle">Get your internet in 2 easy steps</p>
        </div>

        <div class="steps">
            <div class="step"><div class="num">1</div><div>Select a plan and enter your M-Pesa number</div></div>
            <div class="step"><div class="num">2</div><div>Enter your PIN when the prompt appears</div></div>
            <div class="step"><div class="num">3</div><div>Paste the M-Pesa confirmation SMS</div></div>
            <div class="step"><div class="num">4</div><div>You're connected! 🎉</div></div>
        </div>

        <div class="section-label">Choose Your Plan</div>
        <div class="plans" id="planGrid">
            {% for key, plan in plans.items() %}
            <div class="plan {% if key == '7day' %}popular{% endif %}" data-plan="{{ key }}" onclick="selectPlan('{{ key }}')">
                <div class="name">{{ plan.name }}</div>
                <div class="price">KES {{ plan.price }}</div>
                <div class="duration">
                    {% set d = plan.duration // 86400 %}
                    {% set h = (plan.duration % 86400) // 3600 %}
                    {% if d > 0 %}{{ d }}d{% endif %}
                    {% if h > 0 %} {{ h }}h{% endif %}
                    {% if d == 0 and h == 0 %}{{ plan.duration // 60 }}m{% endif %}
                </div>
            </div>
            {% endfor %}
        </div>

        <!-- STEP 1: Send Prompt -->
        <div class="step-section active" id="step1">
            <div class="form-group">
                <label>📱 Your M-Pesa Phone Number</label>
                <div class="prefix-wrapper">
                    <span class="prefix">+254</span>
                    <input type="tel" id="phoneInput" placeholder="712345678">
                </div>
                <div class="hint-text">You'll receive an M-Pesa prompt on this number</div>
            </div>
            <button class="btn btn-prompt" id="promptBtn" onclick="sendPrompt()">
                <span class="btn-text">📲 Send M-Pesa Prompt</span>
                <span class="spinner"></span>
            </button>
        </div>

        <!-- STEP 2: Paste SMS -->
        <div class="step-section" id="step2">
            <div class="divider">▼ Enter PIN on your phone ▼</div>
            <div class="form-group">
                <label>📩 Paste Your M-Pesa Confirmation Message</label>
                <textarea id="mpesaMessage" placeholder="QGH7XYZ123 Confirmed. Ksh18.00 sent to VELARIS..."></textarea>
                <div class="hint-text">🔒 Each M-Pesa code can only be used once</div>
            </div>
            <button class="btn btn-verify" id="verifyBtn" onclick="verifyPayment()">
                <span class="btn-text">✅ Verify &amp; Connect</span>
                <span class="spinner"></span>
            </button>
        </div>

        <div class="status-msg" id="statusMsg"></div>

        <div class="footer">© 2026 {{ business_name }} · <a href="/admin/login">Admin</a></div>
    </div>

    <script>
        var selectedPlan = null;
        var currentCheckoutId = null;
        var isProcessing = false;

        function selectPlan(key) {
            selectedPlan = key;
            var all = document.querySelectorAll('.plan');
            for (var i = 0; i < all.length; i++) all[i].classList.remove('active');
            var el = document.querySelector('.plan[data-plan="' + key + '"]');
            if (el) el.classList.add('active');
            hideStatus();
        }

        function sendPrompt() {
            if (!selectedPlan) { showStatus('Please select a plan first', 'error'); return; }
            var phone = document.getElementById('phoneInput').value.replace(/\\D/g, '');
            if (phone.length < 9) { showStatus('Please enter a valid phone number', 'error'); return; }

            var btn = document.getElementById('promptBtn');
            btn.classList.add('loading'); btn.disabled = true; isProcessing = true;
            showStatus('⏳ Sending M-Pesa prompt...', 'info');

            var fd = new FormData();
            fd.append('phone', phone);
            fd.append('plan', selectedPlan);
            fd.append('mac', '{{ client_mac }}');

            fetch('/send-prompt', { method: 'POST', body: fd })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                btn.classList.remove('loading'); btn.disabled = false; isProcessing = false;
                if (data.success) {
                    currentCheckoutId = data.checkout_id;
                    showStatus('✅ ' + data.message + '\\n\\nAfter entering your PIN, paste the SMS below.', 'success');
                    document.getElementById('step1').classList.remove('active');
                    document.getElementById('step2').classList.add('active');
                } else {
                    showStatus('❌ ' + data.message, 'error');
                }
            })
            .catch(function() {
                btn.classList.remove('loading'); btn.disabled = false; isProcessing = false;
                showStatus('❌ Network error. Try again.', 'error');
            });
        }

        function verifyPayment() {
            var msg = document.getElementById('mpesaMessage').value.trim();
            if (msg.length < 20) { showStatus('Please paste the full M-Pesa message', 'error'); return; }

            var btn = document.getElementById('verifyBtn');
            btn.classList.add('loading'); btn.disabled = true; isProcessing = true;
            showStatus('⏳ Verifying your payment...', 'info');

            var fd = new FormData();
            fd.append('mpesa_message', msg);
            fd.append('plan', selectedPlan);
            fd.append('mac', '{{ client_mac }}');
            if (currentCheckoutId) fd.append('checkout_id', currentCheckoutId);

            fetch('/verify', { method: 'POST', body: fd })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.success) {
                    showStatus('✅ ' + data.message + '\\n\\nYou are now connected!', 'success');
                    setTimeout(function() { window.location.reload(); }, 2500);
                } else {
                    showStatus('❌ ' + data.message, 'error');
                    btn.classList.remove('loading'); btn.disabled = false; isProcessing = false;
                }
            })
            .catch(function() {
                showStatus('❌ Network error. Try again.', 'error');
                btn.classList.remove('loading'); btn.disabled = false; isProcessing = false;
            });
        }

        function showStatus(msg, type) {
            var el = document.getElementById('statusMsg');
            el.textContent = msg;
            el.className = 'status-msg show ' + type;
        }
        function hideStatus() {
            var el = document.getElementById('statusMsg');
            el.className = 'status-msg'; el.textContent = '';
        }
    </script>
</body>
</html>'''

ACTIVE_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Connected to {{ business_name }}</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; min-height:100vh; display:flex; align-items:center; justify-content:center; background:#080c1a; padding:20px; }
        .container { max-width:420px; width:100%; background:rgba(14,22,42,0.95); border-radius:28px; padding:40px 32px; text-align:center; border:1px solid rgba(255,255,255,0.06); box-shadow:0 50px 120px rgba(0,0,0,0.8); position:relative; }
        .container::before { content:''; position:absolute; top:0; left:0; right:0; height:3px; background:linear-gradient(90deg,#0066ff,#00ccff,#0066ff); border-radius:28px 28px 0 0; }
        .status-icon { width:80px; height:80px; background:rgba(16,185,129,0.12); border-radius:50%; display:flex; align-items:center; justify-content:center; margin:0 auto 20px; font-size:40px; border:2px solid rgba(16,185,129,0.2); }
        h2 { color:#fff; margin-bottom:4px; font-size:24px; }
        .sub { color:#5a6a8f; font-size:14px; }
        .info { background:rgba(255,255,255,0.02); border-radius:16px; padding:20px; margin:24px 0; text-align:left; border:1px solid rgba(255,255,255,0.04); }
        .info .row { display:flex; justify-content:space-between; padding:8px 0; border-bottom:1px solid rgba(255,255,255,0.04); font-size:14px; }
        .info .row:last-child { border-bottom:none; }
        .info .label { color:#5a6a8f; }
        .info .value { font-weight:600; color:#fff; }
        .timer { font-size:42px; font-weight:700; color:#0066ff; }
        .btn { display:inline-block; padding:12px 32px; background:rgba(255,255,255,0.05); color:#8a9bbf; border:1px solid rgba(255,255,255,0.06); border-radius:12px; text-decoration:none; font-size:14px; font-weight:600; margin-top:16px; }
        .footer { text-align:center; margin-top:20px; font-size:10px; color:#1a2a4a; }
    </style>
</head>
<body>
    <div class="container">
        <div class="status-icon">✅</div>
        <h2>You're Connected!</h2>
        <p class="sub">Welcome to {{ business_name }}</p>
        <div class="info">
            <div class="row"><span class="label">Plan</span><span class="value">{{ customer.plan }}</span></div>
            <div class="row"><span class="label">Time Remaining</span><span class="value timer" id="timer">{{ remaining }}</span></div>
        </div>
        <a href="/" class="btn">↻ Refresh</a>
        <div class="footer">Auto-refreshes every 30 seconds</div>
    </div>
    <script>
        var seconds = {{ remaining_seconds|int }};
        var timerEl = document.getElementById('timer');
        function formatTime(s) {
            if (s <= 0) return 'Expired';
            var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
            if (d > 0) return d+'d '+h+'h '+m+'m';
            if (h > 0) return h+'h '+m+'m '+sec+'s';
            if (m > 0) return m+'m '+sec+'s';
            return sec+'s';
        }
        setInterval(function() {
            seconds--;
            if (seconds <= 0) { timerEl.textContent = 'Expired'; setTimeout(function(){ window.location.reload(); }, 3000); }
            else { timerEl.textContent = formatTime(seconds); }
        }, 1000);
        setTimeout(function() { window.location.reload(); }, 30000);
    </script>
</body>
</html>'''

ADMIN_LOGIN_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Login - {{ business_name }}</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; min-height:100vh; display:flex; align-items:center; justify-content:center; background:#080c1a; padding:20px; }
        .container { max-width:380px; width:100%; background:rgba(14,22,42,0.95); border-radius:24px; padding:40px 32px; border:1px solid rgba(255,255,255,0.06); box-shadow:0 50px 120px rgba(0,0,0,0.8); position:relative; }
        .container::before { content:''; position:absolute; top:0; left:0; right:0; height:3px; background:linear-gradient(90deg,#0066ff,#00ccff,#0066ff); border-radius:24px 24px 0 0; }
        h2 { text-align:center; color:#fff; margin-bottom:4px; font-size:22px; }
        .sub { text-align:center; color:#5a6a8f; font-size:13px; margin-bottom:24px; }
        .input-group { margin-bottom:16px; }
        .input-group label { display:block; font-size:10px; font-weight:700; color:#5a6a8f; text-transform:uppercase; margin-bottom:4px; }
        .input-group input { width:100%; padding:14px 16px; border:1px solid rgba(255,255,255,0.06); border-radius:12px; font-size:15px; background:rgba(255,255,255,0.03); color:#fff; }
        .input-group input:focus { outline:none; border-color:#0066ff; }
        .btn { width:100%; padding:14px; background:linear-gradient(145deg,#0066ff,#0044cc); color:#fff; border:none; border-radius:12px; font-size:15px; font-weight:700; cursor:pointer; }
        .btn:hover { transform:translateY(-2px); box-shadow:0 12px 40px rgba(0,102,255,0.3); }
        .flash { background:rgba(239,68,68,0.10); color:#f87171; padding:12px; border-radius:10px; font-size:13px; margin-bottom:16px; }
        .footer { text-align:center; margin-top:16px; font-size:11px; color:#1a2a4a; }
        .footer a { color:#4a7aff; text-decoration:none; }
    </style>
</head>
<body>
    <div class="container">
        <h2>🔐 Admin</h2>
        <p class="sub">{{ business_name }} Control Panel</p>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}{% for c, m in messages %}<div class="flash">{{ m }}</div>{% endfor %}{% endif %}
        {% endwith %}
        <form method="POST">
            <div class="input-group"><label>Username</label><input type="text" name="username" value="admin" required></div>
            <div class="input-group"><label>Password</label><input type="password" name="password" value="admin123" required></div>
            <button type="submit" class="btn">Sign In</button>
        </form>
        <div class="footer">Default: admin / admin123 · <a href="/">← Back</a></div>
    </div>
</body>
</html>'''

def admin_sidebar(active):
    items = [
        ('/admin', '📊', 'Dashboard'),
        ('/admin/customers', '👤', 'Customers'),
        ('/admin/transactions', '💳', 'M-Pesa Codes'),
        ('/admin/plans', '⚙️', 'Plans'),
        ('/admin/change-password', '🔑', 'Password'),
        ('/admin/logout', '🚪', 'Logout'),
    ]
    html = '<div class="sidebar"><div class="brand">📶 <span>VELARIS</span></div><div class="nav">'
    for url, icon, label in items:
        cls = 'active' if url == active else ''
        html += f'<a href="{url}" class="{cls}">{icon} <span>{label}</span></a>'
    html += '</div></div>'
    return html

ADMIN_DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard - {{ business_name }}</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background:#080c1a; display:flex; min-height:100vh; }
        .sidebar { width:200px; background:rgba(14,22,42,0.95); border-right:1px solid rgba(255,255,255,0.05); padding:24px 0; min-height:100vh; position:fixed; top:0; left:0; bottom:0; }
        .sidebar .brand { padding:0 24px 24px; font-size:18px; font-weight:700; color:#fff; border-bottom:1px solid rgba(255,255,255,0.05); }
        .sidebar .brand span { color:#4a7aff; }
        .sidebar .nav { padding:16px 0; }
        .sidebar .nav a { display:flex; align-items:center; gap:12px; padding:12px 24px; color:rgba(255,255,255,0.5); text-decoration:none; font-size:14px; }
        .sidebar .nav a:hover, .sidebar .nav a.active { color:#fff; background:rgba(0,102,255,0.06); }
        .sidebar .nav a.active { border-right:3px solid #0066ff; color:#fff; }
        .main { margin-left:200px; padding:24px 32px; flex:1; }
        .top { display:flex; justify-content:space-between; align-items:center; background:rgba(14,22,42,0.95); padding:16px 24px; border-radius:16px; margin-bottom:24px; border:1px solid rgba(255,255,255,0.05); }
        .top h2 { color:#fff; font-size:20px; }
        .top .user { display:flex; align-items:center; gap:12px; color:#8a9bbf; font-size:14px; }
        .top .user .avatar { width:36px; height:36px; background:linear-gradient(145deg,#0066ff,#0044cc); border-radius:50%; display:flex; align-items:center; justify-content:center; color:#fff; font-weight:600; }
        .top .user a { color:#4a7aff; text-decoration:none; margin-left:8px; }
        .stats { display:grid; grid-template-columns:repeat(4,1fr); gap:16px; margin-bottom:24px; }
        .stat { background:rgba(14,22,42,0.95); padding:20px 24px; border-radius:16px; border:1px solid rgba(255,255,255,0.05); }
        .stat .num { font-size:28px; font-weight:700; color:#fff; }
        .stat .label { font-size:13px; color:#5a6a8f; margin-top:2px; }
        .stat .icon { float:right; font-size:28px; opacity:0.3; }
        .card { background:rgba(14,22,42,0.95); border-radius:16px; padding:20px 24px; border:1px solid rgba(255,255,255,0.05); margin-bottom:24px; }
        .card h3 { font-size:16px; margin-bottom:16px; color:#fff; }
        .grid-2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
        .status-list { max-height:300px; overflow-y:auto; }
        .status-item { display:flex; justify-content:space-between; padding:8px 0; border-bottom:1px solid rgba(255,255,255,0.03); font-size:13px; color:#8a9bbf; }
        .status-item:last-child { border-bottom:none; }
        .mac { font-family:monospace; font-size:12px; color:#5a6a8f; }
        .code { font-family:monospace; color:#4a7aff; }
        .empty { color:#3a4a6f; text-align:center; padding:20px; }
        @media (max-width:768px) {
            .sidebar { width:60px; } .sidebar .brand { padding:0 12px 16px; font-size:14px; }
            .sidebar .nav a { padding:12px 16px; font-size:12px; } .sidebar .nav a span { display:none; }
            .main { margin-left:60px; padding:16px; } .stats { grid-template-columns:1fr 1fr; } .grid-2 { grid-template-columns:1fr; }
        }
    </style>
</head>
<body>
''' + admin_sidebar('/admin') + '''
    <div class="main">
        <div class="top">
            <h2>📊 Dashboard</h2>
            <div class="user"><span>Admin</span><div class="avatar">A</div><a href="/admin/logout">Logout</a></div>
        </div>
        <div class="stats">
            <div class="stat"><span class="icon">👤</span><div class="num">{{ total_customers }}</div><div class="label">Total Customers</div></div>
            <div class="stat"><span class="icon">🟢</span><div class="num">{{ active_customers }}</div><div class="label">Active Now</div></div>
            <div class="stat"><span class="icon">💳</span><div class="num">{{ total_transactions }}</div><div class="label">Codes Used</div></div>
            <div class="stat"><span class="icon">💰</span><div class="num">KES {{ "%.0f"|format(total_revenue) }}</div><div class="label">Total Revenue</div></div>
        </div>
        <div class="grid-2">
            <div class="card">
                <h3>🟢 Active Sessions</h3>
                <div class="status-list">
                    {% if active_sessions %}{% for s in active_sessions %}
                    <div class="status-item">
                        <span class="mac">{{ s.mac_address[:17] }}</span>
                        <span style="color:#4a7aff;">{{ s.plan }}</span>
                        <span style="font-size:12px;">{{ ((s.expiry_time - now).total_seconds() // 60)|int }}m left</span>
                    </div>
                    {% endfor %}{% else %}<div class="empty">No active sessions</div>{% endif %}
                </div>
            </div>
            <div class="card">
                <h3>💳 Recent M-Pesa Codes</h3>
                <div class="status-list">
                    {% if recent_transactions %}{% for t in recent_transactions[:5] %}
                    <div class="status-item">
                        <span class="code">{{ t.mpesa_code }}</span>
                        <span>KES {{ "%.0f"|format(t.amount) }}</span>
                        <span style="font-size:11px;color:#3a4a6f;">{{ t.created_at.strftime('%H:%M') }}</span>
                    </div>
                    {% endfor %}{% else %}<div class="empty">No codes yet</div>{% endif %}
                </div>
            </div>
        </div>
    </div>
</body>
</html>'''

ADMIN_CUSTOMERS_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Customers - {{ business_name }}</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background:#080c1a; display:flex; min-height:100vh; }
        .sidebar { width:200px; background:rgba(14,22,42,0.95); border-right:1px solid rgba(255,255,255,0.05); padding:24px 0; min-height:100vh; position:fixed; top:0; left:0; bottom:0; }
        .sidebar .brand { padding:0 24px 24px; font-size:18px; font-weight:700; color:#fff; border-bottom:1px solid rgba(255,255,255,0.05); }
        .sidebar .brand span { color:#4a7aff; }
        .sidebar .nav { padding:16px 0; }
        .sidebar .nav a { display:flex; align-items:center; gap:12px; padding:12px 24px; color:rgba(255,255,255,0.5); text-decoration:none; font-size:14px; }
        .sidebar .nav a:hover, .sidebar .nav a.active { color:#fff; background:rgba(0,102,255,0.06); }
        .sidebar .nav a.active { border-right:3px solid #0066ff; color:#fff; }
        .main { margin-left:200px; padding:24px 32px; flex:1; }
        .top { display:flex; justify-content:space-between; align-items:center; background:rgba(14,22,42,0.95); padding:16px 24px; border-radius:16px; margin-bottom:24px; border:1px solid rgba(255,255,255,0.05); }
        .top h2 { color:#fff; font-size:20px; }
        .top .user { display:flex; align-items:center; gap:12px; color:#8a9bbf; font-size:14px; }
        .top .user .avatar { width:36px; height:36px; background:linear-gradient(145deg,#0066ff,#0044cc); border-radius:50%; display:flex; align-items:center; justify-content:center; color:#fff; font-weight:600; }
        .top .user a { color:#4a7aff; text-decoration:none; margin-left:8px; }
        .card { background:rgba(14,22,42,0.95); border-radius:16px; padding:24px; border:1px solid rgba(255,255,255,0.05); overflow-x:auto; }
        table { width:100%; border-collapse:collapse; font-size:14px; color:#8a9bbf; }
        table th { text-align:left; padding:12px 10px; color:#5a6a8f; font-weight:600; border-bottom:2px solid rgba(255,255,255,0.05); }
        table td { padding:12px 10px; border-bottom:1px solid rgba(255,255,255,0.03); }
        .badge { padding:4px 12px; border-radius:20px; font-size:11px; font-weight:600; }
        .badge.active { background:rgba(16,185,129,0.12); color:#34d399; }
        .badge.inactive { background:rgba(239,68,68,0.12); color:#f87171; }
        .btn-sm { padding:4px 12px; border-radius:6px; border:none; font-size:12px; font-weight:600; cursor:pointer; text-decoration:none; display:inline-block; }
        .btn-toggle-on { background:rgba(16,185,129,0.15); color:#34d399; }
        .btn-toggle-off { background:rgba(239,68,68,0.15); color:#f87171; }
        .mac { font-family:monospace; font-size:12px; color:#6a7a9f; }
        .empty { text-align:center; color:#3a4a6f; padding:40px; }
        @media (max-width:768px) {
            .sidebar { width:60px; } .sidebar .brand { padding:0 12px 16px; font-size:14px; }
            .sidebar .nav a { padding:12px 16px; font-size:12px; } .sidebar .nav a span { display:none; }
            .main { margin-left:60px; padding:16px; }
        }
    </style>
</head>
<body>
''' + admin_sidebar('/admin/customers') + '''
    <div class="main">
        <div class="top">
            <h2>👤 Customers</h2>
            <div class="user"><span>Admin</span><div class="avatar">A</div><a href="/admin/logout">Logout</a></div>
        </div>
        <div class="card">
            {% if customers %}
            <table>
                <thead><tr><th>MAC</th><th>Phone</th><th>Plan</th><th>Expires</th><th>Status</th><th>Action</th></tr></thead>
                <tbody>
                    {% for c in customers %}
                    <tr>
                        <td><span class="mac">{{ c.mac_address[:17] }}</span></td>
                        <td>{% if c.phone_number and c.phone_number != 'unknown' %}0{{ c.phone_number[-9:] }}{% else %}—{% endif %}</td>
                        <td>{{ c.plan or '—' }}</td>
                        <td>{% if c.expiry_time %}{{ c.expiry_time.strftime('%d/%m %H:%M') }} <span style="font-size:11px;color:#3a4a6f;">({{ ((c.expiry_time - now).total_seconds() // 60)|int }}m)</span>{% else %}—{% endif %}</td>
                        <td>{% if c.is_active and c.expiry_time and c.expiry_time > now %}<span class="badge active">Active</span>{% else %}<span class="badge inactive">Inactive</span>{% endif %}</td>
                        <td><a href="/admin/toggle/{{ c.mac_address }}" class="btn-sm {% if c.is_active and c.expiry_time and c.expiry_time > now %}btn-toggle-off{% else %}btn-toggle-on{% endif %}">{% if c.is_active and c.expiry_time and c.expiry_time > now %}Block{% else %}Allow{% endif %}</a></td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}<div class="empty">No customers yet</div>{% endif %}
        </div>
    </div>
</body>
</html>'''

ADMIN_TRANSACTIONS_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>M-Pesa Codes - {{ business_name }}</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background:#080c1a; display:flex; min-height:100vh; }
        .sidebar { width:200px; background:rgba(14,22,42,0.95); border-right:1px solid rgba(255,255,255,0.05); padding:24px 0; min-height:100vh; position:fixed; top:0; left:0; bottom:0; }
        .sidebar .brand { padding:0 24px 24px; font-size:18px; font-weight:700; color:#fff; border-bottom:1px solid rgba(255,255,255,0.05); }
        .sidebar .brand span { color:#4a7aff; }
        .sidebar .nav { padding:16px 0; }
        .sidebar .nav a { display:flex; align-items:center; gap:12px; padding:12px 24px; color:rgba(255,255,255,0.5); text-decoration:none; font-size:14px; }
        .sidebar .nav a:hover, .sidebar .nav a.active { color:#fff; background:rgba(0,102,255,0.06); }
        .sidebar .nav a.active { border-right:3px solid #0066ff; color:#fff; }
        .main { margin-left:200px; padding:24px 32px; flex:1; }
        .top { display:flex; justify-content:space-between; align-items:center; background:rgba(14,22,42,0.95); padding:16px 24px; border-radius:16px; margin-bottom:24px; border:1px solid rgba(255,255,255,0.05); }
        .top h2 { color:#fff; font-size:20px; }
        .top .user { display:flex; align-items:center; gap:12px; color:#8a9bbf; font-size:14px; }
        .top .user .avatar { width:36px; height:36px; background:linear-gradient(145deg,#0066ff,#0044cc); border-radius:50%; display:flex; align-items:center; justify-content:center; color:#fff; font-weight:600; }
        .top .user a { color:#4a7aff; text-decoration:none; margin-left:8px; }
        .card { background:rgba(14,22,42,0.95); border-radius:16px; padding:24px; border:1px solid rgba(255,255,255,0.05); overflow-x:auto; }
        table { width:100%; border-collapse:collapse; font-size:14px; color:#8a9bbf; }
        table th { text-align:left; padding:12px 10px; color:#5a6a8f; font-weight:600; border-bottom:2px solid rgba(255,255,255,0.05); }
        table td { padding:12px 10px; border-bottom:1px solid rgba(255,255,255,0.03); }
        .code { font-family:monospace; color:#4a7aff; font-weight:600; }
        .empty { text-align:center; color:#3a4a6f; padding:40px; }
        .amount { font-weight:600; color:#fff; }
        @media (max-width:768px) {
            .sidebar { width:60px; } .sidebar .brand { padding:0 12px 16px; font-size:14px; }
            .sidebar .nav a { padding:12px 16px; font-size:12px; } .sidebar .nav a span { display:none; }
            .main { margin-left:60px; padding:16px; }
        }
    </style>
</head>
<body>
''' + admin_sidebar('/admin/transactions') + '''
    <div class="main">
        <div class="top">
            <h2>💳 Used M-Pesa Codes</h2>
            <div class="user"><span>Admin</span><div class="avatar">A</div><a href="/admin/logout">Logout</a></div>
        </div>
        <div class="card">
            {% if transactions %}
            <table>
                <thead><tr><th>Code</th><th>Phone</th><th>Amount</th><th>Plan</th><th>Used At</th></tr></thead>
                <tbody>
                    {% for t in transactions %}
                    <tr>
                        <td><span class="code">{{ t.mpesa_code }}</span></td>
                        <td>{% if t.phone_number and t.phone_number != 'unknown' %}0{{ t.phone_number[-9:] }}{% else %}—{% endif %}</td>
                        <td class="amount">KES {{ "%.0f"|format(t.amount) }}</td>
                        <td>{{ t.plan or '—' }}</td>
                        <td style="font-size:12px;color:#3a4a6f;">{{ t.created_at.strftime('%d/%m %H:%M') }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}<div class="empty">No codes used yet</div>{% endif %}
        </div>
    </div>
</body>
</html>'''

ADMIN_PLANS_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Plans - {{ business_name }}</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background:#080c1a; display:flex; min-height:100vh; }
        .sidebar { width:200px; background:rgba(14,22,42,0.95); border-right:1px solid rgba(255,255,255,0.05); padding:24px 0; min-height:100vh; position:fixed; top:0; left:0; bottom:0; }
        .sidebar .brand { padding:0 24px 24px; font-size:18px; font-weight:700; color:#fff; border-bottom:1px solid rgba(255,255,255,0.05); }
        .sidebar .brand span { color:#4a7aff; }
        .sidebar .nav { padding:16px 0; }
        .sidebar .nav a { display:flex; align-items:center; gap:12px; padding:12px 24px; color:rgba(255,255,255,0.5); text-decoration:none; font-size:14px; }
        .sidebar .nav a:hover, .sidebar .nav a.active { color:#fff; background:rgba(0,102,255,0.06); }
        .sidebar .nav a.active { border-right:3px solid #0066ff; color:#fff; }
        .main { margin-left:200px; padding:24px 32px; flex:1; }
        .top { display:flex; justify-content:space-between; align-items:center; background:rgba(14,22,42,0.95); padding:16px 24px; border-radius:16px; margin-bottom:24px; border:1px solid rgba(255,255,255,0.05); }
        .top h2 { color:#fff; font-size:20px; }
        .top .user { display:flex; align-items:center; gap:12px; color:#8a9bbf; font-size:14px; }
        .top .user .avatar { width:36px; height:36px; background:linear-gradient(145deg,#0066ff,#0044cc); border-radius:50%; display:flex; align-items:center; justify-content:center; color:#fff; font-weight:600; }
        .top .user a { color:#4a7aff; text-decoration:none; margin-left:8px; }
        .card { background:rgba(14,22,42,0.95); border-radius:16px; padding:24px; border:1px solid rgba(255,255,255,0.05); margin-bottom:24px; }
        .card h3 { color:#fff; margin-bottom:20px; font-size:16px; }
        .plan-grid { display:grid; grid-template-columns: 1fr 1.2fr 1fr 1.2fr auto; gap:12px; align-items:center; padding:12px 0; border-bottom:1px solid rgba(255,255,255,0.03); }
        .plan-grid.header { color:#5a6a8f; font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:0.5px; padding-bottom:10px; }
        .plan-grid input { padding:10px 12px; border:1px solid rgba(255,255,255,0.06); border-radius:8px; font-size:14px; background:rgba(255,255,255,0.03); color:#fff; width:100%; }
        .plan-grid input:focus { outline:none; border-color:#0066ff; background:rgba(0,102,255,0.04); }
        .plan-grid input.key-field { font-family:monospace; font-size:12px; color:#5a6a8f; }
        .duration-hint { font-size:11px; color:#3a4a6f; }
        .btn { padding:10px 22px; border:none; border-radius:8px; font-size:13px; font-weight:600; cursor:pointer; transition:all 0.3s; }
        .btn-primary { background:linear-gradient(145deg,#0066ff,#0044cc); color:#fff; }
        .btn-primary:hover { transform:translateY(-1px); box-shadow:0 8px 24px rgba(0,102,255,0.2); }
        .btn-success { background:rgba(16,185,129,0.15); color:#34d399; border:1px solid rgba(16,185,129,0.2); }
        .btn-success:hover { background:rgba(16,185,129,0.25); }
        .btn-danger { background:rgba(239,68,68,0.15); color:#f87171; border:1px solid rgba(239,68,68,0.2); padding:10px 14px; }
        .btn-danger:hover { background:rgba(239,68,68,0.25); }
        .flash { padding:14px 18px; border-radius:10px; margin-bottom:16px; font-size:14px; }
        .flash.error { background:rgba(239,68,68,0.10); color:#f87171; border:1px solid rgba(239,68,68,0.2); }
        .flash.success { background:rgba(16,185,129,0.10); color:#34d399; border:1px solid rgba(16,185,129,0.2); }
        .add-form { display:grid; grid-template-columns: 1fr 1.2fr 1fr 1.2fr auto; gap:12px; align-items:end; }
        .add-form .field { display:flex; flex-direction:column; gap:6px; }
        .add-form .field label { font-size:10px; color:#5a6a8f; font-weight:700; text-transform:uppercase; letter-spacing:0.5px; }
        .add-form .field input { padding:12px 14px; border:1px solid rgba(255,255,255,0.06); border-radius:10px; font-size:14px; background:rgba(255,255,255,0.03); color:#fff; }
        .add-form .field input:focus { outline:none; border-color:#0066ff; background:rgba(0,102,255,0.04); }
        .empty { color:#3a4a6f; text-align:center; padding:40px; font-size:14px; }
        .note { background:rgba(0,102,255,0.06); border:1px solid rgba(0,102,255,0.15); border-radius:10px; padding:12px 16px; font-size:12px; color:#60a5fa; margin-bottom:20px; }
        hr { margin:28px 0; border-color:rgba(255,255,255,0.05); }
        @media (max-width:900px) {
            .plan-grid { grid-template-columns: 1fr 1fr; gap:8px; }
            .plan-grid.header { display:none; }
            .add-form { grid-template-columns: 1fr; }
        }
        @media (max-width:768px) {
            .sidebar { width:60px; } .sidebar .brand { padding:0 12px 16px; font-size:14px; }
            .sidebar .nav a { padding:12px 16px; font-size:12px; } .sidebar .nav a span { display:none; }
            .main { margin-left:60px; padding:16px; }
        }
    </style>
</head>
<body>
''' + admin_sidebar('/admin/plans') + '''
    <div class="main">
        <div class="top">
            <h2>⚙️ Pricing Plans</h2>
            <div class="user"><span>Admin</span><div class="avatar">A</div><a href="/admin/logout">Logout</a></div>
        </div>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}{% for c, m in messages %}<div class="flash {{ c }}">{{ m }}</div>{% endfor %}{% endif %}
        {% endwith %}
        <div class="card">
            <h3>📋 Your Plans ({{ plans|length }})</h3>
            <div class="note">💡 Edit prices and click "Save All Changes". Plans stay safe.</div>
            {% if plans %}
            <form method="POST">
                <input type="hidden" name="action" value="update">
                <input type="hidden" name="count" value="{{ plans|length }}">
                <div class="plan-grid header">
                    <span>Key</span><span>Display Name</span><span>Price (KES)</span><span>Duration (sec)</span><span></span>
                </div>
                {% for key, plan in plans.items() %}
                <div class="plan-grid">
                    <input type="text" name="key_{{ loop.index0 }}" value="{{ key }}" class="key-field" readonly>
                    <input type="text" name="name_{{ loop.index0 }}" value="{{ plan.name }}">
                    <input type="number" name="price_{{ loop.index0 }}" value="{{ plan.price }}" step="1" min="0">
                    <input type="number" name="duration_{{ loop.index0 }}" value="{{ plan.duration }}" step="60" min="60">
                    <span class="duration-hint">
                        {% set d = plan.duration // 86400 %}
                        {% set h = (plan.duration % 86400) // 3600 %}
                        {% set m = (plan.duration % 3600) // 60 %}
                        {% if d > 0 %}{{ d }}d{% endif %}{% if h > 0 %} {{ h }}h{% endif %}{% if m > 0 and d == 0 %}{{ m }}m{% endif %}
                    </span>
                </div>
                {% endfor %}
                <div style="margin-top:20px;"><button type="submit" class="btn btn-primary">💾 Save All Changes</button></div>
            </form>
            <hr>
            <h3 style="color:#fff;margin-bottom:16px;font-size:14px;">🗑️ Delete a Plan</h3>
            <div style="display:flex;gap:8px;flex-wrap:wrap;">
                {% for key, plan in plans.items() %}
                <form method="POST" style="display:inline;" onsubmit="return confirm('Delete {{ plan.name }}?');">
                    <input type="hidden" name="action" value="delete">
                    <input type="hidden" name="delete_key" value="{{ key }}">
                    <button type="submit" class="btn btn-danger">❌ {{ plan.name }}</button>
                </form>
                {% endfor %}
            </div>
            {% else %}<div class="empty">No plans yet.</div>{% endif %}
        </div>
        <div class="card">
            <h3>➕ Add New Plan</h3>
            <form method="POST">
                <input type="hidden" name="action" value="add">
                <div class="add-form">
                    <div class="field"><label>Key</label><input type="text" name="new_key" placeholder="2hr" required></div>
                    <div class="field"><label>Display Name</label><input type="text" name="new_name" placeholder="2 Hours" required></div>
                    <div class="field"><label>Price (KES)</label><input type="number" name="new_price" placeholder="35" step="1" min="0" required></div>
                    <div class="field"><label>Duration (sec)</label><input type="number" name="new_duration" placeholder="7200" step="60" min="60" required></div>
                    <button type="submit" class="btn btn-success">+ Add Plan</button>
                </div>
            </form>
        </div>
    </div>
</body>
</html>'''

ADMIN_CHANGE_PASSWORD_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Change Password - {{ business_name }}</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background:#080c1a; display:flex; min-height:100vh; }
        .sidebar { width:200px; background:rgba(14,22,42,0.95); border-right:1px solid rgba(255,255,255,0.05); padding:24px 0; min-height:100vh; position:fixed; top:0; left:0; bottom:0; }
        .sidebar .brand { padding:0 24px 24px; font-size:18px; font-weight:700; color:#fff; border-bottom:1px solid rgba(255,255,255,0.05); }
        .sidebar .brand span { color:#4a7aff; }
        .sidebar .nav { padding:16px 0; }
        .sidebar .nav a { display:flex; align-items:center; gap:12px; padding:12px 24px; color:rgba(255,255,255,0.5); text-decoration:none; font-size:14px; }
        .sidebar .nav a:hover, .sidebar .nav a.active { color:#fff; background:rgba(0,102,255,0.06); }
        .sidebar .nav a.active { border-right:3px solid #0066ff; color:#fff; }
        .main { margin-left:200px; padding:24px 32px; flex:1; }
        .top { display:flex; justify-content:space-between; align-items:center; background:rgba(14,22,42,0.95); padding:16px 24px; border-radius:16px; margin-bottom:24px; border:1px solid rgba(255,255,255,0.05); }
        .top h2 { color:#fff; font-size:20px; }
        .top .user { display:flex; align-items:center; gap:12px; color:#8a9bbf; font-size:14px; }
        .top .user .avatar { width:36px; height:36px; background:linear-gradient(145deg,#0066ff,#0044cc); border-radius:50%; display:flex; align-items:center; justify-content:center; color:#fff; font-weight:600; }
        .top .user a { color:#4a7aff; text-decoration:none; margin-left:8px; }
        .card { background:rgba(14,22,42,0.95); border-radius:16px; padding:32px; border:1px solid rgba(255,255,255,0.05); max-width:500px; }
        .card h3 { color:#fff; margin-bottom:24px; font-size:18px; }
        .input-group { margin-bottom:20px; }
        .input-group label { display:block; font-size:11px; font-weight:700; color:#5a6a8f; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:6px; }
        .input-group input { width:100%; padding:14px 16px; border:1px solid rgba(255,255,255,0.06); border-radius:12px; font-size:15px; background:rgba(255,255,255,0.03); color:#fff; }
        .input-group input:focus { outline:none; border-color:#0066ff; background:rgba(0,102,255,0.04); }
        .btn { padding:14px 32px; background:linear-gradient(145deg,#0066ff,#0044cc); color:#fff; border:none; border-radius:12px; font-size:15px; font-weight:700; cursor:pointer; }
        .btn:hover { transform:translateY(-2px); box-shadow:0 12px 40px rgba(0,102,255,0.3); }
        .flash { padding:14px 18px; border-radius:12px; margin-bottom:20px; font-size:14px; }
        .flash.error { background:rgba(239,68,68,0.10); color:#f87171; border:1px solid rgba(239,68,68,0.2); }
        .flash.success { background:rgba(16,185,129,0.10); color:#34d399; border:1px solid rgba(16,185,129,0.2); }
        .hint { font-size:12px; color:#5a6a8f; margin-top:8px; }
        @media (max-width:768px) {
            .sidebar { width:60px; } .sidebar .brand { padding:0 12px 16px; font-size:14px; }
            .sidebar .nav a { padding:12px 16px; font-size:12px; } .sidebar .nav a span { display:none; }
            .main { margin-left:60px; padding:16px; }
        }
    </style>
</head>
<body>
''' + admin_sidebar('/admin/change-password') + '''
    <div class="main">
        <div class="top">
            <h2>🔑 Change Password</h2>
            <div class="user"><span>Admin</span><div class="avatar">A</div><a href="/admin/logout">Logout</a></div>
        </div>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}{% for c, m in messages %}<div class="flash {{ c }}">{{ m }}</div>{% endfor %}{% endif %}
        {% endwith %}
        <div class="card">
            <h3>Update Admin Password</h3>
            <form method="POST">
                <div class="input-group"><label>Current Password</label><input type="password" name="current_password" required></div>
                <div class="input-group">
                    <label>New Password</label>
                    <input type="password" name="new_password" required minlength="6">
                    <div class="hint">Minimum 6 characters</div>
                </div>
                <div class="input-group"><label>Confirm New Password</label><input type="password" name="confirm_password" required minlength="6"></div>
                <button type="submit" class="btn">💾 Update Password</button>
            </form>
        </div>
    </div>
</body>
</html>'''

# ===================================================================
# STARTUP
# ===================================================================

with app.app_context():
    db.create_all()
    if not User.query.first():
        admin = User(
            username=ADMIN_USERNAME,
            password=hashlib.md5(ADMIN_PASSWORD.encode()).hexdigest()
        )
        db.session.add(admin)
        db.session.commit()
        logger.info(f"Admin created: {ADMIN_USERNAME} / {ADMIN_PASSWORD}")
    if not Setting.query.filter_by(key='plans').first():
        save_plans(DEFAULT_PLANS)
        logger.info("Default plans added")
    logger.info("Database ready")

# ===================================================================
# MAIN
# ===================================================================

if __name__ == '__main__':
    print(f"""
    ╔═══════════════════════════════════════════════════════════════════╗
    ║   📶  {BUSINESS_NAME} - Pay to Connect                            ║
    ║                                                                   ║
    ║   🌐 Portal:    http://localhost:{PORT}                          ║
    ║   🔐 Admin:     http://localhost:{PORT}/admin                    ║
    ║   👤 Login:     {ADMIN_USERNAME} / {ADMIN_PASSWORD}              ║
    ║                                                                   ║
    ║   📲 Mode:      {"TEST (simulated)" if MPESA_TEST_MODE else "LIVE STK Push"}           ║
    ║                                                                   ║
    ║   Press CTRL+C to stop                                           ║
    ╚═══════════════════════════════════════════════════════════════════╝
    """)
    try:
        app.run(host=HOST, port=PORT, debug=DEBUG_MODE)
    except KeyboardInterrupt:
        print("\n[✓] Server stopped")
        sys.exit(0)