import os
import re
from datetime import datetime, timezone
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

API_KEY = os.getenv("RENTCAST_API_KEY")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
ALERT_FROM_EMAIL = os.getenv("ALERT_FROM_EMAIL", "Nestrova Alerts <onboarding@resend.dev>")
NESTROVA_APP_URL = os.getenv("NESTROVA_APP_URL", "https://home-deal-ai.vercel.app")

FREE_ANALYZE_MONTHLY_LIMIT = int(os.getenv("FREE_ANALYZE_MONTHLY_LIMIT", "5"))
FREE_FIND_DEALS_MONTHLY_LIMIT = int(os.getenv("FREE_FIND_DEALS_MONTHLY_LIMIT", "1"))
PRO_ANALYZE_MONTHLY_LIMIT = int(os.getenv("PRO_ANALYZE_MONTHLY_LIMIT", "50"))
PRO_FIND_DEALS_MONTHLY_LIMIT = int(os.getenv("PRO_FIND_DEALS_MONTHLY_LIMIT", "10"))

headers = {"X-Api-Key": API_KEY}

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://home-deal-ai.vercel.app",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AnalyzeRequest(BaseModel):
    address: str
    listing_price: float
    down_payment_percent: float = 25
    interest_rate: float = 6.5
    loan_term_years: int = 30
    user_id: str | None = None
    is_pro: bool = False


class FindDealsRequest(BaseModel):
    city: str
    state: str
    max_price: int
    limit: int = 5
    is_pro: bool = False
    user_id: str | None = None


class RunAlertsRequest(BaseModel):
    max_alerts: int = 25


# -----------------------------
# Cost protection + cache helpers
# -----------------------------

def normalize_address(address):
    return re.sub(r"\s+", " ", address.strip().lower())


def make_cache_key(address, listing_price, down_payment_percent=25, interest_rate=6.5, loan_term_years=30):
    normalized_address = normalize_address(address)
    return (
        f"{normalized_address}|"
        f"price:{round(float(listing_price), 2)}|"
        f"down:{round(float(down_payment_percent), 2)}|"
        f"rate:{round(float(interest_rate), 3)}|"
        f"term:{int(loan_term_years)}"
    )


def get_cached_property(cache_key):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None

    try:
        response = requests.get(
            f"{SUPABASE_URL}/rest/v1/property_cache",
            headers=get_supabase_headers(),
            params={
                "cache_key": f"eq.{cache_key}",
                "select": "result",
                "limit": "1",
            },
            timeout=10,
        )

        if response.status_code != 200:
            return None

        rows = response.json()
        if not rows:
            return None

        result = rows[0].get("result")
        if isinstance(result, dict):
            result["cache_status"] = "hit"
        return result

    except Exception:
        return None


def save_cached_property(cache_key, address, listing_price, result):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return

    try:
        result_to_store = dict(result)
        result_to_store["cache_status"] = "stored"

        payload = {
            "cache_key": cache_key,
            "address": address,
            "listing_price": listing_price,
            "result": result_to_store,
        }

        requests.post(
            f"{SUPABASE_URL}/rest/v1/property_cache",
            headers={
                **get_supabase_headers(),
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
            json=payload,
            timeout=10,
        )
    except Exception:
        return


def calculate_monthly_mortgage(listing_price, down_payment_percent, interest_rate, loan_term_years):
    down_payment = listing_price * (down_payment_percent / 100)
    loan_amount = listing_price - down_payment
    monthly_rate = (interest_rate / 100) / 12
    total_months = loan_term_years * 12

    if monthly_rate == 0:
        monthly_payment = loan_amount / total_months
    else:
        monthly_payment = (
            loan_amount * monthly_rate * (1 + monthly_rate) ** total_months
        ) / ((1 + monthly_rate) ** total_months - 1)

    return monthly_payment, loan_amount, down_payment


def calculate_deal_score(discount_percent, gross_rent_yield, year_built, cash_flow):
    score = 0
    reasons = []

    if discount_percent >= 10:
        score += 40
        reasons.append("More than 10% below Fair Value (+40)")
    elif discount_percent >= 5:
        score += 30
        reasons.append("More than 5% below Fair Value (+30)")
    elif discount_percent >= 0:
        score += 15
        reasons.append("Slightly below Fair Value (+15)")
    else:
        score -= 10
        reasons.append("Above Fair Value (-10)")

    if gross_rent_yield >= 6:
        score += 30
        reasons.append("Rental yield above 6% (+30)")
    elif gross_rent_yield >= 4:
        score += 20
        reasons.append("Rental yield above 4% (+20)")
    elif gross_rent_yield >= 3:
        score += 10
        reasons.append("Rental yield above 3% (+10)")
    else:
        reasons.append("Low rental yield (+0)")

    if year_built >= 2015:
        score += 15
        reasons.append("Relatively new property (+15)")
    elif year_built >= 2000:
        score += 10
        reasons.append("Built after 2000 (+10)")
    elif year_built >= 1980:
        score += 5
        reasons.append("Older but acceptable condition (+5)")
    else:
        score -= 5
        reasons.append("Aging property (-5)")

    if cash_flow >= 500:
        score += 15
        reasons.append("Strong positive monthly cash flow (+15)")
    elif cash_flow >= 0:
        score += 8
        reasons.append("Positive monthly cash flow (+8)")
    elif cash_flow >= -500:
        score -= 5
        reasons.append("Slightly negative monthly cash flow (-5)")
    else:
        score -= 15
        reasons.append("Weak monthly cash flow (-15)")

    return max(0, min(score, 100)), reasons


def calculate_forecast_score(discount_percent, gross_rent_yield, deal_score, cash_flow, year_built):
    score = 50
    reasons = []

    if discount_percent >= 10:
        score += 18
        reasons.append("Property appears significantly undervalued versus estimated fair value.")
    elif discount_percent >= 5:
        score += 12
        reasons.append("Property appears moderately undervalued versus estimated fair value.")
    elif discount_percent < -5:
        score -= 12
        reasons.append("Property appears overpriced versus estimated fair value.")

    if gross_rent_yield >= 6:
        score += 12
        reasons.append("Strong rental yield supports investment demand.")
    elif gross_rent_yield >= 4:
        score += 7
        reasons.append("Moderate rental yield supports stable investment potential.")
    else:
        score -= 6
        reasons.append("Low rental yield may limit investor demand.")

    if deal_score >= 80:
        score += 12
        reasons.append("High deal score indicates strong overall investment quality.")
    elif deal_score >= 65:
        score += 7
        reasons.append("Good deal score indicates above-average investment quality.")
    elif deal_score < 45:
        score -= 8
        reasons.append("Low deal score suggests weaker investment quality.")

    if cash_flow >= 500:
        score += 10
        reasons.append("Strong positive cash flow improves holding potential.")
    elif cash_flow >= 0:
        score += 5
        reasons.append("Positive cash flow improves holding stability.")
    elif cash_flow < -500:
        score -= 10
        reasons.append("Weak cash flow may reduce investment attractiveness.")

    if year_built >= 2015:
        score += 5
        reasons.append("Newer property may reduce maintenance risk.")
    elif year_built < 1980:
        score -= 5
        reasons.append("Older property may carry higher repair risk.")

    score = max(0, min(score, 100))

    if score >= 80:
        outlook = "Strong Growth Potential"
    elif score >= 65:
        outlook = "Growth Potential"
    elif score >= 45:
        outlook = "Stable Outlook"
    elif score >= 25:
        outlook = "Limited Growth"
    else:
        outlook = "Weak Outlook"

    return score, outlook, reasons


def calculate_neighborhood_score(gross_rent_yield, cash_flow, year_built, deal_score, forecast_score):
    score = 50
    reasons = []

    if gross_rent_yield >= 6:
        score += 15
        reasons.append("Strong rental yield suggests healthy rental demand.")
    elif gross_rent_yield >= 4:
        score += 10
        reasons.append("Moderate rental yield suggests stable rental demand.")
    else:
        score -= 8
        reasons.append("Low rental yield may indicate weaker rental demand.")

    if cash_flow >= 500:
        score += 15
        reasons.append("Strong cash flow supports long-term holding strength.")
    elif cash_flow >= 0:
        score += 8
        reasons.append("Positive cash flow supports investment stability.")
    else:
        score -= 10
        reasons.append("Negative cash flow may create holding risk.")

    if year_built >= 2015:
        score += 12
        reasons.append("Newer property may reduce repair and maintenance risk.")
    elif year_built >= 2000:
        score += 8
        reasons.append("Relatively modern property condition.")
    elif year_built < 1980:
        score -= 8
        reasons.append("Older property may require more maintenance review.")

    if deal_score >= 80:
        score += 10
        reasons.append("High deal score supports a strong local investment profile.")
    elif deal_score >= 65:
        score += 6
        reasons.append("Good deal score supports a positive investment profile.")
    elif deal_score < 45:
        score -= 8
        reasons.append("Lower deal score weakens the investment profile.")

    if forecast_score >= 80:
        score += 8
        reasons.append("Strong appreciation outlook supports neighborhood potential.")
    elif forecast_score >= 65:
        score += 5
        reasons.append("Positive appreciation outlook supports neighborhood potential.")
    elif forecast_score < 45:
        score -= 6
        reasons.append("Weak appreciation outlook may limit neighborhood upside.")

    score = max(0, min(score, 100))

    if score >= 85:
        grade = "Excellent Neighborhood Profile"
    elif score >= 70:
        grade = "Strong Neighborhood Profile"
    elif score >= 55:
        grade = "Stable Neighborhood Profile"
    elif score >= 40:
        grade = "Mixed Neighborhood Profile"
    else:
        grade = "Weak Neighborhood Profile"

    return score, grade, reasons

def calculate_appreciation_forecast(forecast_score, deal_score, neighborhood_score):
    if forecast_score >= 90:
        appreciation = 10.0
    elif forecast_score >= 80:
        appreciation = 7.0
    elif forecast_score >= 70:
        appreciation = 5.0
    elif forecast_score >= 60:
        appreciation = 3.0
    elif forecast_score >= 50:
        appreciation = 1.0
    elif forecast_score >= 40:
        appreciation = -1.0
    else:
        appreciation = -3.5

    confidence = round((forecast_score + deal_score + neighborhood_score) / 3)

    return round(appreciation, 1), confidence


def calculate_overall_score(deal_score, forecast_score, neighborhood_score):
    return round(
        deal_score * 0.4
        + forecast_score * 0.35
        + neighborhood_score * 0.25
    )


def generate_summary(status, gross_rent_yield, year_built, cash_flow):
    summary = ""

    if status == "UNDERVALUED":
        summary += "This property appears to be priced below its estimated fair value. "
    elif status == "OVERPRICED":
        summary += "This property appears to be priced above its estimated fair value. "
    else:
        summary += "This property appears fairly priced based on available data. "

    if gross_rent_yield >= 5:
        summary += "The rental yield is attractive for investment purposes. "
    elif gross_rent_yield >= 3:
        summary += "The rental yield is moderate and may appeal to long-term investors. "
    else:
        summary += "The rental yield is relatively low compared to many investment properties. "

    if cash_flow >= 0:
        summary += "The estimated monthly cash flow is positive based on the assumptions provided. "
    else:
        summary += "The estimated monthly cash flow is negative based on the assumptions provided. "

    if year_built >= 2015:
        summary += "The property is relatively new, which may reduce maintenance costs."
    elif year_built >= 2000:
        summary += "The property is not very old, but maintenance costs should still be reviewed."
    else:
        summary += "The property is older, so maintenance and repair risks should be reviewed carefully."

    return summary


def analyze_single_property_uncached(address, listing_price, down_payment_percent=25, interest_rate=6.5, loan_term_years=30):
    value_response = requests.get(
        "https://api.rentcast.io/v1/avm/value",
        headers=headers,
        params={"address": address},
        timeout=15,
    )

    if value_response.status_code != 200:
        raise HTTPException(status_code=400, detail="Could not get fair value data for this address.")

    value_data = value_response.json()

    fair_value = value_data.get("price")
    low_value = value_data.get("priceRangeLow")
    high_value = value_data.get("priceRangeHigh")
    year_built = value_data.get("subjectProperty", {}).get("yearBuilt", 1990)

    rent_response = requests.get(
        "https://api.rentcast.io/v1/avm/rent/long-term",
        headers=headers,
        params={"address": address},
        timeout=15,
    )

    if rent_response.status_code != 200:
        raise HTTPException(status_code=400, detail="Could not get rent estimate data for this address.")

    rent_data = rent_response.json()
    monthly_rent = rent_data.get("rent")

    if not fair_value or not monthly_rent:
        raise HTTPException(status_code=400, detail="Missing property value or rent data.")

    annual_rent = monthly_rent * 12

    monthly_mortgage, loan_amount, down_payment = calculate_monthly_mortgage(
        listing_price,
        down_payment_percent,
        interest_rate,
        loan_term_years,
    )

    monthly_property_tax = (listing_price * 0.0125) / 12
    monthly_insurance = (listing_price * 0.0035) / 12
    monthly_maintenance = (listing_price * 0.01) / 12

    monthly_cash_flow = monthly_rent - monthly_mortgage - monthly_property_tax - monthly_insurance - monthly_maintenance

    discount_percent = ((fair_value - listing_price) / fair_value) * 100
    gross_rent_yield = (annual_rent / listing_price) * 100

    if discount_percent >= 5:
        status = "UNDERVALUED"
    elif discount_percent <= -5:
        status = "OVERPRICED"
    else:
        status = "FAIR PRICE"

    deal_score, reasons = calculate_deal_score(
        discount_percent,
        gross_rent_yield,
        year_built,
        monthly_cash_flow,
    )

    forecast_score, forecast_outlook, forecast_reasons = calculate_forecast_score(
        discount_percent,
        gross_rent_yield,
        deal_score,
        monthly_cash_flow,
        year_built,
    )

    neighborhood_score, neighborhood_grade, neighborhood_reasons = calculate_neighborhood_score(
        gross_rent_yield,
        monthly_cash_flow,
        year_built,
        deal_score,
        forecast_score,
    )
    
    expected_appreciation, confidence_score = calculate_appreciation_forecast(
        forecast_score,
        deal_score,
        neighborhood_score,
    )

    overall_score = calculate_overall_score(
        deal_score,
        forecast_score,
        neighborhood_score,
    )
    
    summary = generate_summary(status, gross_rent_yield, year_built, monthly_cash_flow)

    return {
        "address": address,
        "listing_price": round(listing_price, 2),
        "fair_value": round(fair_value, 2),
        "fair_value_low": round(low_value or fair_value, 2),
        "fair_value_high": round(high_value or fair_value, 2),
        "estimated_monthly_rent": round(monthly_rent, 2),
        "discount_percent": round(discount_percent, 2),
        "gross_rent_yield": round(gross_rent_yield, 2),
        "status": status,
        "deal_score": deal_score,
        "reasons": reasons,
        "summary": summary,
        "forecast_score": forecast_score,
        "forecast_outlook": forecast_outlook,
        "forecast_reasons": forecast_reasons,
        "neighborhood_score": neighborhood_score,
        "neighborhood_grade": neighborhood_grade,
        "neighborhood_reasons": neighborhood_reasons,
        "expected_appreciation": expected_appreciation,
        "confidence_score": confidence_score,
        "overall_score": overall_score,
        "down_payment": round(down_payment, 2),
        "loan_amount": round(loan_amount, 2),
        "monthly_mortgage": round(monthly_mortgage, 2),
        "monthly_property_tax": round(monthly_property_tax, 2),
        "monthly_insurance": round(monthly_insurance, 2),
        "monthly_maintenance": round(monthly_maintenance, 2),
        "estimated_monthly_cash_flow": round(monthly_cash_flow, 2),
        "cache_status": "miss",
    }


def analyze_single_property(address, listing_price, down_payment_percent=25, interest_rate=6.5, loan_term_years=30):
    cache_key = make_cache_key(
        address,
        listing_price,
        down_payment_percent,
        interest_rate,
        loan_term_years,
    )

    cached_result = get_cached_property(cache_key)
    if cached_result:
        return cached_result

    result = analyze_single_property_uncached(
        address,
        listing_price,
        down_payment_percent,
        interest_rate,
        loan_term_years,
    )
    save_cached_property(cache_key, address, listing_price, result)
    return result


def get_supabase_headers():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(
            status_code=500,
            detail="Supabase environment variables are missing.",
        )

    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


def get_active_alerts(limit=25):
    supabase_headers = get_supabase_headers()

    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/deal_alerts",
        headers=supabase_headers,
        params={
            "select": "*",
            "is_active": "eq.true",
            "limit": limit,
            "order": "created_at.desc",
        },
        timeout=20,
    )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load alerts: {response.text}",
        )

    return response.json()


def send_deal_alert_email(alert, deals):
    if not RESEND_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="RESEND_API_KEY is missing.",
        )

    email = alert.get("email")
    if not email:
        return {
            "sent": False,
            "reason": "Alert has no email address.",
        }

    city = alert.get("city", "")
    state = alert.get("state", "")
    best_deal = deals[0]

    subject = f"New high-score deal found in {city}, {state}"

    appreciation = best_deal.get("expected_appreciation", 0)
    appreciation_text = f"+{appreciation}%" if appreciation and appreciation > 0 else f"{appreciation}%"

    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 640px; margin: 0 auto; color: #111827;">
      <h1 style="font-size: 26px; margin-bottom: 8px;">New high-score deal found</h1>
      <p style="font-size: 16px; color: #4b5563;">
        Nestrova found a property that matches your alert for <strong>{city}, {state}</strong>.
      </p>

      <div style="border: 1px solid #e5e7eb; border-radius: 16px; padding: 20px; margin-top: 20px;">
        <p style="font-size: 13px; color: #6b7280; margin: 0 0 8px;">Top Match</p>
        <h2 style="font-size: 22px; margin: 0 0 12px;">{best_deal.get("address", "Unknown address")}</h2>

        <p style="font-size: 16px; margin: 6px 0;"><strong>Overall Score:</strong> {best_deal.get("overall_score", "N/A")}/100</p>
        <p style="font-size: 16px; margin: 6px 0;"><strong>Deal Score:</strong> {best_deal.get("deal_score", "N/A")}/100</p>
        <p style="font-size: 16px; margin: 6px 0;"><strong>Expected Appreciation:</strong> {appreciation_text}</p>
        <p style="font-size: 16px; margin: 6px 0;"><strong>Cash Flow:</strong> ${round(best_deal.get("estimated_monthly_cash_flow", 0)):,}/mo</p>
        <p style="font-size: 16px; margin: 6px 0;"><strong>Price:</strong> ${round(best_deal.get("listing_price", 0)):,}</p>
      </div>

      <p style="margin-top: 24px;">
        <a href="{NESTROVA_APP_URL}" style="display: inline-block; background: #111827; color: white; padding: 12px 18px; border-radius: 10px; text-decoration: none; font-weight: bold;">
          View on Nestrova
        </a>
      </p>

      <p style="font-size: 12px; color: #6b7280; margin-top: 28px;">
        This alert is for informational purposes only and is not financial advice.
      </p>
    </div>
    """

    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": ALERT_FROM_EMAIL,
            "to": [email],
            "subject": subject,
            "html": html,
        },
        timeout=20,
    )

    if response.status_code >= 400:
        return {
            "sent": False,
            "email": email,
            "reason": response.text,
        }

    return {
        "sent": True,
        "email": email,
        "resend_response": response.json(),
    }


def get_month_key():
    now = datetime.now(timezone.utc)
    return f"{now.year}-{str(now.month).zfill(2)}"


def get_usage_limit(action, is_pro):
    if action == "analyze":
        return PRO_ANALYZE_MONTHLY_LIMIT if is_pro else FREE_ANALYZE_MONTHLY_LIMIT

    if action == "find_deals":
        return PRO_FIND_DEALS_MONTHLY_LIMIT if is_pro else FREE_FIND_DEALS_MONTHLY_LIMIT

    return 0


def get_usage_counts(user_id):
    if not user_id:
        return {
            "analyze_count": 0,
            "find_deals_count": 0,
            "month_key": get_month_key(),
        }

    supabase_headers = get_supabase_headers()
    month_key = get_month_key()

    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/usage_limits",
        headers=supabase_headers,
        params={
            "user_id": f"eq.{user_id}",
            "month_key": f"eq.{month_key}",
            "select": "*",
            "limit": "1",
        },
        timeout=10,
    )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load usage limits: {response.text}",
        )

    rows = response.json()
    if not rows:
        return {
            "analyze_count": 0,
            "find_deals_count": 0,
            "month_key": month_key,
        }

    row = rows[0]
    return {
        "analyze_count": int(row.get("analyze_count") or 0),
        "find_deals_count": int(row.get("find_deals_count") or 0),
        "month_key": month_key,
    }


def enforce_usage_limit(user_id, action, is_pro):
    # Backward-compatible safety:
    # If user_id is not provided, do not break old frontend/docs tests.
    # The frontend should send Clerk user.id next so this becomes fully enforced per user.
    if not user_id:
        return {
            "allowed": True,
            "tracked": False,
            "reason": "No user_id provided; usage tracking skipped.",
        }

    counts = get_usage_counts(user_id)
    limit = get_usage_limit(action, is_pro)

    current_count = (
        counts["analyze_count"]
        if action == "analyze"
        else counts["find_deals_count"]
    )

    if current_count >= limit:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Monthly {action.replace('_', ' ')} limit reached. "
                f"Limit: {limit}. Upgrade to Pro to continue."
            ),
        )

    return {
        "allowed": True,
        "tracked": True,
        "month_key": counts["month_key"],
        "current_count": current_count,
        "limit": limit,
        "remaining_before_request": max(limit - current_count, 0),
    }


def increment_usage(user_id, action, is_pro):
    if not user_id:
        return None

    counts = get_usage_counts(user_id)
    month_key = counts["month_key"]

    next_analyze_count = counts["analyze_count"]
    next_find_deals_count = counts["find_deals_count"]

    if action == "analyze":
        next_analyze_count += 1
        limit = get_usage_limit("analyze", is_pro)
        current = next_analyze_count
    else:
        next_find_deals_count += 1
        limit = get_usage_limit("find_deals", is_pro)
        current = next_find_deals_count

    payload = {
        "user_id": user_id,
        "month_key": month_key,
        "analyze_count": next_analyze_count,
        "find_deals_count": next_find_deals_count,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    response = requests.post(
        f"{SUPABASE_URL}/rest/v1/usage_limits",
        headers={
            **get_supabase_headers(),
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        json=payload,
        timeout=10,
    )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=500,
            detail=f"Could not update usage limits: {response.text}",
        )

    return {
        "month_key": month_key,
        "action": action,
        "count": current,
        "limit": limit,
        "remaining": max(limit - current, 0),
        "is_pro": is_pro,
    }


@app.get("/")
def root():
    return {"message": "Home Deal API is running"}


@app.post("/analyze")
def analyze_property(request: AnalyzeRequest):
    address = request.address.strip()

    if not address:
        raise HTTPException(status_code=400, detail="Property address is required.")

    if request.listing_price <= 0:
        raise HTTPException(status_code=400, detail="Listing price must be greater than 0.")

    enforce_usage_limit(
        user_id=request.user_id,
        action="analyze",
        is_pro=request.is_pro,
    )

    result = analyze_single_property(
        address=address,
        listing_price=request.listing_price,
        down_payment_percent=request.down_payment_percent,
        interest_rate=request.interest_rate,
        loan_term_years=request.loan_term_years,
    )

    result = dict(result)
    result["usage"] = increment_usage(
        user_id=request.user_id,
        action="analyze",
        is_pro=request.is_pro,
    )

    return result


@app.post("/find-deals")
def find_deals(request: FindDealsRequest):
    city = request.city.strip()
    state = request.state.strip().upper()
    max_price = request.max_price

    enforce_usage_limit(
        user_id=request.user_id,
        action="find_deals",
        is_pro=request.is_pro,
    )

    if request.is_pro:
        # Cost protection: Pro can see more results, but we still limit expensive full analyses.
        result_limit = min(request.limit, 10)
        search_limit = 25
        max_full_analyses = 10
        plan = "pro"
    else:
        # Cost protection: Free users only trigger a small number of RentCast full analyses.
        result_limit = 3
        search_limit = 10
        max_full_analyses = 3
        plan = "free"

    listings_response = requests.get(
        "https://api.rentcast.io/v1/listings/sale",
        headers=headers,
        params={
            "city": city,
            "state": state,
            "status": "Active",
            "limit": search_limit,
        },
        timeout=20,
    )

    if listings_response.status_code != 200:
        raise HTTPException(status_code=400, detail="Could not retrieve sale listings.")

    listings = listings_response.json()
    deals = []
    analyzed_count = 0

    for listing in listings:
        try:
            if analyzed_count >= max_full_analyses:
                break

            address = listing.get("formattedAddress")
            listing_price = listing.get("price")

            if not address or not listing_price:
                continue

            if listing_price > max_price:
                continue

            analysis = analyze_single_property(address, listing_price)
            analyzed_count += 1

            deals.append({
                "address": analysis["address"],
                "listing_price": analysis["listing_price"],
                "fair_value": analysis["fair_value"],
                "estimated_monthly_rent": analysis["estimated_monthly_rent"],
                "discount_percent": analysis["discount_percent"],
                "gross_rent_yield": analysis["gross_rent_yield"],
                "deal_score": analysis["deal_score"],
                "forecast_score": analysis["forecast_score"],
                "forecast_outlook": analysis["forecast_outlook"],
                "neighborhood_score": analysis["neighborhood_score"],
                "neighborhood_grade": analysis["neighborhood_grade"],
                "expected_appreciation": analysis["expected_appreciation"],
                "confidence_score": analysis["confidence_score"],
                "overall_score": analysis["overall_score"],
                "status": analysis["status"],
                "estimated_monthly_cash_flow": analysis["estimated_monthly_cash_flow"],
                "cache_status": analysis.get("cache_status", "unknown"),
            })

        except Exception:
            continue

    deals = sorted(deals, key=lambda item: item["overall_score"], reverse=True)

    usage = increment_usage(
        user_id=request.user_id,
        action="find_deals",
        is_pro=request.is_pro,
    )

    return {
        "city": city,
        "state": state,
        "max_price": max_price,
        "plan": plan,
        "result_limit": result_limit,
        "search_limit": search_limit,
        "max_full_analyses": max_full_analyses,
        "total_analyzed": len(deals),
        "usage": usage,
        "deals": deals[:result_limit],
    }


@app.post("/run-alerts")
def run_alerts(request: RunAlertsRequest):
    alerts = get_active_alerts(limit=request.max_alerts)

    results = []

    for alert in alerts:
        try:
            city = str(alert.get("city", "")).strip()
            state = str(alert.get("state", "")).strip().upper()
            max_price = int(alert.get("max_price") or 0)
            min_score = int(alert.get("min_score") or 0)

            if not city or not state or max_price <= 0:
                results.append({
                    "alert_id": alert.get("id"),
                    "sent": False,
                    "reason": "Invalid alert data.",
                })
                continue

            # Cost protection: scheduled alerts use the smaller analysis limit.
            # Upgrade this to is_pro=True only after paid plans are active.
            search_request = FindDealsRequest(
                city=city,
                state=state,
                max_price=max_price,
                limit=3,
                is_pro=False,
            )

            search_result = find_deals(search_request)
            matching_deals = [
                deal for deal in search_result["deals"]
                if int(deal.get("overall_score", 0)) >= min_score
            ]

            if not matching_deals:
                results.append({
                    "alert_id": alert.get("id"),
                    "city": city,
                    "state": state,
                    "sent": False,
                    "reason": "No matching deals found.",
                })
                continue

            email_result = send_deal_alert_email(alert, matching_deals)

            results.append({
                "alert_id": alert.get("id"),
                "city": city,
                "state": state,
                "matches": len(matching_deals),
                **email_result,
            })

        except Exception as error:
            results.append({
                "alert_id": alert.get("id"),
                "sent": False,
                "reason": str(error),
            })

    return {
        "alerts_checked": len(alerts),
        "emails_sent": len([item for item in results if item.get("sent")]),
        "results": results,
    }
