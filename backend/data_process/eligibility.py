"""Auditable English routing and text features, independent of sentiment model.

Neither language probabilities nor content heuristics are human gold labels.
Retain raw input; exclusions apply only to this processing cohort.
"""
import hashlib
import re

POLICY_VERSION = "global-en-v1"
LANGUAGE_MODEL = "langid-1.1.6"
URL = re.compile(r"https?://\s*\S+", re.IGNORECASE)
TAG = re.compile(r"(?<!\w)#\s*[\w-]+", re.UNICODE)
MENTION = re.compile(r"(?<!\w)@\S+")


def model_text(text):
    """Normalize URL/mention tokens; do not pretend to reconstruct broken URLs."""
    text = URL.sub("http", str(text or ""))
    return re.sub(r"\s+", " ", MENTION.sub("@user", text)).strip()


def prose_text(text):
    return re.sub(r"\s+", " ", TAG.sub(" ", MENTION.sub(" ", URL.sub(" ", str(text or ""))))).strip()


def content_kind(text):
    """Observable text-form hints; 'link_post' is not a verified news label."""
    prose = prose_text(text)
    if re.search(r"\b(?:buy now|shop now|subscribe|sponsored|discount code|download our app)\b", prose, re.I):
        return "promotion_candidate"
    if re.search(r"\b(?:I|I'm|I've|my|we|our)\b", prose, re.I):
        return "commentary_candidate"
    return "link_post" if URL.search(str(text or "")) else "other_text"


class LanguageDetector:
    def __init__(self):
        from langid.langid import LanguageIdentifier, model
        self.identifier = LanguageIdentifier.from_modelstring(model, norm_probs=True)

    def classify(self, text):
        # Long syndicated articles should not dominate routing CPU time.
        language, confidence = self.identifier.classify(text[:4000])
        return language, float(confidence)


def assess(doc, detector, *, threshold=0.8, minimum_words=6):
    text = doc.get("raw_text") or ""
    prose = prose_text(text)
    language = (doc.get("lang") or "").lower().split("-")[0]
    result = {"eligibility_policy": POLICY_VERSION, "language_model": LANGUAGE_MODEL,
              "provider_language": language or "unknown", "content_kind": content_kind(text),
              "detected_language": None, "language_confidence": None,
              "substantive_words": len(re.findall(r"[^\W\d_]+", prose)),
              "text_fingerprint": hashlib.sha256(model_text(text).casefold().encode()).hexdigest()}
    if language != "en":
        reason = "provider_language_unknown" if language in {"", "und", "unknown"} else "provider_non_english"
    elif result["substantive_words"] < minimum_words:
        reason = "insufficient_prose"
    else:
        detected, confidence = detector.classify(prose)
        result.update(detected_language=detected, language_confidence=confidence)
        reason = "language_disagreement" if detected != "en" else "language_uncertain" if confidence < threshold else "eligible"
    result.update(decision="process" if reason == "eligible" else "skip", reason=reason)
    return result
