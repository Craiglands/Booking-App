# Craiglands Booking System

A web‑based booking management system for Craiglands Hotel.  
Built with Flask, SQLite, and Bootstrap.

## Features
- Create, edit, and cancel bookings for **Lunch/Afternoon Tea**, **Dinner**, and **Packed Lunch**.
- Daily availability view with slot limits (max 2 bookings per 15‑minute slot).
- Export to Excel, print daily sheet.
- Email confirmations to guests and hotel notifications.
- Activity log for audit trail.
- Daily backup email (future bookings) – triggered by a cron job on Render.

## Local Development

### Requirements
- Python 3.10 (or later)
- Install dependencies: `pip install -r requirements.txt`

### Run locally
```bash
python app.py