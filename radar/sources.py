from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Source:
    key: str
    name: str
    region: str
    kind: str
    url: str
    adapter: str
    weight: float = 1.0
    filter_ai: bool = False
    query: str = ""


X_PRIORITY_HANDLES = (
    "karpathy", "swyx", "gregisenberg", "lennysan", "joshwoodward",
    "kevinweil", "petergyang", "thenanyu", "realmadhuguru", "mckaywrigley",
    "stevenbjohnson", "amandaaskell", "boris_cherny", "_catwu", "trq212",
    "googlelabs", "george__mack", "raizamrtn", "amasad", "rauchg",
    "rileybrown", "alexalbert__", "hamelhusein", "levie", "ryolu_",
    "garrytan", "lulumeservey", "pvncher", "mattturck", "joulee",
)

X_AI_TERMS = (
    "(\"AI\" OR \"LLM\" OR \"GPT\" OR \"Claude\" OR \"Gemini\" OR "
    "\"DeepSeek\" OR \"Qwen\" OR \"Cursor\" OR \"Agent\" OR \"MCP\" OR "
    "\"Codex\" OR \"LLMs\" OR \"大模型\" OR \"智能体\" OR \"开源模型\")"
)


def _x_priority_query(handles: tuple[str, ...]) -> str:
    authors = " OR ".join(f"from:{handle}" for handle in handles)
    return f"({authors}) {X_AI_TERMS} -is:retweet -is:reply"


X_PRIORITY_QUERIES = (
    _x_priority_query(X_PRIORITY_HANDLES[:15]),
    _x_priority_query(X_PRIORITY_HANDLES[15:]),
)

# Supplement the user's watchlist with model researchers, independent model
# evaluation and technical practitioners; not a replacement for watched people.
X_RESEARCH_QUERY = _x_priority_query((
    "rasbt", "simonw", "omarsar0", "jeremyphoward", "fchollet",
    "DrJimFan", "Yampeleg", "arena", "ArtificialAnlys",
))

X_AI_QUERY = (
    f"{X_AI_TERMS} (reasoning OR inference OR benchmark OR evaluation OR training "
    "OR workflow OR agents OR research OR 发布 OR 评测 OR 推理 OR 训练) "
    '-is:retweet -is:reply -airdrop -giveaway -presale -"free tokens" -"telegram bot"'
)

X_OFFICIAL_HANDLES = (
    "OpenAI", "OpenAIDevs", "AnthropicAI", "ClaudeAI", "ClaudeDevs",
    "GoogleDeepMind", "GeminiApp", "GoogleAI", "GoogleAIStudio",
)
X_OFFICIAL_QUERY = "(" + " OR ".join(f"from:{h}" for h in X_OFFICIAL_HANDLES) + ") -is:retweet -is:reply"

MODEL_RESEARCH_TERMS = (
    "large language model", "language model", "foundation model", "world model",
    "vision-language", "multimodal model", "generative model", "neural model",
    "model training", "model evaluation", "model architecture", "model inference",
    "llm", "vlm", "transformer", "diffusion", "mixture of experts", "moe",
    "agent", "reasoning", "reinforcement learning", "rlhf", "rlvr",
    "distillation", "fine-tun", "pretrain", "quantization", "token", "embedding",
    "大语言模型", "基础模型", "多模态模型", "生成模型", "模型训练", "模型推理",
    "智能体", "强化学习", "蒸馏", "微调", "量化",
)


def is_model_research(title: str, summary: str = "", tags: tuple[str, ...] | list[str] = ()) -> bool:
    haystack = " ".join((title, summary, *[str(tag) for tag in tags])).lower()
    return any(term in haystack for term in MODEL_RESEARCH_TERMS)


SOURCES = (
    Source("github-llm", "GitHub LLM 新项目", "global", "project", "https://api.github.com/search/repositories", "github", 1.35),
    Source("hf-models", "Hugging Face 模型", "global", "model", "https://huggingface.co/api/models?sort=trendingScore&limit=40&full=true", "huggingface", 1.3),
    Source("hf-spaces", "Hugging Face Spaces", "global", "project", "https://huggingface.co/api/spaces?sort=trendingScore&limit=30&full=true", "huggingface", 1.25),
    Source("hf-papers", "Hugging Face Daily Papers", "global", "paper", "https://huggingface.co/api/daily_papers", "hf_papers", 1.25),
    Source("hacker-news", "Hacker News", "global", "discussion", "https://hn.algolia.com/api/v1/search_by_date", "hackernews", 1.1),
    Source("x-ai", "X AI 热帖", "global", "news", "https://api.x.com/2/tweets/search/recent", "x", 1.1, False, X_AI_QUERY),
    Source("arxiv-ai", "arXiv AI / ML / CL", "global", "paper", "https://export.arxiv.org/api/query", "arxiv", 1.0),
    Source("devto-ai", "DEV Community AI", "global", "discussion", "https://dev.to/api/articles?tag=ai&top=7&per_page=30", "devto", 0.9),
    Source("reddit-ml", "Reddit MachineLearning", "global", "discussion", "https://www.reddit.com/r/MachineLearning/.rss", "reddit", 0.95),
    Source("reddit-localllama", "Reddit LocalLLaMA", "global", "discussion", "https://www.reddit.com/r/LocalLLaMA/.rss", "reddit", 1.0),
    Source("techcrunch-ai", "TechCrunch AI", "global", "news", "https://techcrunch.com/category/artificial-intelligence/feed/", "rss", 1.0),
    Source("venturebeat-ai", "VentureBeat AI", "global", "news", "https://venturebeat.com/category/ai/feed/", "rss", 0.95),
    Source("the-decoder", "The Decoder", "global", "news", "https://the-decoder.com/feed/", "rss", 0.95),
    Source("openai-news", "OpenAI News", "global", "news", "https://openai.com/news/rss.xml", "rss", 1.15),
    Source("deepmind-blog", "Google DeepMind", "global", "news", "https://deepmind.google/blog/rss.xml", "rss", 1.15),
    Source("nvidia-ai", "NVIDIA Deep Learning", "global", "news", "https://blogs.nvidia.com/blog/category/deep-learning/feed/", "rss", 1.0),
    Source("simon-willison", "Simon Willison", "global", "news", "https://simonwillison.net/atom/everything/", "rss", 1.05, True),
    Source("import-ai", "Import AI", "global", "news", "https://importai.substack.com/feed", "rss", 0.9),
    Source("qbitai", "量子位", "china", "news", "https://www.qbitai.com/feed", "rss", 1.15),
    Source("jiqizhixin", "机器之心", "china", "news", "https://www.jiqizhixin.com/rss", "rss", 1.15),
    Source("solidot", "Solidot", "china", "news", "https://www.solidot.org/index.rss", "rss", 0.9, True),
    Source("hellogithub", "HelloGitHub", "china", "project", "https://hellogithub.com/rss", "rss", 0.95, True),
    Source("sspai", "少数派", "china", "news", "https://sspai.com/feed", "rss", 0.9, True),
    Source("ruanyifeng", "阮一峰网络日志", "china", "news", "https://www.ruanyifeng.com/blog/atom.xml", "rss", 0.9, True),
)

SOURCE_BY_KEY = {source.key: source for source in SOURCES}
