import os
import re
from datetime import datetime, timezone
import requests
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

API_KEY = os.getenv("RENTCAST_API_KEY")
NESTROVA_INTERNAL_API_KEY = os.getenv("NESTROVA_INTERNAL_API_KEY")
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

def rank_listing_candidate(listing, max_price):
    price = float(listing.get("price") or 0)

    if price <= 0:
        return -1000

    if price > float(max_price):
        return -1000

    score = 0.0

    property_type = str(
        listing.get("propertyType") or ""
    ).strip().lower()

    bedrooms = listing.get("bedrooms")
    bathrooms = listing.get("bathrooms")
    square_footage = listing.get("squareFootage")

    # Prefer typical residential inventory first.
    if (
        "single" in property_type
        or "detached" in property_type
    ):
        score += 25

    elif "condo" in property_type:
        score += 22

    elif "town" in property_type:
        score += 21

    elif (
        "multi" in property_type
        or "duplex" in property_type
        or "triplex" in property_type
        or "fourplex" in property_type
    ):
        score += 20

    elif (
        "manufactured" in property_type
        or "mobile" in property_type
    ):
        score += 12

    elif "land" in property_type:
        score -= 30

    elif property_type:
        score += 5

    # Reward listings with enough metadata for better comparison.
    if bedrooms is not None:
        score += 6

    if bathrooms is not None:
        score += 6

    try:
        sqft = float(square_footage or 0)
        if sqft > 0:
            score += 8
    except (TypeError, ValueError):
        pass

    # Slight preference for listings comfortably inside budget.
    budget_ratio = price / max(float(max_price), 1.0)

    if budget_ratio <= 0.85:
        score += 6
    elif budget_ratio <= 0.95:
        score += 3

    return round(score, 2)

def normalize_address(address):
    return re.sub(r"\s+", " ", address.strip().lower())


def make_property_data_cache_key(address):
    normalized_address = normalize_address(address)

    return (
        f"property_data|"
        f"{normalized_address}"
    )

def make_cache_key(address, listing_price, down_payment_percent=25, interest_rate=6.5, loan_term_years=30):
    normalized_address = normalize_address(address)
    return (
        f"{normalized_address}|"
        f"price:{round(float(listing_price), 2)}|"
        f"down:{round(float(down_payment_percent), 2)}|"
        f"rate:{round(float(interest_rate), 3)}|"
        f"term:{int(loan_term_years)}"
    )


def make_find_deals_cache_key(
    city,
    state,
    max_price,
    plan,
):
    normalized_city = re.sub(
        r"\s+",
        " ",
        str(city or "").strip().lower(),
    )

    normalized_state = (
        str(state or "")
        .strip()
        .upper()
    )

    normalized_plan = (
        str(plan or "free")
        .strip()
        .lower()
    )

    return (
        f"find_deals|"
        f"{normalized_city}|"
        f"{normalized_state}|"
        f"{int(float(max_price))}|"
        f"{normalized_plan}"
    )


def get_cached_find_deals(
    user_id,
    search_key,
):
    if (
        not user_id
        or not SUPABASE_URL
        or not SUPABASE_SERVICE_ROLE_KEY
    ):
        return None

    try:
        response = requests.get(
            f"{SUPABASE_URL}/rest/v1/property_search_cache",
            headers=get_supabase_headers(),
            params={
                "user_id": f"eq.{user_id}",
                "search_key": f"eq.{search_key}",
                "select": "result,updated_at",
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
        updated_at = rows[0].get("updated_at")

        if not isinstance(result, dict):
            return None

        if not updated_at:
            return None

        try:
            cached_at = datetime.fromisoformat(
                str(updated_at).replace("Z", "+00:00")
            )

            if cached_at.tzinfo is None:
                cached_at = cached_at.replace(
                    tzinfo=timezone.utc
                )

            cache_age_seconds = (
                datetime.now(timezone.utc) - cached_at
            ).total_seconds()

            if cache_age_seconds > 1800:
                return None

        except Exception:
            return None

        cached_deals = result.get("deals")

        if not isinstance(cached_deals, list):
            return None

        if len(cached_deals) == 0:
            return None

        result = dict(result)
        result["search_cache_status"] = "hit"
        result["usage_charged"] = False

        return result

    except Exception:
        return None


def save_cached_find_deals(
    user_id,
    search_key,
    city,
    state,
    max_price,
    plan,
    result,
):
    if (
        not user_id
        or not SUPABASE_URL
        or not SUPABASE_SERVICE_ROLE_KEY
    ):
        return

    try:
        deals = result.get("deals")

        if not isinstance(deals, list):
            return

        if len(deals) == 0:
            return

        result_to_store = dict(result)
        result_to_store["search_cache_status"] = "stored"

        payload = {
            "user_id": user_id,
            "search_key": search_key,
            "city": city,
            "state": state,
            "max_price": max_price,
            "plan": plan,
            "result": result_to_store,
            "updated_at":
                datetime.now(timezone.utc).isoformat(),
        }

        requests.post(
            f"{SUPABASE_URL}/rest/v1/property_search_cache",
            headers={
                **get_supabase_headers(),
                "Prefer":
                    "resolution=merge-duplicates,return=minimal",
            },
            params={
                "on_conflict":
                    "user_id,search_key",
            },
            json=payload,
            timeout=10,
        )

    except Exception:
        return

def get_property_market_data(address):
    cache_key = make_property_data_cache_key(
        address,
    )

    cached = get_cached_property(
        cache_key,
    )

    if cached:
        return {
            "value_data":
                cached.get("value_data") or {},
            "rent_data":
                cached.get("rent_data") or {},
            "cache_status": "hit",
        }

    value_response = requests.get(
        "https://api.rentcast.io/v1/avm/value",
        headers=headers,
        params={"address": address},
        timeout=15,
    )

    if value_response.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=
                "Could not get fair value data for this address.",
        )

    value_data = value_response.json()

    rent_response = requests.get(
        "https://api.rentcast.io/v1/avm/rent/long-term",
        headers=headers,
        params={"address": address},
        timeout=15,
    )

    if rent_response.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=
                "Could not get rent estimate data for this address.",
        )

    rent_data = rent_response.json()

    payload = {
        "value_data": value_data,
        "rent_data": rent_data,
        "cache_status": "miss",
    }

    save_cached_property(
        cache_key,
        address,
        0,
        payload,
    )

    return payload

def get_cached_property(cache_key):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None

    try:
        response = requests.get(
            f"{SUPABASE_URL}/rest/v1/property_cache",
            headers=get_supabase_headers(),
            params={
                "cache_key": f"eq.{cache_key}",
                "select": "result,updated_at",
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

def build_ranked_comparables(value_data, limit=5):
    subject = value_data.get("subjectProperty") or {}
    raw_comparables = value_data.get("comparables") or []

    subject_sqft = float(
        subject.get("squareFootage") or 0
    )
    subject_year = int(
        subject.get("yearBuilt") or 0
    )
    subject_beds = float(
        subject.get("bedrooms") or 0
    )
    subject_baths = float(
        subject.get("bathrooms") or 0
    )
    subject_type = str(
        subject.get("propertyType") or ""
    ).strip().lower()

    ranked = []

    for comp in raw_comparables:
        try:
            correlation = float(
                comp.get("correlation") or 0
            )
            distance = float(
                comp.get("distance") or 999
            )

            comp_sqft = float(
                comp.get("squareFootage") or 0
            )
            comp_year = int(
                comp.get("yearBuilt") or 0
            )
            comp_beds = float(
                comp.get("bedrooms") or 0
            )
            comp_baths = float(
                comp.get("bathrooms") or 0
            )
            comp_type = str(
                comp.get("propertyType") or ""
            ).strip().lower()

            if subject_sqft > 0 and comp_sqft > 0:
                sqft_similarity = max(
                    0.0,
                    1.0
                    - abs(
                        comp_sqft - subject_sqft
                    )
                    / subject_sqft,
                )
            else:
                sqft_similarity = 0.5

            if subject_year > 0 and comp_year > 0:
                year_similarity = max(
                    0.0,
                    1.0
                    - abs(
                        comp_year - subject_year
                    )
                    / 25.0,
                )
            else:
                year_similarity = 0.5

            bed_similarity = max(
                0.0,
                1.0
                - min(
                    abs(comp_beds - subject_beds),
                    2.0,
                )
                / 2.0,
            )

            bath_similarity = max(
                0.0,
                1.0
                - min(
                    abs(comp_baths - subject_baths),
                    2.0,
                )
                / 2.0,
            )

            room_similarity = (
                bed_similarity
                + bath_similarity
            ) / 2.0

            distance_similarity = max(
                0.0,
                1.0
                - min(distance, 2.0) / 2.0,
            )

            type_similarity = (
                1.0
                if (
                    subject_type
                    and comp_type
                    and subject_type == comp_type
                )
                else 0.6
            )

            similarity_score = round(
                (
                    correlation * 0.35
                    + sqft_similarity * 0.25
                    + year_similarity * 0.12
                    + room_similarity * 0.10
                    + distance_similarity * 0.10
                    + type_similarity * 0.08
                )
                * 100
            )

            ranked.append(
                {
                    "id": comp.get("id"),
                    "address": comp.get(
                        "formattedAddress"
                    ),
                    "city": comp.get("city"),
                    "state": comp.get("state"),
                    "zip_code": comp.get(
                        "zipCode"
                    ),
                    "property_type": comp.get(
                        "propertyType"
                    ),
                    "bedrooms": comp.get(
                        "bedrooms"
                    ),
                    "bathrooms": comp.get(
                        "bathrooms"
                    ),
                    "square_footage": comp.get(
                        "squareFootage"
                    ),
                    "lot_size": comp.get(
                        "lotSize"
                    ),
                    "year_built": comp.get(
                        "yearBuilt"
                    ),
                    "status": comp.get("status"),
                    "price": comp.get("price"),
                    "listing_type": comp.get(
                        "listingType"
                    ),
                    "listed_date": comp.get(
                        "listedDate"
                    ),
                    "removed_date": comp.get(
                        "removedDate"
                    ),
                    "days_on_market": comp.get(
                        "daysOnMarket"
                    ),
                    "distance_miles": comp.get(
                        "distance"
                    ),
                    "days_old": comp.get(
                        "daysOld"
                    ),
                    "rentcast_correlation": comp.get(
                        "correlation"
                    ),
                    "similarity_score": max(
                        0,
                        min(100, similarity_score),
                    ),
                }
            )

        except (TypeError, ValueError):
            continue

    ranked.sort(
        key=lambda item: (
            item["similarity_score"],
            float(
                item.get(
                    "rentcast_correlation"
                )
                or 0
            ),
            -float(
                item.get("distance_miles")
                or 999
            ),
        ),
        reverse=True,
    )

    return ranked[: max(1, int(limit))]


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

def generate_home_report(
    status,
    discount_percent,
    monthly_cash_flow,
    gross_rent_yield,
    overall_score,
):
    if overall_score >= 80:
        recommendation = "BUY"
        label = "Strong Buy"
    elif overall_score >= 65:
        recommendation = "CONSIDER_BUYING"
        label = "Consider Buying"
    elif overall_score >= 45:
        recommendation = "NEGOTIATE"
        label = "Negotiate"
    else:
        recommendation = "PASS"
        label = "Pass"

    strengths = []

    if status == "UNDERVALUED":
        strengths.append("The asking price appears below estimated market value.")

    if gross_rent_yield >= 4:
        strengths.append("The property has healthy rental potential.")

    if monthly_cash_flow >= 0:
        strengths.append("Estimated monthly ownership performs reasonably well.")

    risks = []

    if status == "OVERPRICED":
        risks.append("The asking price appears higher than estimated market value.")

    if monthly_cash_flow < 0:
        risks.append("Estimated monthly ownership cost exceeds rental income.")

    if gross_rent_yield < 3:
        risks.append("Rental demand may be weaker than average.")

    if recommendation == "BUY":
        thesis = (
            "This home appears attractively priced with a healthy overall outlook."
        )

    elif recommendation == "CONSIDER_BUYING":
        thesis = (
            "This home looks like a solid purchase, but you should review financing and monthly costs."
        )

    elif recommendation == "NEGOTIATE":
        thesis = (
            "The property has potential, but negotiating a lower purchase price is recommended."
        )

    else:
        thesis = (
            "There are enough concerns that waiting for a better opportunity may be the safer choice."
        )

    return {
        "recommended_action": recommendation,
        "recommendation_label": label,
        "investment_thesis": thesis,
        "key_strengths": strengths,
        "key_risks": risks,
    }

def generate_negotiation_strategy(
    listing_price,
    fair_value,
    discount_percent,
    comparables=None,
):
    """
    Nestrova Negotiation AI.

    Uses the subject listing price, AI fair value,
    and ranked comparable prices to estimate an
    opening offer, target price, and walk-away price.
    """

    comparable_prices = []

    for comp in comparables or []:
        try:
            price = float(comp.get("price") or 0)

            if price > 0:
                comparable_prices.append(price)

        except (TypeError, ValueError):
            continue

    comparable_prices.sort()

    comparable_median = None

    if comparable_prices:
        midpoint = len(comparable_prices) // 2

        if len(comparable_prices) % 2 == 1:
            comparable_median = comparable_prices[
                midpoint
            ]
        else:
            comparable_median = (
                comparable_prices[midpoint - 1]
                + comparable_prices[midpoint]
            ) / 2

    best_match_price = None

    if comparables:
        try:
            best_match_price = float(
                comparables[0].get("price") or 0
            )

            if best_match_price <= 0:
                best_match_price = None

        except (TypeError, ValueError, IndexError):
            best_match_price = None

    reference_values = [
        float(fair_value),
    ]

    if comparable_median:
        reference_values.append(
            comparable_median
        )

    if best_match_price:
        reference_values.append(
            best_match_price
        )

    market_reference = sum(
        reference_values
    ) / len(reference_values)

    strategy_reasons = []

    if discount_percent >= 5:
        opening_offer = listing_price * 0.975
        target_price = min(
            listing_price * 0.99,
            market_reference,
        )

        walk_away_price = min(
            fair_value * 1.01,
            market_reference * 1.02,
        )

        strategy_reasons.append(
            "The asking price already appears below AI fair value."
        )
        strategy_reasons.append(
            "Use a modest opening discount to avoid losing a strong-value property."
        )

    elif discount_percent >= 0:
        opening_offer = listing_price * 0.955

        target_price = min(
            listing_price * 0.985,
            market_reference,
        )

        walk_away_price = min(
            fair_value,
            market_reference * 1.01,
        )

        strategy_reasons.append(
            "The property appears close to fair value, leaving room for measured negotiation."
        )
        strategy_reasons.append(
            "Comparable pricing should be used to support incremental counteroffers."
        )

    else:
        opening_offer = min(
            listing_price * 0.94,
            fair_value * 0.96,
            market_reference * 0.96,
        )

        target_price = min(
            fair_value * 0.985,
            market_reference * 0.985,
            listing_price * 0.97,
        )

        walk_away_price = min(
            fair_value,
            market_reference,
        )

        strategy_reasons.append(
            "The current asking price appears above AI fair value."
        )
        strategy_reasons.append(
            "The offer should remain disciplined unless inspection or market evidence justifies a premium."
        )

    if comparable_median:
        if listing_price < comparable_median:
            strategy_reasons.append(
                "The subject listing is below the median price of the strongest comparables."
            )
        elif listing_price > comparable_median:
            strategy_reasons.append(
                "The subject listing is above the median price of the strongest comparables."
            )
        else:
            strategy_reasons.append(
                "The subject listing is aligned with the comparable median."
            )

    if best_match_price:
        if listing_price < best_match_price:
            strategy_reasons.append(
                "The best-match comparable is priced above the subject property."
            )
        elif listing_price > best_match_price:
            strategy_reasons.append(
                "The best-match comparable is priced below the subject property."
            )

    opening_offer = max(
        0,
        min(opening_offer, listing_price),
    )

    target_price = max(
        opening_offer,
        min(target_price, listing_price),
    )

    walk_away_price = max(
        target_price,
        walk_away_price,
    )

    estimated_savings = max(
        0,
        listing_price - target_price,
    )

    if walk_away_price < listing_price:
        walk_away_price = min(
            listing_price,
            max(
                walk_away_price,
                target_price,
            ),
        )

    strategy = " ".join(
        strategy_reasons[:3]
    )

    return {
        "suggested_offer": round(
            opening_offer
        ),
        "recommended_target": round(
            target_price
        ),
        "maximum_offer": round(
            walk_away_price
        ),
        "walk_away_price": round(
            walk_away_price
        ),
        "estimated_savings": round(
            estimated_savings
        ),
        "comparable_median": (
            round(comparable_median)
            if comparable_median
            else None
        ),
        "best_match_price": (
            round(best_match_price)
            if best_match_price
            else None
        ),
        "market_reference": round(
            market_reference
        ),
        "comparable_count": len(
            comparable_prices
        ),
        "strategy": strategy,
        "strategy_reasons": strategy_reasons,
    }


def analyze_single_property_uncached(address, listing_price, down_payment_percent=25, interest_rate=6.5, loan_term_years=30):
    market_data = get_property_market_data(
        address,
    )

    value_data = (
        market_data.get("value_data") or {}
    )

    rent_data = (
        market_data.get("rent_data") or {}
    )

    comparables = build_ranked_comparables(
        value_data,
        limit=5,
    )

    fair_value = value_data.get("price")
    low_value = value_data.get("priceRangeLow")
    high_value = value_data.get("priceRangeHigh")

    subject_property = (
        value_data.get("subjectProperty") or {}
    )

    year_built = subject_property.get(
        "yearBuilt",
        1990,
    )

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

    home_report = generate_home_report(
    status,
    discount_percent,
    monthly_cash_flow,
    gross_rent_yield,
    overall_score,
    )

    negotiation = generate_negotiation_strategy(
        listing_price,
        fair_value,
        discount_percent,
        comparables=comparables,
    )

    return {
        "address": address,
        "listing_price": round(listing_price, 2),
        "fair_value": round(fair_value, 2),
        "fair_value_low": round(low_value or fair_value, 2),
        "fair_value_high": round(high_value or fair_value, 2),
        "property_type": subject_property.get("propertyType"),
        "bedrooms": subject_property.get("bedrooms"),
        "bathrooms": subject_property.get("bathrooms"),
        "square_footage": subject_property.get("squareFootage"),
        "year_built": subject_property.get("yearBuilt"),
        "latitude": subject_property.get("latitude"),
        "longitude": subject_property.get("longitude"),
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
        "comparables": comparables,
        "comparable_count": len(comparables),
        "cache_status": "miss",
        "home_report": home_report,
        "negotiation": negotiation,
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

def verify_internal_request(internal_key: str | None):
    if not NESTROVA_INTERNAL_API_KEY:
        return

    if internal_key != NESTROVA_INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid internal API key.")


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
        params={
            "on_conflict": "user_id,month_key",
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
def analyze_property(
    request: AnalyzeRequest,
    x_nestrova_internal_key: str | None = Header(default=None),
):
    verify_internal_request(x_nestrova_internal_key)
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
def find_deals(
    request: FindDealsRequest,
    x_nestrova_internal_key: str | None = Header(default=None),
):
    verify_internal_request(x_nestrova_internal_key)

    city = request.city.strip()
    state = request.state.strip().upper()
    max_price = request.max_price

    plan = (
        "pro"
        if request.is_pro
        else "free"
    )

    search_key = make_find_deals_cache_key(
        city,
        state,
        max_price,
        plan,
    )

    cached_search = get_cached_find_deals(
        request.user_id,
        search_key,
    )

    if cached_search:
        return cached_search

    enforce_usage_limit(
        user_id=request.user_id,
        action="find_deals",
        is_pro=request.is_pro,
    )

    if request.is_pro:
        # Cost protection: Pro can see more results, but we still limit expensive full analyses.
        result_limit = min(request.limit, 10)
        search_limit = 25
        max_new_analyses = 4
        plan = "pro"
    else:
        # Cost protection: Free users only trigger a small number of RentCast full analyses.
        result_limit = 3
        search_limit = 10
        max_new_analyses = 2
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

    print(
        "[FIND_DEALS_RENTCAST]",
        {
            "city": city,
            "state": state,
            "max_price": max_price,
            "status": listings_response.status_code,
            "listing_count": len(listings),
        },
        flush=True,
    )

    eligible_listings = []

    for listing in listings:
        score = rank_listing_candidate(
            listing,
            max_price,
        )

        if score <= -1000:
            continue

        eligible_listings.append(
            {
                "listing": listing,
                "pre_filter_score": score,
            }
        )

    eligible_listings.sort(
        key=lambda item: item["pre_filter_score"],
        reverse=True,
    )

    listings = [
        item["listing"]
        for item in eligible_listings
    ]

    print(
        "[FIND_DEALS_ELIGIBLE]",
        {
            "eligible_count": len(listings),
        },
        flush=True,
    )

    deals = []
    new_analysis_count = 0
    cache_hit_count = 0

    for listing in listings:
        try:
            address = listing.get("formattedAddress")
            listing_price = listing.get("price")

            if not address or not listing_price:
                continue

            if listing_price > max_price:
                continue

            cache_key = make_cache_key(
                address,
                listing_price,
            )

            analysis = get_cached_property(
                cache_key,
            )

            if analysis:
                cache_hit_count += 1

            else:
                if (
                    new_analysis_count >=
                    max_new_analyses
                ):
                    continue

                analysis = analyze_single_property_uncached(
                    address,
                    listing_price,
                )

                save_cached_property(
                    cache_key,
                    address,
                    listing_price,
                    analysis,
                )

                new_analysis_count += 1

            deals.append({
                "address": analysis["address"],
                "listing_price": analysis["listing_price"],
                "property_type": analysis.get("property_type"),
                "bedrooms": analysis.get("bedrooms"),
                "bathrooms": analysis.get("bathrooms"),
                "square_footage": analysis.get("square_footage"),
                "year_built": analysis.get("year_built"),
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

        except Exception as error:
            print(
                "[FIND_DEALS_ANALYSIS_FAILED]",
                {
                    "address": listing.get("formattedAddress"),
                    "price": listing.get("price"),
                    "error_type": type(error).__name__,
                    "error": str(error),
                },
                flush=True,
            )
            continue

    print(
        "[FIND_DEALS_SUMMARY]",
        {
            "eligible_count": len(listings),
            "successful_deals": len(deals),
            "new_analysis_count": new_analysis_count,
            "cache_hit_count": cache_hit_count,
        },
        flush=True,
    )

    deals = sorted(deals, key=lambda item: item["overall_score"], reverse=True)

    usage = increment_usage(
        user_id=request.user_id,
        action="find_deals",
        is_pro=request.is_pro,
    )

    response_payload = {
        "city": city,
        "state": state,
        "max_price": max_price,
        "plan": plan,
        "result_limit": result_limit,
        "search_limit": search_limit,
        "max_new_analyses": max_new_analyses,
        "new_analysis_count": new_analysis_count,
        "cache_hit_count": cache_hit_count,
        "total_analyzed": len(deals),
        "usage": usage,
        "deals": deals[:result_limit],
        "search_cache_status": "miss",
        "usage_charged": True,
    }

    save_cached_find_deals(
        request.user_id,
        search_key,
        city,
        state,
        max_price,
        plan,
        response_payload,
    )

    return response_payload


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
