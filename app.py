import os
import sys
import socket
import logging
import smtplib
import json
import time
import threading 
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, jsonify, session
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from io import BytesIO
import pandas as pd
from functools import wraps
import psycopg2
import psycopg2.extras

# ============================================
# CONFIGURATION
# ============================================
HOST = '0.0.0.0'
PORT = 10000
DEBUG = False

# ============================================
# EMAIL CONFIGURATION (from environment)
# ============================================
EMAIL_SENDER = "craiglandsg@gmail.com"
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_RECIPIENT = "reservations@craiglands.co.uk"
EMAIL_CC = "gm@craiglands.co.uk"

# ============================================
# DATABASE CONFIGURATION (from environment)
# ============================================
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL environment variable not set")

def get_db_connection():
    """Return a PostgreSQL connection with dict-like rows."""
    conn = psycopg2.connect(DATABASE_URL)
    conn.cursor_factory = psycopg2.extras.DictCursor
    return conn

# ============================================
# SETUP LOGGING
# ============================================
if not os.path.exists('logs'):
    os.makedirs('logs')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================
# GET NETWORK INFO
# ============================================
def get_network_info():
    try:
        hostname = socket.gethostname()
        ip_addresses = []
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            ip_addresses.append(local_ip)
        except:
            pass
        try:
            for addr in socket.getaddrinfo(hostname, None):
                if addr[0] == socket.AF_INET:
                    ip = addr[4][0]
                    if ip not in ip_addresses and not ip.startswith('127.'):
                        ip_addresses.append(ip)
        except:
            pass
        return hostname, ip_addresses
    except Exception as e:
        logger.error(f"Error getting network info: {e}")
        return "Unknown", ["127.0.0.1"]

# ============================================
# HELPER - TODAY'S DATE
# ============================================
def get_today_date():
    return datetime.now().strftime('%Y-%m-%d')

def get_date_days_from_now(days):
    return (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d')

# ============================================
# DATABASE SETUP & MIGRATION
# ============================================
def init_database():
    print("=== INIT DATABASE START ===")
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Create bookings table
        c.execute('''
            CREATE TABLE IF NOT EXISTS bookings (
                id SERIAL PRIMARY KEY,
                date TEXT NOT NULL,
                service TEXT NOT NULL,
                time TEXT NOT NULL,
                name TEXT NOT NULL,
                tel TEXT,
                guest_email TEXT,
                voucher TEXT,
                notes TEXT,
                dietary TEXT,
                paid TEXT DEFAULT 'Unpaid',
                guests INTEGER DEFAULT 1,
                room TEXT,
                surname TEXT,
                filling TEXT,
                bread TEXT,
                collection_time TEXT,
                confirmation_sent INTEGER DEFAULT 0,
                is_deleted INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Check for missing columns using information_schema
        c.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'bookings'
        """)
        existing_columns = [row[0] for row in c.fetchall()]

        expected_columns = {
            'guests', 'room', 'surname', 'filling', 'bread',
            'collection_time', 'guest_email', 'confirmation_sent', 'is_deleted'
        }

        for col in expected_columns:
            if col not in existing_columns:
                c.execute(f"ALTER TABLE bookings ADD COLUMN {col} TEXT")

        # Create guest_meals table
        c.execute('''
            CREATE TABLE IF NOT EXISTS guest_meals (
                id SERIAL PRIMARY KEY,
                booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
                guest_number INTEGER NOT NULL,
                filling TEXT,
                bread TEXT,
                dietary TEXT
            )
        ''')

        # Create activity_log table
        c.execute('''
            CREATE TABLE IF NOT EXISTS activity_log (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                action_type TEXT NOT NULL,
                booking_id INTEGER,
                details TEXT,
                username TEXT DEFAULT 'system'
            )
        ''')

        # Indexes
        c.execute('CREATE INDEX IF NOT EXISTS idx_date ON bookings(date)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_service ON bookings(service)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_name ON bookings(name)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_room ON bookings(room)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_is_deleted ON bookings(is_deleted)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_booking_id ON guest_meals(booking_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_log_timestamp ON activity_log(timestamp)')

        conn.commit()
        conn.close()
        logger.info("✅ Database initialized/migrated successfully")
        print("=== INIT DATABASE SUCCESS ===")

    except Exception as e:
        logger.error(f"❌ Database initialization error: {e}")
        print(f"=== INIT DATABASE ERROR: {e} ===")

# Run database init on startup
init_database()

# ============================================
# LOGGING FUNCTION
# ============================================
def log_activity(action_type, booking_id=None, details=None):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            INSERT INTO activity_log (action_type, booking_id, details)
            VALUES (%s, %s, %s)
        ''', (action_type, booking_id, json.dumps(details) if details else None))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error logging activity: {e}")

# ============================================
# EMAIL FUNCTIONS
# ============================================
def send_customer_confirmation(booking_id, guest_email, is_cancellation=False):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT * FROM bookings WHERE id = %s', (booking_id,))
        booking = dict(c.fetchone())

        if booking['service'] == 'Packed Lunch':
            c.execute('SELECT * FROM guest_meals WHERE booking_id = %s ORDER BY guest_number', (booking_id,))
            booking['guest_meals'] = [dict(row) for row in c.fetchall()]
        conn.close()

        if is_cancellation:
            subject = f"Craiglands Booking Cancellation - {booking['date']}"
            body = f"""
🏨 CRAIGLANDS HOTEL - BOOKING CANCELLATION
Booking ID: {booking_id}
===========================================

Dear {booking['name']},

Your booking has been cancelled:

Date: {booking['date']}
Service: {booking['service']}
Time: {booking['time'] if booking['service'] != 'Packed Lunch' else booking['collection_time']}

If this cancellation was unexpected, please contact us.

Thank you for considering Craiglands Hotel.
"""
        else:
            subject = f"Craiglands Booking Confirmation - {booking['date']}"
            body = f"""
🏨 CRAIGLANDS HOTEL - BOOKING CONFIRMATION
Booking ID: {booking_id}
===========================================

Dear {booking['name']},

Your booking has been confirmed:

Date: {booking['date']}
Service: {booking['service']}
Time: {booking['time'] if booking['service'] != 'Packed Lunch' else booking['collection_time']}

"""
            if booking['service'] == 'Packed Lunch':
                body += f"""
Room: {booking['room']}
Number of Guests: {booking['guests']}

Meal Selections:
"""
                for meal in booking.get('guest_meals', []):
                    body += f"  Guest {meal['guest_number']}: {meal['filling']} ({meal['bread']})\n"

            body += f"""
Dietary Requirements: {booking['dietary'] or 'None'}
Special Notes: {booking['notes'] or 'None'}

Thank you for choosing Craiglands Hotel.
"""

        body += "\nThis is an automated message. Please do not reply."

        msg = MIMEMultipart()
        msg['From'] = EMAIL_SENDER
        msg['To'] = guest_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)
        logger.info(f"✅ Guest email sent to {guest_email}")

        msg = MIMEMultipart()
        msg['From'] = EMAIL_SENDER
        msg['To'] = EMAIL_RECIPIENT
        msg['Subject'] = f"[COPY] {subject}"
        msg.attach(MIMEText(body, 'plain'))
        server.send_message(msg)
        logger.info(f"✅ Hotel copy sent to {EMAIL_RECIPIENT}")

        server.quit()
        logger.info(f"✅ {'Cancellation' if is_cancellation else 'Confirmation'} email sent to {guest_email} and {EMAIL_RECIPIENT}")
        return True
    except Exception as e:
        logger.error(f"❌ Email failed: {e}")
        return False

def send_hotel_notification(booking_id, action):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT * FROM bookings WHERE id = %s', (booking_id,))
        booking = dict(c.fetchone())
        conn.close()

        subject = f"Craiglands Booking {action} - {booking['date']}"
        body = f"""
📋 CRAIGLANDS BOOKING SYSTEM NOTIFICATION
Booking ID: {booking_id}
===========================================

Action: {action}
Date: {booking['date']}
Service: {booking['service']}
Time: {booking['time'] if booking['service'] != 'Packed Lunch' else booking['collection_time']}
Customer: {booking['name']}
Phone: {booking['tel'] or 'N/A'}
Email: {booking['guest_email'] or 'N/A'}
Payment: {booking['paid']}
"""
        if booking['service'] == 'Packed Lunch':
            body += f"Room: {booking['room']}\nGuests: {booking['guests']}\n"

        msg = MIMEMultipart()
        msg['From'] = EMAIL_SENDER
        msg['To'] = EMAIL_RECIPIENT
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()

        logger.info(f"✅ Hotel notification sent for booking {booking_id} ({action}) to {EMAIL_RECIPIENT}")
    except Exception as e:
        logger.error(f"❌ Hotel notification failed: {e}")

def send_future_bookings_backup(schedule_time="20:00"):
    try:
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            SELECT * FROM bookings
            WHERE date >= %s AND is_deleted = 0
            ORDER BY date, service, time
        ''', (tomorrow,))
        rows = c.fetchall()
        data = [dict(row) for row in rows]
        conn.close()

        if not data:
            logger.info(f"No future bookings at {schedule_time}, skipping backup")
            return

        df = pd.DataFrame(data)

        excel_buffer = BytesIO()
        with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='Future_Bookings', index=False)
        excel_buffer.seek(0)

        exports_dir = os.path.join(os.path.dirname(__file__), 'exports', 'future_backups')
        os.makedirs(exports_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        local_file = os.path.join(exports_dir, f'future_bookings_{timestamp}.xlsx')
        df.to_excel(local_file, index=False)

        body_text = f"📅 FUTURE BOOKINGS REPORT ({schedule_time})\n"
        body_text += "=" * 50 + "\n"
        body_text += f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        body_text += f"Period: from {tomorrow} onward\n"
        body_text += f"Total bookings: {len(df)}\n\n"

        for date_val in sorted(df['date'].unique()):
            day_df = df[df['date'] == date_val]
            body_text += f"\n--- {date_val} ---\n"
            for _, row in day_df.iterrows():
                body_text += f"{row['time']} | {row['service']} | {row['name']}"
                if row['room']:
                    body_text += f" (Room {row['room']})"
                if row['guests'] and row['guests'] > 1:
                    body_text += f" | {row['guests']} guests"
                if row['filling']:
                    body_text += f" | {row['filling']}"
                if row.get('guest_email'):
                    body_text += f" | 📧 {row['guest_email']}"
                if row.get('voucher'):
                    body_text += f" | 🎟️ {row['voucher']}"
                body_text += "\n"

        msg = MIMEMultipart()
        msg['From'] = EMAIL_SENDER
        msg['To'] = EMAIL_RECIPIENT
        msg['Cc'] = EMAIL_CC
        msg['Subject'] = f'📅 Craiglands Future Bookings Backup ({schedule_time}) - {datetime.now().strftime("%Y-%m-%d")}'

        msg.attach(MIMEText(body_text, 'plain'))

        attachment = MIMEApplication(excel_buffer.read(), _subtype="xlsx")
        attachment.add_header('Content-Disposition', 'attachment',
                              filename=f'future_bookings_{datetime.now().strftime("%Y%m%d_%H%M")}.xlsx')
        msg.attach(attachment)

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()

        logger.info(f"✅ Future bookings backup email sent at {schedule_time} (CC: {EMAIL_CC})")
    except Exception as e:
        logger.error(f"❌ Future bookings backup failed at {schedule_time}: {e}")

# ============================================
# CREATE FLASK APP
# ============================================
app = Flask(__name__)
app.secret_key = 'craiglands-booking-system-2024-network'
app.config['SESSION_TYPE'] = 'filesystem'
@app.route('/health')
def health():
    return "OK", 200
# ============================================
# AUTHENTICATION
# ============================================
FIXED_PASSWORD = "1020"
ADMIN_IMPORT_CODE = "2020"

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('authenticated'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def admin_import_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_import'):
            return redirect(url_for('import_auth', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == FIXED_PASSWORD:
            session['authenticated'] = True
            next_page = request.args.get('next') or url_for('index')
            return redirect(next_page)
        else:
            flash('Incorrect code', 'danger')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('authenticated', None)
    session.pop('admin_import', None)
    flash('You have been logged out.', 'success')
    return redirect(url_for('login'))

@app.route('/import_auth', methods=['GET', 'POST'])
def import_auth():
    if request.method == 'POST':
        code = request.form.get('code')
        if code == ADMIN_IMPORT_CODE:
            session['admin_import'] = True
            next_page = request.args.get('next') or url_for('import_bookings')
            return redirect(next_page)
        else:
            flash('Incorrect admin code', 'danger')
    return render_template('import_auth.html')

# ============================================
# SERVICE CONFIGURATION
# ============================================
SERVICES = {
    'Lunch/Afternoon Tea': {'start': '12:00', 'end': '17:00'},
    'Dinner': {'start': '17:00', 'end': '21:00'},
    'Packed Lunch': {'start': '07:00', 'end': '12:00'}
}
SERVICE_ORDER = ['Packed Lunch', 'Lunch/Afternoon Tea', 'Dinner']

# ============================================
# HELPER FUNCTIONS
# ============================================
def get_db():
    return get_db_connection()

def row_to_dict(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}

def rows_to_dicts(rows):
    return [row_to_dict(row) for row in rows]

def generate_time_slots(start_time, end_time):
    slots = []
    try:
        current = datetime.strptime(start_time, '%H:%M')
        end = datetime.strptime(end_time, '%H:%M')
        while current < end:
            slots.append(current.strftime('%H:%M'))
            current += timedelta(minutes=15)
    except:
        if start_time == '12:00':
            for hour in range(12, 17):
                for minute in ['00', '15', '30', '45']:
                    slots.append(f"{hour:02d}:{minute}")
        elif start_time == '07:00':
            for hour in range(7, 12):
                for minute in ['00', '15', '30', '45']:
                    slots.append(f"{hour:02d}:{minute}")
        else:
            for hour in range(18, 21):
                for minute in ['00', '15', '30', '45']:
                    slots.append(f"{hour:02d}:{minute}")
    return slots

def get_booking_count(date_str, service, time_slot):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''
            SELECT COUNT(*) as count FROM bookings
            WHERE date = %s AND service = %s AND time = %s AND is_deleted = 0
        ''', (date_str, service, time_slot))
        result = c.fetchone()
        conn.close()
        return result['count'] if result else 0
    except:
        return 0
def get_availability_batch(date_str):
    """Fetch all booking counts for all services and time slots in one query"""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''
            SELECT service, time, COUNT(*) as count 
            FROM bookings 
            WHERE date = %s AND is_deleted = 0
            GROUP BY service, time
        ''', (date_str,))
        rows = c.fetchall()
        conn.close()
        
        # Build a lookup dictionary: (service, time) -> count
        counts = {}
        for row in rows:
            counts[(row['service'], row['time'])] = row['count']
        return counts
    except Exception as e:
        logger.error(f"Error fetching availability: {e}")
        return {}
def get_bookings_with_meals(bookings_rows):
    bookings = rows_to_dicts(bookings_rows)
    conn = get_db()
    for booking in bookings:
        if booking['service'] == 'Packed Lunch':
            c = conn.cursor()
            c.execute('SELECT * FROM guest_meals WHERE booking_id = %s ORDER BY guest_number', (booking['id'],))
            booking['guest_meals'] = rows_to_dicts(c.fetchall())
        else:
            booking['guest_meals'] = []
    conn.close()
    return bookings

def search_bookings(query):
    try:
        conn = get_db()
        c = conn.cursor()
        search_term = f"%{query}%"
        c.execute('''
            SELECT * FROM bookings
            WHERE (name LIKE %s OR room LIKE %s OR tel LIKE %s OR surname LIKE %s OR CAST(id AS TEXT) LIKE %s)
              AND is_deleted = 0
            ORDER BY date DESC, service, time
        ''', (search_term, search_term, search_term, search_term, search_term))
        rows = c.fetchall()
        conn.close()
        return get_bookings_with_meals(rows)
    except Exception as e:
        logger.error(f"Search error: {e}")
        return []

def search_suggestions(query):
    try:
        conn = get_db()
        c = conn.cursor()
        search_term = f"%{query}%"
        c.execute('''
            SELECT id, date, service, time, name, room, is_deleted
            FROM bookings
            WHERE name LIKE %s OR room LIKE %s OR tel LIKE %s OR surname LIKE %s OR CAST(id AS TEXT) LIKE %s
            ORDER BY date DESC
            LIMIT 20
        ''', (search_term, search_term, search_term, search_term, search_term))
        rows = c.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Suggestions error: {e}")
        return []

def get_logs_for_booking(booking_id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''
            SELECT timestamp, action_type, details FROM activity_log
            WHERE booking_id = %s ORDER BY timestamp DESC
        ''', (booking_id,))
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"Error fetching logs for booking {booking_id}: {e}")
        return []

# ============================================
# ROUTES
# ============================================
@app.route('/')
@login_required
def index():
    selected_date = request.args.get('date', get_today_date())
    search_query = request.args.get('search', '')
    show_cancelled = request.args.get('show_cancelled', '0') == '1'

    try:
        if search_query:
            bookings = search_bookings(search_query)
        else:
            conn = get_db()
            c = conn.cursor()
            if show_cancelled:
                c.execute('SELECT * FROM bookings WHERE date = %s ORDER BY service, time', (selected_date,))
            else:
                c.execute('SELECT * FROM bookings WHERE date = %s AND is_deleted = 0 ORDER BY service, time', (selected_date,))
            bookings_rows = c.fetchall()
            conn.close()
            bookings = get_bookings_with_meals(bookings_rows)
    except Exception as e:
        logger.error(f"Error fetching bookings: {e}")
        bookings = []
        flash('Error loading bookings', 'danger')

    # Get all availability counts in one batch query
    availability_counts = get_availability_batch(selected_date)

    # Build the availability structure
    availability = {}
    for service_name, times in SERVICES.items():
        slots = generate_time_slots(times['start'], times['end'])
        service_avail = {}
        for slot in slots:
            count = availability_counts.get((service_name, slot), 0)
            service_avail[slot] = {
                'count': count,
                'available': count < 2,
                'status': 'available' if count < 2 else 'full'
            }
        availability[service_name] = service_avail

    return render_template('index.html',
                           bookings=bookings,
                           availability=availability,
                           selected_date=selected_date,
                           search_query=search_query,
                           show_cancelled=show_cancelled,
                           services=SERVICES,
                           service_order=SERVICE_ORDER,
                           datetime=datetime)

@app.route('/api/search_suggestions')
@login_required
def search_suggestions_api():
    query = request.args.get('q', '')
    if len(query) < 2:
        return jsonify([])
    suggestions = search_suggestions(query)
    return jsonify(suggestions)

@app.route('/api/booking/<int:booking_id>')
@login_required
def get_booking_api(booking_id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM bookings WHERE id = %s', (booking_id,))
        booking_row = c.fetchone()

        if not booking_row:
            return jsonify({'error': 'Booking not found'}), 404

        booking = row_to_dict(booking_row)

        if booking['service'] == 'Packed Lunch':
            c.execute('SELECT * FROM guest_meals WHERE booking_id = %s ORDER BY guest_number', (booking_id,))
            booking['guest_meals'] = rows_to_dicts(c.fetchall())
        else:
            booking['guest_meals'] = []

        if booking['is_deleted'] == 1:
            c.execute('''
                SELECT details FROM activity_log
                WHERE booking_id = %s AND action_type = 'delete'
                ORDER BY timestamp DESC LIMIT 1
            ''', (booking_id,))
            delete_log = c.fetchone()
            if delete_log:
                try:
                    details = json.loads(delete_log['details'])
                    booking['delete_reason'] = details.get('reason', 'No reason provided')
                except:
                    booking['delete_reason'] = str(delete_log['details'])
            else:
                booking['delete_reason'] = None

        conn.close()
        return jsonify(booking)
    except Exception as e:
        logger.error(f"API error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/booking/logs/<int:booking_id>')
@login_required
def booking_logs_api(booking_id):
    logs = get_logs_for_booking(booking_id)
    log_list = []
    for log in logs:
        log_list.append({
            'timestamp': log['timestamp'],
            'action_type': log['action_type'],
            'details': log['details']
        })
    return jsonify(log_list)

@app.route('/api/booking', methods=['POST'])
@login_required
def create_booking_api():
    try:
        data = request.json
        service = data.get('service')

        if service == 'Packed Lunch':
            conn = get_db()
            c = conn.cursor()
            c.execute('''
                INSERT INTO bookings
                (date, service, time, name, tel, guest_email, notes, guests, room, surname, collection_time, paid)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (data['date'], 'Packed Lunch', data['collection_time'],
                  data.get('name', ''), data.get('tel', ''),
                  data.get('guest_email', ''), data.get('notes', ''),
                  data['guest_count'], data.get('room', ''), data.get('surname', ''),
                  data.get('collection_time', ''), data.get('paid', 'Unpaid')))
            booking_id = c.fetchone()[0]

            for i, guest in enumerate(data.get('guests', []), 1):
                c.execute('''
                    INSERT INTO guest_meals
                    (booking_id, guest_number, filling, bread, dietary)
                    VALUES (%s, %s, %s, %s, %s)
                ''', (booking_id, i, guest.get('filling', ''),
                      guest.get('bread', ''), guest.get('dietary', '')))

            conn.commit()
            conn.close()

            log_activity('new', booking_id, data)
            send_hotel_notification(booking_id, 'CREATED')

            return jsonify({'success': True, 'id': booking_id, 'message': 'Packed lunch booking created'})

        else:
            conn = get_db()
            c = conn.cursor()
            c.execute('''
                INSERT INTO bookings
                (date, service, time, name, tel, guest_email, voucher, notes, dietary, paid, room, surname, guests)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (data['date'], service, data['time'], data['name'],
                  data.get('tel', ''), data.get('guest_email', ''),
                  data.get('voucher', ''), data.get('notes', ''),
                  data.get('dietary', ''), data.get('paid', 'Unpaid'),
                  data.get('room', ''), data.get('surname', ''),
                  data.get('guests', 1)))
            booking_id = c.fetchone()[0]
            conn.commit()
            conn.close()

            log_activity('new', booking_id, data)
            send_hotel_notification(booking_id, 'CREATED')

            return jsonify({'success': True, 'id': booking_id, 'message': 'Booking created'})

    except Exception as e:
        logger.error(f"API create error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/booking/<int:booking_id>', methods=['PUT'])
@login_required
def update_booking_api(booking_id):
    try:
        data = request.json
        conn = get_db()
        c = conn.cursor()

        c.execute('SELECT * FROM bookings WHERE id = %s', (booking_id,))
        old_data = dict(c.fetchone())

        if data.get('service') == 'Packed Lunch':
            c.execute('''
                UPDATE bookings SET
                    date = %s, service = %s, time = %s, name = %s, tel = %s, guest_email = %s,
                    notes = %s, guests = %s, room = %s, surname = %s, collection_time = %s, paid = %s
                WHERE id = %s
            ''', (data['date'], 'Packed Lunch', data['collection_time'],
                  data.get('name', ''), data.get('tel', ''),
                  data.get('guest_email', ''), data.get('notes', ''),
                  data['guest_count'], data.get('room', ''), data.get('surname', ''),
                  data.get('collection_time', ''), data.get('paid', 'Unpaid'), booking_id))

            c.execute('DELETE FROM guest_meals WHERE booking_id = %s', (booking_id,))
            for i, guest in enumerate(data.get('guests', []), 1):
                c.execute('''
                    INSERT INTO guest_meals
                    (booking_id, guest_number, filling, bread, dietary)
                    VALUES (%s, %s, %s, %s, %s)
                ''', (booking_id, i, guest.get('filling', ''),
                      guest.get('bread', ''), guest.get('dietary', '')))
        else:
            c.execute('''
                UPDATE bookings SET
                    date = %s, service = %s, time = %s, name = %s, tel = %s, guest_email = %s,
                    voucher = %s, notes = %s, dietary = %s, paid = %s, room = %s, surname = %s, guests = %s
                WHERE id = %s
            ''', (data['date'], data['service'], data['time'], data['name'],
                  data.get('tel', ''), data.get('guest_email', ''),
                  data.get('voucher', ''), data.get('notes', ''),
                  data.get('dietary', ''), data.get('paid', 'Unpaid'),
                  data.get('room', ''), data.get('surname', ''),
                  data.get('guests', 1), booking_id))

        conn.commit()
        conn.close()

        log_activity('edit', booking_id, {'old': old_data, 'new': data})
        send_hotel_notification(booking_id, 'UPDATED')

        return jsonify({'success': True, 'message': 'Booking updated'})
    except Exception as e:
        logger.error(f"API update error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/booking/<int:booking_id>', methods=['DELETE'])
@login_required
def delete_booking_api(booking_id):
    try:
        data = request.json or {}
        reason = data.get('reason', 'No reason provided')

        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM bookings WHERE id = %s', (booking_id,))
        old_data = dict(c.fetchone())

        c.execute('UPDATE bookings SET is_deleted = 1 WHERE id = %s', (booking_id,))
        conn.commit()
        conn.close()

        log_activity('delete', booking_id, {'old': old_data, 'reason': reason})
        send_hotel_notification(booking_id, 'DELETED')

        return jsonify({'success': True, 'message': 'Booking deleted'})
    except Exception as e:
        logger.error(f"API delete error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/booking/<int:booking_id>/restore', methods=['POST'])
@login_required
def restore_booking_api(booking_id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE bookings SET is_deleted = 0 WHERE id = %s', (booking_id,))
        conn.commit()
        conn.close()

        log_activity('restore', booking_id, {'restored': True})
        send_hotel_notification(booking_id, 'RESTORED')

        return jsonify({'success': True, 'message': 'Booking restored'})
    except Exception as e:
        logger.error(f"Restore error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/booking/<int:booking_id>/send_confirmation', methods=['POST'])
@login_required
def send_confirmation_api(booking_id):
    try:
        data = request.json
        guest_email = data.get('guest_email')
        is_cancellation = data.get('is_cancellation', False)

        if not guest_email:
            return jsonify({'error': 'Email address required'}), 400

        success = send_customer_confirmation(booking_id, guest_email, is_cancellation)

        if success:
            if not is_cancellation:
                conn = get_db()
                c = conn.cursor()
                c.execute('UPDATE bookings SET confirmation_sent = 1 WHERE id = %s', (booking_id,))
                conn.commit()
                conn.close()

            log_activity('email', booking_id, {'to': guest_email, 'type': 'cancellation' if is_cancellation else 'confirmation'})
            return jsonify({'success': True, 'message': 'Email sent'})
        else:
            return jsonify({'error': 'Failed to send email'}), 500
    except Exception as e:
        logger.error(f"Confirmation API error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/availability')
@login_required
def availability_api():
    date_str = request.args.get('date', get_today_date())
    availability = {}
    for service_name, times in SERVICES.items():
        slots = generate_time_slots(times['start'], times['end'])
        service_avail = {}
        for slot in slots:
            count = get_booking_count(date_str, service_name, slot)
            service_avail[slot] = {
                'count': count,
                'available': count < 2
            }
        availability[service_name] = service_avail
    return jsonify(availability)

@app.route('/export')
@login_required
def export_bookings():
    selected_date = request.args.get('date', get_today_date())
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''
            SELECT date, service, time, name, tel, guest_email, voucher, dietary, notes, paid, guests, room, surname
            FROM bookings WHERE date = %s AND is_deleted = 0 ORDER BY service, time
        ''', (selected_date,))
        rows = c.fetchall()
        conn.close()

        if not rows:
            flash(f'No bookings for {selected_date}', 'warning')
            return redirect(f'/?date={selected_date}')

        df = pd.DataFrame([dict(row) for row in rows])

        exports_dir = os.path.join(os.path.dirname(__file__), 'exports')
        os.makedirs(exports_dir, exist_ok=True)
        filename = f'craiglands_bookings_{selected_date}.xlsx'
        filepath = os.path.join(exports_dir, filename)
        df.to_excel(filepath, index=False)
        logger.info(f"Exported bookings for {selected_date}")
        return send_file(filepath, as_attachment=True)
    except Exception as e:
        logger.error(f"Export error: {e}")
        flash(f'Error exporting: {str(e)}', 'danger')
        return redirect(f'/?date={selected_date}')

@app.route('/import_auth_page')
def import_auth_page():
    return redirect(url_for('import_auth'))

@app.route('/import', methods=['GET', 'POST'])
@login_required
@admin_import_required
def import_bookings():
    if request.method == 'POST':
        file = request.files.get('file')
        mode = request.form.get('mode', 'append')
        if not file or not file.filename.endswith(('.xlsx', '.xls')):
            flash('Please upload a valid Excel file', 'danger')
            return redirect(url_for('import_bookings'))

        try:
            df = pd.read_excel(file)
            required = ['date', 'service', 'time', 'name']
            if not all(col in df.columns for col in required):
                flash('Excel must contain at least: date, service, time, name', 'danger')
                return redirect(url_for('import_bookings'))

            conn = get_db()
            c = conn.cursor()

            if mode == 'replace':
                c.execute('UPDATE bookings SET is_deleted = 1 WHERE is_deleted = 0')
                logger.info("All existing bookings soft-deleted for import replace")

            imported = 0
            skipped = 0
            today = get_today_date()
            for _, row in df.iterrows():
                if mode in ('future', 'future_unique') and row['date'] < today:
                    continue

                data = {
                    'date': row.get('date'),
                    'service': row.get('service'),
                    'name': row.get('name'),
                    'tel': row.get('tel', ''),
                    'guest_email': row.get('guest_email', ''),
                    'voucher': row.get('voucher', ''),
                    'notes': row.get('notes', ''),
                    'dietary': row.get('dietary', ''),
                    'paid': row.get('paid', 'Unpaid'),
                    'guests': row.get('guests', 1),
                    'room': row.get('room', ''),
                    'surname': row.get('surname', ''),
                    'filling': row.get('filling', ''),
                    'bread': row.get('bread', ''),
                    'collection_time': row.get('collection_time', ''),
                }
                if data['service'] == 'Packed Lunch':
                    data['time'] = data['collection_time']
                else:
                    data['time'] = row.get('time', '')

                if mode == 'future_unique':
                    c.execute('''
                        SELECT id FROM bookings
                        WHERE date = %s AND service = %s AND time = %s AND name = %s AND is_deleted = 0
                    ''', (data['date'], data['service'], data['time'], data['name']))
                    if c.fetchone():
                        skipped += 1
                        continue

                c.execute('''
                    INSERT INTO bookings
                    (date, service, time, name, tel, guest_email, voucher, notes, dietary, paid, guests, room, surname, filling, bread, collection_time)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                ''', (data['date'], data['service'], data['time'], data['name'],
                      data['tel'], data['guest_email'], data['voucher'], data['notes'],
                      data['dietary'], data['paid'], data['guests'], data['room'],
                      data['surname'], data['filling'], data['bread'], data['collection_time']))
                booking_id = c.fetchone()[0]

                if data['service'] == 'Packed Lunch' and data['guests'] > 0:
                    numbered_columns_exist = any(f'guest_{i}_filling' in df.columns for i in range(1, data['guests']+1))
                    if numbered_columns_exist:
                        for i in range(1, data['guests'] + 1):
                            filling_col = f'guest_{i}_filling'
                            bread_col = f'guest_{i}_bread'
                            dietary_col = f'guest_{i}_dietary'
                            guest_filling = row.get(filling_col, '')
                            guest_bread = row.get(bread_col, '')
                            guest_dietary = row.get(dietary_col, '') if dietary_col in df.columns else ''
                            c.execute('''
                                INSERT INTO guest_meals
                                (booking_id, guest_number, filling, bread, dietary)
                                VALUES (%s, %s, %s, %s, %s)
                            ''', (booking_id, i, guest_filling, guest_bread, guest_dietary))
                    else:
                        if data['guests'] == 1:
                            guest_filling = data.get('filling', '')
                            guest_bread = data.get('bread', '')
                            guest_dietary = data.get('dietary', '')
                            c.execute('''
                                INSERT INTO guest_meals
                                (booking_id, guest_number, filling, bread, dietary)
                                VALUES (%s, %s, %s, %s, %s)
                            ''', (booking_id, 1, guest_filling, guest_bread, guest_dietary))

                imported += 1

            conn.commit()
            conn.close()
            log_activity('import', details={'count': imported, 'skipped': skipped, 'mode': mode})
            if skipped > 0:
                flash(f'✅ Imported {imported} bookings, skipped {skipped} duplicates (mode: {mode})', 'success')
            else:
                flash(f'✅ Successfully imported {imported} bookings (mode: {mode})', 'success')
            return redirect(url_for('index'))
        except Exception as e:
            logger.error(f"Import error: {e}")
            flash(f'❌ Import failed: {str(e)}', 'danger')
            return redirect(url_for('import_bookings'))

    return render_template('import.html')

@app.route('/print')
@login_required
def print_view():
    selected_date = request.args.get('date', get_today_date())
    orientation = request.args.get('orientation', 'portrait')
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM bookings WHERE date = %s AND is_deleted = 0 ORDER BY service, time', (selected_date,))
        rows = c.fetchall()
        conn.close()
        bookings = get_bookings_with_meals(rows)
    except Exception as e:
        logger.error(f"Print error: {e}")
        bookings = []
    return render_template('print.html',
                           bookings=bookings,
                           selected_date=selected_date,
                           orientation=orientation,
                           services=SERVICES,
                           service_order=SERVICE_ORDER,
                           datetime=datetime)

@app.route('/deleted')
@login_required
def deleted_bookings():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM bookings WHERE is_deleted = 1 ORDER BY date DESC, service, time')
        rows = c.fetchall()
        conn.close()
        bookings = get_bookings_with_meals(rows)
    except Exception as e:
        logger.error(f"Deleted bookings error: {e}")
        bookings = []
        flash('Error loading deleted bookings', 'danger')
    return render_template('deleted.html',
                           bookings=bookings,
                           services=SERVICES,
                           service_order=SERVICE_ORDER,
                           datetime=datetime)

@app.route('/test_email')
@login_required
def test_email_route():
    try:
        send_future_bookings_backup("Manual Test")
        flash('✅ Test future bookings email sent successfully!', 'success')
    except Exception as e:
        flash(f'❌ Email test failed: {str(e)}', 'danger')
    return redirect('/')

@app.route('/cron_backup')
def cron_backup():
    """Trigger backup in background so cron job doesn't timeout"""
    def send_backup():
        try:
            send_future_bookings_backup("Cron")
        except Exception as e:
            logger.error(f"Background backup failed: {e}")
    
    thread = threading.Thread(target=send_backup)
    thread.start()
    return "Backup started", 200

@app.route('/test_simple_email')
@login_required
def test_simple_email():
    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_SENDER
        msg['To'] = EMAIL_RECIPIENT
        msg['Subject'] = "Test email from Craiglands"
        msg.attach(MIMEText("This is a test.", 'plain'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        return "Email sent successfully"
    except Exception as e:
        return f"Error: {str(e)}"

@app.route('/logs')
@login_required
def view_logs():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM activity_log ORDER BY timestamp DESC LIMIT 100')
        logs = c.fetchall()
        conn.close()
        return render_template('logs.html', logs=logs, datetime=datetime)
    except Exception as e:
        flash(f'Error loading logs: {str(e)}', 'danger')
        return redirect('/')

@app.route('/reports')
@login_required
def reports():
    view = request.args.get('view', 'upcoming')
    today = get_today_date()
    next_week = get_date_days_from_now(7)
    show_cancelled = request.args.get('show_cancelled', '0') == '1'

    try:
        conn = get_db()
        c = conn.cursor()

        if view == 'upcoming':
            if show_cancelled:
                c.execute('SELECT * FROM bookings WHERE date >= %s ORDER BY date, service, time', (today,))
            else:
                c.execute('SELECT * FROM bookings WHERE date >= %s AND is_deleted = 0 ORDER BY date, service, time', (today,))
            title = f"Upcoming Bookings (from {today})"
            start_date = today
            end_date = 'Future'
        else:
            start_date = request.args.get('start_date', today)
            end_date = request.args.get('end_date', next_week)
            if show_cancelled:
                c.execute('SELECT * FROM bookings WHERE date BETWEEN %s AND %s ORDER BY date DESC, service, time', (start_date, end_date))
            else:
                c.execute('SELECT * FROM bookings WHERE date BETWEEN %s AND %s AND is_deleted = 0 ORDER BY date DESC, service, time', (start_date, end_date))
            title = f"Bookings from {start_date} to {end_date}"

        rows = c.fetchall()
        conn.close()
        bookings = get_bookings_with_meals(rows)

    except Exception as e:
        logger.error(f"Reports error: {e}")
        bookings = []
        flash('Error loading reports', 'danger')
        title = "Error Loading Reports"
        start_date = today
        end_date = next_week

    return render_template('reports_unified.html',
                           bookings=bookings,
                           start_date=start_date,
                           end_date=end_date,
                           view=view,
                           title=title,
                           show_cancelled=show_cancelled,
                           services=SERVICES,
                           service_order=SERVICE_ORDER,
                           show_actions=True,
                           datetime=datetime)

@app.route('/export_range')
@login_required
def export_range():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    view = request.args.get('view', 'range')

    try:
        conn = get_db()
        if view == 'upcoming' or end_date == 'Future':
            today = get_today_date()
            query = 'SELECT * FROM bookings WHERE date >= %s AND is_deleted = 0 ORDER BY date, service, time'
            params = (today,)
            filename = f'craiglands_upcoming_{today}.xlsx'
        else:
            query = 'SELECT * FROM bookings WHERE date BETWEEN %s AND %s AND is_deleted = 0 ORDER BY date DESC, service, time'
            params = (start_date, end_date)
            filename = f'craiglands_bookings_{start_date}_to_{end_date}.xlsx'

        c = conn.cursor()
        c.execute(query, params)
        rows = c.fetchall()
        conn.close()

        if not rows:
            flash('No bookings to export', 'warning')
            return redirect('/reports')

        df = pd.DataFrame([dict(row) for row in rows])

        os.makedirs('exports', exist_ok=True)
        filepath = os.path.join('exports', filename)
        df.to_excel(filepath, index=False)
        return send_file(filepath, as_attachment=True)
    except Exception as e:
        flash(f'Error exporting: {str(e)}', 'danger')
        return redirect('/reports')

@app.route('/network')
@login_required
def network_info():
    hostname, ips = get_network_info()
    return f'''
    <h1>Craiglands Booking System - Network Access</h1>
    <p><strong>Computer Name:</strong> {hostname}</p>
    <p><strong>Local Access:</strong></p>
    <ul>
        <li><a href="http://localhost:5000">http://localhost:5000</a></li>
        <li><a href="http://127.0.0.1:5000">http://127.0.0.1:5000</a></li>
    </ul>
    <p><strong>Network Access:</strong></p>
    <ul>
        {"".join([f'<li><a href="http://{ip}:5000">http://{ip}:5000</a></li>' for ip in ips])}
        <li><a href="http://{hostname}:5000">http://{hostname}:5000</a></li>
    </ul>
    <p><a href="/">Back to main page</a></p>
    '''

# ============================================
# START APPLICATION
# ============================================
if __name__ == '__main__':
    print("=" * 70)
    print("CRAIGLANDS BOOKING SYSTEM - NETWORK SERVER")
    print("=" * 70)
    hostname, ips = get_network_info()
    for folder in ['templates', 'exports', 'logs', 'backups']:
        os.makedirs(folder, exist_ok=True)
    print("\n✓ Database ready")
    print("\n" + "=" * 70)
    print("ACCESS INFORMATION:")
    print("=" * 70)
    print(f"\nComputer Name: {hostname}")
    print("\nLOCAL ACCESS (this computer):")
    print("  • http://localhost:5000")
    print("  • http://127.0.0.1:5000")
    if ips:
        print("\nNETWORK ACCESS (other computers):")
        for ip in ips:
            print(f"  • http://{ip}:5000")
        print(f"  • http://{hostname}:5000")
    print("\n" + "=" * 70)
    print("DATA LOCATION: PostgreSQL database")
    print("=" * 70)
    print("\nStarting server... (Press Ctrl+C to stop)")
    try:
        app.run(host=HOST, port=PORT, debug=DEBUG)
    except Exception as e:
        print(f"\nError: {e}")
        print("Trying alternative port 5001...")
        try:
            app.run(host=HOST, port=5001, debug=DEBUG)
        except Exception as e2:
            print(f"Failed: {e2}")
            input("Press Enter to exit...")
