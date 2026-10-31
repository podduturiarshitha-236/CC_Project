import os, re, uuid
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify, abort)
from pymongo import MongoClient
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = "change-this-secret-key"
ADMIN_USER, ADMIN_PASS = "admin", "admin123"

client = MongoClient("mongodb://localhost:27017/")
db = client["lost_found_db"]
items, claims, logs = db["items"], db["claims"], db["logs"]

UPLOAD = os.path.join("static", "uploads")
os.makedirs(UPLOAD, exist_ok=True)
CATEGORIES = ["ID/Cards", "Electronics", "Books", "Bags", "Keys", "Clothing", "Other"]

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log(item_id, event):
    logs.insert_one({"time": now(), "item_id": item_id, "event": event})

def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return f(*a, **kw)
    return wrapper

# ---------- Dashboard (replaces OLED) ----------
@app.route("/")
def index():
    stats = {
        "found": items.count_documents({"type": "found"}),
        "lost": items.count_documents({"type": "lost"}),
        "in_box": items.count_documents({"status": "Checked In"}),
        "returned": items.count_documents({"status": "Returned"}),
    }
    recent = list(items.find({}, {"_id": 0}).sort("created", -1).limit(6))
    activity = list(logs.find({}, {"_id": 0}).sort("time", -1).limit(8))
    return render_template("index.html", stats=stats, recent=recent, activity=activity)

# ---------- Report lost / Register found ----------
def item_form(kind):
    if request.method == "POST":
        f = request.form
        filename = ""
        img = request.files.get("image")
        if img and img.filename:
            filename = uuid.uuid4().hex[:6] + "_" + secure_filename(img.filename)
            img.save(os.path.join(UPLOAD, filename))
        item_id = "ITM-" + uuid.uuid4().hex[:8].upper()
        items.insert_one({
            "item_id": item_id, "type": kind,
            "name": f["name"].strip(), "category": f["category"],
            "description": f["description"].strip(), "location": f["location"].strip(),
            "date": f["date"], "contact": f["contact"].strip(), "image": filename,
            "status": "Checked In" if kind == "found" else "Reported",
            "created": now(),
        })
        log(item_id, "Found item registered (checked in)" if kind == "found"
            else "Lost item reported")
        flash(f"Saved! Your Item ID is {item_id}", "success")
        return redirect(url_for("item_page", item_id=item_id))
    return render_template("item_form.html", kind=kind, categories=CATEGORIES)

@app.route("/report-lost", methods=["GET", "POST"])
def report_lost():
    return item_form("lost")

@app.route("/register-found", methods=["GET", "POST"])
def register_found():
    return item_form("found")

# ---------- Search ----------
@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    kind = request.args.get("type", "")
    cat = request.args.get("category", "")
    query = {}
    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        query["$or"] = [{"name": rx}, {"description": rx},
                        {"location": rx}, {"item_id": rx}]
    if kind: query["type"] = kind
    if cat: query["category"] = cat
    results = list(items.find(query, {"_id": 0}).sort("created", -1))
    return render_template("search.html", results=results, q=q, kind=kind,
                           cat=cat, categories=CATEGORIES)

# ---------- Item details + claim ----------
@app.route("/item/<item_id>")
def item_page(item_id):
    item = items.find_one({"item_id": item_id}, {"_id": 0})
    if not item: abort(404)
    return render_template("item.html", item=item)

@app.route("/claim/<item_id>", methods=["POST"])
def claim(item_id):
    item = items.find_one({"item_id": item_id})
    if not item or item["type"] != "found" or item["status"] != "Checked In":
        flash("This item is not available for claiming.", "error")
        return redirect(url_for("item_page", item_id=item_id))
    f = request.form
    claims.insert_one({
        "claim_id": "CLM-" + uuid.uuid4().hex[:8].upper(), "item_id": item_id,
        "claimant_name": f["name"].strip(), "roll_no": f["roll_no"].strip(),
        "email": f["email"].strip(), "proof": f["proof"].strip(),
        "status": "Pending", "created": now(),
    })
    items.update_one({"item_id": item_id}, {"$set": {"status": "Claim Pending"}})
    log(item_id, f"Claim submitted by {f['name'].strip()}")
    flash("Claim submitted. The admin will verify your ownership.", "success")
    return redirect(url_for("item_page", item_id=item_id))

# ---------- Admin ----------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form["username"] == ADMIN_USER and request.form["password"] == ADMIN_PASS:
            session["admin"] = True
            return redirect(url_for("admin"))
        flash("Invalid credentials", "error")
    return render_template("admin_login.html")

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/admin")
@admin_required
def admin():
    pending = list(claims.find({"status": "Pending"}, {"_id": 0}).sort("created", 1))
    for c in pending:
        c["item"] = items.find_one({"item_id": c["item_id"]}, {"_id": 0})
    all_items = list(items.find({}, {"_id": 0}).sort("created", -1))
    return render_template("admin.html", pending=pending, all_items=all_items)

@app.route("/admin/claim/<claim_id>/<action>", methods=["POST"])
@admin_required
def decide(claim_id, action):
    c = claims.find_one({"claim_id": claim_id})
    if not c or action not in ("approve", "reject"): abort(404)
    if action == "approve":
        claims.update_one({"claim_id": claim_id}, {"$set": {"status": "Approved"}})
        items.update_one({"item_id": c["item_id"]}, {"$set": {
            "status": "Returned", "returned_to": c["claimant_name"], "returned_on": now()}})
        log(c["item_id"], f"Ownership verified. Returned to {c['claimant_name']}")
    else:
        claims.update_one({"claim_id": claim_id}, {"$set": {"status": "Rejected"}})
        items.update_one({"item_id": c["item_id"]}, {"$set": {"status": "Checked In"}})
        log(c["item_id"], f"Claim by {c['claimant_name']} rejected")
    return redirect(url_for("admin"))

# Simulated RFID scan: enter an Item ID and press Check-in or Check-out
@app.route("/admin/scan", methods=["POST"])
@admin_required
def scan():
    item_id = request.form["item_id"].strip().upper()
    action = request.form["action"]
    if not items.find_one({"item_id": item_id}):
        flash("Item ID not found.", "error")
    elif action == "checkin":
        items.update_one({"item_id": item_id}, {"$set": {"status": "Checked In"}})
        log(item_id, "Checked IN (scan)")
        flash(f"{item_id} checked in.", "success")
    else:
        items.update_one({"item_id": item_id}, {"$set": {"status": "Checked Out"}})
        log(item_id, "Checked OUT (scan)")
        flash(f"{item_id} checked out.", "success")
    return redirect(url_for("admin"))

# ---------- JSON API (replaces AWS Lambda endpoints) ----------
@app.route("/api/items")
def api_items():
    return jsonify(list(items.find({}, {"_id": 0}).sort("created", -1)))

@app.route("/api/items/<item_id>")
def api_item(item_id):
    item = items.find_one({"item_id": item_id}, {"_id": 0})
    return (jsonify(item), 200) if item else (jsonify({"error": "not found"}), 404)

if __name__ == "__main__":
    app.run(debug=True)
