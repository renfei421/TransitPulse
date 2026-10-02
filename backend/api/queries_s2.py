"""Compatibility aliases; avg_sentiment now means signed label balance."""
from backend.api.queries import daily

def query_social_daily_sentiment_by_topic(es, from_date, to_date):
    return daily(es, "social/sentiment", from_date, to_date)

def query_news_daily_sentiment(es, from_date, to_date):
    return daily(es, "news/sentiment", from_date, to_date)
