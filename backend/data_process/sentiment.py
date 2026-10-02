"""Versioned sentiment metrics and explicit model selection.

Polarity is signed; confidence is never used as the direction of sentiment.
There is no silent transformer-to-VADER fallback. A failed model load fails the
job so that time series cannot silently combine incompatible scoring systems.
"""
from copy import deepcopy
from collections import OrderedDict
import os
import re

from backend.common.logging import get_logger

MODEL_NAME = "cardiffnlp/twitter-roberta-base-sentiment-latest"
MODEL_REVISION = "3216a57f2a0d9c45a2e6c20157c20c49fb4bf9c7"
PIPELINE_VERSION = "sentiment-v2"
LABELS = ("negative", "neutral", "positive")
log = get_logger("sentiment")


def normalize_text(text):
    text = re.sub(r"https?://\S+|@\S+", "", str(text or "").lower())
    return re.sub(r"\s+", " ", text).strip(" .,!?:;\"'“”‘’()[]{}")


def token_count(text):
    return len(re.findall(r"\w+|[\u4e00-\u9fff]", str(text or "")))


def is_short_reply(text, max_tokens=5, max_chars=35):
    return token_count(text) <= max_tokens and len(str(text or "")) <= max_chars


def detect_reply_relation(text):
    """Conservative short-reply rules. Negation takes precedence over agreement."""
    norm = normalize_text(text)
    if not is_short_reply(norm):
        return "not_short", 1.0
    if re.search(r"\b(?:not|never|don't|do not|cannot|can't)\b.*\b(?:agree|true|correct|right)\b", norm):
        return "disagreement", 0.9
    if re.search(r"\b(?:disagree|wrong|false|nope|nah|nonsense)\b", norm) or norm in {"no", "不同意", "不对", "错"}:
        return "disagreement", 0.9
    if norm in {"agree", "agreed", "exactly", "same", "same here", "this", "true", "yes", "yep", "yeah",
                "correct", "+1", "100%", "同意", "没错", "对", "确实", "是的", "赞同"}:
        return "agreement", 1.0
    return "short_unclear", 0.5


def is_explicit_opinion(text):
    return detect_reply_relation(text)[0] == "not_short" and token_count(text) >= 6


def flip_sentiment(label):
    """Retained for reproducing the historical baseline, not used by v2 inference."""
    return {"positive":"negative", "negative":"positive"}.get(label, "neutral")


def build_context(root_text, anchor_text, parent_text, reply_text):
    # Reserve most of the character budget for the reply; the tokenizer truncates
    # from the left so long ancestors cannot silently remove the target text.
    parts = []
    for tag, value in (("ROOT", root_text), ("ANCESTOR", anchor_text), ("PARENT", parent_text)):
        if value and str(value) not in [p.split("\n", 1)[-1] for p in parts]:
            parts.append(f"[{tag}]\n{str(value)[:500]}")
    parts.append("[REPLY]\n"+str(reply_text or "")[-6000:])
    return "\n\n".join(parts)


class SentimentModel:
    def __init__(self, model_name=MODEL_NAME, use_transformers=True, *, revision=None, batch_size=16,
                 truncation_side="left"):
        if truncation_side not in {"left", "right"} or batch_size < 1:
            raise ValueError("Invalid inference batch size or truncation side")
        if use_transformers and model_name != MODEL_NAME and not (revision or os.getenv("MODEL_REVISION")):
            raise ValueError("Custom models require an explicit pinned MODEL_REVISION")
        self.model_name = model_name if use_transformers else "vaderSentiment"
        self.revision = revision or os.getenv("MODEL_REVISION", MODEL_REVISION)
        self.batch_size = batch_size
        self.pipe = None
        self.vader = None
        self.cache = OrderedDict()
        self.cache_limit = int(os.getenv("SENTIMENT_CACHE_SIZE", "2048"))
        if use_transformers:
            from transformers import pipeline
            self.pipe = pipeline(
                "sentiment-analysis", model=model_name, tokenizer=model_name, revision=self.revision,
                truncation=True, max_length=512, device=-1, top_k=None,
            )
            self.pipe.tokenizer.truncation_side = truncation_side
        else:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            self.vader = SentimentIntensityAnalyzer()
            self.revision = "3.3.2"
        log.info("model_loaded", extra={"fields":{"model":self.model_name, "revision":self.revision}})

    def _result(self, scores):
        mapping = {"label_0":"negative", "label_1":"neutral", "label_2":"positive"}
        probabilities = {mapping.get(x["label"].lower(), x["label"].lower()):float(x["score"]) for x in scores}
        if set(probabilities) != set(LABELS):
            raise ValueError("Model must return negative, neutral and positive probabilities")
        total = sum(probabilities.values())
        if total <= 0 or any(not 0 <= p <= 1 for p in probabilities.values()):
            raise ValueError("Invalid model probabilities")
        probabilities = {k:v/total for k,v in probabilities.items()}
        label = max(probabilities, key=probabilities.get)
        return {
            "label":label, "score":probabilities[label], "confidence":probabilities[label],
            "polarity":probabilities["positive"]-probabilities["negative"],
            "probabilities":probabilities, "model":self.model_name, "revision":self.revision,
            "score_type":"probability" if self.pipe is not None else "lexicon_proportions",
        }

    def classify_many(self, texts):
        texts = [str(t or "").strip() for t in texts]
        missing = list(dict.fromkeys(t for t in texts if t not in self.cache))
        computed = {}
        if self.pipe is not None and missing:
            outputs = self.pipe(missing, batch_size=self.batch_size)
            computed = {text:self._result(scores) for text,scores in zip(missing, outputs)}
        else:
            for text in missing:
                values = self.vader.polarity_scores(text)
                scores = [{"label":label, "score":values[key]} for label,key in zip(LABELS, ("neg","neu","pos"))]
                if not text:
                    scores = [{"label":label,"score":float(label == "neutral")} for label in LABELS]
                result = self._result(scores)
                # VADER compound is explicitly tagged as lexicon polarity.
                result["polarity"] = float(values["compound"])
                result["label"] = "positive" if values["compound"] >= 0.05 else "negative" if values["compound"] <= -0.05 else "neutral"
                result["confidence"] = result["score"] = result["probabilities"][result["label"]]
                computed[text] = result
        output = [deepcopy(self.cache[t] if t in self.cache else computed[t]) for t in texts]
        for text,result in computed.items():
            self.cache[text] = result
            while len(self.cache) > self.cache_limit:
                self.cache.popitem(last=False)
        return output

    def classify(self, text):
        return self.classify_many([text])[0]


def annotate_thread(thread, model, *, use_context=True):
    """Annotate an acyclic discussion tree, preserving direct/inherited topics.

    Agreement inheritance is an explicit heuristic. Disagreement does NOT invert
    sentiment: rejecting an angry claim may itself be angry. Both local and
    contextual outcomes are retained for ablation and human evaluation.
    """
    root = deepcopy(thread)
    entries = []
    stack = [(root, None, 0)]
    visited = set()
    while stack:
        node, parent, depth = stack.pop()
        identifier = node.get("post_id") or id(node)
        if identifier in visited:
            raise ValueError("Cycle or duplicate node in discussion tree")
        visited.add(identifier)
        entries.append((node, parent, depth))
        stack.extend((c, node, depth+1) for c in reversed(node.get("children") or []))
    local = model.classify_many([n.get("raw_text","") for n,_,_ in entries])
    contexts = [build_context(root.get("raw_text"), None, p.get("raw_text") if p else None, n.get("raw_text"))
                for n,p,_ in entries]
    contextual = list(local)
    needed = [i for i,(node,parent,_) in enumerate(entries)
              if use_context and parent is not None and detect_reply_relation(node.get("raw_text"))[0] != "agreement"]
    for i, result in zip(needed, model.classify_many([contexts[i] for i in needed])):
        contextual[i] = result
    rows = []
    for (node,parent,depth), own, ctx, context in zip(entries,local,contextual,contexts):
        relation, relation_confidence = detect_reply_relation(node.get("raw_text"))
        result, method = own, "local_classification"
        if parent is None:
            relation, method = "root", "root_direct_classification"
        elif use_context:
            result, method = ctx, "context_classification"
            if relation == "agreement":
                result, method = parent["_result"], "agreement_inherit_parent"
        node["_result"] = result
        direct = sorted(set(node.get("direct_candidate_topics", node.get("candidate_topics")) or []))
        inherited = sorted(set(node.get("inherited_candidate_topics") or
                               (parent.get("candidate_topics") if parent else []) or []))
        node["candidate_topics"] = sorted(set(direct+inherited))
        node.update(
            local_sentiment_label=own["label"], local_sentiment_score=own["confidence"],
            local_sentiment_polarity=own["polarity"],
            contextual_sentiment_label=result["label"], contextual_sentiment_score=result["confidence"],
            contextual_sentiment_polarity=result["polarity"],
            sentiment_probabilities=result["probabilities"], sentiment_method=method,
            reply_relation=relation, reply_relation_confidence=relation_confidence,
            context_text=context, direct_candidate_topics=direct, inherited_candidate_topics=inherited,
            topic_source="direct" if direct else "inherited" if inherited else "unassigned",
            model_name=result["model"], model_revision=result["revision"], pipeline_version=PIPELINE_VERSION,
            score_type=result["score_type"], schema_version=2,
        )
        row = {k:v for k,v in node.items() if k not in {"children", "_result", "md_depth"}}
        row["parent_post_id"] = parent.get("post_id") if parent else node.get("parent_post_id")
        row["thread_root_id"] = node.get("thread_root_id") or root.get("post_id")
        row["post_type"] = "reply" if row.get("is_reply") or parent else "root"
        row["depth"] = depth
        rows.append(row)
    for node,_,_ in entries:
        node.pop("_result", None)
    return root, rows
