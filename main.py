import os
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

API_KEY = os.getenv("RENTCAST_API_KEY")

headers = {
    "X-Api-Key": API_KEY
}

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


class FindDealsRequest(BaseModel):
    city: str
    state: str
    max_price: int
    limit: int = 5
    is_pro: bool = False


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


def analyze_single_property(address, listing_price, down_payment_percent=25, interest_rate=6.5, loan_term_years=30):
    value_response = requests.get(
        "https://api.rentcast.io/v1/avm/value",
        headers=headers,
        params={"address": address},
        timeout=15
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
        timeout=15
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
        loan_term_years
    )

    monthly_property_tax = (listing_price * 0.0125) / 12
    monthly_insurance = (listing_price * 0.0035) / 12
    monthly_maintenance = (listing_price * 0.01) / 12

    monthly_cash_flow = (
        monthly_rent
        - monthly_mortgage
        - monthly_property_tax
        - monthly_insurance
        - monthly_maintenance
    )

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
        monthly_cash_flow
    )

    forecast_score, forecast_outlook, forecast_reasons = calculate_forecast_score(
        discount_percent,
        gross_rent_yield,
        deal_score,
        monthly_cash_flow,
        year_built
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
        "down_payment": round(down_payment, 2),
        "loan_amount": round(loan_amount, 2),
        "monthly_mortgage": round(monthly_mortgage, 2),
        "monthly_property_tax": round(monthly_property_tax, 2),
        "monthly_insurance": round(monthly_insurance, 2),
        "monthly_maintenance": round(monthly_maintenance, 2),
        "estimated_monthly_cash_flow": round(monthly_cash_flow, 2),
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

    return analyze_single_property(
        address=address,
        listing_price=request.listing_price,
        down_payment_percent=request.down_payment_percent,
        interest_rate=request.interest_rate,
        loan_term_years=request.loan_term_years
    )


@app.post("/find-deals")
def find_deals(request: FindDealsRequest):
    city = request.city.strip()
    state = request.state.strip().upper()
    max_price = request.max_price

    if request.is_pro:
        result_limit = min(request.limit, 50)
        search_limit = 100
        plan = "pro"
    else:
        result_limit = 5
        search_limit = 50
        plan = "free"

    listings_response = requests.get(
        "https://api.rentcast.io/v1/listings/sale",
        headers=headers,
        params={
            "city": city,
            "state": state,
            "status": "Active",
            "limit": search_limit
        },
        timeout=20
    )

    if listings_response.status_code != 200:
        raise HTTPException(status_code=400, detail="Could not retrieve sale listings.")

    listings = listings_response.json()
    deals = []

    for listing in listings:
        try:
            address = listing.get("formattedAddress")
            listing_price = listing.get("price")

            if not address or not listing_price:
                continue

            if listing_price > max_price:
                continue

            analysis = analyze_single_property(address, listing_price)

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
                "status": analysis["status"],
                "estimated_monthly_cash_flow": analysis["estimated_monthly_cash_flow"]
            })

        except Exception:
            continue

    deals = sorted(deals, key=lambda item: item["deal_score"], reverse=True)

    return {
        "city": city,
        "state": state,
        "max_price": max_price,
        "plan": plan,
        "result_limit": result_limit,
        "total_analyzed": len(deals),
        "deals": deals[:result_limit]
    }
