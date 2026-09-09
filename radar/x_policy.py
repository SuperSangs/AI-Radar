"""Deterministic admission and conservative near-duplicate checks for X."""
import re
from difflib import SequenceMatcher

POLICY_VERSION = 2
AI = re.compile(r"\b(ai|llms?|gpt\w*|claude|gemini|openai|anthropic|deepseek|qwen|llama|mistral|agents?|mcp|transformers?|diffusion|codex|cursor)\b|人工智能|大模型|智能体|模型", re.I)
PROMO = re.compile(r"airdrop|giveaway|presale|whitelist|\b(crypto|token sale|settlement layer)\b|positioned above every model|crossed [\d,]+ users|surpassed [\d,]+ users|抽奖|空投|返佣", re.I)
LOW_SIGNAL = re.compile(r"opening.{0,30}office|open.{0,30}international office|we.re hiring|join us at|happy birthday|招聘|节日快乐", re.I)
SALES = re.compile(r"free tokens|claim.{0,40}tokens|below official|why pay full price|telegram bot|the only ai tools list|here are \d+ ai tools|免费额度.{0,30}领取|充值返|邀请码", re.I)


def admissible(text, metrics, *, official=False, priority=False, article=False):
    if PROMO.search(text) or LOW_SIGNAL.search(text):
        return False
    if official:
        return len(text.strip()) >= 20 and bool(AI.search(text) or re.search(r"release|launch|available|introduc|update|\bapi\b|context|reasoning|pricing|发布|上线|更新", text, re.I))
    if not AI.search(text):
        return False
    if SALES.search(text):
        return False
    actions = sum(int(metrics.get(k, 0) or 0) for k in (
        "like_count", "retweet_count", "quote_count", "reply_count", "bookmark_count"
    ))
    # Long text alone and impressions alone cannot qualify a post.
    minimum = 10 if priority else 50
    return len(text.strip()) >= (80 if article else 120) and actions >= minimum


def normalized(text):
    text = re.sub(r"https?://\S+|@\w+", " ", text.lower())
    return re.sub(r"[\W_]+", " ", text).strip()


def duplicate(left, right):
    a, b = normalized(left), normalized(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < 100:
        return False
    # Compare substantive text, not merely a shared URL or topic.
    return SequenceMatcher(None, a[:6000], b[:6000], autojunk=False).ratio() >= .93
