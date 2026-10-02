"""Compatibility aliases for older notebooks."""
from backend.api.queries import daily

def query_social_daily_topic_volume(es, from_date, to_date):
    return daily(es, "social/volume", from_date, to_date)

def query_gdelt_daily_volume(es, from_date, to_date):
    return daily(es, "news/volume", from_date, to_date)

def query_oil_daily_brent_price(es, from_date, to_date):
    return daily(es, "oil/prices", from_date, to_date)
