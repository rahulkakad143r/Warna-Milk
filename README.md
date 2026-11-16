# Warana Milk - Inventory & Billing System

A Django 5.2-based billing and inventory system for small dairy/bakery shops. It features a clean brown and cream UI, live cart, checkout with multiple payment modes, automatic stock deduction, receipts, and simple reports.

## Features

- Authentication - login for admin/cashier roles
- Dashboard - today's sales, bill counts, products, stock units, 7-day sales chart, low stock, top sellers
- Point of Sale (POS) - product cards with images or category-themed tiles
- Cart & Checkout - shared session cart, inline qty edits, product-wise discounts and GST, live subtotal/discount/tax/payable, Cash/Card/UPI
- Billing - auto bill numbers, atomic stock deduction, product-wise tax on receipts, printable A4-friendly receipt
- Inventory - stock levels, reorder points, low-stock warnings
- Product Management - add/edit products with ID, stock, rate / unit, images, pricing, categories, individual GST rates and automatic discount percentages; every category carries a one-line description
- Suppliers & Purchases - default Warana Milk supplier, purchase entry with tax totals, purchase edits that adjust stock
- Reports - Customer, Product, Stock and Bill reports with Warana Milk letterhead PDF and CSV exports, plus daily, monthly and yearly sales, purchases, tax, bill counts, payment split and best sellers
- Responsive UI - collapsible sidebar on smaller screens
- No JS framework - charts/totals in plain JS (static/bakery/js/app.js)

## Tech Stack

- Python 3.11+/3.14 (tested on 3.14)
- Django 5.2.x
- SQLite (default) or MySQL
- Pillow, reportlab
- HTML/CSS/JS (vanilla)

## Quick Start

### 1) Clone & setup environment

python -m venv venv
.\venv\Scripts\activate

pip install -r requirements.txt

### 2) Configure database

Option A: SQLite (no setup) - works out of the box.

Option B: MySQL (recommended)

mysql -u root -e 'CREATE DATABASE warana_milk CHARACTER SET utf8mb4;'

='warana_milk'
='root'
='your_password'
='127.0.0.1'

### 3) Run migrations & seed data

python manage.py migrate
python manage.py seed_data

### 4) Start server

python manage.py runserver

Open http://127.0.0.1:8000/

### Default credentials

- admin / admin123
- cashier / cashier123

Django admin: http://127.0.0.1:8000/admin/

## Common Commands

python manage.py check
python manage.py test  (runs 95 tests)
python manage.py collectstatic --noinput
python manage.py import_product_images --folder 'C:/path/to/shots' --dry-run
python manage.py import_product_images --folder 'C:/path/to/shots'

## Key Files

- bakery/models.py - models (Category, Product, Inventory, Customer, Cart, CartItem, Bill, BillItem, Payment)
- bakery/views.py - all views
- bakery/billing.py - bill numbering, totals, atomic stock deduction
- bakery/forms.py - validation forms
- bakery/urls.py - URL routes
- bakery/templates/bakery/ - HTML templates
- static/bakery/css/style.css - theme styles
- static/bakery/js/app.js - client-side logic/charts
- bakery/management/commands/seed_data.py - demo data
- bakery/management/commands/import_product_images.py - bulk image import

## Product Images

Resolution order for product artwork (shared partial _product_art.html):
1. product.image (uploaded file)
2. product.image_url (remote URL)
3. Category-themed gradient tile

Uploaded files go to media/products/.

## Notes

- Without MYSQL_* env vars, it falls back to SQLite (db.sqlite3).
- Tests all pass locally.
