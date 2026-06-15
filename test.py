import requests

# =========================
# 설정
# =========================

API_KEY = "77774519c38049f8b70e30782e264f42"

address = "157 Damsel, Irvine, CA 92620"
listing_price = 1490000

headers = {
    "X-Api-Key": API_KEY
}

# =========================
# Fair Value 조회
# =========================

value_url = "https://api.rentcast.io/v1/avm/value"

value_response = requests.get(
    value_url,
    headers=headers,
    params={
        "address": address
    }
)

value_data = value_response.json()

fair_value = value_data["price"]
low_value = value_data["priceRangeLow"]
high_value = value_data["priceRangeHigh"]

# =========================
# Rent Estimate 조회
# =========================

rent_url = "https://api.rentcast.io/v1/avm/rent/long-term"

rent_response = requests.get(
    rent_url,
    headers=headers,
    params={
        "address": address
    }
)

rent_data = rent_response.json()

monthly_rent = rent_data["rent"]
annual_rent = monthly_rent * 12

# =========================
# 계산
# =========================

discount_percent = ((fair_value - listing_price) / fair_value) * 100
gross_rent_yield = (annual_rent / listing_price) * 100

# =========================
# 상태 판정
# =========================

if discount_percent >= 5:
    status = "UNDERVALUED"
elif discount_percent <= -5:
    status = "OVERPRICED"
else:
    status = "FAIR PRICE"

# =========================
# Deal Score 계산
# =========================

score = 0
reasons = []

# 할인율 점수
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

# 렌트 수익률 점수
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

# 건축년도 점수
year_built = value_data["subjectProperty"]["yearBuilt"]

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

# 점수 제한
score = max(0, min(score, 100))

# =========================
# 출력
# =========================

print()
print("===================================")
print("REAL ESTATE DEAL ANALYZER")
print("===================================")

print()
print("Address:")
print(address)

print()
print("Listing Price:")
print(f"${listing_price:,.0f}")

print()
print("AI Fair Value:")
print(f"${fair_value:,.0f}")

print()
print("Fair Value Range:")
print(f"${low_value:,.0f} ~ ${high_value:,.0f}")

print()
print("Estimated Rent:")
print(f"${monthly_rent:,.0f} / month")

print()
print("Discount / Premium:")
print(f"{discount_percent:.2f}%")

print()
print("Gross Rent Yield:")
print(f"{gross_rent_yield:.2f}%")

print()
print("Status:")
print(status)

print()
print("Deal Score:")
print(f"{score}/100")

print()
print("Reasons:")
for reason in reasons:
    print("-", reason)

print()
print("===================================")