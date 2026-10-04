#!/usr/bin/env python3
"""
================================================================================
PAY & CONNECT - WiFi Billing System (M-Pesa SMS Verification)
Complete Single File - Paste M-Pesa Message to Connect
================================================================================
How it works:
1. Customer sends money to Till 1671404 manually via M-Pesa
2. Customer receives M-Pesa confirmation SMS
3. Customer pastes the SMS on the portal
4. System extracts the code, verifies amount matches plan
5. System checks code hasn't been used before
6. Access granted automatically
================================================================================
"""

import os
import sys
import json
import hashlib
import re
import subprocess
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from apscheduler.schedulers.background import BackgroundScheduler

# ===================================================================
# CONFIGURATION
# ===================================================================

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"

# M-Pesa Till Number (customers send money here)
MPESA_TILL_NUMBER = "1671404"

SECRET_KEY = "your-super-secret-key-change-this"
DEBUG_MODE = True
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
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///wifi_billing.db'
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
    email = db.Column(db.String(120))
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
    mpesa_code = db.Column(db.String(20), unique=True, nullable=False)  # The M-Pesa receipt code
    phone_number = db.Column(db.String(15))
    amount = db.Column(db.Float, nullable=False)
    plan = db.Column(db.String(50))
    customer_mac = db.Column(db.String(17))
    status = db.Column(db.String(20), default='verified')  # verified, used, rejected
    raw_message = db.Column(db.Text)  # The full SMS pasted
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    used_at = db.Column(db.DateTime)

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
    """Get client MAC address from ARP table"""
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
    if days > 0:
        parts.append(f"{int(days)}d")
    if hours > 0:
        parts.append(f"{int(hours)}h")
    if minutes > 0 and days == 0:
        parts.append(f"{int(minutes)}m")
    return " ".join(parts) if parts else "0s"

def parse_mpesa_message(message):
    """
    Parse M-Pesa confirmation SMS to extract:
    - Transaction code (e.g., QGH7XYZ123)
    - Amount (e.g., 18.00)
    - Phone number
    - Recipient (Till number)
    
    Supports common Safaricom SMS formats.
    """
    result = {
        'valid': False,
        'code': None,
        'amount': None,
        'phone': None,
        'till': None,
        'error': None
    }
    
    if not message or len(message.strip()) < 20:
        result['error'] = 'Message too short'
        return result
    
    # Clean the message
    msg = message.strip()
    
    # Extract M-Pesa code (typically 10 chars starting with letter)
    # Examples: QGH7XYZ123, SFH4K2LM9P, etc.
    code_patterns = [
        r'\b([A-Z]{3}[A-Z0-9]{7})\b',  # 10-char code
        r'\b([A-Z0-9]{10})\b',  # Any 10-char alphanumeric
    ]
    
    for pattern in code_patterns:
        matches = re.findall(pattern, msg.upper())
        for m in matches:
            # M-Pesa codes typically start with a letter and are 10 chars
            if len(m) == 10 and m[0].isalpha():
                result['code'] = m
                break
        if result['code']:
            break
    
    if not result['code']:
        result['error'] = 'Could not find M-Pesa code in message'
        return result
    
    # Extract amount - look for "Ksh" or "KES" followed by number
    amount_patterns = [
        r'Ksh\s*([\d,]+(?:\.\d{2})?)',
        r'KES\s*([\d,]+(?:\.\d{2})?)',
        r'([\d,]+(?:\.\d{2})?)\s*(?:Ksh|KES)',
    ]
    
    for pattern in amount_patterns:
        match = re.search(pattern, msg, re.IGNORECASE)
        if match:
            amount_str = match.group(1).replace(',', '')
            try:
                result['amount'] = float(amount_str)
                break
            except:
                pass
    
    if result['amount'] is None:
        result['error'] = 'Could not find amount in message'
        return result
    
    # Extract phone number (Safaricom format)
    phone_patterns = [
        r'(?:254|0)([17]\d{8})',
        r'(\d{10})',
    ]
    
    for pattern in phone_patterns:
        match = re.search(pattern, msg)
        if match:
            phone = match.group(1)
            if len(phone) == 9:
                result['phone'] = phone
            elif len(phone) == 10:
                result['phone'] = phone[1:]
            break
    
    # Extract Till number
    till_pattern = r'(?:till|to)\s*(\d{5,7})'
    match = re.search(till_pattern, msg, re.IGNORECASE)
    if match:
        result['till'] = match.group(1)
    
    # If we got this far, it's valid
    result['valid'] = True
    return result

def find_matching_plan(amount, plans):
    """Find which plan matches the paid amount"""
    for key, plan in plans.items():
        if abs(plan['price'] - amount) < 1:  # Allow small rounding
            return key, plan
    return None, None

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ===================================================================
# NETWORK MANAGER
# ===================================================================

class NetworkManager:
    def __init__(self):
        self.platform = 'windows' if os.name == 'nt' else 'linux'
        logger.info(f"Network Manager initialized on {self.platform}")
    
    def allow_device(self, mac_address):
        try:
            if self.platform == 'windows':
                rule_name = f"WiFiBill_{mac_address.replace(':', '')}"
                cmd = f'netsh advfirewall firewall add rule name="{rule_name}" dir=in action=allow remoteip=any'
                subprocess.run(cmd, shell=True, capture_output=True, timeout=5)
            else:
                cmd = f'sudo iptables -I FORWARD -m mac --mac-source {mac_address} -j ACCEPT'
                subprocess.run(cmd, shell=True, capture_output=True, timeout=5)
            logger.info(f"Allowed device: {mac_address}")
            return True
        except Exception as e:
            logger.error(f"Failed to allow device {mac_address}: {e}")
            return False
    
    def block_device(self, mac_address):
        try:
            if self.platform == 'windows':
                rule_name = f"WiFiBill_{mac_address.replace(':', '')}"
                cmd = f'netsh advfirewall firewall delete rule name="{rule_name}"'
                subprocess.run(cmd, shell=True, capture_output=True, timeout=5)
            else:
                cmd = f'sudo iptables -D FORWARD -m mac --mac-source {mac_address} -j ACCEPT'
                subprocess.run(cmd, shell=True, capture_output=True, timeout=5)
            logger.info(f"Blocked device: {mac_address}")
            return True
        except Exception as e:
            logger.error(f"Failed to block device {mac_address}: {e}")
            return False

network = NetworkManager()

# ===================================================================
# ROUTES - PUBLIC
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
                                     remaining_seconds=remaining)
    
    plans = get_plans()
    return render_template_string(INDEX_HTML, 
                                plans=plans, 
                                client_mac=client_mac, 
                                till_number=MPESA_TILL_NUMBER)

@app.route('/verify', methods=['POST'])
def verify_payment():
    """Verify pasted M-Pesa message and grant access"""
    try:
        client_mac = request.form.get('mac', '')
        message = request.form.get('mpesa_message', '').strip()
        selected_plan = request.form.get('plan', '')
        
        if not message:
            return jsonify({'success': False, 'message': 'Please paste your M-Pesa message'})
        
        if not selected_plan:
            return jsonify({'success': False, 'message': 'Please select a plan first'})
        
        # Parse the M-Pesa message
        parsed = parse_mpesa_message(message)
        
        if not parsed['valid']:
            logger.warning(f"Failed to parse message: {parsed.get('error')}")
            return jsonify({
                'success': False, 
                'message': f"Invalid M-Pesa message: {parsed.get('error', 'Could not parse')}"
            })
        
        code = parsed['code']
        amount = parsed['amount']
        phone = parsed['phone']
        
        logger.info(f"Parsed: code={code}, amount={amount}, phone={phone}")
        
        # Check if code already used
        existing = Transaction.query.filter_by(mpesa_code=code).first()
        if existing:
            if existing.status == 'used':
                return jsonify({
                    'success': False,
                    'message': f'M-Pesa code {code} has already been used for a subscription'
                })
            else:
                return jsonify({
                    'success': False,
                    'message': f'M-Pesa code {code} has already been submitted'
                })
        
        # Get plans and find matching plan
        plans = get_plans()
        plan_key, plan = find_matching_plan(amount, plans)
        
        if not plan_key:
            # Check if amount matches selected plan
            if selected_plan in plans:
                expected = plans[selected_plan]['price']
                return jsonify({
                    'success': False,
                    'message': f'Amount KES {amount:.0f} does not match any plan. Expected KES {expected} for {plans[selected_plan]["name"]}'
                })
            return jsonify({
                'success': False,
                'message': f'Amount KES {amount:.0f} does not match any available plan'
            })
        
        # Verify selected plan matches the paid amount
        if selected_plan and selected_plan != plan_key:
            return jsonify({
                'success': False,
                'message': f'You selected {plans[selected_plan]["name"]} but paid for {plan["name"]}. Please select the correct plan.'
            })
        
        # Verify Till number if it appears in message
        if parsed['till'] and parsed['till'] != MPESA_TILL_NUMBER:
            return jsonify({
                'success': False,
                'message': f'Payment was sent to wrong Till number ({parsed["till"]}). Please send to {MPESA_TILL_NUMBER}'
            })
        
        # All checks passed - create transaction
        transaction = Transaction(
            mpesa_code=code,
            phone_number=phone or 'unknown',
            amount=amount,
            plan=plan_key,
            customer_mac=client_mac,
            status='used',
            raw_message=message[:500],
            used_at=datetime.utcnow()
        )
        db.session.add(transaction)
        db.session.commit()
        
        # Grant access to customer
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
            'message': f'Payment verified! Code {code} accepted for {plan["name"]}',
            'plan': plan['name'],
            'expiry': expiry.isoformat(),
            'remaining': plan['duration']
        })
        
    except Exception as e:
        logger.error(f"Verification error: {e}")
        return jsonify({'success': False, 'message': f'Error: {str(e)}'})

# ===================================================================
# ROUTES - ADMIN
# ===================================================================

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if current_user.is_authenticated:
        return redirect(url_for('admin_dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        if user and user.password == hashlib.md5(password.encode()).hexdigest():
            login_user(user, remember=True)
            return redirect(url_for('admin_dashboard'))
        
        flash('Invalid credentials', 'error')
    
    return render_template_string(ADMIN_LOGIN_HTML)

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
                                 till_number=MPESA_TILL_NUMBER)

@app.route('/admin/customers')
@login_required
def admin_customers():
    customers = Customer.query.order_by(Customer.created_at.desc()).all()
    return render_template_string(ADMIN_CUSTOMERS_HTML, 
                                 customers=customers, 
                                 now=datetime.utcnow())

@app.route('/admin/transactions')
@login_required
def admin_transactions():
    transactions = Transaction.query.order_by(Transaction.created_at.desc()).all()
    return render_template_string(ADMIN_TRANSACTIONS_HTML, 
                                 transactions=transactions)

@app.route('/admin/plans', methods=['GET', 'POST'])
@login_required
def admin_plans():
    if request.method == 'POST':
        if request.form.get('add'):
            new_key = request.form.get('new_key')
            new_name = request.form.get('new_name')
            new_price = request.form.get('new_price')
            new_duration = request.form.get('new_duration')
            
            if new_key and new_name and new_price and new_duration:
                plans = get_plans()
                plans[new_key] = {
                    'name': new_name,
                    'price': float(new_price),
                    'duration': int(new_duration)
                }
                save_plans(plans)
                flash('Plan added successfully', 'success')
                return redirect(url_for('admin_plans'))
        
        plans = {}
        count = int(request.form.get('count', 0))
        for i in range(count):
            key = request.form.get(f'key_{i}')
            if key and key.strip():
                plans[key] = {
                    'name': request.form.get(f'name_{i}', ''),
                    'price': float(request.form.get(f'price_{i}', 0)),
                    'duration': int(request.form.get(f'duration_{i}', 0))
                }
        
        save_plans(plans)
        flash('Plans updated successfully', 'success')
        return redirect(url_for('admin_plans'))
    
    plans = get_plans()
    return render_template_string(ADMIN_PLANS_HTML, plans=plans)

@app.route('/admin/toggle/<mac>')
@login_required
def toggle_user(mac):
    customer = Customer.query.filter_by(mac_address=mac).first()
    
    if customer:
        if customer.is_active and customer.expiry_time and customer.expiry_time > datetime.utcnow():
            customer.is_active = False
            network.block_device(mac)
            flash(f'Access revoked for {mac}', 'warning')
        else:
            customer.is_active = True
            customer.expiry_time = datetime.utcnow() + timedelta(hours=1)
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
            
            for customer in expired:
                customer.is_active = False
                network.block_device(customer.mac_address)
                logger.info(f"Expired session: {customer.mac_address}")
            
            if expired:
                db.session.commit()
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(cleanup_expired, 'interval', minutes=1)
scheduler.start()
logger.info("Background scheduler started")

# ===================================================================
# INDEX HTML (Customer Portal - Paste M-Pesa Message)
# ===================================================================

INDEX_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PAY & CONNECT - WiFi Portal</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            min-height: 100vh;
            background: #080c1a;
            padding: 20px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .container {
            max-width: 560px;
            width: 100%;
            background: rgba(14,22,42,0.95);
            border-radius: 28px;
            padding: 36px 32px;
            border: 1px solid rgba(255,255,255,0.06);
            box-shadow: 0 50px 120px rgba(0,0,0,0.8);
            position: relative;
            max-height: 95vh;
            overflow-y: auto;
        }
        .container::before {
            content: '';
            position: absolute;
            top: 0; left: 0; right: 0;
            height: 3px;
            background: linear-gradient(90deg, #0066ff, #00ccff, #0066ff);
            border-radius: 28px 28px 0 0;
        }
        .header { text-align:center; margin-bottom:24px; }
        .logo {
            width:64px; height:64px;
            background:linear-gradient(145deg,#0066ff,#0044cc);
            border-radius:18px;
            display:flex;
            align-items:center;
            justify-content:center;
            margin:0 auto 12px;
            font-size:28px;
            box-shadow:0 12px 48px rgba(0,102,255,0.25);
        }
        .header h1 {
            font-size:22px; font-weight:800;
            color:#ffffff;
            letter-spacing:-0.5px;
        }
        .header .subtitle { color:#5a6a8f; font-size:13px; margin-top:2px; }
        .till-box {
            background: linear-gradient(135deg, rgba(0,102,255,0.15), rgba(0,102,255,0.05));
            border: 1px solid rgba(0,102,255,0.2);
            border-radius: 16px;
            padding: 16px;
            text-align: center;
            margin-bottom: 20px;
        }
        .till-box .label { font-size:10px; color:#5a6a8f; text-transform:uppercase; letter-spacing:1px; margin-bottom:4px; }
        .till-box .number {
            font-size: 32px;
            font-weight: 800;
            color: #4a7aff;
            font-family: monospace;
            letter-spacing: 2px;
        }
        .till-box .hint { font-size: 11px; color: #6a7a9f; margin-top: 6px; }
        .section-label {
            font-size:10px; font-weight:700; color:#5a6a8f; text-transform:uppercase;
            letter-spacing:0.8px; margin-bottom:10px;
        }
        .plans { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:20px; }
        .plan {
            border:1px solid rgba(255,255,255,0.06);
            border-radius:14px;
            padding:12px 6px;
            text-align:center;
            cursor:pointer;
            transition:all 0.3s ease;
            background:rgba(255,255,255,0.02);
            position:relative;
        }
        .plan:hover { border-color:rgba(0,102,255,0.25); background:rgba(0,102,255,0.04); }
        .plan.active { border-color:#0066ff; background:rgba(0,102,255,0.10); }
        .plan.active::after {
            content:'✓';
            position:absolute;
            top:-5px; right:-5px;
            width:18px; height:18px;
            background:#0066ff;
            border-radius:50%;
            font-size:10px;
            color:#fff;
            display:flex;
            align-items:center;
            justify-content:center;
        }
        .plan .name { font-size:9px; font-weight:700; color:#5a6a8f; text-transform:uppercase; }
        .plan .price { font-size:17px; font-weight:800; color:#ffffff; margin:2px 0; }
        .plan .duration { font-size:9px; color:#3a4a6f; }
        .plan.popular { border-color:rgba(0,102,255,0.3); }
        .plan.popular::before {
            content:'⭐ BEST';
            position:absolute;
            top:-8px; left:50%;
            transform:translateX(-50%);
            background:linear-gradient(145deg,#0066ff,#0044cc);
            color:#fff;
            font-size:7px; font-weight:700;
            padding:2px 8px;
            border-radius:10px;
        }
        .form-group { margin-bottom:16px; }
        .form-group label { display:block; font-size:10px; font-weight:700; color:#5a6a8f; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:6px; }
        .form-group textarea {
            width:100%;
            min-height: 110px;
            padding: 12px 14px;
            border:1px solid rgba(255,255,255,0.06);
            border-radius:12px;
            font-size:13px;
            font-family: monospace;
            background:rgba(255,255,255,0.03);
            color:#ffffff;
            resize: vertical;
            line-height: 1.5;
        }
        .form-group textarea:focus { outline:none; border-color:#0066ff; background:rgba(0,102,255,0.04); }
        .form-group textarea::placeholder { color:#2a3a5f; }
        .hint-text { font-size:10px; color:#3a4a6f; margin-top:5px; }
        .verify-btn {
            width:100%; padding:16px;
            background:linear-gradient(145deg,#0066ff,#0044cc);
            color:#ffffff;
            border:none;
            border-radius:12px;
            font-size:15px; font-weight:700;
            cursor:pointer;
            transition:all 0.3s ease;
            display:flex;
            align-items:center;
            justify-content:center;
            gap:10px;
        }
        .verify-btn:hover:not(:disabled) { transform:translateY(-2px); box-shadow:0 16px 48px rgba(0,102,255,0.30); }
        .verify-btn:disabled { opacity:0.5; cursor:not-allowed; }
        .verify-btn .spinner { display:none; width:18px; height:18px; border:2px solid rgba(255,255,255,0.2); border-top:2px solid #fff; border-radius:50%; animation:spin 0.8s linear infinite; }
        .verify-btn.loading .spinner { display:block; }
        .verify-btn.loading .btn-text { display:none; }
        @keyframes spin { to { transform:rotate(360deg); } }
        .status-msg { margin-top:14px; padding:12px 16px; border-radius:12px; display:none; font-size:13px; line-height: 1.5; }
        .status-msg.show { display:block; }
        .status-msg.success { background:rgba(16,185,129,0.10); color:#34d399; border:1px solid rgba(16,185,129,0.2); }
        .status-msg.error { background:rgba(239,68,68,0.10); color:#f87171; border:1px solid rgba(239,68,68,0.2); }
        .status-msg.info { background:rgba(0,102,255,0.08); color:#60a5fa; border:1px solid rgba(0,102,255,0.2); }
        .steps {
            background: rgba(255,255,255,0.02);
            border-radius: 12px;
            padding: 14px;
            margin-bottom: 20px;
            border: 1px solid rgba(255,255,255,0.04);
        }
        .steps .step {
            display: flex;
            gap: 10px;
            padding: 6px 0;
            font-size: 12px;
            color: #8a9bbf;
        }
        .steps .step .num {
            width: 20px;
            height: 20px;
            background: rgba(0,102,255,0.15);
            color: #4a7aff;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 10px;
            font-weight: 700;
            flex-shrink: 0;
        }
        .footer { text-align:center; margin-top:20px; padding-top:16px; border-top:1px solid rgba(255,255,255,0.04); font-size:10px; color:#1a2a4a; }
        .footer a { color:#4a7aff; text-decoration:none; }
        @media (max-width:480px) {
            .container { padding: 24px 18px; }
            .till-box .number { font-size: 26px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="logo">📶</div>
            <h1>PAY &amp; CONNECT</h1>
            <p class="subtitle">Pay via M-Pesa · Paste message · Get connected</p>
        </div>

        <div class="till-box">
            <div class="label">Send Payment To</div>
            <div class="number">{{ till_number }}</div>
            <div class="hint">M-Pesa → Send Money → Till Number</div>
        </div>

        <div class="steps">
            <div class="step"><div class="num">1</div><div>Send money to Till <strong>{{ till_number }}</strong></div></div>
            <div class="step"><div class="num">2</div><div>Wait for the M-Pesa SMS confirmation</div></div>
            <div class="step"><div class="num">3</div><div>Select a plan below and paste the SMS</div></div>
            <div class="step"><div class="num">4</div><div>Click "Verify &amp; Connect" to get internet</div></div>
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

        <div class="form-group">
            <label>📩 Paste Your M-Pesa Message</label>
            <textarea id="mpesaMessage" placeholder="Example:

QGH7XYZ123 Confirmed. Ksh18.00 sent to PAY & CONNECT 1671404 on 3/9/26 at 10:30 AM. New balance Ksh100.00."></textarea>
            <div class="hint-text">🔒 Each M-Pesa code can only be used once</div>
        </div>

        <button class="verify-btn" id="verifyBtn" onclick="verifyPayment()">
            <span class="btn-text">✅ Verify &amp; Connect</span>
            <span class="spinner"></span>
        </button>

        <div class="status-msg" id="statusMsg"></div>

        <div class="footer">
            © 2026 PAY &amp; CONNECT · <a href="/admin/login">Admin</a>
        </div>
    </div>

    <script>
        var selectedPlan = null;
        var isProcessing = false;

        function selectPlan(key) {
            selectedPlan = key;
            var all = document.querySelectorAll('.plan');
            for (var i = 0; i < all.length; i++) {
                all[i].classList.remove('active');
            }
            var el = document.querySelector('.plan[data-plan="' + key + '"]');
            if (el) el.classList.add('active');
            hideStatus();
        }

        function verifyPayment() {
            if (!selectedPlan) {
                showStatus('Please select a plan first', 'error');
                return;
            }
            var msg = document.getElementById('mpesaMessage').value.trim();
            if (msg.length < 20) {
                showStatus('Please paste the full M-Pesa confirmation message', 'error');
                return;
            }

            var btn = document.getElementById('verifyBtn');
            btn.classList.add('loading');
            btn.disabled = true;
            isProcessing = true;

            showStatus('⏳ Verifying your payment...', 'info');

            var fd = new FormData();
            fd.append('mpesa_message', msg);
            fd.append('plan', selectedPlan);
            fd.append('mac', '{{ client_mac }}');

            fetch('/verify', { method: 'POST', body: fd })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.success) {
                    showStatus('✅ ' + data.message + '\\n\\nYou are now connected!', 'success');
                    setTimeout(function() { window.location.reload(); }, 2500);
                } else {
                    showStatus('❌ ' + data.message, 'error');
                    btn.classList.remove('loading');
                    btn.disabled = false;
                    isProcessing = false;
                }
            })
            .catch(function(err) {
                showStatus('❌ Network error. Please try again.', 'error');
                btn.classList.remove('loading');
                btn.disabled = false;
                isProcessing = false;
            });
        }

        function showStatus(msg, type) {
            var el = document.getElementById('statusMsg');
            el.textContent = msg;
            el.className = 'status-msg show ' + type;
        }

        function hideStatus() {
            var el = document.getElementById('statusMsg');
            el.className = 'status-msg';
            el.textContent = '';
        }
    </script>
</body>
</html>'''

# ===================================================================
# ACTIVE HTML
# ===================================================================

ACTIVE_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Connected - PAY & CONNECT</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            min-height: 100vh; display: flex; align-items: center; justify-content: center;
            background: #080c1a; padding: 20px;
        }
        .container {
            max-width: 420px; width: 100%;
            background: rgba(14,22,42,0.95);
            border-radius: 28px; padding: 40px 32px; text-align: center;
            border: 1px solid rgba(255,255,255,0.06);
            box-shadow: 0 50px 120px rgba(0,0,0,0.8);
            position: relative;
        }
        .container::before {
            content:''; position:absolute; top:0; left:0; right:0; height:3px;
            background:linear-gradient(90deg,#0066ff,#00ccff,#0066ff);
            border-radius:28px 28px 0 0;
        }
        .status-icon { width:80px; height:80px; background:rgba(16,185,129,0.12); border-radius:50%; display:flex; align-items:center; justify-content:center; margin:0 auto 20px; font-size:40px; border:2px solid rgba(16,185,129,0.2); }
        h2 { color:#ffffff; margin-bottom:4px; font-size:24px; }
        .sub { color:#5a6a8f; font-size:14px; }
        .info { background:rgba(255,255,255,0.02); border-radius:16px; padding:20px; margin:24px 0; text-align:left; border:1px solid rgba(255,255,255,0.04); }
        .info .row { display:flex; justify-content:space-between; padding:8px 0; border-bottom:1px solid rgba(255,255,255,0.04); font-size:14px; }
        .info .row:last-child { border-bottom:none; }
        .info .label { color:#5a6a8f; }
        .info .value { font-weight:600; color:#ffffff; }
        .timer { font-size:42px; font-weight:700; color:#0066ff; }
        .btn { display:inline-block; padding:12px 32px; background:rgba(255,255,255,0.05); color:#8a9bbf; border:1px solid rgba(255,255,255,0.06); border-radius:12px; text-decoration:none; font-size:14px; font-weight:600; margin-top:16px; }
        .btn:hover { background:rgba(255,255,255,0.08); color:#ffffff; }
        .footer { text-align:center; margin-top:20px; font-size:10px; color:#1a2a4a; }
    </style>
</head>
<body>
    <div class="container">
        <div class="status-icon">✅</div>
        <h2>You're Connected!</h2>
        <p class="sub">Enjoy high-speed internet</p>
        <div class="info">
            <div class="row"><span class="label">Plan</span><span class="value">{{ customer.plan }}</span></div>
            <div class="row"><span class="label">Time Remaining</span><span class="value timer" id="timer">{{ remaining }}</span></div>
            <div class="row"><span class="label">Device</span><span class="value" style="font-size:12px;font-family:monospace;color:#6a7a9f;">{{ customer.mac_address[:17] }}</span></div>
        </div>
        <a href="/" class="btn">↻ Refresh</a>
        <div class="footer">Auto-refreshes every 30 seconds</div>
    </div>
    <script>
        var seconds = {{ remaining_seconds|int }};
        var timerEl = document.getElementById('timer');
        function formatTime(s) {
            if (s <= 0) return 'Expired';
            var d = Math.floor(s / 86400);
            var h = Math.floor((s % 86400) / 3600);
            var m = Math.floor((s % 3600) / 60);
            var sec = Math.floor(s % 60);
            if (d > 0) return d+'d '+h+'h '+m+'m';
            if (h > 0) return h+'h '+m+'m '+sec+'s';
            if (m > 0) return m+'m '+sec+'s';
            return sec+'s';
        }
        setInterval(function() {
            seconds--;
            if (seconds <= 0) {
                timerEl.textContent = 'Expired';
                setTimeout(function() { window.location.reload(); }, 3000);
            } else {
                timerEl.textContent = formatTime(seconds);
            }
        }, 1000);
        setTimeout(function() { window.location.reload(); }, 30000);
    </script>
</body>
</html>'''

# ===================================================================
# ADMIN TEMPLATES
# ===================================================================

ADMIN_LOGIN_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Login</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; min-height:100vh; display:flex; align-items:center; justify-content:center; background:#080c1a; padding:20px; }
        .container { max-width:380px; width:100%; background:rgba(14,22,42,0.95); border-radius:24px; padding:40px 32px; border:1px solid rgba(255,255,255,0.06); box-shadow:0 50px 120px rgba(0,0,0,0.8); position:relative; }
        .container::before { content:''; position:absolute; top:0; left:0; right:0; height:3px; background:linear-gradient(90deg,#0066ff,#00ccff,#0066ff); border-radius:24px 24px 0 0; }
        h2 { text-align:center; color:#ffffff; margin-bottom:4px; font-size:22px; }
        .sub { text-align:center; color:#5a6a8f; font-size:13px; margin-bottom:24px; }
        .input-group { margin-bottom:16px; }
        .input-group label { display:block; font-size:10px; font-weight:700; color:#5a6a8f; text-transform:uppercase; margin-bottom:4px; }
        .input-group input { width:100%; padding:14px 16px; border:1px solid rgba(255,255,255,0.06); border-radius:12px; font-size:15px; background:rgba(255,255,255,0.03); color:#ffffff; }
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
        <p class="sub">Sign in to manage your WiFi</p>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}{% for c, m in messages %}<div class="flash">{{ m }}</div>{% endfor %}{% endif %}
        {% endwith %}
        <form method="POST">
            <div class="input-group">
                <label>Username</label>
                <input type="text" name="username" value="admin" required>
            </div>
            <div class="input-group">
                <label>Password</label>
                <input type="password" name="password" value="admin123" required>
            </div>
            <button type="submit" class="btn">Sign In</button>
        </form>
        <div class="footer">Default: admin / admin123 · <a href="/">← Back</a></div>
    </div>
</body>
</html>'''

ADMIN_DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard</title>
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
        table { width:100%; border-collapse:collapse; font-size:14px; color:#8a9bbf; }
        table th { text-align:left; padding:10px 8px; color:#5a6a8f; font-weight:600; border-bottom:1px solid rgba(255,255,255,0.05); }
        table td { padding:10px 8px; border-bottom:1px solid rgba(255,255,255,0.03); }
        .badge { padding:4px 12px; border-radius:20px; font-size:11px; font-weight:600; }
        .badge.active, .badge.used { background:rgba(16,185,129,0.12); color:#34d399; }
        .badge.pending { background:rgba(251,191,36,0.12); color:#fbbf24; }
        .status-list { max-height:300px; overflow-y:auto; }
        .status-item { display:flex; justify-content:space-between; padding:8px 0; border-bottom:1px solid rgba(255,255,255,0.03); font-size:13px; color:#8a9bbf; }
        .status-item:last-child { border-bottom:none; }
        .mac { font-family:monospace; font-size:12px; color:#5a6a8f; }
        .empty { color:#3a4a6f; text-align:center; padding:20px; }
        .till-info { color:#4a7aff; font-family:monospace; font-size:12px; background:rgba(0,102,255,0.06); padding:2px 10px; border-radius:10px; }
        @media (max-width:768px) {
            .sidebar { width:60px; }
            .sidebar .brand { padding:0 12px 16px; font-size:14px; }
            .sidebar .nav a { padding:12px 16px; font-size:12px; }
            .sidebar .nav a span { display:none; }
            .main { margin-left:60px; padding:16px; }
            .stats { grid-template-columns:1fr 1fr; }
            .grid-2 { grid-template-columns:1fr; }
        }
    </style>
</head>
<body>
    <div class="sidebar">
        <div class="brand">📶 <span>PAY</span>&amp;CONNECT</div>
        <div class="nav">
            <a href="/admin" class="active">📊 <span>Dashboard</span></a>
            <a href="/admin/customers">👤 <span>Customers</span></a>
            <a href="/admin/transactions">💳 <span>M-Pesa Codes</span></a>
            <a href="/admin/plans">⚙️ <span>Plans</span></a>
            <a href="/admin/logout">🚪 <span>Logout</span></a>
        </div>
    </div>
    <div class="main">
        <div class="top">
            <h2>📊 Dashboard <span class="till-info">Till: {{ till_number }}</span></h2>
            <div class="user"><span>Admin</span><div class="avatar">A</div><a href="/admin/logout">Logout</a></div>
        </div>
        <div class="stats">
            <div class="stat"><span class="icon">👤</span><div class="num">{{ total_customers }}</div><div class="label">Total Customers</div></div>
            <div class="stat"><span class="icon">🟢</span><div class="num">{{ active_customers }}</div><div class="label">Active Now</div></div>
            <div class="stat"><span class="icon">💳</span><div class="num">{{ total_transactions }}</div><div class="label">Codes Verified</div></div>
            <div class="stat"><span class="icon">💰</span><div class="num">KES {{ "%.0f"|format(total_revenue) }}</div><div class="label">Total Revenue</div></div>
        </div>
        <div class="grid-2">
            <div class="card">
                <h3>🟢 Active Sessions</h3>
                <div class="status-list">
                    {% if active_sessions %}
                        {% for s in active_sessions %}
                        <div class="status-item">
                            <span class="mac">{{ s.mac_address[:17] }}</span>
                            <span style="color:#4a7aff;">{{ s.plan }}</span>
                            <span style="font-size:12px;">{{ ((s.expiry_time - now).total_seconds() // 60)|int }}m left</span>
                        </div>
                        {% endfor %}
                    {% else %}<div class="empty">No active sessions</div>{% endif %}
                </div>
            </div>
            <div class="card">
                <h3>💳 Recent M-Pesa Codes</h3>
                <div class="status-list">
                    {% if recent_transactions %}
                        {% for t in recent_transactions[:5] %}
                        <div class="status-item">
                            <span style="font-family:monospace;color:#4a7aff;">{{ t.mpesa_code }}</span>
                            <span>KES {{ "%.0f"|format(t.amount) }}</span>
                            <span style="font-size:11px;color:#3a4a6f;">{{ t.created_at.strftime('%H:%M') }}</span>
                        </div>
                        {% endfor %}
                    {% else %}<div class="empty">No codes yet</div>{% endif %}
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
    <title>Customers</title>
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
            .sidebar { width:60px; }
            .sidebar .brand { padding:0 12px 16px; font-size:14px; }
            .sidebar .nav a { padding:12px 16px; font-size:12px; }
            .sidebar .nav a span { display:none; }
            .main { margin-left:60px; padding:16px; }
        }
    </style>
</head>
<body>
    <div class="sidebar">
        <div class="brand">📶 <span>PAY</span>&amp;CONNECT</div>
        <div class="nav">
            <a href="/admin">📊 <span>Dashboard</span></a>
            <a href="/admin/customers" class="active">👤 <span>Customers</span></a>
            <a href="/admin/transactions">💳 <span>M-Pesa Codes</span></a>
            <a href="/admin/plans">⚙️ <span>Plans</span></a>
            <a href="/admin/logout">🚪 <span>Logout</span></a>
        </div>
    </div>
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
                        <td>{% if c.phone_number %}0{{ c.phone_number[-9:] }}{% else %}—{% endif %}</td>
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
    <title>M-Pesa Codes</title>
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
            .sidebar { width:60px; }
            .sidebar .brand { padding:0 12px 16px; font-size:14px; }
            .sidebar .nav a { padding:12px 16px; font-size:12px; }
            .sidebar .nav a span { display:none; }
            .main { margin-left:60px; padding:16px; }
        }
    </style>
</head>
<body>
    <div class="sidebar">
        <div class="brand">📶 <span>PAY</span>&amp;CONNECT</div>
        <div class="nav">
            <a href="/admin">📊 <span>Dashboard</span></a>
            <a href="/admin/customers">👤 <span>Customers</span></a>
            <a href="/admin/transactions" class="active">💳 <span>M-Pesa Codes</span></a>
            <a href="/admin/plans">⚙️ <span>Plans</span></a>
            <a href="/admin/logout">🚪 <span>Logout</span></a>
        </div>
    </div>
    <div class="main">
        <div class="top">
            <h2>💳 Used M-Pesa Codes</h2>
            <div class="user"><span>Admin</span><div class="avatar">A</div><a href="/admin/logout">Logout</a></div>
        </div>
        <div class="card">
            {% if transactions %}
            <table>
                <thead><tr><th>M-Pesa Code</th><th>Phone</th><th>Amount</th><th>Plan</th><th>Used At</th></tr></thead>
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
    <title>Plans</title>
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
        .card { background:rgba(14,22,42,0.95); border-radius:16px; padding:24px; border:1px solid rgba(255,255,255,0.05); }
        .card h3 { color:#fff; margin-bottom:16px; font-size:16px; }
        .plan-row { display:grid; grid-template-columns:1fr 1fr 1fr 1fr auto; gap:12px; padding:12px 0; border-bottom:1px solid rgba(255,255,255,0.03); align-items:center; }
        .plan-row.header { font-weight:600; color:#5a6a8f; font-size:12px; text-transform:uppercase; }
        .plan-row input { padding:8px 12px; border:1px solid rgba(255,255,255,0.06); border-radius:8px; font-size:14px; background:rgba(255,255,255,0.03); color:#fff; }
        .plan-row input:focus { outline:none; border-color:#0066ff; }
        .btn { padding:8px 20px; border:none; border-radius:8px; font-size:13px; font-weight:600; cursor:pointer; }
        .btn-primary { background:linear-gradient(145deg,#0066ff,#0044cc); color:#fff; }
        .btn-success { background:rgba(16,185,129,0.15); color:#34d399; }
        .flash { background:rgba(239,68,68,0.10); color:#f87171; padding:12px 16px; border-radius:10px; margin-bottom:16px; font-size:14px; }
        .flash.success { background:rgba(16,185,129,0.10); color:#34d399; }
        .mt-16 { margin-top:16px; }
        .flex { display:flex; gap:12px; align-items:end; flex-wrap:wrap; }
        .flex .field { display:flex; flex-direction:column; gap:4px; }
        .flex .field label { font-size:10px; color:#5a6a8f; font-weight:600; text-transform:uppercase; }
        .flex .field input { padding:8px 12px; border:1px solid rgba(255,255,255,0.06); border-radius:8px; font-size:14px; background:rgba(255,255,255,0.03); color:#fff; }
        hr { margin:24px 0; border-color:rgba(255,255,255,0.05); }
        @media (max-width:768px) {
            .sidebar { width:60px; }
            .sidebar .brand { padding:0 12px 16px; font-size:14px; }
            .sidebar .nav a { padding:12px 16px; font-size:12px; }
            .sidebar .nav a span { display:none; }
            .main { margin-left:60px; padding:16px; }
            .plan-row { grid-template-columns:1fr 1fr; gap:8px; }
        }
    </style>
</head>
<body>
    <div class="sidebar">
        <div class="brand">📶 <span>PAY</span>&amp;CONNECT</div>
        <div class="nav">
            <a href="/admin">📊 <span>Dashboard</span></a>
            <a href="/admin/customers">👤 <span>Customers</span></a>
            <a href="/admin/transactions">💳 <span>M-Pesa Codes</span></a>
            <a href="/admin/plans" class="active">⚙️ <span>Plans</span></a>
            <a href="/admin/logout">🚪 <span>Logout</span></a>
        </div>
    </div>
    <div class="main">
        <div class="top">
            <h2>⚙️ Pricing Plans</h2>
            <div class="user"><span>Admin</span><div class="avatar">A</div><a href="/admin/logout">Logout</a></div>
        </div>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}{% for c, m in messages %}<div class="flash {{ c }}">{{ m }}</div>{% endfor %}{% endif %}
        {% endwith %}
        <div class="card">
            <h3>Edit Plans</h3>
            <form method="POST">
                <div class="plan-row header"><span>Key</span><span>Name</span><span>Price (KES)</span><span>Duration (sec)</span><span></span></div>
                {% for key, plan in plans.items() %}
                <div class="plan-row">
                    <input type="text" name="key_{{ loop.index0 }}" value="{{ key }}" style="font-family:monospace;font-size:12px;">
                    <input type="text" name="name_{{ loop.index0 }}" value="{{ plan.name }}">
                    <input type="number" name="price_{{ loop.index0 }}" value="{{ plan.price }}" step="1" min="0">
                    <input type="number" name="duration_{{ loop.index0 }}" value="{{ plan.duration }}" step="60" min="60">
                </div>
                {% endfor %}
                <input type="hidden" name="count" value="{{ plans|length }}">
                <div class="mt-16"><button type="submit" class="btn btn-primary">💾 Save Plans</button></div>
            </form>
            <hr>
            <h3 style="color:#fff;margin-bottom:12px;font-size:16px;">➕ Add New Plan</h3>
            <form method="POST">
                <div class="flex">
                    <div class="field"><label>Key</label><input type="text" name="new_key" placeholder="2hr" style="width:100px;"></div>
                    <div class="field"><label>Name</label><input type="text" name="new_name" placeholder="2 Hours" style="width:120px;"></div>
                    <div class="field"><label>Price (KES)</label><input type="number" name="new_price" placeholder="35" style="width:100px;"></div>
                    <div class="field"><label>Duration (sec)</label><input type="number" name="new_duration" placeholder="7200" style="width:110px;"></div>
                    <button type="submit" name="add" value="1" class="btn btn-success">+ Add</button>
                </div>
            </form>
        </div>
    </div>
</body>
</html>'''

# ===================================================================
# MAIN ENTRY
# ===================================================================

if __name__ == '__main__':
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
        
        logger.info("Database initialized")
    
    print("""
    ╔═══════════════════════════════════════════════════════════════════╗
    ║                                                                   ║
    ║   📶  PAY & CONNECT - M-Pesa SMS Verification System             ║
    ║                                                                   ║
    ║   How it works:                                                  ║
    ║   1. Customer sends money to Till """ + MPESA_TILL_NUMBER + """                  ║
    ║   2. Customer receives M-Pesa SMS                                ║
    ║   3. Customer pastes SMS on portal                               ║
    ║   4. System verifies code (each code used ONCE only)             ║
    ║   5. Internet access granted automatically                       ║
    ║                                                                   ║
    ║   🌐 Portal:    http://localhost:5000                            ║
    ║   🔐 Admin:     http://localhost:5000/admin                     ║
    ║   👤 Login:     admin / admin123                                ║
    ║                                                                   ║
    ║   Press CTRL+C to stop                                           ║
    ║                                                                   ║
    ╚═══════════════════════════════════════════════════════════════════╝
    """)
    
    try:
        app.run(host=HOST, port=PORT, debug=DEBUG_MODE)
    except KeyboardInterrupt:
        print("\n\n[✓] Server stopped")
        sys.exit(0)